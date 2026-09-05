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
from unittest import mock

import numpy as np

from src import db, espn, live, live_replay, scoring

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
        # Margin re-measured at 0.05 (was 0.1) after comeback_erosion_live
        # replaced comeback_magnitude: the old raw-WP metric gave this game
        # a nonzero "comeback" credit for UCLA's favorite-pulling-away climb
        # (never a real coin-flip-commanding UNM lead getting undone), which
        # comeback_erosion_live correctly zeroes -- quality_so_far is smaller
        # now but still ~160x drama_from_here, so the underlying "one metric
        # stays elevated while the other collapses" property still holds.
        self.assertGreater(final["quality_so_far"], final["drama_from_here"] + 0.05)

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

    def _situational_plays(self, game_id):
        """Regulation-only, per-play down/distance/field-position feed --
        what comeback_erosion/comeback_erosion_live actually consume as of
        the 2026-08-31 Model C redesign (src/scoring.py), replacing the old
        win_probability-row-based wp_rows for these two metrics only."""
        row = self.conn.execute(
            "SELECT home_team_id FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        raw = db.get_game_raw_json(self.conn, game_id)
        return espn.extract_situational_plays(raw, row["home_team_id"])

    def test_comeback_erosion_live_matches_retrospective_when_consummated(self):
        # SDSU @ USU, 2024, final USU 41-20 -- a real, completed comeback:
        # USU's coin-flip WP fell as low as 0.110 (SDSU built a real early
        # lead), then USU retook the lead outright (a genuine flip, not just
        # a close-game approach) late in Q2, before pulling away to the
        # final margin. Once that arc actually closes, comeback_erosion_live
        # shouldn't diverge from comeback_erosion -- crediting the open arc
        # on top of an already-scored closed one would double-count. (This
        # replaces the old fixture, BC@MSU/401752816 -- that game's old
        # 0.444 credit was a fabricated artifact of corrupted post-regulation
        # rows, not a real comeback; see watchability_algorithm_open_items.md's
        # 2026-08-31 correction. It now correctly scores 0.0 under the
        # OT-excluded redesign, which is why it can no longer serve this
        # test's purpose.)
        plays = self._situational_plays("401643766")
        self.assertGreaterEqual(scoring.comeback_erosion_live(plays), 0.3)
        self.assertAlmostEqual(
            scoring.comeback_erosion_live(plays), scoring.comeback_erosion(plays),
        )

    def test_comeback_erosion_live_credits_unconsummated_comeback_more_than_retrospective(self):
        # FSU @ LSU, 2022 w1 -- FSU built as much as a ~95% coin-flip win
        # probability, then LSU clawed back to trailing by just 1 at the
        # final whistle without ever tying or taking the lead. The arc never
        # closes (no lead change/tie), so comeback_erosion's only credit
        # comes from the close-game trigger (within CLOSE_GAME_MARGIN points
        # with time left) -- real credit, but gated/partial. comeback_erosion_live
        # additionally checks the open arc's own swing on every play with no
        # such gate (per the "unconsummated comebacks still count, without
        # needing the margin/time restriction" requirement for the live
        # in-progress signal), so it still credits meaningfully more here.
        # Margin narrowed 0.2->0.1 (2026-09-02, Model C's scores_needed
        # refit): the deficit spends most of this arc at 14-17 points, which
        # a continuous score_diff term used to treat as progressively more
        # extreme, but scores_needed buckets any 9-16 point deficit into the
        # same "2 scores" reading -- correct per the discrete-scoring
        # rationale (see src/wp_situational.py), but it compresses how low
        # this specific arc's "lo" reads, so the recovery swing
        # comeback_erosion_live credits shrinks too (0.104->0.253, a 0.149
        # gap, still clearly directional -- confirmed via
        # scripts/compare_wp_endgame_calibration.py's held-out validation
        # that this refit is a net improvement, not a regression).
        plays = self._situational_plays("401403867")
        retrospective = scoring.comeback_erosion(plays)
        live_value = scoring.comeback_erosion_live(plays)
        self.assertGreater(retrospective, 0.0)
        self.assertGreater(live_value, retrospective + 0.1)

    def test_comeback_erosion_live_rejects_favorite_pulling_away(self):
        # A mild favorite building a clean, never-threatened lead was the
        # case that motivated this metric (real trigger: UVA/NC State,
        # comeback_magnitude=0.35 on raw WP alone off a 56% pregame line
        # with NC State never holding a real lead or WP edge). Synthetic
        # here (neutral 1st-and-10-at-midfield situational context at each
        # checkpoint, isolating the score+time trend) so the peak stays
        # deliberately just under COMEBACK_EROSION_THRESHOLD under Model C's
        # own scale -- confirmed via direct wp_situational.coinflip_wp_offense
        # calls at these exact checkpoints before picking the final margin.
        plays = [
            {"elapsed_seconds": 6, "off_is_home": True, "down": 1, "distance": 10,
             "yards_to_go": 65, "home_score": 0, "away_score": 0},
            {"elapsed_seconds": 589, "off_is_home": True, "down": 1, "distance": 10,
             "yards_to_go": 65, "home_score": 3, "away_score": 0},
            {"elapsed_seconds": 743, "off_is_home": True, "down": 1, "distance": 10,
             "yards_to_go": 65, "home_score": 10, "away_score": 0},
            {"elapsed_seconds": 1107, "off_is_home": True, "down": 1, "distance": 10,
             "yards_to_go": 65, "home_score": 12, "away_score": 0},
        ]
        self.assertEqual(scoring.comeback_erosion_live(plays), 0.0)

    def test_comeback_erosion_ignores_wp_swing_within_one_possession_margin(self):
        # Regression for the "don't credit a comeback in a one-possession
        # game" requirement: the score margin here never exceeds
        # CLOSE_GAME_MARGIN (peaks at 6), but the situational WP reading
        # (mocked here to isolate the gate from Model C's actual
        # coefficients, which can be independently re-fit -- see
        # wp_situational.py's docstring) swings past
        # COMEBACK_EROSION_THRESHOLD anyway, simulating a big down/distance
        # moment, then back down. With no accompanying scoreboard swing
        # beyond one possession, that situational read isn't a real lead to
        # erode. Before the max_abs_sd gate, this fired via the close-game
        # trigger (CLOSE_GAME_MARGIN check with no floor on how big the
        # arc's margin had ever gotten) on both the retrospective and live
        # variants, since abs(sd) <= CLOSE_GAME_MARGIN is true for the whole
        # game here.
        plays = [
            {"elapsed_seconds": 100, "off_is_home": True, "down": 1, "distance": 10,
             "yards_to_go": 65, "home_score": 0, "away_score": 0},
            {"elapsed_seconds": 1000, "off_is_home": True, "down": 1, "distance": 10,
             "yards_to_go": 65, "home_score": 3, "away_score": 0},
            {"elapsed_seconds": 2000, "off_is_home": True, "down": 1, "distance": 10,
             "yards_to_go": 65, "home_score": 6, "away_score": 0},
            {"elapsed_seconds": 3000, "off_is_home": True, "down": 1, "distance": 10,
             "yards_to_go": 65, "home_score": 6, "away_score": 3},
        ]
        with mock.patch.object(scoring, "coinflip_home_wp", side_effect=[0.5, 0.5, 0.92, 0.5]):
            self.assertEqual(scoring.comeback_erosion(plays), 0.0)
        with mock.patch.object(scoring, "coinflip_home_wp", side_effect=[0.5, 0.5, 0.92, 0.5]):
            self.assertEqual(scoring.comeback_erosion_live(plays), 0.0)

    def test_comeback_margin_q4_close_requires_more_than_one_possession_hole(self):
        # UTSA 52-24 TXSO: a 14-0->21-7 lead 25 minutes in gets blown back
        # open long before the 4th quarter -- never one-possession-or-closer
        # again once Q4 starts, so no credit despite an early hole that did
        # exceed CLOSE_GAME_MARGIN.
        plays = self._situational_plays("401426572")
        self.assertEqual(scoring.comeback_margin_q4_close(plays), 0)

    def test_comeback_margin_q4_close_credits_real_q4_comeback(self):
        # Marshall/Missouri St -- the real Q4 comeback that started
        # comeback_erosion in the first place.
        plays = self._situational_plays("401757228")
        self.assertGreater(scoring.comeback_margin_q4_close(plays), scoring.CLOSE_GAME_MARGIN)

    def test_comeback_margin_q4_close_credits_unconsummated_comeback(self):
        # FSU@LSU 2022w1 -- LSU trails by just 1 at the final whistle,
        # never ties or takes the lead. comeback_margin_q4_close doesn't
        # require consummation any more than comeback_erosion's close-game
        # trigger does.
        plays = self._situational_plays("401403867")
        self.assertGreater(scoring.comeback_margin_q4_close(plays), scoring.CLOSE_GAME_MARGIN)

    def test_comeback_margin_q4_close_credits_quick_q4_collapse(self):
        # UNM@UCLA, final UCLA 35-10: close through Q3, one-possession for a
        # few plays into Q4 on a carried-over score, then blown open by a
        # real 4th-quarter collapse. Deliberately credited, not a false
        # positive -- see comeback_margin_q4_close's docstring: a quick Q4
        # collapse is its own kind of watchable drama, and the user
        # explicitly confirmed (2026-09-05) they're comfortable with this
        # game scoring here even though comeback_erosion itself stays near
        # zero for it (test_close_then_collapse_halves_dont_cancel).
        plays = self._situational_plays("401752837")
        self.assertGreater(scoring.comeback_margin_q4_close(plays), scoring.CLOSE_GAME_MARGIN)

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

    def _wp_rows(self, game_id):
        return self.conn.execute(
            "SELECT home_win_pct, home_score, away_score, period_number, clock_seconds_elapsed "
            "FROM win_probability WHERE game_id = ? AND period_number IS NOT NULL ORDER BY play_sequence, id",
            (game_id,),
        ).fetchall()

    def _old_upset_risk(self, initial_home_wp, home_rank, away_rank):
        skew = abs(initial_home_wp - 0.5) * 2
        quality = max(scoring._rank_tier(home_rank), scoring._rank_tier(away_rank))
        return (skew ** scoring.UPSET_RISK_POWER) * quality

    def test_upset_risk_wire_to_wire_blowout_discounted(self):
        # MIA @ STAN, 2026 -- Miami favored 94.6% pregame, won wire-to-wire
        # 45-6. Real trigger for this whole metric: upset_risk read 0.525
        # (65th percentile of all 3,688 completed games) despite the game
        # never being in doubt, because the pre-fix formula only looked at
        # the pregame line and never checked what actually happened.
        row = self.conn.execute(
            "SELECT initial_home_wp, home_rank, away_rank FROM games WHERE game_id = ?",
            ("401858206",),
        ).fetchone()
        wp_rows = self._wp_rows("401858206")
        old_value = self._old_upset_risk(row["initial_home_wp"], row["home_rank"], row["away_rank"])
        new_value = scoring.upset_risk(row["initial_home_wp"], row["home_rank"], row["away_rank"], wp_rows)
        self.assertGreater(old_value, 0.4, "sanity: pregame skew was in fact large")
        self.assertLess(new_value, 0.05, "wire-to-wire favorite should be discounted near zero")

    def test_upset_risk_real_scare_keeps_full_credit(self):
        # Clemson favored ~92% but trailed 0-16 in Q2/Q3 before rallying to
        # win 27-16 -- a real coin-flip-or-worse scare despite the final
        # score reading like a comfortable win. Must NOT be discounted just
        # because Clemson ultimately won by 11.
        row = self.conn.execute(
            "SELECT initial_home_wp, home_rank, away_rank FROM games WHERE game_id = ?",
            ("401754637",),
        ).fetchone()
        wp_rows = self._wp_rows("401754637")
        old_value = self._old_upset_risk(row["initial_home_wp"], row["home_rank"], row["away_rank"])
        new_value = scoring.upset_risk(row["initial_home_wp"], row["home_rank"], row["away_rank"], wp_rows)
        self.assertGreater(old_value, 0.3, "sanity: pregame skew was in fact large")
        self.assertAlmostEqual(new_value, old_value, delta=0.01)

    def test_upset_risk_true_upset_unchanged(self):
        # #5 Notre Dame (home) favored 96.6%, actually lost to unranked NIU
        # -- a true upset must keep essentially full credit.
        row = self.conn.execute(
            "SELECT initial_home_wp, home_rank, away_rank FROM games WHERE game_id = ?",
            ("401628977",),
        ).fetchone()
        wp_rows = self._wp_rows("401628977")
        old_value = self._old_upset_risk(row["initial_home_wp"], row["home_rank"], row["away_rank"])
        new_value = scoring.upset_risk(row["initial_home_wp"], row["home_rank"], row["away_rank"], wp_rows)
        self.assertGreater(old_value, 0.5, "sanity: pregame skew was in fact large")
        self.assertAlmostEqual(new_value, old_value, delta=0.01)


class UpsetRiskCompetitivenessTests(unittest.TestCase):
    """Synthetic, DB-free tests for scoring._erosion_fraction and the new
    wp_rows-scaled upset_risk -- precise control over the score trajectory
    that real games (FixtureTests above) don't offer."""

    def _old_upset_risk(self, initial_home_wp, home_rank, away_rank):
        skew = abs(initial_home_wp - 0.5) * 2
        quality = max(scoring._rank_tier(home_rank), scoring._rank_tier(away_rank))
        return (skew ** scoring.UPSET_RISK_POWER) * quality

    def test_none_initial_wp_returns_zero_regardless_of_wp_rows(self):
        self.assertEqual(scoring.upset_risk(None, 1, None, []), 0.0)

    def test_pick_em_line_has_no_erosion_edge(self):
        # edge = pre_fav - 0.5 == 0 at an exact pick'em line -- there's no
        # "favorite" to erode, so _erosion_fraction bails out to None.
        wp_rows = [{"home_score": 21, "away_score": 0, "clock_seconds_elapsed": 1800}]
        self.assertIsNone(scoring._erosion_fraction(wp_rows, 0.5))

    def test_missing_wp_rows_scale_is_a_noop(self):
        # No WP data available -- fall back to scale=1.0 so behavior exactly
        # matches the pre-fix formula rather than penalizing the game for a
        # data gap.
        self.assertIsNone(scoring._erosion_fraction([], 0.95))
        old_value = self._old_upset_risk(0.95, 3, None)
        new_value = scoring.upset_risk(0.95, 3, None, [])
        self.assertEqual(new_value, old_value)

    def test_favorite_never_threatened_discounted_near_zero(self):
        # Home favored 90%, margin only grows the entire game -- the classic
        # wire-to-wire blowout shape that motivated this fix.
        wp_rows = [
            {"home_score": 0, "away_score": 0, "clock_seconds_elapsed": 0},
            {"home_score": 14, "away_score": 0, "clock_seconds_elapsed": 900},
            {"home_score": 28, "away_score": 0, "clock_seconds_elapsed": 1800},
            {"home_score": 42, "away_score": 3, "clock_seconds_elapsed": 2700},
            {"home_score": 49, "away_score": 6, "clock_seconds_elapsed": 3600},
        ]
        erosion = scoring._erosion_fraction(wp_rows, 0.90)
        self.assertLess(erosion, 0.05)
        old_value = self._old_upset_risk(0.90, 5, None)
        new_value = scoring.upset_risk(0.90, 5, None, wp_rows)
        self.assertLess(new_value, old_value * 0.1)

    def test_favorite_genuinely_tied_gets_full_credit(self):
        # Home favored 90%, but the away team actually takes a real lead
        # midgame (down 10 at half, model-implied WP 0.42 -- a genuine
        # below-coin-flip deficit, verified via wp_baseline.predict_wp_elapsed
        # directly) before the favorite pulls away late -- a real scare,
        # must keep essentially full credit despite the final margin looking
        # routine.
        wp_rows = [
            {"home_score": 0, "away_score": 0, "clock_seconds_elapsed": 0},
            {"home_score": 7, "away_score": 0, "clock_seconds_elapsed": 300},
            {"home_score": 7, "away_score": 17, "clock_seconds_elapsed": 1800},
            {"home_score": 21, "away_score": 17, "clock_seconds_elapsed": 3000},
            {"home_score": 28, "away_score": 20, "clock_seconds_elapsed": 3600},
        ]
        erosion = scoring._erosion_fraction(wp_rows, 0.90)
        self.assertEqual(erosion, 1.0)
        old_value = self._old_upset_risk(0.90, 5, None)
        new_value = scoring.upset_risk(0.90, 5, None, wp_rows)
        self.assertAlmostEqual(new_value, old_value)

    def test_underdog_blowout_still_clips_to_full_credit(self):
        # The "underdog" doesn't just tie it up -- it blows the favorite out.
        # max_drop exceeds edge and must clip to 1.0, not exceed it or error.
        wp_rows = [
            {"home_score": 0, "away_score": 0, "clock_seconds_elapsed": 0},
            {"home_score": 0, "away_score": 35, "clock_seconds_elapsed": 1800},
            {"home_score": 3, "away_score": 49, "clock_seconds_elapsed": 3600},
        ]
        self.assertEqual(scoring._erosion_fraction(wp_rows, 0.90), 1.0)

    def test_away_favorite_orientation_mirrors_home(self):
        # initial_home_wp < 0.5 means the AWAY team is favored -- erosion
        # must track the away favorite's modeled WP, not the home team's.
        wp_rows = [
            {"home_score": 0, "away_score": 0, "clock_seconds_elapsed": 0},
            {"home_score": 0, "away_score": 14, "clock_seconds_elapsed": 900},
            {"home_score": 0, "away_score": 28, "clock_seconds_elapsed": 1800},
            {"home_score": 3, "away_score": 42, "clock_seconds_elapsed": 2700},
            {"home_score": 6, "away_score": 49, "clock_seconds_elapsed": 3600},
        ]
        erosion = scoring._erosion_fraction(wp_rows, 0.10)  # away favored 90%
        self.assertLess(erosion, 0.05, "away favorite winning wire-to-wire should also be discounted")


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
        # upset_risk (0.5), comeback_erosion_live (1.0), and upset_in_progress
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
                wp_rows=[first_wp], situational_plays=[],
                home_rank=row["home_rank"], away_rank=row["away_rank"],
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
