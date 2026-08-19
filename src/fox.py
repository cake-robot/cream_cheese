import re
import time

import requests

from .config import FOX_BASE, FOX_APIKEY, FOX_RATE_LIMIT_SECONDS

_last_request_time = 0.0


def _safe_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def fetch_event(fox_event_id, attempts=4, backoff_base=2.0):
    """
    GET the full event payload. Returns the parsed JSON dict, or None if the
    event doesn't exist (404). Raises after exhausting retries on repeated
    429/5xx/network errors -- this runs as a long unattended scan against an
    unofficial endpoint, so a silent partial result is worse than a loud stop.

    No Range support: the endpoint ignores Range headers and returns the full
    ~200KB body regardless (confirmed by inspecting response headers -- no
    accept-ranges/content-range, status always 200), so every call is a full
    fetch and there's no cheaper "header-only" request to make.
    """
    global _last_request_time
    url = f"{FOX_BASE}/event/{fox_event_id}/data?apikey={FOX_APIKEY}"
    last_exc = None
    for attempt in range(attempts):
        elapsed = time.time() - _last_request_time
        if elapsed < FOX_RATE_LIMIT_SECONDS:
            time.sleep(FOX_RATE_LIMIT_SECONDS - elapsed)
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            _last_request_time = time.time()
            if resp.status_code == 404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{resp.status_code} on event {fox_event_id}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            _last_request_time = time.time()
            if attempt < attempts - 1:
                time.sleep(backoff_base ** attempt)
    raise RuntimeError(
        f"Fox fetch failed for event {fox_event_id} after {attempts} attempts: {last_exc}"
    )


def _team_id_from_uri(uri):
    """'football/cfb/teams/39' -> 39. This numeric id is stable across every
    event a team appears in, unlike names/abbreviations which vary by source
    and sometimes by event."""
    if not uri:
        return None
    m = re.search(r"(\d+)\s*$", uri)
    return int(m.group(1)) if m else None


def _parse_team(side):
    return {
        "fox_team_id": _team_id_from_uri(side.get("uri")),
        "abbr": side.get("name", ""),
        "school_name": side.get("longName", ""),
        "mascot": side.get("stackedNameBottom", ""),
        "full_name": side.get("entityLink", {}).get("title", ""),
    }


def parse_header(payload):
    """
    Extract date/teams/final score from a fetched event payload.

    away_name/home_name come from `longName` (e.g. 'Texas A&M'), not the
    short `name` field (e.g. 'TXA&M') -- longName tracks ESPN's team naming
    much more closely and is what the ESPN<->Fox team crosswalk keys on
    instead of abbreviations, which diverge often enough to be unusable
    alone (e.g. ESPN 'TA&M' vs Fox 'TXA&M').

    away_team/home_team carry the full per-team identity block (fox_team_id,
    abbr, school_name, mascot, full_name) -- fox_team_id is what the
    crosswalk actually keys on once a team is resolved; the rest is stored
    in fox_teams as a byproduct of every header parse, growing that table
    automatically as more events get pulled.
    """
    header = payload.get("header", {})
    left = header.get("leftTeam", {}) or {}
    right = header.get("rightTeam", {}) or {}
    event_time = header.get("eventTime", "") or ""
    away_team = _parse_team(left)
    home_team = _parse_team(right)
    return {
        "event_date": event_time[:10] if event_time else None,
        "away_abbr": away_team["abbr"],
        "home_abbr": home_team["abbr"],
        "away_name": away_team["school_name"],
        "home_name": home_team["school_name"],
        "away_score": _safe_int(left.get("score")),
        "home_score": _safe_int(right.get("score")),
        "status_line": header.get("statusLine", ""),
        "away_team": away_team,
        "home_team": home_team,
    }


