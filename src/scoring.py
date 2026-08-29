import sqlite3

from . import db, wp_baseline

# --- Normalization caps (tunable) ---
MAX_VOLATILITY = 10.0
MAX_LEAD_CHANGES = 14
MAX_TEAM_PROFILE = 1.5
MAX_LATE_VOLATILITY = 4.5

# --- "Close game" WP band ---
CLOSE_LOWER = 0.30
CLOSE_UPPER = 0.70

# --- Rank tiers for team_profile (AP-style 1-25 rank) ---
RANK_TIER_TOP5 = 1.0
RANK_TIER_TOP10 = 0.7
RANK_TIER_TOP25 = 0.4

# --- upset_risk power curve exponent ---
UPSET_RISK_POWER = 2.5

# --- late_volatility: period_number >= LATE_PERIOD_THRESHOLD counts as "late game" (4 = Q4 and any OT) ---
LATE_PERIOD_THRESHOLD = 4

# --- clutch_finish: decisive score within the final minutes of regulation ---
REGULATION_SECONDS = 3600
CLUTCH_FINISH_WINDOW_SECONDS = 300
MAX_CLUTCH_FINISH = 1.5
CLUTCH_FINISH_FIELD_GOAL_VALUE = 1.0
CLUTCH_FINISH_NON_FIELD_GOAL_VALUE = 1.5
# Credit scales linearly across the window: CLUTCH_FINISH_MIN_FRACTION at the
# window's start (5:00 left) up to 1.0 at the window's end (0:00 left). Chosen
# as a first pass -- worth revisiting with a non-linear curve or a piecewise
# slope (e.g. steeper in the final minute than in minutes 2-5) if a flat
# linear ramp under- or over-credits scores that land mid-window.
CLUTCH_FINISH_MIN_FRACTION = 0.20
# Any game that reaches overtime carries some late tension even without a
# qualifying swing in the window -- below both real-event tiers above
# (0.7/1.5 = 0.47 normalized), but above a flat zero.
CLUTCH_FINISH_OT_FLOOR = 0.7

# --- comeback_erosion: how far a side's coin-flip-normalized WP must have
# climbed before a later decline off it counts as eroding a real lead.
# Chosen empirically: a coinflip team up 14 at Q1 end implies ~79-83% by
# wp_baseline (verified against 82.4% actual win rate in 51 comparable
# historical games); 0.84 sits above that, requiring something closer to a
# genuine 3rd-quarter-or-later command of the game, not just a good
# Q1 lead. ---
COMEBACK_EROSION_THRESHOLD = 0.84

# --- UW rooting bias: flat bonus for a Washington Huskies loss. Deliberate,
# not a general watchability signal (plans/personal_notes/personal_notes.md:
# "extra credit for a UW loss (rooting bias, deliberate -- not a general
# watchability principle)") -- kept as a flat add-on rather than a METRICS
# entry so it isn't diluted by weighted-average normalization. ---
UW_TEAM_ID = "264"
UW_LOSS_BONUS = 0.07


# --- Metric functions ---

def wp_volatility(wp_rows):
    """Sum of absolute WP deltas across the game."""
    wps = [r["home_win_pct"] for r in wp_rows]
    return sum(abs(wps[i + 1] - wps[i]) for i in range(len(wps) - 1))


def late_volatility(wp_rows):
    """
    Sum of absolute WP deltas restricted to the 4th quarter and any overtime
    (period_number >= LATE_PERIOD_THRESHOLD). Same shape as wp_volatility,
    windowed to reward drama that shows up specifically late in the game
    rather than spread evenly (or front-loaded) across the whole thing.
    """
    wps = [r["home_win_pct"] for r in wp_rows]
    periods = [r["period_number"] for r in wp_rows]
    total = 0.0
    for i in range(len(wps) - 1):
        p = periods[i + 1]
        if p is not None and p >= LATE_PERIOD_THRESHOLD:
            total += abs(wps[i + 1] - wps[i])
    return total


