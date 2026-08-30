"""
Manual spoiler-hiding policy for the web UI.

This is deliberately stored outside data/cfb.db, in data/spoilers.json, so
serve.py's connection to the pipeline database stays strictly mode=ro (see
serve.py's module docstring -- the pipeline is the only writer to cfb.db).
This module owns the prefs file: an mtime/size-cached load, an atomic
tempfile+os.replace write guarded by a lock, and the precedence logic that
decides a given game's current spoiler LEVEL.

Three levels, ordinal (higher reveals strictly more):

    LEVEL_HIDDEN (0) -- nothing revealed. The original, and still default,
        behavior.
    LEVEL_SCORE  (1) -- the watchability composite (score/rank/percentile/
        n_scored) is revealed; the final score, winner, OT status, and every
        per-metric component stay hidden. Lets someone build a watch list
        from "this was a 97th-percentile game" without learning who won.
    LEVEL_FULL   (2) -- everything revealed. The original "not hidden".

Precedence (most specific wins): an explicit per-game override, then an
explicit per-week override, then the default `hidden_from` threshold --
{season_year, season_type, week}, e.g. "2026 week 1 (regular)". A game's
DEFAULT level is binary, never LEVEL_SCORE: LEVEL_HIDDEN on or after that
point in the season calendar (any later season entirely, or the same season
from that week onward, or, if the threshold itself is postseason, any
postseason game of that year), LEVEL_FULL before it. This is an ordinal
comparison, not an enumeration, so both "every season from 2027 on"
(season_type=2, week=1) and "this season from week 3 on" (mid-season
recalibration) fall out of the same rule with no per-season config.
LEVEL_SCORE is reachable only through an explicit game or week override --
see the module-level Context in the two-tier-spoiler plan for why the default
threshold stays a two-way cut rather than growing a second one. A week/game
key's *absence* from the policy means "fall through to the next tier" --
callers never persist an explicit `null`; clearing an override means
deleting the key (see set_week/set_game below).

Stored policy values for `weeks`/`games` are ints in {0, 1, 2} (see the
LEVEL_* constants below). Older policy files/rows predate LEVEL_SCORE and
store bools -- `true` meant hidden, `false` meant revealed -- so `_normalize()`
migrates `True -> LEVEL_HIDDEN`, `False -> LEVEL_FULL` on load; every write
after that persists the int form.

Redaction has two "choke point" functions per shape -- redact_game_full()/
redact_game_score_only() for a shape_game()-shaped dict, redact_live_full()/
redact_live_score_only() for the additive live payload. All four null out
spoiler fields on an already-shaped dict rather than deleting keys -- several
client call sites (web/app.js's fmtMatchup, web/charts.js's
contributionBars) test `!== null`, which is true for `undefined`, so deleting
a key would make those checks fail open (print "undefined", or throw). See
serve.py's shape_game()/build_live_payload() for where these get called.
"""

import json
import os
import pathlib
import tempfile
import threading
from datetime import datetime, timezone

from . import users as _users

# Ordinal spoiler levels -- higher reveals strictly more than lower. See the
# module docstring for what each one reveals.
LEVEL_HIDDEN = 0
LEVEL_SCORE = 1
LEVEL_FULL = 2

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


def _coerce_level(v):
    """A stored weeks/games value can be a legacy bool (True meant hidden,
    False meant revealed -- there was no middle tier) or a level int written
    since LEVEL_SCORE was added. Returns an int in {0,1,2}, or None if `v`
    is neither -- the caller drops the key in that case rather than letting
    a hand-edited or corrupted file propagate a bad value into SQL/JSON
    responses. isinstance(v, bool) is checked before isinstance(v, int)
    because bool is an int subclass in Python."""
    if isinstance(v, bool):
        return LEVEL_HIDDEN if v else LEVEL_FULL
    if isinstance(v, int) and v in (LEVEL_HIDDEN, LEVEL_SCORE, LEVEL_FULL):
        return v
    return None


def _normalize(policy):
    """Defensive normalization applied to anything loaded from disk (or
    handed in by a caller): coerce hidden_from to a well-formed
    {season_year, season_type, week} dict (migrating the old bare-year
    `hidden_from_season` schema if that's what's on disk -- a saved int
    year Y means exactly "hide season Y onward", i.e. season_type=2,
    week=1), and coerce weeks/games entries to a level int via
    _coerce_level(), dropping anything that doesn't map to one."""
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

    def _coerce_map(raw):
        result = {}
        for k, v in (raw or {}).items():
            level = _coerce_level(v)
            if level is not None:
                result[str(k)] = level
        return result

    out["weeks"] = _coerce_map(out.get("weeks"))
    out["games"] = _coerce_map(out.get("games"))
    return out


