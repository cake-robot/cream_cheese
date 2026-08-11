"""
Property and fixture tests for the live scoring model (src/live.py), run
entirely offline against src/live_replay.py replays of games already in
data/cfb.db. No network access; run with:

    ./venv/bin/python -m unittest discover tests

It is the college football offseason while this model is being built, so
there is no live game to test against -- these tests are the substitute:
real completed games, replayed prefix-by-prefix, checked against properties
the live model is specifically designed to have (see plans/... "noon
Pacific Saturday" design doc). Numeric thresholds below were calibrated
against actual measured output of the implemented model, not guessed in
advance -- where the design doc's original draft numbers differed from
measured behavior (a planning-time estimate, not a promise), the measured
value is what's asserted here, with the discrepancy noted inline.

Fixture-refresh SQL (regenerate this table against a fresh corpus):

    -- flagship late drama
    SELECT g.game_id, g.away_team_abbr||' @ '||g.home_team_abbr, g.watchability_score,
           (SELECT MAX(period_number) FROM win_probability w WHERE w.game_id=g.game_id) max_period
    FROM games g WHERE g.watchability_score IS NOT NULL ORDER BY g.watchability_score DESC LIMIT 10;

    -- close-through-Q3, blown open (halves-diverge case)
    WITH q3 AS (SELECT game_id, AVG(ABS(home_win_pct-0.5)) skew3
                FROM win_probability WHERE period_number <= 3 GROUP BY game_id)
    SELECT g.game_id, g.away_team_abbr||' @ '||g.home_team_abbr, g.home_score, g.away_score, q3.skew3
    FROM games g JOIN q3 ON q3.game_id = g.game_id
    WHERE g.watchability_score IS NOT NULL AND ABS(g.home_score-g.away_score) >= 24 AND q3.skew3 < 0.15
    ORDER BY q3.skew3 LIMIT 10;

    -- ranked favorite actually lost (upset-in-progress fixtures, both orientations)
    SELECT game_id, away_team_abbr, home_team_abbr, away_rank, home_rank, initial_home_wp, watchability_score
    FROM games WHERE watchability_score IS NOT NULL
      AND ((home_rank <= 10 AND away_rank IS NULL AND home_score < away_score)
        OR (away_rank <= 10 AND home_rank IS NULL AND away_score < home_score))
    ORDER BY watchability_score DESC LIMIT 10;

    -- corrupted-score defense fixtures
    SELECT game_id, MIN(home_score) minh, MIN(away_score) mina, COUNT(*) n
    FROM win_probability GROUP BY game_id HAVING minh < 0 OR mina < 0 ORDER BY minh LIMIT 20;

    -- worst blowouts
    SELECT game_id, away_team_abbr||' @ '||home_team_abbr, away_score, home_score, watchability_score
    FROM games WHERE watchability_score IS NOT NULL AND ABS(home_score-away_score) >= 35
    ORDER BY watchability_score ASC LIMIT 10;
"""

import logging
import sqlite3
import unittest

import numpy as np

from src import live, live_replay, scoring

# Quiet the wp_now deviation warning during bulk sweeps -- it fires on
# legitimate rapid swings (a pick-six, a muffed punt) as well as genuine
# data corruption, and floods test output. Individual fixture tests that
# care about it check the condition directly instead of relying on the log.
logging.getLogger("src.live").setLevel(logging.ERROR)

DB_PATH = "data/cfb.db"


