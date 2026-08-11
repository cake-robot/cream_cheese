import re
import unicodedata

from . import db, fox


def _normalize_school(name):
    """
    Strict school-name normalization for ESPN<->Fox matching: strip
    diacritics, punctuation, collapse whitespace, uppercase. Deliberately
    does NOT touch "State"/"St" -- blanket-stripping that reopens the exact
    hole strict equality was built to close: Ohio and Ohio State are both
    real, distinct teams, so treating them as equal would silently conflate
    them the same way naive token overlap conflated Virginia and Virginia
    Tech (see FOX_SCHOOL_ALIASES below for how the real St/State spelling
    divergences that remain are handled instead -- one reviewed pair at a
    time, not a generic rule).
    """
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip().upper()


# Explicit {espn_school: fox_school} pairs for teams whose bare school names
# genuinely diverge in a way generic normalization can't safely resolve.
# Built via a full manual batch-review of all 184 week-1-2025 teams against
# already-pulled Fox data (see plans/algorithm -- 166/184 resolved via
# strict normalized equality alone; these 18 needed a human-verified pair).
# Each entry reviewed once, not guessed by a heuristic -- same pattern as
# src/corrections.py.
FOX_SCHOOL_ALIASES = {
    "App State": "Appalachian State",
    "Arkansas-Pine Bluff": "Ark-Pine Bluff",
    "Central Connecticut": "Central Connecticut State",
    "Charleston Southern": "Charleston So",
    "Coastal Carolina": "C.Carolina",
    "Florida International": "FIU",
    "Georgia Southern": "GA Southern",
    "Jacksonville State": "Jacksonville St",
    "Long Island University": "LIU",
    "Massachusetts": "UMass",
    "Miami": "Miami (FL)",
    "Mississippi Valley State": "Miss Valley State",
    "NC State": "North Carolina St",
    "Nicholls": "Nicholls State",
    "Saint Francis": "St. Francis (PA)",
    "Southeast Missouri State": "SE Missouri State",
    "UAlbany": "University at Albany",
    "UConn": "Connecticut",
    "UT Martin": "Tennessee-Martin",
}
_ALIAS_NORM = {_normalize_school(k): _normalize_school(v) for k, v in FOX_SCHOOL_ALIASES.items()}


def sync_team_crosswalk(conn, season=None, week=None, season_type=2):
    """
    1. Seed the ESPN side of team_crosswalk for every distinct team_id
       appearing in `games` (scoped to season/week/season_type if given,
       else all teams) -- never clobbers an already-resolved fox_* match.
    2. Auto-match every crosswalk row with fox_team_id IS NULL against
       fox_teams (harvested as a byproduct of every Fox header parse) via
       _normalize_school() equality, direct or through FOX_SCHOOL_ALIASES.
    Returns (seeded, matched) counts.
    """
    if season is not None and week is not None:
        rows = conn.execute("""
            SELECT DISTINCT t.team_id, t.abbreviation, t.school, t.name
            FROM teams t
            JOIN games g ON t.team_id IN (g.home_team_id, g.away_team_id)
            WHERE g.season_year = ? AND g.week = ? AND g.season_type = ?
        """, (season, week, season_type)).fetchall()
    else:
        rows = conn.execute("SELECT team_id, abbreviation, school, name FROM teams").fetchall()

    for r in rows:
        db.seed_team_crosswalk(conn, r["team_id"], r["abbreviation"], r["school"], r["name"])
    conn.commit()

    fox_by_norm = {}
    for r in conn.execute("SELECT fox_team_id, fox_school_name FROM fox_teams"):
        fox_by_norm.setdefault(_normalize_school(r["fox_school_name"]), r["fox_team_id"])

    unmatched = conn.execute(
        "SELECT espn_team_id, espn_school FROM team_crosswalk WHERE fox_team_id IS NULL"
    ).fetchall()

    matched = 0
    for r in unmatched:
        key = _normalize_school(r["espn_school"])
        fox_team_id = fox_by_norm.get(key)
        method = "school_name"
        if fox_team_id is None and key in _ALIAS_NORM:
            fox_team_id = fox_by_norm.get(_ALIAS_NORM[key])
            method = "alias"
        if fox_team_id is not None:
            db.set_team_crosswalk_match(conn, r["espn_team_id"], fox_team_id, method)
            matched += 1
    conn.commit()

    return len(rows), matched


def unmatched_teams(conn):
    """Crosswalk rows still missing fox_team_id -- the human worklist."""
    rows = conn.execute("""
        SELECT espn_team_id, espn_abbr, espn_school, espn_name
        FROM team_crosswalk WHERE fox_team_id IS NULL
        ORDER BY espn_school
    """).fetchall()
    return [
        {
            "espn_team_id": r["espn_team_id"],
            "espn_school": r["espn_school"],
            "suggested_query": f'site:foxsports.com "{r["espn_school"]}" college-football boxscore',
        }
        for r in rows
    ]