def _default_level(season_year, season_type, week, hidden_from):
    """The level a game gets when neither a game nor week override applies:
    a pure ordinal comparison against the hidden_from threshold, same as
    _default_hidden() used to compute directly. Always LEVEL_HIDDEN or
    LEVEL_FULL -- the default threshold is a single two-way cut, never
    LEVEL_SCORE (see module docstring). Postseason is treated as coming
    after every regular-season week of the same year (it's chronologically
    later, even though it's stored with week=1 -- see db comments on why
    postseason has no real week numbering), and as a single undivided unit
    within itself (no week granularity to subdivide a postseason threshold
    by)."""
    ty, tt, tw = hidden_from["season_year"], hidden_from["season_type"], hidden_from["week"]
    if season_year != ty:
        hidden = season_year > ty
    elif tt == 2:
        hidden = True if season_type == 3 else week >= tw
    else:
        hidden = season_type == 3  # tt == 3: threshold is "this year's postseason"
    return LEVEL_HIDDEN if hidden else LEVEL_FULL


def _default_hidden(season_year, season_type, week, hidden_from):
    """Bool convenience wrapper around _default_level(), kept for callers
    (and tests) that only care about the hidden/not-hidden distinction."""
    return _default_level(season_year, season_type, week, hidden_from) == LEVEL_HIDDEN


def _default_level_sql(alias, hidden_from):
    """SQL mirror of _default_level() -- see its docstring for the ordering
    rule. Returns (expr, params); expr evaluates to 0 (LEVEL_HIDDEN) or 2
    (LEVEL_FULL), never 1."""
    sql = (
        f"CASE WHEN {alias}.season_year > ? THEN {LEVEL_HIDDEN} "
        f"WHEN {alias}.season_year < ? THEN {LEVEL_FULL} "
        f"ELSE (CASE WHEN ? = 2 "
        f"THEN (CASE WHEN {alias}.season_type = 3 THEN {LEVEL_HIDDEN} "
        f"WHEN {alias}.week >= ? THEN {LEVEL_HIDDEN} ELSE {LEVEL_FULL} END) "
        f"ELSE (CASE WHEN {alias}.season_type = 3 THEN {LEVEL_HIDDEN} ELSE {LEVEL_FULL} END) END) END"
    )
    params = [hidden_from["season_year"], hidden_from["season_year"], hidden_from["season_type"], hidden_from["week"]]
    return sql, params


def _default_hidden_sql(alias, hidden_from):
    """Bool-SQL convenience wrapper around _default_level_sql(), kept for
    callers (and tests) that only care about the hidden/not-hidden
    distinction, mirroring _default_hidden()'s relationship to
    _default_level(). Returns (expr, params); expr evaluates to 1 or 0."""
    expr, params = _default_level_sql(alias, hidden_from)
    return f"(({expr}) = {LEVEL_HIDDEN})", params


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


def level_of_row(row, policy):
    """row must support __getitem__ for game_id/season_year/season_type/
    week -- a sqlite3.Row or a plain dict both work. Precedence: game
    override > week override > default threshold (see module docstring)."""
    game_id = row["game_id"]
    games = policy.get("games", {})
    if game_id in games:
        return games[game_id]
    key = week_key(row["season_year"], row["season_type"], row["week"])
    weeks = policy.get("weeks", {})
    if key in weeks:
        return weeks[key]
    hidden_from = policy.get("hidden_from", DEFAULT_HIDDEN_FROM)
    return _default_level(row["season_year"], row["season_type"], row["week"], hidden_from)


def is_hidden_row(row, policy):
    """Bool convenience wrapper around level_of_row() for callers that only
    care about the fully-hidden/not distinction."""
    return level_of_row(row, policy) == LEVEL_HIDDEN


def set_week(season_year, season_type, week, level):
    """level: 0 | 1 | 2 | a legacy bool | None. None clears the override
    (deletes the key), which is what lets the UI fall back to the default.
    A legacy bool is accepted too (not just int) -- this stores the raw
    value and leans on save_policy()'s _normalize() call to coerce it via
    _coerce_level(), the same pass every value goes through on load, rather
    than duplicating that coercion here (and getting it wrong: a bare
    `int(level)` would silently turn a legacy `True` into LEVEL_SCORE
    instead of LEVEL_HIDDEN, since bool is an int subclass). The whole
    load-modify-save cycle is one critical section (see _LOCK's docstring
    above) so a concurrent set_*() call can't read stale state."""
    with _LOCK:
        policy = load_policy()
        key = week_key(season_year, season_type, week)
        weeks = dict(policy.get("weeks", {}))
        if level is None:
            weeks.pop(key, None)
        else:
            weeks[key] = level
        policy = dict(policy)
        policy["weeks"] = weeks
        return save_policy(policy)


