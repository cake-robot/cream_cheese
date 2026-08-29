"""
Manual spoiler-hiding policy for the web UI.

This is deliberately stored outside data/cfb.db, in data/spoilers.json, so
serve.py's connection to the pipeline database stays strictly mode=ro (see
serve.py's module docstring -- the pipeline is the only writer to cfb.db).
This module owns the prefs file: an mtime/size-cached load, an atomic
tempfile+os.replace write guarded by a lock, and the precedence logic that
decides whether a given game is currently spoiler-hidden.

Precedence (most specific wins): an explicit per-game override, then an
explicit per-week override, then the default `hidden_from` threshold --
{season_year, season_type, week}, e.g. "2026 week 1 (regular)". A game is
hidden by default when it falls on or after that point in the season
calendar: any later season entirely, or the same season from that week
onward, or (if the threshold itself is postseason) any postseason game of
that year. This is an ordinal comparison, not an enumeration, so both
"every season from 2027 on" (season_type=2, week=1) and "this season from
week 3 on" (mid-season recalibration) fall out of the same rule with no
per-season config. A week/game key's *absence* from the policy means "fall
through to the next tier" -- callers never persist an explicit `null`;
clearing an override means deleting the key (see set_week/set_game below).

Two pure "choke point" functions are the actual redaction: redact_game()
and redact_live(). Both null out spoiler fields on an already-shaped dict
rather than deleting keys -- several client call sites (web/app.js's
fmtMatchup, web/charts.js's contributionBars) test `!== null`, which is
true for `undefined`, so deleting a key would make those checks fail open
(print "undefined", or throw). See serve.py's shape_game()/
build_live_payload() for where these get called.
"""

import json
import os
import pathlib
import tempfile
import threading
from datetime import datetime, timezone

from . import users as _users

DEFAULT_HIDDEN_FROM = {"season_year": 2026, "season_type": 2, "week": 1}

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY_PATH = _REPO_ROOT / "data" / "spoilers.json"

# Guards the whole read-modify-write cycle in set_week/set_game/set_default
# below, not just the file write inside save_policy() -- Flask's dev server
# runs threaded (see serve.py's app.run(...)), so two concurrent POSTs could
# otherwise both load_policy() the same pre-write state and then each save
# their own modification, with the second write silently dropping the
# first's change. Reentrant because set_*() holds it across its own call
# into save_policy(), which also acquires it.
_LOCK = threading.RLock()
_cache = {"key": None, "policy": None}


def _default_policy():
    return {
        "version": 1,
        "hidden_from": dict(DEFAULT_HIDDEN_FROM),
        "weeks": {},
        "games": {},
        "updated_at": None,
    }


def _normalize(policy):
    """Defensive normalization applied to anything loaded from disk (or
    handed in by a caller): coerce hidden_from to a well-formed
    {season_year, season_type, week} dict (migrating the old bare-year
    `hidden_from_season` schema if that's what's on disk -- a saved int
    year Y means exactly "hide season Y onward", i.e. season_type=2,
    week=1), and drop any non-bool/None entries from weeks/games rather
    than letting a hand-edited or corrupted file propagate a bad value
    into SQL/JSON responses."""
    out = _default_policy()
    out.update({k: v for k, v in policy.items() if k in out})

    hidden_from = policy.get("hidden_from")
    if not isinstance(hidden_from, dict):
        legacy_year = policy.get("hidden_from_season")
        try:
            hidden_from = {"season_year": int(legacy_year), "season_type": 2, "week": 1}
        except (TypeError, ValueError):
            hidden_from = None

    if isinstance(hidden_from, dict):
        try:
            season_type = int(hidden_from.get("season_type", 2))
            if season_type not in (2, 3):
                season_type = 2
            out["hidden_from"] = {
                "season_year": int(hidden_from["season_year"]),
                "season_type": season_type,
                "week": int(hidden_from.get("week", 1)),
            }
        except (KeyError, TypeError, ValueError):
            out["hidden_from"] = dict(DEFAULT_HIDDEN_FROM)
    else:
        out["hidden_from"] = dict(DEFAULT_HIDDEN_FROM)

    out["weeks"] = {str(k): bool(v) for k, v in (out.get("weeks") or {}).items() if v is not None}
    out["games"] = {str(k): bool(v) for k, v in (out.get("games") or {}).items() if v is not None}
    return out


