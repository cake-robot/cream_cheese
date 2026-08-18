"""
Companion to test_readonly_invariant.py: that file guards cfb.db staying
read-only from serve.py's side. This guards the other half of the same
separation from src/users.py's side -- accounts/invites/per-user spoiler
policy live in their own database (data/users.db) specifically so cfb.db
never needs a write connection at all (see src/users.py's and
src/spoilers.py's module docstrings). Two checks: a textual one (this
module has no path to cfb.db's config at all) and a behavioral one (a real
users.db, freshly built by src/users.py, and the real cfb.db never share a
single table name).

Run with: ./venv/bin/python -m unittest discover tests
"""

import pathlib
import re
import sqlite3
import tempfile
import unittest

from src import users

import serve

USERS_PY = pathlib.Path(__file__).resolve().parent.parent / "src" / "users.py"


class TestUsersDbInvariant(unittest.TestCase):
    def test_no_coupling_to_cfb_db_config(self):
        source = USERS_PY.read_text()
        self.assertNotIn("from .config import", source)
        self.assertNotIn("from . import config", source)
        self.assertNotRegex(source, r"\bconfig\.DB_PATH\b")

    def test_exactly_one_connect_call_and_it_uses_this_modules_own_db_path(self):
        source = USERS_PY.read_text()
        calls = re.findall(r"sqlite3\.connect\([^)]*\)", source)
        self.assertEqual(len(calls), 1, f"expected exactly one sqlite3.connect(...) call, found {calls}")
        self.assertIn("DB_PATH", calls[0])

    def test_users_db_and_cfb_db_share_no_table_names(self):
        # sqlite_sequence is SQLite's own internal bookkeeping table
        # (present because `users` uses AUTOINCREMENT), not a real schema
        # collision -- excluded rather than a false positive on every run.
        def _real_tables(conn):
            return {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not r[0].startswith("sqlite_")
            }

        with tempfile.TemporaryDirectory() as d:
            users_db_path = pathlib.Path(d) / "users_invariant_test.db"
            conn = users.init_db(users_db_path)
            users_tables = _real_tables(conn)
            conn.close()

        cfb_conn = sqlite3.connect(f"file:{serve.DB_FILE}?mode=ro", uri=True)
        cfb_tables = _real_tables(cfb_conn)
        cfb_conn.close()

        overlap = users_tables & cfb_tables
        self.assertEqual(overlap, set(), f"users.db and cfb.db must never share table names, found {overlap}")
        # Sanity: both non-empty, so an empty set on either side wouldn't
        # make the assertion above trivially true.
        self.assertTrue(users_tables)
        self.assertTrue(cfb_tables)


if __name__ == "__main__":
    unittest.main()
