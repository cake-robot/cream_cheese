import argparse
import logging
import sys
from datetime import date

from src import db, espn, fetchlog, fox, fox_match, fox_reconcile, fox_wp, live, live_replay, scoring
from src.config import DEFAULT_SEASON, FOX_SEASON_ANCHORS, FOX_SCAN_OVERRUN


def _caller_label(args):
    """Best-effort fetch_log.caller for this invocation, in the same order
    main() dispatches below. One label per process (fetchlog.configure() is
    called once) -- purely descriptive grouping for the Feed page, never
    used for control flow, so a run that touches more than one path (e.g.
    the default discover-then-fetch-details pipeline) just gets the label
    of whichever branch it entered first."""
    if args.find_team:
        return "find-team"
    if args.live or args.live_once:
        return "live"
    if args.fox_event:
        return "fox-event"
    if args.fox_pull:
        return "fox-scan"
    if args.fox_sync_teams:
        return "fox-sync-teams"
    if args.fox_match_games:
        return "fox-match"
    if args.seed_teams:
        return "find-team"
    if args.game:
        return "detail"
    return "discover"


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
        # See the week=1 comment in the full-season branch below -- same
        # unreliable week=None behavior on non-current seasons applies here.
        postseason = espn.fetch_scoreboard(args.season, week=1, season_type=3)
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
        # week=None is unreliable for seasontype=3 on non-current seasons -- ESPN's
        # `dates` param alone resolves to whatever postseason it considers "current"
        # rather than the requested season (confirmed: for season=2023 it silently
        # returned Jan-2023 bowls, i.e. the *2022* season's postseason, while
        # week=1 correctly returns the full 2023 postseason regardless of how old
        # the season is). All of a season's bowls/CFP live under week=1.
        games = espn.fetch_scoreboard(args.season, week=1, season_type=3)
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
            db.upsert_game_raw_json(conn, game_id, summary)
            db.mark_detail_fetched(conn, game_id, home_score, away_score, attendance, initial_home_wp)

    print(f"Detail fetch complete: {n} games processed.")


def refetch_detail(conn, game_id):
    """Re-pull an already detail-fetched game from ESPN, safely -- unlike
    fetch_details() (which only ever touches a game with detail_fetched=0,
    so it can never be used to correct one), this always deletes the game's
    existing win_probability rows before re-inserting the fresh set.

    That delete-first step matters because upsert_win_probability() is
    INSERT OR IGNORE keyed on (game_id, play_id): re-fetching on top of an
    existing row silently keeps the OLD row (old sequence_number, old
    score/period/clock) instead of the new one. A play seen by an earlier,
    incomplete pull (an ESPN payload that hadn't been fully backfilled yet,
    or -- the case this was built for -- one the live poller already wrote
    mid-game) then keeps whatever position it had at that time forever,
    scrambling every play's chronological order that the two pulls happen
    to disagree on. src/live.py's handle_completions() already does this
    delete-first dance for the live->final transition; this is the same
    safety for the "an already-completed game's ESPN data looked
    incomplete/wrong, try pulling it again" case, which has no other safe,
    reusable entry point.

    Also recomputes play_sequence (order depends on the freshly-inserted
    sequence_numbers) -- scoring is the caller's job, same as
    fetch_details(), since a caller might want to refetch several games
    before scoring them all in one score_games() call (it runs
    apply_corrections() internally, which iterates the full corrections
    table on every invocation -- batch, don't call per-game).
    """
    row = conn.execute(
        "SELECT away_team_abbr, home_team_abbr FROM games WHERE game_id = ?", (game_id,)
    ).fetchone()
    if not row:
        print(f"Game {game_id} not in DB -- nothing to refetch.")
        return
    label = f"{row['away_team_abbr']} @ {row['home_team_abbr']}"
    print(f"Refetching detail for {label} ({game_id})...")

    summary = espn.fetch_game_summary(game_id)
    wp_rows, home_score, away_score, attendance, initial_home_wp = espn.parse_summary_detail(summary)
    if not wp_rows:
        print("  Warning: no win probability data in this fetch either.")

    with conn:
        db.delete_win_probability(conn, game_id)
        if wp_rows:
            db.upsert_win_probability(conn, wp_rows)
        db.upsert_game_raw_json(conn, game_id, summary)
        db.mark_detail_fetched(conn, game_id, home_score, away_score, attendance, initial_home_wp)

    db.compute_play_sequences(conn, game_id=game_id)
    print(f"  {len(wp_rows)} WP rows stored, chronological order recomputed.")


