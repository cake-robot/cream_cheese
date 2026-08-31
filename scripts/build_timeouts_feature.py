"""
Builds a per-play "timeouts remaining" feature from game_raw_json's play-by-
play, and validates it before it's trusted enough to feed into a model.

Per the investigation in this session: ESPN logs every stoppage (team
timeouts AND the automatic under-2-minute timeout AND routine TV/media
timeouts) under the same play `type.text == "Timeout"`. The only reliable
signal for "was this actually charged to a team" is whether the play's free-
text description names a team: `"Timeout <SCHOOL>, clock MM:SS"` for a real
team timeout vs. `"Timeout , clock MM:SS"` (blank) for an uncharged
stoppage -- validated by cross-checking against `start.down` across 300
sampled games (92.8% clean agreement; the disagreements confirmed `down` is
the LESS reliable of the two signals near drive starts, not the team-name
text). The matched team name is compared against this specific game's own
home/away `teams.school` values (not a global fuzzy match) -- exact,
case-insensitive, with those columns already populated for 761/768 teams
and observed to exactly match ESPN's play-text school names (e.g. "Miami
(OH)", "UL Monroe", "Florida International").

Running per-team counters start at 3 (NCAA's per-half allotment) at the
start of the game and reset to 3 at the start of the second half (period
3). Overtime's reset rule is NOT assumed -- it's determined empirically
below by testing which candidate reset produces zero "called a timeout
with 0 remaining" events, since NCAA's OT timeout rule has changed across
the seasons this project already tracks in
plans/algorithm/cfp_playoff_format_history.md-style history notes.

Usage:
    venv/bin/python3 scripts/build_timeouts_feature.py [path/to/cfb.db]
"""
import re
import sys
from collections import Counter

sys.path.insert(0, ".")

from src import db
from src.espn import _parse_clock

TIMEOUT_TEXT = re.compile(r"^Timeout\s*(.*?),\s*clock", re.IGNORECASE)
HALFTIME_RESET_PERIOD = 3
REGULATION_RESET = 3


def _iter_drives(raw):
    drives = raw.get("drives", {})
    out = list(drives.get("previous", []))
    current = drives.get("current")
    if isinstance(current, dict):
        out.append(current)
    elif isinstance(current, list):
        out.extend(current)
    return out


def _all_plays_ordered(raw):
    """Every play across every drive, in the drives' own list order (which
    is already chronological -- drives.previous is itself ordered, and each
    drive's plays list is ordered)."""
    plays = []
    for drive in _iter_drives(raw):
        plays.extend(drive.get("plays", []))
    return plays


def _match_team(extracted, home_school, away_school):
    extracted = (extracted or "").strip().casefold()
    if not extracted:
        return None
    if home_school and extracted == home_school.strip().casefold():
        return "home"
    if away_school and extracted == away_school.strip().casefold():
        return "away"
    return None


