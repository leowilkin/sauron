# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sauron (ai-tracker) server: Hack Club Auth accounts, device keys, ingest, public profiles, live stats.

Environment:
  PUBLIC_ORIGIN        e.g. https://sauron.k.hackclub.dev (OAuth redirect is <origin>/auth/callback)
  HCA_CLIENT_ID/SECRET from auth.hackclub.com/developer/apps
  SESSION_SECRET       optional; generated and kept in the database when unset
  ADMINS               comma-separated Hack Club Auth ids or Slack ids who are always admins (not usernames:
                       anyone can pick a username that isn't taken)
  TYPESAFE_API_KEY     the one TypeSafe API key used to label everyone's prompts (POST /api/label)
  LABEL_DAILY_LIMIT    most prompts labelled per user per day with that key (default 1500)
  DATABASE_URL         postgres://... (SQLite file when unset, for local runs)

Privacy: the server accepts only counts-only, Hackatime-level fields (see EVENT_FIELDS and
clean_label) and drops everything else, even if a client sends it. Prompt text never arrives.
"""
import datetime
import hashlib
import html
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tracker"))
import aitrack  # noqa: E402
import effort  # noqa: E402
import sync  # noqa: E402  (shared path rules)
import jev_judge  # noqa: E402
from db import DB, dumps  # noqa: E402

AUTH_BASE = "https://auth.hackclub.com"
SCOPES = "openid profile email name slack_id verification_status"
ORIGIN = os.environ.get("PUBLIC_ORIGIN", "http://127.0.0.1:8000").rstrip("/")
REPO_URL = "https://github.com/2Mon/sauron"

EVENT_FIELDS = {
    "session", "project", "prompt_id", "source", "agent_id", "reason", "agent_type",
    "chars", "pasted_chars", "typing_ms", "compose_ms", "keys_typed", "pastes", "fixes", "rewrites", "revisions",
    "pauses", "chars_deleted", "drafts_abandoned", "abandoned_typing_ms",
    "tool", "file", "lines_added", "lines_removed", "response_text_chars", "response_code_lines",
}
EVENT_KINDS = {"session_start", "session_end", "prompt_sent", "ai_edit", "ai_done", "subagent_done", "away"}
PATTERNS = set(jev_judge.QUESTIONS["pattern"]["criteria"])
MAX_SCORE = max(len(q.get("criteria") or ()) for q in jev_judge.QUESTIONS.values() if q["type"] == "score")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IMPORTED = "claude-code-import"  # the source the importer gives its events
MAX_BATCH = 2000

db = DB()


def _scrub_outside_paths():
    """One-off cleanup for events stored before paths outside the project were refused."""
    rows = db.query("SELECT key_id, n, data FROM events WHERE kind = %s AND data LIKE %s", ("ai_edit", '%"file":%'))
    for r in rows:
        d = json.loads(r["data"])
        if not sync.inside_project(d.get("file")):
            d["file"] = sync.OUTSIDE
            db.execute("UPDATE events SET data = %s WHERE key_id = %s AND n = %s", (dumps(d), r["key_id"], r["n"]))


_scrub_outside_paths()


def session_secret():
    """SESSION_SECRET if set, else one generated on first start and kept in the database,
    so sessions survive restarts without anyone handling a secret."""
    if os.environ.get("SESSION_SECRET"):
        return os.environ["SESSION_SECRET"]
    db.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
               ("session_secret", secrets.token_hex(32)))
    return db.one("SELECT value FROM settings WHERE key = %s", ("session_secret",))["value"]


app = FastAPI(title="Sauron", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def canonical_host(request: Request, call_next):
    """Send browsers on an old address (ai-tracker.k.hackclub.dev) to the current one. The API keeps
    answering on every address, so devices connected before a rename keep syncing."""
    want = urllib.parse.urlsplit(ORIGIN).netloc
    host = request.headers.get("host", "")
    if want and host and host != want and not host.startswith(("127.0.0.1", "localhost", "testserver")) \
            and not request.url.path.startswith(("/api/", "/healthz")):
        q = ("?" + request.url.query) if request.url.query else ""
        return RedirectResponse(ORIGIN + request.url.path + q, 301)
    return await call_next(request)
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


def env_admin(u):
    """Admins named in the ADMINS setting. They can't be removed from the site, only from the server settings.
    Matched on ids Hack Club Auth sets, never on usernames, which users choose themselves."""
    names = {x.strip().lower() for x in os.environ.get("ADMINS", "").split(",") if x.strip()}
    return bool(u) and bool(names & ({str(u.get(k) or "").lower() for k in ("hca_id", "slack_id")} - {""}))


def is_admin(u):
    return bool(u) and (bool(u.get("is_admin")) or env_admin(u))


def admin_log(actor, action, target=None):
    db.execute("INSERT INTO admin_log (actor_id, action, target_id, at) VALUES (%s, %s, %s, %s)",
               (actor["id"], action, target["id"] if target else None, now_iso()))


def require_admin(request):
    me = current_user(request)
    if not me:
        raise HTTPException(401, "Sign in first")
    if not is_admin(me):
        raise HTTPException(404, "Not found")
    return me


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
        return page("Sign-in Isn't Set Up", '<div class="panel"><p class="sub">The server is missing %s. Add it as a '
                    'secret environment variable on the deployment, then try again.</p></div>'
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
        """<tr><td><div class="sname"><b>%s</b><span>added %s</span></div></td><td class="quiet">%s</td>
<td class="quiet">%s events</td><td style="text-align:right"><form method="post" action="/settings/keys/%d/revoke">
<input type="hidden" name="csrf_token" value="%s"><button class="plain danger">Revoke</button></form></td></tr>"""
        % (esc(k["label"] or "Unnamed device"), esc(day_label(k["created_at"])),
           esc("synced " + ago(k["last_used_at"]) if k["last_used_at"] else "never synced"), "{:,}".format(k["last_n"]),
           k["id"], t) for k in keys)
    shown = ""
    if new_key:
        shown = """<div class="panel keypanel"><div class="phead"><h2>Finish connecting this device</h2>
<span class="meta">The key is shown once</span></div>
<p class="sub">Run this on the computer you code on, then paste the key at the hidden prompt. No <code>sauron</code> command yet? Use <code>python3 ~/sauron/tracker/aitrack.py</code> instead.</p>
<pre class="cmd">sauron connect %s</pre>
<div class="keyrow"><code id="key">%s</code><button class="glass" type="button" onclick="navigator.clipboard.writeText(document.getElementById('key').textContent);this.textContent='Copied'">Copy</button></div></div>""" % (esc(ORIGIN), esc(new_key))
    device_table = ("""<div class="table-scroll"><table class="stable"><thead><tr><th>Device</th><th>Last sync</th><th>Sent</th><th></th></tr></thead>
<tbody>%s</tbody></table></div>""" % rows) if keys else \
        '<p class="empty">No devices yet. Add one, then run the command it shows on that computer.</p>'
    body = """%s
<div class="panel"><div class="phead"><h2>Devices</h2><span class="meta">%s</span></div>
%s
<form method="post" action="/settings/keys" class="inline-form">
<input type="hidden" name="csrf_token" value="%s">
<input name="label" placeholder="Device name, like school laptop" maxlength="60" aria-label="Device name">
<button class="btn">Add Device</button></form></div>
<div class="panel"><div class="phead"><h2>Profile</h2></div>
<form method="post" action="/settings/profile" class="fields">
<input type="hidden" name="csrf_token" value="%s">
<label>Username<input name="username" value="%s" pattern="[a-z0-9-]{2,30}" required></label>
<label class="check-row"><input type="checkbox" name="public" value="1" %s><span>Show my profile to everyone who can open this site (admins can always see it)</span></label>
<div><button class="btn">Save</button></div></form></div>
<div class="privacy-line"><img src="/assets/icon-lock.svg" alt=""><span>Devices send counts, timings, project names and effort labels. Never prompt text, code or AI output. Admins of this server can see all synced data, including private profiles.</span>
<form method="post" action="/logout"><input type="hidden" name="csrf_token" value="%s"><button class="plain">Sign Out</button></form></div>
""" % (shown, "%d connected" % len(keys) if keys else "none yet", device_table, t, t, esc(u["username"]),
       "checked" if u["public"] else "", t)
    return page("Devices & Settings", body, me=u, active="settings", sub="Connect computers and choose who sees your profile")


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
    c = clean_fields(e)
    if c is None:
        return None
    try:
        n = int(e["n"])
    except (KeyError, ValueError, TypeError):
        return None
    h, prev = str(e.get("h", "")), e.get("prev")
    if not HEX64.match(h) or (prev is not None and not HEX64.match(str(prev))):
        return None
    return dict(c, n=n, h=h, prev=prev)


def clean_fields(e):
    """{ts, kind, project, data} with only allowlisted, well-formed fields, or None. Shared by live and imported."""
    if not isinstance(e, dict) or e.get("event") not in EVENT_KINDS:
        return None
    try:
        ts = str(e["ts"])
        aitrack.parse_ts(ts)
    except (KeyError, ValueError, TypeError):
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
    if "file" in data and not sync.inside_project(data["file"]):
        data["file"] = sync.OUTSIDE  # never store paths outside the project: they can name the user's folders
    return {"ts": ts, "kind": e["event"], "project": data.get("project"), "data": data}


def _num(v, lo, hi):
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, (int, float)) and math.isfinite(v):
        return min(max(v, lo), hi)
    return None


def clean_label(lab):
    """Keep only well-typed label fields, within the ranges Jev answers in. A malformed label from a device
    must not break the stats it feeds (and with them the leaderboard everyone loads)."""
    if not isinstance(lab, dict):
        return {}
    out = {}
    if lab.get("pattern") in PATTERNS:
        out["pattern"] = lab["pattern"]
    probs = lab.get("pattern_probs")
    if isinstance(probs, dict):
        probs = {k: _num(v, 0, 1) for k, v in probs.items() if k in PATTERNS}
        probs = {k: v for k, v in probs.items() if v is not None}
        if probs:
            out["pattern_probs"] = probs
    for f in ("pattern_confidence", "own_thinking_confidence", "understanding", "read_previous", "pasted_output"):
        if _num(lab.get(f), 0, 1) is not None:
            out[f] = _num(lab[f], 0, 1)
    for f in ("own_thinking", "complexity"):
        if _num(lab.get(f), 0, MAX_SCORE) is not None:
            out[f] = _num(lab[f], 0, MAX_SCORE)
    for f, lim in (("model", 100), ("at", 40)):
        if isinstance(lab.get(f), str):
            out[f] = lab[f][:lim]
    return out


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
            data = clean_label(lab)
            db.execute("INSERT INTO labels (user_id, prompt_id, data) VALUES (%s, %s, %s) "
                       "ON CONFLICT (user_id, prompt_id) DO UPDATE SET data = excluded.data",
                       (k["user_id"], lab["prompt_id"][:100], dumps(data)))
        for name, url in (body.get("projects") or {}).items():
            if isinstance(name, str) and isinstance(url, str) and re.match(r"^https://[\w.-]+/[\w./-]+$", url):
                db.execute("INSERT INTO projects (user_id, name, repo_url) VALUES (%s, %s, %s) "
                           "ON CONFLICT (user_id, name) DO UPDATE SET repo_url = excluded.repo_url",
                           (k["user_id"], name[:200], url[:300]))
    _forget(k["user_id"])
    return {"stored": stored, "head": {"n": last_n, "h": last_h}, "breaks": breaks}


@app.post("/api/import")
async def import_history(request: Request):
    """History a device backfilled from Claude Code transcripts. Same fields as /api/ingest, no hash chain.
    Stored apart, deduplicated by content, shown on profiles as imported and never ranked."""
    k = device(request)
    body = await request.json()
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list) or len(events) > MAX_BATCH:
        raise HTTPException(413, "send at most %d events per request" % MAX_BATCH)
    received, stored = now_iso(), 0
    with db.transaction():
        for c in filter(None, map(clean_fields, events)):
            c["data"]["source"] = IMPORTED  # whatever the device said, this is imported history
            d = dumps(c["data"])
            h = hashlib.sha256(("%s\n%s\n%s" % (c["kind"], c["ts"], d)).encode("utf-8")).hexdigest()
            stored += db.execute("INSERT INTO imported_events (user_id, key_id, k, ts, kind, session, project, data, "
                                 "received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                 (k["user_id"], k["id"], h, c["ts"], c["kind"], c["data"].get("session"),
                                  c["project"], d, received)) or 0
    _forget(k["user_id"])
    return {"stored": stored}


# ---------------------------------------------------------------- labelling with the shared key

STATE_LIMITS = {"prompt": jev_judge.MAX_PROMPT, "ai_previous_reply_end": jev_judge.MAX_PREV,
                "previous_prompt": jev_judge.MAX_PREV_PROMPT}


@app.post("/api/label")
async def label(request: Request):
    """Label one prompt with Jev using the server's key. The text is used for this one call and never
    stored or logged; only the label is kept, exactly as if the device had sent it."""
    k = device(request)
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise HTTPException(503, "labelling isn't set up on this server")
    body = await request.json()
    pid, state = body.get("prompt_id"), body.get("state")
    if not isinstance(pid, str) or not pid or len(pid) > 100 or not isinstance(state, dict) \
            or not isinstance(state.get("prompt"), str):
        raise HTTPException(400, "send prompt_id and state.prompt")
    have = db.one("SELECT data FROM labels WHERE user_id = %s AND prompt_id = %s", (k["user_id"], pid))
    if have:
        return json.loads(have["data"])  # already labelled: no second call
    clean = {f: jev_judge.redact(state[f][-lim:] if f == "ai_previous_reply_end" else state[f][:lim])
             for f, lim in STATE_LIMITS.items() if isinstance(state.get(f), str) and state[f].strip()}
    day = datetime.date.today().isoformat()
    used = db.one("SELECT n FROM label_usage WHERE user_id = %s AND day = %s", (k["user_id"], day))
    if used and used["n"] >= int(os.environ.get("LABEL_DAILY_LIMIT", "1500")):
        raise HTTPException(429, "daily labelling limit reached; it resets tomorrow")
    db.execute("INSERT INTO label_usage (user_id, day, n) VALUES (%s, %s, 1) "
               "ON CONFLICT (user_id, day) DO UPDATE SET n = label_usage.n + 1", (k["user_id"], day))
    from starlette.concurrency import run_in_threadpool
    try:
        res = await run_in_threadpool(jev_judge.ask_jev, key, clean, 2)
    except Exception:
        raise HTTPException(502, "Jev didn't answer; try again later")  # no detail: it could echo the text
    lab = dict(jev_judge.flatten(res.get("answers")), model=res.get("model"))
    if isinstance(body.get("at"), str):
        lab["at"] = body["at"][:40]
    data = clean_label(lab)
    db.execute("INSERT INTO labels (user_id, prompt_id, data) VALUES (%s, %s, %s) "
               "ON CONFLICT (user_id, prompt_id) DO UPDATE SET data = excluded.data", (k["user_id"], pid, dumps(data)))
    _forget(k["user_id"])
    return data


# ---------------------------------------------------------------- stats

_cache = {}
_cache_lock = threading.Lock()


def _forget(uid):
    with _cache_lock:
        for history in (False, True):
            _cache.pop((uid, history), None)


def _imported_events(uid, live_sessions):
    """Backfilled history, minus sessions the live log also has: those are already counted, with better data."""
    out = []
    for i, r in enumerate(db.query("SELECT ts, kind, session, data FROM imported_events WHERE user_id = %s "
                                   "ORDER BY ts", (uid,))):
        if r["session"] in live_sessions:
            continue
        e = json.loads(r["data"])
        e.update(ts=r["ts"], event=r["kind"], _t=aitrack.parse_ts(r["ts"]), _line=i)
        out.append(e)
    return out


def user_stats(u, history=False):
    """Totals for a user, recomputed when new events arrive. Live, hash-chained events only, unless history:
    then imported transcript history is added (for the profile; the leaderboard never includes it)."""
    ver = db.one("SELECT COUNT(*) AS n, MAX(received_at) AS r FROM events WHERE user_id = %s", (u["id"],))
    lab = db.one("SELECT COUNT(*) AS n FROM labels WHERE user_id = %s", (u["id"],))
    imp = db.one("SELECT COUNT(*) AS n, MAX(received_at) AS r FROM imported_events WHERE user_id = %s",
                 (u["id"],)) if history else None
    key = (ver["n"], ver["r"], lab["n"], imp and (imp["n"], imp["r"]))
    with _cache_lock:
        hit = _cache.get((u["id"], history))
        if hit and hit[0] == key:
            return hit[1]
    evs = []
    for r in db.query("SELECT n, ts, kind, data FROM events WHERE user_id = %s ORDER BY ts, n", (u["id"],)):
        e = json.loads(r["data"])
        e.update(ts=r["ts"], event=r["kind"], _t=aitrack.parse_ts(r["ts"]), _line=r["n"])
        evs.append(e)
    imported = set()
    if history:
        extra = _imported_events(u["id"], {e.get("session") for e in evs})
        imported = {e.get("session") for e in extra}
        evs.extend(extra)
    labels = {r["prompt_id"]: clean_label(json.loads(r["data"]))  # also covers labels stored before checks existed
              for r in db.query("SELECT prompt_id, data FROM labels WHERE user_id = %s", (u["id"],))}
    rows = aitrack.prompt_rows_from(evs, labels) if evs else []
    per = effort.totals(rows)
    repos = {r["name"]: r["repo_url"] for r in db.query("SELECT name, repo_url FROM projects WHERE user_id = %s",
                                                        (u["id"],))}
    today = datetime.date.today()
    week0 = (today - datetime.timedelta(days=6)).isoformat()

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
    alloc = effort.allocate(rows)
    sess = {}
    for r in rows:
        k = r.get("session") or "?"
        you, ai = alloc.get(id(r), (0.0, 0.0))
        x = sess.setdefault(k, {"start": r["at"], "end": r["at"], "project": r.get("project") or "?", "prompts": 0,
                                "you_s": 0.0, "ai_s": 0.0, "effort_s": 0.0, "judged": 0, "_p": {},
                                "imported": k in imported})
        x["_p"][r.get("project") or "?"] = x["_p"].get(r.get("project") or "?", 0) + 1
        x["start"], x["end"] = min(x["start"], r["at"]), max(x["end"], r["at"])
        x["prompts"] += 1
        x["you_s"] += you
        x["ai_s"] += ai
        x["effort_s"] += you * effort.weight(r)
        x["judged"] += 1 if r.get("pattern") else 0
    for x in sess.values():
        x["project"] = max(x.pop("_p").items(), key=lambda kv: kv[1])[0]
    recent = sorted(sess.values(), key=lambda x: x["end"], reverse=True)[:12]
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
        "sessions": [{f: (round(v) if isinstance(v, float) else v) for f, v in x.items()} for x in recent],
        "judged": sum(1 for r in rows if r.get("pattern")), "prompts": len(rows),
        "audit": {"devices": len(devices), "heads": sum(d["heads"] for d in devices),
                  "breaks": sum(d["breaks"] for d in devices)},
    }
    if history:
        t = {"sessions": len(imported), "prompts": 0, "you_s": 0.0, "effort_s": 0.0}
        for r in rows:
            if r.get("session") in imported:
                you = alloc.get(id(r), (0.0, 0.0))[0]
                t["prompts"] += 1
                t["you_s"] += you
                t["effort_s"] += you * effort.weight(r)
        out["imported"] = {f: round(v) for f, v in t.items()}
    with _cache_lock:
        _cache[(u["id"], history)] = (key, out)
    return out


def visible_user(request, username):
    u = db.one("SELECT * FROM users WHERE username = %s", (username,))
    me = current_user(request)
    if not u or (not u["public"] and not (me and me["id"] == u["id"]) and not is_admin(me)):
        raise HTTPException(404, "No such profile")
    return u, me


@app.get("/api/u/{username}")
def api_user(username: str, request: Request):
    u, _ = visible_user(request, username)
    return JSONResponse(user_stats(u, history=True), headers={"Cache-Control": "no-store"})


@app.get("/u/{username}", response_class=HTMLResponse)
def profile(username: str, request: Request):
    u, me = visible_user(request, username)
    mine = bool(me and me["id"] == u["id"])
    body = PROFILE_HTML.replace("__USER__", esc(u["username"]))
    if not u["public"]:
        body = ('<p class="notice">Only you and admins can see this profile. <a class="link" href="/settings">Change this in Settings</a></p>'
                if mine else '<p class="notice">This profile is private. You can see it because you\'re an admin.</p>') + body
    return page(u["name"] or u["username"], body, me=me, active="me" if mine else "", sub="@" + u["username"],
                toolbar=RANGE_SEG, status_user=u)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    me = current_user(request)
    since = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
    active = db.query("SELECT DISTINCT u.id FROM users u JOIN events e ON e.user_id = u.id "
                      "WHERE u.public = 1 AND e.ts >= %s", (since,))
    board = []
    for a_ in active[:200]:
        u = db.one("SELECT * FROM users WHERE id = %s", (a_["id"],))
        st = user_stats(u)
        board.append((st["week"]["effort_s"], st, u))
    board.sort(key=lambda b_: -b_[0])
    rows = "".join(
        """<tr onclick="location.href='/u/%s'"><td class="quiet rank">%d</td><td><div class="sname"><b>%s%s</b><span>@%s</span></div></td>
<td class="eff">%s</td><td>%s</td><td class="proj">%s</td></tr>"""
        % (esc(u["username"]), i + 1, '<img class="livedot" src="/assets/dot-live.svg" alt="coding now"> ' if st["live"] else "",
           esc(u["name"] or u["username"]), esc(u["username"]), hm(e), hm(st["week"]["observed_s"]),
           esc(st["projects"][0]["name"] if st["projects"] else ""))
        for i, (e, st, u) in enumerate(board[:50]))
    table = ("""<div class="table-scroll"><table class="stable"><thead><tr><th></th><th>Builder</th>
<th><img src="/assets/dot6-effort.svg" alt="">Effort</th><th><img src="/assets/dot6-observed.svg" alt="">Observed</th>
<th>Top project</th></tr></thead><tbody>%s</tbody></table></div>""" % rows) if board else \
        '<p class="empty">Nobody has synced this week yet.</p>'
    intro = "" if me else """<div class="panel intro"><h2>Time you put in, not time the AI ran</h2>
<p class="sub">Sauron runs quietly next to Claude Code. It counts your reading, thinking and typing between prompts,
leaves out the minutes the agent worked alone, and estimates how much of your time was real effort.</p>
<a class="btn" href="/auth/start">Sign In with Hack Club</a></div>"""
    body = intro + """<div class="panel"><div class="phead"><h2>This Week</h2><span class="meta">%s</span></div>%s</div>
<div class="privacy-line"><img src="/assets/icon-lock.svg" alt=""><span>Synced: counts, timings, project names and effort labels. Never prompt text, code or AI output.</span><a href="%s">Source Code</a></div>""" % (
        "%d builder%s" % (len(board), "" if len(board) == 1 else "s"), table, REPO_URL)
    return page("This Week", body, me=me, active="week", sub="Ranked by estimated effort")


# ---------------------------------------------------------------- admin

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    me = require_admin(request)
    t = csrf(request)
    users = db.query("SELECT * FROM users ORDER BY id")
    rows = []
    for u in users:
        st = user_stats(u)
        dev = db.one("SELECT COUNT(*) AS n FROM api_keys WHERE user_id = %s AND revoked_at IS NULL", (u["id"],))["n"]
        if env_admin(u):
            role = '<span class="tag blue">Admin</span> <span class="quiet">set on the server</span>'
        elif u["is_admin"]:
            role = ('<span class="tag blue">Admin</span> <form method="post" action="/admin/u/%s/role" class="inline">'
                    '<input type="hidden" name="csrf_token" value="%s"><input type="hidden" name="admin" value="0">'
                    '<button class="plain danger">Remove</button></form>' % (esc(u["username"]), t)) \
                if u["id"] != me["id"] else '<span class="tag blue">Admin</span> <span class="quiet">you</span>'
        else:
            role = ('<form method="post" action="/admin/u/%s/role" class="inline"><input type="hidden" name="csrf_token" value="%s">'
                    '<input type="hidden" name="admin" value="1"><button class="plain">Make admin</button></form>'
                    % (esc(u["username"]), t))
        rows.append("""<tr><td><div class="sname"><b><a class="link" href="/admin/u/%s">%s</a></b><span>@%s%s</span></div></td>
<td class="eff">%s</td><td>%s</td><td class="quiet">%d</td><td class="quiet">%s</td><td>%s</td><td>%s</td></tr>""" % (
            esc(u["username"]), esc(u["name"] or u["username"]), esc(u["username"]), "" if u["public"] else " · private",
            hm(st["week"]["effort_s"]), hm(st["all"]["effort_s"]), dev,
            esc(ago(st["last_event"]) if st["last_event"] else "never"),
            ('<span class="tag">%d</span>' % st["audit"]["breaks"]) if st["audit"]["breaks"] else '<span class="quiet">0</span>', role))
    log = db.query("SELECT l.action, l.at, a.username AS actor, t.username AS target FROM admin_log l "
                   "JOIN users a ON a.id = l.actor_id LEFT JOIN users t ON t.id = l.target_id ORDER BY l.id DESC LIMIT 50")
    log_rows = "".join("<tr><td class='quiet'>%s</td><td>@%s</td><td>%s</td><td>%s</td></tr>" % (
        esc(ago(r["at"])), esc(r["actor"]), esc(ADMIN_ACTIONS.get(r["action"], r["action"])),
        ("@" + esc(r["target"])) if r["target"] else "") for r in log)
    body = """<div class="panel"><div class="phead"><h2>Everyone</h2><span class="meta">%d user%s</span></div>
<div class="table-scroll"><table class="stable"><thead><tr><th>User</th><th><img src="/assets/dot6-effort.svg" alt="">This week</th>
<th>All time</th><th>Devices</th><th>Last active</th><th>Chain breaks</th><th>Role</th></tr></thead><tbody>%s</tbody></table></div></div>
<div class="panel"><div class="phead"><h2>Admin Activity</h2><span class="meta">who did what, newest first</span></div>%s</div>
<div class="privacy-line"><img src="/assets/icon-lock.svg" alt=""><span>Admins see everything the server holds: counts, timings, project and file names, effort labels and log checkpoints. It never receives prompt text or code. Opening someone's data is logged here.</span></div>""" % (
        len(users), "" if len(users) == 1 else "s", "".join(rows),
        ('<div class="table-scroll"><table class="stable"><thead><tr><th>When</th><th>Admin</th><th>Action</th><th>User</th></tr></thead>'
         '<tbody>%s</tbody></table></div>' % log_rows) if log else '<p class="empty">Nothing yet.</p>')
    return page("Admin", body, me=me, active="admin", sub="Every account on this server")


