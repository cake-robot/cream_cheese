"""
Local read-only web UI for the CFB watchability pipeline.

Serves a small JSON API (under /api) plus the static files in web/. Opens
data/cfb.db strictly read-only (mode=ro) so this process can never mutate
pipeline data -- the pipeline (pipeline.py) remains the only writer.

Run from anywhere:
    ./venv/bin/python serve.py
"""

import pathlib
import sqlite3
import sys

from flask import Flask, abort, g, jsonify, request, send_from_directory

REPO_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from src import config, corrections as corrections_module, scoring  # noqa: E402

DB_FILE = (REPO_ROOT / config.DB_PATH).resolve()
WEB_DIR = REPO_ROOT / "web"

TOTAL_WEIGHT = sum(m["weight"] for m in scoring.METRICS)
MANUAL_CORRECTION_GAME_IDS = {c["game_id"] for c in corrections_module.CORRECTIONS}

BIN_WIDTH = 0.035
N_BINS = 20

# ---------------------------------------------------------------------------
# Metric prose -- kept separate from scoring.py (which owns weights/caps/math)
# so this file never invents a number, only labels/describes ones scoring.py
# already computed. The assertion below fails fast if a metric is added to
# scoring.py without updating this table.
# ---------------------------------------------------------------------------

METRIC_COPY = {
    "wp_volatility": {
        "label": "WP volatility",
        "description": (
            "Sum of absolute win-probability swings across every play -- the total "
            "distance the game's outcome probability traveled, regardless of when."
        ),
    },
    "lead_changes": {
        "label": "Lead changes",
        "description": (
            "Number of times the score state changes between home-leading, "
            "away-leading, and tied."
        ),
    },
    "time_spent_close": {
        "label": "Time spent close",
        "description": (
            "Share of plays where home win probability sat between 30% and 70%."
        ),
    },
    "team_profile": {
        "label": "Team profile",
        "description": (
            "Credit for ranked teams playing -- sum of both teams' AP-style rank tiers."
        ),
    },
    "upset_risk": {
        "label": "Upset risk",
        "description": (
            "How lopsided the pregame win probability was, scaled by the "
            "better-ranked team's tier. Measures pregame skew only -- it does not "
            "know who actually won."
        ),
    },
    "late_volatility": {
        "label": "Late volatility",
        "description": (
            "Same as WP volatility, but counted only in the 4th quarter and "
            "overtime -- rewards drama that shows up late rather than spread "
            "evenly across the game."
        ),
    },
    "clutch_finish": {
        "label": "Clutch finish",
        "description": (
            "Credit for a team taking the lead in the final minute of regulation "
            "-- breaking a tie or overcoming a deficit -- or tying the game and "
            "that tie holding into overtime. Worth more if it isn't a field "
            "goal. A score that pads an already-held lead doesn't count, and "
            "neither does a tie that gets broken again before regulation ends. "
            "Every game that reaches overtime gets at least a 0.7 floor even "
            "with no such swing in the final minute."
        ),
    },
}

assert set(METRIC_COPY) == set(scoring.METRICS_BY_NAME), (
    "METRIC_COPY is out of sync with scoring.METRICS -- add/remove an entry "
    "to match every registered metric"
)

NOT_IMPLEMENTED = [
    {
        "name": "comeback_magnitude",
        "note": (
            "Largest win-probability deficit overcome by the eventual winner. "
            "Explicitly requested in the user's personal notes ('doesn't need "
            "consummation' -- a near-comeback should count too), but has no "
            "metric function or game_metrics rows today."
        ),
    },
    {
        "name": "final_margin",
        "note": "Absolute point differential. Not implemented.",
    },
    {
        "name": "scoring_volume",
        "note": "Combined final score. Not implemented.",
    },
    {
        "name": "pregame_uncertainty",
        "note": (
            "1 - 2*|initial_home_wp - 0.5|. Not stored as its own metric -- "
            "upset_risk uses the inverse of this same skew term, scaled by rank "
            "quality, but pregame_uncertainty itself does not exist."
        ),
    },
    {
        "name": "upset_confirmed",
        "note": (
            "Whether the pregame underdog actually won. upset_risk measures "
            "pregame skew only, not the outcome -- this is a genuinely "
            "different, unimplemented idea."
        ),
    },
    {
        "name": "ot_bonus",
        "note": (
            "A dedicated overtime bonus/flag as its own scored metric. OT only "
            "shows up implicitly today, via late_volatility's period window and "
            "clutch_finish's n/a."
        ),
    },
    {
        "name": "turnovers_big_plays",
        "note": (
            "No turnover or play-type data exists anywhere in the schema -- "
            "would require a new data source."
        ),
    },
]

RANKED_CTE_SQL = """
    ranked AS (
        SELECT game_id,
               RANK() OVER (ORDER BY watchability_score DESC) AS rnk,
               COUNT(*) OVER () AS n_scored
        FROM games WHERE watchability_score IS NOT NULL
    )
"""

FOX_FLAG_CTE_SQL = "fox_flag AS (SELECT DISTINCT game_id FROM fox_score_corrections WHERE tier = 'diff')"

OT_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM win_probability wp "
    "WHERE wp.game_id = g.game_id AND wp.period_number > 4)"
)

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------------------
# Connection handling -- one read-only connection per request, never write-mode.
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        if not DB_FILE.exists():
            abort(500, description=f"database not found at {DB_FILE}")
        try:
            conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            abort(503, description="the pipeline appears to be mid-write; retry in a moment")
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


