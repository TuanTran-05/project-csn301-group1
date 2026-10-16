from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.changes import service as changes
from network_copilot.changes import batch_service
from network_copilot.changes.batch_service import BatchOperation
from network_copilot.errors import AppError, ConflictError, ForbiddenError, InvalidStateError
from network_copilot.groups.model import Group
from network_copilot.governance.access import issue_grant, permitted
from network_copilot.governance.model import Grant, Policy, Suspension, DeviceLease, now
from network_copilot.governance.workflow import claim, risk


@pytest.fixture
def classroom(db, admin_user, access_switch):
    teacher = User(username="teacher", role="TEACHER")
    student = User(username="student", role="STUDENT")
    outsider = User(username="outsider", role="STUDENT")
    for user in (teacher, student, outsider):
        user.set_password("ClassroomPass123!")
        db.session.add(user)
    group = Group(name="Class A", teachers=[teacher], students=[student], devices=[access_switch], is_auto_approve=True)
    access_switch.environment = "virtual"
    access_switch.isolated = True
    db.session.add(group)
    db.session.commit()
    for action in ("device.read", "device.observe", "config.create", "config.validate", "config.simulate", "config.approve", "config.deploy", "assignment.manage", "device.reserve"):
        parent = issue_grant(admin_user, {"user_id": teacher.id, "group_id": group.id, "action": action, "delegable": True})
        db.session.commit()
        if action not in {"config.approve", "assignment.manage"}:
            issue_grant(teacher, {"user_id": student.id, "group_id": group.id, "action": action})
            db.session.commit()
    return teacher, student, outsider, group, access_switch


def headers(user):
    return {"Authorization": "Bearer " + issue_token(user)}


def preview(user, device, group):
    return changes.create_preview(user.id, device.id, ["vlan 50", "name STUDENT"], group_id=group.id)


def test_scope_cannot_be_combined(db, classroom, admin_user, make_device):
    teacher, student, outsider, group, device = classroom
    other_device = make_device("OTHER-SW", "172.16.3.199", "access")
    other = Group(name="Class B", students=[student], teachers=[teacher], devices=[other_device])
    db.session.add(other)
    db.session.commit()
    assert permitted(student, "config.deploy", group.id, device)
    assert not permitted(student, "config.deploy", other.id, other_device)
    assert not permitted(student, "config.deploy", group.id, other_device)
    assert not permitted(outsider, "config.deploy", group.id, device)


def test_revocation_invalidates_delegated_children(db, classroom):
    teacher, student, _, group, device = classroom
    parent = db.session.query(Grant).filter_by(user_id=teacher.id, action="config.deploy").one()
    parent.revoked = True
    db.session.commit()
    assert not permitted(student, "config.deploy", group.id, device)


@pytest.mark.parametrize("mutation", ["expiry", "leave", "suspend", "lock", "archive", "deallocate"])
def test_revoke_before_apply_never_opens_ssh(db, classroom, ssh_factory, mutation):
    teacher, student, _, group, device = classroom
    change = preview(student, device, group)
    changes.approve(change.id, teacher.id)
    if mutation == "expiry":
        grant = db.session.query(Grant).filter_by(user_id=student.id, action="config.deploy").one()
        grant.expires_at = now() - timedelta(seconds=1)
    elif mutation == "leave":
        group.students.remove(student)
    elif mutation == "suspend":
        db.session.add(Suspension(group_id=group.id, user_id=student.id))
    elif mutation == "lock":
        student.is_active = False
    elif mutation == "archive":
        group.archived = True
    else:
        group.devices.remove(device)
    db.session.commit()
    with pytest.raises(ForbiddenError):
        changes.apply(change.id, student.id)
    assert not ssh_factory.clients


def test_teacher_cannot_self_approve_or_delegate_approval(db, classroom):
    teacher, student, _, group, device = classroom
    change = preview(teacher, device, group)
    with pytest.raises(ForbiddenError):
        changes.approve(change.id, teacher.id)
    with pytest.raises(ForbiddenError):
        issue_grant(teacher, {"user_id": student.id, "action": "config.approve", "group_id": group.id})


def test_delegation_cannot_widen_scope_or_expiry(db, classroom):
    teacher, student, _, group, device = classroom
    parent = db.session.query(Grant).filter_by(user_id=teacher.id, action="config.deploy").one()
    parent.device_id = device.id
    parent.environment = "virtual"
    parent.expires_at = now() + timedelta(hours=1)
    db.session.commit()
    for data in ({}, {"device_id": device.id}, {"device_id": device.id, "environment": "virtual", "expires_at": (now() + timedelta(hours=2)).isoformat() + "Z"}):
        with pytest.raises(ForbiddenError):
            issue_grant(teacher, {"user_id": student.id, "group_id": group.id, "action": "config.deploy", **data})


