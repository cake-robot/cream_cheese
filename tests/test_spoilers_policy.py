"""
Pure tests for src/spoilers.py -- no Flask, no request context, no real
data/cfb.db. Each test gets its own scratch policy file (spoilers.POLICY_PATH
monkeypatched to a tempdir) so nothing here touches, or is affected by, the
repo's real data/spoilers.json.

Run with: ./venv/bin/python -m unittest discover tests
"""

import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src import spoilers


class PolicyFileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_path = spoilers.POLICY_PATH
        spoilers.POLICY_PATH = Path(self._tmpdir) / "spoilers.json"
        spoilers._cache["key"] = None
        spoilers._cache["policy"] = None

    def tearDown(self):
        spoilers.POLICY_PATH = self._orig_path
        spoilers._cache["key"] = None
        spoilers._cache["policy"] = None
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class TestPrecedence(PolicyFileTestCase):
    ROW_HIDDEN_WEEK = {"game_id": "g1", "season_year": 2026, "season_type": 2, "week": 3}
    ROW_VISIBLE_WEEK = {"game_id": "g1", "season_year": 2024, "season_type": 2, "week": 3}

    def test_default_only(self):
        policy = spoilers.load_policy()
        self.assertTrue(spoilers.is_hidden_row(self.ROW_HIDDEN_WEEK, policy))
        self.assertFalse(spoilers.is_hidden_row(self.ROW_VISIBLE_WEEK, policy))

    def test_week_override_hides_a_visible_season(self):
        spoilers.set_week(2024, 2, 3, True)
        policy = spoilers.load_policy()
        self.assertTrue(spoilers.is_hidden_row(self.ROW_VISIBLE_WEEK, policy))

    def test_week_override_reveals_a_hidden_season(self):
        spoilers.set_week(2026, 2, 3, False)
        policy = spoilers.load_policy()
        self.assertFalse(spoilers.is_hidden_row(self.ROW_HIDDEN_WEEK, policy))

    def test_game_override_beats_week_override_hide_over_reveal(self):
        spoilers.set_week(2026, 2, 3, False)  # week revealed
        spoilers.set_game("g1", True)  # this one game re-hidden
        policy = spoilers.load_policy()
        self.assertTrue(spoilers.is_hidden_row(self.ROW_HIDDEN_WEEK, policy))

    def test_game_override_beats_week_override_reveal_over_hide(self):
        spoilers.set_week(2024, 2, 3, True)  # week hidden
        spoilers.set_game("g1", False)  # this one game revealed
        policy = spoilers.load_policy()
        self.assertFalse(spoilers.is_hidden_row(self.ROW_VISIBLE_WEEK, policy))

    def test_clearing_game_override_falls_back_to_week(self):
        spoilers.set_week(2024, 2, 3, True)
        spoilers.set_game("g1", False)
        spoilers.set_game("g1", None)
        policy = spoilers.load_policy()
        self.assertTrue(spoilers.is_hidden_row(self.ROW_VISIBLE_WEEK, policy))

    def test_clearing_week_override_falls_back_to_default(self):
        spoilers.set_week(2024, 2, 3, True)
        spoilers.set_week(2024, 2, 3, None)
        policy = spoilers.load_policy()
        self.assertFalse(spoilers.is_hidden_row(self.ROW_VISIBLE_WEEK, policy))

    def test_hidden_false_is_a_real_override_not_a_noop(self):
        # hidden:false on an otherwise-hidden week must persist as an
        # explicit entry, distinct from clearing (hidden:None) -- that's
        # what makes the (now four-state) UI possible. Stored as
        # LEVEL_FULL, not the legacy bool -- assertFalse(2) would wrongly
        # pass/fail here since 2 is truthy, so compare against the level
        # constant explicitly rather than by truthiness.
        spoilers.set_week(2026, 2, 3, False)
        policy = spoilers.load_policy()
        self.assertIn("2026:2:3", policy["weeks"])
        self.assertEqual(policy["weeks"]["2026:2:3"], spoilers.LEVEL_FULL)

    def test_score_only_level_beats_default_hidden(self):
        spoilers.set_week(2026, 2, 3, spoilers.LEVEL_SCORE)
        policy = spoilers.load_policy()
        self.assertEqual(spoilers.level_of_row(self.ROW_HIDDEN_WEEK, policy), spoilers.LEVEL_SCORE)

    def test_game_override_score_only_beats_week_hidden(self):
        spoilers.set_week(2026, 2, 3, spoilers.LEVEL_HIDDEN)
        spoilers.set_game("g1", spoilers.LEVEL_SCORE)
        policy = spoilers.load_policy()
        self.assertEqual(spoilers.level_of_row(self.ROW_HIDDEN_WEEK, policy), spoilers.LEVEL_SCORE)


