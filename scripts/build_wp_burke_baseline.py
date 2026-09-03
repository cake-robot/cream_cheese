"""
Dev-only fitting script for the Burke-2007-style non-parametric win
probability baseline -- see plans/algorithm/wp_burke_baseline.md for the
full design rationale. Generates src/wp_burke_baseline_grid.json (a smoothed
lookup table) which src/wp_burke_baseline.py loads at runtime.

Unlike src/wp_situational.py's dozen logistic-regression coefficients, this
model has no parametric form at all: bin historical plays into a
(down, yards_to_go, scores_needed, elapsed_seconds) grid, take the empirical
win rate per cell, and smooth with a Gaussian filter (weighted by cell count
via smooth(wins)/smooth(n)) to handle the ~3.4 rows/cell average sparsity.

Deliberately NO team-strength/pregame-WP input and NO timeouts -- see the
plan doc's "Scope" section for why that's the point, not an oversight.

Requires numpy/scipy (dev-only, see requirements-dev.txt) -- NOT a runtime
dependency of src/wp_burke_baseline.py itself (that module is pure Python,
loading only the generated JSON grid).

Usage:
    venv/bin/python3 scripts/build_wp_burke_baseline.py [path/to/cfb.db]
"""
import datetime
import itertools
import json
import math
import random
import sys

import numpy as np
from scipy.ndimage import gaussian_filter

sys.path.insert(0, ".")

from src import db, espn

OUTPUT_GRID_PATH = "src/wp_burke_baseline_grid.json"
TEST_FRACTION = 0.2
RANDOM_SEED = 20260830

# Binning -- see plan doc's "Dimensions and binning" table.
YTG_BIN_WIDTH = 2
YTG_MAX = 100
N_YTG_BINS = YTG_MAX // YTG_BIN_WIDTH  # 50

SN_MIN, SN_MAX = -6, 6
N_SN_BINS = SN_MAX - SN_MIN + 1  # 13

TIME_BIN_WIDTH = 60
TIME_MAX = 3600
N_TIME_BINS = TIME_MAX // TIME_BIN_WIDTH  # 60

DOWNS = [1, 2, 3, 4]

# Candidate smoothing bandwidths (in BINS, not real units) to grid-search,
# picked via held-out Brier on a train-side split (never the final test
# split) -- see plan doc's "Smoothing" section.
SIGMA_CANDIDATES = [
    (0.5, 0.5, 0.5),
    (0.75, 0.5, 0.75),
    (1.0, 0.5, 1.0),
    (1.0, 0.75, 1.0),
    (1.5, 1.0, 1.5),
    (2.0, 1.0, 2.0),
    (2.5, 1.5, 2.5),
    (3.0, 1.5, 3.0),
    (4.0, 2.0, 4.0),
    (6.0, 2.5, 6.0),
]  # (sigma_ytg, sigma_sn, sigma_time)


def scores_needed(score_diff):
    if score_diff == 0:
        return 0
    return int(math.copysign(math.ceil(abs(score_diff) / 8.0), score_diff))


