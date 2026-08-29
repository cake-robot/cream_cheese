"""
Offline replay harness for the live scorer (src/live.py).

Re-runs score_live() over a completed game's already-stored win_probability
series at every prefix, with no network access -- this is how the live
model's weights/caps/thresholds get tuned (see replay_curve_ascii) and how
its properties get asserted against real games (see tests/test_live_scoring.py),
months before an actual live game exists to test against.

Honesty check, stated once here rather than repeated at every call site: a
replay prefix is NOT a byte-identical stand-in for a real live payload.
ESPN revises WP values retroactively after the fact, and a genuinely live
row carries `drives.current` provenance a stored/completed row doesn't. This
harness validates the *scorer* -- given a certain (period, clock, score, WP
series) state, does score_live() produce sensible output. It does not
validate the *fetcher* (src/espn.py's live parsing) or the poll loop
(src/live.py's daemon) -- see tests/fixtures/live_*.json and --live-dry-run
for that half of verification.
"""

from . import live

_BLOCKS = " ▁▂▃▄▅▆▇█"


def _status_from_wp_row(row):
    """
    Derive (period, clock_remaining) from a stored WP row's own
    period_number/clock_seconds_elapsed, standing in for what the live
    poller would normally get from the scoreboard's status block directly.
    This inversion is replay-only machinery -- the real poller never derives
    a clock this way, it reads status.period/status.clock verbatim.

    OT periods return remaining=None (unknown): CFB overtime doesn't run a
    continuously-decreasing clock the way regulation does, and the stored
    per-play elapsed value is a synthetic counter (espn.py's ot_period_counter),
    not a real remaining-time. live.build_live_context handles a None
    clock_remaining fine -- it only feeds progress_of()/elapsed derivation.
    """
    period = row["period_number"]
    elapsed = row["clock_seconds_elapsed"]
    if period is None or elapsed is None:
        return None, None
    if period <= 4:
        remaining = max(0, 900 - (elapsed - (period - 1) * 900))
    else:
        remaining = None
    return period, remaining


def replay_game(conn, game_id, step=1):
    """
    Re-run the live scorer over game_id's stored WP series at every prefix
    (or every `step`'th prefix, always including the final row).

    Returns a list of dicts: {i, n, progress, period, wp_now, live_score,
    quality_so_far, drama_from_here, headline}.
    """
    row = conn.execute(
        "SELECT home_rank, away_rank, initial_home_wp FROM games WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no such game: {game_id}")

    all_wp = conn.execute(
        "SELECT home_win_pct, home_score, away_score, period_number, clock_seconds_elapsed "
        "FROM win_probability WHERE game_id = ? ORDER BY play_sequence, id",
        (game_id,),
    ).fetchall()
    if not all_wp:
        raise ValueError(f"no win_probability rows for game: {game_id}")

    n = len(all_wp)

    def _score_prefix(i):
        prefix = all_wp[:i]
        last = prefix[-1]
        period, remaining = _status_from_wp_row(last)
        ctx = live.build_live_context(
            wp_rows=prefix,
            home_rank=row["home_rank"], away_rank=row["away_rank"],
            initial_home_wp=row["initial_home_wp"],
            status_period=period, status_clock_seconds=remaining,
        )
        result = live.score_live(ctx, cycle_seq=i)
        return {
            "i": i, "n": n, "progress": ctx["progress"], "period": ctx["period"],
            "wp_now": ctx["wp_now"], "live_score": result["live_score"],
            "quality_so_far": result["quality_so_far"],
            "drama_from_here": result["drama_from_here"], "headline": result["headline"],
        }

    indices = list(range(1, n + 1, step))
    if indices[-1] != n:
        indices.append(n)
    return [_score_prefix(i) for i in indices]


def _sparkline(values, width):
    """Bin `values` into `width` columns in prefix order, taking the max
    within each bin (a brief spike shouldn't get smoothed away), rendered as
    a block-character sparkline."""
    n = len(values)
    if n == 0:
        return " " * width
    cols = []
    for c in range(width):
        lo = int(c / width * n)
        hi = max(lo + 1, int((c + 1) / width * n))
        chunk = [v for v in values[lo:hi] if v is not None]
        v = max(chunk) if chunk else 0.0
        idx = min(len(_BLOCKS) - 1, max(0, round(v * (len(_BLOCKS) - 1))))
        cols.append(_BLOCKS[idx])
    return "".join(cols)


def _period_markers(rows, width):
    n = len(rows)
    markers = [" "] * width
    seen = None
    for c in range(width):
        idx = min(n - 1, int(c / width * n))
        p = rows[idx]["period"]
        if p != seen:
            markers[c] = str(p) if p is not None else "?"
            seen = p
    return "".join(markers)


def replay_curve_ascii(rows, width=72):
    """
    Fixed-width ASCII plot of live_score / quality_so_far / drama_from_here
    against progress, with period-change markers underneath -- the tool for
    actually setting LIVE_W_SO_FAR, LATENESS_POWER, and MAX_UPSET_IN_PROGRESS
    by eye, not just asserting on them after the fact.
    """
    if not rows:
        return "(no rows)"

    label_w = 17
    lines = [
        f"{'live_score':<{label_w}}[{_sparkline([r['live_score'] for r in rows], width)}]",
        f"{'quality_so_far':<{label_w}}[{_sparkline([r['quality_so_far'] for r in rows], width)}]",
        f"{'drama_from_here':<{label_w}}[{_sparkline([r['drama_from_here'] for r in rows], width)}]",
        " " * (label_w + 1) + _period_markers(rows, width),
    ]

    peak = max(rows, key=lambda r: r["live_score"])
    final = rows[-1]

    lines.append("")
    lines.append(f"peak live_score = {peak['live_score']:.3f} at progress={peak['progress']:.2f} "
                  f"(period {peak['period']}, i={peak['i']}/{peak['n']})")
    lines.append(f"final: live={final['live_score']:.3f} so_far={final['quality_so_far']} "
                  f"from_here={final['drama_from_here']:.3f} headline={final['headline']!r}")
    return "\n".join(lines)
