# SPDX-License-Identifier: AGPL-3.0-or-later
"""ai-tracker server: Hack Club Auth accounts, device keys, ingest, public profiles, live stats.

Environment:
  PUBLIC_ORIGIN        e.g. https://ai-tracker.k.hackclub.dev (OAuth redirect is <origin>/auth/callback)
  HCA_CLIENT_ID/SECRET from auth.hackclub.com/developer/apps
  SESSION_SECRET       optional; generated and kept in the database when unset
  DATABASE_URL         postgres://... (SQLite file when unset, for local runs)

Privacy: the server accepts only counts-only, Hackatime-level fields (see EVENT_FIELDS and
LABEL_FIELDS) and drops everything else, even if a client sends it. Prompt text never arrives.
"""
import datetime
import hashlib
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tracker"))
import aitrack  # noqa: E402
import effort  # noqa: E402
from db import DB, dumps  # noqa: E402

AUTH_BASE = "https://auth.hackclub.com"
SCOPES = "openid profile email name slack_id verification_status"
ORIGIN = os.environ.get("PUBLIC_ORIGIN", "http://127.0.0.1:8000").rstrip("/")
REPO_URL = "https://github.com/2Mon/ai-tracker"

EVENT_FIELDS = {
    "session", "project", "prompt_id", "source", "agent_id", "reason", "agent_type",
    "chars", "pasted_chars", "typing_ms", "compose_ms", "keys_typed", "pastes", "fixes", "rewrites", "revisions",
    "pauses", "chars_deleted", "drafts_abandoned", "abandoned_typing_ms",
    "tool", "file", "lines_added", "lines_removed", "response_text_chars", "response_code_lines",
}
EVENT_KINDS = {"session_start", "session_end", "prompt_sent", "ai_edit", "ai_done", "subagent_done", "away"}
LABEL_FIELDS = {"pattern", "pattern_confidence", "pattern_probs", "understanding", "read_previous", "pasted_output",
                "own_thinking", "own_thinking_confidence", "complexity", "model", "at"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_BATCH = 2000

db = DB()


def session_secret():
    """SESSION_SECRET if set, else one generated on first start and kept in the database,
    so sessions survive restarts without anyone handling a secret."""
    if os.environ.get("SESSION_SECRET"):
        return os.environ["SESSION_SECRET"]
    db.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
               ("session_secret", secrets.token_hex(32)))
    return db.one("SELECT value FROM settings WHERE key = %s", ("session_secret",))["value"]


app = FastAPI(title="ai-tracker", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=session_secret(),
                   same_site="lax", https_only=ORIGIN.startswith("https"), max_age=60 * 60 * 24 * 30)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def key_hash(k):
    return hashlib.sha256(k.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- accounts

def current_user(request):
    uid = request.session.get("uid")
    return db.one("SELECT * FROM users WHERE id = %s", (uid,)) if uid else None


def csrf(request):
    t = request.session.get("csrf")
    if not t:
        t = request.session["csrf"] = secrets.token_urlsafe(24)
    return t


def check_csrf(request, token):
    if not token or token != request.session.get("csrf"):
        raise HTTPException(403, "Form expired. Go back, reload and try again.")


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:30] or "hacker"


def unique_username(base):
    name, i = base, 1
    while db.one("SELECT 1 AS x FROM users WHERE username = %s", (name,)):
        i += 1
        name = "%s-%d" % (base, i)
    return name


