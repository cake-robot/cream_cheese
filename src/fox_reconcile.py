from . import db, scoring
from .scoring import (
    REGULATION_SECONDS,
    CLUTCH_FINISH_WINDOW_SECONDS,
    CLUTCH_FINISH_FIELD_GOAL_VALUE,
    CLUTCH_FINISH_NON_FIELD_GOAL_VALUE,
)


def _parse_clock(display):
    """Convert Fox's 'MM:SS' time_of_play to seconds remaining in the
    period. Same format/logic as espn._parse_clock()."""
    try:
        parts = (display or "").split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError, AttributeError):
        return None


def _elapsed_seconds(period_number, time_of_play):
    """
    Regulation-only synthetic elapsed-seconds clock, matching the exact
    convention espn.parse_summary_detail() uses: (period-1)*900 +
    (900 - secs_remaining). Returns None for OT or an unparseable clock --
    OT games are already excluded from clutch_finish entirely (see
    fox_clutch_finish), so this only ever needs to handle periods 1-4.
    """
    if period_number is None or period_number > 4:
        return None
    secs_remaining = _parse_clock(time_of_play)
    if secs_remaining is None:
        return None
    return (period_number - 1) * 900 + (900 - secs_remaining)


def build_espn_ladder(wp_rows):
    """
    Independent reconstruction of ESPN's own score progression from
    win_probability rows, for side-by-side display against fox_ladder
    during diff review. Uses the identical non-decreasing sanitization
    scoring.clutch_finish()/lead_changes() already apply inline (running
    max per side, discard anything below it) -- a faithful picture of what
    those functions actually see, not a stricter or looser reading invented
    for this comparison.

    NOT used to compute the compared metric values themselves -- diff_game()
    calls scoring.clutch_finish()/lead_changes() directly for that,
    guaranteeing the ESPN-side number is exactly what's actually stored in
    game_metrics, not a second, potentially-drifting reimplementation of the
    same logic.
    """
    steps = []
    home_score, away_score = 0, 0
    for r in wp_rows:
        h, a = r["home_score"], r["away_score"]
        if h is not None and h > home_score:
            steps.append({
                "team": "home", "new_value": h, "delta": h - home_score,
                "clock_seconds_elapsed": r["clock_seconds_elapsed"],
                "period_number": r["period_number"],
            })
        if h is not None and h >= home_score:
            home_score = h
        if a is not None and a > away_score:
            steps.append({
                "team": "away", "new_value": a, "delta": a - away_score,
                "clock_seconds_elapsed": r["clock_seconds_elapsed"],
                "period_number": r["period_number"],
            })
        if a is not None and a >= away_score:
            away_score = a
    return steps


def fox_lead_changes(fox_ladder):
    """
    Independent computation of scoring.lead_changes()'s statistic (3-state
    home/away/tied tracking, a return-to-tie counts as its own event),
    driven by fox_score_sequence's ordered steps instead of win_probability
    rows -- a genuinely different computation from Fox's differently-shaped
    data, not a duplicate of calling scoring.lead_changes() again.

    last_state starts at 0 (tied), not None: scoring.lead_changes() always
    has an explicit pregame 0-0 row as its first win_probability entry, so
    its first real lead change -- whoever scores first, breaking the tie --
    IS counted (last_state=0 from the pregame row differs from the first
    real state). fox_ladder has no equivalent seed row, so it has to be
    supplied here to match; starting from None instead would silently drop
    that first transition on every single game.
    """
    count = 0
    last_state = 0
    home_score, away_score = 0, 0
    for step in fox_ladder:
        if step["team"] == "home":
            home_score = step["new_value"]
        else:
            away_score = step["new_value"]
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


def fox_clutch_finish(fox_ladder, fox_plays_by_seq):
    """
    Mirrors scoring.clutch_finish() exactly (same return convention: None
    for OT/not-applicable, else 0.0/1.0/1.5), driven by Fox's ladder + plays
    instead of ESPN's win_probability rows.

    The last step in fox_ladder is the game's final score change,
    chronologically, across both teams (build_score_sequence() appends
    steps in scan order as they're encountered, not per-team). If that step
    is range-localized (exact=0) rather than pinned to a specific play,
    there's no single play to check the clock on -- the true scoring play
    could be anywhere in [seq_lo, seq_hi] -- so this returns None
    (undetermined) rather than guessing; diff_game() only compares
    clutch_finish when both sides resolve to a concrete value.
    """
    if not fox_ladder:
        return None
    if any(s["period_number"] is not None and s["period_number"] > 4 for s in fox_ladder):
        return None

    last = fox_ladder[-1]
    if not last["exact"]:
        return None

    play = fox_plays_by_seq.get(last["seq_hi"])
    if not play:
        return None

    elapsed = _elapsed_seconds(play["period_number"], play["time_of_play"])
    if elapsed is None:
        return None
    if elapsed < REGULATION_SECONDS - CLUTCH_FINISH_WINDOW_SECONDS:
        return 0.0
    return CLUTCH_FINISH_FIELD_GOAL_VALUE if last["delta"] == 3 else CLUTCH_FINISH_NON_FIELD_GOAL_VALUE


