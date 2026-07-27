import pytest

from network_copilot.changes import service as change_service
from network_copilot.changes.model import ChangeRequest
from network_copilot.errors import PolicyViolationError, ValidationError
from network_copilot.extensions import db

VLAN_COMMANDS = ["configure terminal", "vlan 25", "name MARKETING", "end"]


@pytest.fixture
def change_service_module(app):
    return change_service


def test_vlan_preview_requires_approval(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        commands=[
            "configure terminal",
            "vlan 25",
            "name MARKETING",
            "end",
        ],
        verification_commands=["show vlan brief"],
    )
    assert change.status == "pending_approval"
    assert change.risk_level == "low"


# -- supported templates --------------------------------------------------


def test_vlan_rename_is_supported(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        commands=["configure terminal", "vlan 25", "name SALES-NEW", "end"],
    )
    assert change.status == "pending_approval"


def test_access_port_assignment_is_supported(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        commands=[
            "configure terminal",
            "interface GigabitEthernet0/1",
            "switchport mode access",
            "switchport access vlan 25",
            "end",
        ],
    )
    assert change.status == "pending_approval"


def test_interface_description_is_supported(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        commands=[
            "configure terminal",
            "interface GigabitEthernet0/2",
            "description LINK TO MARKETING",
            "end",
        ],
    )
    assert change.status == "pending_approval"


# -- blocked operations ---------------------------------------------------


def test_shutdown_on_core_uplink_is_blocked(app, admin_user, core_switch):
    with pytest.raises(PolicyViolationError):
        change_service.create_preview(
            admin_user.id,
            device_id=core_switch.id,
            commands=[
                "configure terminal",
                "interface GigabitEthernet0/1",
                "shutdown",
                "end",
            ],
        )


def test_shutdown_on_distribution_uplink_is_blocked(app, admin_user, dist_switch):
    with pytest.raises(PolicyViolationError):
        change_service.create_preview(
            admin_user.id,
            device_id=dist_switch.id,
            commands=[
                "configure terminal",
                "interface GigabitEthernet0/0",
                "shutdown",
                "end",
            ],
        )


def test_removing_ospf_is_blocked(app, admin_user, core_switch):
    with pytest.raises(PolicyViolationError):
        change_service.create_preview(
            admin_user.id,
            device_id=core_switch.id,
            commands=["configure terminal", "no router ospf 1", "end"],
        )


@pytest.mark.parametrize("vlan_id", [1, 1002, 1003, 1004, 1005])
def test_deleting_a_system_vlan_is_blocked(app, admin_user, access_switch, vlan_id):
    with pytest.raises(PolicyViolationError):
        change_service.create_preview(
            admin_user.id,
            device_id=access_switch.id,
            commands=["configure terminal", f"no vlan {vlan_id}", "end"],
        )


@pytest.mark.parametrize(
    "command",
    [
        "write erase",
        "reload",
        "erase startup-config",
        "hostname HACKED",
        "ip route 0.0.0.0 0.0.0.0 1.2.3.4",
        "username backdoor privilege 15 secret Pass123",
        "router ospf 1",
        "banner motd #owned#",
    ],
)
def test_commands_outside_the_supported_templates_are_blocked(
    app, admin_user, access_switch, command
):
    with pytest.raises(PolicyViolationError):
        change_service.create_preview(
            admin_user.id,
            device_id=access_switch.id,
            commands=["configure terminal", command, "end"],
        )


def test_empty_command_list_is_rejected(app, admin_user, access_switch):
    with pytest.raises(ValidationError):
        change_service.create_preview(
            admin_user.id, device_id=access_switch.id, commands=[]
        )


# -- preview content ------------------------------------------------------


def test_preview_derives_verification_commands_when_omitted(
    app, admin_user, access_switch
):
    change = change_service.create_preview(
        admin_user.id, device_id=access_switch.id, commands=VLAN_COMMANDS
    )
    assert "show vlan brief" in change.verification_commands


