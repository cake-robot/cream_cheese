"""
API-level tests for the spoiler feature, run against the real (read-only)
data/cfb.db via Flask's test client. No fixtures needed -- 2026 gives ~900
naturally-hidden scheduled games under the default policy, and 2025 gives a
large revealed, scored corpus to hide/reveal in individual tests.

Each test gets its own scratch spoilers.json (spoilers.POLICY_PATH
monkeypatched to a tempdir), so nothing here touches the repo's real
data/spoilers.json and tests don't interfere with each other.

Run with: ./venv/bin/python -m unittest discover tests
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src import spoilers

import serve

SPOILER_NUMERIC_FIELDS = ("watchability_score", "rank", "percentile", "n_scored", "applicable_weight")


def _walk_and_check(node, path, failures):
    """Recursively walk a decoded JSON response; for any dict carrying
    spoiler_hidden: true, record any field that should have been redacted
    but wasn't. Mirrors serve.py's own dev-only _walk_spoiler_leaks, but
    runs unconditionally here (not gated on app.debug)."""
    if isinstance(node, dict):
        if node.get("spoiler_hidden") is True:
            for f in SPOILER_NUMERIC_FIELDS:
                if f in node and node[f] is not None:
                    failures.append(f"{path}.{f} is non-null on a spoiler_hidden game")
            away, home = node.get("away"), node.get("home")
            if isinstance(away, dict) and away.get("score") is not None:
                failures.append(f"{path}.away.score is non-null on a spoiler_hidden game")
            if isinstance(home, dict) and home.get("score") is not None:
                failures.append(f"{path}.home.score is non-null on a spoiler_hidden game")
            if node.get("ot") is not None:
                failures.append(f"{path}.ot is non-null on a spoiler_hidden game")
            if node.get("metrics"):
                failures.append(f"{path}.metrics is non-empty on a spoiler_hidden game")
        for k, v in node.items():
            _walk_and_check(v, f"{path}.{k}", failures)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_and_check(v, f"{path}[{i}]", failures)


def _raw_db():
    return sqlite3.connect(f"file:{serve.DB_FILE}?mode=ro", uri=True)


class SpoilerApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_path = spoilers.POLICY_PATH
        spoilers.POLICY_PATH = Path(self._tmpdir) / "spoilers.json"
        spoilers._cache["key"] = None
        spoilers._cache["policy"] = None
        serve.app.testing = True
        self.client = serve.app.test_client()

    def tearDown(self):
        spoilers.POLICY_PATH = self._orig_path
        spoilers._cache["key"] = None
        spoilers._cache["policy"] = None
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def get(self, path, **params):
        resp = self.client.get(path, query_string=params)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def post(self, path, body):
        resp = self.client.post(path, json=body)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def _any_2025_scored_game(self):
        data = self.get("/api/games", limit=1, sort="score", season=2025)
        return data["games"][0]

    def _any_2025_week5_game_id(self):
        conn = _raw_db()
        row = conn.execute(
            "SELECT game_id FROM games WHERE season_year=2025 AND season_type=2 AND week=5 "
            "AND watchability_score IS NOT NULL LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0]


class TestScanner(SpoilerApiTestCase):
    """The load-bearing test: walks every response from every default-
    policy GET endpoint and asserts nothing spoiler_hidden carries a
    redacted field. This is what makes a new endpoint safe by default."""

    def test_no_leaks_across_default_endpoints(self):
        endpoints = [
            ("/api/games", {"season": 2026, "scored": "all", "limit": 20}),
            ("/api/games", {"limit": 50}),
            ("/api/slate", {"scope": "week", "date": "2026-09-05"}),
            ("/api/slate", {"scope": "day", "date": "2026-09-05"}),
            ("/api/top", {"season": 2026, "limit": 25}),
            ("/api/top", {"limit": 25}),
        ]
        for path, params in endpoints:
            data = self.get(path, **params)
            failures = []
            _walk_and_check(data, path, failures)
            self.assertEqual(failures, [], f"{path}: {failures}")

    def test_no_leaks_on_a_freshly_hidden_scored_game(self):
        gid = self._any_2025_scored_game()["game_id"]
        self.post("/api/spoilers/game", {"game_id": gid, "hidden": True})
        data = self.get(f"/api/games/{gid}")
        failures = []
        _walk_and_check(data, f"/api/games/{gid}", failures)
        self.assertEqual(failures, [], failures)


class TestExclusion(SpoilerApiTestCase):
    def test_games_list_excludes_2026_under_every_sort_and_filter(self):
        combos = [
            {},
            {"sort": "margin"},
            {"ot": "1"},
            {"ot": "0"},
            {"sort": "metric:lead_changes"},
            {"min_score": 0},
        ]
        for extra in combos:
            data = self.get("/api/games", season=2026, scored="all", limit=500, **extra)
            self.assertEqual(data["games"], [], f"expected no 2026 games for {extra}")

    def test_games_excluded_count_reported(self):
        data = self.get("/api/games", season=2026, scored="all", limit=1)
        self.assertGreater(data["spoiler_excluded"]["count"], 0)

    def test_ot_filter_does_not_change_excluded_count(self):
        # The excluded count for a given schedule scope must be identical
        # regardless of the ot= filter -- otherwise the count itself would
        # reconstruct an outcome-shaped fact (whether the hidden games in
        # that scope went to overtime) about the games it's counting.
        base = self.get("/api/games", season=2026, week=1, scored="all", ot="all", limit=1)
        with_ot = self.get("/api/games", season=2026, week=1, scored="all", ot="1", limit=1)
        without_ot = self.get("/api/games", season=2026, week=1, scored="all", ot="0", limit=1)
        self.assertEqual(base["spoiler_excluded"]["count"], with_ot["spoiler_excluded"]["count"])
        self.assertEqual(base["spoiler_excluded"]["count"], without_ot["spoiler_excluded"]["count"])

    def test_top_composite_excludes_2026(self):
        data = self.get("/api/top", season=2026, limit=25)
        self.assertEqual(data["results"], [])

    def test_top_metric_excludes_2026(self):
        data = self.get("/api/top", season=2026, by="lead_changes", limit=25)
        self.assertEqual(data["results"], [])

    def test_top_teams_excludes_2026(self):
        data = self.get("/api/top/teams", season=2026, min_games=1)
        self.assertEqual(data["results"], [])

    def test_weekly_peaks_excludes_2026(self):
        data = self.get("/api/top/weekly-peaks", season=2026)
        self.assertEqual(data["weeks"], [])


class TestRankContiguity(SpoilerApiTestCase):
    def test_ranks_are_contiguous_over_visible_scored_games(self):
        data = self.get("/api/games", limit=500, sort="score", scored=1)
        games = data["games"]
        self.assertTrue(games)
        for i, g in enumerate(games, start=1):
            if i > 1 and g["watchability_score"] == games[i - 2]["watchability_score"]:
                continue  # a genuine tie -- RANK() legitimately repeats/skips here
            self.assertEqual(g["rank"], i, f"rank gap at position {i}: game {g['game_id']} has rank {g['rank']}")

    def test_hiding_the_top_game_promotes_the_next_one_to_rank_one_no_gap(self):
        top = self.get("/api/games", limit=1, sort="score")["games"][0]
        self.post("/api/spoilers/game", {"game_id": top["game_id"], "hidden": True})
        new_top = self.get("/api/games", limit=1, sort="score")["games"][0]
        self.assertEqual(new_top["rank"], 1)
        self.assertNotEqual(new_top["game_id"], top["game_id"])


class TestStringScanner(SpoilerApiTestCase):
    def test_hidden_game_matchup_score_string_absent(self):
        g = self._any_2025_scored_game()
        gid = g["game_id"]
        matchup_string = f"{g['away']['abbr']} {g['away']['score']} at {g['home']['abbr']} {g['home']['score']}"

        self.post("/api/spoilers/game", {"game_id": gid, "hidden": True})

        checks = [
            (f"/api/games/{gid}", {}),
            ("/api/games", {"season": 2025, "limit": 500}),
            ("/api/top", {"season": 2025, "limit": 100}),
        ]
        for path, params in checks:
            resp = self.client.get(path, query_string=params)
            text = resp.get_data(as_text=True)
            self.assertNotIn(matchup_string, text, f"{path} leaked the hidden game's matchup string")


class TestNeighborSafety(SpoilerApiTestCase):
    def test_hidden_game_never_appears_as_a_neighbor(self):
        top = self.get("/api/games", limit=2, sort="score")["games"]
        hidden_gid, next_gid = top[0]["game_id"], top[1]["game_id"]
        self.post("/api/spoilers/game", {"game_id": hidden_gid, "hidden": True})

        detail = self.get(f"/api/games/{next_gid}")
        neighbor_ids = [
            (detail["neighbors"]["prev_by_rank"] or {}).get("game_id"),
            (detail["neighbors"]["next_by_rank"] or {}).get("game_id"),
        ]
        self.assertNotIn(hidden_gid, neighbor_ids)


class TestGameDetailGuards(SpoilerApiTestCase):
    def test_hidden_scored_game_nulls_every_layer_two_payload(self):
        gid = self._any_2025_scored_game()["game_id"]
        self.post("/api/spoilers/game", {"game_id": gid, "hidden": True})
        detail = self.get(f"/api/games/{gid}")

        self.assertIsNone(detail["wp"])
        self.assertIsNone(detail["fox_score"])
        self.assertIsNone(detail["score_integrity"])
        self.assertIsNone(detail["rank_context"])
        self.assertEqual(detail["neighbors"], {"prev_by_rank": None, "next_by_rank": None})
        self.assertEqual(detail["ot"], {"is_ot": None, "max_period_in_data": None, "note": None})
        self.assertEqual(detail["corrections"], {
            "manual": [], "fox": [], "unusable": False, "unusable_notes": [], "fox_event_id": None,
        })
        self.assertIsNone(detail["live"])
        self.assertIsNone(detail["live_history"])
        self.assertTrue(detail["game"]["spoiler_hidden"])


class TestSlateOrdering(SpoilerApiTestCase):
    def test_completed_section_stays_score_ordered_when_hidden(self):
        self.post("/api/spoilers/week", {"season_year": 2025, "season_type": 2, "week": 5, "hidden": True})
        hidden_slate = self.get("/api/slate", scope="week", date="2025-09-27")
        hidden_order = [
            g["game_id"] for g in hidden_slate["sections"]["completed"]
            if g["season_year"] == 2025 and g["week"] == 5
        ]
        self.assertTrue(hidden_order)
        self.assertTrue(all(
            g["spoiler_hidden"] for g in hidden_slate["sections"]["completed"] if g["game_id"] in hidden_order
        ))

        self.post("/api/spoilers/week", {"season_year": 2025, "season_type": 2, "week": 5, "hidden": None})
        revealed_slate = self.get("/api/slate", scope="week", date="2025-09-27")
        revealed_order = [
            g["game_id"] for g in revealed_slate["sections"]["completed"]
            if g["season_year"] == 2025 and g["week"] == 5
        ]

        self.assertEqual(hidden_order, revealed_order)


class TestReveal(SpoilerApiTestCase):
    def test_reveal_unredacts_only_the_named_game(self):
        self.post("/api/spoilers/week", {"season_year": 2025, "season_type": 2, "week": 5, "hidden": True})
        data = self.get("/api/games", season=2025, week=5, scored="all")
        self.assertEqual(data["games"], [])

        gid = self._any_2025_week5_game_id()

        detail = self.get(f"/api/games/{gid}", reveal=1)
        self.assertFalse(detail["game"]["spoiler_hidden"])
        self.assertTrue(detail["game"]["spoiler_revealed"])
        self.assertIsNotNone(detail["game"]["watchability_score"])

        data2 = self.get("/api/games", season=2025, week=5, scored="all", reveal=gid)
        ids = [g["game_id"] for g in data2["games"]]
        self.assertEqual(ids, [gid])

    def test_reveal_all_does_nothing(self):
        self.post("/api/spoilers/week", {"season_year": 2025, "season_type": 2, "week": 5, "hidden": True})
        data = self.get("/api/games", season=2025, week=5, scored="all", reveal="all")
        self.assertEqual(data["games"], [])


class TestSpoilersSearch(SpoilerApiTestCase):
    """/api/spoilers/search exists specifically to find games /api/games
    would exclude -- these tests are really about proving the bypass
    works, since that's the entire reason the endpoint exists."""

    def test_exact_id_lookup_finds_a_hidden_game(self):
        gid = self._any_2025_scored_game()["game_id"]
        self.post("/api/spoilers/game", {"game_id": gid, "hidden": True})

        # confirm it's actually excluded from the normal listing first --
        # otherwise this test wouldn't be exercising the bypass at all
        normal_ids = [g["game_id"] for g in self.get("/api/games", season=2025, scored="all", limit=500)["games"]]
        self.assertNotIn(gid, normal_ids)

        data = self.get("/api/spoilers/search", game_id=gid)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["game_id"], gid)

    def test_team_search_finds_hidden_games_too(self):
        # 2026 is fully hidden by default -- a q= search for a 2026 team
        # must still surface results, or the settings page could never
        # find a game to build a per-game override for.
        data = self.get("/api/spoilers/search", q="TCU", limit=50)
        self.assertTrue(any(r["season_year"] == 2026 for r in data["results"]), data["results"])

    def test_response_never_carries_score_or_metric_fields(self):
        data = self.get("/api/spoilers/search", q="Oregon", limit=25)
        self.assertTrue(data["results"])
        blob = str(data)
        for forbidden in ("watchability_score", "\"score\"", "metrics", "rank"):
            self.assertNotIn(forbidden, blob, f"unexpected field '{forbidden}' in search response")

    def test_no_query_returns_empty(self):
        data = self.get("/api/spoilers/search")
        self.assertEqual(data["results"], [])


