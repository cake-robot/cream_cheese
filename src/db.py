import gzip
import json
import sqlite3
from .config import DB_PATH

# win_probability.source: 'espn' (default, real ESPN WP data) or this --
# rows synthesized by src/fox_wp.py from Fox score-sequence data for games
# ESPN has no WP for at all. Kept as its own constant (not just a literal
# string) so db.py and src/fox_wp.py can't drift on the tag.
SYNTHETIC_WP_SOURCE = "fox_synthetic"

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id      TEXT PRIMARY KEY,
    abbreviation TEXT NOT NULL,
    name         TEXT NOT NULL,
    school     TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS games (
    game_id             TEXT PRIMARY KEY,
    season_year         INTEGER NOT NULL,
    season_type         INTEGER NOT NULL,
    week                INTEGER,
    game_date           TEXT NOT NULL,

    home_team_id        TEXT NOT NULL REFERENCES teams(team_id),
    home_team_abbr      TEXT NOT NULL,
    home_team_name      TEXT NOT NULL,
    home_rank           INTEGER,

    away_team_id        TEXT NOT NULL REFERENCES teams(team_id),
    away_team_abbr      TEXT NOT NULL,
    away_team_name      TEXT NOT NULL,
    away_rank           INTEGER,

    conference_game     INTEGER NOT NULL DEFAULT 0,
    neutral_site        INTEGER NOT NULL DEFAULT 0,
    venue_name          TEXT,

    status_state        TEXT NOT NULL,
    completed           INTEGER NOT NULL DEFAULT 0,

    home_score          INTEGER,
    away_score          INTEGER,
    attendance          INTEGER,

    initial_home_wp     REAL,

    detail_fetched      INTEGER NOT NULL DEFAULT 0,
    detail_fetched_at   TEXT,

    watchability_score  REAL,

    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_games_season ON games(season_year, season_type, week);
CREATE INDEX IF NOT EXISTS idx_games_detail ON games(completed, detail_fetched);
-- Kickoff-time lookups: the live poller's schedule query (src/live.py's
-- _schedule_interval) and serve.py's _default_slate_date both ask for
-- "MIN(game_date) WHERE status_state='pre' AND game_date >= now". Leading
-- with status_state makes this a covering index for both of those and for
-- the poller's "is anything live right now" count -- game_date alone leaves
-- the latter a full table scan.
CREATE INDEX IF NOT EXISTS idx_games_state_date ON games(status_state, game_date);

CREATE TRIGGER IF NOT EXISTS trg_games_updated_at
AFTER UPDATE ON games
BEGIN
    UPDATE games SET updated_at = datetime('now') WHERE game_id = NEW.game_id;
END;

CREATE TABLE IF NOT EXISTS win_probability (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                 TEXT NOT NULL,
    play_id                 TEXT NOT NULL,
    sequence_number         INTEGER,

    home_win_pct            REAL NOT NULL,
    tie_pct                 REAL NOT NULL DEFAULT 0.0,

    clock_seconds_elapsed   INTEGER,
    period_number           INTEGER,
    clock_display           TEXT,

    home_team_id            TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id            TEXT NOT NULL REFERENCES teams(team_id),

    home_score              INTEGER,
    away_score              INTEGER,

    play_sequence           INTEGER,
    source                  TEXT NOT NULL DEFAULT 'espn',

    FOREIGN KEY (game_id) REFERENCES games(game_id),
    UNIQUE (game_id, play_id)
);

CREATE INDEX IF NOT EXISTS idx_wp_game_seq ON win_probability(game_id, sequence_number);

CREATE TABLE IF NOT EXISTS game_metrics (
    game_id     TEXT NOT NULL REFERENCES games(game_id),
    metric_name TEXT NOT NULL,
    raw_value   REAL NOT NULL,
    norm_value  REAL NOT NULL,
    PRIMARY KEY (game_id, metric_name)
);

