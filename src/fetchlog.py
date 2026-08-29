"""
Durable per-request log for every outbound call to an external data source
(ESPN, Fox), plus a one-row "what is the live poller about to do" record.
See plans/... (Feed page plan) for the full design; this module is the
instrumentation layer src/espn.py and src/fox.py call into.

Two things this module refuses to ever do, on purpose:
  1. Raise. record() is called from inside the two hottest fetch paths in
     the app (the always-on live poller and the Fox ID-walk); an
     observability feature that can crash the thing it's observing is worse
     than no observability at all. Every public function catches broadly.
  2. Require espn.py/fox.py to accept a `conn` argument. Threading a
     connection through would touch ~11 call sites across pipeline.py,
     src/live.py, and src/fox_match.py. Instead this module owns its own
     lazily-opened, autocommit connection -- safe because no write
     transaction anywhere in the app spans an HTTP fetch (verified: fetches
     always happen before the `with conn:` block that follows them), so
     there's no lock contention with the caller's own connection.

Disabled (a no-op) until configure() is called -- importing espn/fox from a
test or a one-off script must not silently create/write data/cfb.db.
"""

import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config, db

_MAX_CONSECUTIVE_FAILURES = 3

_enabled = False
_db_path = None
_default_caller = None
_conn = None
_consecutive_failures = 0
_context_stack = []
_lock = threading.Lock()

_APIKEY_RE = re.compile(r"([?&]apikey=)[^&]*", re.IGNORECASE)


def configure(caller=None, db_path=None, enabled=True):
    """Call once per process (pipeline.py's main(), before any fetch). Safe
    to call more than once -- e.g. a test pointing db_path at a tmpdir."""
    global _enabled, _db_path, _default_caller, _conn, _consecutive_failures
    with _lock:
        _enabled = enabled
        _db_path = db_path or config.DB_PATH
        _default_caller = caller
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        _consecutive_failures = 0


def _redact(url):
    """Strips the Fox API key (src/config.py's FOX_APIKEY, embedded in
    every Fox URL by src/fox.py) before a URL is ever stored -- otherwise
    the key leaks into the DB, into `just backup` snapshots, and onto the
    Feed page."""
    return _APIKEY_RE.sub(r"\1REDACTED", url)


def now_iso():
    """ISO8601 UTC, ms precision -- the timestamp format used throughout
    fetch_log/poller_state. Public so src/live.py can stamp poller_state's
    started_at/last_cycle_at/next_wake_at/stopped_at with the same format
    without duplicating it."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _get_conn():
    global _conn
    if _conn is None:
        conn = sqlite3.connect(_db_path, isolation_level=None)  # autocommit: one row per request
        conn.execute("PRAGMA busy_timeout = 5000")
        _conn = conn
    return _conn


def _disable_after_failure():
    global _enabled, _consecutive_failures
    _consecutive_failures += 1
    if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
        _enabled = False
        import logging
        logging.getLogger(__name__).exception(
            "fetchlog: %d consecutive failures writing fetch_log -- disabling for the rest of this process",
            _consecutive_failures,
        )


@contextmanager
def context(**fields):
    """Ambient fields (caller/cycle_seq/game_id/...) merged into every
    record() call made while this context is active. Nests: an inner
    context only overrides the keys it passes, and restores the outer
    context on exit -- including on exception, so a failed cycle doesn't
    leak stale context into the next one."""
    _context_stack.append(fields)
    try:
        yield
    finally:
        _context_stack.pop()


def _current_context():
    merged = {}
    for frame in _context_stack:
        merged.update(frame)
    return merged


def record(source, endpoint_kind, url, *, ok, http_status=None, latency_ms=None,
           bytes=None, error=None, attempt=1, game_id=None, source_ref=None):
    """Log one HTTP request. Never raises -- see module docstring. `bytes`
    is the response body size; shadows the builtin within this function
    only, matching the fetch_log column name."""
    if not _enabled:
        return
    try:
        ctx = _current_context()
        row = {
            "requested_at": now_iso(),
            "source": source,
            "endpoint_kind": endpoint_kind,
            "url": _redact(url),
            "caller": ctx.get("caller", _default_caller),
            "cycle_seq": ctx.get("cycle_seq"),
            "game_id": game_id if game_id is not None else ctx.get("game_id"),
            "source_ref": source_ref,
            "attempt": attempt,
            "ok": 1 if ok else 0,
            "http_status": http_status,
            "latency_ms": latency_ms,
            "bytes": bytes,
            "error": (str(error)[:500] if error else None),
        }
        conn = _get_conn()
        db.insert_fetch_log(conn, row)
        global _consecutive_failures
        _consecutive_failures = 0
    except Exception:
        _disable_after_failure()


def record_poller_state(poller, **fields):
    """Thin passthrough to db.upsert_poller_state on this module's own
    connection, same never-raise contract as record(). Separate from
    record() because it isn't per-request -- src/live.py calls this once
    per cycle/lifecycle event, not once per fetch."""
    if not _enabled:
        return
    try:
        conn = _get_conn()
        db.upsert_poller_state(conn, poller, **fields)
        global _consecutive_failures
        _consecutive_failures = 0
    except Exception:
        _disable_after_failure()