@app.get("/auth/start")
def auth_start(request: Request):
    missing = [k for k in ("HCA_CLIENT_ID", "HCA_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        return page("Sign-in not set up", "<h1>Sign-in isn't set up yet</h1><p>The server is missing %s. "
                    "Add it as a secret environment variable on the deployment, then try again.</p>"
                    % " and ".join("<code>%s</code>" % k for k in missing))
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    q = urllib.parse.urlencode({"client_id": os.environ["HCA_CLIENT_ID"], "redirect_uri": ORIGIN + "/auth/callback",
                                "response_type": "code", "scope": SCOPES, "state": state})
    return RedirectResponse(AUTH_BASE + "/oauth/authorize?" + q, 302)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    if not code or not state or state != request.session.pop("oauth_state", None):
        raise HTTPException(400, "Sign-in expired. Try again.")
    tok = httpx.post(AUTH_BASE + "/oauth/token", json={
        "grant_type": "authorization_code", "code": code, "client_id": os.environ["HCA_CLIENT_ID"],
        "client_secret": os.environ["HCA_CLIENT_SECRET"], "redirect_uri": ORIGIN + "/auth/callback"}, timeout=20)
    if tok.status_code != 200:
        raise HTTPException(502, "Hack Club Auth refused the sign-in (%d)" % tok.status_code)
    me = httpx.get(AUTH_BASE + "/api/v1/me", headers={"Authorization": "Bearer " + tok.json()["access_token"]},
                   timeout=20)
    if me.status_code != 200:
        raise HTTPException(502, "Couldn't read your Hack Club profile (%d)" % me.status_code)
    ident = me.json().get("identity") or {}
    hca_id = ident.get("id") or ident.get("slack_id") or ident.get("primary_email")
    if not hca_id:
        raise HTTPException(502, "Hack Club Auth returned no identity")
    name = ident.get("name") or " ".join(x for x in (ident.get("first_name"), ident.get("last_name")) if x) or None
    u = db.one("SELECT * FROM users WHERE hca_id = %s", (hca_id,))
    if u:
        db.execute("UPDATE users SET slack_id = %s, name = %s, email = %s WHERE id = %s",
                   (ident.get("slack_id"), name, ident.get("primary_email"), u["id"]))
    else:
        db.execute("INSERT INTO users (hca_id, slack_id, name, email, username, public, created_at) "
                   "VALUES (%s, %s, %s, %s, %s, 1, %s)",
                   (hca_id, ident.get("slack_id"), name, ident.get("primary_email"),
                    unique_username(slugify(name or (ident.get("primary_email") or "").split("@")[0])), now_iso()))
        u = db.one("SELECT * FROM users WHERE hca_id = %s", (hca_id,))
    request.session["uid"] = u["id"]
    return RedirectResponse("/settings" if not db.one("SELECT 1 AS x FROM api_keys WHERE user_id = %s", (u["id"],))
                            else "/u/" + u["username"], 302)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/", 303)


# ---------------------------------------------------------------- settings

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/auth/start", 302)
    new_key = request.session.pop("new_key", None)
    keys = db.query("SELECT id, label, created_at, last_used_at, last_n FROM api_keys "
                    "WHERE user_id = %s AND revoked_at IS NULL ORDER BY id", (u["id"],))
    t = csrf(request)
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%d events</td><td><form method=post action='/settings/keys/%d/revoke'>"
        "<input type=hidden name=csrf_token value='%s'><button class=link>Revoke</button></form></td></tr>"
        % (esc(k["label"] or "device"), esc((k["created_at"] or "")[:10]), esc((k["last_used_at"] or "never")[:16]),
           k["last_n"], k["id"], t) for k in keys) or "<tr><td colspan=5 class=muted>No devices yet.</td></tr>"
    shown = ""
    if new_key:
        shown = """<div class="card good"><h3>Your new device key</h3>
<p>Copy it now: it's shown only once. On the computer you code on, run this and paste the key at the hidden prompt:</p>
<pre>python3 ~/ai-tracker/tracker/aitrack.py connect %s</pre>
<p><code class=key>%s</code></p></div>""" % (esc(ORIGIN), esc(new_key))
    body = """
<h1>Settings</h1>
%s
<div class="card"><h3>Profile</h3>
<form method=post action="/settings/profile" class=row>
<input type=hidden name=csrf_token value="%s">
<label>Username <input name=username value="%s" pattern="[a-z0-9-]{2,30}" required></label>
<label><input type=checkbox name=public value=1 %s> Public profile</label>
<button>Save</button></form>
<p class=muted>Profiles are public by default: hours, estimated effort and project names. Never prompt text.
<a href="/u/%s">View your profile</a></p></div>
<div class="card"><h3>Devices</h3>
<table><tr><th>Device</th><th>Added</th><th>Last sync</th><th>Synced</th><th></th></tr>%s</table>
<form method=post action="/settings/keys" class=row>
<input type=hidden name=csrf_token value="%s">
<input name=label placeholder="Device name, e.g. school laptop" maxlength=60>
<button>Add a device</button></form></div>
<form method=post action="/logout"><input type=hidden name=csrf_token value="%s"><button class=link>Sign out</button></form>
""" % (shown, t, esc(u["username"]), "checked" if u["public"] else "", esc(u["username"]), rows, t, t)
    return page("Settings", body, u)


