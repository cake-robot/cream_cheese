"""
Exploratory (read-only): does adding down/distance/field position to the
existing score+time+pregame-line win-probability baseline (src/wp_baseline.py)
actually improve win-probability estimates, now that every completed game's
full ESPN /summary payload -- including per-play down/distance/yardsToEndzone
-- is archived in game_raw_json (see src/db.py's upsert_game_raw_json)?

Unlike scripts/fit_wp_quarter_model.py (which regresses toward ESPN's own WP
as the training target), this fits toward the actual game OUTCOME (who
ultimately won), from the perspective of whichever team is on offense for a
given play. That sidesteps the question of whether ESPN's own WP is
trustworthy as ground truth (see scripts/diagnose_ot_wp.py's OT-reliability
findings) and lets a clean apples-to-apples comparison happen: at the exact
same set of held-out plays, whose predicted win probability was closer to
the real final outcome -- ESPN's live WP, the existing score+time+line
baseline, or this new model with down/distance/field position added?

Only plays with a valid scrimmage down (1-4) and known distance/yardsToEndzone
are used -- this excludes kickoffs, PATs, timeouts, and period-boundary
plays, which carry placeholder down/distance values that would just be noise.

Train/test split is by GAME, not by play, so held-out evaluation can't leak
same-game plays across the split (plays within a game are highly correlated).

Requires numpy/pandas/statsmodels (see requirements-dev.txt), same as
fit_wp_quarter_model.py. Read-only -- writes nothing to the DB, and does not
(yet) regenerate a src/wp_*.py module; that's a follow-up once/if this shows
the added fields earn their keep.

Usage:
    venv/bin/python3 scripts/fit_wp_situational_model.py
"""
import math
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
MAX_DISTANCE = 30  # clip rare penalty-inflated distances (half-the-distance etc.)
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
        SELECT game_id, home_team_id, away_team_id, home_score, away_score, initial_home_wp
        FROM games
        WHERE completed = 1 AND detail_fetched = 1
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND home_score != away_score
          AND initial_home_wp IS NOT NULL
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

        ot_counter = {}
        for drive in _iter_drives(raw):
            off_team = str(drive.get("team", {}).get("id", ""))
            if not off_team:
                continue
            off_is_home = off_team == g["home_team_id"]

            for play in drive.get("plays", []):
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

                is_ot = period > 4
                if is_ot:
                    seconds_remaining_reg = 0
                    n = ot_counter.get(period, 0)
                    elapsed_seconds = 3600 + (period - 5) * 100 + n
                    ot_counter[period] = n + 1
                else:
                    secs_remaining = _parse_clock((play.get("clock") or {}).get("displayValue") or "")
                    if secs_remaining is None:
                        continue
                    elapsed_seconds = (period - 1) * 900 + (900 - secs_remaining)
                    seconds_remaining_reg = max(0, 3600 - elapsed_seconds)

                offense_score = home_score if off_is_home else away_score
                defense_score = away_score if off_is_home else home_score
                offense_pregame_wp = g["initial_home_wp"] if off_is_home else 1 - g["initial_home_wp"]
                offense_won = home_won if off_is_home else 1 - home_won

                play_id = str(play.get("id", ""))
                espn_wp_home = espn_wp_by_play.get(play_id)
                espn_wp_offense = (
                    espn_wp_home if espn_wp_home is None
                    else (espn_wp_home if off_is_home else 1 - espn_wp_home)
                )

                rows.append({
                    "game_id": g["game_id"],
                    "down": down,
                    "distance": min(distance, MAX_DISTANCE),
                    "yards_to_go": yards_to_go,
                    "goal_to_go": int(distance >= yards_to_go),
                    "score_diff": offense_score - defense_score,
                    "seconds_remaining_reg": seconds_remaining_reg,
                    "elapsed_seconds": elapsed_seconds,
                    "is_ot": int(is_ot),
                    "offense_pregame_wp": offense_pregame_wp,
                    "home_score": home_score,
                    "away_score": away_score,
                    "off_is_home": int(off_is_home),
                    "home_win": home_won,
                    "offense_won": offense_won,
                    "espn_wp_offense": espn_wp_offense,
                })

    return pd.DataFrame(rows), n_games_used, len(games)


