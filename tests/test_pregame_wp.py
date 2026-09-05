"""
Tests for the pregame win-probability capture: the source-rank guard on
games.initial_home_wp, the predictor/spread parsers, and the live poller's
pregame sweep-target query.

Background -- ESPN changed the `winprobability` feed for the 2026 season so
that it no longer emits a pregame entry; wp_entries[0] is now the WP after the
game's first play or two, carrying no line information. Measured across the
corpus: 2022-2025 initial_home_wp spans 0.014-0.999, while all 27 stored 2026
games span 0.564-0.631 (0.5744 repeats across 8 unrelated matchups). The fix
prefers ESPN's `predictor` (pregame-only) and falls back to the betting line.

The rank guard is the load-bearing piece: without it, the completion-time
detail fetch (handle_completions -> fetch_details -> mark_detail_fetched)
overwrites a good pregame capture with that first-play number the instant a
game goes final.

Needs a writable fixture DB, so each test builds its own via db.init_db()
into a tempdir, mirroring tests/test_live_schedule.py.

Run with: ./venv/bin/python -m unittest discover tests
"""

import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src import db, espn, live


def _fresh_conn(tmpdir):
    return db.init_db(pathlib.Path(tmpdir) / "pregame_test.db")


def _insert_game(conn, game_id, game_date, status_state="pre", season_year=2026):
    db.upsert_team(conn, "T1", "AAA", "Team A")
    db.upsert_team(conn, "T2", "BBB", "Team B")
    conn.execute("""
        INSERT INTO games (
            game_id, season_year, season_type, week, game_date,
            home_team_id, home_team_abbr, home_team_name,
            away_team_id, away_team_abbr, away_team_name,
            status_state, completed
        ) VALUES (?, ?, 2, 1, ?, 'T1', 'AAA', 'Team A', 'T2', 'BBB', 'Team B', ?, ?)
    """, (game_id, season_year, game_date, status_state,
          1 if status_state == "post" else 0))
    conn.commit()


def _fmt(dt):
    return dt.strftime(live.GAME_DATE_FMT)


