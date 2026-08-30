"""
Exploratory (read-only): closes two known gaps in src/wp_baseline.py's
shipped ELAPSED_MODEL at once:

1. ELAPSED_MODEL was only ever fit on Q1-3 win_probability rows (see
   scripts/fit_wp_quarter_model.py) -- Q4/OT predictions have always been
   extrapolation past the fitted range, flagged in project notes as
   "accepted, not independently verified." Every completed game's full
   play-by-play (all periods, via game_raw_json) is available now, so this
   refits the same score+time+pregame-line design on the WHOLE game
   including real Q4/OT data instead of extrapolating into it.
2. scripts/fit_wp_situational_model.py showed down/distance/field position
   (from the same game_raw_json play data) measurably improves a
   win-probability estimate. This adds those as additional predictors,
   home-signed (multiplied by +1/-1 depending on whether the home team is
   the offense) so they combine with the home-referenced score_diff/target
   the same design already uses.

Unlike fit_wp_situational_model.py (which targets the actual game outcome),
this keeps the ORIGINAL model's target: ESPN's own live win probability, in
logit space, via OLS -- same modeling approach as fit_wp_quarter_model.py,
just extended in scope. That makes the three-way comparison below a clean
isolation of two separate effects:
  A. does real Q4/OT training data (vs. extrapolating into it) improve fit?
  B. does adding down/distance/field position improve it further?

Train/test split is by GAME (not by play), and all evaluation is against
ESPN's actual WP value on held-out games, broken out by period so it's
visible exactly where each change helps (the answer is expected to be:
mostly Q4/OT, where the old model was extrapolating blind).

Requires numpy/pandas/statsmodels (requirements-dev.txt). Read-only --
writes nothing to the DB and does not regenerate src/wp_baseline.py; that's
a follow-up once/if this is deemed worth shipping.

Usage:
    venv/bin/python3 scripts/fit_wp_full_model.py [path/to/cfb.db]
"""
import random
import sys

import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, ".")

from src import db
from src.espn import _parse_clock
from src import wp_baseline

EPS = 1e-4
MAX_DISTANCE = 30
TEST_FRACTION = 0.2
RANDOM_SEED = 20260830

PERIOD_LABELS = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def inv_logit(x):
    return 1 / (1 + np.exp(-x))


def _iter_drives(raw):
    drives = raw.get("drives", {})
    out = list(drives.get("previous", []))
    current = drives.get("current")
    if isinstance(current, dict):
        out.append(current)
    elif isinstance(current, list):
        out.extend(current)
    return out


def build_dataset(conn):
    games = conn.execute("""
        SELECT game_id, home_team_id, away_team_id, initial_home_wp
        FROM games
        WHERE completed = 1 AND detail_fetched = 1 AND initial_home_wp IS NOT NULL
    """).fetchall()

    rows = []
    n_used = 0
    for g in games:
        raw = db.get_game_raw_json(conn, g["game_id"])
        if not raw:
            continue
        espn_wp_by_play = {
            r["play_id"]: r["home_win_pct"]
            for r in conn.execute(
                "SELECT play_id, home_win_pct FROM win_probability WHERE game_id = ? AND source = 'espn'",
                (g["game_id"],),
            )
        }
        if not espn_wp_by_play:
            continue
        n_used += 1

        ot_counter = {}
        for drive in _iter_drives(raw):
            off_team = str(drive.get("team", {}).get("id", ""))
            if not off_team:
                continue
            sign = 1.0 if off_team == g["home_team_id"] else -1.0

            for play in drive.get("plays", []):
                play_id = str(play.get("id", ""))
                espn_wp_home = espn_wp_by_play.get(play_id)
                if espn_wp_home is None:
                    continue

                start = play.get("start", {})
                down = start.get("down")
                distance = start.get("distance")
                yards_to_go = start.get("yardsToEndzone")
                if down is None or distance is None or yards_to_go is None:
                    continue
                if not (1 <= down <= 4) or not (0 < yards_to_go <= 100) or distance < 0:
                    continue

                period = (play.get("period") or {}).get("number")
                home_score = play.get("homeScore")
                away_score = play.get("awayScore")
                if period is None or home_score is None or away_score is None:
                    continue

                if period > 4:
                    n = ot_counter.get(period, 0)
                    elapsed_seconds = 3600 + (period - 5) * 100 + n
                    ot_counter[period] = n + 1
                else:
                    secs_remaining = _parse_clock((play.get("clock") or {}).get("displayValue") or "")
                    if secs_remaining is None:
                        continue
                    elapsed_seconds = (period - 1) * 900 + (900 - secs_remaining)

                rows.append({
                    "game_id": g["game_id"],
                    "initial_home_wp": g["initial_home_wp"],
                    "score_diff": home_score - away_score,
                    "elapsed_seconds": elapsed_seconds,
                    "period": period,
                    "sign": sign,
                    "down": down,
                    "distance": min(distance, MAX_DISTANCE),
                    "yards_to_go": yards_to_go,
                    "goal_to_go": int(distance >= yards_to_go),
                    "espn_wp_home": espn_wp_home,
                })

    return pd.DataFrame(rows), n_used, len(games)


