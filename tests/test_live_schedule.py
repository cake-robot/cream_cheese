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

from src import db, fetchlog, live

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
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))
        self.assertTrue(hold_awake)
        self.assertIn("in progress", reason)

    def test_kickoff_exactly_at_lead_boundary_is_active(self):
        kick = self.now + timedelta(seconds=live.LIVE_KICKOFF_LEAD_SECONDS)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, _, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))
        self.assertTrue(hold_awake)

    def test_kickoff_just_past_lead_boundary_floors_to_fast_interval(self):
        kick = self.now + timedelta(seconds=live.LIVE_KICKOFF_LEAD_SECONDS + 1)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, _, _, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))

    def test_kickoff_25_minutes_out_sleeps_exactly_to_the_lead(self):
        # self.now is 2026-09-05 18:00 UTC == 14:00 ET, a Saturday: this
        # week's Tuesday anchor and today's day-of anchor (both 08:00 ET)
        # are already behind `now`, so kickoff lead is the only future
        # candidate and this collapses to the pre-rewrite behavior exactly.
        kick = self.now + timedelta(minutes=25)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, _, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, timedelta(minutes=25).total_seconds() - live.LIVE_KICKOFF_LEAD_SECONDS)
        self.assertTrue(hold_awake)  # inside the 3h caffeinate lead

    def test_kickoff_two_hours_out_sleeps_to_the_kickoff_lead_not_a_flat_cap(self):
        # Same "today, anchors already past" setup as above -- the whole
        # point of the rewrite is that a known-but-distant kickoff is no
        # longer clipped to a flat idle ceiling; it sleeps almost the full
        # gap.
        kick = self.now + timedelta(hours=2)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, reason, needs_poll = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, timedelta(hours=2).total_seconds() - live.LIVE_KICKOFF_LEAD_SECONDS)
        self.assertTrue(hold_awake)  # still inside the 3h caffeinate lead
        self.assertIn("kickoff lead", reason)
        self.assertTrue(needs_poll)  # unlike caffeinate lead, this wake exists to catch pre->in

    def test_caffeinate_lead_wins_over_kickoff_lead_when_it_is_the_closer_boundary(self):
        # Kickoff far enough out that T-3h (caffeinate lead) lands before
        # T-1min (kickoff lead) -- assert the *earlier* boundary is chosen,
        # and that hold_awake is still False now but flips True once a
        # second call lands exactly at that boundary.
        kick = self.now + timedelta(hours=5)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        expected = timedelta(hours=5).total_seconds() - live.LIVE_CAFFEINATE_LEAD_SECONDS
        self.assertEqual(seconds, expected)
        self.assertFalse(hold_awake)  # not yet inside the 3h caffeinate lead
        self.assertIn("caffeinate lead", reason)

        _, hold_awake_at_boundary, _, _ = live._schedule_interval(
            self.conn, now=self.now + timedelta(seconds=seconds))
        self.assertTrue(hold_awake_at_boundary)

    def test_caffeinate_lead_wake_does_not_need_a_poll(self):
        # The caffeinate-lead boundary exists purely to arm the idle-sleep
        # assertion ahead of a long sleep -- nothing about a game hours
        # from kickoff can change between polls, so this is the one wake
        # reason that shouldn't cost a real ESPN request. Confirmed as the
        # root cause of two valueless production `scoreboard` calls,
        # 2026-09-04.
        kick = self.now + timedelta(hours=5)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        _, _, reason, needs_poll = live._schedule_interval(self.conn, now=self.now)
        self.assertIn("caffeinate lead", reason)
        self.assertFalse(needs_poll)

    def test_caffeinate_lead_wake_is_not_floored_to_the_fast_interval(self):
        # Unlike a poll-needing wake (floored to LIVE_INTERVAL_SECONDS so a
        # boundary 61s out can't produce a 1-second sleep and a wasted
        # request), a caffeinate-lead wake makes no request either way, so
        # flooring it would just add a pointless extra wake before the real
        # (kickoff lead) target. Kickoff at 3h5m out puts caffeinate lead
        # (kickoff - 3h) 5 minutes away -- well under the 900s/15min floor.
        # (game_date is minute-precision -- GAME_DATE_FMT has no seconds
        # field -- so this stays a whole number of minutes to round-trip
        # exactly through storage.)
        kick = self.now + timedelta(hours=3, minutes=5)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, _, reason, needs_poll = live._schedule_interval(self.conn, now=self.now)
        self.assertIn("caffeinate lead", reason)
        self.assertFalse(needs_poll)
        self.assertAlmostEqual(seconds, timedelta(minutes=5).total_seconds(), delta=1.0)

    def test_live_game_and_near_kickoff_wakes_need_a_poll(self):
        _insert_game(self.conn, "g1", _fmt(self.now - timedelta(hours=1)), "in")
        _, _, _, needs_poll = live._schedule_interval(self.conn, now=self.now)
        self.assertTrue(needs_poll)

        conn2 = _fresh_conn(tempfile.mkdtemp())
        kick = self.now + timedelta(seconds=live.LIVE_KICKOFF_LEAD_SECONDS)
        _insert_game(conn2, "g1", _fmt(kick), "pre")
        _, _, _, needs_poll = live._schedule_interval(conn2, now=self.now)
        self.assertTrue(needs_poll)

    def test_kickoff_three_hours_past_still_pre_is_active_inside_grace(self):
        kick = self.now - timedelta(hours=3)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))
        self.assertTrue(hold_awake)
        self.assertIn("kickoff", reason)

    def test_kickoff_past_grace_window_still_anchors_today_via_the_ungated_query(self):
        # This is the TBD-placeholder rescue case: a 'pre' row 7h in the
        # past falls out of next_kickoff's 6h grace floor, but is still
        # *today*, so _NEXT_ANCHOR_SQL (floored at start-of-today, not
        # now-grace) still finds it and derives day-of/week-anchor from it
        # rather than silently jumping to whatever's discovered next.
        kick = self.now - timedelta(hours=7)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        # self.now (14:00 ET) is itself past both this week's Tuesday
        # anchor and today's 08:00 ET day-of anchor, so every candidate is
        # already behind us -- the "keep checking through today" fallback.
        self.assertEqual(seconds, float(live.LIVE_INTERVAL_SECONDS))
        self.assertTrue(hold_awake)
        self.assertIn("refresh window passed", reason)

    def test_empty_schedule_with_no_history_returns_the_no_schedule_backstop(self):
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_NO_SCHEDULE_BACKSTOP_SECONDS))
        self.assertFalse(hold_awake)
        self.assertIn("no scheduled kickoff", reason)

    def test_recent_regular_season_game_with_no_future_row_triggers_the_blind_backstop(self):
        # The conference-championship -> first-bowl gap: nothing 'pre' on
        # record, but the most recent known game is season_type=2 and
        # recent -- treated as "postseason not yet discovered" rather than
        # a genuinely empty schedule, so it polls daily instead of sleeping
        # a year.
        _insert_game(self.conn, "g1", _fmt(self.now - timedelta(days=3)), "post", season_type=2)
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_BLIND_BACKSTOP_SECONDS))
        self.assertFalse(hold_awake)
        self.assertIn("postseason not yet discovered", reason)

    def test_recent_postseason_game_does_not_trigger_the_blind_backstop(self):
        # After the CFP final itself, the most recent game is
        # season_type=3 -- the blind backstop must not arm here, since
        # there's no reason to expect a same-season game still coming.
        _insert_game(self.conn, "g1", _fmt(self.now - timedelta(days=2)), "post", season_type=3)
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_NO_SCHEDULE_BACKSTOP_SECONDS))
        self.assertFalse(hold_awake)
        self.assertIn("no scheduled kickoff", reason)

    def test_old_regular_season_game_outside_the_blind_window_does_not_trigger_it(self):
        _insert_game(self.conn, "g1", _fmt(self.now - timedelta(days=30)), "post", season_type=2)
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_NO_SCHEDULE_BACKSTOP_SECONDS))
        self.assertFalse(hold_awake)
        self.assertIn("no scheduled kickoff", reason)

    def test_past_season_row_never_surfaces_as_next_kickoff(self):
        _insert_game(self.conn, "old", _fmt(self.now - timedelta(days=700)), "pre", season_year=2024)
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertEqual(seconds, float(live.LIVE_NO_SCHEDULE_BACKSTOP_SECONDS))
        self.assertFalse(hold_awake)
        self.assertIn("no scheduled kickoff", reason)

    def test_week_anchor_wins_when_next_game_is_far_out_mid_week(self):
        # A Wednesday with the next game 10 days out (the following
        # Saturday): must sleep to *that game's own* week-Tuesday, not to
        # a rolling N-day timer and not to tomorrow.
        now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)  # Wednesday
        kick = datetime(2026, 9, 19, 19, 0, tzinfo=timezone.utc)  # Saturday, 10 days out
        _insert_game(self.conn, "g1", _fmt(kick), "pre")
        seconds, hold_awake, reason, needs_poll = live._schedule_interval(self.conn, now=now)
        expected_wake = live._et_anchor(datetime(2026, 9, 15))  # that Saturday's own Tuesday
        self.assertEqual(seconds, (expected_wake - now).total_seconds())
        self.assertFalse(hold_awake)  # nowhere near kickoff or caffeinate lead yet
        self.assertIn("week anchor", reason)
        self.assertTrue(needs_poll)  # exists to catch a newly-added/rescheduled game

    def test_all_tbd_saturday_wakes_at_day_of_refresh_once_grace_ages_out(self):
        # Every game this Saturday shares an ET-midnight TBD placeholder
        # timestamp. While the placeholder is still within
        # LIVE_KICKOFF_GRACE_SECONDS (6h) of `now`, caffeinate-lead (3h
        # before it) is *closer* than day-of (8am the same day) and wins
        # instead -- day-of only becomes reachable once next_kickoff's
        # grace floor has excluded the placeholder (so kickoff-lead/
        # caffeinate-lead aren't computed at all) but 08:00 ET hasn't
        # arrived yet. That's a real window: grace expires at 06:00 ET,
        # day-of fires at 08:00 ET. Pick `now` inside it (07:00 ET).
        placeholder = live._et_anchor(datetime(2026, 9, 12), hour=0)  # 00:00 ET Saturday
        _insert_game(self.conn, "g1", _fmt(placeholder), "pre")
        now = live._et_anchor(datetime(2026, 9, 12), hour=7)  # 07:00 ET, same Saturday
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=now)
        expected_wake = live._et_anchor(datetime(2026, 9, 12))  # 08:00 ET, same Saturday
        self.assertEqual(seconds, (expected_wake - now).total_seconds())
        self.assertFalse(hold_awake)  # no credible next_kickoff to hold awake for
        self.assertIn("day-of refresh", reason)

    def test_all_tbd_saturday_does_not_silently_jump_to_next_weeks_placeholder(self):
        # The failure mode this design exists to prevent: with two
        # all-TBD Saturdays back to back, once this week's placeholder
        # ages past grace, a design driven only by the grace-gated
        # next_kickoff would jump straight to *next* week's placeholder
        # and sleep through the whole intervening day. The ungated anchor
        # query must keep resolving to *this* Saturday until it's done.
        this_week = live._et_anchor(datetime(2026, 9, 12), hour=0)
        next_week = live._et_anchor(datetime(2026, 9, 19), hour=0)
        _insert_game(self.conn, "g1", _fmt(this_week), "pre")
        _insert_game(self.conn, "g2", _fmt(next_week), "pre")
        now = live._et_anchor(datetime(2026, 9, 12), hour=7)  # 07:00 ET, past this week's grace
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=now)
        expected_wake = live._et_anchor(datetime(2026, 9, 12))  # still today, not next Saturday
        self.assertEqual(seconds, (expected_wake - now).total_seconds())
        self.assertIn("2026-09-12", reason)

    def test_postseason_semifinal_to_championship_gap_needs_no_blind_backstop(self):
        # One postseason discovery pull returns the whole bracket at once
        # (verified live against ESPN), so the ~10-day semifinal ->
        # championship gap is walked on ordinary boundaries -- the blind
        # backstop must never arm here.
        _insert_game(self.conn, "sf", _fmt(self.now - timedelta(days=1)), "post", season_type=3)
        champ = self.now + timedelta(days=9)
        _insert_game(self.conn, "champ", _fmt(champ), "pre", season_type=3)
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=self.now)
        self.assertNotIn("no scheduled kickoff", reason)
        self.assertNotIn("postseason not yet discovered", reason)
        self.assertGreater(seconds, 0)

    def test_offseason_with_opener_already_discovered_sleeps_straight_to_its_week(self):
        # The explicit requirement this rewrite exists to satisfy: zero
        # pings between the CFP final and the opener's game week, when the
        # next season is already discovered (this project's normal
        # pattern).
        now = datetime(2026, 1, 21, 0, 0, tzinfo=timezone.utc)  # day after the 2026-01-20 CFP final
        opener = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)  # ~7 months out
        _insert_game(self.conn, "opener", _fmt(opener), "pre", season_year=2026, season_type=2, week=1)
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=now)
        expected_wake = live._et_anchor(datetime(2026, 8, 25))  # opener week's Tuesday
        self.assertEqual(seconds, (expected_wake - now).total_seconds())
        self.assertFalse(hold_awake)
        self.assertIn("week anchor", reason)

    def test_offseason_with_nothing_discovered_is_unbounded_not_a_daily_ping(self):
        # No games at all on record, well clear of any conf-champ/bowl
        # window -- must fall to the (effectively unbounded) no-schedule
        # backstop, not a recurring daily/weekly check.
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        seconds, hold_awake, reason, _ = live._schedule_interval(self.conn, now=now)
        self.assertEqual(seconds, float(live.LIVE_NO_SCHEDULE_BACKSTOP_SECONDS))
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


