DB_PATH = "data/cfb.db"
DEFAULT_SEASON = 2026
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
RATE_LIMIT_SECONDS = 1.0

FOX_BASE = "https://api.foxsports.com/bifrost/v1/cfb"
FOX_APIKEY = "jE7yBJVRNAwdDesMgTzTXUUSx1It41Fq"
FOX_RATE_LIMIT_SECONDS = 2.0  # double ESPN's -- each call pulls the full ~200KB payload
# known (event_id -> approx date) seed for the ID walk. No 2026 entry yet --
# last year's step (41258 - 39500 = 1758) is a rough extrapolation, not a
# verified anchor, since 2026 events don't exist until the season starts.
# Pass --fox-anchor explicitly for the first --fox-pull of 2026 (find a real
# event ID from foxsports.com and confirm it resolves before trusting it as
# a seed), then add the verified id here.
FOX_SEASON_ANCHORS = {2025: 41258, 2024: 39500}
FOX_SCAN_OVERRUN = 25  # consecutive out-of-window/missing IDs before the walk stops