def diff_game(conn, game_id):
    """
    Reconcile one ESPN game against its matched Fox event. Returns None if
    the game isn't matched in fox_games. Otherwise a record:
      tier: 'unusable' -- Fox's own final score doesn't match the official
            box score (games.home_score/away_score) -- don't trust anything
            else about this comparison, flag for separate review
      tier: 'agree'    -- Fox and ESPN-derived values match on every metric
      tier: 'diff'     -- they disagree on at least one metric -- the
                           actual finding this whole pilot exists to surface
    `diffs` maps metric_name -> {espn, fox} only for metrics that disagree.
    `espn_ladder`/`fox_ladder` are included for human review, not scoring.
    """
    fox_row = conn.execute(
        "SELECT fox_event_id FROM fox_games WHERE game_id = ?", (game_id,)
    ).fetchone()
    if not fox_row:
        return None
    fox_event_id = fox_row["fox_event_id"]

    game = conn.execute(
        "SELECT home_score, away_score FROM games WHERE game_id = ?", (game_id,)
    ).fetchone()

    wp_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM win_probability WHERE game_id = ? ORDER BY play_sequence", (game_id,)
    ).fetchall()]
    fox_ladder = [dict(r) for r in conn.execute(
        "SELECT * FROM fox_score_sequence WHERE fox_event_id = ? ORDER BY step_number", (fox_event_id,)
    ).fetchall()]
    fox_plays_by_seq = {
        r["play_sequence"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM fox_plays WHERE fox_event_id = ?", (fox_event_id,)
        ).fetchall()
    }

    fox_final = {"home": 0, "away": 0}
    for step in fox_ladder:
        fox_final[step["team"]] = step["new_value"]
    # Checked against the box score in either orientation, not just ESPN's
    # own home/away labeling: a neutral-site game has no true home team, and
    # ESPN and Fox don't always agree on which side gets the label (see
    # fox_match.match_game()). lead_changes/clutch_finish are computed from
    # Fox's own ladder without ever referencing ESPN's labels, so a flipped
    # orientation doesn't affect them -- this check just needs to allow it.
    straight = fox_final["home"] == game["home_score"] and fox_final["away"] == game["away_score"]
    flipped = fox_final["home"] == game["away_score"] and fox_final["away"] == game["home_score"]
    if not (straight or flipped):
        return {
            "game_id": game_id, "fox_event_id": fox_event_id, "tier": "unusable",
            "diffs": {},
            "notes": (
                f"Fox final {fox_final['away']}-{fox_final['home']} != box score "
                f"{game['away_score']}-{game['home_score']} (either orientation)"
            ),
        }

    espn_lc = scoring.lead_changes(wp_rows)
    fox_lc = fox_lead_changes(fox_ladder)
    espn_cf = scoring.clutch_finish(wp_rows)
    fox_cf = fox_clutch_finish(fox_ladder, fox_plays_by_seq)

    diffs = {}
    if espn_lc != fox_lc:
        diffs["lead_changes"] = {"espn": espn_lc, "fox": fox_lc}
    # clutch_finish: None means "not applicable" (OT) or "undetermined"
    # (Fox couldn't resolve a time) -- only compare when both resolve.
    if espn_cf is not None and fox_cf is not None and espn_cf != fox_cf:
        diffs["clutch_finish"] = {"espn": espn_cf, "fox": fox_cf}

    return {
        "game_id": game_id, "fox_event_id": fox_event_id,
        "tier": "diff" if diffs else "agree",
        "diffs": diffs,
        "espn_ladder": build_espn_ladder(wp_rows),
        "fox_ladder": fox_ladder,
        "notes": "",
    }


def _games_in_scope(conn, season, week, season_type=2):
    if season is not None and week is not None:
        return [r[0] for r in conn.execute("""
            SELECT game_id FROM games
            WHERE season_year = ? AND week = ? AND season_type = ?
              AND game_id IN (SELECT game_id FROM fox_games)
        """, (season, week, season_type))]
    return [r[0] for r in conn.execute("SELECT game_id FROM fox_games")]


def _persist(conn, r):
    """
    Writes r's tier/diffs into fox_score_corrections, replacing any prior
    rows for this game -- the queryable, always-current record of every
    ESPN<->Fox discrepancy. 'agree' games get their rows cleared (nothing to
    see); 'unusable' gets one whole-game row (metric_name=None) with no
    value applied; 'diff' gets one row per disagreeing metric -- these are
    exactly the rows scoring.apply_corrections() trusts automatically.
    """
    if r["tier"] == "agree":
        db.replace_fox_score_corrections(conn, r["game_id"], [])
    elif r["tier"] == "unusable":
        db.replace_fox_score_corrections(conn, r["game_id"], [{
            "fox_event_id": r["fox_event_id"], "tier": "unusable",
            "metric_name": None, "espn_value": None, "fox_value": None,
            "notes": r["notes"],
        }])
    else:
        db.replace_fox_score_corrections(conn, r["game_id"], [
            {
                "fox_event_id": r["fox_event_id"], "tier": "diff",
                "metric_name": metric, "espn_value": vals["espn"], "fox_value": vals["fox"],
                "notes": None,
            }
            for metric, vals in r["diffs"].items()
        ])


def reconcile_all(conn, season=None, week=None, season_type=2):
    """diff_game() over every matched game in scope, persisting each result
    to fox_score_corrections as it goes. Returns the list of records."""
    results = []
    for game_id in _games_in_scope(conn, season, week, season_type):
        r = diff_game(conn, game_id)
        if r:
            _persist(conn, r)
            results.append(r)
    conn.commit()
    return results


def print_report(conn, season=None, week=None, season_type=2):
    results = reconcile_all(conn, season=season, week=week, season_type=season_type)
    counts = {}
    for r in results:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    summary = ", ".join(f"{t}={n}" for t, n in sorted(counts.items()))
    print(f"Reconciled {len(results)} game(s): {summary}")

    for r in results:
        if r["tier"] == "unusable":
            print(f"\n  [unusable] game {r['game_id']} (fox {r['fox_event_id']}): {r['notes']}")
        elif r["tier"] == "diff":
            print(f"\n  [diff] game {r['game_id']} (fox {r['fox_event_id']})")
            for metric, vals in r["diffs"].items():
                print(f"    {metric}: espn={vals['espn']}  fox={vals['fox']}")
