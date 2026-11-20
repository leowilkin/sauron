# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amp adapter.

The Amp plugin (amp/sauron/index.ts) sends one JSON payload per Amp event on
stdin through hooks/run.sh. This maps it to a tracker event and appends it to
the log, the same way claude_code.py does for Claude Code hooks. Never prints,
never raises, always exits 0.

What Amp lets a plugin see, and so what this adapter can report:

  session_start  every time Amp starts a thread session (a new thread, or
                 opening an existing one). Carries caps, below.
  prompt_sent    chars only. Amp can't tell typed from pasted text, and records
                 no keystrokes, so pasted_chars and the typing fields are left
                 out (unknown), never 0. origin is "agent" for prompts in a
                 child thread, which another agent wrote; otherwise it's left
                 out, since Amp doesn't say who sent a prompt.
  ai_edit        edit_file, create_file, and apply_patch on one file are
                 counted. A multi-file apply_patch has no line counts (unknown).
  ai_done        reply size of the turn's last assistant message.

Amp has no session end, no away signal and no subagent stop, so CAPS lists
only ai_edit among the optional event kinds. The time model uses that to give
the last reply in a thread its break credit instead of nothing.

Privacy: prompt text, edit contents and reply text arrive on stdin only to be
counted and are never written anywhere.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402
import claude_code  # noqa: E402

SOURCE = "amp"
CAPS = ["ai_edit"]  # optional event kinds Amp reports (no session_end, away or subagent_done)
ORIGINS = ("you", "agent", "other")


def _patch_counts(patch):
    """(added, removed) in a one-file patch, unified or Amp/Codex style. File header lines don't count."""
    added = removed = 0
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "***")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def edit_counts(tool, ti, files):
    """(lines_added, lines_removed), or None when this edit can't be counted."""
    ti = ti if isinstance(ti, dict) else {}
    if tool == "edit_file":
        return claude_code._diff_counts(ti.get("old_str") or "", ti.get("new_str") or "")
    if tool == "create_file":
        return claude_code.count_lines(ti.get("content") or ""), 0
    if tool == "apply_patch" and files == 1:
        patch = next((v for v in (ti.get("patch"), ti.get("input")) if isinstance(v, str)), None)
        if patch is not None:
            return _patch_counts(patch)
    return None


def handle(p):
    """Append the events for one plugin payload. Returns the records written."""
    kind = p.get("event")
    cwd = p.get("cwd") or os.getcwd()
    base = {"session": p.get("session")}
    if p.get("prompt_id"):
        base["prompt_id"] = str(p["prompt_id"])

    evs = []
    if kind == "session_start":
        evs.append(dict(base, event="session_start", caps=CAPS))
    elif kind == "prompt":
        ev = dict(base, event="prompt_sent", chars=len(p.get("prompt") or ""))
        if p.get("origin") in ORIGINS:
            ev["origin"] = p["origin"]
        evs.append(ev)
    elif kind == "edit":
        paths = [x for x in (p.get("paths") or []) if isinstance(x, str) and x]
        root, _ = eventlog.find_project_root(cwd)
        counts = edit_counts(p.get("tool"), p.get("input"), len(paths))
        for path in paths:
            ev = dict(base, event="ai_edit", tool=p.get("tool"), file=eventlog.relative_to(path, root))
            if counts is not None:
                ev["lines_added"], ev["lines_removed"] = counts
            evs.append(ev)
    elif kind == "done":
        text_chars, code_lines = claude_code.response_size(p.get("text"))
        ev = dict(base, event="ai_done", response_text_chars=text_chars, response_code_lines=code_lines)
        if p.get("status") in ("error", "cancelled"):
            ev["reason"] = p["status"]
        evs.append(ev)

    out = [eventlog.append_event(ev, cwd=cwd, source=SOURCE) for ev in evs]
    if kind in ("session_start", "done"):
        claude_code._start_sync()  # after the append, so this event goes too
    return out


def main():
    if os.environ.get("AI_TRACKER_DISABLE") == "1":
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict):
            handle(payload)
    except Exception as e:  # never break Amp
        claude_code._log_error(e)


if __name__ == "__main__":
    main()
    sys.exit(0)
