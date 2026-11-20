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

    def test_paths_outside_the_project_are_never_stored(self):
        evs = self._log()
        evs[2]["file"] = "../../../private/tmp/-Users-ada-secret/x.py"
        self.ingest({"events": evs})
        dump = json.dumps(self.app.db.query("SELECT data FROM events"))
        self.assertNotIn("Users-ada", dump)
        self.assertIn("(outside project)", dump)
        import sync
        for p, ok in (("src/a.py", True), ("../x", False), ("/etc/x", False), ("C:\\x", False), ("a/../../b", False)):
            self.assertEqual(sync.inside_project(p), ok, p)

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


    # ---- labelling with the shared key
    def test_label_uses_the_shared_key_and_keeps_only_the_label(self):
        import jev_judge
        seen = []
        def fake(key, state, retries=4):
            seen.append((key, state))
            return {"model": "jev-test", "answers": {"pattern": {"choice": "concept"}, "complexity": {"score": 2}}}
        orig = jev_judge.ask_jev
        jev_judge.ask_jev = fake
        try:
            body = {"prompt_id": "p9", "state": {"prompt": "how does this work? key=sk-hc-v1-abcdefghijklmnopqrstuv"}}
            h = {"Authorization": "Bearer " + self.key}
            self.assertEqual(self.c.post("/api/label", json=body, headers=h).status_code, 503)  # no key on server
            os.environ["JEV_API_KEY"] = "shared"
            r = self.c.post("/api/label", json=body, headers=h)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["pattern"], "concept")
            self.assertEqual(seen[0][0], "shared")
            self.assertNotIn("sk-hc-v1-abcdef", json.dumps(seen[0][1]))  # redacted before sending
            self.assertEqual(self.c.post("/api/label", json=body, headers=h).status_code, 200)
            self.assertEqual(len(seen), 1)  # second ask for the same prompt is free
            dump = json.dumps(self.app.db.query("SELECT * FROM labels")) + json.dumps(self.app.db.query("SELECT * FROM events"))
            self.assertNotIn("how does this work", dump)
            os.environ["LABEL_DAILY_LIMIT"] = "1"
            r = self.c.post("/api/label", json={"prompt_id": "p10", "state": {"prompt": "x"}}, headers=h)
            self.assertEqual(r.status_code, 429)
            self.assertEqual(self.c.post("/api/label", json=body, headers={"Authorization": "Bearer nope"}).status_code, 401)
        finally:
            jev_judge.ask_jev = orig
            os.environ.pop("JEV_API_KEY", None)
            os.environ.pop("LABEL_DAILY_LIMIT", None)

    def test_client_labels_through_the_server_without_its_own_key(self):
        import jev_judge
        os.makedirs(os.environ["AI_TRACKER_DIR"], exist_ok=True)
        jev_judge._set_config(server="http://testserver", jev_since="2026-01-01T00:00:00Z")
        with open(os.path.join(os.environ["AI_TRACKER_DIR"], "server.key"), "w") as f:
            f.write(self.key)
        sent = []
        def via_server(meta, state):
            sent.append(state)
            return {"model": "jev-test", "pattern": "direct", "complexity": 1}
        path = os.path.join(self.tmp, "t.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-11-20T10:00:00Z", "sessionId": "s", "promptId": "q1",
                                "promptSource": "typed", "message": {"content": "use a heap here"}}) + "\n")
        self.assertIsNotNone(jev_judge.server_labeller())
        self.assertEqual(jev_judge.auto([path], labeller=via_server), 1)
        out = open(jev_judge.labels_path()).read()
        self.assertIn('"pattern": "direct"', out)
        self.assertNotIn("use a heap", out)

    # ---- admins
    def _as(self, username):
        """Act as this user (or nobody) for the next requests."""
        app = self.app
        u = app.db.one("SELECT * FROM users WHERE username = %s", (username,)) if username else None
        app.current_user = lambda request: app.db.one("SELECT * FROM users WHERE id = %s", (u["id"],)) if u else None
        return u

    def _csrf(self):
        # settings issues a CSRF token into the session; read it back from the form
        import re
        html = self.c.get("/settings").text
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def _second_user(self, public=0):
        self.app.db.execute("INSERT INTO users (hca_id, name, username, public, created_at) VALUES (%s, %s, %s, %s, %s)",
                            ("ident!y", "Bo", "bo", public, "2026-11-20"))

    def test_admin_pages_are_hidden_from_non_admins(self):
        self._as("ada")
        self.assertEqual(self.c.get("/admin").status_code, 404)
        self.assertEqual(self.c.get("/admin/u/ada").status_code, 404)
        self._as(None)
        self.assertEqual(self.c.get("/admin").status_code, 401)

    def test_env_admin_sees_private_profiles_and_views_are_logged(self):
        self._second_user(public=0)
        self._as("ada")
        self.assertEqual(self.c.get("/api/u/bo").status_code, 404)  # private, not admin yet
        os.environ["ADMINS"] = "ident!x"  # Ada's Hack Club Auth id
        try:
            self.assertEqual(self.c.get("/admin").status_code, 200)
            self.assertEqual(self.c.get("/api/u/bo").status_code, 200)
            self.assertIn("because you're an admin", self.c.get("/u/bo").text)
            self.assertEqual(self.c.get("/admin/u/bo").status_code, 200)
            log = self.app.db.query("SELECT action FROM admin_log")
            self.assertEqual([r["action"] for r in log], ["view"])
        finally:
            os.environ.pop("ADMINS", None)

    def test_admins_setting_never_matches_a_username(self):
        # usernames are self-chosen: taking the one ADMINS names must not make you an admin
        self._as("ada")
        os.environ["ADMINS"] = "ada"
        try:
            self.assertEqual(self.c.get("/admin").status_code, 404)
        finally:
            os.environ.pop("ADMINS", None)

    def test_malformed_labels_are_cleaned_and_cant_break_the_leaderboard(self):
        evs = self._log()
        bad = [{"prompt_id": "p1", "pattern": "concept", "pattern_probs": "x", "own_thinking": "lots",
                "complexity": 1e9, "read_previous": {"a": 1}, "model": "m" * 5000, "junk": "y"},
               {"prompt_id": "p2", "pattern": "<script>", "pattern_probs": {"concept": 2, "evil": 1, "direct": "z"}}]
        self.assertEqual(self.ingest({"events": evs, "labels": bad}).status_code, 200)
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertEqual(self.c.get("/api/u/ada").status_code, 200)
        got = {r["prompt_id"]: json.loads(r["data"]) for r in self.app.db.query("SELECT prompt_id, data FROM labels")}
        self.assertEqual(got["p1"], {"pattern": "concept", "complexity": self.app.MAX_SCORE, "model": "m" * 100})
        self.assertEqual(got["p2"], {"pattern_probs": {"concept": 1}})
        # a bad label stored before these checks existed doesn't break stats either
        self.app.db.execute("UPDATE labels SET data = %s WHERE prompt_id = %s", ('{"pattern_probs": "x", "pattern": "concept"}', "p1"))
        self.app._cache.clear()
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_grant_and_revoke_admin(self):
        self._second_user()
        self.app.db.execute("UPDATE users SET is_admin = 1 WHERE username = %s", ("ada",))
        self._as("ada")
        t = self._csrf()
        self.assertEqual(self.c.post("/admin/u/bo/role", data={"csrf_token": "wrong", "admin": "1"}).status_code, 403)
        self.c.post("/admin/u/bo/role", data={"csrf_token": t, "admin": "1"}, follow_redirects=False)
        self.assertEqual(self.app.db.one("SELECT is_admin FROM users WHERE username = %s", ("bo",))["is_admin"], 1)
        self.c.post("/admin/u/bo/role", data={"csrf_token": t, "admin": "0"}, follow_redirects=False)
        self.assertEqual(self.app.db.one("SELECT is_admin FROM users WHERE username = %s", ("bo",))["is_admin"], 0)
        self.assertEqual(self.c.post("/admin/u/ada/role", data={"csrf_token": t, "admin": "0"}).status_code, 400)
        self.assertEqual([r["action"] for r in self.app.db.query("SELECT action FROM admin_log ORDER BY id")],
                         ["grant", "revoke"])


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
