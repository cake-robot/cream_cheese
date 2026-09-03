# Burke-2007-style non-parametric win probability baseline

## Why this exists

Model C (`src/wp_situational.py`) is a hand-specified logistic regression: down,
distance, yards-to-go, score, time, and pregame WP enter as a fixed set of terms
and interactions we chose. Every blind spot found this session (the field-goal
blind spot, the goal-line/short-yardage collapse, the narrow-lead miscalibration
that motivated `scores_needed`) was the same failure shape: some region of the
situational state space behaves qualitatively differently from the rest, and
nobody told the regression that region existed, because it only knows the
interactions we explicitly wrote down. Each fix has been "notice a bug, hand-add
a term for it" -- there's no reason to believe we've found the last one.

ESPN's own win-probability writeups (see the earlier web-research pass) point to
a materially different lineage: Brian Burke's original 2007-2013 model, which
Dean Oliver's ESPN explainer directly echoes ("built on actual outcomes from
recent seasons that featured similar circumstances"). That model is
**non-parametric**: bin historical plays into situational buckets, use the
empirical win rate in each bucket (smoothed across neighbors to handle sparse
cells), and never hand-specify an interaction at all. Burke's own stated reason
for not using a plain logistic regression was that "football scores are chunky"
(3s and 7s) -- the same discreteness insight that motivated our `scores_needed`
feature, just resolved a different way: instead of discretizing the score
margin analytically, discretize *everything* and let empirical frequency find
whatever shape actually exists in each cell, including shapes we'd never think
to hand-specify.

**The disagreement this is meant to resolve:** ESPN clearly beats Model C in
Q4 and in the games' final possessions. Is that because ESPN has team-strength
inputs Model C lacks, or because Model C's parametric form is structurally
missing interactions that a non-parametric approach would capture for free?

**The test:** build a Burke-style baseline with the same information Model C's
coin-flip mode has -- no pregame team strength, no timeouts (we don't have
reliable timeout data either, see the abandoned timeouts-feature investigation
in project memory) -- and see how much of ESPN's Q4/endgame edge over Model C
it closes on its own. If it closes most of it, the gap was functional form, not
missing team-strength data. If it doesn't, team strength (or timeouts, though
this test can't isolate that specifically) is more likely the real story.

## Scope

This is an offline **comparison model**, not a production replacement. It does
not touch `src/scoring.py`, `serve.py`, or any production code path. It lives
entirely under a new `src/wp_burke_baseline.py` (+ a generated data file) and a
`scripts/compare_wp_burke_vs_model_c.py` evaluation script, following the same
"generated module, dev-only fitting script" pattern as `wp_baseline.py` /
`wp_situational.py`.

## Data

Identical corpus to every Model C comparison this session:
`espn.extract_situational_plays()` output, non-OT completed games, offense
perspective, target = actual game outcome (`offense_won`). Same 80/20
game-level train/test split (`RANDOM_SEED = 20260830`) used throughout, so
every number is directly comparable to the Model C and ESPN numbers already on
record.

## Design

### Dimensions and binning

Four inputs, matching what Model C's coin-flip mode (`offense_pregame_wp=0.5`)
already uses -- deliberately no team-strength input, deliberately no timeouts
(unavailable):

| Dimension | Treatment | Rationale |
|---|---|---|
| `down` | 4 separate strata (1 grid per down) | Qualitatively different regimes, not a smooth continuum -- shouldn't be smoothed *across*, only *within*. |
| `yards_to_go` (field position) | Binned every 2 yards, 0-100 (50 bins) | Finest practical resolution on the axis where the goal-line and FG-range blind spots actually live. |
| `scores_needed` | Integer bins -6..+6, saturating beyond (13 bins) | Reuses our own validated insight (a 1-pt and 2-pt deficit are the same problem) instead of raw point margin -- a deliberate, disclosed hybrid of Burke's method and our own finding, not a pure replication. |
| `elapsed_seconds` (time) | Binned every 60 seconds, 0-3600 (60 bins) | Uniform bins; the smoothing kernel (not the bin boundaries) is what should adapt resolution near the end of the game -- see below. |

Total cells: 4 x 50 x 13 x 60 = 156,000. Against ~528K training plays, that's
~3.4 rows/cell on average -- confirms raw per-cell frequencies would be far too
noisy on their own, which is exactly why Burke's method (and this one) leans on
smoothing rather than raw binning.