def record_manual_team_match(conn, espn_team_id, fox_team_id):
    """
    Human found the right fox_team_id (read off a boxscore URL's event id,
    the way VT@SC's 42834 was found this session, then cross-referenced
    against that event's header). There's no endpoint to fetch a Fox team
    directly -- team data only exists as a byproduct of fetching an event
    that features it -- so this refuses rather than fetching anything
    itself: pull the event first via `--fox-event`, which harvests both
    teams into fox_teams automatically, then this just records the match.
    """
    row = conn.execute(
        "SELECT 1 FROM fox_teams WHERE fox_team_id = ?", (fox_team_id,)
    ).fetchone()
    if not row:
        raise ValueError(
            f"Fox team {fox_team_id} not in fox_teams yet -- pull an event featuring "
            f"it first (e.g. `--fox-event <id>`), which harvests team data as a "
            f"byproduct, then retry."
        )

    espn_row = conn.execute(
        "SELECT espn_team_id FROM team_crosswalk WHERE espn_team_id = ?", (espn_team_id,)
    ).fetchone()
    if not espn_row:
        raise ValueError(f"ESPN team {espn_team_id} not in team_crosswalk -- run sync_team_crosswalk first.")

    db.set_team_crosswalk_match(conn, espn_team_id, fox_team_id, "manual")
    conn.commit()


def match_game(conn, game_id):
    """
    Exact-match a games row to a fox_events row via team_crosswalk's
    fox_team_id on both sides + date within +-1 day. No string comparison
    at this layer at all. Returns the matched fox_event_id, or None.

    Checks both home/away orderings, not just the one ESPN uses: a neutral-
    site game has no true home team, and ESPN and Fox don't always agree on
    which side gets the label -- confirmed on 3 separate 2024 bowls/showcase
    games (USC@LSU at Allegiant Stadium, the Myrtle Beach Bowl, the Las
    Vegas Bowl), all with the designation flipped between the two sources
    despite being the same two teams on the same date. lead_changes/
    clutch_finish are computed from Fox's own ladder and don't reference
    ESPN's home/away labels at all, so a flipped match doesn't affect them --
    only ties the game_id to the right fox_event_id.
    """
    game = conn.execute(
        "SELECT game_id, home_team_id, away_team_id, game_date FROM games WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    if not game:
        return None

    ids = conn.execute("""
        SELECT
            (SELECT fox_team_id FROM team_crosswalk WHERE espn_team_id = ?) AS home_fox_id,
            (SELECT fox_team_id FROM team_crosswalk WHERE espn_team_id = ?) AS away_fox_id
    """, (game["home_team_id"], game["away_team_id"])).fetchone()
    if ids["home_fox_id"] is None or ids["away_fox_id"] is None:
        return None

    game_date = game["game_date"][:10]
    candidate = conn.execute("""
        SELECT fox_event_id, pbp_fetched FROM fox_events
        WHERE ((home_fox_team_id = ? AND away_fox_team_id = ?)
            OR (home_fox_team_id = ? AND away_fox_team_id = ?))
          AND event_date IS NOT NULL
          AND date(event_date) BETWEEN date(?, '-1 day') AND date(?, '+1 day')
        LIMIT 1
    """, (
        ids["home_fox_id"], ids["away_fox_id"],
        ids["away_fox_id"], ids["home_fox_id"],
        game_date, game_date,
    )).fetchone()
    if not candidate:
        return None

    fox_event_id = candidate["fox_event_id"]
    if not candidate["pbp_fetched"]:
        # A match can land on a header-only row -- e.g. a primary-pool
        # neighbor caught only by the boundary overrun walk, never in-window
        # itself. A match should always come with usable data, so fetch it
        # now rather than leaving a matched game with no score sequence.
        payload = fox.fetch_event(fox_event_id)
        if payload is not None:
            plays = fox.parse_pbp_plays(payload)
            db.upsert_fox_plays(conn, fox_event_id, plays)
            db.replace_fox_score_sequence(conn, fox_event_id, fox.build_score_sequence(plays))
            db.mark_fox_pbp_fetched(conn, fox_event_id)

    db.upsert_fox_game(conn, game_id, fox_event_id)
    return fox_event_id


def match_all_games(conn, season=None, week=None, season_type=2):
    """match_game() over every games row in scope. Returns (attempted, matched)."""
    if season is not None and week is not None:
        rows = conn.execute(
            "SELECT game_id FROM games WHERE season_year = ? AND week = ? AND season_type = ?",
            (season, week, season_type),
        ).fetchall()
    else:
        rows = conn.execute("SELECT game_id FROM games").fetchall()

    matched = 0
    for r in rows:
        if match_game(conn, r["game_id"]):
            matched += 1
    conn.commit()
    return len(rows), matched