ADMIN_ACTIONS = {"grant": "made admin", "revoke": "removed admin", "view": "opened data"}


@app.post("/admin/u/{username}/role")
def admin_role(username: str, request: Request, csrf_token: str = Form(""), admin: str = Form("")):
    me = require_admin(request)
    check_csrf(request, csrf_token)
    u = db.one("SELECT * FROM users WHERE username = %s", (username,))
    if not u:
        raise HTTPException(404, "No such user")
    make = admin == "1"
    if not make and u["id"] == me["id"]:
        raise HTTPException(400, "You can't remove your own admin access.")
    if bool(u["is_admin"]) != make:
        db.execute("UPDATE users SET is_admin = %s WHERE id = %s", (1 if make else 0, u["id"]))
        admin_log(me, "grant" if make else "revoke", u)
    return RedirectResponse("/admin", 303)


@app.get("/admin/u/{username}", response_class=HTMLResponse)
def admin_user(username: str, request: Request):
    me = require_admin(request)
    u = db.one("SELECT * FROM users WHERE username = %s", (username,))
    if not u:
        raise HTTPException(404, "No such user")
    if u["id"] != me["id"]:
        admin_log(me, "view", u)
    st = user_stats(u)
    devices = db.query("SELECT id, label, created_at, last_used_at, last_n, revoked_at FROM api_keys WHERE user_id = %s ORDER BY id",
                       (u["id"],))
    dev_rows = "".join("""<tr><td><div class="sname"><b>%s</b><span>added %s%s</span></div></td><td class="quiet">%s</td>
<td>%s</td><td>%d</td><td>%s</td></tr>""" % (
        esc(d["label"] or "Unnamed device"), esc(day_label(d["created_at"])), " · revoked" if d["revoked_at"] else "",
        esc(ago(d["last_used_at"]) if d["last_used_at"] else "never"), "{:,}".format(d["last_n"]),
        db.one("SELECT COUNT(*) AS n FROM heads WHERE key_id = %s", (d["id"],))["n"],
        ('<span class="tag">%d</span>' % b) if (b := db.one("SELECT COUNT(*) AS n FROM heads WHERE key_id = %s AND linked = 0",
                                                         (d["id"],))["n"]) else '<span class="quiet">0</span>')
        for d in devices)
    heads = db.query("SELECT h.n, h.h, h.linked, h.received_at, k.label FROM heads h JOIN api_keys k ON k.id = h.key_id "
                     "WHERE k.user_id = %s ORDER BY h.id DESC LIMIT 20", (u["id"],))
    head_rows = "".join("<tr><td class='quiet'>%s</td><td>%s</td><td>%s</td><td><code>%s…</code></td><td>%s</td></tr>" % (
        esc(ago(h["received_at"])), esc(h["label"] or "device"), "{:,}".format(h["n"]), esc(h["h"][:16]),
        '<span class="quiet">linked</span>' if h["linked"] else '<span class="tag">gap</span>') for h in heads)
    events = db.query("SELECT ts, kind, project, data FROM events WHERE user_id = %s ORDER BY ts DESC, n DESC LIMIT 100", (u["id"],))
    ev_rows = "".join("<tr><td class='quiet'>%s</td><td>%s</td><td class='proj'>%s</td><td class='quiet mono'>%s</td></tr>" % (
        esc(e["ts"][:19].replace("T", " ")), esc(e["kind"]), esc(e["project"] or ""),
        esc(", ".join("%s=%s" % (k, v) for k, v in sorted(json.loads(e["data"]).items())
                      if k not in ("session", "project", "source", "prompt_id"))[:160])) for e in events)
    n_events = db.one("SELECT COUNT(*) AS n FROM events WHERE user_id = %s", (u["id"],))["n"]
    n_labels = db.one("SELECT COUNT(*) AS n FROM labels WHERE user_id = %s", (u["id"],))["n"]

    def table(head, rows, empty):
        return ('<div class="table-scroll"><table class="stable"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
                % ("".join("<th>%s</th>" % h for h in head), rows)) if rows else '<p class="empty">%s</p>' % empty
    body = """<div class="measures">
<div class="measure"><div class="mhead"><span class="dotlabel"><img src="/assets/dot-effort.svg" alt="">Estimated Effort</span></div><div class="big">%s</div><p>%s this week</p></div>
<div class="measure"><div class="mhead"><span class="dotlabel"><img src="/assets/dot-observed.svg" alt="">Observed</span></div><div class="big">%s</div><p>all time</p></div>
<div class="measure"><div class="mhead"><span class="dotlabel"><img src="/assets/dot-submitted.svg" alt="">Synced</span></div><div class="big">%s</div><p>events, %s effort labels</p></div></div>
<div class="panel"><div class="phead"><h2>Devices</h2><a class="link" href="/u/%s">Open profile</a></div>%s</div>
<div class="panel"><div class="phead"><h2>Log Checkpoints</h2><span class="meta">latest hash of each device's log, kept as it arrived</span></div>%s</div>
<div class="panel"><div class="phead"><h2>Synced Events</h2><span class="meta">latest 100 of %s</span></div>%s</div>
<div class="privacy-line"><img src="/assets/icon-lock.svg" alt=""><span>Opening this page is recorded in the admin log.</span></div>""" % (
        hm(st["all"]["effort_s"]), hm(st["week"]["effort_s"]), hm(st["all"]["observed_s"]), "{:,}".format(n_events),
        "{:,}".format(n_labels), esc(u["username"]),
        table(["Device", "Last sync", "Events", "Checkpoints", "Chain breaks"], dev_rows, "No devices."),
        table(["Received", "Device", "Line", "Hash", "Chain"], head_rows, "No checkpoints yet."),
        "{:,}".format(n_events), table(["Time", "Event", "Project", "Counts"], ev_rows, "Nothing synced yet."))
    return page(u["name"] or u["username"], body, me=me, active="admin",
                sub="@%s · %s · %s" % (u["username"], "public" if u["public"] else "private",
                                       "admin" if is_admin(u) else "member"))


