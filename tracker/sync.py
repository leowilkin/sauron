# SPDX-License-Identifier: AGPL-3.0-or-later
"""Send new log lines and Jev labels to the ai-tracker server, in the background.

Started by the hooks after each turn once `sauron connect` has been run. Sends only the
Hackatime-level fields in SEND_FIELDS, each line's hash and the hash it links to (so the server
can check the chain and keep heads as they arrive), labels, and each project's git remote.
Never cwd, prompt text or anything else. Honours hide_project_names / hide_file_names in
~/.wakatime.cfg.

    python3 sync.py          # sync now (what the hooks run)
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402

SEND_FIELDS = {
    "event", "ts", "session", "project", "prompt_id", "source", "agent_id", "reason", "agent_type",
    "chars", "pasted_chars", "typing_ms", "compose_ms", "keys_typed", "pastes", "fixes", "rewrites", "revisions",
    "pauses", "chars_deleted", "drafts_abandoned", "abandoned_typing_ms",
    "tool", "file", "lines_added", "lines_removed", "response_text_chars", "response_code_lines",
}
LABEL_FIELDS = {"prompt_id", "pattern", "pattern_confidence", "pattern_probs", "understanding", "read_previous",
                "pasted_output", "own_thinking", "own_thinking_confidence", "complexity", "model", "at"}
BATCH = 1000


def _path(name):
    return os.path.join(eventlog.data_dir(), name)


def config():
    try:
        with open(_path("config.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def server_key():
    try:
        with open(_path("server.key"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def enabled():
    return bool(config().get("server") and server_key())


def _state():
    try:
        with open(_path("sync.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    with open(_path("sync.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(_path("sync.json.tmp"), _path("sync.json"))


def _hide_settings():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import aitrack
        return aitrack.wakatime_hide_settings()
    except Exception:
        return False, False


def _anon(name):
    return "project-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def git_remote(cwd):
    """https URL of the project's origin remote, without credentials, or None."""
    try:
        root, _ = eventlog.find_project_root(cwd)
        out = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"], capture_output=True, timeout=3)
        url = out.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if url.startswith("git@") and ":" in url:
        host, path = url[4:].split(":", 1)
        url = "https://%s/%s" % (host, path)
    if not url.startswith("https://"):
        return None
    url = "https://" + url[8:].split("@", 1)[-1]  # drop user:token@
    return url[:-4] if url.endswith(".git") else url


OUTSIDE = "(outside project)"


def inside_project(path):
    """True for a path relative to the project folder; False for absolute paths and ones that climb out."""
    if not isinstance(path, str) or not path:
        return True
    p = path.replace("\\", "/")
    return not (p.startswith(("/", "~", "../")) or "/../" in p or p == ".." or (len(p) > 1 and p[1] == ":"))


def read_new_lines(path, offset, n0, prev0):
    """(events to send, new offset, last line number, last hash, cwd by project) from byte `offset`."""
    out, cwds = [], {}
    n, prev = n0, prev0
    try:
        f = open(path, "rb")
    except OSError:
        return out, offset, n, prev, cwds
    with f:
        f.seek(offset)
        while len(out) < BATCH:
            raw = f.readline()
            if not raw or not raw.endswith(b"\n"):
                break  # a line still being written: next time
            offset += len(raw)
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            n += 1
            h = eventlog.line_hash(line)
            try:
                e = json.loads(line.decode("utf-8"))
            except ValueError:
                prev = h
                continue
            ev = {k: v for k, v in e.items() if k in SEND_FIELDS}
            if not inside_project(ev.get("file")):
                ev["file"] = OUTSIDE  # paths outside the project can name the user's folders
            ev.update(n=n, h=h, prev=e.get("prev_hash"))
            if e.get("project") and e.get("cwd"):
                cwds.setdefault(e["project"], e["cwd"])
            out.append(ev)
            prev = h
    return out, offset, n, prev, cwds


def read_new_labels(path, offset):
    out = []
    try:
        f = open(path, "rb")
    except OSError:
        return out, offset
    with f:
        f.seek(offset)
        while len(out) < BATCH:
            raw = f.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            offset += len(raw)
            try:
                d = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if d.get("prompt_id") and not d.get("error"):
                out.append({k: v for k, v in d.items() if k in LABEL_FIELDS})
    return out, offset


def post(url, key, body):
    req = urllib.request.Request(url.rstrip("/") + "/api/ingest", data=json.dumps(body).encode("utf-8"), headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "ai-tracker-sync/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return read_json(r, url)


def read_json(r, url):
    """The JSON a Sauron API call returned, or a clear error when something else answered,
    like a login page a proxy put in front of the server."""
    if urllib.parse.urlsplit(r.geturl()).netloc != urllib.parse.urlsplit(url).netloc \
            or "json" not in (r.headers.get("Content-Type") or ""):
        raise OSError("the server address is behind a login page; ask its admin to let /api/ingest and /api/label "
                      "through without it (%s)" % urllib.parse.urlsplit(r.geturl()).netloc)
    return json.load(r)


def sync(send=None):
    """Send everything new. Returns events stored by the server, or -1 when it couldn't sync."""
    cfg, key = config(), server_key()
    if not cfg.get("server") or not key:
        return -1
    send = send or (lambda body: post(cfg["server"], key, body))
    lock = eventlog._FileLock(_path("sync.lock"))
    lock.__enter__()
    try:
        if not lock.locked:
            return -1
        hide_proj, hide_files = _hide_settings()
        st = _state()
        if st.get("server") != cfg["server"]:
            st = {"server": cfg["server"]}  # new server: start from the beginning
        total = 0
        while True:
            evs, off, n, prev, cwds = read_new_lines(eventlog.log_path(), st.get("offset", 0), st.get("n", 0),
                                                     st.get("prev"))
            labs, loff = read_new_labels(_path(os.path.join("jev", "events.jsonl")), st.get("labels_offset", 0))
            if not evs and not labs:
                break
            projects = {}
            for name, cwd in cwds.items():
                if name not in st.setdefault("repos", {}):
                    st["repos"][name] = git_remote(cwd) or ""
                if st["repos"][name] and not hide_proj:
                    projects[name] = st["repos"][name]
            for e in evs:
                if hide_proj and e.get("project"):
                    e["project"] = _anon(e["project"])
                if hide_files:
                    e.pop("file", None)
            res = send({"events": evs, "labels": labs, "projects": projects})
            total += int(res.get("stored", 0))
            st.update(offset=off, n=n, prev=prev, labels_offset=loff, at=eventlog.now_iso(),
                      server_head=res.get("head"))
            _save_state(st)
        return total
    except (urllib.error.URLError, OSError, ValueError) as e:
        try:
            with open(_path("errors.log"), "a", encoding="utf-8") as f:
                f.write("%s sync: %s\n" % (eventlog.now_iso(), str(e)[:200]))
        except OSError:
            pass
        return -1
    finally:
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    if os.environ.get("AI_TRACKER_DISABLE") != "1":
        r = sync()
        if len(sys.argv) > 1 and sys.argv[1] == "--verbose":
            print("synced %d new events" % r if r >= 0 else "not synced (see errors.log, or run sauron connect)")