def clutch_finish(wp_rows):
    """
    Credit for the final CLUTCH_FINISH_WINDOW_SECONDS of regulation being
    genuinely live: a team taking the lead (breaking a tie or overcoming a
    prior deficit), OR the trailing/tied team tying the game *and that tie
    holding through the end of regulation* (i.e. it forces overtime). Both
    are worth more if the decisive score isn't a field goal, and both scale
    linearly by how late in the window the score landed -- a swing right at
    the window's start (5:00 left) earns CLUTCH_FINISH_MIN_FRACTION of the
    tier value, ramping up to the full value at 0:00 left.

    A score that pads a lead the scoring team already held doesn't count,
    and neither does a tie that gets broken again before regulation ends --
    that's not the game's actual final state, just a fleeting one (whatever
    happens afterward, including a subsequent go-ahead score, is evaluated
    on its own merits as it's encountered).

    Every game that reaches overtime gets at least CLUTCH_FINISH_OT_FLOOR,
    even with no qualifying swing in the window -- going to overtime at all
    means regulation ended unresolved, which is real tension even when the
    tying score happened earlier than the window.

    Field-goal detection uses the score delta alone (a made FG is exactly 3
    points; nothing else scores exactly 3) rather than fetching play-type
    data -- reuses the same non-decreasing score sanitization as
    lead_changes() to ignore ESPN score-field glitches, and the same
    3-state (home/away/tied) tracking, seeded tied like lead_changes().
    """
    home_score, away_score = 0, 0
    last_state = 0
    last_qualifying_delta = None
    last_qualifying_elapsed = None
    is_ot = any(r["period_number"] is not None and r["period_number"] > 4 for r in wp_rows)
    window_start = REGULATION_SECONDS - CLUTCH_FINISH_WINDOW_SECONDS

    for r in wp_rows:
        h, a = r["home_score"], r["away_score"]
        delta = None
        if h is not None and h > home_score:
            delta = h - home_score
        if h is not None and h >= home_score:
            home_score = h
        if a is not None and a > away_score:
            delta = a - away_score
        if a is not None and a >= away_score:
            away_score = a

        if home_score > away_score:
            state = 1
        elif away_score > home_score:
            state = -1
        else:
            state = 0

        if state != last_state:
            period, elapsed = r["period_number"], r["clock_seconds_elapsed"]
            in_window = (
                period == 4 and elapsed is not None
                and elapsed >= window_start
            )
            # A lead-take always qualifies if it's in the window. A
            # tie-transition only qualifies if the game actually went to
            # OT -- otherwise this tie was undone later in regulation and
            # isn't the game's real final state (that later transition,
            # win or tie, gets its own chance to qualify as it's reached).
            if in_window and (state != 0 or is_ot):
                last_qualifying_delta = delta
                last_qualifying_elapsed = elapsed
        last_state = state

    if last_qualifying_delta is not None:
        base = CLUTCH_FINISH_FIELD_GOAL_VALUE if last_qualifying_delta == 3 else CLUTCH_FINISH_NON_FIELD_GOAL_VALUE
        t = (last_qualifying_elapsed - window_start) / CLUTCH_FINISH_WINDOW_SECONDS
        t = max(0.0, min(1.0, t))
        fraction = CLUTCH_FINISH_MIN_FRACTION + (1.0 - CLUTCH_FINISH_MIN_FRACTION) * t
        return base * fraction
    if is_ot:
        return CLUTCH_FINISH_OT_FLOOR
    return 0.0


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




def upset_in_progress(current_home_wp, initial_home_wp, home_rank, away_rank):
    """How far the pregame favorite's win probability has already fallen from
    its opening line, scaled by the better-ranked team's tier (reuses
    _rank_tier so it agrees with team_profile/upset_risk on what "ranked" is
    worth).

    Distinct from upset_risk (pregame skew only, blind to what's actually
    happened) -- this tracks the favorite's in-game slide, and like
    comeback_erosion_live it doesn't require the upset to actually land: a
    favorite that slid from 85% to 40% and then recovered still gets credit
    for how real the threat was.

    Returns None (not 0.0) when the pregame line or current WP is unknown, so
    it drops out of the composite's denominator rather than penalizing the
    game -- same "not applicable" convention as clutch_finish/late_volatility.
    """
    if initial_home_wp is None or current_home_wp is None:
        return None
    fav_home = initial_home_wp >= 0.5
    pre = initial_home_wp if fav_home else 1.0 - initial_home_wp
    now = current_home_wp if fav_home else 1.0 - current_home_wp
    quality = max(_rank_tier(home_rank), _rank_tier(away_rank))
    return max(0.0, pre - now) * quality