@app.get("/healthz")
def healthz():
    db.one("SELECT 1 AS x")
    return {"ok": True}


# ---------------------------------------------------------------- html
# Pages use the local dashboard's own stylesheet and icons (the "Sauron · Liquid glass" Figma design),
# so the site and the dashboard can't drift apart. SITE_CSS only adds what the site needs on top.

ASSETS = os.path.join(os.path.dirname(HERE), "tracker", "assets")


def _dashboard_css():
    with open(os.path.join(os.path.dirname(HERE), "tracker", "dashboard.html"), encoding="utf-8") as f:
        page_src = f.read()
    css = page_src.split("<style>", 1)[1].split("</style>", 1)[0]
    return css.replace("url(assets/", "url(/assets/").replace('"assets/', '"/assets/')


SITE_CSS = """
/* Web layout: the Figma's materials (glass, type, panels, colours) on a normal page instead of an app window. */
body.site { background: #f5f5f7; font-size: 14px; min-height: 100vh; display: flex; flex-direction: column; }
.topbar { position: sticky; top: 0; z-index: 20; background: rgba(245, 245, 247, 0.72);
  -webkit-backdrop-filter: saturate(1.8) blur(20px); backdrop-filter: saturate(1.8) blur(20px); border-bottom: 0.5px solid rgba(0, 0, 0, 0.1); }
.topbar-in { max-width: 1120px; margin: 0 auto; height: 52px; padding: 0 24px; display: flex; align-items: center; gap: 28px; }
.wordmark { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; color: var(--t1); text-decoration: none; }
.topbar-in nav { display: flex; gap: 22px; }
.topbar-in nav a { font-size: 13px; color: var(--t2); text-decoration: none; padding: 16px 0 14px; border-bottom: 2px solid transparent; }
.topbar-in nav a:hover { color: var(--t1); }
.topbar-in nav a.on { color: var(--t1); font-weight: 590; border-bottom-color: var(--t1); }
.bar-end { margin-left: auto; display: flex; align-items: center; gap: 10px; }
.avatar { width: 30px; height: 30px; border-radius: 15px; background: var(--effort); color: #fff; display: grid; place-items: center; font-size: 13px; font-weight: 700; text-decoration: none; }
.wrap { width: 100%; max-width: 1120px; margin: 0 auto; padding: 36px 24px 48px; flex: 1; }
.pagehead { display: flex; align-items: flex-end; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
.pagehead > div:first-child { margin-right: auto; min-width: 0; }
.pagehead h1 { margin: 0; font-size: 34px; font-weight: 700; letter-spacing: -0.6px; line-height: 1.1; }
.pagehead p { margin: 6px 0 0; font-size: 14px; color: var(--t3); display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.live-pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--t2); background: rgba(52, 199, 89, 0.12); padding: 3px 10px; border-radius: 999px; }
.live-pill img { width: 7px; height: 7px; }
.live-pill b { font-weight: 590; color: #1f8f3a; }
.live-pill.idle { background: rgba(0, 0, 0, 0.05); }
.live-pill.idle b { color: var(--t2); }
.live-pill.idle img { filter: grayscale(1); opacity: 0.5; }
.view { gap: 16px; }
.panel, .measure { background: #fff; box-shadow: 0 0 0 0.5px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.04); border-radius: 14px; padding: 18px 20px; }
.measure .big { font-size: 32px; }
.panel h2 { font-size: 15px !important; }
.stable tbody tr:hover td { background: rgba(0, 0, 0, 0.025); }
.stable .eff { color: var(--effort); }
.stable td.rank { width: 28px; }
.livedot { width: 8px; height: 8px; vertical-align: 0; }
.privacy-line { padding: 4px 2px 0; }
.sitefoot { max-width: 1120px; width: 100%; margin: 0 auto; padding: 18px 24px 28px; display: flex; gap: 14px; font-size: 12px; color: var(--t4); border-top: 0.5px solid rgba(0, 0, 0, 0.08); }
.sitefoot a { color: var(--t3); text-decoration: none; }
.notice { margin: 0; font-size: 13px; color: var(--t2); background: rgba(255, 141, 40, 0.12); border-radius: 12px; padding: 12px 14px; }
.intro { padding: 28px !important; }
.intro h2 { font-size: 24px !important; font-weight: 700 !important; letter-spacing: -0.3px; margin-bottom: 6px !important; }
.intro .sub { font-size: 15px; line-height: 22px; max-width: 60ch; color: var(--t2); margin-bottom: 18px; }
.plain { font: 510 13px var(--font); color: var(--accent); background: none; border: 0; padding: 0; cursor: pointer; }
.plain.danger { color: var(--danger); }
form.inline { display: inline; margin-left: 6px; }
.privacy-line { flex-wrap: nowrap; align-items: flex-start; }
.privacy-line span { flex: 1 1 auto; min-width: 0; }
.privacy-line img { margin-top: 1px; }
.mono { font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; white-space: normal !important; }
.stable .link { color: var(--t1); }
.privacy-line form { margin-left: auto; }
.inline-form { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
.inline-form input, .fields input:not([type=checkbox]) {
  font: 14px var(--font); color: var(--t1); background: #fff; border: 0.5px solid rgba(0, 0, 0, 0.16); border-radius: 9px;
  padding: 0 11px; height: 34px; min-width: 0; flex: 1 1 220px; max-width: 360px; }
.fields { display: flex; flex-direction: column; gap: 14px; }
.fields input:not([type=checkbox]) { flex: none; width: 100%; }
.fields label { display: flex; flex-direction: column; gap: 5px; font-size: 12px; font-weight: 590; color: var(--t3); }
.fields .check-row { flex-direction: row; align-items: center; gap: 8px; font-size: 14px; font-weight: 400; color: var(--t1); }
.keypanel { background: #effbf2 !important; }
.cmd, .keyrow code { font: 13px ui-monospace, SFMono-Regular, Menlo, monospace; background: #f5f5f7; border-radius: 9px; padding: 10px 12px; margin: 0; overflow-x: auto; display: block; }
.keyrow { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
.keyrow code { flex: 1; min-width: 0; word-break: break-all; }
.keyrow .glass { padding: 0 14px; }
.heat { overflow-x: auto; }
.heat svg { min-width: 640px; }
.heat-foot { display: flex; align-items: center; gap: 6px; justify-content: flex-end; font-size: 11px; color: var(--t3); margin-top: 6px; }
.heat-foot i { width: 10px; height: 10px; border-radius: 2.5px; display: inline-block; }
.kinds { display: flex; flex-direction: column; gap: 7px; }
.kind { display: grid; grid-template-columns: 180px 1fr 36px; gap: 10px; align-items: center; font-size: 13px; }
.kind .bar { height: 8px; border-radius: 4px; background: var(--effort); min-width: 2px; }
.kind .bar.dim { background: var(--gray); opacity: 0.45; }
.kind b { font-weight: 510; }
.kind span { text-align: right; color: var(--t3); font-variant-numeric: tabular-nums; }
@media (max-width: 720px) {
  .topbar-in { gap: 16px; padding: 0 16px; }
  .topbar-in nav { gap: 14px; }
  .wrap { padding: 24px 16px 36px; }
  .pagehead h1 { font-size: 28px; }
  .kind { grid-template-columns: 120px 1fr 30px; }
  .sitefoot { padding: 16px; }
}
"""

