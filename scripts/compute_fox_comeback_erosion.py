"""
Computes a Fox-play-by-play-driven comeback_erosion value for a game whose
ESPN play-by-play is too corrupted for espn.extract_situational_plays to
produce a trustworthy arc-walk (see plans/algorithm/data_quality_findings.md
and src/corrections.py's existing entries for this same class of game).

Fox's fox_score_sequence has no down/distance/field-position data -- just
score, period, and elapsed time -- so it can't feed Model C
(src/wp_situational.py). It CAN feed the older, simpler score+time+line-only
model (src/wp_baseline.py's coinflip_wp_elapsed), the same fallback already
used to backfill win_probability for the ~72 games where ESPN itself has no
WP data at all (src/fox_wp.py).

This script runs the EXACT SAME arc-walk algorithm as
scoring._comeback_erosion_walk (same threshold, same PARITY clamp at
arc-close only, same close-game trigger, same regulation-only scope via the
period<=4 filter) -- the only thing that changes is which win-probability
function feeds it. Read-only: produces a number to hand-copy into
src/corrections.py's CORRECTIONS list, exactly like this project's existing
hand-verified overrides. Does not write to the database or touch
src/wp_situational.py.

Usage:
    venv/bin/python3 scripts/compute_fox_comeback_erosion.py <game_id>
"""
import sys

sys.path.insert(0, ".")

from src import db, scoring, wp_baseline


def fox_regulation_events(conn, fox_event_id):
    """[{elapsed_seconds, home_score, away_score}], regulation only (period
    <= 4), in fox_score_sequence's own step order (already chronological --
    unlike the corrupted ESPN drives this script exists to route around).
    A synthetic (0, 0, 0) leadoff row seeds the walk before any score."""
    rows = conn.execute(
        "SELECT step_number, team, new_value, period_number, elapsed_seconds "
        "FROM fox_score_sequence WHERE fox_event_id = ? ORDER BY step_number",
        (fox_event_id,),
    ).fetchall()
    home, away = 0, 0
    events = [{"elapsed_seconds": 0, "home_score": 0, "away_score": 0}]
    for r in rows:
        if r["period_number"] > 4:
            break
        if r["team"] == "home":
            home = r["new_value"]
        else:
            away = r["new_value"]
        events.append({"elapsed_seconds": r["elapsed_seconds"], "home_score": home, "away_score": away})
    return events


def fox_comeback_erosion_walk(events, credit_open_arc=False):
    """scoring._comeback_erosion_walk, with coinflip_home_wp(play) replaced
    by wp_baseline.coinflip_wp_elapsed(elapsed_seconds, score_diff) -- see
    module docstring. Constants imported from scoring, not hardcoded, so
    this can never silently drift from production's actual threshold/clamp/
    trigger values."""
    if not events:
        return 0.0, []
    best = 0.0
    lo = hi = 0.5
    state = 0
    trace = []
    for e in events:
        sd = e["home_score"] - e["away_score"]
        w = wp_baseline.coinflip_wp_elapsed(e["elapsed_seconds"], sd)
        w_hi = max(w, scoring.PARITY)
        w_lo = min(w, scoring.PARITY)
        new_state = 1 if sd > 0 else (-1 if sd < 0 else 0)
        if new_state != state:
            if hi >= scoring.COMEBACK_EROSION_THRESHOLD:
                best = max(best, hi - w_hi)
            if lo <= 1 - scoring.COMEBACK_EROSION_THRESHOLD:
                best = max(best, w_lo - lo)
            lo = hi = w
            state = new_state
        else:
            lo = min(lo, w)
            hi = max(hi, w)
            if credit_open_arc:
                if hi >= scoring.COMEBACK_EROSION_THRESHOLD:
                    best = max(best, hi - w)
                if lo <= 1 - scoring.COMEBACK_EROSION_THRESHOLD:
                    best = max(best, w - lo)
        seconds_left = 3600 - e["elapsed_seconds"]
        if abs(sd) <= scoring.CLOSE_GAME_MARGIN and seconds_left > scoring.CLOSE_GAME_MIN_SECONDS_LEFT:
            if hi >= scoring.COMEBACK_EROSION_THRESHOLD:
                best = max(best, hi - w)
            if lo <= 1 - scoring.COMEBACK_EROSION_THRESHOLD:
                best = max(best, w - lo)
        trace.append((e["elapsed_seconds"], e["home_score"], e["away_score"], round(w, 4), round(best, 4)))
    return best, trace


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    game_id = sys.argv[1]
    conn = db.get_connection()

    fg = conn.execute("SELECT fox_event_id FROM fox_games WHERE game_id = ?", (game_id,)).fetchone()
    if not fg:
        print(f"no fox_games mapping for {game_id}")
        sys.exit(1)
    fox_event_id = fg["fox_event_id"]

    events = fox_regulation_events(conn, fox_event_id)
    print(f"{game_id} (fox_event_id={fox_event_id}): {len(events)} regulation score-state events")
    best, trace = fox_comeback_erosion_walk(events)

    print(f"\n{'elapsed':>8}{'home':>6}{'away':>6}{'coinflip_wp':>13}{'running_best':>14}")
    for row in trace:
        print(f"{row[0]:>8}{row[1]:>6}{row[2]:>6}{row[3]:>13}{row[4]:>14}")

    print(f"\nFox-driven comeback_erosion for {game_id}: {best:.4f}")


if __name__ == "__main__":
    main()
