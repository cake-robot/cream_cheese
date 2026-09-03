"""
Local read-only web UI for the CFB watchability pipeline.

Serves a small JSON API (under /api) plus the static files in web/. Opens
data/cfb.db strictly read-only (mode=ro) so this process can never mutate
pipeline data -- the pipeline (pipeline.py) remains the only writer.

Run from anywhere:
    ./venv/bin/python serve.py
"""

import bisect
import json
import os
import pathlib
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Flask, abort, g, jsonify, redirect, request, send_from_directory, session
from flask_limiter import Limiter

REPO_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from src import config, corrections as corrections_module, db, espn, live, scoring, spoilers, users  # noqa: E402

DB_FILE = (REPO_ROOT / config.DB_PATH).resolve()
WEB_DIR = REPO_ROOT / "web"
AUTH_FILE = REPO_ROOT / "data" / "auth.json"

TOTAL_WEIGHT = sum(m["weight"] for m in scoring.METRICS)
MANUAL_CORRECTION_GAME_IDS = {c["game_id"] for c in corrections_module.CORRECTIONS}

BIN_WIDTH = 0.035
N_BINS = 20

# ---------------------------------------------------------------------------
# Metric prose -- kept separate from scoring.py (which owns weights/caps/math)
# so this file never invents a number, only labels/describes ones scoring.py
# already computed. The assertion below fails fast if a metric is added to
# scoring.py without updating this table.
# ---------------------------------------------------------------------------

METRIC_COPY = {
    "wp_volatility": {
        "label": "WP volatility",
        "description": (
            "Sum of absolute win-probability swings across every play -- the total "
            "distance the game's outcome probability traveled, regardless of when."
        ),
    },
    "lead_changes": {
        "label": "Lead changes",
        "description": (
            "Number of times the score state changes between home-leading, "
            "away-leading, and tied."
        ),
    },
    "time_spent_close": {
        "label": "Time spent close",
        "description": (
            "Share of plays where home win probability sat between 30% and 70%."
        ),
    },
    "team_profile": {
        "label": "Team profile",
        "description": (
            "Credit for ranked teams playing -- sum of both teams' AP-style rank tiers."
        ),
    },
    "upset_risk": {
        "label": "Upset risk",
        "description": (
            "How lopsided the pregame win probability was, scaled by the "
            "better-ranked team's tier. Measures pregame skew only -- it does not "
            "know who actually won."
        ),
    },
    "late_volatility": {
        "label": "Late volatility",
        "description": (
            "Same as WP volatility, but counted only in the 4th quarter and "
            "overtime -- rewards drama that shows up late rather than spread "
            "evenly across the game."
        ),
    },
    "clutch_finish": {
        "label": "Clutch finish",
        "description": (
            "Credit for a team taking the lead in the final minute of regulation "
            "-- breaking a tie or overcoming a deficit -- or tying the game and "
            "that tie holding into overtime. Worth more if it isn't a field "
            "goal. A score that pads an already-held lead doesn't count, and "
            "neither does a tie that gets broken again before regulation ends. "
            "Every game that reaches overtime gets at least a 0.7 floor even "
            "with no such swing in the final minute."
        ),
    },
    "comeback_erosion": {
        "label": "Comeback",
        "description": (
            "Credit for a commanding lead getting torn down -- measured once per "
            "lead-change arc, in coin-flip-normalized win-probability terms, so a "
            "heavy pregame favorite's high WP off a modest lead doesn't count on "
            "its own."
        ),
    },
}

assert set(METRIC_COPY) == set(scoring.METRICS_BY_NAME), (
    "METRIC_COPY is out of sync with scoring.METRICS -- add/remove an entry "
    "to match every registered metric"
)

# ---------------------------------------------------------------------------
# Live metric prose -- same pattern as METRIC_COPY above, kept as a wholly
# separate table from it on purpose: the live registries (src/live.py) are
# tuned for partial data and must never be confused with, or merged into,
# the retrospective METRIC_COPY/METRICS that the scored corpus depends on.
# ---------------------------------------------------------------------------

# naLabel: shown in place of a value when the metric fn returns None for a
# live game (see live.py's per-metric None gates). Only metrics that can
# actually go null carry one -- each reason is specific to that metric's own
# gate, since "not applicable" during a live game is almost never about
# overtime (only late_volatility_rate/clutch_finish gate on the late-game
# window, and even that's "before the 4th quarter", not "in overtime").
LIVE_METRIC_COPY = {
    "wp_volatility_rate": {"label": "WP volatility (rate)", "description":
        "Sum of absolute win-probability swings so far, divided by how much of the game has "
        "elapsed -- lets an early-game hot streak be compared fairly against a full 60 minutes.",
        "naLabel": "not applicable yet -- too early in the game"},
    "lead_change_rate": {"label": "Lead changes (rate)", "description":
        "Lead/tie changes so far, divided by elapsed progress.",
        "naLabel": "not applicable yet -- too early in the game"},
    "comeback_erosion_live": {"label": "Comeback", "description":
        "Credit for a commanding lead already eroding, in coin-flip-normalized win-probability "
        "terms -- same basis as the retrospective Comeback metric, except a material swing away "
        "from the current lead doesn't require an actual tie or lead change to count yet."},
    "upset_in_progress": {"label": "Upset in progress", "description":
        "How far the pregame favorite's win probability has already fallen from its opening "
        "line, scaled by the better-ranked team's tier.",
        "naLabel": "not applicable -- no pregame line available"},
    "team_profile": {"label": "Team profile", "description":
        "Credit for ranked teams playing -- identical to the retrospective metric of the same name."},
    "upset_risk": {"label": "Upset risk", "description":
        "How lopsided the pregame line was, scaled by rank quality -- identical to the "
        "retrospective metric of the same name."},
    "late_volatility_rate": {"label": "Late volatility (rate)", "description":
        "WP swings in the 4th quarter or overtime so far, divided by how much of the late-game "
        "window has elapsed. Not applicable until that window opens.",
        "naLabel": "not applicable yet -- before the 4th quarter"},
    "clutch_finish": {"label": "Clutch finish", "description":
        "Credit for a decisive score in the final minute of regulation, or reaching overtime. "
        "Not applicable until the 4th quarter starts.",
        "naLabel": "not applicable yet -- before the 4th quarter"},
    "tension_now": {"label": "Tension right now", "description":
        "How close the win probability is at this exact moment, weighted up sharply the later "
        "in the game it is -- the core 'is this worth turning on right now' signal.",
        "naLabel": "not applicable -- no live win probability yet"},
    "upset_finish_potential": {"label": "Upset finish potential", "description":
        "How much upset the pregame favorite could still lose by from here, weighted by rank "
        "quality and lateness.",
        "naLabel": "not applicable -- no live win probability yet"},
    "recent_volatility": {"label": "Recent swings", "description":
        "Win-probability volatility over just the last 20 plays -- momentum, not the whole game.",
        "naLabel": "not applicable yet -- not enough recent plays"},
    "ot_live": {"label": "Overtime", "description":
        "A game already in overtime is guaranteed more drama from here; grows with each "
        "additional OT period."},
}

_ALL_LIVE_NAMES = {m["name"] for m in live.LIVE_SO_FAR_METRICS} | {m["name"] for m in live.LIVE_FROM_HERE_METRICS}
assert set(LIVE_METRIC_COPY) == _ALL_LIVE_NAMES, (
    "LIVE_METRIC_COPY is out of sync with live.LIVE_SO_FAR_METRICS/LIVE_FROM_HERE_METRICS -- "
    "add/remove an entry to match every registered live metric"
)

NOT_IMPLEMENTED = [
    {
        "name": "comeback_magnitude",
        "note": (
            "Largest win-probability deficit overcome by the eventual winner. "
            "Explicitly requested in the user's personal notes ('doesn't need "
            "consummation' -- a near-comeback should count too), but has no "
            "metric function or game_metrics rows today."
        ),
    },
    {
        "name": "final_margin",
        "note": "Absolute point differential. Not implemented.",
    },
    {
        "name": "scoring_volume",
        "note": "Combined final score. Not implemented.",
    },
    {
        "name": "pregame_uncertainty",
        "note": (
            "1 - 2*|initial_home_wp - 0.5|. Not stored as its own metric -- "
            "upset_risk uses the inverse of this same skew term, scaled by rank "
            "quality, but pregame_uncertainty itself does not exist."
        ),
    },
    {
        "name": "upset_confirmed",
        "note": (
            "Whether the pregame underdog actually won. upset_risk measures "
            "pregame skew only, not the outcome -- this is a genuinely "
            "different, unimplemented idea."
        ),
    },
    {
        "name": "ot_bonus",
        "note": (
            "A dedicated overtime bonus/flag as its own scored metric. OT only "
            "shows up implicitly today, via late_volatility's period window and "
            "clutch_finish's n/a."
        ),
    },
    {
        "name": "turnovers_big_plays",
        "note": (
            "No turnover or play-type data exists anywhere in the schema -- "
            "would require a new data source."
        ),
    },
]

FOX_FLAG_CTE_SQL = "fox_flag AS (SELECT DISTINCT game_id FROM fox_score_corrections WHERE tier = 'diff')"

OT_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM win_probability wp "
    "WHERE wp.game_id = g.game_id AND wp.period_number > 4)"
)

app = Flask(__name__, static_folder=None)


def _load_or_create_secret_key():
    """app.secret_key signs the session cookie -- data/auth.json holds only
    this, never a password (those live hashed in data/users.db via
    src/users.py). Auto-generated on first run rather than requiring a
    manual init step: an unreadable/corrupt file is a real problem (fail
    loudly), but a missing one just means this is the first run anywhere,
    which should work out of the box. Regenerating it invalidates every
    existing session cookie, which is a minor inconvenience, not a
    security or data-integrity issue -- so this file is worth including in
    backups (see `just backup`) but isn't precious the way users.db is."""
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text())
            key = data.get("secret_key")
        except (OSError, ValueError):
            key = None
        if isinstance(key, str) and len(key) >= 32:
            return key
        raise SystemExit(f"FATAL: {AUTH_FILE} exists but doesn't contain a valid secret_key")
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    AUTH_FILE.write_text(json.dumps({"secret_key": key}, indent=2) + "\n")
    print(f"[serve.py] generated a new session secret at {AUTH_FILE}")
    return key


app.secret_key = _load_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Tied to CC_PUBLIC_ORIGIN (see ALLOWED_POST_ORIGINS below) rather than
    # hardcoded True: once that's set we're expected to be reached only via
    # the HTTPS tunnel, so the cookie can require Secure. Left off for
    # plain-http local/loopback testing, where a Secure cookie would just
    # silently never be sent back by the browser and every login would
    # appear to fail for no visible reason.
    SESSION_COOKIE_SECURE=bool(os.environ.get("CC_PUBLIC_ORIGIN", "").startswith("https://")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def _rate_limit_key():
    """cloudflared sets CF-Connecting-IP on every request it proxies, so
    this is the real visitor IP once the tunnel is the only way in. Falls
    back to remote_addr for local/loopback testing where that header is
    never set. This is only a safe key precisely because serve.py stays
    bound to 127.0.0.1 (see the __main__ block) with cloudflared as the
    sole ingress -- a client talking to this process directly could forge
    CF-Connecting-IP and pick its own rate-limit bucket. If this process is
    ever bound to a non-loopback address directly (no tunnel in front of
    it), this key function must change first."""
    return request.headers.get("CF-Connecting-IP", request.remote_addr) or "unknown"


limiter = Limiter(key_func=_rate_limit_key, app=app, storage_uri="memory://", default_limits=["120 per minute"])


@limiter.request_filter
def _exempt_static_from_rate_limit():
    # default_limits above applies only to /api/* -- static assets
    # (style.css, app.js, charts.js, every page) and the two auth pages
    # stay unlimited. The two POST routes that most need a tighter cap
    # (login, signup) get their own stricter @limiter.limit() below, which
    # stacks with -- doesn't replace -- the 120/minute default.
    return not request.path.startswith("/api/")


# ---------------------------------------------------------------------------
# Connection handling -- one read-only connection per request, never write-mode.
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        if not DB_FILE.exists():
            abort(500, description=f"database not found at {DB_FILE}")
        try:
            conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            abort(503, description="the pipeline appears to be mid-write; retry in a moment")
        conn.row_factory = sqlite3.Row
        # Read-only-safe (doesn't touch _startup_selfcheck's writability
        # probe) and covers SQLITE_BUSY *mid-query* -- previously only the
        # connect-time OperationalError above was caught, but the live
        # poller (src/live.py) now checkpoints WAL far more often than the
        # old batch pipeline did, since --live writes every ~60s instead of
        # once per manual run.
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def get_users_db():
    """Fresh per-request connection to data/users.db, same lifecycle as
    get_db()'s cfb.db connection above -- opened lazily, closed in
    close_db()'s teardown. Unlike get_db() this one is read-write: accounts,
    invites, and per-user spoiler policy all live here (see src/users.py's
    module docstring for why this is a second database rather than a table
    in cfb.db)."""
    if "users_db" not in g:
        g.users_db = users.get_connection()
    return g.users_db


def _startup_selfcheck():
    if not DB_FILE.exists():
        raise SystemExit(f"FATAL: database not found at {DB_FILE}")
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE __ro_probe__ (x)")
        conn.close()
        raise SystemExit("FATAL: database opened writable -- refusing to start")
    except sqlite3.OperationalError:
        pass
    sqlite_version = conn.execute("SELECT sqlite_version() AS v").fetchone()["v"]
    scored = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE watchability_score IS NOT NULL"
    ).fetchone()["n"]
    conn.close()
    print(
        f"[serve.py] read-only OK -- sqlite {sqlite_version}, {scored} scored games, "
        f"{len(scoring.METRICS)} metrics, total_weight={TOTAL_WEIGHT}"
    )

    # Spoiler prefs live in their own file (data/spoilers.json), never in
    # cfb.db -- see src/spoilers.py's module docstring for why. Confirm the
    # policy loads (a corrupt file falls back to defaults, never raises)
    # and that its directory is writable, since the /api/spoilers/* routes
    # need a write path this process's DB connection deliberately lacks.
    policy = spoilers.load_policy()
    if not os.access(spoilers.POLICY_PATH.parent, os.W_OK):
        raise SystemExit(f"FATAL: {spoilers.POLICY_PATH.parent} is not writable -- spoiler toggles need it")
    hf = policy["hidden_from"]
    hf_label = f"{hf['season_year']} postseason" if hf["season_type"] == 3 else f"{hf['season_year']} week {hf['week']}"
    print(
        f"[serve.py] spoilers OK -- {len(policy['weeks'])} week rules, {len(policy['games'])} game rules, "
        f"default hidden from {hf_label} onward"
    )

    # data/users.db: accounts, invites, and (once a user has saved settings
    # at least once) per-user spoiler policy. init_db() is idempotent --
    # CREATE TABLE IF NOT EXISTS -- so this is safe to run on every start,
    # same as db.init_db() for cfb.db in pipeline.py.
    users_conn = users.init_db()
    if not os.access(users.DB_PATH.parent, os.W_OK):
        raise SystemExit(f"FATAL: {users.DB_PATH.parent} is not writable -- accounts need it")
    n_users = users_conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    n_open_invites = users_conn.execute(
        "SELECT COUNT(*) AS n FROM invites WHERE redeemed_by IS NULL"
    ).fetchone()["n"]
    users_conn.close()
    print(f"[serve.py] accounts OK -- {n_users} user(s), {n_open_invites} unredeemed invite(s)")
    if n_users == 0:
        print("[serve.py] no accounts exist yet -- run `just create-admin <username>` before logging in")


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()
    users_db = g.pop("users_db", None)
    if users_db is not None:
        users_db.close()


# ---------------------------------------------------------------------------
# Auth gate -- registered ahead of _guard_writes below (Flask runs
# before_request hooks in registration/definition order), so an
# unauthenticated request never reaches either the origin check or a route
# handler. Login-wall-on-everything: every path requires a session except
# the small allowlist needed to reach the login/signup pages themselves and
# the loopback-only healthz probe `just status` depends on.
# ---------------------------------------------------------------------------

_UNAUTH_PATHS = {"/login.html", "/signup.html", "/style.css", "/api/login", "/api/signup"}
# style.css's @font-face rules point at these -- without the prefix here,
# an unauthenticated request for one 302s to /login.html (the font-file
# request, not the page) and the browser silently falls back to a system
# font, so login.html/signup.html -- the only pages reachable pre-auth --
# would never actually render in the intended type.
_UNAUTH_PREFIXES = ("/fonts/",)


