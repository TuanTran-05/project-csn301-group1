from datetime import timedelta

import pytest

from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.changes import service as changes
from network_copilot.chat.model import ChatSession
from network_copilot.credentials.service import get_cipher
from network_copilot.governance.access import issue_grant
from network_copilot.governance.model import Lab, Policy, now
from network_copilot.extensions import db

# Reuse the same explicit Admin -> Teacher -> Student grant chain.
from .test_governance import classroom, headers, preview


def test_teacher_creates_group_and_student_without_grants(client, db):
    teacher = User(username="independent_teacher", role="TEACHER")
    teacher.set_password("TeacherPassword123!")
    db.session.add(teacher)
    db.session.commit()
    token = headers(teacher)
    group = client.post("/api/groups", headers=token, json={"name": "New class"})
    assert group.status_code == 201
    assert group.json["teacher_ids"] == [teacher.id]
    student = client.post("/api/users", headers=token, json={
        "username": "new_student", "password": "StudentPassword123!", "role": "STUDENT"})
    assert student.status_code == 201
    assert student.json["role"] == "STUDENT"
    assert client.put(f"/api/groups/{group.json['id']}/members/{student.json['id']}", headers=token).status_code == 200
    assert client.post("/api/users", headers=token, json={
        "username": "another_teacher", "password": "TeacherPassword123!", "role": "TEACHER"}).status_code == 403
    student_token = headers(db.session.get(User, student.json["id"]))
    assert client.post("/api/groups", headers=student_token, json={"name": "Unauthorized"}).status_code == 403
    assert client.post("/api/users", headers=student_token, json={
        "username": "unauthorized", "password": "StudentPassword123!"}).status_code == 403
    teacher.is_active = False
    db.session.commit()
    assert client.post("/api/groups", headers=token, json={"name": "Locked"}).status_code == 401


def test_invitation_review_suspension_and_leave(client, classroom):
    teacher, student, outsider, group, device = classroom
    teacher_token, outsider_token = headers(teacher), headers(outsider)
    invited = client.post(f"/api/groups/{group.id}/invitations", headers=teacher_token)
    assert invited.status_code == 201
    joined = client.post("/api/groups/join", headers=outsider_token, json={"code": invited.json["code"]})
    assert joined.status_code == 201
    reviewed = client.post(f"/api/join-requests/{joined.json['id']}/review", headers=teacher_token, json={"accepted": True})
    assert reviewed.status_code == 200
    grant = client.post("/api/permissions", headers=teacher_token, json={"user_id": outsider.id, "group_id": group.id, "action": "device.read"})
    assert grant.status_code == 201
    assert len(client.get("/api/devices", headers=outsider_token).json["items"]) == 1
    assert client.patch(f"/api/groups/{group.id}/members/{outsider.id}", headers=teacher_token, json={"suspended": True}).status_code == 200
    assert client.get("/api/devices", headers=outsider_token).json["items"] == []
    client.patch(f"/api/groups/{group.id}/members/{outsider.id}", headers=teacher_token, json={"suspended": False})
    assert client.delete(f"/api/groups/{group.id}/members/{outsider.id}", headers=outsider_token).status_code == 204
    assert client.get("/api/devices", headers=outsider_token).json["items"] == []


def test_group_transfer_invalidates_old_teacher_chain(client, admin_user, classroom):
    teacher, student, _, group, device = classroom
    replacement = User(username="replacement", role="TEACHER")
    replacement.set_password("StrongReplacement123!")
    db.session.add(replacement)
    db.session.commit()
    response = client.post(f"/api/groups/{group.id}/transfer", headers=headers(admin_user), json={"teacher_id": replacement.id})
    assert response.status_code == 200
    assert response.json["student_ids"] == [student.id]
    assert client.get("/api/devices", headers=headers(student)).json["items"] == []
    assert client.patch(f"/api/groups/{group.id}", headers=headers(teacher), json={"name": "Hijacked"}).status_code == 403


def test_transfer_admin_revokes_both_old_tokens(client, admin_user):
    teacher = User(username="next_admin", role="TEACHER")
    teacher.set_password("StrongPass123!")
    db.session.add(teacher)
    db.session.commit()
    old_admin, old_teacher = headers(admin_user), headers(teacher)
    denied = client.post("/api/admin/transfer", headers=old_admin, json={"user_id": teacher.id, "password": "wrong"})
    assert denied.status_code == 403
    result = client.post("/api/admin/transfer", headers=old_admin, json={"user_id": teacher.id, "password": "StrongPass123!"})
    assert result.status_code == 200
    assert result.json["role"] == "ADMIN"
    assert db.session.query(User).filter_by(role="ADMIN").count() == 1
    assert client.get("/api/users", headers=old_admin).status_code == 401
    assert client.get("/api/users", headers=old_teacher).status_code == 401


