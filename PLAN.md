# CFB Data Pipeline — Implementation Plan

## Status: Phase 1 complete ✓

Core pipeline is built and verified against the Oregon Ducks 2025 season (15 games discovered, all 15 detail-fetched including 3 CFP playoff games).

---

## Context

Building a local data pipeline for a College Football watchability web app. Collect game metadata + win probability series from the ESPN API, store in SQLite. A watchability scoring algorithm will connect later.

Scope: CFB only, 2025 season default (configurable via `--season`), regular + postseason, SQLite storage.

---

## File Structure

```
cream_cheese/
    pipeline.py         # orchestration + argparse CLI (main entry point)
    src/
        __init__.py
        config.py       # constants: DB_PATH, DEFAULT_SEASON=2025, ESPN_BASE, RATE_LIMIT_SECONDS
        db.py           # schema DDL, get_connection(), upsert helpers
        espn.py         # ESPN API fetch + parse functions
    data/
        cfb.db          # SQLite database (gitignored)
```

---

## CLI Usage

```bash
python pipeline.py --find-team "Oregon"               # search teams, print ID
python pipeline.py --seed-teams                       # populate teams table (~754 teams)
python pipeline.py --team 2483 --season 2025 --discover-only
python pipeline.py --team 2483 --season 2025          # discover + fetch detail
python pipeline.py --week 1 --season 2025             # single week
python pipeline.py --game 401752804                   # single game
python pipeline.py --detail-only                      # fetch detail for all unfetched completed games
```

---

## SQLite Schema

### `teams` table (dimension)
```sql
CREATE TABLE IF NOT EXISTS teams (
    team_id      TEXT PRIMARY KEY,
    abbreviation TEXT NOT NULL,
    name         TEXT NOT NULL,
    school       TEXT,           -- institution name (e.g. "Oregon", "Alabama")
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### `games` table (fact)
```sql
CREATE TABLE IF NOT EXISTS games (
    game_id             TEXT PRIMARY KEY,
    season_year         INTEGER NOT NULL,
    season_type         INTEGER NOT NULL,   -- 2=regular, 3=postseason
    week                INTEGER,            -- NULL for postseason
    game_date           TEXT NOT NULL,      -- ISO8601 UTC

    home_team_id        TEXT NOT NULL REFERENCES teams(team_id),
    home_team_abbr      TEXT NOT NULL,      -- denormalized for convenience
    home_team_name      TEXT NOT NULL,
    home_rank           INTEGER,            -- NULL if unranked

    away_team_id        TEXT NOT NULL REFERENCES teams(team_id),
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
```

### `win_probability` table (fact)
```sql
CREATE TABLE IF NOT EXISTS win_probability (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                 TEXT NOT NULL REFERENCES games(game_id),
    play_id                 TEXT NOT NULL,
    sequence_number         INTEGER,

    home_win_pct            REAL NOT NULL,
    tie_pct                 REAL NOT NULL DEFAULT 0.0,

    clock_seconds_elapsed   INTEGER,   -- 0–3600 regulation; synthetic >3600 for OT; NULL if unknown
    period_number           INTEGER,
    clock_display           TEXT,

    home_team_id            TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id            TEXT NOT NULL REFERENCES teams(team_id),

    home_score              INTEGER,
    away_score              INTEGER,

    UNIQUE (game_id, play_id)
);
```

---

## ESPN API Endpoints

| Endpoint | URL | Use |
|----------|-----|-----|
| Scoreboard | `{BASE}/scoreboard?limit=100&week={week}&dates={year}&seasontype={type}` | Game discovery by week |
| Team schedule | `{BASE}/teams/{team_id}/schedule?season={year}` | Regular season by team |
| Teams list | `{BASE}/teams?limit=100&page={n}` | Paginated; ~8 pages, ~754 teams |
| Summary | `{BASE}/summary?event={game_id}` | Full game detail + WP |

Rate limit: 1 second between requests.

---

## Key Implementation Notes

- **Teams pagination**: `limit=400` misses teams with IDs > ~2248 (including Oregon ID 2483). Must paginate with `limit=100&page=N`.
- **Postseason discovery**: Team schedule endpoint only returns `seasonType=2` (regular season). Postseason requires `fetch_scoreboard(season, season_type=3)` filtered to the target team. `seasontype=3&dates={season}` uses season year, so it captures Jan games of the next year correctly.
- **Rank handling**: Check `competitor.curatedRank.current`, fall back to `competitor.rank`. Store NULL if rank > 25 or absent.
- **`watchability_score` preservation**: ON CONFLICT uses COALESCE to keep existing non-NULL value.
- **OT clock**: CFB OT is untimed. Assign synthetic elapsed = `3600 + (period - 5) * 100 + within_period_counter`. Keeps OT plays ordered after regulation.
- **`--game` bootstrap**: If game_id not in `games`, extract metadata from summary header before inserting WP data.
- **Missing WP data**: Log warning, mark `detail_fetched = 1` anyway to avoid infinite retries.
- **`updated_at`**: Handled by SQLite trigger, not application code.
- **`school` field**: ESPN's `location` field on teams (e.g. "Oregon", "Alabama") — renamed to `school`. Preserved via COALESCE so game-derived upserts don't overwrite seed data.

---

## Next Steps

- [ ] Watchability scoring algorithm (consumes `win_probability` series per game, writes `watchability_score` back to `games`)
- [ ] Full season data load (all teams, all weeks)
- [ ] Web app / API layer to serve ranked game list
