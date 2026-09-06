"""
Regenerates the _COMEBACK_MARGIN_Q4_CURVE lookup table in src/scoring.py.

comeback_margin_q4_close()'s raw output is always a non-negative integer
point margin (score differentials are always whole points), so rather than
evaluating a spline at runtime, this script bakes the curve down to a plain
Python list indexed by integer raw value -- no scipy/numpy dependency needed
in scoring.py itself, matching this project's existing pattern of keeping
runtime modules dependency-free (see src/wp_baseline.py, src/wp_situational.py).

CONTROL_POINTS below is the one thing to edit if the curve's shape changes --
chosen interactively against the live corpus (2026-09-05 tuning session):
user wanted a floor-value comeback (9 points, the minimum that can ever fire)
pulled down harder than a flat linear cap would, while a ~20-21 point recovery
stays close to "maxed out" instead of being cut by the same proportional
amount a higher flat cap would need -- i.e. steep early, then a long,
decelerating tail rather than a hard clip. Run this script and paste its
output over the table in scoring.py whenever CONTROL_POINTS changes.
"""
from scipy.interpolate import PchipInterpolator

CONTROL_POINTS = [
    (0, 0.0),
    (9, 0.25),
    (14, 0.42),
    (17, 0.68),
    (21, 0.90),
    (24, 0.95),
    (28, 0.98),
    (32, 0.99),
    (40, 1.0),
]

TABLE_MAX = 40  # anything at or above this raw value clamps to 1.0 at lookup time


def build_table():
    xs = [p[0] for p in CONTROL_POINTS]
    ys = [p[1] for p in CONTROL_POINTS]
    pchip = PchipInterpolator(xs, ys)
    return [round(float(min(pchip(i), 1.0)), 4) for i in range(TABLE_MAX + 1)]


if __name__ == "__main__":
    table = build_table()
    print("_COMEBACK_MARGIN_Q4_CURVE = [")
    for i in range(0, len(table), 10):
        chunk = table[i:i + 10]
        print("    " + ", ".join(f"{v:.4f}" for v in chunk) + ",")
    print("]")
    print()
    for x, y in CONTROL_POINTS:
        print(f"  check: raw={x:3d}  table={table[x]:.4f}  target={y:.4f}")
