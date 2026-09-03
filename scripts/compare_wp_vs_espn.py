"""
Exploratory (read-only, no DB/module writes): does the round-2 scores_needed
candidate (see scripts/compare_wp_endgame_calibration.py) beat ESPN's OWN
live win probability against real outcomes -- not just our own current
Model C -- and specifically in the endgame tail this whole investigation is
about?

Same question as scripts/fit_wp_situational_model.py's original "Follow-up
2" comparison (ESPN raw WP: Brier 0.1082 vs. our outcome-trained model:
0.1084 -- effectively tied overall, ESPN wins Q1/Q4, ours wins Q2/Q3), but:
  (a) re-run against the CURRENT feature set and the NEW round-2 candidate,
      not the original exploratory model from that session, and
  (b) sliced by the literal endgame tail (last 2 min / last 30s) instead of
      by quarter, since that's the specific region under investigation.

Uses real offense_pregame_wp (NOT forced to 0.5/coin-flip) since ESPN's own
WP is also pregame-anchored -- coin-flip-forcing either side would make
this an apples-to-oranges comparison.

Usage:
    venv/bin/python3 scripts/compare_wp_vs_espn.py [path/to/cfb.db]
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
    """Same as compare_wp_endgame_calibration.py's build_dataset(), plus
    play_id (to join ESPN's own WP) and espn_wp_offense (None if this
    game/play has no real ESPN WP row -- e.g. fox_synthetic-sourced games,
    or a play ESPN's own array never matched)."""
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

        espn_wp_by_play = {
            r["play_id"]: r["home_win_pct"]
            for r in conn.execute(
                "SELECT play_id, home_win_pct FROM win_probability WHERE game_id = ? AND source = 'espn'",
                (g["game_id"],),
            )
        }

        for play in espn.extract_situational_plays(raw, g["home_team_id"]):
            if not play["play_id"]:
                continue

            off_is_home = play["off_is_home"]
            offense_score = play["home_score"] if off_is_home else play["away_score"]
            defense_score = play["away_score"] if off_is_home else play["home_score"]
            offense_pregame_wp = g["initial_home_wp"] if off_is_home else 1 - g["initial_home_wp"]
            offense_won = home_won if off_is_home else 1 - home_won
            seconds_remaining_reg = max(0, 3600 - play["elapsed_seconds"])

            espn_wp_home = espn_wp_by_play.get(play["play_id"])
            espn_wp_offense = (
                None if espn_wp_home is None
                else (espn_wp_home if off_is_home else 1 - espn_wp_home)
            )

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
                "espn_wp_offense": espn_wp_offense,
            })

    return pd.DataFrame(rows), n_games_used, len(games)


def _base_fields(df):
    return df["seconds_remaining_reg"] / 3600.0


def design_current(df):
    time_frac = _base_fields(df)
    urgency = df["score_diff"] * (1 - time_frac)
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": df["score_diff"], "time_remaining_frac": time_frac, "urgency": urgency,
        "down2": (df["down"] == 2).astype(int), "down3": (df["down"] == 3).astype(int),
        "down4": (df["down"] == 4).astype(int), "distance": df["distance"], "yards_to_go": df["yards_to_go"],
    })
    return sm.add_constant(X, has_constant="add")


def design_round2(df):
    """The round-2 winner: design_scores_needed_final from
    compare_wp_endgame_calibration.py (scores_needed urgency family, minus
    the one insignificant quadratic term)."""
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


CANDIDATES = {
    "current (production)": design_current,
    "round 2 (scores-needed)": design_round2,
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
        print(f"  {name:<26s} n=0")
        return
    print(f"  {name:<26s} n={n:>6d}  brier={brier(pred, outcome):.4f}  logloss={log_loss(pred, outcome):.4f}")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building dataset (with ESPN WP joined by play_id)...")
    df, n_games_used, n_games_total = build_dataset(conn)
    print(f"Games with raw JSON available (non-OT): {n_games_used}/{n_games_total}")
    print(f"Regulation scrimmage-down plays: {len(df)}")
    print(f"...of which have a real ESPN WP match: {df['espn_wp_offense'].notna().sum()}\n")

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
    espn_pred = df_test["espn_wp_offense"].to_numpy(dtype=float)
    espn_mask = ~np.isnan(espn_pred)

    models = {}
    for name, design_fn in CANDIDATES.items():
        models[name] = fit(df_train, design_fn)

    def all_preds():
        preds = {name: models[name].predict(design_fn(df_test)).to_numpy() for name, design_fn in CANDIDATES.items()}
        preds["ESPN live WP"] = espn_pred
        return preds

    preds = all_preds()

    print("=== Held-out evaluation, ALL plays, restricted to rows with a real ESPN WP match (fair 3-way comparison) ===")
    for name, p in preds.items():
        evaluate(name, p, outcome, mask=espn_mask)

    print("\n=== Last 2 minutes of regulation (<=120s left), ESPN-matched rows only ===")
    mask_2min = (secs_left <= 120) & espn_mask
    for name, p in preds.items():
        evaluate(name, p, outcome, mask=mask_2min)

    print("\n=== Last 30 seconds of regulation (<=30s left), ESPN-matched rows only ===")
    mask_30s = (secs_left <= 30) & espn_mask
    for name, p in preds.items():
        evaluate(name, p, outcome, mask=mask_30s)

    print("\n=== Targeted slice: offense leading 1-8, fresh 1st & 10, <=30s left, ESPN-matched only ===")
    fresh_downs_mask = (df_test["down"] == 1).to_numpy() & (df_test["distance"] == 10).to_numpy()
    one_score_lead_mask = (df_test["score_diff"] >= 1).to_numpy() & (df_test["score_diff"] <= 8).to_numpy()
    narrow_mask = fresh_downs_mask & one_score_lead_mask & (secs_left <= 30) & espn_mask
    for name, p in preds.items():
        evaluate(name, p, outcome, mask=narrow_mask)
    print("\n  mean predicted vs actual on that slice:")
    for name, p in preds.items():
        sub_pred, sub_out = p[narrow_mask], outcome[narrow_mask]
        if len(sub_out) == 0:
            continue
        print(f"    {name:<26s} n={len(sub_out):>4d}  mean_pred={sub_pred.mean():.3f}  actual={sub_out.mean():.3f}")


if __name__ == "__main__":
    main()
