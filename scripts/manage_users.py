"""
Account management CLI for data/users.db -- the operations that don't
belong behind a web form: bootstrapping the first admin (before any invite
exists to sign up with), minting invite codes, and emergency password
resets (there's no self-service reset flow -- see the deployment plan doc's
"No password reset" residual limitation).

Invoked via `just` recipes, not run directly day to day:
    just create-admin <username>
    just invite ["a note"]
    just reset-password <username>
    just list-invites
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import users  # noqa: E402


def _prompt_password(confirm=True):
    password = getpass.getpass("Password: ")
    if confirm and password != getpass.getpass("Confirm password: "):
        print("Passwords don't match.", file=sys.stderr)
        sys.exit(1)
    return password


def create_admin(args):
    conn = users.init_db()
    password = _prompt_password()
    try:
        user = users.create_user(conn, args.username, password, is_admin=True, invite_code=None)
    except (users.UsernameTaken, ValueError) as e:
        print(f"Could not create account: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Created admin '{user['username']}' (user_id={user['user_id']}).")


def invite(args):
    conn = users.init_db()
    # Normalize "" to None -- the `just invite` recipe always passes a note
    # argument, empty string when the caller didn't give one, and an empty
    # note isn't meaningfully different from no note at all.
    code = users.create_invite(conn, note=(args.note or None))
    print(code)


def reset_password(args):
    conn = users.init_db()
    row = users.get_user_by_username(conn, args.username)
    if row is None:
        print(f"No such user: {args.username}", file=sys.stderr)
        sys.exit(1)
    password = _prompt_password()
    try:
        users.set_password(conn, row["user_id"], password)
    except ValueError as e:
        print(f"Could not set password: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Password reset for '{args.username}' -- their existing sessions are now logged out.")


def list_invites(args):
    conn = users.init_db()
    rows = users.list_invites(conn)
    if not rows:
        print("No invites yet.")
        return
    for r in rows:
        status = f"redeemed by user_id={r['redeemed_by']} at {r['redeemed_at']}" if r["redeemed_by"] else "unredeemed"
        note = f" ({r['note']})" if r["note"] else ""
        print(f"{r['code']}{note} -- {status}")


def main():
    parser = argparse.ArgumentParser(description="Manage data/users.db accounts and invites")
    sub = parser.add_subparsers(dest="command", required=True)

    p_admin = sub.add_parser("create-admin", help="Bootstrap the first admin account (no invite needed)")
    p_admin.add_argument("username")
    p_admin.set_defaults(func=create_admin)

    p_invite = sub.add_parser("invite", help="Mint a new invite code")
    p_invite.add_argument("note", nargs="?", default=None)
    p_invite.set_defaults(func=invite)

    p_reset = sub.add_parser("reset-password", help="Reset an existing user's password")
    p_reset.add_argument("username")
    p_reset.set_defaults(func=reset_password)

    p_list = sub.add_parser("list-invites", help="List all invite codes and their redemption status")
    p_list.set_defaults(func=list_invites)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