def upset_risk(initial_home_wp, home_rank, away_rank):
    """
    How lopsided the pregame win probability was (0 = even matchup, 1 =
    near-certain outcome), scaled down when neither team was actually ranked.

    A skewed line between two unranked teams isn't a real "upset risk" in the
    way a ranked favorite nearly losing is — it just means one unranked team
    was somewhat better than another. Scaled by the better-ranked team's tier
    (same tiers as team_profile): 0 if neither team is ranked, up to 1.0 if a
    top-5 team is involved, regardless of which side was favored.

    Raised to UPSET_RISK_POWER so credit ramps up slowly for modest favorites
    (a 68/32 split reads as only mildly skewed) and accelerates only as the
    game approaches a near-lock — linear (power 1) over-credited ordinary
    ranked-vs-ranked favorites.
    """
    if initial_home_wp is None:
        return 0.0
    skew = abs(initial_home_wp - 0.5) * 2
    quality = max(_rank_tier(home_rank), _rank_tier(away_rank))
    return (skew ** UPSET_RISK_POWER) * quality


def _sanitized_score_events(wp_rows):
    """
    Collapse wp_rows to a chronological list of (clock_seconds_elapsed,
    score_diff) for each *real* score change, in two passes:

    1. Drop isolated spikes -- a distinct (home,away) tuple immediately
       followed by a LOWER one in either coordinate is noise that gets
       reverted, not a real score (confirmed example: an away score
       reading 13 -> 16 -> 13 for a single row). This is the failure mode
       the classic non-decreasing/running-max guard (used below, and in
       lead_changes()/clutch_finish()) actually makes WORSE, not better --
       it would lock in the bad high value as the new floor and reject the
       correct lower readings that follow. Iterated to a fixed point in
       case of adjacent spikes.
    2. Standard non-decreasing sanitization (defends against the OTHER
       failure mode, a row reverting to a stale LOWER value -- confirmed
       example: a score reading -1 for 14 consecutive rows), now that
       spikes are already gone.
    """
    distinct = []
    last = None
    for r in wp_rows:
        if r["clock_seconds_elapsed"] is None:
            continue
        cur = (r["home_score"], r["away_score"])
        if cur != last:
            distinct.append((r["clock_seconds_elapsed"], cur[0], cur[1]))
            last = cur

    changed = True
    while changed:
        changed = False
        cleaned = []
        i = 0
        while i < len(distinct):
            if i < len(distinct) - 1:
                _, h, a = distinct[i]
                _, h2, a2 = distinct[i + 1]
                if (h is not None and h2 is not None and h > h2) or \
                   (a is not None and a2 is not None and a > a2):
                    changed = True
                    i += 1
                    continue
            cleaned.append(distinct[i])
            i += 1
        distinct = cleaned

    events = []
    last = (None, None)
    home_max, away_max = 0, 0
    for elapsed, h, a in distinct:
        if h is not None and h >= home_max:
            home_max = h
        if a is not None and a >= away_max:
            away_max = a
        cur = (home_max, away_max)
        if cur != last:
            events.append((elapsed, cur[0] - cur[1]))
            last = cur
    return events


def _comeback_erosion_walk(wp_rows, credit_open_arc):
    """Shared arc-walk for comeback_erosion/comeback_erosion_live: segments
    _sanitized_score_events into "arcs" (stretches between lead changes),
    tracking each arc's coin-flip-normalized WP extreme (lo/hi) via
    wp_baseline.coinflip_wp_elapsed. See comeback_erosion's docstring for why
    coin-flip normalization and arc segmentation both matter.

    credit_open_arc controls whether the current (still-open, not yet ended
    by a lead change/tie) arc's own running lo/hi is also checked against the
    current point on every event, not just at the moment an arc ends -- see
    comeback_erosion_live's docstring.
    """
    events = _sanitized_score_events(wp_rows)
    if not events:
        return 0.0
    best = 0.0
    lo = hi = 0.5
    state = 0  # -1 away ahead, +1 home ahead, 0 tied
    for elapsed, sd in events:
        w = wp_baseline.coinflip_wp_elapsed(elapsed, sd)
        new_state = 1 if sd > 0 else (-1 if sd < 0 else 0)
        if new_state != state:
            if hi >= COMEBACK_EROSION_THRESHOLD:
                best = max(best, hi - w)
            if lo <= 1 - COMEBACK_EROSION_THRESHOLD:
                best = max(best, w - lo)
            lo = hi = w
            state = new_state
        else:
            lo = min(lo, w)
            hi = max(hi, w)
            if credit_open_arc:
                if hi >= COMEBACK_EROSION_THRESHOLD:
                    best = max(best, hi - w)
                if lo <= 1 - COMEBACK_EROSION_THRESHOLD:
                    best = max(best, w - lo)
    return best


