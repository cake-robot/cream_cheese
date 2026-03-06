# CFB Data Pipeline — Implementation Plan

## Context
Building a local data pipeline for a College Football watchability web app. Collect game metadata + win probability series from the ESPN API, store in SQLite. A watchability scoring algorithm will connect later.

Scope: CFB only, 2025 season default (configurable via `--season`), regular + postseason, SQLite storage. First test case: Oregon Ducks 2025 season (`--team 2483 --season 2025`).

---

## File Structure

Four new files. No frameworks, no ORMs. Standard library + `requests`.

```
cream_cheese/
    config.py       # constants: DB path, default season year (2025), API URL base, rate limit
    db.py           # schema DDL, get_connection(), upsert helpers
    espn.py         # ESPN API fetch + parse functions
    pipeline.py     # orchestration + argparse CLI (main entry point)
    cfb.db          # created on first run (add to .gitignore)
```

---

## Implementation Order

### Step 1: `config.py`
Constants only:
- `DB_PATH = "cfb.db"`
- `DEFAULT_SEASON = 2025`
- `ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"`
- `RATE_LIMIT_SECONDS = 1.0`

### Step 2: `db.py`
- `init_db()` — runs CREATE TABLE IF NOT EXISTS for both tables + indexes + updated_at trigger
- `get_connection()` — returns sqlite3 connection with WAL mode, foreign keys ON
- `upsert_game(conn, game_dict)` — INSERT OR REPLACE, preserving `watchability_score` via a read-modify-write or COALESCE pattern
- `upsert_win_probability(conn, rows)` — bulk INSERT OR IGNORE for WP rows
- `mark_detail_fetched(conn, game_id, scores, attendance, initial_wp)` — UPDATE games SET detail_fetched=1 + scores + attendance + initial_home_wp

**`updated_at` trigger:**
```sql
CREATE TRIGGER IF NOT EXISTS trg_games_updated_at
AFTER UPDATE ON games
BEGIN
    UPDATE games SET updated_at = datetime('now') WHERE game_id = NEW.game_id;
END;
```

### Step 3: `espn.py`
Each function returns parsed Python dicts/lists, no DB logic.

- `fetch_json(url)` — GET with rate limiting (1s sleep), returns dict or raises
- `fetch_scoreboard(season, week, season_type=2)` — returns list of game dicts from scoreboard
- `fetch_team_schedule(team_id, season)` — returns list of game dicts from team schedule endpoint
- `fetch_teams_list()` — returns list of {id, name, abbreviation} for --find-team (limit=400 to cover FBS + FCS)
- `fetch_game_summary(game_id)` — returns full summary dict
- `parse_scoreboard_game(event)` — extract game metadata from a scoreboard event
- `parse_team_schedule_game(event)` — extract game metadata from team schedule response (different structure)
- `parse_summary_detail(summary)` — extract WP rows, scores, attendance from summary; build play_id→clock map from drives

**Clock derivation logic (in `parse_summary_detail`):**
1. Build `play_id → {period, clock_display, home_score, away_score}` map from `drives.previous[].plays[]`
2. For each WP entry, look up its `playId` in the map
3. Regulation: `elapsed = (period - 1) * 900 + (900 - parse_clock(clock_display))`
4. Pre-game (first WP entry, sequence 0): `elapsed = 0`
5. **OT plays** (period > 4): no game clock in CFB OT. Assign `clock_seconds_elapsed = 3600 + (period - 4) * 100 + sequence_within_period`. This gives synthetic but monotonically increasing values that keep OT plays ordered after regulation. `clock_display` still stored as-is (likely "0:00" or absent).
6. Unmatched play_ids (WP entry with no corresponding play in drives): set `clock_seconds_elapsed = NULL`, still store the WP row

### Step 4: `pipeline.py`
Orchestration + argparse CLI.

**argparse flags:**
- `--season YEAR` (default: 2025)
- `--week N` (specific week, regular season)
- `--team TEAM_ID` (use team schedule endpoint)
- `--game GAME_ID` (single game — skip discovery)
- `--discover-only` (Phase 1 only, no detail fetch)
- `--detail-only` (Phase 2 only, skip discovery)
- `--find-team NAME` (search + print, exit)