def test_preview_records_rollback_commands(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id, device_id=access_switch.id, commands=VLAN_COMMANDS
    )
    assert "no vlan 25" in " ".join(change.rollback_commands)


def test_preview_stores_the_requesting_user_and_device(
    app, admin_user, access_switch
):
    change = change_service.create_preview(
        admin_user.id, device_id=access_switch.id, commands=VLAN_COMMANDS
    )
    assert change.requested_by_id == admin_user.id
    assert change.device_id == access_switch.id
    assert db.session.query(ChangeRequest).count() == 1


def test_preview_normalises_commands(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        commands=["  configure   terminal ", "VLAN 25", "name MARKETING", "end"],
    )
    assert change.commands[0] == "configure terminal"
    assert change.commands[1] == "vlan 25"


def test_preview_adds_config_wrapper_when_missing(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        commands=["vlan 25", "name MARKETING"],
    )
    assert change.commands[0] == "configure terminal"
    assert change.commands[-1] == "end"


def test_core_device_change_is_higher_risk(app, admin_user, core_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=core_switch.id,
        commands=[
            "configure terminal",
            "interface GigabitEthernet0/5",
            "description UPLINK TO DIST",
            "end",
        ],
    )
    assert change.risk_level == "medium"


def test_unknown_device_raises(app, admin_user):
    from network_copilot.errors import NotFoundError

    with pytest.raises(NotFoundError):
        change_service.create_preview(admin_user.id, device_id=999, commands=VLAN_COMMANDS)


# -- API ------------------------------------------------------------------


def test_preview_endpoint_returns_full_payload(client, admin_headers, access_switch):
    response = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={
            "device_id": access_switch.id,
            "commands": VLAN_COMMANDS,
            "verification_commands": ["show vlan brief"],
            "description": "Create VLAN 25 for marketing",
        },
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["status"] == "pending_approval"
    assert body["risk_level"] == "low"
    assert body["device"]["hostname"] == "ACC-SW1"
    assert body["commands"] == VLAN_COMMANDS
    assert body["verification_commands"] == ["show vlan brief"]
    assert "warnings" in body
    assert "rollback_commands" in body


def test_preview_endpoint_blocks_dangerous_changes(
    client, admin_headers, access_switch
):
    response = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={
            "device_id": access_switch.id,
            "commands": ["configure terminal", "no router ospf 1", "end"],
        },
    )
    assert response.status_code == 403


def test_preview_endpoint_requires_admin(client, viewer_headers, access_switch):
    response = client.post(
        "/api/changes/preview",
        headers=viewer_headers,
        json={"device_id": access_switch.id, "commands": VLAN_COMMANDS},
    )
    assert response.status_code == 403


def test_preview_endpoint_requires_authentication(client, access_switch):
    response = client.post(
        "/api/changes/preview",
        json={"device_id": access_switch.id, "commands": VLAN_COMMANDS},
    )
    assert response.status_code == 401


def test_preview_never_opens_an_ssh_session(
    client, admin_headers, access_switch, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={"device_id": access_switch.id, "commands": VLAN_COMMANDS},
    )
    assert fake.calls == []


def test_list_and_get_changes(client, admin_headers, access_switch):
    change_id = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={"device_id": access_switch.id, "commands": VLAN_COMMANDS},
    ).get_json()["id"]

    listing = client.get("/api/changes", headers=admin_headers)
    assert listing.status_code == 200
    assert len(listing.get_json()["items"]) == 1

    detail = client.get(f"/api/changes/{change_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.get_json()["id"] == change_id


def test_change_states_are_the_documented_set():
    from network_copilot.changes.model import CHANGE_STATUSES

    assert set(CHANGE_STATUSES) == {
        "pending_approval",
        "approved",
        "running",
        "success",
        "failed",
        "cancelled",
    }