@app.post("/settings/keys")
def add_key(request: Request, csrf_token: str = Form(""), label: str = Form("")):
    u = current_user(request)
    if not u:
        raise HTTPException(401)
    check_csrf(request, csrf_token)
    k = "ait_" + secrets.token_urlsafe(32)
    db.execute("INSERT INTO api_keys (user_id, key_hash, label, created_at) VALUES (%s, %s, %s, %s)",
               (u["id"], key_hash(k), label.strip()[:60] or None, now_iso()))
    request.session["new_key"] = k
    return RedirectResponse("/settings", 303)


@app.post("/settings/keys/{kid}/revoke")
def revoke_key(kid: int, request: Request, csrf_token: str = Form("")):
    u = current_user(request)
    if not u:
        raise HTTPException(401)
    check_csrf(request, csrf_token)
    db.execute("UPDATE api_keys SET revoked_at = %s WHERE id = %s AND user_id = %s", (now_iso(), kid, u["id"]))
    return RedirectResponse("/settings", 303)


@app.post("/settings/profile")
def save_profile(request: Request, csrf_token: str = Form(""), username: str = Form(""), public: str = Form("")):
    u = current_user(request)
    if not u:
        raise HTTPException(401)
    check_csrf(request, csrf_token)
    name = slugify(username)
    if name != u["username"]:
        if len(name) < 2 or db.one("SELECT 1 AS x FROM users WHERE username = %s", (name,)):
            raise HTTPException(400, "That username is taken or too short.")
    db.execute("UPDATE users SET username = %s, public = %s WHERE id = %s", (name, 1 if public else 0, u["id"]))
    return RedirectResponse("/settings", 303)


# ---------------------------------------------------------------- ingest

def device(request):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing device key")
    k = db.one("SELECT * FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL", (key_hash(auth[7:].strip()),))
    if not k:
        raise HTTPException(401, "unknown or revoked device key")
    return k


def clean_event(e):
    """Keep only allowlisted, well-formed fields. Returns None for anything malformed."""
    if not isinstance(e, dict) or e.get("event") not in EVENT_KINDS:
        return None
    try:
        n = int(e["n"])
        ts = str(e["ts"])
        aitrack.parse_ts(ts)
    except (KeyError, ValueError, TypeError):
        return None
    h, prev = str(e.get("h", "")), e.get("prev")
    if not HEX64.match(h) or (prev is not None and not HEX64.match(str(prev))):
        return None
    data = {}
    for k in EVENT_FIELDS:
        v = e.get(k)
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            data[k] = v
        elif isinstance(v, str):
            data[k] = v[:200]
    return {"n": n, "h": h, "prev": prev, "ts": ts, "kind": e["event"], "project": data.get("project"), "data": data}


