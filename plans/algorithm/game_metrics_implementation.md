# Add `game_metrics` Table for Per-Metric Observability

## Context

The scoring algorithm (Phase 3) currently computes a composite `watchability_score` and writes only that single number to the `games` table. To iterate on the algorithm — tuning weights, caps, and adding/removing metrics — we need to see each metric's raw and normalized value per game, stored persistently so we can query/compare without re-running scoring.

A narrow `game_metrics` table fits the existing metric registry pattern: one row per (game, metric). Adding a new metric to the registry automatically produces rows for it on the next scoring run — no schema migration needed.

---

## Changes

### 1. `src/db.py` — Add table DDL + upsert function

**Schema** (append to `SCHEMA` string):
```sql
CREATE TABLE IF NOT EXISTS game_metrics (
    game_id     TEXT NOT NULL REFERENCES games(game_id),
    metric_name TEXT NOT NULL,
    raw_value   REAL NOT NULL,
    norm_value  REAL NOT NULL,
    PRIMARY KEY (game_id, metric_name)
);
```

**New function**:
```python
def upsert_game_metrics(conn, game_id, breakdown):
    """Write per-metric breakdown rows. breakdown: {metric_name: {raw, normalized, weighted}}."""
    conn.executemany("""
        INSERT INTO game_metrics (game_id, metric_name, raw_value, norm_value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(game_id, metric_name) DO UPDATE SET
            raw_value  = excluded.raw_value,
            norm_value = excluded.norm_value
    """, [
        (game_id, name, vals["raw"], vals["normalized"])
        for name, vals in breakdown.items()
    ])
```

### 2. `src/scoring.py` — Persist breakdown alongside composite

In `score_games()`, after the existing `db.update_watchability_score()` call, add:
```python
db.upsert_game_metrics(conn, game_id, breakdown)
```

No other changes to scoring logic.

### 3. `pipeline.py` — No changes needed

The scoring entry points already call `score_games()`, which will now write metrics automatically.

---

## Verification

1. `python3 pipeline.py --score-only --rescore` — rescores all games, populating `game_metrics`
2. Query per-metric values:
   ```sql
   SELECT g.away_team_abbr, g.home_team_abbr, gm.metric_name, gm.raw_value, gm.norm_value
   FROM game_metrics gm
   JOIN games g ON g.game_id = gm.game_id
   ORDER BY g.watchability_score DESC
   LIMIT 30;
   ```
3. Verify row count: should be `(number of scored games) × 3` rows in `game_metrics`
4. Re-run `--score-only --rescore` — idempotent (upsert overwrites same values)

## Step 0: Write this plan to ALGORITHM.md and commit before implementing
