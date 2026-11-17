"""Problems met while doing CSN301 Lab 3 with an Operator account."""

import pytest

from conftest import _create_device
from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.ai.service import AIService
from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.commands import service as command_service
from network_copilot.credentials.model import DeviceCredential
from network_copilot.governance.redaction import redact_output
from network_copilot.projects.model import Project


@pytest.fixture
def lab(app, db, ssh_factory):
    owner = User(username="trinh_lab3", role="OPERATOR")
    owner.set_password("Lab3Password123!")
    db.session.add(owner)
    db.session.flush()
    project = Project(name="Lab3", management_network="172.16.3.0/24", environment="pnetlab",
                      owner_id=owner.id, ai_management_enabled=True)
    db.session.add(project)
    db.session.commit()
    devices = {}
    for hostname, ip in (("PC1", "172.16.3.17"), ("PC3", "172.16.3.19")):
        device = _create_device(hostname, ip, "access", project=project)
        db.session.add(DeviceCredential(device_id=device.id, username="u", password_encrypted="p"))
        devices[hostname] = device
    db.session.commit()
    headers = {"Authorization": "Bearer " + issue_token(owner), "X-Project-Id": str(project.id)}
    return {"owner": owner, "project": project, "devices": devices, "headers": headers, "ssh": ssh_factory}


# -- #1 an Operator can read the commands of a batch before approving it -----------------

def test_an_operator_reads_the_commands_of_a_batch(app, client, lab):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider({
        "intent": "configure",
        "operations": [{
            "device_hostnames": ["PC1"], "execution_mode": "config",
            "commands": ["interface GigabitEthernet0/0", "no switchport",
                         "ip address 172.20.5.163 255.255.255.240", "no shutdown", "exit",
                         "ip default-gateway 172.20.5.161"],
            "verification_commands": [],
        }],
        "explanation": "Đặt IP cho PC1.",
    })

    chat = client.post("/api/ai/chat", headers=lab["headers"],
                       json={"message": "đặt IP cho PC1 trên cổng Gi0/0 172.20.5.163/28"})

    assert chat.status_code == 200, chat.get_json()
    batch = client.get(f"/api/change-batches/{chat.get_json()['batch']['id']}", headers=lab["headers"]).get_json()
    commands = batch["changes"][0]["commands"]
    assert "ip address 172.20.5.163 255.255.255.240" in commands
    assert "ip default-gateway 172.20.5.161" in commands
    assert "***REDACTED***" not in str(commands)


def test_a_viewer_still_sees_the_commands_masked(app, client, db, lab):
    from network_copilot.projects.model import ProjectMember

    test_an_operator_reads_the_commands_of_a_batch(app, client, lab)
    viewer = User(username="lab3_viewer", role="VIEWER")
    viewer.set_password("Lab3Password123!")
    db.session.add(viewer)
    db.session.flush()
    db.session.add(ProjectMember(project_id=lab["project"].id, user_id=viewer.id, access="viewer"))
    db.session.commit()
    headers = {"Authorization": "Bearer " + issue_token(viewer), "X-Project-Id": str(lab["project"].id)}

    batches = client.get("/api/change-batches", headers=headers)

    assert batches.status_code == 200, batches.get_json()
    assert "***REDACTED***" in str(batches.get_json())          # the batch is listed, masked
    assert "ip address 172.20.5.163" not in str(batches.get_json())


def test_a_project_owner_reads_the_commands_whatever_the_role(app, db, lab):
    from network_copilot.governance.redaction import may_read_commands

    teacher = User(username="lab3_teacher", role="TEACHER")
    teacher.set_password("Lab3Password123!")
    db.session.add(teacher)
    db.session.commit()
    lab["project"].owner_id = teacher.id
    db.session.commit()

    with app.test_request_context(headers={"X-Project-Id": str(lab["project"].id)}):
        assert may_read_commands(teacher) is True
    with app.test_request_context(headers={"X-Project-Id": "999"}):
        assert may_read_commands(teacher) is False


def test_secrets_stay_hidden_from_an_operator():
    shown = redact_output(commands=True, value={
        "commands": ["interface GigabitEthernet0/0", "username g1 secret Group1Pass!",
                     "enable password Cisco123"],
        "password": "Lab-Pass-123",
    })

    assert shown["commands"][0] == "interface GigabitEthernet0/0"
    assert "Group1Pass" not in str(shown) and "Cisco123" not in str(shown)
    assert "Lab-Pass-123" not in str(shown)


# -- #2 a ping with no reply is not a success -----------------------------------------------

@pytest.mark.parametrize("output, status", [
    ("Sending 5, 100-byte ICMP Echos to 172.20.5.161\n.....\nSuccess rate is 0 percent (0/5)", "failed"),
    ("Sending 5, 100-byte ICMP Echos to 172.20.5.161\n.!!!!\nSuccess rate is 80 percent (4/5)", "success"),
    ("Sending 5, 100-byte ICMP Echos to 172.20.5.161\n!!!!!\nSuccess rate is 100 percent (5/5)", "success"),
])
def test_the_ping_status_follows_its_replies(lab, output, status):
    lab["ssh"].set_client("PC1", default_output=output)

    execution = command_service.execute_readonly(
        lab["devices"]["PC1"].id, "ping 172.20.5.161", lab["owner"].id, project_id=lab["project"].id)

    assert execution.status == status
    assert (execution.reason is not None) == (status == "failed")


def test_only_a_ping_is_judged_by_its_replies(lab):
    lab["ssh"].set_client("PC1", default_output="Success rate is 0 percent (0/5)")

    execution = command_service.execute_readonly(
        lab["devices"]["PC1"].id, "show logging", lab["owner"].id, project_id=lab["project"].id)

    assert execution.status == "success"


# -- #3 "xem cấu hình đang chạy của PC1" is a show run ---------------------------------------

@pytest.mark.parametrize("message", [
    "show run PC1",
    "show run trên PC1",
    "xem cấu hình đang chạy của PC1",
    "cho mình xem running config trên PC1",
    "Xem cấu hình PC1 giúp mình",
    "show the running config of PC1",
])
def test_asking_for_the_running_config_in_plain_words(lab, message):
    lab["ssh"].set_client("PC1", responses={"show running-config": "hostname PC1\n!\nend"})

    reply = AIService.running_config_request(message, lab["owner"].id, lab["project"].id)

    assert reply is not None and reply["device"] == "PC1"
    assert reply["results"][0]["command"] == "show running-config"


@pytest.mark.parametrize("message", [
    "xem cấu hình VLAN trên PC1",            # a narrower question: the model answers it
    "xem cấu hình interface Gi0/0 của PC1",
    "đặt IP cho PC1",
])
def test_other_requests_are_not_taken_for_a_show_run(lab, message):
    assert AIService.running_config_request(message, lab["owner"].id, lab["project"].id) is None


def test_a_show_run_without_a_device_asks_which_one(lab):
    reply = AIService.running_config_request("xem cấu hình đang chạy", lab["owner"].id, lab["project"].id)

    assert reply["intent"] == "chat" and "thiết bị nào" in reply["explanation"]