def _startup_selfcheck():
    if not DB_FILE.exists():
        raise SystemExit(f"FATAL: database not found at {DB_FILE}")
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE __ro_probe__ (x)")
        conn.close()
        raise SystemExit("FATAL: database opened writable -- refusing to start")
    except sqlite3.OperationalError:
        pass
    sqlite_version = conn.execute("SELECT sqlite_version() AS v").fetchone()["v"]
    scored = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE watchability_score IS NOT NULL"
    ).fetchone()["n"]
    conn.close()
    print(
        f"[serve.py] read-only OK -- sqlite {sqlite_version}, {scored} scored games, "
        f"{len(scoring.METRICS)} metrics, total_weight={TOTAL_WEIGHT}"
    )


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def percentile_from_rank(rank, n):
    """'Better than X% of games'. Rank 1 of 1828 -> 99, not 100 (matches the
    plan's convention: the top game outranks n-1 of n peers, and we report a
    floor so the median of an even corpus reads as a clean 50)."""
    if rank is None or not n:
        return None
    return int((n - rank) * 100 // n)


def build_metrics_map(metric_rows):
    """Registry-driven map: every metric name is always a key, in registry
    order. A metric with no game_metrics row (not applicable) maps to None --
    never to a zero. A metric with a real raw value of 0 maps to a normal
    dict with raw=0.0, distinguishable from None at every layer above this."""
    by_name = {r["metric_name"]: r for r in metric_rows}
    out = {}
    for m in scoring.METRICS:
        name = m["name"]
        row = by_name.get(name)
        if row is None:
            out[name] = None
            continue
        raw = row["raw_value"]
        norm = row["norm_value"]
        out[name] = {
            "raw": raw,
            "norm": norm,
            "weighted": norm * m["weight"],
            "at_cap": norm >= 1.0,
            "applicable": True,
        }
    return out


def applicable_weight_of(metrics_map):
    total = 0.0
    for m in scoring.METRICS:
        if metrics_map.get(m["name"]) is not None:
            total += m["weight"]
    return total


def fetch_metrics_maps(conn, game_ids):
    """Batch-fetch game_metrics for many games at once and return
    {game_id: metrics_map}. Callers must only pass ids of SCORED games --
    an unscored game's metrics map is always {} (never scored), handled by
    the caller, not by an empty result from this function."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"SELECT game_id, metric_name, raw_value, norm_value FROM game_metrics "
        f"WHERE game_id IN ({placeholders})",
        game_ids,
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["game_id"], []).append(r)
    return {gid: build_metrics_map(grouped.get(gid, [])) for gid in game_ids}


def fetch_fox_diff_game_ids(conn, game_ids):
    """Which of these games have a real Fox-derived value substitution
    (tier='diff') -- deliberately excludes tier='unusable' rows, which
    mean reconciliation was attempted and inconclusive, not that any value
    was actually corrected from Fox."""
    if not game_ids:
        return set()
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"SELECT DISTINCT game_id FROM fox_score_corrections "
        f"WHERE tier = 'diff' AND game_id IN ({placeholders})",
        game_ids,
    ).fetchall()
    return {r["game_id"] for r in rows}


def histogram(scores, bin_width=BIN_WIDTH, n_bins=N_BINS):
    bins = [0] * n_bins
    for s in scores:
        idx = int(s // bin_width)
        idx = max(0, min(idx, n_bins - 1))
        bins[idx] += 1
    return {"bin_width": bin_width, "bins": bins}


def shape_game(row, metrics_map, rank=None, n_scored=None, has_fox_correction=False, has_manual_correction=False):
    """The canonical game JSON shape shared by every endpoint that lists or
    embeds a game. `metrics_map` must be {} for an unscored game (never a
    registry of nulls) -- that distinction is the caller's responsibility.

    has_fox_correction and has_manual_correction are deliberately separate:
    a game can have either, both, or neither, and they mean different
    things (an actual Fox-derived value substitution vs. a hand-verified
    override) -- collapsing them into one flag makes the UI claim "FOX"
    on games that were only ever fixed by hand (see corrections.py)."""
    scored = row["watchability_score"] is not None
    pct = percentile_from_rank(rank, n_scored) if scored else None
    is_ot = row["is_ot"] if "is_ot" in row.keys() else None
    return {
        "game_id": row["game_id"],
        "season_year": row["season_year"],
        "season_type": row["season_type"],
        "week": row["week"],
        "event_note": row["event_note"],
        "game_date": row["game_date"],
        "away": {
            "abbr": row["away_team_abbr"],
            "name": row["away_team_name"],
            "rank": row["away_rank"],
            "score": row["away_score"],
        },
        "home": {
            "abbr": row["home_team_abbr"],
            "name": row["home_team_name"],
            "rank": row["home_rank"],
            "score": row["home_score"],
        },
        "venue_name": row["venue_name"],
        "attendance": row["attendance"],
        "conference_game": bool(row["conference_game"]),
        "neutral_site": bool(row["neutral_site"]),
        "completed": bool(row["completed"]),
        "status_state": row["status_state"],
        "ot": (bool(is_ot) if is_ot is not None else None),
        "initial_home_wp": row["initial_home_wp"],
        "watchability_score": row["watchability_score"],
        "rank": rank if scored else None,
        "percentile": pct,
        "n_scored": n_scored if scored else None,
        "has_fox_correction": bool(has_fox_correction),
        "has_manual_correction": bool(has_manual_correction),
        "metrics": metrics_map if scored else {},
        "applicable_weight": (applicable_weight_of(metrics_map) if scored else None),
    }


def build_misalignment_callout(rows):
    """One or two generated sentences naming whichever metric most
    over-delivers vs. its designed weight, and whichever most under-delivers.
    Never hardcoded to a specific metric name -- if weights/caps are retuned,
    this text follows automatically."""
    if not rows:
        return None
    over = max(rows, key=lambda r: r["delta"])
    under = min(rows, key=lambda r: r["delta"])
    if over["name"] == under["name"]:
        return None
    return (
        f"{over['label']} carries weight {over['weight']} "
        f"({over['designed_share'] * 100:.1f}% of the designed total) but delivers "
        f"{over['delivered_share'] * 100:.1f}% of the mean score -- more than its "
        f"weight alone would suggest. {under['label']}, at weight {under['weight']} "
        f"({under['designed_share'] * 100:.1f}% designed), delivers only "
        f"{under['delivered_share'] * 100:.1f}%."
    )


def build_wp_payload(wp_rows, game_row):
    """Parallel-array WP series for the game-detail chart. See serve.py's
    module docstring notes / the design plan for why: play-ordinal x-axis
    (elapsed is non-monotonic in 356 games), whole-series period carry
    forward (not just row 0), and a sanitized non-decreasing score ladder
    mirroring scoring.lead_changes()'s own sanitization so the chart can
    never visually disagree with the stored metric."""
    n = len(wp_rows)
    home_win_pct = [r["home_win_pct"] for r in wp_rows]
    period_raw = [r["period_number"] for r in wp_rows]
    clock_display = [r["clock_display"] for r in wp_rows]
    elapsed = [r["clock_seconds_elapsed"] for r in wp_rows]
    home_score_raw = [r["home_score"] for r in wp_rows]
    away_score_raw = [r["away_score"] for r in wp_rows]

    period_filled = []
    last = 0
    for p in period_raw:
        if p is not None:
            last = p
        period_filled.append(last)

    home_clean, away_clean = [], []
    h, a = 0, 0
    for rh, ra in zip(home_score_raw, away_score_raw):
        if rh is not None and rh >= h:
            h = rh
        if ra is not None and ra >= a:
            a = ra
        home_clean.append(h)
        away_clean.append(a)

    def period_label(p):
        if p == 0:
            return "Pregame"
        if p <= 4:
            return f"Q{p}"
        return f"OT{p - 4}"

    period_starts = []
    prev = None
    for idx, p in enumerate(period_filled):
        if p != prev:
            period_starts.append({"i": idx, "period": p, "label": period_label(p)})
            prev = p

    score_changes = []
    ph, pa = 0, 0
    for idx, (hs, as_) in enumerate(zip(home_clean, away_clean)):
        if hs != ph or as_ != pa:
            if hs != ph:
                delta, team = hs - ph, "home"
            else:
                delta, team = as_ - pa, "away"
            score_changes.append({"i": idx, "home": hs, "away": as_, "delta": delta, "team": team})
            ph, pa = hs, as_

    elapsed_monotonic = True
    prev_e = None
    for e in elapsed:
        if e is None:
            continue
        if prev_e is not None and e < prev_e:
            elapsed_monotonic = False
            break
        prev_e = e

    return {
        "n": n,
        "i": list(range(n)),
        "home_win_pct": home_win_pct,
        "period": period_raw,
        "period_filled": period_filled,
        "clock_display": clock_display,
        "elapsed": elapsed,
        "home_score": home_score_raw,
        "away_score": away_score_raw,
        "home_score_clean": home_clean,
        "away_score_clean": away_clean,
        "meta": {
            "period_starts": period_starts,
            "score_changes": score_changes,
            "final": {"home": game_row["home_score"], "away": game_row["away_score"]},
            "elapsed_monotonic": elapsed_monotonic,
            "wp_final": home_win_pct[-1] if home_win_pct else None,
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.errorhandler(400)
def handle_400(e):
    return jsonify({"error": "bad_request", "detail": e.description}), 400


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "not_found", "detail": e.description}), 404


@app.errorhandler(503)
def handle_503(e):
    return jsonify({"error": "service_unavailable", "detail": e.description}), 503


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "internal_error", "detail": str(e)}), 500


# ---- health / meta / registry --------------------------------------------

@app.route("/api/healthz")
def api_healthz():
    conn = get_db()
    ro_ok = False
    try:
        conn.execute("CREATE TABLE __ro_probe__ (x)")
    except sqlite3.OperationalError:
        ro_ok = True
    scored = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE watchability_score IS NOT NULL"
    ).fetchone()["n"]
    sqlite_version = conn.execute("SELECT sqlite_version() AS v").fetchone()["v"]
    return jsonify({
        "status": "ok",
        "read_only_verified": ro_ok,
        "sqlite_version": sqlite_version,
        "scored": scored,
        "metrics_registered": len(scoring.METRICS),
        "total_weight": TOTAL_WEIGHT,
        "db_path": str(DB_FILE),
    })


@app.route("/api/meta")
def api_meta():
    conn = get_db()
    season_type_rows = conn.execute(
        "SELECT DISTINCT season_year, season_type FROM games ORDER BY season_year, season_type"
    ).fetchall()
    seasons = sorted({r["season_year"] for r in season_type_rows})
    season_types = {}
    for r in season_type_rows:
        season_types.setdefault(str(r["season_year"]), []).append(r["season_type"])

    week_rows = conn.execute(
        "SELECT DISTINCT season_year, season_type, week FROM games "
        "WHERE week IS NOT NULL ORDER BY season_year, season_type, week"
    ).fetchall()
    weeks = {}
    for r in week_rows:
        key = f"{r['season_year']}:{r['season_type']}"
        weeks.setdefault(key, []).append(r["week"])

    team_rows = conn.execute("""
        SELECT abbr, MAX(name) AS name FROM (
            SELECT home_team_abbr AS abbr, home_team_name AS name FROM games
            UNION ALL
            SELECT away_team_abbr, away_team_name FROM games
        )
        GROUP BY abbr ORDER BY name
    """).fetchall()

    return jsonify({
        "seasons": seasons,
        "season_types": season_types,
        "weeks": weeks,
        "teams": [{"abbr": r["abbr"], "name": r["name"]} for r in team_rows],
    })


@app.route("/api/metrics")
def api_metrics():
    conn = get_db()
    stat_rows = conn.execute("""
        SELECT metric_name, COUNT(*) n, AVG(raw_value) avg_raw, MAX(raw_value) max_raw,
               AVG(norm_value) avg_norm,
               SUM(CASE WHEN norm_value >= 1.0 THEN 1 ELSE 0 END) n_at_cap
        FROM game_metrics GROUP BY metric_name
    """).fetchall()
    stats_by_name = {r["metric_name"]: r for r in stat_rows}
    out = []
    for m in scoring.METRICS:
        name = m["name"]
        s = stats_by_name.get(name)
        n_metric = s["n"] if s else 0
        out.append({
            "name": name,
            "label": METRIC_COPY[name]["label"],
            "description": METRIC_COPY[name]["description"],
            "weight": m["weight"],
            "cap": m["cap"],
            "n": n_metric,
            "avg_raw": s["avg_raw"] if s else None,
            "max_raw": s["max_raw"] if s else None,
            "avg_norm": s["avg_norm"] if s else None,
            "n_at_cap": s["n_at_cap"] if s else 0,
            "pct_at_cap": (s["n_at_cap"] / n_metric * 100) if n_metric else 0.0,
        })
    return jsonify({"metrics": out, "total_weight": TOTAL_WEIGHT, "not_implemented": NOT_IMPLEMENTED})


# ---- games list / detail ---------------------------------------------------

SORT_WHITELIST = {
    "score": "g.watchability_score",
    "date": "g.game_date",
    "week": "g.week",
    "margin": "ABS(COALESCE(g.home_score,0) - COALESCE(g.away_score,0))",
}


def _csv_ints(args, name):
    vals = []
    for v in args.getlist(name):
        vals.extend(v.split(","))
    out = []
    for v in vals:
        v = v.strip()
        if not v:
            continue
        try:
            out.append(int(v))
        except ValueError:
            abort(400, description=f"invalid integer in {name}: {v}")
    return out


@app.route("/api/games")
def api_games():
    conn = get_db()
    args = request.args
    where = []
    params = []

    seasons = _csv_ints(args, "season")
    if seasons:
        where.append(f"g.season_year IN ({','.join('?' * len(seasons))})")
        params.extend(seasons)

    season_type = args.get("season_type", "all")
    if season_type not in ("all", "2", "3"):
        abort(400, description="season_type must be 2, 3, or all")
    if season_type != "all":
        where.append("g.season_type = ?")
        params.append(int(season_type))

    weeks = _csv_ints(args, "week")
    if weeks:
        where.append(f"g.week IN ({','.join('?' * len(weeks))})")
        params.extend(weeks)

    teams = [t.strip().upper() for t in args.getlist("team") if t.strip()]
    if teams:
        placeholders = ",".join("?" * len(teams))
        where.append(
            f"(UPPER(g.home_team_abbr) IN ({placeholders}) OR UPPER(g.away_team_abbr) IN ({placeholders}))"
        )
        params.extend(teams)
        params.extend(teams)

    ranked = args.get("ranked", "any")
    if ranked not in ("any", "one", "both"):
        abort(400, description="ranked must be any, one, or both")
    if ranked == "one":
        where.append("(g.home_rank IS NOT NULL OR g.away_rank IS NOT NULL)")
    elif ranked == "both":
        where.append("(g.home_rank IS NOT NULL AND g.away_rank IS NOT NULL)")

    for flag_name, col in (("conference", "g.conference_game"), ("neutral", "g.neutral_site")):
        v = args.get(flag_name, "all")
        if v not in ("0", "1", "all"):
            abort(400, description=f"{flag_name} must be 0, 1, or all")
        if v != "all":
            where.append(f"{col} = ?")
            params.append(int(v))

    ot = args.get("ot", "all")
    if ot not in ("0", "1", "all"):
        abort(400, description="ot must be 0, 1, or all")
    if ot == "1":
        where.append(OT_EXISTS_SQL)
    elif ot == "0":
        where.append(f"NOT {OT_EXISTS_SQL}")

    scored = args.get("scored", "1")
    if scored not in ("0", "1", "all"):
        abort(400, description="scored must be 0, 1, or all")
    if scored == "1":
        where.append("g.watchability_score IS NOT NULL")
    elif scored == "0":
        where.append("g.watchability_score IS NULL")

    min_score, max_score = args.get("min_score"), args.get("max_score")
    if min_score is not None:
        try:
            where.append("g.watchability_score >= ?")
            params.append(float(min_score))
        except ValueError:
            abort(400, description="min_score must be a number")
    if max_score is not None:
        try:
            where.append("g.watchability_score <= ?")
            params.append(float(max_score))
        except ValueError:
            abort(400, description="max_score must be a number")

    q = args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        where.append("(g.home_team_name LIKE ? OR g.away_team_name LIKE ? OR g.venue_name LIKE ?)")
        params.extend([like, like, like])

    sort = args.get("sort", "score")
    dir_ = args.get("dir", "desc")
    if dir_ not in ("asc", "desc"):
        abort(400, description="dir must be asc or desc")

    sort_metric_name = None
    if sort.startswith("metric:"):
        sort_metric_name = sort[len("metric:"):]
        if sort_metric_name not in scoring.METRICS_BY_NAME:
            abort(400, description=f"unknown metric: {sort_metric_name}")
        sort_expr = "sort_metric_val"
    elif sort in SORT_WHITELIST:
        sort_expr = SORT_WHITELIST[sort]
    else:
        abort(400, description=f"invalid sort: {sort}")

    try:
        limit = int(args.get("limit", 50))
        offset = int(args.get("offset", 0))
    except ValueError:
        abort(400, description="limit/offset must be integers")
    if not (1 <= limit <= 500):
        abort(400, description="limit must be between 1 and 500")
    if offset < 0:
        abort(400, description="offset must be >= 0")

    where_sql = " AND ".join(where) if where else "1=1"

    metric_select, metric_params = "", []
    if sort_metric_name:
        metric_select = (
            ", (SELECT raw_value FROM game_metrics gm WHERE gm.game_id = g.game_id "
            "AND gm.metric_name = ?) AS sort_metric_val"
        )
        metric_params = [sort_metric_name]

    total = conn.execute(f"SELECT COUNT(*) AS n FROM games g WHERE {where_sql}", params).fetchone()["n"]

    sql = f"""
        WITH {RANKED_CTE_SQL},
        {FOX_FLAG_CTE_SQL}
        SELECT g.*, r.rnk, r.n_scored, (fc.game_id IS NOT NULL) AS has_fox_correction,
               {OT_EXISTS_SQL} AS is_ot
               {metric_select}
        FROM games g
        LEFT JOIN ranked r ON r.game_id = g.game_id
        LEFT JOIN fox_flag fc ON fc.game_id = g.game_id
        WHERE {where_sql}
        ORDER BY {sort_expr} IS NULL, {sort_expr} {dir_.upper()}
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, metric_params + params + [limit, offset]).fetchall()

    scored_game_ids = [r["game_id"] for r in rows if r["watchability_score"] is not None]
    metrics_by_game = fetch_metrics_maps(conn, scored_game_ids)

    games_out = []
    for row in rows:
        gid = row["game_id"]
        m_map = metrics_by_game.get(gid, {})
        games_out.append(shape_game(
            row, m_map, rank=row["rnk"], n_scored=row["n_scored"],
            has_fox_correction=bool(row["has_fox_correction"]),
            has_manual_correction=(gid in MANUAL_CORRECTION_GAME_IDS),
        ))

    score_rows = conn.execute(
        f"SELECT watchability_score AS s FROM games g WHERE {where_sql} AND g.watchability_score IS NOT NULL",
        params,
    ).fetchall()
    scores = sorted(r["s"] for r in score_rows)
    if scores:
        n = len(scores)
        median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2
        stats = {"n": n, "min": scores[0], "median": median, "max": scores[-1], "histogram": histogram(scores)}
    else:
        stats = {"n": 0, "min": None, "median": None, "max": None, "histogram": histogram([])}

    n_scored_corpus = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE watchability_score IS NOT NULL"
    ).fetchone()["n"]

    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "dir": dir_,
        "n_scored_corpus": n_scored_corpus,
        "filtered_score_stats": stats,
        "games": games_out,
    })