@pytest.mark.parametrize("tamper", ["commands", "baseline", "environment", "policy", "expiry"])
def test_frozen_approval_rejects_tampering(db, classroom, ssh_factory, tamper):
    teacher, student, _, group, device = classroom
    change = preview(student, device, group)
    changes.approve(change.id, teacher.id)
    if tamper == "commands":
        change.commands = ["configure terminal", "vlan 60", "end"]
    elif tamper == "baseline":
        device.baseline_revision += 1
    elif tamper == "environment":
        device.environment = "production"
    elif tamper == "policy":
        db.session.add(Policy(id=1, independent_approval=False))
    else:
        change.approved_at = now() - timedelta(hours=2)
    db.session.commit()
    with pytest.raises(InvalidStateError):
        changes.apply(change.id, student.id)
    assert not ssh_factory.clients


def test_explicit_deploy_permission_required_even_with_auto_approval(db, classroom, ssh_factory):
    _, student, _, group, device = classroom
    change = preview(student, device, group)
    grant = db.session.query(Grant).filter_by(user_id=student.id, action="config.deploy").one()
    grant.revoked = True
    db.session.commit()
    with pytest.raises(ForbiddenError):
        changes.apply(change.id, student.id)
    assert not ssh_factory.clients


def test_student_can_deploy_teacher_approved_configuration(db, classroom, ssh_factory):
    teacher, student, _, group, device = classroom
    change = preview(student, device, group)
    changes.approve(change.id, teacher.id)
    result = changes.apply(change.id, student.id)
    assert result.status in {"success", "failed"}  # Verification is real, never forced to pass.
    assert device.hostname in ssh_factory.clients
    assert db.session.get(DeviceLease, device.id) is None
    assert device.baseline_revision == 1


def test_device_lease_blocks_second_change(db, classroom):
    teacher, student, _, group, device = classroom
    first, second = preview(student, device, group), preview(student, device, group)
    changes.approve(first.id, teacher.id)
    changes.approve(second.id, teacher.id)
    claim(first)
    with pytest.raises(ConflictError):
        claim(second)
    assert db.session.get(DeviceLease, device.id).change_id == first.id


def test_batch_service_cannot_bypass_student_approval(db, classroom):
    _, student, _, _, device = classroom
    batch = batch_service.create_batch_preview(student.id, [BatchOperation([device.hostname], "config", ["vlan 50"], [])], None)
    with pytest.raises(ForbiddenError):
        batch_service.approve_batch(batch.id, student.id)


def test_expired_role_token_and_locked_account_are_denied(db, client, classroom):
    teacher, student, _, _, _ = classroom
    token = headers(teacher)
    teacher.is_active = False
    db.session.commit()
    assert client.get("/api/auth/me", headers=token).status_code == 401
    token = headers(student)
    student.token_version += 1
    db.session.commit()
    assert client.get("/api/devices", headers=token).status_code == 401


def test_cross_group_reads_hidden(client, classroom):
    teacher, student, outsider, group, device = classroom
    change = preview(student, device, group)
    token = headers(outsider)
    assert client.get("/api/devices", headers=token).json["items"] == []
    assert client.get("/api/changes", headers=token).json["items"] == []
    assert client.get(f"/api/changes/{change.id}", headers=token).status_code == 403
    assert client.get(f"/api/devices/{device.id}/status", headers=token).status_code == 403


def test_admin_singleton_and_cannot_disable(db, client, admin_user):
    another = User(username="secondadmin", role="ADMIN")
    another.set_password("StrongPass123!")
    db.session.add(another)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    result = client.patch(f"/api/users/{admin_user.id}", headers=headers(admin_user), json={"is_active": False})
    assert result.status_code == 403


def test_admin_login_does_not_require_mfa(app, client, admin_user):
    app.config["ADMIN_MFA_REQUIRED"] = True  # Legacy config cannot re-enable enrollment.
    response = client.post("/api/auth/login", json={"username": "admin", "password": "StrongPass123!"})
    assert response.status_code == 200
    token = {"Authorization": "Bearer " + response.json["access_token"]}
    assert response.json["mfa_setup_required"] is False
    assert client.get("/api/users", headers=token).status_code == 200


def test_environments_and_risk(classroom):
    _, _, _, _, device = classroom
    assert risk(["vlan 50", "name LAB"], device) == "low"
    assert risk(["router ospf 1"], device) == "high"
    device.isolated = False
    assert risk(["vlan 50"], device) == "high"
    device.environment = "production"
    assert risk(["vlan 50"], device) == "high"