def current_user():
    """The logged-in user's row, or None -- cached on flask.g like get_db().
    A session whose session_epoch doesn't match the user's current one
    (password changed, or an admin forced a logout) is treated as if there
    were no session at all, rather than raising -- the cookie itself is
    still well-formed, it's just stale, and the right behavior for a stale
    cookie is the same as no cookie: fall through to logged-out.

    When CC_DISABLE_AUTH=1 (see _require_auth), routes past the gate still
    assume a real user row -- e.g. spoiler_ctx()'s policy lookup. Falls back
    to user_id 1 (the only account that exists) instead of None so that
    invariant holds for anonymous requests too while the wall is down."""
    if "user" not in g:
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            row = users.get_user_by_id(get_users_db(), user_id)
            if row is not None and row["session_epoch"] == session.get("session_epoch"):
                g.user = row
        if g.user is None and os.environ.get("CC_DISABLE_AUTH") == "1":
            g.user = users.get_user_by_id(get_users_db(), 1)
    return g.user


def _require_admin():
    """Gate for the Feed page's routes (/api/feed*) -- ops internals (raw
    URLs, error text, per-caller/per-cycle breakdowns) aren't for a general
    account the way the rest of the app's read routes are. Call after
    _require_auth has already run (i.e. from inside a route, not another
    before_request hook) -- current_user() is guaranteed non-None by then
    except under CC_DISABLE_AUTH, where it falls back to user_id 1."""
    user = current_user()
    if user is None or not user["is_admin"]:
        abort(403, description="admin only")


def _is_loopback(addr):
    return addr in ("127.0.0.1", "::1")


@app.before_request
def _require_auth():
    # Temporary escape hatch: CC_DISABLE_AUTH=1 in the launchd plist's env
    # drops the login wall entirely. Manual, deploy-time-only toggle -- flip
    # the plist back and reinstall the service to restore the wall, there is
    # no in-app way to turn it off. TODO(cream_cheese): remove once no
    # longer needed for the requested temporary pause.
    if os.environ.get("CC_DISABLE_AUTH") == "1":
        return None
    path = request.path
    if path in _UNAUTH_PATHS or path.startswith(_UNAUTH_PREFIXES):
        return None
    if path == "/api/healthz":
        # cloudflared sets CF-Connecting-IP on every request it proxies;
        # its absence together with a loopback remote_addr is what
        # distinguishes `just status`'s own curl (never left the machine)
        # from a real visitor arriving through the tunnel, who must still
        # authenticate -- healthz exposes scored-game counts and live-feed
        # staleness, which have no business being public.
        if "CF-Connecting-IP" not in request.headers and _is_loopback(request.remote_addr):
            return None
    if current_user() is None:
        if path.startswith("/api/"):
            abort(401, description="login required")
        target = path + (("?" + request.query_string.decode("utf-8", "ignore")) if request.query_string else "")
        return redirect(f"/login.html?next={quote(target, safe='')}")
    return None


ALLOWED_POST_ORIGINS = {"http://127.0.0.1:5050", "http://localhost:5050"}
# The public tunnel hostname, once one exists, is supplied at deploy time
# rather than hardcoded here -- e.g. CC_PUBLIC_ORIGIN=https://cfb.example.com.
_public_origin = os.environ.get("CC_PUBLIC_ORIGIN")
if _public_origin:
    ALLOWED_POST_ORIGINS.add(_public_origin)


