"""
Tests for the schedule-aware live poller: _schedule_interval (the adaptive
cadence), _sync_caffeinate (the idle-sleep assertion), _sleep_until (the
wall-clock sleep loop), and the idx_games_state_date index it all leans on.

Unlike tests/test_live_scoring.py, these need a writable fixture DB (that
file deliberately opens the real data/cfb.db read-only), so each test gets
its own fresh, in-memory-ish DB via db.init_db() into a tempdir, mirroring
tests/test_users_db_invariant.py.

Run with: ./venv/bin/python -m unittest discover tests
"""

import pathlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from src import db, live

DB_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "cfb.db"


def _fresh_conn(tmpdir):
    return db.init_db(pathlib.Path(tmpdir) / "schedule_test.db")


def _insert_game(conn, game_id, game_date, status_state, season_year=2026, season_type=2, week=1):
    db.upsert_team(conn, "T1", "AAA", "Team A")
    db.upsert_team(conn, "T2", "BBB", "Team B")
    conn.execute("""
        INSERT INTO games (
            game_id, season_year, season_type, week, game_date,
            home_team_id, home_team_abbr, home_team_name,
            away_team_id, away_team_abbr, away_team_name,
            status_state, completed
        ) VALUES (?, ?, ?, ?, ?, 'T1', 'AAA', 'Team A', 'T2', 'BBB', 'Team B', ?, ?)
    """, (game_id, season_year, season_type, week, game_date, status_state,
          1 if status_state == "post" else 0))
    conn.commit()


def _fmt(dt):
    return dt.strftime(live.GAME_DATE_FMT)


class TestScheduleInterval(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = _fresh_conn(self._tmp.name)
        self.now = datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_live_game_dominates_even_with_a_far_off_next_kickoff(self):
        _insert_game(self.conn, "g1", _fmt(self.now + timedelta(days=3)), "pre")
        _insert_game(self.conn, "g2", _fmt(self.now - timedelta(hours=1)), "in")
        seconds, hold_awake, reason = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))
        self.assertTrue(hold_awake)
        self.assertIn("in progress", reason)

    def test_kickoff_exactly_at_lead_boundary_is_active(self):
        kick = self.now + timedelta(seconds=live.LIVE_KICKOFF_LEAD_SECONDS)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))
        self.assertTrue(hold_awake)

    def test_kickoff_just_past_lead_boundary_floors_to_fast_interval(self):
        kick = self.now + timedelta(seconds=live.LIVE_KICKOFF_LEAD_SECONDS + 1)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, _, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))

    def test_kickoff_25_minutes_out_sleeps_exactly_to_the_lead(self):
        kick = self.now + timedelta(minutes=25)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, timedelta(minutes=25).total_seconds() - live.LIVE_KICKOFF_LEAD_SECONDS)
        self.assertTrue(hold_awake)  # inside the 3h caffeinate lead

    def test_kickoff_two_hours_out_hits_the_idle_cap(self):
        kick = self.now + timedelta(hours=2)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_IDLE_INTERVAL_SECONDS))
        self.assertTrue(hold_awake)  # still inside the 3h caffeinate lead

    def test_hold_awake_decouples_from_the_idle_cap_at_the_caffeinate_lead(self):
        kick = self.now + timedelta(hours=4)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_IDLE_INTERVAL_SECONDS))
        self.assertFalse(hold_awake)  # outside the 3h caffeinate lead, same idle cap

    def test_kickoff_three_hours_past_still_pre_is_active_inside_grace(self):
        kick = self.now - timedelta(hours=3)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, reason = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))
        self.assertTrue(hold_awake)
        self.assertIn("kickoff", reason)

    def test_kickoff_past_grace_window_is_excluded_from_next_kickoff(self):
        kick = self.now - timedelta(hours=7)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, reason = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_IDLE_INTERVAL_SECONDS))
        self.assertFalse(hold_awake)
        self.assertIn("no scheduled kickoff", reason)

    def test_empty_schedule_returns_idle_cap(self):
        seconds, hold_awake, reason = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_IDLE_INTERVAL_SECONDS))
        self.assertFalse(hold_awake)
        self.assertIn("no scheduled kickoff", reason)

    def test_all_post_games_returns_idle_cap(self):
        _insert_game(self.conn, "g1", _fmt(self.now - timedelta(days=1)), "post")
        seconds, hold_awake, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_IDLE_INTERVAL_SECONDS))
        self.assertFalse(hold_awake)

    def test_past_season_row_never_surfaces_as_next_kickoff(self):
        _insert_game(self.conn, "old", _fmt(self.now - timedelta(days=700)), "pre", season_year=2024)
        seconds, hold_awake, reason = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_IDLE_INTERVAL_SECONDS))
        self.assertFalse(hold_awake)
        self.assertIn("no scheduled kickoff", reason)


