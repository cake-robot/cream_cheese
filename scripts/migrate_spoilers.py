"""
One-shot migration: copy the old shared data/spoilers.json policy into the
first admin's per-user row in data/users.db, so the defaults and overrides
you'd already set before accounts existed survive the cutover to per-user
spoiler policy (see src/spoilers.py's module docstring on why the
file-based functions are kept around specifically for this).

Run once, after `just create-admin` and before anyone else signs up:
    just migrate-spoilers

Safe to run more than once -- it always overwrites the target user's
policy with whatever's currently in data/spoilers.json, so a second run
just re-applies the same state. data/spoilers.json itself is left on disk;
nothing in the running app reads it anymore (see serve.py's spoiler_ctx()),
so it can be deleted whenever you're confident this migration worked, but
there's no rush.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import spoilers, users  # noqa: E402


def main():
    if not spoilers.POLICY_PATH.exists():
        print(f"No {spoilers.POLICY_PATH} to migrate -- nothing to do.")
        return

    conn = users.init_db()
    admin = conn.execute(
        "SELECT * FROM users WHERE is_admin = 1 ORDER BY user_id LIMIT 1"
    ).fetchone()
    if admin is None:
        print("No admin account exists yet -- run `just create-admin <username>` first.", file=sys.stderr)
        sys.exit(1)

    legacy_policy = spoilers.load_policy()
    spoilers.save_user_policy(admin["user_id"], legacy_policy, conn=conn)
    conn.close()

    hf = legacy_policy["hidden_from"]
    hf_label = f"{hf['season_year']} postseason" if hf["season_type"] == 3 else f"{hf['season_year']} week {hf['week']}"
    print(
        f"Migrated {spoilers.POLICY_PATH} into {admin['username']}'s per-user policy: "
        f"{len(legacy_policy['weeks'])} week rule(s), {len(legacy_policy['games'])} game rule(s), "
        f"default hidden from {hf_label} onward."
    )


if __name__ == "__main__":
    main()