### Smoothing

Per down-stratum, compute two 3-D grids over (yards_to_go, scores_needed,
elapsed_seconds): `wins` (sum of `offense_won`) and `n` (count). Smooth each
independently with a 3-D Gaussian filter (`scipy.ndimage.gaussian_filter`),
then divide `smoothed_wins / smoothed_n` per cell -- the standard trick for
sample-size-weighted kernel smoothing without hand-rolling a weighted kernel:
a sparse cell's raw 0/1 rate gets pulled toward its (better-populated)
neighbors' rate automatically, proportional to how little data it has.

Bandwidth (`sigma`) is chosen independently per axis and validated by held-out
Brier score, not guessed -- start from a bandwidth of roughly "a few bins" per
axis (e.g. sigma=2 bins on yards_to_go and time, sigma=1 bin on scores_needed)
and do a small grid search over sigma combinations, picking whatever minimizes
held-out log-loss on the train-side split (never the held-out test split, to
avoid tuning against the same data used for final evaluation).

This also directly answers the "finer near the end of the game" instinct from
the original proposal: rather than hand-designing non-uniform bin widths,
keeping bins uniform and letting a single Gaussian bandwidth apply means the
model's *effective* resolution is uniform in bin-space, not time-space --
which is a real simplification versus Burke's own description. If validation
shows the endgame specifically needs sharper resolution than the mid-game, the
follow-up is a bandwidth that shrinks as a function of `elapsed_seconds`
(heteroscedastic smoothing) rather than a single global sigma -- flagged as a
concrete next step if the flat-bandwidth version underperforms specifically in
the last few minutes.

### Inference (the generated runtime module)

`src/wp_burke_baseline.py` loads a companion data file
(`src/wp_burke_baseline_grid.json` -- a flat table too large to embed as
literal Python source, unlike `wp_situational.py`'s dozen coefficients) and
exposes:

```python
def predict_wp_offense(*, down, yards_to_go, score_diff, elapsed_seconds) -> float
```

Deliberately no `offense_pregame_wp` parameter at all -- this model has no
concept of pregame strength, by design (see "Scope" above). Internally: convert
`score_diff` to `scores_needed`, then **trilinear interpolation** across the
three continuous axes within the matching down-stratum's smoothed grid (pure
Python, no numpy at runtime -- same dependency-free-at-runtime rule
`wp_situational.py` already follows). Trilinear interpolation, not
nearest-cell lookup, so the exposed function is continuous in its inputs
despite the underlying grid being discrete -- avoids visible "steps" in a
chart that reads this model's output play-by-play.

### What this deliberately does NOT do

- No team-strength/pregame-WP input (the whole point -- see "Scope").
- No timeouts (not available in the archived data -- shared limitation with
  Model C, not something this comparison can fix or hide).
- No OT (matches every other Model C investigation this session).
- Not wired into `comeback_erosion`, the coin-flip chart, or any production
  path. Purely a `scripts/compare_wp_burke_vs_model_c.py`-driven comparison
  artifact.

## Validation plan

Reuse the exact held-out slices this session has already established as the
places Model C struggles, evaluated on the SAME held-out test games as every
prior comparison:

1. Overall held-out Brier/log-loss (sanity check, not the interesting number).
2. Last 2 minutes / last 30 seconds of regulation.
3. The narrow-lead, fresh-downs, <=30s slice (`scores_needed`'s original
   target).
4. The field-goal-range/high-leverage slice (`down4`, `yards_to_go<=40`,
   close+late).
5. The goal-line slice (trailing offense, <=30s left, `yards_to_go<=10`) --
   the largest miscalibration found this session (41 points at its worst).
6. All five slices ALSO compared directly against ESPN's actual live WP
   (already available via `win_probability` rows with `source='espn'`, joined
   by `play_id`, same technique as `scripts/compare_wp_vs_espn.py`) --
   Model C compared in its coin-flip (no-pregame-WP) mode for a fair
   apples-to-apples fight against a model that structurally has no
   pregame-WP concept at all.

**The number that actually matters**: for each slice, what fraction of
ESPN's edge over Model C does the Burke baseline recover? Concretely,
`(burke_brier - modelc_brier) / (espn_brier - modelc_brier)` per slice --
close to 1 means "the gap was functional form, matches the user's hypothesis";
close to 0 means "the gap survives even with a completely different modeling
approach, so it's more likely to be missing data (team strength or timeouts)
than functional form."

