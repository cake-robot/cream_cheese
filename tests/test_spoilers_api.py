"""
API-level tests for the spoiler feature, run against the real (read-only)
data/cfb.db via Flask's test client. No fixtures needed -- 2026 gives ~900
naturally-hidden scheduled games under the default policy, and 2025 gives a
large revealed, scored corpus to hide/reveal in individual tests.

Every route now requires a session (see serve.py's login-wall before_request
gate), and every spoiler route now reads/writes a per-user policy in
data/users.db rather than the old shared data/spoilers.json (see
src/spoilers.py's get_user_policy/save_user_policy and serve.py's
spoiler_ctx()). So each test gets its own scratch users.db (users.DB_PATH
monkeypatched to a tempdir) with one freshly-created, freshly-logged-in
user -- a brand-new account's spoiler policy is the same DEFAULT_HIDDEN_FROM
this file's tests were already written against, so the actual assertions
below are unchanged; only setUp/tearDown had to learn about accounts.
data/spoilers.json is no longer in serve.py's request path at all (it's
now read only by the one-shot migration script), so it needs no isolation
here.

Run with: ./venv/bin/python -m unittest discover tests
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src import spoilers, users

import serve

# spoiler_hidden means "spoiler_level < LEVEL_FULL" (levels 0 and 1 both),
# so only the fields level 1 is ALSO supposed to hide belong in the
# unconditional list -- watchability_score/rank/percentile/n_scored are
# level 1's whole payload and must only be checked at level 0. Mirrors
# serve.py's own dev-only _walk_spoiler_leaks split.
SPOILER_ALWAYS_REDACTED_FIELDS = ("uw_loss_bonus", "applicable_weight")
SPOILER_LEVEL0_ONLY_FIELDS = ("watchability_score", "rank", "percentile", "n_scored")


def _walk_and_check(node, path, failures):
    """Recursively walk a decoded JSON response; for any dict carrying
    spoiler_hidden: true, record any field that should have been redacted
    but wasn't. Mirrors serve.py's own dev-only _walk_spoiler_leaks, but
    runs unconditionally here (not gated on app.debug)."""
    if isinstance(node, dict):
        if node.get("spoiler_hidden") is True:
            level = node.get("spoiler_level", spoilers.LEVEL_HIDDEN)
            fields = SPOILER_ALWAYS_REDACTED_FIELDS
            if level == spoilers.LEVEL_HIDDEN:
                fields = fields + SPOILER_LEVEL0_ONLY_FIELDS
            for f in fields:
                if f in node and node[f] is not None:
                    failures.append(f"{path}.{f} is non-null on a spoiler_hidden game (level={level})")
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
        self._orig_users_db_path = users.DB_PATH
        users.DB_PATH = Path(self._tmpdir) / "users.db"
        conn = users.init_db(users.DB_PATH)
        users.create_user(conn, "testuser", "testuserpassword1", invite_code=None)
        conn.close()

        serve.app.testing = True
        self.client = serve.app.test_client()
        # /api/login is rate-limited to 5/minute in production (see
        # serve.py) -- the limiter's in-memory storage is process-wide and
        # persists across tests, and every test's login here shares one
        # bucket (Flask's test client always presents as 127.0.0.1, so
        # _rate_limit_key()'s CF-Connecting-IP fallback can't tell tests
        # apart). Reset before every test rather than exempting login from
        # the limiter in test mode -- this way a change to the real 5/minute
        # limit still gets exercised by whatever here happens to log in
        # more than 5 times.
        serve.limiter.reset()
        # Every route requires a session now -- log the fixture user in
        # once here so every self.get()/self.post() below carries a valid
        # cookie, same as a real browser would after visiting /login.html.
        login_resp = self.client.post(
            "/api/login", json={"username": "testuser", "password": "testuserpassword1"},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        assert login_resp.status_code == 200, login_resp.get_data(as_text=True)

    def tearDown(self):
        users.DB_PATH = self._orig_users_db_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def get(self, path, **params):
        resp = self.client.get(path, query_string=params)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def post(self, path, body):
        # serve.py's origin guard rejects a POST with no Origin header (see
        # test_cross_origin_post_rejected / test_same_origin_post_allowed
        # below for the guard's own tests) -- everything else in this file
        # is exercising route behavior, not the guard, so it needs a
        # same-origin Origin header to get past it.
        resp = self.client.post(path, json=body, headers={"Origin": "http://127.0.0.1:5050"})
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


class TestLevelOne(SpoilerApiTestCase):
    """LEVEL_SCORE: the watchability composite (score/rank/percentile/
    n_scored) is revealed, everything else stays hidden exactly as at
    LEVEL_HIDDEN. Unlike LEVEL_HIDDEN, a LEVEL_SCORE game joins the ranked
    lists (see the two-tier-spoiler plan's decision 1) -- these tests cover
    both what it reveals and the leak channels opened by that inclusion."""

    def _set_level_one(self, gid):
        self.post("/api/spoilers/game", {"game_id": gid, "level": 1})

    def test_score_survives_outcome_does_not(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)
        data = self.get("/api/games", season=2025, scored="all", limit=500)
        g = next(x for x in data["games"] if x["game_id"] == gid)
        self.assertTrue(g["spoiler_hidden"])
        self.assertEqual(g["spoiler_level"], 1)
        self.assertIsNotNone(g["watchability_score"])
        self.assertIsNotNone(g["rank"])
        self.assertIsNotNone(g["percentile"])
        self.assertIsNotNone(g["n_scored"])
        self.assertIsNone(g["away"]["score"])
        self.assertIsNone(g["home"]["score"])
        self.assertIsNone(g["ot"])
        self.assertIsNone(g["uw_loss_bonus"])
        self.assertIsNone(g["applicable_weight"])
        self.assertEqual(g["metrics"], {})
        self.assertFalse(g["has_fox_correction"])
        self.assertFalse(g["has_manual_correction"])

    def test_appears_in_top_composite(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)
        data = self.get("/api/top", season=2025, limit=500)
        ids = [r["game"]["game_id"] for r in data["results"]]
        self.assertIn(gid, ids)

    def test_top_contributors_empty_for_a_level_one_row(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)
        data = self.get("/api/top", season=2025, limit=500)
        row = next(r for r in data["results"] if r["game"]["game_id"] == gid)
        self.assertEqual(row["top_contributors"], [])

    def test_excluded_from_outcome_shaped_games_surfaces(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)
        combos = [
            {"sort": "margin"},
            {"ot": "1"},
            {"ot": "0"},
            {"sort": "metric:lead_changes"},
        ]
        for extra in combos:
            data = self.get("/api/games", season=2025, scored="all", limit=500, **extra)
            ids = [g["game_id"] for g in data["games"]]
            self.assertNotIn(gid, ids, f"level-1 game leaked through outcome-ordered surface {extra}")

    def test_included_under_score_sort_and_min_score_filter(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)
        data = self.get("/api/games", season=2025, scored="all", sort="score", limit=500)
        self.assertIn(gid, [g["game_id"] for g in data["games"]])
        data2 = self.get("/api/games", season=2025, scored="all", min_score=0, limit=500)
        self.assertIn(gid, [g["game_id"] for g in data2["games"]])

    def test_excluded_from_top_by_metric(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)
        data = self.get("/api/top", season=2025, by="lead_changes", limit=500)
        ids = [r["game"]["game_id"] for r in data["results"]]
        self.assertNotIn(gid, ids)

    def test_rank_contiguous_with_a_level_one_game_in_the_corpus(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)
        data = self.get("/api/games", limit=500, sort="score", scored=1)
        games = data["games"]
        self.assertTrue(games)
        for i, g in enumerate(games, start=1):
            if i > 1 and g["watchability_score"] == games[i - 2]["watchability_score"]:
                continue  # a genuine tie
            self.assertEqual(g["rank"], i, f"rank gap at position {i}: game {g['game_id']} has rank {g['rank']}")

    def test_matchup_string_absent_from_neighbor_and_weekly_peaks(self):
        top = self.get("/api/games", limit=2, sort="score")["games"]
        g, neighbor_gid = top[0], top[1]["game_id"]
        matchup_string = f"{g['away']['abbr']} {g['away']['score']} at {g['home']['abbr']} {g['home']['score']}"
        self._set_level_one(g["game_id"])

        neighbor_detail = self.get(f"/api/games/{neighbor_gid}")
        neighbor_json = self.client.get(f"/api/games/{neighbor_gid}").get_data(as_text=True)
        self.assertNotIn(matchup_string, neighbor_json)
        nb_ids = [
            (neighbor_detail["neighbors"]["prev_by_rank"] or {}).get("game_id"),
            (neighbor_detail["neighbors"]["next_by_rank"] or {}).get("game_id"),
        ]
        if g["game_id"] in nb_ids:
            # The level-1 game legitimately CAN appear as a neighbor now
            # (that's decision 1) -- its watchability_score is fine to show,
            # but the label must carry no score.
            entry = (
                neighbor_detail["neighbors"]["prev_by_rank"]
                if (neighbor_detail["neighbors"]["prev_by_rank"] or {}).get("game_id") == g["game_id"]
                else neighbor_detail["neighbors"]["next_by_rank"]
            )
            self.assertIsNotNone(entry["watchability_score"])
            self.assertNotIn(str(g["away"]["score"]), entry["label"])

        weekly = self.get("/api/top/weekly-peaks", season=g["season_year"], season_type=g["season_type"])
        weekly_text = self.client.get(
            "/api/top/weekly-peaks", query_string={"season": g["season_year"], "season_type": g["season_type"]}
        ).get_data(as_text=True)
        self.assertNotIn(matchup_string, weekly_text)
        del weekly  # response already checked as raw text above

    def test_game_detail_reveals_rank_but_not_metrics(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)
        detail = self.get(f"/api/games/{gid}")
        self.assertEqual(detail["game"]["spoiler_level"], 1)
        self.assertIsNotNone(detail["rank_context"])
        self.assertIsNone(detail["score_integrity"])
        self.assertIsNone(detail["wp"])
        self.assertIsNone(detail["fox_score"])
        self.assertEqual(detail["game"]["metrics"], {})
        self.assertEqual(detail["ot"], {"is_ot": None, "max_period_in_data": None, "note": None})

    def test_legacy_hidden_true_still_maps_to_level_zero(self):
        gid = self._any_2025_scored_game()["game_id"]
        self.post("/api/spoilers/game", {"game_id": gid, "hidden": True})
        detail = self.get(f"/api/games/{gid}")
        self.assertEqual(detail["game"]["spoiler_level"], 0)
        self.assertIsNone(detail["game"]["watchability_score"])

    def test_level_and_hidden_together_rejected(self):
        gid = self._any_2025_scored_game()["game_id"]
        resp = self.client.post(
            "/api/spoilers/game", json={"game_id": gid, "level": 1, "hidden": True},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_per_user_isolation_of_a_level_one_override(self):
        gid = self._any_2025_scored_game()["game_id"]
        self._set_level_one(gid)

        conn = users.get_connection(users.DB_PATH)
        users.create_user(conn, "seconduser", "seconduserpassword1", invite_code=None)
        conn.close()
        client2 = serve.app.test_client()
        login2 = client2.post(
            "/api/login", json={"username": "seconduser", "password": "seconduserpassword1"},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        self.assertEqual(login2.status_code, 200)

        resp2 = client2.get(f"/api/games/{gid}")
        self.assertEqual(resp2.status_code, 200)
        other_level = resp2.get_json()["game"]["spoiler_level"]
        self.assertNotEqual(other_level, 1, "seconduser must not see testuser's level-1 override")


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


class TestGameOverrideUnhide(SpoilerApiTestCase):
    """Replaces the old client-side session-reveal mechanism (a `reveal=`
    query param, opt-in per browser session, that won over every policy
    tier including an explicit game override -- see git history). Revealing
    a game is now just an ordinary hidden:false game override through the
    same /api/spoilers/game route Settings' game-override card uses, so
    it's real, persistent, and manageable from Settings -- not a special
    tier of its own."""

    def test_game_override_unhides_only_the_named_game(self):
        self.post("/api/spoilers/week", {"season_year": 2025, "season_type": 2, "week": 5, "hidden": True})
        data = self.get("/api/games", season=2025, week=5, scored="all")
        self.assertEqual(data["games"], [])

        gid = self._any_2025_week5_game_id()
        self.post("/api/spoilers/game", {"game_id": gid, "hidden": False})

        detail = self.get(f"/api/games/{gid}")
        self.assertFalse(detail["game"]["spoiler_hidden"])
        self.assertIsNotNone(detail["game"]["watchability_score"])

        data2 = self.get("/api/games", season=2025, week=5, scored="all")
        ids = [g["game_id"] for g in data2["games"]]
        self.assertEqual(ids, [gid])

    def test_game_override_is_visible_and_clearable_via_active_overrides(self):
        gid = self._any_2025_week5_game_id()
        self.post("/api/spoilers/game", {"game_id": gid, "hidden": False})
        overrides = self.get("/api/spoilers")["active_overrides"]
        # _spoiler_active_overrides() now reports `level` alongside the
        # legacy `hidden` flag (see the two-tier-spoiler plan) -- hidden:
        # False maps to LEVEL_FULL.
        self.assertIn(
            {"type": "game", "game_id": gid, "hidden": False, "level": 2}, overrides
        )

        self.post("/api/spoilers/game", {"game_id": gid, "hidden": None})
        overrides2 = self.get("/api/spoilers")["active_overrides"]
        self.assertNotIn(gid, [o["game_id"] for o in overrides2 if o["type"] == "game"])


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


class TestPerUserIsolation(SpoilerApiTestCase):
    """The thing the whole per-user rewrite (src/users.py, src/spoilers.py's
    get_user_policy/save_user_policy, serve.py's spoiler_ctx()) was for:
    two different logged-in sessions get two different redactions from the
    exact same route, and neither can see or affect the other's policy."""

    def _second_logged_in_client(self):
        conn = users.get_connection(users.DB_PATH)
        users.create_user(conn, "seconduser", "seconduserpassword1", invite_code=None)
        conn.close()
        client2 = serve.app.test_client()
        resp = client2.post(
            "/api/login", json={"username": "seconduser", "password": "seconduserpassword1"},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return client2

    def test_week_hide_by_one_user_does_not_affect_another(self):
        gid = self._any_2025_week5_game_id()
        client2 = self._second_logged_in_client()

        # Before either user touches anything, both see the same default
        # (2025 is well before DEFAULT_HIDDEN_FROM's 2026 week 1) -- confirms
        # this test's premise (game visible pre-hide) rather than assuming it.
        pre = self.get("/api/games", season=2025, week=5, scored="all")
        self.assertTrue(any(g["game_id"] == gid for g in pre["games"]))

        # self.client (testuser, logged in by setUp) hides week 5 of 2025.
        self.post("/api/spoilers/week", {"season_year": 2025, "season_type": 2, "week": 5, "hidden": True})

        after_testuser = self.get("/api/games", season=2025, week=5, scored="all")
        self.assertFalse(
            any(g["game_id"] == gid for g in after_testuser["games"]),
            "testuser hid this week -- it must be excluded from testuser's own list",
        )

        resp2 = client2.get("/api/games", query_string={"season": 2025, "week": 5, "scored": "all"})
        self.assertEqual(resp2.status_code, 200, resp2.get_data(as_text=True))
        after_seconduser = resp2.get_json()
        self.assertTrue(
            any(g["game_id"] == gid for g in after_seconduser["games"]),
            "seconduser never touched this week -- testuser's override must not leak across accounts",
        )

    def test_default_threshold_is_independent_per_user(self):
        client2 = self._second_logged_in_client()

        self.post("/api/spoilers/default", {"season_year": 2025, "season_type": 2, "week": 1})
        testuser_policy = self.get("/api/spoilers")["policy"]
        self.assertEqual(testuser_policy["hidden_from"], {"season_year": 2025, "season_type": 2, "week": 1})

        resp2 = client2.get("/api/spoilers")
        self.assertEqual(resp2.status_code, 200)
        seconduser_policy = resp2.get_json()["policy"]
        self.assertEqual(
            seconduser_policy["hidden_from"], {"season_year": 2026, "season_type": 2, "week": 1},
            "seconduser's default threshold must still be the account default, untouched by testuser's write",
        )


class TestWritePath(SpoilerApiTestCase):
    def test_week_game_default_round_trip(self):
        gid = self._any_2025_scored_game()["game_id"]
        self.post("/api/spoilers/week", {"season_year": 2025, "season_type": 2, "week": 9, "hidden": True})
        self.post("/api/spoilers/game", {"game_id": gid, "hidden": True})
        self.post("/api/spoilers/default", {"season_year": 2027, "season_type": 2, "week": 1})

        policy = self.get("/api/spoilers")["policy"]
        self.assertEqual(policy["hidden_from"], {"season_year": 2027, "season_type": 2, "week": 1})
        # Stored as LEVEL_HIDDEN (0), not the legacy bool -- assertTrue(0)
        # would wrongly fail here since 0 is falsy, so compare against the
        # level constant explicitly.
        self.assertEqual(policy["weeks"]["2025:2:9"], spoilers.LEVEL_HIDDEN)
        self.assertEqual(policy["games"][gid], spoilers.LEVEL_HIDDEN)

    def test_default_reset_via_null_season_year(self):
        self.post("/api/spoilers/default", {"season_year": 2030, "season_type": 3, "week": 1})
        self.post("/api/spoilers/default", {"season_year": None})
        policy = self.get("/api/spoilers")["policy"]
        self.assertEqual(policy["hidden_from"], {"season_year": 2026, "season_type": 2, "week": 1})

    def test_default_does_not_require_the_season_to_exist_yet(self):
        # The whole point of the default threshold is future-proofing a
        # season not yet in the DB -- unlike week/game overrides, this
        # must succeed even though no 2031 games exist.
        resp = self.client.post(
            "/api/spoilers/default", json={"season_year": 2031, "season_type": 2, "week": 4},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_default_bad_season_type_rejected(self):
        resp = self.client.post(
            "/api/spoilers/default", json={"season_year": 2027, "season_type": 5, "week": 1},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
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
            "/api/spoilers/week", json={"season_year": 1900, "season_type": 2, "week": 1, "hidden": True},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_bad_game_rejected(self):
        resp = self.client.post(
            "/api/spoilers/game", json={"game_id": "nonexistent_id", "hidden": True},
            headers={"Origin": "http://127.0.0.1:5050"},
        )
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