@app.before_request
def _guard_writes():
    """Every POST route in this app writes only to data/users.db (accounts,
    invites, per-user spoiler policy via src/users.py) -- cfb.db stays
    strictly read-only. This is the only CSRF defense: no Origin header, or
    an Origin outside the allowlist,
    is rejected outright. Failing open on a missing Origin was tolerable
    when this only ever ran on loopback with a trusted single user; once a
    tunnel makes the app reachable from anywhere, an absent Origin is no
    longer good evidence of a same-origin request, so it's treated the same
    as a wrong one."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin not in ALLOWED_POST_ORIGINS:
            abort(403, description="cross-origin POST rejected")


@app.after_request
def _no_store(resp):
    """Nothing else in this app sets cache headers, so a browser's bfcache
    could otherwise restore a fully-rendered unredacted page (or a stale
    /api/games response with a now-hidden game's score) after a spoiler
    setting changes and the user hits Back."""
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # 'unsafe-inline' is required for both script-src and style-src: every
    # page in web/ has multiple inline <script> blocks plus a few inline
    # style= attributes, and there is no templating layer here to add
    # per-request nonces without a much larger rewrite. Everything else is
    # same-origin -- no CDN dependency exists anywhere in web/ -- so this is
    # still a meaningful restriction against injected third-party content.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )
    return resp


# spoiler_hidden now means "spoiler_level < LEVEL_FULL" (see shape_game()),
# i.e. true for both LEVEL_HIDDEN and LEVEL_SCORE -- so the fields a
# LEVEL_SCORE game is *allowed* to reveal (its whole reason to exist) must
# not be flagged when that's the level in play. Split accordingly:
# always-redacted whenever spoiler_hidden is true, vs. redacted only at
# LEVEL_HIDDEN specifically.
_SPOILER_ALWAYS_REDACTED_FIELDS = ("uw_loss_bonus", "applicable_weight")
_SPOILER_LEVEL0_ONLY_FIELDS = ("watchability_score", "rank", "percentile", "n_scored")


def _walk_spoiler_leaks(node, path=""):
    """Recursively walk a JSON-able structure; for any dict carrying
    spoiler_hidden: true, yield a description of any field that should
    have been redacted but wasn't. A missing spoiler_level (an older
    payload shape) is treated as LEVEL_HIDDEN -- the strictest check --
    rather than skipped. Dev-only tripwire (see _assert_no_spoiler_leaks
    below) -- not a substitute for tests/test_spoilers_api.py's scanner
    test, just a second net for whatever that test didn't anticipate."""
    if isinstance(node, dict):
        if node.get("spoiler_hidden") is True:
            level = node.get("spoiler_level", spoilers.LEVEL_HIDDEN)
            fields = _SPOILER_ALWAYS_REDACTED_FIELDS
            if level == spoilers.LEVEL_HIDDEN:
                fields = fields + _SPOILER_LEVEL0_ONLY_FIELDS
            for f in fields:
                if f in node and node[f] is not None:
                    yield f"{path}.{f} is non-null on a spoiler_hidden game (level={level})"
            away, home = node.get("away"), node.get("home")
            if isinstance(away, dict) and away.get("score") is not None:
                yield f"{path}.away.score is non-null on a spoiler_hidden game"
            if isinstance(home, dict) and home.get("score") is not None:
                yield f"{path}.home.score is non-null on a spoiler_hidden game"
            if node.get("ot") is not None:
                yield f"{path}.ot is non-null on a spoiler_hidden game"
            if node.get("metrics"):
                yield f"{path}.metrics is non-empty on a spoiler_hidden game"
        for k, v in node.items():
            yield from _walk_spoiler_leaks(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_spoiler_leaks(v, f"{path}[{i}]")


SPOILER_TRIPWIRE = os.environ.get("CC_SPOILER_TRIPWIRE", "1") != "0"


@app.after_request
def _assert_no_spoiler_leaks(resp):
    """Walks every JSON response and fails loudly if a spoiler_hidden game
    still carries a redacted field, turning "a new endpoint forgot to
    think about spoilers" into an immediate 500 instead of a silent leak.

    Independent of app.debug on purpose: this used to be gated on debug
    mode, but debug=True is not safe to run once the app is reachable from
    outside loopback (Werkzeug's debugger is remote code execution if
    exposed), and disabling debug must not also disable the one thing in
    this file that actively watches for a spoiler leaking to someone other
    than you. Set CC_SPOILER_TRIPWIRE=0 to turn it off (e.g. if the extra
    per-response walk ever shows up as real latency)."""
    if not SPOILER_TRIPWIRE or not resp.is_json:
        return resp
    try:
        data = resp.get_json()
    except Exception:
        return resp
    leaks = list(_walk_spoiler_leaks(data))
    if leaks:
        raise AssertionError(f"spoiler leak on {request.path}: {leaks[:5]}")
    return resp


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def spoiler_ctx():
    """The current request's per-user spoiler policy, computed once and
    cached on flask.g -- mirrors get_db()'s per-request caching pattern.

    Policy is per-user (src/spoilers.py's get_user_policy(), backed by
    data/users.db) rather than the single shared data/spoilers.json this
    used to read -- current_user() is guaranteed non-None here because
    every route that reaches this point already passed the _require_auth
    gate above, which is registered before any view function runs.

    Used to return (policy, revealed_ids) -- a `reveal=<game_id>` query
    param that won over every policy tier, including an explicit "hidden"
    game override, for the current browser session only. Replaced by
    game.html's "Reveal this game" POSTing a real hidden:false game
    override (the same /api/spoilers/game route Settings' game-override
    card already used) -- one mechanism, visible and undoable from
    Settings, instead of two."""
    if "spoiler_ctx" not in g:
        g.spoiler_ctx = spoilers.get_user_policy(current_user()["user_id"], conn=get_users_db())
    return g.spoiler_ctx


def ranked_cte(alias="g"):
    """A `ranked AS (...)` CTE scoped to games that are both scored AND
    visible under the current request's spoiler policy -- ranks and
    percentiles are computed over the visible set only, so excluding
    hidden games from a result list never leaves gaps in rank (a gap
    itself would say "a hidden game is right here"). Returns (cte_sql,
    params); params must be spliced into the query's param list ahead of
    the rest of that query's own WHERE params, since the CTE is first in
    the WITH clause."""
    policy = spoiler_ctx()
    visible_clause, params = spoilers.visible_sql(policy, alias=alias)
    cte_sql = f"""
        ranked AS (
            SELECT game_id,
                   RANK() OVER (ORDER BY watchability_score DESC) AS rnk,
                   COUNT(*) OVER () AS n_scored
            FROM games {alias} WHERE watchability_score IS NOT NULL AND {visible_clause}
        )
    """
    return cte_sql, params


def percentile_from_rank(rank, n):
    """'Better than X% of games'. Rank 1 of 1828 -> 99, not 100 (matches the
    plan's convention: the top game outranks n-1 of n peers, and we report a
    floor so the median of an even corpus reads as a clean 50)."""
    if rank is None or not n:
        return None
    return int((n - rank) * 100 // n)


# Minimum percentile (among every scored game's value for that same metric)
# a metric must clear to appear in a "Top games" row's top_contributors --
# keeps the "Driven by X + Y" line from naming a metric that barely moved
# the needle just because it happened to be this game's highest of a weak set.
TOP_CONTRIBUTOR_PERCENTILE_MIN = 60


def fetch_metric_distributions(conn):
    """Sorted norm_value list per metric name, across every scored game's
    game_metrics rows -- the reference population metric_percentile() ranks
    a single game's value against."""
    rows = conn.execute(
        "SELECT metric_name, norm_value FROM game_metrics ORDER BY metric_name, norm_value"
    ).fetchall()
    dists = {}
    for r in rows:
        dists.setdefault(r["metric_name"], []).append(r["norm_value"])
    return dists


def metric_percentile(sorted_vals, v):
    """'Better than X% of games' for a single metric's value, same
    nearest-rank convention as percentile_from_rank (count strictly below,
    over population size)."""
    if not sorted_vals:
        return None
    return bisect.bisect_left(sorted_vals, v) * 100 // len(sorted_vals)


def build_metrics_map(metric_rows):
    """Registry-driven map: every metric name is always a key, in registry
    order. A metric with no game_metrics row (not applicable) maps to None --
    never to a zero. A metric with a real raw value of 0 maps to a normal
    dict with raw=0.0, distinguishable from None at every layer above this."""
    by_name = {r["metric_name"]: r for r in metric_rows}
    out = {}
    for m in scoring.METRICS:
        name = m["name"]
        row = by_name.get(name)
        if row is None:
            out[name] = None
            continue
        raw = row["raw_value"]
        norm = row["norm_value"]
        out[name] = {
            "raw": raw,
            "norm": norm,
            "weighted": norm * m["weight"],
            "at_cap": norm >= 1.0,
            "applicable": True,
        }
    return out


def applicable_weight_of(metrics_map):
    total = 0.0
    for m in scoring.METRICS:
        if metrics_map.get(m["name"]) is not None:
            total += m["weight"]
    return total


def fetch_metrics_maps(conn, game_ids):
    """Batch-fetch game_metrics for many games at once and return
    {game_id: metrics_map}. Callers must only pass ids of SCORED games --
    an unscored game's metrics map is always {} (never scored), handled by
    the caller, not by an empty result from this function."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"SELECT game_id, metric_name, raw_value, norm_value FROM game_metrics "
        f"WHERE game_id IN ({placeholders})",
        game_ids,
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["game_id"], []).append(r)
    return {gid: build_metrics_map(grouped.get(gid, [])) for gid in game_ids}


def fetch_fox_diff_game_ids(conn, game_ids):
    """Which of these games have a real Fox-derived value substitution
    (tier='diff') -- deliberately excludes tier='unusable' rows, which
    mean reconciliation was attempted and inconclusive, not that any value
    was actually corrected from Fox."""
    if not game_ids:
        return set()
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"SELECT DISTINCT game_id FROM fox_score_corrections "
        f"WHERE tier = 'diff' AND game_id IN ({placeholders})",
        game_ids,
    ).fetchall()
    return {r["game_id"] for r in rows}


def histogram(scores, bin_width=BIN_WIDTH, n_bins=N_BINS):
    bins = [0] * n_bins
    for s in scores:
        idx = int(s // bin_width)
        idx = max(0, min(idx, n_bins - 1))
        bins[idx] += 1
    return {"bin_width": bin_width, "bins": bins}


def shape_game(row, metrics_map, rank=None, n_scored=None, has_fox_correction=False, has_manual_correction=False):
    """The canonical game JSON shape shared by every endpoint that lists or
    embeds a game. `metrics_map` must be {} for an unscored game (never a
    registry of nulls) -- that distinction is the caller's responsibility.

    has_fox_correction and has_manual_correction are deliberately separate:
    a game can have either, both, or neither, and they mean different
    things (an actual Fox-derived value substitution vs. a hand-verified
    override) -- collapsing them into one flag makes the UI claim "FOX"
    on games that were only ever fixed by hand (see corrections.py).

    This is also the single choke point for spoiler redaction (see
    src/spoilers.py's module docstring): every field below is computed
    first, then nulled out by spoilers.redact_game_full()/
    redact_game_score_only() according to the game's current spoiler
    level. Because every endpoint that lists or embeds a game goes through
    this function, a new endpoint that forgets to think about spoilers
    fails by over-redacting rather than leaking."""
    scored = row["watchability_score"] is not None
    pct = percentile_from_rank(rank, n_scored) if scored else None
    is_ot = row["is_ot"] if "is_ot" in row.keys() else None
    out = {
        "game_id": row["game_id"],
        "season_year": row["season_year"],
        "season_type": row["season_type"],
        "week": row["week"],
        "event_note": row["event_note"],
        "game_date": row["game_date"],
        "away": {
            "abbr": row["away_team_abbr"],
            "name": row["away_team_name"],
            "rank": row["away_rank"],
            "score": row["away_score"],
        },
        "home": {
            "abbr": row["home_team_abbr"],
            "name": row["home_team_name"],
            "rank": row["home_rank"],
            "score": row["home_score"],
        },
        "venue_name": row["venue_name"],
        "attendance": row["attendance"],
        "conference_game": bool(row["conference_game"]),
        "neutral_site": bool(row["neutral_site"]),
        "completed": bool(row["completed"]),
        "status_state": row["status_state"],
        "ot": (bool(is_ot) if is_ot is not None else None),
        "initial_home_wp": row["initial_home_wp"],
        "watchability_score": row["watchability_score"],
        "uw_loss_bonus": (
            scoring.uw_loss_bonus(row["home_team_id"], row["away_team_id"], row["home_score"], row["away_score"])
            if scored else None
        ),
        "rank": rank if scored else None,
        "percentile": pct,
        "n_scored": n_scored if scored else None,
        "has_fox_correction": bool(has_fox_correction),
        "has_manual_correction": bool(has_manual_correction),
        "metrics": metrics_map if scored else {},
        "applicable_weight": (applicable_weight_of(metrics_map) if scored else None),
    }
    policy = spoiler_ctx()
    level = spoilers.level_of_row(row, policy)
    out["spoiler_level"] = level
    # "Something is hidden" -- true for LEVEL_HIDDEN *and* LEVEL_SCORE, not
    # just LEVEL_HIDDEN. Every existing client/server guard that reads this
    # flag was written when hidden was the only state below full reveal, so
    # keeping it true for LEVEL_SCORE too means an un-updated call site
    # keeps over-redacting a score-only game rather than treating it as
    # fully revealed and rendering a field that's actually null (see the
    # two-tier-spoiler plan's JSON-contract section).
    out["spoiler_hidden"] = level < spoilers.LEVEL_FULL
    if level == spoilers.LEVEL_HIDDEN:
        return spoilers.redact_game_full(out)
    if level == spoilers.LEVEL_SCORE:
        return spoilers.redact_game_score_only(out)
    return out


def fetch_live_metrics_maps(conn, game_ids):
    """Batch-fetch live_metrics for many games, {game_id: {"so_far": {...},
    "from_here": {...}}}. Mirrors fetch_metrics_maps' batching but keyed by
    half as well as metric name, and applicability is read from the stored
    `applicable` column rather than row-absence (see live_metrics' schema
    comment in src/db.py -- a live "not applicable yet" is a state the UI
    renders, not an implicit null)."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"SELECT game_id, half, metric_name, raw_value, norm_value, weight, applicable "
        f"FROM live_metrics WHERE game_id IN ({placeholders})",
        game_ids,
    ).fetchall()
    out = {gid: {"so_far": {}, "from_here": {}} for gid in game_ids}
    for r in rows:
        out[r["game_id"]][r["half"]][r["metric_name"]] = {
            "raw": r["raw_value"],
            "normalized": r["norm_value"],
            "weight": r["weight"],
            "applicable": bool(r["applicable"]),
        }
    return out


def build_live_payload(row, metrics_for_game, level=spoilers.LEVEL_FULL):
    """The additive "live" key attached to a game's shape_game() output for
    any game currently tracked in live_scores. `row` must carry the
    live_scores columns plus status_period/status_clock_display/status_detail
    from `games` and a `stale_seconds` column computed by the caller's SQL
    (julianday-based, so it doesn't need Python-side datetime parsing of
    computed_at).

    This is the second spoiler-redaction choke point (shape_game() is the
    first) -- `level` picks spoilers.redact_live_full()/
    redact_live_score_only() before this ever leaves the function, so a
    live-tracked game below LEVEL_FULL can never surface its game score,
    headline, or quality/drama bars through this payload. At LEVEL_SCORE
    the running composite (live_score) does survive -- that's the tier's
    whole point, mirroring watchability_score for a completed game."""
    payload = {
        "live_score": row["live_score"],
        "quality_so_far": row["quality_so_far"],
        "drama_from_here": row["drama_from_here"],
        "progress": row["progress"],
        "wp_now": row["wp_now"],
        "status": {
            "period": row["status_period"],
            "clock_display": row["status_clock_display"],
            "detail": row["status_detail"],
        },
        "so_far": {"applicable_weight": row["so_far_weight"], "metrics": metrics_for_game["so_far"]},
        "from_here": {"applicable_weight": row["from_here_weight"], "metrics": metrics_for_game["from_here"]},
        "headline": row["headline"],
        "computed_at": row["computed_at"],
        "stale_seconds": row["stale_seconds"],
    }
    if level == spoilers.LEVEL_HIDDEN:
        return spoilers.redact_live_full(payload)
    if level == spoilers.LEVEL_SCORE:
        return spoilers.redact_live_score_only(payload)
    return payload


def build_misalignment_callout(rows):
    """One or two generated sentences naming whichever metric most
    over-delivers vs. its designed weight, and whichever most under-delivers.
    Never hardcoded to a specific metric name -- if weights/caps are retuned,
    this text follows automatically."""
    if not rows:
        return None
    over = max(rows, key=lambda r: r["delta"])
    under = min(rows, key=lambda r: r["delta"])
    if over["name"] == under["name"]:
        return None
    return (
        f"{over['label']} carries weight {over['weight']} "
        f"({over['designed_share'] * 100:.1f}% of the designed total) but delivers "
        f"{over['delivered_share'] * 100:.1f}% of the mean score -- more than its "
        f"weight alone would suggest. {under['label']}, at weight {under['weight']} "
        f"({under['designed_share'] * 100:.1f}% designed), delivers only "
        f"{under['delivered_share'] * 100:.1f}%."
    )


def period_label(p):
    if p == 0:
        return "Pregame"
    if p <= 4:
        return f"Q{p}"
    return f"OT{p - 4}"


def _fox_clock_display(period, elapsed_seconds):
    """Inverse of fox._regulation_elapsed_seconds -- 'MM:SS' remaining in
    the period. None for OT/pregame, where Fox's elapsed_seconds is
    synthetic rather than a real clock reading (see fox.py)."""
    if period is None or period <= 0 or period > 4 or elapsed_seconds is None:
        return None
    remaining = 900 - (elapsed_seconds - (period - 1) * 900)
    if remaining < 0 or remaining > 900:
        return None
    return f"{remaining // 60}:{remaining % 60:02d}"


def _fold_try_into_touchdown(score_changes, is_try_of_prev):
    """Collapse a made PAT/two-point-try entry into its preceding touchdown
    entry so the score ladder/chart draws one mark per real scoring
    possession, not one per WP/score-feed row -- ESPN's and Fox's feeds are
    each inconsistent about whether the try gets its own row versus already
    being folded into the touchdown's by the time it's recorded, so without
    this the same TD+PAT can render as one bar in one game and two in
    another. `is_try_of_prev(prev, cur)` decides whether `cur` is the try
    belonging to `prev`'s touchdown."""
    folded = []
    for sc in score_changes:
        if folded and is_try_of_prev(folded[-1], sc):
            prev = folded[-1]
            prev["delta"] += sc["delta"]
            prev["home"] = sc["home"]
            prev["away"] = sc["away"]
            prev["i"] = sc["i"]
            if "exact" in prev and "exact" in sc:
                prev["exact"] = prev["exact"] and sc["exact"]
        else:
            folded.append(dict(sc))
    return folded


def build_wp_payload(wp_rows, game_row):
    """Parallel-array WP series for the game-detail chart. See serve.py's
    module docstring notes / the design plan for why: play-ordinal x-axis
    (elapsed is non-monotonic in 356 games), whole-series period carry
    forward (not just row 0), and a sanitized non-decreasing score ladder
    mirroring scoring.lead_changes()'s own sanitization so the chart can
    never visually disagree with the stored metric."""
    n = len(wp_rows)
    home_win_pct = [r["home_win_pct"] for r in wp_rows]
    period_raw = [r["period_number"] for r in wp_rows]
    clock_display = [r["clock_display"] for r in wp_rows]
    elapsed = [r["clock_seconds_elapsed"] for r in wp_rows]
    home_score_raw = [r["home_score"] for r in wp_rows]
    away_score_raw = [r["away_score"] for r in wp_rows]

    period_filled = []
    last = 0
    for p in period_raw:
        if p is not None:
            last = p
        period_filled.append(last)

    home_clean, away_clean = [], []
    h, a = 0, 0
    for rh, ra in zip(home_score_raw, away_score_raw):
        if rh is not None and rh >= h:
            h = rh
        if ra is not None and ra >= a:
            a = ra
        home_clean.append(h)
        away_clean.append(a)

    period_starts = []
    prev = None
    for idx, p in enumerate(period_filled):
        if p != prev:
            period_starts.append({"i": idx, "period": p, "label": period_label(p)})
            prev = p

    score_changes = []
    ph, pa = 0, 0
    for idx, (hs, as_) in enumerate(zip(home_clean, away_clean)):
        if hs != ph or as_ != pa:
            if hs != ph:
                delta, team = hs - ph, "home"
            else:
                delta, team = as_ - pa, "away"
            score_changes.append({"i": idx, "home": hs, "away": as_, "delta": delta, "team": team})
            ph, pa = hs, as_

    def _espn_is_try_of_prev(prev, cur):
        # The real game clock is stopped for the whole try attempt, so a
        # made PAT/two-point try shares its touchdown's clock reading --
        # same signal fox.py's _assign_elapsed_seconds uses to pin a try's
        # elapsed_seconds to its touchdown's. A real safety (also delta 2)
        # never immediately follows that same team's own touchdown, so this
        # doesn't collide with one.
        if cur["team"] != prev["team"] or prev["delta"] != 6 or cur["delta"] not in (1, 2):
            return False
        return elapsed[cur["i"]] is not None and elapsed[cur["i"]] == elapsed[prev["i"]]

    score_changes = _fold_try_into_touchdown(score_changes, _espn_is_try_of_prev)

    elapsed_monotonic = True
    prev_e = None
    for e in elapsed:
        if e is None:
            continue
        if prev_e is not None and e < prev_e:
            elapsed_monotonic = False
            break
        prev_e = e

    return {
        "n": n,
        "i": list(range(n)),
        "home_win_pct": home_win_pct,
        "period": period_raw,
        "period_filled": period_filled,
        "clock_display": clock_display,
        "elapsed": elapsed,
        "home_score": home_score_raw,
        "away_score": away_score_raw,
        "home_score_clean": home_clean,
        "away_score_clean": away_clean,
        "meta": {
            "period_starts": period_starts,
            "score_changes": score_changes,
            "final": {"home": game_row["home_score"], "away": game_row["away_score"]},
            "elapsed_monotonic": elapsed_monotonic,
            "wp_final": home_win_pct[-1] if home_win_pct else None,
        },
    }


def attach_coinflip_wp(wp_payload, wp_rows, conn, game_id, home_team_id):
    """Adds a "coinflip" WP overlay to wp_payload: the same win-probability
    series but with pregame favoritism removed (src/wp_situational.py's
    Model C, run with offense_pregame_wp forced to 0.5 -- see
    scoring.coinflip_home_wp), so the chart can show what the game's WP
    swings would look like between two evenly-matched teams.

    Only available for games with an archived game_raw_json (completed
    games that have gone through pipeline.py's detail fetch/backfill --
    live in-progress games and any not-yet-backfilled legacy game don't have
    it, see project notes) since serve.py never makes outbound ESPN calls
    itself. Sets has_coinflip=False and leaves the series absent when
    unavailable, rather than fetching over the network to fill it in.

    The join is by play_id (win_probability rows <-> situational plays), and
    is gappy by construction -- OT and non-down plays (kickoffs, PATs) have
    no situational reading -- so gaps are forward-filled from the last known
    value for a continuous line, same technique build_wp_payload uses for
    period. A game that reaches OT will show a flat coinflip line through
    the OT rows: the model deliberately never extrapolates into overtime
    (see comeback_erosion's docstring for why), not a bug in this chart.
    """
    raw = db.get_game_raw_json(conn, game_id)
    if not raw:
        wp_payload["has_coinflip"] = False
        return wp_payload

    situational_plays = espn.extract_situational_plays(raw, home_team_id)
    by_play_id = scoring.coinflip_wp_by_play_id(situational_plays)

    series = []
    last = None
    for r in wp_rows:
        val = by_play_id.get(r["play_id"])
        if val is not None:
            last = val
        series.append(last)
    # Back-fill any leading gap (before the first situational reading) with
    # the first known value, so the line doesn't start with a null run.
    first_known = next((v for v in series if v is not None), None)
    series = [v if v is not None else first_known for v in series]

    wp_payload["has_coinflip"] = first_known is not None
    wp_payload["home_win_pct_coinflip"] = series
    return wp_payload


def attach_situational_text(wp_payload, wp_rows, conn, game_id):
    """Adds per-play down/distance and field-position text to wp_payload,
    parallel to wp_rows, for the WP chart tooltip -- the same fields for
    both the regular and coin-flip series, since they share the same play
    list. Straight off espn.extract_play_situations (ESPN's own
    pre-formatted text), so unlike attach_coinflip_wp there's no model
    computation here -- just a join. None entries are plays with no real
    down/distance (kickoffs, timeouts, PATs, quarter breaks) or a game with
    no archived game_raw_json yet."""
    raw = db.get_game_raw_json(conn, game_id)
    situations = espn.extract_play_situations(raw) if raw else {}
    wp_payload["down_distance"] = [situations.get(r["play_id"], {}).get("down_distance") for r in wp_rows]
    wp_payload["field_position"] = [situations.get(r["play_id"], {}).get("field_position") for r in wp_rows]
    return wp_payload


def build_fox_score_payload(conn, game_id, game_row):
    """Fox's own running score, shaped to match build_wp_payload's score
    fields closely enough that the same chart renderer can draw either one.
    x = step ordinal into fox_score_sequence (one entry per actual scoring
    event, already reconciled by fox.build_score_sequence() -- group-level
    backstop scores included -- so this is the validated ladder, not a
    naive re-derivation from raw play rows).

    Fox's home/away assignment is matched independently of ESPN's (see
    fox_match.match_game -- a neutral-site game can come back flipped), so
    every team-labeled value here is remapped into ESPN's home/away frame
    via team_crosswalk before it leaves this function. Nothing downstream
    needs to know Fox ever disagreed about which side was "home"."""
    fox_game = conn.execute(
        "SELECT fox_event_id FROM fox_games WHERE game_id=?", (game_id,)
    ).fetchone()
    if fox_game is None:
        return None
    fox_event_id = fox_game["fox_event_id"]

    fox_event = conn.execute(
        "SELECT home_fox_team_id, away_fox_team_id, home_score, away_score, status_line "
        "FROM fox_events WHERE fox_event_id=?", (fox_event_id,)
    ).fetchone()
    if fox_event is None or fox_event["home_fox_team_id"] is None:
        return None

    espn_for_fox_home = conn.execute(
        "SELECT espn_team_id FROM team_crosswalk WHERE fox_team_id=?",
        (fox_event["home_fox_team_id"],),
    ).fetchone()
    flipped = bool(espn_for_fox_home) and espn_for_fox_home["espn_team_id"] != game_row["home_team_id"]

    def to_espn(side):
        """Map a Fox-frame 'home'/'away' label into ESPN's frame."""
        if not flipped:
            return side
        return "away" if side == "home" else "home"

    steps = conn.execute(
        "SELECT step_number, team, new_value, delta, exact, period_number, evidence, "
        "elapsed_seconds, clock_pinned, try_type, try_result, try_evidence, try_decisive "
        "FROM fox_score_sequence WHERE fox_event_id=? ORDER BY step_number",
        (fox_event_id,),
    ).fetchall()
    n = len(steps) + 1  # a synthetic 0-0 pregame point precedes the first step

    home_score, away_score = [0], [0]
    period_filled, evidence, exact_flags = [0], [None], [True]
    elapsed_seconds, clock_pinned = [0], [False]
    try_info = [None]
    score_changes = []
    h, a = 0, 0
    for s in steps:
        espn_team = to_espn(s["team"])
        if espn_team == "home":
            h = s["new_value"]
        else:
            a = s["new_value"]
        home_score.append(h)
        away_score.append(a)
        period_filled.append(s["period_number"] if s["period_number"] is not None else period_filled[-1])
        evidence.append(s["evidence"])
        exact_flags.append(bool(s["exact"]))
        elapsed_seconds.append(
            s["elapsed_seconds"] if s["elapsed_seconds"] is not None else elapsed_seconds[-1]
        )
        clock_pinned.append(bool(s["clock_pinned"]))
        try_info.append(
            {
                "type": s["try_type"],
                "result": s["try_result"],
                "evidence": s["try_evidence"],
                "decisive": s["try_decisive"],
            }
            if s["try_type"] is not None
            else None
        )
        score_changes.append({
            "i": len(home_score) - 1, "home": h, "away": a,
            "delta": s["delta"], "team": espn_team, "exact": bool(s["exact"]),
            "clock_pinned": bool(s["clock_pinned"]),
        })

    # clock_pinned is already exactly "this step is a made PAT/two-point
    # try pinned to the immediately preceding touchdown's clock" (see
    # fox.py's _assign_elapsed_seconds) -- no need to re-derive it here.
    score_changes = _fold_try_into_touchdown(score_changes, lambda prev, cur: cur["clock_pinned"])
    for sc in score_changes:
        sc.pop("clock_pinned", None)

    period_starts = []
    prev = None
    for idx, p in enumerate(period_filled):
        if p != prev:
            period_starts.append({"i": idx, "period": p, "label": period_label(p)})
            prev = p

    clock_display = [_fox_clock_display(p, e) for p, e in zip(period_filled, elapsed_seconds)]

    return {
        "n": n,
        "i": list(range(n)),
        "home_score_clean": home_score,
        "away_score_clean": away_score,
        "evidence": evidence,
        "exact": exact_flags,
        "elapsed_seconds": elapsed_seconds,
        "clock_display": clock_display,
        "clock_pinned": clock_pinned,
        "try_info": try_info,
        "meta": {
            "period_starts": period_starts,
            "score_changes": score_changes,
            "final": {"home": home_score[-1], "away": away_score[-1]},
            "box_score_final": {"home": game_row["home_score"], "away": game_row["away_score"]},
            "status_line": fox_event["status_line"],
            "flipped": flipped,
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.errorhandler(400)
def handle_400(e):
    return jsonify({"error": "bad_request", "detail": e.description}), 400


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "not_found", "detail": e.description}), 404


@app.errorhandler(503)
def handle_503(e):
    return jsonify({"error": "service_unavailable", "detail": e.description}), 503


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "internal_error", "detail": str(e)}), 500


