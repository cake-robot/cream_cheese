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

## Fox score-chart tooltip: point-differential signal

Flagged 2026-08-19. The tooltip shows the running score ("FRES 7 – KU 21")
but never the size of the jump that produced it -- reading a TD (+6, or +7
with the PAT) vs. a FG (+3) vs. a safety (+2) means mentally diffing
against whatever the previous hover showed. Earlier review mockups
(https://claude.ai/code/artifact/e5f78efd-008e-4bab-af0c-423dfc85ff2b,
options 1 and 2) showed a "(+7)" badge next to the score line; it didn't
carry into the shipped option 3. Add it back in `moveTo()`
(web/charts.js) -- for every scoring marker, not just merged TD+try
groups, so a bare FG reads "+3" and a lone defensive/special-teams TD
reads "+6" too.

## Fox tooltip: OT mandatory two-point tries

Flagged 2026-08-19, user referenced Oregon @ Penn State 2025 (game_id
401752854, fox_event_id 41759) while asking about tooltip polish. Checked
-- the try-result pipeline already gets this one right: Oregon's 2OT
touchdown (step 15) correctly shows a FAILED two-point try (mandatory
from the 2nd OT on, no PAT option in CFB rules), final score 30-24
matches the box score. It's a rich edge case worth keeping as a reference
-- that particular play was actually an interception returned toward a
defensive 2-point score that also failed ("TWO-POINT ATTEMPT FAILS.
DEFENSIVE CONVERSION RECOVERY FAILS." in Fox's own text), and
`_classify_try()` (src/fox.py) still classified it correctly as a failed
try for Oregon.

Nothing broken, but the OT-mandatory-2pt rule itself isn't called out
anywhere in the UI -- a viewer who doesn't know that rule could see a 2OT+
"two-point conversion" tooltip and wonder why the team didn't just kick
it. Possible future polish: a period-aware label distinguishing "2pt
(choice)" from "2pt (mandatory, OT)" if this becomes a recurring point of
confusion.

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
