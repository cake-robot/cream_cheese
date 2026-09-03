"""
Exploratory (read-only, no DB/module writes): does Model C's coin-flip WP
under-react at the literal end of regulation for modest (single-score)
deficits?

Prompted by three real games where the coin-flip chart looked wrong right
at the final snap(s) of a decided game:
  - 401525861 (Texas 30, Oklahoma 34, 2023 Red River): Texas's final play is
    a literal Hail Mary from the OU 44 down 4 with 0:00 on the clock --
    Model C reads 36.8% for Texas to still win. A real Hail Mary from the
    44 succeeds far less often than that.
  - 401520413 (Missouri 33, Florida 31): Missouri kicks the go-ahead FG as
    time expires; Florida's meaningless final snap from their own 25 with
    0 seconds left reads Missouri (the winner) at only ~57-59%.
  - 401442015 (Georgia 42, Ohio State 41, 2022 Peach Bowl): Ohio State
    facing 4th & 11 with ~3 seconds left, down 1 -- convert-or-lose -- and
    Model C reads a literal coin flip (49%) for Georgia, the team that
    wins outright if this play fails. Georgia's actual final kneel-down
    snap (0:00 left, ball at their own ~17, 1-point lead) reads only 57.5%.

Hypothesis: Model C's only clock-related terms are LINEAR in
time_remaining_frac (`time_remaining_frac` and `urgency = score_diff *
(1 - time_remaining_frac)`, see src/wp_situational.py) -- unlike
src/wp_baseline.py's ELAPSED_MODEL, which has QUADRATIC elapsed terms
(b_elapsed2, b_sd_elapsed2, b_init_elapsed2) baked in from the start. A
linear urgency term can't sharply saturate as time_frac -> 0: a 1-4 point
deficit only nudges the logit a little regardless of how close to 0:00 the
clock actually is, because urgency caps out at exactly `score_diff` (not
some much larger effective magnitude) once time_remaining_frac hits 0.

This script re-fits Model C's exact production dataset (same
espn.extract_situational_plays() call, same non-OT game filter, same
offense-perspective/score-as-of-snap construction as
scripts/build_wp_situational_module.py) under a proper 80/20 GAME-level
train/test split, then compares several candidate design matrices against
the CURRENT production feature set on:
  1. Overall held-out Brier/log-loss (must not regress -- a fix for the
     10-second tail that quietly makes the other 99.9% of plays worse is
     not a win).
  2. The literal endgame tail: held-out plays with <=120s and <=30s left
     in regulation specifically -- the region the three example games all
     live in.
  3. Recomputed WP for the three example plays above, under each
     candidate, so "does this actually fix what we saw" has a direct
     answer, not just an aggregate metric.

Deliberately does NOT write src/wp_situational.py or touch production --
purely a comparison, per the explicit ask to look before converting
anything over.

Usage:
    venv/bin/python3 scripts/compare_wp_endgame_calibration.py [path/to/cfb.db]
"""
import random
import sys

import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, ".")

from src import db, espn

EPS = 1e-4
MAX_DISTANCE = 30
TEST_FRACTION = 0.2
RANDOM_SEED = 20260830


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def build_dataset(conn):
    """Same construction as scripts/build_wp_situational_module.py's
    build_dataset(), plus game_id (for the train/test split) and
    elapsed_seconds (for slicing the endgame tail in evaluation)."""
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
                "game_id": g["game_id"],
                "down": play["down"],
                "distance": min(play["distance"], MAX_DISTANCE),
                "yards_to_go": play["yards_to_go"],
                "score_diff": offense_score - defense_score,
                "seconds_remaining_reg": seconds_remaining_reg,
                "elapsed_seconds": play["elapsed_seconds"],
                "offense_pregame_wp": offense_pregame_wp,
                "offense_won": offense_won,
            })

    return pd.DataFrame(rows), n_games_used, len(games)


def _base_fields(df):
    time_frac = df["seconds_remaining_reg"] / 3600.0
    return time_frac


def design_current(df):
    """Exact reproduction of src/wp_situational.py's production feature set."""
    time_frac = _base_fields(df)
    urgency = df["score_diff"] * (1 - time_frac)
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": df["score_diff"],
        "time_remaining_frac": time_frac,
        "urgency": urgency,
        "down2": (df["down"] == 2).astype(int),
        "down3": (df["down"] == 3).astype(int),
        "down4": (df["down"] == 4).astype(int),
        "distance": df["distance"],
        "yards_to_go": df["yards_to_go"],
    })
    return sm.add_constant(X, has_constant="add")


def design_quad_urgency(df):
    """current + urgency2 = score_diff * (1 - time_frac)^2 -- lets the
    deficit's effect accelerate non-linearly as time runs out, instead of
    being capped at exactly score_diff once time_frac hits 0."""
    X = design_current(df)
    time_frac = _base_fields(df)
    X["urgency2"] = df["score_diff"] * (1 - time_frac) ** 2
    return X


