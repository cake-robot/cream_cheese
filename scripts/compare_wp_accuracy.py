"""
Exploratory (read-only): "who is actually more accurate" across every win-
probability candidate this project has now built or examined, judged
against the one true ground truth -- the ACTUAL GAME OUTCOME -- not against
each other. Restricted to regulation plays (period 1-4); OT is excluded
per request, since scripts/diagnose_ot_wp.py already found ESPN's own WP
degrades in OT and the OT sample here is small/noisy anyway.

Four candidates, same held-out games, same evaluation:
  1. ESPN's own live WP (raw, as published)
  2. Model A: score+time+pregame-line only, refit on Q1-4 -- same design as
     the shipped src/wp_baseline.py, targets ESPN's own WP
  3. Model B: + down/distance/field position -- from
     scripts/fit_wp_full_model.py, also targets ESPN's own WP
  4. Model C: the situational model from
     scripts/fit_wp_situational_model.py -- targets the actual game outcome
     directly (not ESPN's WP), offense-perspective, converted back to
     home-perspective for this comparison

Models A/B/C are all refit here (not loaded from the other scripts) on an
identical train/test game split so the comparison is apples-to-apples.

Usage:
    venv/bin/python3 scripts/compare_wp_accuracy.py [path/to/cfb.db]
"""
import random
import sys

import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, ".")

from src import db
from src.espn import _parse_clock

