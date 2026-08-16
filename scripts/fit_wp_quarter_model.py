"""
Fits two win-probability baseline models against completed, detail-fetched
games, and (re)generates src/wp_baseline.py from the result:

1. QUARTER_MODELS: logit(wp) ~ logit(initial_home_wp) + score_diff, fit
   separately at the Q1/Q2/Q3 end-of-quarter checkpoints.
2. ELAPSED_MODEL: the continuous-time counterpart -- same log-odds
   regression, but with score_diff/elapsed and initial_wp/elapsed quadratic
   interactions instead of discrete quarter buckets, fit on every Q1-3 WP
   row (not just the 3 checkpoints). Verified this predicts the same
   quarter-end values as QUARTER_MODELS almost exactly despite the
   individual coefficients not matching term-for-term -- one continuous
   model substitutes for three. Kept quadratic (not just linear-interaction)
   specifically because the quadratic terms are highly significant and show
   real, accelerating sensitivity growth, which is the right shape to
   extrapolate into Q4/OT (that extrapolation is accepted but not
   independently verified -- there's no Q4/OT training data at all).

Log-odds space handles win probability's [0,1] boundary compression
naturally: a fixed score margin shifts log-odds by roughly a constant
amount regardless of how extreme the pregame line was, which -- once
converted back to probability space -- reproduces the empirical fact that
a heavy favorite's WP barely moves for a modest lead it was already
expected to have, while the same lead moves a coinflip game's WP a lot.
See plans/algorithm/ (or ask -- this was developed interactively) for the
comeback-detection motivation.

Requires numpy/pandas/statsmodels -- deliberately NOT a runtime dependency
of the scoring pipeline, so this stays a standalone script rather than a
pipeline.py flag. Re-run this after pulling additional seasons; it always
regenerates src/wp_baseline.py from scratch (not a diff/patch).

Usage:
    venv/bin/python3 scripts/fit_wp_quarter_model.py
"""
import datetime

import numpy as np
import pandas as pd
import statsmodels.api as sm

from src import db

EPS = 1e-4
QUARTERS = (1, 2, 3)
OUTPUT_PATH = "src/wp_baseline.py"


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def build_dataset(conn):
    games = conn.execute("""
        SELECT game_id, season_year, initial_home_wp
        FROM games
        WHERE completed = 1 AND detail_fetched = 1
          AND initial_home_wp IS NOT NULL
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    """).fetchall()

    rows = []
    seasons = set()
    for g in games:
        seasons.add(g["season_year"])
        init = g["initial_home_wp"]
        wp_rows = conn.execute(
            "SELECT home_win_pct, home_score, away_score, period_number "
            "FROM win_probability WHERE game_id = ? ORDER BY play_sequence, id",
            (g["game_id"],),
        ).fetchall()
        if not wp_rows:
            continue
        for q in QUARTERS:
            candidates = [r for r in wp_rows if r["period_number"] == q]
            if not candidates:
                continue
            r = candidates[-1]
            if r["home_score"] is None or r["away_score"] is None:
                continue
            rows.append({
                "quarter": q,
                "initial_wp": init,
                "wp": r["home_win_pct"],
                "score_diff": r["home_score"] - r["away_score"],
            })
    return pd.DataFrame(rows), min(seasons), max(seasons), len(games)


def fit_models(df):
    fitted = {}
    for q in QUARTERS:
        sub = df[df["quarter"] == q]
        X = sm.add_constant(pd.DataFrame({
            "logit_initial_wp": logit(sub["initial_wp"]),
            "score_diff": sub["score_diff"],
        }))
        y = logit(sub["wp"])
        model = sm.OLS(y, X).fit()
        fitted[q] = {
            "const": float(model.params["const"]),
            "b_logit_initial_wp": float(model.params["logit_initial_wp"]),
            "b_score_diff": float(model.params["score_diff"]),
            "r_squared": float(model.rsquared),
            "n": int(len(sub)),
        }
        print(f"Q{q}: n={len(sub)}  R2={model.rsquared:.3f}  "
              f"const={fitted[q]['const']:+.4f}  "
              f"b_logit_initial_wp={fitted[q]['b_logit_initial_wp']:+.4f}  "
              f"b_score_diff={fitted[q]['b_score_diff']:+.4f}")
    return fitted