**Phase 1 — Discovery:**
- Default: scoreboard for weeks 0–15 (season_type=2) + postseason (season_type=3)
- `--week N`: single scoreboard call
- `--team ID`: team schedule endpoint (single call, ~13–15 games)
- `--game ID`: skip discovery; ensure game row exists by extracting metadata from summary response in Phase 2 (`--game` must also populate `games` table if row doesn't exist)
- `--find-team`: fetch teams list, fuzzy match on name/abbreviation, print results, exit

**Phase 2 — Detail fetch:**
- Query: `SELECT game_id FROM games WHERE completed = 1 AND detail_fetched = 0` (scoped to discovered game_ids if filter was applied)
- For each game_id:
  - `fetch_game_summary(game_id)`
  - `parse_summary_detail(summary)` → WP rows + scores + attendance + initial_wp
  - If no WP data returned: log warning, still mark `detail_fetched = 1` to avoid infinite retries
  - Single transaction: upsert WP rows + update game row
- Print progress: `"Fetching detail for game {i}/{n}: {away} @ {home}..."`

**Idempotent**: `detail_fetched` flag prevents re-fetching. Safe to re-run.

---

## SQLite Schema

### `games` table
```sql
CREATE TABLE IF NOT EXISTS games (
    game_id             TEXT PRIMARY KEY,
    season_year         INTEGER NOT NULL,
    season_type         INTEGER NOT NULL,   -- 2=regular, 3=postseason
    week                INTEGER,            -- NULL for postseason
    game_date           TEXT NOT NULL,      -- ISO8601 UTC

    home_team_id        TEXT NOT NULL,
    home_team_abbr      TEXT NOT NULL,
    home_team_name      TEXT NOT NULL,
    home_rank           INTEGER,            -- NULL if unranked

    away_team_id        TEXT NOT NULL,
    away_team_abbr      TEXT NOT NULL,
    away_team_name      TEXT NOT NULL,
    away_rank           INTEGER,

    conference_game     INTEGER NOT NULL DEFAULT 0,
    neutral_site        INTEGER NOT NULL DEFAULT 0,
    venue_name          TEXT,

    status_state        TEXT NOT NULL,      -- "pre", "in", "post"
    completed           INTEGER NOT NULL DEFAULT 0,

    home_score          INTEGER,
    away_score          INTEGER,
    attendance          INTEGER,

    initial_home_wp     REAL,

    detail_fetched      INTEGER NOT NULL DEFAULT 0,
    detail_fetched_at   TEXT,

    watchability_score  REAL,               -- NULL until algorithm runs

    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_games_season ON games(season_year, season_type, week);
CREATE INDEX IF NOT EXISTS idx_games_detail ON games(completed, detail_fetched);

CREATE TRIGGER IF NOT EXISTS trg_games_updated_at
AFTER UPDATE ON games
BEGIN
    UPDATE games SET updated_at = datetime('now') WHERE game_id = NEW.game_id;
END;
```

### `win_probability` table
```sql
CREATE TABLE IF NOT EXISTS win_probability (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                 TEXT NOT NULL,
    play_id                 TEXT NOT NULL,
    sequence_number         INTEGER,

    home_win_pct            REAL NOT NULL,
    tie_pct                 REAL NOT NULL DEFAULT 0.0,

    clock_seconds_elapsed   INTEGER,         -- 0–3600 regulation; synthetic >3600 for OT; NULL if unknown
    period_number           INTEGER,
    clock_display           TEXT,

    home_score              INTEGER,
    away_score              INTEGER,

    FOREIGN KEY (game_id) REFERENCES games(game_id),
    UNIQUE (game_id, play_id)
);

CREATE INDEX IF NOT EXISTS idx_wp_game_seq ON win_probability(game_id, sequence_number);
```

---

## ESPN API Endpoints

| Endpoint | URL | Use |
|----------|-----|-----|
| Scoreboard | `{BASE}/scoreboard?limit=100&week={week}&dates={year}&seasontype={type}` | Game discovery by week |
| Team schedule | `{BASE}/teams/{team_id}/schedule?season={year}` | Game discovery by team |
| Teams list | `{BASE}/teams?limit=400` | `--find-team` lookup (covers FBS + FCS) |
| Summary | `{BASE}/summary?event={game_id}` | Full game detail + WP |

Rate limit: 1 second between requests.

---

## Key Implementation Notes

- **Rank handling**: Check `competitor.get("curatedRank", {}).get("current")` and also `competitor.get("rank")`. Store NULL if rank > 25 or absent.
- **`watchability_score` preservation**: ON CONFLICT clause uses COALESCE to keep existing non-NULL value.
- **OT clock**: CFB OT is untimed. Assign synthetic elapsed values (3600 + offset) to keep ordering. Do NOT assume 15-minute OT periods.
- **`--game` bootstrap**: If game_id has no row in `games`, extract metadata from the summary response before inserting WP data.
- **Missing WP data**: Log warning, mark `detail_fetched = 1` anyway. Some games (canceled, suspended) won't have WP.
- **`updated_at`**: Handled by SQLite trigger, not in application code.

---

## Verification (Oregon Ducks 2025 test case)

1. **Find team ID**: `python pipeline.py --find-team "Oregon"` → should print team ID `2483`
2. **Discover Oregon 2025**: `python pipeline.py --team 2483 --season 2025 --discover-only`
   - Check: `sqlite3 cfb.db "SELECT COUNT(*) FROM games WHERE season_year=2025;"` → ~13–15 rows
   - Check: `sqlite3 cfb.db "SELECT away_team_abbr, home_team_abbr, game_date FROM games ORDER BY game_date;"` → Oregon's schedule
3. **Fetch detail**: `python pipeline.py --team 2483 --season 2025`
   - Check: `sqlite3 cfb.db "SELECT game_id, home_score, away_score, initial_home_wp FROM games WHERE detail_fetched=1;"` → scores + WP populated
   - Check: `sqlite3 cfb.db "SELECT COUNT(*) FROM win_probability;"` → ~150 rows per completed game
4. **Idempotency**: re-run same command → "0 games need detail fetching"
5. **Single game**: pick a game_id from step 2, run `python pipeline.py --game {id}` → should be a no-op (already fetched)