def set_game(game_id, level):
    """See set_week()'s docstring -- same level/bool/None acceptance and
    normalize-on-save handling."""
    with _LOCK:
        policy = load_policy()
        games = dict(policy.get("games", {}))
        if level is None:
            games.pop(game_id, None)
        else:
            games[game_id] = level
        policy = dict(policy)
        policy["games"] = games
        return save_policy(policy)


def set_default(season_year, season_type, week):
    """season_year: int | None. None resets the whole threshold to
    DEFAULT_HIDDEN_FROM and ignores season_type/week. Otherwise all three
    are required -- unlike set_week/set_game, this has no per-field clear;
    the threshold always has some value, it's just a question of which
    one. The threshold itself stays a binary hidden/full cut -- see module
    docstring -- so there is no level parameter here."""
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


def set_user_week(user_id, season_year, season_type, week, level, conn=None):
    """level: 0 | 1 | 2 | a legacy bool | None -- see set_week()'s
    docstring, including why the raw value is stored as-is and left to
    save_user_policy()'s _normalize() call rather than coerced here with a
    bare int(). Same semantics, just scoped to one user_id. The
    read-modify-write is one critical section under users.LOCK (see that
    module's docstring for why this needs to be the same lock object
    users.py itself uses internally, not a separate one) so a concurrent
    set_user_*() call for the same user can't read stale state."""
    own_conn = conn is None
    conn = conn or _users.get_connection()
    try:
        with _users.LOCK:
            policy = get_user_policy(user_id, conn=conn)
            key = week_key(season_year, season_type, week)
            weeks = dict(policy.get("weeks", {}))
            if level is None:
                weeks.pop(key, None)
            else:
                weeks[key] = level
            policy = dict(policy)
            policy["weeks"] = weeks
            return save_user_policy(user_id, policy, conn=conn)
    finally:
        if own_conn:
            conn.close()


def set_user_game(user_id, game_id, level, conn=None):
    """See set_user_week()'s docstring -- same level/bool/None acceptance
    and normalize-on-save handling."""
    own_conn = conn is None
    conn = conn or _users.get_connection()
    try:
        with _users.LOCK:
            policy = get_user_policy(user_id, conn=conn)
            games = dict(policy.get("games", {}))
            if level is None:
                games.pop(game_id, None)
            else:
                games[game_id] = level
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

def redact_game_full(shaped):
    """Nulls out every spoiler field on an already shape_game()-shaped
    dict, for a LEVEL_HIDDEN game. Total and idempotent: every key listed
    here is always present on the input and always present (just nulled)
    on the output."""
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


def redact_game_score_only(shaped):
    """The LEVEL_SCORE redaction: same as redact_game_full() except
    watchability_score/rank/percentile/n_scored survive -- that's the
    entire point of the tier. Everything else stays nulled, in particular
    uw_loss_bonus (its truthiness alone tells you Washington played and
    lost -- a winner leak wearing a number's clothes, see
    web/app.js's gameChips()) and ot (an outcome fact, rendered as its own
    chip)."""
    out = redact_game_full(shaped)
    out["watchability_score"] = shaped["watchability_score"]
    out["rank"] = shaped["rank"]
    out["percentile"] = shaped["percentile"]
    out["n_scored"] = shaped["n_scored"]
    return out


def redact_live_full(live):
    """Nulls out the additive "live" payload built by serve.py's
    build_live_payload(), for a LEVEL_HIDDEN game. status.detail is nulled
    unconditionally -- it's ESPN's shortDetail, which for an overtime game
    can read literally "Final/OT" -- but status.period/clock_display
    survive when period <= 4: "Q3 4:12" is useful for deciding whether to
    tune in right now and isn't itself a spoiler; a period past 4 is an OT
    tell on its own, so that one gets nulled too. `progress` is
    deliberately left alone at every level -- it's how far into the game
    play has gotten, not an outcome fact."""
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


def redact_live_score_only(live):
    """The LEVEL_SCORE redaction for a live payload: same as
    redact_live_full() except live_score survives -- the running composite
    is the "score" a LEVEL_SCORE game reveals, same as watchability_score
    is for a completed one. wp_now/headline/quality_so_far/drama_from_here/
    status.detail and the OT period tell all stay nulled."""
    if live is None:
        return None
    out = redact_live_full(live)
    out["live_score"] = live["live_score"]
    return out


