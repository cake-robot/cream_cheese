import argparse
import sys

from src import db, espn, fox, fox_match, fox_reconcile, scoring
from src.config import DEFAULT_SEASON, FOX_SEASON_ANCHORS, FOX_SCAN_OVERRUN


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


def _fox_event_dict(fox_event_id, status, header=None, in_window=False):
    header = header or {}
    away_team = header.get("away_team") or {}
    home_team = header.get("home_team") or {}
    return {
        "fox_event_id": fox_event_id,
        "status": status,
        "event_date": header.get("event_date"),
        "away_abbr": header.get("away_abbr"),
        "home_abbr": header.get("home_abbr"),
        "away_name": header.get("away_name"),
        "home_name": header.get("home_name"),
        "away_fox_team_id": away_team.get("fox_team_id"),
        "home_fox_team_id": home_team.get("fox_team_id"),
        "away_score": header.get("away_score"),
        "home_score": header.get("home_score"),
        "status_line": header.get("status_line"),
        "in_window": int(in_window),
    }


def _fox_harvest_teams(conn, fox_event_id, header):
    """Record both teams' identity from a parsed header into fox_teams, a
    byproduct of every fetch regardless of in_window/match status -- grows
    automatically as more events get pulled, same pattern upsert_team() uses
    on the ESPN side."""
    for team in (header.get("away_team"), header.get("home_team")):
        if team and team.get("fox_team_id") is not None:
            db.upsert_fox_team(conn, {**team, "first_seen_event_id": fox_event_id})


def _fox_store_pbp(conn, fox_event_id, payload):
    plays = fox.parse_pbp_plays(payload)
    db.upsert_fox_plays(conn, fox_event_id, plays)
    seq = fox.build_score_sequence(plays)
    db.replace_fox_score_sequence(conn, fox_event_id, seq)
    db.mark_fox_pbp_fetched(conn, fox_event_id)
    return plays, seq


def _fox_get(conn, fox_event_id, window_start, window_end, counters):
    """
    Fetch-or-cache a single Fox event ID against fox_events. Every ID ever
    touched (hit, miss, or error) is recorded, so a re-run of a walk that
    covers the same IDs issues zero HTTP requests -- EXCEPT when a
    previously out-of-window cached event's date newly falls inside a
    *different* window being walked now. Adjacent weeks' ID ranges overlap
    at the boundary (each walk overruns past its own window's edge into the
    next week's territory), so a cached row's in_window flag is only ever
    true relative to whichever window first probed it; it's recomputed
    against the current window on every lookup, and plays are backfilled
    if it newly qualifies but was never fetched.
    Returns (status, event_date, in_window). counters['fetches'] is
    incremented only on a live HTTP call, so --fox-max-fetches caps real
    request volume, not cache hits.
    """
    row = conn.execute(
        "SELECT status, event_date, in_window, pbp_fetched FROM fox_events WHERE fox_event_id = ?",
        (fox_event_id,),
    ).fetchone()
    if row:
        if row["status"] != "ok" or not row["event_date"]:
            return row["status"], row["event_date"], bool(row["in_window"])

        in_window = bool(window_start <= row["event_date"] <= window_end)

        if in_window and not row["pbp_fetched"]:
            if counters["fetches"] >= counters["max_fetches"]:
                raise SystemExit(
                    f"Hit --fox-max-fetches={counters['max_fetches']}; stopping. "
                    f"Already-probed IDs are cached, so re-running resumes from here."
                )
            counters["fetches"] += 1
            payload = fox.fetch_event(fox_event_id)
            if payload is not None:
                plays, seq = _fox_store_pbp(conn, fox_event_id, payload)
                print(f"  event {fox_event_id}: {row['event_date']} (backfilled from prior probe) "
                      f"[{len(plays)} plays, {len(seq)} sequence steps]")

        if in_window != bool(row["in_window"]):
            conn.execute(
                "UPDATE fox_events SET in_window = ? WHERE fox_event_id = ?",
                (int(in_window), fox_event_id),
            )
            conn.commit()

        return row["status"], row["event_date"], in_window

    if counters["fetches"] >= counters["max_fetches"]:
        raise SystemExit(
            f"Hit --fox-max-fetches={counters['max_fetches']}; stopping. "
            f"Already-probed IDs are cached, so re-running resumes from here."
        )
    counters["fetches"] += 1

    try:
        payload = fox.fetch_event(fox_event_id)
    except RuntimeError as e:
        print(f"  event {fox_event_id}: ERROR ({e})")
        db.upsert_fox_event(conn, _fox_event_dict(fox_event_id, "error"))
        conn.commit()
        return "error", None, False

    if payload is None:
        db.upsert_fox_event(conn, _fox_event_dict(fox_event_id, "missing"))
        conn.commit()
        return "missing", None, False

    header = fox.parse_header(payload)
    date = header["event_date"]
    in_window = bool(date and window_start <= date <= window_end)
    db.upsert_fox_event(conn, _fox_event_dict(fox_event_id, "ok", header, in_window))
    _fox_harvest_teams(conn, fox_event_id, header)

    if in_window:
        plays, seq = _fox_store_pbp(conn, fox_event_id, payload)
        label = f"{header['away_abbr']} @ {header['home_abbr']}"
        print(
            f"  event {fox_event_id}: {date}  {label}  "
            f"{header['away_score']}-{header['home_score']}  "
            f"[{len(plays)} plays, {len(seq)} sequence steps]"
        )

    conn.commit()
    return "ok", date, in_window


