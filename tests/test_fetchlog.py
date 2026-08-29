"""
Tests for src/fetchlog.py -- the fetch_log/poller_state writer that
src/espn.py and src/fox.py call into on every outbound HTTP request.

Same fresh-fixture-DB pattern as tests/test_live_schedule.py (db.init_db()
into a tempdir): fetchlog owns its own connection separate from whatever
the caller is using, so each test gets its own scratch DB and points
fetchlog.configure() at it directly.

Run with: ./venv/bin/python -m unittest discover tests
"""

import pathlib
import tempfile
import unittest

from src import db, fetchlog


class TestRedact(unittest.TestCase):
    def test_strips_fox_apikey(self):
        url = "https://api.foxsports.com/bifrost/v1/cfb/event/41258/data?apikey=jE7yBJVRNAwdDesMgTzTXUUSx1It41Fq"
        redacted = fetchlog._redact(url)
        self.assertNotIn("jE7yBJVRNAwdDesMgTzTXUUSx1It41Fq", redacted)
        self.assertIn("apikey=REDACTED", redacted)

    def test_leaves_a_key_less_url_untouched(self):
        url = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event=401752804"
        self.assertEqual(fetchlog._redact(url), url)

    def test_apikey_not_necessarily_first_param_still_redacted(self):
        url = "https://api.foxsports.com/x?foo=bar&apikey=SECRET123&baz=1"
        redacted = fetchlog._redact(url)
        self.assertNotIn("SECRET123", redacted)
        self.assertIn("baz=1", redacted)  # only the key itself is touched


class TestRecordAndContext(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self._tmp.name) / "fetchlog_test.db"
        db.init_db(self.db_path)  # creates fetch_log/poller_state
        fetchlog.configure(caller="test", db_path=str(self.db_path), enabled=True)

    def tearDown(self):
        fetchlog.configure(caller=None, db_path=None, enabled=False)  # reset module state
        self._tmp.cleanup()

    def _rows(self):
        conn = db.get_connection(str(self.db_path))
        rows = conn.execute("SELECT * FROM fetch_log ORDER BY id").fetchall()
        conn.close()
        return rows

    def test_record_writes_a_row(self):
        fetchlog.record("espn", "scoreboard", "https://example.com/x", ok=True, http_status=200, latency_ms=100)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "espn")
        self.assertEqual(rows[0]["endpoint_kind"], "scoreboard")
        self.assertEqual(rows[0]["ok"], 1)
        self.assertEqual(rows[0]["caller"], "test")

    def test_disabled_by_default_before_configure(self):
        fetchlog.configure(caller=None, db_path=None, enabled=False)
        fetchlog.record("espn", "scoreboard", "https://example.com/x", ok=True)
        # No exception, and nothing written (module is a no-op when disabled).
        rows = self._rows()
        self.assertEqual(rows, [])

    def test_context_merges_and_nests(self):
        with fetchlog.context(cycle_seq=7):
            with fetchlog.context(game_id="g1"):
                fetchlog.record("espn", "summary", "https://example.com/x", ok=True)
            # Outer context alone, after the inner one exits.
            fetchlog.record("espn", "scoreboard", "https://example.com/y", ok=True)
        rows = self._rows()
        self.assertEqual(rows[0]["cycle_seq"], 7)
        self.assertEqual(rows[0]["game_id"], "g1")
        self.assertEqual(rows[1]["cycle_seq"], 7)
        self.assertIsNone(rows[1]["game_id"])

    def test_context_restores_on_exception(self):
        with self.assertRaises(ValueError):
            with fetchlog.context(cycle_seq=1):
                with fetchlog.context(cycle_seq=2):
                    raise ValueError("boom")
        # Stack must be back to empty -- a failed cycle can't leak stale
        # context into the next one.
        fetchlog.record("espn", "scoreboard", "https://example.com/z", ok=True)
        rows = self._rows()
        self.assertIsNone(rows[-1]["cycle_seq"])

    def test_explicit_game_id_overrides_context(self):
        with fetchlog.context(game_id="ambient"):
            fetchlog.record("espn", "summary", "https://example.com/x", ok=True, game_id="explicit")
        rows = self._rows()
        self.assertEqual(rows[0]["game_id"], "explicit")

    def test_url_is_redacted_before_storage(self):
        url = "https://api.foxsports.com/bifrost/v1/cfb/event/1/data?apikey=jE7yBJVRNAwdDesMgTzTXUUSx1It41Fq"
        fetchlog.record("fox", "event", url, ok=True)
        rows = self._rows()
        self.assertNotIn("jE7yBJVRNAwdDesMgTzTXUUSx1It41Fq", rows[0]["url"])


class TestNeverRaises(unittest.TestCase):
    def tearDown(self):
        fetchlog.configure(caller=None, db_path=None, enabled=False)

    def test_record_swallows_a_broken_connection_and_self_disables(self):
        # A path whose parent directory doesn't exist -- sqlite3.connect()
        # raises OperationalError the first time record() tries to use it.
        fetchlog.configure(caller="test", db_path="/nonexistent_dir_xyz_cc/fetchlog.db", enabled=True)
        for _ in range(fetchlog._MAX_CONSECUTIVE_FAILURES):
            fetchlog.record("espn", "scoreboard", "https://example.com/x", ok=True)  # must not raise
        self.assertFalse(fetchlog._enabled)
        # Once disabled, further calls are a silent no-op (no more attempts
        # to open the broken connection).
        fetchlog.record("espn", "scoreboard", "https://example.com/x", ok=True)

    def test_record_poller_state_swallows_a_broken_connection(self):
        fetchlog.configure(caller="test", db_path="/nonexistent_dir_xyz_cc/fetchlog.db", enabled=True)
        for _ in range(fetchlog._MAX_CONSECUTIVE_FAILURES):
            fetchlog.record_poller_state("live", pid=1)  # must not raise
        self.assertFalse(fetchlog._enabled)


class TestNoForeignKeyOnGameId(unittest.TestCase):
    """The schema constraint fetch_log.game_id is built to satisfy: a Fox
    id-walk probe or an as-yet-undiscovered ESPN game must never be
    rejected by PRAGMA foreign_keys=ON just because `games` doesn't have
    a matching row yet. Uses db.get_connection() directly (which does set
    foreign_keys=ON), not fetchlog's own connection, so this is a genuine
    schema-level check rather than an artifact of fetchlog's own pragmas."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self._tmp.name) / "fk_test.db"
        db.init_db(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_insert_with_orphan_game_id_succeeds(self):
        conn = db.get_connection(str(self.db_path))
        fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk_on, 1)
        db.insert_fetch_log(conn, {
            "requested_at": "2026-01-01T00:00:00.000Z",
            "source": "espn", "endpoint_kind": "summary", "url": "https://example.com",
            "ok": 1, "game_id": "no-such-game",
        })
        conn.commit()
        row = conn.execute("SELECT game_id FROM fetch_log").fetchone()
        self.assertEqual(row["game_id"], "no-such-game")
        conn.close()


if __name__ == "__main__":
    unittest.main()