def build_elapsed_dataset(conn):
    """Every individual Q1-3 win_probability row (not just the 3 quarter-end
    checkpoints) -- elapsed time is continuous here, so there's no reason to
    throw away the in-between rows the way build_dataset() does."""
    games = conn.execute("""
        SELECT game_id, initial_home_wp
        FROM games
        WHERE completed = 1 AND detail_fetched = 1
          AND initial_home_wp IS NOT NULL
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    """).fetchall()

    rows = []
    for g in games:
        init = g["initial_home_wp"]
        wp_rows = conn.execute(
            "SELECT home_win_pct, home_score, away_score, period_number, clock_seconds_elapsed "
            "FROM win_probability WHERE game_id = ? ORDER BY play_sequence, id",
            (g["game_id"],),
        ).fetchall()
        for r in wp_rows:
            if r["period_number"] not in QUARTERS:
                continue
            if r["home_score"] is None or r["away_score"] is None or r["clock_seconds_elapsed"] is None:
                continue
            rows.append({
                "elapsed_q": r["clock_seconds_elapsed"] / 900.0,
                "initial_wp": init,
                "wp": r["home_win_pct"],
                "score_diff": r["home_score"] - r["away_score"],
            })
    return pd.DataFrame(rows)


def fit_elapsed_model(df):
    li = logit(df["initial_wp"])
    eq = df["elapsed_q"]
    sd = df["score_diff"]
    X = sm.add_constant(pd.DataFrame({
        "logit_initial_wp": li,
        "score_diff": sd,
        "elapsed_q": eq,
        "elapsed_q2": eq ** 2,
        "sd_x_elapsed": sd * eq,
        "sd_x_elapsed2": sd * eq ** 2,
        "init_x_elapsed": li * eq,
        "init_x_elapsed2": li * eq ** 2,
    }))
    y = logit(df["wp"])
    model = sm.OLS(y, X).fit()
    fitted = {
        "const": float(model.params["const"]),
        "b_logit_initial_wp": float(model.params["logit_initial_wp"]),
        "b_score_diff": float(model.params["score_diff"]),
        "b_elapsed": float(model.params["elapsed_q"]),
        "b_elapsed2": float(model.params["elapsed_q2"]),
        "b_sd_elapsed": float(model.params["sd_x_elapsed"]),
        "b_sd_elapsed2": float(model.params["sd_x_elapsed2"]),
        "b_init_elapsed": float(model.params["init_x_elapsed"]),
        "b_init_elapsed2": float(model.params["init_x_elapsed2"]),
        "r_squared": float(model.rsquared),
        "n": int(len(df)),
    }
    print(f"Elapsed model: n={len(df)}  R2={model.rsquared:.4f}")
    return fitted


