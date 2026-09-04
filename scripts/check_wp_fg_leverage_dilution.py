"""
Is the down4-in-FG-range effect real but DILUTED by garbage time, rather
than genuinely absent?

Hypothesis: coaches mostly choose the better of {kick, go for it}, so the
REALIZED outcome at "4th & long, in FG range" should already reflect
something close to the better option's win rate -- no explicit play-type
feature needed, a plain regression on outcomes should be able to see it.
But a single global down4 x yards_to_go term is fit across the WHOLE
season, and the overwhelming majority of "4th & long, in range" snaps
happen in low-leverage moments (blowouts, garbage time) where this one
play has ~zero bearing on who wins the game -- diluting any real signal
that only shows up when the play is actually decisive.

Test: restrict to HIGH-LEVERAGE snaps only (close score, late in the game)
and compare the CURRENT model's own prediction for down4-in-range plays
against the empirical win rate in that same restricted slice. If the
interaction is real but diluted, this slice should show a visible gap;
if the interaction is truly not there, it won't.
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
            if not play.get("play_id") or play["down"] != 4:
                continue
            off_is_home = play["off_is_home"]
            off_score = play["home_score"] if off_is_home else play["away_score"]
            def_score = play["away_score"] if off_is_home else play["home_score"]
            score_diff = off_score - def_score
            secs_left = max(0, 3600 - play["elapsed_seconds"])
            offense_won = home_won if off_is_home else not home_won

            model_pred = scoring.coinflip_home_wp(play)
            model_pred_offense = model_pred if off_is_home else 1 - model_pred
            # de-coinflip: use the ACTUAL model call with real pregame wp instead --
            # coinflip_home_wp forces 0.5, which is fine here since we only care
            # about the down/distance/yards_to_go shape, not the pregame anchor.

            rows.append({
                "score_diff": score_diff, "secs_left": secs_left, "yards_to_go": play["yards_to_go"],
                "distance": play["distance"], "offense_won": offense_won, "model_pred": model_pred_offense,
            })

    print(f"Total 4th-down plays: {len(rows)}\n")

    def report(label, subset):
        n = len(subset)
        if n == 0:
            print(f"{label}: n=0")
            return
        emp = sum(r["offense_won"] for r in subset) / n
        pred = sum(r["model_pred"] for r in subset) / n
        print(f"{label:<55s} n={n:>5d}  empirical={emp*100:5.1f}%  model_pred={pred*100:5.1f}%  gap={100*(pred-emp):+5.1f}pts")

    in_range = [r for r in rows if r["yards_to_go"] <= 40]
    out_range = [r for r in rows if r["yards_to_go"] > 40]

    print("=== ALL leverage levels (this is what the full-corpus regression sees) ===")
    report("4th down, IN FG range (ytg<=40)", in_range)
    report("4th down, OUT of FG range (ytg>40)", out_range)

    print("\n=== HIGH LEVERAGE ONLY: <=5 min left AND within 1 score (|diff|<=8) ===")
    high_lev = [r for r in rows if r["secs_left"] <= 300 and abs(r["score_diff"]) <= 8]
    in_range_hl = [r for r in high_lev if r["yards_to_go"] <= 40]
    out_range_hl = [r for r in high_lev if r["yards_to_go"] > 40]
    report("4th down, IN FG range", in_range_hl)
    report("4th down, OUT of FG range", out_range_hl)

    print("\n=== ULTRA HIGH LEVERAGE: <=2 min left AND within 1 score ===")
    ultra = [r for r in rows if r["secs_left"] <= 120 and abs(r["score_diff"]) <= 8]
    in_range_u = [r for r in ultra if r["yards_to_go"] <= 40]
    out_range_u = [r for r in ultra if r["yards_to_go"] > 40]
    report("4th down, IN FG range", in_range_u)
    report("4th down, OUT of FG range", out_range_u)

    print("\n=== Finer cut on high-leverage, IN-range group: by actual FG distance bucket ===")
    for lo, hi, label in [(0, 20, "short (<=20, chip shot/close)"), (21, 33, "mid (21-33, ~38-50yd FG)"), (34, 40, "long (34-40, ~51-57yd FG)")]:
        sub = [r for r in in_range_hl if lo <= r["yards_to_go"] <= hi]
        report(f"  ytg {lo}-{hi} ({label})", sub)


if __name__ == "__main__":
    main()
