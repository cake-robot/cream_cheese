"""
Validation harness for the Burke-2007-style non-parametric baseline
(src/wp_burke_baseline.py) -- see plans/algorithm/wp_burke_baseline.md.

Compares three things on the IDENTICAL held-out test games (same corpus,
same 80/20 game-level split used by every Model C comparison this session):
  1. Model C in COIN-FLIP mode (offense_pregame_wp forced to 0.5) -- the fair
     comparison, since the Burke baseline has no pregame-WP concept at all.
  2. The Burke baseline (src/wp_burke_baseline.py).
  3. ESPN's own live WP (has team strength baked in -- included for context,
     not as a fair fight; the point is how much of ESPN's edge each of 1/2
     closes, not whether either beats ESPN outright).

Reports the six slices from the plan doc and, for each, what fraction of
ESPN's edge over Model-C-coinflip the Burke baseline recovers:
    (burke_brier - modelc_brier) / (espn_brier - modelc_brier)
close to 1 => the gap was functional form; close to 0 => the gap survives a
totally different modeling approach, so it's more likely team-strength/timeouts.

Usage:
    venv/bin/python3 scripts/compare_wp_burke_vs_model_c.py [path/to/cfb.db]
"""
import random
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")

from src import db, espn, wp_burke_baseline, wp_situational

EPS = 1e-4
TEST_FRACTION = 0.2
RANDOM_SEED = 20260830


def build_dataset(conn):
    """Same as build_wp_burke_baseline.py's build_dataset(), plus play_id
    (to join ESPN's own WP, same technique as scripts/compare_wp_vs_espn.py)."""
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
            offense_won = home_won if off_is_home else 1 - home_won

            espn_wp_home = espn_wp_by_play.get(play["play_id"])
            espn_wp_offense = None if espn_wp_home is None else (espn_wp_home if off_is_home else 1 - espn_wp_home)

            rows.append({
                "game_id": g["game_id"], "down": play["down"], "distance": play["distance"],
                "yards_to_go": play["yards_to_go"], "score_diff": offense_score - defense_score,
                "elapsed_seconds": play["elapsed_seconds"], "offense_won": offense_won,
                "espn_wp_offense": espn_wp_offense,
            })

    return pd.DataFrame(rows), n_games_used, len(games)


def brier(pred, outcome):
    return float(np.mean((pred - outcome) ** 2))


def log_loss(pred, outcome):
    p = np.clip(pred, EPS, 1 - EPS)
    return float(-np.mean(outcome * np.log(p) + (1 - outcome) * np.log(1 - p)))


def evaluate_slice(label, preds, outcome, mask):
    row = {"slice": label}
    for name, p in preds.items():
        sub_pred, sub_out = p[mask], outcome[mask]
        n = len(sub_out)
        if n == 0:
            row[name] = None
            continue
        row[name] = (brier(sub_pred, sub_out), n)
    return row