@app.post("/api/ingest")
async def ingest(request: Request):
    k = device(request)
    body = await request.json()
    events = body.get("events") or []
    if not isinstance(events, list) or len(events) > MAX_BATCH:
        raise HTTPException(413, "send at most %d events per request" % MAX_BATCH)
    cleaned = sorted((c for c in map(clean_event, events) if c), key=lambda c: c["n"])
    received = now_iso()
    stored = breaks = 0
    with db.transaction():
        cur = db.one("SELECT last_n, last_h FROM api_keys WHERE id = %s", (k["id"],))
        last_n, last_h = cur["last_n"], cur["last_h"]
        for c in cleaned:
            if c["n"] <= last_n:
                continue  # already have it
            linked = last_n == 0 or (c["n"] == last_n + 1 and c["prev"] == last_h)
            breaks += 0 if linked else 1
            db.execute("INSERT INTO events (user_id, key_id, n, h, prev, ts, kind, project, data, received_at) "
                       "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                       (k["user_id"], k["id"], c["n"], c["h"], c["prev"], c["ts"], c["kind"], c["project"],
                        dumps(c["data"]), received))
            last_n, last_h = c["n"], c["h"]
            stored += 1
        if stored:
            db.execute("UPDATE api_keys SET last_n = %s, last_h = %s, last_used_at = %s WHERE id = %s",
                       (last_n, last_h, received, k["id"]))
            db.execute("INSERT INTO heads (key_id, n, h, linked, received_at) VALUES (%s, %s, %s, %s, %s)",
                       (k["id"], last_n, last_h, 0 if breaks else 1, received))
        for lab in (body.get("labels") or [])[:MAX_BATCH]:
            if not isinstance(lab, dict) or not isinstance(lab.get("prompt_id"), str):
                continue
            data = {f: lab[f] for f in LABEL_FIELDS if f in lab}
            db.execute("INSERT INTO labels (user_id, prompt_id, data) VALUES (%s, %s, %s) "
                       "ON CONFLICT (user_id, prompt_id) DO UPDATE SET data = excluded.data",
                       (k["user_id"], lab["prompt_id"][:100], dumps(data)))
        for name, url in (body.get("projects") or {}).items():
            if isinstance(name, str) and isinstance(url, str) and re.match(r"^https://[\w.-]+/[\w./-]+$", url):
                db.execute("INSERT INTO projects (user_id, name, repo_url) VALUES (%s, %s, %s) "
                           "ON CONFLICT (user_id, name) DO UPDATE SET repo_url = excluded.repo_url",
                           (k["user_id"], name[:200], url[:300]))
    _cache.pop(k["user_id"], None)
    return {"stored": stored, "head": {"n": last_n, "h": last_h}, "breaks": breaks}


# ---------------------------------------------------------------- stats

_cache = {}
_cache_lock = threading.Lock()


