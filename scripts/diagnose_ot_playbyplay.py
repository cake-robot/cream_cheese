"""
Diagnostic (read-only): is the OT *play-by-play* data in game_raw_json
(down/distance/yardsToEndzone/score per play) itself corrupted, separate
from the already-documented ESPN win_probability-value unreliability in OT
(see scripts/diagnose_ot_wp.py) and the known-bad 8-OT game 401628439 (see
plans/algorithm/data_quality_findings.md)?

This matters specifically because fit_wp_situational_model.py's "Model C"
trains directly on drives.*.plays' start.down/start.distance/
start.yardsToEndzone and homeScore/awayScore fields for every period
including OT -- if those raw fields are themselves garbage in OT (not just
ESPN's own derived WP), Model C would be learning from noise there.

Checks, per completed/detail_fetched game with >=1 OT play in game_raw_json:
  1. Down/distance/field-position range violations in OT plays specifically
     (down not 1-4, distance<0, yardsToEndzone outside 0-100) -- rate
     compared against a same-size random sample of non-OT (Q1-4) plays.
  2. Score backward-moves or upward-then-reverting spikes across OT plays,
     in the SAME chronological order the model consumes (per-drive, drive
     list order) -- not the separately-computed win_probability table.
     Reported BOTH for all OT plays and for the subset that would actually
     survive fit_wp_situational_model.py's own down/distance filter (the
     rows that would actually reach Model C's training set).
  3. Play-count-per-OT-period sanity (a normal OT possession is a handful
     of plays; anomalously large counts suggest duplicated/mis-scoped
     plays).
  4. Malformed drives: a single drive object (one team's one possession)
     whose OWN plays list spans non-adjacent periods (e.g. period 1 AND
     period 5 in the same drive bucket) -- not just cross-drive ordering,
     but a drive that structurally cannot be one real possession. Compared
     against a same-size random sample of non-OT games as a baseline. This
     is the root cause behind most of check 2's backward-score findings:
     walking a drive list where a drive bucket itself mixes unrelated
     periods will look like the score went backward, even though each
     individual play's own homeScore/awayScore field is plausibly correct
     in isolation -- the corruption is in which drive bucket a play landed
     in, not the score field itself.
  5. Cross-check against the one already-known-bad game (401628439) to
     confirm this script's checks actually catch it, as a sanity check on
     the checks themselves.

Usage:
    venv/bin/python3 scripts/diagnose_ot_playbyplay.py [path/to/cfb.db]
"""
import random
import sys

sys.path.insert(0, ".")

from src import db

KNOWN_BAD_GAME = "401628439"


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
    plays = []
    for drive in _iter_drives(raw):
        plays.extend(drive.get("plays", []))
    return plays


def _play_period(play):
    return (play.get("period") or {}).get("number")


def check_range_violations(plays):
    bad = []
    for p in plays:
        start = p.get("start", {})
        down = start.get("down")
        distance = start.get("distance")
        ytg = start.get("yardsToEndzone")
        if down is None or distance is None or ytg is None:
            continue  # not a scrimmage-down play (kickoff/PAT/timeout) -- not counted either way
        violation = not (1 <= down <= 4) or distance < 0 or not (0 < ytg <= 100)
        if violation:
            bad.append((p.get("id"), down, distance, ytg))
    return bad


def check_malformed_drives(drives):
    """The most serious finding this script exists to catch: a single drive
    object (one team's one possession) whose OWN plays list spans
    non-adjacent periods (e.g. period 1 AND period 5 in the same drive) --
    not just plays being listed out of chronological order across drives,
    but a single drive bucket containing plays that cannot possibly belong
    to one real possession. Returns the list of (periods_spanned) for any
    such drive found."""
    bad = []
    for d in drives:
        periods = set((p.get("period") or {}).get("number") for p in d.get("plays", []))
        periods.discard(None)
        if periods and (max(periods) - min(periods) > 1):
            bad.append(sorted(periods))
    return bad


def check_score_anomalies(plays_ordered):
    """Same two-shape corruption pattern already documented for win_probability
    (stale-revert / upward-spike), applied to game_raw_json's own homeScore/
    awayScore fields in native drive/play order."""
    hmax = amax = -1
    backward = []
    prev_h = prev_a = None
    spikes = []
    scored = [p for p in plays_ordered if p.get("homeScore") is not None and p.get("awayScore") is not None]
    for i, p in enumerate(scored):
        h, a = p["homeScore"], p["awayScore"]
        if hmax >= 0 and (h < hmax or a < amax):
            backward.append((p.get("id"), h, a, hmax, amax))
        hmax, amax = max(hmax, h), max(amax, a)
        if prev_h is not None and i + 1 < len(scored):
            nxt = scored[i + 1]
            if (h > prev_h or a > prev_a) and (nxt["homeScore"] < h or nxt["awayScore"] < a):
                spikes.append((p.get("id"), prev_h, prev_a, h, a, nxt["homeScore"], nxt["awayScore"]))
        prev_h, prev_a = h, a
    return backward, spikes


