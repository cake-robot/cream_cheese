"""
Runtime-dependency-free lookup module for the Burke-2007-style
non-parametric win probability baseline -- see
plans/algorithm/wp_burke_baseline.md for the full design rationale and
scripts/build_wp_burke_baseline.py for how src/wp_burke_baseline_grid.json
(loaded below) is generated.

Unlike src/wp_situational.py (Model C, a hand-specified logistic regression),
this model has NO parametric form: it's a per-down 3-D grid of
(yards_to_go, scores_needed, elapsed_seconds) -> empirical win rate,
smoothed with a Gaussian filter at generation time to handle sparse cells.
predict_wp_offense() does pure-Python trilinear interpolation into that grid
-- no numpy/scipy needed at runtime, matching wp_baseline.py/wp_situational.py's
existing dependency-free-at-runtime convention.

Deliberately has NO offense_pregame_wp parameter at all (unlike
wp_situational.py's predict_wp_offense/coinflip_wp_offense pair) -- this
model has no concept of pregame team strength by design, so there's nothing
to force to a coin flip; every prediction IS already the coin-flip reading.
See the plan doc's "Scope" section for why that's the point of this
comparison, not an oversight.

This module is NOT wired into src/scoring.py, serve.py, or any production
code path -- it exists purely for scripts/compare_wp_burke_vs_model_c.py.
"""
import json
import math
import os

_GRID_PATH = os.path.join(os.path.dirname(__file__), "wp_burke_baseline_grid.json")

with open(_GRID_PATH) as _f:
    _PAYLOAD = json.load(_f)

_BINS = _PAYLOAD["bins"]
_YTG_BIN_WIDTH = _BINS["ytg_bin_width"]
_N_YTG_BINS = _BINS["n_ytg_bins"]
_SN_MIN = _BINS["sn_min"]
_N_SN_BINS = _BINS["n_sn_bins"]
_TIME_BIN_WIDTH = _BINS["time_bin_width"]
_N_TIME_BINS = _BINS["n_time_bins"]

# {down (int): 3-D nested list [ytg_bin][sn_bin][time_bin] -> smoothed win rate}
_GRIDS = {int(d): grid for d, grid in _PAYLOAD["grids"].items()}


def scores_needed(score_diff):
    """Identical to build_wp_burke_baseline.py's helper (and
    src/wp_situational.py's) -- a 1-8 point deficit is "1 score", 9-16 is
    "2 scores", etc., sign preserved. Kept in sync by hand across the three
    copies (build script, this module, wp_situational.py) since each lives
    in a different dependency tier; a shared import would pull scipy/numpy
    into this runtime module's dependency chain."""
    if score_diff == 0:
        return 0
    return math.copysign(math.ceil(abs(score_diff) / 8.0), score_diff)


def _axis_coord(value, bin_width, n_bins, bin_min=0):
    c = (value - bin_min) / bin_width - 0.5
    return max(0.0, min(c, n_bins - 1.0))


def predict_wp_offense(*, down, yards_to_go, score_diff, elapsed_seconds):
    """Win probability for the team on offense, given their own field
    position, the score (offense - defense), and elapsed game seconds
    (0-3600, regulation only -- do not call this for OT plays). No down/
    distance-to-first-down input beyond `down` itself, and no pregame-WP
    input at all -- see the module docstring for why."""
    down = max(1, min(int(down), 4))
    grid = _GRIDS[down]
    sn = scores_needed(score_diff)

    yc = _axis_coord(yards_to_go, _YTG_BIN_WIDTH, _N_YTG_BINS)
    sc = _axis_coord(sn, 1, _N_SN_BINS, bin_min=_SN_MIN)
    tc = _axis_coord(elapsed_seconds, _TIME_BIN_WIDTH, _N_TIME_BINS)

    y0, s0, t0 = int(yc), int(sc), int(tc)
    y1, s1, t1 = min(y0 + 1, _N_YTG_BINS - 1), min(s0 + 1, _N_SN_BINS - 1), min(t0 + 1, _N_TIME_BINS - 1)
    fy, fs, ft = yc - y0, sc - s0, tc - t0

    total = 0.0
    for yi, wy in ((y0, 1 - fy), (y1, fy)):
        for si, ws in ((s0, 1 - fs), (s1, fs)):
            for ti, wt in ((t0, 1 - ft), (t1, ft)):
                total += wy * ws * wt * grid[yi][si][ti]
    return total
