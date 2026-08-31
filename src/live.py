"""
Live/in-progress game scoring.

Blends two halves into a single live_score, both in [0,1] and both kept
separately reportable so a ranking stays explainable (see plans/... the
"noon Pacific Saturday" design doc for the full rationale):

  live_score = LIVE_W_SO_FAR * quality_so_far + LIVE_W_FROM_HERE * drama_from_here

`quality_so_far` is a partial-retrospective score -- as much of the existing
watchability algorithm as validly applies to a prefix of the game, using
scoring.composite_from() against LIVE_SO_FAR_METRICS. `drama_from_here` is
purely prospective -- how worth turning on the rest of this game is, using
the same algebra against LIVE_FROM_HERE_METRICS.

Deliberately separate from src/scoring.py's METRICS/score_game(): these
registries and their weights/caps are tuned for partial data and must never
be merged into the retrospective corpus (games.watchability_score,
game_metrics) that leaderboards, analytics, and manual corrections depend on.
Scoring games that haven't kicked off is out of scope by design -- an
unstarted game is just listed, not scored (see the "Kickoff soon" slate
section) -- so there is no pregame tier here.
"""

import fcntl
import logging
import os
import subprocess
import sys
import signal
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, espn, fetchlog, scoring

logger = logging.getLogger(__name__)

# --- Blend weights ---
LIVE_W_SO_FAR = 0.40
LIVE_W_FROM_HERE = 0.60

# --- Partial-data guards ---
# Below this much game-clock elapsed, a "rate" (count / progress) is one or
# two plays extrapolated over sixty minutes -- meaningless, and liable to
# read as a false 1.0. Only gates the two rate metrics; proportions/maxima
# (comeback_erosion_live, upset_in_progress) degrade gracefully on their own.
LIVE_MIN_ELAPSED_SECONDS = 300
# Floor on the rate denominator so a wild opening sequence can't alone
# saturate a rate metric's cap.
LIVE_MIN_PROGRESS = 0.15

# --- New live-only metric caps ---
MAX_UPSET_IN_PROGRESS = 0.60
MAX_RECENT_VOLATILITY = 1.5
LIVE_RECENT_WINDOW = 20  # trailing WP rows considered by recent_volatility

# --- drama_from_here shape ---
LATENESS_FLOOR = 0.35
LATENESS_POWER = 2.0

# Minimum normalized (0-1, post-cap) score a from_here metric must clear to
# appear in headline_for()'s "Driven by X + Y" line. A flat bar rather than
# a percentile: from_here metrics (tension_now, upset_finish_potential, ...)
# have no stored historical distribution to rank against -- live_metrics is
# overwritten every poll cycle, not accumulated -- unlike the retrospective
# game_metrics corpus the completed-game leaderboard's top_contributors gate
# (serve.py's TOP_CONTRIBUTOR_PERCENTILE_MIN) can use.
LIVE_HEADLINE_MIN_NORMALIZED = 0.38

# wp_now is the median of the last 3 WP rows, not the last row alone (ESPN's
# WP series occasionally ends on an isolated garbage row -- verified on
# 15/1828 completed games). Log when the raw last row deviates from that
# median by more than this, so a live run surfaces the same class of glitch
# for manual review.
WP_NOW_DEVIATION_LOG_THRESHOLD = 0.30

REGULATION_SECONDS = scoring.REGULATION_SECONDS  # 3600, shared with scoring.py

# --- Refresh loop ---
LIVE_INTERVAL_SECONDS = 600
# Tier 2 (per-game /summary fetch) budget per cycle. ~40 simultaneous live
# games x 1 request each would be ~40s of a 600s cycle -- comfortable
# headroom even without relying on the priority ordering below, but the
# budget is kept below the realistic max concurrent slate size (rather than
# unbounded) so a pathological/duplicated slate can't turn one cycle into an
# unbounded request burst.
LIVE_SUMMARY_BUDGET = 50
LIVE_MAX_STALENESS_SECONDS = 300
LIVE_ALWAYS_REFRESH_PROGRESS = 0.85

LOCK_PATH = "data/live.lock"

# --- Schedule-aware sleeping ---
# The poller runs continuously (see deploy/com.creamcheese.live.plist) and
# derives its own cadence from the kickoff times already in `games`, rather
# than from a launchd calendar schedule that was only ever a proxy for
# "when are games on". LIVE_INTERVAL_SECONDS applies whenever something is
# (or is about to be) in progress; otherwise the poller sleeps to the
# earliest of a handful of schedule-derived boundaries (kickoff lead,
# caffeinate lead, the start of the next game's week, the next game's own
# day) rather than a flat idle ceiling -- see _schedule_interval.

# How early before a scheduled kickoff to switch to the fast cadence.
LIVE_KICKOFF_LEAD_SECONDS = 1 * 60

# How long past a scheduled kickoff a game the scoreboard still calls 'pre'
# is treated as possibly underway. ESPN routinely lags the real kickoff by a
# few minutes, weather delays run longer, and a stored kickoff time can be
# days stale (the schedule is only refreshed by `just discover`). Generous
# on purpose: the cost of being wrong is one request a minute, the cost of
# being right is not missing a game.
LIVE_KICKOFF_GRACE_SECONDS = 6 * 3600

# Hour (ET) at which the schedule-refresh wakes below land -- early enough
# to be well ahead of the earliest observed real kickoff (11:00 ET), so a
# refresh always precedes the day's first game.
LIVE_WEEK_ANCHOR_HOUR_ET = 8

# When next_kickoff is unknown (no 'pre' row on record) but the most recent
# game seen is a regular-season game within this many days, treat it as the
# ~7-day conference-championship -> first-bowl gap rather than a genuinely
# empty schedule, and poll daily until the postseason is discovered.
LIVE_BLIND_RECENT_GAME_DAYS = 14
LIVE_BLIND_BACKSTOP_SECONDS = 24 * 3600

# Genuinely nothing on record at all -- not the conf-champ/bowl gap
# LIVE_BLIND_BACKSTOP_SECONDS exists for, but the DB having no upcoming game
# whatsoever (e.g. before any discovery has ever been run for a season).
# Deliberately not a polling cadence: offseason discovery is manual, and a
# discovery run needs a subsequent `just live-now` to be noticed -- this is
# just a large finite bound so the sleep math stays well-defined, not a
# timer meant to ever actually fire in normal operation.
LIVE_NO_SCHEDULE_BACKSTOP_SECONDS = 365 * 24 * 3600

# `caffeinate` is asserted on a deliberately wider window than the fast poll
# cadence: an assertion can only *keep* the machine awake, never wake it, so
# by the time a kickoff is a minute out it is already too late if the Mac
# idle-slept that afternoon. Three hours ahead of the day's first kickoff
# reproduces roughly what the old unconditional `caffeinate -i` under a
# 09:00 StartCalendarInterval did on a gameday, without holding the machine
# awake the other ~80% of the year.
LIVE_CAFFEINATE_LEAD_SECONDS = 3 * 3600

