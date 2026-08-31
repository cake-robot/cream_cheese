"""
Fits the production version of "Model C" (see scripts/fit_wp_situational_model.py
for the original exploratory version and its held-out evaluation) and
generates src/wp_situational.py -- a runtime-dependency-free module, same
pattern as scripts/fit_wp_quarter_model.py -> src/wp_baseline.py.

Differences from the exploratory fit:
  - REGULATION ONLY (period <= 4). No OT plays enter the training set at
    all, and there's no is_ot feature -- this model is never meant to be
    evaluated on OT, matching the decision to stop comeback_erosion from
    scoring into overtime (see plans/algorithm/watchability_algorithm_open_items.md's
    2026-08-31 entries: OT win_probability/play-by-play data has multiple
    confirmed corruption modes, and a real regulation-era comeback getting
    wiped out by a bogus post-tie OT reset is the concrete case that
    triggered this whole redesign -- excluding OT outright is simpler and
    safer than trying to sanitize around it).
  - Fit on the FULL corpus (no train/test split) -- this is the model
    actually going into production, not an accuracy comparison. The
    exploratory script's held-out Brier/log-loss numbers already answered
    "does this generalize" using the identical feature set; a production
    fit should use every available row.
  - Offense-perspective output (predict_wp_offense/coinflip_wp_offense),
    since down/distance/field position are only meaningful from the
    offense's perspective. Callers needing a home-perspective value flip
    the result based on which team is on offense (see src/scoring.py's
    comeback_erosion, which needs exactly this).

Requires numpy/pandas/statsmodels (dev-only, see requirements-dev.txt) --
deliberately NOT a runtime dependency of src/wp_situational.py itself.

Usage:
    venv/bin/python3 scripts/build_wp_situational_module.py [path/to/cfb.db]
"""
import datetime
import sys

import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, ".")

from src import db
from src.espn import _parse_clock