def ytg_bin(yards_to_go):
    return min(int(yards_to_go) // YTG_BIN_WIDTH, N_YTG_BINS - 1)


def sn_bin(sn):
    return min(max(sn, SN_MIN), SN_MAX) - SN_MIN


def time_bin(elapsed_seconds):
    return min(int(elapsed_seconds) // TIME_BIN_WIDTH, N_TIME_BINS - 1)


def build_dataset(conn):
    """Same corpus/target as every Model C comparison this session --
    espn.extract_situational_plays(), non-OT games, offense perspective,
    target = actual game outcome. Returns a plain list of dicts (not a
    DataFrame -- this script grids/sums rather than fitting a regression, so
    pandas isn't needed)."""
    games = conn.execute("""
        SELECT game_id, home_team_id, home_score, away_score
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
            offense_won = home_won if off_is_home else 1 - home_won

            rows.append({
                "game_id": g["game_id"],
                "down": play["down"],
                "yards_to_go": play["yards_to_go"],
                "score_diff": offense_score - defense_score,
                "elapsed_seconds": play["elapsed_seconds"],
                "offense_won": offense_won,
            })

    return rows, n_games_used, len(games)


def split_train_test(rows):
    game_ids = sorted({r["game_id"] for r in rows})
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(game_ids)
    n_test = int(len(game_ids) * TEST_FRACTION)
    test_ids = set(game_ids[:n_test])
    train = [r for r in rows if r["game_id"] not in test_ids]
    test = [r for r in rows if r["game_id"] in test_ids]
    return train, test, len(game_ids) - n_test, n_test


def build_raw_grids(rows):
    """Returns {down: (wins_grid, n_grid)}, each a (N_YTG_BINS, N_SN_BINS,
    N_TIME_BINS) numpy array."""
    grids = {
        d: (np.zeros((N_YTG_BINS, N_SN_BINS, N_TIME_BINS)),
            np.zeros((N_YTG_BINS, N_SN_BINS, N_TIME_BINS)))
        for d in DOWNS
    }
    for r in rows:
        d = r["down"]
        if d not in grids:
            continue
        yi = ytg_bin(r["yards_to_go"])
        si = sn_bin(scores_needed(r["score_diff"]))
        ti = time_bin(r["elapsed_seconds"])
        wins, n = grids[d]
        wins[yi, si, ti] += r["offense_won"]
        n[yi, si, ti] += 1
    return grids


def smooth_grid(wins, n, sigma):
    """smooth(wins)/smooth(n) -- the standard weighted-kernel-smoothing
    trick: a sparse cell's raw rate gets pulled toward better-populated
    neighbors' rate, proportional to how little data of its own it has."""
    smoothed_wins = gaussian_filter(wins, sigma=sigma, mode="nearest")
    smoothed_n = gaussian_filter(n, sigma=sigma, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        rate = np.where(smoothed_n > 1e-9, smoothed_wins / smoothed_n, 0.5)
    return np.clip(rate, 1e-4, 1 - 1e-4)


def predict_from_grids(smoothed, down, yards_to_go, score_diff, elapsed_seconds):
    """Trilinear interpolation into the smoothed grid for `down` -- mirrors
    exactly what src/wp_burke_baseline.py does at runtime, used here only
    for held-out evaluation during sigma selection / final reporting."""
    grid = smoothed[down]
    sn = scores_needed(score_diff)

    def axis_coord(value, bin_width, n_bins, bin_min=0):
        c = (value - bin_min) / bin_width - 0.5
        return max(0.0, min(c, n_bins - 1.0))

    yc = axis_coord(yards_to_go, YTG_BIN_WIDTH, N_YTG_BINS)
    sc = axis_coord(sn, 1, N_SN_BINS, bin_min=SN_MIN)
    tc = axis_coord(elapsed_seconds, TIME_BIN_WIDTH, N_TIME_BINS)

    y0, s0, t0 = int(math.floor(yc)), int(math.floor(sc)), int(math.floor(tc))
    y1, s1, t1 = min(y0 + 1, N_YTG_BINS - 1), min(s0 + 1, N_SN_BINS - 1), min(t0 + 1, N_TIME_BINS - 1)
    fy, fs, ft = yc - y0, sc - s0, tc - t0

    total = 0.0
    for yi, wy in ((y0, 1 - fy), (y1, fy)):
        for si, ws in ((s0, 1 - fs), (s1, fs)):
            for ti, wt in ((t0, 1 - ft), (t1, ft)):
                total += wy * ws * wt * grid[yi, si, ti]
    return total


def brier(preds, outcomes):
    return float(np.mean((np.array(preds) - np.array(outcomes)) ** 2))


def log_loss(preds, outcomes):
    p = np.clip(np.array(preds), 1e-4, 1 - 1e-4)
    o = np.array(outcomes)
    return float(-np.mean(o * np.log(p) + (1 - o) * np.log(1 - p)))


def select_sigma(train_rows):
    """Grid-search sigma on a further split of the TRAIN games only (never
    touches the held-out test split) -- fit grids on one half, evaluate
    Brier on the other, per candidate sigma."""
    game_ids = sorted({r["game_id"] for r in train_rows})
    rng = random.Random(RANDOM_SEED + 1)
    rng.shuffle(game_ids)
    n_val = int(len(game_ids) * 0.2)
    val_ids = set(game_ids[:n_val])
    fit_rows = [r for r in train_rows if r["game_id"] not in val_ids]
    val_rows = [r for r in train_rows if r["game_id"] in val_ids]

    raw = build_raw_grids(fit_rows)
    print(f"  sigma search: fitting on {len(fit_rows)} rows, validating on {len(val_rows)} rows")

    best_sigma, best_brier = None, float("inf")
    for sigma in SIGMA_CANDIDATES:
        smoothed = {d: smooth_grid(w, n, sigma) for d, (w, n) in raw.items()}
        preds = [predict_from_grids(smoothed, r["down"], r["yards_to_go"], r["score_diff"], r["elapsed_seconds"])
                  for r in val_rows if r["down"] in DOWNS]
        outs = [r["offense_won"] for r in val_rows if r["down"] in DOWNS]
        b = brier(preds, outs)
        print(f"    sigma={sigma}  val_brier={b:.4f}")
        if b < best_brier:
            best_brier, best_sigma = b, sigma
    return best_sigma, best_brier


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building dataset (same corpus as every Model C comparison)...")
    rows, n_games_used, n_games_total = build_dataset(conn)
    print(f"Games with raw JSON available (non-OT): {n_games_used}/{n_games_total}")
    print(f"Regulation scrimmage-down plays: {len(rows)}\n")

    train_rows, test_rows, n_train_games, n_test_games = split_train_test(rows)
    print(f"Train games: {n_train_games}  ({len(train_rows)} plays)")
    print(f"Test games:  {n_test_games}  ({len(test_rows)} plays)\n")

    print("Selecting smoothing bandwidth (grid search on a train-side split)...")
    sigma, val_brier = select_sigma(train_rows)
    print(f"  chosen sigma (ytg, scores_needed, time) = {sigma}  (val brier {val_brier:.4f})\n")

    print("Fitting final grids on the FULL train split with the chosen sigma...")
    raw = build_raw_grids(train_rows)
    coverage = {d: float(np.mean(n > 0)) for d, (_, n) in raw.items()}
    print(f"  raw-cell coverage (fraction of cells with >=1 row) by down: {coverage}")
    smoothed = {d: smooth_grid(w, n, sigma) for d, (w, n) in raw.items()}

    print("\nHeld-out evaluation on the TEST split (never touched during sigma selection):")
    test_preds = [predict_from_grids(smoothed, r["down"], r["yards_to_go"], r["score_diff"], r["elapsed_seconds"])
                  for r in test_rows if r["down"] in DOWNS]
    test_outs = [r["offense_won"] for r in test_rows if r["down"] in DOWNS]
    print(f"  n={len(test_outs)}  brier={brier(test_preds, test_outs):.4f}  logloss={log_loss(test_preds, test_outs):.4f}")

    # Refit on ALL rows (train+test) for the production grid -- same
    # "held-out numbers already answered the generalization question, ship
    # the full-data fit" logic build_wp_situational_module.py uses.
    print("\nRefitting on the FULL corpus (train+test) for the shipped grid...")
    raw_full = build_raw_grids(rows)
    smoothed_full = {d: smooth_grid(w, n, sigma) for d, (w, n) in raw_full.items()}

    payload = {
        "generated_at": datetime.date.today().isoformat(),
        "n_games": n_games_used,
        "n_rows": len(rows),
        "sigma": {"yards_to_go": sigma[0], "scores_needed": sigma[1], "elapsed_seconds": sigma[2]},
        "held_out_brier": brier(test_preds, test_outs),
        "held_out_logloss": log_loss(test_preds, test_outs),
        "bins": {
            "ytg_bin_width": YTG_BIN_WIDTH, "n_ytg_bins": N_YTG_BINS,
            "sn_min": SN_MIN, "sn_max": SN_MAX, "n_sn_bins": N_SN_BINS,
            "time_bin_width": TIME_BIN_WIDTH, "n_time_bins": N_TIME_BINS,
        },
        "grids": {str(d): smoothed_full[d].tolist() for d in DOWNS},
    }
    with open(OUTPUT_GRID_PATH, "w") as f:
        json.dump(payload, f)
    print(f"\nWrote {OUTPUT_GRID_PATH}")


if __name__ == "__main__":
    main()
