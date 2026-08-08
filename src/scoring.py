import sqlite3

from . import db

# --- Normalization caps (tunable) ---
MAX_VOLATILITY = 8.0
MAX_LEAD_CHANGES = 14
MAX_TEAM_PROFILE = 1.5

# --- "Close game" WP band ---
CLOSE_LOWER = 0.30
CLOSE_UPPER = 0.70

# --- Rank tiers for team_profile (AP-style 1-25 rank) ---
RANK_TIER_TOP5 = 1.0
RANK_TIER_TOP10 = 0.7
RANK_TIER_TOP25 = 0.4


# --- Metric functions ---

def wp_volatility(wp_rows):
    """Sum of absolute WP deltas across the game."""
    wps = [r["home_win_pct"] for r in wp_rows]
    return sum(abs(wps[i + 1] - wps[i]) for i in range(len(wps) - 1))


def lead_changes(wp_rows):
    """Count of times the score state changes: a team takes the lead, or the
    score returns to a tie. States are home-leading / away-leading / tied,
    tracked over the actual score.

    ESPN's per-play score fields occasionally glitch (negative values, or a
    single stale row around a scoring play reverting on the next row).
    Football scores only increase, so any value that's negative or below the
    running max is discarded in favor of the last valid score. The initial
    pregame 0-0 tie is not counted (no prior state to change from).
    """
    count = 0
    last_state = None  # +1 = home leading, -1 = away leading, 0 = tied
    home_score, away_score = 0, 0
    for r in wp_rows:
        home, away = r["home_score"], r["away_score"]
        if home is not None and home >= home_score:
            home_score = home
        if away is not None and away >= away_score:
            away_score = away
        if home_score > away_score:
            current = 1
        elif away_score > home_score:
            current = -1
        else:
            current = 0
        if last_state is not None and current != last_state:
            count += 1
        last_state = current
    return count


def time_spent_close(wp_rows):
    """Proportion of entries where WP is in the close-game band."""
    if not wp_rows:
        return 0.0
    close = sum(1 for r in wp_rows if CLOSE_LOWER <= r["home_win_pct"] <= CLOSE_UPPER)
    return close / len(wp_rows)


def _rank_tier(rank):
    """Map an AP-style 1-25 rank to a profile score. Unranked (None) = 0."""
    if rank is None:
        return 0.0
    if rank <= 5:
        return RANK_TIER_TOP5
    if rank <= 10:
        return RANK_TIER_TOP10
    return RANK_TIER_TOP25


def team_profile(home_rank, away_rank):
    """
    Sum of both teams' rank-tier scores (capped at MAX_TEAM_PROFILE).

    Summing (rather than averaging) means a single highly-ranked team gives a
    real bump even against an unranked opponent, while two ranked teams
    together can still reach the cap — a marquee matchup between two good
    teams outscores a lone ranked team's game, but isn't required to unlock
    meaningful credit.
    """
    return _rank_tier(home_rank) + _rank_tier(away_rank)


# --- Metric registry ---
# Each fn takes a context dict: {"wp_rows": [...], "home_rank": int|None, "away_rank": int|None}

METRICS = [
    {"name": "wp_volatility",    "fn": lambda ctx: wp_volatility(ctx["wp_rows"]),                    "weight": 1.0, "cap": MAX_VOLATILITY},
    {"name": "lead_changes",     "fn": lambda ctx: lead_changes(ctx["wp_rows"]),                      "weight": 1.0, "cap": MAX_LEAD_CHANGES},
    {"name": "time_spent_close", "fn": lambda ctx: time_spent_close(ctx["wp_rows"]),                  "weight": 1.0, "cap": None},
    {"name": "team_profile",     "fn": lambda ctx: team_profile(ctx["home_rank"], ctx["away_rank"]),  "weight": 1.0, "cap": MAX_TEAM_PROFILE},
]


# --- Scoring ---

def score_game(context):
    """
    Compute composite watchability score for a single game.

    context: {"wp_rows": [...], "home_rank": int|None, "away_rank": int|None}
    Returns (composite: float, breakdown: dict).
    breakdown keys: metric name → {raw, normalized, weighted}.
    """
    breakdown = {}
    total_weight = sum(m["weight"] for m in METRICS)
    composite = 0.0

    for m in METRICS:
        raw = m["fn"](context)
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
            f"SELECT g.game_id, g.away_team_abbr, g.home_team_abbr, g.home_rank, g.away_rank "
            f"FROM games g "
            f"WHERE g.completed = 1 AND g.detail_fetched = 1 "
            f"AND g.game_id IN ({placeholders})"
        )
        params = list(game_ids)
    else:
        base = (
            "SELECT g.game_id, g.away_team_abbr, g.home_team_abbr, g.home_rank, g.away_rank "
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
            "SELECT home_win_pct, home_score, away_score FROM win_probability WHERE game_id = ? ORDER BY play_sequence, id",
            (game_id,),
        ).fetchall()

        if not wp_rows:
            print(f"[{i}/{n}] {label} — no WP data, skipping.")
            continue

        context = {"wp_rows": wp_rows, "home_rank": row["home_rank"], "away_rank": row["away_rank"]}
        composite, breakdown = score_game(context)
        db.update_watchability_score(conn, game_id, composite)
        db.upsert_game_metrics(conn, game_id, breakdown)

        parts = "  ".join(
            f"{name}: {v['raw']:.3f}→{v['normalized']:.3f}"
            for name, v in breakdown.items()
        )
        print(f"[{i}/{n}] {label}  score={composite:.4f}  [{parts}]")

    conn.commit()
    print(f"Scoring complete: {n} games scored.")
