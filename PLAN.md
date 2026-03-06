# CFB Data Pipeline Plan

## Context
Building a local data pipeline for a College Football watchability web app. The goal is to collect a "slate" of games from the ESPN API — with win probability series and game metadata — stored in a queryable database so a watchability scoring algorithm can be connected later.

Scope: CFB only, 2025 season default (regular + postseason), SQLite storage. Season is configurable via CLI.

---

## File Structure

Four new files alongside existing `test.py` / notebook. No frameworks, no ORMs.

```
cream_cheese/
    config.py       # constants: DB path, default season year (2025), API URLs, rate limit
    db.py           # schema DDL, get_connection(), upsert helpers
    espn.py         # ESPN API fetch + parse functions
    pipeline.py     # orchestration: discover games → fetch detail (main entry point)
    cfb.db          # created on first run (add to .gitignore)
```

---

## SQLite Schema

### `games` table
One row per game. Populated in two phases: basic metadata from scoreboard, detail from summary.

```sql
CREATE TABLE IF NOT EXISTS games (
    game_id             TEXT PRIMARY KEY,   -- ESPN event id, e.g. "401628483"
    season_year         INTEGER NOT NULL,
    season_type         INTEGER NOT NULL,   -- 2=regular, 3=postseason
    week                INTEGER,            -- NULL for postseason
    game_date           TEXT NOT NULL,      -- ISO8601 UTC

    home_team_id        TEXT NOT NULL,
    home_team_abbr      TEXT NOT NULL,
    home_team_name      TEXT NOT NULL,
    home_rank           INTEGER,            -- NULL if unranked (>25 stored as NULL)

    away_team_id        TEXT NOT NULL,
    away_team_abbr      TEXT NOT NULL,
    away_team_name      TEXT NOT NULL,
    away_rank           INTEGER,

    conference_game     INTEGER NOT NULL DEFAULT 0,  -- 0/1 boolean
    neutral_site        INTEGER NOT NULL DEFAULT 0,
    venue_name          TEXT,

    status_state        TEXT NOT NULL,      -- "pre", "in", "post"
    completed           INTEGER NOT NULL DEFAULT 0,

    home_score          INTEGER,
    away_score          INTEGER,
    attendance          INTEGER,

    initial_home_wp     REAL,               -- first WP entry = pre-game expectation

    detail_fetched      INTEGER NOT NULL DEFAULT 0,
    detail_fetched_at   TEXT,

    watchability_score  REAL,               -- NULL until algorithm runs

    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_games_season   ON games(season_year, season_type, week);
CREATE INDEX IF NOT EXISTS idx_games_detail   ON games(completed, detail_fetched);
```

### `win_probability` table
One row per play-by-play WP entry. `clock_seconds_elapsed` is derived by joining WP playIds against the drives/plays data (since `secondsLeft` is always 0 in completed game responses).

```sql
CREATE TABLE IF NOT EXISTS win_probability (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                 TEXT NOT NULL,
    play_id                 TEXT NOT NULL,
    sequence_number         INTEGER,         -- positional order within game

    home_win_pct            REAL NOT NULL,
    tie_pct                 REAL NOT NULL DEFAULT 0.0,

    -- Derived from play-by-play: ((period-1) * 900) + (900 - clock_remaining_seconds)
    -- First WP entry (pre-game) gets elapsed = 0 by convention
    clock_seconds_elapsed   INTEGER,         -- 0–3600 regulation; >3600 = OT
    period_number           INTEGER,
    clock_display           TEXT,            -- raw "MM:SS" from ESPN

    home_score              INTEGER,
    away_score              INTEGER,

    FOREIGN KEY (game_id) REFERENCES games(game_id),
    UNIQUE (game_id, play_id)
);

CREATE INDEX IF NOT EXISTS idx_wp_game_seq ON win_probability(game_id, sequence_number);
```

---

## ESPN API

**Scoreboard** (game discovery by week):
`https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?limit=100&week={week}&dates={year}&seasontype=2`

Postseason (no week param):
`https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?limit=100&dates={year}&seasontype=3`

**Team schedule** (game discovery by team — more surgical):
`https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams/{team_id}/schedule?season={year}`
Returns only that team's games for the season. Oregon Ducks = team ID `2483`.

**Team search** (look up a team ID by name):
`https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams?limit=200`
Used by `--find-team` to resolve a name/abbreviation to an ID.

**Summary** (full game detail):
`https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={game_id}`

