"""Problems met while preparing the NetAI design-and-push demo (small lab)."""

import pytest

from conftest import _create_device
from network_copilot.ai.service import AIService
from network_copilot.auth.model import User
from network_copilot.changes import service as change_service
from network_copilot.commands.policy import ai_policy, default_policy
from network_copilot.credentials.model import DeviceCredential
from network_copilot.projects.model import Project


@pytest.fixture
def lab(app, db, ssh_factory):
    owner = User(username="trinh_demo", role="OPERATOR")
    owner.set_password("DemoPassword123!")
    db.session.add(owner)
    db.session.flush()
    project = Project(name="Demo NetAI 2", management_network="172.16.3.0/24", environment="pnetlab",
                      owner_id=owner.id, ai_management_enabled=True)
    db.session.add(project)
    db.session.commit()
    devices = {}
    for hostname, ip, role in (("EIU-R1", "172.16.3.45", "core"), ("EIU-S1", "172.16.3.46", "access"),
                               ("PC1", "172.16.3.47", "access")):
        device = _create_device(hostname, ip, role, project=project)
        device.environment = "pnetlab"
        db.session.add(DeviceCredential(device_id=device.id, username="u", password_encrypted="p"))
        devices[hostname] = device
    db.session.commit()
    return {"owner": owner, "project": project, "devices": devices, "ssh": ssh_factory}


# -- #14 "show interfaces description" -------------------------------------------------

def test_show_interfaces_description_is_a_read_only_command():
    assert default_policy.evaluate("show interfaces description", "access").allowed
    assert ai_policy.evaluate("show interfaces description", "core").allowed


# -- #13 one interface of the running config, typed by the user ---------------------------

def test_one_interface_of_the_running_config_is_never_for_the_model():
    assert default_policy.evaluate("show running-config interface Gi0/2", "core").allowed
    assert not ai_policy.evaluate("show running-config interface Gi0/2", "core").allowed
    assert not default_policy.evaluate("show running-config interface Gi0/2; reload", "core").allowed


@pytest.mark.parametrize("message", [
    "chạy show running-config interface GigabitEthernet0/2 trên EIU-R1",
    "show run interface Gi0/2 on EIU-R1",
    "show run int gi0/2 EIU-R1",
    "xem cấu hình interface Gi0/2 của EIU-R1",
])
def test_a_typed_interface_show_run_answers_one_stanza(lab, message):
    lab["ssh"].set_client("EIU-R1", responses={
        "show running-config interface GigabitEthernet0/2":
            "interface GigabitEthernet0/2\n no ip address\n shutdown\nend"})

    reply = AIService.running_config_request(message, lab["owner"].id, lab["project"].id)

    assert reply is not None and reply["device"] == "EIU-R1"
    assert reply["results"][0]["command"].lower() == "show running-config interface gigabitethernet0/2"
    assert reply["results"][0]["status"] == "success"


# -- #10 a switch standing in for a PC gets an address and a gateway ----------------------

def test_an_address_on_a_switch_port_leaves_switching_first(app, lab):
    pc1 = lab["devices"]["PC1"]
    pc1.kind = "switch"
    change = change_service.create_preview(lab["owner"].id, pc1.id, [
        "interface GigabitEthernet0/1", "ip address 172.20.5.2 255.255.255.192", "no shutdown", "exit",
        "ip default-gateway 172.20.5.1",
    ])
    assert change.commands == [
        "configure terminal",
        "interface GigabitEthernet0/1", "no switchport", "ip address 172.20.5.2 255.255.255.192",
        "no shutdown",
        "ip default-gateway 172.20.5.1", "ip route 0.0.0.0 0.0.0.0 172.20.5.1",
        "end",
    ]


def test_routed_ports_and_svis_are_left_alone(app, lab):
    pc1 = lab["devices"]["PC1"]
    commands = [
        "interface GigabitEthernet0/1", "no switchport", "ip address 172.20.5.2 255.255.255.192",
        "interface Vlan10", "ip address 172.20.5.3 255.255.255.192",
        "ip route 0.0.0.0 0.0.0.0 172.20.5.1", "ip default-gateway 172.20.5.1",
    ]
    change = change_service.create_preview(lab["owner"].id, pc1.id, commands)
    assert change.commands[1:-1] == commands


def test_a_router_is_not_touched(app, lab):
    r1 = lab["devices"]["EIU-R1"]
    commands = ["interface GigabitEthernet0/1", "ip address 172.20.5.1 255.255.255.192", "no shutdown"]
    change = change_service.create_preview(lab["owner"].id, r1.id, commands)
    assert change.commands[1:-1] == commands
