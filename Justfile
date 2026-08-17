venv := "./venv/bin/python"
live_log := "logs/live.log"
live_pid := "logs/live.pid"
serve_log := "logs/serve.log"
serve_pid := "logs/serve.pid"

default:
    @just --list

# Start the live poller in the background under `caffeinate -i` (blocks idle
# sleep, NOT lid-close sleep -- if the lid closes with no external display
# attached, this pauses until the Mac wakes; reconcile_on_start() cleans up
# the gap automatically on the next cycle). Survives the terminal closing.
live:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p logs
    nohup caffeinate -i {{venv}} pipeline.py --live > {{live_log}} 2>&1 &
    echo $! > {{live_pid}}
    echo "live poller started, pid $(cat {{live_pid}}); 'just logs-live' to watch"

# Run the live poller in the foreground (Ctrl-C to stop cleanly).
live-fg:
    caffeinate -i {{venv}} pipeline.py --live

stop-live:
    #!/usr/bin/env bash
    if [ -f {{live_pid}} ] && kill -0 "$(cat {{live_pid}})" 2>/dev/null; then
        kill -TERM "$(cat {{live_pid}})"
        echo "sent SIGTERM to $(cat {{live_pid}}) -- it finishes the current cycle before exiting"
    else
        echo "no running live poller found"
    fi
    rm -f {{live_pid}}

# Start the read-only web UI in the background.
serve:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p logs
    nohup {{venv}} serve.py > {{serve_log}} 2>&1 &
    echo $! > {{serve_pid}}
    echo "web UI started, pid $(cat {{serve_pid}}) -- http://127.0.0.1:5050"

stop-serve:
    #!/usr/bin/env bash
    if [ -f {{serve_pid}} ] && kill -0 "$(cat {{serve_pid}})" 2>/dev/null; then
        kill "$(cat {{serve_pid}})"
        echo "stopped web UI (pid $(cat {{serve_pid}}))"
    else
        echo "no running web UI found"
    fi
    rm -f {{serve_pid}}

# Start both the live poller and the web UI -- the usual gameday command.
gameday: live serve
    @echo "both started -- 'just status' to check, 'just stop-all' when done for the day"

stop-all: stop-live stop-serve

status:
    #!/usr/bin/env bash
    echo "-- live poller --"
    if [ -f {{live_pid}} ] && kill -0 "$(cat {{live_pid}})" 2>/dev/null; then
        echo "running, pid $(cat {{live_pid}})"
    else
        echo "not running"
    fi
    echo "-- web UI --"
    if [ -f {{serve_pid}} ] && kill -0 "$(cat {{serve_pid}})" 2>/dev/null; then
        echo "running, pid $(cat {{serve_pid}})"
    else
        echo "not running"
    fi
    echo "-- healthz --"
    curl -s http://127.0.0.1:5050/api/healthz | python3 -m json.tool 2>/dev/null || echo "web UI unreachable"

logs-live:
    tail -f {{live_log}}

logs-serve:
    tail -f {{serve_log}}

# Backfill discover+fetch+score for a specific week -- useful for days
# --live's rolling today/tomorrow window never covered (e.g. Thu/Fri
# openers if --live wasn't started until Saturday morning).
discover week season="2026":
    {{venv}} pipeline.py --week {{week}} --season {{season}}

# Re-run Phase 3 scoring only (e.g. after a scoring.py change).
rescore:
    {{venv}} pipeline.py --score-only --rescore