MODULE_TEMPLATE = '''"""
AUTO-GENERATED by scripts/fit_wp_quarter_model.py on {generated_at} --
do not hand-edit. Re-run that script (after pulling more seasons, etc.)
to regenerate this file; it always overwrites, not patches.

Fitted on {n_games} completed games, seasons {season_min}-{season_max}.

Model: logit(wp_at_quarter_end) ~ logit(initial_home_wp) + score_diff,
fit separately per quarter via OLS. Log-odds space reproduces the
empirical fact that a fixed score margin means much less to a heavy
pregame favorite's win probability than to a coinflip game's -- see the
fitting script's docstring for the full rationale.

predict_wp() / wp_residual() are quarter-checkpoint entry points.
predict_wp_elapsed() / coinflip_wp_elapsed() are the continuous-time
counterparts (accept elapsed_seconds directly instead of a quarter
bucket) -- the intended entry points for a comeback-detection metric
that needs a value at every score-change event, not just quarter ends.
Both families agree closely within Q1-3; predict_wp_elapsed() is the
only one of the two defined (via extrapolation) past that range.
"""
import math

QUARTER_MODELS = {{
{model_entries}
}}

ELAPSED_MODEL = {elapsed_model_entry}


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def inv_logit(x):
    return 1 / (1 + math.exp(-x))


def predict_wp(quarter, initial_home_wp, score_diff):
    """Model-predicted home_win_pct at the end of `quarter` (1, 2, or 3),
    given the pregame line and the score at that point. Raises KeyError
    for quarters outside the fitted range (only Q1-Q3 currently)."""
    m = QUARTER_MODELS[quarter]
    l_pred = m["const"] + m["b_logit_initial_wp"] * logit(initial_home_wp) + m["b_score_diff"] * score_diff
    return inv_logit(l_pred)


def coinflip_wp(quarter, score_diff):
    """predict_wp() with the pregame line forced to a 50/50 coin flip --
    i.e. what this score_diff alone implies about win probability, with the
    pregame-favorite skew factored out entirely. This is the anchor-free
    scale a comeback should be measured against: a "true" comeback is one
    where THIS quantity swings a lot, not just the raw (anchor-skewed) WP."""
    return predict_wp(quarter, 0.5, score_diff)


def wp_residual(quarter, initial_home_wp, score_diff, actual_home_wp):
    """actual - predicted, in probability space. Positive means the home
    team is doing better than the anchor+score model expects; negative
    means worse. Near zero means this WP value is unremarkable once the
    pregame line and actual score are accounted for -- e.g. a heavy
    favorite's high WP off a modest lead residuals near zero, rather than
    reading as a dramatic swing the way raw WP would."""
    return actual_home_wp - predict_wp(quarter, initial_home_wp, score_diff)


def predict_wp_elapsed(elapsed_seconds, initial_home_wp, score_diff):
    """Continuous-time counterpart to predict_wp() -- quadratic-in-elapsed
    interactions instead of a discrete quarter bucket. Fit on Q1-3 data
    only (elapsed 0-2700s); called on a Q4/OT elapsed value extrapolates
    past that range on purpose (accepted, not independently verified --
    see the fitting script's docstring)."""
    m = ELAPSED_MODEL
    eq = elapsed_seconds / 900.0
    li = logit(initial_home_wp)
    l_pred = (m["const"] + m["b_logit_initial_wp"] * li + m["b_score_diff"] * score_diff
              + m["b_elapsed"] * eq + m["b_elapsed2"] * eq ** 2
              + m["b_sd_elapsed"] * score_diff * eq + m["b_sd_elapsed2"] * score_diff * eq ** 2
              + m["b_init_elapsed"] * li * eq + m["b_init_elapsed2"] * li * eq ** 2)
    return inv_logit(l_pred)


def coinflip_wp_elapsed(elapsed_seconds, score_diff):
    """predict_wp_elapsed() with the pregame line forced to a coin flip --
    continuous-time analogue of coinflip_wp()."""
    return predict_wp_elapsed(elapsed_seconds, 0.5, score_diff)
'''

ENTRY_TEMPLATE = '''    {q}: {{"const": {const!r}, "b_logit_initial_wp": {b_logit_initial_wp!r}, "b_score_diff": {b_score_diff!r}, "r_squared": {r_squared!r}, "n": {n!r}}},'''

ELAPSED_ENTRY_TEMPLATE = '''{{
    "const": {const!r}, "b_logit_initial_wp": {b_logit_initial_wp!r}, "b_score_diff": {b_score_diff!r},
    "b_elapsed": {b_elapsed!r}, "b_elapsed2": {b_elapsed2!r},
    "b_sd_elapsed": {b_sd_elapsed!r}, "b_sd_elapsed2": {b_sd_elapsed2!r},
    "b_init_elapsed": {b_init_elapsed!r}, "b_init_elapsed2": {b_init_elapsed2!r},
    "r_squared": {r_squared!r}, "n": {n!r},
}}'''


def write_module(fitted, elapsed_fitted, season_min, season_max, n_games):
    entries = "\n".join(
        ENTRY_TEMPLATE.format(q=q, **fitted[q]) for q in QUARTERS
    )
    elapsed_entry = ELAPSED_ENTRY_TEMPLATE.format(**elapsed_fitted)
    content = MODULE_TEMPLATE.format(
        generated_at=datetime.date.today().isoformat(),
        n_games=n_games,
        season_min=season_min,
        season_max=season_max,
        model_entries=entries,
        elapsed_model_entry=elapsed_entry,
    )
    with open(OUTPUT_PATH, "w") as f:
        f.write(content)
    print(f"\nWrote {OUTPUT_PATH}")


def main():
    conn = db.get_connection()
    df, season_min, season_max, n_games = build_dataset(conn)
    print(f"Fitting on {n_games} games, seasons {season_min}-{season_max} ({len(df)} quarter-checkpoint rows)\n")
    fitted = fit_models(df)

    elapsed_df = build_elapsed_dataset(conn)
    print(f"\nFitting elapsed model on {len(elapsed_df)} individual Q1-3 WP rows")
    elapsed_fitted = fit_elapsed_model(elapsed_df)

    write_module(fitted, elapsed_fitted, season_min, season_max, n_games)


if __name__ == "__main__":
    main()