## Deliverables

- `plans/algorithm/wp_burke_baseline.md` (this file)
- `scripts/build_wp_burke_baseline.py` -- dev-only fitting script: builds the
  dataset, bins, smooths (with the small sigma grid-search), writes
  `src/wp_burke_baseline_grid.json` + a generation report (cell coverage,
  chosen sigmas, train-side held-out Brier used to pick them)
- `src/wp_burke_baseline.py` -- runtime-dependency-free lookup module
- `scripts/compare_wp_burke_vs_model_c.py` -- the validation harness above,
  reporting all six comparisons

## Result (implemented and run, 2026-09-02)

Built exactly as designed above. Sigma bracketed properly (0.5 through 6.0
per axis tested; optimum at (1.0, 0.75, 1.0), a shallow minimum -- not an
artifact of a narrow initial search). Held-out overall Brier: 0.1449 (Burke)
vs. 0.1362 (Model C, coin-flip mode) vs. 0.1050 (ESPN).

**The honest result is mixed, not a clean win for either hypothesis:**

| Slice | Model C (coin-flip) | Burke baseline | ESPN | Burke vs. Model C |
|---|---|---|---|---|
| All plays | 0.1362 | 0.1420 | 0.1050 | worse |
| Last 2 min | 0.0536 | 0.0655 | 0.0406 | worse |
| Last 30s | 0.0351 | 0.0469 | 0.0141 | worse |
| Narrow lead (n=62, small) | 0.0160 | 0.0785 | 0.0161 | worse |
| FG-range/high-leverage (n=181) | 0.1254 | 0.1504 | 0.0781 | worse |
| **Goal-line blind spot (n=28, small)** | 0.3182 | **0.2682** | 0.0381 | **better** |

On the goal-line anchor example (401521330, OSU 1st & goal from the 1,
trailing by 4, 7s left, ESPN reads 65.1%): Model C reads 25.9%, the Burke
baseline reads **39.9%** -- a real improvement, still well short of ESPN
and the ~53-66% empirical rate found for this situational bucket earlier
this session, but a clear step in the right direction from a model with
literally zero hand-specified interaction terms.

**Why this is a genuinely useful (if humbling) result, not a failed
experiment:** if Model C's functional form were the dominant explanation
for ESPN's whole Q4 edge, a model with the same information but NO
functional-form constraints should have at least matched Model C across
the board while still trailing ESPN by roughly the same margin everywhere.
Instead, Burke's baseline is *worse* than Model C almost everywhere except
the one specific situation (goal-line/short-yardage endgame) where Model
C's blind spot was most severe. That's consistent with: (a) Model C's
parametric form, imperfect as it is, extrapolates statistical strength
across the state space far more efficiently than raw local-frequency
counting can at our current data volume (~528K rows spread over 156K
cells is enough to reveal a *severe, badly-wrong* shape like the goal-line
collapse, but not enough to reliably beat a smooth parametric fit on
*everything else*), and (b) the broader Model-C-to-ESPN gap in Q4 is more
likely genuinely missing information (team strength and/or timeouts) than
a wholesale functional-form problem -- Model C's functional form explains
SOME of what we found (the specific blind spots we hand-diagnosed this
session), not most of the aggregate Q4 gap.

**A concrete, more promising use for this artifact going forward,
suggested by this result**: not a wholesale replacement candidate, but a
**diagnostic scanner** -- run it periodically to flag (Model C prediction)
vs (Burke empirical rate) divergences across the whole state space
systematically, rather than relying on someone stumbling onto the next
blind spot by hand-tracing one anecdote at a time (which is literally how
both the FG-range and goal-line blind spots were found this session).

**Caveats on the numbers above**: two of the six slices have very small
held-out sample sizes (n=28, n=62) -- directionally informative, not
statistically definitive on their own. This comparison structurally
cannot separate "team strength" from "timeouts" as explanations for the
remaining gap (see "Scope" above) -- only that *neither* is captured by
either model, and the gap mostly survives regardless.

## Explicitly out of scope for this round

- Adding a team-strength adjustment (Burke's own 2013 upgrade) -- a natural
  follow-up once the vanilla comparison's result is in, not before.
- Wiring this into `comeback_erosion`, the coin-flip chart, or any production
  code path.
- Resolving the timeouts question -- this comparison structurally cannot
  isolate that variable, since neither model has it.
