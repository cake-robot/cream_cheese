# Data Quality Findings — Watchability Score Audit

## Context

Prompted by "find high-watchability games with lopsided final margins." That specific
pattern turned out to be legitimate (games competitive for most of regulation, decided
late — no fix needed there). The audit surfaced three real issues instead.

## 1. `wp_volatility` cap was too low (fixed)

`MAX_VOLATILITY` was `5.0`, but the actual distribution of raw `wp_volatility` across
931 scored games:

| percentile | raw value |
|---|---|
| p50 | 2.63 |
| p90 | 6.15 |
| p95 | 7.15 |
| p99 | 9.17 |
| max | 11.18 |

The cap sat below p90, so **194/931 games (21%)** saturated at `norm=1.0` — the metric
couldn't differentiate among the most volatile fifth of games, exactly where
differentiation matters most for a "most watchable" ranking.

**Fix**: raised `MAX_VOLATILITY` to `8.0` (saturates 23/931 = 2.5%, a reasonable outlier
tail). `src/scoring.py`.

## 2. ESPN per-play score fields are unreliable (fixed differently than planned)

ESPN's `play.homeScore`/`play.awayScore` fields (used for `lead_changes`) are
occasionally wrong. Confirmed by re-fetching live raw API responses. Two distinct
patterns:

**a. Severe/sustained corruption (rare, 8/931 games = 0.9%)** — negative scores,
sometimes persisting for dozens of consecutive plays. Example: `401778328` (IOWA@VAN)
has `homeScore=-3` for 39 consecutive plays. `401757173` (UAB@CONN) opens with
`homeScore=-38, awayScore=-19` (exactly the negation of the final score) for 30 plays,
and later the home score field is non-monotonic (39→38→21→38) — i.e. not just a sign
issue, the field is untrustworthy as a running tally for that game.

**b. Common single-row jitter (43% of games = 398/931)** — a lone row's score
momentarily reverts to a stale value (often exactly matching the value 2 rows back) right
around a scoring play, then self-corrects on the very next row. Example: `401752682`
(ARST@ARK), away score reads `14 → 7 → 14` across three consecutive rows all sharing the
same game-clock timestamp. Worst observed case (`401760376`, UNLV@M-OH) is just repeated
±1 flicker around PAT attempts (13⇄14, 20⇄21, 23⇄24, 37⇄38) — never disconnected from
the real score.

Initial plan was to add a flag that excludes affected games from scoring entirely. That
assumed the issue was rare (~8 games, matching pattern **a**). It isn't — a literal
"any negative or non-monotonic score" check hits 406/931 games (43%), mostly pattern
**b**, which is basically harmless noise. Excluding 43% of the dataset to work around a
single-row artifact was the wrong shape of fix.

**Fix instead**: sanitize scores at the point of use. `lead_changes()` in
`src/scoring.py` now tracks a running non-decreasing, non-negative score per team —
any `home_score`/`away_score` value that's negative or below the current running max is
discarded in favor of the last valid value (football scores only increase). This single
change fixes both pattern (a) and (b) uniformly, with no games excluded and no new DB
schema/flag needed. `wp_volatility` and `time_spent_close` are unaffected — they only
read `home_win_pct`, which is a separate ESPN field not subject to this corruption.

