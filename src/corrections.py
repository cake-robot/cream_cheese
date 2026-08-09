"""
Manual data corrections for cases the automated pipeline gets wrong.

Every entry here is a ground-truth-verified override for a specific
(game_id, metric_name) pair, discovered by tracing a specific game's raw
ESPN data and finding the pipeline's inference logic disagreed with reality
(usually because ESPN's own play-by-play has a glitch that fools our
sanitization). See plans/algorithm/data_quality_findings.md for the
investigation behind each one.

This list is applied automatically at the end of scoring.score_games(), so
corrections survive a full re-pull/rescore of a season -- add an entry here
rather than hand-editing the database, or a future --rescore will silently
wipe the fix out.

Each entry:
    game_id:     ESPN game id (string)
    metric_name: must match a name in scoring.METRICS
    raw_value:   the corrected raw value; norm_value is always recomputed
                 from this via the metric's current cap, never hand-supplied,
                 so a future cap change re-normalizes corrections consistently
    reason:      why the automated value was wrong -- keep this specific
                 enough that a future re-investigation isn't required to
                 understand the fix
"""

CORRECTIONS = [
    {
        "game_id": "401641031",  # UTEP@NMSU
        "metric_name": "clutch_finish",
        "raw_value": 1.0,
        "reason": (
            "Ground truth (ESPN scoringType='field-goal' on the decisive final play) "
            "confirms this was a field goal. clutch_finish's score-delta inference "
            "(delta==3 => field goal) computed a non-3 delta because of a corrupted "
            "intermediate score row upstream of the real final score, misclassifying "
            "it as a non-field-goal finish (raw=1.5 instead of 1.0)."
        ),
    },
    {
        "game_id": "401752684",  # USF@FLA
        "metric_name": "clutch_finish",
        "raw_value": 1.0,
        "reason": (
            "Ground truth (ESPN scoringType='field-goal' on Nico Gramatica's 20-yard "
            "walk-off) confirms this was a field goal. A phantom score row (17, "
            "between the real 15 after a safety and the real 18 after the FG) fooled "
            "the delta computation into reading delta=1 instead of the true delta=3, "
            "misclassifying it as a non-field-goal finish (raw=1.5 instead of 1.0)."
        ),
    },
]