def test_reservations_exclude_overlaps_and_maintenance_blocks_admin(client, admin_user, classroom, ssh_factory):
    teacher, _, _, group, device = classroom
    start, end = (now() - timedelta(minutes=1)).isoformat() + "Z", (now() + timedelta(hours=1)).isoformat() + "Z"
    data = {"device_id": device.id, "group_id": group.id, "starts_at": start, "ends_at": end}
    token = headers(teacher)
    reserved = client.post("/api/reservations", headers=token, json=data)
    assert reserved.status_code == 201
    assert client.post("/api/reservations", headers=token, json=data).status_code == 409
    assert client.delete(f"/api/reservations/{reserved.json['id']}", headers=token).status_code == 204
    data["maintenance"] = True
    assert client.post("/api/reservations", headers=headers(admin_user), json=data).status_code == 201
    change = preview(teacher, device, group)
    changes.approve(change.id, admin_user.id)
    result = client.post(f"/api/changes/{change.id}/apply", headers=headers(admin_user))
    assert result.status_code == 403
    assert not ssh_factory.clients


def test_ai_quota_and_chat_scope(app, client, admin_user, classroom):
    from fakes.fake_ai_provider import FakeAIProvider
    from network_copilot.chat.model import ChatMessage
    from network_copilot.projects.model import Project

    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider({"intent": "chat", "operations": [], "explanation": "Hi"})

    teacher, student, outsider, group, device = classroom
    issue_grant(admin_user, {"user_id": student.id, "action": "ai.use"})
    db.session.add(Policy(id=1, ai_daily_limit=1))
    elsewhere = Project(name="Outsider lab", management_network="10.77.0.0/24", environment="pnetlab",
                        owner_id=outsider.id)
    db.session.add(elsewhere)
    db.session.flush()
    foreign = ChatSession(created_by_id=outsider.id, project_id=elsewhere.id)
    db.session.add(foreign)
    db.session.flush()
    db.session.add(ChatMessage(session_id=foreign.id, role="user", content="outsider secret question"))
    db.session.commit()
    token = headers(student)
    # Someone else's session id never opens it: the caller gets their own session.
    read = client.get(f"/api/chat/messages?session_id={foreign.id}", headers=token)
    assert "outsider secret question" not in read.get_data(as_text=True)
    assert all(item["id"] != foreign.id for item in client.get("/api/chat/sessions", headers=token).json["items"])
    client.post("/api/ai/chat", headers=token, json={"message": "Hi", "session_id": foreign.id})
    assert db.session.query(ChatMessage).filter_by(session_id=foreign.id).count() == 1
    second = client.post("/api/ai/chat", headers=token, json={"message": "Hi"})
    assert second.status_code == 429


def test_lab_provider_is_required_and_deadline_is_passed(app, client, admin_user, classroom):
    teacher, student, outsider, group, _ = classroom
    for action in ("lab.create", "lab.template", "lab.start", "lab.stop", "lab.reset"):
        issue_grant(admin_user, {"user_id": teacher.id, "action": action})
    issue_grant(admin_user, {"user_id": student.id, "action": "lab.start", "group_id": group.id})
    db.session.commit()
    template = client.post("/api/lab-templates", headers=headers(admin_user), json={"name": "Isolated VLAN", "topology": {"nodes": []}, "allowed_teacher_ids": [teacher.id]})
    assert template.status_code == 201
    data = {"template_id": template.json["id"], "group_id": group.id, "owner_id": student.id}
    assert client.post("/api/labs", headers=headers(teacher), json=data).status_code == 503
    assert db.session.query(Lab).count() == 0

    class Provider:
        def provision(self, topology, *, deadline, idempotency_key):
            assert deadline > now()
            return "external-lab-1"
        def operate(self, external_id, action, *, deadline):
            assert external_id == "external-lab-1"

    app.config["LAB_DRIVER"] = Provider()
    created = client.post("/api/labs", headers=headers(teacher), json=data)
    assert created.status_code == 201
    lab_id = created.json["id"]
    assert client.post(f"/api/labs/{lab_id}/start", headers=headers(outsider)).status_code == 403
    assert client.post(f"/api/labs/{lab_id}/start", headers=headers(student)).json["status"] == "running"
    lab = db.session.get(Lab, lab_id)
    lab.expires_at = now() - timedelta(seconds=1)
    db.session.commit()
    assert client.post(f"/api/labs/{lab_id}/start", headers=headers(student)).status_code == 403