def build_timeout_events_multi(conn, ot_resets):
    """Single decompression pass over the whole corpus, computing results for
    every ot_reset hypothesis in `ot_resets` at once (each key -> its own
    running counters), so testing several OT hypotheses doesn't cost a
    separate full pass per hypothesis.

    Returns {ot_reset: (events, unmatched, negative_events)} -- see the
    single-hypothesis docstring this replaced for the shape of each.
    unmatched is identical across hypotheses (team-name matching doesn't
    depend on the OT reset rule) but is still keyed per-hypothesis for a
    uniform return shape.
    """
    games = conn.execute("""
        SELECT game_id, home_team_id, away_team_id
        FROM games WHERE completed = 1 AND detail_fetched = 1
    """).fetchall()

    school_cache = {}
    def school_of(team_id):
        if team_id not in school_cache:
            row = conn.execute("SELECT school FROM teams WHERE team_id = ?", (team_id,)).fetchone()
            school_cache[team_id] = row["school"] if row else None
        return school_cache[team_id]

    results = {r: ([], [], []) for r in ot_resets}

    for g in games:
        raw = db.get_game_raw_json(conn, g["game_id"])
        if not raw:
            continue
        home_school = school_of(g["home_team_id"])
        away_school = school_of(g["away_team_id"])
        plays = _all_plays_ordered(raw)

        state = {
            r: {"left": {"home": REGULATION_RESET, "away": REGULATION_RESET},
                "seen_half2": False, "seen_ot": set()}
            for r in ot_resets
        }

        for play in plays:
            period = (play.get("period") or {}).get("number")
            if period is None:
                continue

            for ot_reset in ot_resets:
                st = state[ot_reset]
                if HALFTIME_RESET_PERIOD <= period <= 4 and not st["seen_half2"]:
                    st["left"] = {"home": REGULATION_RESET, "away": REGULATION_RESET}
                    st["seen_half2"] = True
                if period > 4 and period not in st["seen_ot"]:
                    st["seen_ot"].add(period)
                    if ot_reset is not None:
                        st["left"] = {"home": ot_reset, "away": ot_reset}

            if play.get("type", {}).get("text") != "Timeout":
                continue
            m = TIMEOUT_TEXT.match(play.get("text", "") or "")
            extracted = m.group(1) if m else ""
            if not extracted.strip():
                continue  # uncharged stoppage (2-min warning / TV / media)

            team = _match_team(extracted, home_school, away_school)

            secs_remaining = _parse_clock((play.get("clock") or {}).get("displayValue") or "")
            elapsed = None
            if period <= 4 and secs_remaining is not None:
                elapsed = (period - 1) * 900 + (900 - secs_remaining)

            for ot_reset in ot_resets:
                events, unmatched, negative_events = results[ot_reset]
                st = state[ot_reset]
                if team is None:
                    unmatched.append((g["game_id"], extracted))
                    continue
                if st["left"][team] <= 0:
                    negative_events.append((g["game_id"], period, team))
                    st["left"][team] = 0
                else:
                    st["left"][team] -= 1
                events.append({
                    "game_id": g["game_id"],
                    "period": period,
                    "elapsed_seconds": elapsed,
                    "team": team,
                    "timeouts_left_after": st["left"][team],
                })

    return results


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    ot_resets = (None, 1, 2, 3)
    print(f"Single pass over the corpus, testing {len(ot_resets)} OT-reset hypotheses at once...\n")
    results = build_timeout_events_multi(conn, ot_resets)

    for ot_reset in ot_resets:
        events, unmatched, negatives = results[ot_reset]
        ot_negatives = [n for n in negatives if n[1] > 4]
        reg_negatives = [n for n in negatives if n[1] <= 4]
        label = "no reset (carries over from regulation)" if ot_reset is None else f"reset to {ot_reset} each OT period"
        print(f"OT hypothesis: {label}")
        print(f"  total matched team-charged timeouts: {len(events)}")
        print(f"  unmatched (named a team, no school match): {len(unmatched)}")
        print(f"  negative-count events in regulation (period<=4): {len(reg_negatives)}")
        print(f"  negative-count events in OT (period>4):          {len(ot_negatives)}")
        print()

    _, unmatched, _ = results[1]
    print("Unmatched examples (first 20, should be rare/explainable):")
    for gid, name in unmatched[:20]:
        print(f"  {gid}: {name!r}")

    print("\nEnd-of-regulation timeouts-remaining distribution (using ot_reset=1 for the OT segment, irrelevant to this table):")
    events, _, _ = results[1]
    by_game = {}
    for e in events:
        if e["period"] <= 4:
            by_game.setdefault(e["game_id"], {}).setdefault(e["team"], e["timeouts_left_after"])
    counts = Counter()
    for gid, teams in by_game.items():
        for team, left in teams.items():
            counts[left] += 1
    for k in sorted(counts):
        print(f"  {k} left at last regulation timeout call: {counts[k]} team-games")


if __name__ == "__main__":
    main()
