"""Deployments on either migration branch must reach the complete schema."""

from pathlib import Path

import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash

from network_copilot.app import create_app
from network_copilot.config import TestConfig
from network_copilot.extensions import db

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


@pytest.mark.parametrize("starting_revision", ["9e54c2bd3d7e", "b2d4e6f8a0c1"])
def test_upgrade_both_branches_preserves_account_and_allows_login(tmp_path, starting_revision):
    class MigrationConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + (tmp_path / "deployment.db").as_posix()

    app = create_app(MigrationConfig)
    with app.app_context():
        upgrade(directory=MIGRATIONS, revision=starting_revision)
        values = {
            "username": "migration_student",
            "password_hash": generate_password_hash("MigrationPassword123!"),
            "role": "STUDENT",
            "is_active": True,
            "created_at": "2026-01-01 00:00:00",
        }
        columns = {column["name"] for column in inspect(db.engine).get_columns("users")}
        if "updated_at" in columns:
            values["updated_at"] = values["created_at"]
        names = ", ".join(values)
        placeholders = ", ".join(":" + name for name in values)
        db.session.execute(text(f"INSERT INTO users ({names}) VALUES ({placeholders})"), values)
        db.session.commit()

        upgrade(directory=MIGRATIONS)
        inspector = inspect(db.engine)
        for table in db.metadata.sorted_tables:
            assert inspector.has_table(table.name), table.name
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            assert set(table.columns.keys()) <= actual, table.name
        assert db.session.execute(text("SELECT password_hash FROM users WHERE username = 'migration_student'")).scalar() == values["password_hash"]
        assert db.session.execute(text("PRAGMA foreign_key_check")).all() == []
        response = app.test_client().post("/api/auth/login", json={
            "username": "migration_student", "password": "MigrationPassword123!",
        })
        assert response.status_code == 200
        assert response.json["user"]["role"] == "STUDENT"
        headers = {"Authorization": "Bearer " + response.json["access_token"]}
        assert app.test_client().get("/api/auth/me", headers=headers).status_code == 200
        db.session.remove()
        db.engine.dispose()
