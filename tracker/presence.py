# SPDX-License-Identifier: AGPL-3.0-or-later
"""Presence sampler: were you at the computer, and what kind of app was in front?

Opt-in ({"presence": true} in ~/.ai-tracker/config.json). Started by the Claude
Code hooks, one copy at a time, and exits after IDLE_EXIT_S without Claude Code
activity. Every SAMPLE_S it reads:

  * input idle time: seconds since the last keyboard or mouse input
  * the KIND of app in front: terminal, editor, browser, ai_app, other, locked

It never stores app names, window titles or anything on screen: only the
category and the idle time. Lines go to ~/.ai-tracker/presence/YYYY-MM-DD.jsonl,
written when the category or active/idle state changes, plus a heartbeat every
HEARTBEAT_S.

    python3 presence.py run      # the loop (normally started by the hooks)
    python3 presence.py sample   # print one sample, for checking
"""
import datetime
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402

SAMPLE_S = 5
HEARTBEAT_S = 60
ACTIVE_IDLE_S = 60       # input within this many seconds counts as active
IDLE_EXIT_S = 30 * 60    # stop after this long without Claude Code activity

# App name (macOS), executable (Windows) or window class (Linux), lowercased -> category.
CATEGORIES = {
    "terminal": {
        "terminal", "iterm2", "iterm", "ghostty", "wezterm", "wezterm-gui", "alacritty", "kitty", "warp", "hyper", "tabby",
        "windowsterminal", "cmd", "powershell", "pwsh", "mintty", "conhost", "openconsole",
        "gnome-terminal-server", "gnome-terminal", "konsole", "xterm", "tilix", "terminator", "org.wezfurlong.wezterm",
        "foot", "xfce4-terminal", "ptyxis", "com.mitchellh.ghostty",
    },
    "editor": {
        "code", "visual studio code", "cursor", "windsurf", "zed", "sublime text", "sublime_text", "xcode", "nova", "bbedit",
        "intellij idea", "idea64", "pycharm", "pycharm64", "webstorm", "webstorm64", "clion", "clion64", "goland", "goland64",
        "rider", "rider64", "android studio", "studio64", "devenv", "macvim", "gvim", "emacs", "neovide", "kate", "gedit",
        "jetbrains-idea", "jetbrains-pycharm", "code-oss", "vscodium",
    },
    "browser": {
        "safari", "google chrome", "chrome", "arc", "firefox", "microsoft edge", "msedge", "brave browser", "brave",
        "opera", "vivaldi", "orion", "chromium", "dia", "zen", "zen browser", "google-chrome", "brave-browser", "librewolf",
    },
    "ai_app": {"claude", "chatgpt"},
    "locked": {"loginwindow", "screensaverengine", "lockapp"},
}
_LOOKUP = {name: cat for cat, names in CATEGORIES.items() for name in names}


def category(app):
    if not app:
        return "unknown"
    a = app.strip().lower()
    if a.endswith(".exe"):
        a = a[:-4]
    if a in _LOOKUP:
        return _LOOKUP[a]
    if a.startswith("jetbrains-"):
        return "editor"
    return "other"


def _run(argv, timeout=2):
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
        return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# ---------- macOS ----------

def _mac_idle():
    out = _run(["ioreg", "-c", "IOHIDSystem", "-d", "4", "-r", "-k", "HIDIdleTime"])
    if not out:
        return None
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            try:
                return int(line.rsplit("=", 1)[1].strip()) / 1e9
            except ValueError:
                return None
    return None


def _mac_front():
    asn = _run(["lsappinfo", "front"])
    if not asn or not asn.strip():
        return None
    out = _run(["lsappinfo", "info", "-only", "name", asn.strip()])
    if not out:
        return None
    # e.g.  "Terminal" ASN:0x0-0x9d09d: (in front)   or   "LSDisplayName"="Terminal"
    q = out.split('"')
    if len(q) >= 4 and q[1] in ("LSDisplayName", "name", "CFBundleName"):
        return q[3]
    return q[1] if len(q) >= 2 else None


# ---------- Windows ----------

def _win_idle():
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def _win_front():
    import ctypes
    from ctypes import wintypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return "lockapp"
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)
    return None


# ---------- Linux (X11; Wayland desktops don't expose the front app) ----------

def _linux_idle():
    out = _run(["xprintidle"])
    try:
        return int(out.strip()) / 1000.0 if out else None
    except ValueError:
        return None


def _linux_front():
    if not os.environ.get("DISPLAY"):
        return None
    out = _run(["xdotool", "getactivewindow", "getwindowclassname"])
    if out and out.strip():
        return out.strip()
    root = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    if not root or "#" not in root:
        return None
    wid = root.rsplit("#", 1)[1].strip().split(",")[0]
    cls = _run(["xprop", "-id", wid, "WM_CLASS"])
    if cls and '"' in cls:
        return cls.split('"')[-2]
    return None


def sample():
    """{"app": category, "idle": seconds or None}."""
    try:
        if sys.platform == "darwin":
            idle, front = _mac_idle(), _mac_front()
        elif os.name == "nt":
            idle, front = _win_idle(), _win_front()
        else:
            idle, front = _linux_idle(), _linux_front()
    except Exception:
        idle, front = None, None
    return {"app": category(front), "idle": None if idle is None else int(idle)}


# ---------- files, config, single instance ----------

def enabled():
    try:
        with open(os.path.join(eventlog.data_dir(), "config.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("presence"))
    except (OSError, ValueError, AttributeError):
        return False


def presence_dir():
    return os.path.join(eventlog.data_dir(), "presence")


def _pidfile():
    return os.path.join(eventlog.data_dir(), "presence.pid")


def _alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_pid():
    try:
        with open(_pidfile(), encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def ensure_running():
    """Start the sampler in the background if it's enabled and not already running."""
    if not enabled() or running_pid():
        return False
    argv = [sys.executable, "-I", "-S", os.path.abspath(__file__), "run"]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)
    return True


def _last_claude_activity():
    try:
        return os.path.getmtime(eventlog.log_path())
    except OSError:
        return 0


def _write(line, now):
    d = presence_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d") + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, separators=(",", ":")) + "\n")


def run():
    if running_pid():
        return
    os.makedirs(eventlog.data_dir(), exist_ok=True)
    with open(_pidfile(), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    last_state, last_write = None, 0.0
    try:
        while enabled() and time.time() - _last_claude_activity() < IDLE_EXIT_S:
            now = time.time()
            s = sample()
            active = s["idle"] is not None and s["idle"] < ACTIVE_IDLE_S
            state = (s["app"], active)
            if state != last_state or now - last_write >= HEARTBEAT_S:
                _write({"ts": eventlog.now_iso(), "app": s["app"], "idle": s["idle"], "active": active}, now)
                last_state, last_write = state, now
            time.sleep(SAMPLE_S)
        _write({"ts": eventlog.now_iso(), "app": "stopped", "idle": None, "active": False}, time.time())
    finally:
        try:
            if running_pid() in (None, os.getpid()):
                os.remove(_pidfile())
        except OSError:
            pass


def load(day_from=None):
    """All presence lines (newest days last), parsed, with "_t" datetimes."""
    out = []
    d = presence_dir()
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".jsonl"))
    except OSError:
        return out
    for n in names:
        if day_from and n[:10] < day_from:
            continue
        with open(os.path.join(d, n), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    r["_t"] = datetime.datetime.fromisoformat(r["ts"])
                    out.append(r)
                except (ValueError, KeyError):
                    continue
    out.sort(key=lambda r: r["_t"])
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sample"
    if cmd == "run":
        run()
    else:
        print(json.dumps(sample()))
