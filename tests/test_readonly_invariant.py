"""
Mechanical guard for the one invariant this whole feature was built around
preserving: serve.py must never open data/cfb.db in anything but read-only
mode. Every write serve.py performs -- accounts, invites, per-user spoiler
policy -- goes through src/users.py instead, against the separate
data/users.db (see that module's and src/spoilers.py's docstrings for why).
test_users_db_invariant.py is this file's companion, checking the same
separation from src/users.py's side.

Run with: ./venv/bin/python -m unittest discover tests
"""

import pathlib
import re
import unittest

SERVE_PY = pathlib.Path(__file__).resolve().parent.parent / "serve.py"


class TestReadOnlyInvariant(unittest.TestCase):
    def test_every_sqlite_connect_is_read_only(self):
        source = SERVE_PY.read_text()
        calls = re.findall(r"sqlite3\.connect\([^)]*\)", source)
        self.assertTrue(calls, "expected at least one sqlite3.connect(...) call in serve.py")
        for call in calls:
            self.assertIn("mode=ro", call, f"found a non-read-only sqlite3.connect in serve.py: {call}")

    def test_no_write_sql_verbs_against_the_games_db(self):
        # Cheap belt-and-braces: none of INSERT/UPDATE/DELETE/CREATE/DROP/
        # ALTER should appear in serve.py at all -- every write this
        # process performs goes through spoilers.save_policy() instead,
        # which touches data/spoilers.json, never cfb.db.
        source = SERVE_PY.read_text()
        write_verbs = re.findall(r'"\s*(INSERT|UPDATE|DELETE|CREATE TABLE|DROP TABLE|ALTER TABLE)\b', source, re.IGNORECASE)
        # The one expected exception: _startup_selfcheck()'s and
        # api_healthz()'s writability *probes*, which deliberately attempt
        # (and expect to fail) a CREATE TABLE to prove the connection is
        # really read-only.
        unexpected = [v for v in write_verbs if v.upper() != "CREATE TABLE"]
        self.assertEqual(unexpected, [], f"unexpected write verb(s) in serve.py: {unexpected}")


if __name__ == "__main__":
    unittest.main()
