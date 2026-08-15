import pytest

from network_copilot.extensions import db
from network_copilot.monitoring.model import DeviceSnapshot
from network_copilot.monitoring.service import (
    commands_for_role,
    poll_all_enabled_devices,
    poll_device,
)
from network_copilot.ssh.exceptions import SSHConnectionError, SSHTimeoutError

IFACE = """Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet0/1     10.10.10.11     YES NVRAM  up                    up
"""

ROUTES = """C        10.10.10.0/24 is directly connected, GigabitEthernet0/1
"""

OSPF = """Neighbor ID     Pri   State           Dead Time   Address         Interface
2.2.2.2           1   FULL/DR         00:00:33    10.255.0.6      GigabitEthernet0/2
"""

VLANS = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
25   MARKETING                        active    Gi0/1
"""


# -- command selection by role -------------------------------------------


def test_base_commands_for_every_role():
    assert commands_for_role("management") == [
        "show ip interface brief",
        "show ip route",
    ]


@pytest.mark.parametrize("role", ["core", "distribution"])
def test_routing_roles_poll_ospf(role):
    assert "show ip ospf neighbor" in commands_for_role(role)


@pytest.mark.parametrize("role", ["access", "distribution"])
def test_switching_roles_poll_vlans(role):
    assert "show vlan brief" in commands_for_role(role)


def test_distribution_polls_both_ospf_and_vlans():
    assert commands_for_role("distribution") == [
        "show ip interface brief",
        "show ip route",
        "show ip ospf neighbor",
        "show vlan brief",
        "show interfaces trunk",
        "show ip dhcp pool",
    ]


def test_access_role_does_not_poll_ospf():
    assert "show ip ospf neighbor" not in commands_for_role("access")


# -- poll_device ----------------------------------------------------------


def test_successful_poll_marks_device_online(app, device, ssh_factory):
    ssh_factory.set_client(
        device.hostname,
        responses={
            "show ip interface brief": IFACE,
            "show ip route": ROUTES,
            "show ip ospf neighbor": OSPF,
        },
    )
    snapshot = poll_device(device.id)
    assert snapshot.status == "online"
    assert device.status == "online"
    assert device.last_seen_at is not None


def test_failed_poll_marks_device_offline(app, device, ssh_factory):
    ssh_factory.set_failing(device.hostname, SSHConnectionError("host unreachable"))
    snapshot = poll_device(device.id)
    assert snapshot.status == "offline"
    assert device.status == "offline"
    assert "unreachable" in snapshot.error.lower()


def test_timeout_marks_device_offline(app, device, ssh_factory):
    ssh_factory.set_failing(device.hostname, SSHTimeoutError("timed out"))
    assert poll_device(device.id).status == "offline"
    assert device.status == "offline"


def test_snapshot_stores_raw_and_parsed_output(app, device, ssh_factory):
    ssh_factory.set_client(
        device.hostname,
        responses={
            "show ip interface brief": IFACE,
            "show ip route": ROUTES,
            "show ip ospf neighbor": OSPF,
        },
    )
    snapshot = poll_device(device.id)
    assert "GigabitEthernet0/1" in snapshot.raw_output["show ip interface brief"]
    interfaces = snapshot.parsed_data["show ip interface brief"]
    assert interfaces[0]["interface"] == "GigabitEthernet0/1"
    assert snapshot.parsed_data["show ip ospf neighbor"][0]["state"] == "FULL/DR"


def test_snapshot_keeps_raw_output_when_parsing_yields_nothing(
    app, device, ssh_factory
):
    ssh_factory.set_client(device.hostname, default_output="% Invalid input detected")
    snapshot = poll_device(device.id)
    assert snapshot.raw_output["show ip route"] == "% Invalid input detected"
    assert snapshot.parsed_data["show ip route"] == []


def test_poll_runs_the_commands_for_the_device_role(app, access_switch, ssh_factory):
    fake = ssh_factory.set_client(access_switch.hostname, responses={
        "show vlan brief": VLANS
    })
    poll_device(access_switch.id)
    assert fake.show_commands == [
        "show ip interface brief",
        "show ip route",
        "show vlan brief",
        "show interfaces trunk",
    ]


def test_poll_persists_a_snapshot(app, device, ssh_factory):
    ssh_factory.set_client(device.hostname, default_output="ok")
    poll_device(device.id)
    assert db.session.query(DeviceSnapshot).count() == 1


def test_poll_unknown_device_raises(app):
    from network_copilot.errors import NotFoundError

    with pytest.raises(NotFoundError):
        poll_device(999)


# -- poll_all_enabled_devices --------------------------------------------


def test_poll_all_skips_disabled_devices(app, device, access_switch, ssh_factory):
    access_switch.monitoring_enabled = False
    db.session.commit()
    ssh_factory.set_client(device.hostname, default_output="ok")
    ssh_factory.set_client(access_switch.hostname, default_output="ok")

    snapshots = poll_all_enabled_devices()
    assert len(snapshots) == 1
    assert snapshots[0].device_id == device.id


def test_poll_all_continues_after_a_device_fails(
    app, device, access_switch, ssh_factory
):
    ssh_factory.set_failing(device.hostname, SSHConnectionError("down"))
    ssh_factory.set_client(access_switch.hostname, default_output="ok")

    snapshots = poll_all_enabled_devices()
    assert len(snapshots) == 2
    statuses = {snapshot.device_id: snapshot.status for snapshot in snapshots}
    assert statuses[device.id] == "offline"
    assert statuses[access_switch.id] == "online"


# -- API ------------------------------------------------------------------


def test_status_endpoint_returns_latest_snapshot(
    client, admin_headers, device, ssh_factory, app
):
    ssh_factory.set_client(
        device.hostname, responses={"show ip interface brief": IFACE}
    )
    poll_device(device.id)
    response = client.get(f"/api/devices/{device.id}/status", headers=admin_headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "online"
    assert body["snapshot"]["parsed_data"]["show ip interface brief"]


def test_status_endpoint_without_a_snapshot(client, admin_headers, device):
    response = client.get(f"/api/devices/{device.id}/status", headers=admin_headers)
    assert response.status_code == 200
    assert response.get_json()["snapshot"] is None


def test_status_endpoint_requires_authentication(client, device):
    assert client.get(f"/api/devices/{device.id}/status").status_code == 401


def test_refresh_endpoint_polls_the_device(
    client, admin_headers, device, ssh_factory
):
    ssh_factory.set_client(device.hostname, default_output="ok")
    response = client.post(f"/api/devices/{device.id}/refresh", headers=admin_headers)
    assert response.status_code == 200
    assert response.get_json()["status"] == "online"


def test_refresh_endpoint_reports_offline_devices(
    client, admin_headers, device, ssh_factory
):
    ssh_factory.set_failing(device.hostname, SSHConnectionError("down"))
    response = client.post(f"/api/devices/{device.id}/refresh", headers=admin_headers)
    assert response.status_code == 200
    assert response.get_json()["status"] == "offline"


def test_refresh_endpoint_404s_for_unknown_device(client, admin_headers):
    assert (
        client.post("/api/devices/999/refresh", headers=admin_headers).status_code == 404
    )


def test_asa_devices_poll_the_asa_command_spellings(app, make_device):
    from network_copilot.monitoring.service import commands_for_device

    firewall = make_device("FW-TEST", "10.0.0.99", "firewall", device_type="cisco_asa")

    assert commands_for_device(firewall) == [
        "show interface ip brief",
        "show route",
    ]


def test_ios_devices_are_unaffected_by_the_asa_branch(app, make_device):
    from network_copilot.monitoring.service import commands_for_device

    switch = make_device("DIST-TEST", "10.0.0.98", "distribution")

    assert commands_for_device(switch) == [
        "show ip interface brief",
        "show ip route",
        "show ip ospf neighbor",
        "show vlan brief",
        "show interfaces trunk",
        "show ip dhcp pool",
    ]


def test_asa_gets_no_role_extras(app, make_device):
    """"firewall" is in neither ROUTING_ROLES nor SWITCHING_ROLES, so an ASA
    is never asked for OSPF neighbours or a VLAN database it does not have."""
    from network_copilot.monitoring.service import commands_for_device

    firewall = make_device("FW-TEST2", "10.0.0.97", "firewall", device_type="cisco_asa")

    assert "show vlan brief" not in commands_for_device(firewall)
    assert "show ip ospf neighbor" not in commands_for_device(firewall)


def test_poll_runs_the_asa_commands_on_an_asa_device(app, ssh_factory, make_device):
    from network_copilot.monitoring.service import poll_device

    firewall = make_device("FW-TEST3", "10.0.0.96", "firewall", device_type="cisco_asa")
    fake = ssh_factory.set_client(firewall.hostname, default_output="ok")

    poll_device(firewall.id)

    assert fake.show_commands == ["show interface ip brief", "show route"]

