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

## 7. Added `upset_risk` metric — pregame favorite-skew credit (new feature)

Requested: credit games where pregame win probability was most lopsided (one team
heavily favored), independent of how the game actually went — the presence of live
upset potential is itself part of watchability. `games.initial_home_wp` was already
fully populated (931/931 games) and unused for scoring.

**Design**: `upset_risk(initial_home_wp) = abs(initial_home_wp - 0.5) * 2`. Naturally
bounded 0 (even matchup) to 1 (near-certain outcome) — no cap needed, same pattern as
`time_spent_close`. Added to `METRICS` at weight 1.0, sourced via the same `context`
dict plumbing as `team_profile` (`score_games()` now also selects `initial_home_wp`).

**Verified both ends of the distribution before considering it done**:
- High `upset_risk` + high overall watchability: `CAL@LOU` (unranked road team beats
  #15 in OT), `LOU@MIA` (unranked road team beats #2), `GT@BC` (ranked GT barely
  survives unranked BC by 2) — exactly the "heavy favorite, game stayed live" scenario
  this is meant to catch.
- High `upset_risk` alone (max-skew games in the dataset) are FCS-cupcake blowouts
  (`GRAM@OSU` 0-70, `EIU@ALA` 0-56) that got full `upset_risk` credit but no actual
  upset — confirmed these do NOT rank highly overall (rank 281/931 and 691/931
  respectively), since `wp_volatility`/`lead_changes`/`time_spent_close` correctly stay
  near-zero for a game that was never competitive. Additive, not dominant — same
  behavior already established for `team_profile`.

## 8. `wp_volatility` cap raised again — was concentrating at the top (fixed)