def design_quad_both(df):
    """quad urgency + time_remaining_frac^2 (a pure clock-shape term, no
    score interaction) -- matches wp_baseline.ELAPSED_MODEL's pattern of
    carrying both a quadratic clock term AND a quadratic score*clock term."""
    X = design_quad_urgency(df)
    time_frac = _base_fields(df)
    X["time_remaining_frac2"] = time_frac ** 2
    return X


def design_cubic_urgency(df):
    """current + urgency2 + urgency3 = score_diff * (1-time_frac)^3 --
    even sharper late-game acceleration."""
    X = design_quad_both(df)
    time_frac = _base_fields(df)
    X["urgency3"] = df["score_diff"] * (1 - time_frac) ** 3
    return X


def design_inv_sqrt_urgency(df):
    """Replaces linear urgency with a hyperbolic clock-urgency term
    (score_diff / sqrt(seconds_remaining + c)), the shape commonly used in
    published NFL/CFB win-probability models (e.g. nflfastR) specifically
    because it blows up as seconds_remaining -> 0 rather than saturating
    at a fixed multiple of score_diff. c=5 avoids a divide-by-zero at the
    literal final tick without meaningfully changing the curve elsewhere."""
    time_frac = _base_fields(df)
    urgency = df["score_diff"] * (1 - time_frac)  # keep the original as well
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": df["score_diff"],
        "time_remaining_frac": time_frac,
        "urgency": urgency,
        "inv_sqrt_urgency": df["score_diff"] / np.sqrt(df["seconds_remaining_reg"] + 5.0),
        "down2": (df["down"] == 2).astype(int),
        "down3": (df["down"] == 3).astype(int),
        "down4": (df["down"] == 4).astype(int),
        "distance": df["distance"],
        "yards_to_go": df["yards_to_go"],
    })
    return sm.add_constant(X, has_constant="add")


def design_cubic_plus_inv_sqrt(df):
    """cubic urgency (helps the mid/last-2-min region) + inv-sqrt urgency
    (the term that actually diverges at the literal buzzer, unlike any
    polynomial in time_frac, which caps out at (1-time_frac)=1 for every
    power once time_frac hits exactly 0) -- see design_inv_sqrt_urgency's
    docstring. Best-of-both candidate."""
    X = design_cubic_urgency(df)
    X["inv_sqrt_urgency"] = df["score_diff"] / np.sqrt(df["seconds_remaining_reg"] + 5.0)
    return X


CANDIDATES = {
    "current (production)": design_current,
    "+ quad urgency": design_quad_urgency,
    "+ quad urgency + quad time": design_quad_both,
    "+ cubic urgency": design_cubic_urgency,
    "+ inv-sqrt urgency (replaces linear)": design_inv_sqrt_urgency,
    "+ cubic + inv-sqrt urgency": design_cubic_plus_inv_sqrt,
}


def fit(df_train, design_fn):
    X = design_fn(df_train)
    y = df_train["offense_won"]
    return sm.Logit(y, X).fit(disp=0, maxiter=100)


def brier(pred, outcome):
    return float(np.mean((pred - outcome) ** 2))


def log_loss(pred, outcome):
    p = np.clip(pred, EPS, 1 - EPS)
    return float(-np.mean(outcome * np.log(p) + (1 - outcome) * np.log(1 - p)))


def evaluate(name, pred, outcome, mask=None):
    if mask is not None:
        pred, outcome = pred[mask], outcome[mask]
    n = len(outcome)
    if n == 0:
        print(f"  {name:<40s} n=0 (no rows in this slice)")
        return
    print(f"  {name:<40s} n={n:>6d}  brier={brier(pred, outcome):.4f}  logloss={log_loss(pred, outcome):.4f}")


# The three example plays from the reported games, hand-transcribed from
# extract_situational_plays() output (offense perspective, score_diff =
# offense - defense). Each is annotated with what "should" happen if the
# play fails, for eyeballing the predicted probability against intuition
# -- not a formal test, since we don't have a la a ground-truth WP.
EXAMPLE_PLAYS = [
    {
        "label": "401525861 TEX final Hail Mary (down 4, OU 44, 0:00 left)",
        "down": 1, "distance": 10, "yards_to_go": 44,
        "score_diff": -4, "seconds_remaining_reg": 0,
        "offense_pregame_wp": 0.5,
        "note": "offense (TEX) needs a TD on this exact snap or loses",
    },
    {
        "label": "401520413 UF final snap (down 2, own 25, 0:00 left)",
        "down": 1, "distance": 10, "yards_to_go": 75,
        "score_diff": -2, "seconds_remaining_reg": 0,
        "offense_pregame_wp": 0.5,
        "note": "offense (UF) needs a TD on this exact snap or loses",
    },
    {
        "label": "401442015 OSU 4th & 11 (down 1, ~midfield, 3s left)",
        "down": 4, "distance": 11, "yards_to_go": 32,
        "score_diff": -1, "seconds_remaining_reg": 3,
        "offense_pregame_wp": 0.5,
        "note": "offense (OSU) must convert 4th & 11 (or kick a long FG) or loses outright",
    },
    {
        "label": "401442015 UGA final kneel-down (up 1, own ~17, 0:00 left)",
        "down": 1, "distance": 25, "yards_to_go": 83,
        "score_diff": 1, "seconds_remaining_reg": 0,
        "offense_pregame_wp": 0.5,
        "note": "offense (UGA) just needs to not fumble/be scored on -- should read near-certain",
    },
]


