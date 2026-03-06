import sqlite3

from . import db

# --- Normalization caps (tunable) ---
MAX_VOLATILITY = 5.0
MAX_LEAD_CHANGES = 10

# --- "Close game" WP band ---
CLOSE_LOWER = 0.30
CLOSE_UPPER = 0.70


# --- Metric functions ---

def wp_volatility(wp_rows):
    """Sum of absolute WP deltas across the game."""
    wps = [r["home_win_pct"] for r in wp_rows]
    return sum(abs(wps[i + 1] - wps[i]) for i in range(len(wps) - 1))


def lead_changes(wp_rows):
    """Count of times home_win_pct strictly crosses 0.50."""
    wps = [r["home_win_pct"] for r in wp_rows]
    count = 0
    for i in range(len(wps) - 1):
        if (wps[i] < 0.50 and wps[i + 1] > 0.50) or (wps[i] > 0.50 and wps[i + 1] < 0.50):
            count += 1
    return count


def time_spent_close(wp_rows):
    """Proportion of entries where WP is in the close-game band."""
    if not wp_rows:
        return 0.0
    close = sum(1 for r in wp_rows if CLOSE_LOWER <= r["home_win_pct"] <= CLOSE_UPPER)
    return close / len(wp_rows)


# --- Metric registry ---

METRICS = [
    {"name": "wp_volatility",    "fn": wp_volatility,    "weight": 1.0, "cap": MAX_VOLATILITY},
    {"name": "lead_changes",     "fn": lead_changes,     "weight": 1.0, "cap": MAX_LEAD_CHANGES},
    {"name": "time_spent_close", "fn": time_spent_close, "weight": 1.0, "cap": None},
]


# --- Scoring ---

def score_game(wp_rows):
    """
    Compute composite watchability score for a single game.

    Returns (composite: float, breakdown: dict).
    breakdown keys: metric name → {raw, normalized, weighted}.
    """
    breakdown = {}
    total_weight = sum(m["weight"] for m in METRICS)
    composite = 0.0

    for m in METRICS:
        raw = m["fn"](wp_rows)
        if m["cap"] is None:
            normalized = min(raw, 1.0)
        else:
            normalized = min(raw / m["cap"], 1.0)
        weighted = normalized * m["weight"]
        breakdown[m["name"]] = {"raw": raw, "normalized": normalized, "weighted": weighted}
        composite += weighted

    composite /= total_weight
    return composite, breakdown


def score_games(conn, game_ids=None, rescore=False):
    """
    Phase 3: score all eligible completed games and write watchability_score to DB.

    Eligible = completed=1 AND detail_fetched=1 AND watchability_score IS NULL
    (unless rescore=True, which drops the NULL check).
    """
    if game_ids:
        placeholders = ",".join("?" * len(game_ids))
        base = (
            f"SELECT g.game_id, g.away_team_abbr, g.home_team_abbr "
            f"FROM games g "
            f"WHERE g.completed = 1 AND g.detail_fetched = 1 "
            f"AND g.game_id IN ({placeholders})"
        )
        params = list(game_ids)
    else:
        base = (
            "SELECT g.game_id, g.away_team_abbr, g.home_team_abbr "
            "FROM games g "
            "WHERE g.completed = 1 AND g.detail_fetched = 1"
        )
        params = []

    if not rescore:
        base += " AND g.watchability_score IS NULL"

    rows = conn.execute(base, params).fetchall()
    n = len(rows)

    if n == 0:
        print("No games to score.")
        return

    for i, row in enumerate(rows, 1):
        game_id = row["game_id"]
        label = f"{row['away_team_abbr']} @ {row['home_team_abbr']}"

        wp_rows = conn.execute(
            "SELECT home_win_pct FROM win_probability WHERE game_id = ? ORDER BY id",
            (game_id,),
        ).fetchall()

        if not wp_rows:
            print(f"[{i}/{n}] {label} — no WP data, skipping.")
            continue

        composite, breakdown = score_game(wp_rows)
        db.update_watchability_score(conn, game_id, composite)

        parts = "  ".join(
            f"{name}: {v['raw']:.3f}→{v['normalized']:.3f}"
            for name, v in breakdown.items()
        )
        print(f"[{i}/{n}] {label}  score={composite:.4f}  [{parts}]")

    conn.commit()
    print(f"Scoring complete: {n} games scored.")