# Granularity of the interruptible sleep. Bounds SIGTERM latency (well under
# launchd's default 20s SIGTERM->SIGKILL grace).
LIVE_SLEEP_SLICE_SECONDS = 5.0

# games.game_date as ESPN stores it -- UTC, minute precision, bare 'Z'. Not
# a format SQLite's datetime() accepts, but lexicographically ordered, so
# range comparisons are done against a string formatted the same way. Same
# pattern as serve.py's _default_slate_date.
GAME_DATE_FMT = "%Y-%m-%dT%H:%MZ"

CAFFEINATE_PATH = "/usr/bin/caffeinate"


# --- context helpers ---

def _closeness(wp):
    """1.0 at a 50/50 game, 0.0 at a fully lopsided one."""
    return 1.0 - abs(2.0 * wp - 1.0)


def _lateness_factor(elapsed):
    """
    How much a given closeness should count toward drama_from_here, scaled
    by how late in the game we are. Squared so Q4 dominates (Q1 ~= 0.37,
    halftime ~= 0.51, Q4 start ~= 0.72, 2:00 left ~= 0.99, OT = 1.0): a
    50/50 game with 2 minutes left is a materially better recommendation
    than a 50/50 game at the 5:00 mark of Q1, which still has 55 minutes to
    stop being close. LATENESS_FLOOR keeps an early coin-flip from reading
    as zero -- it's still a decent watch, just not the best one available.
    """
    if elapsed is None:
        lateness = 0.0
    else:
        lateness = max(0.0, min(1.0, elapsed / REGULATION_SECONDS))
    return LATENESS_FLOOR + (1.0 - LATENESS_FLOOR) * (lateness ** LATENESS_POWER)


def _late_window_open(wp_rows):
    """True once any WP row has reached period >= LATE_PERIOD_THRESHOLD (Q4
    or any OT). Shared gate for late_volatility_rate and clutch_finish_live:
    both are only meaningfully defined once the late-game window has
    actually opened -- before that, "no late swing yet" isn't a real 0, it's
    unknown, so the metric should drop out of the composite rather than
    silently score the game as if it had no chance of late drama."""
    return any(
        r["period_number"] is not None and r["period_number"] >= scoring.LATE_PERIOD_THRESHOLD
        for r in wp_rows
    )


def _q4_progress(elapsed):
    """Fraction of the 4th quarter (or later) that has elapsed, capped at
    1.0 once past regulation -- the rate denominator for late_volatility_rate,
    parallel to how progress_of() denominates the whole-game rate metrics."""
    if elapsed is None or elapsed <= 2700:
        return 0.0
    return min(1.0, (elapsed - 2700) / 900.0)


def progress_of(elapsed):
    """Fraction of regulation elapsed, clamped to [0,1] -- 1.0 for the whole
    of any overtime period, since "how late is it" saturates once the game
    is already past regulation."""
    if elapsed is None:
        return 0.0
    return max(0.0, min(1.0, elapsed / REGULATION_SECONDS))


