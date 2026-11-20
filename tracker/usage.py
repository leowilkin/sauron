# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tokens and estimated spend per model, for every coding agent ccusage can read (Claude Code, Codex,
Gemini CLI, OpenCode, Amp and so on), sent to the Sauron server as daily totals.

Sauron itself only listens to Claude Code. ccusage (https://github.com/ryoppippi/ccusage) already reads
the other agents' local logs, so this runs it and sends the numbers: day, agent, model, token counts and
ccusage's cost estimate at list price. Never prompts, paths or project names.

It runs only when ccusage is installed (`npm install -g ccusage`), or when config.json names a command
in "usage_command". It never downloads ccusage by itself. Turn it off with {"usage": false}.

    python3 usage.py         # print what would be sent
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402

EVERY_S = 3600       # at most one ccusage run an hour: it reads every agent's logs, which takes a while
TIMEOUT_S = 180
RECENT_DAYS = 3      # after the first full run, resend the last few days, since today keeps growing
BATCH = 1000
NAME = re.compile(r"^[\w.:@/+-]{1,80}$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TOKEN_FIELDS = (("input", "inputTokens"), ("output", "outputTokens"), ("cache_write", "cacheCreationTokens"),
                ("cache_read", "cacheReadTokens"))


def command(cfg):
    """The ccusage command to run, as a list, or None when usage isn't to be collected."""
    if cfg.get("usage") is False:
        return None
    own = cfg.get("usage_command")
    if isinstance(own, str) and own.strip():
        return shlex.split(own)
    found = shutil.which("ccusage")
    return [found] if found else None


def rows_from(report):
    """Flatten `ccusage daily --json --by-agent` into [{day, agent, model, input, output, cache_write,
    cache_read, cost}], keeping only well-formed names and numbers."""
    out = []
    for d in (report or {}).get("daily") or []:
        day = d.get("period") or d.get("date")
        if not isinstance(day, str) or not DAY.match(day):
            continue
        for a in d.get("agents") or []:
            agent = a.get("agent")
            if not isinstance(agent, str) or not NAME.match(agent):
                continue
            for m in a.get("modelBreakdowns") or []:
                model = m.get("modelName")
                if not isinstance(model, str) or not NAME.match(model):
                    continue
                r = {"day": day, "agent": agent, "model": model}
                for k, src in TOKEN_FIELDS:
                    v = m.get(src)
                    r[k] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else 0
                c = m.get("cost")
                r["cost"] = round(float(c), 6) if isinstance(c, (int, float)) and not isinstance(c, bool) \
                    and c >= 0 else 0.0
                out.append(r)
    return out


def collect(cmd, since=None):
    args = list(cmd) + ["daily", "--json", "--by-agent"]
    if since:
        args += ["--since", since.replace("-", "")]
    out = subprocess.run(args, capture_output=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise OSError("ccusage exited with %d: %s" % (out.returncode,
                                                       out.stderr.decode("utf-8", "replace").strip()[-200:]))
    return rows_from(json.loads(out.stdout.decode("utf-8")))


def due(st, now=None):
    return (now or time.time()) - st.get("usage_run", 0) >= EVERY_S


def sync_usage(st, cfg, send, now=None, force=False):
    """Run ccusage if it's time and send its rows. Returns how many rows the server took, or -1 when skipped.
    Updates st (the sync state) but doesn't save it."""
    cmd = command(cfg)
    if not cmd or not (force or due(st, now)):
        return -1
    st["usage_run"] = now or time.time()  # also on failure, so a broken ccusage isn't rerun after every turn
    since = None
    if st.get("usage_full"):
        since = time.strftime("%Y-%m-%d", time.localtime((now or time.time()) - RECENT_DAYS * 86400))
    rows = collect(cmd, since)
    stored = 0
    for i in range(0, len(rows), BATCH):
        stored += int(send({"rows": rows[i:i + BATCH]}).get("stored", 0))
    st["usage_full"] = True
    st["usage_at"] = eventlog.now_iso()
    return stored


if __name__ == "__main__":
    import sync
    cmd = command(sync.config())
    if not cmd:
        print("ccusage isn't installed (npm install -g ccusage), or usage is turned off in config.json.")
        sys.exit(1)
    print(json.dumps(collect(cmd), indent=1))
