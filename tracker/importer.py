# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill events from Claude Code's own session transcripts.

Claude Code keeps every session as JSONL under ~/.claude/projects/<dir>/<session>.jsonl.
This turns them into the same event schema the live hooks write, so the time
model can run on history you had before installing the tracker, or on someone
else's export.

Output goes to its own file, never the live log: transcripts are editable, so
imported events are not evidence of anything. Every imported event carries
"source": "claude-code-import".

Privacy: like the hooks, only counts and timestamps are written. Prompt text,
code and model output are read only to be measured.

The transcript format is internal to Claude Code and undocumented. This was
written against Claude Code 2.1.3xx transcripts and skips anything it doesn't
recognize instead of failing.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_code  # noqa: E402
import eventlog  # noqa: E402

SOURCE = "claude-code-import"
EDIT_TOOLS = set(claude_code.EDIT_TOOLS)
# User records whose string content starts with these are UI/command plumbing, not prompts.
NOT_PROMPT_PREFIXES = ("<local-command", "<command-", "<bash-", "<task-notification", "<system-reminder",
                       "Caveat:", "[Request interrupted")


def find_transcripts(root):
    """(path, is_subagent) for every .jsonl under root, sorted."""
    out = []
    for dp, dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".jsonl"):
                p = os.path.join(dp, f)
                out.append((p, os.sep + "subagents" + os.sep in p))
    return sorted(out)


