"""
Fits the production version of "Model C" (see scripts/fit_wp_situational_model.py
for the original exploratory version and its held-out evaluation) and
generates src/wp_situational.py -- a runtime-dependency-free module, same
pattern as scripts/fit_wp_quarter_model.py -> src/wp_baseline.py.

Differences from the exploratory fit:
  - REGULATION ONLY (period <= 4). No OT plays enter the training set at
    all, and there's no is_ot feature -- this model is never meant to be
    evaluated on OT, matching the decision to stop comeback_erosion from
    scoring into overtime (see plans/algorithm/watchability_algorithm_open_items.md's
    2026-08-31 entries: OT win_probability/play-by-play data has multiple
    confirmed corruption modes, and a real regulation-era comeback getting
    wiped out by a bogus post-tie OT reset is the concrete case that
    triggered this whole redesign -- excluding OT outright is simpler and
    safer than trying to sanitize around it).
  - Fit on the FULL corpus (no train/test split) -- this is the model
    actually going into production, not an accuracy comparison. The
    exploratory script's held-out Brier/log-loss numbers already answered
    "does this generalize" using the identical feature set; a production
    fit should use every available row.
  - Offense-perspective output (predict_wp_offense/coinflip_wp_offense),
    since down/distance/field position are only meaningful from the
    offense's perspective. Callers needing a home-perspective value flip
    the result based on which team is on offense (see src/scoring.py's
    comeback_erosion, which needs exactly this).
  - No goal_to_go feature (dropped 2026-09-01). It's deterministic on
    distance/yards_to_go (goal-to-go is just "distance-to-a-first-down and
    distance-to-the-end-zone have converged"), and once build_dataset()'s
    score/situation pairing fix (see its own docstring) removed a confound
    that had been inflating its apparent effect, a likelihood-ratio test
    showed dropping it costs nothing (pseudo-R^2 0.485748 -> 0.485747,
    p=0.34) -- distance/yards_to_go already capture everything it was
    adding.
  - Training set excludes games that went to overtime entirely (2026-09-01,
    ~4.1% of games, ~4.3% of rows). A play from the 3rd quarter of a game
    that later ties and goes to OT carries a target label (offense_won)
    partly decided by OT's own near-coinflip resolution -- something no
    regulation-era feature can predict -- injecting label noise concentrated
    in exactly the close/late-game region comeback_erosion cares about
    most. Confirmed via scripts/compare_wp_ot_exclusion.py: excluding these
    games costs nothing on held-out accuracy (Brier 0.1049 -> 0.1046 on a
    clean non-OT test slice; 0.1126 -> 0.1127, noise-level, on a test slice
    that still includes OT games) while producing a slightly more decisive
    model (e.g. logit_offense_pregame_wp 0.743 -> 0.780) -- consistent with
    OT games being disproportionately close/back-and-forth and diluting
    what "a comfortable situation" looks like. This is a training-set
    choice only; a regulation play from a game that happens to go to OT is
    still scored normally at inference time (see espn.extract_situational_plays'
    period gate on its running score tracker for the separate,
    inference-side guarantee that OT's outcome can never leak into a
    regulation play's own reading).
  - Endgame urgency now runs on a DISCRETE scores_needed count, not the raw
    continuous score_diff (2026-09-02). Prompted by three games where the
    coin-flip chart stayed mushy through the final snap of an already-
    decided game (401525861, 401520413, 401442015) and a direct empirical
    check (scripts/validate_endgame_lead_win_rate.py): teams leading by
    exactly 1, 2, or 3 points with a fresh set of downs and <=30s left
    actually win 93.0%/100.0%/93.5% of the time in real games (n=43/30/77),
    while a first-pass fix (adding cubic + inverse-sqrt urgency terms to
    the OLD continuous score_diff -- see scripts/compare_wp_endgame_calibration.py's
    design_cubic_plus_inv_sqrt) still only predicted 80.1%/89.2%/93.2% for
    those same situations. The reason: football scoring is discrete (a
    field goal is always exactly 3 points), so a 1-point deficit and a
    2-point deficit are functionally IDENTICAL -- both erased by the same
    single made field goal -- but every continuous-score_diff urgency term
    spread them apart as if they carried meaningfully different
    information. `scores_needed()` buckets any deficit of 1-8 (up to one
    TD+2pt, the max in a single possession) into a single discrete count.
    On a full-corpus fit, this doesn't just help -- it fully SUBSUMES the
    old continuous urgency story: every raw-score_diff urgency term became
    statistically non-significant (p=0.18-0.62) once the scores_needed
    terms were added, so this feature set replaces them outright rather
    than adding alongside. Closes nearly all of the calibration gap on the
    narrow-lead slice (92.5% predicted vs. 92.0% actual, held-out) at the
    cost of a barely-measurable whole-game brier regression (0.1068->0.1071
    on a held-out comparison) that doesn't reach anywhere close to
    significance at this sample size. See scripts/compare_wp_endgame_calibration.py
    for the full candidate comparison and scripts/compare_wp_vs_espn.py for
    how this stacks up against ESPN's own live WP (closes most, not all, of
    that gap too -- ESPN remains sharper in the literal final seconds,
    likely from real-time inputs like timeouts that aren't in this dataset).

Requires numpy/pandas/statsmodels (dev-only, see requirements-dev.txt) --
deliberately NOT a runtime dependency of src/wp_situational.py itself.

Usage:
    venv/bin/python3 scripts/build_wp_situational_module.py [path/to/cfb.db]
"""
import datetime
import math
import sys

