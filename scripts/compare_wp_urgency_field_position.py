"""
Exploratory (read-only, no DB/module writes): does interacting the
scores_needed urgency family with yards_to_go fix the goal-line blind
spot -- a trailing offense right on the doorstep with no time left reads
FAR too low a win probability (401521330: OSU 1st & goal from the 1,
trailing by 4, 7s left -- production reads 27.9%, ESPN reads 65.1%,
empirical rate for this exact shape across the corpus is 53-66%) --
without breaking the far-field cases (Hail Marys, victory formations)
those same urgency terms were built to fix?

RESULT: yes -- "+ full urgency family x ytg" (design_urgency_x_ytg_full)
won on every slice tested (all held-out plays, the goal-line bug's exact
shape, AND the far-field slice) and was productionized in
scripts/build_wp_situational_module.py (2026-09-03); see that script's
module docstring for the full writeup. The two "distance-adjusted
inv-sqrt" candidates were rejected -- they broke the Hail Mary anchor.

Root cause (see conversation/memory): sn_urgency/sn_urgency3/sn_inv_sqrt_urgency
are functions of ONLY scores_needed and time_remaining -- they collapse a
trailing team's WP toward the leader's regardless of how far that trailing
team actually has to travel. Correct when they're 75 yards away with 7
seconds left; badly wrong when they're 1 yard away with 7 seconds left.

Why this shouldn't suffer the same dilution that killed the down4 x
yards_to_go test (scripts/compare_wp_fg_range_interaction.py, negative
result): down4 fires on every 4th down all game, so an interaction with
yards_to_go got averaged into insignificance by the overwhelming mass of
low-leverage garbage-time snaps. The urgency terms here are ALREADY
concentrated in exactly the late/close-game situations that matter by
construction ((1-time_frac) and 1/sqrt(seconds_remaining) both shrink to
~0 everywhere else) -- an interaction with yards_to_go only ever activates
in the same narrow window the base terms already do, so it shouldn't get
drowned out the same way.

Candidates (all additive on top of the current production round-2
feature set -- see scripts/compare_wp_endgame_calibration.py):
  1. current (production)
  2. + sn_urgency x yards_to_go (linear scaling: penalty grows with distance)
  3. + also sn_urgency3 x yards_to_go, sn_inv_sqrt_urgency x yards_to_go
     (let every urgency term in the family scale with distance)
  4. A single combined "distance-adjusted" inverse-sqrt urgency term:
     sn / sqrt(seconds_remaining + k*yards_to_go + 5) -- yards_to_go
     effectively COSTS time before the urgency denominator sees it, so
     being close "buys back" apparent time remaining. k is a free
     yards-to-seconds conversion rate, tried at a couple of values.

Usage:
    venv/bin/python3 scripts/compare_wp_urgency_field_position.py [path/to/cfb.db]
"""
import math
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


def scores_needed(score_diff):
    if score_diff == 0:
        return 0
    return math.copysign(math.ceil(abs(score_diff) / 8.0), score_diff)


def build_dataset(conn):
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
                continue

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
    return df["seconds_remaining_reg"] / 3600.0


def design_current(df):
    time_frac = _base_fields(df)
    sn = df["score_diff"].map(scores_needed)
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": df["score_diff"], "time_remaining_frac": time_frac,
        "down2": (df["down"] == 2).astype(int), "down3": (df["down"] == 3).astype(int),
        "down4": (df["down"] == 4).astype(int), "distance": df["distance"], "yards_to_go": df["yards_to_go"],
        "sn_urgency": sn * (1 - time_frac), "sn_urgency3": sn * (1 - time_frac) ** 3,
        "sn_time_remaining_frac2": time_frac ** 2,
        "sn_inv_sqrt_urgency": sn / np.sqrt(df["seconds_remaining_reg"] + 5.0),
    })
    return sm.add_constant(X, has_constant="add")


def design_urgency_x_ytg_linear(df):
    X = design_current(df)
    time_frac = _base_fields(df)
    sn = df["score_diff"].map(scores_needed)
    X["sn_urgency_x_ytg"] = sn * (1 - time_frac) * df["yards_to_go"] / 50.0
    return X


