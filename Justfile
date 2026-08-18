venv := "./venv/bin/python"
log_dir := env_var("HOME") + "/Library/Logs/creamcheese"
live_log := log_dir + "/live.log"
serve_log := log_dir + "/serve.log"
backup_dir := env_var_or_default("CC_BACKUP_DIR", env_var("HOME") + "/cream_cheese_backups")

plist_dir := env_var("HOME") + "/Library/LaunchAgents"
serve_label := "com.creamcheese.serve"
live_label := "com.creamcheese.live"
uid := `id -u`

default:
    @just --list

# ---- launchd services -----------------------------------------------------
# One-time setup: copies the plists in deploy/ into ~/Library/LaunchAgents
# and bootstraps them with launchd. After this, serve is always-on
# (RunAtLoad+KeepAlive) and live starts itself Thu/Fri/Sat 09:00 ET, ending
# its own window at --live-until (see deploy/com.creamcheese.live.plist).
# Safe to re-run -- bootout's failure (nothing loaded yet) is swallowed.
install-services:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{plist_dir}} {{log_dir}}
    for label in {{serve_label}} {{live_label}}; do
        cp deploy/$label.plist {{plist_dir}}/$label.plist
        launchctl bootout gui/{{uid}}/$label 2>/dev/null || true
        launchctl bootstrap gui/{{uid}} {{plist_dir}}/$label.plist
        echo "installed and bootstrapped $label"
    done
    echo "serve is running now; live will start itself Thu/Fri/Sat 09:00 ET -- 'just live-now' to start it immediately instead of waiting"

# Reverse of install-services -- unloads both from launchd and removes the
# installed plists (the copies in deploy/ are untouched).
uninstall-services:
    #!/usr/bin/env bash
    set -euo pipefail
    for label in {{serve_label}} {{live_label}}; do
        launchctl bootout gui/{{uid}}/$label 2>/dev/null || echo "$label was not loaded"
        rm -f {{plist_dir}}/$label.plist
    done

# (Re)start the web UI right now (requires `just install-services` first).
serve:
    #!/usr/bin/env bash
    set -euo pipefail
    launchctl kickstart -k gui/{{uid}}/{{serve_label}}
    echo "web UI (re)started -- http://127.0.0.1:5050${CC_PUBLIC_ORIGIN:+ (public: $CC_PUBLIC_ORIGIN)}"

stop-serve:
    launchctl bootout gui/{{uid}}/{{serve_label}} 2>/dev/null || echo "not running"

# Start the live poller right now, ignoring the Thu/Fri/Sat schedule --
# for off-schedule games (Tue/Wed MACtion, weekday bowls/CFP). Runs until
# the same --live-until deadline a scheduled start would (see the plist);
# `just discover <week>` backfills anything a window entirely missed.
live-now:
    launchctl kickstart -k gui/{{uid}}/{{live_label}}

stop-live:
    launchctl bootout gui/{{uid}}/{{live_label}} 2>/dev/null || echo "not running"

# Run the live poller in the foreground (Ctrl-C to stop cleanly) -- outside
# launchd entirely, e.g. for local testing before `install-services`.
live-fg:
    caffeinate -i {{venv}} pipeline.py --live

# Make sure both are running right now -- the usual gameday command once
# services are installed (most days, `just live` doesn't need this at all;
# it's already running on schedule).
gameday: live-now serve
    @echo "both started -- 'just status' to check"

stop-all: stop-live stop-serve

status:
    #!/usr/bin/env bash
    echo "-- live poller --"
    launchctl print gui/{{uid}}/{{live_label}} 2>/dev/null | grep -E "state =|pid =" || echo "not loaded (see 'just install-services')"
    echo "-- web UI --"
    launchctl print gui/{{uid}}/{{serve_label}} 2>/dev/null | grep -E "state =|pid =" || echo "not loaded (see 'just install-services')"
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

# ---- accounts -----------------------------------------------------------

# Bootstrap the first admin account (prompts for a password). No invite
# needed for this one -- it's how the invite system itself gets started.
create-admin username:
    {{venv}} scripts/manage_users.py create-admin {{username}}

# Mint a new invite code, e.g. `just invite "for Dave"`.
invite note="":
    {{venv}} scripts/manage_users.py invite "{{note}}"

# List every invite code and whether it's been redeemed.
list-invites:
    {{venv}} scripts/manage_users.py list-invites

# Reset an existing user's password (prompts for the new one). There's no
# self-service reset flow -- invite-only signup means no email to send a
# reset link to -- so this is the only way back into a lost account.
reset-password username:
    {{venv}} scripts/manage_users.py reset-password {{username}}

# One-shot: copy the legacy data/spoilers.json policy into the first
# admin's per-user row in data/users.db. Run once, after create-admin and
# before anyone else signs up, to carry forward settings from before
# accounts existed.
migrate-spoilers:
    {{venv}} scripts/migrate_spoilers.py

# ---- backups --------------------------------------------------------------

# Snapshot both databases (VACUUM INTO -- safe against a live WAL database,
# unlike a plain file copy, and safe to run while `just live`/`just serve`
# are up) plus data/auth.json, to a timestamped directory outside the repo
# (override with CC_BACKUP_DIR). Keeps the last 7 snapshots. data/users.db
# has no ESPN source to rebuild from if it's lost -- unlike cfb.db, back it
# up like it matters.
backup:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{backup_dir}}
    dest="{{backup_dir}}/$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$dest"
    sqlite3 data/cfb.db "VACUUM INTO '$dest/cfb.db'"
    if [ -f data/users.db ]; then
        sqlite3 data/users.db "VACUUM INTO '$dest/users.db'"
    else
        echo "note: data/users.db doesn't exist yet (no accounts created) -- skipping"
    fi
    [ -f data/auth.json ] && cp data/auth.json "$dest/auth.json"
    echo "backed up to $dest"
    ls -1dt {{backup_dir}}/*/ 2>/dev/null | tail -n +8 | xargs -r rm -rf
    kept=$(ls -1d {{backup_dir}}/*/ 2>/dev/null | wc -l | tr -d ' ')
    echo "pruned to the last 7 -- $kept snapshot(s) retained"
