import pytest

from network_copilot.auth.model import Role, User
from network_copilot.changes import service as change_service
from network_copilot.errors import ForbiddenError
from network_copilot.extensions import db
from network_copilot.groups.model import Group
from network_copilot.governance.access import issue_grant
from network_copilot.projects.model import Project, ProjectMember


@pytest.fixture(autouse=True)
def classroom_administrator(admin_user):
    return admin_user


def _user(username, role):
    user = User(username=username, role=role)
    user.set_password("StrongPass123!")
    db.session.add(user)
    db.session.commit()
    return user


def _group_with_student_and_device(
    student, device, *, auto_approve=False, teacher=None
):
    group = Group(name="lab-group", is_auto_approve=auto_approve)
    group.students.append(student)
    group.devices.append(device)
    if teacher is not None:
        group.teachers.append(teacher)
    db.session.add(group)
    db.session.commit()
    admin = db.session.query(User).filter_by(role=Role.ADMIN).one()
    for member in [student] + ([teacher] if teacher else []):
        actions = ["device.read", "config.create", "config.deploy"]
        if member.role == Role.TEACHER:
            actions.append("config.approve")
        for action in actions:
            issue_grant(admin, {"user_id": member.id, "group_id": group.id, "action": action})
    db.session.commit()
    return group


def test_student_low_risk_change_is_auto_approved_and_applied(
    app, client, access_switch, ssh_factory
):
    student = _user("student", Role.STUDENT)
    _group_with_student_and_device(student, access_switch, auto_approve=True)
    # The default lab is view-only for everyone but Admin, so the exercise
    # runs in a classroom project the student can edit.
    classroom = Project(name="Classroom", management_network="172.16.3.0/24",
                        environment="pnetlab")
    db.session.add(classroom)
    db.session.flush()
    access_switch.project_id = classroom.id
    db.session.add(ProjectMember(project_id=classroom.id, user_id=student.id,
                                 access="editor"))
    db.session.commit()
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACCESS-SW\n",
            "show clock": "12:00:00 UTC",
        },
        config_output="",
    )
    headers = {
        "X-Project-Id": str(classroom.id),
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login",
            json={"username": "student", "password": "StrongPass123!"},
        ).get_json()["access_token"]
    }

    preview = client.post(
        "/api/changes/preview",
        headers=headers,
        json={
            "device_id": access_switch.id,
            "commands": ["hostname STUDENT-LAB"],
            "verification_commands": ["show clock"],
        },
    )
    assert preview.status_code == 201, preview.get_json()
    change_id = preview.get_json()["id"]

    response = client.post(f"/api/changes/{change_id}/apply", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
    assert response.get_json()["approved_by_id"] is None
    assert [call[0] for call in ssh_factory.get(access_switch.hostname).calls] == [
        "run_show",
        "run_config",
        "run_show",
        "run_show",
    ]


def test_student_cannot_auto_approve_when_group_setting_is_off(
    app, access_switch
):
    student = _user("student", Role.STUDENT)
    _group_with_student_and_device(student, access_switch)
    change = change_service.create_preview(
        student.id, access_switch.id, ["hostname STUDENT-LAB"]
    )

    with pytest.raises(ForbiddenError, match="Teacher approval"):
        change_service.apply(change.id, student.id)

    assert change.status == "pending_approval"


def test_student_high_risk_change_requires_teacher_approval_without_ssh(
    app, access_switch, ssh_factory
):
    student = _user("student", Role.STUDENT)
    _group_with_student_and_device(student, access_switch, auto_approve=True)
    change = change_service.create_preview(
        student.id,
        access_switch.id,
        ["write erase"],
        execution_mode="exec",
        description="Reset my assigned isolated lab for the exercise.",
    )

    with pytest.raises(ForbiddenError, match="Teacher approval"):
        change_service.apply(change.id, student.id)

    assert change.status == "pending_approval"
    assert ssh_factory.clients == {}


def test_teacher_can_approve_and_apply_change_in_managed_group(
    app, access_switch, ssh_factory
):
    teacher = _user("teacher", Role.TEACHER)
    student = _user("student", Role.STUDENT)
    _group_with_student_and_device(
        student, access_switch, teacher=teacher
    )
    change = change_service.create_preview(
        student.id,
        access_switch.id,
        ["hostname STUDENT-LAB"],
        verification_commands=["show clock"],
    )
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACCESS-SW\n",
            "show clock": "12:00:00 UTC",
        },
    )

    approved = change_service.approve(change.id, teacher.id)
    assert approved.status == "approved"
    applied = change_service.apply(change.id, teacher.id)
    assert applied.status == "success"


def test_teacher_can_toggle_auto_approval_for_managed_group(
    app, client, access_switch
):
    teacher = _user("teacher", Role.TEACHER)
    group = Group(name="teacher-lab")
    group.teachers.append(teacher)
    group.devices.append(access_switch)
    db.session.add(group)
    db.session.commit()
    admin = db.session.query(User).filter_by(role=Role.ADMIN).one()
    issue_grant(admin, {"user_id": teacher.id, "group_id": group.id, "action": "config.approve"})
    db.session.commit()
    token = client.post(
        "/api/auth/login",
        json={"username": "teacher", "password": "StrongPass123!"},
    ).get_json()["access_token"]

    response = client.patch(
        f"/api/groups/{group.id}/auto-approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"is_auto_approve": True},
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["is_auto_approve"] is True