def user_stats(u):
    """Totals for a user, recomputed when new events arrive."""
    ver = db.one("SELECT COUNT(*) AS n, MAX(received_at) AS r FROM events WHERE user_id = %s", (u["id"],))
    lab = db.one("SELECT COUNT(*) AS n FROM labels WHERE user_id = %s", (u["id"],))
    key = (ver["n"], ver["r"], lab["n"])
    with _cache_lock:
        hit = _cache.get(u["id"])
        if hit and hit[0] == key:
            return hit[1]
    evs = []
    for r in db.query("SELECT n, ts, kind, data FROM events WHERE user_id = %s ORDER BY ts, n", (u["id"],)):
        e = json.loads(r["data"])
        e.update(ts=r["ts"], event=r["kind"], _t=aitrack.parse_ts(r["ts"]), _line=r["n"])
        evs.append(e)
    labels = {r["prompt_id"]: json.loads(r["data"])
              for r in db.query("SELECT prompt_id, data FROM labels WHERE user_id = %s", (u["id"],))}
    rows = aitrack.prompt_rows_from(evs, labels) if evs else []
    per = effort.totals(rows)
    repos = {r["name"]: r["repo_url"] for r in db.query("SELECT name, repo_url FROM projects WHERE user_id = %s",
                                                        (u["id"],))}
    today = datetime.date.today()
    week0, d30 = (today - datetime.timedelta(days=6)).isoformat(), (today - datetime.timedelta(days=29)).isoformat()

    def tot(pred):
        t = {"you_s": 0.0, "ai_s": 0.0, "effort_s": 0.0, "prompts": 0}
        for (day, proj), v in per.items():
            if pred(day):
                for f in t:
                    t[f] += v[f]
        t["observed_s"] = t["you_s"] + t["ai_s"]
        return {f: round(v) for f, v in t.items()}

    days = {}
    for (day, proj), v in per.items():
        if day >= d30:
            d = days.setdefault(day, {"day": day, "you_s": 0.0, "ai_s": 0.0, "effort_s": 0.0})
            for f in ("you_s", "ai_s", "effort_s"):
                d[f] += v[f]
    projects = {}
    for (day, proj), v in per.items():
        p = projects.setdefault(proj, {"name": proj, "repo_url": repos.get(proj), "you_s": 0.0, "ai_s": 0.0,
                                       "effort_s": 0.0, "prompts": 0, "last_day": day})
        for f in ("you_s", "ai_s", "effort_s", "prompts"):
            p[f] += v[f]
        p["last_day"] = max(p["last_day"], day)
    kinds = {}
    for r in rows:
        if r.get("pattern"):
            kinds[r["pattern"]] = kinds.get(r["pattern"], 0) + 1
    last = db.one("SELECT MAX(ts) AS t FROM events WHERE user_id = %s", (u["id"],))["t"]
    live = bool(last) and (time.time() - aitrack.parse_ts(last).timestamp()) < 300
    devices = db.query("SELECT k.id, k.label, k.last_n, "
                       "(SELECT COUNT(*) FROM heads h WHERE h.key_id = k.id) AS heads, "
                       "(SELECT COUNT(*) FROM heads h WHERE h.key_id = k.id AND h.linked = 0) AS breaks "
                       "FROM api_keys k WHERE k.user_id = %s AND k.revoked_at IS NULL", (u["id"],))
    out = {
        "user": {"username": u["username"], "name": u["name"]},
        "live": live, "last_event": last,
        "today": tot(lambda d: d == today.isoformat()), "week": tot(lambda d: d >= week0), "all": tot(lambda d: True),
        "days": [{f: (round(v) if isinstance(v, float) else v) for f, v in d.items()} for d in sorted(days.values(),
                 key=lambda d: d["day"])],
        "projects": sorted(({f: (round(v) if isinstance(v, float) else v) for f, v in p.items()}
                            for p in projects.values()), key=lambda p: -p["effort_s"]),
        "kinds": kinds,
        "judged": sum(1 for r in rows if r.get("pattern")), "prompts": len(rows),
        "audit": {"devices": len(devices), "heads": sum(d["heads"] for d in devices),
                  "breaks": sum(d["breaks"] for d in devices)},
    }
    with _cache_lock:
        _cache[u["id"]] = (key, out)
    return out


def visible_user(request, username):
    u = db.one("SELECT * FROM users WHERE username = %s", (username,))
    me = current_user(request)
    if not u or (not u["public"] and not (me and me["id"] == u["id"])):
        raise HTTPException(404, "No such profile")
    return u, me


@app.get("/api/u/{username}")
def api_user(username: str, request: Request):
    u, _ = visible_user(request, username)
    return JSONResponse(user_stats(u), headers={"Cache-Control": "no-store"})


