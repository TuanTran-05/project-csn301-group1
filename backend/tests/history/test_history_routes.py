"""Real Flask requests against isolated test DB; history must never invoke SSH."""
from network_copilot.app import create_app
from network_copilot.config import TestConfig
from network_copilot.changes.model import ChangeRequest, ChangeBatch
from network_copilot.history_extension.routes import init_app


BASE = "/api/draft-history"


def push(client, headers, snapshot, query=""):
    response = client.post(BASE + "/states" + query, headers=headers, json={
        "snapshot": snapshot, "action_type": "CONFIG_INT", "description": "Edit interface draft",
        "actor": "forged-client-actor",
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_registration_is_idempotent_and_preserves_history(app, client, admin_headers):
    push(client, admin_headers, {"interfaces": {"Gi0/0": {"description": "original"}}})
    before = [r.rule for r in app.url_map.iter_rules() if r.rule.startswith(BASE)]
    init_app(app)
    after = [r.rule for r in app.url_map.iter_rules() if r.rule.startswith(BASE)]
    assert before == after
    assert len(before) == 6
    assert client.get(BASE, headers=admin_headers).get_json()["snapshot"] is not None


def test_snapshots_undo_redo_branch_backup_restore_without_core_writes(client, admin_headers, ssh_factory, db):
    first = {"interfaces": {"Gi0/0": {"description": "before", "enabled": True}}}
    second = {"interfaces": {"Gi0/0": {"description": "after", "enabled": False}}}
    empty = client.get(BASE, headers=admin_headers).get_json()
    assert empty == {"snapshot": None, "diff": [], "can_undo": False, "can_redo": False}
    assert client.post(BASE + "/undo", headers=admin_headers).status_code == 409
    assert not push(client, admin_headers, first)["can_undo"]
    assert push(client, admin_headers, second)["snapshot"] == second
    undone = client.post(BASE + "/undo", headers=admin_headers).get_json()
    assert undone["snapshot"] == first
    assert undone["can_redo"] and not undone["can_undo"]
    backup_response = client.get(BASE + "/backup", headers=admin_headers)
    assert "attachment" in backup_response.headers["Content-Disposition"]
    backup = backup_response.get_json()
    assert [e["action_type"] for e in backup["audit_trail"]] == ["CONFIG_INT", "CONFIG_INT", "UNDO"]
    assert all(e["actor"] == "actor-1" for e in backup["audit_trail"])
    from network_copilot.history_extension.protection import cipher_for, decode_backup
    assert all(e["actor"] == "admin" for e in decode_backup(backup, cipher_for())["audit_trail"])
    assert client.post(BASE + "/redo", headers=admin_headers).get_json()["snapshot"] == second
    restored = client.post(BASE + "/restore", headers=admin_headers, json=backup).get_json()
    assert restored["snapshot"] == first and restored["can_redo"]
    assert client.post(BASE + "/redo", headers=admin_headers).get_json()["snapshot"] == second
    client.post(BASE + "/undo", headers=admin_headers)
    assert not push(client, admin_headers, {"interfaces": {}})["can_redo"]
    assert db.session.query(ChangeRequest).count() == 0
    assert db.session.query(ChangeBatch).count() == 0
    assert ssh_factory.clients == {}


def test_user_and_topology_isolation(client, admin_headers, app, db):
    from network_copilot.auth.model import User
    from network_copilot.auth.service import issue_token
    from sqlalchemy import text
    # Two administrator identities (as before and after an admin transfer):
    # lift the single-admin index in this test database only.
    db.session.execute(text("DROP INDEX uq_single_admin"))
    other = User(username="other-history-admin", role="ADMIN")
    other.set_password("OtherHistoryPass123!")
    db.session.add(other)
    db.session.commit()
    other_headers = {"Authorization": "Bearer " + issue_token(other)}
    push(client, admin_headers, {"real_draft": 1}, "?topology_id=lab-a")
    assert client.get(BASE, headers=admin_headers).get_json()["snapshot"] is None
    assert client.get(BASE + "?topology_id=lab-a", headers=other_headers).get_json()["snapshot"] is None
    # Distinct factory instances also own distinct storage.
    second_app = create_app(TestConfig)
    from flask_jwt_extended import create_access_token
    with second_app.app_context():
        from network_copilot.extensions import db as second_db
        second_db.create_all()
        user = User(username="admin", role="ADMIN")
        user.set_password("SecondHistoryPass123!")
        second_db.session.add(user)
        second_db.session.commit()
        token = create_access_token(identity=str(user.id), additional_claims={"role": "ADMIN", "username": "admin"})
        result = second_app.test_client().get(BASE + "?topology_id=lab-a", headers={"Authorization": "Bearer " + token})
        assert result.get_json()["snapshot"] is None
        second_db.session.remove()
        second_db.drop_all()


def test_authentication_and_active_role(client, admin_headers, viewer_headers, admin_user, db):
    assert client.get(BASE).status_code == 401
    for endpoint in (BASE, BASE + "/backup"):
        assert client.get(endpoint, headers=viewer_headers).status_code == 403
    for suffix in ("/states", "/undo", "/redo", "/restore"):
        assert client.post(BASE + suffix, headers=viewer_headers, json={}).status_code == 403
    # The role is read live, not from the token. (The sole Administrator can
    # never be deactivated, so demote the account behind the token's back.)
    admin_user.role = "TEACHER"
    db.session.commit()
    assert client.get(BASE, headers=admin_headers).status_code == 403


def test_invalid_backup_does_not_change_snapshot(client, admin_headers):
    push(client, admin_headers, {"interfaces": {"Gi0/0": {"enabled": True}}})
    before = client.get(BASE, headers=admin_headers).get_json()
    for value in ({}, {"backup_version": "2.0"}, []):
        response = client.post(BASE + "/restore", headers=admin_headers, json=value)
        assert response.status_code == 400
        assert client.get(BASE, headers=admin_headers).get_json() == before
    response = client.post(BASE + "/states", headers=admin_headers, data="{", content_type="application/json")
    assert response.status_code == 400


def test_history_does_not_modify_existing_pending_preview(client, admin_headers, access_switch, ssh_factory):
    preview = client.post("/api/changes/preview", headers=admin_headers, json={
        "device_id": access_switch.id,
        "commands": ["configure terminal", "interface GigabitEthernet0/2", "description ORIGINAL", "end"],
        "description": "Existing pending preview",
    })
    assert preview.status_code == 201
    original = preview.get_json()
    push(client, admin_headers, {"interfaces": {"Gi0/2": {"description": "ORIGINAL"}}})
    push(client, admin_headers, {"interfaces": {"Gi0/2": {"description": "DRAFT_ONLY"}}})
    client.post(BASE + "/undo", headers=admin_headers)
    client.post(BASE + "/redo", headers=admin_headers)
    current = client.get(f"/api/changes/{original['id']}", headers=admin_headers).get_json()
    assert current == original
    assert current["status"] == "pending_approval"
    assert ssh_factory.clients == {}


def test_index_includes_toolbar_and_preserves_pending_work(client):
    html = client.get("/").get_data(as_text=True)
    assert 'class="sidebar changes-panel" x-data="historyDraft"' in html
    assert html.count('data-history-action="undo"') == 1
    assert html.count('data-history-action="redo"') == 1
    assert "pendingChanges" in html and "pendingBatches" in html
    assert "approveChange(change.id)" in html and "applyBatch(batch.id)" in html
    assert html.index("js/history-panel.js") < html.index("vendor/alpine.min.js")
    assert "css/history-toolbar.css" in html
