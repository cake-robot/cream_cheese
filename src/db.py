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
    conn.commit()
    return conn


def compute_play_sequences(conn, game_id=None):
    """
    Assign play_sequence (1-based chronological rank) to win_probability rows,
    ordered by (clock_seconds_elapsed, id) per game.
    """
    if game_id:
        game_ids = [game_id]
    else:
        game_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT game_id FROM win_probability"
        )]

    for gid in game_ids:
        rows = conn.execute(
            "SELECT id FROM win_probability WHERE game_id = ? ORDER BY clock_seconds_elapsed, id",
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
    """Write per-metric breakdown rows. breakdown: {metric_name: {raw, normalized, weighted}}."""
    conn.executemany("""
        INSERT INTO game_metrics (game_id, metric_name, raw_value, norm_value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(game_id, metric_name) DO UPDATE SET
            raw_value  = excluded.raw_value,
            norm_value = excluded.norm_value
    """, [
        (game_id, name, vals["raw"], vals["normalized"])
        for name, vals in breakdown.items()
    ])


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