def design_urgency_x_ytg_full(df):
    X = design_urgency_x_ytg_linear(df)
    time_frac = _base_fields(df)
    sn = df["score_diff"].map(scores_needed)
    X["sn_urgency3_x_ytg"] = sn * (1 - time_frac) ** 3 * df["yards_to_go"] / 50.0
    X["sn_inv_sqrt_urgency_x_ytg"] = (sn / np.sqrt(df["seconds_remaining_reg"] + 5.0)) * df["yards_to_go"] / 50.0
    return X


def design_distance_adjusted_inv_sqrt(k):
    """sn / sqrt(seconds_remaining + k*yards_to_go + 5) REPLACES the plain
    sn_inv_sqrt_urgency term -- yards_to_go costs `k` seconds of apparent
    urgency-relevant time before hitting the sqrt, so being close to the
    end zone buys back urgency the same way having more real time would."""
    def _design(df):
        X = design_current(df).drop(columns=["sn_inv_sqrt_urgency"])
        time_frac = _base_fields(df)
        sn = df["score_diff"].map(scores_needed)
        X[f"sn_inv_sqrt_urgency_adj_k{k}"] = sn / np.sqrt(df["seconds_remaining_reg"] + k * df["yards_to_go"] + 5.0)
        return X
    return _design


CANDIDATES = {
    "current (production)": design_current,
    "+ sn_urgency x ytg (linear)": design_urgency_x_ytg_linear,
    "+ full urgency family x ytg": design_urgency_x_ytg_full,
    "distance-adjusted inv-sqrt (k=3)": design_distance_adjusted_inv_sqrt(3),
    "distance-adjusted inv-sqrt (k=6)": design_distance_adjusted_inv_sqrt(6),
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
        print(f"  {name:<34s} n=0")
        return
    print(f"  {name:<34s} n={n:>6d}  brier={brier(pred, outcome):.4f}  logloss={log_loss(pred, outcome):.4f}")


def predict_with_model(model, design_fn, row):
    df_row = pd.DataFrame([row])
    X = design_fn(df_row)
    return float(model.predict(X).iloc[0])


OSU_GOALLINE = {
    "down": 1, "distance": 1, "yards_to_go": 1, "score_diff": -4,
    "seconds_remaining_reg": 7, "offense_pregame_wp": 0.5,
}
OSU_HAILMARY = {  # sanity check: must NOT regress this one
    "down": 1, "distance": 10, "yards_to_go": 44, "score_diff": -4,
    "seconds_remaining_reg": 0, "offense_pregame_wp": 0.5,
}


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building regulation-only play-level dataset...")
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
    ytg = df_test["yards_to_go"].to_numpy()
    sd = df_test["score_diff"].to_numpy()

    models = {}
    print("=== Fitting each candidate on the train split ===")
    for name, design_fn in CANDIDATES.items():
        model = fit(df_train, design_fn)
        models[name] = model
        print(f"  {name:<34s} pseudo-R^2={model.prsquared:.4f}")
        for term in model.params.index:
            if "x_ytg" in term or "adj_k" in term:
                print(f"      {term:<30s} coef={model.params[term]:+.5f}  p={model.pvalues[term]:.4f}")

    print("\n=== Held-out evaluation, ALL plays (must not regress) ===")
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name, pred, outcome)

    print("\n=== Held-out: trailing offense, <=30s left, split by field position (the bug's exact shape) ===")
    trailing_low_time = (sd < 0) & (sd >= -8) & (secs_left <= 30)
    close_mask = trailing_low_time & (ytg <= 10)
    far_mask = trailing_low_time & (ytg > 30)
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name + " [close, ytg<=10]", pred, outcome, mask=close_mask)
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name + " [far, ytg>30 -- must not regress]", pred, outcome, mask=far_mask)

    print("\n  mean predicted vs actual, close/ytg<=10 slice:")
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()[close_mask]
        act = outcome[close_mask]
        print(f"    {name:<34s} n={len(act):>4d}  mean_pred={pred.mean():.3f}  actual={act.mean():.3f}")

    print("\n=== The two anchor examples ===")
    for label, row in [("OSU 1st & goal from the 1, 7s left (ESPN: 65.1%, empirical bucket: 53-66%)", OSU_GOALLINE),
                        ("Hail Mary from the 44, 0s left (must stay LOW, ~10-15%)", OSU_HAILMARY)]:
        print(f"\n  {label}")
        for name, design_fn in CANDIDATES.items():
            p = predict_with_model(models[name], design_fn, row)
            print(f"    {name:<34s} offense WP = {p*100:.1f}%")


if __name__ == "__main__":
    main()
