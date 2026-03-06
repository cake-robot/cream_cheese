import argparse
import sys

from src import db, espn, scoring
from src.config import DEFAULT_SEASON


def find_team(name_query):
    print(f"Searching for teams matching '{name_query}'...")
    teams = espn.fetch_teams_list()
    query = name_query.lower()
    matches = [
        t for t in teams
        if query in t["name"].lower()
        or query in t["abbreviation"].lower()
        or query in t["school"].lower()
    ]
    if not matches:
        print("No teams found.")
    else:
        print(f"{'ID':<8} {'Abbreviation':<14} {'Name'}")
        print("-" * 50)
        for t in matches:
            print(f"{t['id']:<8} {t['abbreviation']:<14} {t['name']}")


def discover_games(conn, args):
    """Phase 1: discover games and upsert metadata into games table."""
    game_ids = []

    if args.team:
        team_id = str(args.team)

        print(f"Fetching schedule for team {team_id}, season {args.season}...")
        games = espn.fetch_team_schedule(team_id, args.season)
        for g in games:
            db.upsert_game(conn, g)
            game_ids.append(g["game_id"])
        print(f"  {len(games)} regular season games.")

        print(f"Fetching postseason scoreboard for season {args.season}...")
        postseason = espn.fetch_scoreboard(args.season, season_type=3)
        team_games = [g for g in postseason if g["home_team_id"] == team_id or g["away_team_id"] == team_id]
        for g in team_games:
            db.upsert_game(conn, g)
            game_ids.append(g["game_id"])
        print(f"  {len(team_games)} postseason games.")

        conn.commit()
        print(f"  Discovered {len(games) + len(team_games)} total games.")

    elif args.week:
        print(f"Fetching scoreboard week {args.week}, season {args.season}...")
        games = espn.fetch_scoreboard(args.season, week=args.week)
        for g in games:
            db.upsert_game(conn, g)
            game_ids.append(g["game_id"])
        conn.commit()
        print(f"  Discovered {len(games)} games.")

    else:
        # Full season: weeks 0-15 regular season + postseason
        total = 0
        for week in range(0, 16):
            print(f"  Fetching scoreboard week {week}, season {args.season}...", end=" ", flush=True)
            games = espn.fetch_scoreboard(args.season, week=week, season_type=2)
            for g in games:
                db.upsert_game(conn, g)
                game_ids.append(g["game_id"])
            conn.commit()
            print(f"{len(games)} games")
            total += len(games)

        print(f"  Fetching postseason (season_type=3), season {args.season}...", end=" ", flush=True)
        games = espn.fetch_scoreboard(args.season, week=None, season_type=3)
        for g in games:
            db.upsert_game(conn, g)
            game_ids.append(g["game_id"])
        conn.commit()
        print(f"{len(games)} games")
        total += len(games)

        print(f"Discovery complete: {total} total games found.")

    return game_ids


def fetch_details(conn, game_ids=None):
    """Phase 2: fetch game summaries for completed, unfetched games."""
    if game_ids:
        placeholders = ",".join("?" * len(game_ids))
        rows = conn.execute(
            f"SELECT game_id, away_team_abbr, home_team_abbr FROM games "
            f"WHERE completed = 1 AND detail_fetched = 0 AND game_id IN ({placeholders})",
            game_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT game_id, away_team_abbr, home_team_abbr FROM games "
            "WHERE completed = 1 AND detail_fetched = 0"
        ).fetchall()

    n = len(rows)
    if n == 0:
        print("0 games need detail fetching.")
        return

    for i, row in enumerate(rows, 1):
        game_id = row["game_id"]
        label = f"{row['away_team_abbr']} @ {row['home_team_abbr']}"
        print(f"Fetching detail for game {i}/{n}: {label}...")

        summary = espn.fetch_game_summary(game_id)
        wp_rows, home_score, away_score, attendance, initial_home_wp = espn.parse_summary_detail(summary)

        if not wp_rows:
            print(f"  Warning: no win probability data for game {game_id}. Marking fetched anyway.")

        with conn:
            if wp_rows:
                db.upsert_win_probability(conn, wp_rows)
            db.mark_detail_fetched(conn, game_id, home_score, away_score, attendance, initial_home_wp)

    print(f"Detail fetch complete: {n} games processed.")


def handle_game_arg(conn, game_id):
    """Ensure a game row exists when --game is specified; return [game_id]."""
    row = conn.execute("SELECT game_id FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if row:
        return [game_id]

    # Bootstrap: fetch summary to get metadata
    print(f"Game {game_id} not in DB, fetching metadata from summary...")
    summary = espn.fetch_game_summary(game_id)
    meta = espn.parse_summary_game_meta(summary)
    if not meta.get("game_id"):
        meta["game_id"] = game_id
    db.upsert_game(conn, meta)
    conn.commit()
    return [game_id]


def main():
    parser = argparse.ArgumentParser(description="CFB data pipeline")
    parser.add_argument("--season", type=int, default=DEFAULT_SEASON)
    parser.add_argument("--week", type=int, help="Specific week (regular season)")
    parser.add_argument("--team", type=str, help="Team ID (uses team schedule endpoint)")
    parser.add_argument("--game", type=str, help="Single game ID")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--detail-only", action="store_true")
    parser.add_argument("--score-only", action="store_true", help="Only run Phase 3 scoring")
    parser.add_argument("--skip-scoring", action="store_true", help="Skip Phase 3 scoring")
    parser.add_argument("--rescore", action="store_true", help="Re-score already-scored games")
    parser.add_argument("--find-team", type=str, metavar="NAME")
    parser.add_argument("--seed-teams", action="store_true", help="Populate teams table from ESPN teams list")
    args = parser.parse_args()

    if args.find_team:
        find_team(args.find_team)
        sys.exit(0)

    conn = db.init_db()

    if args.score_only:
        scoring.score_games(conn, rescore=args.rescore)
        sys.exit(0)

    if args.seed_teams:
        print("Seeding teams table from ESPN teams list...")
        teams = espn.fetch_teams_list()
        for t in teams:
            db.upsert_team(conn, t["id"], t["abbreviation"], t["name"], t["school"])
        conn.commit()
        print(f"  {len(teams)} teams upserted.")
        if not any([args.team, args.week, args.game, args.discover_only, args.detail_only]):
            sys.exit(0)

    game_ids = None  # None means "all eligible"

    if args.game:
        game_ids = handle_game_arg(conn, args.game)
        if not args.detail_only:
            # --game implies skip discovery; game row is bootstrapped above
            pass
        fetch_details(conn, game_ids)
        if not args.skip_scoring:
            scoring.score_games(conn, game_ids=game_ids, rescore=args.rescore)
        return

    if not args.detail_only:
        game_ids = discover_games(conn, args)

    if not args.discover_only:
        fetch_details(conn, game_ids if game_ids else None)
        if not args.skip_scoring:
            scoring.score_games(conn, game_ids=game_ids if game_ids else None, rescore=args.rescore)


if __name__ == "__main__":
    main()