EPS = 1e-4
MAX_DISTANCE = 30
TEST_FRACTION = 0.2
RANDOM_SEED = 20260830


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
        SELECT game_id, home_team_id, home_score, away_score, initial_home_wp
        FROM games
        WHERE completed = 1 AND detail_fetched = 1
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND home_score != away_score
          AND initial_home_wp IS NOT NULL
    """).fetchall()

    rows = []
    for g in games:
        raw = db.get_game_raw_json(conn, g["game_id"])
        if not raw:
            continue
        home_won = 1 if g["home_score"] > g["away_score"] else 0

        espn_wp_by_play = {
            r["play_id"]: r["home_win_pct"]
            for r in conn.execute(
                "SELECT play_id, home_win_pct FROM win_probability WHERE game_id = ? AND source = 'espn'",
                (g["game_id"],),
            )
        }
        if not espn_wp_by_play:
            continue

        for drive in _iter_drives(raw):
            off_team = str(drive.get("team", {}).get("id", ""))
            if not off_team:
                continue
            off_is_home = off_team == g["home_team_id"]
            sign = 1.0 if off_is_home else -1.0

            for play in drive.get("plays", []):
                period = (play.get("period") or {}).get("number")
                if period is None or period > 4:  # regulation only -- OT excluded
                    continue

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

                home_score = play.get("homeScore")
                away_score = play.get("awayScore")
                if home_score is None or away_score is None:
                    continue

                secs_remaining = _parse_clock((play.get("clock") or {}).get("displayValue") or "")
                if secs_remaining is None:
                    continue
                elapsed_seconds = (period - 1) * 900 + (900 - secs_remaining)

                offense_score = home_score if off_is_home else away_score
                defense_score = away_score if off_is_home else home_score
                offense_pregame_wp = g["initial_home_wp"] if off_is_home else 1 - g["initial_home_wp"]
                offense_won = home_won if off_is_home else 1 - home_won

                rows.append({
                    "game_id": g["game_id"],
                    "period": period,
                    "sign": sign,
                    "off_is_home": int(off_is_home),
                    "down": down,
                    "distance": min(distance, MAX_DISTANCE),
                    "yards_to_go": yards_to_go,
                    "goal_to_go": int(distance >= yards_to_go),
                    "score_diff_home": home_score - away_score,
                    "elapsed_seconds": elapsed_seconds,
                    "initial_home_wp": g["initial_home_wp"],
                    "offense_pregame_wp": offense_pregame_wp,
                    "espn_wp_home": espn_wp_home,
                    "home_win": home_won,
                    "offense_won": offense_won,
                })

    return pd.DataFrame(rows), len(games)


# --- Model A / B: home-perspective, target = logit(ESPN's own WP) ---

def make_design_home(df, situational):
    eq = df["elapsed_seconds"] / 900.0
    li = logit(df["initial_home_wp"].to_numpy())
    sd = df["score_diff_home"]
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


def fit_model_ab(df_train, situational):
    X = make_design_home(df_train, situational)
    y = logit(df_train["espn_wp_home"].to_numpy())
    return sm.OLS(y, X).fit()


# --- Model C: offense-perspective, target = actual outcome ---

def make_design_offense(df):
    time_frac = np.clip(3600 - df["elapsed_seconds"].to_numpy(), 0, None) / 3600.0
    off_sign = np.where(df["off_is_home"].to_numpy().astype(bool), 1, -1)
    score_diff_off = df["score_diff_home"].to_numpy() * off_sign
    urgency = score_diff_off * (1 - time_frac)
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": score_diff_off,
        "time_remaining_frac": time_frac,
        "urgency": urgency,
        "down2": (df["down"] == 2).astype(int),
        "down3": (df["down"] == 3).astype(int),
        "down4": (df["down"] == 4).astype(int),
        "distance": df["distance"],
        "yards_to_go": df["yards_to_go"],
        "goal_to_go": df["goal_to_go"],
    })
    return sm.add_constant(X)


def fit_model_c(df_train):
    X = make_design_offense(df_train)
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
    print(f"  {name:<36s} n={n:>7d}  brier={brier(pred, outcome):.4f}  logloss={log_loss(pred, outcome):.4f}")


PERIOD_LABELS = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building regulation-only (period 1-4) play-level dataset...")
    df, n_games = build_dataset(conn)
    print(f"Games: {n_games}  Regulation plays: {len(df)}\n")

    game_ids = df["game_id"].unique().tolist()
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(game_ids)
    n_test = int(len(game_ids) * TEST_FRACTION)
    test_ids = set(game_ids[:n_test])
    is_test = df["game_id"].isin(test_ids)
    df_train, df_test = df[~is_test].reset_index(drop=True), df[is_test].reset_index(drop=True)
    print(f"Train games: {len(game_ids) - n_test} ({len(df_train)} plays)  "
          f"Test games: {n_test} ({len(df_test)} plays)\n")

    print("Fitting Model A (score+time+line, targets ESPN WP)...")
    model_a = fit_model_ab(df_train, situational=False)
    print("Fitting Model B (+ down/distance/field position, targets ESPN WP)...")
    model_b = fit_model_ab(df_train, situational=True)
    print("Fitting Model C (situational, targets actual outcome)...")
    model_c = fit_model_c(df_train)
    print()

    outcome = df_test["home_win"].to_numpy().astype(float)
    espn_pred = df_test["espn_wp_home"].to_numpy(dtype=float)
    a_pred = inv_logit(model_a.predict(make_design_home(df_test, situational=False)).to_numpy())
    b_pred = inv_logit(model_b.predict(make_design_home(df_test, situational=True)).to_numpy())
    c_pred_offense = model_c.predict(make_design_offense(df_test)).to_numpy()
    off_is_home = df_test["off_is_home"].to_numpy().astype(bool)
    c_pred_home = np.where(off_is_home, c_pred_offense, 1 - c_pred_offense)

    print("=== Accuracy vs. ACTUAL GAME OUTCOME, regulation plays only (lower is better) ===")
    evaluate("ESPN live WP (raw)", espn_pred, outcome)
    evaluate("Model A: score+time+line (->ESPN WP)", a_pred, outcome)
    evaluate("Model B: +situational (->ESPN WP)", b_pred, outcome)
    evaluate("Model C: situational (->actual outcome)", c_pred_home, outcome)

    print("\n=== Same four, by quarter ===")
    for period in (1, 2, 3, 4):
        mask = (df_test["period"] == period).to_numpy()
        print(f"\n {PERIOD_LABELS[period]} (n={int(mask.sum())}):")
        evaluate("ESPN live WP (raw)", espn_pred, outcome, mask=mask)
        evaluate("Model A: score+time+line (->ESPN WP)", a_pred, outcome, mask=mask)
        evaluate("Model B: +situational (->ESPN WP)", b_pred, outcome, mask=mask)
        evaluate("Model C: situational (->actual outcome)", c_pred_home, outcome, mask=mask)


if __name__ == "__main__":
    main()