def test_physical_device_requires_baseline_and_rejects_drift(client, admin_user, classroom, ssh_factory):
    _, _, _, group, device = classroom
    device.environment = "physical_lab"
    db.session.commit()
    fake = ssh_factory.set_client(device.hostname, responses={"show running-config": "hostname ORIGINAL"})
    token = headers(admin_user)
    assert client.post(f"/api/devices/{device.id}/baseline", headers=token).status_code == 201
    change = changes.create_preview(admin_user.id, device.id, ["vlan 50"], group_id=group.id)
    changes.approve(change.id, admin_user.id)
    fake.responses["show running-config"] = "hostname CHANGED"
    result = client.post(f"/api/changes/{change.id}/apply", headers=token)
    assert result.status_code == 200
    assert result.json["status"] == "failed"
    assert not any(call[0] == "run_config" for call in fake.calls)


def test_ai_templates_are_admin_managed_and_reject_credentials(client, admin_user, classroom):
    teacher, _, _, _, _ = classroom
    data = {"name": "VLAN", "instructions": "Generate a VLAN", "commands": ["vlan 50"]}
    assert client.post("/api/ai-templates", headers=headers(teacher), json=data).status_code == 403
    created = client.post("/api/ai-templates", headers=headers(admin_user), json=data)
    assert created.status_code == 201
    rejected = client.patch(f"/api/ai-templates/{created.json['id']}", headers=headers(admin_user), json={"commands": ["enable secret 5 do-not-store"]})
    assert rejected.status_code == 422
    assert client.get("/api/ai-templates", headers=headers(admin_user)).json["items"][0]["commands"] == ["vlan 50"]


def test_device_credentials_are_encrypted_and_admin_only(client, admin_user, classroom):
    from network_copilot.credentials.model import DeviceCredential
    from network_copilot.audit.model import AuditLog
    _, student, _, _, device = classroom
    data = {"username": "netadmin", "password": "DeviceSecret123!", "enable_secret": "EnableSecret123!"}
    url = f"/api/devices/{device.id}/credentials"
    assert client.put(url, headers=headers(student), json=data).status_code == 403
    result = client.put(url, headers=headers(admin_user), json=data)
    assert result.status_code == 200
    assert "DeviceSecret" not in result.get_data(as_text=True)
    stored = db.session.query(DeviceCredential).filter_by(device_id=device.id).one()
    assert "DeviceSecret" not in stored.password_encrypted
    assert get_cipher().decrypt(stored.password_encrypted) == data["password"]
    assert "DeviceSecret" not in str([e.to_dict() for e in db.session.query(AuditLog)])


def test_restore_preview_keeps_approval_and_permission_requirements(client, admin_user, classroom):
    teacher, student, _, group, device = classroom
    original = preview(student, device, group)
    original.status = "failed"
    device.recovery_instructions = "Review the previous VLAN config and restore it via a new approved change."
    db.session.commit()
    url = f"/api/changes/{original.id}/restore-preview"
    data = {"commands": ["vlan 50"], "description": "Restore the reviewed VLAN definition."}
    assert client.post(url, headers=headers(teacher), json=data).status_code == 403
    issue_grant(admin_user, {"user_id": teacher.id, "group_id": group.id, "action": "config.restore"})
    db.session.commit()
    result = client.post(url, headers=headers(teacher), json=data)
    assert result.status_code == 201
    assert result.json["risk_level"] == "high"
    assert result.json["status"] == "pending_approval"
    assert result.json["requires_confirmation"]
    assert client.post(f"/api/changes/{result.json['id']}/approve", headers=headers(teacher)).status_code == 403


def test_private_key_block_never_leaks():
    from network_copilot.governance.redaction import redact_output
    output = redact_output("-----BEGIN RSA PRIVATE KEY-----\nVerySecretBase64\n-----END RSA PRIVATE KEY-----\ninterface Gi0/1")
    assert "VerySecretBase64" not in output
    assert "interface Gi0/1" in output
