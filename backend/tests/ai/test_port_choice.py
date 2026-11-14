"""Asking which port to use, and refusing changes to the management interface."""

import pytest
from fakes.fake_ai_provider import FakeAIProvider

from conftest import _create_device
from network_copilot.backups.model import ConfigBackup
from network_copilot.changes.management_guard import ManagementInterfaceError, check, management_interface
from network_copilot.changes.model import ChangeBatch
from network_copilot.extensions import db
from network_copilot.topology.model import TopologyLink

PC_REQUEST = ("Đặt IP cho các PC: PC1 172.20.5.163/28 và PC2 172.20.5.164/28, gateway VLAN10")


@pytest.fixture
def lab3(app):
    """EIU-R1 - EIU-S1 Gi0/0, EIU-S1 Gi0/1/Gi0/2 - PC1/PC2 Gi0/0, all PNETLab switches."""
    r1 = _create_device("EIU-R1", "172.16.3.13", "core")
    s1 = _create_device("EIU-S1", "172.16.3.15", "access")
    pcs = [_create_device(f"PC{n}", f"172.16.3.{16 + n}", "access") for n in (1, 2)]
    for device in (r1, s1, *pcs):
        device.environment = "pnetlab"
    cables = [(r1, "GigabitEthernet0/0", s1, "GigabitEthernet0/0")]
    cables += [(s1, f"GigabitEthernet0/{n}", pc, "GigabitEthernet0/0") for n, pc in enumerate(pcs, start=1)]
    for a, if_a, b, if_b in cables:
        db.session.add(TopologyLink(project_id=r1.project_id, device_a_id=a.id, interface_a=if_a,
                                    device_b_id=b.id, interface_b=if_b, link_type="physical"))
    db.session.commit()
    return {"r1": r1, "s1": s1, "pc1": pcs[0], "pc2": pcs[1]}


def _chat(client, headers, message):
    return client.post("/api/ai/chat", headers=headers, json={"message": message})


# -- asking for the port --------------------------------------------------------

def test_an_address_request_without_a_port_asks_with_the_cabled_ports(client, admin_headers, app, lab3):
    app.config["AI_PROVIDER_INSTANCE"] = provider = FakeAIProvider(responses={"intent": "chat", "explanation": "x"})

    response = _chat(client, admin_headers, PC_REQUEST)

    assert response.status_code == 200
    body = response.get_json()
    choice = body["port_choice"]
    assert choice["message"] == PC_REQUEST
    assert [d["hostname"] for d in choice["devices"]] == ["PC1", "PC2"]
    pc1 = choice["devices"][0]
    assert pc1["suggested"] == "GigabitEthernet0/0"
    assert pc1["options"] == [{"interface": "GigabitEthernet0/0", "short": "Gi0/0",
                               "peer": "EIU-S1", "peer_interface": "Gi0/1"}]
    assert "cổng nào" in body["explanation"]
    assert provider.calls == 0           # answered before the model is asked
    assert db.session.query(ChangeBatch).count() == 0


@pytest.mark.parametrize("message", [
    "Đặt IP cho PC1 trên cổng Gi0/0: 172.20.5.163/28, gateway 172.20.5.161",
    "Đặt IP cho PC1 172.20.5.163/28 — dùng cổng PC1 Gi0/0",
    "Đặt IP quản lý cho EIU-S1 là 172.20.5.162/28 trên VLAN 10",
    "Tạo subinterface cho EIU-R1 với IP 172.20.5.161/28",
    "show ip interface brief trên PC1",
    "Đặt IP cho PC1",                            # no address yet: nothing to place
])
def test_a_clear_request_goes_straight_on(client, admin_headers, app, lab3, message):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses={"intent": "chat", "explanation": "ok"})

    body = _chat(client, admin_headers, message).get_json()

    assert "port_choice" not in body


def test_a_switch_with_many_cables_is_left_to_the_model(client, admin_headers, app, lab3):
    # EIU-S1 is normally addressed on an SVI; asking for one of its PC ports would mislead.
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses={"intent": "chat", "explanation": "ok"})

    body = _chat(client, admin_headers, "Đặt IP quản lý cho EIU-S1 là 172.20.5.162/28").get_json()

    assert "port_choice" not in body


def test_a_router_is_asked_about_too(client, admin_headers, app, lab3):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses={"intent": "chat", "explanation": "ok"})

    body = _chat(client, admin_headers, "Đặt IP 172.20.5.1/25 cho EIU-R1").get_json()

    assert [d["hostname"] for d in body["port_choice"]["devices"]] == ["EIU-R1"]


def test_a_viewer_is_not_asked(client, viewer_headers, app, lab3):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses={"intent": "chat", "explanation": "ok"})

    body = _chat(client, viewer_headers, PC_REQUEST).get_json()

    assert "port_choice" not in (body or {})


# -- the management interface ------------------------------------------------------------

def test_a_pnetlab_switch_keeps_its_management_address_on_vlan1(lab3):
    assert management_interface(lab3["pc1"]) == "Vlan1"


def test_the_newest_backup_says_where_the_management_address_is(lab3):
    db.session.add(ConfigBackup(device_id=lab3["r1"].id, reason="baseline", running_config=(
        "hostname EIU-R1\n!\ninterface GigabitEthernet0/0\n no ip address\n!\n"
        "interface GigabitEthernet0/1\n ip address 172.16.3.13 255.255.255.0\n!\nend\n")))
    db.session.commit()

    assert management_interface(lab3["r1"]) == "GigabitEthernet0/1"
    with pytest.raises(ManagementInterfaceError):
        check(lab3["r1"], ["interface Gi0/1", "ip address 172.20.5.1 255.255.255.128"])
    check(lab3["r1"], ["interface GigabitEthernet0/0.10", "encapsulation dot1Q 10",
                       "ip address 172.20.5.161 255.255.255.240"])


@pytest.mark.parametrize("commands", [
    ["interface Vlan1", "ip address 172.20.5.163 255.255.255.240", "no shutdown"],
    ["interface vlan 1", "shutdown"],
    ["no interface Vlan1"],
])
def test_changes_to_the_management_interface_are_refused(lab3, commands):
    with pytest.raises(ManagementInterfaceError) as caught:
        check(lab3["pc1"], commands)
    assert "Vlan1" in caught.value.message and "Gi0/0" in caught.value.message


@pytest.mark.parametrize("commands", [
    ["interface GigabitEthernet0/0", "no switchport", "ip address 172.20.5.163 255.255.255.240",
     "no shutdown", "exit", "ip default-gateway 172.20.5.161"],
    ["interface Vlan1", "description management", "exit"],
    ["interface Vlan10", "ip address 172.20.5.162 255.255.255.240"],
])
def test_other_changes_go_through(lab3, commands):
    check(lab3["pc1"], commands)


def test_a_model_plan_touching_vlan1_becomes_a_chat_answer(client, admin_headers, app, lab3):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses={
        "intent": "configure",
        "operations": [{
            "device_hostnames": ["PC1"], "execution_mode": "config",
            "commands": ["interface Vlan1", "ip address 172.20.5.163 255.255.255.240", "no shutdown"],
            "verification_commands": [],
        }],
        "explanation": "Đặt IP cho PC1.",
    })

    response = _chat(client, admin_headers, "Đặt IP cho PC1 trên Vlan1 172.20.5.163/28")

    assert response.status_code == 200
    body = response.get_json()
    assert body["intent"] == "chat" and "IP quản lý" in body["explanation"]
    assert db.session.query(ChangeBatch).count() == 0
