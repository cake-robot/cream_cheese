DB_PATH = "data/cfb.db"
DEFAULT_SEASON = 2025
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
RATE_LIMIT_SECONDS = 1.0

FOX_BASE = "https://api.foxsports.com/bifrost/v1/cfb"
FOX_APIKEY = "jE7yBJVRNAwdDesMgTzTXUUSx1It41Fq"
FOX_RATE_LIMIT_SECONDS = 2.0  # double ESPN's -- each call pulls the full ~200KB payload
FOX_SEASON_ANCHORS = {2025: 41258, 2024: 39500}  # known (event_id -> approx date) seed for the ID walk
FOX_SCAN_OVERRUN = 25  # consecutive out-of-window/missing IDs before the walk stops
