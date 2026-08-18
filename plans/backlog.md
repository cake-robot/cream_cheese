# Backlog

Future work flagged in conversation, not yet scoped into a plan. Not
algorithm-scoring preferences (those live in
`plans/personal_notes/personal_notes.md`) -- this is feature/data-pipeline
ideas.

## Acronym support in game search

Flagged 2026-08-18. `api_spoilers_search()` (serve.py:2268), the query
behind the settings page's game picker, LIKE-matches only
`home_team_name`/`away_team_name`/`venue_name` -- **not**
`home_team_abbr`/`away_team_abbr`. Searching "PSU" or "OSU" today only
matches if those letters happen to appear in sequence in the full team
name, which they usually don't -- acronym search effectively doesn't work.
Fix is probably just adding the two abbr columns to the existing LIKE
clause. Worth double-checking with the user this is actually what "acronym
support" meant before implementing -- flagged in passing, not scoped in
detail.

## Fox score chart: real game-time markers

Flagged 2026-08-18. `build_fox_score_payload()` (serve.py:936) positions
the Fox score chart's x-axis by `step_number` -- ordinal index into
`fox_score_sequence`, i.e. "the Nth scoring event" -- not by actual elapsed
game-clock time. Period bands exist (`period_number` is tracked), but
within a period every step is evenly spaced regardless of how much game
clock actually elapsed between scores. The ESPN chart doesn't have this gap
-- `wp_payload` is genuinely time-proportional via `clock_seconds_elapsed`.
`src/fox.py`'s parsed play data would need to carry a real elapsed-time (or
game-clock) value per scoring step before the chart could plot on that
axis; unclear yet whether Fox's raw play-by-play exposes that at all.

## Team rankings go stale after discovery, never refreshed

Flagged 2026-08-18, user asked "week 1 '26 is still unranked, right?" --
confirmed: **yes**, 0/99 week 1 2026 games have `home_rank`/`away_rank` set
(vs. 23/96 for 2025 week 1, which was discovered after the season, and
polls, existed). `home_rank`/`away_rank` (src/db.py's `games` table) are
captured once at discovery time (`espn._parse_competition`) and never
updated afterward -- a game discovered in the preseason before the first AP
poll stays permanently unranked in our DB even after real rankings come
out, unless the whole week is manually re-discovered (`just discover`).

Same root cause and likely the same fix as the schedule-freshness gap noted
in the live-poller plan's "Deferred: automatic re-discovery" section (see
git log around 2026-08-18, "Make the live poller schedule-aware") --
`discover_games()` already re-upserts on every call, so periodic
re-discovery would fix both problems at once (stale kickoff times AND
stale rankings) rather than needing two separate mechanisms. Worth
designing together, not as two backlogs.
