"""
Accounts: authentication, invite-code signup, and per-user state (spoiler
policy today, ratings later) -- backed by its own database, data/users.db,
kept separate from data/cfb.db so serve.py's connection to the pipeline
database can stay strictly mode=ro (see src/spoilers.py's and serve.py's
module docstrings for why that separation exists; cfb.db is derived and
rebuildable from ESPN, users.db is not).

This module owns every write to users.db, the same way src/spoilers.py owns
every write to data/spoilers.json -- so serve.py itself never executes SQL
against either database, which is what tests/test_readonly_invariant.py
mechanically checks (extended to cover this file too, see
tests/test_users_db_invariant.py).

Table ownership split: this module owns users/invites/user_spoiler_policy/
ratings as *storage* (schema, CRUD, the username-uniqueness and
invite-redemption transactions). It has no opinion on what a spoiler policy
*means* -- that stays entirely in src/spoilers.py, which stores its JSON
blob in the user_spoiler_policy table via get_policy_json/set_policy_json
below rather than owning a connection of its own.
"""

import pathlib
import secrets
import sqlite3
import threading

from werkzeug.security import check_password_hash, generate_password_hash

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = _REPO_ROOT / "data" / "users.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    note        TEXT,
    created_by  INTEGER REFERENCES users(user_id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    redeemed_by INTEGER REFERENCES users(user_id),
    redeemed_at TEXT
);

CREATE TABLE IF NOT EXISTS user_spoiler_policy (
    user_id     INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    policy_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Provisioned now, unused until the ratings follow-up (see the deployment
-- plan doc) -- so nothing has to migrate later. No FK to games.game_id:
-- that's a different database file, SQLite can't enforce a cross-file FK.
CREATE TABLE IF NOT EXISTS ratings (
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    game_id    TEXT NOT NULL,
    rating     INTEGER NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, game_id)
);
"""

# Serializes each multi-statement read-modify-write in this module (invite
# redemption, session-epoch bumps) and, via src/spoilers.py's per-user
# functions, the read-then-write policy update cycle too -- mirrors
# src/spoilers.py's own _LOCK for the same reason: waitress runs threaded,
# so two concurrent requests in this one process must not interleave a read
# and a write. SQLite's own locking keeps two separate *processes* safe;
# this is about two threads in this process racing between a SELECT and
# the UPDATE/INSERT that follows it. Public (no leading underscore)
# because src/spoilers.py's set_user_week/set_user_game/set_user_default
# need to hold it across their own get_policy_json + set_policy_json pair,
# not just within a single call into this module.
LOCK = threading.RLock()


class UsernameTaken(Exception):
    pass


class InvalidInvite(Exception):
    pass


def get_connection(path=None):
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path=None):
    if path is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    conn.executescript(SCHEMA)
    return conn


def get_user_by_username(conn, username):
    return conn.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
    ).fetchone()


def get_user_by_id(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def verify_password(user_row, password):
    return check_password_hash(user_row["password_hash"], password)


def create_user(conn, username, password, *, is_admin=False, invite_code=None):
    """Create an account. If invite_code is given, its redemption is part
    of the same transaction as the INSERT -- an invalid/already-redeemed
    code raises InvalidInvite and rolls back the user row along with it, so
    a code can never appear spent without a user existing, or vice versa.
    Pass invite_code=None only for the bootstrap admin path (see `just
    create-admin`); every signup-page account must supply one."""
    username = username.strip()
    if not username:
        raise ValueError("username must not be blank")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")

    with LOCK, conn:
        if get_user_by_username(conn, username) is not None:
            raise UsernameTaken(username)

        password_hash = generate_password_hash(password)
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, password_hash, 1 if is_admin else 0),
        )
        user_id = cur.lastrowid

        if invite_code is not None:
            redeemed = conn.execute(
                "UPDATE invites SET redeemed_by = ?, redeemed_at = datetime('now') "
                "WHERE code = ? AND redeemed_by IS NULL",
                (user_id, invite_code),
            )
            if redeemed.rowcount == 0:
                raise InvalidInvite(invite_code)  # rolls back the INSERT above too

    return get_user_by_id(conn, user_id)


def set_password(conn, user_id, new_password):
    """Also bumps session_epoch, invalidating every existing session cookie
    for this user -- a password change should log out anyone (including an
    attacker who had a stolen cookie) who isn't re-authenticating."""
    if len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")
    with LOCK, conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE user_id = ?",
            (generate_password_hash(new_password), user_id),
        )


def bump_session_epoch(conn, user_id):
    """Invalidate every existing session cookie for this user without
    touching their password -- e.g. an admin-initiated 'log out everywhere'."""
    with LOCK, conn:
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE user_id = ?", (user_id,))


def touch_last_seen(conn, user_id):
    with conn:
        conn.execute("UPDATE users SET last_seen_at = datetime('now') WHERE user_id = ?", (user_id,))


def create_invite(conn, created_by=None, note=None):
    code = secrets.token_urlsafe(12)
    with conn:
        conn.execute(
            "INSERT INTO invites (code, note, created_by) VALUES (?, ?, ?)",
            (code, note, created_by),
        )
    return code


def list_invites(conn):
    return conn.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()


# ---------------------------------------------------------------------------
# Spoiler policy storage -- pure key/value on behalf of src/spoilers.py,
# which owns the JSON shape, defaults, and normalization. This module only
# ever stores/retrieves the blob.
# ---------------------------------------------------------------------------

def get_policy_json(conn, user_id):
    row = conn.execute(
        "SELECT policy_json FROM user_spoiler_policy WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["policy_json"] if row is not None else None


def set_policy_json(conn, user_id, policy_json):
    with LOCK, conn:
        conn.execute(
            "INSERT INTO user_spoiler_policy (user_id, policy_json, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET policy_json = excluded.policy_json, "
            "updated_at = excluded.updated_at",
            (user_id, policy_json),
        )