@pytest.mark.parametrize("commands,expected", [
    (["hostname LAB-SW"], "low"),
    ([" configure   terminal ", "vlan 50", "name LAB", " END "], "low"),
    (["interface GigabitEthernet0/1", "description Student port", "switchport mode access", "switchport access vlan 50"], "low"),
    (["vlan 50", "ip route 0.0.0.0 0.0.0.0 10.0.0.1"], "high"),
    (["access-list 10 permit any"], "high"),
    (["interface vlan 50", "ip address 10.0.0.1 255.255.255.0"], "high"),
    (["shutdown"], "high"),
    (["no vlan 50"], "high"),
    (["switchport mode trunk"], "high"),
    (["username admin secret example"], "high"),
    (["unrecognized command"], "high"),
])
def test_automatic_command_risk(classroom, commands, expected):
    assert risk(commands, classroom[-1]) == expected


@pytest.mark.parametrize("device_role", ["core", "distribution", "isp", "firewall"])
def test_critical_device_risk_is_always_high(classroom, device_role):
    device = classroom[-1]
    device.role = device_role
    assert risk(["hostname LAB"], device) == "high"


def test_draft_versions_and_submission_window(db, client, classroom):
    teacher, student, outsider, group, device = classroom
    student_headers = headers(student)
    result = client.post("/api/drafts", headers=student_headers,
                         json={"group_id": group.id, "device_id": device.id, "title": "VLAN exercise", "commands": ["vlan 50"]})
    assert result.status_code == 201
    draft_id, version_id = result.json["draft"]["id"], result.json["version"]["id"]
    version2 = client.post(f"/api/drafts/{draft_id}/versions", headers=student_headers, json={"commands": ["vlan 60"]})
    assert version2.status_code == 201
    diff = client.get(f"/api/drafts/{draft_id}/diff?from={version_id}&to={version2.json['id']}", headers=student_headers)
    assert "-vlan 50" in diff.json["diff"]
    assert client.get(f"/api/drafts/{draft_id}/versions", headers=headers(outsider)).status_code == 403
    assignment = client.post(f"/api/groups/{group.id}/assignments", headers=headers(teacher), json={
        "device_id": device.id, "title": "Create VLAN", "instructions": "Create VLAN 50", "due_at": (now() + timedelta(hours=1)).isoformat() + "Z", "allowed_prefixes": ["vlan"]})
    assert assignment.status_code == 201
    submission = client.post(f"/api/assignments/{assignment.json['id']}/submissions", headers=student_headers, json={"version_id": version_id})
    assert submission.status_code == 201
    graded = client.patch(f"/api/submissions/{submission.json['id']}/grade", headers=headers(teacher), json={"score": 95, "feedback": "Correct."})
    assert graded.status_code == 200
    assert client.post(f"/api/drafts/{draft_id}/simulate", headers=student_headers, json={"version_id": version_id}).status_code == 503


def test_secret_lines_redacted():
    from network_copilot.governance.redaction import redact_output
    output = redact_output("hostname ROUTER\nusername admin secret 5 hash123\nsnmp-server community public RO")
    assert "hash123" not in output and "public" not in output
    assert "hostname ROUTER" in output


def test_device_with_permission_history_cannot_be_deleted(db, classroom, admin_user):
    from network_copilot.devices.service import delete_device
    _, _, outsider, group, device = classroom
    group.students.append(outsider)
    db.session.commit()
    grant = issue_grant(admin_user, {"user_id": outsider.id, "group_id": group.id,
                                      "device_id": device.id, "action": "device.read"})
    db.session.commit()
    old_id = device.id
    assert permitted(outsider, "device.read", group.id, device)
    with pytest.raises(ConflictError, match="retain it for audit"):
        delete_device(old_id)
    from network_copilot.devices.model import Device
    assert db.session.get(Device, old_id) is device
    assert grant.device_id == old_id


def test_group_grants_do_not_cover_unscoped_admin_changes(client, classroom, admin_user):
    teacher, _, _, _, device = classroom
    change = changes.create_preview(admin_user.id, device.id, ["vlan 50"])
    assert change.group_id is None
    assert client.get(f"/api/changes/{change.id}", headers=headers(teacher)).status_code == 403
    with pytest.raises(ForbiddenError, match="explicit group"):
        changes.approve(change.id, teacher.id)
    changes.approve(change.id, admin_user.id)
    with pytest.raises(ForbiddenError, match="explicit group"):
        changes.apply(change.id, teacher.id)