EPS = 1e-4
MAX_DISTANCE = 30
OUTPUT_PATH = "src/wp_situational.py"


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


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
    """Regulation-only (period <= 4) scrimmage-down plays, offense
    perspective, target = actual game outcome. Same filter fit_wp_situational_model.py
    uses (valid down 1-4, valid distance/yardsToEndzone) -- these are the
    exact plays the production model needs to handle at inference time too,
    via the shared extraction helper in src/scoring.py."""
    games = conn.execute("""
        SELECT game_id, home_team_id, home_score, away_score, initial_home_wp
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

        for drive in _iter_drives(raw):
            off_team = str(drive.get("team", {}).get("id", ""))
            if not off_team:
                continue
            off_is_home = off_team == g["home_team_id"]

            for play in drive.get("plays", []):
                period = (play.get("period") or {}).get("number")
                if period is None or period > 4:  # regulation only -- OT excluded
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
                seconds_remaining_reg = max(0, 3600 - elapsed_seconds)

                offense_score = home_score if off_is_home else away_score
                defense_score = away_score if off_is_home else home_score
                offense_pregame_wp = g["initial_home_wp"] if off_is_home else 1 - g["initial_home_wp"]
                offense_won = home_won if off_is_home else 1 - home_won

                rows.append({
                    "down": down,
                    "distance": min(distance, MAX_DISTANCE),
                    "yards_to_go": yards_to_go,
                    "goal_to_go": int(distance >= yards_to_go),
                    "score_diff": offense_score - defense_score,
                    "seconds_remaining_reg": seconds_remaining_reg,
                    "offense_pregame_wp": offense_pregame_wp,
                    "offense_won": offense_won,
                })

    return pd.DataFrame(rows), n_games_used, len(games)


def make_design(df):
    time_frac = df["seconds_remaining_reg"] / 3600.0
    urgency = df["score_diff"] * (1 - time_frac)
    X = pd.DataFrame({
        "logit_offense_pregame_wp": logit(df["offense_pregame_wp"].to_numpy()),
        "score_diff": df["score_diff"],
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


def fit(df):
    X = make_design(df)
    y = df["offense_won"]
    return sm.Logit(y, X).fit(disp=0, maxiter=100)


MODULE_TEMPLATE = '''"""
AUTO-GENERATED by scripts/build_wp_situational_module.py on {generated_at} --
do not hand-edit. Re-run that script (after pulling more seasons, etc.) to
regenerate this file; it always overwrites, not patches.

Fitted on {n_games} completed games, regulation plays only (period 1-4,
{n_rows} scrimmage-down plays) -- OT is deliberately excluded from both
training and the intended use of this module (see the fitting script's
docstring). Offense-perspective logistic regression (target = actual game
outcome, not ESPN's own WP) -- McFadden pseudo-R^2={pseudo_r2:.4f}.

predict_wp_offense()/coinflip_wp_offense() take the offense's own down/
distance/field position plus score/time/pregame WP and return the win
probability for the team CURRENTLY ON OFFENSE. Callers needing a
home-perspective value must flip the result based on which team has the
ball -- see src/scoring.py's comeback_erosion for the wrapper that does
this.
"""
import math

MODEL = {{
{coef_entries}
}}


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def inv_logit(x):
    return 1 / (1 + math.exp(-x))


def predict_wp_offense(*, down, distance, yards_to_go, goal_to_go,
                        score_diff, elapsed_seconds, offense_pregame_wp):
    """Win probability for the team on offense, given their own down/
    distance/field position, the score (offense - defense), elapsed game
    seconds (0-3600, regulation only -- do not call this for OT plays),
    and their own pregame win probability."""
    m = MODEL
    time_remaining_frac = max(0.0, 3600 - elapsed_seconds) / 3600.0
    urgency = score_diff * (1 - time_remaining_frac)
    l_pred = (m["const"]
              + m["b_logit_offense_pregame_wp"] * logit(offense_pregame_wp)
              + m["b_score_diff"] * score_diff
              + m["b_time_remaining_frac"] * time_remaining_frac
              + m["b_urgency"] * urgency
              + m["b_down2"] * (1 if down == 2 else 0)
              + m["b_down3"] * (1 if down == 3 else 0)
              + m["b_down4"] * (1 if down == 4 else 0)
              + m["b_distance"] * min(distance, {max_distance})
              + m["b_yards_to_go"] * yards_to_go
              + m["b_goal_to_go"] * (1 if goal_to_go else 0))
    return inv_logit(l_pred)


def coinflip_wp_offense(*, down, distance, yards_to_go, goal_to_go,
                         score_diff, elapsed_seconds):
    """predict_wp_offense() with the offense's own pregame WP forced to a
    50/50 coin flip -- the anchor-free scale to judge an in-game swing
    against, same rationale as wp_baseline.coinflip_wp_elapsed()."""
    return predict_wp_offense(
        down=down, distance=distance, yards_to_go=yards_to_go, goal_to_go=goal_to_go,
        score_diff=score_diff, elapsed_seconds=elapsed_seconds, offense_pregame_wp=0.5,
    )
'''

COEF_TEMPLATE = '    "{name}": {value!r},'


def write_module(model, n_games, n_rows, pseudo_r2):
    param_map = {
        "const": "const",
        "logit_offense_pregame_wp": "b_logit_offense_pregame_wp",
        "score_diff": "b_score_diff",
        "time_remaining_frac": "b_time_remaining_frac",
        "urgency": "b_urgency",
        "down2": "b_down2",
        "down3": "b_down3",
        "down4": "b_down4",
        "distance": "b_distance",
        "yards_to_go": "b_yards_to_go",
        "goal_to_go": "b_goal_to_go",
    }
    entries = "\n".join(
        COEF_TEMPLATE.format(name=out_name, value=float(model.params[in_name]))
        for in_name, out_name in param_map.items()
    )
    content = MODULE_TEMPLATE.format(
        generated_at=datetime.date.today().isoformat(),
        n_games=n_games,
        n_rows=n_rows,
        pseudo_r2=model.prsquared,
        coef_entries=entries,
        max_distance=MAX_DISTANCE,
    )
    with open(OUTPUT_PATH, "w") as f:
        f.write(content)
    print(f"Wrote {OUTPUT_PATH}")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print("Building regulation-only play-level dataset (full corpus, no train/test split)...")
    df, n_games_used, n_games_total = build_dataset(conn)
    print(f"Games with raw JSON available: {n_games_used}/{n_games_total}")
    print(f"Regulation scrimmage-down plays: {len(df)}\n")

    print("Fitting situational logistic model (offense perspective) on the full corpus...")
    model = fit(df)
    print(model.summary())

    write_module(model, n_games_used, len(df), model.prsquared)


if __name__ == "__main__":
    main()