def _clock_remaining_seconds(time_of_play):
    """'11:06' -> 666 (seconds left in the period). None if unparsable."""
    if not time_of_play:
        return None
    m = re.match(r"^(\d+):(\d{2})$", time_of_play.strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _regulation_elapsed_seconds(period, time_of_play):
    """Elapsed seconds since kickoff, periods 1-4 only (15:00 quarters).
    Returns None for OT (period >= 5, where Fox's clock is always '0:00' --
    real CFB overtime has no game clock) or an unparsable/missing period."""
    if period is None or period < 1 or period > 4:
        return None
    remaining = _clock_remaining_seconds(time_of_play)
    if remaining is None:
        return None
    return (period - 1) * 900 + (900 - remaining)


def _assign_elapsed_seconds(steps):
    """
    Attach elapsed_seconds (x-axis position for the time-aligned score chart)
    to each step, in place.

    Regulation: derived from the play's own period/time_of_play. OT: Fox
    logs '0:00' for every OT play (there's no real game clock in CFB
    overtime), so OT steps get a synthetic 3600 + (period-5)*100 + counter
    position instead -- same convention the ESPN/win-probability side uses
    for its own synthetic OT clock.

    PAT/two-point tries are pinned to their touchdown's elapsed_seconds
    rather than using their own (later-logged) time_of_play: the real game
    clock is stopped for the whole try attempt, so the few seconds of drift
    Fox logs between the TD's play and the try's play is a logging artifact,
    not a clock change. Detected as: this step's delta is 1 or 2 (PAT or
    two-point try) and the immediately preceding step is a touchdown (delta
    6) by the same team -- a real safety (also delta 2) never immediately
    follows that same team's own touchdown, so this doesn't collide with it.
    """
    ot_counters = {}
    for step in steps:
        period = step["period_number"]
        elapsed = _regulation_elapsed_seconds(period, step.get("time_of_play"))
        if elapsed is None:
            ot_period = period if period is not None and period >= 5 else 5
            ot_counters[ot_period] = ot_counters.get(ot_period, 0) + 1
            elapsed = 3600 + (ot_period - 5) * 100 + ot_counters[ot_period]
        step["elapsed_seconds"] = elapsed
        step["clock_pinned"] = False

    for i, step in enumerate(steps):
        if i == 0 or step["delta"] not in (1, 2):
            continue
        prev = steps[i - 1]
        if prev["team"] == step["team"] and prev["delta"] == 6:
            step["elapsed_seconds"] = prev["elapsed_seconds"]
            step["clock_pinned"] = True


def _is_kickoff(text):
    return "kicks" in text and "yards from" in text


def _classify_try(text):
    """
    Classify a single play's own text as a PAT/two-point try, or not one at
    all. Returns (try_type, try_result, decisive_phrase):
      try_type   -- 'pat' | 'two_point' | None (not a try attempt)
      try_result -- 'good' | 'failed' | None (a try attempt, but Fox's text
                    doesn't contain a phrase this recognizes -- rare, e.g. a
                    penalty truncating the sentence before the outcome word)
      decisive_phrase -- the exact substring in `text` that decided
                    try_result, so a caller can highlight just that part
                    instead of the whole sentence. None when try_result is.

    'and it is good' covers a defense returning an intercepted two-point
    try the length of the field -- Fox doesn't tag that with the usual
    SUCCEEDS/FAILS. 'is missed'/'is blocked' cover both apply to PATs;
    two-point tries only ever use SUCCEEDS/FAILS in this data.
    """
    if text.startswith("TWO-POINT CONVERSION ATTEMPT"):
        for phrase in ("SUCCEEDS", "and it is good"):
            if phrase in text:
                return "two_point", "good", phrase
        if "FAILS" in text:
            return "two_point", "failed", "FAILS"
        return "two_point", None, None
    if "extra point" in text:
        if "is good" in text:
            return "pat", "good", "is good"
        for phrase in ("is no good", "is blocked", "is missed"):
            if phrase in text:
                return "pat", "failed", phrase
        return "pat", None, None
    return None, None, None


def _attach_try_results(steps, play_rows):
    """
    After each touchdown, look ahead in the raw play stream for its
    PAT/two-point try and record the result on the TD step -- including a
    FAILED try, which never produces its own score step (build_score_
    sequence only records plays that changed the score, and a missed try
    doesn't).

    Bounded by the next kickoff (a try always happens before the ensuing
    kickoff) or another touchdown, so a TD with no try logged by Fox at all
    -- a real gap in Fox's feed, not rare (~1150 cases dataset-wide) -- is
    correctly left unattributed instead of borrowing a later drive's PAT.
    play_rows is 1:1 with play_sequence (play_rows[i]['play_sequence'] ==
    i + 1), so a TD's own play_sequence is a direct list index into the
    plays right after it.
    """
    n_plays = len(play_rows)
    for step in steps:
        step["try_type"] = None
        step["try_result"] = None
        step["try_evidence"] = None
        step["try_decisive"] = None
        if step["delta"] != 6:
            continue
        td_seq = step["seq_hi"]
        for idx in range(td_seq, min(td_seq + 25, n_plays)):
            desc = play_rows[idx]["play_description"] or ""
            if "TOUCHDOWN" in desc or _is_kickoff(desc):
                break
            try_type, try_result, decisive = _classify_try(desc)
            if try_type:
                step["try_type"] = try_type
                step["try_result"] = try_result
                step["try_evidence"] = desc
                step["try_decisive"] = decisive
                break


def _period_from_section_title(title):
    """
    '1ST QUARTER'..'4TH QUARTER' -> 1..4. 'OVERTIME' -> 5, 'nTH OVERTIME' ->
    4+n (matches ESPN's synthetic OT period numbering convention). Verified
    against a real 2OT game: sections are 'OVERTIME' then '2ND OVERTIME'.
    """
    t = (title or "").upper()
    quarter_map = {"1ST QUARTER": 1, "2ND QUARTER": 2, "3RD QUARTER": 3, "4TH QUARTER": 4}
    if t in quarter_map:
        return quarter_map[t]
    if "OVERTIME" in t:
        m = re.match(r"(\d+)", t)
        n = int(m.group(1)) if m else 1
        return 4 + n
    return None


def parse_pbp_plays(payload):
    """
    Flatten pbp.sections[].groups[].plays[] into an ordered list of play
    dicts, one row per play, 1-based play_sequence assigned globally.

    Each group (drive) carries its own end-of-drive leftTeamScore/
    rightTeamScore, present on every group -- unlike per-play scores, which
    appear on only the plays that are themselves scoring plays (~10% of
    plays in a typical game). That group-level score is denormalized onto
    every play row as group_away_score/group_home_score, because it's what
    lets build_score_sequence() close gaps in drives that have no
    individually-flagged scoring play at all (confirmed case: a punt-return
    TD's PAT that appears on no play in the drive at all, only recoverable
    via this backstop), and what lets a future ladder rebuild work from
    stored rows alone without re-fetching.
    """
    rows = []
    seq = 0
    sections = (payload.get("pbp") or {}).get("sections", [])
    for section in sections:
        period = _period_from_section_title(section.get("title", ""))
        for group in section.get("groups", []):
            group_away = _safe_int(group.get("leftTeamScore"))
            group_home = _safe_int(group.get("rightTeamScore"))
            plays = group.get("plays", [])
            for i, play in enumerate(plays):
                seq += 1
                rows.append({
                    "play_sequence": seq,
                    "fox_play_id": str(play.get("id", "")),
                    "period_number": period,
                    "group_id": group.get("id"),
                    "group_title": group.get("title", ""),
                    "play_title": play.get("title", ""),
                    "play_description": play.get("playDescription", ""),
                    "time_of_play": play.get("timeOfPlay", ""),
                    "away_score": _safe_int(play.get("leftTeamScore")),
                    "home_score": _safe_int(play.get("rightTeamScore")),
                    "away_score_change": int(bool(play.get("leftTeamScoreChange"))),
                    "home_score_change": int(bool(play.get("rightTeamScoreChange"))),
                    "group_away_score": group_away,
                    "group_home_score": group_home,
                    "is_last_in_group": i == len(plays) - 1,
                })
    return rows


def build_score_sequence(play_rows):
    """
    Reconstruct each team's scoring ladder from parse_pbp_plays() output.

    Core principle (validated by hand against the UTSA@TAM punt-return-TD
    case, where the resulting PAT has no flagged play anywhere): track the
    latest known value per team, and treat ANY score observation -- whether
    the play that reports it flagged its own *ScoreChange or not -- as
    evidence of the true value as of that play. A step is 'exact' only when
    the play reporting the new value also flagged that side's own change;
    otherwise it's range-localized between the last confirmed play and the
    play where the new value was first observed.

    Per-play score fields cover the true scoring plays directly (exact).
    The group's own end-of-drive score (present on every group, unlike
    per-play scores) is applied as a backstop after each drive, catching
    silent changes in drives where no play carries score info at all.

    Any observed value below the running score is rejected outright: a
    football score never decreases, so a lower value is Fox-side data
    corruption, not a correction. Confirmed case (NEB@CIN, event 42815): a
    PAT play was wrongly flagged as also changing the OTHER team's score,
    and separately the final "END OF GAME" pseudo-drive carried a stale
    group-level total -- both reported a value one point below what was
    already confirmed. Same non-decreasing-score sanitization scoring.py
    already applies on the ESPN side.
    """
    steps = []
    running = {"away": 0, "home": 0}
    last_seq = {"away": 0, "home": 0}

    def observe(side, value, seq, exact, period, evidence, time_of_play):
        if value is None:
            return
        if value < running[side]:
            print(f"  [fox] rejected backward score observation: {side} {value} < "
                  f"running {running[side]} at play_sequence {seq} ({evidence[:60]!r})")
            return
        if value == running[side]:
            last_seq[side] = seq
            return
        steps.append({
            "step_number": len(steps) + 1,
            "team": side,
            "new_value": value,
            "delta": value - running[side],
            "exact": int(bool(exact)),
            "seq_lo": last_seq[side],
            "seq_hi": seq,
            "period_number": period,
            "evidence": evidence,
            "time_of_play": time_of_play,
        })
        running[side] = value
        last_seq[side] = seq

    current_group_id = object()
    group_rows = []

    def flush_group():
        if not group_rows:
            return
        last = group_rows[-1]
        observe(
            "away", last["group_away_score"], last["play_sequence"], False,
            last["period_number"], f"[drive end] {last['group_title']}", last["time_of_play"],
        )
        observe(
            "home", last["group_home_score"], last["play_sequence"], False,
            last["period_number"], f"[drive end] {last['group_title']}", last["time_of_play"],
        )

    for row in play_rows:
        if row["group_id"] != current_group_id:
            flush_group()
            group_rows = []
            current_group_id = row["group_id"]
        group_rows.append(row)

        observe(
            "away", row["away_score"], row["play_sequence"], bool(row["away_score_change"]),
            row["period_number"], row["play_description"], row["time_of_play"],
        )
        observe(
            "home", row["home_score"], row["play_sequence"], bool(row["home_score_change"]),
            row["period_number"], row["play_description"], row["time_of_play"],
        )
    flush_group()

    _assign_elapsed_seconds(steps)
    _attach_try_results(steps, play_rows)
    return steps