def make_design(df, situational):
    eq = df["elapsed_seconds"] / 900.0
    li = logit(df["initial_home_wp"].to_numpy())
    sd = df["score_diff"]
    cols = {
        "logit_initial_wp": li,
        "score_diff": sd,
        "elapsed_q": eq,
        "elapsed_q2": eq ** 2,
        "sd_x_elapsed": sd * eq,
        "sd_x_elapsed2": sd * eq ** 2,
        "init_x_elapsed": li * eq,
        "init_x_elapsed2": li * eq ** 2,
    }
    if situational:
        sign = df["sign"]
        cols.update({
            "signed_down2": sign * (df["down"] == 2),
            "signed_down3": sign * (df["down"] == 3),
            "signed_down4": sign * (df["down"] == 4),
            "signed_distance": sign * df["distance"],
            "signed_yards_to_go": sign * df["yards_to_go"],
            "signed_goal_to_go": sign * df["goal_to_go"],
        })
    return sm.add_constant(pd.DataFrame(cols))


def fit_ols(df, situational):
    X = make_design(df, situational)
    y = logit(df["espn_wp_home"].to_numpy())
    return sm.OLS(y, X).fit()


def old_model_predict(df):
    return np.array([
        wp_baseline.predict_wp_elapsed(r.elapsed_seconds, r.initial_home_wp, r.score_diff)
        for r in df.itertuples()
    ])


def evaluate(name, pred, actual, mask=None):
    if mask is not None:
        pred, actual = pred[mask], actual[mask]
    n = len(actual)
    rmse = float(np.sqrt(np.mean((pred - actual) ** 2)))
    mae = float(np.mean(np.abs(pred - actual)))
    print(f"  {name:<34s} n={n:>7d}  RMSE={rmse:.4f}  MAE={mae:.4f}")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building whole-game play-level dataset (down/distance/field position, matched to ESPN's own WP)...")
    df, n_used, n_total = build_dataset(conn)
    print(f"Games used: {n_used}/{n_total}")
    print(f"Plays collected (every scrimmage down w/ a matched ESPN WP row): {len(df)}")
    print("By period:")
    print(df["period"].value_counts().sort_index().to_string())

    game_ids = df["game_id"].unique().tolist()
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(game_ids)
    n_test = int(len(game_ids) * TEST_FRACTION)
    test_ids = set(game_ids[:n_test])
    is_test = df["game_id"].isin(test_ids)
    df_train, df_test = df[~is_test].reset_index(drop=True), df[is_test].reset_index(drop=True)
    print(f"\nTrain games: {len(game_ids) - n_test} ({len(df_train)} plays)  "
          f"Test games: {n_test} ({len(df_test)} plays)\n")

    print("=== Model A: score+time+line only, refit on the WHOLE game (Q1-OT), not just Q1-3 ===")
    model_time_only = fit_ols(df_train, situational=False)
    print(f"In-sample R^2: {model_time_only.rsquared:.4f}\n")

    print("=== Model B: + down/distance/field position (home-signed) ===")
    model_full = fit_ols(df_train, situational=True)
    print(model_full.summary())

    actual = df_test["espn_wp_home"].to_numpy()
    old_pred = old_model_predict(df_test)
    time_only_pred = inv_logit(model_time_only.predict(make_design(df_test, situational=False)).to_numpy())
    full_pred = inv_logit(model_full.predict(make_design(df_test, situational=True)).to_numpy())

    print("\n=== Held-out fit to ESPN's own WP, whole game (lower is better) ===")
    evaluate("current wp_baseline (shipped)", old_pred, actual)
    evaluate("refit on Q1-OT, score+time+line", time_only_pred, actual)
    evaluate("+ down/distance/field position", full_pred, actual)

    print("\n=== Same three, broken out by period (this is where it should matter) ===")
    for period in sorted(df_test["period"].unique()):
        label = PERIOD_LABELS.get(period, f"OT{period - 4}")
        mask = (df_test["period"] == period).to_numpy()
        print(f"\n {label} (n={int(mask.sum())}):")
        evaluate("current wp_baseline (shipped)", old_pred, actual, mask=mask)
        evaluate("refit on Q1-OT, score+time+line", time_only_pred, actual, mask=mask)
        evaluate("+ down/distance/field position", full_pred, actual, mask=mask)


if __name__ == "__main__":
    main()
