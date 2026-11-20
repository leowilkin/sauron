# SPDX-License-Identifier: AGPL-3.0-or-later
"""Estimated effort: the dashboard's model, in Python, for the server and the CLI.

Mirrors dashboard.html (dynParts, kindWeight/weightWith, allocMap) so a profile on the
server shows the same numbers as the local dashboard with default settings.
"""
import datetime

COUNTS = {"concept", "understand_ai_work", "code_and_explain", "review_pushback"}  # learning kinds
THINK_PER = 30.0   # seconds of thinking per point of complexity + own thinking
FULL_AT = 1.5      # own-thinking score that earns full credit
FLOOR = 1.0        # learning kinds earn at least this share


def you_seconds(r, per=THINK_PER, idle_min=15.0):
    """Your time before a prompt: reading x engagement + thinking + writing (see dynParts)."""
    if not r.get("pattern") or r.get("origin") in ("agent", "other"):
        return float(r.get("you_before_s") or 0)
    engage = 0.3 + 0.7 * (r.get("read_previous") or 0)
    read = 0.0 if r.get("gap_s") is None else min(r.get("read_s") or 0, idle_min * 60) * engage
    think = per * ((r.get("complexity") or 0) + (r.get("own_thinking") or 0))
    write = r.get("compose_s") or 0
    if r.get("gap_s") is None:
        return write + min(think, max(0.0, (r.get("you_before_s") or 0) - write))
    avail = r["present_s"] if r.get("present_s") is not None else r["gap_s"]
    rt = min(read + think, avail)
    return rt + (write if r.get("typing_ms") is not None else min(write, max(0.0, r["gap_s"] - rt)))


def _kind_weight(kind, thinking, full, floor):
    scaled = max(0.0, min(1.0, thinking / full))
    return max(floor, scaled) if kind in COUNTS else scaled


def weight(r, full=FULL_AT, floor=FLOOR):
    """Share of your time that counts as effort, averaged over Jev's probabilities for each kind."""
    if not r.get("pattern"):
        return 0.0
    th = r.get("own_thinking") or 0
    probs = r.get("pattern_probs") or {}
    total = sum(probs.values())
    if not total:
        return _kind_weight(r["pattern"], th, full, floor)
    return sum(p * _kind_weight(k, th, full, floor) for k, p in probs.items()) / total


def _ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def allocate(rows, per=THINK_PER):
    """{id(row): (your seconds, agent seconds)} with each moment counted once across sessions.

    Your interval ends at the prompt, the agent's starts there; your time beats the agent's,
    then the interval that started most recently wins (the Python time model's rule).
    """
    iv = []
    for r in rows:
        e = _ts(r["at"])
        y = you_seconds(r, per)
        if y > 0:
            iv.append((e - y, e, True, r))
        if r.get("ai_s"):
            iv.append((e, e + r["ai_s"], False, r))
    pts = sorted({x[0] for x in iv} | {x[1] for x in iv})
    by_start = sorted(iv, key=lambda x: x[0])
    out = {}
    active, i = [], 0
    for t0, t1 in zip(pts, pts[1:]):
        while i < len(by_start) and by_start[i][0] <= t0:
            active.append(by_start[i])
            i += 1
        active = [x for x in active if x[1] > t0]
        if not active:
            continue
        w = max(active, key=lambda x: (x[2], x[0], x[1]))
        you, ai = out.get(id(w[3]), (0.0, 0.0))
        out[id(w[3])] = (you + (t1 - t0), ai) if w[2] else (you, ai + (t1 - t0))
    return out


def totals(rows, per=THINK_PER):
    """Per (day, project): your time, agent time, estimated effort and prompts."""
    alloc = allocate(rows, per)
    out = {}
    for r in rows:
        you, ai = alloc.get(id(r), (0.0, 0.0))
        k = (r["day"], r.get("project") or "?")
        t = out.setdefault(k, {"you_s": 0.0, "ai_s": 0.0, "effort_s": 0.0, "prompts": 0, "judged": 0})
        t["you_s"] += you
        t["ai_s"] += ai
        t["effort_s"] += you * weight(r)
        t["prompts"] += 1
        t["judged"] += 1 if r.get("pattern") else 0
    return out
