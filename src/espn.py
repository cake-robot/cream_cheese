import time
import requests
from .config import ESPN_BASE, RATE_LIMIT_SECONDS

_last_request_time = 0.0


def fetch_json(url):
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    resp = requests.get(url, timeout=15)
    _last_request_time = time.time()
    resp.raise_for_status()
    return resp.json()


def fetch_scoreboard(season, week=None, season_type=2):
    url = f"{ESPN_BASE}/scoreboard?limit=100&dates={season}&seasontype={season_type}"
    if week is not None:
        url += f"&week={week}"
    data = fetch_json(url)
    events = data.get("events", [])
    return [parse_scoreboard_game(e) for e in events if e.get("competitions")]


def fetch_team_schedule(team_id, season):
    url = f"{ESPN_BASE}/teams/{team_id}/schedule?season={season}"
    data = fetch_json(url)
    events = data.get("events", [])
    return [parse_team_schedule_game(e) for e in events if e.get("competitions")]


def fetch_teams_list():
    teams = []
    page = 1
    while True:
        url = f"{ESPN_BASE}/teams?limit=100&page={page}"
        data = fetch_json(url)
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
    return fetch_json(url)


def _get_rank(competitor):
    curated = competitor.get("curatedRank")
    if curated:
        rank = curated.get("current")
    else:
        rank = competitor.get("rank")
    if rank and isinstance(rank, int) and rank <= 25:
        return rank
    return None


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
        "conference_game": int(bool(comp.get("conferenceCompetition", False))),
        "neutral_site": int(bool(comp.get("neutralSite", False))),
        "venue_name": venue_name,
        "status_state": status.get("state", "pre"),
        "completed": int(bool(status.get("completed", False))),
        "home_score": None,
        "away_score": None,
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

    return {
        "game_id": str(header.get("id", "")),
        "season_year": season.get("year"),
        "season_type": season.get("type", 2),
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
        "conference_game": int(bool(comp.get("conferenceCompetition", False))),
        "neutral_site": int(bool(comp.get("neutralSite", False))),
        "venue_name": venue_name,
        "status_state": status.get("state", "post"),
        "completed": int(bool(status.get("completed", True))),
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
    for drive in drives.get("previous", []):
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