def backfill_initial_wp(conn):
    """Repair games whose initial_home_wp came from ESPN's post-2026 first-play
    winprobability entry, using the betting line in their archived /summary.

    Entirely offline -- reads game_raw_json, makes no network calls. Only
    touches rows the rank guard accepts (source 'espn_wp' or NULL), so a
    genuine 2022-2025 pregame value ('espn_wp_pregame') and anything already
    upgraded to 'predictor' are both left alone no matter how often this runs.

    The predictor itself cannot be recovered here: ESPN drops it the moment a
    game starts, so for games already played the closing spread is the only
    pregame signal that still exists.
    """
    rows = conn.execute("""
        SELECT g.game_id, g.away_team_abbr, g.home_team_abbr,
               g.initial_home_wp, g.initial_home_wp_source
          FROM games g
          JOIN game_raw_json r ON r.game_id = g.game_id
         WHERE g.initial_home_wp_source IS NULL
            OR g.initial_home_wp_source = 'espn_wp'
         ORDER BY g.game_date
    """).fetchall()

    if not rows:
        print("0 games need initial_home_wp repair.")
        return

    n_fixed = n_noline = n_rejected = 0
    for row in rows:
        summary = db.get_game_raw_json(conn, row["game_id"])
        if summary is None:
            continue
        value = espn.parse_spread_home_wp(summary)
        if value is None:
            n_noline += 1
            continue
        with conn:
            if db.set_initial_home_wp(conn, row["game_id"], value, "spread"):
                n_fixed += 1
                # A NULL previous value is a game that never had a pregame WP
                # at all (no ESPN entry to parse); the line gives it one.
                before = ("%.4f" % row["initial_home_wp"]
                          if row["initial_home_wp"] is not None else "  none")
                print(f"  {row['game_id']} {row['away_team_abbr']:>5} @ "
                      f"{row['home_team_abbr']:<5} {before} -> {value:.4f}")
            else:
                n_rejected += 1

    print(f"\ninitial_home_wp repaired for {n_fixed} game(s); "
          f"{n_noline} had no line archived, {n_rejected} already better.")
    if n_fixed:
        print("Re-run with --score-only --rescore to refresh affected metrics.")


def backfill_raw_json(conn, limit):
    """Archive game_raw_json for games that were detail-fetched before that
    table existed. Incremental and resumable by design (default cap of
    `limit` games/run, oldest game_date first) -- meant to be re-run
    periodically rather than pulling the whole backlog in one shot, since
    (unlike fetch_details()) this path re-fetches from ESPN and costs a
    real request per game."""
    rows = conn.execute("""
        SELECT g.game_id, g.away_team_abbr, g.home_team_abbr
        FROM games g
        LEFT JOIN game_raw_json r ON r.game_id = g.game_id
        WHERE g.completed = 1 AND g.detail_fetched = 1 AND r.game_id IS NULL
        ORDER BY g.game_date
        LIMIT ?
    """, (limit,)).fetchall()

    total_remaining = conn.execute("""
        SELECT COUNT(*) FROM games g
        LEFT JOIN game_raw_json r ON r.game_id = g.game_id
        WHERE g.completed = 1 AND g.detail_fetched = 1 AND r.game_id IS NULL
    """).fetchone()[0]

    n = len(rows)
    if n == 0:
        print("0 games need raw JSON backfill.")
        return

    for i, row in enumerate(rows, 1):
        game_id = row["game_id"]
        label = f"{row['away_team_abbr']} @ {row['home_team_abbr']}"
        print(f"Backfilling raw JSON {i}/{n}: {label} ({game_id})...")
        summary = espn.fetch_game_summary(game_id)
        with conn:
            db.upsert_game_raw_json(conn, game_id, summary)

    print(f"Raw JSON backfill complete: {n} games fetched, "
          f"{total_remaining - n} still remaining.")