def _fox_walk_direction(conn, start_eid, step, window_start, window_end, counters, overrun):
    """
    Walk Fox event IDs one at a time in `step` direction (+1 or -1) from
    start_eid, until `overrun` consecutive IDs come back out-of-window,
    missing, or errored. IDs are not strictly date-monotonic across a full
    season (bowl games sort out of order against the late regular season),
    so this overruns past the first miss rather than stopping immediately.
    """
    eid = start_eid
    misses = 0
    while misses < overrun:
        status, date, in_window = _fox_get(conn, eid, window_start, window_end, counters)
        if status == "ok" and in_window:
            misses = 0
        else:
            misses += 1
        eid += step


def _fox_window(conn, args):
    if args.fox_start and args.fox_end:
        return args.fox_start, args.fox_end
    if not args.week:
        raise SystemExit(
            "--fox-pull needs a date window: pass --week (with --season) to look it "
            "up from the games table, or pass --fox-start/--fox-end explicitly."
        )
    row = conn.execute(
        "SELECT MIN(game_date), MAX(game_date) FROM games "
        "WHERE season_year = ? AND week = ? AND season_type = 2",
        (args.season, args.week),
    ).fetchone()
    if not row or not row[0]:
        raise SystemExit(
            f"No regular-season games found for season={args.season} week={args.week} "
            f"to derive a date window from. Run discovery first, or pass "
            f"--fox-start/--fox-end. (Note: postseason games are also tagged week=1 "
            f"in season_type=3 -- this lookup only considers season_type=2.)"
        )
    return row[0][:10], row[1][:10]


def _fox_pick_anchor(conn, args, window_start, window_end):
    """
    Prefer the closest already-probed event to the target window over the
    static per-season seed: FOX_SEASON_ANCHORS is only meant to bootstrap
    the very first pull ever run for a season. Every pull after that should
    start from the edge of what's already known, since consecutive weeks'
    ID ranges sit close together -- restarting from a week-1 anchor for,
    say, week 5 would need an overrun large enough to bridge the entire gap
    in one run, which isn't a reasonable thing to size for.
    """
    if args.fox_anchor:
        return args.fox_anchor

    row = conn.execute("""
        SELECT fox_event_id FROM fox_events
        WHERE event_date IS NOT NULL AND event_date <= ?
        ORDER BY event_date DESC, fox_event_id DESC LIMIT 1
    """, (window_end,)).fetchone()
    if row:
        return row["fox_event_id"]

    row = conn.execute("""
        SELECT fox_event_id FROM fox_events
        WHERE event_date IS NOT NULL AND event_date >= ?
        ORDER BY event_date ASC, fox_event_id ASC LIMIT 1
    """, (window_start,)).fetchone()
    if row:
        return row["fox_event_id"]

    return FOX_SEASON_ANCHORS.get(args.season)