**Impact of the fix**: 58/931 games (6.2%) had a different `lead_changes` raw value
after sanitization. Notably `401752937` (FRES@ORST) — the #1 ranked game before this
fix — had `lead_changes` raw=10 (hit the metric's cap, `norm=1.0`) when the sanitized
true value is 8; after fixing, it drops from #1 to #4 in the ranking.

## 3. `play_sequence` ordering bug (fixed)

Asked "was `wp_volatility` also affected by these spurious oscillations?" while
investigating (2). Initial answer was too hasty — flagged 271 single-row "spike then
immediate revert" WP swings (>0.15 in, >0.15 back out) as likely artifacts without
verifying against real play data. Challenged on this; re-investigated properly by
re-fetching raw ESPN play text for the flagged rows.

Root cause: `play_sequence` (introduced in the prior session's `d33709e` commit) orders
`win_probability` rows by a re-derived `clock_seconds_elapsed`, replacing the previous
plain `id`/native-array order. This computed clock is unreliable and **scrambles
otherwise-correct chronology** for regulation-time plays:

- `401756956` (ASU@COLO): native array order shows COLO scoring to lead 14-13, then ASU
  immediately answering to retake the lead 21-14 — verified against real play text
  (`"J.Lewis pass complete deep left"` then ASU's answering drive) as two genuine,
  back-to-back lead changes. `play_sequence` order missed both, undercounting
  `lead_changes` (2 vs the correct 4).
- `401760417` (USU@FRES): two real, distinct drives (QB Barnes struggling for the home
  team; QB Warner driving and scoring 20→28 for the away team) have `clock_seconds_elapsed`
  ranges that spuriously overlap, so `play_sequence` interleaves them into one scrambled
  timeline. Confirmed via `drives.previous` array position (a reliable, independently
  chronological ESPN field) — the two drives are cleanly sequential there, not
  interleaved at all. This alone accounted for 74% of this game's `wp_volatility`.

But the prior session's fix wasn't baseless — checked before reverting, per instruction
to investigate rather than assume. In OT games, the *native* array order has its own
real bug: two key OT entries in `401757313` (KENN@LIB) appear at raw array positions 39
and 92 (deep in regulation-time territory) when they belong at the very end of the game.
Pure revert-to-array-order would have reintroduced that regression.

**Fix**: `compute_play_sequences` in `src/db.py` now orders by
`(period_number, sequence_number, id)` instead of `(clock_seconds_elapsed, id)`.
`period_number` is coarse and reliably parsed per play, so it's trusted to correctly
separate OT from regulation (fixing the OT bug); `sequence_number` (native WP-array
order) is trusted within a period instead of the fragile re-derived clock (fixing the
regulation-time scrambling). Validated against all 8 games where sanitized
`lead_changes` differed between array-order and clock-order: the hybrid reproduces
array order on every confirmed-regulation case and clock order on all 3 OT cases.

**Impact**: `401760417` (USU@FRES) `wp_volatility` corrected from raw 10.04 (saturated
at the cap) to 6.12. Dataset-wide, aggregate "spike-revert" WP volatility barely moved
(6.23% → 6.46% of total mass) — confirming most flagged swings were real drama, not
sequencing artifacts, which is why a blanket smoothing fix on `wp_volatility` (considered
and rejected earlier) would have been the wrong call. `wp_volatility` cap saturation
stayed healthy post-fix (2.6%, cap=8.0 from fix #1).

## 4. `lead_changes` cap raised (fixed)

While reviewing the top-20 ranked games post-fixes, `401760386` (AFA@UNLV, #1 overall)
had `lead_changes` raw=10 exactly matching `MAX_LEAD_CHANGES=10`, saturating norm=1.0.
Unlike `wp_volatility`, this wasn't a broad problem — only 1/931 games (0.1%) sat at the
cap, and the dataset's actual max raw value was exactly 10 (p99 was only 6.0) — but a
single game landing exactly on the boundary is still worth giving headroom so it isn't
artificially flattened, and so any future outlier isn't either.

**Fix**: raised `MAX_LEAD_CHANGES` from 10 to 12 in `src/scoring.py`. AFA@UNLV's
`lead_changes` norm value changed from 1.000 to 0.833 (raw 10/12); it remains #1 overall
(watchability 0.780) but on a more honest score. 0/931 games saturate the new cap.

## 5. `lead_changes` now credits ties, not just lead swaps (design change)

Requested: give watchability credit both when a team takes the lead *and* when the score
returns to a tie — a comeback that ties the game is dramatic even before the tying team
pulls ahead. Previously, ties were invisible to the metric: `lead_changes()` hit
`else: continue` on a tie, which skipped updating `last_leader` entirely, so `home, tie,
home` counted as zero events (correctly, no real change) but `home, tie, away` and `home,
tie, <home retakes lead>` both undercounted the drama of the tie itself.

Explicitly asked to guard against false-positive ties from the score-corruption patterns
found earlier this session — reasonable given nearly every game changes under this
edit (929/931), roughly doubling typical `lead_changes` values (each real tie now
generates two events: reaching it, then leaving it). Checked before shipping:

- Of 567 in-game tie occurrences (excluding the pregame 0-0 state) across the dataset,
  **562 (99.1%) are sustained across 2+ consecutive WP rows** — not a single-row blip,
  the kind of artifact this session repeatedly found from residual score jitter.
- The remaining 5 "flash" (1-row) ties were individually checked against their
  neighboring rows and `home_win_pct` — all 5 are genuine, plausible sequences (e.g.
  `401757234` JVST@GASO: 7-0 → tied 7-7 → 14-7 immediately after, WP moving smoothly
  0.508→0.437→0.592 throughout). None show the negative/non-monotonic signatures from
  fixes #2/#3.

No additional filtering needed — the existing sanitization (fix #2) and sequencing fix
(fix #3) already eliminate the mechanisms that would produce a spurious tie.

**Fix**: `lead_changes()` in `src/scoring.py` now tracks a 3-state variable (home
leading / away leading / tied) instead of only tracking a 2-state leader with ties
skipped. Any state transition counts, including into or out of a tie. The pregame 0-0
tie still doesn't count (state starts as `None`, matching the existing convention for
the first-ever score).

**Cap**: raw `lead_changes` roughly doubled in typical value (p50 0→2, p90 3→6, p99
6→10, max 10→13), so `MAX_LEAD_CHANGES` raised again, 12→14 (0/931 games saturate).

**Impact**: rankings shifted meaningfully — e.g. `401752816` (BC@MSU, 2-point margin)
moves to #1 overall, and several new close-margin games (CIN@KU, DUKE@CLEM, EMU@BUFF)
enter the top 15 for the first time, all games with genuine back-and-forth scoring.

## 6. Added `team_profile` metric — ranked-team matchup credit (new feature)

Requested: give watchability credit for higher-profile matchups (ranked teams), which
was a gap — all 3 prior metrics are purely in-game trajectory, nothing about pregame
stakes. `games.home_rank`/`away_rank` (AP-style 1-25, populated pregame) were already
in the schema and unused for scoring. Coverage: 64/931 games both ranked, 223 one
ranked, 644 neither.

**Design** (worked through with the user, weighing average vs. sum-capped combination
methods with concrete real-game examples before deciding):
- Each team's rank maps to a tier score: top-5 → 1.0, top-10 → 0.7, top-25 → 0.4,
  unranked → 0.0 (`_rank_tier()`).
- `team_profile(home_rank, away_rank)` = **sum** of both teams' tier scores (not
  average) — a single elite team still gives real credit even against an unranked
  opponent, rather than requiring both teams to be good.
- `MAX_TEAM_PROFILE = 1.5` — deliberately below the max possible sum (2.0, two top-5
  teams), so a lone elite team (raw 1.0) normalizes to 0.667 (meaningful, not
  negligible), while two good teams together (e.g. top-5 + top-10 = 1.7) hit the cap
  and normalize to 1.0. Two truly elite teams isn't required to max out the metric, but
  it's rewarded more than a single elite team steamrolling a cupcake.

**Architecture change**: `team_profile` needs `home_rank`/`away_rank`, not `wp_rows`,
so `score_game()` now takes a `context` dict (`{wp_rows, home_rank, away_rank}`)
instead of `wp_rows` directly, and each `METRICS` entry is a small lambda unpacking
what it needs from `context` — existing metric function bodies (`wp_volatility`,
`lead_changes`, `time_spent_close`) are unchanged, only the registry wiring changed.
`score_games()` now selects `home_rank`/`away_rank` alongside the existing game columns.

**Impact**: `401752854` (ORE@PSU, ranked #6/#3, went to OT) jumps from #21 to **#1**
overall — previously given zero credit for being a top-10 showdown. `401778330`
(MICH@TEX, #18/#13) moves from #17 to #7. Several unranked-but-dramatic games (BC@MSU,
CIN@KU, DUKE@CLEM, CONN@DEL) remain in the top 20 with `team_profile=0.000`, confirming
the new metric adds credit without dominating the ranking for games that earn it purely
on in-game drama.

## Possible future refinement (not implemented)

`header.competitions[0].competitors[].linescores` gives independent per-quarter
cumulative score checkpoints, straight from ESPN's header rather than the glitchy
per-play field. Could be used to validate/repair the per-play score sequence more
precisely (e.g. detect corruption that the simple non-decreasing rule wouldn't catch —
a value that's wrong but still technically higher than the previous one). Not needed
given how effective the simple sanitization turned out to be; worth revisiting only if
future analysis finds `lead_changes` still looks wrong for some game.