def model_c_kept_plays(plays):
    """The exact subset of plays fit_wp_situational_model.py would keep --
    valid scrimmage down (1-4), valid distance (>=0), valid yardsToEndzone
    (0,100], and a known homeScore/awayScore. Used to measure contamination
    in terms of what Model C ACTUALLY trains on, not the raw play list."""
    kept = []
    for p in plays:
        start = p.get("start", {})
        down, distance, ytg = start.get("down"), start.get("distance"), start.get("yardsToEndzone")
        if down is None or distance is None or ytg is None:
            continue
        if not (1 <= down <= 4) or not (0 < ytg <= 100) or distance < 0:
            continue
        if p.get("homeScore") is None or p.get("awayScore") is None:
            continue
        kept.append(p)
    return kept


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    games = conn.execute("""
        SELECT game_id FROM games WHERE completed = 1 AND detail_fetched = 1
    """).fetchall()

    ot_games = []
    non_ot_game_ids = []
    per_game_ot_plays = {}
    per_game_reg_plays = {}
    per_game_drives = {}

    for g in games:
        gid = g["game_id"]
        raw = db.get_game_raw_json(conn, gid)
        if not raw:
            continue
        drives = _iter_drives(raw)
        plays = _all_plays_ordered(raw)
        ot_plays = [p for p in plays if (_play_period(p) or 0) > 4]
        reg_plays = [p for p in plays if (_play_period(p) or 0) and (_play_period(p) or 0) <= 4]
        if ot_plays:
            ot_games.append(gid)
            per_game_ot_plays[gid] = ot_plays
            per_game_reg_plays[gid] = reg_plays
            per_game_drives[gid] = drives
        else:
            non_ot_game_ids.append(gid)

    print(f"{len(ot_games)} completed games have >=1 OT play in game_raw_json.\n")

    total_ot_plays = sum(len(v) for v in per_game_ot_plays.values())
    total_ot_range_bad = 0
    total_ot_scrimmage = 0
    range_bad_games = []
    backward_games = []
    spike_games = []
    ot_period_counts = {}  # period -> list of per-game play counts

    for gid, ot_plays in per_game_ot_plays.items():
        bad = check_range_violations(ot_plays)
        scrimmage_n = sum(1 for p in ot_plays if p.get("start", {}).get("down") is not None)
        total_ot_scrimmage += scrimmage_n
        if bad:
            total_ot_range_bad += len(bad)
            range_bad_games.append((gid, bad))

        backward, spikes = check_score_anomalies(ot_plays)
        if backward:
            backward_games.append((gid, backward))
        if spikes:
            spike_games.append((gid, spikes))

        by_period = {}
        for p in ot_plays:
            per = _play_period(p)
            by_period.setdefault(per, 0)
            by_period[per] += 1
        for per, n in by_period.items():
            ot_period_counts.setdefault(per, []).append((gid, n))

    print("=== 1. Down/distance/field-position range violations (down not 1-4, distance<0, yardsToEndzone not in (0,100]) ===")
    print(f"  OT scrimmage-down plays checked: {total_ot_scrimmage}")
    print(f"  OT range violations: {total_ot_range_bad}  ({len(range_bad_games)} games affected)")
    for gid, bad in range_bad_games[:15]:
        print(f"    {gid}: {bad[:5]}{' ...' if len(bad) > 5 else ''}")

    # Same check on a same-size random sample of regulation plays, as a baseline
    reg_sample_games = random.sample(list(per_game_reg_plays.keys()), min(len(ot_games), len(per_game_reg_plays)))
    reg_scrimmage = 0
    reg_bad = 0
    for gid in reg_sample_games:
        plays = per_game_reg_plays[gid]
        scrimmage_n = sum(1 for p in plays if p.get("start", {}).get("down") is not None)
        reg_scrimmage += scrimmage_n
        reg_bad += len(check_range_violations(plays))
    print(f"  Baseline -- same-size random sample of REGULATION plays from OT games themselves:")
    print(f"  regulation scrimmage-down plays checked: {reg_scrimmage}")
    print(f"  regulation range violations: {reg_bad}")

    print(f"\n  OT violation rate: {100 * total_ot_range_bad / total_ot_scrimmage:.3f}%"
          if total_ot_scrimmage else "  n/a")
    print(f"  Regulation violation rate (same games): {100 * reg_bad / reg_scrimmage:.3f}%"
          if reg_scrimmage else "  n/a")

    print("\n=== 2. Score backward-moves / spikes in OT plays (native drive/play order) ===")
    print(f"  ALL OT plays -- games with >=1 backward move: {len(backward_games)}/{len(ot_games)}")
    for gid, backward in sorted(backward_games, key=lambda x: -len(x[1]))[:10]:
        print(f"    {gid}: {len(backward)} backward move(s), e.g. {backward[0]}")
    print(f"  ALL OT plays -- games with >=1 spike: {len(spike_games)}/{len(ot_games)}")
    for gid, spikes in sorted(spike_games, key=lambda x: -len(x[1]))[:10]:
        print(f"    {gid}: {len(spikes)} spike(s), e.g. {spikes[0]}")

    print("\n  Restricted to the exact subset Model C's own down/distance filter would KEEP for training "
          "(the number that actually matters):")
    kept_total = 0
    kept_backward_total = 0
    kept_spike_total = 0
    kept_affected_games = set()
    for gid, ot_plays in per_game_ot_plays.items():
        kept = model_c_kept_plays(ot_plays)
        kept_total += len(kept)
        backward, spikes = check_score_anomalies(kept)
        if backward:
            kept_backward_total += len(backward)
            kept_affected_games.add(gid)
        if spikes:
            kept_spike_total += len(spikes)
            kept_affected_games.add(gid)
    print(f"  OT plays kept by Model C's filter: {kept_total}  (of {total_ot_plays} total OT plays)")
    print(f"  backward moves among kept plays: {kept_backward_total}  spikes among kept plays: {kept_spike_total}")
    print(f"  games affected (of the kept subset): {len(kept_affected_games)}/{len(ot_games)}")
    print(f"  as a share of Model C's FULL training set (regulation + OT): "
          f"OT rows are ~{100 * kept_total / 608428:.2f}% of all training rows (608,428 in the original fit)")

    print("\n=== 3. Malformed drives: a single drive's plays list spanning non-adjacent periods ===")
    ot_malformed_games = 0
    ot_malformed_examples = []
    for gid, drives in per_game_drives.items():
        bad = check_malformed_drives(drives)
        if bad:
            ot_malformed_games += 1
            if len(ot_malformed_examples) < 10:
                ot_malformed_examples.append((gid, bad))
    print(f"  OT games with >=1 malformed drive: {ot_malformed_games}/{len(ot_games)}")
    for gid, bad in ot_malformed_examples:
        print(f"    {gid}: {bad}")

    non_ot_sample = random.sample(non_ot_game_ids, min(len(ot_games), len(non_ot_game_ids)))
    non_ot_malformed = 0
    for gid in non_ot_sample:
        raw = db.get_game_raw_json(conn, gid)
        if not raw:
            continue
        if check_malformed_drives(_iter_drives(raw)):
            non_ot_malformed += 1
    print(f"  Baseline -- same-size random sample of NON-OT games: {non_ot_malformed}/{len(non_ot_sample)}")

    print("\n=== 4. Plays-per-OT-period distribution (sanity: normal OT possession is a handful of plays) ===")
    for per in sorted(ot_period_counts):
        counts = [n for _, n in ot_period_counts[per]]
        counts_sorted = sorted(counts, reverse=True)
        print(f"  period {per}: n_games={len(counts)}  mean={sum(counts)/len(counts):.1f}  "
              f"max={counts_sorted[0]}  top games: {sorted(ot_period_counts[per], key=lambda x: -x[1])[:5]}")

    print(f"\n=== 5. Cross-check: does this catch the already-known-bad game {KNOWN_BAD_GAME}? ===")
    if KNOWN_BAD_GAME in per_game_ot_plays:
        bad = check_range_violations(per_game_ot_plays[KNOWN_BAD_GAME])
        backward, spikes = check_score_anomalies(per_game_ot_plays[KNOWN_BAD_GAME])
        print(f"  range violations: {len(bad)}  backward moves: {len(backward)}  spikes: {len(spikes)}")
        print(f"  total OT plays in this game: {len(per_game_ot_plays[KNOWN_BAD_GAME])}")
        by_period = {}
        for p in per_game_ot_plays[KNOWN_BAD_GAME]:
            by_period.setdefault(_play_period(p), 0)
            by_period[_play_period(p)] += 1
        print(f"  plays per OT period: {sorted(by_period.items())}")
    else:
        print(f"  {KNOWN_BAD_GAME} has no OT plays in game_raw_json for this DB (or raw JSON missing) -- can't cross-check.")

    print("\n=== Summary ===")
    any_flag = set(g for g, _ in range_bad_games) | set(g for g, _ in backward_games) | set(g for g, _ in spike_games)
    print(f"  OT games with >=1 red flag (range violation / backward score / spike): "
          f"{len(any_flag)}/{len(ot_games)} ({100 * len(any_flag) / len(ot_games):.1f}%)")
    if any_flag:
        print(f"  flagged game ids: {sorted(any_flag)}")


if __name__ == "__main__":
    main()
