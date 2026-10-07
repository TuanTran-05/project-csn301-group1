from conftest import _create_user
from network_copilot.auth.model import User
from network_copilot.extensions import db
from scripts import manage_users


def test_ensure_admin_promotes_an_existing_account_and_keeps_its_password(app, capsys):
    user = _create_user("g1", "Original-pass-12", "OPERATOR")
    user.is_active = False
    db.session.commit()
    version = user.token_version

    assert manage_users.cmd_ensure_admin("g1") == 0

    db.session.refresh(user)
    assert (user.role, user.is_active) == ("ADMIN", True)
    assert user.token_version == version + 1
    assert user.check_password("Original-pass-12")
    assert "password unchanged" in capsys.readouterr().out


def test_ensure_admin_is_idempotent(app, capsys):
    _create_user("g1", "Original-pass-12", "ADMIN")
    assert manage_users.cmd_ensure_admin("g1") == 0
    assert "already" in capsys.readouterr().out


def test_ensure_admin_creates_a_missing_account(app, monkeypatch):
    monkeypatch.setenv("NEW_USER_PASSWORD", "Chosen-password-1")
    assert manage_users.cmd_ensure_admin("g1") == 0
    user = db.session.query(User).filter_by(username="g1").one()
    assert user.role == "ADMIN" and user.check_password("Chosen-password-1")


def test_set_password_updates_and_revokes(app, monkeypatch):
    user = _create_user("g1", "Original-pass-12", "ADMIN")
    monkeypatch.setenv("NEW_USER_PASSWORD", "Another-password-2")
    assert manage_users.cmd_set_password("g1") == 0
    db.session.refresh(user)
    assert user.check_password("Another-password-2")
    assert user.token_version == 1
    assert manage_users.cmd_set_password("ghost") == 1


def test_list_prints_every_account(app, capsys):
    _create_user("g1", "Original-pass-12", "ADMIN")
    assert manage_users.cmd_list() == 0
    assert "g1" in capsys.readouterr().out