def _read(path):
    recs = []
    with open(path, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("timestamp"):
                try:
                    d["_t"] = _parse_utc(d["timestamp"])
                except ValueError:
                    continue
                recs.append(d)
    recs.sort(key=lambda d: d["_t"])
    return recs


def _parse_utc(s):
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(s)


def _blocks(rec):
    m = rec.get("message")
    c = m.get("content") if isinstance(m, dict) else None
    return c


def prompt_text(rec):
    """The typed prompt text of a user record, or None if it isn't a human prompt."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    if rec.get("promptSource") not in (None, "typed"):
        return None
    c = _blocks(rec)
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        text = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if not text:
            return None
    else:
        return None
    if text.lstrip().startswith(NOT_PROMPT_PREFIXES):
        return None
    return text


class _Ctx(object):
    def __init__(self, tz, session, cwd):
        self.tz, self.session, self.cwd = tz, session, cwd

    def project(self, cwd):
        # The original machine's paths usually don't exist here, so use the folder
        # name unless the repo is actually present locally.
        cwd = cwd or self.cwd or "?"
        if os.path.isdir(cwd):
            return eventlog.find_project_root(cwd)
        return cwd, os.path.basename(cwd.rstrip("/\\").replace("\\", "/")) or cwd

    def event(self, t, kind, cwd, **fields):
        _, project = self.project(cwd)
        rec = {"v": eventlog.SCHEMA_VERSION, "ts": t.astimezone(self.tz).isoformat(timespec="seconds"),
               "source": SOURCE, "session": self.session, "event": kind, "project": project}
        rec.update({k: v for k, v in fields.items() if v is not None})
        rec["cwd"] = cwd
        return rec


def _edit_event(ctx, rec, tool_uses):
    """ai_edit for a tool_result record that answers an Edit/Write/... tool_use."""
    c = _blocks(rec)
    r = rec.get("toolUseResult")
    if not isinstance(c, list) or not isinstance(r, dict):
        return None
    for b in c:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            name, tinput = tool_uses.get(b.get("tool_use_id"), (None, {}))
            if name not in EDIT_TOOLS:
                continue
            path = tinput.get("file_path") or tinput.get("notebook_path") or r.get("filePath")
            if not path:
                continue
            root, _ = ctx.project(rec.get("cwd"))
            added, removed = claude_code.edit_line_counts(name, tinput, r)
            rel = os.path.relpath(path, root) if os.path.isabs(path) and os.path.isabs(root) else path
            return ctx.event(rec["_t"], "ai_edit", rec.get("cwd"), prompt_id=rec.get("promptId"), tool=name,
                             file=rel.replace("\\", "/"), lines_added=added, lines_removed=removed)
    return None


def transcript_events(path, tz, edits_only=False):
    recs = _read(path)
    if not recs:
        return []
    session = next((r.get("sessionId") for r in recs if r.get("sessionId")), os.path.basename(path)[:-6])
    first_cwd = next((r.get("cwd") for r in recs if r.get("cwd")), None)
    ctx = _Ctx(tz, session, first_cwd)

    tool_uses = {}
    for r in recs:
        c = _blocks(r)
        if r.get("type") == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_uses[b.get("id")] = (b.get("name"), b.get("input") or {})

    out = []
    if not edits_only:
        out.append(ctx.event(recs[0]["_t"], "session_start", first_cwd, reason="import"))

    turn = None  # {"prompt": rec, "last_ai": rec, "texts": [...], "closed": bool}

    def close_turn(at=None):
        # Ends at Claude Code's own turn_duration record when there is one (the
        # terminal writes it); otherwise at the turn's last assistant message.
        if turn and not turn["closed"] and turn["last_ai"] is not None:
            turn["closed"] = True
            text_chars, code_lines = claude_code.response_size("\n".join(turn["texts"]))
            out.append(ctx.event(at or turn["last_ai"]["_t"], "ai_done", turn["last_ai"].get("cwd"),
                                 prompt_id=turn["prompt"].get("promptId"),
                                 response_text_chars=text_chars, response_code_lines=code_lines))

    for r in recs:
        if not edits_only:
            text = prompt_text(r)
            if text is not None:
                close_turn()
                pasted = sum(len(m) for m in claude_code._PASTE_RE.findall(text))
                typed = len(claude_code._PASTE_RE.sub("", text))
                out.append(ctx.event(r["_t"], "prompt_sent", r.get("cwd"), prompt_id=r.get("promptId"),
                                     chars=typed, pasted_chars=pasted))
                turn = {"prompt": r, "last_ai": None, "texts": [], "closed": False}
                continue
            if (r.get("type") == "system" and r.get("subtype") == "turn_duration"
                    and turn is not None and not turn["closed"] and turn["last_ai"] is not None):
                close_turn(at=r["_t"])
                continue
        # Work after the turn ended without a new typed prompt (a background task's
        # notification, say) isn't stretched onto that turn.
        if r.get("type") == "assistant" and turn is not None and not turn["closed"]:
            turn["last_ai"] = r
            c = _blocks(r)
            if isinstance(c, list):
                turn["texts"].extend(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if not edits_only and r.get("type") == "system" and r.get("subtype") == "away_summary":
            out.append(ctx.event(r["_t"], "away", r.get("cwd")))
            continue
        if r.get("type") == "user":
            ev = _edit_event(ctx, r, tool_uses)
            if ev is not None:
                out.append(ev)

    if not edits_only:
        close_turn()
        out.append(ctx.event(recs[-1]["_t"], "session_end", recs[-1].get("cwd") or first_cwd, reason="import"))
    return out


TYPING_FIELDS = ("typing_ms", "compose_ms", "keys_typed", "pastes", "fixes", "rewrites", "revisions",
                 "pauses", "chars_deleted", "drafts_abandoned", "abandoned_typing_ms")


def _recorded_typing(log_path):
    """{prompt_id: typing fields} from this machine's live log, for prompts the typing module saw."""
    out = {}
    try:
        with open(log_path, "rb") as f:
            for raw in f:
                try:
                    e = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if e.get("event") == "prompt_sent" and e.get("prompt_id") and "typing_ms" in e:
                    out[e["prompt_id"]] = {k: e[k] for k in TYPING_FIELDS if k in e}
    except OSError:
        pass
    return out


def import_dir(root, out_path, utc_offset_h=None):
    """Write all events from transcripts under root to out_path. Returns (n_files, n_events)."""
    if utc_offset_h is None:
        tz = datetime.datetime.now().astimezone().tzinfo
    else:
        tz = datetime.timezone(datetime.timedelta(hours=utc_offset_h))
    events = []
    files = find_transcripts(root)
    for path, is_sub in files:
        events.extend(transcript_events(path, tz, edits_only=is_sub))
    # Prompts the live typing module also saw keep their recorded keystroke stats.
    # Prompt ids are unique, so this only ever matches this machine's own sessions.
    typing = _recorded_typing(eventlog.log_path())
    for e in events:
        if e.get("event") == "prompt_sent" and e.get("prompt_id") in typing:
            e.update(typing[e["prompt_id"]])
    events.sort(key=lambda e: e["ts"])
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    prev = None
    with open(out_path, "wb") as f:
        for e in events:
            e["prev_hash"] = eventlog.line_hash(prev) if prev else eventlog.GENESIS_HASH
            line = json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            f.write(line + b"\n")
            prev = line
    return len(files), len(events)