class TestPollerStatePersistence(unittest.TestCase):
    """run_forever's src/fetchlog.py wiring: after a cycle, poller_state
    should reflect exactly the (period, reason) _schedule_interval itself
    would compute for the same game state -- persisting a value that
    already exists, not re-deriving a second one that could drift from it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self._tmp.name) / "poller_state_test.db"
        self.conn = db.init_db(self.db_path)
        fetchlog.configure(caller="live", db_path=str(self.db_path), enabled=True)
        self._orig_lock_path = live.LOCK_PATH
        live.LOCK_PATH = str(pathlib.Path(self._tmp.name) / "live.lock")

    def tearDown(self):
        live.LOCK_PATH = self._orig_lock_path
        fetchlog.configure(caller=None, db_path=None, enabled=False)
        self.conn.close()
        self._tmp.cleanup()

    def test_run_forever_persists_schedule_consistent_with_schedule_interval(self):
        now = datetime.now(timezone.utc)
        kick = now + timedelta(minutes=25)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")

        class _StopLoop(Exception):
            """Raised from the mocked _sleep_until to escape run_forever's
            `while True` after exactly one iteration -- once=True would
            exit before the schedule-write this test is checking (see
            run_forever: that write only happens on the non-break path)."""

        expected_period, _, expected_reason, _ = live._schedule_interval(self.conn)

        # No real network calls -- Tier 1 would otherwise hit the live
        # ESPN API. Empty slate keeps run_cycle's own logic (upserts,
        # completion detection, Tier 2) a no-op so this test is purely
        # about the schedule-write plumbing around it.
        with patch.object(live.espn, "fetch_scoreboard_dates", return_value=[]), \
             patch.object(live, "_sleep_until", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                live.run_forever(self.conn, once=False, mode="normal", dates="20260101")

        row = self.conn.execute("SELECT * FROM poller_state WHERE poller='live'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["cycle_seq"], 1)
        self.assertEqual(row["interval_reason"], expected_reason)
        self.assertAlmostEqual(row["interval_seconds"], expected_period, delta=5.0)
        self.assertIsNotNone(row["next_wake_at"])
        self.assertIsNotNone(row["started_at"])
        # _StopLoop is a normal Python exception, so run_forever's `finally`
        # runs before it propagates out (same as any other exception) and
        # stamps stopped_at -- only an actual SIGKILL skips Python cleanup
        # entirely and leaves a genuinely crashed poller's row stopped_at
        # NULL, which is what makes the field meaningful in production.
        self.assertIsNotNone(row["stopped_at"])

    def test_second_wake_on_a_caffeinate_lead_only_schedule_skips_the_poll(self):
        # A game 5h out: the very first wake always polls regardless (see
        # run_forever's cycle_seq == 0 rule), but nothing changes about the
        # schedule between the first and second wake here (still 5h out,
        # give or take milliseconds of test runtime) -- so the second wake
        # should be the caffeinate-lead-only case, and skip run_cycle
        # entirely rather than costing a second real ESPN call. Root cause
        # of two valueless production `scoreboard` calls, 2026-09-04.
        now = datetime.now(timezone.utc)
        kick = now + timedelta(hours=5)
        _insert_game(self.conn, "g1", _fmt(kick), "pre")

        class _StopLoop(Exception):
            pass

        scoreboard = Mock(return_value=[])
        # First call: let the loop continue to a second iteration. Second
        # call: escape before a third.
        sleep_calls = {"n": 0}

        def _fake_sleep(*a, **kw):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 2:
                raise _StopLoop

        with patch.object(live.espn, "fetch_scoreboard_dates", scoreboard), \
             patch.object(live, "_sleep_until", side_effect=_fake_sleep):
            with self.assertRaises(_StopLoop):
                live.run_forever(self.conn, once=False, mode="normal", dates="20260101")

        self.assertEqual(scoreboard.call_count, 1)  # only the forced first-wake poll

        row = self.conn.execute("SELECT * FROM poller_state WHERE poller='live'").fetchone()
        # Unchanged since the (only) real cycle: the skip never touches
        # last_cycle_*/cycle_seq, since nothing was actually fetched.
        self.assertEqual(row["cycle_seq"], 1)
        # But schedule bookkeeping still advances on the skip -- it's a
        # real (if brief) sleep, not a no-op.
        self.assertIn("caffeinate lead", row["interval_reason"])
        self.assertIsNotNone(row["next_wake_at"])


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
            wake_at = base + 1800.0
            live._sleep_until(wake_at, lambda: False, lambda: False)
        # Should have returned after at most one more time.time() check,
        # never actually sleeping out the (now long-past) budget.
        self.assertLessEqual(len(sleeps), 1)

    def test_stops_immediately_when_stop_flag_already_set(self):
        with patch.object(live.time, "sleep") as sleep_mock:
            live._sleep_until(live.time.time() + 100, lambda: True, lambda: False)
        sleep_mock.assert_not_called()

    def test_backward_clock_jump_is_capped_at_the_originally_computed_budget(self):
        """Sleeps now run up to days (offseason: months). A clock that
        jumps *backward* mid-sleep (NTP correction, VM restore) would
        otherwise inflate `wake_at - time.time()` well past what was
        originally intended -- assert `remaining` is clamped to the budget
        computed once at the start, not to the jumped-back value.

        wake_at is set only 2s out, well under LIVE_SLEEP_SLICE_SECONDS
        (5.0): an *unclamped* remaining (50002s after the jump) would still
        pick the 5.0s slice via min(SLICE, remaining), which would look
        identical to a correctly-clamped 5.0s slice from the same numbers
        -- indistinguishable. So the budget itself (2s) has to be smaller
        than the slice for the sleep() call's argument to reveal whether
        the clamp actually held.
        """
        calls = {"n": 0}
        base = 1_000_000.0

        def fake_time():
            calls["n"] += 1
            # First call establishes the 2s budget; every call after
            # simulates the clock having jumped 50,000s into the past.
            return base if calls["n"] == 1 else base - 50_000.0

        sleeps = []
        stopped = {"flag": False}

        def fake_sleep(seconds):
            sleeps.append(seconds)
            stopped["flag"] = True  # bail after one slice -- the fake
            # clock never advances on its own, so nothing else would.

        wake_at = base + 2.0
        with patch.object(live.time, "time", side_effect=fake_time), \
             patch.object(live.time, "sleep", side_effect=fake_sleep):
            live._sleep_until(wake_at, lambda: stopped["flag"], lambda: False)

        self.assertEqual(sleeps, [2.0])


if __name__ == "__main__":
    unittest.main()