def print_slice(row):
    n = next((v[1] for v in row.values() if isinstance(v, tuple)), 0)
    print(f"\n{row['slice']}  (n={n})")
    for name, v in row.items():
        if name == "slice" or v is None:
            continue
        b, _ = v
        print(f"  {name:<22s} brier={b:.4f}")
    mc = row.get("Model C (coin-flip)")
    burke = row.get("Burke baseline")
    espn_v = row.get("ESPN (live)")
    if mc and burke and espn_v and (espn_v[0] - mc[0]) != 0:
        frac = (mc[0] - burke[0]) / (mc[0] - espn_v[0])
        print(f"  --> Burke recovers {frac*100:.0f}% of ESPN's edge over Model C on this slice")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building dataset (with ESPN WP joined by play_id)...")
    df, n_games_used, n_games_total = build_dataset(conn)
    print(f"Games with raw JSON available (non-OT): {n_games_used}/{n_games_total}")
    print(f"Regulation scrimmage-down plays: {len(df)}\n")

    game_ids = df["game_id"].unique().tolist()
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(game_ids)
    n_test = int(len(game_ids) * TEST_FRACTION)
    test_ids = set(game_ids[:n_test])
    df_test = df[df["game_id"].isin(test_ids)].reset_index(drop=True)
    print(f"Test games: {n_test}  ({len(df_test)} plays)\n")

    outcome = df_test["offense_won"].to_numpy().astype(float)
    secs_left = (3600 - df_test["elapsed_seconds"]).to_numpy()
    ytg = df_test["yards_to_go"].to_numpy()
    sd = df_test["score_diff"].to_numpy()
    down = df_test["down"].to_numpy()
    distance = df_test["distance"].to_numpy()
    espn_pred = df_test["espn_wp_offense"].to_numpy(dtype=float)
    espn_mask = ~np.isnan(espn_pred)

    print("Scoring Model C (coin-flip mode) and the Burke baseline on every held-out play...")
    modelc_pred = np.array([
        wp_situational.coinflip_wp_offense(
            down=int(r.down), distance=int(r.distance), yards_to_go=int(r.yards_to_go),
            score_diff=int(r.score_diff), elapsed_seconds=int(r.elapsed_seconds),
        ) for r in df_test.itertuples()
    ])
    burke_pred = np.array([
        wp_burke_baseline.predict_wp_offense(
            down=int(r.down), yards_to_go=int(r.yards_to_go),
            score_diff=int(r.score_diff), elapsed_seconds=int(r.elapsed_seconds),
        ) for r in df_test.itertuples()
    ])

    preds = {"Model C (coin-flip)": modelc_pred, "Burke baseline": burke_pred, "ESPN (live)": espn_pred}

    print("\n" + "=" * 90)
    print("All six slices, ESPN-matched rows only (fair 3-way comparison)")
    print("=" * 90)

    print_slice(evaluate_slice("1. ALL plays", preds, outcome, espn_mask))

    mask_2min = (secs_left <= 120) & espn_mask
    print_slice(evaluate_slice("2. Last 2 minutes of regulation", preds, outcome, mask_2min))

    mask_30s = (secs_left <= 30) & espn_mask
    print_slice(evaluate_slice("3. Last 30 seconds of regulation", preds, outcome, mask_30s))

    fresh_downs = (down == 1) & (distance == 10)
    one_score_lead = (sd >= 1) & (sd <= 8)
    narrow_mask = fresh_downs & one_score_lead & (secs_left <= 30) & espn_mask
    print_slice(evaluate_slice("4. Leading 1-8, fresh 1st & 10, <=30s left", preds, outcome, narrow_mask))

    down4_mask = (down == 4)
    fg_leverage_mask = down4_mask & (ytg <= 40) & (secs_left <= 300) & (np.abs(sd) <= 8) & espn_mask
    print_slice(evaluate_slice("5. 4th down, in FG range, high leverage (<=5min, within 1 score)", preds, outcome, fg_leverage_mask))

    trailing_low_time = (sd < 0) & (sd >= -8) & (secs_left <= 30)
    goalline_mask = trailing_low_time & (ytg <= 10) & espn_mask
    print_slice(evaluate_slice("6. Trailing offense, <=30s left, ytg<=10 (goal-line blind spot)", preds, outcome, goalline_mask))

    print("\n" + "=" * 90)
    print("Anchor examples")
    print("=" * 90)
    examples = [
        ("401521330 OSU 1st & goal from the 1, trailing by 4, 7s left (ESPN read: 65.1%)",
         dict(down=1, yards_to_go=1, score_diff=-4, elapsed_seconds=3593)),
        ("401442015 OSU 4th & 11 FG attempt, trailing by 1, 3s left",
         dict(down=4, yards_to_go=32, score_diff=-1, elapsed_seconds=3597)),
        ("Hail Mary from the 44, trailing by 4, 0s left (should stay low)",
         dict(down=1, yards_to_go=44, score_diff=-4, elapsed_seconds=3600)),
    ]
    for label, kw in examples:
        mc = wp_situational.coinflip_wp_offense(distance=10, **kw)
        bu = wp_burke_baseline.predict_wp_offense(**kw)
        print(f"\n  {label}")
        print(f"    Model C (coin-flip) = {mc*100:.1f}%   Burke baseline = {bu*100:.1f}%")


if __name__ == "__main__":
    main()
