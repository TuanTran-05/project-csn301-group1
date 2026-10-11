from pathlib import Path

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text

from network_copilot.app import create_app
from network_copilot.config import TestConfig
from network_copilot.extensions import db

MIGRATIONS = str(Path(__file__).resolve().parents[2] / "migrations")


@pytest.mark.parametrize("multiple_admins", [False, True])
def test_upgrade_preserves_existing_users_and_rejects_duplicate_admins(tmp_path, multiple_admins):
    class MigrationConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + (tmp_path / "migration.db").as_posix()

    app = create_app(MigrationConfig)
    with app.app_context():
        upgrade(directory=MIGRATIONS, revision="c84b2f10a6d3")
        for identity, name, role in ((1, "admin", "ADMIN"), (2, "legacy", "ADMIN" if multiple_admins else "VIEWER")):
            db.session.execute(text("INSERT INTO users (id, username, password_hash, role, is_active, created_at) VALUES (:id, :name, 'preserved-hash', :role, 1, '2026-01-01 00:00:00')"),
                               {"id": identity, "name": name, "role": role})
        db.session.commit()
        if multiple_admins:
            with pytest.raises(SystemExit) as stopped:
                upgrade(directory=MIGRATIONS)
            assert stopped.value.code == 1
            assert "permission_grants" not in inspect(db.engine).get_table_names()
        else:
            upgrade(directory=MIGRATIONS)
            rows = db.session.execute(text("SELECT id, username, password_hash, role, token_version FROM users ORDER BY id")).all()
            assert tuple(rows[0]) == (1, "admin", "preserved-hash", "ADMIN", 1)
            assert tuple(rows[1]) == (2, "legacy", "preserved-hash", "STUDENT", 1)
            assert db.session.execute(text("SELECT COUNT(*) FROM permission_grants")).scalar() == 0
            assert db.session.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action = 'platform.authorization.migrate'")).scalar() == 1
            db.session.remove()
            downgrade(directory=MIGRATIONS, revision="c84b2f10a6d3")
            assert db.session.execute(text("SELECT COUNT(*) FROM users")).scalar() == 2
        db.session.remove()
        db.engine.dispose()
