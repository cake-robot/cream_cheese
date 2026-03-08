# Watchability Scoring Algorithm — Ideation

## Goal

Score each completed game on how "watchable" it was — i.e., how exciting, dramatic, and compelling the game would be for a neutral viewer watching a replay. Higher score = more worth watching.

---

## Available Data Points

### Per-game (from `games` table)
- **home_rank / away_rank** — AP/CFP top-25 ranking (NULL if unranked)
- **conference_game** — boolean
- **neutral_site** — boolean
- **home_score / away_score** — final score
- **attendance** — stadium attendance
- **initial_home_wp** — pregame win probability (from ESPN's model)
- **season_type** — regular (2) vs postseason (3)

### Per-play win probability series (from `win_probability` table)
- **home_win_pct** — ESPN's home win probability at each play (0.0–1.0)
- **tie_pct** — probability of a tie at each play
- **clock_seconds_elapsed** — normalized game clock (0–3600 regulation, synthetic >3600 for OT)
- **period_number** — quarter (1–4) or OT period (5+)
- **home_score / away_score** — score at each play

---

## Candidate Metrics

### 1. Win Probability Volatility
How much the WP line swung around during the game. Could be measured as:
- Sum of absolute deltas: `Σ |WP[i+1] - WP[i]|`
- Standard deviation of WP values
- Both capture "how much the lead felt uncertain"

### 2. Lead Changes
Number of times `home_win_pct` crosses 0.50. More lead changes = more drama.

### 3. Time Spent Close
Proportion of game time where WP was between 0.30 and 0.70 (i.e., either team could plausibly win). A game that spends 90% of its time in this band was competitive throughout.

### 4. Late-Game Drama
WP volatility specifically in the 4th quarter (elapsed > 2700s). A blowout that gets interesting late is more watchable than one that stays boring. Conversely, a close game that has a dramatic finish gets bonus points.

### 5. Comeback Magnitude
The largest WP deficit overcome by the eventual winner. A team that was at 15% WP and came back to win is more exciting than one that led wire-to-wire.

### 6. Final Margin
`|home_score - away_score|`. Closer final scores generally indicate more competitive games. Inverse relationship with watchability — smaller margin = better.

### 7. Scoring Volume
`home_score + away_score`. High-scoring games tend to be more entertaining (more action, more momentum shifts). A 45-42 game is generally more watchable than a 3-2 game.

### 8. Pregame Uncertainty
How close `initial_home_wp` is to 0.50. Games expected to be close are more likely to be interesting. `1 - 2 * |initial_home_wp - 0.5|` gives a 0–1 scale (1 = coin flip, 0 = heavy favorite).

### 9. Upset Factor
Did the pregame underdog win? Bonus if the underdog had a low initial WP. An upset where the team at 20% pregame WP wins is more dramatic than a 48% underdog winning.

### 10. Ranked Matchup Quality
- Both teams ranked: highest prestige
- One team ranked: moderate
- Neither ranked: lowest (but could still be a great game)

### 11. Overtime
Games that went to OT (period_number > 4) inherently had close regulation play. Binary bonus.

---

## Open Questions

- **Weighting**: How much should each metric contribute? WP-based drama metrics should probably dominate since they directly measure "was this game exciting to watch." Context metrics (ranking, conference) are more about "would you have cared about this game beforehand."
- **Normalization**: Each metric is on a different scale. Normalize to 0–1 before combining? Percentile rank across the season?
- **Blowout penalty**: Should there be a hard penalty for games where WP exceeded 0.90 for one team for most of the game?
- **"Garbage time" filtering**: Late scoring in blowouts can inflate scoring volume and create fake WP movement. Should we discount WP swings when one team is up 4+ scores?
- **Retrospective vs. prospective**: This is purely retrospective — "was this game good?" Not "will this game be good?" (which would rely more on rankings and pregame WP).

---