After adding `team_profile` and `upset_risk`, the previous `MAX_VOLATILITY = 8.0`
(set in fix #1) started showing a different problem: overall dataset saturation was
still low (2.6%, 24/931 — looked fine in aggregate) but **saturation concentrates at
the top of the ranking**, since high volatility correlates with high overall
watchability. Checked directly: 32% of the top 25 games (8/25) were capped at
`norm=1.0`, versus 2.6% dataset-wide — exactly where losing differentiation hurts
most, the same shape of problem as fix #1 but revealed only once ranking-adjacent
metrics existed to shift what surfaces at the top.

**Fix**: raised `MAX_VOLATILITY` 8.0 → 10.0 (actual dataset max is 10.94, so this
clips only the single most extreme game). Top-25 saturation drops from 8/25 to 1/25;
dataset-wide from 24/931 to 4/931.

**Takeaway for future cap tuning**: check saturation *within the top-N*, not just
dataset-wide — a cap that looks fine in aggregate can still be actively suppressing
differentiation exactly where the ranking is read.

## 9. `upset_risk` reshaped to a power curve — was over-crediting modest favorites (fixed)

After shipping `upset_risk` (fix #7) as a linear function of pregame skew, `IU@ORE`
(#7 away vs #3 home, a 67.75/32.25 split — an ordinary ranked-vs-ranked favorite, not
a real mismatch) landed at #1 overall, ahead of `ORE@PSU` — the actual top-10 OT
showdown that started this whole investigation. Traced the specific number: a 68/32
split is already worth 0.355 of the metric's max under a linear scale, which felt too
generous for what's a fairly ordinary favorite between two ranked teams.

**Explored the shape with the user** (percentile table + a published interactive
chart plotting `skew^p` for p ∈ {1, 1.5, 2, 2.5, 3, 4} against real reference games)
before picking `p = 2.5`: gives IU@ORE only 0.075 (down from 0.355) while a genuine
90/10 mismatch still keeps solid credit (0.572) — a linear curve over-credits modest
favorites, `p=3` compresses even real 80/20-90/10 mismatches too much.

**Fix**: `upset_risk()` in `src/scoring.py` now raises the linear skew to
`UPSET_RISK_POWER = 2.5` before returning. Verified in simulation before shipping:
`ORE@PSU` reclaims #1, `IU@ORE` drops to #2/#3 depending on other concurrent changes,
while games with real lopsided-but-live outcomes (`CAL@LOU`, unranked road team beats
#15 in OT; `GT@BC`, ranked team barely survives unranked opponent) keep strong upset
credit (0.635, 0.482) — the curve pulls back on ordinary favorites without flattening
credit for genuine mismatches.

## 10. `upset_risk` scaled by favorite quality — was crediting unranked-vs-unranked skew (fixed)

Even after the power-curve fix (#9), `AFA@UNLV` (both teams unranked, final 48-51)
sat at #10 overall with `upset_risk=0.414` — a real pregame skew, but between two
unranked teams. Flagged as wrong in kind: a lopsided line only carries "upset risk"
prestige when a genuinely good team is involved; two unranked teams with one favored
over the other isn't the same thing as a ranked contender nearly losing.

**Fix**: `upset_risk()` now multiplies the power-curved skew by a quality factor —
`max(_rank_tier(home_rank), _rank_tier(away_rank))`, the same tier scale as
`team_profile`. Neither team ranked → multiplier 0, upset credit disappears entirely
regardless of how lopsided the pregame line was. A top-5 team involved → multiplier
1.0, full credit preserved regardless of which side was favored (deliberately uses
the *better-ranked* team of the two, not specifically "the favorite," so a ranked
team nearly upset by an unranked opponent — `LOU@MIA`, #2 MIA — still gets full
credit even though MIA was the favorite, not the underdog).

**Verified before shipping**: `AFA@UNLV` → 0.000 (dropped out of the top 25
entirely). `LOU@MIA` (#2 MIA nearly upset) and `UGA@FLA` (#5 UGA involved) stay
unchanged at full credit, since a top-5 team's quality multiplier is 1.0.
`CAL@LOU` (#15, top-25 tier) and `GT@BC` (#16, top-25 tier) get scaled down to 0.4×
their prior value — still real credit, since #15/#16 are genuinely ranked, just not
elite.

## 11. `time_spent_close` weight halved — was crowding out spiky-but-exciting games (design change)

Concern: a game that's exciting through big momentum swings or a near-upset, but
doesn't linger in the 0.30–0.70 WP band, was getting buried relative to games with
sustained closeness — `lead_changes` already captures drama shape to some degree, so
equal weighting (1.0, same as the other 4 metrics) was arguably double-counting
"closeness" as a concept.

Concrete example surfaced while simulating candidate weights: `401752733`
(WSU@MISS) — unranked Washington State nearly upsetting #4 Ole Miss on the road
(21-24) — has `time_spent_close=0.097` (WP rarely sat in the close band) despite 4
real lead changes and `upset_risk=0.920`. At equal weighting this game doesn't crack
the top 30; the WP likely swung through the close band quickly during scoring plays
rather than lingering there.

**Simulated three weights (1.0 / 0.5 / 0.25) against the full 2025 top-15 before
picking one** — the real tradeoff: `OU@ALA`, a genuine wire-to-wire nail-biter
(`close%=0.903`), drops from #4 (weight 1.0) to #11 (weight 0.25) as the weight
drops, since true sustained-closeness loses ranking power along with the spiky
games this was meant to fix. `weight=0.5` was the middle ground chosen: `OU@ALA`
holds at #8, while `WSU@MISS`-style games start surfacing (#14) without being
pushed to the top.

**Fix**: `time_spent_close`'s entry in `METRICS` (`src/scoring.py`) weight changed
1.0 → 0.5. Composite is now a weighted average over 4.5 total weight instead of 5.0.
Verified post-rescore against the simulation: top-15 matches exactly.

## Data note: 2024 season pulled

Ran the full pipeline for `season=2024` (previously only 2025 was loaded) to get a
second season for cross-validation and qualitative comparison against expert "best
games" lists. 919 games discovered, 918 completed/detail-fetched, 897 scored (21 games
have no ESPN win-probability data, same known category as 2025's gap — not an error).
One game (`LIB@APP`, 2024-09-28) is discovered but not marked `completed` despite
`status_state='post'` — plausibly a Hurricane Helene-related cancellation given the
date and Appalachian State's location, not a pipeline bug. Both seasons now coexist in
`data/cfb.db`, distinguished by `games.season_year`.

## 12. Qualitative validation against expert "best games" lists, and `late_volatility` (new metric)

Compared the algorithm's top-ranked games against real "instant classic" lists (ESPN's
Bill Connelly 100-best, Athlon, CFB Select, CFRA "greatest game of the year") for both
seasons. Several strong hits (`IU@ORE` #6, `TENN@MSST` top-25, `CONN@DEL` #66/1828)
confirmed the algorithm is generally sound. Two informative misses:

- **`GT@UGA` (2024, the legendary 8-OT game — 2nd longest in CFB history) ranked only
  #20/1828.** Investigated directly against ESPN's raw API: our stored data caps at
  `period=6` when the real game reached `period=12`. ESPN's own `drives` data has
  actual gaps (periods 7, 8, 10, 11 missing entirely) and the score field is frozen at
  an impossible `50-42` (exceeds the real final of 44-42) for dozens of consecutive
  plays during the alternating-2-point-conversion shootout phase (confirmed this
  format directly from play text/team-alternation — matches current NCAA OT rules).
  This is a genuine ESPN data-quality gap for this specific extreme-length game, not
  a pipeline bug — no clean fix without a different ESPN data source.
- **`MRSH@UL` (2025, Louisiana erases a 17-point 4th-quarter deficit, wins in 2OT) and
  `USF@FLA` (2025, USF upsets #13 Florida on a last-second FG) ranked #297 and #437.**
  Traced their actual `home_win_pct` series rather than assuming — both show real,
  substantial late swings (not muted), and both score above the dataset median on
  `wp_volatility`. The actual cause: `team_profile`/`upset_risk` are structurally zero
  for unranked-vs-unranked games (2 of 5 metrics), capping the theoretical max
  composite for such games at ~0.556 — below where ranked-team games already sit.
  Not a missing metric, a structural ceiling.

**Follow-up investigation** (charted `MRSH@UL`, `USF@FLA` against `WKU@LT` — the
highest raw `wp_volatility` in the top 30 — and `BOIS@USF`, a smooth one-directional
blowout) confirmed `wp_volatility` measures total path length (sum of `|Δwp|`), not
net displacement — a steady one-directional swing (`BOIS@USF`, vol=2.11) scores far
lower than an oscillating game covering similar net ground (`WKU@LT`, vol=10.94),
purely because oscillation retraces the same distance repeatedly. Deep-dived
`WKU@LT` specifically: none of its top-15 individual swings are turnovers or chunk
plays — the two biggest are OT snaps at `2nd/3rd & Goal` where even a stuffed run
carries huge leverage, and ~26% of the game's total volatility comes from a scoreless
Q3 stretch driven by field-position churn (punts, third-down conversions/failures),
not points. Confirmed `WKU@LT`'s visibly "bouncier" chart shape isn't a data quirk:
across all 8 example games, WP swings are systematically larger when the score is
near a true toss-up (mean delta when `0.3≤wp≤0.7` exceeds mean delta outside that
band, in every single game checked) — and `WKU@LT` spent 75.4% of its plays in that
high-sensitivity band, the highest of the set, so its overall bounciness is a genuine
reflection of being persistently, unusually close, not a bug.

**New metric — `late_volatility`**: from the above, decided to add a metric that
specifically credits volatility concentrated late in the game. Same formula as
`wp_volatility` (`sum(|Δwp|)`), windowed to `period_number >= LATE_PERIOD_THRESHOLD`
(4 = Q4 through any OT). `MAX_LATE_VOLATILITY = 4.5` (3.1% dataset-wide saturation,
consistent with other caps).

Checked correlation with `wp_volatility` before weighting: **r = 0.886** — a real
double-counting risk, since a game with high overall volatility is likely to have
some in Q4/OT too just by association. Simulated weight 1.0 vs 0.5 before deciding;
picked **0.5**, same treatment `time_spent_close` got for the same reason. Post-fix,
`GT@UGA` climbs to #13 overall (from #20) despite its known-incomplete OT data, and
`NIU@ND` (2024's famous Week 1 upset of #5 Notre Dame) enters the top 15 — validated
against the simulation exactly after rescoring.

## 13. New metric — `clutch_finish`, plus a "not applicable" mechanism in `score_game()`

Requested: credit games decided by a score in the final minute of regulation, worth
1.5x if that score isn't a field goal — but explicitly *without* penalizing OT games
for not having one, since a game that reached OT by definition wasn't decided by a
final-minute regulation score, and simply scoring it 0 would recreate the same
structural-ceiling problem found in fix #12 (`team_profile`/`upset_risk` zeroing out
40% of the composite for unranked-vs-unranked games).

**Architecture change**: `score_game()` (`src/scoring.py`) now treats a metric
function returning `None` as "not applicable" — excluded from *both* the numerator
and the weight total for that game, rather than included as a 0. This is a general
mechanism, not specific to `clutch_finish`; `db.upsert_game_metrics()` correspondingly
skips writing a `game_metrics` row for `None` entries (no row = not applicable,
distinct from a row scored 0). Verified directly: `ORE@PSU` (went to OT) has no
`clutch_finish` row in `game_metrics` and stayed at #2 overall, unaffected.

**Detection approach**: field-goal vs. not is inferred from the score delta alone (a
made FG is exactly 3 points; nothing else scores exactly 3) rather than fetching
play-type data — reuses the same non-decreasing-score sanitization as `lead_changes`.
Verified against `IU@PSU`'s known finish (Indiana's go-ahead TD with 36 seconds left):
delta=7 at `clock_seconds_elapsed=3564` (3600-3564=36s remaining) → correctly
identified as a non-field-goal clutch finish, `raw=1.5`.

**Values**: `CLUTCH_FINISH_WINDOW_SECONDS=60` (final minute of regulation),
`CLUTCH_FINISH_FIELD_GOAL_VALUE=1.0`, `CLUTCH_FINISH_NON_FIELD_GOAL_VALUE=1.5`,
`MAX_CLUTCH_FINISH=1.5` (so FG→norm 0.667, non-FG→norm 1.0, preserving the exact 1.5x
ratio). Weight 1.0.

**Distribution** (1750/1828 games applicable, 78 excluded for going to OT): 1426 no
clutch finish, 89 field-goal finishes, 235 non-field-goal finishes. `MIA@MISS` (a
non-FG walk-off) jumps to #1 overall; OT games (`ORE@PSU`, `UGA@TEX`, `GT@UGA`, etc.)
remain high in the rankings unaffected, confirming the "don't penalize OT" requirement
holds in practice, not just in the isolated `ORE@PSU` check.

## 14. `clutch_finish` FG/non-FG bug found and fixed via a persistent corrections registry

Spot-checking `USF@FLA`'s `clutch_finish` value against the known story (a walk-off
field goal) found it stored as `raw=1.5` (non-field-goal) — wrong. Root cause: a
phantom score row (`17`, between the real `15` after a safety and the real `18` after
the FG) fooled the delta-based FG detector (`delta==3` => field goal) into reading
`delta=1` instead of the true `delta=3`.

**Scoped the blast radius** before fixing anything: scanned all 7 games whose
clutch-window final delta fell outside the plausible single-play set `{2,3,6,7,8}`.
Cross-checked each against ESPN's `scoringType` field (ground truth, same endpoint
already fetched during Phase 2 — just a field we don't currently store). Only 2 of the
7 were actually misclassified in outcome (`UTEP@NMSU`, `USF@FLA` — both true field
goals stored as non-FG); the other 5 happened to land on the correct classification
anyway, since the detector defaults to "non-FG" for any delta != 3, and all 5 of those
were genuine touchdowns.

**Broader scan, prompted by "can we see impossible diffs in general"**: across *all*
score transitions (not just the clutch window), 255 of 16,610 transitions (1.5%) have
a delta outside `{2,3,6,7,8}`, spanning 197 distinct games (10.8% of the dataset).
Broke this down further: `delta=1` (90 instances) has no valid standalone explanation
given TD+PAT are always bundled into a single row in this data — high-confidence
corruption. Other deltas (4, 5, 9, 10, 14, 21, 28...) are mostly *ambiguous*, not
necessarily corrupted — several are suspiciously clean multiples of 7 (14=2×7, 21=3×7,
28=4×7), suggesting ESPN's feed sometimes just skips generating a win-probability
snapshot between two real scoring plays, combining them in our diff rather than
fabricating a wrong value. Of the 90 confident `delta=1` cases, only 10 land at a
moment where the phantom point actually flips who's leading or creates a false tie
(most land when the game state was already clearly decided, so the bad point doesn't
change any conclusion). One of those 10 (`401628469`, BOIS@ORE) was individually
ground-truthed: ESPN's own data explicitly flags the offending play `scoringPlay=False`
(a touchback kickoff, nothing scored) while its `homeScore` field is still incorrectly
incremented by 1 — a clean, fully-confirmed example of the failure mode. `lead_changes`
corrections for the other 9 are scoped but not yet applied (pending individual
verification, unlike the 2 clutch_finish fixes which are ground-truth confirmed).

**Also found while investigating `BOIS@ORE`**: the entire 2024 season had `NULL`
`play_sequence` for all 159,235 `win_probability` rows — `--compute-sequences` was
never run after the `pipeline.py --season 2024` pull (it's a separate manual step, not
part of the automatic discover→fetch→score flow). Every 2024 metric had silently been
computed using raw insertion order instead of the `(period_number, sequence_number)`
fix from earlier this session. Fixed by rerunning `--compute-sequences` (now covers
both seasons) and rescoring. Process gap to remember for any future season pull:
`--compute-sequences` must be run manually after `--season <year>`, it is not automatic.

**Fix — persistent corrections registry**: rather than hand-patching the database
(which a future `--rescore` or a from-scratch re-pull would silently wipe out), added
`src/corrections.py` — a version-controlled `CORRECTIONS` list of `{game_id,
metric_name, raw_value, reason}` entries, each requiring a ground-truth-verified
`reason`. `scoring.apply_corrections()` applies the list automatically at the end of
every `score_games()` run: recomputes `norm_value` from the metric's *current* cap
(never hand-supplied, so a future cap change re-normalizes corrections consistently)
and recomputes the game's overall `watchability_score` via the new
`scoring.recompute_composite()` helper (reads only already-stored `game_metrics` rows
— no wp_rows re-fetch needed, since only one metric changed). Verified: a full
`--rescore` printed `"Applied 2 manual correction(s)"` and both `UTEP@NMSU`/`USF@FLA`
landed on the corrected values automatically, with no manual step required.

## Possible future refinement (not implemented)

`header.competitions[0].competitors[].linescores` gives independent per-quarter
cumulative score checkpoints, straight from ESPN's header rather than the glitchy
per-play field. Could be used to validate/repair the per-play score sequence more
precisely (e.g. detect corruption that the simple non-decreasing rule wouldn't catch —
a value that's wrong but still technically higher than the previous one). Not needed
given how effective the simple sanitization turned out to be; worth revisiting only if
future analysis finds `lead_changes` still looks wrong for some game.
