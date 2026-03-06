import sqlite3
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id      TEXT PRIMARY KEY,
    abbreviation TEXT NOT NULL,
    name         TEXT NOT NULL,
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

    FOREIGN KEY (game_id) REFERENCES games(game_id),
    UNIQUE (game_id, play_id)
);

CREATE INDEX IF NOT EXISTS idx_wp_game_seq ON win_probability(game_id, sequence_number);
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
    conn.commit()
    return conn


def upsert_team(conn, team_id, abbreviation, name):
    conn.execute("""
        INSERT INTO teams (team_id, abbreviation, name)
        VALUES (?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            abbreviation = excluded.abbreviation,
            name         = excluded.name,
            updated_at   = datetime('now')
    """, (team_id, abbreviation, name))


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