# ---- health / meta / registry --------------------------------------------

@app.route("/api/healthz")
def api_healthz():
    conn = get_db()
    ro_ok = False
    try:
        conn.execute("CREATE TABLE __ro_probe__ (x)")
    except sqlite3.OperationalError:
        ro_ok = True
    scored = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE watchability_score IS NOT NULL"
    ).fetchone()["n"]
    sqlite_version = conn.execute("SELECT sqlite_version() AS v").fetchone()["v"]
    live_feed = conn.execute(
        "SELECT COUNT(*) AS n, MAX(computed_at) AS last, "
        "(julianday('now') - julianday(MAX(computed_at))) * 86400.0 AS staleness_seconds "
        "FROM live_scores"
    ).fetchone()
    return jsonify({
        "status": "ok",
        "read_only_verified": ro_ok,
        "sqlite_version": sqlite_version,
        "scored": scored,
        "metrics_registered": len(scoring.METRICS),
        "total_weight": TOTAL_WEIGHT,
        "db_path": str(DB_FILE),
        "live_feed": {
            "tracked_games": live_feed["n"],
            "last_cycle_at": live_feed["last"],
            "staleness_seconds": live_feed["staleness_seconds"],
        },
    })


@app.route("/api/meta")
def api_meta():
    conn = get_db()
    season_type_rows = conn.execute(
        "SELECT DISTINCT season_year, season_type FROM games ORDER BY season_year, season_type"
    ).fetchall()
    seasons = sorted({r["season_year"] for r in season_type_rows})
    season_types = {}
    for r in season_type_rows:
        season_types.setdefault(str(r["season_year"]), []).append(r["season_type"])

    week_rows = conn.execute(
        "SELECT DISTINCT season_year, season_type, week FROM games "
        "WHERE week IS NOT NULL ORDER BY season_year, season_type, week"
    ).fetchall()
    weeks = {}
    for r in week_rows:
        key = f"{r['season_year']}:{r['season_type']}"
        weeks.setdefault(key, []).append(r["week"])

    team_rows = conn.execute("""
        SELECT abbr, MAX(name) AS name FROM (
            SELECT home_team_abbr AS abbr, home_team_name AS name FROM games
            UNION ALL
            SELECT away_team_abbr, away_team_name FROM games
        )
        GROUP BY abbr ORDER BY name
    """).fetchall()

    return jsonify({
        "seasons": seasons,
        "season_types": season_types,
        "weeks": weeks,
        "teams": [{"abbr": r["abbr"], "name": r["name"]} for r in team_rows],
    })


@app.route("/api/metrics")
def api_metrics():
    conn = get_db()
    stat_rows = conn.execute("""
        SELECT metric_name, COUNT(*) n, AVG(raw_value) avg_raw, MAX(raw_value) max_raw,
               AVG(norm_value) avg_norm,
               SUM(CASE WHEN norm_value >= 1.0 THEN 1 ELSE 0 END) n_at_cap
        FROM game_metrics GROUP BY metric_name
    """).fetchall()
    stats_by_name = {r["metric_name"]: r for r in stat_rows}
    out = []
    for m in scoring.METRICS:
        name = m["name"]
        s = stats_by_name.get(name)
        n_metric = s["n"] if s else 0
        out.append({
            "name": name,
            "label": METRIC_COPY[name]["label"],
            "description": METRIC_COPY[name]["description"],
            "weight": m["weight"],
            "cap": m["cap"],
            "n": n_metric,
            "avg_raw": s["avg_raw"] if s else None,
            "max_raw": s["max_raw"] if s else None,
            "avg_norm": s["avg_norm"] if s else None,
            "n_at_cap": s["n_at_cap"] if s else 0,
            "pct_at_cap": (s["n_at_cap"] / n_metric * 100) if n_metric else 0.0,
        })
    return jsonify({"metrics": out, "total_weight": TOTAL_WEIGHT, "not_implemented": NOT_IMPLEMENTED})


# ---- slate ---------------------------------------------------------------

@app.route("/api/slate/registry")
def api_slate_registry():
    # short_label is live.py's LIVE_METRIC_LABELS -- the casual "why watch"
    # wording also used to generate a live game's headline (see
    # live.headline_for) -- distinct from `label`/`description` above,
    # which are the more technical copy the rich game-detail breakdown uses.
    so_far = [{
        "name": m["name"], "half": "so_far", "label": LIVE_METRIC_COPY[m["name"]]["label"],
        "short_label": live.LIVE_METRIC_LABELS[m["name"]],
        "description": LIVE_METRIC_COPY[m["name"]]["description"],
        "naLabel": LIVE_METRIC_COPY[m["name"]].get("naLabel"),
        "weight": m["weight"], "cap": m["cap"],
    } for m in live.LIVE_SO_FAR_METRICS]
    from_here = [{
        "name": m["name"], "half": "from_here", "label": LIVE_METRIC_COPY[m["name"]]["label"],
        "short_label": live.LIVE_METRIC_LABELS[m["name"]],
        "description": LIVE_METRIC_COPY[m["name"]]["description"],
        "naLabel": LIVE_METRIC_COPY[m["name"]].get("naLabel"),
        "weight": m["weight"], "cap": m["cap"],
    } for m in live.LIVE_FROM_HERE_METRICS]
    return jsonify({
        "weights": {"so_far": live.LIVE_W_SO_FAR, "from_here": live.LIVE_W_FROM_HERE},
        "so_far_total_weight": sum(m["weight"] for m in live.LIVE_SO_FAR_METRICS),
        "from_here_total_weight": sum(m["weight"] for m in live.LIVE_FROM_HERE_METRICS),
        "so_far": so_far,
        "from_here": from_here,
    })