def backfill_conferences(conn):
    """Backfill games.home_conference_id/away_conference_id for games that
    predate conference capture (added to discover_games()'s scoreboard-fed
    upserts) or were discovered via --team mode's team-schedule endpoint,
    which carries no conferenceId at all. One scoreboard call per
    (season, week[, season_type]) group present in the DB, not per game --
    a real request per group but cheap (~80 total for the whole 2022-2026
    history) since a single scoreboard fetch covers every game in that
    week. Idempotent/safe to re-run: only groups with at least one NULL
    conference id are fetched, and the UPDATE below never clobbers an
    already-good value.

    Matches each fetched game back to its stored row by team_id rather than
    assuming ESPN's home/away orientation always matches what's stored --
    defensive, not known to actually happen, but cheap to guard against."""
    groups = conn.execute("""
        SELECT DISTINCT season_year, season_type, week FROM games
        WHERE week IS NOT NULL
          AND (home_conference_id IS NULL OR away_conference_id IS NULL)
        ORDER BY season_year, season_type, week
    """).fetchall()

    if not groups:
        print("0 (season, week) groups need conference backfill.")
        return

    n_updated = 0
    for i, grp in enumerate(groups, 1):
        season, season_type, week = grp["season_year"], grp["season_type"], grp["week"]
        # season_type=3 (postseason) is unreliable without an explicit
        # week=1 -- see the week=1 comment in discover_games()'s full-season
        # branch. Postseason rows are always stored with week=1 already, so
        # this just makes the fetch match what's on disk.
        fetch_week = 1 if season_type == 3 else week
        print(f"[{i}/{len(groups)}] Fetching scoreboard season={season} "
              f"season_type={season_type} week={fetch_week}...", end=" ", flush=True)
        games = espn.fetch_scoreboard(season, week=fetch_week, season_type=season_type)
        for g in games:
            conn.execute("""
                UPDATE games SET
                    home_conference_id = CASE
                        WHEN home_team_id = :home_team_id THEN COALESCE(:home_conference_id, home_conference_id)
                        WHEN home_team_id = :away_team_id THEN COALESCE(:away_conference_id, home_conference_id)
                        ELSE home_conference_id END,
                    away_conference_id = CASE
                        WHEN away_team_id = :home_team_id THEN COALESCE(:home_conference_id, away_conference_id)
                        WHEN away_team_id = :away_team_id THEN COALESCE(:away_conference_id, away_conference_id)
                        ELSE away_conference_id END
                WHERE game_id = :game_id
            """, g)
        conn.commit()
        print(f"{len(games)} games")
        n_updated += len(games)

    print(f"Conference backfill complete: {len(groups)} (season, week) groups, "
          f"{n_updated} game rows touched.")


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


def _fox_pbp_is_final(status_line):
    """Whether Fox's own status line means this event will never produce any
    *more* play data -- either it already finished (FINAL) or it will never
    be played at all (CANCELLED/POSTPONED). Anything else -- blank (still
    scheduled, pre-kickoff) or a live in-progress clock -- means real plays
    may still show up on a later fetch, so pbp_fetched must not be set yet.
    """
    s = (status_line or "").upper()
    return "FINAL" in s or "CANCEL" in s or "POSTPON" in s