def fox_pull(conn, args):
    """
    Walk a contiguous block of Fox event IDs bracketing a date window,
    storing a scoring sequence for every event whose date falls inside it.
    Fox has no scoreboard endpoint, so this ID walk is the only way to
    enumerate a slate. There's no separate "discover" pass: since Fox
    doesn't support HTTP Range (confirmed -- responses are always the full
    ~200KB body, status 200, no accept-ranges/content-range), a header-only
    probe costs the same as a full fetch, so identification and detail
    fetch happen in the same request.
    """
    window_start, window_end = _fox_window(conn, args)
    print(f"Fox pull: window {window_start} .. {window_end}")

    anchor = _fox_pick_anchor(conn, args, window_start, window_end)
    if anchor is None:
        raise SystemExit(
            f"No Fox season anchor for season {args.season}; add one to "
            f"FOX_SEASON_ANCHORS in src/config.py, or pass --fox-anchor."
        )

    counters = {"fetches": 0, "max_fetches": args.fox_max_fetches or float("inf")}

    print(f"Probing anchor event {anchor}...")
    _fox_get(conn, anchor, window_start, window_end, counters)

    print(f"Walking backward from {anchor - 1}...")
    _fox_walk_direction(conn, anchor - 1, -1, window_start, window_end, counters, FOX_SCAN_OVERRUN)
    print(f"Walking forward from {anchor + 1}...")
    _fox_walk_direction(conn, anchor + 1, 1, window_start, window_end, counters, FOX_SCAN_OVERRUN)

    in_window = conn.execute(
        "SELECT COUNT(*) FROM fox_events WHERE in_window = 1 "
        "AND event_date BETWEEN ? AND ?",
        (window_start, window_end),
    ).fetchone()[0]
    print(f"Fox pull complete: {counters['fetches']} live fetches this run, "
          f"{in_window} in-window events stored total.")


def fox_pull_event(conn, fox_event_id, force=False):
    """Pull a single Fox event by ID, bypassing the date-window walk -- the debugging path."""
    if not force:
        row = conn.execute(
            "SELECT event_date, pbp_fetched FROM fox_events WHERE fox_event_id = ? AND pbp_fetched = 1",
            (fox_event_id,),
        ).fetchone()
        if row:
            print(f"Event {fox_event_id} already fetched (date={row['event_date']}); "
                  f"pass force=True to refetch.")
            return

    payload = fox.fetch_event(fox_event_id)
    if payload is None:
        print(f"Event {fox_event_id}: not found (404).")
        db.upsert_fox_event(conn, _fox_event_dict(fox_event_id, "missing"))
        conn.commit()
        return

    header = fox.parse_header(payload)
    db.upsert_fox_event(conn, _fox_event_dict(fox_event_id, "ok", header, in_window=True))
    _fox_harvest_teams(conn, fox_event_id, header)
    plays, seq = _fox_store_pbp(conn, fox_event_id, payload)
    conn.commit()
    print(
        f"Event {fox_event_id}: {header['away_abbr']} {header['away_score']} @ "
        f"{header['home_abbr']} {header['home_score']}  "
        f"({len(plays)} plays, {len(seq)} sequence steps)"
    )


def fox_rebuild_sequences(conn, fox_event_id=None):
    """Re-derive score sequences from already-stored fox_plays rows -- no network access."""
    if fox_event_id:
        ids = [fox_event_id]
    else:
        ids = [r[0] for r in conn.execute(
            "SELECT fox_event_id FROM fox_events WHERE pbp_fetched = 1"
        )]

    for eid in ids:
        rows = conn.execute(
            "SELECT * FROM fox_plays WHERE fox_event_id = ? ORDER BY play_sequence", (eid,)
        ).fetchall()
        seq = fox.build_score_sequence([dict(r) for r in rows])
        db.replace_fox_score_sequence(conn, eid, seq)

    conn.commit()
    print(f"Rebuilt score sequences for {len(ids)} event(s).")


def fox_sync_teams(conn, season=None, week=None):
    seeded, matched = fox_match.sync_team_crosswalk(conn, season=season, week=week)
    print(f"Crosswalk sync: {seeded} team(s) in scope, {matched} newly matched this run.")


def fox_teams_worklist(conn):
    worklist = fox_match.unmatched_teams(conn)
    if not worklist:
        print("No unmatched teams.")
        return
    print(f"{len(worklist)} unmatched team(s):")
    for w in worklist:
        print(f"  {w['espn_team_id']:>8}  {w['espn_school']:35s}  {w['suggested_query']}")


