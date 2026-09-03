"""
Empirical check (no fitting, read-only): among plays where the offense
(a) is leading, (b) has the ball, and (c) just got a fresh set of downs
(1st & 10), with various amounts of time left in regulation -- how often
does that team actually go on to win the real game? Compared against what
"current" (production Model C), "round 1" (cubic + inv-sqrt urgency, see
scripts/compare_wp_endgame_calibration.py), and "round 2" (scores_needed,
same script) predict for the exact same situations.

Prompted by a reported case (401442015, Georgia up 1 with the ball and a
fresh set of downs, ~30s left) that read only ~70% even after round 1 --
this checks whether that's a real remaining gap or just one noisy example.

FINDING: the aggregate "leading 1-8, fresh downs, <=30s" bucket wins ~97-98%
of the time (n=922) and round 1 was already close there (~97.8% predicted)
-- but that aggregate is dominated by big (multi-possession-safe) leads. Cut
by EXACT lead size, a real gap shows up specifically at 1-3 points (n=43,
30, 77): empirical win rates of 93.0%, 100.0%, 93.5% vs. round 1's much
lower 80.1%, 89.2%, 93.2%. This directly matches football's discrete
scoring (a field goal is always 3 -- a 1-point and 2-point deficit are
erased by the identical single kick) and motivated round 2's scores_needed
feature (see compare_wp_endgame_calibration.py), which closes almost the
entire gap: 92.5% predicted vs. 92.0% actual on the pooled 1-3pt/<=30s
slice (n=25 held-out).

CAVEAT: no timeouts-remaining data exists in the archived ESPN payload
(investigated and abandoned previously -- see memory: real team names are
also attached to non-chargeable injury/media timeouts, with no signal in
the payload to tell them apart). This bucket is therefore a blend of two
different real situations that look IDENTICAL to any of these models:
  (a) genuine victory-formation snaps, trailing team out of timeouts, ~100%
  (b) a still-technically-live drive because the trailing team still has a
      timeout or two, so the leading team must still convert/punt cleanly
No model without timeout data can fully separate those -- the empirical
rate here is the honest ceiling any of these candidates can match in this
specific bucket, not necessarily 99-100%.

Usage:
    venv/bin/python3 scripts/validate_endgame_lead_win_rate.py [path/to/cfb.db]
"""
import math
import sys

sys.path.insert(0, ".")

from src import db, espn

# Production Model C (src/wp_situational.py) as of this session.
CURRENT = {
    "const": 1.1099742687889258, "logit_offense_pregame_wp": 0.7972313735417178,
    "score_diff": 0.06049179479305411, "time_remaining_frac": -0.32467680373205055,
    "urgency": 0.21824273966762975, "down2": -0.06940103866903506,
    "down3": -0.1825414317089864, "down4": -0.3550788726186753,
    "distance": -0.011922454168318525, "yards_to_go": -0.009476447910007096,
}

# Round 1 candidate: current + cubic urgency + inv-sqrt urgency (full-corpus
# refit -- see compare_wp_endgame_calibration.py's design_cubic_plus_inv_sqrt).
ROUND1 = {
    "const": 1.3145891060867303, "logit_offense_pregame_wp": 0.8006395700077514,
    "score_diff": 0.05209148876905877, "time_remaining_frac": -1.1760129936838497,
    "urgency": 0.4316319713025558, "down2": -0.08925430805815017,
    "down3": -0.20462937592150926, "down4": -0.3833288605544227,
    "distance": -0.011252688877176481, "yards_to_go": -0.009485551688489344,
    "urgency2": -0.9741581019896343, "time_remaining_frac2": 0.7564274352977852,
    "urgency3": 0.823635349413121, "inv_sqrt_urgency": 0.9316871770187598,
}

# Round 2 candidate: scores_needed replaces every raw-score_diff urgency term
# (full-corpus refit, parsimony pass -- see design_scores_needed_final).
ROUND2 = {
    "const": 1.316898491950799, "logit_offense_pregame_wp": 0.7990132554278359,
    "score_diff": 0.13807377176149227, "time_remaining_frac": -1.263217042478765,
    "down2": -0.08875115012568387, "down3": -0.20496958297361362, "down4": -0.38411864074961194,
    "distance": -0.011346235375379302, "yards_to_go": -0.009351649686584326,
    "sn_urgency": -0.36752746673283976, "sn_urgency3": 1.3966167964176304,
    "sn_time_remaining_frac2": 0.8776342329690779, "sn_inv_sqrt_urgency": 2.318340401941777,
}


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def inv_logit(x):
    return 1 / (1 + math.exp(-x))


def scores_needed(score_diff):
    if score_diff == 0:
        return 0
    return math.copysign(math.ceil(abs(score_diff) / 8.0), score_diff)


def predict_current(down, distance, yards_to_go, score_diff, elapsed_seconds):
    m = CURRENT
    time_frac = max(0.0, 3600 - elapsed_seconds) / 3600.0
    urgency = score_diff * (1 - time_frac)
    l = (m["const"] + m["logit_offense_pregame_wp"] * logit(0.5)
         + m["score_diff"] * score_diff + m["time_remaining_frac"] * time_frac + m["urgency"] * urgency
         + m["down2"] * (1 if down == 2 else 0) + m["down3"] * (1 if down == 3 else 0)
         + m["down4"] * (1 if down == 4 else 0)
         + m["distance"] * min(distance, 30) + m["yards_to_go"] * yards_to_go)
    return inv_logit(l)


