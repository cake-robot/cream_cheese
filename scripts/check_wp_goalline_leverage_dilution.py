"""
Is the goal-line/short-yardage-with-no-time miscalibration (401521330,
OSU 1st & goal from the 1, trailing by 4, 7s left -- production read them
at 27.9%, ESPN reads 65.1%) a real systemic pattern, or one anecdote?

Hypothesis: the urgency terms (sn_urgency/sn_urgency3/sn_inv_sqrt_urgency,
all functions of score_diff and time_remaining ONLY) have zero interaction
with yards_to_go. They were calibrated on the "long field, no time" story
(a trailing team far from the end zone really is nearly done once the
clock runs out), but they generalize WRONGLY to "already at the doorstep,
no time" -- where one snap is all that's needed, so the clock barely
matters. Same root class of bug as the field-goal blind spot: urgency
and field-position are fit as separate additive terms, but the TRUE
relationship is that how much urgency matters depends on how far there is
to go.
"""
import sys

sys.path.insert(0, ".")

from src import db, espn, scoring


def main():
    conn = db.get_connection()
    games = conn.execute("""
        SELECT game_id, home_team_id, home_score, away_score
        FROM games
        WHERE completed = 1 AND detail_fetched = 1
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND home_score != away_score
    """).fetchall()

    rows = []
    for g in games:
        raw = db.get_game_raw_json(conn, g["game_id"])
        if not raw:
            continue
        home_won = g["home_score"] > g["away_score"]

        for play in espn.extract_situational_plays(raw, g["home_team_id"]):
            if not play.get("play_id"):
                continue
            off_is_home = play["off_is_home"]
            off_score = play["home_score"] if off_is_home else play["away_score"]
            def_score = play["away_score"] if off_is_home else play["home_score"]
            score_diff = off_score - def_score
            secs_left = max(0, 3600 - play["elapsed_seconds"])
            offense_won = home_won if off_is_home else not home_won

            model_pred = scoring.coinflip_home_wp(play)
            model_pred_offense = model_pred if off_is_home else 1 - model_pred

            rows.append({
                "score_diff": score_diff, "secs_left": secs_left, "yards_to_go": play["yards_to_go"],
                "down": play["down"], "offense_won": offense_won, "model_pred": model_pred_offense,
            })

    def report(label, subset):
        n = len(subset)
        if n == 0:
            print(f"{label}: n=0")
            return
        emp = sum(r["offense_won"] for r in subset) / n
        pred = sum(r["model_pred"] for r in subset) / n
        print(f"{label:<60s} n={n:>5d}  empirical={emp*100:5.1f}%  model_pred={pred*100:5.1f}%  gap={100*(pred-emp):+5.1f}pts")

    # The specific shape: TRAILING offense (needs to score to win/tie),
    # very little time left, but very CLOSE to the end zone already.
    print("=== Trailing offense, <=30s left, by field position (close vs far) ===")
    trailing_low_time = [r for r in rows if -8 <= r["score_diff"] < 0 and r["secs_left"] <= 30]
    close = [r for r in trailing_low_time if r["yards_to_go"] <= 10]
    mid = [r for r in trailing_low_time if 10 < r["yards_to_go"] <= 30]
    far = [r for r in trailing_low_time if r["yards_to_go"] > 30]
    report("yards_to_go <= 10 (goal-line-ish)", close)
    report("yards_to_go 11-30", mid)
    report("yards_to_go > 30 (needs a real drive/Hail Mary)", far)

    print("\n=== Same cut, restricted to <=15s left (more extreme) ===")
    trailing_ultra = [r for r in rows if -8 <= r["score_diff"] < 0 and r["secs_left"] <= 15]
    close_u = [r for r in trailing_ultra if r["yards_to_go"] <= 10]
    far_u = [r for r in trailing_ultra if r["yards_to_go"] > 30]
    report("yards_to_go <= 10", close_u)
    report("yards_to_go > 30", far_u)

    print("\n=== Finer cut, yards_to_go <= 10 bucket, <=30s left: by exact distance ===")
    for lo, hi in [(0, 3), (4, 6), (7, 10)]:
        sub = [r for r in close if lo <= r["yards_to_go"] <= hi]
        report(f"  yards_to_go {lo}-{hi}", sub)


if __name__ == "__main__":
    main()