def predict_with_model(model, design_fn, row):
    df_row = pd.DataFrame([row])
    X = design_fn(df_row)
    return float(model.predict(X).iloc[0])


def calibration_table(name, pred, outcome, n_bins=10):
    """Reliability check: bucket predicted probability into deciles and
    compare mean predicted vs actual observed win rate per bucket. A model
    that's just running to extreme confidence without real support will
    show observed rates NOT tracking predicted ones in the tail buckets."""
    print(f"\n  {name}:")
    edges = np.linspace(0, 1, n_bins + 1)
    bin_idx = np.clip(np.digitize(pred, edges) - 1, 0, n_bins - 1)
    for b in range(n_bins):
        mask = bin_idx == b
        n = mask.sum()
        if n == 0:
            continue
        mean_pred = pred[mask].mean()
        mean_actual = outcome[mask].mean()
        print(f"    pred [{edges[b]:.1f}-{edges[b+1]:.1f}) n={n:>5d}  mean_pred={mean_pred:.3f}  actual_win_rate={mean_actual:.3f}")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building regulation-only play-level dataset (same as production build_wp_situational_module.py)...")
    df, n_games_used, n_games_total = build_dataset(conn)
    print(f"Games with raw JSON available (non-OT): {n_games_used}/{n_games_total}")
    print(f"Regulation scrimmage-down plays: {len(df)}\n")

    game_ids = df["game_id"].unique().tolist()
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(game_ids)
    n_test = int(len(game_ids) * TEST_FRACTION)
    test_ids = set(game_ids[:n_test])
    is_test = df["game_id"].isin(test_ids)
    df_train, df_test = df[~is_test].reset_index(drop=True), df[is_test].reset_index(drop=True)
    print(f"Train games: {len(game_ids) - n_test}  ({len(df_train)} plays)")
    print(f"Test games:  {n_test}  ({len(df_test)} plays)\n")

    outcome = df_test["offense_won"].to_numpy().astype(float)
    secs_left = df_test["seconds_remaining_reg"].to_numpy()

    models = {}
    print("=== Fitting each candidate on the train split ===")
    for name, design_fn in CANDIDATES.items():
        model = fit(df_train, design_fn)
        models[name] = model
        print(f"  {name:<40s} pseudo-R^2={model.prsquared:.4f}")

    print("\n=== Held-out evaluation, ALL plays (must not regress) ===")
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name, pred, outcome)

    print("\n=== Held-out evaluation, last 2 minutes of regulation (<=120s left) ===")
    mask_2min = secs_left <= 120
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name, pred, outcome, mask=mask_2min)

    print("\n=== Held-out evaluation, last 30 seconds of regulation (<=30s left) ===")
    mask_30s = secs_left <= 30
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name, pred, outcome, mask=mask_30s)

    print("\n=== Calibration check (last 2 min, <=120s left): predicted decile vs actual observed win rate ===")
    print("current (production):")
    calibration_table("current (production)", models["current (production)"].predict(design_current(df_test)).to_numpy()[mask_2min], outcome[mask_2min])
    calibration_table("+ inv-sqrt urgency (replaces linear)", models["+ inv-sqrt urgency (replaces linear)"].predict(design_inv_sqrt_urgency(df_test)).to_numpy()[mask_2min], outcome[mask_2min])
    calibration_table("+ cubic + inv-sqrt urgency", models["+ cubic + inv-sqrt urgency"].predict(design_cubic_plus_inv_sqrt(df_test)).to_numpy()[mask_2min], outcome[mask_2min])

    print("\n=== The three reported example plays, predicted WP for the OFFENSE under each candidate ===")
    for ex in EXAMPLE_PLAYS:
        row = {k: v for k, v in ex.items() if k not in ("label", "note")}
        print(f"\n  {ex['label']}")
        print(f"    ({ex['note']})")
        for name, design_fn in CANDIDATES.items():
            p = predict_with_model(models[name], design_fn, row)
            print(f"    {name:<40s} offense WP = {p:.3f}")


if __name__ == "__main__":
    main()
