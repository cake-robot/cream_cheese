"""
Exploratory (read-only, no DB/module writes): can a down4 x yards_to_go
interaction teach Model C that 4th down behaves very differently depending
on field position -- without adding an explicit "this is a field goal
attempt" feature at all?

Prompted by a real example (401442015, OSU@Georgia 2022 Peach Bowl): as
OSU's final drive advances into makable field-goal range (a 27-yard run to
the Georgia 31), the model correctly credits it, but then Georgia's WP
keeps climbing through 2nd/3rd/4th down AT THE SAME SPOT ON THE FIELD --
down=4 with 32 yards to go (a ~49-yard FG try) reads OSU's win probability
at just 18.6%, well before the kick even happens. Root cause: `down4` in
the current production model (src/wp_situational.py, "round 2" --
see scripts/compare_wp_endgame_calibration.py) is a flat additive dummy
with no interaction with yards_to_go, so it applies the SAME penalty to a
real, low-odds 4th-and-long conversion attempt at midfield and a
routine, much-higher-odds kick attempt once in range. The training data
already contains real field-goal attempts (`Field Goal Good`/`Field Goal
Missed`/etc. are NOT excluded from extract_situational_plays -- unlike
Timeout/Kickoff, they carry a genuine pre-snap down/distance/field-
position read) but the model currently has no way to express that a
4th-down-in-range subpopulation behaves differently, because down4 never
interacts with distance-to-goal.

This is a test of whether the regression can "sus this out" on its own
once given the right interaction term -- NOT an explicit is_field_goal_
attempt feature (that's a separate, bigger change requiring play-type
data that isn't currently threaded through espn.extract_situational_plays
at all). Candidates:
  1. current (production, round 2 -- scores_needed urgency, flat down
     dummies)
  2. + down4 x yards_to_go (linear interaction -- lets the down-4 penalty
     shrink continuously as the offense gets closer to the end zone)
  3. + down4 x yards_to_go + down4 x yards_to_go^2 (adds curvature, since
     real FG-make probability isn't linear in distance -- it's fairly
     flat up close and drops off increasingly fast past ~40 yards)
  4. + down3 x yards_to_go too (symmetry check -- does 3rd-and-long also
     need this, or is the effect down-4-specific since that's where the
     kick-vs-go-for-it decision actually happens)

Usage:
    venv/bin/python3 scripts/compare_wp_fg_range_interaction.py [path/to/cfb.db]
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
    """Exact reproduction of production src/wp_situational.py (round 2)."""
    time_frac = _base_fields(df)
    sn = df["score_diff"].map(scores_needed)
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": df["score_diff"],
        "time_remaining_frac": time_frac,
        "down2": (df["down"] == 2).astype(int),
        "down3": (df["down"] == 3).astype(int),
        "down4": (df["down"] == 4).astype(int),
        "distance": df["distance"],
        "yards_to_go": df["yards_to_go"],
        "sn_urgency": sn * (1 - time_frac),
        "sn_urgency3": sn * (1 - time_frac) ** 3,
        "sn_time_remaining_frac2": time_frac ** 2,
        "sn_inv_sqrt_urgency": sn / np.sqrt(df["seconds_remaining_reg"] + 5.0),
    })
    return sm.add_constant(X, has_constant="add")


def design_down4_interaction(df):
    X = design_current(df)
    down4 = (df["down"] == 4).astype(int)
    X["down4_x_ytg"] = down4 * df["yards_to_go"]
    return X


def design_down4_interaction_quad(df):
    X = design_down4_interaction(df)
    down4 = (df["down"] == 4).astype(int)
    X["down4_x_ytg2"] = down4 * (df["yards_to_go"] ** 2) / 100.0  # scaled to keep coef magnitudes sane
    return X


def design_down34_interaction_quad(df):
    X = design_down4_interaction_quad(df)
    down3 = (df["down"] == 3).astype(int)
    X["down3_x_ytg"] = down3 * df["yards_to_go"]
    return X


CANDIDATES = {
    "current (production)": design_current,
    "+ down4 x ytg": design_down4_interaction,
    "+ down4 x ytg + ytg^2": design_down4_interaction_quad,
    "+ down3 x ytg too": design_down34_interaction_quad,
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
        print(f"  {name:<30s} n=0")
        return
    print(f"  {name:<30s} n={n:>6d}  brier={brier(pred, outcome):.4f}  logloss={log_loss(pred, outcome):.4f}")


def predict_with_model(model, design_fn, row):
    df_row = pd.DataFrame([row])
    X = design_fn(df_row)
    return float(model.predict(X).iloc[0])


# OSU's actual final drive in 401442015 (offense perspective, OSU trailing
# by 1 throughout -- score_diff = -1). Hand-transcribed from
# extract_situational_plays() output.
OSU_DRIVE = [
    {"label": "1st & 10 at own 25 (post-kickoff)", "down": 1, "distance": 10, "yards_to_go": 75, "score_diff": -1, "seconds_remaining_reg": 42, "offense_pregame_wp": 0.5},
    {"label": "2nd & 5 (own 30ish)", "down": 2, "distance": 5, "yards_to_go": 70, "score_diff": -1, "seconds_remaining_reg": 39, "offense_pregame_wp": 0.5},
    {"label": "1st & 10 at OSU 42 (12yd gain)", "down": 1, "distance": 10, "yards_to_go": 58, "score_diff": -1, "seconds_remaining_reg": 28, "offense_pregame_wp": 0.5},
    {"label": "1st & 10 at GEO 31 (27yd RUN)", "down": 1, "distance": 10, "yards_to_go": 31, "score_diff": -1, "seconds_remaining_reg": 19, "offense_pregame_wp": 0.5},
    {"label": "2nd & 11 at GEO 32 (1yd loss)", "down": 2, "distance": 11, "yards_to_go": 32, "score_diff": -1, "seconds_remaining_reg": 15, "offense_pregame_wp": 0.5},
    {"label": "3rd & 11 at GEO 32 (incomplete)", "down": 3, "distance": 11, "yards_to_go": 32, "score_diff": -1, "seconds_remaining_reg": 8, "offense_pregame_wp": 0.5},
    {"label": "4th & 11 at GEO 32 -- THE FG ATTEMPT", "down": 4, "distance": 11, "yards_to_go": 32, "score_diff": -1, "seconds_remaining_reg": 3, "offense_pregame_wp": 0.5},
]


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

    models = {}
    print("=== Fitting each candidate on the train split ===")
    for name, design_fn in CANDIDATES.items():
        model = fit(df_train, design_fn)
        models[name] = model
        print(f"  {name:<30s} pseudo-R^2={model.prsquared:.4f}")
        if "down4" in name or "down3" in name:
            for term in ["down4_x_ytg", "down4_x_ytg2", "down3_x_ytg"]:
                if term in model.params.index:
                    print(f"      {term:<16s} coef={model.params[term]:+.5f}  p={model.pvalues[term]:.4f}")

    print("\n=== Held-out evaluation, ALL plays (must not regress) ===")
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name, pred, outcome)

    print("\n=== Held-out evaluation, 4th-down plays only, split by field position ===")
    down4_mask = (df_test["down"] == 4).to_numpy()
    in_range_mask = down4_mask & (df_test["yards_to_go"] <= 40).to_numpy()
    out_range_mask = down4_mask & (df_test["yards_to_go"] > 40).to_numpy()
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name + " [in FG range, ytg<=40]", pred, outcome, mask=in_range_mask)
    for name, design_fn in CANDIDATES.items():
        pred = models[name].predict(design_fn(df_test)).to_numpy()
        evaluate(name + " [out of range, ytg>40]", pred, outcome, mask=out_range_mask)

    print("\n=== OSU's actual final drive in 401442015, predicted OFFENSE (OSU) win% under each candidate ===")
    for play in OSU_DRIVE:
        row = {k: v for k, v in play.items() if k != "label"}
        vals = []
        for name, design_fn in CANDIDATES.items():
            p = predict_with_model(models[name], design_fn, row)
            vals.append(f"{name}={p*100:.1f}%")
        print(f"  {play['label']:<42s} " + "  ".join(vals))


if __name__ == "__main__":
    main()
