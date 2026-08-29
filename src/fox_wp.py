"""
Synthesizes a win_probability series for games ESPN has no WP data for at
all, from a matched Fox score_sequence, so scoring.score_games() can score
them the same way it scores an ESPN-sourced game. Prompted by
plans/algorithm/data_quality_findings.md #15 (401628511, ILL@PUR 50-49,
ESPN's own summary API returns an empty winprobability array) -- but not
specific to that one game: as of 2026-08-20, all 72 completed/detail-fetched
games with zero win_probability rows are Fox-matched, so this closes that
whole backlog bucket rather than one game.

Rows are inserted into win_probability itself (source=db.SYNTHETIC_WP_SOURCE,
play_id='fox-synth-<n>') via db.replace_synthetic_wp(), not held in a
parallel table -- every downstream consumer (scoring.py, serve.py's WP
chart, fox_reconcile) already reads that table and needs no changes to pick
these up.

Known v1 gaps, accepted (2026-08-20):
- Coarse granularity: one point per Fox score-changing play (~20-30/game)
  instead of ESPN's ~150-200 WP entries/game.
- No pregame line: a Fox-only game has no initial_home_wp, so every
  synthetic point uses wp_baseline.coinflip_wp_elapsed() (score_diff +
  elapsed only) rather than predict_wp_elapsed() -- "what the score+clock
  alone implies", not a line-anchored WP. Same fallback upset_risk/
  upset_in_progress already use for an unknown pregame line.
- Overtime plays themselves are dropped, not modeled -- CFB OT has no real
  game clock (Fox logs 0:00 for every OT play; see src/fox.py's
  _assign_elapsed_seconds), and wp_baseline.ELAPSED_MODEL is already an
  accepted-but-unverified extrapolation past Q1-3 for real elapsed time,
  let alone OT's synthetic one. The *fact* that a game went to OT is still
  flagged via a marker row (see _append_ot_marker) -- without it,
  scoring.clutch_finish()'s OT-floor / tie-holds-into-OT credit would
  silently never fire for a synthetic-only game, understating real drama.
"""
from . import db, wp_baseline


def _append_ot_marker(rows):
    """
    Regulation can't end in a tie in CFB -- a tie at the end of the 4th is
    itself proof the game went to OT, cheaper and more direct than a second
    query against fox_score_sequence for period_number > 4. If the last
    (regulation) row is tied, append one marker row with period_number=5 so
    scoring.clutch_finish()'s `is_ot` check (any row with period_number > 4)
    sees it. The marker repeats the last row's score/WP verbatim (delta 0
    against every other row), so every other metric -- which all key off a
    *change* between consecutive rows -- treats it as a no-op; only the
    period_number-based is_ot check reacts to it.
    """
    if len(rows) < 2 or rows[-1]["home_score"] != rows[-1]["away_score"]:
        return rows
    marker = dict(rows[-1])
    marker["period_number"] = 5
    marker["clock_seconds_elapsed"] = None
    rows.append(marker)
    return rows


def build_synthetic_wp_rows(conn, game_id):
    """
    Returns a list of dicts (home_win_pct, home_score, away_score,
    period_number, clock_seconds_elapsed) reconstructed from this game's
    matched fox_score_sequence -- regulation only (period 1-4; see module
    docstring for why OT is dropped and how it's still flagged). None if
    this game has no Fox match.
    """
    fox_row = conn.execute(
        "SELECT fox_event_id FROM fox_games WHERE game_id = ?", (game_id,)
    ).fetchone()
    if not fox_row:
        return None
    fox_event_id = fox_row["fox_event_id"]

    steps = conn.execute("""
        SELECT team, new_value, period_number, elapsed_seconds
        FROM fox_score_sequence
        WHERE fox_event_id = ? AND period_number IS NOT NULL AND period_number <= 4
        ORDER BY step_number
    """, (fox_event_id,)).fetchall()

    home_score, away_score = 0, 0
    rows = [{
        "home_win_pct": 0.5, "home_score": 0, "away_score": 0,
        "period_number": 1, "clock_seconds_elapsed": 0,
    }]
    for s in steps:
        if s["team"] == "home":
            home_score = s["new_value"]
        else:
            away_score = s["new_value"]
        elapsed = s["elapsed_seconds"]
        wp = wp_baseline.coinflip_wp_elapsed(elapsed, home_score - away_score)
        rows.append({
            "home_win_pct": wp,
            "home_score": home_score,
            "away_score": away_score,
            "period_number": s["period_number"],
            "clock_seconds_elapsed": elapsed,
        })
    return _append_ot_marker(rows)


def synthesize_missing_wp(conn, season=None, week=None, season_type=2):
    """
    For every completed, detail_fetched game in scope with zero
    win_probability rows and a Fox match, build and store a synthetic
    series. Returns (candidates, synthesized) counts -- synthesized can be
    lower than candidates if a Fox match exists but its fox_score_sequence
    is empty. Idempotent: db.replace_synthetic_wp() replaces this game's
    fox_synthetic rows wholesale on a re-run.
    """
    where = ("g.completed = 1 AND g.detail_fetched = 1 "
             "AND NOT EXISTS (SELECT 1 FROM win_probability w WHERE w.game_id = g.game_id) "
             "AND g.game_id IN (SELECT game_id FROM fox_games)")
    params = []
    if season is not None:
        where += " AND g.season_year = ?"
        params.append(season)
    if week is not None:
        where += " AND g.week = ? AND g.season_type = ?"
        params.extend([week, season_type])

    rows = conn.execute(
        f"SELECT game_id, home_team_id, away_team_id, home_team_abbr, away_team_abbr "
        f"FROM games g WHERE {where}", params,
    ).fetchall()

    synthesized = 0
    for r in rows:
        wp_rows = build_synthetic_wp_rows(conn, r["game_id"])
        if not wp_rows:
            continue
        db.replace_synthetic_wp(conn, r["game_id"], r["home_team_id"], r["away_team_id"], wp_rows)
        synthesized += 1
        print(f"  {r['away_team_abbr']} @ {r['home_team_abbr']} ({r['game_id']}): "
              f"{len(wp_rows)} synthetic WP rows")
    conn.commit()
    return len(rows), synthesized