class TestWritePath(SpoilerApiTestCase):
    def test_week_game_default_round_trip(self):
        gid = self._any_2025_scored_game()["game_id"]
        self.post("/api/spoilers/week", {"season_year": 2025, "season_type": 2, "week": 9, "hidden": True})
        self.post("/api/spoilers/game", {"game_id": gid, "hidden": True})
        self.post("/api/spoilers/default", {"season_year": 2027, "season_type": 2, "week": 1})

        policy = self.get("/api/spoilers")["policy"]
        self.assertEqual(policy["hidden_from"], {"season_year": 2027, "season_type": 2, "week": 1})
        self.assertTrue(policy["weeks"]["2025:2:9"])
        self.assertTrue(policy["games"][gid])

    def test_default_reset_via_null_season_year(self):
        self.post("/api/spoilers/default", {"season_year": 2030, "season_type": 3, "week": 1})
        self.post("/api/spoilers/default", {"season_year": None})
        policy = self.get("/api/spoilers")["policy"]
        self.assertEqual(policy["hidden_from"], {"season_year": 2026, "season_type": 2, "week": 1})

    def test_default_does_not_require_the_season_to_exist_yet(self):
        # The whole point of the default threshold is future-proofing a
        # season not yet in the DB -- unlike week/game overrides, this
        # must succeed even though no 2031 games exist.
        resp = self.client.post("/api/spoilers/default", json={"season_year": 2031, "season_type": 2, "week": 4})
        self.assertEqual(resp.status_code, 200)

    def test_default_bad_season_type_rejected(self):
        resp = self.client.post("/api/spoilers/default", json={"season_year": 2027, "season_type": 5, "week": 1})
        self.assertEqual(resp.status_code, 400)

    def test_mid_season_default_recalibration(self):
        # The scenario from the feature request: "in week 3 I don't care
        # about being spoiled for week 1 anymore" -- move the default
        # threshold forward within an in-progress season.
        self.post("/api/spoilers/default", {"season_year": 2026, "season_type": 2, "week": 3})
        data = self.get("/api/games", season=2026, week=1, scored="all")
        self.assertGreater(len(data["games"]), 0, "week 1 of 2026 should now be visible")
        data3 = self.get("/api/games", season=2026, week=3, scored="all")
        self.assertEqual(data3["games"], [], "week 3 of 2026 should still be hidden")

    def test_cfb_db_still_read_only_after_writes(self):
        self.post("/api/spoilers/default", {"season_year": 2027, "season_type": 2, "week": 1})
        conn = _raw_db()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE __test_probe__ (x)")
        conn.close()

    def test_bad_week_rejected(self):
        resp = self.client.post(
            "/api/spoilers/week", json={"season_year": 1900, "season_type": 2, "week": 1, "hidden": True}
        )
        self.assertEqual(resp.status_code, 404)

    def test_bad_game_rejected(self):
        resp = self.client.post("/api/spoilers/game", json={"game_id": "nonexistent_id", "hidden": True})
        self.assertEqual(resp.status_code, 404)

    def test_cross_origin_post_rejected(self):
        resp = self.client.post(
            "/api/spoilers/default", json={"season_year": 2027, "season_type": 2, "week": 1},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_same_origin_post_allowed(self):
        resp = self.client.post(
            "/api/spoilers/default", json={"season_year": 2027, "season_type": 2, "week": 1},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