def predict_round1(down, distance, yards_to_go, score_diff, elapsed_seconds):
    m = ROUND1
    time_frac = max(0.0, 3600 - elapsed_seconds) / 3600.0
    urgency = score_diff * (1 - time_frac)
    l = (m["const"] + m["logit_offense_pregame_wp"] * logit(0.5)
         + m["score_diff"] * score_diff + m["time_remaining_frac"] * time_frac + m["urgency"] * urgency
         + m["down2"] * (1 if down == 2 else 0) + m["down3"] * (1 if down == 3 else 0)
         + m["down4"] * (1 if down == 4 else 0)
         + m["distance"] * min(distance, 30) + m["yards_to_go"] * yards_to_go
         + m["urgency2"] * score_diff * (1 - time_frac) ** 2 + m["time_remaining_frac2"] * time_frac ** 2
         + m["urgency3"] * score_diff * (1 - time_frac) ** 3
         + m["inv_sqrt_urgency"] * score_diff / math.sqrt(max(0.0, 3600 - elapsed_seconds) + 5.0))
    return inv_logit(l)


def predict_round2(down, distance, yards_to_go, score_diff, elapsed_seconds):
    m = ROUND2
    time_frac = max(0.0, 3600 - elapsed_seconds) / 3600.0
    sn = scores_needed(score_diff)
    l = (m["const"] + m["logit_offense_pregame_wp"] * logit(0.5)
         + m["score_diff"] * score_diff + m["time_remaining_frac"] * time_frac
         + m["down2"] * (1 if down == 2 else 0) + m["down3"] * (1 if down == 3 else 0)
         + m["down4"] * (1 if down == 4 else 0)
         + m["distance"] * min(distance, 30) + m["yards_to_go"] * yards_to_go
         + m["sn_urgency"] * sn * (1 - time_frac) + m["sn_urgency3"] * sn * (1 - time_frac) ** 3
         + m["sn_time_remaining_frac2"] * time_frac ** 2
         + m["sn_inv_sqrt_urgency"] * sn / math.sqrt(max(0.0, 3600 - elapsed_seconds) + 5.0))
    return inv_logit(l)


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)
    games = conn.execute("""
        SELECT game_id, home_team_id, home_score, away_score
        FROM games
        WHERE completed = 1 AND detail_fetched = 1
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND home_score != away_score
    """).fetchall()

    buckets = [(0, 10), (11, 20), (21, 30), (31, 45), (46, 60), (0, 30), (0, 60)]
    rows_by_bucket = {b: [] for b in buckets}

    n_games_checked = 0
    for g in games:
        raw = db.get_game_raw_json(conn, g["game_id"])
        if not raw:
            continue
        n_games_checked += 1
        home_won = g["home_score"] > g["away_score"]

        for play in espn.extract_situational_plays(raw, g["home_team_id"]):
            if not play.get("play_id"):
                continue
            if play["down"] != 1 or play["distance"] != 10:
                continue
            off_is_home = play["off_is_home"]
            off_score = play["home_score"] if off_is_home else play["away_score"]
            def_score = play["away_score"] if off_is_home else play["home_score"]
            score_diff = off_score - def_score
            if score_diff <= 0:
                continue  # offense must be LEADING
            secs_left = max(0, 3600 - play["elapsed_seconds"])
            offense_won = home_won if off_is_home else not home_won

            for (lo, hi) in buckets:
                if lo <= secs_left <= hi:
                    rows_by_bucket[(lo, hi)].append({
                        "offense_won": offense_won, "score_diff": score_diff,
                        "elapsed_seconds": play["elapsed_seconds"], "yards_to_go": play["yards_to_go"],
                    })

    def mean_preds(rows, fn):
        return sum(fn(1, 10, r["yards_to_go"], r["score_diff"], r["elapsed_seconds"]) for r in rows) / len(rows)

    print(f"Games checked: {n_games_checked}\n")
    print(f"{'seconds left':<14s} {'n':>6s} {'empirical':>10s} {'current':>9s} {'round 1':>9s} {'round 2':>9s} {'median lead':>12s}")
    for (lo, hi) in buckets:
        rows = rows_by_bucket[(lo, hi)]
        n = len(rows)
        if n == 0:
            print(f"{lo}-{hi}s  n=0")
            continue
        empirical = sum(r["offense_won"] for r in rows) / n
        leads = sorted(r["score_diff"] for r in rows)
        print(f"{lo}-{hi}s{'':<8s} {n:>6d} {empirical*100:>9.1f}% {mean_preds(rows, predict_current)*100:>8.1f}% "
              f"{mean_preds(rows, predict_round1)*100:>8.1f}% {mean_preds(rows, predict_round2)*100:>8.1f}%  "
              f"{leads[n // 2]:>10.0f} pts")

    print("\nSplit by exact lead size, seconds left 0-30 (this is where the real gap lives):")
    rows = rows_by_bucket[(0, 30)]
    print(f"  {'lead':>5s} {'n':>5s} {'empirical':>10s} {'current':>9s} {'round 1':>9s} {'round 2':>9s}")
    for sd in range(1, 9):
        sub = [r for r in rows if r["score_diff"] == sd]
        if not sub:
            continue
        n = len(sub)
        emp = sum(r["offense_won"] for r in sub) / n
        print(f"  {sd:>5d} {n:>5d} {emp*100:>9.1f}% {mean_preds(sub, predict_current)*100:>8.1f}% "
              f"{mean_preds(sub, predict_round1)*100:>8.1f}% {mean_preds(sub, predict_round2)*100:>8.1f}%")


if __name__ == "__main__":
    main()
