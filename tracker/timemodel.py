# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn the event log into time.

Step 1: each session becomes a list of segments, each one "ai" or "you".

    session_start ──you──▶ prompt ──ai──▶ ai_done ──you──▶ prompt ──ai──▶ ai_done ──you──▶ session_end

  ai   prompt_sent → its ai_done. The AI is working and you're waiting or
       watching. Not capped: a long agent run is real wall-clock time. It's
       reported separately so a reviewer can weigh it.
  ai (interrupted)  a prompt with no ai_done before the next prompt or
       session_end (Esc, crash). Counted up to `idle`, since we don't know
       when it actually stopped.
  you  session_start → first prompt: counted if at most `idle`.
  you  typing: a prompt's typing_ms (plus drafts given up before it), as a span
       ending when it was sent. Counted whatever the gap, since keystrokes are
       direct evidence. Only Claude Code's terminal records it (hooks/typing/).
       ai_done → next prompt (or → session_end): you're reading, testing,
       thinking and typing. How much counts depends on the turn's read time
       (see below).

Read time. Each finished turn gets an estimate of how long it takes to read
what the AI produced:

    read_s = response prose chars / READ_CHARS_PER_S
           + (code lines in the response + diff lines it wrote) * CODE_S_PER_LINE

capped at MAX_READ_S. The gap after ai_done is then credited like this:

    gap <= max(idle, read_s)  → the whole gap            (reading a big diff isn't a break)
    gap >  max(idle, read_s)  → min(read_s, idle) only   (glanced at it, then left)

The break credit is capped at `idle`: real data had one prompt, a 72-minute
agent run writing 4,000 lines, then a 36-hour absence. Crediting the full
(1h-capped) read time there gave an hour of "you" time for walking away.

Each reply gap is also kept as a "read" observation (gap vs read_s), so a
report can flag replies far faster than the reading would take.

Step 2: segments from every session are laid on one timeline, and each moment
is credited to exactly one project, so a day's total can never exceed wall-clock
time, even with 5 sessions open at once. Where segments overlap:

  1. a "you" segment beats an "ai" segment (you can only think about one thing,
     but agents can run in parallel), then
  2. the segment that started most recently wins (that's where your attention
     went last).

Time that crosses midnight is split between the two local days.
"""
import datetime
from collections import defaultdict

YOU = "you"
AI = "ai"

# Reading-speed assumptions. Deliberately skim-level, not careful-review speeds.
READ_CHARS_PER_S = 20.0  # ~240 words/min silent reading at ~5 chars/word
CODE_S_PER_LINE = 2.0    # ~30 lines/min skim; careful review is ~5-10 lines/min
MAX_READ_S = 3600.0      # nobody gets more than an hour of credit for reading one reply

# Time between the AI finishing and your next prompt counts only as far as there's
# evidence for it: reading the reply, a short allowance to think, and writing the
# next prompt. The rest of the gap may have been spent anywhere, so it doesn't count.
THINK_S = 60.0           # thinking allowance after reading, per reply
TYPE_CHARS_PER_S = 3.5   # fallback typing pace when a person has no recorded keystrokes (~40 wpm)
PACE_MIN_KEYS = 20       # prompts this long or longer give a usable pace sample
PACE_MIN_SAMPLES = 3     # samples needed before trusting a personal pace
# Claude Code writes an away_summary about 3 minutes after you stop attending a session
# (seen in its transcripts); when one falls in a gap, you'd left 3 minutes before it.
AWAY_LEAD_S = 180.0

# A reply counts as "fast" when it came in under FAST_RATIO of the read time,
# and only for turns that need at least FAST_MIN_READ_S to read.
FAST_RATIO = 0.25
FAST_MIN_READ_S = 30.0


def personal_pace(events):
    """(chars per second, samples) from this person's recorded typing; the fallback pace if too few.

    Median of keys typed / typing time over prompts with at least PACE_MIN_KEYS keys. Typing time
    includes short pauses (under 30 s), so this is writing pace, thinking-while-typing included.
    """
    rates = sorted(int(e["keys_typed"]) / (int(e["typing_ms"]) / 1000.0)
                   for e in events
                   if e.get("event") == "prompt_sent" and int(e.get("typing_ms") or 0) > 0
                   and int(e.get("keys_typed") or 0) >= PACE_MIN_KEYS)
    if len(rates) < PACE_MIN_SAMPLES:
        return TYPE_CHARS_PER_S, len(rates)
    n = len(rates)
    return (rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2), n


def compose_seconds(prompt_event, pace=TYPE_CHARS_PER_S):
    """Time spent writing a prompt: recorded typing when there is any, else typed chars at this person's pace."""
    if "typing_ms" in prompt_event:
        s = int(prompt_event.get("typing_ms") or 0) / 1000.0
    else:
        s = int(prompt_event.get("chars") or 0) / (pace or TYPE_CHARS_PER_S)
    return s + int(prompt_event.get("abandoned_typing_ms") or 0) / 1000.0


def read_seconds(done_event, diff_lines):
    text = int(done_event.get("response_text_chars") or 0)
    code = int(done_event.get("response_code_lines") or 0)
    return min(text / READ_CHARS_PER_S + (code + diff_lines) * CODE_S_PER_LINE, MAX_READ_S)


class Segment(object):
    __slots__ = ("start", "end", "kind", "project", "session")

    def __init__(self, start, end, kind, project, session):
        self.start, self.end, self.kind, self.project, self.session = start, end, kind, project, session

    def __repr__(self):
        return "Segment(%s..%s %s %s)" % (self.start.time(), self.end.time(), self.kind, self.project)


class Read(object):
    """One ai_done → next prompt gap, compared with the turn's read time."""
    __slots__ = ("at", "day", "project", "gap_s", "read_s", "is_break", "credited_s", "away", "present_s")

    def __init__(self, at, project, gap_s, read_s, is_break, credited_s=0.0, away=False, present_s=None):
        self.at = at  # ts of the ai_done this gap follows
        self.day = at[:10]
        self.project, self.gap_s, self.read_s, self.is_break = project, gap_s, read_s, is_break
        self.credited_s = credited_s  # "you" time this gap earned: reading + thinking + writing
        self.away = away              # Claude Code logged that you were away during it
        self.present_s = gap_s if present_s is None else present_s  # gap time before you left, if you did

    @property
    def fast(self):
        return self.read_s >= FAST_MIN_READ_S and self.gap_s < FAST_RATIO * self.read_s


def _diff_lines_by_prompt(evs):
    out = defaultdict(int)
    for e in evs:
        if e.get("event") == "ai_edit" and e.get("prompt_id"):
            out[e["prompt_id"]] += int(e.get("lines_added") or 0) + int(e.get("lines_removed") or 0)
    return out


def calibrated(name, default):
    """A personal constant saved by `aitrack presence --fit --apply` in config.json, or the default."""
    import json
    import os
    data = os.path.expanduser(os.environ.get("AI_TRACKER_DIR") or os.path.join("~", ".ai-tracker"))
    try:
        with open(os.path.join(data, "config.json"), encoding="utf-8") as f:
            v = json.load(f).get("calibration", {}).get(name)
        return float(v) if v is not None else default
    except (OSError, ValueError, AttributeError, TypeError):
        return default


def session_segments(evs, idle_s, reads=None, pace=TYPE_CHARS_PER_S, think_s=THINK_S):
    """Segments for one session's events, which must already be sorted by time.

    If `reads` is a list, a Read is appended for every ai_done → next prompt.
    """
    segs = []
    idle = datetime.timedelta(seconds=idle_s)
    by_prompt = _diff_lines_by_prompt(evs)
    pending = None    # prompt_sent awaiting ai_done
    turn_diff = 0     # diff lines for the pending turn from edits with no prompt_id
    done = None       # (ai_done event, read_s) awaiting the next prompt
    started = None    # session_start awaiting the first prompt
    away_from = None  # when an away_summary in the current gap says you'd left

    def add(a, b, kind, e):
        if b > a:
            segs.append(Segment(a, b, kind, e.get("project") or "?", e.get("session")))

    def close_interrupted(t):
        a = pending["_t"]
        add(a, min(t, a + idle), AI, pending)

    def after_done(t, prompt=None):
        """Credit the gap after a reply: reading + thinking (until you left), then writing the prompt."""
        e, read_s = done
        a = e["_t"]
        gap = (t - a).total_seconds()
        is_break = gap > max(idle_s, read_s)
        present_end = t if away_from is None else min(t, max(a, away_from))
        read_end = min(a + datetime.timedelta(seconds=min(read_s, idle_s) + think_s), present_end)
        add(a, read_end, YOU, e)
        credited = max(0.0, (read_end - a).total_seconds())
        if prompt is not None:
            c = compose_seconds(prompt, pace)
            if "typing_ms" in prompt:
                # Recorded keystrokes count even if you started typing while the AI was still working.
                w0 = t - datetime.timedelta(seconds=c)
            else:
                w0 = max(a, read_end, t - datetime.timedelta(seconds=c))
            add(w0, t, YOU, prompt)
            overlap = max(0.0, (min(read_end, t) - max(a, w0)).total_seconds())
            credited += max(0.0, (t - w0).total_seconds()) - overlap
            if reads is not None and gap >= 0:
                reads.append(Read(e["ts"], e.get("project") or "?", gap, read_s, is_break,
                                  credited, away_from is not None,
                                  max(0.0, (present_end - a).total_seconds())))

    for e in evs:
        kind, t = e.get("event"), e["_t"]
        if kind == "session_start":
            if pending is not None:
                close_interrupted(t)
            pending, done, started, away_from = None, None, e, None
        elif kind == "away":
            if done is not None or started is not None:
                away_from = t - datetime.timedelta(seconds=AWAY_LEAD_S)
        elif kind == "prompt_sent":
            if pending is not None:
                close_interrupted(t)
            if done is not None and pending is None:
                after_done(t, prompt=e)
            else:
                # First prompt of a session, or typed while the AI was still working:
                # a short thinking allowance (after a session start) plus writing it.
                c = compose_seconds(e, pace)
                a = t - datetime.timedelta(seconds=c)
                if started is not None and pending is None:
                    a = max(started["_t"], t - datetime.timedelta(seconds=c + think_s))
                    if away_from is not None:
                        a = max(a, t - datetime.timedelta(seconds=c))
                add(a, t, YOU, e)
            pending, done, started, turn_diff, away_from = e, None, None, 0, None
        elif kind == "ai_edit":
            if pending is not None and not e.get("prompt_id"):
                turn_diff += int(e.get("lines_added") or 0) + int(e.get("lines_removed") or 0)
        elif kind == "ai_done":
            pid_p, pid_d = (pending or {}).get("prompt_id"), e.get("prompt_id")
            if pending is not None and pid_p and pid_d and pid_p != pid_d:
                # This completion belongs to another prompt: don't credit a turn we can't match.
                close_interrupted(t)
                pending = None
            if pending is not None:
                add(pending["_t"], t, AI, pending)
                diff = by_prompt.get(pending.get("prompt_id"), 0) if pending.get("prompt_id") else turn_diff
                done = (e, read_seconds(e, diff))
            else:
                done = (e, read_seconds(e, 0))
            pending, started, away_from = None, None, None
        elif kind == "session_end":
            if pending is not None:
                close_interrupted(t)
            elif done is not None:
                after_done(t)
            pending, done, started, away_from = None, None, None, None
    return segs


def all_segments(events, idle_s, reads=None, think_s=None):
    pace, _ = personal_pace(events)
    by_session = defaultdict(list)
    for e in events:
        by_session[e.get("session")].append(e)
    segs = []
    for evs in by_session.values():
        evs.sort(key=lambda e: (e["_t"], e.get("_line", 0)))
        segs.extend(session_segments(evs, idle_s, reads, pace,
                                     calibrated("think_s", THINK_S) if think_s is None else think_s))
    return segs


def _winner(active):
    # "you" beats "ai", then the most recent start, then session id so the result is deterministic.
    return max(active, key=lambda s: (s.kind == YOU, s.start, s.session or ""))


def _split_days(a, b):
    """Yield (local_date_iso, seconds) for [a, b), split at local midnight in a's timezone."""
    while a < b:
        midnight = datetime.datetime.combine(a.date() + datetime.timedelta(days=1), datetime.time(), a.tzinfo)
        end = min(b, midnight)
        yield a.date().isoformat(), (end - a).total_seconds()
        a = end


def attribute(segs):
    """Return {(day, project): {"you": seconds, "ai": seconds}} with no double counting."""
    out = defaultdict(lambda: {YOU: 0.0, AI: 0.0})
    if not segs:
        return out
    points = sorted({s.start for s in segs} | {s.end for s in segs})
    by_start = sorted(segs, key=lambda s: s.start)
    active, i = [], 0
    for t0, t1 in zip(points, points[1:]):
        while i < len(by_start) and by_start[i].start <= t0:
            active.append(by_start[i])
            i += 1
        active = [s for s in active if s.end > t0]
        if not active:
            continue
        w = _winner(active)
        for day, secs in _split_days(t0, t1):
            out[(day, w.project)][w.kind] += secs
    return out


def analyze(events, idle_min=15.0, think_s=None):
    """Return (credited time by (day, project), list of Read observations)."""
    reads = []
    segs = all_segments(events, idle_min * 60, reads, think_s)
    return attribute(segs), reads


def credited_time(events, idle_min=15.0):
    return analyze(events, idle_min)[0]
