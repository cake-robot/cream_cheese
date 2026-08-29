"""
Dry-run preview of src/fox_wp.py's synthetic win-probability reconstruction
for a single game -- prints the synthetic series and resulting composite
without writing anything to the DB. The real (writing) path is
`pipeline.py --fox-synthesize-wp` followed by `--score-only`; use this
script to eyeball a specific game first, e.g. after a new Fox match.

Usage:
    venv/bin/python3 scripts/poc_fox_wp.py 401628511
"""
import sys

sys.path.insert(0, ".")

from src import db, fox_wp, scoring


def main():
    if len(sys.argv) != 2:
        print("Usage: venv/bin/python3 scripts/poc_fox_wp.py <game_id>")
        sys.exit(1)
    game_id = sys.argv[1]

    conn = db.get_connection()
    game = conn.execute(
        "SELECT home_team_abbr, away_team_abbr, home_rank, away_rank, "
        "home_team_id, away_team_id, home_score, away_score "
        "FROM games WHERE game_id = ?", (game_id,),
    ).fetchone()
    if not game:
        print(f"game {game_id} not found")
        sys.exit(1)

    wp_rows = fox_wp.build_synthetic_wp_rows(conn, game_id)
    if wp_rows is None:
        print(f"game {game_id} has no fox_games match -- can't synthesize.")
        sys.exit(1)

    print(f"{game['away_team_abbr']} @ {game['home_team_abbr']}  "
          f"(final {game['away_score']}-{game['home_score']})")
    print(f"{len(wp_rows)} synthetic WP points (regulation only, "
          f"+OT marker if the game reached it):\n")
    for r in wp_rows:
        t = f"{r['clock_seconds_elapsed']:>5}" if r["clock_seconds_elapsed"] is not None else " [OT]"
        print(f"  q{r['period_number']} t={t}  "
              f"{r['away_score']:>3}-{r['home_score']:<3}  home_wp={r['home_win_pct']:.3f}")

    context = {
        "wp_rows": wp_rows,
        "home_rank": game["home_rank"],
        "away_rank": game["away_rank"],
        "initial_home_wp": None,
        "home_team_id": game["home_team_id"],
        "away_team_id": game["away_team_id"],
        "home_score": game["home_score"],
        "away_score": game["away_score"],
    }
    composite, breakdown = scoring.score_game(context)
    print(f"\ncomposite watchability_score = {composite:.4f}\n")
    for name, v in breakdown.items():
        if v["raw"] is None:
            print(f"  {name}: n/a")
        else:
            print(f"  {name}: raw={v['raw']:.3f}  norm={v['normalized']:.3f}  weighted={v['weighted']:.3f}")


if __name__ == "__main__":
    main()
