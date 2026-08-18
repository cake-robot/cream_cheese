
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
   - **Speed of the swing** -- how *late* within the window the decisive
     score lands (e.g. Clemson/SMU, ACCCG 2024 -- decided on a play with
     almost no time left), as distinct from magnitude. A go-ahead score
     with 55 seconds left and one with 3 seconds left both currently count
     the same.
   Open question: are these two different metrics, or two terms folded
   into one? Needs the same kind of benchmark-game validation the
   comeback_erosion work used before wiring anything into `scoring.py`'s
   `METRICS`.


 ## Things I don't think I care about
- Do i care about pregame uncertainty (evenly matched)?
- care about time spent close?
- dont think i care about final margin