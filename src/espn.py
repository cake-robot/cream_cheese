import time
import requests
from . import fetchlog
from .config import ESPN_BASE, RATE_LIMIT_SECONDS

_last_request_time = 0.0


def fetch_json(url, kind="unknown", game_id=None, source_ref=None):
    """`kind`/`game_id`/`source_ref` exist only for src/fetchlog.py's
    fetch_log rows -- every caller below passes its own kind so a Feed page
    can tell a scoreboard poll apart from a per-game summary fetch. The
    RATE_LIMIT_SECONDS sleep happens before timing starts, so latency_ms
    reflects the server, not our own pacing."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    t0 = time.monotonic()
    try:
        resp = requests.get(url, timeout=15)
        _last_request_time = time.time()
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        fetchlog.record(
            "espn", kind, url, ok=False, http_status=status,
            latency_ms=int((time.monotonic() - t0) * 1000), error=exc,
            game_id=game_id, source_ref=source_ref,
        )
        raise
    fetchlog.record(
        "espn", kind, url, ok=True, http_status=resp.status_code,
        latency_ms=int((time.monotonic() - t0) * 1000), bytes=len(resp.content),
        game_id=game_id, source_ref=source_ref,
    )
    return data


def fetch_scoreboard(season, week=None, season_type=2):
    url = f"{ESPN_BASE}/scoreboard?limit=100&dates={season}&seasontype={season_type}&groups=80"
    if week is not None:
        url += f"&week={week}"
    data = fetch_json(url, kind="scoreboard")
    events = data.get("events", [])
    return [parse_scoreboard_game(e) for e in events if e.get("competitions")]


def fetch_scoreboard_dates(dates, limit=200):
    """
    Scoreboard fetch scoped by explicit date(s) rather than a season/week --
    used by the live poller to pull an entire day or multi-day window in one
    request, live and final and future games alike (no seasontype filter, no
    week resolution needed).

    dates: 'YYYYMMDD' for a single day, or 'YYYYMMDD-YYYYMMDD' for a range.
    Verified against the live API: ESPN interprets `dates` in US/Eastern and
    a 3-day range returns every event with no truncation at limit=200 (a
    Saturday tops out around 60-70 FBS games). Returned event.date fields are
    UTC, so a late West-Coast kickoff on the requested Eastern day can appear
    dated the next UTC day -- callers wanting a specific viewer-local day
    should request a padded range and filter the results themselves rather
    than relying on this call alone to draw the boundary.
    """
    url = f"{ESPN_BASE}/scoreboard?limit={limit}&dates={dates}&groups=80"
    data = fetch_json(url, kind="scoreboard")
    events = data.get("events", [])
    return [parse_scoreboard_game(e) for e in events if e.get("competitions")]


def fetch_team_schedule(team_id, season):
    url = f"{ESPN_BASE}/teams/{team_id}/schedule?season={season}"
    data = fetch_json(url, kind="schedule")
    events = data.get("events", [])
    return [parse_team_schedule_game(e) for e in events if e.get("competitions")]


def fetch_teams_list():
    teams = []
    page = 1
    while True:
        url = f"{ESPN_BASE}/teams?limit=100&page={page}"
        data = fetch_json(url, kind="teams")
        items = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        if not items:
            break
        for item in items:
            t = item.get("team", {})
            teams.append({
                "id": t.get("id"),
                "name": t.get("displayName", ""),
                "abbreviation": t.get("abbreviation", ""),
                "school": t.get("location", ""),
            })
        page += 1
    return teams


def fetch_game_summary(game_id):
    url = f"{ESPN_BASE}/summary?event={game_id}"
    return fetch_json(url, kind="summary", game_id=game_id)


def _get_rank(competitor):
    curated = competitor.get("curatedRank")
    if curated:
        rank = curated.get("current")
    else:
        rank = competitor.get("rank")
    if rank and isinstance(rank, int) and rank <= 25:
        return rank
    return None


def _is_conference_championship_note(season_type, event_note):
    """
    ESPN's own `conferenceCompetition` flag is false for conference
    championship games themselves -- verified via live API checks across
    all 9 FBS conferences that hold one (SEC, Big Ten, Big 12, ACC, MAC,
    American, Mountain West, Sun Belt, Conference USA). `event_note`
    (ESPN's own branded-event label, e.g. "SEC Championship") reliably
    says "<Conference> Championship" for every one of them, whether that
    conference uses a fixed neutral venue or the higher seed's home
    stadium -- unlike a venue-based check, this covers all of them
    uniformly with no per-conference special-casing.

    Gated to season_type == 2 (regular season) because the College
    Football Playoff National Championship's note also contains the word
    "championship", but it's a cross-conference postseason game
    (season_type == 3) -- requiring regular-season scope excludes it
    without needing to pattern-match around that one case.
    """
    if season_type != 2 or not event_note:
        return False
    return "championship" in event_note.lower()


def _parse_competition(event, comp):
    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})

    status = comp.get("status", {}).get("type", {})
    season = event.get("season", {})
    week_obj = event.get("week")
    if isinstance(week_obj, dict):
        week = week_obj.get("number")
    elif isinstance(week_obj, int):
        week = week_obj
    else:
        week = None

    # Season type: try event.season.type, then event.seasonType
    season_type = season.get("type")
    if season_type is None:
        st = event.get("seasonType", {})
        season_type = st.get("type") or (int(st["id"]) if st.get("id") else 2)

    home_team = home.get("team", {})
    away_team = away.get("team", {})

    venue = comp.get("venue")
    venue_name = venue.get("fullName") if venue else None

    # ESPN's per-competition branded-event label, e.g. "SEC Championship",
    # "Duke's Mayo Bowl", "College Football Playoff Semifinal at the Orange
    # Bowl" -- empty for ordinary games. Captured verbatim, no filtering or
    # interpretation here; just the raw headline for later use.
    notes = comp.get("notes") or []
    event_note = notes[0].get("headline") if notes else None
    conference_game = int(
        bool(comp.get("conferenceCompetition", False))
        or _is_conference_championship_note(season_type, event_note)
    )

    status_state = status.get("state", "pre")

    # Scoreboard competitors carry a live "score" field even before kickoff --
    # verified live: a pregame competitor returns score="0" (a string), not
    # null. upsert_game's COALESCE(excluded.home_score, games.home_score)
    # treats 0 as a real value, so parsing it unconditionally would write
    # 0-0 into every not-yet-started game on every discovery/live-poll pass.
    # Only trust the scoreboard's score once the game has actually started.
    home_score = away_score = None
    if status_state != "pre":
        try:
            home_score = int(home.get("score"))
        except (TypeError, ValueError):
            home_score = None
        try:
            away_score = int(away.get("score"))
        except (TypeError, ValueError):
            away_score = None

    # Live status detail, read from the same comp.status block as status_state/
    # completed above. `period`/`clock`/`displayClock` sit alongside `type` on
    # comp.status (not inside comp.status.type) -- verified live: the summary
    # endpoint's header status carries `type` only, no clock, so the
    # scoreboard is the sole source for these. All pregame-zero / absent.
    raw_status = comp.get("status", {})
    status_period = raw_status.get("period")
    status_clock_seconds = raw_status.get("clock")
    status_clock_display = raw_status.get("displayClock")
    status_detail = status.get("shortDetail")

    return {
        "game_id": str(event.get("id", comp.get("id", ""))),
        "season_year": season.get("year"),
        "season_type": season_type,
        "week": week,
        "game_date": comp.get("date") or event.get("date"),
        "home_team_id": str(home_team.get("id", "")),
        "home_team_abbr": home_team.get("abbreviation", ""),
        "home_team_name": home_team.get("displayName", ""),
        "home_rank": _get_rank(home),
        "away_team_id": str(away_team.get("id", "")),
        "away_team_abbr": away_team.get("abbreviation", ""),
        "away_team_name": away_team.get("displayName", ""),
        "away_rank": _get_rank(away),
        "conference_game": conference_game,
        "neutral_site": int(bool(comp.get("neutralSite", False))),
        "venue_name": venue_name,
        "event_note": event_note,
        "status_state": status_state,
        "completed": int(bool(status.get("completed", False))),
        "status_period": status_period,
        "status_clock_display": status_clock_display,
        "status_clock_seconds": status_clock_seconds,
        "status_detail": status_detail,
        "home_score": home_score,
        "away_score": away_score,
        "attendance": None,
        "initial_home_wp": None,
        "detail_fetched": 0,
        "watchability_score": None,
    }


def parse_scoreboard_game(event):
    comp = event["competitions"][0]
    return _parse_competition(event, comp)


def parse_team_schedule_game(event):
    comp = event["competitions"][0]
    return _parse_competition(event, comp)


def parse_summary_game_meta(summary):
    """Extract game metadata from a summary response (for --game bootstrap)."""
    header = summary.get("header", {})
    comp = header.get("competitions", [{}])[0]
    competitors = comp.get("competitors", [])

    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})

    season = header.get("season", {})
    week_val = header.get("week")  # integer directly in header
    status = comp.get("status", {}).get("type", {})

    home_team = home.get("team", {})
    away_team = away.get("team", {})

    venue = comp.get("venue")
    venue_name = venue.get("fullName") if venue else None

    season_type = season.get("type", 2)
    event_note = ((comp.get("notes") or [{}])[0]).get("headline")
    conference_game = int(
        bool(comp.get("conferenceCompetition", False))
        or _is_conference_championship_note(season_type, event_note)
    )

    return {
        "game_id": str(header.get("id", "")),
        "season_year": season.get("year"),
        "season_type": season_type,
        "week": week_val if isinstance(week_val, int) else None,
        "game_date": comp.get("date", ""),
        "home_team_id": str(home_team.get("id", "")),
        "home_team_abbr": home_team.get("abbreviation", ""),
        "home_team_name": home_team.get("displayName", ""),
        "home_rank": _get_rank(home),
        "away_team_id": str(away_team.get("id", "")),
        "away_team_abbr": away_team.get("abbreviation", ""),
        "away_team_name": away_team.get("displayName", ""),
        "away_rank": _get_rank(away),
        "conference_game": conference_game,
        "neutral_site": int(bool(comp.get("neutralSite", False))),
        "venue_name": venue_name,
        "event_note": event_note,
        "status_state": status.get("state", "post"),
        "completed": int(bool(status.get("completed", True))),
        # This bootstrap path (--game bootstrap of a game absent from the DB)
        # doesn't carry the scoreboard's live status.{period,clock,displayClock}
        # block -- the summary's header status has only `type`. Left None;
        # the live poller's scoreboard-based upserts populate these once the
        # game is actually tracked.
        "status_period": None,
        "status_clock_display": None,
        "status_clock_seconds": None,
        "status_detail": None,
        "home_score": None,
        "away_score": None,
        "attendance": None,
        "initial_home_wp": None,
        "detail_fetched": 0,
        "watchability_score": None,
    }


def _parse_clock(display):
    """Convert 'MM:SS' clock display to seconds remaining in the period."""
    try:
        parts = display.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None


def _iter_drives(summary):
    drives = summary.get("drives", {})
    out = list(drives.get("previous", []))
    current = drives.get("current")
    if isinstance(current, dict):
        out.append(current)
    elif isinstance(current, list):
        out.extend(current)
    return out


def extract_situational_plays(summary, home_team_id):
    """Regulation-only (period 1-4), valid-scrimmage-down plays from a raw
    ESPN /summary payload -- the exact same filter src/wp_situational.py's
    fitting script (scripts/build_wp_situational_module.py) uses, so a
    caller can feed these plays straight into wp_situational.coinflip_wp_offense().
    OT is deliberately excluded outright (not filtered downstream) -- see
    src/scoring.py's comeback_erosion for why.

    Works identically for a completed game's archived game_raw_json and a
    live game's freshly-fetched summary -- both are the same raw /summary
    shape.

    Returns a list of dicts in the drives' own (chronological) order:
    {play_id, elapsed_seconds, off_is_home, down, distance, yards_to_go,
    home_score, away_score}. play_id joins back onto the win_probability
    table's own play_id (see scoring.coinflip_wp_by_play_id). No goal_to_go
    field -- dropped 2026-09-01 as a Model C feature (see
    scripts/build_wp_situational_module.py) once a likelihood-ratio test
    confirmed it carries no information beyond distance/yards_to_go
    (goal_to_go is deterministic on those two: it's just "distance to a
    first down and distance to the end zone have converged").

    home_score/away_score are the score AS OF THE SNAP -- i.e. NOT a
    scoring play's own homeScore/awayScore field, which ESPN already
    updates to include that same play's points (and, for a touchdown, its
    try). down/distance/field position always describe the situation
    BEFORE the snap, so pairing them with a not-yet-true post-play score
    would hand the model an impossible combination: great field position
    AND the lead it's about to produce, stacked. Confirmed concretely on
    game 401752854 (PSU@Oregon): the play that ties the game -- 1st &
    Goal at the ORE 7, PSU trailing 10-17 -- carries homeScore=17 (the
    tying score already applied) on its own play record. Feeding the
    model that pairing read as 70% for PSU; feeding it the correct
    pre-snap margin (down 7) reads 23%, matching the surrounding drive's
    trend. Tracked here as a running score updated from EVERY play's own
    recorded score (not just the ones that pass the down/distance filter
    below), so a scoring play that itself isn't a valid scrimmage down
    (a kickoff/punt/INT return TD) still advances the count for whatever
    real scrimmage play comes next.
    """
    plays = []
    prev_home_score, prev_away_score = 0, 0
    for drive in _iter_drives(summary):
        off_team = str(drive.get("team", {}).get("id", ""))
        if not off_team:
            continue
        off_is_home = off_team == str(home_team_id)

        for play in drive.get("plays", []):
            period = (play.get("period") or {}).get("number")
            if period is not None and period <= 4:
                start = play.get("start", {})
                down = start.get("down")
                distance = start.get("distance")
                yards_to_go = start.get("yardsToEndzone")
                secs_remaining = _parse_clock((play.get("clock") or {}).get("displayValue") or "")
                valid = (
                    down is not None and distance is not None and yards_to_go is not None
                    and 1 <= down <= 4 and 0 < yards_to_go <= 100 and distance >= 0
                    and secs_remaining is not None
                )
                if valid:
                    elapsed_seconds = (period - 1) * 900 + (900 - secs_remaining)
                    plays.append({
                        "play_id": str(play.get("id", "")),
                        "elapsed_seconds": elapsed_seconds,
                        "off_is_home": off_is_home,
                        "down": down,
                        "distance": distance,
                        "yards_to_go": yards_to_go,
                        "home_score": prev_home_score,
                        "away_score": prev_away_score,
                    })

            # Gated on period <= 4, same as the emit check above -- an OT
            # play must never move this tracker, for two reasons at once:
            # (1) on a game with the documented "drive spans non-adjacent
            # periods" corruption, an out-of-order OT play could otherwise
            # retroactively corrupt a LATER-emitted regulation play's score
            # (confirmed on 401628439, the known-bad 8-OT game); (2) it's
            # also what the closing entry below reads once the loop ends --
            # gating here means that value freezes at the true end-of-
            # regulation score, so a game that goes to OT gets closed out
            # on its (by definition, tied) regulation-ending score rather
            # than however OT actually resolved it. Model C is never meant
            # to know OT happened at all, and this is the last place OT's
            # outcome could otherwise sneak back in.
            if period is not None and period <= 4:
                home_score_after = play.get("homeScore")
                away_score_after = play.get("awayScore")
                if home_score_after is not None:
                    prev_home_score = home_score_after
                if away_score_after is not None:
                    prev_away_score = away_score_after

    # The scoring-play-that-ends-regulation case: pushing score onto the
    # NEXT valid play (above) fixes the double-count bug, but if that
    # scoring play is also the last valid situational play in regulation
    # (its own ensuing kickoff/kneel-down never got a valid down/distance
    # reading, or there simply isn't a next regulation play), the score
    # change it caused would otherwise never appear anywhere in the
    # returned list at all -- comeback_erosion's arc-walk needs to see
    # that final regulation-era score change to credit a game-ending
    # comeback/lead-change. Confirmed empirically this isn't rare: ~9% of
    # a random sample had a final valid play whose scoreboard doesn't
    # match the game's real end-of-regulation score. Patched with one
    # synthetic closing entry, reusing the last play's situational read
    # (the best available proxy -- there's no "postgame" down/distance) but
    # the score AS OF THE END OF REGULATION (prev_home_score/prev_away_score
    # are now frozen there for an OT game, per the period gate above -- NOT
    # the game's true whole-game final score, which would smuggle OT's
    # outcome in here). play_id is blanked so this doesn't collide with the
    # real play's own entry in coinflip_wp_by_play_id's join.
    if plays and (plays[-1]["home_score"], plays[-1]["away_score"]) != (prev_home_score, prev_away_score):
        closer = dict(plays[-1])
        closer["play_id"] = ""
        closer["home_score"] = prev_home_score
        closer["away_score"] = prev_away_score
        plays.append(closer)

    return plays


def parse_summary_detail(summary):
    """
    Returns:
        wp_rows: list of dicts for win_probability table
        home_score: int or None
        away_score: int or None
        attendance: int or None
        initial_home_wp: float or None (first WP entry's homeWinPercentage)
    """
    # Build play map: play_id -> {period, clock_display, home_score, away_score, sequence_number}
    play_map = {}
    drives = summary.get("drives", {})

    def _index_drive(drive):
        for play in drive.get("plays", []):
            pid = str(play.get("id", ""))
            if pid:
                play_map[pid] = {
                    "period": play.get("period", {}).get("number"),
                    "clock_display": play.get("clock", {}).get("displayValue"),
                    "home_score": play.get("homeScore"),
                    "away_score": play.get("awayScore"),
                    "sequence_number": play.get("sequenceNumber"),
                }

    for drive in drives.get("previous", []):
        _index_drive(drive)

    # A live (in-progress) game's active drive lives in drives.current, which
    # is completely separate from drives.previous -- a finished game omits
    # it, and a not-yet-started game omits the whole `drives` key (both
    # verified live). Reading only `previous` meant every play in a live
    # game's active drive -- typically its newest, most decisive plays --
    # landed with period=None/clock=None/elapsed=None, which silently broke
    # late_volatility/clutch_finish right at the live edge. `current` is
    # documented (and observed on other ESPN sports) as a single dict, not a
    # list like `previous`; handle both shapes defensively in case a future
    # payload nests it as a list.
    current = drives.get("current")
    if isinstance(current, dict):
        _index_drive(current)
    elif isinstance(current, list):
        for drive in current:
            _index_drive(drive)

    # Extract scores and attendance from header
    header = summary.get("header", {})
    comp = header.get("competitions", [{}])[0]
    competitors = comp.get("competitors", [])
    home_comp = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away_comp = next((c for c in competitors if c.get("homeAway") == "away"), {})

    def _safe_int(val):
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    home_score = _safe_int(home_comp.get("score"))
    away_score = _safe_int(away_comp.get("score"))
    attendance = _safe_int(summary.get("gameInfo", {}).get("attendance"))

    game_id = str(header.get("id", ""))
    home_team_id = str(home_comp.get("team", {}).get("id", ""))
    away_team_id = str(away_comp.get("team", {}).get("id", ""))

    # Build WP rows
    wp_entries = summary.get("winprobability", [])
    if not wp_entries:
        return [], home_score, away_score, attendance, None

    # Track OT period counters for synthetic clock
    ot_period_counter = {}
    wp_rows = []

    for idx, wp in enumerate(wp_entries):
        play_id = str(wp.get("playId", ""))
        home_win_pct = wp.get("homeWinPercentage", 0.5)
        tie_pct = wp.get("tiePercentage", 0.0)

        play_info = play_map.get(play_id)

        if play_info:
            period = play_info["period"]
            clock_display = play_info["clock_display"]
            p_home_score = play_info["home_score"]
            p_away_score = play_info["away_score"]

            if period is not None and period <= 4:
                secs_remaining = _parse_clock(clock_display) if clock_display else None
                if secs_remaining is not None:
                    elapsed = (period - 1) * 900 + (900 - secs_remaining)
                else:
                    elapsed = None
            elif period is not None and period > 4:
                ot_count = ot_period_counter.get(period, 0)
                elapsed = 3600 + (period - 5) * 100 + ot_count
                ot_period_counter[period] = ot_count + 1
            else:
                elapsed = None
        else:
            period = None
            clock_display = None
            p_home_score = None
            p_away_score = None
            elapsed = 0 if idx == 0 else None

        wp_rows.append({
            "game_id": game_id,
            "play_id": play_id,
            "sequence_number": idx,
            "home_win_pct": home_win_pct,
            "tie_pct": tie_pct,
            "clock_seconds_elapsed": elapsed,
            "period_number": period,
            "clock_display": clock_display,
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "home_score": p_home_score,
            "away_score": p_away_score,
        })

    initial_home_wp = wp_entries[0].get("homeWinPercentage") if wp_entries else None
    return wp_rows, home_score, away_score, attendance, initial_home_wp