def redact_live_history(rows, level):
    """Per-row redaction for /api/games/<id>'s live_history key (the
    live_score_history rows -- computed_at, progress, live_score,
    quality_so_far, drama_from_here). Called only when level >= LEVEL_SCORE
    -- a LEVEL_HIDDEN game's caller skips this section of the route
    entirely, same as it skips live_payload. At LEVEL_SCORE,
    live_score/progress/computed_at survive (mirrors
    redact_live_score_only's live payload) but quality_so_far/
    drama_from_here -- the same per-half quality/drama signal live_score's
    running total is built from -- stay nulled until LEVEL_FULL."""
    if level >= LEVEL_FULL:
        return rows
    out = []
    for r in rows:
        r = dict(r)
        r["quality_so_far"] = None
        r["drama_from_here"] = None
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Exclusion -- the SQL-level predicate for Games/Top/weekly-peaks/team
# leaderboard, where a game below the caller's minimum level is dropped
# from the result set entirely rather than redacted in place (see the
# two-tier-spoiler plan: exclusion below LEVEL_SCORE is what makes rank
# contiguous, and exclusion below LEVEL_FULL on specific outcome-shaped
# query surfaces is what closes the sort/filter leak channels -- see
# serve.py's /api/games and /api/top for where min_level=LEVEL_FULL is used).
# ---------------------------------------------------------------------------

def level_sql(policy, alias="g"):
    """Returns (sql_fragment, params) -- a SQL expression that evaluates to
    the game's spoiler level (0/1/2). Mirrors level_of_row()'s precedence
    exactly (game override > week override > default).

    Built as small IN-lists for whichever override tiers are non-empty (one
    per level, so a mixed policy can produce up to three IN-lists per
    override kind), falling back to _default_level_sql()'s ordinal
    comparison against season_year/season_type/week -- all three are
    covered by idx_games_season(season_year, season_type, week), so the
    common case (no explicit overrides in play) stays sargable rather than
    degrading into a big IN scan."""
    weeks = policy.get("weeks", {})
    games = policy.get("games", {})
    hidden_from = policy.get("hidden_from", DEFAULT_HIDDEN_FROM)

    week_expr = f"({alias}.season_year || ':' || {alias}.season_type || ':' || {alias}.week)"

    whens = []
    params = []

    def _in_clause(expr, values, result):
        whens.append(f"WHEN {expr} IN ({','.join('?' * len(values))}) THEN {result}")
        params.extend(values)

    for level in (LEVEL_HIDDEN, LEVEL_SCORE, LEVEL_FULL):
        game_ids = [gid for gid, v in games.items() if v == level]
        if game_ids:
            _in_clause(f"{alias}.game_id", game_ids, level)
    for level in (LEVEL_HIDDEN, LEVEL_SCORE, LEVEL_FULL):
        week_keys = [k for k, v in weeks.items() if v == level]
        if week_keys:
            _in_clause(week_expr, week_keys, level)

    # Game overrides must win over week overrides even though the loop
    # above emits game WHENs first followed by week WHENs -- CASE picks the
    # first matching WHEN, and a game_id match can never also match the
    # week_expr IN-list coincidentally (different domains), so ordering
    # game-tier WHENs before week-tier ones here is sufficient, matching
    # level_of_row()'s precedence.
    default_expr, default_params = _default_level_sql(alias, hidden_from)
    default_expr = f"({default_expr})"
    params.extend(default_params)

    if whens:
        case_sql = "CASE " + " ".join(whens) + f" ELSE {default_expr} END"
    else:
        case_sql = default_expr

    return case_sql, params


def visible_sql(policy, alias="g", min_level=LEVEL_SCORE):
    """Returns (sql_fragment, params) -- a boolean SQL expression that is
    true exactly when a game's level is >= min_level, i.e. should be
    INCLUDED in a result set built with that minimum. Defaults to
    LEVEL_SCORE (today's "visible" meaning "not fully hidden"); pass
    min_level=LEVEL_FULL for an outcome-shaped query surface (a sort by
    margin or by a metric, an OT filter, a per-metric leaderboard) where a
    LEVEL_SCORE game would otherwise leak something beyond its score
    through the query itself -- see the two-tier-spoiler plan's leak-
    channels section."""
    expr, params = level_sql(policy, alias)
    return f"(({expr}) >= {int(min_level)})", params