def _default_slate_date(conn, tz):
    """No explicit ?date= -- pick the nearest day (today or later) that still
    has something to watch, rather than always landing on "today" when
    today's games are already final or there are no games today at all
    (bye week between slates, offseason).
    """
    if conn.execute("SELECT 1 FROM games WHERE status_state = 'in' LIMIT 1").fetchone():
        return datetime.now(tz).date()
    now_utc_str = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%MZ")
    row = conn.execute(
        "SELECT MIN(game_date) AS d FROM games WHERE status_state = 'pre' AND game_date >= ?",
        (now_utc_str,),
    ).fetchone()
    if row and row["d"]:
        next_utc = datetime.strptime(row["d"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=ZoneInfo("UTC"))
        return next_utc.astimezone(tz).date()
    return datetime.now(tz).date()


_CFP_ROUND_ORDER = ["first_round", "quarterfinal", "semifinal", "championship"]


def _cfp_round(event_note):
    """Classify a postseason game's event_note into its CFP round, or None
    for a standard (non-playoff) bowl. Covers both the 4-team era's short
    "CFP ..." notes and the 12-team era's full "College Football Playoff
    ..." notes (see cfp_playoff_format_history memory) -- "National
    Championship" is checked first since it's unambiguous, unlike the other
    three which only ever appear in a playoff note to begin with.
    """
    if not event_note:
        return None
    if "National Championship" in event_note:
        return "championship"
    if "Quarterfinal" in event_note:
        return "quarterfinal"
    if "Semifinal" in event_note:
        return "semifinal"
    if "First Round" in event_note:
        return "first_round"
    return None


def _tuesday_window(local_date, tz):
    """The Tuesday-anchored 7-day window containing local_date. Confirmed
    against 4 seasons of data: a CFB week's earliest game is never a Sun/Mon
    (early-week MACtion games belong with the *upcoming* weekend, not the
    one just past) and its last game is almost always Sat, occasionally
    trailing to Sun/Mon -- so Tue->Mon, not the calendar Mon->Sun week, is
    the real boundary. The weekday math itself lives in src/live.py's
    _week_tuesday, shared with the live poller's own week-anchor cadence
    (_schedule_interval) so the two agree on what "the week" means.
    """
    tuesday = live._week_tuesday(local_date)
    window_start = datetime(tuesday.year, tuesday.month, tuesday.day, tzinfo=tz)
    return window_start, window_start + timedelta(days=7)


def _postseason_week_window(conn, local_date, tz, iso_utc):
    """Postseason equivalent of the regular season's `week`-field grouping
    below. `week` is forced to a flat 1 across the entire ~6-week bowl slate
    (see project notes -- postseason discovery needs `week=1` to work at
    all), so it can't group postseason games the way it groups regular
    season ones; a naive field match would pull in the whole postseason as
    "the week". Bucket by the same Tuesday-anchored calendar window instead.

    This deliberately does NOT split CFP games out of the query into their
    own window: the CFP semifinal and championship already isolate into
    their own single-event bucket purely from the real multi-week gaps
    around them, while First Round/Quarterfinal games land in the same
    bucket as the standard bowls airing the same days -- which is exactly
    how that week is actually watched. `postseason.cfp_rounds`/`has_bowls`
    below exist so the UI can *label* what's in the bucket without the
    query having artificially separated them.
    """
    window_start, window_end = _tuesday_window(local_date, tz)
    bucket_rows = conn.execute(
        "SELECT season_year, event_note FROM games "
        "WHERE season_type = 3 AND game_date >= ? AND game_date < ?",
        (iso_utc(window_start), iso_utc(window_end)),
    ).fetchall()
    rounds_present = sorted(
        {r for r in (_cfp_round(row["event_note"]) for row in bucket_rows) if r},
        key=_CFP_ROUND_ORDER.index,
    )
    has_bowls = any(_cfp_round(row["event_note"]) is None for row in bucket_rows)
    return (
        "g.season_type = 3 AND g.game_date >= ? AND g.game_date < ?",
        [iso_utc(window_start), iso_utc(window_end)],
        {
            "date": str(local_date),
            "window_utc": [iso_utc(window_start), iso_utc(window_end)],
            "weeks": [],
            "postseason": {
                "season_year": bucket_rows[0]["season_year"] if bucket_rows else local_date.year,
                "has_bowls": has_bowls,
                "cfp_rounds": rounds_present,
            },
        },
    )


def _slate_window(conn, scope, date_str, tz_name):
    """Resolve the requested scope into a (where_sql, params, resolved) tuple.
    `where_sql`/`params` select against `g.game_date` (a fixed-format UTC ISO
    string, e.g. "2026-08-29T16:00Z") -- bounds are computed here in Python
    with zoneinfo and formatted the same way, rather than doing timezone math
    in SQL (a fixed-offset SQL date() adjustment would be wrong half the year
    across DST, and would defeat the season/week index besides).

    scope="week" resolves the (season_year, season_type, week) tuple(s)
    present in the day window and matches on that instead -- consistent with
    how the rest of the app defines "a week" (see game.html's postseason
    handling) rather than a raw 7-day span. Falls back to a Tuesday-anchored
    calendar window around the date if the day itself has no games with a
    week number (see _tuesday_window). Postseason dates skip the field-based
    match entirely and go through _postseason_week_window instead, since
    `week` is a flat 1 across the whole bowl slate there.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        abort(400, description=f"unknown tz: {tz_name}")

    if date_str:
        try:
            local_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            abort(400, description="date must be YYYY-MM-DD")
    else:
        local_date = _default_slate_date(conn, tz)

    def _iso_utc(dt):
        return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%MZ")

    day_start = datetime(local_date.year, local_date.month, local_date.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    window_start, window_end = day_start, day_end

    def _week_tuples(start, end):
        rows = conn.execute(
            "SELECT DISTINCT season_year, season_type, week FROM games "
            "WHERE game_date >= ? AND game_date < ? AND week IS NOT NULL",
            (_iso_utc(start), _iso_utc(end)),
        ).fetchall()
        return [{"season_year": r["season_year"], "season_type": r["season_type"], "week": r["week"]} for r in rows]

    if scope == "day":
        # The nav toggle needs to know which (season, season_type, week)
        # a day's games belong to even though the WHERE clause below
        # filters on the raw date window, not on week -- a Friday-night
        # game and its Saturday slate-mates are always the same week in
        # practice, but this is computed either way rather than assumed.
        return (
            "g.game_date >= ? AND g.game_date < ?",
            [_iso_utc(day_start), _iso_utc(day_end)],
            {
                "date": str(local_date),
                "window_utc": [_iso_utc(window_start), _iso_utc(window_end)],
                "weeks": _week_tuples(day_start, day_end),
            },
        )

    # scope == "week"
    tuples = conn.execute(
        "SELECT DISTINCT season_year, season_type, week FROM games "
        "WHERE game_date >= ? AND game_date < ? AND week IS NOT NULL",
        (_iso_utc(day_start), _iso_utc(day_end)),
    ).fetchall()
    if tuples and {t["season_type"] for t in tuples} == {3}:
        return _postseason_week_window(conn, local_date, tz, _iso_utc)
    if not tuples:
        window_start, window_end = _tuesday_window(local_date, tz)
        tuples = conn.execute(
            "SELECT DISTINCT season_year, season_type, week FROM games "
            "WHERE game_date >= ? AND game_date < ? AND week IS NOT NULL",
            (_iso_utc(window_start), _iso_utc(window_end)),
        ).fetchall()
        if tuples and {t["season_type"] for t in tuples} == {3}:
            return _postseason_week_window(conn, local_date, tz, _iso_utc)

    resolved_weeks = [{"season_year": t["season_year"], "season_type": t["season_type"], "week": t["week"]} for t in tuples]

    if not tuples:
        return "0", [], {
            "date": str(local_date), "window_utc": [_iso_utc(window_start), _iso_utc(window_end)], "weeks": [],
        }

    clause = " OR ".join(["(g.season_year = ? AND g.season_type = ? AND g.week = ?)"] * len(tuples))
    params = []
    for t in tuples:
        params.extend([t["season_year"], t["season_type"], t["week"]])
    return clause, params, {
        "date": str(local_date), "window_utc": [_iso_utc(window_start), _iso_utc(window_end)], "weeks": resolved_weeks,
    }


@app.route("/api/slate")
def api_slate():
    conn = get_db()
    scope = request.args.get("scope", "week")
    if scope not in ("day", "week"):
        abort(400, description="scope must be day or week")
    tz_name = request.args.get("tz", "America/Los_Angeles")

    where_scope, scope_params, resolved = _slate_window(conn, scope, request.args.get("date"), tz_name)

    # -- live section: additive "live" key, sorted by live_score desc -------
    # No game is excluded from either this section or the completed one
    # below, at any spoiler level -- the Slate is the one surface that still
    # shows a spoiler-hidden game, ranked by watchability but with every
    # number redacted (shape_game()/build_live_payload() do the actual
    # per-level redaction).
    live_where = f"g.status_state = 'in' AND ({where_scope})"
    live_params = list(scope_params)
    live_rows = conn.execute(f"""
        SELECT g.*, {OT_EXISTS_SQL} AS is_ot,
               ls.live_score, ls.quality_so_far, ls.drama_from_here, ls.progress,
               ls.wp_now, ls.n_wp_rows, ls.so_far_weight, ls.from_here_weight, ls.headline,
               ls.computed_at,
               (julianday('now') - julianday(ls.computed_at)) * 86400.0 AS stale_seconds
        FROM games g JOIN live_scores ls ON ls.game_id = g.game_id
        WHERE {live_where}
        ORDER BY ls.live_score DESC
    """, live_params).fetchall()
    live_game_ids = [r["game_id"] for r in live_rows]
    live_metrics_by_game = fetch_live_metrics_maps(conn, live_game_ids)
    live_out = []
    for row in live_rows:
        g_out = shape_game(row, {}, has_manual_correction=(row["game_id"] in MANUAL_CORRECTION_GAME_IDS))
        g_out["live"] = build_live_payload(
            row, live_metrics_by_game.get(row["game_id"], {"so_far": {}, "from_here": {}}),
            level=g_out["spoiler_level"],
        )
        live_out.append(g_out)

    # -- completed section: existing retrospective shape, sorted by score ---
    ranked_sql, ranked_params = ranked_cte()
    completed_where = f"g.completed = 1 AND ({where_scope})"
    completed_rows = conn.execute(f"""
        WITH {ranked_sql},
        {FOX_FLAG_CTE_SQL}
        SELECT g.*, {OT_EXISTS_SQL} AS is_ot, r.rnk, r.n_scored,
               (fc.game_id IS NOT NULL) AS has_fox_correction
        FROM games g
        LEFT JOIN ranked r ON r.game_id = g.game_id
        LEFT JOIN fox_flag fc ON fc.game_id = g.game_id
        WHERE {completed_where}
        ORDER BY g.watchability_score IS NULL, g.watchability_score DESC
    """, ranked_params + scope_params).fetchall()
    completed_ids = [r["game_id"] for r in completed_rows if r["watchability_score"] is not None]
    metrics_by_game = fetch_metrics_maps(conn, completed_ids)
    completed_out = [
        shape_game(
            row, metrics_by_game.get(row["game_id"], {}), rank=row["rnk"], n_scored=row["n_scored"],
            has_fox_correction=bool(row["has_fox_correction"]),
            has_manual_correction=(row["game_id"] in MANUAL_CORRECTION_GAME_IDS),
        )
        for row in completed_rows
    ]

    # -- upcoming section: plain listing, no score of any kind, by design ---
    # (see plans/... "noon Pacific Saturday" design doc: judging a game that
    # hasn't kicked off is left to the viewer, not this algorithm)
    upcoming_where = f"g.status_state = 'pre' AND ({where_scope})"
    upcoming_rows = conn.execute(f"""
        SELECT g.*, {OT_EXISTS_SQL} AS is_ot FROM games g
        WHERE {upcoming_where}
        ORDER BY g.game_date ASC
    """, scope_params).fetchall()
    upcoming_out = [shape_game(row, {}) for row in upcoming_rows]

    live_feed = conn.execute(
        "SELECT COUNT(*) AS n, MAX(computed_at) AS last, "
        "(julianday('now') - julianday(MAX(computed_at))) * 86400.0 AS staleness_seconds "
        "FROM live_scores"
    ).fetchone()

    return jsonify({
        "as_of": datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": scope,
        "date": resolved["date"],
        "tz": tz_name,
        "window_utc": resolved["window_utc"],
        "weeks": resolved.get("weeks", []),
        "live_feed": {
            "tracked_games": live_feed["n"],
            "last_cycle_at": live_feed["last"],
            "staleness_seconds": live_feed["staleness_seconds"],
        },
        "weights": {"so_far": live.LIVE_W_SO_FAR, "from_here": live.LIVE_W_FROM_HERE},
        "counts": {"live": len(live_out), "completed": len(completed_out), "upcoming": len(upcoming_out)},
        "sections": {"live": live_out, "completed": completed_out, "upcoming": upcoming_out},
    })


@app.route("/api/slate/adjacent")
def api_slate_adjacent():
    """Where the Slate's prev/next buttons should land. A plain +-1-day
    (Day scope) or +-7-day (Week scope) step would walk through months of
    dead air between one season's CFP championship and the next season's
    opening week -- this jumps straight across that gap instead, landing on
    the adjacent season's first/last game date. Anything short of crossing
    a season boundary (a bye week, the regular-season-to-bowl-season
    transition) is a normal step; /api/slate's own resolution (Tuesday-
    anchored windows, CFP/bowl labeling) handles what to actually show once
    it gets there -- this endpoint only decides which date to hand it.
    """
    conn = get_db()
    date_str = request.args.get("date")
    scope = request.args.get("scope", "week")
    direction = request.args.get("dir")
    tz_name = request.args.get("tz", "America/Los_Angeles")
    if scope not in ("day", "week"):
        abort(400, description="scope must be day or week")
    if direction not in ("next", "prev"):
        abort(400, description="dir must be next or prev")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        abort(400, description=f"unknown tz: {tz_name}")
    try:
        local_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        abort(400, description="date must be YYYY-MM-DD")

    def to_local_date(iso_utc):
        return datetime.strptime(iso_utc, "%Y-%m-%dT%H:%MZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()

    rows = conn.execute("""
        SELECT g.season_year, MIN(g.game_date) AS start,
               (SELECT MAX(g2.game_date) FROM games g2
                WHERE g2.season_year = g.season_year AND g2.season_type = 3) AS postseason_end
        FROM games g WHERE g.season_type = 2
        GROUP BY g.season_year ORDER BY g.season_year
    """).fetchall()
    seasons = [{
        "year": r["season_year"],
        "start": to_local_date(r["start"]),
        # A season still in progress (postseason not discovered/played yet)
        # has no postseason_end -- treat its "end" as its own start so nothing
        # downstream mistakes an in-progress season for one it can skip past.
        "end": to_local_date(r["postseason_end"]) if r["postseason_end"] else to_local_date(r["start"]),
    } for r in rows]

    step = timedelta(days=7 if scope == "week" else 1)
    naive = local_date + step if direction == "next" else local_date - step

    current = next((s for s in reversed(seasons) if s["start"] <= local_date), None)
    if current is not None:
        idx = seasons.index(current)
        if direction == "next" and naive > current["end"] and idx + 1 < len(seasons):
            return jsonify({"date": str(seasons[idx + 1]["start"])})
        if direction == "prev" and naive < current["start"] and idx - 1 >= 0:
            return jsonify({"date": str(seasons[idx - 1]["end"])})
    return jsonify({"date": str(naive)})


# ---- games list / detail ---------------------------------------------------

SORT_WHITELIST = {
    "score": "g.watchability_score",
    "date": "g.game_date",
    "week": "g.week",
    "margin": "ABS(COALESCE(g.home_score,0) - COALESCE(g.away_score,0))",
}


def _csv_ints(args, name):
    vals = []
    for v in args.getlist(name):
        vals.extend(v.split(","))
    out = []
    for v in vals:
        v = v.strip()
        if not v:
            continue
        try:
            out.append(int(v))
        except ValueError:
            abort(400, description=f"invalid integer in {name}: {v}")
    return out


@app.route("/api/games")
def api_games():
    conn = get_db()
    args = request.args
    # Split into schedule-shaped filters (season/week/team/state/etc) and
    # outcome-shaped ones (ot/scored/min_score/max_score). The spoiler-
    # excluded count further down must only ever be computed with the
    # schedule-shaped filters applied -- a narrow outcome filter (e.g.
    # ot=1 on one team/week) combined with a count would reconstruct
    # exactly the fact being hidden. See the design plan's "leak channels"
    # section.
    where_schedule = []
    params_schedule = []

    seasons = _csv_ints(args, "season")
    if seasons:
        where_schedule.append(f"g.season_year IN ({','.join('?' * len(seasons))})")
        params_schedule.extend(seasons)

    season_type = args.get("season_type", "all")
    if season_type not in ("all", "2", "3"):
        abort(400, description="season_type must be 2, 3, or all")
    if season_type != "all":
        where_schedule.append("g.season_type = ?")
        params_schedule.append(int(season_type))

    weeks = _csv_ints(args, "week")
    if weeks:
        where_schedule.append(f"g.week IN ({','.join('?' * len(weeks))})")
        params_schedule.extend(weeks)

    teams = [t.strip().upper() for t in args.getlist("team") if t.strip()]
    if teams:
        placeholders = ",".join("?" * len(teams))
        where_schedule.append(
            f"(UPPER(g.home_team_abbr) IN ({placeholders}) OR UPPER(g.away_team_abbr) IN ({placeholders}))"
        )
        params_schedule.extend(teams)
        params_schedule.extend(teams)

    state = args.get("state", "all")
    if state not in ("pre", "in", "post", "all"):
        abort(400, description="state must be pre, in, post, or all")
    if state != "all":
        where_schedule.append("g.status_state = ?")
        params_schedule.append(state)

    ranked = args.get("ranked", "any")
    if ranked not in ("any", "one", "both"):
        abort(400, description="ranked must be any, one, or both")
    if ranked == "one":
        # Exactly one side ranked (XOR) -- IS NULL evaluates to 0/1 in
        # SQLite, so != between the two is a real XOR, not "one or more".
        where_schedule.append("((g.home_rank IS NULL) != (g.away_rank IS NULL))")
    elif ranked == "both":
        where_schedule.append("(g.home_rank IS NOT NULL AND g.away_rank IS NOT NULL)")

    for flag_name, col in (("conference", "g.conference_game"), ("neutral", "g.neutral_site")):
        v = args.get(flag_name, "all")
        if v not in ("0", "1", "all"):
            abort(400, description=f"{flag_name} must be 0, 1, or all")
        if v != "all":
            where_schedule.append(f"{col} = ?")
            params_schedule.append(int(v))

    q = args.get("q", "").strip()
    if q:
        # Split on whitespace so each term is AND'd in (OR'd across fields
        # per term) -- "PSU OSU" then requires both acronyms present, one
        # per team, rather than matching either team alone. Same fix as
        # api_spoilers_search().
        for term in q.split():
            like = f"%{term}%"
            where_schedule.append(
                "(g.home_team_abbr LIKE ? OR g.away_team_abbr LIKE ? OR "
                "g.home_team_name LIKE ? OR g.away_team_name LIKE ? OR g.venue_name LIKE ?)"
            )
            params_schedule.extend([like, like, like, like, like])

    where_outcome = []
    params_outcome = []

    ot = args.get("ot", "all")
    if ot not in ("0", "1", "all"):
        abort(400, description="ot must be 0, 1, or all")
    if ot == "1":
        where_outcome.append(OT_EXISTS_SQL)
    elif ot == "0":
        where_outcome.append(f"NOT {OT_EXISTS_SQL}")

    scored = args.get("scored", "1")
    if scored not in ("0", "1", "all"):
        abort(400, description="scored must be 0, 1, or all")
    if scored == "1":
        where_outcome.append("g.watchability_score IS NOT NULL")
    elif scored == "0":
        where_outcome.append("g.watchability_score IS NULL")

    min_score, max_score = args.get("min_score"), args.get("max_score")
    if min_score is not None:
        try:
            where_outcome.append("g.watchability_score >= ?")
            params_outcome.append(float(min_score))
        except ValueError:
            abort(400, description="min_score must be a number")
    if max_score is not None:
        try:
            where_outcome.append("g.watchability_score <= ?")
            params_outcome.append(float(max_score))
        except ValueError:
            abort(400, description="max_score must be a number")

    sort = args.get("sort", "score")
    dir_ = args.get("dir", "desc")
    if dir_ not in ("asc", "desc"):
        abort(400, description="dir must be asc or desc")

    sort_metric_name = None
    if sort.startswith("metric:"):
        sort_metric_name = sort[len("metric:"):]
        if sort_metric_name not in scoring.METRICS_BY_NAME:
            abort(400, description=f"unknown metric: {sort_metric_name}")
        sort_expr = "sort_metric_val"
    elif sort in SORT_WHITELIST:
        sort_expr = SORT_WHITELIST[sort]
    else:
        abort(400, description=f"invalid sort: {sort}")

    try:
        limit = int(args.get("limit", 50))
        offset = int(args.get("offset", 0))
    except ValueError:
        abort(400, description="limit/offset must be integers")
    if not (1 <= limit <= 500):
        abort(400, description="limit must be between 1 and 500")
    if offset < 0:
        abort(400, description="offset must be >= 0")

    # Games below LEVEL_SCORE are excluded from this endpoint entirely --
    # under every filter and every sort, not just redacted -- which is what
    # keeps sort=margin, ot=0/1, and sort=metric:* leak-free: without this,
    # they could surface a hidden game's outcome shape by its absence, even
    # fully redacted.
    #
    # A LEVEL_SCORE game reveals watchability_score but nothing about how
    # the game ended, so it's safe to include under the ordinary
    # score/date/week sorts and the min_score/max_score/scored filters --
    # but NOT under sort=margin, sort=metric:*, or an ot=0/1 filter, each of
    # which would leak exactly what LEVEL_SCORE is supposed to hide through
    # the query's own ordering or membership rather than through any field
    # on the game itself (see the two-tier-spoiler plan's leak-channels
    # section). Raise the inclusion floor to LEVEL_FULL whenever one of
    # those outcome-shaped surfaces is in play.
    outcome_ordered = (ot != "all") or (sort == "margin") or (sort_metric_name is not None)
    min_level = spoilers.LEVEL_FULL if outcome_ordered else spoilers.LEVEL_SCORE
    policy = spoiler_ctx()
    visible_clause, visible_params = spoilers.visible_sql(policy, alias="g", min_level=min_level)

    where = where_schedule + where_outcome + [visible_clause]
    params = params_schedule + params_outcome + visible_params
    where_sql = " AND ".join(where) if where else "1=1"

    metric_select, metric_params = "", []
    if sort_metric_name:
        metric_select = (
            ", (SELECT raw_value FROM game_metrics gm WHERE gm.game_id = g.game_id "
            "AND gm.metric_name = ?) AS sort_metric_val"
        )
        metric_params = [sort_metric_name]

    total = conn.execute(f"SELECT COUNT(*) AS n FROM games g WHERE {where_sql}", params).fetchone()["n"]

    ranked_sql, ranked_params = ranked_cte()
    sql = f"""
        WITH {ranked_sql},
        {FOX_FLAG_CTE_SQL}
        SELECT g.*, r.rnk, r.n_scored, (fc.game_id IS NOT NULL) AS has_fox_correction,
               {OT_EXISTS_SQL} AS is_ot
               {metric_select}
        FROM games g
        LEFT JOIN ranked r ON r.game_id = g.game_id
        LEFT JOIN fox_flag fc ON fc.game_id = g.game_id
        WHERE {where_sql}
        ORDER BY {sort_expr} IS NULL, {sort_expr} {dir_.upper()}
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, ranked_params + metric_params + params + [limit, offset]).fetchall()

    scored_game_ids = [r["game_id"] for r in rows if r["watchability_score"] is not None]
    metrics_by_game = fetch_metrics_maps(conn, scored_game_ids)

    games_out = []
    for row in rows:
        gid = row["game_id"]
        m_map = metrics_by_game.get(gid, {})
        games_out.append(shape_game(
            row, m_map, rank=row["rnk"], n_scored=row["n_scored"],
            has_fox_correction=bool(row["has_fox_correction"]),
            has_manual_correction=(gid in MANUAL_CORRECTION_GAME_IDS),
        ))

    score_rows = conn.execute(
        f"SELECT watchability_score AS s FROM games g WHERE {where_sql} AND g.watchability_score IS NOT NULL",
        params,
    ).fetchall()
    scores = sorted(r["s"] for r in score_rows)
    if scores:
        n = len(scores)
        median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2
        stats = {"n": n, "min": scores[0], "median": median, "max": scores[-1], "histogram": histogram(scores)}
    else:
        stats = {"n": 0, "min": None, "median": None, "max": None, "histogram": histogram([])}

    n_scored_corpus = conn.execute(
        f"SELECT COUNT(*) AS n FROM games g WHERE watchability_score IS NOT NULL AND {visible_clause}",
        visible_params,
    ).fetchone()["n"]

    # Spoiler-excluded count: schedule-shaped filters only (never ot/
    # min_score/max_score/scored, split out above) -- otherwise the count
    # itself would reconstruct an outcome-shaped fact about the games it's
    # counting. Uses the same min_level as the main query's visible_clause
    # (computed above), so this always reports exactly what was dropped
    # from *this* response -- a level-1 game only counts as excluded when
    # an outcome-ordered surface forced min_level up to LEVEL_FULL.
    where_sched_sql = " AND ".join(where_schedule) if where_schedule else "1=1"
    excluded_count = conn.execute(
        f"SELECT COUNT(*) AS n FROM games g WHERE {where_sched_sql} AND NOT ({visible_clause})",
        params_schedule + visible_params,
    ).fetchone()["n"]

    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "dir": dir_,
        "n_scored_corpus": n_scored_corpus,
        "filtered_score_stats": stats,
        "spoiler_excluded": {"count": excluded_count},
        "games": games_out,
    })