import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, ".")

from src import db, espn

EPS = 1e-4
MAX_DISTANCE = 30
OUTPUT_PATH = "src/wp_situational.py"


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def scores_needed(score_diff):
    """Signed 'possessions needed' -- football scoring is discrete (FG=3,
    TD=6/7/8), so a 1-point deficit and a 2-point deficit are functionally
    identical (either is erased by a single made field goal); a continuous
    score_diff-based urgency term treating them as meaningfully different
    magnitudes is exactly backwards for the endgame. Maps any deficit of
    1-8 (up to a single TD+2pt, the max obtainable in one possession) to
    "1 score", 9-16 to "2 scores", etc. Sign preserved so a trailing
    offense reads negative; zero maps to zero (tied)."""
    if score_diff == 0:
        return 0
    return math.copysign(math.ceil(abs(score_diff) / 8.0), score_diff)


def build_dataset(conn):
    """Regulation-only (period <= 4) scrimmage-down plays, offense
    perspective, target = actual game outcome. Uses the same
    espn.extract_situational_plays() the production model consumes at
    inference time (src/scoring.py's comeback_erosion, serve.py's chart
    toggle) -- previously this duplicated that extraction logic inline and
    drifted out of sync with it: this script paired each play's down/
    distance with that SAME play's own (already-updated) post-play score,
    while the production extractor got fixed to use the score as of the
    snap (see src/espn.py's 2026-08-31 fix, commit 8815ce7 -- a scoring
    play's own homeScore/awayScore already includes its own points, an
    impossible combination with its pre-snap down/distance). Reusing the
    shared extractor here means the training set and the model's actual
    runtime inputs can never drift apart like that again.

    Synthetic closing entries (play_id=="" -- extract_situational_plays'
    end-of-game safety net for comeback_erosion's arc-walk, not a real
    play) are excluded: they duplicate the prior play's situational
    reading under a different score, which isn't a real independent
    observation for a play-level model to train on.

    Games that went to overtime are excluded entirely (see the module
    docstring's "Training set excludes..." entry) -- their regulation
    plays' own outcome label is partly decided by OT's near-coinflip
    resolution, not by anything a regulation-era feature could predict."""
    games = conn.execute("""
        SELECT game_id, home_team_id, home_score, away_score, initial_home_wp
        FROM games g
        WHERE completed = 1 AND detail_fetched = 1
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND home_score != away_score
          AND initial_home_wp IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM win_probability wp
              WHERE wp.game_id = g.game_id AND wp.period_number > 4
          )
    """).fetchall()

    rows = []
    n_games_used = 0
    for g in games:
        raw = db.get_game_raw_json(conn, g["game_id"])
        if not raw:
            continue
        n_games_used += 1
        home_won = 1 if g["home_score"] > g["away_score"] else 0

        for play in espn.extract_situational_plays(raw, g["home_team_id"]):
            if not play["play_id"]:
                continue  # synthetic closing entry, not a real play

            off_is_home = play["off_is_home"]
            offense_score = play["home_score"] if off_is_home else play["away_score"]
            defense_score = play["away_score"] if off_is_home else play["home_score"]
            offense_pregame_wp = g["initial_home_wp"] if off_is_home else 1 - g["initial_home_wp"]
            offense_won = home_won if off_is_home else 1 - home_won
            seconds_remaining_reg = max(0, 3600 - play["elapsed_seconds"])

            rows.append({
                "down": play["down"],
                "distance": min(play["distance"], MAX_DISTANCE),
                "yards_to_go": play["yards_to_go"],
                "score_diff": offense_score - defense_score,
                "seconds_remaining_reg": seconds_remaining_reg,
                "offense_pregame_wp": offense_pregame_wp,
                "offense_won": offense_won,
            })

    return pd.DataFrame(rows), n_games_used, len(games)