def comeback_erosion(wp_rows):
    """
    Did a real, commanding lead get torn down -- credited once per "arc"
    (the stretch between lead changes), at the moment the arc ends, using
    that arc's own coin-flip-normalized WP extreme. Segmenting the game
    this way and crediting exactly once per arc is what stops a team from
    getting extra credit for continuing to blow a game open after they've
    already completed the comeback (confirmed case: SDSU@USU's real
    comeback completed around 0.39 when USU first retook the lead: crediting
    continued Q4 margin-building on top of that inflated it to 0.90 in an
    earlier, unsegmented version of this metric).

    "Commanding" is judged in coin-flip terms (wp_baseline.coinflip_wp_elapsed
    -- the pregame line forced to 50/50), not raw WP, so a heavy pregame
    favorite's WP being high because it was already expected to be doesn't
    count on its own (confirmed case: Alabama's 93% WP off a modest early
    lead against 9%-underdog FSU was mostly the pregame anchor, not a real
    lead -- coin-flip-normalized it never reaches COMEBACK_EROSION_THRESHOLD).

    A tie counts as full erosion of whoever was ahead, same as the
    opponent actually taking the lead -- checked explicitly, not just on
    transitions to the opposite side (an earlier version only checked on a
    flip to the other side and wrongly scored 0 for USC 30-Penn State 33,
    whose real drama was USC's lead getting fully erased into a tie before
    PSU won in OT).

    Uses elapsed-time directly (via coinflip_wp_elapsed), not quarter
    buckets, so it evaluates Q4/OT rows too -- extrapolating
    wp_baseline.ELAPSED_MODEL (fit on Q1-3 only) past its trained range,
    same accepted-but-unverified tradeoff as elsewhere in wp_baseline.

    Only credits an arc once it *ends* (a lead change or tie) -- an ongoing
    comeback attempt that hasn't yet flipped the game gets zero credit here,
    even in the final, still-open arc of a completed game. That's correct
    for the retrospective corpus (the outcome is known, so "erosion" that
    never actually happened isn't real erosion). See comeback_erosion_live
    for the in-progress counterpart that credits this case.
    """
    return _comeback_erosion_walk(wp_rows, credit_open_arc=False)


def comeback_erosion_live(wp_rows):
    """
    Live counterpart to comeback_erosion for a game still in progress: same
    coin-flip-normalized, arc-segmented, COMEBACK_EROSION_THRESHOLD-gated
    logic, except a material swing away from the current arc's own extreme
    is credited as soon as it happens, not only once a lead change or tie
    actually ends that arc.

    Replaces the earlier comeback_magnitude metric, which measured raw
    (non-coin-flip-normalized) WP drawup/drawdown over the whole game with no
    arc segmentation and no "commanding" threshold -- exposing it to the
    exact anti-pattern comeback_erosion was hardened against: a mild pregame
    favorite (e.g. 56%) that just builds a comfortable, never-threatened lead
    reads as a "big comeback" purely because raw WP climbs, even though the
    trailing team never had a lead or a real WP advantage to come back from
    (confirmed case: UVA 17-0 over NC State, comeback_magnitude=0.35 off a
    56% pregame line and a lead that was never really in question).

    Unlike comeback_erosion, an ongoing arc's own lo/hi is checked against
    the current point on every event (see _comeback_erosion_walk's
    credit_open_arc), not just at the arc's end -- the whole point of a live
    "so far" signal is to catch a real comeback attempt while it's still
    unresolved, per the explicit request that this not require consummation.
    A lead change or tie still resets lo/hi to start a fresh arc, so a team
    that completes a comeback and keeps running up the score gets no extra
    credit for that -- same protection as comeback_erosion, just also
    evaluated before an arc formally ends.
    """
    return _comeback_erosion_walk(wp_rows, credit_open_arc=True)


