"""
Follow-up (read-only): tests whether Model C's mild low/high-probability
miscalibration (see scripts/model_c_layman_metrics.py's calibration table --
overconfident on ~15-25% underdogs, underconfident on ~75-97% favorites, the
classic "compressed toward 50/50" signature of a slightly-too-simple linear
model) is fixable with the standard, low-risk fix for exactly that shape:
Platt scaling -- refit logit(actual) ~ a + b*logit(model_pred) on a slice of
data Model C never trained on, then remap its raw output through that.

Three-way split BY GAME (not just train/test), so recalibration is fit on
data Model C never saw, and final evaluation is on a THIRD slice untouched
by either fitting step:
  - train (60%): fits Model C itself
  - calib (20%): Model C's predictions here fit the 2-parameter recalibration
  - test  (20%): final, twice-held-out evaluation of raw vs. recalibrated

Usage:
    venv/bin/python3 scripts/model_c_recalibration.py [path/to/cfb.db]
"""
import random
import sys

import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from src import db
import compare_wp_accuracy as cwa
from model_c_layman_metrics import accuracy, auc, calibration_table

RANDOM_SEED = cwa.RANDOM_SEED


def brier(pred, outcome):
    return float(np.mean((pred - outcome) ** 2))


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building dataset...")
    df, _ = cwa.build_dataset(conn)

    game_ids = df["game_id"].unique().tolist()
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(game_ids)
    n = len(game_ids)
    n_test = int(n * 0.2)
    n_calib = int(n * 0.2)
    test_ids = set(game_ids[:n_test])
    calib_ids = set(game_ids[n_test:n_test + n_calib])
    train_ids = set(game_ids[n_test + n_calib:])

    df_train = df[df["game_id"].isin(train_ids)].reset_index(drop=True)
    df_calib = df[df["game_id"].isin(calib_ids)].reset_index(drop=True)
    df_test = df[df["game_id"].isin(test_ids)].reset_index(drop=True)
    print(f"train games={len(train_ids)} ({len(df_train)} plays)  "
          f"calib games={len(calib_ids)} ({len(df_calib)} plays)  "
          f"test games={len(test_ids)} ({len(df_test)} plays)\n")

    print("Fitting Model C on train only...")
    model_c = cwa.fit_model_c(df_train)

    def predict_home(d):
        p_off = model_c.predict(cwa.make_design_offense(d)).to_numpy()
        off_is_home = d["off_is_home"].to_numpy().astype(bool)
        return np.where(off_is_home, p_off, 1 - p_off)

    calib_pred = predict_home(df_calib)
    calib_outcome = df_calib["home_win"].to_numpy().astype(float)

    print("Fitting Platt-scaling recalibration on the calib slice (Model C never trained on this)...")
    X_calib = sm.add_constant(pd.DataFrame({"logit_pred": cwa.logit(calib_pred)}))
    platt = sm.Logit(calib_outcome, X_calib).fit(disp=0)
    print(platt.summary())

    test_pred_raw = predict_home(df_test)
    test_outcome = df_test["home_win"].to_numpy().astype(float)
    X_test = sm.add_constant(pd.DataFrame({"logit_pred": cwa.logit(test_pred_raw)}))
    test_pred_calibrated = platt.predict(X_test).to_numpy()

    print("\n=== Test-set accuracy, raw vs. recalibrated (a THIRD held-out slice, untouched by both fits) ===")
    print(f"  Brier    raw={brier(test_pred_raw, test_outcome):.4f}   recalibrated={brier(test_pred_calibrated, test_outcome):.4f}")
    print(f"  AUC      raw={auc(test_pred_raw, test_outcome):.4f}   recalibrated={auc(test_pred_calibrated, test_outcome):.4f}")
    print(f"  Accuracy raw={accuracy(test_pred_raw, test_outcome):.1%}   recalibrated={accuracy(test_pred_calibrated, test_outcome):.1%}")

    print("\n=== Calibration table, RAW Model C ===")
    print(f"  {'bucket':<12s}{'n':>8s}{'model says':>14s}{'actually won':>16s}")
    for lo, hi, n_, mean_pred, mean_actual in calibration_table(test_pred_raw, test_outcome):
        print(f"  {lo:.0%}-{hi:.0%}{'':<5s}{n_:>8d}{mean_pred:>13.1%}{mean_actual:>16.1%}")

    print("\n=== Calibration table, RECALIBRATED ===")
    print(f"  {'bucket':<12s}{'n':>8s}{'model says':>14s}{'actually won':>16s}")
    for lo, hi, n_, mean_pred, mean_actual in calibration_table(test_pred_calibrated, test_outcome):
        print(f"  {lo:.0%}-{hi:.0%}{'':<5s}{n_:>8d}{mean_pred:>13.1%}{mean_actual:>16.1%}")


if __name__ == "__main__":
    main()
