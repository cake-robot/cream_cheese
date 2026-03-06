# Watchability Scoring — POC Implementation Plan

## Context

We have 933 games with 165K win probability rows loaded for the 2025-26 CFB season. Now building Phase 3 of the pipeline: a watchability scoring algorithm. Starting with 3 metrics equally weighted as a POC, architected so adding/tweaking metrics and weights is trivial.

---

## Architecture: Metric Registry Pattern

A list of `{name, fn, weight, cap}` dicts at module level. Adding a metric = write one function + add one dict entry. Changing weights = edit one number. The composite scorer iterates the registry automatically.

Weights are relative (all `1.0` for POC). Composite = `sum(normalized * weight) / sum(weights)`. When a 4th metric is added later at weight `1.0`, the other three auto-adjust — no need to recalculate anything.

**Normalization**: Fixed caps, not min-max or percentile (those shift when new games are added). Each metric is normalized to 0–1 via `min(raw / cap, 1.0)`. Caps are tunable constants. `time_spent_close` is already 0–1 so no cap needed.

---

## Files to Change

### 1. New: `src/scoring.py`

All scoring logic in one module:

**Constants** (top of file):
- `MAX_VOLATILITY = 5.0` — normalization cap (tunable)
- `MAX_LEAD_CHANGES = 10` — normalization cap (tunable)
- `CLOSE_LOWER = 0.30` / `CLOSE_UPPER = 0.70` — "close game" WP band

**3 metric functions** (each takes `wp_rows: list[sqlite3.Row]`, returns `float`):
- `wp_volatility(wp_rows)` — `Σ |WP[i+1] - WP[i]|`
- `lead_changes(wp_rows)` — count of crossings of 0.50 (strictly above→below or below→above)
- `time_spent_close(wp_rows)` — proportion of entries where WP ∈ [0.30, 0.70]

**Registry**:
```python
METRICS = [
    {"name": "wp_volatility",    "fn": wp_volatility,    "weight": 1.0, "cap": MAX_VOLATILITY},
    {"name": "lead_changes",     "fn": lead_changes,     "weight": 1.0, "cap": MAX_LEAD_CHANGES},
    {"name": "time_spent_close", "fn": time_spent_close, "weight": 1.0, "cap": None},
]
```

**`score_game(wp_rows)`** — returns `(composite_float, breakdown_dict)`. Breakdown includes raw/normalized/weighted per metric for debugging output.

**`score_games(conn, game_ids=None, rescore=False)`** — batch scorer (Phase 3). Queries `WHERE completed=1 AND detail_fetched=1 AND watchability_score IS NULL` (or drops the NULL check if `rescore=True`). Prints per-game progress with breakdown. Single `conn.commit()` at end.

### 2. Edit: `src/db.py`

Add one function:
```python
def update_watchability_score(conn, game_id, score):
    conn.execute("UPDATE games SET watchability_score = ? WHERE game_id = ?", (score, game_id))
```

### 3. Edit: `pipeline.py`

- Add import: `from src import scoring`
- Add 3 CLI flags: `--score-only`, `--skip-scoring`, `--rescore`
- Add Phase 3 calls:
  - `--score-only`: run `scoring.score_games()` and exit
  - `--game` path: add scoring after detail fetch
  - Default path: add scoring after Phase 2 (unless `--discover-only` or `--skip-scoring`)

---

## Verification

1. **Score all loaded games**: `python3 pipeline.py --score-only`
   - Should process 933 games, print per-game breakdowns
2. **Check scores**: `sqlite3 data/cfb.db "SELECT away_team_abbr, home_team_abbr, watchability_score FROM games ORDER BY watchability_score DESC LIMIT 10;"` — top 10 most watchable games
3. **Idempotency**: re-run `--score-only` → "No games to score."
4. **Rescore**: `python3 pipeline.py --score-only --rescore` → re-processes all 933
5. **Sanity check**: known exciting games (close finishes, OT) should rank higher than known blowouts
