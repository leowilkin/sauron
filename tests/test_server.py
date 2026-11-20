# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server tests. Need the server's dependencies (pip install -r server/requirements.txt); skipped otherwise."""
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tracker"))
sys.path.insert(0, os.path.join(ROOT, "server"))

try:
    from fastapi.testclient import TestClient
except ImportError:  # server deps not installed
    TestClient = None


@unittest.skipIf(TestClient is None, "server dependencies not installed")
class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AI_TRACKER_DIR"] = os.path.join(self.tmp, "data")
        os.environ["WAKATIME_HOME"] = self.tmp
        os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(self.tmp, "t.db")
        for m in ("app", "db"):
            sys.modules.pop(m, None)
        import app
        self.app = app
        self.c = TestClient(app.app)
        db = app.db
        db.execute("INSERT INTO users (hca_id, name, username, public, created_at) VALUES (%s, %s, %s, 1, %s)",
                   ("ident!x", "Ada", "ada", "2026-11-20"))
        self.uid = db.one("SELECT id FROM users WHERE username = %s", ("ada",))["id"]
        self.key = "ait_testkey"
        db.execute("INSERT INTO api_keys (user_id, key_hash, created_at) VALUES (%s, %s, %s)",
                   (self.uid, app.key_hash(self.key), "2026-11-20"))
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.repo, ".git"))

    def tearDown(self):
        for k in ("AI_TRACKER_DIR", "DATABASE_URL", "WAKATIME_HOME"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log(self):
        """A small real log: two prompts, an edit, replies. Returns the events as sync would send them."""
        import eventlog
        import sync
        now = datetime.datetime.now().astimezone()
        def at(s):
            return (now - datetime.timedelta(seconds=600 - s)).isoformat(timespec="seconds")
        for t, ev in [(0, {"event": "session_start"}),
                      (60, {"event": "prompt_sent", "chars": 120, "prompt_id": "p1", "typing_ms": 30000,
                            "keys_typed": 130}),
                      (90, {"event": "ai_edit", "file": "src/a.py", "lines_added": 5, "lines_removed": 1,
                            "prompt_id": "p1"}),
                      (180, {"event": "ai_done", "response_text_chars": 800, "prompt_id": "p1"}),
                      (400, {"event": "prompt_sent", "chars": 60, "prompt_id": "p2"}),
                      (460, {"event": "ai_done", "response_text_chars": 300, "prompt_id": "p2"})]:
            ev.update(session="s1", _ts=at(t))
            self._append_at(ev)
        evs, *_ = sync.read_new_lines(eventlog.log_path(), 0, 0, None)
        return evs

    def _append_at(self, e):
        import eventlog
        ts = e.pop("_ts")
        orig = eventlog.now_iso
        eventlog.now_iso = lambda: ts
        try:
            eventlog.append_event(e, cwd=self.repo, source="test")
        finally:
            eventlog.now_iso = orig

    def ingest(self, body, key=None):
        return self.c.post("/api/ingest", json=body, headers={"Authorization": "Bearer " + (key or self.key)})

    def test_ingest_drops_everything_outside_the_allowlist(self):
        evs = self._log()
        evs[1]["text"] = "my secret prompt"
        evs[1]["cwd"] = "/Users/ada/secret"
        r = self.ingest({"events": evs, "labels": [{"prompt_id": "p1", "pattern": "concept", "text": "secret"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["stored"], 6)
        self.assertEqual(r.json()["breaks"], 0)
        dump = json.dumps(self.app.db.query("SELECT * FROM events")) + json.dumps(self.app.db.query("SELECT * FROM labels"))
        for s in ("secret", "/Users/ada"):
            self.assertNotIn(s, dump)

    def test_resending_is_idempotent_and_gaps_are_flagged(self):
        evs = self._log()
        self.ingest({"events": evs[:3]})
        self.assertEqual(self.ingest({"events": evs[:3]}).json()["stored"], 0)
        r = self.ingest({"events": evs[4:]})  # line 4 missing: chain doesn't link
        self.assertEqual(r.json()["breaks"], 1)
        heads = self.app.db.query("SELECT n, linked FROM heads ORDER BY id")
        self.assertEqual([(h["n"], h["linked"]) for h in heads], [(3, 1), (6, 0)])

    def test_bad_or_revoked_key_is_refused(self):
        self.assertEqual(self.ingest({"events": []}, key="nope").status_code, 401)
        self.app.db.execute("UPDATE api_keys SET revoked_at = %s", ("2026-11-20",))
        self.assertEqual(self.ingest({"events": []}).status_code, 401)

    def test_profile_stats_and_privacy(self):
        evs = self._log()
        self.ingest({"events": evs, "labels": [{"prompt_id": "p2", "pattern": "concept", "own_thinking": 2,
                                                 "complexity": 1, "read_previous": 1}],
                     "projects": {"proj": "https://github.com/ada/proj"}})
        s = self.c.get("/api/u/ada").json()
        self.assertGreater(s["all"]["observed_s"], 0)
        self.assertLessEqual(s["all"]["observed_s"], 600)  # never more than the real time that passed
        self.assertGreater(s["all"]["effort_s"], 0)
        self.assertEqual(s["projects"][0]["repo_url"], "https://github.com/ada/proj")
        self.assertTrue(s["live"])
        self.assertEqual(self.c.get("/u/ada").status_code, 200)
        self.app.db.execute("UPDATE users SET public = 0")
        self.assertEqual(self.c.get("/api/u/ada").status_code, 404)
        self.assertEqual(self.c.get("/u/ada").status_code, 404)

    def test_client_sync_end_to_end(self):
        import sync
        import jev_judge
        self._log()
        jev_judge._set_config(server="http://testserver")
        with open(os.path.join(os.environ["AI_TRACKER_DIR"], "server.key"), "w") as f:
            f.write(self.key)
        send = lambda body: self.ingest(body).json()
        self.assertEqual(sync.sync(send=send), 6)
        self.assertEqual(sync.sync(send=send), 0)  # nothing new
        import eventlog
        eventlog.append_event({"event": "prompt_sent", "session": "s1", "chars": 5}, cwd=self.repo, source="t")
        self.assertEqual(sync.sync(send=send), 1)
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM heads WHERE linked = 0")["n"], 0)

    def test_settings_needs_sign_in_and_csrf(self):
        r = self.c.get("/settings", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.post("/settings/keys", data={"csrf_token": "x"}).status_code, 401)


class TestEffortMatchesDashboard(unittest.TestCase):
    def test_overlap_rule_counts_each_moment_once(self):
        import effort
        # same scenario as the dashboard check: union of activity is 14 minutes
        rows = [{"at": "2026-11-19T10:00:00+00:00", "day": "2026-11-19", "you_before_s": 120, "ai_s": 600},
                {"at": "2026-11-19T10:10:00+00:00", "day": "2026-11-19", "you_before_s": 480, "ai_s": 120}]
        t = effort.totals(rows)[("2026-11-19", "?")]
        self.assertAlmostEqual(t["you_s"] + t["ai_s"], 840)
        self.assertAlmostEqual(t["you_s"], 600)


if __name__ == "__main__":
    unittest.main()
