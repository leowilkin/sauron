# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run: python3 -m unittest discover -s tests -v"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tracker"))

import aitrack  # noqa: E402
import timemodel  # noqa: E402
import claude_code  # noqa: E402
import eventlog  # noqa: E402

RUN_SH = os.path.join(ROOT, "hooks", "run.sh")
SECRET_PROMPT = "please refactor motion.c, my password is hunter2"
SECRET_CODE = "int secret_function(void) { return 42; }\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        os.environ["AI_TRACKER_DIR"] = self.data
        self._wk = os.environ.get("WAKATIME_HOME")
        os.environ["WAKATIME_HOME"] = self.tmp  # never read the real ~/.wakatime.cfg
        # A fake repo: greg/.git + greg/src
        self.repo = os.path.join(self.tmp, "greg")
        os.makedirs(os.path.join(self.repo, ".git"))
        os.makedirs(os.path.join(self.repo, "src"))

    def tearDown(self):
        os.environ.pop("AI_TRACKER_DIR", None)
        if self._wk is None:
            os.environ.pop("WAKATIME_HOME", None)
        else:
            os.environ["WAKATIME_HOME"] = self._wk
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hook(self, payload, via_shell=True):
        """Feed one payload to the hook exactly as Claude Code would."""
        data = json.dumps(payload).encode()
        if via_shell:
            env = dict(os.environ, AI_TRACKER_DIR=self.data)
            p = subprocess.run(["bash", RUN_SH], input=data, capture_output=True, env=env, timeout=10)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout, b"", "hooks must not print to stdout")
        else:
            claude_code.handle(payload)

    def lines(self):
        with open(os.path.join(self.data, "events.jsonl"), encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def payload(event, cwd, **kw):
    d = {"session_id": "s1", "prompt_id": "p1", "cwd": cwd, "hook_event_name": event,
         "transcript_path": "/x.jsonl", "permission_mode": "default"}
    d.update(kw)
    return d


class TestLineCounts(unittest.TestCase):
    """Shapes copied from real Claude Code 2.1 PostToolUse payloads."""

    def test_write_create(self):
        tr = {"type": "create", "content": "one\ntwo\nthree\n", "structuredPatch": [], "originalFile": None}
        self.assertEqual(claude_code.edit_line_counts("Write", {"content": "one\ntwo\nthree\n"}, tr), (3, 0))

    def test_write_update_uses_patch(self):
        tr = {"type": "update", "structuredPatch": [{"lines": [" a", "-b", "+B", " c", "-d"]}],
              "originalFile": "a\nb\nc\nd\n"}
        self.assertEqual(claude_code.edit_line_counts("Write", {"content": "a\nB\nc\n"}, tr), (1, 2))

    def test_edit_patch(self):
        tr = {"structuredPatch": [{"lines": [" one", "-two", "+TWO", "+extra", " three"]}]}
        self.assertEqual(claude_code.edit_line_counts("Edit", {}, tr), (2, 1))

    def test_edit_replace_all_counts_every_hunk(self):
        tr = {"structuredPatch": [{"lines": ["-c", "+cc"]}, {"lines": ["-c", "+cc"]}]}
        self.assertEqual(claude_code.edit_line_counts("Edit", {"replace_all": True}, tr), (2, 2))

    def test_edit_fallback_without_patch(self):
        ti = {"old_string": "a\nb\nc", "new_string": "a\nX\nY\nc"}
        self.assertEqual(claude_code.edit_line_counts("Edit", ti, {}), (2, 1))

    def test_multiedit_fallback(self):
        ti = {"edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "", "new_string": "x\ny"}]}
        self.assertEqual(claude_code.edit_line_counts("MultiEdit", ti, None), (3, 1))

    def test_write_no_trailing_newline(self):
        self.assertEqual(claude_code.edit_line_counts("Write", {"content": "a\nb"}, {"type": "create"}), (2, 0))


class TestHookEndToEnd(Base):
    def test_session_flow_and_privacy(self):
        cwd = os.path.join(self.repo, "src")
        f = os.path.join(self.repo, "src", "motion.c")
        self.hook(payload("SessionStart", cwd, source="startup"))
        self.hook(payload("UserPromptSubmit", cwd,
                          prompt=SECRET_PROMPT + "<pasted_content id=\"ab\">\n12345\n</pasted_content id=\"ab\">"))
        self.hook(payload("PostToolUse", cwd, tool_name="Write",
                          tool_input={"file_path": f, "content": SECRET_CODE},
                          tool_response={"type": "create", "content": SECRET_CODE,
                                         "structuredPatch": [], "originalFile": None}))
        self.hook(payload("PostToolUse", cwd, tool_name="Read", tool_input={"file_path": f}, tool_response={}))
        self.hook(payload("Stop", cwd, stop_hook_active=False,
                          last_assistant_message="Done!\n```c\n" + SECRET_CODE + "```"))
        self.hook(payload("SessionEnd", cwd, reason="prompt_input_exit"))

        ev = self.lines()
        self.assertEqual([e["event"] for e in ev],
                         ["session_start", "prompt_sent", "ai_edit", "ai_done", "session_end"])
        for e in ev:
            self.assertEqual(e["project"], "greg")
            self.assertEqual(e["session"], "s1")
            self.assertEqual(e["source"], "claude-code")
            datetime.datetime.fromisoformat(e["ts"])  # parses, has offset
            self.assertIsNotNone(datetime.datetime.fromisoformat(e["ts"]).utcoffset())
        self.assertEqual(ev[1]["chars"], len(SECRET_PROMPT))
        self.assertEqual(ev[1]["pasted_chars"], len("\n12345\n"))
        self.assertEqual(ev[2]["file"], "src/motion.c")
        self.assertEqual((ev[2]["lines_added"], ev[2]["lines_removed"]), (1, 0))
        self.assertEqual(ev[4]["reason"], "prompt_input_exit")
        self.assertEqual((ev[3]["response_text_chars"], ev[3]["response_code_lines"]), (5, 1))

        raw = read(os.path.join(self.data, "events.jsonl"))
        for secret in ("hunter2", "secret_function", "Done!", "12345"):
            self.assertNotIn(secret, raw)

    def test_non_repo_falls_back_to_folder_name(self):
        d = os.path.join(self.tmp, "scratch-folder")
        os.makedirs(d)
        self.hook(payload("Stop", d), via_shell=False)
        self.assertEqual(self.lines()[0]["project"], "scratch-folder")

    def test_worktree_named_after_main_repo(self):
        wt = os.path.join(self.tmp, "greg-feature")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write("gitdir: %s\n" % os.path.join(self.repo, ".git", "worktrees", "greg-feature"))
        self.hook(payload("Stop", wt), via_shell=False)
        self.assertEqual(self.lines()[0]["project"], "greg")

    def test_garbage_stdin_never_fails(self):
        env = dict(os.environ, AI_TRACKER_DIR=self.data)
        for data in (b"", b"not json", b"[1,2]", b"\xff\xfe"):
            p = subprocess.run(["bash", RUN_SH], input=data, capture_output=True, env=env, timeout=10)
            self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_unwritable_dir_never_fails(self):
        blocker = os.path.join(self.tmp, "file-not-dir")
        open(blocker, "w").close()
        env = dict(os.environ, AI_TRACKER_DIR=os.path.join(blocker, "sub"))
        p = subprocess.run(["bash", RUN_SH], input=json.dumps(payload("Stop", self.repo)).encode(),
                           capture_output=True, env=env, timeout=10)
        self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_latency(self):
        p = payload("UserPromptSubmit", self.repo, prompt="hi")
        times = []
        for _ in range(10):
            t = time.perf_counter()
            self.hook(p)
            times.append(time.perf_counter() - t)
        med = sorted(times)[len(times) // 2]
        print("\n  hook latency median %.1f ms, max %.1f ms" % (med * 1000, max(times) * 1000))
        # Interpreter startup dominates and varies wildly by machine and load, and the
        # hooks run async so Claude never waits on them. This is a sanity bound only.
        budget = float(os.environ.get("AI_TRACKER_LATENCY_BUDGET_MS", "500")) / 1000
        self.assertLess(med, budget)


class TestHashChain(Base):
    def _write(self, n):
        for i in range(n):
            eventlog.append_event({"event": "prompt_sent", "session": "s", "chars": i}, cwd=self.repo, source="t")
        return os.path.join(self.data, "events.jsonl")

    def test_intact(self):
        path = self._write(5)
        self.assertEqual(aitrack.verify(path), [])
        self.assertEqual(self.lines()[0]["prev_hash"], eventlog.GENESIS_HASH)

    def test_edit_detected(self):
        path = self._write(5)
        ls = read(path).splitlines(True)
        ls[2] = ls[2].replace('"chars":2', '"chars":9000')
        write(path, "".join(ls))
        probs = aitrack.verify(path)
        self.assertEqual(len(probs), 1)
        self.assertIn("line 4", probs[0])

    def test_deletion_detected(self):
        path = self._write(5)
        ls = read(path).splitlines(True)
        del ls[1]
        write(path, "".join(ls))
        self.assertTrue(aitrack.verify(path))

    def test_truncated_last_line_is_isolated(self):
        path = self._write(2)
        with open(path, "ab") as f:
            f.write(b'{"partial":')  # crash mid-write
        self._write(1)
        ev = read(path, "rb").splitlines()
        self.assertEqual(len(ev), 4)
        self.assertEqual(json.loads(ev[3])["chars"], 0)

    def test_concurrent_writers(self):
        code = ("import sys; sys.path.insert(0, %r); import eventlog\n"
                "for i in range(25): eventlog.append_event({'event':'x','session':sys.argv[1]}, cwd=%r, source='t')"
                % (os.path.join(ROOT, "tracker"), self.repo))
        env = dict(os.environ, AI_TRACKER_DIR=self.data)
        procs = [subprocess.Popen([sys.executable, "-c", code, str(i)], env=env) for i in range(8)]
        for p in procs:
            p.wait()
        path = os.path.join(self.data, "events.jsonl")
        self.assertEqual(len(self.lines()), 200)
        self.assertEqual(aitrack.verify(path), [])


class TestSummarize(unittest.TestCase):
    def ev(self, t, event, session="s1", project="greg", **kw):
        base = datetime.datetime(2026, 11, 18, 22, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))
        ts = (base + datetime.timedelta(seconds=t)).isoformat()
        d = {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": t, "event": event,
             "session": session, "project": project}
        d.update(kw)
        return d

    def test_cycles_and_response_times(self):
        evs = [
            self.ev(0, "session_start"),
            self.ev(10, "prompt_sent", chars=100, prompt_id="a"),
            self.ev(40, "ai_edit", file="motion.c", lines_added=140, lines_removed=3),
            self.ev(70, "ai_done", prompt_id="a"),            # cycle 60s
            self.ev(100, "prompt_sent", chars=50, prompt_id="b"),  # user 30s
            self.ev(120, "ai_done", prompt_id="b"),           # cycle 20s
            self.ev(220, "prompt_sent", chars=10, prompt_id="c"),  # user 100s
            self.ev(225, "prompt_sent", chars=5, prompt_id="d"),   # c interrupted
            self.ev(265, "ai_done", prompt_id="d"),           # cycle 40s
            self.ev(4000, "prompt_sent", chars=1, prompt_id="e"),  # break > 30m
            self.ev(4010, "ai_edit", file="motion.c", lines_added=2, lines_removed=2),
            self.ev(4100, "ai_done", prompt_id="e"),          # cycle 100s
            # Interleaved second session in another project
            self.ev(50, "prompt_sent", session="s2", project="other", chars=7),
            self.ev(55, "ai_done", session="s2", project="other"),
        ]
        out = aitrack.summarize(evs, idle_min=30)
        g = out[("2026-11-18", "greg")]
        self.assertEqual(g["prompts"], 5)
        self.assertEqual(g["chars"], 166)
        self.assertEqual(sorted(g["cycle_secs"]), [20, 40, 60, 100])
        self.assertEqual(g["interrupted"], 1)
        self.assertEqual(sorted(g["user_secs"]), [30, 100])
        self.assertEqual(g["breaks"], 1)
        self.assertEqual(g["files"]["motion.c"], [142, 5])
        o = out[("2026-11-18", "other")]
        self.assertEqual((o["prompts"], o["cycle_secs"]), (1, [5]))

    def test_cli_runs(self):
        tmp = tempfile.mkdtemp()
        try:
            os.environ["AI_TRACKER_DIR"] = tmp
            eventlog.append_event({"event": "prompt_sent", "session": "s", "chars": 3}, cwd=tmp, source="t")
            eventlog.append_event({"event": "ai_done", "session": "s"}, cwd=tmp, source="t")
            script = os.path.join(ROOT, "tracker", "aitrack.py")
            env = dict(os.environ)
            for args in (["summarize"], ["summarize", "--json", "--days", "1"], ["summarize", "--replies"],
                         ["summarize", "--json", "--replies"], ["verify"], []):
                p = subprocess.run([sys.executable, script] + args, capture_output=True, env=env)
                self.assertEqual(p.returncode, 0, p.stderr)
            j = json.loads(subprocess.run([sys.executable, script, "summarize", "--json"],
                                          capture_output=True, env=env).stdout)
            self.assertEqual(j[0]["prompts"], 1)
            self.assertEqual(j[0]["cycles"], 1)
        finally:
            os.environ.pop("AI_TRACKER_DIR", None)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class TestTimeModel(unittest.TestCase):
    TZ = datetime.timezone(datetime.timedelta(hours=-5))

    def ev(self, t, event, session="s1", project="A", base=None):
        base = base or datetime.datetime(2026, 11, 18, 12, 0, 0, tzinfo=self.TZ)
        ts = (base + datetime.timedelta(seconds=t)).isoformat()
        return {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": t, "event": event,
                "session": session, "project": project}

    def credited(self, evs, idle_min=15):
        import timemodel
        return {k: (round(v["you"]), round(v["ai"])) for k, v in timemodel.credited_time(evs, idle_min).items()}

    def test_single_session(self):
        evs = [self.ev(0, "session_start"), self.ev(10, "prompt_sent"), self.ev(70, "ai_done"),
               self.ev(100, "prompt_sent"), self.ev(120, "ai_done"), self.ev(130, "session_end")]
        # you: 0-10, 70-100, 120-130 = 50; ai: 10-70, 100-120 = 80
        self.assertEqual(self.credited(evs), {("2026-11-18", "A"): (50, 80)})

    def test_parallel_sessions_never_double_count(self):
        evs = [self.ev(0, "session_start"), self.ev(10, "prompt_sent"), self.ev(70, "ai_done"),
               self.ev(100, "prompt_sent"), self.ev(120, "ai_done"), self.ev(130, "session_end"),
               # Second session, other project, AI running 80-200 in parallel
               self.ev(80, "prompt_sent", "s2", "B"), self.ev(200, "ai_done", "s2", "B")]
        c = self.credited(evs)
        # 80-100: your reading in A beats B's agent. 100-120: A's newer AI turn wins.
        # 130-200: only B's agent is running.
        self.assertEqual(c[("2026-11-18", "A")], (50, 80))
        self.assertEqual(c[("2026-11-18", "B")], (0, 70))
        self.assertEqual(sum(y + a for y, a in c.values()), 200)  # == wall-clock union

    def test_gap_counts_only_reading_thinking_writing(self):
        # No reply size and no typed chars: just the 60 s thinking allowance, however long the gap.
        evs = [self.ev(0, "prompt_sent"), self.ev(60, "ai_done"),
               self.ev(60 + 16 * 60, "prompt_sent"), self.ev(60 + 16 * 60 + 30, "ai_done")]
        self.assertEqual(self.credited(evs), {("2026-11-18", "A"): (60, 90)})
        self.assertEqual(self.credited(evs, idle_min=20), {("2026-11-18", "A"): (60, 90)})

    def test_interrupted_turn_capped(self):
        evs = [self.ev(0, "prompt_sent"), self.ev(5000, "prompt_sent"), self.ev(5010, "ai_done")]
        self.assertEqual(self.credited(evs), {("2026-11-18", "A"): (0, 900 + 10)})

    def test_dangling_prompt_at_session_end_capped(self):
        evs = [self.ev(0, "prompt_sent"), self.ev(40, "session_end")]
        self.assertEqual(self.credited(evs), {("2026-11-18", "A"): (0, 40)})

    def test_long_ai_turn_not_capped(self):
        evs = [self.ev(0, "prompt_sent"), self.ev(3600, "ai_done")]
        self.assertEqual(self.credited(evs), {("2026-11-18", "A"): (0, 3600)})

    def test_midnight_split(self):
        base = datetime.datetime(2026, 11, 18, 23, 59, 0, tzinfo=self.TZ)
        evs = [self.ev(0, "prompt_sent", base=base), self.ev(120, "ai_done", base=base)]
        self.assertEqual(self.credited(evs), {("2026-11-18", "A"): (0, 60), ("2026-11-19", "A"): (0, 60)})

    def test_summarize_reports_time(self):
        evs = [self.ev(0, "session_start"), self.ev(10, "prompt_sent"), self.ev(70, "ai_done"),
               self.ev(100, "session_end")]
        b = aitrack.summarize(evs)[("2026-11-18", "A")]
        self.assertEqual((round(b["you_s"]), round(b["ai_s"])), (40, 60))
        row = aitrack.to_json({("2026-11-18", "A"): b})[0]
        self.assertEqual((row["active_s"], row["you_s"], row["ai_only_s"]), (100, 40, 60))


class TestSummarizeOutput(unittest.TestCase):
    TZ = datetime.timezone(datetime.timedelta(hours=-5))

    def ev(self, day, t, event, **kw):
        base = datetime.datetime(2026, 11, day, 12, 0, 0, tzinfo=self.TZ)
        ts = (base + datetime.timedelta(seconds=t)).isoformat()
        d = {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": day * 10000 + t, "event": event,
             "session": "s%d" % day, "project": "A"}
        d.update(kw)
        return d

    def run_text(self, evs, replies=False):
        import contextlib
        import io
        out = aitrack.summarize(evs)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            aitrack.print_text(out, 15, 10)
            if replies:
                aitrack.print_replies(out)
        return buf.getvalue(), out

    def test_overall_and_replies(self):
        evs = []
        for day in (17, 18):
            evs += [self.ev(day, 0, "prompt_sent", chars=10), self.ev(day, 60, "ai_done", response_text_chars=600),
                    self.ev(day, 61, "prompt_sent", chars=5),  # 1s reply to a 30s read -> FAST
                    self.ev(day, 90, "ai_done")]
        text, out = self.run_text(evs, replies=True)
        self.assertIn("All days  2026-11-17 -> 2026-11-18   (2 days with activity)", text)
        self.assertIn("prompts           4", text)
        self.assertIn("AI turns          4", text)
        self.assertEqual(text.count("FAST"), 3)  # two rows + the legend
        rows = aitrack.to_json(out, replies=True)
        self.assertEqual(rows[0]["replies"], [{"at": "2026-11-17T12:01:00-05:00", "gap_s": 1, "read_s": 30,
                                               "fast": True, "break": False}])
        self.assertNotIn("replies", aitrack.to_json(out)[0])

    def test_single_day_has_no_overall(self):
        text, _ = self.run_text([self.ev(18, 0, "prompt_sent"), self.ev(18, 60, "ai_done")])
        self.assertNotIn("All days", text)


class TestReadTime(unittest.TestCase):
    TZ = datetime.timezone(datetime.timedelta(hours=-5))

    def ev(self, t, event, **kw):
        base = datetime.datetime(2026, 11, 18, 12, 0, 0, tzinfo=self.TZ)
        ts = (base + datetime.timedelta(seconds=t)).isoformat()
        d = {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": t, "event": event,
             "session": "s1", "project": "A"}
        d.update(kw)
        return d

    def analyze(self, evs, idle_min=15):
        import timemodel
        credited, reads = timemodel.analyze(evs, idle_min)
        return {k: (round(v["you"]), round(v["ai"])) for k, v in credited.items()}, reads

    def test_response_size(self):
        msg = "Here you go:\n```python\nx = 1\ny = 2\n```\nDone."
        self.assertEqual(claude_code.response_size(msg), (len("Here you go:\nDone."), 2))
        self.assertEqual(claude_code.response_size("~~~\na\n```\nb\n~~~\nok"), (2, 3))
        self.assertEqual(claude_code.response_size(None), (0, 0))

    def test_read_seconds(self):
        import timemodel
        # 2000 prose chars = 100s; 10 code lines + 40 diff lines = 100s
        self.assertEqual(timemodel.read_seconds({"response_text_chars": 2000, "response_code_lines": 10}, 40), 200)
        self.assertEqual(timemodel.read_seconds({"response_code_lines": 10 ** 6}, 0), timemodel.MAX_READ_S)

    def test_big_diff_extends_break_limit(self):
        # 900 diff lines -> 1800s (30 min) to read; a 25 min gap is reading, not a break
        evs = [self.ev(0, "prompt_sent", prompt_id="a"),
               self.ev(30, "ai_edit", prompt_id="a", lines_added=900, lines_removed=0),
               self.ev(60, "ai_done", prompt_id="a"),
               self.ev(60 + 1500, "prompt_sent", prompt_id="b")]
        credited, reads = self.analyze(evs)
        # reading is capped at the idle cutoff (900 s) plus 60 s thinking, not the whole 1500 s gap
        self.assertEqual(credited, {("2026-11-18", "A"): (960, 60)})
        self.assertEqual((reads[0].is_break, reads[0].read_s), (False, 1800))

    def test_break_credit_capped_at_idle(self):
        # Real case: one prompt, big agent diff (read time hits the 1h cap), then 36h away.
        evs = [self.ev(0, "prompt_sent", prompt_id="a"),
               self.ev(60, "ai_edit", prompt_id="a", lines_added=3000, lines_removed=1000),
               self.ev(120, "ai_done", prompt_id="a"),
               self.ev(120 + 36 * 3600, "prompt_sent", prompt_id="b")]
        credited, reads = self.analyze(evs)
        self.assertEqual(credited, {("2026-11-18", "A"): (960, 120)})
        self.assertTrue(reads[0].is_break)

    def test_break_still_earns_read_time(self):
        # 4000 chars -> 200s to read; then a 2h gap. Credit the 200s, not the 2h, not zero.
        evs = [self.ev(0, "prompt_sent"), self.ev(60, "ai_done", response_text_chars=4000),
               self.ev(60 + 7200, "prompt_sent")]
        credited, reads = self.analyze(evs)
        self.assertEqual(credited, {("2026-11-18", "A"): (260, 60)})  # 200 s reading + 60 s thinking
        self.assertTrue(reads[0].is_break)

    def test_fast_reply_flagged(self):
        # 600 diff lines = 20 min to read; next prompt 5s later
        evs = [self.ev(0, "prompt_sent", prompt_id="a"),
               self.ev(50, "ai_edit", prompt_id="a", lines_added=600),
               self.ev(60, "ai_done", prompt_id="a"),
               self.ev(65, "prompt_sent", prompt_id="b"),
               # small reply: answering in 5s is fine
               self.ev(70, "ai_done", prompt_id="b", response_text_chars=100),
               self.ev(75, "prompt_sent", prompt_id="c")]
        _, reads = self.analyze(evs)
        self.assertEqual([r.fast for r in reads], [True, False])
        b = aitrack.summarize(evs)[("2026-11-18", "A")]
        self.assertEqual(b["fast"], 1)

    def test_late_async_edit_still_matched_by_prompt_id(self):
        # Async PostToolUse can be logged just after the synchronous Stop.
        evs = [self.ev(0, "prompt_sent", prompt_id="a"), self.ev(60, "ai_done", prompt_id="a"),
               self.ev(61, "ai_edit", prompt_id="a", lines_added=30), self.ev(70, "prompt_sent", prompt_id="b")]
        _, reads = self.analyze(evs)
        self.assertEqual(reads[0].read_s, 60)

    def test_edit_without_prompt_id_uses_turn_window(self):
        evs = [self.ev(0, "prompt_sent"), self.ev(30, "ai_edit", lines_added=15),
               self.ev(60, "ai_done"), self.ev(70, "prompt_sent")]
        _, reads = self.analyze(evs)
        self.assertEqual(reads[0].read_s, 30)


class TestImporter(unittest.TestCase):
    """Synthetic transcript in the Claude Code 2.1.3xx shape."""

    def test_import(self):
        import importer
        tmp = tempfile.mkdtemp()
        try:
            proj = os.path.join(tmp, "projects", "-home-x-greg")
            os.makedirs(os.path.join(proj, "sess1", "subagents"))
            cwd = "/home/x/greg"  # doesn't exist here, like an export from another machine
            def rec(t, typ, **kw):
                d = {"type": typ, "timestamp": "2026-10-06T16:%02d:00.000Z" % t, "sessionId": "sess1",
                     "cwd": cwd, "promptId": "p1"}
                d.update(kw)
                return json.dumps(d)
            lines = [
                rec(0, "permission-mode"),
                rec(1, "user", promptSource="typed", origin={"kind": "human"},
                    message={"role": "user", "content": "fix the jump " + SECRET_PROMPT}),
                rec(2, "assistant", message={"content": [
                    {"type": "text", "text": "Looking."},
                    {"type": "tool_use", "id": "t1", "name": "Edit",
                     "input": {"file_path": cwd + "/src/motion.c", "old_string": "a", "new_string": "b"}}]}),
                rec(3, "user", message={"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
                    toolUseResult={"filePath": cwd + "/src/motion.c",
                                   "structuredPatch": [{"lines": ["-a", "+b", "+c"]}]}),
                rec(4, "user", isMeta=True, message={"content": "<local-command-caveat>x"}),
                rec(5, "user", message={"content": "<command-name>/model</command-name>"}),
                rec(6, "assistant", message={"content": [{"type": "text", "text": "Done:\n```c\nint x;\n```"}]}),
                rec(9, "user", promptSource="typed", promptId="p2", message={"content": "thanks"}),
            ]
            write(os.path.join(proj, "sess1.jsonl"), "\n".join(lines) + "\n")
            out = os.path.join(tmp, "out.jsonl")
            n_files, n_events = importer.import_dir(os.path.join(tmp, "projects"), out, utc_offset_h=-5)
            ev = [json.loads(l) for l in read(out).splitlines()]
            self.assertEqual([e["event"] for e in ev],
                             ["session_start", "prompt_sent", "ai_edit", "ai_done", "prompt_sent", "session_end"])
            self.assertTrue(all(e["source"] == "claude-code-import" and e["project"] == "greg" for e in ev))
            self.assertEqual(ev[1]["ts"], "2026-10-06T11:01:00-05:00")
            self.assertEqual(ev[1]["chars"], len("fix the jump " + SECRET_PROMPT))
            self.assertEqual((ev[2]["file"], ev[2]["lines_added"], ev[2]["lines_removed"]), ("src/motion.c", 2, 1))
            # All text in the turn counts, not just the last message
            self.assertEqual((ev[3]["response_text_chars"], ev[3]["response_code_lines"]),
                             (len("Looking.\nDone:"), 1))
            self.assertEqual(aitrack.verify(out), [])
            self.assertNotIn("hunter2", read(out))

            # A turn_duration record ends the turn; later AI work without a typed prompt doesn't stretch it.
            lines2 = lines[:7] + [
                rec(7, "system", subtype="turn_duration", durationMs=360000),
                rec(8, "assistant", message={"content": [{"type": "text", "text": "Background task finished."}]}),
            ] + lines[7:]
            write(os.path.join(proj, "sess1.jsonl"), "\n".join(lines2) + "\n")
            importer.import_dir(os.path.join(tmp, "projects"), out, utc_offset_h=-5)
            ev = [json.loads(l) for l in read(out).splitlines()]
            done = [e for e in ev if e["event"] == "ai_done"]
            self.assertEqual([d["ts"][11:16] for d in done], ["11:07"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestServeSources(Base):
    def test_find_sources(self):
        eventlog.append_event({"event": "x", "session": "s"}, cwd=self.repo, source="t")
        friends = os.path.join(self.tmp, "friends")
        os.makedirs(friends)
        write(os.path.join(friends, "raygen.jsonl"), "")
        write(os.path.join(friends, "notes.txt"), "")
        labels = [l for l, _ in aitrack.find_sources([friends, os.path.join(friends, "missing.jsonl")])]
        self.assertEqual(labels, ["Live log", "raygen"])

    def test_dashboard_page_exists(self):
        page = read(os.path.join(ROOT, "tracker", "dashboard.html"))
        self.assertIn("/api/data", page)


class TestTyping(Base):
    """The module (hooks/typing/) writes per-draft files; the Python hooks fold them in."""

    def drop(self, sid, at, kind, **stats):
        d = os.path.join(self.data, "typing", sid)
        os.makedirs(d, exist_ok=True)
        write(os.path.join(d, "%d-%s.json" % (at, kind)), json.dumps(dict({"v": 1, "kind": kind, "at": at}, **stats)))
        return d

    def test_prompt_gets_typing_fields_and_files_are_consumed(self):
        now = int(time.time() * 1000)
        d = self.drop("s1", now - 2000, "abandoned", typing_ms=4000, keys_typed=30)
        self.drop("s1", now - 500, "sent", typing_ms=12000, compose_ms=20000, keys_typed=140,
                  pastes=1, fixes=6, rewrites=1, revisions=2, pauses=0, chars_deleted=25)
        self.hook(payload("UserPromptSubmit", self.repo, prompt="x" * 140))
        e = self.lines()[0]
        self.assertEqual((e["typing_ms"], e["keys_typed"], e["fixes"], e["rewrites"]), (12000, 140, 6, 1))
        self.assertEqual((e["drafts_abandoned"], e["abandoned_typing_ms"]), (1, 4000))
        self.assertEqual(os.listdir(d), [])

    def test_no_module_data_means_no_fields(self):
        self.hook(payload("UserPromptSubmit", self.repo, prompt="hello"))
        self.assertNotIn("typing_ms", self.lines()[0])

    def test_stale_sent_file_is_not_attached(self):
        now = int(time.time() * 1000)
        self.drop("s1", now - 10 * 60 * 1000, "sent", typing_ms=999)
        self.hook(payload("UserPromptSubmit", self.repo, prompt="hello"))
        self.assertNotIn("typing_ms", self.lines()[0])

    def test_two_queued_prompts_each_get_their_own(self):
        import typingstats
        now = int(time.time() * 1000)
        self.drop("s1", now - 900, "sent", typing_ms=1000)
        self.drop("s1", now - 100, "sent", typing_ms=2000)
        self.assertEqual(typingstats.for_prompt("s1", now)["typing_ms"], 1000)
        self.assertEqual(typingstats.for_prompt("s1", now)["typing_ms"], 2000)
        self.assertEqual(typingstats.for_prompt("s1", now), {})

    def test_session_end_takes_leftover_drafts_and_cleans_up(self):
        now = int(time.time() * 1000)
        d = self.drop("s1", now, "abandoned", typing_ms=3000)
        self.hook(payload("SessionEnd", self.repo, reason="prompt_input_exit"))
        e = self.lines()[0]
        self.assertEqual((e["drafts_abandoned"], e["abandoned_typing_ms"]), (1, 3000))
        self.assertFalse(os.path.exists(d))

    def test_bad_session_id_is_ignored(self):
        import typingstats
        self.assertEqual(typingstats.for_prompt("../../etc"), {})

    def test_typing_is_credited_even_after_a_break(self):
        import timemodel
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        base = datetime.datetime(2026, 11, 18, 12, 0, 0, tzinfo=tz)

        def ev(t, event, **kw):
            ts = (base + datetime.timedelta(seconds=t)).isoformat()
            d = {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": t, "event": event,
                 "session": "s1", "project": "A"}
            d.update(kw)
            return d
        # Reply came 2h later, but 5 min of it was typing the prompt (+1 min on a draft given up).
        evs = [ev(0, "prompt_sent"), ev(60, "ai_done"),
               ev(60 + 7200, "prompt_sent", typing_ms=300000, abandoned_typing_ms=60000)]
        c = timemodel.credited_time(evs)
        self.assertEqual(round(c[("2026-11-18", "A")]["you"]), 60 + 360)  # thinking + 5 min typing + 1 min draft
        b = aitrack.summarize(evs)[("2026-11-18", "A")]
        self.assertEqual((b["typed_prompts"], b["typing_ms"]), (1, 300000))


class TestReplays(Base):
    def drop(self, at, replay, **stats):
        d = os.path.join(self.data, "typing", "s1")
        os.makedirs(d, exist_ok=True)
        rec = dict({"v": 1, "kind": "sent", "at": at, "typing_ms": 5000, "keys_typed": 9}, replay=replay, **stats)
        write(os.path.join(d, "%d-sent.json" % at), json.dumps(rec))

    def test_replay_stored_and_hash_on_event(self):
        import typingstats
        now = int(time.time() * 1000)
        self.drop(now - 100, {"initial": "", "steps": [[0, 0, 0, "3a"], [150, 3, 3, "_5a"], [400, 8, 9, ""]]})
        self.hook(payload("UserPromptSubmit", self.repo, prompt="x" * 8))
        e = self.lines()[0]
        lines = read(typingstats.replays_path("s1"), "rb").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(e["replay_hash"], eventlog.line_hash(lines[0]))
        rec = json.loads(lines[0])
        self.assertEqual((rec["prompt_id"], rec["steps"][1]), ("p1", [150, 3, 3, "_5a"]))

    def test_replay_with_text_is_refused(self):
        import typingstats
        now = int(time.time() * 1000)
        self.drop(now - 100, {"initial": "", "steps": [[0, 0, 0, "hunter2"]]})
        self.hook(payload("UserPromptSubmit", self.repo, prompt="hunter2"))
        e = self.lines()[0]
        self.assertNotIn("replay_hash", e)
        self.assertEqual(e["keys_typed"], 9)  # stats still recorded
        self.assertFalse(os.path.exists(typingstats.replays_path("s1")))


class TestPresence(unittest.TestCase):
    """Gaps count only as reading + thinking + writing; away_summary cuts them."""
    TZ = datetime.timezone(datetime.timedelta(hours=-5))

    def ev(self, t, event, **kw):
        base = datetime.datetime(2026, 11, 18, 12, 0, 0, tzinfo=self.TZ)
        ts = (base + datetime.timedelta(seconds=t)).isoformat()
        d = {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": t, "event": event,
             "session": "s1", "project": "A"}
        d.update(kw)
        return d

    def you(self, evs):
        import timemodel
        return round(timemodel.credited_time(evs)[("2026-11-18", "A")]["you"])

    def test_writing_estimated_from_typed_chars(self):
        # 2000 chars reply = 100 s reading; +60 s thinking; 70 typed chars / 3.5 = 20 s writing; gap 10 min
        evs = [self.ev(0, "prompt_sent", chars=5), self.ev(60, "ai_done", response_text_chars=2000),
               self.ev(660, "prompt_sent", chars=70, pasted_chars=5000)]
        self.assertEqual(self.you(evs), round(5 / 3.5) + 100 + 60 + 20)

    def test_short_gap_counts_fully(self):
        evs = [self.ev(0, "prompt_sent"), self.ev(60, "ai_done", response_text_chars=2000),
               self.ev(120, "prompt_sent", chars=35)]
        self.assertEqual(self.you(evs), 60)  # the whole 60 s gap, never more than the gap

    def test_away_summary_cuts_the_gap(self):
        # 20 min reply needs 1200 s (capped 900) to read, but Claude Code logged you away at +3 min.
        evs = [self.ev(0, "prompt_sent"), self.ev(60, "ai_done", response_text_chars=24000),
               self.ev(60 + 180, "away"), self.ev(60 + 3600, "prompt_sent", typing_ms=30000)]
        # you'd left 3 min before the away record, i.e. right when the AI finished: only the 30 s typing counts
        self.assertEqual(self.you(evs), 30)

    def test_importer_emits_away_events(self):
        import importer
        tmp = tempfile.mkdtemp()
        try:
            proj = os.path.join(tmp, "p", "-x")
            os.makedirs(proj)
            recs = [
                {"type": "user", "timestamp": "2026-11-18T17:00:00Z", "sessionId": "s", "cwd": "/x/a",
                 "promptSource": "typed", "message": {"content": "hi"}},
                {"type": "assistant", "timestamp": "2026-11-18T17:00:30Z", "sessionId": "s", "cwd": "/x/a",
                 "message": {"content": [{"type": "text", "text": "hello"}]}},
                {"type": "system", "subtype": "turn_duration", "timestamp": "2026-11-18T17:00:31Z", "sessionId": "s", "cwd": "/x/a"},
                {"type": "system", "subtype": "away_summary", "timestamp": "2026-11-18T17:03:31Z", "sessionId": "s",
                 "cwd": "/x/a", "content": "While you were away..."},
            ]
            write(os.path.join(proj, "s.jsonl"), "\n".join(json.dumps(r) for r in recs) + "\n")
            out = os.path.join(tmp, "o.jsonl")
            importer.import_dir(os.path.join(tmp, "p"), out, utc_offset_h=0)
            kinds = [json.loads(l)["event"] for l in read(out).splitlines()]
            self.assertIn("away", kinds)
            self.assertNotIn("While you were away", read(out))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPace(unittest.TestCase):
    def test_personal_pace_is_median_of_real_prompts(self):
        import timemodel
        evs = [{"event": "prompt_sent", "typing_ms": ms, "keys_typed": k}
               for ms, k in ((10000, 50), (10000, 60), (20000, 60), (1000, 5))]  # last one too short to count
        pace, n = timemodel.personal_pace(evs)
        self.assertEqual((round(pace, 2), n), (5.0, 3))

    def test_too_few_samples_falls_back(self):
        import timemodel
        pace, n = timemodel.personal_pace([{"event": "prompt_sent", "typing_ms": 10000, "keys_typed": 80}])
        self.assertEqual((pace, n), (timemodel.TYPE_CHARS_PER_S, 1))

    def test_compose_uses_pace_only_without_recorded_typing(self):
        import timemodel
        self.assertEqual(timemodel.compose_seconds({"chars": 100}, pace=5.0), 20.0)
        self.assertEqual(timemodel.compose_seconds({"chars": 100, "typing_ms": 9000}, pace=5.0), 9.0)


class TestTypingDuringAITurn(unittest.TestCase):
    def test_recorded_typing_before_ai_finished_counts(self):
        import timemodel
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        base = datetime.datetime(2026, 11, 18, 12, 0, 0, tzinfo=tz)
        def ev(t, event, **kw):
            ts = (base + datetime.timedelta(seconds=t)).isoformat()
            d = {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": t, "event": event, "session": "s", "project": "A"}
            d.update(kw)
            return d
        # AI works 0..100; you type for 43 s, sending 4 s after it finished.
        evs = [ev(0, "prompt_sent"), ev(100, "ai_done"), ev(104, "prompt_sent", typing_ms=43000)]
        reads = []
        credited = timemodel.attribute(timemodel.all_segments(evs, 900, reads))
        self.assertEqual(round(reads[0].credited_s), 43)          # the typing, not just the 4 s gap
        self.assertEqual(round(credited[("2026-11-18", "A")]["you"]), 43)
        self.assertEqual(round(credited[("2026-11-18", "A")]["ai"]), 61)  # AI time while you typed goes to you


class TestLabels(Base):
    def test_record_and_load_labels(self):
        aitrack.record_label("abc12345-x", "yes")
        aitrack.record_label("def12345-y", "partly")
        aitrack.record_label("abc12345-x", "no")      # changed mind: latest wins
        aitrack.record_label("def12345-y", None)      # cleared
        self.assertEqual(aitrack.load_labels(), {"abc12345-x": "no"})

    def test_bad_labels_refused(self):
        for pid, label in (("../etc", "yes"), ("abc12345", "maybe"), (None, "yes")):
            with self.assertRaises(ValueError):
                aitrack.record_label(pid, label)


class TestPresence2(Base):
    def test_categories(self):
        import presence
        cases = {"Terminal": "terminal", "iTerm2": "terminal", "WindowsTerminal.exe": "terminal", "Code": "editor",
                 "Cursor": "editor", "jetbrains-pycharm": "editor", "Google Chrome": "browser", "msedge.exe": "browser",
                 "Claude": "ai_app", "loginwindow": "locked", "Slack": "other", None: "unknown"}
        for app, want in cases.items():
            self.assertEqual(presence.category(app), want, app)

    def test_off_by_default_and_never_started_then(self):
        import presence
        self.assertFalse(presence.enabled())
        self.assertFalse(presence.ensure_running())
        os.makedirs(self.data, exist_ok=True)
        write(os.path.join(self.data, "config.json"), '{"presence": true}')
        self.assertTrue(presence.enabled())

    def test_report_splits_a_gap_by_what_presence_saw(self):
        import presence
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        base = datetime.datetime(2026, 11, 18, 12, 0, 0, tzinfo=tz)
        def ev(t, event, **kw):
            ts = (base + datetime.timedelta(seconds=t)).isoformat()
            d = {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": t, "event": event, "session": "s", "project": "A"}
            d.update(kw)
            return d
        def pl(t, app, active):
            ts = (base + datetime.timedelta(seconds=t)).isoformat()
            return {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "app": app, "idle": 0 if active else 300, "active": active}
        events = [ev(0, "prompt_sent"), ev(60, "ai_done", response_text_chars=2000), ev(300, "prompt_sent", typing_ms=20000)]
        # gap 60..300: terminal active 60..120, browser active 120..180, then inactive 180..300
        lines = [pl(60, "terminal", True), pl(120, "browser", True), pl(180, "terminal", False), pl(240, "terminal", False)]
        rows = aitrack.presence_report(events, lines)
        self.assertEqual(len(rows), 1)
        seen = rows[0]["seen"]
        self.assertEqual((round(seen["terminal"]), round(seen["browser"]), round(seen["inactive"])), (60, 60, 120))
        self.assertEqual(rows[0]["gap_s"], 240)


class TestReviewFindings(Base):
    """Regression tests for the external review of 2026-11-19."""

    def _fixture(self):
        sid = "3de7a071-b66c-4c3b-96fb-35d59234ec76"
        repo = self.repo
        eventlog.append_event({"event": "session_start", "session": sid}, cwd=repo, source="t")
        eventlog.append_event({"event": "prompt_sent", "session": sid, "prompt_id": "p-0001", "chars": 20,
                               "typing_ms": 5000, "keys_typed": 20, "replay_hash": "x"}, cwd=repo, source="t")
        eventlog.append_event({"event": "ai_edit", "session": sid, "prompt_id": "p-0001", "file": "src/private-name.py",
                               "lines_added": 3, "lines_removed": 1}, cwd=repo, source="t")
        eventlog.append_event({"event": "ai_done", "session": sid, "prompt_id": "p-0001"}, cwd=repo, source="t")
        # a transcript holding the prompt's text, for --prompt-text
        proj = os.path.join(self.tmp, "projects", "-x")
        os.makedirs(proj)
        write(os.path.join(proj, sid + ".jsonl"), json.dumps({
            "type": "user", "timestamp": "2026-11-18T17:00:00Z", "sessionId": sid, "cwd": repo, "promptId": "p-0001",
            "promptSource": "typed", "message": {"content": "my secret prompt about private-name.py"}}) + "\n")
        os.makedirs(os.path.join(self.data, "replays"))
        write(os.path.join(self.data, "replays", sid + ".jsonl"), json.dumps({"session": sid, "steps": [[0, 0, 0, "3a"]]}) + "\n")
        return sid, os.path.join(self.tmp, "projects")

    def _export(self, *extra):
        out = os.path.join(self.tmp, "site")
        shutil.rmtree(out, ignore_errors=True)
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            aitrack.main(["export-site", "--out", out] + list(extra))
        return read(os.path.join(out, "data.json"))

    def test_default_export_publishes_no_names_ids_text_or_replays(self):
        sid, _ = self._fixture()
        blob = self._export("--hide-file-names")
        self.assertNotIn("private-name", blob)
        self.assertNotIn(sid, blob)
        self.assertNotIn("p-0001", blob)
        self.assertNotIn("secret prompt", blob)
        self.assertNotIn(self.tmp, blob)  # no local paths at all
        snap = json.loads(blob)
        self.assertTrue(all(not v.get("replays") for k, v in snap.items() if k.startswith("/api/replays")))
        rows = [r for k, v in snap.items() if k.startswith("/api/data") for r in v["rows"]]
        self.assertEqual(sum(f["added"] for r in rows for f in r["files"].values()), 3)  # totals survive

    def test_opt_ins_add_exactly_what_was_asked(self):
        sid, transcripts = self._fixture()
        blob = self._export("--prompt-text", transcripts)
        self.assertIn("private-name", blob)
        self.assertIn("secret prompt", blob)
        self.assertNotIn(sid, blob)  # session ids are never published

    def test_serve_does_not_read_prompt_text_unless_asked(self):
        # the /api/prompts payload without --prompt-text has no text
        self._fixture()
        payload = aitrack.api_payload(aitrack.find_sources([]), "/api/prompts", {"source": "0"}, 15)
        self.assertTrue(all("text" not in r for r in payload["rows"]))

    def test_redaction_catches_short_passwords_and_emails(self):
        import jev_judge
        for s in ('password="short"', "token: abc", "API_KEY=xyz", "mail me at kid@example.com", "Bearer abc.def.ghi1234"):
            self.assertNotIn(s.split("=")[-1].split(":")[-1].split()[-1].strip('"'), jev_judge.redact(s), s)

    def test_verify_with_saved_head_catches_end_edits(self):
        for i in range(3):
            eventlog.append_event({"event": "x", "session": "s", "n": i}, cwd=self.repo, source="t")
        p = eventlog.log_path()
        n, h = aitrack.head(p)
        lines = read(p).splitlines()
        write(p, "\n".join(lines[:-1] + [lines[-1].replace('"n":2', '"n":9')]) + "\n")
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(aitrack.main(["verify", "--expect-head", "%d:%s" % (n, h)]), 1)
        write(p, "\n".join(lines[:-1]) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(aitrack.main(["verify", "--expect-head", "%d:%s" % (n, h)]), 1)

    def test_completion_for_another_prompt_is_not_credited_in_full(self):
        import timemodel
        tz = datetime.timezone.utc
        b = datetime.datetime(2026, 11, 18, 12, tzinfo=tz)
        def ev(t, k, **kw):
            ts = (b + datetime.timedelta(seconds=t)).isoformat()
            d = {"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": t, "event": k, "session": "s", "project": "A"}
            d.update(kw)
            return d
        c = timemodel.credited_time([ev(0, "prompt_sent", prompt_id="new"), ev(3600, "ai_done", prompt_id="old")])
        self.assertLessEqual(c[("2026-11-18", "A")]["ai"], 900)


class TestReviewFindings2(Base):
    """Regression tests for the second review round (2026-11-20)."""
    _fixture = TestReviewFindings._fixture
    _export = TestReviewFindings._export

    def test_default_export_matches_hackatime_and_hide_flags_work(self):
        sid, _ = self._fixture()
        extra = os.path.join(self.tmp, "my-private-log-name.jsonl")
        shutil.copy(eventlog.log_path(), extra)
        project = os.path.basename(self.repo)
        # Default: what Hackatime heartbeats already carry (project, file path), nothing beyond it.
        blob = self._export(extra)
        self.assertIn(project, blob)
        self.assertIn("private-name", blob)
        self.assertNotIn("secret prompt", blob)
        self.assertNotIn(sid, blob)
        hidden = self._export(extra, "--hide-project-names", "--hide-file-names")
        for s in (project, "my-private-log-name", "private-name"):
            self.assertNotIn(s, hidden)
        self.assertIn("project 1", hidden)

    def test_wakatime_cfg_hide_settings_are_honoured(self):
        self._fixture()
        home = os.path.join(self.tmp, "wk")
        os.makedirs(home)
        write(os.path.join(home, ".wakatime.cfg"), "[settings]\nhide_file_names = true\nhide_project_names = false\n")
        os.environ["WAKATIME_HOME"] = home
        self.assertEqual(aitrack.wakatime_hide_settings(), (False, True))
        blob = self._export()
        self.assertNotIn("private-name", blob)
        self.assertIn(os.path.basename(self.repo), blob)

    def test_mismatched_completion_counts_as_interrupted(self):
        tz = datetime.timezone.utc
        b = datetime.datetime(2026, 11, 18, 12, tzinfo=tz)
        evs = []
        for i, (t, k, pid) in enumerate([(0, "prompt_sent", "new"), (3600, "ai_done", "old")]):
            ts = (b + datetime.timedelta(seconds=t)).isoformat()
            evs.append({"ts": ts, "_t": datetime.datetime.fromisoformat(ts), "_line": i, "event": k,
                        "session": "s", "project": "A", "prompt_id": pid})
        rows = aitrack.to_json(aitrack.summarize(evs, 15))
        self.assertEqual(sum(r["interrupted"] for r in rows), 1)
        self.assertEqual(sum(r["cycles"] for r in rows), 0)

    def test_plain_verify_catches_end_edit_after_an_earlier_verify(self):
        import contextlib, io
        for i in range(3):
            eventlog.append_event({"event": "x", "session": "s", "n": i}, cwd=self.repo, source="t")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(aitrack.main(["verify"]), 0)
        p = eventlog.log_path()
        lines = read(p).splitlines()
        write(p, "\n".join(lines[:-1]) + "\n")  # drop the last record
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(aitrack.main(["verify"]), 1)

    def test_redaction_catches_personal_details(self):
        import jev_judge
        for s in ("call (415) 555-0134", "or +1 415-555-0134", "SSN 123-45-6789", "card 4111 1111 1111 1111",
                  "ship to 1600 Pennsylvania Avenue NW, Apt 4B"):
            self.assertIn("[REDACTED]", jev_judge.redact(s), s)
        self.assertEqual(jev_judge.redact("open /Users/alex/Downloads/x.zip"), "open ~/Downloads/x.zip")
        self.assertEqual(jev_judge.redact("C:\\Users\\alex\\code"), "~\\code")
        for s in ("version 2.1.3 on port 8765", "ts 1795136720123", "line 42"):
            self.assertEqual(jev_judge.redact(s), s)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_save_key_tightens_existing_permissions(self):
        import jev_judge, getpass
        path = os.path.join(self.data, "jev.key")
        os.makedirs(self.data, exist_ok=True)
        write(path, "old")
        os.chmod(path, 0o644)
        orig = getpass.getpass
        getpass.getpass = lambda *a, **k: "sk-hc-v1-new"
        try:
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(jev_judge.main(["--save-key"]), 0)
        finally:
            getpass.getpass = orig
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(read(path), "sk-hc-v1-new")


class TestJevAuto(Base):
    """Automatic background labelling: only new prompts, labels only, retries after failures."""

    def _transcript(self, n=3):
        sid = "aaaaaaaa-0000-4000-8000-000000000001"
        path = os.path.join(self.tmp, "projects", "-x", sid + ".jsonl")
        os.makedirs(os.path.dirname(path))
        recs = []
        for i in range(n):
            recs.append({"type": "user", "timestamp": "2026-11-20T10:%02d:00Z" % i, "sessionId": sid, "cwd": self.repo,
                         "promptId": "p%d" % i, "promptSource": "typed",
                         "message": {"content": "secret words %d about /Users/alex/x" % i}})
            recs.append({"type": "assistant", "timestamp": "2026-11-20T10:%02d:30Z" % i, "sessionId": sid,
                         "message": {"content": [{"type": "text", "text": "reply %d" % i}]}})
        write(path, "\n".join(json.dumps(r) for r in recs) + "\n")
        return path

    def setUp(self):
        super().setUp()
        import jev_judge
        self.j = jev_judge
        os.environ.pop("JEV_API_KEY", None)
        os.makedirs(self.data, exist_ok=True)
        write(os.path.join(self.data, "jev.key"), "k")
        jev_judge._set_config(jev_since="2026-11-20T10:01:00Z")
        self.sent = []

    def fake(self, key, state):
        self.sent.append(state)
        return {"model": "jev-test", "answers": {"pattern": {"choice": "concept"}, "complexity": {"score": 1}}}

    def test_labels_new_prompts_once_and_keeps_no_text(self):
        path = self._transcript()
        self.assertEqual(self.j.auto([path], ask=self.fake), 2)  # p0 is before jev_since
        self.assertEqual(self.j.auto([path], ask=self.fake), 0)  # already labelled
        out = read(self.j.labels_path())
        self.assertEqual(sorted(json.loads(l)["prompt_id"] for l in out.splitlines()), ["p1", "p2"])
        self.assertNotIn("secret words", out)
        self.assertTrue(all("/Users/alex" not in json.dumps(s) for s in self.sent))  # home folder redacted
        self.assertIn("previous_prompt", self.sent[0])

    def test_failure_stops_and_next_run_retries(self):
        path = self._transcript()
        def broken(key, state):
            raise OSError("offline")
        self.assertEqual(self.j.auto([path], ask=broken), 0)
        self.assertFalse(os.path.exists(self.j.labels_path()) and read(self.j.labels_path()).strip())
        self.assertEqual(self.j.auto([path], ask=self.fake), 2)

    def test_nothing_sent_without_key(self):
        path = self._transcript()
        os.remove(os.path.join(self.data, "jev.key"))
        self.assertEqual(self.j.auto([path], ask=self.fake), 0)
        self.assertEqual(self.sent, [])


class TestPresenceFit(Base):
    """Calibration from presence: the fit should recover the thinking time that matches observed activity."""

    def _data(self, gaps=5, active_s=120):
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        t = datetime.datetime(2026, 11, 20, 10, tzinfo=tz)
        evs, lines = [], []
        def ev(at, kind, **kw):
            d = {"v": 1, "ts": at.isoformat(), "source": "t", "session": "s", "event": kind, "project": "A"}
            d.update(kw)
            evs.append(d)
        def pl(at, app, active):
            lines.append({"ts": at.isoformat(), "_t": at, "app": app, "idle": 0 if active else 300, "active": active})
        for i in range(gaps):
            ev(t, "prompt_sent", chars=0, prompt_id="p%d" % i)
            done = t + datetime.timedelta(seconds=10)
            ev(done, "ai_done", response_text_chars=0, response_code_lines=0, prompt_id="p%d" % i)
            # active in the terminal for active_s after the reply, then away from the keyboard
            for k in range(0, 300, 30):
                pl(done + datetime.timedelta(seconds=k), "terminal", k < active_s)
            t = done + datetime.timedelta(seconds=300)
        ev(t, "prompt_sent", chars=0, prompt_id="last")
        path = os.path.join(self.tmp, "log.jsonl")
        write(path, "\n".join(json.dumps(e) for e in evs) + "\n")
        return path, lines

    def test_fit_recovers_observed_thinking_time(self):
        path, lines = self._data(active_s=120)
        f = aitrack.fit_presence(path, lines)
        self.assertEqual(f["gaps"], 5)
        best = aitrack._best(f["cli"], 60)
        self.assertEqual(best[0], 120)
        self.assertEqual(best[1][1], 1.0)  # every gap in range

    def test_apply_needs_enough_gaps_and_then_is_used(self):
        import contextlib, io, presence
        path, lines = self._data(active_s=90)
        orig = presence.load
        presence.load = lambda *a, **k: lines
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                aitrack.main(["presence", "--fit", "--apply", "--file", path])
            self.assertEqual(timemodel.calibrated("think_s", 60), 60)  # 5 gaps: not applied
            old = aitrack.FIT_MIN_GAPS
            aitrack.FIT_MIN_GAPS = 5
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    aitrack.main(["presence", "--fit", "--apply", "--file", path])
            finally:
                aitrack.FIT_MIN_GAPS = old
        finally:
            presence.load = orig
        self.assertEqual(timemodel.calibrated("think_s", 60), 90)


class TestSyncErrors(unittest.TestCase):
    def test_login_page_gives_a_clear_error(self):
        import sync

        class Fake(object):
            def __init__(self, url, ctype):
                self.url, self.headers = url, {"Content-Type": ctype}

            def geturl(self):
                return self.url
        with self.assertRaises(OSError) as e:
            sync.read_json(Fake("https://orchard.example/auth/protect?x=1", "text/html"), "https://sauron.example/api/ingest")
        self.assertIn("behind a login page", str(e.exception))
        with self.assertRaises(OSError):
            sync.read_json(Fake("https://sauron.example/api/ingest", "text/html"), "https://sauron.example/api/ingest")

    def test_redirects_are_not_followed_so_the_key_stays_put(self):
        import http.server
        import threading
        import sync
        seen = []

        class Elsewhere(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)

        class Redirector(Elsewhere):
            def do_POST(self):
                self.send_response(307)  # 307 keeps the method and body, the worst case
                self.send_header("Location", "http://127.0.0.1:%d/login" % other.server_address[1])
                self.end_headers()
        here = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        for s in (other, here):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(OSError) as e:
                sync.post("http://127.0.0.1:%d" % here.server_address[1], "ait_secret", {"events": []})
            self.assertIn("redirects to 127.0.0.1:%d" % other.server_address[1], str(e.exception))
            self.assertEqual(seen, [])
        finally:
            for s in (other, here):
                s.shutdown()
                s.server_close()


class TestConnectAddress(Base):
    def test_plain_http_only_for_this_computer(self):
        import argparse
        import getpass
        orig = getpass.getpass
        asked = []
        getpass.getpass = lambda prompt: asked.append(prompt) or ""  # empty key: stops right after the check
        try:
            for url in ("http://localhost.evil.example", "http://127.0.0.1.evil.example", "http://localhost@evil.example",
                        "http://evil.example", "ftp://localhost", "https://"):
                self.assertEqual(aitrack.cmd_connect(argparse.Namespace(server=url)), 2, url)
            self.assertEqual(asked, [])
            for url in ("https://sauron.example", "http://localhost:8000", "http://127.0.0.1:8000"):
                self.assertEqual(aitrack.cmd_connect(argparse.Namespace(server=url)), 1, url)
            self.assertEqual(len(asked), 3)
        finally:
            getpass.getpass = orig


class TestServeHostCheck(Base):
    def test_only_local_host_names_are_answered(self):
        import argparse
        import http.client
        import http.server
        import threading
        made = []
        orig = http.server.ThreadingHTTPServer

        class Capture(orig):
            def __init__(self, *a, **kw):
                orig.__init__(self, *a, **kw)
                made.append(self)
        http.server.ThreadingHTTPServer = Capture
        try:
            args = argparse.Namespace(paths=[], file=[], prompt_text=None, idle=15.0, port=0, no_open=True)
            threading.Thread(target=aitrack.cmd_serve, args=(args,), daemon=True).start()
            for _ in range(100):
                if made:
                    break
                time.sleep(0.02)
        finally:
            http.server.ThreadingHTTPServer = orig
        srv = made[0]
        port = srv.server_address[1]

        def status(method, host, path="/api/sources"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", host)
            if method == "POST":
                c.putheader("X-Aitrack", "1")
                c.putheader("Content-Length", "2")
            c.endheaders(b"{}" if method == "POST" else None)
            r = c.getresponse()
            r.read()
            c.close()
            return r.status
        try:
            self.assertEqual(status("GET", "127.0.0.1:%d" % port), 200)
            self.assertEqual(status("GET", "localhost:%d" % port), 200)
            self.assertEqual(status("GET", "rebind.evil.example:%d" % port), 403)
            self.assertEqual(status("GET", "rebind.evil.example:%d" % port, "/"), 403)
            self.assertEqual(status("POST", "rebind.evil.example:%d" % port, "/api/label"), 403)
        finally:
            srv.shutdown()
            srv.server_close()
