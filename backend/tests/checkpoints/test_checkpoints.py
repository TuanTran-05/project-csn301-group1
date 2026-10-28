"""Undo/redo of applied configuration (config checkpoints)."""

import pytest

from network_copilot.changes import service as change_service
from network_copilot.checkpoints import service as checkpoint_service
from network_copilot.checkpoints.diff import configs_match, restore_commands
from network_copilot.checkpoints.model import ConfigCheckpoint
from network_copilot.errors import ConflictError, InvalidStateError
from network_copilot.extensions import db

from conftest import default_project_id
from fakes.fake_ssh_client import FakeSSHClient

BEFORE = """Building configuration...

Current configuration : 900 bytes
!
! Last configuration change at 10:00:00 UTC
hostname ACC-SW1
!
username admin privilege 15 secret 9 $9$abc
!
interface GigabitEthernet0/1
 switchport mode access
!
end
"""

AFTER = """Building configuration...

Current configuration : 960 bytes
!
! Last configuration change at 10:05:00 UTC
hostname ACC-SW1
!
username admin privilege 15 secret 9 $9$abc
!
vlan 25
 name MARKETING
!
interface GigabitEthernet0/1
 switchport access vlan 25
 switchport mode access
!
end
"""

VLAN_WITH_25 = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/3
25   MARKETING                        active    Gi0/1
"""

VLAN_WITHOUT_25 = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/3
"""


# -- diff ---------------------------------------------------------------------


def test_restore_commands_reverse_a_vlan_change():
    plan = restore_commands(AFTER, BEFORE)
    assert plan.commands == [
        "interface GigabitEthernet0/1",
        "no switchport access vlan 25",
        "no vlan 25",
    ]


def test_restore_commands_replay_a_vlan_change():
    plan = restore_commands(BEFORE, AFTER)
    assert plan.commands == [
        "vlan 25",
        "name MARKETING",
        "interface GigabitEthernet0/1",
        "switchport access vlan 25",
    ]


def test_volatile_header_lines_are_not_a_difference():
    assert configs_match(BEFORE, BEFORE.replace("10:00:00", "11:11:11").replace("900", "901"))
    assert not configs_match(BEFORE, AFTER)


def test_single_value_lines_are_replaced_not_negated_first():
    plan = restore_commands("hostname NEW\ninterface Gi0/1\n description b\n",
                            "hostname OLD\ninterface Gi0/1\n description a\n")
    assert plan.commands == ["hostname OLD", "interface Gi0/1", "description a"]


def test_negated_lines_and_shutdown_are_inverted():
    plan = restore_commands(
        "no ip domain-lookup\ninterface Gi0/2\n shutdown\n",
        "interface Gi0/2\n",
    )
    assert plan.commands == ["ip domain-lookup", "interface Gi0/2", "no shutdown"]


def test_secrets_and_banners_are_never_emitted():
    plan = restore_commands(
        "username admin secret 9 $new\nenable secret 9 $x\nbanner motd ^C\nHello\n^C\n",
        "username admin secret 9 $old\nbanner motd ^C\nBye\n^C\n",
    )
    assert plan.commands == []
    assert all("$" not in line for line in plan.skipped)
    assert "banner motd ^C" in plan.skipped


def test_new_sections_come_before_their_users_and_removed_ones_after():
    plan = restore_commands(
        "interface Vlan9\n ip address 10.9.0.1 255.255.255.0\ninterface Gi0/1\n switchport access vlan 9\n",
        "vlan 10\ninterface Gi0/1\n switchport access vlan 10\n",
    )
    assert plan.commands.index("vlan 10") < plan.commands.index("interface Gi0/1")
    assert plan.commands[-1] == "no interface Vlan9"


# -- workflow -----------------------------------------------------------------


class StatefulSSH(FakeSSHClient):
    """A device whose running-config becomes ``next_config`` once configured."""

    def __init__(self, config, next_config, vlan_after):
        super().__init__(responses={"show running-config": config})
        self.next_config = next_config
        self.vlan_after = vlan_after

    def run_config(self, commands):
        result = super().run_config(commands)
        self.responses["show running-config"] = self.next_config
        self.responses["show vlan brief"] = self.vlan_after
        return result


@pytest.fixture
def checkpoints_on(app):
    app.config["CONFIG_CHECKPOINTS_ENABLED"] = True


def _apply(change, admin_user, ssh_factory, config, next_config, vlan_after):
    ssh_factory.clients[change.target_hostname] = StatefulSSH(config, next_config, vlan_after)
    change_service.approve(change.id, admin_user.id)
    return change_service.apply(change.id, admin_user.id, confirm_hostname=change.target_hostname)


