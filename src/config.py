DB_PATH = "data/cfb.db"
DEFAULT_SEASON = 2026
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
RATE_LIMIT_SECONDS = 1.0

# ESPN's FBS conference group ids (the `groups=80` set), id -> display name.
# Verified live against the scoreboard/core APIs 2026-09-03: ids and names
# are stable across 2022-2026 -- only conference *membership* moves between
# seasons. No conferences table needed; this is a fixed set.
FBS_CONFERENCES = {
    1: "ACC",
    4: "Big 12",
    5: "Big Ten",
    8: "SEC",
    9: "Pac-12",
    12: "Conference USA",
    15: "MAC",
    17: "Mountain West",
    18: "FBS Independents",
    37: "Sun Belt",
    151: "American",
}

# Notre Dame sits in FBS Independents (18) alongside UConn/UMass, so "Power 4
# + Notre Dame" has to key off the team id, not the conference id. Same
# hardcoded-team-id pattern as UW_TEAM_ID in src/scoring.py.
NOTRE_DAME_TEAM_ID = "87"

# Power-conference ids, season-aware: the Pac-12 (9) was a power conference
# through the 2023 season; the 2023-07/2024 realignment wave (Pac-12
# collapse, Texas/Oklahoma to SEC, USC/UCLA/Oregon/Washington to Big Ten,
# etc. -- see conference_realignment_history memory) took effect for the
# 2024 season, after which "power" is just the current four. The 2026
# relaunched Pac-12 (Boise St, CSU, Fresno, SDSU, Texas St, Utah St, plus
# Oregon St/Washington St) is a different, non-power league reusing the same
# conference id -- it correctly falls out of this set from 2024 on.
_POWER_5_IDS = frozenset({1, 4, 5, 8, 9})
_POWER_4_IDS = frozenset({1, 4, 5, 8})


def power_conference_ids(season_year):
    """FBS conference ids considered 'power' for the given season."""
    return _POWER_5_IDS if season_year is not None and season_year <= 2023 else _POWER_4_IDS

FOX_BASE = "https://api.foxsports.com/bifrost/v1/cfb"
FOX_APIKEY = "jE7yBJVRNAwdDesMgTzTXUUSx1It41Fq"
FOX_RATE_LIMIT_SECONDS = 2.0  # double ESPN's -- each call pulls the full ~200KB payload
# known (event_id -> approx date) seed for the ID walk. No 2026 entry yet --
# last year's step (41258 - 39500 = 1758) is a rough extrapolation, not a
# verified anchor, since 2026 events don't exist until the season starts.
# Pass --fox-anchor explicitly for the first --fox-pull of 2026 (find a real
# event ID from foxsports.com and confirm it resolves before trusting it as
# a seed), then add the verified id here.
FOX_SEASON_ANCHORS = {2025: 41258, 2024: 39500, 2023: 38022, 2022: 36349}  # 36349 verified 2022-09-03 (from a foxsports.com boxscore URL)
FOX_SCAN_OVERRUN = 25  # consecutive out-of-window/missing IDs before the walk stops