-- Archive of ESPN's full /summary payload for completed games, gzip-
-- compressed. Captured for free alongside the detail fetch fetch_details()
-- already does for WP/score parsing -- no extra network call. Escape hatch
-- for fields not (yet) parsed into the schema (down/distance, boxscore,
-- odds, drive results, ...) without re-fetching from ESPN.
CREATE TABLE IF NOT EXISTS game_raw_json (
    game_id          TEXT PRIMARY KEY REFERENCES games(game_id),
    raw_json_gzip    BLOB NOT NULL,
    raw_size         INTEGER NOT NULL,
    compressed_size  INTEGER NOT NULL,
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Fox Sports scoring-sequence pull. Standalone from `games` on purpose --
-- POC scope has no ESPN<->Fox matching yet (see plans), so fox_event_id is
-- the only identity here.
CREATE TABLE IF NOT EXISTS fox_events (
    fox_event_id   INTEGER PRIMARY KEY,
    status         TEXT NOT NULL,          -- 'ok' | 'missing' | 'error'
    event_date     TEXT,
    away_abbr      TEXT,
    home_abbr      TEXT,
    away_name      TEXT,                   -- Fox 'longName', e.g. 'Texas A&M' -- tracks
    home_name      TEXT,                   -- ESPN naming much better than away_abbr/home_abbr
    away_score     INTEGER,
    home_score     INTEGER,
    status_line    TEXT,
    in_window      INTEGER NOT NULL DEFAULT 0,
    pbp_fetched    INTEGER NOT NULL DEFAULT 0,
    pbp_fetched_at TEXT,
    probed_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fox_events_date ON fox_events(event_date);

CREATE TABLE IF NOT EXISTS fox_plays (
    fox_event_id      INTEGER NOT NULL REFERENCES fox_events(fox_event_id),
    play_sequence      INTEGER NOT NULL,
    fox_play_id        TEXT,
    period_number       INTEGER,
    group_id            TEXT,
    group_title         TEXT,
    play_title          TEXT,
    play_description    TEXT,
    time_of_play        TEXT,
    away_score          INTEGER,
    home_score          INTEGER,
    away_score_change   INTEGER NOT NULL DEFAULT 0,
    home_score_change   INTEGER NOT NULL DEFAULT 0,
    group_away_score    INTEGER,
    group_home_score    INTEGER,
    is_last_in_group     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fox_event_id, play_sequence)
);

CREATE TABLE IF NOT EXISTS fox_score_sequence (
    fox_event_id     INTEGER NOT NULL REFERENCES fox_events(fox_event_id),
    step_number      INTEGER NOT NULL,
    team             TEXT NOT NULL,       -- 'home' | 'away'
    new_value        INTEGER NOT NULL,
    delta            INTEGER NOT NULL,
    exact            INTEGER NOT NULL,    -- 1 = pinned to a flagged play; 0 = range-localized
    seq_lo           INTEGER,
    seq_hi           INTEGER,
    period_number    INTEGER,
    evidence         TEXT,
    elapsed_seconds  INTEGER,             -- x-axis position for the time-aligned score chart;
                                           -- synthetic in OT (see fox._assign_elapsed_seconds)
    clock_pinned     INTEGER NOT NULL DEFAULT 0, -- 1 = a PAT/2pt try pinned to its TD's clock
    try_type         TEXT,                -- TD steps only: 'pat' | 'two_point' | NULL (no try found)
    try_result       TEXT,                -- 'good' | 'failed' | NULL (found but Fox's text is ambiguous)
    try_evidence     TEXT,                -- the try's own play_description, incl. missed/blocked tries,
                                           -- which never get their own step (see fox._attach_try_results)
    try_decisive     TEXT,                -- exact substring of try_evidence that decided try_result,
                                           -- for highlighting just that part rather than the whole line
    PRIMARY KEY (fox_event_id, step_number)
);

-- One row per Fox team, harvested as a byproduct of every header parse
-- (regardless of whether that event is in-window or matched to anything) --
-- same "grows automatically as we pull more data" pattern `teams` uses via
-- upsert_team().
CREATE TABLE IF NOT EXISTS fox_teams (
    fox_team_id         INTEGER PRIMARY KEY,   -- numeric id from team.uri
    fox_abbr            TEXT,                  -- short 'name', e.g. 'TXA&M'
    fox_school_name     TEXT,                  -- 'longName', e.g. 'Texas A&M'
    fox_mascot          TEXT,                  -- 'stackedNameBottom', e.g. 'Aggies'
    fox_full_name       TEXT,                  -- entityLink.title, e.g. 'TEXAS A&M AGGIES'
    first_seen_event_id INTEGER,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The actual ESPN<->Fox key, one row per ESPN team. Wide/denormalized on
-- purpose -- the point is a table a person can SELECT * and read end to
-- end without joins, to verify the mapping is right.
CREATE TABLE IF NOT EXISTS team_crosswalk (
    espn_team_id     TEXT PRIMARY KEY REFERENCES teams(team_id),
    espn_abbr        TEXT NOT NULL,
    espn_school      TEXT NOT NULL,      -- bare school name, e.g. 'Virginia Tech'
    espn_name        TEXT NOT NULL,      -- full display name, e.g. 'Virginia Tech Hokies'
    fox_team_id      INTEGER REFERENCES fox_teams(fox_team_id),
    fox_abbr         TEXT,
    fox_school_name  TEXT,
    fox_mascot       TEXT,
    fox_full_name    TEXT,
    match_method     TEXT,               -- 'school_name' | 'alias' | 'manual' | NULL (unmatched)
    matched_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_team_crosswalk_fox_team ON team_crosswalk(fox_team_id);

-- The ESPN game_id <-> Fox event_id key, derived from team_crosswalk via
-- exact id lookup (see src/fox_match.py:match_game) -- no string comparison
-- at this layer.
CREATE TABLE IF NOT EXISTS fox_games (
    game_id       TEXT PRIMARY KEY REFERENCES games(game_id),
    fox_event_id  INTEGER NOT NULL REFERENCES fox_events(fox_event_id),
    matched_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fox_games_fox_event ON fox_games(fox_event_id);

-- Persisted, queryable record of every ESPN<->Fox reconciliation result
-- worth seeing: one row per (game, metric) for 'diff' games (the actual
-- corrections scoring.apply_corrections() trusts automatically), one row
-- per game for 'unusable' games (Fox's own data didn't reconcile with the
-- box score -- ESPN's original value is left untouched, flagged here for
-- visibility only). 'agree' games get no row -- nothing to see.
CREATE TABLE IF NOT EXISTS fox_score_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id       TEXT NOT NULL REFERENCES games(game_id),
    fox_event_id  INTEGER NOT NULL,
    tier          TEXT NOT NULL,   -- 'diff' | 'unusable'
    metric_name   TEXT,            -- NULL for 'unusable' (whole-game issue)
    espn_value    REAL,
    fox_value     REAL,
    notes         TEXT,
    reconciled_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fox_corrections_game ON fox_score_corrections(game_id);

-- Live/in-progress scoring. Deliberately separate from games.watchability_score
-- and game_metrics (the retrospective corpus those feed -- leaderboards,
-- analytics, the correlation matrix, and scoring.apply_corrections()'s manual
-- overrides) so a live value can never pollute it. One row per currently-live
-- game; deleted the moment the game completes (see live.handle_completions).
CREATE TABLE IF NOT EXISTS live_scores (
    game_id          TEXT PRIMARY KEY REFERENCES games(game_id),
    live_score       REAL,
    quality_so_far   REAL,               -- NULL = no applicable so-far weight yet
    drama_from_here  REAL NOT NULL,
    progress         REAL,               -- 0..1 fraction of regulation elapsed
    wp_now           REAL,
    n_wp_rows        INTEGER,
    so_far_weight    REAL,               -- applicable weight, for explainability
    from_here_weight REAL,
    headline         TEXT,
    cycle_seq        INTEGER,
    computed_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-metric live breakdown, mirroring game_metrics but keyed by half
-- ('so_far' | 'from_here') and with `applicable` stored explicitly (unlike
-- game_metrics' row-absence convention) -- "not applicable yet" is a state
-- the UI renders ("clutch finish -- window not open"), and a stable row set
-- across polls keeps an expanded UI row from flickering every cycle.
CREATE TABLE IF NOT EXISTS live_metrics (
    game_id     TEXT NOT NULL REFERENCES games(game_id),
    half        TEXT NOT NULL,           -- 'so_far' | 'from_here'
    metric_name TEXT NOT NULL,
    raw_value   REAL,
    norm_value  REAL,
    weight      REAL NOT NULL,
    applicable  INTEGER NOT NULL,
    PRIMARY KEY (game_id, half, metric_name)
);

-- Every computed live score, retained after the game completes (unlike
-- live_scores). This is the UI sparkline source and the verification
-- artifact for comparing a live run against the eventual retrospective score.
CREATE TABLE IF NOT EXISTS live_score_history (
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    computed_at     TEXT NOT NULL,
    progress        REAL,
    live_score      REAL,
    quality_so_far  REAL,
    drama_from_here REAL,
    PRIMARY KEY (game_id, computed_at)
);
CREATE INDEX IF NOT EXISTS idx_live_hist_game ON live_score_history(game_id, computed_at);

-- One row per outbound HTTP request to an external data source. The only two
-- functions in the repo that touch the network (src/espn.py's fetch_json and
-- src/fox.py's fetch_event) both write here, so this is a complete record by
-- construction -- a third source added later is logged by calling
-- fetchlog.record() from its own fetch helper.
--
-- No FK on game_id on purpose: the Fox id-walk probes event ids that map to no
-- game at all, and a scoreboard can return a game not yet in `games`. A logging
-- table must never be able to reject a write (PRAGMA foreign_keys is ON).
--
-- Retained indefinitely -- ~2 req/min while polling works out to roughly 50k
-- rows and ~7MB a season, which is not worth a pruning job.
CREATE TABLE IF NOT EXISTS fetch_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at   TEXT NOT NULL,        -- ISO8601 UTC, ms precision
    source         TEXT NOT NULL,        -- 'espn' | 'fox'
    endpoint_kind  TEXT NOT NULL,        -- 'scoreboard' | 'summary' | 'teams' | 'schedule' | 'event'
    url            TEXT NOT NULL,        -- API key redacted, see fetchlog._redact
    caller         TEXT,                 -- 'live' | 'discover' | 'detail' | 'fox-scan' | ...
    cycle_seq      INTEGER,              -- live poller cycle, NULL outside the poller
    game_id        TEXT,                 -- ESPN game id when the call is about one game
    source_ref     TEXT,                 -- source-native id (e.g. the Fox event id)
    attempt        INTEGER NOT NULL DEFAULT 1,   -- 1-based; Fox retries produce >1
    ok             INTEGER NOT NULL,     -- 1 = usable response returned to the caller
    http_status    INTEGER,              -- NULL on a connection/timeout error
    latency_ms     INTEGER,
    bytes          INTEGER,
    error          TEXT                  -- exception class + message, truncated
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_time ON fetch_log(requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_fetch_log_game ON fetch_log(game_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_fetch_log_bad  ON fetch_log(ok, requested_at DESC);

-- Current intent of a long-running poller: not history (that's fetch_log) but
-- "what is it about to do", which nothing else in the schema can answer.
-- src/live.py's _schedule_interval already computes the interval and a
-- human-readable reason for the log line; this persists that decision so the
-- Feed page can show it. One row, keyed by poller name.
--
-- A dead poller is detected by now() > next_wake_at + slack: run_forever writes
-- this every cycle, so a stale row IS the outage signal. stopped_at is set on a
-- clean shutdown to distinguish "deliberately stopped" from "crashed".
CREATE TABLE IF NOT EXISTS poller_state (
    poller            TEXT PRIMARY KEY,  -- 'live'
    pid               INTEGER,
    mode              TEXT,              -- 'normal' | 'shadow' | 'dry_run'
    started_at        TEXT,
    stopped_at        TEXT,              -- non-NULL only after a clean exit
    cycle_seq         INTEGER,
    last_cycle_at     TEXT,
    last_cycle_ms     INTEGER,
    last_cycle_reqs   INTEGER,
    last_cycle_error  TEXT,              -- traceback summary if the cycle raised
    slate_in          INTEGER,
    slate_post        INTEGER,
    slate_pre         INTEGER,
    next_wake_at      TEXT,
    interval_seconds  REAL,
    interval_reason   TEXT,              -- verbatim from _schedule_interval
    hold_awake        INTEGER,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_connection(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path=None):
    conn = get_connection(path)
    conn.executescript(SCHEMA)
    # Add play_sequence column if it doesn't exist yet (migration for existing DBs)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(win_probability)")}
    if "play_sequence" not in cols:
        conn.execute("ALTER TABLE win_probability ADD COLUMN play_sequence INTEGER")
    if "source" not in cols:
        conn.execute("ALTER TABLE win_probability ADD COLUMN source TEXT NOT NULL DEFAULT 'espn'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wp_game_play_seq ON win_probability(game_id, play_sequence)")

    fox_event_cols = {row[1] for row in conn.execute("PRAGMA table_info(fox_events)")}
    if "away_name" not in fox_event_cols:
        conn.execute("ALTER TABLE fox_events ADD COLUMN away_name TEXT")
    if "home_name" not in fox_event_cols:
        conn.execute("ALTER TABLE fox_events ADD COLUMN home_name TEXT")
    if "away_fox_team_id" not in fox_event_cols:
        conn.execute("ALTER TABLE fox_events ADD COLUMN away_fox_team_id INTEGER")
    if "home_fox_team_id" not in fox_event_cols:
        conn.execute("ALTER TABLE fox_events ADD COLUMN home_fox_team_id INTEGER")

    fox_play_cols = {row[1] for row in conn.execute("PRAGMA table_info(fox_plays)")}
    if "time_of_play" not in fox_play_cols:
        conn.execute("ALTER TABLE fox_plays ADD COLUMN time_of_play TEXT")

    fox_seq_cols = {row[1] for row in conn.execute("PRAGMA table_info(fox_score_sequence)")}
    if "elapsed_seconds" not in fox_seq_cols:
        conn.execute("ALTER TABLE fox_score_sequence ADD COLUMN elapsed_seconds INTEGER")
    if "clock_pinned" not in fox_seq_cols:
        conn.execute("ALTER TABLE fox_score_sequence ADD COLUMN clock_pinned INTEGER NOT NULL DEFAULT 0")
    if "try_type" not in fox_seq_cols:
        conn.execute("ALTER TABLE fox_score_sequence ADD COLUMN try_type TEXT")
    if "try_result" not in fox_seq_cols:
        conn.execute("ALTER TABLE fox_score_sequence ADD COLUMN try_result TEXT")
    if "try_evidence" not in fox_seq_cols:
        conn.execute("ALTER TABLE fox_score_sequence ADD COLUMN try_evidence TEXT")
    if "try_decisive" not in fox_seq_cols:
        conn.execute("ALTER TABLE fox_score_sequence ADD COLUMN try_decisive TEXT")

    game_cols = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
    if "event_note" not in game_cols:
        # ESPN's per-competition `notes` field -- a branded event label present
        # on bowls ("Duke's Mayo Bowl"), CFP rounds ("Semifinal at the Orange
        # Bowl"), and conference championships ("SEC Championship"), empty for
        # ordinary games. Captured going forward only -- existing rows are not
        # backfilled, so this is NULL for every game already in the DB.
        conn.execute("ALTER TABLE games ADD COLUMN event_note TEXT")

    if "rivalry_name" not in game_cols:
        # Static team-pair lookup (src/rivalries.py), not an ESPN field --
        # ESPN's API has no structured rivalry signal. Computed at parse time
        # from home/away team_id and stored here so slate/game-detail queries
        # don't need to import the lookup table themselves.
        conn.execute("ALTER TABLE games ADD COLUMN rivalry_name TEXT")

    # Live status mirror -- ESPN's comp.status block (period/clock/detail),
    # refreshed from the scoreboard every live-poll cycle. Always overwritten,
    # never historical (that's what live_score_history is for); belongs on
    # `games` because it's exactly one row per game and lets both the slate
    # query and the game-detail page read it without a join.
    if "status_period" not in game_cols:
        conn.execute("ALTER TABLE games ADD COLUMN status_period INTEGER")
    if "status_clock_display" not in game_cols:
        conn.execute("ALTER TABLE games ADD COLUMN status_clock_display TEXT")
    if "status_clock_seconds" not in game_cols:
        conn.execute("ALTER TABLE games ADD COLUMN status_clock_seconds REAL")
    if "status_detail" not in game_cols:
        conn.execute("ALTER TABLE games ADD COLUMN status_detail TEXT")
    if "live_updated_at" not in game_cols:
        conn.execute("ALTER TABLE games ADD COLUMN live_updated_at TEXT")

    # live_scores.decided removed -- the flag was found to force
    # drama_from_here to 0 on games that were still genuinely live (a real
    # final drive briefly crossing an extreme WP reading), while barely
    # moving the needle on true blowouts (ESPN's own WP model keeps
    # jittering rather than pinning near 1.0, so recent_volatility/
    # tension_now already read those as low without an override). Dropped
    # outright rather than deprecated -- live_scores is always-transient
    # data, cleared on every game completion, so there's no historical
    # value at stake.
    live_scores_cols = {row[1] for row in conn.execute("PRAGMA table_info(live_scores)")}
    if "decided" in live_scores_cols:
        conn.execute("ALTER TABLE live_scores DROP COLUMN decided")

    conn.commit()
    return conn


def compute_play_sequences(conn, game_id=None):
    """
    Assign play_sequence (1-based chronological rank) to win_probability rows,
    ordered by (period_number, sequence_number, id) per game.

    period_number is coarse and reliably parsed per play, so it's trusted to
    separate OT from regulation (the native WP-array order occasionally
    misplaces OT plays far too early). Within a period, native array order
    (sequence_number) is trusted over a re-derived clock_seconds_elapsed,
    which was found to scramble otherwise-correctly-ordered drives whose
    computed elapsed time ranges spuriously overlap.
    """
    if game_id:
        game_ids = [game_id]
    else:
        game_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT game_id FROM win_probability"
        )]

    for gid in game_ids:
        rows = conn.execute(
            "SELECT id FROM win_probability WHERE game_id = ? ORDER BY period_number, sequence_number, id",
            (gid,),
        ).fetchall()
        conn.executemany(
            "UPDATE win_probability SET play_sequence = ? WHERE id = ?",
            [(rank, row[0]) for rank, row in enumerate(rows, 1)],
        )

    conn.commit()
    return len(game_ids)


def upsert_team(conn, team_id, abbreviation, name, school=None):
    conn.execute("""
        INSERT INTO teams (team_id, abbreviation, name, school)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            abbreviation = excluded.abbreviation,
            name         = excluded.name,
            school     = COALESCE(excluded.school, teams.school),
            updated_at   = datetime('now')
    """, (team_id, abbreviation, name, school))


def upsert_game(conn, game):
    upsert_team(conn, game["home_team_id"], game["home_team_abbr"], game["home_team_name"])
    upsert_team(conn, game["away_team_id"], game["away_team_abbr"], game["away_team_name"])
    conn.execute("""
        INSERT INTO games (
            game_id, season_year, season_type, week, game_date,
            home_team_id, home_team_abbr, home_team_name, home_rank,
            away_team_id, away_team_abbr, away_team_name, away_rank,
            conference_game, neutral_site, venue_name, event_note, rivalry_name,
            status_state, completed,
            status_period, status_clock_display, status_clock_seconds, status_detail,
            live_updated_at,
            home_score, away_score, attendance, initial_home_wp,
            detail_fetched, watchability_score
        ) VALUES (
            :game_id, :season_year, :season_type, :week, :game_date,
            :home_team_id, :home_team_abbr, :home_team_name, :home_rank,
            :away_team_id, :away_team_abbr, :away_team_name, :away_rank,
            :conference_game, :neutral_site, :venue_name, :event_note, :rivalry_name,
            :status_state, :completed,
            :status_period, :status_clock_display, :status_clock_seconds, :status_detail,
            CASE WHEN :status_period IS NOT NULL THEN datetime('now') ELSE NULL END,
            :home_score, :away_score, :attendance, :initial_home_wp,
            :detail_fetched, :watchability_score
        )
        ON CONFLICT(game_id) DO UPDATE SET
            season_year     = excluded.season_year,
            season_type     = excluded.season_type,
            week            = excluded.week,
            game_date       = excluded.game_date,
            home_team_id    = excluded.home_team_id,
            home_team_abbr  = excluded.home_team_abbr,
            home_team_name  = excluded.home_team_name,
            home_rank       = excluded.home_rank,
            away_team_id    = excluded.away_team_id,
            away_team_abbr  = excluded.away_team_abbr,
            away_team_name  = excluded.away_team_name,
            away_rank       = excluded.away_rank,
            conference_game = excluded.conference_game,
            neutral_site    = excluded.neutral_site,
            venue_name      = excluded.venue_name,
            event_note      = excluded.event_note,
            rivalry_name    = excluded.rivalry_name,
            status_state    = excluded.status_state,
            completed       = excluded.completed,
            status_period        = excluded.status_period,
            status_clock_display = excluded.status_clock_display,
            status_clock_seconds = excluded.status_clock_seconds,
            status_detail        = excluded.status_detail,
            live_updated_at = CASE WHEN excluded.status_period IS NOT NULL
                                    THEN datetime('now') ELSE games.live_updated_at END,
            home_score      = COALESCE(excluded.home_score, games.home_score),
            away_score      = COALESCE(excluded.away_score, games.away_score),
            watchability_score = COALESCE(excluded.watchability_score, games.watchability_score)
    """, {
        **game,
        "home_score": game.get("home_score"),
        "away_score": game.get("away_score"),
        "attendance": game.get("attendance"),
        "initial_home_wp": game.get("initial_home_wp"),
        "detail_fetched": game.get("detail_fetched", 0),
        "watchability_score": game.get("watchability_score"),
        "event_note": game.get("event_note"),
        "rivalry_name": game.get("rivalry_name"),
        "status_period": game.get("status_period"),
        "status_clock_display": game.get("status_clock_display"),
        "status_clock_seconds": game.get("status_clock_seconds"),
        "status_detail": game.get("status_detail"),
    })


def upsert_win_probability(conn, rows):
    conn.executemany("""
        INSERT OR IGNORE INTO win_probability (
            game_id, play_id, sequence_number,
            home_win_pct, tie_pct,
            clock_seconds_elapsed, period_number, clock_display,
            home_team_id, away_team_id,
            home_score, away_score
        ) VALUES (
            :game_id, :play_id, :sequence_number,
            :home_win_pct, :tie_pct,
            :clock_seconds_elapsed, :period_number, :clock_display,
            :home_team_id, :away_team_id,
            :home_score, :away_score
        )
    """, rows)


def replace_synthetic_wp(conn, game_id, home_team_id, away_team_id, rows):
    """
    Replace this game's fox-synthetic win_probability rows wholesale
    (source=SYNTHETIC_WP_SOURCE) -- idempotent on a re-run. Only deletes
    rows carrying that source, so a real ESPN pull backfilling this game
    later (upsert_win_probability, source defaults to 'espn') coexists
    untouched rather than being clobbered by this.

    rows: from fox_wp.build_synthetic_wp_rows() -- each a dict with
    home_win_pct, home_score, away_score, period_number,
    clock_seconds_elapsed. Ordered already (chronological), so play_sequence
    is assigned directly from list position rather than needing a separate
    compute_play_sequences() pass.
    """
    conn.execute(
        "DELETE FROM win_probability WHERE game_id = ? AND source = ?",
        (game_id, SYNTHETIC_WP_SOURCE),
    )
    conn.executemany("""
        INSERT INTO win_probability (
            game_id, play_id, sequence_number, play_sequence,
            home_win_pct, tie_pct,
            clock_seconds_elapsed, period_number, clock_display,
            home_team_id, away_team_id,
            home_score, away_score, source
        ) VALUES (
            :game_id, :play_id, :sequence_number, :play_sequence,
            :home_win_pct, 0.0,
            :clock_seconds_elapsed, :period_number, NULL,
            :home_team_id, :away_team_id,
            :home_score, :away_score, :source
        )
    """, [
        {
            "game_id": game_id,
            "play_id": f"fox-synth-{i}",
            "sequence_number": i,
            "play_sequence": i,
            "home_win_pct": r["home_win_pct"],
            "clock_seconds_elapsed": r["clock_seconds_elapsed"],
            "period_number": r["period_number"],
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "source": SYNTHETIC_WP_SOURCE,
        }
        for i, r in enumerate(rows, 1)
    ])


def update_watchability_score(conn, game_id, score):
    conn.execute("UPDATE games SET watchability_score = ? WHERE game_id = ?", (score, game_id))


def upsert_game_metrics(conn, game_id, breakdown):
    """
    Write per-metric breakdown rows. breakdown: {metric_name: {raw, normalized, weighted}}.
    Entries with raw=None ("not applicable" for this game, e.g. clutch_finish
    for an OT game) are skipped entirely rather than written -- no row means
    not applicable, distinct from a row scored 0.
    """
    rows = [
        (game_id, name, vals["raw"], vals["normalized"])
        for name, vals in breakdown.items()
        if vals["raw"] is not None
    ]
    if not rows:
        return
    conn.executemany("""
        INSERT INTO game_metrics (game_id, metric_name, raw_value, norm_value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(game_id, metric_name) DO UPDATE SET
            raw_value  = excluded.raw_value,
            norm_value = excluded.norm_value
    """, rows)


def upsert_game_raw_json(conn, game_id, summary):
    """Archive ESPN's full /summary payload for `game_id`, gzip-compressed."""
    raw = json.dumps(summary, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9)
    conn.execute("""
        INSERT INTO game_raw_json (game_id, raw_json_gzip, raw_size, compressed_size)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            raw_json_gzip   = excluded.raw_json_gzip,
            raw_size        = excluded.raw_size,
            compressed_size = excluded.compressed_size,
            fetched_at      = datetime('now')
    """, (game_id, compressed, len(raw), len(compressed)))


def get_game_raw_json(conn, game_id):
    """Return the archived ESPN /summary payload for `game_id` as a dict, or None."""
    row = conn.execute(
        "SELECT raw_json_gzip FROM game_raw_json WHERE game_id = ?", (game_id,)
    ).fetchone()
    if not row:
        return None
    return json.loads(gzip.decompress(row["raw_json_gzip"]))


def upsert_fox_event(conn, event):
    """
    event: {fox_event_id, status, event_date, away_abbr, home_abbr,
            away_name, home_name, away_fox_team_id, home_fox_team_id,
            away_score, home_score, status_line, in_window}
    Every probed ID gets a row regardless of outcome (hit/missing/error) so
    a re-run of the walk costs ~0 requests.
    """
    conn.execute("""
        INSERT INTO fox_events (
            fox_event_id, status, event_date, away_abbr, home_abbr,
            away_name, home_name, away_fox_team_id, home_fox_team_id,
            away_score, home_score, status_line, in_window
        ) VALUES (
            :fox_event_id, :status, :event_date, :away_abbr, :home_abbr,
            :away_name, :home_name, :away_fox_team_id, :home_fox_team_id,
            :away_score, :home_score, :status_line, :in_window
        )
        ON CONFLICT(fox_event_id) DO UPDATE SET
            status           = excluded.status,
            event_date       = excluded.event_date,
            away_abbr        = excluded.away_abbr,
            home_abbr        = excluded.home_abbr,
            away_name        = excluded.away_name,
            home_name        = excluded.home_name,
            away_fox_team_id = excluded.away_fox_team_id,
            home_fox_team_id = excluded.home_fox_team_id,
            away_score       = excluded.away_score,
            home_score       = excluded.home_score,
            status_line      = excluded.status_line,
            in_window        = excluded.in_window,
            probed_at        = datetime('now')
    """, event)


def upsert_fox_team(conn, team):
    """team: {fox_team_id, abbr, school_name, mascot, full_name, first_seen_event_id}"""
    conn.execute("""
        INSERT INTO fox_teams (
            fox_team_id, fox_abbr, fox_school_name, fox_mascot, fox_full_name,
            first_seen_event_id
        ) VALUES (
            :fox_team_id, :abbr, :school_name, :mascot, :full_name,
            :first_seen_event_id
        )
        ON CONFLICT(fox_team_id) DO UPDATE SET
            fox_abbr        = excluded.fox_abbr,
            fox_school_name = excluded.fox_school_name,
            fox_mascot      = excluded.fox_mascot,
            fox_full_name   = excluded.fox_full_name,
            updated_at      = datetime('now')
    """, team)


def seed_team_crosswalk(conn, espn_team_id, espn_abbr, espn_school, espn_name):
    """Insert the ESPN side of a crosswalk row if it doesn't exist yet --
    never overwrites an already-resolved fox_* match on a re-run."""
    conn.execute("""
        INSERT INTO team_crosswalk (espn_team_id, espn_abbr, espn_school, espn_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(espn_team_id) DO UPDATE SET
            espn_abbr   = excluded.espn_abbr,
            espn_school = excluded.espn_school,
            espn_name   = excluded.espn_name
    """, (espn_team_id, espn_abbr, espn_school, espn_name))


def set_team_crosswalk_match(conn, espn_team_id, fox_team_id, match_method):
    """Fill in the Fox side of a crosswalk row, denormalizing from fox_teams
    so team_crosswalk stays a single wide table a person can read without
    joining."""
    conn.execute("""
        UPDATE team_crosswalk SET
            fox_team_id     = :fox_team_id,
            fox_abbr        = (SELECT fox_abbr FROM fox_teams WHERE fox_team_id = :fox_team_id),
            fox_school_name = (SELECT fox_school_name FROM fox_teams WHERE fox_team_id = :fox_team_id),
            fox_mascot      = (SELECT fox_mascot FROM fox_teams WHERE fox_team_id = :fox_team_id),
            fox_full_name   = (SELECT fox_full_name FROM fox_teams WHERE fox_team_id = :fox_team_id),
            match_method    = :match_method,
            matched_at      = datetime('now')
        WHERE espn_team_id = :espn_team_id
    """, {"espn_team_id": espn_team_id, "fox_team_id": fox_team_id, "match_method": match_method})


def upsert_fox_game(conn, game_id, fox_event_id):
    conn.execute("""
        INSERT INTO fox_games (game_id, fox_event_id)
        VALUES (?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            fox_event_id = excluded.fox_event_id,
            matched_at   = datetime('now')
    """, (game_id, fox_event_id))


def replace_fox_score_corrections(conn, game_id, rows):
    """rows: list of {fox_event_id, tier, metric_name, espn_value, fox_value, notes}.
    Replaces this game's rows wholesale so a re-reconcile is idempotent."""
    conn.execute("DELETE FROM fox_score_corrections WHERE game_id = ?", (game_id,))
    if not rows:
        return
    conn.executemany("""
        INSERT INTO fox_score_corrections (
            game_id, fox_event_id, tier, metric_name, espn_value, fox_value, notes
        ) VALUES (
            :game_id, :fox_event_id, :tier, :metric_name, :espn_value, :fox_value, :notes
        )
    """, [{**r, "game_id": game_id} for r in rows])


def upsert_fox_plays(conn, fox_event_id, rows):
    conn.execute("DELETE FROM fox_plays WHERE fox_event_id = ?", (fox_event_id,))
    conn.executemany("""
        INSERT INTO fox_plays (
            fox_event_id, play_sequence, fox_play_id, period_number,
            group_id, group_title, play_title, play_description, time_of_play,
            away_score, home_score, away_score_change, home_score_change,
            group_away_score, group_home_score, is_last_in_group
        ) VALUES (
            :fox_event_id, :play_sequence, :fox_play_id, :period_number,
            :group_id, :group_title, :play_title, :play_description, :time_of_play,
            :away_score, :home_score, :away_score_change, :home_score_change,
            :group_away_score, :group_home_score, :is_last_in_group
        )
    """, [{**r, "fox_event_id": fox_event_id} for r in rows])


def replace_fox_score_sequence(conn, fox_event_id, steps):
    conn.execute("DELETE FROM fox_score_sequence WHERE fox_event_id = ?", (fox_event_id,))
    conn.executemany("""
        INSERT INTO fox_score_sequence (
            fox_event_id, step_number, team, new_value, delta,
            exact, seq_lo, seq_hi, period_number, evidence,
            elapsed_seconds, clock_pinned, try_type, try_result, try_evidence, try_decisive
        ) VALUES (
            :fox_event_id, :step_number, :team, :new_value, :delta,
            :exact, :seq_lo, :seq_hi, :period_number, :evidence,
            :elapsed_seconds, :clock_pinned, :try_type, :try_result, :try_evidence, :try_decisive
        )
    """, [{**s, "fox_event_id": fox_event_id} for s in steps])


def mark_fox_pbp_fetched(conn, fox_event_id):
    conn.execute(
        "UPDATE fox_events SET pbp_fetched = 1, pbp_fetched_at = datetime('now') "
        "WHERE fox_event_id = ?",
        (fox_event_id,),
    )


def set_initial_home_wp(conn, game_id, value):
    """Write initial_home_wp only if it isn't already set. A narrow,
    side-effect-free alternative to mark_detail_fetched() for the live poller:
    mark_detail_fetched also sets detail_fetched=1 and overwrites
    home_score/away_score/attendance, none of which should happen while a
    game is still live (detail_fetched must stay 0 so the normal pipeline
    picks the game up cleanly at completion -- see live.handle_completions)."""
    if value is None:
        return
    conn.execute(
        "UPDATE games SET initial_home_wp = ? WHERE game_id = ? AND initial_home_wp IS NULL",
        (value, game_id),
    )


def delete_win_probability(conn, game_id):
    """Discard every win_probability row for a game. Used at the live->final
    transition: upsert_win_probability is INSERT OR IGNORE keyed on
    (game_id, play_id), so a play first seen while still part of the live
    active drive keeps whatever period/clock/sequence it had at that moment
    forever -- it can never be corrected in place by a later upsert. A clean
    delete-then-refetch via the normal pipeline.fetch_details() path
    guarantees a live-touched game's WP series ends up byte-identical to one
    that was only ever fetched after the fact."""
    conn.execute("DELETE FROM win_probability WHERE game_id = ?", (game_id,))


def upsert_live_score(conn, game_id, result):
    """result: {live_score, quality_so_far, drama_from_here, progress,
    wp_now, n_wp_rows, so_far_weight, from_here_weight, headline, cycle_seq}."""
    conn.execute("""
        INSERT INTO live_scores (
            game_id, live_score, quality_so_far, drama_from_here,
            progress, wp_now, n_wp_rows, so_far_weight, from_here_weight,
            headline, cycle_seq, computed_at
        ) VALUES (
            :game_id, :live_score, :quality_so_far, :drama_from_here,
            :progress, :wp_now, :n_wp_rows, :so_far_weight, :from_here_weight,
            :headline, :cycle_seq, datetime('now')
        )
        ON CONFLICT(game_id) DO UPDATE SET
            live_score       = excluded.live_score,
            quality_so_far   = excluded.quality_so_far,
            drama_from_here  = excluded.drama_from_here,
            progress         = excluded.progress,
            wp_now           = excluded.wp_now,
            n_wp_rows        = excluded.n_wp_rows,
            so_far_weight    = excluded.so_far_weight,
            from_here_weight = excluded.from_here_weight,
            headline         = excluded.headline,
            cycle_seq        = excluded.cycle_seq,
            computed_at      = datetime('now')
    """, {"game_id": game_id, **result})


def replace_live_metrics(conn, game_id, halves):
    """halves: {"so_far": {metric_name: {raw, normalized, weighted, weight,
    applicable}}, "from_here": {...}}. Deletes and reinserts this game's
    live_metrics wholesale each cycle -- unlike game_metrics, `applicable` is
    stored explicitly (not row-absence), so this keeps the row set stable
    across polls (no UI flicker) while still reflecting the current cycle."""
    conn.execute("DELETE FROM live_metrics WHERE game_id = ?", (game_id,))
    rows = []
    for half, metrics in halves.items():
        for name, v in metrics.items():
            rows.append((
                game_id, half, name, v.get("raw"), v.get("normalized"),
                v.get("weight"), int(bool(v.get("applicable"))),
            ))
    if not rows:
        return
    conn.executemany("""
        INSERT INTO live_metrics (game_id, half, metric_name, raw_value, norm_value, weight, applicable)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, rows)


def append_live_history(conn, game_id, result):
    conn.execute("""
        INSERT OR REPLACE INTO live_score_history (
            game_id, computed_at, progress, live_score, quality_so_far, drama_from_here
        ) VALUES (?, datetime('now'), ?, ?, ?, ?)
    """, (game_id, result.get("progress"), result.get("live_score"),
          result.get("quality_so_far"), result.get("drama_from_here")))


def insert_fetch_log(conn, row):
    """row: dict with fetch_log's columns (requested_at/source/endpoint_kind/
    url/ok are the only required keys -- everything else defaults to NULL).
    Plain INSERT -- there's nothing to conflict on, every request is its own
    row. Used exclusively by src/fetchlog.py, which owns its own connection
    and swallows any exception this raises (a logging table must never be
    able to take down the thing it's observing)."""
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    conn.execute(
        f"INSERT INTO fetch_log ({', '.join(cols)}) VALUES ({placeholders})",
        row,
    )


def upsert_poller_state(conn, poller, **fields):
    """Writes only the columns passed in `fields`, so run_forever can update
    e.g. just the schedule fields (next_wake_at/interval_seconds/
    interval_reason/hold_awake) after a sleep decision without clobbering the
    cycle fields (last_cycle_at/last_cycle_ms/...) written after the prior
    run_cycle, and vice versa. updated_at always advances regardless of which
    fields were passed."""
    cols = list(fields.keys())
    insert_cols = ["poller"] + cols
    insert_vals = [poller] + [fields[c] for c in cols]
    placeholders = ", ".join("?" for _ in insert_cols)
    update_clause = ", ".join([f"{c} = excluded.{c}" for c in cols] + ["updated_at = datetime('now')"])
    conn.execute(f"""
        INSERT INTO poller_state ({', '.join(insert_cols)}, updated_at)
        VALUES ({placeholders}, datetime('now'))
        ON CONFLICT(poller) DO UPDATE SET
            {update_clause}
    """, insert_vals)


def clear_live_score(conn, game_id):
    """Remove a game's live_scores/live_metrics rows once it has completed
    and been picked up by the normal retrospective scorer. live_score_history
    is deliberately NOT cleared -- it's the verification/sparkline record."""
    conn.execute("DELETE FROM live_scores WHERE game_id = ?", (game_id,))
    conn.execute("DELETE FROM live_metrics WHERE game_id = ?", (game_id,))


def live_game_ids(conn):
    """game_ids currently holding a live_scores row (i.e. tracked as live by
    a prior cycle) -- used to detect the in->post transition."""
    return [r[0] for r in conn.execute("SELECT game_id FROM live_scores")]


def mark_detail_fetched(conn, game_id, home_score, away_score, attendance, initial_home_wp):
    conn.execute("""
        UPDATE games SET
            detail_fetched    = 1,
            detail_fetched_at = datetime('now'),
            home_score        = COALESCE(:home_score, home_score),
            away_score        = COALESCE(:away_score, away_score),
            attendance        = COALESCE(:attendance, attendance),
            initial_home_wp   = COALESCE(:initial_home_wp, initial_home_wp)
        WHERE game_id = :game_id
    """, {
        "game_id": game_id,
        "home_score": home_score,
        "away_score": away_score,
        "attendance": attendance,
        "initial_home_wp": initial_home_wp,
    })