def _wp(conn, game_id):
    row = conn.execute(
        "SELECT initial_home_wp, initial_home_wp_source FROM games WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    return row["initial_home_wp"], row["initial_home_wp_source"]


class SourceRankGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = _fresh_conn(self._tmp.name)
        _insert_game(self.conn, "G1", "2026-09-12T16:00Z")

    def test_writes_into_empty(self):
        self.assertTrue(db.set_initial_home_wp(self.conn, "G1", 0.948, "predictor"))
        self.assertEqual(_wp(self.conn, "G1"), (0.948, "predictor"))

    def test_higher_rank_overwrites_lower(self):
        db.set_initial_home_wp(self.conn, "G1", 0.60, "espn_wp")
        self.assertTrue(db.set_initial_home_wp(self.conn, "G1", 0.055, "spread"))
        self.assertEqual(_wp(self.conn, "G1"), (0.055, "spread"))

    def test_lower_rank_rejected(self):
        db.set_initial_home_wp(self.conn, "G1", 0.948, "predictor")
        self.assertFalse(db.set_initial_home_wp(self.conn, "G1", 0.60, "espn_wp"))
        self.assertEqual(_wp(self.conn, "G1"), (0.948, "predictor"))

    def test_equal_rank_refreshes(self):
        # The day-of pass re-reads a game already captured days earlier and
        # must be able to pick up line movement.
        db.set_initial_home_wp(self.conn, "G1", 0.90, "predictor")
        self.assertTrue(db.set_initial_home_wp(self.conn, "G1", 0.94, "predictor"))
        self.assertEqual(_wp(self.conn, "G1"), (0.94, "predictor"))

    def test_historical_pregame_value_survives_refetch(self):
        # The regression the user asked about: re-fetching a 2022-2025 game
        # (or rescoring one) must not degrade its genuine pregame number.
        db.set_initial_home_wp(self.conn, "G1", 0.0301, "espn_wp_pregame")
        self.assertFalse(db.set_initial_home_wp(self.conn, "G1", 0.59, "espn_wp"))
        self.assertFalse(db.set_initial_home_wp(self.conn, "G1", 0.20, "spread"))
        self.assertEqual(_wp(self.conn, "G1"), (0.0301, "espn_wp_pregame"))

    def test_none_value_is_a_noop(self):
        db.set_initial_home_wp(self.conn, "G1", 0.5, "predictor")
        self.assertFalse(db.set_initial_home_wp(self.conn, "G1", None, "spread"))
        self.assertEqual(_wp(self.conn, "G1"), (0.5, "predictor"))

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            db.set_initial_home_wp(self.conn, "G1", 0.5, "vibes")

    def test_mark_detail_fetched_cannot_clobber_predictor(self):
        # End-to-end version of the completion-time regression.
        db.set_initial_home_wp(self.conn, "G1", 0.948, "predictor")
        db.mark_detail_fetched(self.conn, "G1", 45, 6, 1000, 0.5999)
        self.assertEqual(_wp(self.conn, "G1"), (0.948, "predictor"))
        row = self.conn.execute(
            "SELECT detail_fetched, home_score FROM games WHERE game_id='G1'").fetchone()
        self.assertEqual((row["detail_fetched"], row["home_score"]), (1, 45))

    def test_mark_detail_fetched_still_fills_an_empty_value(self):
        db.mark_detail_fetched(self.conn, "G1", 45, 6, 1000, 0.5999)
        self.assertEqual(_wp(self.conn, "G1"), (0.5999, "espn_wp"))


class PredictorParseTest(unittest.TestCase):
    def test_parses_game_projection(self):
        summary = {"predictor": {"homeTeam": {"id": "2483", "gameProjection": "94.8"},
                                 "awayTeam": {"id": "68", "gameProjection": "5.2"}}}
        self.assertAlmostEqual(espn.parse_predictor(summary), 0.948)

    def test_absent_predictor_returns_none(self):
        # Every completed-game payload looks like this.
        self.assertIsNone(espn.parse_predictor({"winprobability": []}))

    def test_garbage_projection_returns_none(self):
        self.assertIsNone(espn.parse_predictor(
            {"predictor": {"homeTeam": {"gameProjection": "n/a"}}}))
        self.assertIsNone(espn.parse_predictor(
            {"predictor": {"homeTeam": {"gameProjection": "180"}}}))


class SpreadParseTest(unittest.TestCase):
    def test_home_favorite_yields_high_wp(self):
        # "ORE -24.5" on a home Oregon game: spread is home-relative.
        wp = espn.parse_spread_home_wp({"pickcenter": [{"spread": -24.5}]})
        self.assertGreater(wp, 0.9)

    def test_home_underdog_yields_low_wp(self):
        # Miami -24.5 at Stanford, i.e. home-relative +24.5. The real game
        # ended 45-6; the stored value was 0.5999.
        wp = espn.parse_spread_home_wp({"pickcenter": [{"spread": 24.5}]})
        self.assertLess(wp, 0.10)

    def test_pick_em_is_a_coinflip(self):
        self.assertAlmostEqual(
            espn.parse_spread_home_wp({"pickcenter": [{"spread": 0}]}), 0.5)

    def test_no_line_returns_none(self):
        # Books post nothing on lopsided FCS matchups -- 12 of 24 on one slate.
        self.assertIsNone(espn.parse_spread_home_wp({"pickcenter": []}))
        self.assertIsNone(espn.parse_spread_home_wp({}))
        self.assertIsNone(espn.parse_spread_home_wp({"pickcenter": [{"spread": None}]}))

    def test_matches_espn_predictor_within_tolerance(self):
        # Fitted against ESPN's own predictor; mean |resid| was 0.029, max
        # 0.098. These are real (spread, predictor) pairs from the 2026 week-2
        # slate, so a refit that badly changed the shape would trip this.
        for spread, predictor in [(6.5, 0.333), (19.5, 0.092), (-14.5, 0.903),
                                  (1.5, 0.522), (21.5, 0.075), (-16.5, 0.822),
                                  (-35.5, 0.981), (-44.5, 0.995)]:
            got = espn.parse_spread_home_wp({"pickcenter": [{"spread": spread}]})
            self.assertLess(abs(got - predictor), 0.12,
                            f"spread {spread}: got {got:.3f}, ESPN said {predictor}")


class PregameTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = _fresh_conn(self._tmp.name)
        self.now = datetime.now(timezone.utc)

    def _add(self, game_id, days_out, status_state="pre"):
        _insert_game(self.conn, game_id, _fmt(self.now + timedelta(days=days_out)),
                     status_state=status_state)

    def _archive(self, game_id, captured_days_ago):
        db.upsert_game_pregame_json(self.conn, game_id, {"predictor": {}})
        stamp = (self.now - timedelta(days=captured_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE game_pregame_json SET fetched_at = ? WHERE game_id = ?",
            (stamp, game_id))
        self.conn.commit()

    def test_uncaptured_upcoming_game_is_a_target(self):
        self._add("G1", 3)
        self.assertEqual(live._pregame_targets(self.conn), ["G1"])

    def test_beyond_horizon_is_skipped(self):
        self._add("G1", live.LIVE_PREGAME_HORIZON_DAYS + 2)
        self.assertEqual(live._pregame_targets(self.conn), [])

    def test_already_started_game_is_skipped(self):
        self._add("G1", 3, status_state="in")
        self._add("G2", 3, status_state="post")
        self.assertEqual(live._pregame_targets(self.conn), [])

    def test_past_kickoff_is_skipped(self):
        # A game the scoreboard still calls 'pre' after its kickoff has passed
        # has no predictor left to capture.
        self._add("G1", -0.5)
        self.assertEqual(live._pregame_targets(self.conn), [])

    def test_stale_capture_is_refreshed_day_of(self):
        # Captured a week ago, kickoff tomorrow -> due for the day-of refresh.
        self._add("G1", 1)
        self._archive("G1", captured_days_ago=7)
        self.assertEqual(live._pregame_targets(self.conn), ["G1"])

    def test_fresh_capture_inside_the_lead_window_drops_out(self):
        # Captured just now, kickoff in 12h -> already inside the final day.
        self._add("G1", 0.5)
        self._archive("G1", captured_days_ago=0)
        self.assertEqual(live._pregame_targets(self.conn), [])

    def test_early_capture_of_a_distant_game_is_not_refetched(self):
        # Captured now, kickoff 6 days out: already archived and kickoff is
        # far, so nothing to do. This is the busy-loop guard -- without the
        # "kickoff is near" half of the predicate, an early capture looks
        # perpetually stale and gets re-fetched on every single cycle.
        self._add("G1", 6)
        self._archive("G1", captured_days_ago=0)
        self.assertEqual(live._pregame_targets(self.conn), [])
        self.assertEqual(live._pregame_targets(self.conn), [])

    def test_that_same_game_returns_as_a_target_near_kickoff(self):
        # The same game a few days later: capture is now well outside the
        # refresh window and kickoff is hours away, so it is due again.
        self._add("G1", 0.25)
        self._archive("G1", captured_days_ago=6)
        self.assertEqual(live._pregame_targets(self.conn), ["G1"])

    def test_soonest_kickoff_first_and_budget_capped(self):
        self._add("G1", 5)
        self._add("G2", 1)
        self._add("G3", 3)
        self.assertEqual(live._pregame_targets(self.conn), ["G2", "G3", "G1"])
        self.assertEqual(live._pregame_targets(self.conn, budget=2), ["G2", "G3"])


class PregameArchiveTest(unittest.TestCase):
    def test_roundtrip_and_separate_from_completed_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _fresh_conn(tmp)
            _insert_game(conn, "G1", "2026-09-12T16:00Z")
            pre = {"predictor": {"homeTeam": {"gameProjection": "94.8"}}, "winprobability": []}
            post = {"winprobability": [{"homeWinPercentage": 0.6043}], "drives": {}}
            db.upsert_game_pregame_json(conn, "G1", pre)
            db.upsert_game_raw_json(conn, "G1", post)
            conn.commit()
            # Same game_id in both tables, neither clobbering the other --
            # the reason this is a separate table, not a composite key.
            self.assertEqual(db.get_game_pregame_json(conn, "G1"), pre)
            self.assertEqual(db.get_game_raw_json(conn, "G1"), post)

    def test_reupsert_overwrites_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _fresh_conn(tmp)
            _insert_game(conn, "G1", "2026-09-12T16:00Z")
            db.upsert_game_pregame_json(conn, "G1", {"v": 1})
            db.upsert_game_pregame_json(conn, "G1", {"v": 2})
            conn.commit()
            self.assertEqual(db.get_game_pregame_json(conn, "G1"), {"v": 2})
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM game_pregame_json").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
