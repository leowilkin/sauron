# SPDX-License-Identifier: AGPL-3.0-or-later
"""Append-only, hash-chained JSONL event log.

Tool-agnostic core. Any adapter (Claude Code hooks today; Cursor, Codex, etc.
later) builds an event dict and calls append_event(). This module handles:

  * the common fields (v, ts, source, project, cwd),
  * locating the project (git repo root, worktree-aware, no `git` subprocess),
  * cross-process locking so concurrent sessions don't interleave lines,
  * the prev_hash chain: each line carries sha256(previous line bytes).

Standard library only, Python 3.8+.
"""
import datetime
import json
import os
import sys
import time

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
LOCK_TIMEOUT_S = 1.0


def data_dir():
    d = os.environ.get("AI_TRACKER_DIR")
    if d:
        return os.path.expanduser(d)
    return os.path.join(os.path.expanduser("~"), ".ai-tracker")


def log_path():
    return os.path.join(data_dir(), "events.jsonl")


def now_iso():
    """Local time with UTC offset, second precision: 2026-11-18T23:04:10-05:00."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


try:  # the bare builtin skips hashlib's ~10ms of OpenSSL/blake2/sha3 imports
    from _sha2 import sha256 as _sha256  # Python 3.12+
except ImportError:
    try:
        from _sha256 import sha256 as _sha256  # Python <= 3.11
    except ImportError:
        from hashlib import sha256 as _sha256


def line_hash(line_bytes):
    return _sha256(line_bytes).hexdigest()


# --------------------------------------------------------------------------
# Project detection
# --------------------------------------------------------------------------

def find_project_root(path):
    """Return (root_dir, project_name) for `path`.

    Walks up looking for `.git` (a directory, or a file for worktrees and
    submodules). For a linked worktree the project is named after the main
    repository, so work in `repo-wt-feature/` still counts toward `repo`.
    Falls back to the folder itself when it is not inside a repo.
    """
    path = os.path.abspath(path or os.getcwd())
    cur = path
    while True:
        dotgit = os.path.join(cur, ".git")
        if os.path.isdir(dotgit):
            return cur, os.path.basename(cur) or cur
        if os.path.isfile(dotgit):
            return cur, _name_from_gitfile(dotgit, cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return path, os.path.basename(path) or path


def _name_from_gitfile(dotgit_file, root):
    # Worktree: "gitdir: /repos/main/.git/worktrees/feature"
    try:
        with open(dotgit_file, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
        if first.startswith("gitdir:"):
            gitdir = os.path.normpath(os.path.join(root, first[len("gitdir:"):].strip()))
            parts = gitdir.replace("\\", "/").split("/")
            if "worktrees" in parts:
                i = len(parts) - 1 - parts[::-1].index("worktrees")
                main_git = "/".join(parts[:i])  # .../main/.git
                if main_git.endswith("/.git"):
                    return os.path.basename(main_git[: -len("/.git")]) or os.path.basename(root)
    except OSError:
        pass
    return os.path.basename(root)


def relative_to(path, root):
    """Path relative to the project root, with forward slashes.

    Files outside the project keep a relative path (`../other/x.py`). On
    Windows, a file on a different drive has no relative path, so only its
    file name is kept rather than leaking an absolute path.
    """
    try:
        rel = os.path.relpath(os.path.abspath(path), root)
    except ValueError:
        rel = os.path.basename(path)
    return rel.replace("\\", "/")


# --------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------

class _FileLock(object):
    """Best-effort exclusive lock on a sidecar lock file.

    Waits at most LOCK_TIMEOUT_S. If the lock can't be taken we still write:
    losing an event is worse than a rare out-of-order chain, which `aitrack
    verify` will report.
    """

    def __init__(self, path):
        self.path = path
        self.fh = None
        self.locked = False

    def __enter__(self):
        try:
            self.fh = open(self.path, "a+b")
        except OSError:
            return self
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.locked = True
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.01)

    def __exit__(self, *exc):
        if self.fh is None:
            return False
        try:
            if self.locked:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        return False


# --------------------------------------------------------------------------
# Append
# --------------------------------------------------------------------------

def _read_tail(fh):
    """Return (last_complete_line_bytes or None, ends_with_newline)."""
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    if size == 0:
        return None, True
    fh.seek(size - 1)
    ends_nl = fh.read(1) == b"\n"
    end = size - 1 if ends_nl else size
    # Read backwards in chunks until we find the newline before the last line.
    buf = b""
    pos = end
    while pos > 0:
        step = min(4096, pos)
        pos -= step
        fh.seek(pos)
        buf = fh.read(step) + buf
        nl = buf.rfind(b"\n")
        if nl != -1:
            return buf[nl + 1 :], ends_nl
    return buf, ends_nl


def append_event(event, cwd=None, source="unknown"):
    """Fill common fields, chain-hash and append one JSON line. Returns the dict."""
    cwd = os.path.abspath(cwd or os.getcwd())
    root, project = find_project_root(cwd)

    record = {
        "v": SCHEMA_VERSION,
        "ts": now_iso(),
        "source": source,
        "session": event.pop("session", None),
        "event": event.pop("event"),
        "project": project,
    }
    record.update(event)
    record["cwd"] = cwd

    d = data_dir()
    os.makedirs(d, exist_ok=True)
    path = log_path()

    with _FileLock(path + ".lock"):
        with open(path, "a+b") as fh:
            last, ends_nl = _read_tail(fh)
            record["prev_hash"] = line_hash(last) if last else GENESIS_HASH
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            fh.seek(0, os.SEEK_END)
            fh.write((b"" if ends_nl else b"\n") + line + b"\n")
            fh.flush()
    return record


def project_of(path):
    return find_project_root(path)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    print(json.dumps(append_event({"event": "test"}, source="manual")))
    sys.exit(0)