def make_design(df):
    """Round-2 feature set (2026-09-02): scores_needed-based urgency
    replaces the old continuous score_diff-based urgency entirely (not
    additive -- see the module docstring's significance-test finding).
    `sn_urgency2` (the quadratic scores_needed term) is deliberately
    omitted -- a full-corpus fit found it non-significant (p=0.223) once
    the linear/cubic/inv-sqrt scores_needed terms were present, and
    dropping it changed held-out metrics by nothing (matching the existing
    goal_to_go-drop precedent above)."""
    time_frac = df["seconds_remaining_reg"] / 3600.0
    sn = df["score_diff"].map(scores_needed)
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": df["score_diff"],
        "time_remaining_frac": time_frac,
        "down2": (df["down"] == 2).astype(int),
        "down3": (df["down"] == 3).astype(int),
        "down4": (df["down"] == 4).astype(int),
        "distance": df["distance"],
        "yards_to_go": df["yards_to_go"],
        "sn_urgency": sn * (1 - time_frac),
        "sn_urgency3": sn * (1 - time_frac) ** 3,
        "sn_time_remaining_frac2": time_frac ** 2,
        "sn_inv_sqrt_urgency": sn / np.sqrt(df["seconds_remaining_reg"] + 5.0),
    })
    return sm.add_constant(X, has_constant="add")


def fit(df):
    X = make_design(df)
    y = df["offense_won"]
    return sm.Logit(y, X).fit(disp=0, maxiter=100)