Rate limit: 1 second between requests. Full season run ≈ 15–20 minutes (~950 games).
Targeted pulls (single team, week, or game) complete in seconds to a few minutes.

---

## CLI Usage

`pipeline.py` uses `argparse`. All flags are optional; default is full 2025 season.

```
# Full season (all weeks + postseason) — use carefully
python pipeline.py

# Specify a season year (default: 2025)
python pipeline.py --season 2024

# Single week — safest for targeted pulls
python pipeline.py --week 3

# All games for one team (uses team schedule endpoint — much lighter)
python pipeline.py --team 2483

# Single game — minimal API usage
python pipeline.py --game 401628483

# Discovery only (no detail/WP fetch) — just populates games table metadata
python pipeline.py --discover-only

# Detail fetch only (skips discovery, pulls WP for already-discovered games)
python pipeline.py --detail-only

# Look up a team's ESPN ID by name or abbreviation
python pipeline.py --find-team "Oregon Ducks"
python pipeline.py --find-team ORE
```

Flag combinations work naturally: `--team 2483 --season 2024 --discover-only` populates Oregon's 2024 schedule without fetching any WP data. `--week 3 --detail-only` fetches WP for already-discovered week 3 games.

---

## Pipeline Logic

### Discovery sources (Phase 1)

**Default / `--week N`**: hits the scoreboard endpoint for specified week(s), upserts all games found.

**`--team TEAM_ID`**: hits the team schedule endpoint (`/teams/{id}/schedule`) — returns only that team's games, roughly 13–15 entries vs. ~70 per scoreboard week. Dramatically fewer API calls. Use this for testing.

**`--game GAME_ID`**: skips discovery entirely, goes straight to Phase 2 for that one game.

**`--find-team NAME`**: hits the teams list endpoint, prints matching teams with their IDs, exits. No DB writes.

From each discovered event, extract: game_id, date, teams (id/abbr/name/rank), conference_game, neutral_site, venue, status, completed. Upsert into `games` (ON CONFLICT preserves `watchability_score` and `detail_fetched`).

### Detail fetch (Phase 2)

Unless `--discover-only`, after discovery run:
- Query: `WHERE completed = 1 AND detail_fetched = 0` (scoped to the discovered game_ids if a filter was applied)
- For each: fetch summary → parse WP + scores + attendance
- **Clock derivation**: build `play_id → {period, clock_display, home_score, away_score}` map from `drives.previous[].plays[]`, then join against `winprobability[]` entries
- Elapsed formula: `(period - 1) * 900 + (900 - clock_remaining_seconds)`; first WP entry gets `elapsed = 0`
- Upsert `win_probability` rows, update `games` with scores/attendance/`initial_home_wp`/`detail_fetched = 1`
- Both upserts inside one transaction per game

**Idempotent**: safe to re-run at any time. Already-fetched games are skipped via `detail_fetched` flag.

---

## Key Implementation Notes

- **Rank handling**: scoreboard `competitors` may expose rank at `competitor["rank"]` directly (not nested). Store NULL if rank > 25 or absent.
- **`watchability_score` preservation**: ON CONFLICT clause omits this column from updates so computed scores survive re-runs.
- **`detail_fetched` flag**: set atomically with WP insert in one transaction — faster than querying the WP table each time.
- **OT**: clock formula works for OT since CFB OT quarters are also 15 min; values > 3600 naturally.

---

## Verification

Start surgical (low API exposure), expand from there:

1. **Find a team ID**: `python pipeline.py --find-team "Oregon Ducks"` → should print `2483`
2. **Single game**: `python pipeline.py --game 401628483` → ~2 API calls, fast
   - Check: `sqlite3 cfb.db "SELECT * FROM games WHERE game_id='401628483';"` — `initial_home_wp ≈ 0.313`, scores 49-14
   - Check: `sqlite3 cfb.db "SELECT COUNT(*) FROM win_probability WHERE game_id='401628483';"` → 158 rows
3. **Team pull (2024)**: `python pipeline.py --team 2483 --season 2024` → ~14 discovery calls + ~14 detail calls for Oregon's 2024 season
4. **Idempotency**: re-run `python pipeline.py --team 2483 --season 2024` → "0 games need detail fetching", finishes in seconds
5. **Week pull**: `python pipeline.py --week 3` → ~1 scoreboard call + detail for all week 3 games not yet fetched
6. **Full season** (only when confident): `python pipeline.py` → ~950 games, 15–20 min (uses default 2025)