def _vlan_change(admin_user, device):
    return change_service.create_preview(
        admin_user.id, device_id=device.id,
        commands=["configure terminal", "vlan 25", "name MARKETING", "end"],
        description="VLAN Marketing",
    )


def test_a_successful_apply_records_a_named_step(app, checkpoints_on, admin_user, access_switch, ssh_factory):
    change = _vlan_change(admin_user, access_switch)
    applied = _apply(change, admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)
    assert applied.status == "success"
    step = db.session.query(ConfigCheckpoint).one()
    assert step.state == "active"
    assert step.name == "ACC-SW1: VLAN Marketing"
    assert step.pre_backup_id == applied.backup_id
    assert step.commands == ["vlan 25", "name MARKETING"]


def test_no_step_is_recorded_when_checkpoints_are_disabled(app, admin_user, access_switch, ssh_factory):
    change = _vlan_change(admin_user, access_switch)
    _apply(change, admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)
    assert db.session.query(ConfigCheckpoint).count() == 0


def test_undo_then_redo_through_the_api(
    client, app, checkpoints_on, admin_user, admin_headers, access_switch, ssh_factory
):
    headers = {**admin_headers, "X-Project-Id": str(default_project_id())}
    _apply(_vlan_change(admin_user, access_switch), admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)

    listing = client.get("/api/checkpoints", headers=headers).get_json()
    step = listing["items"][0]
    assert listing["undo_next_id"] == step["id"] and step["can_undo"] and not step["can_redo"]

    # Undo only stages a change; the device is not written yet.
    ssh_factory.clients["ACC-SW1"] = StatefulSSH(AFTER, BEFORE, VLAN_WITHOUT_25)
    response = client.post(f"/api/checkpoints/{step['id']}/undo", headers=headers)
    assert response.status_code == 201, response.get_json()
    undo_change = response.get_json()["change"]
    assert undo_change["status"] == "pending_approval"
    assert undo_change["source"] == "restore"
    assert undo_change["requires_confirmation"] is True
    assert "no vlan 25" in undo_change["commands"]
    assert ssh_factory.get("ACC-SW1").config_batches == []
    pending = client.get("/api/checkpoints", headers=headers).get_json()["items"][0]
    assert pending["pending_action"] == "undo" and not pending["can_undo"]

    change_service.approve(undo_change["id"], admin_user.id)
    done = change_service.apply(undo_change["id"], admin_user.id, confirm_hostname="ACC-SW1")
    assert done.status == "success"
    step_row = db.session.get(ConfigCheckpoint, step["id"])
    assert step_row.state == "undone"
    assert step_row.last_result == "Đã hoàn tác."

    listing = client.get("/api/checkpoints", headers=headers).get_json()
    assert listing["undo_next_id"] is None and listing["redo_next_id"] == step["id"]

    ssh_factory.clients["ACC-SW1"] = StatefulSSH(BEFORE, AFTER, VLAN_WITH_25)
    response = client.post(f"/api/checkpoints/{step['id']}/redo", headers=headers)
    assert response.status_code == 201, response.get_json()
    redo_change = response.get_json()["change"]
    assert "vlan 25" in redo_change["commands"]
    change_service.approve(redo_change["id"], admin_user.id)
    assert change_service.apply(redo_change["id"], admin_user.id, confirm_hostname="ACC-SW1").status == "success"
    assert db.session.get(ConfigCheckpoint, step["id"]).state == "active"
    # Undo/Redo move the step; they do not add steps of their own.
    assert db.session.query(ConfigCheckpoint).count() == 1


def test_only_the_newest_step_of_a_device_can_be_undone(
    app, checkpoints_on, admin_user, access_switch, ssh_factory
):
    _apply(_vlan_change(admin_user, access_switch), admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)
    second = change_service.create_preview(
        admin_user.id, device_id=access_switch.id,
        commands=["configure terminal", "interface GigabitEthernet0/1", "description Printer", "end"],
    )
    newer = AFTER.replace(" switchport access vlan 25", " description Printer\n switchport access vlan 25")
    _apply(second, admin_user, ssh_factory, AFTER, newer, VLAN_WITH_25)
    older, newest = db.session.query(ConfigCheckpoint).order_by(ConfigCheckpoint.id).all()
    with pytest.raises(InvalidStateError):
        checkpoint_service.request_restore(older, "undo", admin_user.id)
    ssh_factory.clients["ACC-SW1"] = StatefulSSH(newer, AFTER, VLAN_WITH_25)
    change, _ = checkpoint_service.request_restore(newest, "undo", admin_user.id)
    assert change.commands == ["configure terminal", "interface GigabitEthernet0/1",
                               "no description Printer", "end"]
    # A second restore cannot be staged while this one is pending.
    with pytest.raises(ConflictError):
        checkpoint_service.request_restore(newest, "undo", admin_user.id)


