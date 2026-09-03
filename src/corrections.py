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
    {
        "game_id": "401628439",  # Georgia Tech @ Georgia, 2024-11-30
        "metric_name": "lead_changes",
        "raw_value": 7,
        "reason": (
            "This game went to 8 overtimes (the longest game in SEC history, final "
            "44-42 UGA) -- both ESPN's win_probability array AND Fox's play-by-play "
            "independently stop tracking partway through the OT3+ two-point-conversion "
            "shootout phase, each leaving the score frozen at a tied 40-40 (confirmed "
            "'unusable' by fox_reconcile -- neither source's derived final score "
            "matches the real one, so the automated cross-check correctly declined to "
            "trust either). Reconstructed by hand from ESPN's raw drives/plays data "
            "(period-number and score fields are themselves corrupted for this stretch "
            "-- stuck at period 5 and a phantom 42-50 score -- but team attribution and "
            "play text are reliable) cross-referenced against public recaps: OT3, OT4, "
            "OT6, and OT7 ended with both sides failing their 2-point try (no score "
            "change possible); OT5 ended with both sides converting (score moves from "
            "40-40 to 42-42, still tied, still no lead change); OT8 is the only point "
            "after the tied 40-40 mark where the score changes unevenly -- Georgia "
            "converts to go up 44-42 and the game ends immediately on Georgia Tech's "
            "failed answering attempt. That is the single lead change missing from the "
            "truncated data: corrected value is Fox's already-correct in-data count (6) "
            "plus this one final go-ahead score. clutch_finish is unaffected and stays "
            "not-applicable (OT game)."
        ),
    },
    {
        "game_id": "401628439",  # Georgia Tech @ Georgia, 2024-11-30
        "metric_name": "comeback_erosion",
        "raw_value": 0.4541,
        "reason": (
            "espn.extract_situational_plays' output for this game is unusable for the "
            "same reason lead_changes needed hand-correction above, but manifesting "
            "differently: ESPN's raw drives are severely out of chronological order "
            "(not just the OT stretch -- a full regulation drive is misplaced), so "
            "_sanitized_situational_plays' non-decreasing floor gives up at elapsed=2107s "
            "(~Q2) even though the raw feed claims to reach 3600s, discarding the entire "
            "second half. Recomputed instead from Fox's play-by-play (fox_event_id=40983), "
            "which tracks scoring cleanly and in correct chronological order through all "
            "of regulation (confirmed 'unusable' by fox_reconcile only because of the "
            "same OT-truncation issue as lead_changes -- Fox's own final doesn't match "
            "the box score -- but that's irrelevant here since comeback_erosion never "
            "evaluates OT anyway). Fox's play-by-play has no down/distance/field-position "
            "fields, so it can't feed Model C (src/wp_situational.py) -- fed instead "
            "through wp_baseline.coinflip_wp_elapsed, the same score+time+line-only "
            "fallback already used to backfill win_probability for ESPN-WP-less games. "
            "Ran the actual arc-walk algorithm (scripts/compute_fox_comeback_erosion.py -- "
            "same COMEBACK_EROSION_THRESHOLD=0.84, same PARITY=0.5 clamp at arc-close "
            "only, same close-game trigger as scoring._comeback_erosion_walk, just a "
            "different win-probability function) against Fox's regulation-only score "
            "sequence: Georgia Tech builds to a 94.4% coin-flip peak up 17-0, Georgia "
            "answers to within 3 (6-17), GT extends to an even bigger 95.4% peak up 14 "
            "(13-27), and Georgia claws all the way back to a 27-27 tie at the very end "
            "of regulation, right before OT began. Corrected value: 0.4541."
        ),
    },
    {
        "game_id": "401677087",  # USF @ San Jose State, Hawaii Bowl, 2024-12-24
        "metric_name": "lead_changes",
        "raw_value": 10,
        "reason": (
            "5-overtime Hawaii Bowl (first-ever 5OT bowl game, final 41-39 USF). "
            "ESPN's win_probability array has a distinct corruption from the usual "
            "truncation: the true final score (41-39) appears out of chronological "
            "order at play_sequence 222 (still tagged period 5), one row *before* the "
            "correctly-ordered-but-earlier 37-37 at play_sequence 223. Non-decreasing "
            "sanitization (correctly) locks onto the premature 41/39 running maxes and "
            "then (correctly, given what it can see) discards the later 37-37 as stale, "
            "which silently collapses 4 real lead changes into 1 -- naively patching the "
            "stored value (6) with a same-day hand count of the visible drives would have "
            "landed on 9, not the true 10. "
            "Reconstructed instead entirely from scratch using ESPN's raw drives/plays "
            "text (reliable; ignores the corrupted score/period fields), which does reach "
            "the correct 41-39 final: regulation ends tied 27-27 (verified), then OT "
            "score progression is 27-27 -> 34-27 (USF TD) -> 34-34 (SJSU TD) -> 34-37 "
            "(SJSU FG) -> 37-37 (USF FG) -> 39-37 (USF 2pt) -> 39-39 (SJSU 2pt) -> 41-39 "
            "(USF 2pt, game ends on SJSU's answering pass batted down per public recaps). "
            "That's 7 OT lead changes on top of 3 in regulation = 10 total. clutch_finish "
            "stays not-applicable (OT game)."
        ),
    },
    {
        "game_id": "401677180",  # Pitt @ Toledo, GameAbove Sports Bowl, 2024-12-26
        "metric_name": "lead_changes",
        "raw_value": 17,
        "reason": (
            "6-overtime GameAbove Sports Bowl (bowl record at the time, final 48-46 "
            "Toledo) -- the longest game beat this one by 48 hours (see the "
            "401677087/Hawaii Bowl entry above). ESPN's play-by-play score fields "
            "are corrupted for this stretch too (e.g. a phantom 43 appears mid-sequence "
            "that matches no real value before or after), so this was reconstructed "
            "entirely from the literal play text (team attribution + description), "
            "ignoring the score/period fields outright, then cross-checked against "
            "public recaps confirming 'the first defensive stop of any of the six "
            "overtime periods' happened in OT6. Regulation ends tied 30-30 (verified). "
            "OT progression: 30-30 -> 37-30 (PITT TD) -> 37-37 (TOL TD) -> 40-37 (TOL "
            "FG) -> 40-40 (PITT FG) -> 42-40 (PITT 2pt) -> 42-42 (TOL 2pt) -> 44-42 (TOL "
            "2pt) -> 44-44 (PITT 2pt) -> 46-44 (PITT 2pt) -> 46-46 (TOL 2pt) -> 48-46 "
            "(TOL 2pt, game ends on PITT's answering attempt failing) -- reproduces the "
            "real 48-46 final exactly. That's 11 OT lead changes on top of 6 in "
            "regulation = 17 total. clutch_finish stays not-applicable (OT game)."
        ),
    },
]