@app.route("/api/games/<game_id>")
def api_game_detail(game_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if row is None:
        abort(404, description="no such game")

    # This route stays reachable at every spoiler level -- by click-through
    # from the Slate, or by pasting the game_id into the URL -- so every
    # payload shape_game() doesn't itself own (ot_info, score_integrity,
    # corrections, rank_context, neighbors, wp, fox_score, live/
    # live_history) needs its own guard here, keyed off `level` rather than
    # a single hidden bool. Below the level a payload requires, its
    # underlying queries are skipped outright rather than computed and then
    # discarded -- there's nothing to compute a spoiler-safe value from.
    # rank_context/neighbors/live are the LEVEL_SCORE exception: revealing
    # those (score-free) is the entire point of that tier.
    policy = spoiler_ctx()
    level = spoilers.level_of_row(row, policy)
    show_score = level >= spoilers.LEVEL_SCORE
    show_all = level >= spoilers.LEVEL_FULL

    if show_all:
        is_ot = bool(conn.execute(
            "SELECT EXISTS(SELECT 1 FROM win_probability WHERE game_id=? AND period_number>4) AS x",
            (game_id,),
        ).fetchone()["x"])
        max_period = conn.execute(
            "SELECT MAX(period_number) AS mx FROM win_probability WHERE game_id=?", (game_id,)
        ).fetchone()["mx"]
        ot_info = {
            "is_ot": is_ot,
            "max_period_in_data": max_period,
            "note": (
                "Period count is derived from ESPN play data and can be truncated "
                "on long overtime games." if is_ot else None
            ),
        }
    else:
        is_ot = None
        ot_info = {"is_ot": None, "max_period_in_data": None, "note": None}

    scored = row["watchability_score"] is not None
    metrics_map, score_integrity, rank_context, wp_payload = {}, None, None, None
    neighbors = {"prev_by_rank": None, "next_by_rank": None}

    if scored and show_all:
        metric_rows = conn.execute(
            "SELECT metric_name, raw_value, norm_value FROM game_metrics WHERE game_id=?",
            (game_id,),
        ).fetchall()
        metrics_map = build_metrics_map(metric_rows)

        weighted_sum, applicable_weight, excluded = 0.0, 0.0, []
        for m in scoring.METRICS:
            v = metrics_map[m["name"]]
            if v is None:
                excluded.append(m["name"])
                continue
            weighted_sum += v["weighted"]
            applicable_weight += m["weight"]
        uw_bonus = scoring.uw_loss_bonus(
            row["home_team_id"], row["away_team_id"], row["home_score"], row["away_score"]
        )
        composite_recomputed = (
            weighted_sum / applicable_weight + uw_bonus if applicable_weight else None
        )
        composite_stored = row["watchability_score"]
        delta = (composite_recomputed - composite_stored) if composite_recomputed is not None else None
        score_integrity = {
            "composite_stored": composite_stored,
            "composite_recomputed": composite_recomputed,
            "delta": delta,
            "matches": (abs(delta) < 1e-6) if delta is not None else False,
            "weighted_sum": weighted_sum,
            "applicable_weight": applicable_weight,
            "excluded_metrics": excluded,
            "uw_loss_bonus": uw_bonus,
        }

        wp_rows = conn.execute(
            "SELECT play_id, sequence_number, home_win_pct, clock_seconds_elapsed, period_number, "
            "clock_display, home_score, away_score, play_sequence FROM win_probability "
            "WHERE game_id=? AND period_number IS NOT NULL ORDER BY play_sequence, id",
            (game_id,),
        ).fetchall()
        wp_payload = build_wp_payload(wp_rows, row)
        attach_coinflip_wp(wp_payload, wp_rows, conn, game_id, row["home_team_id"])
        attach_situational_text(wp_payload, wp_rows, conn, game_id)
    elif show_all and row["status_state"] == "in":
        # A live-tracked game has real (partial) win_probability rows too --
        # written incrementally by src/live.py's poller -- so the chart can
        # render before the game is scored. build_wp_payload's "final" meta
        # key just reflects whatever score the game currently carries here.
        wp_rows = conn.execute(
            "SELECT play_id, sequence_number, home_win_pct, clock_seconds_elapsed, period_number, "
            "clock_display, home_score, away_score, play_sequence FROM win_probability "
            "WHERE game_id=? AND period_number IS NOT NULL ORDER BY play_sequence, id",
            (game_id,),
        ).fetchall()
        if wp_rows:
            wp_payload = build_wp_payload(wp_rows, row)
            attach_coinflip_wp(wp_payload, wp_rows, conn, game_id, row["home_team_id"])
            attach_situational_text(wp_payload, wp_rows, conn, game_id)

    if scored and show_score:
        # Rank/percentile here (and the neighbor lookup below) are scoped
        # to the same >=LEVEL_SCORE corpus as /api/games and /api/top --
        # otherwise a game's "rank X of N" would silently include hidden
        # games in N, and would disagree with what the Games tab shows for
        # the same game. Split out of the `show_all` block above: a
        # LEVEL_SCORE game gets rank/percentile without metrics/
        # score_integrity, which is the entire point of that tier.
        visible_clause, visible_params = spoilers.visible_sql(policy, alias="g")
        rank_rows = conn.execute(f"""
            SELECT game_id,
                   RANK() OVER (ORDER BY watchability_score DESC) AS rnk_g,
                   COUNT(*) OVER () AS n_g,
                   RANK() OVER (PARTITION BY season_year ORDER BY watchability_score DESC) AS rnk_s,
                   COUNT(*) OVER (PARTITION BY season_year) AS n_s,
                   RANK() OVER (PARTITION BY season_year, season_type, week ORDER BY watchability_score DESC) AS rnk_w,
                   COUNT(*) OVER (PARTITION BY season_year, season_type, week) AS n_w
            FROM games g WHERE watchability_score IS NOT NULL AND {visible_clause}
        """, visible_params).fetchall()
        rc = next((r for r in rank_rows if r["game_id"] == game_id), None)
        if rc:
            rank_context = {
                "global": {"rank": rc["rnk_g"], "percentile": percentile_from_rank(rc["rnk_g"], rc["n_g"]), "n": rc["n_g"]},
                "season": {"rank": rc["rnk_s"], "percentile": percentile_from_rank(rc["rnk_s"], rc["n_s"]), "n": rc["n_s"], "label": str(row["season_year"])},
                "week": {
                    "rank": rc["rnk_w"], "percentile": percentile_from_rank(rc["rnk_w"], rc["n_w"]), "n": rc["n_w"],
                    # Every postseason game is stored as week=1 (ESPN has no
                    # real week numbering once the regular season ends), so
                    # this partition is actually "all of that year's postseason"
                    # -- label it that way instead of the misleading "week 1".
                    "label": f"{row['season_year']} postseason" if row["season_type"] == 3 else f"{row['season_year']} week {row['week']}",
                },
            }
            gr = rc["rnk_g"]
            # ranked_cte() is scoped to the same >=LEVEL_SCORE set, so a
            # LEVEL_HIDDEN game can never come back as a neighbor here --
            # that part is safe "for free". A LEVEL_SCORE neighbor CAN come
            # back though (that's the point of the tier), so its own level
            # is selected here and the label built without its score when
            # that neighbor isn't itself LEVEL_FULL -- see the two-tier-
            # spoiler plan's leak-channels section. This is about the
            # NEIGHBOR's level, not the requested game's.
            ranked_sql, ranked_params = ranked_cte(alias="g")
            level_expr, level_expr_params = spoilers.level_sql(policy, alias="g")
            nb_rows = conn.execute(f"""
                WITH {ranked_sql}
                SELECT g.game_id, g.home_team_abbr, g.away_team_abbr, g.home_score, g.away_score,
                       g.watchability_score, r.rnk, ({level_expr}) AS lvl
                FROM games g JOIN ranked r ON r.game_id = g.game_id
                WHERE r.rnk IN (?, ?)
            """, ranked_params + level_expr_params + [gr - 1, gr + 1]).fetchall()
            for nb in nb_rows:
                nb_full = nb["lvl"] >= spoilers.LEVEL_FULL
                label = (
                    f"{nb['away_team_abbr']} {nb['away_score']} at {nb['home_team_abbr']} {nb['home_score']}"
                    if nb_full else
                    f"{nb['away_team_abbr']} at {nb['home_team_abbr']}"
                )
                entry = {"game_id": nb["game_id"], "rank": nb["rnk"], "label": label, "watchability_score": nb["watchability_score"]}
                if nb["rnk"] == gr - 1:
                    neighbors["prev_by_rank"] = entry
                elif nb["rnk"] == gr + 1:
                    neighbors["next_by_rank"] = entry

    if show_all:
        manual = [c for c in corrections_module.CORRECTIONS if c["game_id"] == game_id]
        fox_rows = conn.execute(
            "SELECT tier, metric_name, espn_value, fox_value, fox_event_id, notes, reconciled_at "
            "FROM fox_score_corrections WHERE game_id=?",
            (game_id,),
        ).fetchall()
        fox_diff = [dict(r) for r in fox_rows if r["tier"] == "diff"]
        unusable_notes = [dict(r) for r in fox_rows if r["tier"] == "unusable"]
        fox_event_row = conn.execute("SELECT fox_event_id FROM fox_games WHERE game_id=?", (game_id,)).fetchone()
        corrections_payload = {
            "manual": manual,
            "fox": fox_diff,
            "unusable": len(unusable_notes) > 0,
            "unusable_notes": unusable_notes,
            "fox_event_id": fox_event_row["fox_event_id"] if fox_event_row else None,
        }
        fox_score_payload = build_fox_score_payload(conn, game_id, row)
    else:
        # The reason strings in src/corrections.py literally describe how
        # games ended ("the decisive final play"), and Fox reconciliation
        # notes can too -- so this isn't just numbers to null, the queries
        # themselves are skipped, below LEVEL_FULL.
        manual, fox_diff, unusable_notes = [], [], []
        corrections_payload = {"manual": [], "fox": [], "unusable": False, "unusable_notes": [], "fox_event_id": None}
        fox_score_payload = None

    # shape_game() does `row["is_ot"]` / `row.keys()` -- sqlite3.Row supports
    # both, but we need to inject the separately-queried is_ot flag onto it,
    # so build a plain dict (which also supports both) instead.
    row_with_ot = dict(row)
    row_with_ot["is_ot"] = is_ot
    game_shaped = shape_game(
        row_with_ot,
        metrics_map,
        rank=(rank_context["global"]["rank"] if rank_context else None),
        n_scored=(rank_context["global"]["n"] if rank_context else None),
        has_fox_correction=bool(fox_diff),
        has_manual_correction=bool(manual),
    )

    registry = [{
        "name": m["name"], "label": METRIC_COPY[m["name"]]["label"],
        "description": METRIC_COPY[m["name"]]["description"],
        "weight": m["weight"], "cap": m["cap"],
    } for m in scoring.METRICS]

    # Additive only -- present only for a game currently tracked live
    # (row["status_state"] == 'in' and a live_scores row exists). A
    # completed or not-yet-started game gets no "live"/"live_history" key
    # at all, rather than nulls -- upcoming games are deliberately unscored
    # (see /api/slate), so there's nothing live to report for them either.
    #
    # Gated on `show_score`, not on live_row presence: db.clear_live_rows()
    # deletes live_scores/live_metrics on completion, but there are two
    # windows where a *completed*, still-below-LEVEL_FULL game could still
    # have a live row -- between the game ending and the poller's next
    # cycle, and any time the poller isn't running -- and status_detail in
    # particular can read literally "Final/OT" for an overtime game.
    # build_live_payload/redact_live_history apply the level-appropriate
    # redaction from here.
    live_payload, live_history = None, None
    if show_score:
        live_row = conn.execute("""
            SELECT ls.*, g.status_period, g.status_clock_display, g.status_detail,
                   (julianday('now') - julianday(ls.computed_at)) * 86400.0 AS stale_seconds
            FROM live_scores ls JOIN games g ON g.game_id = ls.game_id
            WHERE ls.game_id = ?
        """, (game_id,)).fetchone()
        if live_row is not None:
            live_metrics = fetch_live_metrics_maps(conn, [game_id]).get(game_id, {"so_far": {}, "from_here": {}})
            live_payload = build_live_payload(live_row, live_metrics, level=level)
            history_rows = conn.execute(
                "SELECT computed_at, progress, live_score, quality_so_far, drama_from_here "
                "FROM live_score_history WHERE game_id = ? ORDER BY computed_at",
                (game_id,),
            ).fetchall()
            live_history = spoilers.redact_live_history([dict(r) for r in history_rows], level)

    return jsonify({
        "game": game_shaped,
        "rank_context": rank_context,
        "score_integrity": score_integrity,
        "registry": registry,
        "ot": ot_info,
        "corrections": corrections_payload,
        "wp": wp_payload,
        "fox_score": fox_score_payload,
        "neighbors": neighbors,
        "live": live_payload,
        "live_history": live_history,
    })


# ---- top / leaderboards -----------------------------------------------------

@app.route("/api/top")
def api_top():
    conn = get_db()
    by = request.args.get("by", "composite")
    season = request.args.get("season")
    season_type = request.args.get("season_type", "all")
    try:
        limit = int(request.args.get("limit", 25))
    except ValueError:
        abort(400, description="limit must be an integer")
    # 1000 (not the old 100) -- Top Games' progressive disclosure starts at
    # 50 and grows by 25 per "Show more" click, and the unfiltered corpus
    # across all seasons is already well past 100 scored games.
    if not (1 <= limit <= 1000):
        abort(400, description="limit must be between 1 and 1000")

    where = ["g.watchability_score IS NOT NULL"]
    params = []
    if season:
        try:
            where.append("g.season_year = ?")
            params.append(int(season))
        except ValueError:
            abort(400, description="season must be an integer")
    if season_type != "all":
        if season_type not in ("2", "3"):
            abort(400, description="season_type must be 2, 3, or all")
        where.append("g.season_type = ?")
        params.append(int(season_type))

    # Unlike /api/games, every filter on this endpoint (season/season_type)
    # is schedule-shaped, so an excluded count computed against them
    # carries none of the outcome-leak risk that /api/games' ot/min_score
    # filters would.
    where_schedule_sql = " AND ".join(where)
    params_schedule = list(params)
    policy = spoiler_ctx()

    if by == "composite":
        # Included at LEVEL_SCORE -- this is the browsable, spoiler-safe
        # leaderboard the two-tier system exists for (see the two-tier-
        # spoiler plan): a LEVEL_SCORE game's watchability_score is
        # revealed, so ranking by it and displaying it doesn't leak
        # anything beyond what shape_game() already reveals for that game.
        min_level = spoilers.LEVEL_SCORE
    else:
        # A ?by=<metric> leaderboard orders by, and displays, a single raw
        # per-metric value -- that's component detail LEVEL_SCORE is
        # supposed to hide, so this branch requires LEVEL_FULL (same
        # reasoning as /api/games' sort=metric:*, see the leak-channels
        # section).
        min_level = spoilers.LEVEL_FULL

    visible_clause, visible_params = spoilers.visible_sql(policy, alias="g", min_level=min_level)
    excluded_count = conn.execute(
        f"SELECT COUNT(*) AS n FROM games g WHERE {where_schedule_sql} AND NOT ({visible_clause})",
        params_schedule + visible_params,
    ).fetchone()["n"]
    where.append(visible_clause)
    params.extend(visible_params)
    where_sql = " AND ".join(where)

    if by == "composite":
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM games g WHERE {where_sql}", params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT g.*, {OT_EXISTS_SQL} AS is_ot FROM games g WHERE {where_sql} "
            f"ORDER BY g.watchability_score DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        game_ids = [r["game_id"] for r in rows]
        metrics_by_game = fetch_metrics_maps(conn, game_ids)
        metric_dists = fetch_metric_distributions(conn)
        fox_diff_ids = fetch_fox_diff_game_ids(conn, game_ids)
        results = []
        for i, row in enumerate(rows, start=1):
            m_map = metrics_by_game.get(row["game_id"], {})
            # top_contributors is built from m_map before shape_game()'s
            # redaction runs, so a LEVEL_SCORE game's per-metric components
            # (already hidden by redact_game_score_only) must be suppressed
            # here too -- otherwise "Driven by X + Y" would leak exactly the
            # component detail that tier is supposed to hide. See the
            # leak-channels section.
            if spoilers.level_of_row(row, policy) >= spoilers.LEVEL_FULL:
                qualifying = (
                    (n, v["weighted"]) for n, v in m_map.items() if v is not None
                    and (metric_percentile(metric_dists.get(n, []), v["norm"]) or 0) >= TOP_CONTRIBUTOR_PERCENTILE_MIN
                )
                top2 = sorted(qualifying, key=lambda t: t[1], reverse=True)[:2]
            else:
                top2 = []
            results.append({
                "rank": i,
                "game": shape_game(
                    row, m_map,
                    has_fox_correction=(row["game_id"] in fox_diff_ids),
                    has_manual_correction=(row["game_id"] in MANUAL_CORRECTION_GAME_IDS),
                ),
                "top_contributors": [{"name": n, "label": METRIC_COPY[n]["label"], "weighted": w} for n, w in top2],
            })
        return jsonify({
            "by": "composite", "results": results, "cap_warning": None, "total": total,
            "spoiler_excluded": {"count": excluded_count},
        })

    if by not in scoring.METRICS_BY_NAME:
        abort(400, description=f"unknown metric: {by}")
    m = scoring.METRICS_BY_NAME[by]
    rows = conn.execute(f"""
        SELECT g.*, gm.raw_value AS metric_raw, gm.norm_value AS metric_norm, {OT_EXISTS_SQL} AS is_ot
        FROM games g JOIN game_metrics gm ON gm.game_id = g.game_id AND gm.metric_name = ?
        WHERE {where_sql}
        ORDER BY gm.raw_value DESC LIMIT ?
    """, [by] + params + [limit]).fetchall()
    n_at_cap = conn.execute(f"""
        SELECT COUNT(*) AS n FROM games g
        JOIN game_metrics gm ON gm.game_id = g.game_id AND gm.metric_name = ?
        WHERE {where_sql} AND gm.norm_value >= 1.0
    """, [by] + params).fetchone()["n"]
    cap_warning = None
    if m["cap"] is not None and n_at_cap > 0:
        cap_warning = (
            f"{n_at_cap} games are at the {m['cap']} cap for {METRIC_COPY[by]['label']}; "
            f"this ranking separates them by raw value, but the composite score does not."
        )
    # Every row here is scored by definition (it has a game_metrics row for
    # `by`), so it needs its real metrics map -- {} is reserved for "never
    # scored" and must never appear on a scored game (it would also make
    # applicable_weight_of({}) read as 0.0, a false "nothing applies" signal).
    game_ids = [row["game_id"] for row in rows]
    metrics_by_game = fetch_metrics_maps(conn, game_ids)
    fox_diff_ids = fetch_fox_diff_game_ids(conn, game_ids)
    results = []
    for i, row in enumerate(rows, start=1):
        m_map = metrics_by_game.get(row["game_id"], {})
        results.append({
            "rank": i,
            "game": shape_game(
                row, m_map,
                has_fox_correction=(row["game_id"] in fox_diff_ids),
                has_manual_correction=(row["game_id"] in MANUAL_CORRECTION_GAME_IDS),
            ),
            "value": row["metric_raw"],
            "normalized": row["metric_norm"],
            "at_cap": row["metric_norm"] >= 1.0,
        })
    return jsonify({
        "by": by, "results": results, "cap_warning": cap_warning,
        "spoiler_excluded": {"count": excluded_count},
    })


@app.route("/api/top/teams")
def api_top_teams():
    conn = get_db()
    try:
        min_games = int(request.args.get("min_games", 8))
        limit = int(request.args.get("limit", 25))
    except ValueError:
        abort(400, description="min_games/limit must be integers")

    season = request.args.get("season")
    season_type = request.args.get("season_type", "all")
    where = ["watchability_score IS NOT NULL"]
    params = []
    if season:
        try:
            where.append("season_year = ?")
            params.append(int(season))
        except ValueError:
            abort(400, description="season must be an integer")
    if season_type != "all":
        if season_type not in ("2", "3"):
            abort(400, description="season_type must be 2, 3, or all")
        where.append("season_type = ?")
        params.append(int(season_type))

    # Excluded from the aggregate entirely -- otherwise avg_watchability
    # would be pulled by hidden games' scores, and best_game could point
    # at one.
    policy = spoiler_ctx()
    visible_clause, visible_params = spoilers.visible_sql(policy, alias="games")
    where.append(visible_clause)
    params.extend(visible_params)
    where_sql = " AND ".join(where)

    appearances_sql = f"""
        SELECT home_team_abbr AS abbr, home_team_name AS name, game_id, watchability_score
        FROM games WHERE {where_sql}
        UNION ALL
        SELECT away_team_abbr, away_team_name, game_id, watchability_score
        FROM games WHERE {where_sql}
    """

    # Leaderboard is "average of a team's top 5 games", not a mean across all
    # appearances -- otherwise it mostly measures schedule volume/consistency
    # rather than how good the team's best watchability moments were.
    ranked_sql = f"""
        SELECT abbr, name, game_id, watchability_score,
               ROW_NUMBER() OVER (PARTITION BY abbr ORDER BY watchability_score DESC) AS rn,
               COUNT(*) OVER (PARTITION BY abbr) AS n
        FROM ({appearances_sql})
    """

    agg_rows = conn.execute(f"""
        SELECT abbr, MAX(name) AS name, MAX(n) AS n,
               AVG(CASE WHEN rn <= 5 THEN watchability_score END) AS avg_score
        FROM ({ranked_sql})
        GROUP BY abbr HAVING MAX(n) >= ?
        ORDER BY avg_score DESC LIMIT ?
    """, params + params + [min_games, limit]).fetchall()

    best_rows = conn.execute(f"""
        SELECT abbr, game_id, watchability_score, rn
        FROM ({ranked_sql})
    """, params + params).fetchall()
    best_by_abbr = {}
    for r in best_rows:
        if r["rn"] == 1 and r["abbr"] not in best_by_abbr:
            best_by_abbr[r["abbr"]] = {"game_id": r["game_id"], "watchability_score": r["watchability_score"]}

    results = [{
        "rank": i,
        "abbr": r["abbr"],
        "name": r["name"],
        "games_played": r["n"],
        "avg_watchability": r["avg_score"],
        "best_game": best_by_abbr.get(r["abbr"]),
    } for i, r in enumerate(agg_rows, start=1)]

    return jsonify({"min_games": min_games, "results": results})


@app.route("/api/top/weekly-peaks")
def api_weekly_peaks():
    conn = get_db()
    season = request.args.get("season")
    if not season:
        abort(400, description="season is required")
    try:
        season = int(season)
    except ValueError:
        abort(400, description="season must be an integer")
    season_type = request.args.get("season_type", "2")
    if season_type not in ("2", "3"):
        abort(400, description="season_type must be 2 or 3")
    season_type = int(season_type)

    # Excluded from both the peak computation and the outer match -- a
    # game below LEVEL_SCORE must never become "the" peak for its week.
    # A LEVEL_SCORE peak IS included (its watchability_score is exactly
    # what it reveals), but the `matchup` string below must build a
    # score-free label for it -- see the leak-channels section.
    policy = spoiler_ctx()
    visible_g, visible_params_g = spoilers.visible_sql(policy, alias="g")
    visible_inner, visible_params_inner = spoilers.visible_sql(policy, alias="games")
    level_expr, level_expr_params = spoilers.level_sql(policy, alias="g")

    rows = conn.execute(f"""
        SELECT g.week, g.game_id, g.watchability_score, g.home_team_abbr, g.away_team_abbr,
               g.home_score, g.away_score, ({level_expr}) AS lvl
        FROM games g
        JOIN (
            SELECT week, MAX(watchability_score) AS peak FROM games
            WHERE season_year=? AND season_type=? AND watchability_score IS NOT NULL AND {visible_inner}
            GROUP BY week
        ) m ON g.week = m.week AND g.watchability_score = m.peak
        WHERE g.season_year=? AND g.season_type=? AND {visible_g}
        ORDER BY g.week
    """, level_expr_params + [season, season_type] + visible_params_inner + [season, season_type] + visible_params_g).fetchall()

    seen, results = set(), []
    for r in rows:
        if r["week"] in seen:
            continue
        seen.add(r["week"])
        matchup = (
            f"{r['away_team_abbr']} {r['away_score']} at {r['home_team_abbr']} {r['home_score']}"
            if r["lvl"] >= spoilers.LEVEL_FULL
            else f"{r['away_team_abbr']} at {r['home_team_abbr']}"
        )
        results.append({
            "week": r["week"],
            "game_id": r["game_id"],
            "watchability_score": r["watchability_score"],
            "matchup": matchup,
        })
    return jsonify({"season": season, "season_type": season_type, "weeks": results})


# ---- analytics ---------------------------------------------------------------

@app.route("/api/analytics")
def api_analytics():
    conn = get_db()
    season = request.args.get("season")
    season_type = request.args.get("season_type", "all")

    where = ["g.watchability_score IS NOT NULL"]
    params = []
    if season:
        try:
            where.append("g.season_year = ?")
            params.append(int(season))
        except ValueError:
            abort(400, description="season must be an integer")
    if season_type != "all":
        if season_type not in ("2", "3"):
            abort(400, description="season_type must be 2, 3, or all")
        where.append("g.season_type = ?")
        params.append(int(season_type))
    where_sql = " AND ".join(where)

    score_rows = conn.execute(f"SELECT watchability_score AS s FROM games g WHERE {where_sql}", params).fetchall()
    scores = sorted(r["s"] for r in score_rows)
    n = len(scores)
    if n:
        mean_score = sum(scores) / n
        median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2
        p10 = scores[int(n * 0.10)]
        p90 = scores[min(int(n * 0.90), n - 1)]
        dist = {"n": n, "min": scores[0], "median": median, "mean": mean_score, "max": scores[-1],
                "p10": p10, "p90": p90, "histogram": histogram(scores)}
    else:
        mean_score = None
        dist = {"n": 0, "min": None, "median": None, "mean": None, "max": None, "p10": None, "p90": None,
                "histogram": histogram([])}

    stat_rows = conn.execute(f"""
        SELECT gm.metric_name, COUNT(*) n, AVG(gm.raw_value) avg_raw, MAX(gm.raw_value) max_raw,
               AVG(gm.norm_value) avg_norm,
               SUM(CASE WHEN gm.norm_value >= 1.0 THEN 1 ELSE 0 END) n_at_cap
        FROM game_metrics gm JOIN games g ON g.game_id = gm.game_id
        WHERE {where_sql}
        GROUP BY gm.metric_name
    """, params).fetchall()
    stat_by_name = {r["metric_name"]: r for r in stat_rows}

    weight_vs_delivery = []
    for m in scoring.METRICS:
        name = m["name"]
        s = stat_by_name.get(name)
        avg_norm = s["avg_norm"] if s else 0.0
        n_metric = s["n"] if s else 0
        mean_weighted = avg_norm * m["weight"]
        designed_share = m["weight"] / TOTAL_WEIGHT
        delivered_share = (mean_weighted / (TOTAL_WEIGHT * mean_score)) if mean_score else 0.0
        weight_vs_delivery.append({
            "name": name, "label": METRIC_COPY[name]["label"], "weight": m["weight"], "cap": m["cap"],
            "n": n_metric, "avg_raw": s["avg_raw"] if s else None, "max_raw": s["max_raw"] if s else None,
            "avg_norm": avg_norm, "n_at_cap": s["n_at_cap"] if s else 0,
            "pct_at_cap": (s["n_at_cap"] / n_metric * 100) if n_metric else 0.0,
            "designed_share": designed_share, "delivered_share": delivered_share,
            "delta": delivered_share - designed_share,
        })
    callout = build_misalignment_callout(weight_vs_delivery)

    all_seasons = [r["season_year"] for r in conn.execute("SELECT DISTINCT season_year FROM games ORDER BY season_year").fetchall()]
    # Always every season, independent of the page's single-season filter --
    # the by-week chart has its own multiselect on the client, defaulting to
    # showing all of them at once as separate bars.
    by_week_seasons = all_seasons
    by_week_season_type = int(season_type) if season_type != "all" else 2
    by_week_rows = conn.execute(f"""
        SELECT season_year, week, AVG(watchability_score) AS avg_score, COUNT(*) AS n
        FROM games
        WHERE watchability_score IS NOT NULL AND week IS NOT NULL AND season_type = ?
              AND season_year IN ({','.join('?' * len(by_week_seasons)) if by_week_seasons else 'NULL'})
        GROUP BY season_year, week ORDER BY season_year, week
    """, [by_week_season_type] + by_week_seasons).fetchall()
    by_week = {}
    for r in by_week_rows:
        by_week.setdefault(str(r["season_year"]), []).append({"week": r["week"], "avg_score": r["avg_score"], "n": r["n"]})

    corr_cols = ["watchability_score"] + [m["name"] for m in scoring.METRICS]
    case_selects = ",\n               ".join(
        f"MAX(CASE WHEN gm.metric_name='{m['name']}' THEN gm.norm_value END) AS {m['name']}"
        for m in scoring.METRICS
    )
    corr_rows = conn.execute(f"""
        SELECT g.game_id, g.watchability_score,
               {case_selects}
        FROM games g LEFT JOIN game_metrics gm ON gm.game_id = g.game_id
        WHERE {where_sql}
        GROUP BY g.game_id
    """, params).fetchall()

    if corr_rows:
        import pandas as pd
        df = pd.DataFrame([dict(r) for r in corr_rows])[corr_cols]
        mat = df.corr()
        counts = df.notna().astype(int)
        count_mat = counts.T.dot(counts)
        correlation = {
            "labels": corr_cols,
            "matrix": [[(None if pd.isna(mat.iloc[i, j]) else round(float(mat.iloc[i, j]), 4))
                        for j in range(len(corr_cols))] for i in range(len(corr_cols))],
            "n_matrix": [[int(count_mat.iloc[i, j]) for j in range(len(corr_cols))] for i in range(len(corr_cols))],
        }
    else:
        correlation = {"labels": corr_cols, "matrix": [], "n_matrix": []}

    return jsonify({
        "n": n,
        "distribution": dist,
        "weight_vs_delivery": weight_vs_delivery,
        "callout": callout,
        "not_implemented": NOT_IMPLEMENTED,
        "by_week": by_week,
        "correlation": correlation,
        "total_weight": TOTAL_WEIGHT,
    })


# ---- feed (fetch_log / poller_state) ---------------------------------------
#
# Admin-only observability for the data-source pollers (src/fetchlog.py
# writes fetch_log/poller_state, src/espn.py and src/fox.py are the only
# writers of individual rows). Read-only against cfb.db like every other
# route in this file. None of these three routes select score/
# watchability_score/live_score -- only matchup labels, kickoff times, and
# fetch metadata -- so there is nothing here for _assert_no_spoiler_leaks
# to catch even though it never fires on this payload shape (it only walks
# dicts carrying spoiler_hidden); see tests/test_feed_api.py, which checks
# that invariant directly.

def _percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(int(len(s) * p), len(s) - 1)]


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _feed_game_row(r):
    return {
        "game_id": r["game_id"],
        "away_team_abbr": r["away_team_abbr"],
        "home_team_abbr": r["home_team_abbr"],
        "game_date": r["game_date"],
        "status_state": r["status_state"],
    }