@app.get("/u/{username}", response_class=HTMLResponse)
def profile(username: str, request: Request):
    u, me = visible_user(request, username)
    body = PROFILE_HTML.replace("__USER__", esc(u["username"])).replace("__NAME__", esc(u["name"] or u["username"]))
    if not u["public"]:
        body = "<p class=muted>Only you can see this profile. Make it public in <a href=/settings>settings</a>.</p>" + body
    return page(u["name"] or u["username"], body, me)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    me = current_user(request)
    since = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
    active = db.query("SELECT DISTINCT u.id FROM users u JOIN events e ON e.user_id = u.id "
                      "WHERE u.public = 1 AND e.ts >= %s", (since,))
    board = []
    for a in active[:200]:
        u = db.one("SELECT * FROM users WHERE id = %s", (a["id"],))
        s = user_stats(u)
        board.append((s["week"]["effort_s"], s["week"]["observed_s"], u, s["live"]))
    board.sort(key=lambda b: -b[0])
    rows = "".join("<tr><td>%s<a href='/u/%s'>%s</a></td><td>%s</td><td>%s</td></tr>"
                   % ("<span class=live title='coding now'></span>" if live else "", esc(u["username"]),
                      esc(u["name"] or u["username"]), hm(e), hm(o))
                   for e, o, u, live in board[:25]) or "<tr><td colspan=3 class=muted>Nobody yet this week.</td></tr>"
    cta = ("<a class=button href='/u/%s'>Your profile</a>" % esc(me["username"])) if me else \
        "<a class=button href='/auth/start'>Sign in with Hack Club</a>"
    body = """
<section class=hero><h1>ai-tracker</h1>
<p>Passive time tracking for building with AI. It counts the hours you put in, not just the hours the AI worked,
and estimates how much of that was real effort: reading, thinking, directing, learning.</p>
<p>%s <a class=link href="%s">Source (AGPLv3)</a></p></section>
<div class=card><h3>This week</h3><table><tr><th>Who</th><th>Estimated effort</th><th>Observed</th></tr>%s</table></div>
<div class=card><h3>Privacy</h3><p>Synced: the same kind of data Hackatime heartbeats carry (project, file path,
line counts, prompt length, timings) plus each prompt's effort label. Never prompt text, code or AI output.</p></div>
""" % (cta, REPO_URL, rows)
    return page("ai-tracker", body, me)


@app.get("/healthz")
def healthz():
    db.one("SELECT 1 AS x")
    return {"ok": True}


# ---------------------------------------------------------------- html

