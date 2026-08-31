"""
Ad hoc follow-up (read-only): translates Model C's (scripts/compare_wp_accuracy.py)
Brier/log-loss numbers into more intuitive terms for someone used to R^2 --
plain classification accuracy, AUC ("how often does the model correctly rank
the actual winner above the actual loser"), and a calibration table (grouped
by predicted-probability bucket, does the actual win rate in that bucket
match the stated probability?). Reuses compare_wp_accuracy.py's dataset
build and Model C fit on an identical train/test split so these numbers are
directly comparable to that script's Brier/log-loss report.

Usage:
    venv/bin/python3 scripts/model_c_layman_metrics.py [path/to/cfb.db]
"""
import random
import sys

import numpy as np
from scipy.stats import rankdata

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from src import db
import compare_wp_accuracy as cwa

RANDOM_SEED = cwa.RANDOM_SEED
TEST_FRACTION = cwa.TEST_FRACTION


def accuracy(pred, outcome):
    return float(np.mean((pred >= 0.5).astype(int) == outcome))


def auc(pred, outcome):
    """Probability a randomly-chosen winner is ranked above a randomly-chosen
    loser by `pred` -- the standard rank-based (Mann-Whitney U) AUC formula,
    no sklearn required."""
    ranks = rankdata(pred)
    n_pos = int(outcome.sum())
    n_neg = len(outcome) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    sum_ranks_pos = ranks[outcome == 1].sum()
    return float((sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def calibration_table(pred, outcome, n_bins=10):
    bins = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(pred, bins) - 1, 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        mask = idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        rows.append((bins[b], bins[b + 1], n, float(pred[mask].mean()), float(outcome[mask].mean())))
    return rows


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Rebuilding the same regulation-only dataset/split as compare_wp_accuracy.py...")
    df, _n_games = cwa.build_dataset(conn)

    game_ids = df["game_id"].unique().tolist()
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(game_ids)
    n_test = int(len(game_ids) * TEST_FRACTION)
    test_ids = set(game_ids[:n_test])
    is_test = df["game_id"].isin(test_ids)
    df_train, df_test = df[~is_test].reset_index(drop=True), df[is_test].reset_index(drop=True)

    print("Fitting Model C...")
    model_c = cwa.fit_model_c(df_train)

    outcome = df_test["home_win"].to_numpy().astype(int)
    espn_pred = df_test["espn_wp_home"].to_numpy(dtype=float)
    c_pred_offense = model_c.predict(cwa.make_design_offense(df_test)).to_numpy()
    off_is_home = df_test["off_is_home"].to_numpy().astype(bool)
    c_pred_home = np.where(off_is_home, c_pred_offense, 1 - c_pred_offense)

    favorite_pred = (df_test["initial_home_wp"].to_numpy() >= 0.5).astype(int)

    print("\n=== Classification accuracy (treat >=50% as 'the model picks this team') ===")
    print(f"  Always pick the pregame favorite (no in-game info at all): {np.mean(favorite_pred == outcome):.1%}")
    print(f"  ESPN live WP:                                              {accuracy(espn_pred, outcome):.1%}")
    print(f"  Model C (ours, situational):                               {accuracy(c_pred_home, outcome):.1%}")

    print("\n=== AUC: probability the model ranks a random actual-winner above a random actual-loser ===")
    print(f"  ESPN live WP:  {auc(espn_pred, outcome):.4f}")
    print(f"  Model C:       {auc(c_pred_home, outcome):.4f}")
    print("  (0.500 = coin flip / no skill,  1.000 = perfect ranking)")

    print("\n=== Calibration: when Model C says a team has an X% win chance, how often do they actually win? ===")
    print(f"  {'bucket':<12s}{'n':>8s}{'model says':>14s}{'actually won':>16s}")
    for lo, hi, n, mean_pred, mean_actual in calibration_table(c_pred_home, outcome):
        print(f"  {lo:.0%}-{hi:.0%}{'':<5s}{n:>8d}{mean_pred:>13.1%}{mean_actual:>16.1%}")


if __name__ == "__main__":
    main()
