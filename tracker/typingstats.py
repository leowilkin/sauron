# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fold the typing module's per-draft files into log events.

hooks/typing/register.ts writes one JSON file of counts per sent or abandoned
draft to <data dir>/typing/<session id>/<epoch ms>-<sent|abandoned>.json.
The UserPromptSubmit hook takes the oldest recent "sent" file (plus any drafts
abandoned before it) and puts the numbers on prompt_sent; SessionEnd takes the
rest. Files are deleted once read.

Events from tools without keystroke data simply lack these fields, which
means "unknown", not zero.
"""
import json
import os
import re
import time

import eventlog

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NAME = re.compile(r"^(\d+)-(sent|abandoned)\.json$")
MAX_AGE_MS = 120 * 1000          # a "sent" file older than this belongs to no live prompt
STALE_DIR_S = 2 * 24 * 3600      # leftover folders from crashed sessions

SHAPE = re.compile(r"^[0-9a_n]*$")  # layout only: anything else is refused
MAX_REPLAY_BYTES = 512 * 1024

PROMPT_FIELDS = ("typing_ms", "compose_ms", "keys_typed", "pastes", "fixes", "rewrites",
                 "revisions", "pauses", "chars_deleted")


def _dir(session_id):
    if not session_id or not SESSION_ID.match(session_id):
        return None
    return os.path.join(eventlog.data_dir(), "typing", session_id)


def _files(d):
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        m = NAME.match(n)
        if m:
            out.append((int(m.group(1)), m.group(2), os.path.join(d, n)))
    out.sort()
    return out


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _abandoned_fields(files):
    n = ms = 0
    for _, _, path in files:
        d = _load(path)
        n += 1
        ms += int(d.get("typing_ms") or 0)
        _remove(path)
    return {"drafts_abandoned": n, "abandoned_typing_ms": ms}


def replays_path(session_id):
    return os.path.join(eventlog.data_dir(), "replays", session_id + ".jsonl")


def _clean_replay(r):
    """Validated copy of a module replay, or None. Never lets text through."""
    if not isinstance(r, dict) or not isinstance(r.get("steps"), list):
        return None
    initial = r.get("initial", "")
    if not isinstance(initial, str) or not SHAPE.match(initial):
        return None
    steps = []
    for st in r["steps"]:
        if not (isinstance(st, list) and len(st) == 4):
            return None
        t, a, b, shape = st
        if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (t, a, b)):
            return None
        if not isinstance(shape, str) or not SHAPE.match(shape):
            return None
        steps.append([t, a, b, shape])
    out = {"initial": initial, "steps": steps}
    if r.get("truncated"):
        out["truncated"] = True
    return out


def _store_replay(session_id, data, prompt_id):
    """Append the draft's replay to replays/<session>.jsonl; return {"replay_hash": ...} for the event."""
    r = _clean_replay(data.get("replay"))
    if r is None:
        return {}
    rec = {"v": 1, "session": session_id, "prompt_id": prompt_id, "at": int(data.get("at") or 0),
           "typing_ms": int(data.get("typing_ms") or 0), "keys_typed": int(data.get("keys_typed") or 0)}
    rec.update(r)
    line = json.dumps(rec, separators=(",", ":")).encode("utf-8")
    if len(line) > MAX_REPLAY_BYTES:
        return {}
    path = replays_path(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as f:
            f.write(line + b"\n")
    except OSError:
        return {}
    return {"replay_hash": eventlog.line_hash(line)}


def for_prompt(session_id, now_ms=None, prompt_id=None):
    """Typing fields for the prompt being sent now, or {} if the module recorded none."""
    d = _dir(session_id)
    if d is None or not os.path.isdir(d):
        return {}
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    files = _files(d)
    sent = None
    for at, kind, path in files:
        if kind != "sent":
            continue
        if at < now_ms - MAX_AGE_MS:
            _remove(path)  # stale: its prompt hook never ran
            continue
        if at <= now_ms + 5000:
            sent = (at, path)
            break
    if sent is None:
        return {}
    data = _load(sent[1])
    _remove(sent[1])
    fields = {k: int(data.get(k) or 0) for k in PROMPT_FIELDS}
    fields.update(_store_replay(session_id, data, prompt_id))
    fields.update(_abandoned_fields([f for f in files if f[1] == "abandoned" and f[0] <= sent[0]]))
    return fields


def for_session_end(session_id):
    """Drafts abandoned since the last prompt; cleans up the session's folder."""
    d = _dir(session_id)
    if d is None or not os.path.isdir(d):
        return {}
    files = _files(d)
    fields = _abandoned_fields([f for f in files if f[1] == "abandoned"])
    for _, kind, path in files:
        if kind == "sent":
            _remove(path)
    try:
        os.rmdir(d)
    except OSError:
        pass
    return fields if fields["drafts_abandoned"] else {}


def sweep(now_s=None):
    """Remove folders left by sessions that never ended cleanly."""
    root = os.path.join(eventlog.data_dir(), "typing")
    now_s = now_s if now_s is not None else time.time()
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for name in entries:
        p = os.path.join(root, name)
        try:
            if os.path.isdir(p) and now_s - os.path.getmtime(p) > STALE_DIR_S:
                for f in os.listdir(p):
                    _remove(os.path.join(p, f))
                os.rmdir(p)
        except OSError:
            pass
