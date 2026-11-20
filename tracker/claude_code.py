# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claude Code hook adapter.

Reads one hook payload (JSON) on stdin, maps it to a generic tracker event and
appends it to the log. Never prints to stdout (stdout from some hooks is fed
to the model), never raises, always exits 0.

Privacy: prompt text, file contents and model output are read from stdin only
to count characters/lines and are never written anywhere.

Payload shapes were checked against real Claude Code 2.1.x hook input:
  Edit / Write(update) tool_response.structuredPatch = [{"lines": [" a", "-b", "+B"]}, ...]
  Write(create)        tool_response.type == "create", structuredPatch == []
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402
import typingstats  # noqa: E402

SOURCE = "claude-code"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Claude Code wraps pasted text in <pasted_content ...>...</pasted_content ...>.
# We count it separately so "typed by the user" and "pasted in" can be told apart.
_PASTE_RE = re.compile(r"<pasted_content\b[^>]*>(.*?)</pasted_content\b[^>]*>", re.S)


def count_lines(text):
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _diff_counts(old, new):
    """Fallback line diff when no structuredPatch is available."""
    old_l = old.splitlines()
    new_l = new.splitlines()
    if len(old_l) + len(new_l) > 20000:  # keep hooks fast on huge files
        return len(new_l), len(old_l)
    import difflib

    added = removed = 0
    sm = difflib.SequenceMatcher(None, old_l, new_l, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _patch_counts(patch):
    added = removed = 0
    for hunk in patch:
        for line in hunk.get("lines", ()):
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    return added, removed


def edit_line_counts(tool_name, tool_input, tool_response):
    """Return (lines_added, lines_removed) for a file-editing tool call."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    tr = tool_response if isinstance(tool_response, dict) else {}

    patch = tr.get("structuredPatch")
    if isinstance(patch, list) and patch:
        return _patch_counts(patch)

    if tool_name == "Write":
        content = ti.get("content", "")
        original = tr.get("originalFile")
        if tr.get("type") == "create" or original is None:
            return count_lines(content), 0
        return _diff_counts(original, content)

    if tool_name == "Edit":
        a, r = _diff_counts(ti.get("old_string", ""), ti.get("new_string", ""))
        return a, r

    if tool_name == "MultiEdit":
        a = r = 0
        for e in ti.get("edits", []) or []:
            ea, er = _diff_counts(e.get("old_string", ""), e.get("new_string", ""))
            a += ea
            r += er
        return a, r

    if tool_name == "NotebookEdit":
        mode = ti.get("edit_mode", "replace")
        n = count_lines(ti.get("new_source", ""))
        if mode == "insert":
            return n, 0
        if mode == "delete":
            return 0, 0  # deleted cell size isn't in the payload
        return n, 0

    return 0, 0


def response_size(message):
    """(prose chars, code lines) of the AI's final message. Counts only.

    Lines inside ``` / ~~~ fences are code; everything else is prose. The fence
    lines themselves count as neither.
    """
    if not isinstance(message, str) or not message:
        return 0, 0
    text = code = 0
    fence = None
    for line in message.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ("```", "~~~") and (fence is None or marker == fence):
            fence = None if fence else marker
            continue
        if fence:
            code += 1
        else:
            text += len(line) + 1
    return max(text - 1, 0), code


def _start_presence():
    """Start the opt-in presence sampler if it's enabled and not running. Never fails the hook."""
    try:
        import presence
        presence.ensure_running()
    except Exception as e:
        _log_error(e)


def _spawn(argv):
    """Start a detached background process that outlives the hook."""
    import subprocess
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, "-I", "-S"] + argv, **kw)


def _start_jev(paths):
    """Label new prompts with Jev in the background, if a Hack Club AI key is set up. Never fails the hook."""
    try:
        if not (os.environ.get("JEV_API_KEY") or os.path.exists(os.path.join(eventlog.data_dir(), "jev.key"))):
            return
        paths = [p for p in paths if p]
        if paths:
            _spawn([os.path.join(os.path.dirname(os.path.abspath(__file__)), "jev_judge.py"), "--auto"] + paths)
    except Exception as e:
        _log_error(e)


def _start_sync():
    """Send new events to the ai-tracker server in the background, if connected. Never fails the hook."""
    try:
        if os.path.exists(os.path.join(eventlog.data_dir(), "server.key")):
            _spawn([os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.py")])
    except Exception as e:
        _log_error(e)


def handle(payload):
    ev_name = payload.get("hook_event_name")
    cwd = payload.get("cwd") or os.getcwd()
    base = {"session": payload.get("session_id")}
    if payload.get("prompt_id"):
        base["prompt_id"] = payload["prompt_id"]

    if ev_name in ("SessionStart", "UserPromptSubmit"):
        _start_presence()
    if ev_name == "SessionStart":
        typingstats.sweep()
        # catch up on prompts missed while offline (only transcripts touched since the key was saved)
        _start_jev([os.path.join(os.path.expanduser("~"), ".claude", "projects")])
        ev = dict(base, event="session_start", reason=payload.get("source"))
    elif ev_name == "SessionEnd":
        ev = dict(base, event="session_end", reason=payload.get("reason"))
        ev.update(typingstats.for_session_end(payload.get("session_id")))
    elif ev_name == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        pasted = sum(len(m) for m in _PASTE_RE.findall(prompt))
        typed = len(_PASTE_RE.sub("", prompt))
        ev = dict(base, event="prompt_sent", chars=typed, pasted_chars=pasted)
        ev.update(typingstats.for_prompt(payload.get("session_id"), prompt_id=payload.get("prompt_id")))
    elif ev_name == "PostToolUse":
        tool = payload.get("tool_name")
        if tool not in EDIT_TOOLS:
            return None
        ti = payload.get("tool_input") or {}
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if not path:
            return None
        root, _ = eventlog.find_project_root(cwd)
        added, removed = edit_line_counts(tool, ti, payload.get("tool_response"))
        ev = dict(
            base,
            event="ai_edit",
            tool=tool,
            file=eventlog.relative_to(path, root),
            lines_added=added,
            lines_removed=removed,
        )
    elif ev_name == "Stop":
        _start_jev([payload.get("transcript_path")])
        text_chars, code_lines = response_size(payload.get("last_assistant_message"))
        ev = dict(base, event="ai_done", response_text_chars=text_chars, response_code_lines=code_lines)
    elif ev_name == "SubagentStop":
        ev = dict(base, event="subagent_done", agent_type=payload.get("agent_type"))
    else:
        return None

    if payload.get("agent_id"):
        ev["agent_id"] = payload["agent_id"]
    out = eventlog.append_event(ev, cwd=cwd, source=SOURCE)
    if ev_name in ("SessionStart", "Stop", "SessionEnd"):
        _start_sync()  # after the append, so this event goes too
    return out


def main():
    if os.environ.get("AI_TRACKER_DISABLE") == "1":
        return
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(payload, dict):
            handle(payload)
    except Exception as e:  # never break Claude Code
        _log_error(e)


def _log_error(e):
    try:
        d = eventlog.data_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "errors.log"), "a", encoding="utf-8") as f:
            f.write("%s %s: %s\n" % (eventlog.now_iso(), type(e).__name__, e))
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