class TestFutureProofing(PolicyFileTestCase):
    def test_future_seasons_hidden_with_no_config_change(self):
        policy = spoilers.load_policy()  # hidden_from defaults to 2026 week 1 (regular)
        for year in (2027, 2030, 2099):
            row = {"game_id": "x", "season_year": year, "season_type": 2, "week": 1}
            self.assertTrue(spoilers.is_hidden_row(row, policy), f"{year} should be hidden by default")


class TestDefaultHidden(unittest.TestCase):
    """The ordinal comparison behind the Default threshold -- see
    src/spoilers.py's _default_hidden()/_default_hidden_sql() docstrings.
    Mid-season recalibration (a threshold pointing at a week within a
    season already in the DB, not just a bare year) is the whole reason
    this got more granular than a single hidden_from_season int."""

    def test_regular_season_threshold(self):
        hf = {"season_year": 2026, "season_type": 2, "week": 3}
        cases = [
            ((2025, 2, 15), False),  # prior season entirely visible
            ((2026, 2, 1), False),   # same season, before the threshold week
            ((2026, 2, 2), False),
            ((2026, 2, 3), True),    # the threshold week itself is hidden (inclusive)
            ((2026, 2, 15), True),
            ((2026, 3, 1), True),    # postseason of that year is always later
            ((2027, 2, 1), True),    # any later season entirely
        ]
        for (sy, st, wk), expected in cases:
            self.assertEqual(spoilers._default_hidden(sy, st, wk, hf), expected, (sy, st, wk))

    def test_postseason_threshold(self):
        hf = {"season_year": 2026, "season_type": 3, "week": 1}
        cases = [
            ((2026, 2, 15), False),  # regular season always precedes a postseason threshold
            ((2026, 3, 1), True),    # postseason is one undivided unit
            ((2027, 2, 1), True),
        ]
        for (sy, st, wk), expected in cases:
            self.assertEqual(spoilers._default_hidden(sy, st, wk, hf), expected, (sy, st, wk))

    def test_sql_mirrors_python(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE games (game_id TEXT, season_year INT, season_type INT, week INT)")
        rows = [
            ("a", 2025, 2, 15), ("b", 2026, 2, 1), ("c", 2026, 2, 3),
            ("d", 2026, 2, 15), ("e", 2026, 3, 1), ("f", 2027, 2, 1),
        ]
        conn.executemany("INSERT INTO games VALUES (?,?,?,?)", rows)
        for hf in ({"season_year": 2026, "season_type": 2, "week": 3}, {"season_year": 2026, "season_type": 3, "week": 1}):
            sql, params = spoilers._default_hidden_sql("g", hf)
            got = {r[0]: bool(r[1]) for r in conn.execute(f"SELECT game_id, {sql} FROM games g", params).fetchall()}
            expected = {r[0]: spoilers._default_hidden(r[1], r[2], r[3], hf) for r in rows}
            self.assertEqual(got, expected, hf)
        conn.close()


def _shaped_game_fixture():
    return {
        "game_id": "g1", "season_year": 2025, "season_type": 2, "week": 5,
        "event_note": None, "game_date": "2025-09-27T23:30Z",
        "away": {"abbr": "ORE", "name": "Oregon Ducks", "rank": 6, "score": 30},
        "home": {"abbr": "PSU", "name": "Penn State Nittany Lions", "rank": 3, "score": 24},
        "venue_name": "Beaver Stadium", "attendance": 111015,
        "conference_game": True, "neutral_site": False, "completed": True,
        "status_state": "post", "ot": True, "initial_home_wp": 0.5063,
        "watchability_score": 0.672, "uw_loss_bonus": 0.0, "rank": 1, "percentile": 99, "n_scored": 1828,
        "has_fox_correction": False, "has_manual_correction": False,
        "metrics": {"wp_volatility": {"raw": 9.1, "norm": 0.9, "weighted": 0.9, "at_cap": False, "applicable": True}},
        "applicable_weight": 7.0,
        "spoiler_hidden": True,
    }


class TestRedactGame(unittest.TestCase):
    def _shaped(self):
        return _shaped_game_fixture()

    def test_redacted_fields_are_null_not_missing(self):
        out = spoilers.redact_game_full(self._shaped())
        for key in ("watchability_score", "uw_loss_bonus", "rank", "percentile", "n_scored", "ot", "applicable_weight"):
            self.assertIn(key, out)
            self.assertIsNone(out[key])
        self.assertIn("metrics", out)
        self.assertEqual(out["metrics"], {})
        self.assertIsNone(out["away"]["score"])
        self.assertIsNone(out["home"]["score"])
        self.assertFalse(out["has_fox_correction"])
        self.assertFalse(out["has_manual_correction"])

    def test_unrelated_fields_survive(self):
        shaped = self._shaped()
        out = spoilers.redact_game_full(shaped)
        for key in ("game_id", "season_year", "season_type", "week", "game_date",
                    "venue_name", "attendance", "conference_game", "neutral_site",
                    "completed", "status_state", "initial_home_wp"):
            self.assertEqual(out[key], shaped[key])
        self.assertEqual(out["away"]["abbr"], "ORE")
        self.assertEqual(out["away"]["rank"], 6)
        self.assertEqual(out["home"]["abbr"], "PSU")

    def test_idempotent(self):
        shaped = self._shaped()
        once = spoilers.redact_game_full(shaped)
        twice = spoilers.redact_game_full(once)
        self.assertEqual(once, twice)

    def test_does_not_mutate_input(self):
        shaped = self._shaped()
        original_away = dict(shaped["away"])
        spoilers.redact_game_full(shaped)
        self.assertEqual(shaped["away"], original_away)
        self.assertEqual(shaped["watchability_score"], 0.672)


class TestRedactGameScoreOnly(unittest.TestCase):
    """LEVEL_SCORE's redaction: the watchability composite survives, and
    it's the ONLY thing that does -- in particular uw_loss_bonus and ot,
    since both leak outcome facts through fields that look like plain
    numbers (see redact_game_score_only()'s docstring)."""

    def _shaped(self):
        return _shaped_game_fixture()

    def test_score_fields_survive(self):
        out = spoilers.redact_game_score_only(self._shaped())
        self.assertEqual(out["watchability_score"], 0.672)
        self.assertEqual(out["rank"], 1)
        self.assertEqual(out["percentile"], 99)
        self.assertEqual(out["n_scored"], 1828)

    def test_everything_else_matches_full_redaction(self):
        shaped = self._shaped()
        score_only = spoilers.redact_game_score_only(shaped)
        full = spoilers.redact_game_full(shaped)
        for key in ("uw_loss_bonus", "ot", "applicable_weight"):
            self.assertIsNone(score_only[key])
            self.assertEqual(score_only[key], full[key])
        self.assertEqual(score_only["metrics"], {})
        self.assertIsNone(score_only["away"]["score"])
        self.assertIsNone(score_only["home"]["score"])
        self.assertFalse(score_only["has_fox_correction"])
        self.assertFalse(score_only["has_manual_correction"])

    def test_does_not_mutate_input(self):
        shaped = self._shaped()
        spoilers.redact_game_score_only(shaped)
        self.assertEqual(shaped["uw_loss_bonus"], 0.0)
        self.assertEqual(shaped["ot"], True)


def _live_fixture(period=3):
    return {
        "live_score": 0.55, "quality_so_far": 0.4, "drama_from_here": 0.6,
        "progress": 0.5, "wp_now": 0.62,
        "status": {
            "period": period, "clock_display": "4:12",
            "detail": "Final/OT" if period > 4 else f"Q{period} 4:12",
        },
        "so_far": {"applicable_weight": 2.0, "metrics": {"x": {"raw": 1}}},
        "from_here": {"applicable_weight": 3.0, "metrics": {"y": {"raw": 2}}},
        "headline": "Upset finish potential + Recent swings",
        "computed_at": "2026-09-05T20:00:00Z", "stale_seconds": 12.0,
    }


class TestRedactLive(unittest.TestCase):
    def _live(self, period=3):
        return _live_fixture(period)

    def test_none_passthrough(self):
        self.assertIsNone(spoilers.redact_live_full(None))

    def test_nulls_score_and_headline(self):
        out = spoilers.redact_live_full(self._live())
        self.assertIsNone(out["live_score"])
        self.assertIsNone(out["quality_so_far"])
        self.assertIsNone(out["drama_from_here"])
        self.assertIsNone(out["wp_now"])
        self.assertIsNone(out["headline"])
        self.assertEqual(out["so_far"]["metrics"], {})
        self.assertEqual(out["from_here"]["metrics"], {})
        self.assertIsNone(out["so_far"]["applicable_weight"])

    def test_detail_always_nulled(self):
        out = spoilers.redact_live_full(self._live(period=3))
        self.assertIsNone(out["status"]["detail"])

    def test_period_and_clock_survive_in_regulation(self):
        out = spoilers.redact_live_full(self._live(period=3))
        self.assertEqual(out["status"]["period"], 3)
        self.assertEqual(out["status"]["clock_display"], "4:12")

    def test_period_and_clock_nulled_in_overtime(self):
        out = spoilers.redact_live_full(self._live(period=5))
        self.assertIsNone(out["status"]["period"])
        self.assertIsNone(out["status"]["clock_display"])

    def test_progress_and_computed_at_survive(self):
        out = spoilers.redact_live_full(self._live())
        self.assertEqual(out["progress"], 0.5)
        self.assertEqual(out["computed_at"], "2026-09-05T20:00:00Z")
        self.assertEqual(out["stale_seconds"], 12.0)


class TestRedactLiveScoreOnly(unittest.TestCase):
    """LEVEL_SCORE's live redaction: live_score (the running composite)
    survives -- the running-total analog of watchability_score -- but
    wp_now/headline/quality_so_far/drama_from_here and the OT period tell
    stay nulled exactly as they do at LEVEL_HIDDEN."""

    def test_none_passthrough(self):
        self.assertIsNone(spoilers.redact_live_score_only(None))

    def test_live_score_survives(self):
        out = spoilers.redact_live_score_only(_live_fixture())
        self.assertEqual(out["live_score"], 0.55)

    def test_progress_survives(self):
        out = spoilers.redact_live_score_only(_live_fixture())
        self.assertEqual(out["progress"], 0.5)

    def test_everything_else_matches_full_redaction(self):
        live = _live_fixture(period=5)
        score_only = spoilers.redact_live_score_only(live)
        full = spoilers.redact_live_full(live)
        for key in ("quality_so_far", "drama_from_here", "wp_now", "headline"):
            self.assertIsNone(score_only[key])
            self.assertEqual(score_only[key], full[key])
        self.assertIsNone(score_only["status"]["detail"])
        self.assertIsNone(score_only["status"]["period"])
        self.assertIsNone(score_only["status"]["clock_display"])
        self.assertEqual(score_only["so_far"]["metrics"], {})
        self.assertEqual(score_only["from_here"]["metrics"], {})


class TestRedactLiveHistory(unittest.TestCase):
    def _rows(self):
        return [
            {"computed_at": "t1", "progress": 0.2, "live_score": 0.3, "quality_so_far": 0.1, "drama_from_here": 0.4},
            {"computed_at": "t2", "progress": 0.5, "live_score": 0.6, "quality_so_far": 0.5, "drama_from_here": 0.2},
        ]

    def test_level_full_passthrough(self):
        rows = self._rows()
        out = spoilers.redact_live_history(rows, spoilers.LEVEL_FULL)
        self.assertEqual(out, rows)

    def test_level_score_keeps_live_score_and_progress_nulls_rest(self):
        out = spoilers.redact_live_history(self._rows(), spoilers.LEVEL_SCORE)
        for r_in, r_out in zip(self._rows(), out):
            self.assertEqual(r_out["live_score"], r_in["live_score"])
            self.assertEqual(r_out["progress"], r_in["progress"])
            self.assertEqual(r_out["computed_at"], r_in["computed_at"])
            self.assertIsNone(r_out["quality_so_far"])
            self.assertIsNone(r_out["drama_from_here"])

    def test_does_not_mutate_input(self):
        rows = self._rows()
        spoilers.redact_live_history(rows, spoilers.LEVEL_SCORE)
        self.assertEqual(rows[0]["quality_so_far"], 0.1)


class TestVisibleSql(PolicyFileTestCase):
    def _run(self, policy, rows, min_level=spoilers.LEVEL_SCORE):
        """Evaluate visible_sql against an in-memory sqlite table shaped
        like `games` -- exercises the actual SQL, not a Python
        reimplementation of it."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE games (game_id TEXT, season_year INT, season_type INT, week INT, watchability_score REAL)"
        )
        conn.executemany(
            "INSERT INTO games VALUES (?,?,?,?,?)",
            [(r["game_id"], r["season_year"], r["season_type"], r["week"], 0.5) for r in rows],
        )
        clause, params = spoilers.visible_sql(policy, alias="g", min_level=min_level)
        rows_out = conn.execute(f"SELECT game_id FROM games g WHERE {clause}", params).fetchall()
        conn.close()
        return {r[0] for r in rows_out}

    def test_default_only(self):
        policy = spoilers.load_policy()
        rows = [
            {"game_id": "old", "season_year": 2025, "season_type": 2, "week": 1},
            {"game_id": "new", "season_year": 2026, "season_type": 2, "week": 1},
        ]
        self.assertEqual(self._run(policy, rows), {"old"})

    def test_week_and_game_overrides(self):
        spoilers.set_week(2025, 2, 1, True)  # hide an old week
        spoilers.set_game("new", False)  # reveal one new-season game
        policy = spoilers.load_policy()
        rows = [
            {"game_id": "old_hidden_week", "season_year": 2025, "season_type": 2, "week": 1},
            {"game_id": "old_other_week", "season_year": 2025, "season_type": 2, "week": 2},
            {"game_id": "new", "season_year": 2026, "season_type": 2, "week": 1},
            {"game_id": "new_other", "season_year": 2026, "season_type": 2, "week": 1},
        ]
        self.assertEqual(self._run(policy, rows), {"old_other_week", "new"})

    def test_score_only_game_included_at_default_min_level(self):
        spoilers.set_game("scoreonly", spoilers.LEVEL_SCORE)
        policy = spoilers.load_policy()
        rows = [
            {"game_id": "scoreonly", "season_year": 2025, "season_type": 2, "week": 1},
            {"game_id": "full", "season_year": 2025, "season_type": 2, "week": 1},
        ]
        self.assertEqual(self._run(policy, rows), {"scoreonly", "full"})

    def test_score_only_game_excluded_at_min_level_full(self):
        spoilers.set_game("scoreonly", spoilers.LEVEL_SCORE)
        policy = spoilers.load_policy()
        rows = [
            {"game_id": "scoreonly", "season_year": 2025, "season_type": 2, "week": 1},
            {"game_id": "full", "season_year": 2025, "season_type": 2, "week": 1},
        ]
        self.assertEqual(self._run(policy, rows, min_level=spoilers.LEVEL_FULL), {"full"})

    def test_hidden_game_excluded_regardless_of_min_level(self):
        spoilers.set_game("hidden", spoilers.LEVEL_HIDDEN)
        policy = spoilers.load_policy()
        rows = [{"game_id": "hidden", "season_year": 2025, "season_type": 2, "week": 1}]
        self.assertEqual(self._run(policy, rows, min_level=spoilers.LEVEL_SCORE), set())
        self.assertEqual(self._run(policy, rows, min_level=spoilers.LEVEL_FULL), set())


class TestLevelSql(PolicyFileTestCase):
    """SQL/Python mirror test for level_sql(), analogous to
    TestDefaultHidden.test_sql_mirrors_python but exercising the full
    game/week-override precedence, not just the default threshold."""

    def test_sql_mirrors_python_across_a_mixed_policy(self):
        spoilers.set_week(2025, 2, 1, spoilers.LEVEL_SCORE)
        spoilers.set_week(2025, 2, 2, spoilers.LEVEL_HIDDEN)
        spoilers.set_game("g_full_override", spoilers.LEVEL_FULL)  # week 2, game wins
        spoilers.set_game("g_hidden_override", spoilers.LEVEL_HIDDEN)  # week 1, game wins
        policy = spoilers.load_policy()

        rows = [
            ("a", 2025, 2, 1),                 # week-1 override -> SCORE
            ("g_hidden_override", 2025, 2, 1),  # game override beats week-1 -> HIDDEN
            ("b", 2025, 2, 2),                  # week-2 override -> HIDDEN
            ("g_full_override", 2025, 2, 2),    # game override beats week-2 -> FULL
            ("c", 2025, 2, 3),                  # no override -> default (FULL, before threshold)
            ("d", 2026, 2, 5),                  # no override -> default (HIDDEN, at/after threshold)
        ]
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE games (game_id TEXT, season_year INT, season_type INT, week INT)")
        conn.executemany("INSERT INTO games VALUES (?,?,?,?)", rows)
        sql, params = spoilers.level_sql(policy, alias="g")
        got = {r[0]: r[1] for r in conn.execute(f"SELECT game_id, {sql} FROM games g", params).fetchall()}
        conn.close()

        expected = {
            gid: spoilers.level_of_row({"game_id": gid, "season_year": sy, "season_type": st, "week": wk}, policy)
            for gid, sy, st, wk in rows
        }
        self.assertEqual(got, expected)


class TestAtomicWrite(PolicyFileTestCase):
    def test_failed_replace_leaves_original_untouched(self):
        spoilers.set_week(2025, 2, 1, True)
        before = spoilers.POLICY_PATH.read_bytes()

        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                spoilers.set_week(2025, 2, 2, True)

        after = spoilers.POLICY_PATH.read_bytes()
        self.assertEqual(before, after)
        leftovers = [p for p in spoilers.POLICY_PATH.parent.iterdir() if p.name.startswith(".spoilers-")]
        self.assertEqual(leftovers, [])

    def test_reload_after_failed_write_returns_prior_state(self):
        spoilers.set_week(2025, 2, 1, True)
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                spoilers.set_week(2025, 2, 2, True)
        policy = spoilers.load_policy()
        self.assertIn("2025:2:1", policy["weeks"])
        self.assertNotIn("2025:2:2", policy["weeks"])


class TestConcurrency(PolicyFileTestCase):
    def test_concurrent_set_week_all_survive(self):
        n = 20
        errors = []

        def worker(i):
            try:
                spoilers.set_week(2025, 2, i, True)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        policy = spoilers.load_policy()
        for i in range(n):
            self.assertIn(f"2025:2:{i}", policy["weeks"], f"week {i} lost in concurrent write")


class TestMalformedFile(PolicyFileTestCase):
    def test_corrupt_json_falls_back_to_default(self):
        spoilers.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        spoilers.POLICY_PATH.write_text("not json{{{")
        spoilers._cache["key"] = None
        policy = spoilers.load_policy()
        self.assertEqual(policy["hidden_from"], spoilers.DEFAULT_HIDDEN_FROM)
        self.assertEqual(policy["weeks"], {})

    def test_missing_file_returns_default(self):
        policy = spoilers.load_policy()
        self.assertEqual(policy, spoilers._default_policy())


class TestLegacySchemaMigration(PolicyFileTestCase):
    """A file saved before the default threshold gained week granularity
    used a bare `hidden_from_season` int. A saved year Y meant exactly
    "hide season Y onward", which is season_type=2, week=1 under the new
    schema -- _normalize() must read an old file this way rather than
    resetting someone's setting out from under them on first load."""

    def test_legacy_int_migrates_to_season_type_2_week_1(self):
        spoilers.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        spoilers.POLICY_PATH.write_text('{"version": 1, "hidden_from_season": 2027, "weeks": {}, "games": {}}')
        spoilers._cache["key"] = None
        policy = spoilers.load_policy()
        self.assertEqual(policy["hidden_from"], {"season_year": 2027, "season_type": 2, "week": 1})
        self.assertNotIn("hidden_from_season", policy)

    def test_migrated_policy_behaves_like_the_old_rule(self):
        spoilers.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        spoilers.POLICY_PATH.write_text('{"version": 1, "hidden_from_season": 2027, "weeks": {}, "games": {}}')
        spoilers._cache["key"] = None
        policy = spoilers.load_policy()
        self.assertFalse(spoilers.is_hidden_row({"game_id": "x", "season_year": 2026, "season_type": 2, "week": 15}, policy))
        self.assertTrue(spoilers.is_hidden_row({"game_id": "x", "season_year": 2027, "season_type": 2, "week": 1}, policy))

    def test_new_schema_present_wins_over_legacy_key(self):
        # Shouldn't happen in practice (save_policy never writes both), but
        # if a hand-edited file somehow has both, the new key is authoritative.
        spoilers.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        spoilers.POLICY_PATH.write_text(
            '{"version": 1, "hidden_from_season": 2027, '
            '"hidden_from": {"season_year": 2030, "season_type": 2, "week": 5}, "weeks": {}, "games": {}}'
        )
        spoilers._cache["key"] = None
        policy = spoilers.load_policy()
        self.assertEqual(policy["hidden_from"], {"season_year": 2030, "season_type": 2, "week": 5})


class TestCoerceLevel(unittest.TestCase):
    """_coerce_level() is what lets a policy file/row saved before
    LEVEL_SCORE existed (weeks/games values were bare bools) keep working
    unchanged, and what stops a corrupted or out-of-range value from
    propagating into SQL/JSON responses."""

    def test_legacy_true_is_hidden(self):
        self.assertEqual(spoilers._coerce_level(True), spoilers.LEVEL_HIDDEN)

    def test_legacy_false_is_full(self):
        self.assertEqual(spoilers._coerce_level(False), spoilers.LEVEL_FULL)

    def test_valid_ints_pass_through(self):
        for level in (spoilers.LEVEL_HIDDEN, spoilers.LEVEL_SCORE, spoilers.LEVEL_FULL):
            self.assertEqual(spoilers._coerce_level(level), level)

    def test_out_of_range_int_is_dropped(self):
        self.assertIsNone(spoilers._coerce_level(5))
        self.assertIsNone(spoilers._coerce_level(-1))

    def test_garbage_is_dropped(self):
        self.assertIsNone(spoilers._coerce_level("hidden"))
        self.assertIsNone(spoilers._coerce_level(None))
        self.assertIsNone(spoilers._coerce_level(1.0))


class TestLevelMigration(PolicyFileTestCase):
    """A policy file/row written before LEVEL_SCORE existed stores plain
    bools for weeks/games -- _normalize() must read one exactly like the
    pre-LEVEL_SCORE code did (true -> hidden, false -> fully revealed),
    with no migration script and no behavior change for anyone who never
    sets a LEVEL_SCORE override."""

    def test_legacy_bools_migrate_to_levels_on_load(self):
        spoilers.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        spoilers.POLICY_PATH.write_text(
            '{"version": 1, "hidden_from": {"season_year": 2026, "season_type": 2, "week": 1}, '
            '"weeks": {"2025:2:1": true, "2025:2:2": false}, "games": {"g1": true, "g2": false}}'
        )
        spoilers._cache["key"] = None
        policy = spoilers.load_policy()
        self.assertEqual(policy["weeks"]["2025:2:1"], spoilers.LEVEL_HIDDEN)
        self.assertEqual(policy["weeks"]["2025:2:2"], spoilers.LEVEL_FULL)
        self.assertEqual(policy["games"]["g1"], spoilers.LEVEL_HIDDEN)
        self.assertEqual(policy["games"]["g2"], spoilers.LEVEL_FULL)

    def test_out_of_range_level_is_dropped_not_propagated(self):
        spoilers.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        spoilers.POLICY_PATH.write_text(
            '{"version": 1, "hidden_from": {"season_year": 2026, "season_type": 2, "week": 1}, '
            '"weeks": {}, "games": {"g1": 5, "g2": 1}}'
        )
        spoilers._cache["key"] = None
        policy = spoilers.load_policy()
        self.assertNotIn("g1", policy["games"])
        self.assertEqual(policy["games"]["g2"], spoilers.LEVEL_SCORE)

    def test_set_week_accepts_legacy_bool(self):
        spoilers.set_week(2025, 2, 1, True)
        policy = spoilers.load_policy()
        self.assertEqual(policy["weeks"]["2025:2:1"], spoilers.LEVEL_HIDDEN)

    def test_set_game_accepts_legacy_bool(self):
        spoilers.set_game("g1", False)
        policy = spoilers.load_policy()
        self.assertEqual(policy["games"]["g1"], spoilers.LEVEL_FULL)


if __name__ == "__main__":
    unittest.main()