def uw_loss_bonus(home_team_id, away_team_id, home_score, away_score):
    """UW_LOSS_BONUS if Washington played and lost, else 0.0. Deliberately
    outside the METRICS/composite_from() weighted-average machinery -- see
    UW_LOSS_BONUS above -- so it's a flat add rather than one term diluted
    by every other metric's weight."""
    if home_score is None or away_score is None:
        return 0.0
    if home_team_id == UW_TEAM_ID:
        return UW_LOSS_BONUS if home_score < away_score else 0.0
    if away_team_id == UW_TEAM_ID:
        return UW_LOSS_BONUS if away_score < home_score else 0.0
    return 0.0


# --- Metric registry ---
# Each fn takes a context dict: {"wp_rows": [...], "home_rank": int|None,
# "away_rank": int|None, "initial_home_wp": float|None}

METRICS = [
    {"name": "wp_volatility",    "fn": lambda ctx: wp_volatility(ctx["wp_rows"]),                    "weight": 1.0, "cap": MAX_VOLATILITY},
    {"name": "lead_changes",     "fn": lambda ctx: lead_changes(ctx["wp_rows"]),                      "weight": 1.0, "cap": MAX_LEAD_CHANGES},
    {"name": "time_spent_close", "fn": lambda ctx: time_spent_close(ctx["wp_rows"]),                  "weight": 0.5, "cap": None},
    {"name": "team_profile",     "fn": lambda ctx: team_profile(ctx["home_rank"], ctx["away_rank"]),  "weight": 1.0, "cap": MAX_TEAM_PROFILE},
    {"name": "upset_risk",       "fn": lambda ctx: upset_risk(ctx["initial_home_wp"], ctx["home_rank"], ctx["away_rank"]), "weight": 1.0, "cap": None},
    {"name": "late_volatility",  "fn": lambda ctx: late_volatility(ctx["wp_rows"]),                   "weight": 0.5, "cap": MAX_LATE_VOLATILITY},
    {"name": "clutch_finish",    "fn": lambda ctx: clutch_finish(ctx["wp_rows"]),                      "weight": 1.0, "cap": MAX_CLUTCH_FINISH},
    {"name": "comeback_erosion", "fn": lambda ctx: comeback_erosion(ctx["wp_rows"]),                    "weight": 1.0, "cap": None},
]

METRICS_BY_NAME = {m["name"]: m for m in METRICS}


def _normalize(metric_name, raw):
    """Apply a metric's current cap to a raw value, same rule score_game() uses."""
    cap = METRICS_BY_NAME[metric_name]["cap"]
    if cap is None:
        return min(raw, 1.0)
    return min(raw / cap, 1.0)


# --- Scoring ---

def composite_from(metrics, context):
    """
    Shared composite algebra: composite = sum(min(raw/cap, 1) * weight) / sum(weight),
    over the metrics whose fn returned a non-None raw value. Used by score_game()
    against the retrospective METRICS registry, and by src/live.py against the
    live LIVE_SO_FAR_METRICS / LIVE_FROM_HERE_METRICS registries -- same rules,
    different metric lists.

    metrics: list of {"name", "fn", "weight", "cap"} dicts (fn takes `context`
    and returns a raw value or None).
    Returns (composite: float | None, breakdown: dict). breakdown keys: metric
    name -> {raw, normalized, weighted}. composite is None when every metric
    returned None (zero applicable weight) -- callers decide how to handle
    that rather than dividing by zero.

    A metric fn may return None to signal "not applicable" for this context
    (e.g. clutch_finish for a game that went to OT, or any live "so far"
    metric before its definition window has opened) -- such metrics are
    excluded from both the numerator and the weight total, rather than scored
    0, so they don't structurally penalize contexts they don't apply to.
    """
    breakdown = {}
    total_weight = 0.0
    composite = 0.0

    for m in metrics:
        raw = m["fn"](context)
        if raw is None:
            breakdown[m["name"]] = {"raw": None, "normalized": None, "weighted": None}
            continue
        if m["cap"] is None:
            normalized = min(raw, 1.0)
        else:
            normalized = min(raw / m["cap"], 1.0)
        weighted = normalized * m["weight"]
        breakdown[m["name"]] = {"raw": raw, "normalized": normalized, "weighted": weighted}
        total_weight += m["weight"]
        composite += weighted

    if total_weight == 0.0:
        return None, breakdown
    return composite / total_weight, breakdown