def wp_now_of(wp_rows):
    """Median of the last 3 home_win_pct rows -- see WP_NOW_DEVIATION_LOG_THRESHOLD
    docstring above for why this isn't just the last row."""
    if not wp_rows:
        return None
    tail = [r["home_win_pct"] for r in wp_rows[-3:] if r["home_win_pct"] is not None]
    if not tail:
        return None
    med = sorted(tail)[len(tail) // 2]
    last = wp_rows[-1]["home_win_pct"]
    if last is not None and abs(last - med) > WP_NOW_DEVIATION_LOG_THRESHOLD:
        logger.warning(
            "wp_now: last row (%.4f) deviates from 3-row median (%.4f) by more than %.2f -- "
            "using the median.", last, med, WP_NOW_DEVIATION_LOG_THRESHOLD,
        )
    return med


def _elapsed_from_status(period, clock_remaining):
    """Derive elapsed regulation seconds from the scoreboard's status block
    (period + seconds remaining in period) -- the same shape as
    espn.parse_summary_detail's per-play derivation, but from comp.status
    rather than a play's clock display string. The scoreboard is the
    authoritative live clock (verified: the summary endpoint's status has
    no period/clock at all), so this is what progress/lateness are computed
    from, independent of how fresh the WP series itself is.
    """
    if period is None:
        return None
    remaining = clock_remaining if clock_remaining is not None else 0
    if period <= 4:
        return (period - 1) * 900 + (900 - remaining)
    # OT: the scoreboard doesn't expose the per-play OT counter
    # win_probability rows use (espn.py's ot_period_counter) -- approximate
    # with the OT period boundary alone. progress_of() caps at 1.0 for any
    # elapsed > REGULATION_SECONDS regardless, so this only affects display.
    return 3600 + (period - 5) * 100


def build_live_context(*, wp_rows, situational_plays, home_rank, away_rank, initial_home_wp,
                        status_period, status_clock_seconds):
    """
    Assemble the context dict consumed by score_live() / composite_from().

    status_period / status_clock_seconds come from the scoreboard (Tier 1,
    refreshed every cycle) -- verified the authoritative live clock source,
    since the summary endpoint's status carries no period/clock at all.
    wp_rows is whatever win_probability holds for this game right now
    (Tier 2, refreshed on a slower budget) -- it can lag the scoreboard's
    clock by multiple cycles, which is fine: quality_so_far metrics operate
    on whatever prefix of the series is available, and elapsed/progress are
    derived from the fresher scoreboard clock rather than the WP series's
    own (possibly stale) tail. situational_plays (espn.extract_situational_plays
    on the same freshly-fetched summary) is the regulation-only, per-play
    down/distance/field-position feed comeback_erosion_live now needs --
    see src/scoring.py's comeback_erosion for why it moved off wp_rows.

    Drops any pregame rows (period_number IS NULL) before they reach the
    metric functions -- ESPN's feed occasionally emits an extra pregame WP
    entry sitting at an implausible extreme (0.0/1.0) before a snap has
    happened, which wp_volatility_rate (no per-row score/period guard,
    unlike lead_change_rate/late_volatility_rate) would otherwise read as a
    real in-game swing. Same fix as scoring.score_games()'s query for
    completed games.
    """
    wp_rows = [r for r in wp_rows if r["period_number"] is not None]
    wp_now = wp_now_of(wp_rows)
    elapsed = _elapsed_from_status(status_period, status_clock_seconds)
    if elapsed is None and wp_rows:
        # Fall back to the WP series's own last elapsed value only if the
        # scoreboard didn't give us a clock (shouldn't normally happen once
        # a game is genuinely 'in').
        elapsed = wp_rows[-1]["clock_seconds_elapsed"]
    progress = progress_of(elapsed)
    return {
        "wp_rows": wp_rows,
        "situational_plays": situational_plays,
        "home_rank": home_rank,
        "away_rank": away_rank,
        "initial_home_wp": initial_home_wp,
        "wp_now": wp_now,
        "elapsed": elapsed,
        "progress": progress,
        "period": status_period,
    }


# --- Half A: quality_so_far metric functions ---

def wp_volatility_rate(ctx):
    if ctx["elapsed"] is None or ctx["elapsed"] < LIVE_MIN_ELAPSED_SECONDS:
        return None
    raw = scoring.wp_volatility(ctx["wp_rows"])
    return raw / max(ctx["progress"], LIVE_MIN_PROGRESS)


def lead_change_rate(ctx):
    if ctx["elapsed"] is None or ctx["elapsed"] < LIVE_MIN_ELAPSED_SECONDS:
        return None
    raw = scoring.lead_changes(ctx["wp_rows"])
    return raw / max(ctx["progress"], LIVE_MIN_PROGRESS)


def late_volatility_rate(ctx):
    if not _late_window_open(ctx["wp_rows"]):
        return None
    raw = scoring.late_volatility(ctx["wp_rows"])
    return raw / max(_q4_progress(ctx["elapsed"]), LIVE_MIN_PROGRESS)


def clutch_finish_live(ctx):
    if not _late_window_open(ctx["wp_rows"]):
        return None
    return scoring.clutch_finish(ctx["wp_rows"])


def comeback_erosion_live_ctx(ctx):
    return scoring.comeback_erosion_live(ctx["situational_plays"])


def upset_in_progress_ctx(ctx):
    return scoring.upset_in_progress(ctx["wp_now"], ctx["initial_home_wp"], ctx["home_rank"], ctx["away_rank"])


def team_profile_ctx(ctx):
    return scoring.team_profile(ctx["home_rank"], ctx["away_rank"])


def upset_risk_ctx(ctx):
    return scoring.upset_risk(ctx["initial_home_wp"], ctx["home_rank"], ctx["away_rank"])


LIVE_SO_FAR_METRICS = [
    {"name": "wp_volatility_rate",    "fn": wp_volatility_rate,        "weight": 1.0, "cap": scoring.MAX_VOLATILITY},
    {"name": "lead_change_rate",      "fn": lead_change_rate,          "weight": 1.0, "cap": scoring.MAX_LEAD_CHANGES},
    {"name": "comeback_erosion_live", "fn": comeback_erosion_live_ctx, "weight": 1.0, "cap": None},
    {"name": "upset_in_progress",     "fn": upset_in_progress_ctx,     "weight": 1.0, "cap": MAX_UPSET_IN_PROGRESS},
    {"name": "team_profile",          "fn": team_profile_ctx,          "weight": 1.0, "cap": scoring.MAX_TEAM_PROFILE},
    {"name": "upset_risk",            "fn": upset_risk_ctx,            "weight": 0.5, "cap": None},
    {"name": "late_volatility_rate",  "fn": late_volatility_rate,      "weight": 0.5, "cap": scoring.MAX_LATE_VOLATILITY},
    {"name": "clutch_finish",         "fn": clutch_finish_live,        "weight": 1.0, "cap": scoring.MAX_CLUTCH_FINISH},
]


# --- Half B: drama_from_here metric functions ---

def tension_now(ctx):
    if ctx["wp_now"] is None:
        return None
    return _closeness(ctx["wp_now"]) * _lateness_factor(ctx["elapsed"])


def upset_finish_potential(ctx):
    if ctx["initial_home_wp"] is None or ctx["wp_now"] is None:
        return None
    fav_home = ctx["initial_home_wp"] >= 0.5
    fav_wp_now = ctx["wp_now"] if fav_home else 1.0 - ctx["wp_now"]
    quality = max(scoring._rank_tier(ctx["home_rank"]), scoring._rank_tier(ctx["away_rank"]))
    return quality * (1.0 - fav_wp_now) * _lateness_factor(ctx["elapsed"])


def recent_volatility(ctx):
    recent = ctx["wp_rows"][-LIVE_RECENT_WINDOW:]
    if len(recent) < 2:
        return None
    return scoring.wp_volatility(recent)


def ot_live(ctx):
    period = ctx["period"]
    if period is None or period <= 4:
        return 0.0
    return min(1.0, 0.6 + 0.2 * (period - 4))


LIVE_FROM_HERE_METRICS = [
    {"name": "tension_now",            "fn": tension_now,             "weight": 2.0,  "cap": None},
    {"name": "upset_finish_potential", "fn": upset_finish_potential,  "weight": 1.0,  "cap": None},
    {"name": "recent_volatility",      "fn": recent_volatility,       "weight": 0.75, "cap": MAX_RECENT_VOLATILITY},
    {"name": "ot_live",                "fn": ot_live,                 "weight": 0.5,  "cap": None},
]

LIVE_METRIC_LABELS = {
    "tension_now": "a tight game",
    "upset_finish_potential": "upset potential",
    "recent_volatility": "recent swings",
    "ot_live": "overtime",
    "wp_volatility_rate": "back-and-forth action",
    "lead_change_rate": "frequent lead changes",
    "comeback_erosion_live": "a big comeback",
    "upset_in_progress": "an upset in progress",
    "team_profile": "a ranked matchup",
    "upset_risk": "a live upset bid",
    "late_volatility_rate": "late-game swings",
    "clutch_finish": "a clutch finish",
}

_ALL_LIVE_METRIC_NAMES = {m["name"] for m in LIVE_SO_FAR_METRICS} | {m["name"] for m in LIVE_FROM_HERE_METRICS}
_missing_labels = _ALL_LIVE_METRIC_NAMES - set(LIVE_METRIC_LABELS)
assert not _missing_labels, f"LIVE_METRIC_LABELS missing entries for: {_missing_labels}"


def _enrich_breakdown(metrics, breakdown):
    """composite_from() returns {raw, normalized, weighted} per metric; add
    `weight` and `applicable` so the breakdown is self-contained for storage
    (db.replace_live_metrics) and API/UI rendering without needing the
    registry alongside it."""
    return {
        m["name"]: {
            "raw": breakdown[m["name"]]["raw"],
            "normalized": breakdown[m["name"]]["normalized"],
            "weighted": breakdown[m["name"]]["weighted"],
            "weight": m["weight"],
            "applicable": breakdown[m["name"]]["raw"] is not None,
        }
        for m in metrics
    }


def headline_for(ctx, fh_bd):
    """One-line explanation generated from the top two contributing
    from_here metrics that clear LIVE_HEADLINE_MIN_NORMALIZED, driven by the
    registry (LIVE_METRIC_LABELS) rather than hardcoded to any one metric
    name -- retuning a weight or adding a metric updates the generated prose
    automatically. A metric below the bar drops out entirely rather than
    being backfilled by a weaker one, so a lopsided or early game can
    legitimately produce one metric's worth of headline, or none."""
    qualifying = (
        (name, v) for name, v in fh_bd.items()
        if v["applicable"] and v["normalized"] is not None
        and v["normalized"] >= LIVE_HEADLINE_MIN_NORMALIZED
    )
    top = sorted(qualifying, key=lambda kv: kv[1]["weighted"], reverse=True)[:2]
    if not top:
        return "Early — not much to go on yet"
    labels = [LIVE_METRIC_LABELS.get(name, name) for name, _ in top]
    if len(labels) == 1:
        return labels[0][0].upper() + labels[0][1:]
    return f"{labels[0][0].upper() + labels[0][1:]} + {labels[1]}"


def score_live(ctx, cycle_seq=None):
    """
    Compute the blended live score for one in-progress game.

    ctx: as returned by build_live_context().
    Returns a dict matching db.upsert_live_score's expected shape, plus a
    "halves" key ({"so_far": {...}, "from_here": {...}}) for
    db.replace_live_metrics / the API's per-half breakdown.
    """
    so_far_raw, so_bd_raw = scoring.composite_from(LIVE_SO_FAR_METRICS, ctx)
    from_here_raw, fh_bd_raw = scoring.composite_from(LIVE_FROM_HERE_METRICS, ctx)

    so_bd = _enrich_breakdown(LIVE_SO_FAR_METRICS, so_bd_raw)
    fh_bd = _enrich_breakdown(LIVE_FROM_HERE_METRICS, fh_bd_raw)

    drama_from_here = from_here_raw if from_here_raw is not None else 0.0

    if so_far_raw is None:
        live_score = drama_from_here
    else:
        live_score = LIVE_W_SO_FAR * so_far_raw + LIVE_W_FROM_HERE * drama_from_here

    so_far_weight = sum(v["weight"] for v in so_bd.values() if v["applicable"])
    from_here_weight = sum(v["weight"] for v in fh_bd.values() if v["applicable"])

    return {
        "live_score": live_score,
        "quality_so_far": so_far_raw,
        "drama_from_here": drama_from_here,
        "progress": ctx["progress"],
        "wp_now": ctx["wp_now"],
        "n_wp_rows": len(ctx["wp_rows"]),
        "so_far_weight": so_far_weight,
        "from_here_weight": from_here_weight,
        "headline": headline_for(ctx, fh_bd),
        "cycle_seq": cycle_seq,
        "halves": {"so_far": so_bd, "from_here": fh_bd},
    }


# --- Refresh loop ---

def _next_et_deadline(until_str):
    """Parse an "HH:MM" wall-clock time and return the next occurrence of it
    in US/Eastern, strictly after now -- so a deadline of "02:00" started at
    9am always means 2am *tomorrow*, never a deadline already in the past.
    ET (not system local time) so the same --live-until value means the same
    thing whether this runs on a Pacific laptop or a VPS set to UTC."""
    try:
        hour, minute = (int(p) for p in until_str.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ValueError(f"--live-until must be HH:MM (24-hour), got {until_str!r}") from None
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if deadline <= now:
        deadline += timedelta(days=1)
    return deadline


def _et_today_tomorrow():
    """ESPN's scoreboard `dates` param is interpreted in US/Eastern, and a
    game stays bucketed under its kickoff's ET calendar date for its entire
    duration (verified live) -- range end is exclusive (verified live: a
    "date-date" pair returns dates in [start, end), never including end).

    Span yesterday through tomorrow ET (3 days). Yesterday matters even
    though this runs every cycle: a 9pm PT Saturday kickoff still sits in
    ET-Saturday's bucket at 10pm ET, but this function is called anew every
    poll cycle, and ET midnight can pass *while that game is still live*
    (confirmed 2026-08-29/30: a 7pm PT UNLV/Memphis kickoff crossed ET
    midnight at halftime; "today" flipped to Sunday, the game's Saturday
    bucket dropped out of the window, and it stopped being polled --
    live_updated_at/status_detail froze at "Halftime" for hours). Including
    yesterday keeps a late kickoff in the window through its own ET-day
    rollover, not just through the poller's."""
    et = ZoneInfo("America/New_York")
    today = datetime.now(et).date()
    yesterday = today - timedelta(days=1)
    day_after_tomorrow = today + timedelta(days=2)
    return f"{yesterday:%Y%m%d}-{day_after_tomorrow:%Y%m%d}"


_SCHEDULE_SQL = """
    SELECT (SELECT COUNT(*) FROM games WHERE status_state = 'in') AS n_live,
           (SELECT MIN(game_date) FROM games
             WHERE status_state = 'pre' AND game_date >= ?)       AS next_kickoff
"""

# Same shape as _SCHEDULE_SQL's next_kickoff subquery, but floored at the
# start of today (ET) rather than now - LIVE_KICKOFF_GRACE_SECONDS.
#
# Why a second query: an all-day-TBD slate is stored at ET midnight (a
# placeholder, not a real kickoff -- see _schedule_interval's docstring).
# Once LIVE_KICKOFF_GRACE_SECONDS (6h) has passed since that stamp,
# next_kickoff's grace floor excludes it and jumps straight to the next
# already-known row -- which, on a slate that's entirely TBD, is often next
# week. The kickoff-lead/caffeinate-lead boundaries derived from
# next_kickoff can't rescue this: they're anchored to the placeholder's own
# (bogus, early-morning) timestamp, so they always fire *before* a
# same-day refresh could, not after. This query keeps "today" in view for
# the week-anchor/day-of refresh wakes specifically, independent of
# whether today's own row has aged out of the grace floor used for
# entering fast cadence.
_NEXT_ANCHOR_SQL = """
    SELECT MIN(game_date) AS anchor_kickoff FROM games
     WHERE status_state = 'pre' AND game_date >= ?
"""

# Only ever queried when _NEXT_ANCHOR_SQL also comes up empty (no 'pre' row
# anywhere from today onward) -- not the hot path every cycle takes -- so
# unlike the two queries above this deliberately isn't covered by
# idx_games_state_date. A full scan there is fine.
_LATEST_GAME_SQL = "SELECT season_type, game_date FROM games ORDER BY game_date DESC LIMIT 1"

_ET = ZoneInfo("America/New_York")


def _week_tuesday(local_date):
    """Date of the Tuesday beginning the Tue->Mon CFB week containing
    local_date. Same weekday math as serve.py's _tuesday_window (that
    function's docstring has the supporting evidence: confirmed against 4
    seasons of data that a CFB week's earliest game is never a Sun/Mon) --
    shared here so the live poller's week boundary and /api/slate's agree."""
    tue_offset = (local_date.weekday() - 1) % 7
    return local_date - timedelta(days=tue_offset)


def _et_anchor(local_date, hour=LIVE_WEEK_ANCHOR_HOUR_ET):
    """`hour`:00 ET on local_date, converted to UTC."""
    return datetime(local_date.year, local_date.month, local_date.day, hour, tzinfo=_ET).astimezone(timezone.utc)


def _schedule_interval(conn, now=None):
    """
    How long to sleep before the next poll cycle, and whether to hold an
    idle-sleep assertion, derived from the kickoff times in `games`.

    Returns (seconds, hold_awake, reason). `reason` is for the log line --
    an unattended always-on poller needs its cadence decisions to be
    legible after the fact.

    States:
      - anything status_state='in' -> LIVE_INTERVAL_SECONDS
      - next scheduled kickoff within [now - LIVE_KICKOFF_GRACE_SECONDS,
        now + LIVE_KICKOFF_LEAD_SECONDS] -> LIVE_INTERVAL_SECONDS
      - otherwise sleep to the *earliest future* of:
          * LIVE_KICKOFF_LEAD_SECONDS before that (grace-floored)
            next_kickoff, if one exists (enter fast cadence in time)
          * LIVE_CAFFEINATE_LEAD_SECONDS before it (grab the idle-sleep
            assertion exactly on time, not up to LIVE_INTERVAL_SECONDS late)
          * LIVE_WEEK_ANCHOR_HOUR_ET ET on the Tuesday beginning the CFB
            game week of the earliest 'pre' row from today onward (a
            *separate*, ungated query -- see _NEXT_ANCHOR_SQL -- so it
            still refreshes a TBD-placeholder slate that has aged out of
            the grace floor above). Bounded at <=7d by construction since
            it's derived from an actual known row, not a floating timer --
            an offseason "next game" months out collapses this to one wake
            before the opener's week, not a recurring heartbeat.
          * LIVE_WEEK_ANCHOR_HOUR_ET ET on that same row's own day
            (backstop in case the week-anchor refresh didn't resolve it)
        If every one of those has already passed -- today's own refresh
        window is behind us but the game still hasn't gone 'in' -- falls
        back to LIVE_INTERVAL_SECONDS so it keeps checking through the rest
        of the day rather than jumping to next week.
      - no 'pre' row anywhere from today onward: LIVE_BLIND_BACKSTOP_SECONDS
        if the most recent known game is a regular-season game within
        LIVE_BLIND_RECENT_GAME_DAYS days (the conference-championship ->
        first-bowl gap, when the postseason hasn't been discovered yet),
        else LIVE_NO_SCHEDULE_BACKSTOP_SECONDS (nothing scheduled at all --
        e.g. offseason with next season not yet discovered; recovering
        from this is `just discover` + `just live-now`, not a timer).

    The grace floor is what collapses the first two cases into a single
    MIN(): the earliest kickoff at or after `now - grace` is either already
    inside the active window or it *is* the next one to wait for. It also
    quietly excludes every past season's rows, which can never re-enter the
    window no matter how long the process runs.

    _SCHEDULE_SQL's two subqueries are covering seeks on
    idx_games_state_date. `now` is injectable so this is testable without
    waiting for a real kickoff.
    """
    now = now or datetime.now(timezone.utc)
    floor = (now - timedelta(seconds=LIVE_KICKOFF_GRACE_SECONDS)).strftime(GAME_DATE_FMT)
    row = conn.execute(_SCHEDULE_SQL, (floor,)).fetchone()
    n_live, next_kickoff = row["n_live"], row["next_kickoff"]

    if n_live:
        return float(LIVE_INTERVAL_SECONDS), True, f"{n_live} game(s) in progress"

    kick = until = None
    if next_kickoff is not None:
        kick = datetime.strptime(next_kickoff, GAME_DATE_FMT).replace(tzinfo=timezone.utc)
        until = (kick - now).total_seconds()
        if until <= LIVE_KICKOFF_LEAD_SECONDS:
            # Includes until < 0: a game the scoreboard still calls 'pre'
            # whose scheduled kickoff has passed but is inside the grace
            # window.
            return float(LIVE_INTERVAL_SECONDS), True, f"kickoff {next_kickoff} ({until / 60:+.0f} min)"

    today_floor = _et_anchor(now.astimezone(_ET).date(), hour=0).strftime(GAME_DATE_FMT)
    anchor_kickoff = conn.execute(_NEXT_ANCHOR_SQL, (today_floor,)).fetchone()["anchor_kickoff"]

    if anchor_kickoff is None:
        latest = conn.execute(_LATEST_GAME_SQL).fetchone()
        if latest and latest["game_date"] and latest["season_type"] == 2:
            latest_dt = datetime.strptime(latest["game_date"], GAME_DATE_FMT).replace(tzinfo=timezone.utc)
            if (now - latest_dt).total_seconds() <= LIVE_BLIND_RECENT_GAME_DAYS * 86400:
                return float(LIVE_BLIND_BACKSTOP_SECONDS), False, "postseason not yet discovered"
        return float(LIVE_NO_SCHEDULE_BACKSTOP_SECONDS), False, "no scheduled kickoff on record"

    anchor_et_date = datetime.strptime(anchor_kickoff, GAME_DATE_FMT).replace(tzinfo=timezone.utc) \
        .astimezone(_ET).date()
    candidates = {
        "week anchor": _et_anchor(_week_tuesday(anchor_et_date)),
        "day-of refresh": _et_anchor(anchor_et_date),
    }
    if kick is not None:
        # Always future here: until > LEAD was just confirmed above.
        candidates["kickoff lead"] = kick - timedelta(seconds=LIVE_KICKOFF_LEAD_SECONDS)
        candidates["caffeinate lead"] = kick - timedelta(seconds=LIVE_CAFFEINATE_LEAD_SECONDS)

    future = {label: dt for label, dt in candidates.items() if dt > now}
    if not future:
        # Today's own refresh window has already passed but this game
        # still hasn't gone 'in' -- keep checking through the rest of
        # today rather than falling through to next week's anchor.
        return float(LIVE_INTERVAL_SECONDS), True, f"{anchor_et_date} refresh window passed, still unresolved"

    label, wake_at = min(future.items(), key=lambda item: item[1])
    # Floored at LIVE_INTERVAL_SECONDS so a boundary 61s out can't produce
    # a 1-second sleep and a wasted request.
    sleep_for = max(float(LIVE_INTERVAL_SECONDS), (wake_at - now).total_seconds())
    hold_awake = until is not None and until <= LIVE_CAFFEINATE_LEAD_SECONDS
    # Describe whichever boundary actually won, not just whether a credible
    # next_kickoff exists -- anchor_et_date can differ from next_kickoff's
    # own date (the TBD-placeholder-past-grace case), so crediting the wake
    # to next_kickoff whenever one happens to be present would mislabel it.
    # wake_at itself is named explicitly (not just "waking for {label}") so
    # a week-anchor/day-of wake -- which fires *ahead of* anchor_et_date,
    # to refresh it -- doesn't read as though it wakes on that date itself.
    reason = (
        f"next kickoff {next_kickoff} in {until / 3600:.1f}h (waking for {label})"
        if label in ("kickoff lead", "caffeinate lead")
        else f"{label} {wake_at.strftime(GAME_DATE_FMT)} for {anchor_et_date}'s schedule"
    )
    return sleep_for, hold_awake, reason


def _acquire_lock():
    """
    Exclusive, kernel-enforced lock via flock on an fd held for the process
    lifetime -- not a PID-liveness check on a lock *file*. The old approach
    (os.path.exists then open-for-write, with staleness decided by
    os.kill(pid, 0)) had a TOCTOU between the exists-check and the write,
    and only reclaimed on ProcessLookupError -- so a SIGKILL whose PID got
    reused would wedge the lock permanently, and under the live plist's
    KeepAlive that becomes an infinite launchd-throttled crash loop. flock
    is released by the kernel on *any* process death, including SIGKILL, so
    that failure class can't happen. The PID is still written into the file
    for human diagnostics only -- it plays no role in the locking itself.
    """
    os.makedirs(os.path.dirname(LOCK_PATH) or ".", exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        existing = ""
        try:
            with open(LOCK_PATH) as f:
                existing = f.read().strip()
        except OSError:
            pass
        raise RuntimeError(
            f"{LOCK_PATH} is held by pid {existing or '?'} -- the live poller is already running. "
            f"Use `just stop-live` first, or `--live-dry-run` (which takes no lock)."
        ) from None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)
    return fd


def _release_lock(fd):
    # Deliberately does not unlink LOCK_PATH: flock+unlink has its own
    # classic race (a process that opens and locks the file between our
    # close() and the remove() would have its lock silently orphaned once
    # the path is deleted and later recreated under a new inode). Closing
    # the fd alone releases the flock; the file persists holding whichever
    # pid last ran, informational only, and gets truncated and rewritten by
    # the next _acquire_lock.
    try:
        os.close(fd)
    except OSError:
        pass


def _sync_caffeinate(proc, want):
    """
    Hold or drop a macOS idle-sleep assertion, as a child `caffeinate -i`.

    Moved out of the launchd plist (where it wrapped the whole process
    unconditionally) because the poller now runs continuously: an
    unconditional assertion would mean the machine never idle-sleeps again,
    a real regression against the old ~14h x 3 days/week window. Asserted
    only while games are live or close enough to kickoff (see
    _schedule_interval's LIVE_CAFFEINATE_LEAD_SECONDS).

    `-w <our pid>` is the dead-man's switch: the `finally` in run_forever
    and the signal handlers cover clean exits, but a SIGKILL leaves nothing
    to run cleanup, and a leaked caffeinate would silently pin the machine
    awake forever. With -w, caffeinate notices our pid is gone and releases
    the assertion on its own.

    No-ops off darwin -- Phase 2 moves this to a Linux VPS, which has no
    sleep to prevent. Returns the (possibly new, possibly None) handle;
    call it as `caffeinate = _sync_caffeinate(caffeinate, want)`.
    """
    if sys.platform != "darwin" or not os.path.exists(CAFFEINATE_PATH):
        return None

    if want:
        if proc is not None and proc.poll() is None:
            return proc  # already held; poll() also reaps a dead child
        try:
            proc = subprocess.Popen(
                [CAFFEINATE_PATH, "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info("live: holding idle-sleep assertion (caffeinate pid %d)", proc.pid)
            return proc
        except OSError:
            logger.exception("live: could not start caffeinate -- continuing without an assertion")
            return None

    if proc is None:
        return None
    logger.info("live: releasing idle-sleep assertion (caffeinate pid %d)", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    return None


def _sleep_until(wake_at, stop, deadline_passed):
    """
    Sleep in LIVE_SLEEP_SLICE_SECONDS slices until the wall-clock time
    `wake_at` (a time.time()-style epoch timestamp), or until `stop()`/
    `deadline_passed()` says to give up early.

    Deliberately driven off time.time(), not an iteration counter: on
    Darwin, time.sleep() is backed by a clock that does not advance across
    system suspend, so counting iterations would carry the remaining sleep
    budget *across* a lid-close and delay the first poll after wake by up
    to a full idle interval -- exactly the window a 7pm kickoff falls into.
    time.time() advances across suspend, so a wake past wake_at polls on
    the very next slice instead of waiting out a stale budget.

    That wall-clock read has its own failure mode though: a clock that
    jumps *backward* mid-sleep (NTP correction, manual change, VM restore)
    inflates `wake_at - time.time()`, and with sleeps now running up to
    ~7 days (or longer, offseason) that could otherwise oversleep well past
    `wake_at`. `budget`, fixed once before the loop starts, caps `remaining`
    at the sleep's originally-computed length -- a backward jump can make
    this loop restart the sleep from the top, but never exceed the length
    it was already going to run.

    Extracted as its own function so the wall-clock property is directly
    testable (fake time.time()/time.sleep()) rather than only inferable
    from reading run_forever's loop.
    """
    budget = max(0.0, wake_at - time.time())
    while not stop() and not deadline_passed():
        remaining = min(wake_at - time.time(), budget)
        if remaining <= 0:
            return
        time.sleep(min(LIVE_SLEEP_SLICE_SECONDS, remaining))


def _tier2_priority(conn):
    """Order this cycle's live games by how overdue a WP refresh is, most
    overdue first. `urgency = staleness_seconds - LIVE_MAX_STALENESS_SECONDS`,
    forced to +inf for a game deep enough into the 4th quarter that missing
    a refresh would be the worst possible moment to be stale. A game with no
    live_scores row yet (never fetched) always sorts first."""
    rows = conn.execute("""
        SELECT g.game_id,
               (julianday('now') - julianday(ls.computed_at)) * 86400.0 AS staleness_seconds,
               ls.progress
        FROM games g LEFT JOIN live_scores ls ON ls.game_id = g.game_id
        WHERE g.status_state = 'in'
    """).fetchall()
    scored = []
    for r in rows:
        if r["staleness_seconds"] is None:
            urgency = float("inf")
        else:
            urgency = r["staleness_seconds"] - LIVE_MAX_STALENESS_SECONDS
            if (r["progress"] or 0.0) >= LIVE_ALWAYS_REFRESH_PROGRESS:
                urgency = float("inf")
        scored.append((urgency, r["game_id"]))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [gid for _, gid in scored]


def _process_live_game(conn, game_id, cycle_seq, mode="normal"):
    """
    Fetch this game's summary, update its WP series, and (re)score it.

    mode:
      "normal" -- persist WP rows to win_probability (incremental
                  INSERT OR IGNORE append), run compute_play_sequences, and
                  write live_scores/live_metrics/live_score_history. The
                  steady-state operating mode.
      "dry_run" -- fetch and parse only, write nothing at all, log a summary.
      "shadow" -- fetch and parse, score from the freshly-parsed rows held
                  only in memory for this cycle, write live_scores/
                  live_metrics/live_score_history -- but never
                  win_probability. Persisting live-tracked WP rows without
                  the completion-time DELETE (which shadow mode skips
                  entirely, see handle_completions) would let
                  upsert_win_probability's INSERT OR IGNORE permanently fix
                  a play's period/clock/sequence at whatever it was mid-live,
                  uncorrectable by a later real fetch -- exactly the
                  provenance-split risk the hard delete at completion exists
                  to prevent. Re-parsing the full summary from scratch every
                  cycle is wasteful but shadow mode is a bounded validation
                  exercise, not the steady state.
    """
    game_row = conn.execute(
        "SELECT home_team_id, home_rank, away_rank, initial_home_wp, status_period, status_clock_seconds "
        "FROM games WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    if game_row is None:
        return

    try:
        summary = espn.fetch_game_summary(game_id)
        wp_rows, home_score, away_score, attendance, initial_home_wp = espn.parse_summary_detail(summary)
        situational_plays = espn.extract_situational_plays(summary, game_row["home_team_id"])
    except Exception:
        logger.exception("live: failed to fetch/parse summary for %s", game_id)
        return

    if mode == "dry_run":
        logger.info("dry-run: %s -- would process %d WP rows", game_id, len(wp_rows))
        return

    if mode == "normal":
        with conn:
            if wp_rows:
                db.upsert_win_probability(conn, wp_rows)
            db.compute_play_sequences(conn, game_id=game_id)
            db.set_initial_home_wp(conn, game_id, initial_home_wp)

        fresh_wp = conn.execute(
            "SELECT home_win_pct, home_score, away_score, period_number, clock_seconds_elapsed "
            "FROM win_probability WHERE game_id = ? ORDER BY play_sequence, id",
            (game_id,),
        ).fetchall()
        counts = conn.execute(
            "SELECT COUNT(*), MAX(play_sequence) FROM win_probability WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        if fresh_wp and counts[1] != counts[0]:
            # compute_play_sequences just ran above -- this should be
            # unreachable, but forgetting that step once already cost an
            # entire season scored on raw insertion order. Refuse loudly
            # rather than score against an unordered series.
            logger.error(
                "live: play_sequence out of sync for %s (%s rows, max seq %s) -- refusing to score",
                game_id, counts[0], counts[1],
            )
            return
        ctx_iwp = conn.execute(
            "SELECT initial_home_wp FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()[0]
    else:  # shadow
        fresh_wp = wp_rows
        ctx_iwp = game_row["initial_home_wp"] if game_row["initial_home_wp"] is not None else initial_home_wp

    ctx = build_live_context(
        wp_rows=fresh_wp,
        situational_plays=situational_plays,
        home_rank=game_row["home_rank"], away_rank=game_row["away_rank"],
        initial_home_wp=ctx_iwp,
        status_period=game_row["status_period"], status_clock_seconds=game_row["status_clock_seconds"],
    )
    result = score_live(ctx, cycle_seq=cycle_seq)
    with conn:
        db.upsert_live_score(conn, game_id, result)
        db.replace_live_metrics(conn, game_id, result["halves"])
        db.append_live_history(conn, game_id, result)


def handle_completions(conn, game_ids, mode="normal"):
    """
    The in -> post transition for a batch of games the scoreboard now
    reports completed.

    Normal mode: hard-discards everything live-appended to win_probability
    for these games (upsert_win_probability's INSERT OR IGNORE means a play
    first seen mid-live can never be corrected in place -- see
    _process_live_game's docstring), re-fetches through the ordinary
    pipeline path so the result is byte-identical to a game that was only
    ever fetched after the fact, then scores the whole batch in one
    scoring.score_games() call (it runs apply_corrections() internally,
    which iterates the full corrections table on every invocation -- batch,
    don't call per-game).

    Shadow mode skips all of the above entirely -- no delete, no re-fetch,
    no scoring -- so first real-world exposure of this code path cannot
    touch the retrospective corpus (games.watchability_score, game_metrics).
    The game is simply left exactly as the normal (non-live) pipeline would
    have found it, with only its live_scores/live_metrics rows cleared.
    """
    if not game_ids:
        return

    if mode == "shadow":
        logger.info("live (shadow): %d game(s) completed, retrospective pipeline left untouched: %s",
                    len(game_ids), game_ids)
        with conn:
            for gid in game_ids:
                db.clear_live_score(conn, gid)
        return

    from pipeline import fetch_details  # local import: pipeline.py imports
                                         # src.live for CLI dispatch, so a
                                         # module-level import here would be
                                         # circular.

    for gid in game_ids:
        with conn:
            db.delete_win_probability(conn, gid)
        fetch_details(conn, [gid])
        db.compute_play_sequences(conn, game_id=gid)

    scoring.score_games(conn, game_ids=game_ids)

    with conn:
        for gid in game_ids:
            db.clear_live_score(conn, gid)

    logger.info("live: %d game(s) completed and scored: %s", len(game_ids), game_ids)


def reconcile_on_start(conn):
    """
    Converge state after a crash or restart, so an unattended restart
    doesn't need manual cleanup:

    - Any live_scores row whose game the DB already shows as completed
      (crashed mid-transition, or was completed while the poller was down)
      gets the normal completion transition.
    - Any completed game with detail_fetched=0 -- whether or not it was
      ever tracked live -- gets picked up by the normal pipeline. Covers
      both a crash mid-transition and a game that finished while the
      poller wasn't running at all.
    """
    stale_live = [r[0] for r in conn.execute(
        "SELECT ls.game_id FROM live_scores ls JOIN games g ON g.game_id = ls.game_id "
        "WHERE g.completed = 1"
    )]
    if stale_live:
        logger.info("live: reconcile -- %d already-completed game(s) still tracked live", len(stale_live))
        handle_completions(conn, stale_live)

    from pipeline import fetch_details
    unfetched = [r[0] for r in conn.execute(
        "SELECT game_id FROM games WHERE completed = 1 AND detail_fetched = 0"
    )]
    if unfetched:
        logger.info("live: reconcile -- %d completed game(s) never fetched", len(unfetched))
        fetch_details(conn, unfetched)
        for gid in unfetched:
            db.compute_play_sequences(conn, game_id=gid)
        scoring.score_games(conn, game_ids=unfetched)


def run_cycle(conn, cycle_seq, summary_budget=LIVE_SUMMARY_BUDGET, mode="normal", dates=None):
    """
    One poll cycle: Tier 1 (one scoreboard call covering the whole slate),
    completion detection, Tier 2 (budgeted per-game WP refresh). Returns
    {"elapsed": wall-clock seconds, "n_requests": int, "counts": {status_state: n}}
    -- run_forever uses "elapsed" to sleep the remainder of the interval
    rather than stacking a fixed sleep on top of it, and "n_requests"/
    "counts" to populate poller_state without re-deriving them.
    """
    t0 = time.monotonic()
    if dates is None:
        dates = _et_today_tomorrow()

    # cycle_seq tags every fetch_log row this cycle produces (Tier 1's
    # scoreboard call and every Tier 2 summary fetch below), so the Feed
    # page can group requests by cycle without threading cycle_seq through
    # espn.fetch_json's call sites individually.
    with fetchlog.context(cycle_seq=cycle_seq):
        games = espn.fetch_scoreboard_dates(dates)
        n_requests = 1

        if mode != "dry_run":
            with conn:
                for g in games:
                    db.upsert_game(conn, g)

        if mode != "dry_run":
            previously_live = set(db.live_game_ids(conn))
            now_completed = {g["game_id"] for g in games if g["completed"]}
            newly_completed = [gid for gid in previously_live if gid in now_completed]
            if newly_completed:
                handle_completions(conn, newly_completed, mode=mode)

        if mode == "dry_run":
            targets = [g["game_id"] for g in games if g["status_state"] == "in"][:summary_budget]
        else:
            targets = _tier2_priority(conn)[:summary_budget]

        for gid in targets:
            _process_live_game(conn, gid, cycle_seq, mode=mode)
            n_requests += 1

    elapsed = time.monotonic() - t0
    counts = {}
    for g in games:
        counts[g["status_state"]] = counts.get(g["status_state"], 0) + 1
    logger.info(
        "live: cycle %d | slate %d (%d in, %d post, %d pre) | %d req | %.1fs wall",
        cycle_seq, len(games), counts.get("in", 0), counts.get("post", 0), counts.get("pre", 0),
        n_requests, elapsed,
    )
    return {"elapsed": elapsed, "n_requests": n_requests, "counts": counts}


def run_forever(conn, interval=None, summary_budget=LIVE_SUMMARY_BUDGET,
                 once=False, mode="normal", dates=None, until=None):
    """
    The `--live` daemon entry point. Handles its own SIGINT/SIGTERM
    (finishes the current cycle, commits, releases the lock, exits cleanly)
    and single-writer discipline via a PID lock file -- both skipped in
    dry_run mode, which takes no lock and mutates nothing.

    `interval=None` (the default) means schedule-aware: the sleep between
    cycles is derived each iteration from _schedule_interval, which also
    decides whether to hold a caffeinate idle-sleep assertion. Passing a
    fixed `interval` (via --live-interval) disables that entirely -- fixed
    cadence, wake-lock held for the whole run -- which is also the
    documented fallback to the old always-poll behaviour if the
    schedule-aware path ever needs to be bypassed.

    `until`, if given, is an "HH:MM" ET wall-clock time (see
    _next_et_deadline); reaching it exits through the same clean path as a
    SIGTERM -- lets a scheduler start this at a fixed time and trust it to
    end its own window. Mostly useful for manual/foreground runs now that
    the plist itself runs unbounded.
    """
    stop = {"flag": False}
    deadline = _next_et_deadline(until) if until else None
    if deadline is not None:
        logger.info("live: --live-until %s -- will exit cleanly at %s", until, deadline.isoformat())

    def _handle_signal(signum, _frame):
        logger.info("live: received signal %d, finishing current cycle then exiting", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def _deadline_passed():
        return deadline is not None and datetime.now(deadline.tzinfo) >= deadline

    lock_fd = None
    caffeinate = None
    if mode != "dry_run":
        lock_fd = _acquire_lock()
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            reconcile_on_start(conn)
        except Exception:
            logger.exception("live: reconcile_on_start failed -- continuing anyway")
        # stopped_at explicitly cleared (not just left from a prior row) so a
        # restart after a clean shutdown doesn't read as still-stopped.
        fetchlog.record_poller_state(
            "live", pid=os.getpid(), mode=mode,
            started_at=fetchlog.now_iso(), stopped_at=None,
        )

    cycle_seq = 0
    try:
        while True:
            cycle_seq += 1
            cycle_result = None
            cycle_error = None
            try:
                cycle_result = run_cycle(conn, cycle_seq, summary_budget=summary_budget, mode=mode, dates=dates)
                elapsed = cycle_result["elapsed"]
            except Exception as exc:
                logger.exception("live: cycle %d failed -- continuing", cycle_seq)
                elapsed = 0.0
                cycle_error = f"{type(exc).__name__}: {exc}"

            if mode != "dry_run":
                counts = cycle_result["counts"] if cycle_result else {}
                fetchlog.record_poller_state(
                    "live", cycle_seq=cycle_seq,
                    last_cycle_at=fetchlog.now_iso(), last_cycle_ms=int(elapsed * 1000),
                    last_cycle_reqs=cycle_result["n_requests"] if cycle_result else None,
                    last_cycle_error=cycle_error,
                    slate_in=counts.get("in"), slate_post=counts.get("post"), slate_pre=counts.get("pre"),
                )

            if once or stop["flag"] or _deadline_passed():
                if deadline is not None and not stop["flag"] and not once:
                    logger.info("live: reached --live-until deadline, exiting cleanly")
                break

            if interval is not None:
                period, hold_awake, reason = float(interval), True, f"--live-interval {interval}"
            else:
                try:
                    period, hold_awake, reason = _schedule_interval(conn)
                except Exception:
                    logger.exception("live: schedule lookup failed -- falling back to %ds",
                                      LIVE_INTERVAL_SECONDS)
                    period, hold_awake, reason = float(LIVE_INTERVAL_SECONDS), True, "schedule lookup failed"

            caffeinate = _sync_caffeinate(caffeinate, hold_awake and mode != "dry_run")

            sleep_for = max(0.0, period - elapsed)
            if elapsed > period:
                logger.warning("live: cycle %d ran long (%.1fs of a %.0fs budget)", cycle_seq, elapsed, period)
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, (deadline - datetime.now(deadline.tzinfo)).total_seconds()))
            if period > LIVE_INTERVAL_SECONDS:
                logger.info("live: idle -- sleeping %.0fs (%s)", sleep_for, reason)

            if mode != "dry_run":
                wake_at_dt = datetime.now(timezone.utc) + timedelta(seconds=sleep_for)
                fetchlog.record_poller_state(
                    "live",
                    next_wake_at=wake_at_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    interval_seconds=period, interval_reason=reason, hold_awake=int(hold_awake),
                )

            wake_at = time.time() + sleep_for
            _sleep_until(wake_at, lambda: stop["flag"], _deadline_passed)
    finally:
        if mode != "dry_run":
            fetchlog.record_poller_state("live", stopped_at=fetchlog.now_iso())
        caffeinate = _sync_caffeinate(caffeinate, False)
        if lock_fd is not None:
            _release_lock(lock_fd)