def _fox_store_pbp(conn, fox_event_id, payload, status_line=""):
    """
    Parse+store whatever plays this fetch returned, but only latch
    pbp_fetched=1 when there's actually something final to latch: either
    real plays came back, or the event's status says none ever will
    (_fox_pbp_is_final). Fox creates an event row for a game's ID well
    before kickoff (an "in_window" date match, zero plays, blank status) --
    marking that unconditionally as pbp_fetched used to permanently starve
    the game of real data, since every later cache lookup in _fox_get()
    treats pbp_fetched=1 as "nothing left to do" and never asks again, even
    after the game is actually played. Confirmed in production data:
    hundreds of future-dated fox_events rows latched at 0 plays back when
    the ID walk first overran into next season's already-created schedule.
    """
    plays = fox.parse_pbp_plays(payload)
    db.upsert_fox_plays(conn, fox_event_id, plays)
    seq = fox.build_score_sequence(plays)
    db.replace_fox_score_sequence(conn, fox_event_id, seq)
    if plays or _fox_pbp_is_final(status_line):
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
                header = fox.parse_header(payload)
                db.upsert_fox_event(conn, _fox_event_dict(fox_event_id, "ok", header, in_window))
                plays, seq = _fox_store_pbp(conn, fox_event_id, payload, header.get("status_line"))
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
        plays, seq = _fox_store_pbp(conn, fox_event_id, payload, header.get("status_line"))
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


def _fox_pick_anchor(conn, args, window_start, window_end, fetch_threshold_days=10):
    """
    Pick whichever candidate anchor is genuinely closest to the target
    window -- the nearest already-probed events on either side, and the
    season's static seed (FOX_SEASON_ANCHORS) -- rather than accepting the
    first "close enough" already-known candidate under an arbitrary
    threshold.

    That looser approach was tried and failed twice on 2024: (1) with no
    proximity check at all, a leftover 2025 event satisfied the raw SQL
    date-string comparison (any 2025 date is textually >= any 2024 date)
    and got picked as anchor for every regular-season week, storing zero
    real games while still reporting success; (2) with a 45-day threshold,
    an already-known *same-season* event 26 days outside the target week
    passed the check and got picked over the static seed (which sat
    squarely inside the actual window) -- 26 days is short enough to look
    "close enough," but too far for FOX_SCAN_OVERRUN's K=25 to bridge and
    still fully explore the target week, so the walk found only a sliver
    of it. Comparing actual gap-in-days across every option, including the
    static seed, and taking the minimum avoids both failure modes without
    depending on a threshold tuned by hand for one season's ID spacing.

    The static seed's own date isn't known without fetching it, so that
    fetch only happens when no already-known candidate is already close
    (<= fetch_threshold_days) -- once a season is well underway, adjacent
    weeks' own data is already close enough and this costs nothing extra.
    """
    if args.fox_anchor:
        return args.fox_anchor

    def _gap(iso_date, edge):
        return abs((date.fromisoformat(iso_date) - date.fromisoformat(edge)).days)

    candidates = []  # (gap_days, fox_event_id)

    row = conn.execute("""
        SELECT fox_event_id, event_date FROM fox_events
        WHERE event_date IS NOT NULL AND event_date <= ?
        ORDER BY event_date DESC, fox_event_id DESC LIMIT 1
    """, (window_end,)).fetchone()
    if row:
        candidates.append((_gap(row["event_date"], window_end), row["fox_event_id"]))

    row = conn.execute("""
        SELECT fox_event_id, event_date FROM fox_events
        WHERE event_date IS NOT NULL AND event_date >= ?
        ORDER BY event_date ASC, fox_event_id ASC LIMIT 1
    """, (window_start,)).fetchone()
    if row:
        candidates.append((_gap(row["event_date"], window_start), row["fox_event_id"]))

    static_id = FOX_SEASON_ANCHORS.get(args.season)
    if static_id is not None and (not candidates or min(c[0] for c in candidates) > fetch_threshold_days):
        payload = fox.fetch_event(static_id)
        if payload:
            static_date = fox.parse_header(payload)["event_date"]
            if static_date:
                gap = min(_gap(static_date, window_start), _gap(static_date, window_end))
                candidates.append((gap, static_id))

    if not candidates:
        return static_id
    candidates.sort()
    return candidates[0][1]


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
    plays, seq = _fox_store_pbp(conn, fox_event_id, payload, header.get("status_line"))
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