def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def hm(s):
    s = int(s or 0)
    return "%dh %02dm" % (s // 3600, s % 3600 // 60) if s >= 3600 else "%dm" % (s // 60)


def page(title, body, me=None):
    nav = ("<a href='/u/%s'>%s</a> · <a href='/settings'>Settings</a>" % (esc(me["username"]), esc(me["username"]))
           if me else "<a href='/auth/start'>Sign in</a>")
    return HTMLResponse("""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>%s · ai-tracker</title><style>%s</style></head>
<body><header><a href="/" class=brand>ai-tracker</a><nav>%s</nav></header><main>%s</main>
<footer>AGPLv3 · <a href="%s">source</a></footer></body></html>""" % (esc(title), CSS, nav, body, REPO_URL))


CSS = """
:root{--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--effort:#cb30e0;--you:#0088ff;--ai:#8e8e93;--good:#e9f9ee}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f5f5f7;--muted:#98989d;--line:#38383a;--good:#0f2a17}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;max-width:980px;margin:auto}
.brand{font-weight:700;color:var(--text);text-decoration:none}main{max-width:980px;margin:auto;padding:0 16px 40px}
a{color:var(--you)}.muted{color:var(--muted)}footer{text-align:center;color:var(--muted);padding:20px;font-size:13px}
.card{background:var(--card);border-radius:14px;padding:16px 18px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.card.good{background:var(--good)}h1{font-size:30px;margin:10px 0}h3{margin:0 0 8px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:500;font-size:13px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:8px 0}
input{font:inherit;padding:6px 9px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text)}
button,.button{font:inherit;background:var(--you);color:#fff;border:0;border-radius:9px;padding:7px 14px;cursor:pointer;text-decoration:none;display:inline-block}
button.link{background:none;color:var(--you);padding:0}pre,code.key{background:var(--bg);padding:8px 10px;border-radius:8px;overflow-x:auto;display:block;word-break:break-all}
.hero p{max-width:640px}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr));gap:12px}
.tile .v{font-size:26px;font-weight:650}.tile .l{color:var(--muted);font-size:13px}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:#30d158;margin-right:6px;box-shadow:0 0 0 3px rgba(48,209,88,.25)}
.legend span{margin-right:14px;font-size:13px;color:var(--muted)}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
svg text{fill:var(--muted);font-size:10px}
"""

PROFILE_HTML = """
<h1 id=title><span id=live></span>__NAME__</h1>
<p class=muted>@__USER__ · <span id=last></span></p>
<div class=tiles>
 <div class="card tile"><div class=l>Estimated effort, 7 days</div><div class=v id=weekEffort>–</div></div>
 <div class="card tile"><div class=l>Observed, 7 days</div><div class=v id=weekObs>–</div></div>
 <div class="card tile"><div class=l>Today</div><div class=v id=today>–</div></div>
 <div class="card tile"><div class=l>All-time effort</div><div class=v id=allEffort>–</div></div>
</div>
<div class=card><h3>Last 30 days</h3><div id=chart></div>
<div class=legend><span><i style="background:var(--effort)"></i>Estimated effort</span><span><i style="background:var(--you)"></i>Your other time</span><span><i style="background:var(--ai)"></i>AI working alone</span></div></div>
<div class=card><h3>Projects</h3><table id=projects></table></div>
<div class=card><h3>How it's counted</h3><p class=muted id=how></p></div>
<script>
const user = "__USER__";
const hm = (s) => { s = Math.round(s || 0); return s >= 3600 ? `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m` : `${Math.floor(s / 60)}m`; };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function chart(days) {
  const W = 900, H = 160, n = 30, bw = W / n;
  const by = Object.fromEntries(days.map((d) => [d.day, d]));
  const max = Math.max(3600, ...days.map((d) => d.you_s + d.ai_s));
  let g = "";
  for (let i = 0; i < n; i++) {
    const day = new Date(Date.now() - (n - 1 - i) * 864e5).toISOString().slice(0, 10), d = by[day];
    if (d) {
      let y = H - 16;
      for (const [v, c] of [[d.effort_s, "var(--effort)"], [d.you_s - d.effort_s, "var(--you)"], [d.ai_s, "var(--ai)"]]) {
        const h = Math.max(0, v) / max * (H - 24); y -= h;
        if (h > 0) g += `<rect x="${i * bw + 2}" y="${y}" width="${bw - 4}" height="${h}" rx="2" fill="${c}"><title>${day}: ${hm(v)}</title></rect>`;
      }
    }
    if (i % 5 === 0) g += `<text x="${i * bw + 2}" y="${H - 3}">${day.slice(5)}</text>`;
  }
  document.getElementById("chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Time per day">${g}</svg>`;
}
async function load() {
  const r = await fetch(`/api/u/${user}`, { cache: "no-store" });
  if (!r.ok) return;
  const s = await r.json();
  document.getElementById("live").innerHTML = s.live ? "<span class=live title='coding now'></span>" : "";
  document.getElementById("last").textContent = s.last_event ? (s.live ? "coding now" : "last active " + new Date(s.last_event).toLocaleString()) : "no activity yet";
  document.getElementById("weekEffort").textContent = hm(s.week.effort_s);
  document.getElementById("weekObs").textContent = hm(s.week.observed_s);
  document.getElementById("today").textContent = hm(s.today.effort_s) + " effort";
  document.getElementById("allEffort").textContent = hm(s.all.effort_s);
  chart(s.days);
  document.getElementById("projects").innerHTML = "<tr><th>Project</th><th>Estimated effort</th><th>Your time</th><th>AI alone</th><th>Prompts</th><th>Last</th></tr>" +
    (s.projects.map((p) => `<tr><td>${p.repo_url ? `<a href="${esc(p.repo_url)}" rel="nofollow noopener">${esc(p.name)}</a>` : esc(p.name)}</td><td>${hm(p.effort_s)}</td><td>${hm(p.you_s)}</td><td>${hm(p.ai_s)}</td><td>${p.prompts}</td><td>${esc(p.last_day)}</td></tr>`).join("") || "<tr><td class=muted colspan=6>No projects yet.</td></tr>");
  document.getElementById("how").textContent = `${s.judged} of ${s.prompts} prompts judged by Jev. Observed time counts your reading, thinking and typing between prompts plus the AI working alone, each moment once. Estimated effort weights your time by what each prompt was doing; it's an unvalidated estimate. Audit: ${s.audit.devices} device(s), ${s.audit.heads} log heads recorded on arrival, ${s.audit.breaks} chain break(s).`;
}
load(); setInterval(load, 15000);
</script>
"""