def _default_hidden(season_year, season_type, week, hidden_from):
    """Pure ordinal comparison: is (season_year, season_type, week) on or
    after the hidden_from threshold? Postseason is treated as coming after
    every regular-season week of the same year (it's chronologically
    later, even though it's stored with week=1 -- see db comments on why
    postseason has no real week numbering), and as a single undivided unit
    within itself (no week granularity to subdivide a postseason
    threshold by)."""
    ty, tt, tw = hidden_from["season_year"], hidden_from["season_type"], hidden_from["week"]
    if season_year != ty:
        return season_year > ty
    if tt == 2:
        if season_type == 3:
            return True
        return week >= tw
    return season_type == 3  # tt == 3: threshold is "this year's postseason"


def _default_hidden_sql(alias, hidden_from):
    """SQL mirror of _default_hidden() -- see its docstring for the
    ordering rule. Returns (expr, params); expr evaluates to 1 or 0."""
    sql = (
        f"CASE WHEN {alias}.season_year > ? THEN 1 "
        f"WHEN {alias}.season_year < ? THEN 0 "
        f"ELSE (CASE WHEN ? = 2 "
        f"THEN (CASE WHEN {alias}.season_type = 3 THEN 1 WHEN {alias}.week >= ? THEN 1 ELSE 0 END) "
        f"ELSE (CASE WHEN {alias}.season_type = 3 THEN 1 ELSE 0 END) END) END"
    )
    params = [hidden_from["season_year"], hidden_from["season_year"], hidden_from["season_type"], hidden_from["week"]]
    return sql, params


def week_key(season_year, season_type, week):
    return f"{season_year}:{season_type}:{week}"


def load_policy():
    """Cached against (mtime_ns, size) so repeated calls within a request
    (or across requests when the file hasn't changed) don't re-read/parse.
    Falls back to a fresh default policy if the file is absent, unreadable,
    or malformed -- a missing/corrupt prefs file must never take the site
    down."""
    try:
        st = POLICY_PATH.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None

    if cache_key is not None and _cache["key"] == cache_key:
        return _cache["policy"]

    if cache_key is None:
        policy = _default_policy()
    else:
        try:
            with open(POLICY_PATH, "r") as f:
                loaded = json.load(f)
            policy = _normalize(loaded) if isinstance(loaded, dict) else _default_policy()
        except (OSError, ValueError):
            policy = _default_policy()

    _cache["key"] = cache_key
    _cache["policy"] = policy
    return policy