def fox_match_team(conn, espn_team_id, fox_team_id):
    try:
        fox_match.record_manual_team_match(conn, espn_team_id, fox_team_id)
    except ValueError as e:
        raise SystemExit(str(e))
    print(f"Recorded: ESPN team {espn_team_id} <-> Fox team {fox_team_id}")


def fox_match_games(conn, season=None, week=None):
    attempted, matched = fox_match.match_all_games(conn, season=season, week=week)
    print(f"Game matching: {attempted} game(s) in scope, {matched} matched to a Fox event.")


def fox_reconcile_run(conn, season=None, week=None):
    results = fox_reconcile.reconcile_all(conn, season=season, week=week)
    counts = {}
    for r in results:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    summary = ", ".join(f"{t}={n}" for t, n in sorted(counts.items()))
    print(f"Reconciled {len(results)} game(s): {summary}")


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
    parser.add_argument("--compute-sequences", action="store_true", help="Compute play_sequence for all WP rows")
    parser.add_argument("--find-team", type=str, metavar="NAME")
    parser.add_argument("--seed-teams", action="store_true", help="Populate teams table from ESPN teams list")
    parser.add_argument("--fox-pull", action="store_true",
                         help="Pull Fox Sports scoring sequences for --season/--week (or --fox-start/--fox-end)")
    parser.add_argument("--fox-event", type=str, metavar="ID", help="Pull a single Fox event by ID")
    parser.add_argument("--fox-start", type=str, metavar="YYYY-MM-DD")
    parser.add_argument("--fox-end", type=str, metavar="YYYY-MM-DD")
    parser.add_argument("--fox-anchor", type=int, metavar="ID",
                         help="Override the Fox event ID used to seed --fox-pull's walk")
    parser.add_argument("--fox-max-fetches", type=int, metavar="N",
                         help="Safety valve: stop --fox-pull after N live HTTP fetches")
    parser.add_argument("--fox-force", action="store_true", help="Refetch --fox-event even if already stored")
    parser.add_argument("--fox-rebuild-sequences", action="store_true",
                         help="Re-derive score sequences from stored fox_plays; no network access")
    parser.add_argument("--fox-sync-teams", action="store_true",
                         help="Sync team_crosswalk against fox_teams for --season/--week (or all teams)")
    parser.add_argument("--fox-teams-worklist", action="store_true",
                         help="Print ESPN teams still unmatched in team_crosswalk")
    parser.add_argument("--fox-match-team", type=str, metavar="ESPN_ID:FOX_TEAM_ID",
                         help="Record a manually-resolved ESPN<->Fox team match")
    parser.add_argument("--fox-match-games", action="store_true",
                         help="Match games to Fox events via team_crosswalk for --season/--week")
    parser.add_argument("--fox-reconcile", action="store_true",
                         help="Reconcile ESPN vs Fox score sequences for matched games in --season/--week")
    parser.add_argument("--fox-reconcile-report", action="store_true",
                         help="Reconcile and print a full diff/unusable report")
    args = parser.parse_args()

    if args.find_team:
        find_team(args.find_team)
        sys.exit(0)

    conn = db.init_db()

    if args.fox_rebuild_sequences:
        fox_rebuild_sequences(conn, fox_event_id=args.fox_event)
        sys.exit(0)

    if args.fox_event:
        fox_pull_event(conn, args.fox_event, force=args.fox_force)
        sys.exit(0)

    if args.fox_pull:
        fox_pull(conn, args)
        sys.exit(0)

    if args.fox_sync_teams:
        fox_sync_teams(conn, season=args.season, week=args.week)
        sys.exit(0)

    if args.fox_teams_worklist:
        fox_teams_worklist(conn)
        sys.exit(0)

    if args.fox_match_team:
        espn_id, fox_id = args.fox_match_team.split(":")
        fox_match_team(conn, espn_id, int(fox_id))
        sys.exit(0)

    if args.fox_match_games:
        fox_match_games(conn, season=args.season, week=args.week)
        sys.exit(0)

    if args.fox_reconcile:
        fox_reconcile_run(conn, season=args.season, week=args.week)
        sys.exit(0)

    if args.fox_reconcile_report:
        fox_reconcile.print_report(conn, season=args.season, week=args.week)
        sys.exit(0)

    if args.compute_sequences:
        game_id = args.game if args.game else None
        n = db.compute_play_sequences(conn, game_id=game_id)
        print(f"play_sequence computed for {n} game(s).")
        sys.exit(0)

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
