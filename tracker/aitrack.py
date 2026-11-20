#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""aitrack: read ~/.ai-tracker/events.jsonl.

  sauron summarize [--days N | --since YYYY-MM-DD] [--project NAME] [--idle MIN] [--json] [--top N]
  sauron verify
  sauron import [DIR] [--out FILE] [--utc-offset H]
  sauron serve [FILE_OR_DIR ...] [--port 8765] [--idle MIN] [--no-open]

summarize, per project per day:
  * credited time: "you" (reading, thinking, typing between turns) plus "AI
    alone" (the AI working while you weren't engaged anywhere else), with no
    double counting across parallel sessions. See timemodel.py.
  * prompts sent, characters typed (and pasted, counted separately)
  * prompt -> ai_done cycles and the median AI response time
  * median user response time (ai_done -> next prompt in the same session)
    next to the median estimated read time of what the AI produced, and how
    many replies came far faster than that reading would take; gaps longer
    than max(--idle, read time) are breaks, not response time
  * AI-written lines added/removed per file

verify: recompute the prev_hash chain and report any line that was edited,
removed or inserted.
"""
import argparse
import datetime
import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402
import timemodel  # noqa: E402


def parse_ts(s):
    return datetime.datetime.fromisoformat(s)


def load(path):
    events, bad = [], 0
    with open(path, "rb") as f:
        for i, raw in enumerate(f, 1):
            raw = raw.rstrip(b"\r\n")
            if not raw:
                continue
            try:
                e = json.loads(raw.decode("utf-8"))
                e["_t"] = parse_ts(e["ts"])
                e["_line"] = i
                events.append(e)
            except (ValueError, KeyError, TypeError):
                bad += 1
    return events, bad


# --------------------------------------------------------------------------
# summarize
# --------------------------------------------------------------------------

def _bucket():
    return {
        "prompts": 0,
        "chars": 0,
        "pasted_chars": 0,
        "cycle_secs": [],
        "interrupted": 0,
        "user_secs": [],
        "read_secs": [],
        "reads": [],
        "fast": 0,
        "breaks": 0,
        "files": defaultdict(lambda: [0, 0]),
        "sessions": set(),
        "you_s": 0.0,
        "ai_s": 0.0,
        "typed_prompts": 0,
        "typing_ms": 0,
        "keys_typed": 0,
        "fixes": 0,
        "rewrites": 0,
        "revisions": 0,
        "drafts_abandoned": 0,
    }


def summarize(events, idle_min=15.0):
    """Return {(day, project): bucket}. Day = local date recorded in ts."""
    out = defaultdict(_bucket)
    by_session = defaultdict(list)
    for e in events:
        by_session[e.get("session")].append(e)

    for sess, evs in by_session.items():
        evs.sort(key=lambda e: (e["_t"], e["_line"]))
        pending = None    # open prompt awaiting ai_done
        for e in evs:
            kind = e.get("event")
            key = (e["ts"][:10], e.get("project") or "?")
            b = out[key]
            b["sessions"].add(sess)

            if kind == "prompt_sent":
                b["prompts"] += 1
                b["chars"] += int(e.get("chars") or 0)
                b["pasted_chars"] += int(e.get("pasted_chars") or 0)
                if "typing_ms" in e:  # absent = this tool can't see keystrokes, not zero
                    b["typed_prompts"] += 1
                    for k in ("typing_ms", "keys_typed", "fixes", "rewrites", "revisions"):
                        b[k] += int(e.get(k) or 0)
                b["drafts_abandoned"] += int(e.get("drafts_abandoned") or 0)
                if pending is not None:
                    # Previous turn never reached ai_done (user hit Esc, crash).
                    out[pending[1]]["interrupted"] += 1
                pending = (e, key)

            elif kind == "ai_done":
                if pending is not None:
                    p, pkey = pending
                    same = not (p.get("prompt_id") and e.get("prompt_id")) or p["prompt_id"] == e["prompt_id"]
                    if same:
                        out[pkey]["cycle_secs"].append((e["_t"] - p["_t"]).total_seconds())
                    else:
                        # A completion for a different prompt: the open turn never finished.
                        out[pkey]["interrupted"] += 1
                pending = None

            elif kind == "ai_edit":
                f = b["files"][e.get("file") or "?"]
                f[0] += int(e.get("lines_added") or 0)
                f[1] += int(e.get("lines_removed") or 0)

            elif kind in ("session_start", "session_end"):
                b["drafts_abandoned"] += int(e.get("drafts_abandoned") or 0)
                if pending is not None and kind == "session_end":
                    out[pending[1]]["interrupted"] += 1
                pending = None

    credited, reads = timemodel.analyze(events, idle_min)
    for key, t in credited.items():
        out[key]["you_s"] += t[timemodel.YOU]
        out[key]["ai_s"] += t[timemodel.AI]
    for r in reads:
        b = out[(r.day, r.project)]
        b["reads"].append(r)
        if r.is_break:
            b["breaks"] += 1
            continue
        b["user_secs"].append(r.gap_s)
        b["read_secs"].append(r.read_s)
        b["fast"] += r.fast
    return out


def fmt_dur(s):
    if s is None:
        return "-"
    s = int(round(s))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    return "%dh %02dm" % (s // 3600, (s % 3600) // 60)


def fmt_hm(s):
    m = int(round(s / 60.0))
    return "%dh %02dm" % (m // 60, m % 60) if m >= 60 else "%dm" % m


def _median(xs):
    return statistics.median(xs) if xs else None


def _read_json(r):
    return {"at": r.at, "gap_s": round(r.gap_s), "read_s": round(r.read_s),
            "fast": r.fast, "break": r.is_break}


def to_json(out, replies=False):
    rows = []
    for (day, project), b in sorted(out.items()):
        rows.append({
            "day": day,
            "project": project,
            "sessions": len(b["sessions"]),
            "active_s": round(b["you_s"] + b["ai_s"]),
            "you_s": round(b["you_s"]),
            "ai_only_s": round(b["ai_s"]),
            "prompts": b["prompts"],
            "prompt_chars": b["chars"],
            "pasted_chars": b["pasted_chars"],
            "cycles": len(b["cycle_secs"]),
            "interrupted": b["interrupted"],
            "median_ai_response_s": _median(b["cycle_secs"]),
            "median_user_response_s": _median(b["user_secs"]),
            "median_read_s": _median(b["read_secs"]),
            "fast_replies": b["fast"],
            "typed_prompts": b["typed_prompts"],
            "typing_s": round(b["typing_ms"] / 1000),
            "keys_typed": b["keys_typed"],
            "fixes": b["fixes"],
            "rewrites": b["rewrites"],
            "revisions": b["revisions"],
            "drafts_abandoned": b["drafts_abandoned"],
            "breaks": b["breaks"],
            "files": {k: {"added": v[0], "removed": v[1]} for k, v in sorted(b["files"].items())},
        })
        if replies:
            rows[-1]["replies"] = [_read_json(r) for r in sorted(b["reads"], key=lambda r: r.at)]
    return rows


def print_text(out, idle_min, top):
    if not out:
        print("No events in range.")
        return
    day_totals = defaultdict(lambda: [0.0, 0.0, 0])
    for (day, project), b in sorted(out.items()):
        print("%s  %s" % (day, project))
        tot = day_totals[day]
        tot[0] += b["you_s"]
        tot[1] += b["ai_s"]
        tot[2] += 1
        print("  time      %8s   (you %s, AI alone %s)"
              % (fmt_hm(b["you_s"] + b["ai_s"]), fmt_hm(b["you_s"]), fmt_hm(b["ai_s"])))
        paste = ", %s pasted" % format(b["pasted_chars"], ",") if b["pasted_chars"] else ""
        print("  prompts        %4d   (%s chars typed%s)" % (b["prompts"], format(b["chars"], ","), paste))
        intr = "   (%d interrupted)" % b["interrupted"] if b["interrupted"] else ""
        print("  AI cycles      %4d   median AI response   %s%s"
              % (len(b["cycle_secs"]), fmt_dur(_median(b["cycle_secs"])), intr))
        notes = []
        if b["fast"]:
            notes.append("%d faster than reading" % b["fast"])
        if b["breaks"]:
            notes.append("%d break%s" % (b["breaks"], "" if b["breaks"] == 1 else "s"))
        print("  your replies         median %s, reading needs ~%s%s"
              % (fmt_dur(_median(b["user_secs"])), fmt_dur(_median(b["read_secs"])),
                 "   (%s)" % ", ".join(notes) if notes else ""))
        if b["typed_prompts"] or b["drafts_abandoned"]:
            bits = ["%s keys" % format(b["keys_typed"], ","), _plural(b["fixes"], "fix").replace("fixs", "fixes"),
                    _plural(b["rewrites"], "rewrite"), _plural(b["revisions"], "revision")]
            if b["drafts_abandoned"]:
                bits.append("%s abandoned" % _plural(b["drafts_abandoned"], "draft"))
            print("  typing    %8s   over %s   (%s)"
                  % (fmt_dur(b["typing_ms"] / 1000.0), _plural(b["typed_prompts"], "typed prompt"), ", ".join(bits)))
        files = sorted(b["files"].items(), key=lambda kv: (-kv[1][0], kv[0]))
        if files:
            ta = sum(v[0] for _, v in files)
            tr = sum(v[1] for _, v in files)
            print("  AI lines       +%d / -%d across %d file%s" % (ta, tr, len(files), "" if len(files) == 1 else "s"))
            width = min(48, max(len(k) for k, _ in files[:top]))
            for name, (a, r) in files[:top]:
                print("    %-*s  +%-5d -%d" % (width, name, a, r))
            if len(files) > top:
                print("    ... %d more (use --top)" % (len(files) - top))
        print()
    for day, (you, ai, n) in sorted(day_totals.items()):
        if n > 1:
            print("%s  total %s across %d projects   (you %s, AI alone %s)"
                  % (day, fmt_hm(you + ai), n, fmt_hm(you), fmt_hm(ai)))
    if len(day_totals) > 1:
        print_overall(out, sorted(day_totals))


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def print_overall(out, days):
    bs = list(out.values())
    you = sum(b["you_s"] for b in bs)
    ai = sum(b["ai_s"] for b in bs)
    cycles = [x for b in bs for x in b["cycle_secs"]]
    replies = [x for b in bs for x in b["user_secs"]]
    reads = [x for b in bs for x in b["read_secs"]]
    fast = sum(b["fast"] for b in bs)
    breaks = sum(b["breaks"] for b in bs)
    interrupted = sum(b["interrupted"] for b in bs)
    files = set()
    added = removed = 0
    for (day, project), b in out.items():
        for name, (a, r) in b["files"].items():
            files.add((project, name))
            added += a
            removed += r
    chars = sum(b["chars"] for b in bs)
    pasted = sum(b["pasted_chars"] for b in bs)

    print()
    print("All days  %s -> %s   (%s with activity)" % (days[0], days[-1], _plural(len(days), "day")))
    print("  time      %8s   (you %s, AI alone %s%s)"
          % (fmt_hm(you + ai), fmt_hm(you), fmt_hm(ai),
             ", %d%% AI alone" % round(100 * ai / (you + ai)) if you + ai else ""))
    paste = ", %s pasted" % format(pasted, ",") if pasted else ""
    print("  prompts        %4d   (%s chars typed%s)" % (sum(b["prompts"] for b in bs), format(chars, ","), paste))
    if cycles:
        intr = ", %d interrupted" % interrupted if interrupted else ""
        print("  AI turns       %4d   median %s, longest %s%s"
              % (len(cycles), fmt_dur(_median(cycles)), fmt_dur(max(cycles)), intr))
    notes = []
    if fast:
        notes.append("%d faster than reading" % fast)
    if breaks:
        notes.append(_plural(breaks, "break"))
    print("  your replies   %4d   median %s, reading needs ~%s%s"
          % (len(replies), fmt_dur(_median(replies)), fmt_dur(_median(reads)),
             "   (%s)" % ", ".join(notes) if notes else ""))
    typed = sum(b["typed_prompts"] for b in bs)
    if typed:
        print("  typing    %8s   over %s   (%d fixes, %d rewrites, %d revisions, %d drafts abandoned)"
              % (fmt_dur(sum(b["typing_ms"] for b in bs) / 1000.0), _plural(typed, "typed prompt"),
                 sum(b["fixes"] for b in bs), sum(b["rewrites"] for b in bs),
                 sum(b["revisions"] for b in bs), sum(b["drafts_abandoned"] for b in bs)))
    if files:
        print("  AI lines       +%s / -%s across %s"
              % (format(added, ","), format(removed, ","), _plural(len(files), "file")))


def print_replies(out):
    reads = sorted((r for b in out.values() for r in b["reads"]), key=lambda r: r.at)
    print()
    print("Replies: time from the AI finishing to your next prompt, vs. estimated read time")
    if not reads:
        print("  (none)")
        return
    width = min(32, max(len(r.project) for r in reads))
    for r in reads:
        tag = "BREAK" if r.is_break else ("FAST" if r.fast else "")
        ratio = "%.1fx" % (r.gap_s / r.read_s) if r.read_s >= 1 else "-"
        print("  %s %s  %-*s  %9s  vs %8s  %6s  %s"
              % (r.at[:10], r.at[11:16], width, r.project[:width], fmt_dur(r.gap_s), fmt_dur(r.read_s), ratio, tag))
    print("  ratio = reply time / read time.  FAST: under %gx of a read of %ds or more.  BREAK: gap over max(--idle, read time)."
          % (timemodel.FAST_RATIO, timemodel.FAST_MIN_READ_S))


def _nonempty(b):
    return bool(b["prompts"] or b["files"] or b["cycle_secs"] or b["you_s"] or b["ai_s"])


def cmd_summarize(args):
    path = args.file or eventlog.log_path()
    if not os.path.exists(path):
        print("No log yet at %s" % path)
        return 0
    events, bad = load(path)
    if args.days:
        since = (datetime.date.today() - datetime.timedelta(days=args.days - 1)).isoformat()
    else:
        since = args.since
    # Sessions are kept whole for pairing; filter buckets afterwards.
    out = summarize(events, idle_min=args.idle)
    out = {k: v for k, v in out.items()
           if (not since or k[0] >= since) and (not args.project or k[1] == args.project) and _nonempty(v)}
    if args.json:
        json.dump(to_json(out, replies=args.replies), sys.stdout, indent=2)
        print()
    else:
        print_text(out, args.idle, args.top)
        if args.replies:
            print_replies(out)
        if bad:
            print("(%d unreadable line%s skipped; run `sauron verify`)" % (bad, "" if bad == 1 else "s"))
    return 0


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def verify(path):
    """Return a list of problem strings (empty = chain intact)."""
    problems = []
    prev = None
    with open(path, "rb") as f:
        for i, raw in enumerate(f, 1):
            line = raw.rstrip(b"\r\n")
            if not line:
                problems.append("line %d: empty line" % i)
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
            except ValueError:
                problems.append("line %d: not valid JSON" % i)
                prev = line
                continue
            want = eventlog.GENESIS_HASH if prev is None else eventlog.line_hash(prev)
            got = rec.get("prev_hash")
            if got != want:
                problems.append("line %d: prev_hash mismatch (line %d edited, or a line removed/inserted here)"
                                % (i, i - 1))
            prev = line
    return problems


def head(path):
    """(line count, hash of the last line). Record these somewhere else to detect later edits to the end."""
    n, last = 0, None
    with open(path, "rb") as f:
        for raw in f:
            line = raw.rstrip(b"\r\n")
            if line:
                n, last = n + 1, line
    return n, (eventlog.line_hash(last) if last else eventlog.GENESIS_HASH)


def _check_head(path, n, want, problems):
    """Does the log still extend a head LINES:HASH saved earlier? Appends problems; False on bad input."""
    want_n, _, want_h = want.partition(":")
    try:
        want_n = int(want_n)
    except ValueError:
        return False
    if want_n > n:
        problems.append("log is shorter than the saved head (%d < %d lines): records were removed" % (n, want_n))
    elif want_n > 0:
        with open(path, "rb") as f:
            lines = [raw.rstrip(b"\r\n") for raw in f if raw.strip()]
        if eventlog.line_hash(lines[want_n - 1]) != want_h:
            problems.append("line %d no longer matches the saved head: it or an earlier line was changed" % want_n)
    return True


def _heads_path():
    return os.path.join(eventlog.data_dir(), "verify-heads.json")


def _load_heads():
    try:
        with open(_heads_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def cmd_verify(args):
    path = args.file or eventlog.log_path()
    if not os.path.exists(path):
        print("No log yet at %s" % path)
        return 0
    problems = verify(path)
    n, h = head(path)
    if args.expect_head and not _check_head(path, n, args.expect_head, problems):
        print("--expect-head takes LINES:HASH, as printed by a previous verify")
        return 2
    # Every verify remembers the head it saw, so the next plain verify also catches the last records
    # being edited or removed in between. It lives next to the log, so it only catches accidents and
    # naive edits; an outside reviewer should keep their own copy and use --expect-head.
    key = os.path.abspath(path)
    heads = _load_heads()
    prev = heads.get(key)
    if prev and not args.expect_head:
        _check_head(path, n, prev["head"], problems)
    if not problems:
        print("OK: %d lines, links between records intact (%s)" % (n, path))
        if prev and not args.expect_head:
            print("The log still extends the head seen by the last verify (%s)." % prev.get("at", "?"))
        print("head %d:%s" % (n, h))
        print("This is a consistency check, not proof: a rewritten log can pass. For an outside check,")
        print("save the head line somewhere you control and pass it back with --expect-head.")
        heads[key] = {"head": "%d:%s" % (n, h), "at": eventlog.now_iso()}
        try:
            eventlog.make_data_dir()
            with open(_heads_path(), "w", encoding="utf-8") as f:
                json.dump(heads, f)
        except OSError:
            pass
        return 0
    print("BROKEN: %d problem%s in %d lines (%s)" % (len(problems), "" if len(problems) == 1 else "s", n, path))
    for p in problems[:50]:
        print("  " + p)
    if len(problems) > 50:
        print("  ... %d more" % (len(problems) - 50))
    if prev and not args.expect_head:
        print("If you reset or replaced the log on purpose, delete %s." % _heads_path())
    return 1


def cmd_import(args):
    import importer

    src = os.path.expanduser(args.src or os.path.join("~", ".claude", "projects"))
    out = os.path.expanduser(args.out or os.path.join(eventlog.data_dir(), "imported.jsonl"))
    if not os.path.isdir(src):
        print("No such folder: %s" % src)
        return 1
    if os.path.abspath(out) == os.path.abspath(eventlog.log_path()):
        print("Refusing to import into the live log; pick another --out.")
        return 1
    n_files, n_events = importer.import_dir(src, out, args.utc_offset)
    print("Imported %d events from %d transcripts into %s" % (n_events, n_files, out))
    print("See it with:  sauron --file %s summarize" % out)
    return 0


def find_sources(paths, extra=None):
    """[(label, path)]: the live log, imported history, then each file / *.jsonl in each dir given."""
    out = []
    seen = set()

    def add(label, p):
        p = os.path.abspath(os.path.expanduser(p))
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            out.append((label, p))

    if extra:
        add(os.path.basename(extra), extra)
    add("Live log", eventlog.log_path())
    add("Imported history", os.path.join(eventlog.data_dir(), "imported.jsonl"))
    for p in paths or []:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.endswith(".jsonl"):
                    add(f[:-6], os.path.join(p, f))
        else:
            add(os.path.basename(p)[:-6] if p.endswith(".jsonl") else os.path.basename(p), p)
    return out


def load_replays(log_path, limit=300):
    """Replays stored next to a log (replays/*.jsonl), newest first, each matched to its prompt event."""
    folder = os.path.join(os.path.dirname(log_path), "replays")
    if not os.path.isdir(folder):
        return []
    by_hash = {}
    if os.path.isfile(log_path):
        events, _ = load(log_path)
        by_hash = {e["replay_hash"]: e for e in events if e.get("replay_hash")}
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(folder, name), "rb") as f:
            for raw in f:
                raw = raw.rstrip(b"\r\n")
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                ev = by_hash.get(eventlog.line_hash(raw))
                rec["verified"] = ev is not None
                if ev is not None:
                    rec["ts"] = ev["ts"]
                    rec["project"] = ev.get("project")
                    for k in ("fixes", "rewrites", "revisions", "pauses", "pastes", "compose_ms"):
                        if k in ev:
                            rec[k] = ev[k]
                out.append(rec)
    out.sort(key=lambda r: r.get("at", 0), reverse=True)
    return out[:limit]


def prompt_rows(log_path, idle_min=15.0, exclude=()):
    """One row per prompt: the time it owns and what's known about it.

    "you_before_s" is your credited time leading into the prompt (reading the last
    answer, thinking, typing); "ai_s" is how long the AI then worked on it. Jev
    labels (jev/<log name> beside the log) are added when present. Counts,
    timings and labels only, never text.
    """
    labels = {}
    labels_path = os.path.join(os.path.dirname(log_path), "jev", os.path.basename(log_path))
    if os.path.isfile(labels_path):
        with open(labels_path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("prompt_id") and not d.get("error"):
                    labels[d["prompt_id"]] = d
    events, _ = load(log_path)
    events = [e for e in events if e.get("project") not in exclude]
    return prompt_rows_from(events, labels, idle_min)


def prompt_rows_from(events, labels, idle_min=15.0):
    """prompt_rows for already-loaded events (each with "_t" and "_line") and {prompt_id: label}."""
    _, reads = timemodel.analyze(events, idle_min)
    read_by_done = {r.at: r for r in reads}
    idle_s = idle_min * 60
    pace, _ = timemodel.personal_pace(events)
    by_session = defaultdict(list)
    for e in events:
        by_session[e.get("session")].append(e)
    rows = []
    for evs in by_session.values():
        evs.sort(key=lambda e: (e["_t"], e["_line"]))
        last_done, started, cur, away_seen = None, None, None, False
        for e in evs:
            kind = e.get("event")
            if kind == "away":
                away_seen = True
            if kind == "session_start":
                last_done, started, away_seen = None, e, False
            elif kind == "prompt_sent":
                cur = {"day": e["ts"][:10], "at": e["ts"], "project": e.get("project"), "session": e.get("session"),
                       "typed": e.get("chars", 0), "pasted": e.get("pasted_chars", 0),
                       "gap_s": None, "read_s": None, "break": None, "you_before_s": 0, "away": False,
                       "present_s": None, "compose_s": round(timemodel.compose_seconds(e, pace), 1),
                       "ai_s": None, "lines": 0, "reply_chars": None, "replay": bool(e.get("replay_hash")),
                       "_t0": e["_t"], "_pid": e.get("prompt_id")}
                for k in ("typing_ms", "keys_typed", "fixes", "rewrites", "revisions", "pauses"):
                    if k in e:
                        cur[k] = e[k]
                if int(e.get("typing_ms") or 0) > 0 and int(e.get("keys_typed") or 0) > 0:
                    cur["pace"] = round(int(e["keys_typed"]) / (int(e["typing_ms"]) / 1000.0), 1)
                if last_done is not None:
                    rd = read_by_done.get(last_done["ts"])
                    gap = (e["_t"] - last_done["_t"]).total_seconds()
                    cur["gap_s"] = round(gap)
                    if rd:
                        cur["read_s"], cur["break"] = round(rd.read_s), rd.is_break
                        cur["you_before_s"], cur["away"] = round(rd.credited_s), rd.away
                        cur["present_s"] = round(rd.present_s)
                else:
                    c = timemodel.compose_seconds(e, pace)
                    if started is not None and not away_seen:
                        gap = (e["_t"] - started["_t"]).total_seconds()
                        c = min(gap, c + timemodel.THINK_S)
                    cur["you_before_s"] = round(c)
                lab = labels.get(e.get("prompt_id"))
                if lab:
                    for k in ("pattern", "pattern_confidence", "pattern_probs", "understanding", "read_previous",
                              "pasted_output", "own_thinking", "own_thinking_confidence", "complexity", "model"):
                        cur[k] = lab.get(k)
                rows.append(cur)
                last_done, started, away_seen = None, None, False
            elif kind == "ai_edit" and cur is not None:
                cur["lines"] += int(e.get("lines_added") or 0) + int(e.get("lines_removed") or 0)
            elif kind == "ai_done":
                if cur is not None and cur["ai_s"] is None:
                    cur["ai_s"] = round((e["_t"] - cur["_t0"]).total_seconds())
                    cur["reply_chars"] = e.get("response_text_chars")
                last_done = e
            elif kind == "session_end":
                last_done, started = None, None
    for r in rows:
        r.pop("_t0", None)
    rows.sort(key=lambda r: r["at"])
    return rows


def jev_rows(log_path, idle_min=15.0, exclude=()):
    """Prompt rows that have Jev labels; None when the log has no labels file."""
    labels_path = os.path.join(os.path.dirname(log_path), "jev", os.path.basename(log_path))
    if not os.path.isfile(labels_path):
        return None
    rows = [r for r in prompt_rows(log_path, idle_min, exclude) if r.get("pattern")]
    for r in rows:
        r.pop("_pid", None)
        r.pop("session", None)  # not needed for the Jev views, and never published raw
    return rows


MAX_TEXT = 3000
MAX_PASTE_PREVIEW = 300


def prompt_texts(dirs):
    """{prompt_id: text} from Claude Code transcripts, secrets redacted, long pastes shortened.

    Read only when asked for (sauron serve shows your own; export-site only with --prompt-text).
    """
    import claude_code
    import importer
    import jev_judge

    def shorten(m):
        body = m.group(1)
        if len(body) <= MAX_PASTE_PREVIEW:
            return "[pasted] " + body.strip()
        return "[pasted, %s chars] %s…" % (format(len(body), ","), body.strip()[:MAX_PASTE_PREVIEW])

    out = {}
    for d in dirs:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for path, is_sub in importer.find_transcripts(d):
            if is_sub:
                continue
            for r in importer._read(path):
                text = importer.prompt_text(r)
                pid = r.get("promptId")
                if text is None or not pid or pid in out:
                    continue
                text = jev_judge.redact(claude_code._PASTE_RE.sub(shorten, text)).strip()
                if len(text) > MAX_TEXT:
                    text = text[:MAX_TEXT] + "… (%s more chars)" % format(len(text) - MAX_TEXT, ",")
                out[pid] = text
    return out

# Your own "was this struggle?" labels, for calibrating the model. Local only, never exported.
LABEL_VALUES = ("yes", "partly", "no")


def labels_path():
    return os.path.join(eventlog.data_dir(), "labels.jsonl")


def load_labels():
    """{prompt_id: "yes" | "partly" | "no"}; the latest entry for a prompt wins, null clears it."""
    out = {}
    try:
        with open(labels_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                pid = d.get("pid")
                if not pid:
                    continue
                if d.get("label") in LABEL_VALUES:
                    out[pid] = d["label"]
                else:
                    out.pop(pid, None)
    except OSError:
        pass
    return out


def record_label(pid, label):
    import re
    if not isinstance(pid, str) or not re.match(r"^[A-Za-z0-9_-]{8,80}$", pid):
        raise ValueError("bad prompt id")
    if label is not None and label not in LABEL_VALUES:
        raise ValueError("bad label")
    eventlog.make_data_dir()
    with open(labels_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps({"pid": pid, "label": label, "at": eventlog.now_iso()}) + "\n")


def api_payload(sources, route, query, idle_min, snapshot=False, exclude=(), texts=None):
    """The JSON behind each dashboard route. Raises KeyError/IndexError/ValueError for unknown ones.

    With snapshot=True (for publishing), drop what doesn't belong outside this machine:
    file paths, session ids and prompt ids.
    """
    if route == "/api/sources":
        return [{"label": label} for label, _ in sources]
    if route == "/api/jevconfig":
        import jev_judge
        return {"model": jev_judge.MODEL, "questions": jev_judge.QUESTIONS,
                "think_per": timemodel.calibrated("think_per", None),
                "max_prompt": jev_judge.MAX_PROMPT, "max_prev": jev_judge.MAX_PREV,
                "max_prev_prompt": jev_judge.MAX_PREV_PROMPT}
    label, path = sources[int(query.get("source", "0"))]
    if route == "/api/data":
        events, bad = load(path)
        events = [e for e in events if e.get("project") not in exclude]
        out = {k: v for k, v in summarize(events, idle_min=idle_min).items() if _nonempty(v)}
        rows = to_json(out, replies=True)
        if snapshot:
            # Edits outside a project folder carry paths with usernames, temp folders and session ids.
            for r in rows:
                files = {}
                for name, v in r.get("files", {}).items():
                    if name.startswith("../") or name.startswith("/") or ":" in name[:3]:
                        name = "(outside project)/" + name.replace("\\", "/").rsplit("/", 1)[-1]
                    prev = files.get(name, {"added": 0, "removed": 0})
                    files[name] = {"added": prev["added"] + v["added"], "removed": prev["removed"] + v["removed"]}
                r["files"] = files
        res = {"label": label, "file": label if snapshot else path, "rows": rows,
               "bad": bad, "idle_min": idle_min}
        if snapshot:
            res["snapshot_at"] = eventlog.now_iso()[:16].replace("T", " ")
        return res
    if route == "/api/jev":
        rows = jev_rows(path, idle_min, exclude)
        return {"rows": rows, "idle_min": idle_min}
    if route == "/api/prompts":
        rows = prompt_rows(path, idle_min, exclude)
        evs, _ = load(path)
        pace, n = timemodel.personal_pace([e for e in evs if e.get("project") not in exclude])
        texts = texts or {}
        labels = {} if snapshot else load_labels()
        short = {}
        for r in rows:
            if snapshot:  # don't publish raw session ids
                r["session"] = short.setdefault(r.get("session"), "s%d" % (len(short) + 1))
            pid = r.pop("_pid", None)
            if pid in texts:
                r["text"] = texts[pid]
            if not snapshot and pid:
                r["pid"] = pid
                if pid in labels:
                    r["label"] = labels[pid]
        return {"rows": rows, "idle_min": idle_min, "pace": round(pace, 1), "pace_samples": n,
                "pace_default": timemodel.TYPE_CHARS_PER_S}
    if route == "/api/replays":
        reps = [r for r in load_replays(path) if r.get("project") not in exclude]
        if snapshot:
            reps = [{k: v for k, v in r.items() if k not in ("session", "prompt_id")} for r in reps]
        return {"replays": reps}
    raise KeyError(route)


def presence_report(events, lines, idle_min=15.0, think_s=None):
    """For each reply gap (AI finished -> your next prompt), what presence saw vs what the model credited."""
    import presence as pr
    # Presence as intervals: each line's state lasts until the next line, at most 90 s (else unknown).
    spans = []
    for a, b in zip(lines, lines[1:] + [None]):
        end = b["_t"] if b else a["_t"] + datetime.timedelta(seconds=pr.HEARTBEAT_S)
        end = min(end, a["_t"] + datetime.timedelta(seconds=pr.HEARTBEAT_S + 30))
        if a.get("app") != "stopped":
            spans.append((a["_t"], end, a.get("app"), bool(a.get("active"))))
    def seen(t0, t1):
        out = defaultdict(float)
        covered = 0.0
        for s0, s1, app, active in spans:
            lo, hi = max(s0, t0), min(s1, t1)
            if hi > lo:
                d = (hi - lo).total_seconds()
                covered += d
                out[(app if active else "inactive")] += d
        out["unknown"] = max(0.0, (t1 - t0).total_seconds() - covered)
        return out
    _, reads = timemodel.analyze(events, idle_min, think_s=think_s)
    by_done = {r.at: r for r in reads}
    rows = []
    by_session = defaultdict(list)
    for e in events:
        by_session[e.get("session")].append(e)
    for evs in by_session.values():
        evs.sort(key=lambda e: (e["_t"], e["_line"]))
        last_done = None
        for e in evs:
            if e["event"] == "ai_done":
                last_done = e
            elif e["event"] == "prompt_sent" and last_done is not None:
                rd = by_done.get(last_done["ts"])
                t0, t1 = last_done["_t"], e["_t"]
                if t1 > t0 and rd is not None:
                    rows.append({"at": e["ts"], "gap_s": (t1 - t0).total_seconds(), "model_s": rd.credited_s,
                                 "seen": seen(t0, t1)})
                last_done = None
            elif e["event"] in ("session_start", "session_end"):
                last_done = None
    return rows


# Presence calibration. For each reply gap, presence gives a range the model's credit should land in:
# at least the time you were active in a terminal, editor or AI app, and at most the time you were
# active anywhere. Browser time stays undecided (docs or YouTube?), so it widens the range instead of
# being counted or dropped. A constant is good when most gaps land inside their range.
CORE_APPS = ("terminal", "editor", "ai_app")
FIT_MIN_GAPS = 40


def presence_bounds(seen):
    lo = sum(seen.get(k, 0.0) for k in CORE_APPS)
    hi = sum(v for k, v in seen.items() if k not in ("inactive", "unknown", "locked"))
    return lo, max(lo, hi)


def _miss(x, lo, hi):
    return lo - x if x < lo else (x - hi if x > hi else 0.0)


def _score(pairs):
    """pairs of (credited, (lo, hi)) -> (seconds outside the ranges, share of gaps inside, under, over)."""
    miss = sum(_miss(x, *b) for x, b in pairs)
    inside = sum(1 for x, b in pairs if _miss(x, *b) <= 5) / float(len(pairs) or 1)
    under = sum(1 for x, (lo, hi) in pairs if x < lo - 5)
    over = sum(1 for x, (lo, hi) in pairs if x > hi + 5)
    return miss, inside, under, over


def dash_you(r, per, idle_min=15.0):
    """The dashboard's estimate of your time before a prompt (dynParts in dashboard.html) for a gap."""
    engage = 0.3 + 0.7 * (r.get("read_previous") or 0)
    read = min(r.get("read_s") or 0, idle_min * 60) * engage
    think = per * ((r.get("complexity") or 0) + (r.get("own_thinking") or 0))
    write = r.get("compose_s") or 0
    avail = r["present_s"] if r.get("present_s") is not None else r["gap_s"]
    rt = min(read + think, avail)
    return rt + (write if r.get("typing_ms") is not None else min(write, max(0.0, r["gap_s"] - rt)))


def fit_presence(log_path, lines, idle_min=15.0):
    events, _ = load(log_path)
    base = [r for r in presence_report(events, lines, idle_min) if r["seen"]["unknown"] < 0.5 * r["gap_s"]]
    bounds = {r["at"]: presence_bounds(r["seen"]) for r in base}
    out = {"gaps": len(bounds), "cli": [], "dash": [], "dash_gaps": 0}
    if not bounds:
        return out
    for t in range(0, 301, 10):
        rows = [r for r in presence_report(events, lines, idle_min, think_s=t) if r["at"] in bounds]
        out["cli"].append((t, _score([(r["model_s"], bounds[r["at"]]) for r in rows])))
    prows = [r for r in prompt_rows(log_path, idle_min)
             if r.get("pattern") and r.get("gap_s") is not None and r["at"] in bounds]
    out["dash_gaps"] = len(prows)
    if prows:
        for per in range(0, 121, 5):
            out["dash"].append((per, _score([(dash_you(r, per, idle_min), bounds[r["at"]]) for r in prows])))
    return out


def _best(grid, current):
    """Least time outside the ranges, then most gaps inside, then the value closest to the current one,
    so a constant only moves when the data actually prefers another."""
    return min(grid, key=lambda x: (round(x[1][0] / 5), -x[1][1], abs(x[0] - current))) if grid else None


def cmd_presence_fit(args, lines):
    path = args.file or eventlog.log_path()
    f = fit_presence(path, lines)
    if not f["gaps"]:
        print("No reply gaps overlap the presence data yet.")
        return 0
    cur_t = timemodel.calibrated("think_s", timemodel.THINK_S)
    print("Calibrating against %d reply gaps (need %d to apply). For each gap the model should credit at least"
          % (f["gaps"], FIT_MIN_GAPS))
    print("your active time in a terminal, editor or AI app, and at most your active time anywhere.\n")

    def show(name, unit, grid, current):
        best = _best(grid, current)
        cur = min(grid, key=lambda x: abs(x[0] - current))
        for label, (v, (miss, inside, under, over)) in (("now ", cur), ("best", best)):
            print("  %s %-34s %3d%% of gaps in range, %d too low, %d too high, %s outside" % (
                label, "%s = %g %s" % (name, v, unit), round(100 * inside), under, over, fmt_dur(miss)))
        return best

    print("CLI model (thinking allowance after each reply):")
    bt = show("thinking", "s", f["cli"], cur_t)
    bp = None
    if f["dash"]:
        cur_p = timemodel.calibrated("think_per", 30)
        print("\nDashboard model (thinking per point of Jev complexity + own thinking), %d judged gaps:" % f["dash_gaps"])
        bp = show("seconds per point", "s", f["dash"], cur_p)
    if f["gaps"] < FIT_MIN_GAPS:
        print("\nToo few gaps to trust yet. Keep working with presence on, then run this again.")
        if args.apply:
            print("Not applied.")
        return 0
    if args.apply:
        import jev_judge
        cal = {"think_s": bt[0], "gaps": f["gaps"], "at": eventlog.now_iso()}
        if bp:
            cal["think_per"] = bp[0]
        jev_judge._set_config(calibration=cal)
        print("\nApplied: thinking %gs%s. The CLI and dashboard use these from now on." % (
            bt[0], ", %gs per point" % bp[0] if bp else ""))
    else:
        print("\nRun with --apply to use the best values.")
    return 0


def cmd_connect(args):
    """Connect this computer to an ai-tracker server with a device key from its settings page."""
    import getpass
    import sync
    import urllib.parse
    url = args.server.rstrip("/")
    try:
        parts = urllib.parse.urlsplit(url)
        local = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
    except ValueError:
        parts, local = None, False
    # plain http only for this computer itself: http://localhost.example.com is someone else's server
    if not (parts and parts.hostname and (parts.scheme == "https" or local)):
        print("The server address should start with https://")
        return 2
    k = getpass.getpass("Device key from %s/settings (input hidden): " % url).strip()
    if not k:
        print("Nothing saved.")
        return 1
    eventlog.make_data_dir()
    path = os.path.join(eventlog.data_dir(), "server.key")
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(k)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    import jev_judge
    jev_judge._set_config(server=url)
    if not jev_judge._config().get("jev_since"):
        # effort labels come through the server from now on; older prompts aren't sent
        import time
        jev_judge._set_config(jev_since=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    print("Connected. Sending your log (counts and timings, never prompt text)...")
    n = sync.sync()
    if n < 0:
        print("Couldn't reach the server yet; it will retry after your next turn. Details in errors.log.")
        return 1
    print("Sent %d events. From now on new activity is sent after each turn." % n)
    print("For effort labels, each new prompt's text (secrets removed, best-effort) also goes to the server, "
          "which has Jev label it and keeps only the label, not the text.")
    return 0


def cmd_sync(args):
    import sync
    if not sync.enabled():
        print("Not connected. Run: sauron connect https://SERVER")
        return 1
    n = sync.sync()
    print("Sent %d new events." % n if n >= 0 else "Couldn't sync; see %s" % os.path.join(eventlog.data_dir(), "errors.log"))
    return 0 if n >= 0 else 1


def cmd_presence(args):
    import presence as pr
    lines = pr.load()
    if not lines:
        print("No presence data yet. Turn it on with {\"presence\": true} in %s, then use Claude Code."
              % os.path.join(eventlog.data_dir(), "config.json"))
        return 0
    if args.fit:
        return cmd_presence_fit(args, lines)
    events, _ = load(args.file or eventlog.log_path())
    rows = [r for r in presence_report(events, lines) if r["seen"]["unknown"] < 0.5 * r["gap_s"]]
    if not rows:
        print("Presence data exists, but no reply gaps overlap it yet.")
        return 0
    tot = defaultdict(float)
    for r in rows:
        for k, v in r["seen"].items():
            tot[k] += v
    gap = sum(r["gap_s"] for r in rows)
    model = sum(r["model_s"] for r in rows)
    present = sum(v for k, v in tot.items() if k not in ("inactive", "unknown", "locked"))
    print("Reply gaps with presence data: %d, %s in total" % (len(rows), fmt_dur(gap)))
    print("  model credited as your time:   %s" % fmt_dur(model))
    print("  presence saw you active:       %s  (%d%% of the gaps)" % (fmt_dur(present), round(100 * present / gap)))
    for k in ("terminal", "editor", "browser", "ai_app", "other", "inactive", "locked", "unknown"):
        if tot.get(k):
            print("    %-10s %10s" % (k, fmt_dur(tot[k])))
    print("\nRecent gaps (gap / model / active in terminal+editor / anywhere):")
    for r in rows[-args.n:]:
        s = r["seen"]
        print("  %s  %8s  %8s  %8s  %8s" % (r["at"][5:16].replace("T", " "), fmt_dur(r["gap_s"]), fmt_dur(r["model_s"]),
              fmt_dur(s.get("terminal", 0) + s.get("editor", 0)),
              fmt_dur(sum(v for k, v in s.items() if k not in ("inactive", "unknown", "locked")))))
    return 0


# What a published snapshot may contain. Anything not listed here is dropped.
PUBLIC_DATA_ROW = {"day", "project", "sessions", "active_s", "you_s", "ai_only_s", "prompts", "prompt_chars",
                   "pasted_chars", "cycles", "interrupted", "median_ai_response_s", "median_user_response_s",
                   "median_read_s", "fast_replies", "breaks", "typed_prompts", "typing_s", "keys_typed", "fixes",
                   "rewrites", "revisions", "drafts_abandoned", "replies", "files"}
PUBLIC_REPLY = {"at", "gap_s", "read_s", "fast", "break"}
PUBLIC_PROMPT_ROW = {"day", "at", "project", "session", "typed", "pasted", "gap_s", "read_s", "break", "you_before_s",
                     "away", "present_s", "compose_s", "ai_s", "lines", "reply_chars", "typing_ms", "keys_typed",
                     "fixes", "rewrites", "revisions", "pauses", "pace", "pattern", "pattern_confidence",
                     "pattern_probs", "understanding", "read_previous", "pasted_output", "own_thinking",
                     "own_thinking_confidence", "complexity", "model"}


def public_view(route, payload, include):
    """Reduce an API payload to the public allowlist. include: set of {"text", "replays", "file_names"}."""
    if route == "/api/data":
        rows = []
        for r in payload["rows"]:
            r = {k: v for k, v in r.items() if k in PUBLIC_DATA_ROW}
            r["replies"] = [{k: v for k, v in x.items() if k in PUBLIC_REPLY} for x in r.get("replies", [])]
            if "file_names" not in include:
                a = sum(v["added"] for v in r.get("files", {}).values())
                d = sum(v["removed"] for v in r.get("files", {}).values())
                r["files"] = {"(file names not published)": {"added": a, "removed": d}} if (a or d) else {}
            rows.append(r)
        return {"label": payload["label"], "file": payload["label"], "rows": rows, "bad": payload.get("bad", 0),
                "idle_min": payload["idle_min"], "snapshot_at": payload.get("snapshot_at")}
    if route in ("/api/prompts", "/api/jev"):
        if payload.get("rows") is None:
            return payload
        allow = PUBLIC_PROMPT_ROW | ({"text"} if "text" in include else set()) | ({"replay"} if "replays" in include else set())
        out = dict(payload)
        out["rows"] = [{k: v for k, v in r.items() if k in allow} for r in payload["rows"]]
        return out
    if route == "/api/replays":
        return payload if "replays" in include else {"replays": []}
    return payload


def wakatime_hide_settings(path=None):
    """(hide_project_names, hide_file_names) from ~/.wakatime.cfg, so Hackatime's own privacy settings carry over.

    wakatime-cli accepts true/false or a list of regexes; any value other than false hides everything here,
    which is the stricter reading.
    """
    import configparser
    path = path or os.environ.get("WAKATIME_HOME") and os.path.join(os.environ["WAKATIME_HOME"], ".wakatime.cfg") \
        or os.path.join(os.path.expanduser("~"), ".wakatime.cfg")
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return False, False

    def on(key):
        v = cp.get("settings", key, fallback="").strip().lower()
        return bool(v) and v not in ("false", "0", "no", "off")

    return on("hide_project_names"), on("hide_file_names")


def anonymize_names(snap):
    """Replace project names with "project 1", "project 2"... and log labels with "log 1"... in place.

    Projects are numbered by first appearance, so the same project keeps one number across every view.
    """
    projects = {}

    def proj(name):
        if name not in projects:
            projects[name] = "project %d" % (len(projects) + 1)
        return projects[name]

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "project" and isinstance(v, str):
                    x[k] = proj(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for key in sorted(snap):
        walk(snap[key])
    for i, src in enumerate(snap["/api/sources"]):
        src["label"] = "log %d" % (i + 1)
        d = snap.get("/api/data?source=%d" % i)
        if isinstance(d, dict):
            d["label"] = d["file"] = src["label"]
    return len(projects)


def cmd_export_site(args):
    """Write a self-contained copy of the dashboard (index.html + data.json) for publishing."""
    sources = find_sources(args.paths)
    os.makedirs(args.out, exist_ok=True)
    snap = {"/api/sources": api_payload(sources, "/api/sources", {}, args.idle, snapshot=True),
            "/api/jevconfig": api_payload(sources, "/api/jevconfig", {}, args.idle, snapshot=True)}
    texts = prompt_texts(args.prompt_text) if args.prompt_text else {}
    if texts and (args.exclude_project or args.redact_text):
        import re
        # An excluded project shouldn't reappear by name inside prompt text either,
        # nor any extra phrase asked for with --redact-text.
        names = [re.compile(r"(?i)" + r"[-_\s]+".join(map(re.escape, re.split(r"[-_\s]+", n))))
                 for n in args.exclude_project + args.redact_text]
        for pid, t in texts.items():
            for rx in names:
                t = rx.sub("[excluded project]", t)
            texts[pid] = t
    if texts:
        print("Including the text of %d prompts from %s (secrets redacted)" % (len(texts), ", ".join(args.prompt_text)))
    # Default: what a Hack Club member already sends Hackatime in every heartbeat (project name, file path,
    # line changes, AI prompt length, timings). Anything beyond that, like prompt text or keystroke-level
    # replays, is opt-in. Hackatime's own hide settings in ~/.wakatime.cfg are honoured.
    wk_proj, wk_files = wakatime_hide_settings()
    hide_names = args.hide_project_names or wk_proj
    hide_files = args.hide_file_names or wk_files
    if wk_proj or wk_files:
        print("Following ~/.wakatime.cfg: hiding %s." % " and ".join(
            x for x, on in (("project names", wk_proj), ("file names", wk_files)) if on))
    include = ({"text"} if texts else set()) | ({"replays"} if args.include_replays else set()) | \
              (set() if hide_files else {"file_names"}) | (set() if hide_names else {"names"})
    for i in range(len(sources)):
        for route in ("/api/data", "/api/jev", "/api/prompts", "/api/replays"):
            full = api_payload(sources, route, {"source": str(i)}, args.idle,
                               snapshot=True, exclude=set(args.exclude_project), texts=texts)
            snap["%s?source=%d" % (route, i)] = public_view(route, full, include)
    if "names" not in include:
        anonymize_names(snap)
    print("This snapshot includes: counts and timings%s%s%s%s." % (
        ", project names and log labels" if "names" in include else "",
        ", prompt text (secrets removed on a best-effort basis)" if "text" in include else "",
        ", layout-only typing replays" if "replays" in include else "",
        ", file names of AI edits" if "file_names" in include else ""))
    print("Not included: %s." % (", ".join(x for x, k in (
        ("project names and log labels (shown as project 1, log 1...)", "names"), ("prompt text", "text"),
        ("typing replays", "replays"), ("file names", "file_names")) if k not in include) or "nothing optional"))
    with open(os.path.join(args.out, "data.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8") as f:
        page = f.read()
    # The publisher wraps the page in its own document skeleton: keep only title, style, content and scripts.
    head, rest = page.split("<title>", 1)
    page = "<title>" + rest
    for tag in ("</head>", "<body>", "</body>", "</html>"):
        page = page.replace(tag, "")
    page = page.replace("<script>\n\"use strict\";", "<script>window.AITRACK_STATIC = true;</script>\n<script>\n\"use strict\";", 1)
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    import shutil
    shutil.copytree(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"),
                    os.path.join(args.out, "assets"), dirs_exist_ok=True)
    print("Wrote %s (%d sources, data.json %d KB)" % (args.out, len(sources),
          os.path.getsize(os.path.join(args.out, "data.json")) // 1024))
    for label, path in sources:
        print("  %-18s %s" % (label, path))
    return 0


def cmd_serve(args):
    import http.server
    import urllib.parse

    sources = find_sources(args.paths, extra=args.file)
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def _foreign_host(self):
            """True (and answered with 403) unless the request names this server by its local address.
            A web page can point its own domain at 127.0.0.1 (DNS rebinding) and would then be allowed to
            read this dashboard; its requests still carry its own domain in Host."""
            port = self.server.server_address[1]
            if self.headers.get("Host") in ("127.0.0.1:%d" % port, "localhost:%d" % port):
                return False
            self._send(403, b"forbidden", "text/plain")
            return True

        def do_GET(self):
            if self._foreign_host():
                return
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                with open(page, "rb") as f:  # re-read so edits show on refresh
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path.startswith("/assets/"):
                import re
                name = u.path[len("/assets/"):]
                path = os.path.join(os.path.dirname(page), "assets", name)
                if re.match(r"^[a-z0-9-]+\.svg$", name) and os.path.isfile(path):
                    with open(path, "rb") as f:
                        self._send(200, f.read(), "image/svg+xml")
                else:
                    self._send(404, b"not found", "text/plain")
            elif u.path.startswith("/api/"):
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                try:
                    texts = prompt_texts(args.prompt_text) if (u.path == "/api/prompts" and args.prompt_text) else None
                    self._json(api_payload(sources, u.path, q, args.idle, texts=texts))
                except (IndexError, ValueError, KeyError):
                    self._json({"error": "unknown source or route"}, 404)
                except OSError as e:
                    self._json({"error": str(e)}, 500)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self._foreign_host():
                return
            u = urllib.parse.urlparse(self.path)
            # A custom header that browsers won't send cross-site without a preflight we never allow.
            if u.path != "/api/label" or self.headers.get("X-Aitrack") != "1":
                self._send(404, b"not found", "text/plain")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 10000:
                    raise ValueError("too large")
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                record_label(body.get("pid"), body.get("label"))
                self._json({"ok": True})
            except (ValueError, TypeError, AttributeError) as e:
                self._json({"error": str(e)}, 400)

        def log_message(self, *a):
            pass

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print("Can't listen on port %d: %s (try --port)" % (args.port, e))
        return 1
    url = "http://127.0.0.1:%d/" % args.port
    print("ai-tracker dashboard: %s   (Ctrl+C to stop)" % url)
    for label, path in sources:
        print("  %-18s %s" % (label, path))
    if not sources:
        print("  (no logs found yet)")
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sauron", description="Sauron: summarize, verify, view and sync your AI coding log.")
    ap.add_argument("--file", help="log file (default: %s)" % eventlog.log_path())
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("summarize", help="per project, per day stats")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, help="only the last N days (including today)")
    g.add_argument("--since", help="only days on or after YYYY-MM-DD")
    s.add_argument("--project", help="only this project")
    s.add_argument("--idle", type=float, default=15.0, help="minutes after which a gap counts as a break, unless reading the AI's output would take longer (default 15)")
    s.add_argument("--top", type=int, default=10, help="files to list per day (default 10)")
    s.add_argument("--replies", action="store_true", help="also list every reply gap next to its read time")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    v = sub.add_parser("verify", help="check the links between records (an internal consistency check)")
    v.add_argument("--file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    v.add_argument("--expect-head", metavar="LINES:HASH", help="a head printed earlier and kept elsewhere")

    im = sub.add_parser("import", help="backfill events from Claude Code transcripts (counts only)")
    im.add_argument("src", nargs="?", help="folder of transcripts (default ~/.claude/projects)")
    im.add_argument("--out", help="output file (default ~/.ai-tracker/imported.jsonl, never the live log)")
    im.add_argument("--utc-offset", type=float, help="hours from UTC for day boundaries (default: this machine's)")

    cn = sub.add_parser("connect", help="send your log to an ai-tracker server (asks for a device key)")
    cn.add_argument("server", help="server address, e.g. https://sauron.k.hackclub.dev")
    sub.add_parser("sync", help="send new events to the server now")

    pz = sub.add_parser("presence", help="compare presence data with the model's estimate of your time")
    pz.add_argument("-n", type=int, default=15, help="recent gaps to list")
    pz.add_argument("--fit", action="store_true", help="find the thinking constants that best match presence")
    pz.add_argument("--apply", action="store_true", help="with --fit: save them as your calibration")
    pz.add_argument("--file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sv = sub.add_parser("serve", help="local dashboard in your browser")
    sv.add_argument("paths", nargs="*", help="extra .jsonl files or folders of them (e.g. friends' imports)")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--idle", type=float, default=15.0, help="break cutoff in minutes (default 15)")
    sv.add_argument("--no-open", action="store_true", help="don't open a browser")
    sv.add_argument("--prompt-text", action="append", nargs="?", const=os.path.join("~", ".claude", "projects"),
                    default=[], metavar="DIR",
                    help="show prompt text read from these transcript folders (default folder if no DIR); off unless given")
    sv.add_argument("--file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    ex = sub.add_parser("export-site", help="write a publishable snapshot of the dashboard")
    ex.add_argument("paths", nargs="*", help="extra .jsonl files or folders of them")
    ex.add_argument("--out", required=True, help="folder to write index.html and data.json into")
    ex.add_argument("--idle", type=float, default=15.0)
    ex.add_argument("--prompt-text", action="append", default=[], metavar="DIR",
                    help="publish the text of prompts found in these transcript folders (secrets redacted)")
    ex.add_argument("--include-replays", action="store_true", help="publish layout-only typing replays (off by default)")
    ex.add_argument("--hide-file-names", action="store_true",
                    help="leave out names of files the AI edited (like wakatime's hide_file_names)")
    ex.add_argument("--hide-project-names", action="store_true",
                    help="show projects and logs as project 1, log 1... (like wakatime's hide_project_names)")
    ex.add_argument("--redact-text", action="append", default=[], metavar="PHRASE",
                    help="replace this phrase in published prompt text (repeatable)")
    ex.add_argument("--exclude-project", action="append", default=[], metavar="NAME",
                    help="leave a project out of the snapshot entirely (repeatable)")
    ex.add_argument("--file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    args = ap.parse_args(argv)
    if args.cmd == "connect":
        return cmd_connect(args)
    if args.cmd == "sync":
        return cmd_sync(args)
    if args.cmd == "presence":
        return cmd_presence(args)
    if args.cmd == "export-site":
        return cmd_export_site(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "import":
        return cmd_import(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd in (None, "summarize"):
        if args.cmd is None:
            args = ap.parse_args(["summarize"] + (["--file", args.file] if args.file else []))
        return cmd_summarize(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
