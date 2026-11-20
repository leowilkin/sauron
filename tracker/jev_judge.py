#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label each prompt in Claude Code transcripts with Jev (TypeSafe's System One
model, via Hack Club AI) for research on what productive struggle looks like.

  JEV_API_KEY=... python3 jev_judge.py TRANSCRIPTS_DIR --out labels.jsonl [--limit N]

PRIVACY: this sends prompt text and the end of the AI's previous reply to
ai.hackclub.com (and on to TypeSafe). Use it only on transcripts whose owner
agreed to that. The output file holds labels and ids only, never text.

Jev is shown text only, not timing or sizes, so its labels stay independent
of the tracker's passive signals and can be compared with them.
"""
import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402
import importer  # noqa: E402

ENDPOINT = "https://ai.hackclub.com/proxy/v1/jev/systemone"
MODEL = "jev-latest"
MAX_PROMPT = 4000
MAX_PREV = 1500
MAX_PREV_PROMPT = 600

# Best-effort secret removal before anything is sent. Regexes catch common shapes only:
# unusual secrets and personal details can still get through, so sending stays opt-in.
SECRETS = [
    re.compile(r"(?i)\b([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD)[A-Z0-9_]*)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),            # email addresses
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"),                 # sk-..., sk-hc-v1-..., sk-ant-...
    re.compile(r"\b(ghp|gho|ghu|ghs|github_pat|glpat|xox[abpr])[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWTs
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b(\s*[:=]\s*|\s+is\s+)\S{8,}"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),                            # user:pass@ in URLs
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),                              # long hex secrets
    # Personal details. US-centred and best-effort: these catch common formats, not every one.
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                                # SSN
    re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}\b"),  # phone numbers
    re.compile(r"(?i)\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){1,4}(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|"
               r"drive|dr|court|ct|way|place|pl|terrace|ter|parkway|pkwy|highway|hwy|circle|cir|square|sq)\b\.?(?:\s+(?:[NSEW]|NE|NW|SE|SW)\b\.?)?"
               r"(?:,?\s*(?:apt|apartment|unit|suite|ste|#)\.?\s*[A-Za-z0-9-]+)?"),  # street addresses
]

_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


_HOME = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)[^/\\\s\"']+")


def redact(text):
    """Best-effort removal of secrets and personal details. Not a guarantee: read what you send."""
    text = _HOME.sub("~", text)  # home folders name the user: /Users/alex/x -> ~/x
    for rx in SECRETS:
        text = rx.sub("[REDACTED]", text)
    # card numbers: 13-19 digits that pass the Luhn check
    return _CARD.sub(lambda m: "[REDACTED]" if _luhn(re.sub(r"\D", "", m.group())) else m.group(), text)

QUESTIONS = {
    "pattern": {
        "type": "choice",
        "instructions": ("A person is building software with an AI coding assistant. 'prompt' is what they just "
                         "sent; 'ai_previous_reply_end' is the end of the AI's previous reply and 'previous_prompt' "
                         "is what the person sent before that, if any. What is the person mainly doing in this prompt?"),
        "criteria": {
            "delegate": "Hands a task to the AI with little reasoning of their own (e.g. 'add login', 'make it work').",
            "direct": ("Gives specific technical direction from their own understanding: names the approach, "
                       "design, constraints, or the cause of a problem."),
            "debug_handoff": ("Passes an error, failing output or 'still broken' back to the AI to fix, "
                              "without their own diagnosis."),
            "understand_ai_work": "Asks about code or changes the AI just made, in order to understand them.",
            "concept": "Asks how or why something works, or about a concept, rather than asking for code.",
            "code_and_explain": "Asks for code together with an explanation of it.",
            "review_pushback": ("Disagrees with or corrects the AI's previous work or suggestion: points out a mistake, "
                                "rejects it, or asks for it to be done differently. Agreeing with the AI is not this."),
            "approve": ("Agrees with, accepts or approves the AI's suggestion or conclusion, or says to continue "
                        "('yes', 'ok, let's skip that', 'sounds good, go ahead', 'continue')."),
            "ask_info": ("Asks the AI to find, measure, calculate or report information (e.g. 'how many tests are "
                         "failing?', 'what's the latest version of this library?'), rather than to explain how "
                         "something works."),
            "other": "Not about the coding work, or none of these fits.",
        },
    },
    "understanding": {
        "type": "noul",
        "instructions": "The person is trying to understand how or why something works, not only to get a task done.",
    },
    "read_previous": {
        "type": "noul",
        "instructions": ("The prompt shows the person engaged with the AI's previous reply: it refers to specifics "
                         "from it, builds on it, or questions it. False if there is no previous reply."),
    },
    "pasted_output": {
        "type": "noul",
        "instructions": ("Most of the prompt is pasted output (error messages, logs, stack traces, terminal output) "
                         "rather than the person's own words."),
    },
    "complexity": {
        "type": "score",
        "instructions": ("How technically difficult is the problem this prompt is dealing with? Judge the problem "
                         "itself, not how well the prompt is written or how skilled the person seems."),
        "criteria": [
            "Trivial: no real technical problem (approvals, chores, cleanup, settings)",
            "Routine: a small, familiar change or lookup",
            "Moderate: a real feature, a non-obvious bug, or a design choice",
            "Hard: architecture, tricky debugging, or concepts that take real study",
        ],
    },
    "own_thinking": {
        "type": "score",
        "instructions": "How much of the person's own reasoning does the prompt show?",
        "criteria": [
            "None: approval, pasted output, or a bare request",
            "Little: a short request with a hint of intent",
            "Some: explains what they want and why, or a partial diagnosis",
            "A lot: their own hypothesis, design decision or diagnosis, with reasoning",
        ],
    },
}


def turns_from_transcript(path, with_prev_prompt=False):
    """Yield (prompt record, prompt text, end of the previous turn's AI text[, previous prompt text])."""
    recs = importer._read(path)
    prev_texts, cur_texts = [], []
    prev_prompt = ""
    for r in recs:
        text = importer.prompt_text(r)
        if text is not None:
            prev = "\n".join(cur_texts) if cur_texts else "\n".join(prev_texts)
            yield (r, text, prev, prev_prompt) if with_prev_prompt else (r, text, prev)
            prev_texts, cur_texts = (cur_texts or prev_texts), []
            prev_prompt = text
            continue
        c = importer._blocks(r)
        if r.get("type") == "assistant" and isinstance(c, list):
            cur_texts.extend(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def ask_jev(key, state, retries=4):
    body = json.dumps({"model": MODEL, "state": state, "questions": QUESTIONS}).encode("utf-8")
    for attempt in range(retries):
        req = urllib.request.Request(ENDPOINT, data=body, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < retries - 1:
                time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                continue
            raise RuntimeError("Jev HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        except (urllib.error.URLError, TimeoutError, OSError):  # includes read timeouts
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def flatten(answers):
    a = answers or {}
    p = a.get("pattern", {})
    s = a.get("own_thinking", {})
    return {
        "pattern": p.get("choice"),
        "pattern_confidence": p.get("confidence"),
        "pattern_probs": p.get("probabilities"),
        "understanding": a.get("understanding", {}).get("noul"),
        "read_previous": a.get("read_previous", {}).get("noul"),
        "pasted_output": a.get("pasted_output", {}).get("noul"),
        "own_thinking": s.get("score"),
        "own_thinking_confidence": s.get("confidence"),
        "complexity": a.get("complexity", {}).get("score"),
    }


def load_key():
    key = os.environ.get("JEV_API_KEY")
    if key:
        return key.strip()
    try:
        with open(os.path.join(eventlog.data_dir(), "jev.key"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def labels_path():
    return os.path.join(eventlog.data_dir(), "jev", "events.jsonl")


def _config():
    try:
        with open(os.path.join(eventlog.data_dir(), "config.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _set_config(**kw):
    d = _config()
    d.update(kw)
    os.makedirs(eventlog.data_dir(), exist_ok=True)
    path = os.path.join(eventlog.data_dir(), "config.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(path + ".tmp", path)


def labelled_ids(path=None):
    out = set()
    try:
        with open(path or labels_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("prompt_id") and not d.get("error"):
                    out.add(d["prompt_id"])
    except OSError:
        pass
    return out


def jobs_from(paths, since=None, skip=(), modified_after=None, need_id=False):
    """(meta, state) for every prompt in these transcripts (files or folders) not in `skip`."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(f for f, is_sub in importer.find_transcripts(p) if not is_sub)
        elif os.path.isfile(p):
            files.append(p)
    jobs = []
    for path in files:
        if modified_after is not None:
            try:
                if os.path.getmtime(path) < modified_after:
                    continue
            except OSError:
                continue
        for r, text, prev, prev_prompt in turns_from_transcript(path, with_prev_prompt=True):
            if since and (r.get("timestamp") or "") < since:
                continue
            if (need_id and not r.get("promptId")) or (r.get("promptId") and r.get("promptId") in skip):
                continue
            state = {"prompt": redact(text[:MAX_PROMPT])}
            if prev.strip():
                state["ai_previous_reply_end"] = redact(prev[-MAX_PREV:])
            if prev_prompt.strip():
                state["previous_prompt"] = redact(prev_prompt[:MAX_PREV_PROMPT])
            meta = {"session": r.get("sessionId"), "prompt_id": r.get("promptId"), "at": r.get("timestamp"),
                    "cwd": r.get("cwd")}
            jobs.append((meta, state))
    return jobs


AUTO_MAX = 200      # most prompts one background run will send
AUTO_WAIT_S = 300   # how long a run waits for another to finish


def auto(paths, ask=None):
    """Background labelling, started by the hooks: label new prompts since the key was saved.

    Only labels and ids are written. Failures are logged and retried by the next run, since the text
    stays in Claude Code's own transcript and is never copied anywhere by ai-tracker.
    """
    key = load_key()
    since = _config().get("jev_since")
    if not key or not since:
        return 0
    ask = ask or ask_jev
    lp = labels_path()
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    deadline = time.monotonic() + AUTO_WAIT_S
    while True:  # one run at a time, so a prompt isn't sent twice
        lock = eventlog._FileLock(lp + ".lock")
        lock.__enter__()
        if lock.locked or time.monotonic() > deadline:
            break
        lock.__exit__(None, None, None)
        time.sleep(1)
    try:
        if not lock.locked:
            return 0
        try:
            cutoff = time.mktime(time.strptime(since[:19], "%Y-%m-%dT%H:%M:%S")) - 86400
        except ValueError:
            cutoff = None
        jobs = jobs_from(paths, since=since, skip=labelled_ids(lp), modified_after=cutoff,
                         need_id=True)[:AUTO_MAX]
        n = 0
        for meta, st in jobs:
            try:
                res = ask(key, st)
            except Exception as e:
                _log_error("jev auto: %s" % str(e)[:200])
                break  # offline or rate-limited: the next run picks up from here
            with open(lp, "a", encoding="utf-8") as out:
                out.write(json.dumps(dict(meta, model=res.get("model"), **flatten(res.get("answers")))) + "\n")
            n += 1
        return n
    finally:
        lock.__exit__(None, None, None)
        if ask is ask_jev:
            _sync_labels()


def _sync_labels():
    """New labels reach the server right away instead of waiting for the next turn."""
    try:
        import sync
        if sync.enabled():
            sync.sync()
    except Exception as e:
        _log_error("sync after labelling: %s" % str(e)[:200])


def _log_error(msg):
    try:
        with open(os.path.join(eventlog.data_dir(), "errors.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (eventlog.now_iso(), msg))
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sauron jev", description=__doc__.split("\n\n")[0])
    ap.add_argument("src", nargs="?")
    ap.add_argument("--save-key", action="store_true",
                    help="type your Hack Club AI key at a hidden prompt and save it, readable only by you")
    ap.add_argument("--out")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--since", help="only prompts at or after this UTC date/time (YYYY-MM-DD)")
    ap.add_argument("--auto", nargs="+", metavar="TRANSCRIPT",
                    help="background mode used by the hooks: label new prompts in these transcripts or folders")
    args = ap.parse_args(argv)
    if args.auto:
        auto([os.path.expanduser(p) for p in args.auto])
        return 0
    if args.save_key:
        import getpass
        k = getpass.getpass("Hack Club AI key (input hidden): ").strip()
        if not k:
            print("Nothing saved.")
            return 1
        os.makedirs(eventlog.data_dir(), exist_ok=True)
        path = os.path.join(eventlog.data_dir(), "jev.key")
        # Write a fresh 0600 file and swap it in, so an old key file's looser permissions don't carry over.
        tmp = path + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(k)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        if not _config().get("jev_since"):
            # Automatic labelling covers prompts from now on; older history is labelled only on request.
            _set_config(jev_since=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        print("Saved to %s (readable only by you)." % path)
        print("New prompts are now labelled by Jev in the background after each turn.")
        return 0
    if not args.src or not args.out:
        ap.error("give a transcripts folder and --out, or use --save-key")
    key = load_key()
    if not key:
        print("No Hack Club AI key: set JEV_API_KEY, or save it to %s" % os.path.join(eventlog.data_dir(), "jev.key"))
        return 1

    jobs = jobs_from([args.src], since=args.since)
    if args.limit:
        jobs = jobs[: args.limit]
    print("Labelling %d prompts with Jev..." % len(jobs))

    done, tokens, model = 0, 0, None
    with open(args.out, "w", encoding="utf-8") as out, \
            concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(ask_jev, key, st): meta for meta, st in jobs}
        for f in concurrent.futures.as_completed(futures):
            meta = futures[f]
            try:
                res = f.result()
            except Exception as e:  # keep going; report at the end
                out.write(json.dumps(dict(meta, error=str(e)[:200])) + "\n")
                continue
            tokens += (res.get("usage") or {}).get("input_tokens", 0)
            model = res.get("model")
            out.write(json.dumps(dict(meta, model=model, **flatten(res.get("answers")))) + "\n")
            done += 1
    print("Done: %d labelled, %s input tokens, model %s -> %s" % (done, format(tokens, ","), model, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