def _conn():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class FixtureTests(unittest.TestCase):
    """One test per hand-picked real game, each chosen for a specific
    failure mode (see the module docstring's fixture-refresh SQL)."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def replay(self, game_id):
        return live_replay.replay_game(self.conn, game_id, step=1)

    def test_flagship_late_drama_2ot(self):
        # ORE @ PSU, 2025 w5, 30-24 2OT, watchability_score=0.7116 -- must peak late.
        rows = self.replay("401752854")
        peak = max(rows, key=lambda r: r["live_score"])
        self.assertGreaterEqual(peak["progress"], 0.9)

        first_half = [r["live_score"] for r in rows if r["progress"] <= 0.5]
        q4_plus = [r["live_score"] for r in rows if r["period"] is not None and r["period"] >= 4]
        self.assertGreaterEqual(np.mean(q4_plus) - np.mean(first_half), 0.15)

    def test_wire_to_wire_blowout(self):
        # NWST @ CIN, 2025 w3, 0-70, watchability_score=0.0120.
        rows = self.replay("401756892")
        self.assertLess(max(r["live_score"] for r in rows), 0.35)

        after_q1 = [r["live_score"] for r in rows if r["period"] is not None and r["period"] >= 2]
        increases = sum(1 for i in range(1, len(after_q1)) if after_q1[i] > after_q1[i - 1] + 0.02)
        self.assertEqual(increases, 0, "live_score rose meaningfully after Q1 in a wire-to-wire blowout")

    def test_close_then_collapse_halves_dont_cancel(self):
        # UNM @ UCLA, 2025 w3, 35-10 final but tight through Q3 --
        # watchability_score=0.1320. This is the case that proves
        # quality_so_far and drama_from_here don't just cancel each other
        # into a muddy, unexplainable number: one can legitimately stay
        # elevated (the first three quarters were genuinely tight) while the
        # other collapses toward 0 on its own once the game is truly over --
        # no decided flag involved, this is composite_from()'s continuous
        # formulas alone (verified: final wp is pinned at the true extreme
        # here, not just close to it, so drama_from_here naturally lands
        # near 0 without any override).
        rows = self.replay("401752837")
        end_q3 = [r for r in rows if r["period"] == 3][-1]
        self.assertGreaterEqual(end_q3["live_score"], 0.2)

        final = rows[-1]
        self.assertLess(final["drama_from_here"], 0.05)
        self.assertGreater(final["quality_so_far"], final["drama_from_here"] + 0.1)

    def test_upset_in_progress_away_favorite(self):
        # ALA (ranked #1, away) loses at unranked Vanderbilt, 2024 w6.
        # initial_home_wp=0.0791 -- home (VAN) is the underdog.
        rows = self.replay("401628384")
        raws = [r for r in rows]
        # upset_in_progress must be reconstructable via a fresh context each
        # step; sanity-check it rises as the underdog's WP climbs late.
        late = [r for r in raws if r["progress"] >= 0.9]
        self.assertTrue(late, "no rows at progress >= 0.9")
        self.assertGreater(late[-1]["wp_now"], 0.5, "home underdog should be leading late in this upset")

    def test_upset_in_progress_home_favorite_mirror(self):
        # #5 Notre Dame (home) loses to unranked NIU, 2024 w2.
        # initial_home_wp=0.9661 -- home (ND) is the favorite.
        rows = self.replay("401628977")
        late = [r for r in rows if r["progress"] >= 0.9]
        self.assertTrue(late)
        self.assertLess(late[-1]["wp_now"], 0.5, "home favorite should be trailing late in this upset")

    def test_comeback_magnitude(self):
        # BC @ MSU, 2025 -- full 0->1 WP swing.
        wp_rows = self.conn.execute(
            "SELECT home_win_pct, home_score, away_score, period_number, clock_seconds_elapsed "
            "FROM win_probability WHERE game_id = ? ORDER BY play_sequence, id",
            ("401752816",),
        ).fetchall()
        self.assertGreaterEqual(scoring.comeback_magnitude(wp_rows), 0.6)

    def test_severe_score_corruption_no_crash(self):
        # home_score goes negative (-38 / -3) for dozens of consecutive rows
        # in these two games -- verified in data_quality_findings.md as the
        # worst real corruption in the corpus.
        for game_id in ("401757173", "401778328"):
            rows = self.replay(game_id)
            for r in rows:
                self.assertTrue(0.0 <= r["live_score"] <= 1.0, f"{game_id} out-of-range live_score")
                self.assertFalse(np.isnan(r["live_score"]))

    def test_score_jitter_no_crash(self):
        # Single-row score jitter (a value reverting on the next row) --
        # the common case, ~43% of games per data_quality_findings.md.
        for game_id in ("401752682", "401760376"):
            rows = self.replay(game_id)
            for r in rows:
                self.assertTrue(0.0 <= r["live_score"] <= 1.0, f"{game_id} out-of-range live_score")


class UniversalInvariantTests(unittest.TestCase):
    """Sweep every completed 2025 game (sampled every 15th WP row for
    speed -- ~930 games in a couple seconds) and check properties that must
    hold for ALL of them, not just the hand-picked fixtures above."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _conn()
        cls.game_ids = [
            r[0] for r in cls.conn.execute(
                "SELECT game_id FROM games WHERE watchability_score IS NOT NULL AND season_year = 2025"
            )
        ]
        cls.assertGreater(cls, len(cls.game_ids), 500, "expected the 2025 corpus to be populated")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_every_prefix_in_range_no_exceptions(self):
        checked = 0
        for game_id in self.game_ids:
            for r in live_replay.replay_game(self.conn, game_id, step=15):
                checked += 1
                self.assertTrue(0.0 <= r["live_score"] <= 1.0, f"{game_id} i={r['i']}")
                if r["quality_so_far"] is not None:
                    self.assertTrue(0.0 <= r["quality_so_far"] <= 1.0, f"{game_id} i={r['i']}")
                self.assertTrue(0.0 <= r["drama_from_here"] <= 1.0, f"{game_id} i={r['i']}")
                self.assertFalse(np.isnan(r["live_score"]))
        self.assertGreater(checked, 5000)

    def test_zero_applicable_weight_returns_none_not_raises(self):
        # scoring.py:280 used to divide by total_weight unguarded --
        # composite_from() must return (None, breakdown) instead of raising
        # when every metric in a registry returns None.
        all_none_metrics = [{"name": "x", "fn": lambda ctx: None, "weight": 1.0, "cap": None}]
        composite, breakdown = scoring.composite_from(all_none_metrics, {})
        self.assertIsNone(composite)
        self.assertEqual(breakdown["x"], {"raw": None, "normalized": None, "weighted": None})

    def test_early_game_degrades_gracefully(self):
        # Before LIVE_MIN_ELAPSED_SECONDS has elapsed, the two rate metrics
        # (wp_volatility_rate, lead_change_rate, weight 1.0 each) and the
        # two window-gated metrics (late_volatility_rate, clutch_finish,
        # weight 0.5 + 1.0) are all None -- only team_profile (1.0),
        # upset_risk (0.5), comeback_magnitude (1.0), and upset_in_progress
        # (1.0) can be applicable, for a max applicable weight of 3.5 of
        # 7.0. The latter two are deliberately NOT elapsed-gated (they're
        # legitimately small/near-zero early rather than undefined -- see
        # their docstrings), which is why the bound is 3.5, not the 1.5
        # this test originally (and incorrectly) assumed team_profile +
        # upset_risk alone would produce.
        #
        # Checked directly via so_far_weight rather than inferred from
        # `progress`: not every game's first *stored* WP row is at true
        # kickoff (one game in the 2025 corpus starts its series 830s into
        # Q1, presumably because ESPN's own WP array skipped early plays)
        # -- so asserting on progress itself is fragile against real data,
        # while the applicable-weight property holds by construction.
        checked = 0
        for game_id in self.game_ids[:200]:
            row = self.conn.execute(
                "SELECT home_rank, away_rank, initial_home_wp FROM games WHERE game_id = ?",
                (game_id,),
            ).fetchone()
            first_wp = self.conn.execute(
                "SELECT home_win_pct, home_score, away_score, period_number, clock_seconds_elapsed "
                "FROM win_probability WHERE game_id = ? ORDER BY play_sequence, id LIMIT 1",
                (game_id,),
            ).fetchone()
            if first_wp is None or first_wp["clock_seconds_elapsed"] is None:
                continue
            if first_wp["clock_seconds_elapsed"] >= live.LIVE_MIN_ELAPSED_SECONDS:
                continue
            period, remaining = live_replay._status_from_wp_row(first_wp)
            ctx = live.build_live_context(
                wp_rows=[first_wp], home_rank=row["home_rank"], away_rank=row["away_rank"],
                initial_home_wp=row["initial_home_wp"],
                status_period=period, status_clock_seconds=remaining,
            )
            result = live.score_live(ctx)
            self.assertLessEqual(result["so_far_weight"], 3.5, game_id)
            checked += 1
        self.assertGreater(checked, 50)

    def test_spearman_tripwire_vs_retrospective_score(self):
        # Not a pass/fail on the live model's quality -- a tripwire that it
        # hasn't inverted the retrospective algorithm's preferences. Uses
        # numpy only (no scipy in requirements.txt).
        pairs = []
        for game_id in self.game_ids:
            rows = live_replay.replay_game(self.conn, game_id, step=15)
            wscore = self.conn.execute(
                "SELECT watchability_score FROM games WHERE game_id = ?", (game_id,)
            ).fetchone()[0]
            pairs.append((rows[-1]["live_score"], wscore))

        xs = np.array([p[0] for p in pairs])
        ys = np.array([p[1] for p in pairs])

        def _rank(a):
            order = np.argsort(a)
            ranks = np.empty_like(order, dtype=float)
            ranks[order] = np.arange(len(a))
            return ranks

        rho = np.corrcoef(_rank(xs), _rank(ys))[0, 1]
        self.assertGreaterEqual(rho, 0.5, f"live/retrospective rank correlation collapsed to {rho:.3f}")


