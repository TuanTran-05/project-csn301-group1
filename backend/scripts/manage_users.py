"""Manage accounts from the command line (works without a running server).

Usage:
    python scripts/manage_users.py list
    python scripts/manage_users.py ensure-admin <username>
    python scripts/manage_users.py set-password <username>

``ensure-admin`` is idempotent: it creates the account when it does not exist
(the password comes from NEW_USER_PASSWORD, or is asked for) and otherwise
promotes the existing account to ADMIN and re-activates it, leaving its
password alone. This is how the first administrator is made, e.g. turning the
``g1`` account that already exists into an ADMIN.

No password is ever printed or accepted on the command line.
"""

import os
import sys
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from network_copilot.app import create_app  # noqa: E402
from network_copilot.auth.model import User  # noqa: E402
from network_copilot.auth.service import create_user, reset_password  # noqa: E402
from network_copilot.errors import AppError  # noqa: E402
from network_copilot.extensions import db  # noqa: E402


def _password(prompt: str) -> str:
    value = os.environ.get("NEW_USER_PASSWORD")
    if value:
        return value
    first = getpass(prompt)
    if getpass("Repeat: ") != first:
        raise SystemExit("ERROR: the passwords do not match.")
    return first


def cmd_list() -> int:
    for user in db.session.query(User).order_by(User.username):
        state = "active" if user.is_active else "disabled"
        print(f"{user.id:>3}  {user.username:<20} {user.role:<9} {state}")
    return 0


def cmd_ensure_admin(username: str) -> int:
    user = db.session.query(User).filter_by(username=username).one_or_none()
    if user is None:
        create_user(
            {
                "username": username,
                "password": _password(f"Password for new admin '{username}': "),
                "role": "ADMIN",
            }
        )
        print(f"created ADMIN '{username}'")
        return 0

    if user.role == "ADMIN" and user.is_active:
        print(f"'{username}' is already an active ADMIN")
        return 0
    user.role = "ADMIN"
    user.is_active = True
    user.token_version += 1  # old tokens carried the old role
    db.session.commit()
    print(f"'{username}' is now an active ADMIN (password unchanged)")
    return 0


def cmd_set_password(username: str) -> int:
    user = db.session.query(User).filter_by(username=username).one_or_none()
    if user is None:
        print(f"ERROR: no user named '{username}'.", file=sys.stderr)
        return 1
    reset_password(user, _password(f"New password for '{username}': "))
    print(f"password updated for '{username}'")
    return 0


def main(argv: list[str]) -> int:
    usage = __doc__.split("Usage:")[1].split("``ensure-admin``")[0].strip()
    if not argv:
        print("Usage:\n    " + usage, file=sys.stderr)
        return 1
    command, args = argv[0], argv[1:]
    app = create_app()
    with app.app_context():
        try:
            if command == "list" and not args:
                return cmd_list()
            if command == "ensure-admin" and len(args) == 1:
                return cmd_ensure_admin(args[0])
            if command == "set-password" and len(args) == 1:
                return cmd_set_password(args[0])
        except AppError as exc:
            print(f"ERROR: {exc.message} {exc.details or ''}", file=sys.stderr)
            return 1
    print("Usage:\n    " + usage, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