def score_game(context):
    """
    Compute composite watchability score for a single game, against the
    retrospective METRICS registry.

    context: {"wp_rows": [...], "home_rank": int|None, "away_rank": int|None,
              "initial_home_wp": float|None, "home_team_id": str|None,
              "away_team_id": str|None, "home_score": int|None,
              "away_score": int|None}
    Returns (composite: float, breakdown: dict). breakdown keys: metric name
    -> {raw, normalized, weighted}. composite includes the flat UW_LOSS_BONUS
    rooting-bias add-on (see uw_loss_bonus()) on top of the METRICS
    weighted average -- not reflected in breakdown, which stays scoped to
    the registry's own metrics.

    In practice every retrospective game has at least team_profile/upset_risk
    applicable (they need no WP data), so total_weight is never 0 here -- the
    0.0 fallback below exists for defensiveness (composite_from() itself no
    longer divides by zero) and preserves this function's prior behavior of
    always returning a float rather than pushing None-handling onto callers
    that don't expect it (score_games(), apply_corrections()).
    """
    composite, breakdown = composite_from(METRICS, context)
    composite = composite if composite is not None else 0.0
    composite += uw_loss_bonus(
        context.get("home_team_id"), context.get("away_team_id"),
        context.get("home_score"), context.get("away_score"),
    )
    return composite, breakdown


