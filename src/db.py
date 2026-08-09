import sqlite3
from .config import DB_PATH

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
    fox_event_id  INTEGER NOT NULL REFERENCES fox_events(fox_event_id),
    step_number   INTEGER NOT NULL,
    team          TEXT NOT NULL,       -- 'home' | 'away'
    new_value     INTEGER NOT NULL,
    delta         INTEGER NOT NULL,
    exact         INTEGER NOT NULL,    -- 1 = pinned to a flagged play; 0 = range-localized
    seq_lo        INTEGER,
    seq_hi        INTEGER,
    period_number INTEGER,
    evidence      TEXT,
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
            conference_game, neutral_site, venue_name,
            status_state, completed,
            home_score, away_score, attendance, initial_home_wp,
            detail_fetched, watchability_score
        ) VALUES (
            :game_id, :season_year, :season_type, :week, :game_date,
            :home_team_id, :home_team_abbr, :home_team_name, :home_rank,
            :away_team_id, :away_team_abbr, :away_team_name, :away_rank,
            :conference_game, :neutral_site, :venue_name,
            :status_state, :completed,
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
            status_state    = excluded.status_state,
            completed       = excluded.completed,
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
            exact, seq_lo, seq_hi, period_number, evidence
        ) VALUES (
            :fox_event_id, :step_number, :team, :new_value, :delta,
            :exact, :seq_lo, :seq_hi, :period_number, :evidence
        )
    """, [{**s, "fox_event_id": fox_event_id} for s in steps])


def mark_fox_pbp_fetched(conn, fox_event_id):
    conn.execute(
        "UPDATE fox_events SET pbp_fetched = 1, pbp_fetched_at = datetime('now') "
        "WHERE fox_event_id = ?",
        (fox_event_id,),
    )


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