MODULE_TEMPLATE = '''"""
AUTO-GENERATED by scripts/build_wp_situational_module.py on {generated_at} --
do not hand-edit. Re-run that script (after pulling more seasons, etc.) to
regenerate this file; it always overwrites, not patches.

Fitted on {n_games} completed games, regulation plays only (period 1-4,
{n_rows} scrimmage-down plays) -- OT is deliberately excluded from both
training and the intended use of this module (see the fitting script's
docstring). Offense-perspective logistic regression (target = actual game
outcome, not ESPN's own WP) -- McFadden pseudo-R^2={pseudo_r2:.4f}.

Endgame urgency runs on a DISCRETE scores_needed count (see
scores_needed()'s own docstring), not the raw continuous score_diff --
football only scores in fixed increments (FG=3, TD=6/7/8), so a 1-point
and 2-point deficit are functionally identical and a continuous term
spreading them apart was underselling how safe a narrow late lead really
is (see scripts/compare_wp_endgame_calibration.py and
scripts/validate_endgame_lead_win_rate.py for the full investigation and
held-out validation).

predict_wp_offense()/coinflip_wp_offense() take the offense's own down/
distance/field position plus score/time/pregame WP and return the win
probability for the team CURRENTLY ON OFFENSE. Callers needing a
home-perspective value must flip the result based on which team has the
ball -- see src/scoring.py's comeback_erosion for the wrapper that does
this.
"""
import math

MODEL = {{
{coef_entries}
}}


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def inv_logit(x):
    return 1 / (1 + math.exp(-x))


def scores_needed(score_diff):
    """Signed 'possessions needed' -- see build_wp_situational_module.py's
    identical helper for the full rationale. Any deficit of 1-8 (up to a
    single TD+2pt) maps to "1 score", 9-16 to "2 scores", etc.; sign
    preserved so a trailing offense reads negative."""
    if score_diff == 0:
        return 0
    return math.copysign(math.ceil(abs(score_diff) / 8.0), score_diff)


def predict_wp_offense(*, down, distance, yards_to_go,
                        score_diff, elapsed_seconds, offense_pregame_wp):
    """Win probability for the team on offense, given their own down/
    distance/field position, the score (offense - defense), elapsed game
    seconds (0-3600, regulation only -- do not call this for OT plays),
    and their own pregame win probability."""
    m = MODEL
    time_remaining_frac = max(0.0, 3600 - elapsed_seconds) / 3600.0
    sn = scores_needed(score_diff)
    l_pred = (m["const"]
              + m["b_logit_offense_pregame_wp"] * logit(offense_pregame_wp)
              + m["b_score_diff"] * score_diff
              + m["b_time_remaining_frac"] * time_remaining_frac
              + m["b_down2"] * (1 if down == 2 else 0)
              + m["b_down3"] * (1 if down == 3 else 0)
              + m["b_down4"] * (1 if down == 4 else 0)
              + m["b_distance"] * min(distance, {max_distance})
              + m["b_yards_to_go"] * yards_to_go
              + m["b_sn_urgency"] * sn * (1 - time_remaining_frac)
              + m["b_sn_urgency3"] * sn * (1 - time_remaining_frac) ** 3
              + m["b_sn_time_remaining_frac2"] * time_remaining_frac ** 2
              + m["b_sn_inv_sqrt_urgency"] * sn / math.sqrt(max(0.0, 3600 - elapsed_seconds) + 5.0))
    return inv_logit(l_pred)


def coinflip_wp_offense(*, down, distance, yards_to_go,
                         score_diff, elapsed_seconds):
    """predict_wp_offense() with the offense's own pregame WP forced to a
    50/50 coin flip -- the anchor-free scale to judge an in-game swing
    against, same rationale as wp_baseline.coinflip_wp_elapsed()."""
    return predict_wp_offense(
        down=down, distance=distance, yards_to_go=yards_to_go,
        score_diff=score_diff, elapsed_seconds=elapsed_seconds, offense_pregame_wp=0.5,
    )
'''

COEF_TEMPLATE = '    "{name}": {value!r},'


def write_module(model, n_games, n_rows, pseudo_r2):
    param_map = {
        "const": "const",
        "logit_offense_pregame_wp": "b_logit_offense_pregame_wp",
        "score_diff": "b_score_diff",
        "time_remaining_frac": "b_time_remaining_frac",
        "down2": "b_down2",
        "down3": "b_down3",
        "down4": "b_down4",
        "distance": "b_distance",
        "yards_to_go": "b_yards_to_go",
        "sn_urgency": "b_sn_urgency",
        "sn_urgency3": "b_sn_urgency3",
        "sn_time_remaining_frac2": "b_sn_time_remaining_frac2",
        "sn_inv_sqrt_urgency": "b_sn_inv_sqrt_urgency",
    }
    entries = "\n".join(
        COEF_TEMPLATE.format(name=out_name, value=float(model.params[in_name]))
        for in_name, out_name in param_map.items()
    )
    content = MODULE_TEMPLATE.format(
        generated_at=datetime.date.today().isoformat(),
        n_games=n_games,
        n_rows=n_rows,
        pseudo_r2=model.prsquared,
        coef_entries=entries,
        max_distance=MAX_DISTANCE,
    )
    with open(OUTPUT_PATH, "w") as f:
        f.write(content)
    print(f"Wrote {OUTPUT_PATH}")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building regulation-only play-level dataset (full corpus, no train/test split)...")
    df, n_games_used, n_games_total = build_dataset(conn)
    print(f"Games with raw JSON available: {n_games_used}/{n_games_total}")
    print(f"Regulation scrimmage-down plays: {len(df)}\n")

    print("Fitting situational logistic model (offense perspective) on the full corpus...")
    model = fit(df)
    print(model.summary())

    write_module(model, n_games_used, len(df), model.prsquared)


if __name__ == "__main__":
    main()
