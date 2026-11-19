"""NetAI saves a device's first baseline itself.

A change to a PNETLab device is approved only against a saved baseline. A new
user cannot guess they must press "Lưu cấu hình nền cho AI Management" on every
device first, so the baseline is saved when the device first answers on SSH
(after the console bootstrap), and before a change is previewed if still missing.
"""

import pytest

from conftest import _create_device
from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.backups.model import ConfigBackup
from network_copilot.backups.service import ensure_baseline
from network_copilot.credentials.model import DeviceCredential
from network_copilot.projects.model import Project
from network_copilot.ssh.exceptions import SSHConnectionError

PLAN = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["PC1"], "execution_mode": "config",
        "commands": ["interface GigabitEthernet0/0", "no switchport",
                     "ip address 172.20.5.163 255.255.255.240", "no shutdown"],
        "verification_commands": [],
    }],
    "explanation": "Đặt IP cho PC1.",
}


@pytest.fixture
def lab(app, db, ssh_factory):
    owner = User(username="trinh_lab", role="OPERATOR")
    owner.set_password("BaselinePassword123!")
    db.session.add(owner)
    db.session.flush()
    project = Project(name="Lab3", management_network="172.16.3.0/24", environment="pnetlab",
                      owner_id=owner.id, ai_management_enabled=True)
    db.session.add(project)
    db.session.commit()
    device = _create_device("PC1", "172.16.3.17", "access", project=project)
    device.environment, device.isolated = "pnetlab", False
    db.session.add(DeviceCredential(device_id=device.id, username="u", password_encrypted="p"))
    db.session.commit()
    fake = ssh_factory.set_client("PC1", responses={"show running-config": "hostname PC1\n!\nend"},
                                  default_output="ok")
    headers = {"Authorization": "Bearer " + issue_token(owner), "X-Project-Id": str(project.id)}
    return {"device": device, "fake": fake, "headers": headers, "factory": ssh_factory}


def _baselines(db, device):
    return db.session.query(ConfigBackup).filter_by(device_id=device.id).count()


def test_a_change_from_the_chat_applies_without_saving_a_baseline_by_hand(app, client, db, lab):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(PLAN)

    chat = client.post("/api/ai/chat", headers=lab["headers"], json={"message": "đặt IP cho PC1 trên Gi0/0"})

    assert chat.status_code == 200, chat.get_json()
    assert _baselines(db, lab["device"]) == 1
    batch_id = chat.get_json()["batch"]["id"]
    approved = client.post(f"/api/change-batches/{batch_id}/approve", headers=lab["headers"])
    assert approved.status_code == 200, approved.get_json()
    applied = client.post(f"/api/change-batches/{batch_id}/apply", headers=lab["headers"],
                          json={"confirmation": "PC1"})
    assert applied.status_code == 200, applied.get_json()
    assert any("ip address 172.20.5.163 255.255.255.240" in batch for batch in lab["fake"].config_batches)


def test_an_unchanged_baseline_is_not_stored_twice(db, lab):
    db.session.add(ConfigBackup(device_id=lab["device"].id, reason="approval_baseline",
                                running_config="hostname PC1\n!\nend"))
    db.session.commit()

    assert ensure_baseline(lab["device"]) is True
    assert _baselines(db, lab["device"]) == 1


def test_a_stale_baseline_is_refreshed_so_the_chat_change_still_applies(app, client, db, lab):
    # The device was changed outside NetAI (console, a reload, a failed
    # post-change backup): the newest saved config no longer matches it.
    # Before, the apply refused with "Running configuration changed since
    # preview" and the user had to press "Lưu cấu hình nền" by hand first.
    db.session.add(ConfigBackup(device_id=lab["device"].id, reason="approval_baseline",
                                running_config="hostname OLD\n!\nend"))
    db.session.commit()
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(PLAN)

    chat = client.post("/api/ai/chat", headers=lab["headers"], json={"message": "đặt IP cho PC1 trên Gi0/0"})

    assert chat.status_code == 200, chat.get_json()
    assert _baselines(db, lab["device"]) == 2
    batch_id = chat.get_json()["batch"]["id"]
    approved = client.post(f"/api/change-batches/{batch_id}/approve", headers=lab["headers"])
    assert approved.status_code == 200, approved.get_json()
    applied = client.post(f"/api/change-batches/{batch_id}/apply", headers=lab["headers"],
                          json={"confirmation": "PC1"})
    assert applied.status_code == 200, applied.get_json()
    assert any("ip address 172.20.5.163 255.255.255.240" in batch for batch in lab["fake"].config_batches)


def test_an_unreachable_device_keeps_its_existing_baseline(db, lab):
    db.session.add(ConfigBackup(device_id=lab["device"].id, reason="approval_baseline",
                                running_config="hostname PC1"))
    db.session.commit()
    lab["factory"].set_failing("PC1", SSHConnectionError("PC1", "timed out"))

    assert ensure_baseline(lab["device"]) is True
    assert _baselines(db, lab["device"]) == 1


def test_an_isolated_virtual_device_needs_no_baseline(db, lab):
    lab["device"].environment, lab["device"].isolated = "virtual", True
    db.session.commit()

    assert ensure_baseline(lab["device"]) is True
    assert _baselines(db, lab["device"]) == 0


def test_an_unreachable_device_still_gets_its_batch_and_a_clear_reason(app, client, db, lab):
    lab["factory"].set_failing("PC1", SSHConnectionError("PC1", "timed out"))
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(PLAN)

    chat = client.post("/api/ai/chat", headers=lab["headers"], json={"message": "đặt IP cho PC1 trên Gi0/0"})

    assert chat.status_code == 200, chat.get_json()
    assert _baselines(db, lab["device"]) == 0
    batch_id = chat.get_json()["batch"]["id"]
    client.post(f"/api/change-batches/{batch_id}/approve", headers=lab["headers"])
    applied = client.post(f"/api/change-batches/{batch_id}/apply", headers=lab["headers"],
                          json={"confirmation": "PC1"})
    assert applied.status_code != 200
    assert "cấu hình nền của PC1" in str(applied.get_json())


def test_the_console_bootstrap_saves_the_baseline_once_ssh_answers(app, db, lab, monkeypatch):
    from network_copilot.devices import service as device_service
    from network_copilot.topology import bootstrap

    monkeypatch.setattr(device_service, "check_reachability", lambda device: (True, ""))
    app.config["BOOTSTRAP_SSH_RETRY_SECONDS"] = 0

    result = bootstrap._verify(lab["device"], {"hostname": "PC1"})

    assert result["status"] == "ok" and result["baseline"] is True
    assert _baselines(db, lab["device"]) == 1