def save_policy(policy):
    """Atomic write: tempfile in the same directory, then os.replace. If
    os.replace raises mid-write, the original file is left byte-identical
    -- the tempfile is cleaned up and the exception propagates."""
    policy = _normalize(policy)
    policy["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _LOCK:
        POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(POLICY_PATH.parent), prefix=".spoilers-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(policy, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, POLICY_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        finally:
            # Force the next load_policy() to re-read rather than serve a
            # stale cache -- cheaper than trying to keep the cache key in
            # sync with the write we just did.
            _cache["key"] = None
            _cache["policy"] = None
    return policy


def is_hidden_row(row, policy):
    """row must support __getitem__ for game_id/season_year/season_type/
    week -- a sqlite3.Row or a plain dict both work."""
    game_id = row["game_id"]
    games = policy.get("games", {})
    if game_id in games:
        return games[game_id]
    key = week_key(row["season_year"], row["season_type"], row["week"])
    weeks = policy.get("weeks", {})
    if key in weeks:
        return weeks[key]
    hidden_from = policy.get("hidden_from", DEFAULT_HIDDEN_FROM)
    return _default_hidden(row["season_year"], row["season_type"], row["week"], hidden_from)


def set_week(season_year, season_type, week, hidden):
    """hidden: True | False | None. None clears the override (deletes the
    key), which is what lets the tri-state UI fall back to the default.
    The whole load-modify-save cycle is one critical section (see _LOCK's
    docstring above) so a concurrent set_*() call can't read stale state."""
    with _LOCK:
        policy = load_policy()
        key = week_key(season_year, season_type, week)
        weeks = dict(policy.get("weeks", {}))
        if hidden is None:
            weeks.pop(key, None)
        else:
            weeks[key] = bool(hidden)
        policy = dict(policy)
        policy["weeks"] = weeks
        return save_policy(policy)


def set_game(game_id, hidden):
    with _LOCK:
        policy = load_policy()
        games = dict(policy.get("games", {}))
        if hidden is None:
            games.pop(game_id, None)
        else:
            games[game_id] = bool(hidden)
        policy = dict(policy)
        policy["games"] = games
        return save_policy(policy)


def set_default(season_year, season_type, week):
    """season_year: int | None. None resets the whole threshold to
    DEFAULT_HIDDEN_FROM and ignores season_type/week. Otherwise all three
    are required -- unlike set_week/set_game, this has no per-field clear;
    the threshold always has some value, it's just a question of which
    one."""
    with _LOCK:
        policy = load_policy()
        policy = dict(policy)
        if season_year is None:
            policy["hidden_from"] = dict(DEFAULT_HIDDEN_FROM)
        else:
            policy["hidden_from"] = {
                "season_year": int(season_year), "season_type": int(season_type), "week": int(week),
            }
        return save_policy(policy)


# ---------------------------------------------------------------------------
# Per-user policy storage -- backed by data/users.db (via src/users.py)
# instead of the single shared data/spoilers.json file above. Added once the
# app grew multiple accounts, each with their own spoiler preferences; the
# file-based functions above remain untouched (and still fully tested by
# tests/test_spoilers_policy.py) because the one-shot migration script that
# seeds the first admin's row from data/spoilers.json still needs them (see
# `just migrate-spoilers`). No mtime/size cache here the way load_policy()
# has one: that cache existed because spoilers.json could be read many
# times per second across every request, regardless of user; a per-user
# policy is already read at most once per request via serve.py's
# g.spoiler_ctx, so a second cache layer here would add complexity without
# a measurable payoff.
# ---------------------------------------------------------------------------

def get_user_policy(user_id, conn=None):
    """Per-user analog of load_policy(). Falls back to a fresh default
    policy for a user with no row yet (brand-new account, or the pre-
    migration gap before `just migrate-spoilers` has run for this user) --
    same "never take the site down over missing/malformed prefs" contract
    load_policy() has for a missing/corrupt file."""
    own_conn = conn is None
    conn = conn or _users.get_connection()
    try:
        raw = _users.get_policy_json(conn, user_id)
    finally:
        if own_conn:
            conn.close()
    if raw is None:
        return _default_policy()
    try:
        loaded = json.loads(raw)
    except ValueError:
        return _default_policy()
    return _normalize(loaded) if isinstance(loaded, dict) else _default_policy()


def save_user_policy(user_id, policy, conn=None):
    policy = _normalize(policy)
    policy["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    own_conn = conn is None
    conn = conn or _users.get_connection()
    try:
        _users.set_policy_json(conn, user_id, json.dumps(policy, sort_keys=True))
    finally:
        if own_conn:
            conn.close()
    return policy


def set_user_week(user_id, season_year, season_type, week, hidden, conn=None):
    """hidden: True | False | None -- see set_week()'s docstring; same
    tri-state semantics, just scoped to one user_id. The read-modify-write
    is one critical section under users.LOCK (see that module's docstring
    for why this needs to be the same lock object users.py itself uses
    internally, not a separate one) so a concurrent set_user_*() call for
    the same user can't read stale state."""
    own_conn = conn is None
    conn = conn or _users.get_connection()
    try:
        with _users.LOCK:
            policy = get_user_policy(user_id, conn=conn)
            key = week_key(season_year, season_type, week)
            weeks = dict(policy.get("weeks", {}))
            if hidden is None:
                weeks.pop(key, None)
            else:
                weeks[key] = bool(hidden)
            policy = dict(policy)
            policy["weeks"] = weeks
            return save_user_policy(user_id, policy, conn=conn)
    finally:
        if own_conn:
            conn.close()


def set_user_game(user_id, game_id, hidden, conn=None):
    own_conn = conn is None
    conn = conn or _users.get_connection()
    try:
        with _users.LOCK:
            policy = get_user_policy(user_id, conn=conn)
            games = dict(policy.get("games", {}))
            if hidden is None:
                games.pop(game_id, None)
            else:
                games[game_id] = bool(hidden)
            policy = dict(policy)
            policy["games"] = games
            return save_user_policy(user_id, policy, conn=conn)
    finally:
        if own_conn:
            conn.close()


def set_user_default(user_id, season_year, season_type, week, conn=None):
    """season_year: int | None -- see set_default()'s docstring; None
    resets the threshold to DEFAULT_HIDDEN_FROM and ignores
    season_type/week."""
    own_conn = conn is None
    conn = conn or _users.get_connection()
    try:
        with _users.LOCK:
            policy = get_user_policy(user_id, conn=conn)
            policy = dict(policy)
            if season_year is None:
                policy["hidden_from"] = dict(DEFAULT_HIDDEN_FROM)
            else:
                policy["hidden_from"] = {
                    "season_year": int(season_year), "season_type": int(season_type), "week": int(week),
                }
            return save_user_policy(user_id, policy, conn=conn)
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Redaction -- pure, total, null-not-delete (see module docstring).
# ---------------------------------------------------------------------------

def redact_game(shaped):
    """Nulls out every spoiler field on an already shape_game()-shaped
    dict. Total and idempotent: every key listed here is always present on
    the input and always present (just nulled) on the output."""
    out = dict(shaped)
    out["watchability_score"] = None
    out["uw_loss_bonus"] = None
    out["rank"] = None
    out["percentile"] = None
    out["n_scored"] = None
    out["ot"] = None
    out["metrics"] = {}
    out["applicable_weight"] = None
    out["has_fox_correction"] = False
    out["has_manual_correction"] = False
    away = dict(out["away"])
    away["score"] = None
    out["away"] = away
    home = dict(out["home"])
    home["score"] = None
    out["home"] = home
    return out


def redact_live(live):
    """Nulls out the additive "live" payload built by serve.py's
    build_live_payload(). status.detail is nulled unconditionally -- it's
    ESPN's shortDetail, which for an overtime game can read literally
    "Final/OT" -- but status.period/clock_display survive when period <= 4:
    "Q3 4:12" is useful for deciding whether to tune in right now and isn't
    itself a spoiler; a period past 4 is an OT tell on its own, so that one
    gets nulled too."""
    if live is None:
        return None
    out = dict(live)
    out["live_score"] = None
    out["quality_so_far"] = None
    out["drama_from_here"] = None
    out["wp_now"] = None
    out["headline"] = None
    status = dict(out.get("status") or {})
    period = status.get("period")
    if period is not None and period > 4:
        status["period"] = None
        status["clock_display"] = None
    status["detail"] = None
    out["status"] = status
    out["so_far"] = {"applicable_weight": None, "metrics": {}}
    out["from_here"] = {"applicable_weight": None, "metrics": {}}
    return out


# ---------------------------------------------------------------------------
# Exclusion -- the SQL-level predicate for Games/Top/weekly-peaks/team
# leaderboard, where a hidden game is dropped from the result set entirely
# rather than redacted in place (see the plan doc: exclusion is what makes
# rank contiguous and closes the sort/filter leak channels).
# ---------------------------------------------------------------------------

def visible_sql(policy, alias="g"):
    """Returns (sql_fragment, params) -- a boolean SQL expression that is
    true exactly when a game should be INCLUDED. Mirrors is_hidden_row's
    precedence exactly (game override > week override > default) -- there
    used to be a `revealed_ids` tier above even a game override (a session
    reveal, opt-in and client-side, that won over everything including an
    explicit "hidden" game override); that's gone now that "Reveal this
    game" just POSTs a real hidden:false game override through the same
    set_user_game() path Settings' game-override card already used, so the
    ordinary game-override tier below already covers it.

    Built as small IN-lists for whichever override tiers are non-empty,
    falling back to _default_hidden_sql()'s ordinal comparison against
    season_year/season_type/week -- all three are covered by
    idx_games_season(season_year, season_type, week), so the common case
    (no explicit overrides in play) stays sargable rather than degrading
    into a big IN scan."""
    weeks = policy.get("weeks", {})
    games = policy.get("games", {})
    hidden_from = policy.get("hidden_from", DEFAULT_HIDDEN_FROM)

    hidden_game_ids = [gid for gid, v in games.items() if v]
    shown_game_ids = [gid for gid, v in games.items() if not v]
    hidden_week_keys = [k for k, v in weeks.items() if v]
    shown_week_keys = [k for k, v in weeks.items() if not v]

    week_expr = f"({alias}.season_year || ':' || {alias}.season_type || ':' || {alias}.week)"

    whens = []
    params = []

    def _in_clause(expr, values, result):
        whens.append(f"WHEN {expr} IN ({','.join('?' * len(values))}) THEN {result}")
        params.extend(values)

    if hidden_game_ids:
        _in_clause(f"{alias}.game_id", hidden_game_ids, 1)
    if shown_game_ids:
        _in_clause(f"{alias}.game_id", shown_game_ids, 0)
    if hidden_week_keys:
        _in_clause(week_expr, hidden_week_keys, 1)
    if shown_week_keys:
        _in_clause(week_expr, shown_week_keys, 0)

    default_expr, default_params = _default_hidden_sql(alias, hidden_from)
    default_expr = f"({default_expr})"
    params.extend(default_params)

    if whens:
        case_sql = "CASE " + " ".join(whens) + f" ELSE {default_expr} END"
    else:
        case_sql = default_expr

    return f"NOT ({case_sql})", params