def make_design(df):
    time_frac = df["seconds_remaining_reg"] / 3600.0
    urgency = df["score_diff"] * (1 - time_frac)
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": df["score_diff"],
        "time_remaining_frac": time_frac,
        "is_ot": df["is_ot"],
        "urgency": urgency,
        "down2": (df["down"] == 2).astype(int),
        "down3": (df["down"] == 3).astype(int),
        "down4": (df["down"] == 4).astype(int),
        "distance": df["distance"],
        "yards_to_go": df["yards_to_go"],
        "goal_to_go": df["goal_to_go"],
    })
    return sm.add_constant(X)


def fit(df_train):
    X = make_design(df_train)
    y = df_train["offense_won"]
    model = sm.Logit(y, X).fit(disp=0, maxiter=100)
    return model


def brier(pred, outcome):
    return float(np.mean((pred - outcome) ** 2))


def log_loss(pred, outcome):
    p = np.clip(pred, EPS, 1 - EPS)
    return float(-np.mean(outcome * np.log(p) + (1 - outcome) * np.log(1 - p)))


def baseline_predict_offense(df):
    preds = np.empty(len(df))
    for i, r in enumerate(df.itertuples()):
        home_wp = wp_baseline.predict_wp_elapsed(r.elapsed_seconds, r.offense_pregame_wp if r.off_is_home else 1 - r.offense_pregame_wp, r.home_score - r.away_score)
        preds[i] = home_wp if r.off_is_home else 1 - home_wp
    return preds


def evaluate(name, pred, outcome, mask=None):
    if mask is not None:
        pred, outcome = pred[mask], outcome[mask]
    n = len(outcome)
    print(f"  {name:<28s} n={n:>7d}  brier={brier(pred, outcome):.4f}  logloss={log_loss(pred, outcome):.4f}")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)
    print("Building play-level dataset from archived raw JSON (this decompresses ~3.6k games)...")
    df, n_games_used, n_games_total = build_dataset(conn)
    print(f"Games with raw JSON available: {n_games_used}/{n_games_total}")
    print(f"Scrimmage-down plays collected: {len(df)}\n")

    game_ids = df["game_id"].unique().tolist()
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(game_ids)
    n_test = int(len(game_ids) * TEST_FRACTION)
    test_ids = set(game_ids[:n_test])
    is_test = df["game_id"].isin(test_ids)
    df_train, df_test = df[~is_test].reset_index(drop=True), df[is_test].reset_index(drop=True)
    print(f"Train games: {len(game_ids) - n_test}  ({len(df_train)} plays)")
    print(f"Test games:  {n_test}  ({len(df_test)} plays)\n")

    print("Fitting situational logistic model (offense perspective) on train games...")
    model = fit(df_train)
    print(model.summary())

    X_test = make_design(df_test)
    new_model_pred = model.predict(X_test).to_numpy()

    baseline_pred = baseline_predict_offense(df_test)
    outcome = df_test["offense_won"].to_numpy().astype(float)
    espn_pred = df_test["espn_wp_offense"].to_numpy(dtype=float)
    espn_mask = ~np.isnan(espn_pred)

    print("\n=== Held-out evaluation (lower is better) ===")
    evaluate("ESPN live WP", espn_pred, outcome, mask=espn_mask)
    evaluate("score+time+line baseline", baseline_pred, outcome)
    evaluate("+ down/distance/field pos", new_model_pred, outcome)

    print("\nSame three, restricted to plays where ESPN's own WP is also available (fairest comparison):")
    evaluate("ESPN live WP", espn_pred, outcome, mask=espn_mask)
    evaluate("score+time+line baseline", baseline_pred, outcome, mask=espn_mask)
    evaluate("+ down/distance/field pos", new_model_pred, outcome, mask=espn_mask)

    print("\nSame three, restricted to 3rd/4th down (situational fields should matter most here):")
    late_down_mask = (df_test["down"] >= 3).to_numpy()
    evaluate("ESPN live WP", espn_pred, outcome, mask=espn_mask & late_down_mask)
    evaluate("score+time+line baseline", baseline_pred, outcome, mask=late_down_mask)
    evaluate("+ down/distance/field pos", new_model_pred, outcome, mask=late_down_mask)


if __name__ == "__main__":
    main()