def recompute_composite(conn, game_id):
    """
    Recompute a game's composite watchability score purely from its already-
    stored game_metrics rows (no wp_rows re-fetch), applying each metric's
    current weight/cap. A metric with no stored row (went to OT, etc.) is
    excluded from both the numerator and the weight total, matching
    score_game()'s "not applicable" handling. Used after manual corrections,
    where only one metric's value changed and the rest should be reused as-is.

    Also re-applies the flat UW_LOSS_BONUS rooting-bias add-on (see
    uw_loss_bonus()) by looking the game's teams/score back up from `games`
    -- the bonus isn't stored as a game_metrics row, so it has to be
    re-derived here rather than reused from a stored value.
    """
    total_weight = 0.0
    composite = 0.0
    for row in conn.execute(
        "SELECT metric_name, norm_value FROM game_metrics WHERE game_id = ?", (game_id,)
    ):
        m = METRICS_BY_NAME.get(row["metric_name"])
        if m is None:
            continue
        composite += row["norm_value"] * m["weight"]
        total_weight += m["weight"]
    base = composite / total_weight if total_weight else 0.0

    game = conn.execute(
        "SELECT home_team_id, away_team_id, home_score, away_score FROM games WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    if game is None:
        return base
    return base + uw_loss_bonus(
        game["home_team_id"], game["away_team_id"], game["home_score"], game["away_score"]
    )


def _apply_one_correction(conn, game_id, metric_name, raw):
    """Shared by apply_corrections()'s two sources (hand-written
    corrections.py entries and auto-trusted Fox reconciliation diffs).
    Returns False (skip) if the game hasn't been scored yet."""
    exists = conn.execute(
        "SELECT 1 FROM games WHERE game_id = ? AND watchability_score IS NOT NULL", (game_id,)
    ).fetchone()
    if not exists:
        return False
    norm = _normalize(metric_name, raw)
    conn.execute(
        """
        INSERT INTO game_metrics (game_id, metric_name, raw_value, norm_value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(game_id, metric_name) DO UPDATE SET
            raw_value  = excluded.raw_value,
            norm_value = excluded.norm_value
        """,
        (game_id, metric_name, raw, norm),
    )
    new_composite = recompute_composite(conn, game_id)
    conn.execute("UPDATE games SET watchability_score = ? WHERE game_id = ?", (new_composite, game_id))
    return True


def apply_corrections(conn):
    """
    Applies two sources of overrides to already-scored games, recomputing
    norm_value from each metric's current cap and the game's overall
    watchability_score:

    1. corrections.CORRECTIONS -- hand-verified overrides from individually
       investigated games (e.g. UTEP@NMSU, USF@FLA), kept as a historical/
       always-on record.
    2. fox_score_corrections, tier='diff' rows -- Fox-derived values, applied
       automatically. Trusted by default: reconciliation only ever writes a
       'diff' row for a game whose own Fox ladder already reconciles with
       the official box score (see fox_reconcile.diff_game -- anything where
       Fox's own data doesn't check out is tier='unusable' and gets no row
       here at all, leaving ESPN's original value untouched). 'diff' rows
       are the visible, queryable record of every ESPN<->Fox disagreement
       being corrected -- SELECT * FROM fox_score_corrections to review any
       of them by hand.

    Silently skips a correction if its game hasn't been scored yet. Runs
    automatically at the end of score_games() so corrections survive a full
    rescore/re-pull rather than needing to be reapplied by hand.
    """
    from . import corrections as corrections_module

    applied = 0
    for c in corrections_module.CORRECTIONS:
        if _apply_one_correction(conn, c["game_id"], c["metric_name"], c["raw_value"]):
            applied += 1

    fox_rows = conn.execute(
        "SELECT game_id, metric_name, fox_value FROM fox_score_corrections WHERE tier = 'diff'"
    ).fetchall()
    for r in fox_rows:
        if _apply_one_correction(conn, r["game_id"], r["metric_name"], r["fox_value"]):
            applied += 1

    conn.commit()
    if applied:
        print(f"Applied {applied} correction(s) ({len(corrections_module.CORRECTIONS)} manual, "
              f"{len(fox_rows)} Fox-derived).")
    return applied


def score_games(conn, game_ids=None, rescore=False):
    """
    Phase 3: score all eligible completed games and write watchability_score to DB.

    Eligible = completed=1 AND detail_fetched=1 AND watchability_score IS NULL
    (unless rescore=True, which drops the NULL check).
    """
    if game_ids:
        placeholders = ",".join("?" * len(game_ids))
        base = (
            f"SELECT g.game_id, g.away_team_abbr, g.home_team_abbr, g.home_rank, g.away_rank, g.initial_home_wp, "
            f"g.home_team_id, g.away_team_id, g.home_score, g.away_score "
            f"FROM games g "
            f"WHERE g.completed = 1 AND g.detail_fetched = 1 "
            f"AND g.game_id IN ({placeholders})"
        )
        params = list(game_ids)
    else:
        base = (
            "SELECT g.game_id, g.away_team_abbr, g.home_team_abbr, g.home_rank, g.away_rank, g.initial_home_wp, "
            "g.home_team_id, g.away_team_id, g.home_score, g.away_score "
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
            "SELECT home_win_pct, home_score, away_score, period_number, clock_seconds_elapsed "
            "FROM win_probability WHERE game_id = ? AND period_number IS NOT NULL ORDER BY play_sequence, id",
            (game_id,),
        ).fetchall()

        if not wp_rows:
            print(f"[{i}/{n}] {label} — no WP data, skipping.")
            continue

        context = {
            "wp_rows": wp_rows,
            "home_rank": row["home_rank"],
            "away_rank": row["away_rank"],
            "initial_home_wp": row["initial_home_wp"],
            "home_team_id": row["home_team_id"],
            "away_team_id": row["away_team_id"],
            "home_score": row["home_score"],
            "away_score": row["away_score"],
        }
        composite, breakdown = score_game(context)
        db.update_watchability_score(conn, game_id, composite)
        db.upsert_game_metrics(conn, game_id, breakdown)

        parts = "  ".join(
            f"{name}: {v['raw']:.3f}→{v['normalized']:.3f}" if v["raw"] is not None else f"{name}: n/a"
            for name, v in breakdown.items()
        )
        print(f"[{i}/{n}] {label}  score={composite:.4f}  [{parts}]")

    conn.commit()
    print(f"Scoring complete: {n} games scored.")

    apply_corrections(conn)
