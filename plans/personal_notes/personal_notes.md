
## Things I do care about
 - late game drama
 - comeback magnitude (doesnt need consumation)
 - upset magnitude (doesn't need consumation)
 - scoring? turnovers? big plays
 - Overtime flag?  Overtime possession bonus?
 - Ranked team tiers
 - extra credit for a UW loss (rooting bias, deliberate -- not a general watchability principle)

## Ideas flagged 2026-08-18, not yet explored

 - `clutch_finish()` (scoring.py) currently credits the final minute's
   *decisive* swing (a go-ahead score, or a tie that holds to the end of
   regulation). Two possible extra-credit axes on top of that, not yet
   scoped:
   - **Multiple swings**, not just one -- a final minute with two or three
     lead changes should probably score higher than one with a single
     go-ahead score, even if both technically "qualify" today.
   - ~~**Speed of the swing**~~ -- done 2026-08-19: window widened from the
     final 1 minute to the final 5 minutes (`CLUTCH_FINISH_WINDOW_SECONDS`),
     with credit scaling linearly by how late in the window the decisive
     score lands -- `CLUTCH_FINISH_MIN_FRACTION` (0.20) of the tier value at
     5:00 left, ramping to the full value at 0:00 left. Still open: the
     scaling itself is a flat linear ramp as a first pass -- worth
     revisiting with non-linearity (e.g. an exponential/convex curve so a
     3-second-left score is disproportionately more valuable than a
     55-second-left one) or a piecewise slope (steeper in the final minute
     than in minutes 2-5). No benchmark-game validation done yet on the
     curve shape itself.
   Open question: are these two different metrics, or two terms folded
   into one? Needs the same kind of benchmark-game validation the
   comeback_erosion work used before wiring anything else into
   `scoring.py`'s `METRICS`.


 ## Things I don't think I care about
- Do i care about pregame uncertainty (evenly matched)?
- care about time spent close?
- dont think i care about final margin