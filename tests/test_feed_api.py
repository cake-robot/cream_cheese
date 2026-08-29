"""
API-level tests for the Feed page's routes (/api/feed, /api/feed/log,
/api/feed/activity) -- src/fetchlog.py's fetch_log/poller_state tables read
back through serve.py.

Deliberately does NOT use the real data/cfb.db (unlike
tests/test_spoilers_api.py): the point of these tests is the shape and
gating of a brand-new set of routes, not the pipeline's actual game data,
so a small scratch DB built with db.init_db() (same pattern as
tests/test_live_schedule.py) is faster and self-contained. serve.DB_FILE
is monkeypatched to point at it -- get_db() re-reads the module global on
every call, so reassigning it here is enough, no server restart needed.

Run with: ./venv/bin/python -m unittest discover tests
"""

import pathlib
import shutil
import tempfile
import unittest
from pathlib import Path

from src import db, users

import serve

FORBIDDEN_KEYS = {"score", "watchability_score", "live_score", "home_score", "away_score",
                   "quality_so_far", "drama_from_here"}


def _walk_for_forbidden_keys(node, path, failures):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in FORBIDDEN_KEYS:
                failures.append(f"{path}.{k}")
            _walk_for_forbidden_keys(v, f"{path}.{k}", failures)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_for_forbidden_keys(v, f"{path}[{i}]", failures)


def _insert_game(conn, game_id, game_date, status_state):
    db.upsert_team(conn, "T1", "AAA", "Team A")
    db.upsert_team(conn, "T2", "BBB", "Team B")
    conn.execute("""
        INSERT INTO games (
            game_id, season_year, season_type, week, game_date,
            home_team_id, home_team_abbr, home_team_name,
            away_team_id, away_team_abbr, away_team_name,
            status_state, completed
        ) VALUES (?, 2026, 2, 1, ?, 'T1', 'AAA', 'Team A', 'T2', 'BBB', 'Team B', ?, ?)
    """, (game_id, game_date, status_state, 1 if status_state == "post" else 0))
    conn.commit()


class FeedApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

        # Scratch cfb.db, pointed at via serve.DB_FILE.
        self._orig_db_file = serve.DB_FILE
        cfb_path = Path(self._tmpdir) / "cfb.db"
        conn = db.init_db(cfb_path)
        _insert_game(conn, "g1", "2026-09-05T18:00Z", "in")
        _insert_game(conn, "g2", "2026-09-12T18:00Z", "pre")
        db.upsert_poller_state(
            conn, "live", pid=123, mode="normal",
            started_at="2026-09-05T17:00:00.000Z", stopped_at=None,
            cycle_seq=5, last_cycle_at="2026-09-05T18:05:00.000Z",
            last_cycle_ms=150, last_cycle_reqs=3, last_cycle_error=None,
            slate_in=1, slate_post=0, slate_pre=1,
            next_wake_at="2026-09-05T18:06:00.000Z", interval_seconds=60.0,
            interval_reason="1 game(s) in progress", hold_awake=1,
        )
        db.insert_fetch_log(conn, {
            "requested_at": "2026-09-05T18:05:00.000Z", "source": "espn",
            "endpoint_kind": "scoreboard", "url": "https://example.com/scoreboard",
            "caller": "live", "cycle_seq": 5, "ok": 1, "http_status": 200, "latency_ms": 120,
        })
        db.insert_fetch_log(conn, {
            "requested_at": "2026-09-05T18:05:01.000Z", "source": "espn",
            "endpoint_kind": "summary", "url": "https://example.com/summary",
            "caller": "live", "cycle_seq": 5, "game_id": "g1", "ok": 0,
            "http_status": 500, "error": "boom",
        })
        conn.commit()
        conn.close()
        serve.DB_FILE = cfb_path

        # Scratch users.db with one admin and one non-admin account.
        self._orig_users_db_path = users.DB_PATH
        users.DB_PATH = Path(self._tmpdir) / "users.db"
        uconn = users.init_db(users.DB_PATH)
        users.create_user(uconn, "admin", "adminpassword1", is_admin=True, invite_code=None)
        users.create_user(uconn, "regular", "regularpassword1", is_admin=False, invite_code=None)
        uconn.close()

        serve.app.testing = True
        self.client = serve.app.test_client()
        serve.limiter.reset()

    def tearDown(self):
        serve.DB_FILE = self._orig_db_file
        users.DB_PATH = self._orig_users_db_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _login(self, username, password):
        resp = self.client.post(
            "/api/login", json={"username": username, "password": password},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def _login_admin(self):
        self._login("admin", "adminpassword1")

    def _login_regular(self):
        self._login("regular", "regularpassword1")


class TestAdminGate(FeedApiTestCase):
    def test_non_admin_gets_403_on_all_three_routes(self):
        self._login_regular()
        for path in ("/api/feed", "/api/feed/log", "/api/feed/activity"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 403, path)

    def test_admin_gets_200_on_all_three_routes(self):
        self._login_admin()
        for path in ("/api/feed", "/api/feed/log", "/api/feed/activity"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, (path, resp.get_data(as_text=True)))

    def test_unauthenticated_request_is_401_not_403(self):
        # The login wall (_require_auth) runs before the route body, so a
        # logged-out request never reaches _require_admin at all.
        resp = self.client.get("/api/feed")
        self.assertEqual(resp.status_code, 401)


class TestFeedPayload(FeedApiTestCase):
    def setUp(self):
        super().setUp()
        self._login_admin()

    def test_poller_state_round_trips(self):
        data = self.client.get("/api/feed").get_json()
        self.assertIsNotNone(data["poller"])
        self.assertEqual(data["poller"]["cycle_seq"], 5)
        self.assertEqual(data["poller"]["interval_reason"], "1 game(s) in progress")
        self.assertTrue(data["running"])

    def test_live_and_upcoming_games_present(self):
        data = self.client.get("/api/feed").get_json()
        self.assertEqual([g["game_id"] for g in data["live_games"]], ["g1"])
        self.assertEqual([g["game_id"] for g in data["upcoming"]], ["g2"])

    def test_source_rollup_counts_ok_and_error(self):
        data = self.client.get("/api/feed").get_json()
        espn = next(s for s in data["sources_24h"] if s["source"] == "espn")
        self.assertEqual(espn["n"], 2)
        self.assertEqual(espn["n_error"], 1)

    def test_log_rows_and_filters(self):
        data = self.client.get("/api/feed/log").get_json()
        self.assertEqual(len(data["rows"]), 2)

        errors_only = self.client.get("/api/feed/log", query_string={"ok": "0"}).get_json()
        self.assertEqual(len(errors_only["rows"]), 1)
        self.assertEqual(errors_only["rows"][0]["error"], "boom")

        by_game = self.client.get("/api/feed/log", query_string={"game_id": "g1"}).get_json()
        self.assertEqual(len(by_game["rows"]), 1)

    def test_activity_buckets_present(self):
        data = self.client.get("/api/feed/activity", query_string={"hours": 24}).get_json()
        total_ok = sum(b["ok"] for b in data["buckets"])
        total_error = sum(b["error"] for b in data["buckets"])
        self.assertEqual(total_ok, 1)
        self.assertEqual(total_error, 1)

    def test_no_forbidden_score_keys_anywhere_in_any_payload(self):
        for path in ("/api/feed", "/api/feed/log", "/api/feed/activity"):
            data = self.client.get(path).get_json()
            failures = []
            _walk_for_forbidden_keys(data, path, failures)
            self.assertEqual(failures, [], f"forbidden keys leaked: {failures}")


if __name__ == "__main__":
    unittest.main()