@app.route("/api/games/<game_id>")
def api_game_detail(game_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if row is None:
        abort(404, description="no such game")

    is_ot = bool(conn.execute(
        "SELECT EXISTS(SELECT 1 FROM win_probability WHERE game_id=? AND period_number>4) AS x",
        (game_id,),
    ).fetchone()["x"])
    max_period = conn.execute(
        "SELECT MAX(period_number) AS mx FROM win_probability WHERE game_id=?", (game_id,)
    ).fetchone()["mx"]
    ot_info = {
        "is_ot": is_ot,
        "max_period_in_data": max_period,
        "note": (
            "Period count is derived from ESPN play data and can be truncated "
            "on long overtime games." if is_ot else None
        ),
    }

    scored = row["watchability_score"] is not None
    metrics_map, score_integrity, rank_context, wp_payload = {}, None, None, None
    neighbors = {"prev_by_rank": None, "next_by_rank": None}

    if scored:
        metric_rows = conn.execute(
            "SELECT metric_name, raw_value, norm_value FROM game_metrics WHERE game_id=?",
            (game_id,),
        ).fetchall()
        metrics_map = build_metrics_map(metric_rows)

        weighted_sum, applicable_weight, excluded = 0.0, 0.0, []
        for m in scoring.METRICS:
            v = metrics_map[m["name"]]
            if v is None:
                excluded.append(m["name"])
                continue
            weighted_sum += v["weighted"]
            applicable_weight += m["weight"]
        composite_recomputed = weighted_sum / applicable_weight if applicable_weight else None
        composite_stored = row["watchability_score"]
        delta = (composite_recomputed - composite_stored) if composite_recomputed is not None else None
        score_integrity = {
            "composite_stored": composite_stored,
            "composite_recomputed": composite_recomputed,
            "delta": delta,
            "matches": (abs(delta) < 1e-6) if delta is not None else False,
            "weighted_sum": weighted_sum,
            "applicable_weight": applicable_weight,
            "excluded_metrics": excluded,
        }

        rank_rows = conn.execute("""
            SELECT game_id,
                   RANK() OVER (ORDER BY watchability_score DESC) AS rnk_g,
                   COUNT(*) OVER () AS n_g,
                   RANK() OVER (PARTITION BY season_year ORDER BY watchability_score DESC) AS rnk_s,
                   COUNT(*) OVER (PARTITION BY season_year) AS n_s,
                   RANK() OVER (PARTITION BY season_year, season_type, week ORDER BY watchability_score DESC) AS rnk_w,
                   COUNT(*) OVER (PARTITION BY season_year, season_type, week) AS n_w
            FROM games WHERE watchability_score IS NOT NULL
        """).fetchall()
        rc = next((r for r in rank_rows if r["game_id"] == game_id), None)
        if rc:
            rank_context = {
                "global": {"rank": rc["rnk_g"], "percentile": percentile_from_rank(rc["rnk_g"], rc["n_g"]), "n": rc["n_g"]},
                "season": {"rank": rc["rnk_s"], "percentile": percentile_from_rank(rc["rnk_s"], rc["n_s"]), "n": rc["n_s"], "label": str(row["season_year"])},
                "week": {
                    "rank": rc["rnk_w"], "percentile": percentile_from_rank(rc["rnk_w"], rc["n_w"]), "n": rc["n_w"],
                    # Every postseason game is stored as week=1 (ESPN has no
                    # real week numbering once the regular season ends), so
                    # this partition is actually "all of that year's postseason"
                    # -- label it that way instead of the misleading "week 1".
                    "label": f"{row['season_year']} postseason" if row["season_type"] == 3 else f"{row['season_year']} week {row['week']}",
                },
            }
            gr = rc["rnk_g"]
            nb_rows = conn.execute(f"""
                WITH {RANKED_CTE_SQL}
                SELECT g.game_id, g.home_team_abbr, g.away_team_abbr, g.home_score, g.away_score,
                       g.watchability_score, r.rnk
                FROM games g JOIN ranked r ON r.game_id = g.game_id
                WHERE r.rnk IN (?, ?)
            """, (gr - 1, gr + 1)).fetchall()
            for nb in nb_rows:
                label = f"{nb['away_team_abbr']} {nb['away_score']} at {nb['home_team_abbr']} {nb['home_score']}"
                entry = {"game_id": nb["game_id"], "rank": nb["rnk"], "label": label, "watchability_score": nb["watchability_score"]}
                if nb["rnk"] == gr - 1:
                    neighbors["prev_by_rank"] = entry
                elif nb["rnk"] == gr + 1:
                    neighbors["next_by_rank"] = entry

        wp_rows = conn.execute(
            "SELECT play_id, sequence_number, home_win_pct, clock_seconds_elapsed, period_number, "
            "clock_display, home_score, away_score, play_sequence FROM win_probability "
            "WHERE game_id=? ORDER BY play_sequence, id",
            (game_id,),
        ).fetchall()
        wp_payload = build_wp_payload(wp_rows, row)

    manual = [c for c in corrections_module.CORRECTIONS if c["game_id"] == game_id]
    fox_rows = conn.execute(
        "SELECT tier, metric_name, espn_value, fox_value, fox_event_id, notes, reconciled_at "
        "FROM fox_score_corrections WHERE game_id=?",
        (game_id,),
    ).fetchall()
    fox_diff = [dict(r) for r in fox_rows if r["tier"] == "diff"]
    unusable_notes = [dict(r) for r in fox_rows if r["tier"] == "unusable"]
    fox_event_row = conn.execute("SELECT fox_event_id FROM fox_games WHERE game_id=?", (game_id,)).fetchone()
    corrections_payload = {
        "manual": manual,
        "fox": fox_diff,
        "unusable": len(unusable_notes) > 0,
        "unusable_notes": unusable_notes,
        "fox_event_id": fox_event_row["fox_event_id"] if fox_event_row else None,
    }

    # shape_game() does `row["is_ot"]` / `row.keys()` -- sqlite3.Row supports
    # both, but we need to inject the separately-queried is_ot flag onto it,
    # so build a plain dict (which also supports both) instead.
    row_with_ot = dict(row)
    row_with_ot["is_ot"] = is_ot
    game_shaped = shape_game(
        row_with_ot,
        metrics_map,
        rank=(rank_context["global"]["rank"] if rank_context else None),
        n_scored=(rank_context["global"]["n"] if rank_context else None),
        has_fox_correction=bool(fox_diff),
        has_manual_correction=bool(manual),
    )

    registry = [{
        "name": m["name"], "label": METRIC_COPY[m["name"]]["label"],
        "description": METRIC_COPY[m["name"]]["description"],
        "weight": m["weight"], "cap": m["cap"],
    } for m in scoring.METRICS]

    return jsonify({
        "game": game_shaped,
        "rank_context": rank_context,
        "score_integrity": score_integrity,
        "registry": registry,
        "ot": ot_info,
        "corrections": corrections_payload,
        "wp": wp_payload,
        "neighbors": neighbors,
    })


# ---- top / leaderboards -----------------------------------------------------

@app.route("/api/top")
def api_top():
    conn = get_db()
    by = request.args.get("by", "composite")
    season = request.args.get("season")
    season_type = request.args.get("season_type", "all")
    try:
        limit = int(request.args.get("limit", 25))
    except ValueError:
        abort(400, description="limit must be an integer")
    if not (1 <= limit <= 100):
        abort(400, description="limit must be between 1 and 100")

    where = ["g.watchability_score IS NOT NULL"]
    params = []
    if season:
        try:
            where.append("g.season_year = ?")
            params.append(int(season))
        except ValueError:
            abort(400, description="season must be an integer")
    if season_type != "all":
        if season_type not in ("2", "3"):
            abort(400, description="season_type must be 2, 3, or all")
        where.append("g.season_type = ?")
        params.append(int(season_type))
    where_sql = " AND ".join(where)

    if by == "composite":
        rows = conn.execute(
            f"SELECT g.*, {OT_EXISTS_SQL} AS is_ot FROM games g WHERE {where_sql} "
            f"ORDER BY g.watchability_score DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        game_ids = [r["game_id"] for r in rows]
        metrics_by_game = fetch_metrics_maps(conn, game_ids)
        fox_diff_ids = fetch_fox_diff_game_ids(conn, game_ids)
        results = []
        for i, row in enumerate(rows, start=1):
            m_map = metrics_by_game.get(row["game_id"], {})
            top2 = sorted(
                ((n, v["weighted"]) for n, v in m_map.items() if v is not None),
                key=lambda t: t[1], reverse=True,
            )[:2]
            results.append({
                "rank": i,
                "game": shape_game(
                    row, m_map,
                    has_fox_correction=(row["game_id"] in fox_diff_ids),
                    has_manual_correction=(row["game_id"] in MANUAL_CORRECTION_GAME_IDS),
                ),
                "top_contributors": [{"name": n, "label": METRIC_COPY[n]["label"], "weighted": w} for n, w in top2],
            })
        return jsonify({"by": "composite", "results": results, "cap_warning": None})

    if by not in scoring.METRICS_BY_NAME:
        abort(400, description=f"unknown metric: {by}")
    m = scoring.METRICS_BY_NAME[by]
    rows = conn.execute(f"""
        SELECT g.*, gm.raw_value AS metric_raw, gm.norm_value AS metric_norm, {OT_EXISTS_SQL} AS is_ot
        FROM games g JOIN game_metrics gm ON gm.game_id = g.game_id AND gm.metric_name = ?
        WHERE {where_sql}
        ORDER BY gm.raw_value DESC LIMIT ?
    """, [by] + params + [limit]).fetchall()
    n_at_cap = conn.execute(f"""
        SELECT COUNT(*) AS n FROM games g
        JOIN game_metrics gm ON gm.game_id = g.game_id AND gm.metric_name = ?
        WHERE {where_sql} AND gm.norm_value >= 1.0
    """, [by] + params).fetchone()["n"]
    cap_warning = None
    if m["cap"] is not None and n_at_cap > 0:
        cap_warning = (
            f"{n_at_cap} games are at the {m['cap']} cap for {METRIC_COPY[by]['label']}; "
            f"this ranking separates them by raw value, but the composite score does not."
        )
    # Every row here is scored by definition (it has a game_metrics row for
    # `by`), so it needs its real metrics map -- {} is reserved for "never
    # scored" and must never appear on a scored game (it would also make
    # applicable_weight_of({}) read as 0.0, a false "nothing applies" signal).
    game_ids = [row["game_id"] for row in rows]
    metrics_by_game = fetch_metrics_maps(conn, game_ids)
    fox_diff_ids = fetch_fox_diff_game_ids(conn, game_ids)
    results = []
    for i, row in enumerate(rows, start=1):
        m_map = metrics_by_game.get(row["game_id"], {})
        results.append({
            "rank": i,
            "game": shape_game(
                row, m_map,
                has_fox_correction=(row["game_id"] in fox_diff_ids),
                has_manual_correction=(row["game_id"] in MANUAL_CORRECTION_GAME_IDS),
            ),
            "value": row["metric_raw"],
            "normalized": row["metric_norm"],
            "at_cap": row["metric_norm"] >= 1.0,
        })
    return jsonify({"by": by, "results": results, "cap_warning": cap_warning})


@app.route("/api/top/teams")
def api_top_teams():
    conn = get_db()
    try:
        min_games = int(request.args.get("min_games", 8))
        limit = int(request.args.get("limit", 25))
    except ValueError:
        abort(400, description="min_games/limit must be integers")

    season = request.args.get("season")
    season_type = request.args.get("season_type", "all")
    where = ["watchability_score IS NOT NULL"]
    params = []
    if season:
        try:
            where.append("season_year = ?")
            params.append(int(season))
        except ValueError:
            abort(400, description="season must be an integer")
    if season_type != "all":
        if season_type not in ("2", "3"):
            abort(400, description="season_type must be 2, 3, or all")
        where.append("season_type = ?")
        params.append(int(season_type))
    where_sql = " AND ".join(where)

    appearances_sql = f"""
        SELECT home_team_abbr AS abbr, home_team_name AS name, game_id, watchability_score
        FROM games WHERE {where_sql}
        UNION ALL
        SELECT away_team_abbr, away_team_name, game_id, watchability_score
        FROM games WHERE {where_sql}
    """

    agg_rows = conn.execute(f"""
        SELECT abbr, MAX(name) AS name, COUNT(*) AS n, AVG(watchability_score) AS avg_score
        FROM ({appearances_sql})
        GROUP BY abbr HAVING COUNT(*) >= ?
        ORDER BY avg_score DESC LIMIT ?
    """, params + params + [min_games, limit]).fetchall()

    best_rows = conn.execute(f"""
        SELECT abbr, game_id, watchability_score,
               RANK() OVER (PARTITION BY abbr ORDER BY watchability_score DESC) AS rnk
        FROM ({appearances_sql})
    """, params + params).fetchall()
    best_by_abbr = {}
    for r in best_rows:
        if r["rnk"] == 1 and r["abbr"] not in best_by_abbr:
            best_by_abbr[r["abbr"]] = {"game_id": r["game_id"], "watchability_score": r["watchability_score"]}

    results = [{
        "rank": i,
        "abbr": r["abbr"],
        "name": r["name"],
        "games_played": r["n"],
        "avg_watchability": r["avg_score"],
        "best_game": best_by_abbr.get(r["abbr"]),
    } for i, r in enumerate(agg_rows, start=1)]

    return jsonify({"min_games": min_games, "results": results})


@app.route("/api/top/weekly-peaks")
def api_weekly_peaks():
    conn = get_db()
    season = request.args.get("season")
    if not season:
        abort(400, description="season is required")
    try:
        season = int(season)
    except ValueError:
        abort(400, description="season must be an integer")
    season_type = request.args.get("season_type", "2")
    if season_type not in ("2", "3"):
        abort(400, description="season_type must be 2 or 3")
    season_type = int(season_type)

    rows = conn.execute("""
        SELECT g.week, g.game_id, g.watchability_score, g.home_team_abbr, g.away_team_abbr,
               g.home_score, g.away_score
        FROM games g
        JOIN (
            SELECT week, MAX(watchability_score) AS peak FROM games
            WHERE season_year=? AND season_type=? AND watchability_score IS NOT NULL
            GROUP BY week
        ) m ON g.week = m.week AND g.watchability_score = m.peak
        WHERE g.season_year=? AND g.season_type=?
        ORDER BY g.week
    """, (season, season_type, season, season_type)).fetchall()

    seen, results = set(), []
    for r in rows:
        if r["week"] in seen:
            continue
        seen.add(r["week"])
        results.append({
            "week": r["week"],
            "game_id": r["game_id"],
            "watchability_score": r["watchability_score"],
            "matchup": f"{r['away_team_abbr']} {r['away_score']} at {r['home_team_abbr']} {r['home_score']}",
        })
    return jsonify({"season": season, "season_type": season_type, "weeks": results})


# ---- analytics ---------------------------------------------------------------

@app.route("/api/analytics")
def api_analytics():
    conn = get_db()
    season = request.args.get("season")
    season_type = request.args.get("season_type", "all")

    where = ["g.watchability_score IS NOT NULL"]
    params = []
    if season:
        try:
            where.append("g.season_year = ?")
            params.append(int(season))
        except ValueError:
            abort(400, description="season must be an integer")
    if season_type != "all":
        if season_type not in ("2", "3"):
            abort(400, description="season_type must be 2, 3, or all")
        where.append("g.season_type = ?")
        params.append(int(season_type))
    where_sql = " AND ".join(where)

    score_rows = conn.execute(f"SELECT watchability_score AS s FROM games g WHERE {where_sql}", params).fetchall()
    scores = sorted(r["s"] for r in score_rows)
    n = len(scores)
    if n:
        mean_score = sum(scores) / n
        median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2
        p10 = scores[int(n * 0.10)]
        p90 = scores[min(int(n * 0.90), n - 1)]
        dist = {"n": n, "min": scores[0], "median": median, "mean": mean_score, "max": scores[-1],
                "p10": p10, "p90": p90, "histogram": histogram(scores)}
    else:
        mean_score = None
        dist = {"n": 0, "min": None, "median": None, "mean": None, "max": None, "p10": None, "p90": None,
                "histogram": histogram([])}

    stat_rows = conn.execute(f"""
        SELECT gm.metric_name, COUNT(*) n, AVG(gm.raw_value) avg_raw, MAX(gm.raw_value) max_raw,
               AVG(gm.norm_value) avg_norm,
               SUM(CASE WHEN gm.norm_value >= 1.0 THEN 1 ELSE 0 END) n_at_cap
        FROM game_metrics gm JOIN games g ON g.game_id = gm.game_id
        WHERE {where_sql}
        GROUP BY gm.metric_name
    """, params).fetchall()
    stat_by_name = {r["metric_name"]: r for r in stat_rows}

    weight_vs_delivery = []
    for m in scoring.METRICS:
        name = m["name"]
        s = stat_by_name.get(name)
        avg_norm = s["avg_norm"] if s else 0.0
        n_metric = s["n"] if s else 0
        mean_weighted = avg_norm * m["weight"]
        designed_share = m["weight"] / TOTAL_WEIGHT
        delivered_share = (mean_weighted / (TOTAL_WEIGHT * mean_score)) if mean_score else 0.0
        weight_vs_delivery.append({
            "name": name, "label": METRIC_COPY[name]["label"], "weight": m["weight"], "cap": m["cap"],
            "n": n_metric, "avg_raw": s["avg_raw"] if s else None, "max_raw": s["max_raw"] if s else None,
            "avg_norm": avg_norm, "n_at_cap": s["n_at_cap"] if s else 0,
            "pct_at_cap": (s["n_at_cap"] / n_metric * 100) if n_metric else 0.0,
            "designed_share": designed_share, "delivered_share": delivered_share,
            "delta": delivered_share - designed_share,
        })
    callout = build_misalignment_callout(weight_vs_delivery)

    all_seasons = [r["season_year"] for r in conn.execute("SELECT DISTINCT season_year FROM games ORDER BY season_year").fetchall()]
    by_week_seasons = [int(season)] if season else all_seasons
    by_week_season_type = int(season_type) if season_type != "all" else 2
    by_week_rows = conn.execute(f"""
        SELECT season_year, week, AVG(watchability_score) AS avg_score, COUNT(*) AS n
        FROM games
        WHERE watchability_score IS NOT NULL AND week IS NOT NULL AND season_type = ?
              AND season_year IN ({','.join('?' * len(by_week_seasons)) if by_week_seasons else 'NULL'})
        GROUP BY season_year, week ORDER BY season_year, week
    """, [by_week_season_type] + by_week_seasons).fetchall()
    by_week = {}
    for r in by_week_rows:
        by_week.setdefault(str(r["season_year"]), []).append({"week": r["week"], "avg_score": r["avg_score"], "n": r["n"]})

    corr_cols = ["watchability_score"] + [m["name"] for m in scoring.METRICS]
    case_selects = ",\n               ".join(
        f"MAX(CASE WHEN gm.metric_name='{m['name']}' THEN gm.norm_value END) AS {m['name']}"
        for m in scoring.METRICS
    )
    corr_rows = conn.execute(f"""
        SELECT g.game_id, g.watchability_score,
               {case_selects}
        FROM games g LEFT JOIN game_metrics gm ON gm.game_id = g.game_id
        WHERE {where_sql}
        GROUP BY g.game_id
    """, params).fetchall()

    if corr_rows:
        import pandas as pd
        df = pd.DataFrame([dict(r) for r in corr_rows])[corr_cols]
        mat = df.corr()
        counts = df.notna().astype(int)
        count_mat = counts.T.dot(counts)
        correlation = {
            "labels": corr_cols,
            "matrix": [[(None if pd.isna(mat.iloc[i, j]) else round(float(mat.iloc[i, j]), 4))
                        for j in range(len(corr_cols))] for i in range(len(corr_cols))],
            "n_matrix": [[int(count_mat.iloc[i, j]) for j in range(len(corr_cols))] for i in range(len(corr_cols))],
        }
    else:
        correlation = {"labels": corr_cols, "matrix": [], "n_matrix": []}

    return jsonify({
        "n": n,
        "distribution": dist,
        "weight_vs_delivery": weight_vs_delivery,
        "callout": callout,
        "not_implemented": NOT_IMPLEMENTED,
        "by_week": by_week,
        "correlation": correlation,
        "total_weight": TOTAL_WEIGHT,
    })


# ---- static files -------------------------------------------------------------

@app.route("/")
def index_page():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:filename>")
def web_static(filename):
    target = (WEB_DIR / filename).resolve()
    try:
        target.relative_to(WEB_DIR)
    except ValueError:
        abort(404)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(WEB_DIR, filename)


if __name__ == "__main__":
    _startup_selfcheck()
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
