"""Explain why a login answers "Invalid credentials" (works without a running server).

Usage:
    python scripts/diagnose_login.py <username>

It reads the same .env and database the service uses and reports, for that
account: which database file is open, whether the account exists, is active
and which role it has, and whether the password typed at the prompt matches
the stored hash. It also lists the latest login attempts from the audit log.

The password is asked for without echo and is never printed, logged or
stored; neither is the hash. Nothing in the database is changed.
"""

import os
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from network_copilot.app import create_app  # noqa: E402
from network_copilot.audit.model import AuditLog  # noqa: E402
from network_copilot.auth.model import User  # noqa: E402
from network_copilot.extensions import db  # noqa: E402


def database_file(uri: str) -> str:
    if uri.startswith("sqlite:///"):
        path = uri[len("sqlite:///"):]
        if os.path.exists(path):
            changed = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
            return f"{path} ({os.path.getsize(path)} bytes, modified {changed})"
        return f"{path} (DOES NOT EXIST - the app would start with an empty database)"
    return uri.split("@")[-1]  # never print a password embedded in the URL


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <username>", file=sys.stderr)
        return 1
    username = sys.argv[1]
    app = create_app()
    with app.app_context():
        print("Database:     ", database_file(app.config["SQLALCHEMY_DATABASE_URI"]))
        print("DATABASE_URL: ", "set in the environment" if os.environ.get("DATABASE_URL") else "not set (default file)")
        users = db.session.query(User).order_by(User.username).all()
        print("Accounts:     ", len(users), "-", ", ".join(
            f"{u.username}({u.role}{'' if u.is_active else ', DISABLED'})" for u in users) or "none")

        user = db.session.query(User).filter_by(username=username).one_or_none()
        if user is None:
            near = [u.username for u in users if u.username.strip().lower() == username.strip().lower()]
            print(f"\nRESULT: no account is named exactly {username!r}.")
            if near:
                print(f"        Did you mean {near}? Names are case- and space-sensitive.")
            return 2
        print(f"\nAccount:       {user.username} (id {user.id}, role {user.role}, "
              f"{'active' if user.is_active else 'DISABLED'})")
        print("Last login:   ", user.last_login_at or "never")
        print("Password set: ", user.password_changed_at or "unknown")
        print("Latest login attempts (audit log, newest first):")
        attempts = (db.session.query(AuditLog).filter(AuditLog.action == "auth.login")
                    .order_by(AuditLog.id.desc()).limit(10).all())
        for row in attempts:
            print(f"  {row.created_at}  {row.result:<8} {row.username or '-':<16} from {row.source_ip or '-'}")
        if not user.is_active:
            print("\nRESULT: the account is disabled; an ADMIN must re-activate it "
                  "(Users page, or: python scripts/manage_users.py ensure-admin <name> for an admin).")
            return 3

        password = getpass(f"Password to test for {user.username} (not shown, not stored): ")
        if user.check_password(password):
            print("\nRESULT: the password MATCHES. The service should accept this login;")
            print("        if it still answers \"Invalid credentials\", it is not using this database")
            print("        (compare the Database line with the service's DATABASE_URL / working directory),")
            print("        or the browser sends something else (autofilled name, trailing space).")
            return 0
        print("\nRESULT: the password does NOT match the stored one.")
        print(f"        Reset it with: python scripts/manage_users.py set-password {user.username}")

        return 4


if __name__ == "__main__":
    raise SystemExit(main())