@app.route("/api/feed")
def api_feed():
    _require_admin()
    conn = get_db()

    poller_row = conn.execute("SELECT * FROM poller_state WHERE poller = 'live'").fetchone()
    poller = dict(poller_row) if poller_row else None

    now = datetime.now(timezone.utc)
    seconds_since_last_cycle = None
    seconds_to_next_wake = None
    if poller:
        last = _parse_iso(poller.get("last_cycle_at"))
        if last is not None:
            seconds_since_last_cycle = (now - last).total_seconds()
        nxt = _parse_iso(poller.get("next_wake_at"))
        if nxt is not None:
            seconds_to_next_wake = (nxt - now).total_seconds()

    # A crash leaves stopped_at NULL forever (only a clean shutdown sets
    # it), so "running" alone can't tell a live poller from a dead one --
    # staleness against the poller's own last-declared wake time is what
    # actually catches that case. 120s of slack over the wake time itself
    # covers an ordinary cycle's wall-clock run time.
    running = bool(poller) and poller.get("stopped_at") is None
    stale = (
        running and seconds_to_next_wake is not None and seconds_to_next_wake < -120
    )

    threshold_24h = (now - timedelta(hours=24)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    fetch_rows = conn.execute(
        "SELECT source, ok, latency_ms, requested_at FROM fetch_log WHERE requested_at >= ?",
        (threshold_24h,),
    ).fetchall()

    by_source = {}
    for r in fetch_rows:
        b = by_source.setdefault(r["source"], {
            "n": 0, "n_error": 0, "latencies": [], "last_ok_at": None, "last_error_at": None,
        })
        b["n"] += 1
        if r["ok"]:
            if r["latency_ms"] is not None:
                b["latencies"].append(r["latency_ms"])
            if b["last_ok_at"] is None or r["requested_at"] > b["last_ok_at"]:
                b["last_ok_at"] = r["requested_at"]
        else:
            b["n_error"] += 1
            if b["last_error_at"] is None or r["requested_at"] > b["last_error_at"]:
                b["last_error_at"] = r["requested_at"]

    sources = [{
        "source": src,
        "n": b["n"],
        "n_error": b["n_error"],
        "p50_latency_ms": _percentile(b["latencies"], 0.50),
        "p95_latency_ms": _percentile(b["latencies"], 0.95),
        "last_ok_at": b["last_ok_at"],
        "last_error_at": b["last_error_at"],
    } for src, b in sorted(by_source.items())]

    live_games = conn.execute("""
        SELECT game_id, away_team_abbr, home_team_abbr, game_date, status_state
        FROM games WHERE status_state = 'in' ORDER BY game_date ASC
    """).fetchall()

    upcoming = conn.execute("""
        SELECT game_id, away_team_abbr, home_team_abbr, game_date, status_state
        FROM games WHERE status_state = 'pre' AND game_date >= ?
        ORDER BY game_date ASC LIMIT 10
    """, (now.strftime(live.GAME_DATE_FMT),)).fetchall()

    return jsonify({
        "poller": poller,
        "running": running,
        "stale": stale,
        "seconds_since_last_cycle": seconds_since_last_cycle,
        "seconds_to_next_wake": seconds_to_next_wake,
        "sources_24h": sources,
        "live_games": [_feed_game_row(r) for r in live_games],
        "upcoming": [_feed_game_row(r) for r in upcoming],
        "refresh_queue": live._tier2_priority(conn),
    })


@app.route("/api/feed/log")
def api_feed_log():
    _require_admin()
    conn = get_db()

    where, params = [], []
    for field, col in (("source", "source"), ("kind", "endpoint_kind"), ("caller", "caller"), ("game_id", "game_id")):
        val = request.args.get(field)
        if val:
            where.append(f"{col} = ?")
            params.append(val)

    ok = request.args.get("ok")
    if ok is not None:
        if ok not in ("0", "1"):
            abort(400, description="ok must be 0 or 1")
        where.append("ok = ?")
        params.append(int(ok))

    since = request.args.get("since")
    if since:
        where.append("requested_at >= ?")
        params.append(since)

    before_id = request.args.get("before_id")
    if before_id:
        try:
            where.append("id < ?")
            params.append(int(before_id))
        except ValueError:
            abort(400, description="before_id must be an integer")

    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except ValueError:
        abort(400, description="limit must be an integer")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"""
        SELECT id, requested_at, source, endpoint_kind, url, caller, cycle_seq,
               game_id, source_ref, attempt, ok, http_status, latency_ms, bytes, error
        FROM fetch_log
        {where_sql}
        ORDER BY id DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    return jsonify({"rows": [dict(r) for r in rows], "limit": limit})


@app.route("/api/feed/activity")
def api_feed_activity():
    _require_admin()
    conn = get_db()
    try:
        hours = min(max(int(request.args.get("hours", 24)), 1), 24 * 14)
    except ValueError:
        abort(400, description="hours must be an integer")

    now = datetime.now(timezone.utc)
    threshold = (now - timedelta(hours=hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    rows = conn.execute(
        "SELECT requested_at, ok FROM fetch_log WHERE requested_at >= ? ORDER BY requested_at ASC",
        (threshold,),
    ).fetchall()

    bucket_seconds = 300
    buckets = {}
    for r in rows:
        ts = _parse_iso(r["requested_at"])
        if ts is None:
            continue
        epoch = int(ts.timestamp())
        bucket_start = epoch - (epoch % bucket_seconds)
        b = buckets.setdefault(bucket_start, {"ok": 0, "error": 0})
        if r["ok"]:
            b["ok"] += 1
        else:
            b["error"] += 1

    buckets_out = [{
        "bucket_start": datetime.fromtimestamp(start, tz=timezone.utc)
                                 .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "ok": b["ok"],
        "error": b["error"],
    } for start, b in sorted(buckets.items())]

    return jsonify({"hours": hours, "bucket_seconds": bucket_seconds, "buckets": buckets_out})


# ---- spoiler policy ---------------------------------------------------------
#
# The only writes this process ever performs -- all three POST routes below
# go through src/spoilers.py's save_policy(), which touches only
# data/spoilers.json. cfb.db stays untouched and strictly read-only (see
# get_db()'s mode=ro connection and _startup_selfcheck()'s writability
# probe). GET here is read-only against both the policy file and cfb.db
# (used only to validate that a week/game actually exists before writing).

def _spoiler_active_overrides(policy):
    """Flattens policy["weeks"]/policy["games"] into the list the nav
    toggle's "Active overrides" panel renders, each with an ✕ that POSTs
    level: null to clear it. Reports both `level` (the real value) and
    `hidden` (level == LEVEL_HIDDEN) -- the latter kept for any client code
    still reading the pre-LEVEL_SCORE field name."""
    overrides = []
    for key, level in sorted(policy.get("weeks", {}).items()):
        sy, st, wk = key.split(":")
        overrides.append({
            "type": "week", "season_year": int(sy), "season_type": int(st), "week": int(wk),
            "level": level, "hidden": level == spoilers.LEVEL_HIDDEN,
        })
    for gid, level in sorted(policy.get("games", {}).items()):
        overrides.append({
            "type": "game", "game_id": gid, "level": level, "hidden": level == spoilers.LEVEL_HIDDEN,
        })
    return overrides


def _parse_spoiler_level(data):
    """Shared body-parsing for /api/spoilers/week and /api/spoilers/game:
    accepts either a `level` (0 | 1 | 2 | null) or a legacy `hidden`
    (true | false | null) field, never both -- a request sending both is
    ambiguous about which one wins, so it's rejected rather than guessed
    at. `hidden` maps true -> LEVEL_HIDDEN, false -> LEVEL_FULL, null ->
    None (clear the override), the same mapping _coerce_level() applies
    when migrating a stored policy value, so an un-updated client and an
    old on-disk value both land in the same place. Neither field sent
    defaults to `hidden: true` (LEVEL_HIDDEN), matching the pre-existing
    default for these routes."""
    has_level = "level" in data
    has_hidden = "hidden" in data
    if has_level and has_hidden:
        abort(400, description="send either level or hidden, not both")
    if has_level:
        level = data["level"]
        if level is not None and (
            isinstance(level, bool)
            or level not in (spoilers.LEVEL_HIDDEN, spoilers.LEVEL_SCORE, spoilers.LEVEL_FULL)
        ):
            abort(400, description="level must be 0, 1, 2, or null")
        return level
    hidden = data.get("hidden", True)
    if hidden is not None and not isinstance(hidden, bool):
        abort(400, description="hidden must be true, false, or null")
    if hidden is None:
        return None
    return spoilers.LEVEL_HIDDEN if hidden else spoilers.LEVEL_FULL


@app.route("/api/spoilers")
def api_spoilers_get():
    policy = spoiler_ctx()
    return jsonify({"policy": policy, "active_overrides": _spoiler_active_overrides(policy)})


@app.route("/api/spoilers/search")
def api_spoilers_search():
    """Look up games by team/venue name or exact game_id, deliberately
    BYPASSING the spoiler policy -- used only by the spoiler settings
    page's game picker and its active-overrides list. You can't manage a
    game's override if the one endpoint that could find it excludes it for
    being hidden, so this is a second, narrowly-scoped read path.

    Safe by construction, not by discipline: the query never selects
    score, metrics, rank, or any other field on the hidden list -- there
    is nothing in this response that could leak an outcome even if it
    were called on a fully-hidden game."""
    conn = get_db()
    game_id = request.args.get("game_id", "").strip()
    q = request.args.get("q", "").strip()
    try:
        limit = min(max(int(request.args.get("limit", 15)), 1), 50)
    except ValueError:
        abort(400, description="limit must be an integer")

    if game_id:
        where_sql, params = "game_id = ?", [game_id]
    elif q:
        # Split on whitespace and AND the terms together (each term OR'd
        # across all searchable fields) so "PSU OSU" requires both
        # acronyms present -- one per team, in either home/away slot --
        # rather than matching either team alone. A single-term query
        # (the common case) degenerates to the old OR-only behavior.
        terms = q.split()
        clauses, params = [], []
        for term in terms:
            like = f"%{term}%"
            clauses.append(
                "(home_team_abbr LIKE ? OR away_team_abbr LIKE ? OR "
                "home_team_name LIKE ? OR away_team_name LIKE ? OR venue_name LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        where_sql = " AND ".join(clauses)
    else:
        return jsonify({"results": []})

    rows = conn.execute(f"""
        SELECT game_id, season_year, season_type, week, event_note, game_date,
               home_team_abbr, home_team_name, away_team_abbr, away_team_name
        FROM games WHERE {where_sql}
        ORDER BY game_date DESC LIMIT ?
    """, params + [limit]).fetchall()
    results = [{
        "game_id": r["game_id"], "season_year": r["season_year"], "season_type": r["season_type"],
        "week": r["week"], "event_note": r["event_note"], "game_date": r["game_date"],
        "home": {"abbr": r["home_team_abbr"], "name": r["home_team_name"]},
        "away": {"abbr": r["away_team_abbr"], "name": r["away_team_name"]},
    } for r in rows]
    return jsonify({"results": results})


@app.route("/api/spoilers/week", methods=["POST"])
def api_spoilers_week():
    conn = get_db()
    data = request.get_json(silent=True) or {}
    try:
        season_year = int(data["season_year"])
        season_type = int(data["season_type"])
        week = int(data["week"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="season_year, season_type, and week are required integers")
    level = _parse_spoiler_level(data)

    exists = conn.execute(
        "SELECT 1 FROM games WHERE season_year=? AND season_type=? AND week=? LIMIT 1",
        (season_year, season_type, week),
    ).fetchone()
    if exists is None:
        abort(404, description="no games match that season/season_type/week")

    policy = spoilers.set_user_week(
        current_user()["user_id"], season_year, season_type, week, level, conn=get_users_db()
    )
    return jsonify({"policy": policy, "active_overrides": _spoiler_active_overrides(policy)})


@app.route("/api/spoilers/game", methods=["POST"])
def api_spoilers_game():
    conn = get_db()
    data = request.get_json(silent=True) or {}
    game_id = data.get("game_id")
    if not game_id:
        abort(400, description="game_id is required")
    level = _parse_spoiler_level(data)

    exists = conn.execute("SELECT 1 FROM games WHERE game_id=? LIMIT 1", (game_id,)).fetchone()
    if exists is None:
        abort(404, description="no such game")

    policy = spoilers.set_user_game(current_user()["user_id"], game_id, level, conn=get_users_db())
    return jsonify({"policy": policy, "active_overrides": _spoiler_active_overrides(policy)})


@app.route("/api/spoilers/default", methods=["POST"])
def api_spoilers_default():
    """Unlike /spoilers/week and /spoilers/game, this deliberately does NOT
    check that (season_year, season_type, week) matches any existing games
    -- the whole point of the default threshold is to future-proof a
    season that hasn't been ingested yet (see src/spoilers.py's module
    docstring: "2027+ is covered automatically with no config edit"). A
    week number that doesn't exist yet for that season is not an error."""
    data = request.get_json(silent=True) or {}
    season_year = data.get("season_year")
    user_id = current_user()["user_id"]
    if season_year is None:
        policy = spoilers.set_user_default(user_id, None, None, None, conn=get_users_db())
        return jsonify({"policy": policy, "active_overrides": _spoiler_active_overrides(policy)})

    try:
        season_year = int(season_year)
        season_type = int(data["season_type"])
        week = int(data["week"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="season_year, season_type, and week are required integers (or send season_year: null to reset)")
    if season_type not in (2, 3):
        abort(400, description="season_type must be 2 or 3")

    policy = spoilers.set_user_default(user_id, season_year, season_type, week, conn=get_users_db())
    return jsonify({"policy": policy, "active_overrides": _spoiler_active_overrides(policy)})


# ---- auth -----------------------------------------------------------------

def _start_session(user):
    # session.clear() first: a login/signup from a browser that already had
    # a (possibly different-user) session cookie must not merge state --
    # e.g. spoiler_ctx or anything else keyed off flask.g within this
    # request already ran before the auth gate could know who this is.
    session.clear()
    session["user_id"] = user["user_id"]
    session["session_epoch"] = user["session_epoch"]
    session.permanent = True


@app.route("/api/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    conn = get_users_db()
    user = users.get_user_by_username(conn, username) if username else None
    if user is None or not users.verify_password(user, password):
        # Same message either way -- doesn't tell an attacker whether the
        # username exists.
        abort(401, description="invalid username or password")
    users.touch_last_seen(conn, user["user_id"])
    _start_session(user)
    return jsonify({"username": user["username"], "is_admin": bool(user["is_admin"])})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/signup", methods=["POST"])
@limiter.limit("5 per hour")
def api_signup():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    invite_code = (data.get("invite_code") or "").strip()
    if not invite_code:
        abort(400, description="an invite code is required")
    conn = get_users_db()
    try:
        user = users.create_user(conn, username, password, invite_code=invite_code)
    except users.UsernameTaken:
        abort(409, description="that username is taken")
    except users.InvalidInvite:
        abort(400, description="invalid or already-used invite code")
    except ValueError as e:
        abort(400, description=str(e))
    _start_session(user)
    return jsonify({"username": user["username"], "is_admin": bool(user["is_admin"])})


@app.route("/api/me")
def api_me():
    user = current_user()
    return jsonify({"username": user["username"], "is_admin": bool(user["is_admin"])})


# ---- static files -------------------------------------------------------------

@app.route("/")
def index_page():
    # Landing page is Slate -- the page you'd actually want to see first on
    # opening the site. /slate.html is still reachable directly, just no
    # longer the only way to reach it. Every page's nav points its Settings
    # link at "/settings.html" explicitly (not "/") so it isn't shadowed by
    # whatever "/" resolves to.
    return send_from_directory(WEB_DIR, "slate.html")


@app.route("/<path:filename>")
def web_static(filename):
    target = (WEB_DIR / filename).resolve()
    try:
        target.relative_to(WEB_DIR)
    except ValueError:
        abort(404)
    if not target.exists() or not target.is_file():
        abort(404)
    # Font files never change without a filename change and are requested on
    # every page's <head> -- Flask's send_from_directory default of
    # Cache-Control: no-cache forces a revalidation round-trip for each one
    # on every full-page navigation (this is a multi-page app, no client-side
    # routing), which is exactly what was producing a visible flash of
    # fallback-font text before the real font swapped in on every click.
    # Everything else (style.css/app.js/charts.js/html) keeps the no-cache
    # default so edits during development show up immediately.
    if filename.startswith("fonts/"):
        resp = send_from_directory(WEB_DIR, filename, max_age=31536000)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp
    return send_from_directory(WEB_DIR, filename)


if __name__ == "__main__":
    _startup_selfcheck()
    if os.environ.get("CC_DEV") == "1":
        # Local iteration only -- Werkzeug's debugger + reloader. Never set
        # CC_DEV=1 anywhere reachable from outside loopback: the debugger is
        # remote code execution if it's ever exposed to the tunnel.
        app.run(host="127.0.0.1", port=5050, debug=True, use_reloader=True)
    else:
        from waitress import serve as waitress_serve
        print("[serve.py] serving via waitress on http://127.0.0.1:5050")
        waitress_serve(app, host="127.0.0.1", port=5050, threads=8)
