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
# and bootstraps them with launchd. Both services are always-on now
# (RunAtLoad+KeepAlive) -- live has no calendar window anymore, it derives
# its own poll cadence from the kickoff times in `games` (see
# deploy/com.creamcheese.live.plist and src/live.py's _schedule_interval).
# Safe to re-run -- bootout's failure (nothing loaded yet) is swallowed, and
# re-running after editing a plist is the correct way to pick up the change.
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
    echo "both running now -- 'just status' to check"

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

# Restart the poller right now -- e.g. after a code change (install-services
# also does this, but is heavier). Always runs a cycle immediately on start,
# so it's also the fastest way to force an instant catch-up mid-game.
live-now:
    launchctl kickstart -k gui/{{uid}}/{{live_label}}

# Stops the poller until the next login -- launchd reloads the plist (still
# installed in ~/Library/LaunchAgents) automatically then, so this is a
# pause, not an off switch. For "keep it off" (travel, offseason) use
# `just disable-live` instead.
stop-live:
    launchctl bootout gui/{{uid}}/{{live_label}} 2>/dev/null || echo "not running"

# Persistently disable the poller -- survives reboots/logins, unlike
# stop-live. `just enable-live` reverses it.
disable-live:
    #!/usr/bin/env bash
    launchctl bootout gui/{{uid}}/{{live_label}} 2>/dev/null || true
    launchctl disable gui/{{uid}}/{{live_label}}
    echo "live poller disabled -- 'just enable-live' to bring it back"

# Reverse of disable-live: re-enables and (re)starts the poller.
enable-live:
    #!/usr/bin/env bash
    launchctl enable gui/{{uid}}/{{live_label}}
    launchctl bootstrap gui/{{uid}} {{plist_dir}}/{{live_label}}.plist 2>/dev/null \
        || launchctl kickstart -k gui/{{uid}}/{{live_label}}
    echo "live poller enabled and running"

# Run the live poller in the foreground (Ctrl-C to stop cleanly) -- outside
# launchd entirely, e.g. for local testing before `install-services`. The
# process manages its own caffeinate assertion now (see _sync_caffeinate),
# so this no longer wraps itself in one.
live-fg:
    {{venv}} pipeline.py --live

# The documented panic button: forces the old fixed-60s, always-awake
# cadence in the foreground, bypassing schedule-aware sleeping entirely.
# For a persistent version of this, see the FALLBACK comment in
# deploy/com.creamcheese.live.plist.
live-fixed:
    {{venv}} pipeline.py --live --live-interval 60

# Check both services are actually running -- most days nothing else is
# needed, since both are always-on; this is the "did something go wrong"
# check.
gameday: status

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

# Discover+fetch+score a specific week. Also what keeps the schedule the
# poller's own cadence depends on from rotting -- a never-discovered game
# self-heals within 30min once it's in the today/tomorrow window (see
# _schedule_interval), but stays invisible before that. season_type=3
# (postseason) isn't covered by season-long discovery at all; run this
# explicitly for bowls/CFP once the bracket is set.
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
# unlike a plain file copy, and safe to run while the live poller/serve are
# up) plus data/auth.json, to a timestamped directory outside the repo
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