APP_CSS = _dashboard_css() + SITE_CSS
CSS_VERSION = hashlib.sha256(APP_CSS.encode("utf-8")).hexdigest()[:10]  # new URL whenever the styles change

RANGE_SEG = """<div class="glass seg" id="rangeSeg" role="group" aria-label="Time range">
<button data-range="7" class="on">Week</button><button data-range="30">Month</button><button data-range="365">Year</button><button data-range="0">All</button></div>"""


@app.get("/assets/app.css")
def app_css():
    return Response(APP_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=31536000, immutable"})


app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def hm(s):
    s = int(s or 0)
    if s < 60:
        return "0m" if s <= 0 else "<1m"
    return "%dh %02dm" % (s // 3600, s % 3600 // 60) if s >= 3600 else "%dm" % (s // 60)


def day_label(iso):
    try:
        return datetime.date.fromisoformat((iso or "")[:10]).strftime("%b %-d")
    except ValueError:
        return ""


def ago(iso):
    try:
        t = aitrack.parse_ts(iso)
    except (TypeError, ValueError):
        return ""
    s = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    if s < 90:
        return "just now"
    if s < 3600:
        return "%d min ago" % (s // 60)
    if s < 86400:
        return "%d h ago" % (s // 3600)
    return day_label(iso)


def page(title, body, me=None, active="", sub="", toolbar="", status_user=None):
    """A web page in the Figma's visual language: glass top bar, page header, panels. No app window."""
    def nav(key, href, label):
        return '<a href="%s"%s>%s</a>' % (href, ' class="on" aria-current="page"' if key == active else "", label)
    links = nav("week", "/", "This Week")
    if me:
        links += nav("me", "/u/" + esc(me["username"]), "Profile") + nav("settings", "/settings", "Devices")
        if is_admin(me):
            links += nav("admin", "/admin", "Admin")
    account = ('<a class="avatar" href="/u/%s" title="%s"><span>%s</span></a>'
               % (esc(me["username"]), esc(me["name"] or me["username"]), esc((me["name"] or me["username"])[:1].upper()))
               ) if me else '<a class="btn" href="/auth/start">Sign In</a>'
    status = ('<span class="live-pill idle" id="liveStatus"><img src="/assets/dot-live.svg" alt="">'
              '<b id="liveTitle">Checking…</b><span id="liveSub"></span></span>') if status_user is not None else ""
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%s · Sauron</title>
<link rel="stylesheet" href="/assets/app.css?v=%s"></head><body class="site">
<header class="topbar"><div class="topbar-in"><a class="wordmark" href="/">Sauron</a><nav aria-label="Main">%s</nav>
<div class="bar-end">%s</div></div></header>
<main class="wrap"><div class="pagehead"><div><h1>%s</h1><p>%s%s</p></div>%s</div>
<div class="view">%s</div></main>
<footer class="sitefoot"><span>Sauron · AGPLv3</span><a href="%s">Source</a></footer>
<div id="tip"></div></body></html>""" % (esc(title), CSS_VERSION, links, account, esc(title), esc(sub), status, toolbar, body, REPO_URL))


PROFILE_HTML = r"""
<div class="measures" id="measures"></div>
<div class="panel" id="whereTime"></div>
<div class="panel"><div class="phead"><h2>Effort This Year</h2><span class="meta" id="heatMeta"></span></div>
<div class="heat" id="heat"></div>
<div class="heat-foot">Less <i style="background:rgba(0,0,0,0.05)"></i><i style="background:rgba(203,48,224,0.25)"></i><i style="background:rgba(203,48,224,0.45)"></i><i style="background:rgba(203,48,224,0.7)"></i><i style="background:#cb30e0"></i> More</div></div>
<div class="grid2">
<div class="panel"><div class="phead"><h2>Projects</h2><span class="meta" id="projMeta"></span></div><div class="table-scroll" id="projects"></div></div>
<div class="panel"><div class="phead"><h2>What the Prompts Were Doing</h2><span class="meta" id="kindMeta"></span></div>
<p class="sub">Each prompt is labelled by Jev. Purple kinds count toward effort in full; grey ones count only as far as the prompt shows your own thinking.</p>
<div class="kinds" id="kinds"></div></div></div>
<div class="panel"><div class="phead"><h2>Recent Sessions</h2><span class="meta" id="sessMeta"></span></div><div class="table-scroll" id="sessions"></div></div>
<div class="privacy-line"><img src="/assets/icon-lock.svg" alt=""><span id="auditLine">Counts, timings and effort labels only. Never prompt text, code or AI output.</span></div>
<script>
const user = "__USER__";
const $ = (q) => document.querySelector(q);
const hm = (s) => { s = Math.round(s || 0); if (s < 60) return s <= 0 ? "0m" : "<1m"; return s >= 3600 ? `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m` : `${Math.floor(s / 60)}m`; };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const COUNTS = new Set(["concept", "understand_ai_work", "code_and_explain", "review_pushback"]);
const KIND = { concept: "Asking how things work", understand_ai_work: "Understanding the AI's work", code_and_explain: "Code with an explanation",
  review_pushback: "Pushing back on the AI", direct: "Directing with a plan", delegate: "Handing off a task", debug_handoff: "Passing an error along",
  approve: "Approving", ask_info: "Asking for information", other: "Other" };
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function dayName(iso) {
  const d = new Date(iso.length <= 10 ? iso + "T12:00:00" : iso), t = new Date();
  const k = (x) => x.toDateString(), y = new Date(t); y.setDate(t.getDate() - 1);
  if (k(d) === k(t)) return "Today";
  if (k(d) === k(y)) return "Yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function spanText(a, b) {
  const m = Math.max(1, Math.round((Date.parse(b) - Date.parse(a)) / 6e4));
  if (m < 90) return `${m} min`;
  if (m < 24 * 60) return `${Math.round(m / 60)} h`;
  return `${dayName(a)} – ${dayName(b)}`;
}
const local = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
let stats = null, range = 7;
function sumRange(days) {
  const from = range ? local(new Date(Date.now() - (range - 1) * 864e5)) : "";
  const t = { you_s: 0, ai_s: 0, effort_s: 0 };
  for (const d of days) if (d.day >= from) { t.you_s += d.you_s; t.ai_s += d.ai_s; t.effort_s += d.effort_s; }
  return t;
}
const LABEL = { 7: "This Week", 30: "This Month", 365: "This Year", 0: "All Time" };
function measure(dot, label, value, text, tag) {
  return `<div class="measure"><div class="mhead"><span class="dotlabel"><img src="/assets/${dot}" alt="">${label}</span>${tag || ""}</div><div class="big">${value}</div><p>${text}</p></div>`;
}
function render() {
  const s = stats; if (!s) return;
  const t = sumRange(s.days), obs = t.you_s + t.ai_s;
  $("#measures").innerHTML =
    measure("dot-effort.svg", "Estimated Effort", hm(t.effort_s), "Time spent learning, steering and reasoning, weighted by what each prompt was doing.", '<span class="tag effort">Estimate</span>') +
    measure("dot-submitted.svg", "Your Time", hm(t.you_s), "Reading, thinking and typing between prompts. The agent's own time is left out.") +
    measure("dot-observed.svg", "Observed Activity", hm(obs), "Your time plus the minutes the agent ran on its own. Each moment counts once.");
  const other = Math.max(0, t.you_s - t.effort_s);
  const segs = [[t.effort_s, "var(--effort)", "Estimated effort"], [other, "rgba(203,48,224,0.32)", "Your other time"], [t.ai_s, "rgba(0,195,208,0.45)", "Agent ran without you"]].filter((x) => x[0] > 0);
  $("#whereTime").innerHTML = `<div class="phead"><h2>Where the Time Went</h2><span class="meta">${LABEL[range]} · ${hm(obs)} observed</span></div>` +
    (segs.length ? `<div class="stack">${segs.map(([v, c, l]) => `<div style="flex:${v} 1 0;background:${c}" title="${esc(l)} ${hm(v)}"></div>`).join("")}</div>
    <div class="stack-labels">${segs.map(([v, , l]) => `<div style="flex:${v} 1 0"><b>${hm(v)}</b><span>${esc(l)}</span></div>`).join("")}</div>` : `<p class="empty">Nothing tracked ${LABEL[range].toLowerCase()} yet.</p>`);
}
function heat(days) {
  const by = Object.fromEntries(days.map((d) => [d.day, d.effort_s]));
  const today = new Date(); today.setHours(12, 0, 0, 0);
  const start = new Date(today); start.setDate(start.getDate() - 364 - start.getDay());
  const max = Math.max(1800, ...days.map((d) => d.effort_s));
  const C = 11, G = 3, X = 26, Y = 16;
  let g = "", total = 0, active = 0, streak = 0, best = 0, run = 0, lastMonth = -1;
  for (let i = 0, d = new Date(start); d <= today; i++, d.setDate(d.getDate() + 1)) {
    const key = local(d), v = by[key] || 0, col = Math.floor(i / 7), row = d.getDay();
    total += v; if (v > 0) { active++; run++; best = Math.max(best, run); } else run = 0;
    const lvl = v <= 0 ? 0 : v < max * 0.15 ? 1 : v < max * 0.4 ? 2 : v < max * 0.7 ? 3 : 4;
    const fill = ["rgba(0,0,0,0.05)", "rgba(203,48,224,0.25)", "rgba(203,48,224,0.45)", "rgba(203,48,224,0.7)", "#cb30e0"][lvl];
    g += `<rect class="d" x="${X + col * (C + G)}" y="${Y + row * (C + G)}" width="${C}" height="${C}" fill="${fill}"><title>${key}: ${v ? hm(v) + " effort" : "no effort tracked"}</title></rect>`;
    if (row === 0 && d.getMonth() !== lastMonth) { lastMonth = d.getMonth(); g += `<text x="${X + col * (C + G)}" y="10">${d.toLocaleString(undefined, { month: "short" })}</text>`; }
  }
  for (let d = new Date(today); (by[local(d)] || 0) > 0; d.setDate(d.getDate() - 1)) streak++;
  for (const [r, l] of [[1, "Mon"], [3, "Wed"], [5, "Fri"]]) g += `<text x="0" y="${Y + r * (C + G) + 9}">${l}</text>`;
  const W = X + 53 * (C + G), H = Y + 7 * (C + G);
  $("#heat").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Effort per day over the last year">${g}</svg>`;
  $("#heatMeta").textContent = `${hm(total)} over ${active} day${active === 1 ? "" : "s"}` + (streak ? ` · ${streak}-day streak` : "") + (best > 1 ? ` · best ${best}` : "");
}
function projects(ps) {
  $("#projMeta").textContent = ps.length ? `${ps.length} project${ps.length === 1 ? "" : "s"}` : "";
  $("#projects").innerHTML = ps.length ? `<table class="stable"><thead><tr><th>Project</th><th><img src="/assets/dot6-effort.svg" alt="">Effort</th><th>Your time</th><th>Last active</th></tr></thead><tbody>` +
    ps.slice(0, 12).map((p) => `<tr${p.repo_url ? ` onclick="window.open('${esc(p.repo_url)}','_blank','noopener')"` : ""}><td><div class="sname"><b>${esc(p.name)}</b><span>${p.repo_url ? esc(p.repo_url.replace(/^https:\/\//, "")) : plural(p.prompts, "prompt")}</span></div></td><td class="eff">${hm(p.effort_s)}</td><td>${hm(p.you_s)}</td><td class="quiet">${dayName(p.last_day)}</td></tr>`).join("") +
    `</tbody></table>` : `<p class="empty">No projects yet.</p>`;
}
function kinds(k, judged, total) {
  const rows = Object.entries(k).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...rows.map((r) => r[1]));
  $("#kindMeta").textContent = `${judged} of ${total} judged`;
  $("#kinds").innerHTML = rows.length ? rows.map(([name, n]) => `<div class="kind"><b>${esc(KIND[name] || name)}</b><div><div class="bar${COUNTS.has(name) ? "" : " dim"}" style="width:${(n / max * 100).toFixed(1)}%"></div></div><span>${n}</span></div>`).join("") : `<p class="empty">No prompts judged yet.</p>`;
}
function sessions(ss) {
  $("#sessMeta").textContent = ss.length ? `last ${ss.length}` : "";
  const when = (a) => new Date(a).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  $("#sessions").innerHTML = ss.length ? `<table class="stable"><thead><tr><th>Session</th><th>Project</th><th><img src="/assets/dot6-effort.svg" alt="">Effort</th><th>Your time</th><th><img src="/assets/dot6-observed.svg" alt="">Agent alone</th></tr></thead><tbody>` +
    ss.map((x) => `<tr><td><div class="sname"><b>${when(x.start)}</b><span>${spanText(x.start, x.end)} · ${plural(x.prompts, "prompt")}</span></div></td><td class="proj">${esc(x.project)}${x.imported ? ' <span class="tag" title="Backfilled from Claude Code transcripts: not hash-chained, not on the leaderboard">Imported</span>' : ""}</td><td class="eff">${x.judged ? hm(x.effort_s) : "–"}</td><td>${hm(x.you_s)}</td><td>${hm(x.ai_s)}</td></tr>`).join("") +
    `</tbody></table>` : `<p class="empty">No sessions yet. Connect a device in Settings.</p>`;
}
function live(s) {
  const box = $("#liveStatus"); if (!box) return;
  box.classList.toggle("idle", !s.live);
  $("#liveTitle").textContent = s.live ? "Coding now" : s.last_event ? "Not coding" : "No activity yet";
  $("#liveSub").textContent = s.live ? "updates every 15 s" : s.last_event ? "last active " + new Date(s.last_event).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
}
async function load() {
  const r = await fetch(`/api/u/${user}`, { cache: "no-store" });
  if (!r.ok) return;
  stats = await r.json();
  render(); heat(stats.days); projects(stats.projects); kinds(stats.kinds, stats.judged, stats.prompts); sessions(stats.sessions || []); live(stats);
  $("#auditLine").textContent = `Counts, timings and effort labels only, never prompt text or code. Effort is an unvalidated estimate. ${plural(stats.audit.devices, "device")}, ${plural(stats.audit.heads, "log checkpoint")} kept by the server, ${plural(stats.audit.breaks, "chain break")}.` +
    (stats.imported && stats.imported.prompts ? ` Includes ${plural(stats.imported.sessions, "session")} (${hm(stats.imported.effort_s)} effort) backfilled from Claude Code transcripts. Those aren't hash-chained and don't count on the leaderboard.` : "");
}
document.querySelectorAll("#rangeSeg button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#rangeSeg button").forEach((x) => x.classList.toggle("on", x === b));
  range = Number(b.dataset.range); render();
});
load(); setInterval(load, 15000);
</script>
"""