def fox_sync_teams(conn, season=None, week=None, season_type=2):
    seeded, matched = fox_match.sync_team_crosswalk(conn, season=season, week=week, season_type=season_type)
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


def fox_match_games(conn, season=None, week=None, season_type=2):
    attempted, matched = fox_match.match_all_games(conn, season=season, week=week, season_type=season_type)
    print(f"Game matching: {attempted} game(s) in scope, {matched} matched to a Fox event.")


def fox_reconcile_run(conn, season=None, week=None, season_type=2):
    results = fox_reconcile.reconcile_all(conn, season=season, week=week, season_type=season_type)
    counts = {}
    for r in results:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    summary = ", ".join(f"{t}={n}" for t, n in sorted(counts.items()))
    print(f"Reconciled {len(results)} game(s): {summary}")


def fox_synthesize_wp_run(conn, season=None, week=None, season_type=2):
    candidates, synthesized = fox_wp.synthesize_missing_wp(conn, season=season, week=week, season_type=season_type)
    print(f"Fox WP synthesis: {candidates} candidate(s) (no ESPN WP, Fox-matched), "
          f"{synthesized} synthesized. Run --score-only next to score them.")


def main():
    parser = argparse.ArgumentParser(description="CFB data pipeline")
    parser.add_argument("--season", type=int, default=DEFAULT_SEASON)
    parser.add_argument("--week", type=int, help="Specific week (regular season)")
    parser.add_argument("--season-type", type=int, default=2,
                         help="ESPN season_type for --fox-sync-teams/--fox-match-games/--fox-reconcile* "
                              "(2=regular [default], 3=postseason)")
    parser.add_argument("--team", type=str, help="Team ID (uses team schedule endpoint)")
    parser.add_argument("--game", type=str, help="Single game ID")
    parser.add_argument("--refetch", action="store_true",
                         help="With --game: re-pull an already detail-fetched game from ESPN and "
                              "rescore it, safely (deletes its win_probability rows first -- see "
                              "refetch_detail()'s docstring for why a plain re-fetch on top of "
                              "existing rows silently corrupts chronological order). Use when a "
                              "completed game's ESPN data looked incomplete or wrong the first "
                              "time and you want to try again.")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--detail-only", action="store_true")
    parser.add_argument("--score-only", action="store_true", help="Only run Phase 3 scoring")
    parser.add_argument("--skip-scoring", action="store_true", help="Skip Phase 3 scoring")
    parser.add_argument("--rescore", action="store_true", help="Re-score already-scored games")
    parser.add_argument("--compute-sequences", action="store_true", help="Compute play_sequence for all WP rows")
    parser.add_argument("--backfill-initial-wp", action="store_true",
                         help=("Repair initial_home_wp from the archived betting line for "
                               "games whose value came from ESPN's post-2026 first-play "
                               "winprobability entry. Offline -- reads game_raw_json, no "
                               "network. Safe/idempotent: never downgrades a better source."))
    parser.add_argument("--backfill-raw-json", action="store_true",
                         help="Archive ESPN's full /summary JSON (gzip) into game_raw_json for "
                              "already detail-fetched games missing it -- incremental, capped by "
                              "--backfill-raw-json-limit; re-fetches from ESPN (not free like the "
                              "normal detail-fetch path), safe to re-run repeatedly")
    parser.add_argument("--backfill-raw-json-limit", type=int, default=25, metavar="N",
                         help="Max games to fetch per --backfill-raw-json run (default 25)")
    parser.add_argument("--backfill-conferences", action="store_true",
                         help="Backfill home_conference_id/away_conference_id for games discovered "
                              "before conference capture existed, or via --team mode's team-schedule "
                              "path (which carries no conferenceId). One scoreboard call per "
                              "(season, week) group, idempotent, safe to re-run.")
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
    parser.add_argument("--fox-synthesize-wp", action="store_true",
                         help="Synthesize win_probability rows (score_diff + elapsed regression) "
                              "for --season/--week games with zero ESPN WP but a Fox match")
    parser.add_argument("--live", action="store_true",
                         help="Run the live/in-progress poll loop (long-running)")
    parser.add_argument("--live-once", action="store_true",
                         help="Run exactly one live poll cycle, then exit")
    parser.add_argument("--live-interval", type=int, metavar="N",
                         help="Force a fixed N-second poll interval, disabling schedule-aware "
                              "sleeping (default: derived from kickoff times in `games` -- see "
                              f"src/live.py's _schedule_interval; {live.LIVE_INTERVAL_SECONDS}s "
                              "while live, otherwise sleeps to the next kickoff/week-anchor "
                              "boundary, up to days idle between game weeks). Also the "
                              "documented fallback to the old always-poll behaviour.")
    parser.add_argument("--live-budget", type=int, metavar="N",
                         help=f"Max per-game /summary fetches per live cycle (default {live.LIVE_SUMMARY_BUDGET})")
    parser.add_argument("--live-date", type=str, metavar="YYYYMMDD",
                         help="Point the live scoreboard fetch at a specific ET day instead of "
                              "today/tomorrow -- only allowed together with --live-dry-run, so a "
                              "stray past-date run can never mutate historical rows")
    parser.add_argument("--live-dry-run", action="store_true",
                         help="Fetch and score live games but write nothing to the DB; logs what "
                              "would have been written")
    parser.add_argument("--live-shadow", action="store_true",
                         help="Run the live loop writing only live_scores/live_metrics/"
                              "live_score_history -- never touches win_probability, "
                              "games.watchability_score, or game_metrics")
    parser.add_argument("--live-until", type=str, metavar="HH:MM",
                         help="Exit the live loop cleanly at this US/Eastern wall-clock time "
                              "(next occurrence -- e.g. 02:00 started at 9am means 2am tomorrow). "
                              "Lets a scheduler start --live at a fixed time and trust it to end "
                              "its own gameday window.")
    parser.add_argument("--live-replay", action="store_true",
                         help="Offline replay of a stored game's WP series through the live "
                              "scorer at every prefix (requires --game); no network access")
    parser.add_argument("--live-replay-plot", action="store_true",
                         help="With --live-replay, print an ASCII curve instead of a row table")
    args = parser.parse_args()

    # Hoisted out of the `if args.live or args.live_once:` branch below --
    # it used to be configured only there, so any logging added elsewhere
    # (e.g. src/espn.py, src/fox.py) was invisible on every non-live
    # invocation (bare `just discover`, `just rescore`, ...).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    # Moved ahead of the --find-team early exit below (it used to run only
    # right before the branches that actually mutate `games`/etc.) --
    # find_team() fetches through espn.py same as everything else, and
    # fetchlog.record() needs the fetch_log table to already exist.
    # init_db() is idempotent, so running it this early costs nothing on
    # every other path.
    conn = db.init_db()

    fetchlog.configure(caller=_caller_label(args))

    if args.find_team:
        find_team(args.find_team)
        sys.exit(0)

    if args.live_date and not args.live_dry_run:
        print("--live-date is only allowed together with --live-dry-run "
              "(so a stray past-date run can never mutate historical rows).", file=sys.stderr)
        sys.exit(1)

    if args.live_dry_run and args.live_shadow:
        print("--live-dry-run and --live-shadow are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    if args.live_until and not args.live:
        print("--live-until only applies to --live (a single --live-once cycle "
              "already exits immediately).", file=sys.stderr)
        sys.exit(1)

    if args.live_until:
        try:
            live._next_et_deadline(args.live_until)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    if args.refetch and not args.game:
        # Without this, --refetch is silently never read (it's only checked
        # inside the `if args.game:` branch below) and execution falls
        # through to an unscoped discover_games() -- a full-season scoreboard
        # walk nobody asked for. Confirmed cause of a 17-call burst from a
        # `--refetch --discover-only` typo missing --game (2026-09-04).
        print("--refetch requires --game GAME_ID", file=sys.stderr)
        sys.exit(1)

    if args.live_replay:
        if not args.game:
            print("--live-replay requires --game GAME_ID", file=sys.stderr)
            sys.exit(1)
        rows = live_replay.replay_game(conn, args.game, step=1)
        if args.live_replay_plot:
            print(live_replay.replay_curve_ascii(rows))
        else:
            print(f"{'i':>4} {'prog':>5} {'per':>3} {'live':>6} {'so_far':>7} {'from_here':>9}  headline")
            for r in rows:
                so_far = f"{r['quality_so_far']:.3f}" if r["quality_so_far"] is not None else "  n/a"
                print(f"{r['i']:>4} {r['progress']:>5.2f} {str(r['period']):>3} "
                      f"{r['live_score']:>6.3f} {so_far:>7} {r['drama_from_here']:>9.3f}  {r['headline']}")
        sys.exit(0)

    if args.live or args.live_once:
        interval = args.live_interval  # None -> schedule-aware, see live._schedule_interval
        budget = args.live_budget or live.LIVE_SUMMARY_BUDGET
        if args.live_dry_run:
            mode = "dry_run"
        elif args.live_shadow:
            mode = "shadow"
        else:
            mode = "normal"
        live.run_forever(
            conn, interval=interval, summary_budget=budget,
            once=args.live_once, mode=mode, dates=args.live_date,
            until=args.live_until,
        )
        sys.exit(0)

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
        fox_sync_teams(conn, season=args.season, week=args.week, season_type=args.season_type)
        sys.exit(0)

    if args.fox_teams_worklist:
        fox_teams_worklist(conn)
        sys.exit(0)

    if args.fox_match_team:
        espn_id, fox_id = args.fox_match_team.split(":")
        fox_match_team(conn, espn_id, int(fox_id))
        sys.exit(0)

    if args.fox_match_games:
        fox_match_games(conn, season=args.season, week=args.week, season_type=args.season_type)
        sys.exit(0)

    if args.fox_reconcile:
        fox_reconcile_run(conn, season=args.season, week=args.week, season_type=args.season_type)
        sys.exit(0)

    if args.fox_reconcile_report:
        fox_reconcile.print_report(conn, season=args.season, week=args.week, season_type=args.season_type)
        sys.exit(0)

    if args.fox_synthesize_wp:
        fox_synthesize_wp_run(conn, season=args.season, week=args.week, season_type=args.season_type)
        sys.exit(0)

    if args.compute_sequences:
        game_id = args.game if args.game else None
        n = db.compute_play_sequences(conn, game_id=game_id)
        print(f"play_sequence computed for {n} game(s).")
        sys.exit(0)

    if args.backfill_initial_wp:
        backfill_initial_wp(conn)
        sys.exit(0)

    if args.backfill_raw_json:
        backfill_raw_json(conn, args.backfill_raw_json_limit)
        sys.exit(0)

    if args.backfill_conferences:
        backfill_conferences(conn)
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
        if args.refetch:
            refetch_detail(conn, args.game)
        else:
            fetch_details(conn, game_ids)
        if not args.skip_scoring:
            # A refetch is specifically trying to correct a game's existing
            # score/metrics, so force rescore=True regardless of --rescore --
            # skipping it would leave the whole point of --refetch undone.
            scoring.score_games(conn, game_ids=game_ids, rescore=(args.rescore or args.refetch))
        return

    if not args.detail_only:
        game_ids = discover_games(conn, args)

    if not args.discover_only:
        fetch_details(conn, game_ids if game_ids else None)
        if not args.skip_scoring:
            scoring.score_games(conn, game_ids=game_ids if game_ids else None, rescore=args.rescore)


if __name__ == "__main__":
    main()