def test_a_new_change_after_undo_discards_the_redo_step(
    app, checkpoints_on, admin_user, access_switch, ssh_factory
):
    _apply(_vlan_change(admin_user, access_switch), admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)
    step = db.session.query(ConfigCheckpoint).one()
    ssh_factory.clients["ACC-SW1"] = StatefulSSH(AFTER, BEFORE, VLAN_WITHOUT_25)
    undo, _ = checkpoint_service.request_restore(step, "undo", admin_user.id)
    change_service.approve(undo.id, admin_user.id)
    change_service.apply(undo.id, admin_user.id, confirm_hostname="ACC-SW1")
    assert db.session.get(ConfigCheckpoint, step.id).state == "undone"

    fresh = change_service.create_preview(
        admin_user.id, device_id=access_switch.id,
        commands=["configure terminal", "vlan 30", "name LAB", "end"],
    )
    _apply(fresh, admin_user, ssh_factory, BEFORE, BEFORE + "vlan 30\n name LAB\n", VLAN_WITH_25.replace("25   MARKETING", "30   LAB      "))
    assert db.session.get(ConfigCheckpoint, step.id).state == "discarded"
    listing = checkpoint_service.list_checkpoints(default_project_id())
    assert [item["name"] for item in listing["items"]] == ["ACC-SW1: vlan 30 · name LAB"]
    assert listing["redo_next_id"] is None


def test_nothing_to_undo_is_reported(app, checkpoints_on, admin_user, access_switch, ssh_factory):
    _apply(_vlan_change(admin_user, access_switch), admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)
    step = db.session.query(ConfigCheckpoint).one()
    ssh_factory.clients["ACC-SW1"] = StatefulSSH(BEFORE, BEFORE, VLAN_WITHOUT_25)
    with pytest.raises(ConflictError):
        checkpoint_service.request_restore(step, "undo", admin_user.id)


def test_viewers_can_list_steps_but_not_restore_them(
    client, app, checkpoints_on, admin_user, viewer_headers, access_switch, ssh_factory
):
    _apply(_vlan_change(admin_user, access_switch), admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)
    headers = {**viewer_headers, "X-Project-Id": str(default_project_id())}
    listing = client.get("/api/checkpoints", headers=headers)
    assert listing.status_code == 200
    step_id = listing.get_json()["items"][0]["id"]
    assert client.post(f"/api/checkpoints/{step_id}/undo", headers=headers).status_code == 403
    assert client.patch(f"/api/checkpoints/{step_id}", headers=headers, json={"name": "x"}).status_code == 403


def test_a_step_can_be_renamed(client, app, checkpoints_on, admin_user, admin_headers, access_switch, ssh_factory):
    _apply(_vlan_change(admin_user, access_switch), admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)
    headers = {**admin_headers, "X-Project-Id": str(default_project_id())}
    step_id = db.session.query(ConfigCheckpoint).one().id
    response = client.patch(f"/api/checkpoints/{step_id}", headers=headers, json={"name": "Trước khi đổi VLAN"})
    assert response.status_code == 200
    assert response.get_json()["name"] == "Trước khi đổi VLAN"
    assert client.patch(f"/api/checkpoints/{step_id}", headers=headers, json={"name": "  "}).status_code == 422


def test_a_second_change_on_the_same_device_applies(app, admin_user, access_switch, ssh_factory):
    """Regression: the next preview's baseline used to be the pre-change backup."""
    first = _apply(_vlan_change(admin_user, access_switch), admin_user, ssh_factory, BEFORE, AFTER, VLAN_WITH_25)
    assert first.status == "success"
    second = change_service.create_preview(
        admin_user.id, device_id=access_switch.id,
        commands=["configure terminal", "vlan 30", "name LAB", "end"],
    )
    after_second = AFTER + "vlan 30\n name LAB\n"
    applied = _apply(second, admin_user, ssh_factory, AFTER, after_second,
                     VLAN_WITH_25 + "30   LAB                              active\n")
    assert applied.status == "success", applied.error_message