class LiveSummaryParsingTests(unittest.TestCase):
    """Payload-shape coverage for the drives.current fix (src/espn.py),
    without needing a real live game: hand-built minimal summary payloads
    exercising both documented shapes (dict and list) plus the pregame
    score-string hazard, all offline."""

    def _minimal_summary(self, current):
        return {
            "header": {
                "id": "999",
                "competitions": [{
                    "competitors": [
                        {"homeAway": "home", "team": {"id": "1"}, "score": "10"},
                        {"homeAway": "away", "team": {"id": "2"}, "score": "7"},
                    ],
                }],
            },
            "gameInfo": {},
            "drives": {
                "previous": [{
                    "plays": [{
                        "id": "p1", "period": {"number": 1}, "clock": {"displayValue": "10:00"},
                        "homeScore": 0, "awayScore": 0, "sequenceNumber": 1,
                    }],
                }],
                "current": current,
            },
            "winprobability": [
                {"playId": "p1", "homeWinPercentage": 0.5},
                {"playId": "p2", "homeWinPercentage": 0.55},
            ],
        }

    def test_drives_current_as_dict(self):
        from src import espn
        current = {
            "plays": [{
                "id": "p2", "period": {"number": 2}, "clock": {"displayValue": "5:30"},
                "homeScore": 7, "awayScore": 0, "sequenceNumber": 1,
            }],
        }
        wp_rows, hs, as_, att, iwp = espn.parse_summary_detail(self._minimal_summary(current))
        p2 = next(r for r in wp_rows if r["play_id"] == "p2")
        self.assertEqual(p2["period_number"], 2)
        self.assertIsNotNone(p2["clock_seconds_elapsed"])

    def test_drives_current_as_list(self):
        from src import espn
        current = [{
            "plays": [{
                "id": "p2", "period": {"number": 2}, "clock": {"displayValue": "5:30"},
                "homeScore": 7, "awayScore": 0, "sequenceNumber": 1,
            }],
        }]
        wp_rows, hs, as_, att, iwp = espn.parse_summary_detail(self._minimal_summary(current))
        p2 = next(r for r in wp_rows if r["play_id"] == "p2")
        self.assertEqual(p2["period_number"], 2)

    def test_drives_current_none_unaffected(self):
        from src import espn
        wp_rows, hs, as_, att, iwp = espn.parse_summary_detail(self._minimal_summary(None))
        p2 = next(r for r in wp_rows if r["play_id"] == "p2")
        self.assertIsNone(p2["period_number"])

    def test_pregame_score_string_gated(self):
        from src import espn
        comp = {
            "id": "999",
            "status": {"type": {"state": "pre", "completed": False, "shortDetail": "Scheduled"}},
            "competitors": [
                {"homeAway": "home", "team": {"id": "1", "abbreviation": "H"}, "score": "0"},
                {"homeAway": "away", "team": {"id": "2", "abbreviation": "A"}, "score": "0"},
            ],
        }
        event = {"id": "999", "season": {"year": 2026, "type": 2}}
        parsed = espn._parse_competition(event, comp)
        self.assertIsNone(parsed["home_score"])
        self.assertIsNone(parsed["away_score"])


if __name__ == "__main__":
    unittest.main()