class TestGameDateFormat(unittest.TestCase):
    """The assumption everything else here rests on: games.game_date is
    always a 17-char UTC string parseable by GAME_DATE_FMT. Guards against
    the day ESPN starts emitting seconds or an explicit offset, which would
    silently break both the poller's cadence and serve.py's
    _default_slate_date."""

    @unittest.skipUnless(DB_PATH.exists(), "real data/cfb.db not present")
    def test_every_game_date_matches_the_expected_format(self):
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = conn.execute("SELECT game_date FROM games").fetchall()
        conn.close()
        self.assertTrue(rows)
        for (value,) in rows:
            self.assertEqual(len(value), 17, value)
            datetime.strptime(value, live.GAME_DATE_FMT)  # raises on mismatch

    def test_tbd_placeholder_time_round_trips(self):
        datetime.strptime("2026-09-13T03:59Z", live.GAME_DATE_FMT)


class TestIndex(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = _fresh_conn(self._tmp.name)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_idx_games_state_date_exists(self):
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_games_state_date'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_schedule_query_uses_a_covering_index(self):
        plan = self.conn.execute("EXPLAIN QUERY PLAN " + live._SCHEDULE_SQL, ("2026-01-01T00:00Z",)).fetchall()
        detail = " ".join(row[3] for row in plan)
        self.assertIn("COVERING INDEX idx_games_state_date", detail)


class TestSyncCaffeinate(unittest.TestCase):
    def test_noop_off_darwin(self):
        with patch.object(live.sys, "platform", "linux"), patch.object(live.subprocess, "Popen") as popen:
            result = live._sync_caffeinate(None, True)
        self.assertIsNone(result)
        popen.assert_not_called()

    def test_starts_exactly_one_process_across_repeated_wants(self):
        with patch.object(live.sys, "platform", "darwin"), \
             patch.object(live.os.path, "exists", return_value=True), \
             patch.object(live.subprocess, "Popen") as popen:
            fake = Mock()
            fake.poll.return_value = None
            popen.return_value = fake
            proc = live._sync_caffeinate(None, True)
            proc = live._sync_caffeinate(proc, True)
        self.assertIs(proc, fake)
        popen.assert_called_once()

    def test_want_false_terminates_the_held_process(self):
        fake = Mock()
        fake.wait.return_value = None
        result = live._sync_caffeinate(fake, False)
        self.assertIsNone(result)
        fake.terminate.assert_called_once()

    def test_want_false_on_none_is_a_noop(self):
        self.assertIsNone(live._sync_caffeinate(None, False))

    def test_dead_process_is_restarted(self):
        dead = Mock()
        dead.poll.return_value = 0  # already exited
        with patch.object(live.sys, "platform", "darwin"), \
             patch.object(live.os.path, "exists", return_value=True), \
             patch.object(live.subprocess, "Popen") as popen:
            fresh = Mock()
            popen.return_value = fresh
            result = live._sync_caffeinate(dead, True)
        self.assertIs(result, fresh)
        popen.assert_called_once()


class TestSleepUntil(unittest.TestCase):
    def test_returns_promptly_after_a_simulated_suspend(self):
        """The regression this guards: the old loop counted sleep()
        iterations, which don't advance across system suspend, so a
        lid-close could silently eat the whole remaining budget. Simulate a
        4-hour jump in wall-clock time between two time.time() calls and
        assert the loop exits almost immediately rather than sleeping out
        the original budget."""
        calls = {"n": 0}
        base = 1_000_000.0

        def fake_time():
            calls["n"] += 1
            # First call establishes "now"; every call after simulates a
            # 4-hour suspend/resume having already happened.
            return base if calls["n"] == 1 else base + 4 * 3600 + 1

        sleeps = []
        with patch.object(live.time, "time", side_effect=fake_time), \
             patch.object(live.time, "sleep", side_effect=sleeps.append):
            wake_at = base + live.LIVE_IDLE_INTERVAL_SECONDS
            live._sleep_until(wake_at, lambda: False, lambda: False)
        # Should have returned after at most one more time.time() check,
        # never actually sleeping out the (now long-past) budget.
        self.assertLessEqual(len(sleeps), 1)

    def test_stops_immediately_when_stop_flag_already_set(self):
        with patch.object(live.time, "sleep") as sleep_mock:
            live._sleep_until(live.time.time() + 100, lambda: True, lambda: False)
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
