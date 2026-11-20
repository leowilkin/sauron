#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run a tracker adapter (tracker/claude_code.py, or tracker/amp.py) with a real Python 3 (3.8+).
# Claude Code runs hooks with bash on every OS (Git Bash on Windows).
# Always exits 0 so a missing Python can never break a Claude Code session.

src="${BASH_SOURCE[0]//\\//}"  # Windows callers (the Amp plugin's path.join) pass C:\...\run.sh
here="${src%/*}"                # no subshell: forks are slow on Windows
[ "$here" = "$src" ] && here=.
# Which adapter: `run.sh amp` for the Amp plugin; no argument for the Claude Code hooks.
case "${1:-}" in amp) adapter=amp ;; *) adapter=claude_code ;; esac
script="$here/../tracker/$adapter.py"
dir="${AI_TRACKER_DIR:-$HOME/.ai-tracker}"
cache="$dir/.python"

# Git Bash hands us /c/Users/...; native Windows python.exe needs C:\Users\...
if command -v cygpath >/dev/null 2>&1; then
    script="$(cygpath -w "$script")"
fi

# -I: isolated (ignore PYTHON* env vars, user site, script dir on sys.path)
# -S: skip `site` (~30ms faster startup; we use the standard library only)
# -X utf8: UTF-8 I/O regardless of the Windows code page
run() { "$@" -I -S -X utf8 "$script" || true; exit 0; }

# Fast path: interpreter resolved on an earlier run.
if [ -r "$cache" ]; then
    IFS= read -r py < "$cache"
    [ -n "$py" ] && [ -x "$py" ] && run "$py"
fi

for name in python3 python py; do
    exe="$(command -v "$name" 2>/dev/null)" || continue
    case "$exe" in
        # Windows "App Installer" stub that opens the Microsoft Store: not a Python.
        *[Ww]indows[Aa]pps*) continue ;;
    esac
    [ "$name" = py ] && set -- "$exe" -3 || set -- "$exe"
    # Resolve shims (macOS /usr/bin/python3, pyenv) to the real binary once, and cache it.
    real="$("$@" -I -S -c 'import sys; sys.version_info >= (3, 8) and print(sys.executable)' 2>/dev/null)"
    real="${real%$'\r'}"
    [ -n "$real" ] || continue
    if command -v cygpath >/dev/null 2>&1; then real="$(cygpath -u "$real")"; fi
    mkdir -p "$dir" 2>/dev/null && printf '%s\n' "$real" > "$cache" 2>/dev/null
    run "$real"
done

mkdir -p "$dir" 2>/dev/null && echo "$(date) ai-tracker: no Python 3.8+ found on PATH" >> "$dir/errors.log" 2>/dev/null
cat >/dev/null  # drain stdin
exit 0
