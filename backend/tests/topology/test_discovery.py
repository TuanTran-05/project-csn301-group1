import pytest

from conftest import _create_device
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.ssh.exceptions import SSHConnectionError
from network_copilot.topology.model import TopologyLink

CDP_COMMAND = "show cdp neighbors detail"


def cdp(*entries):
    blocks = []
    for neighbor, local, remote in entries:
        blocks.append(
            f"-------------------------\nDevice ID: {neighbor}.lab\nEntry address(es):\n"
            f"  IP address: 10.0.0.1\nPlatform: cisco ,  Capabilities: Router\n"
            f"Interface: {local},  Port ID (outgoing port): {remote}\nHoldtime : 1 sec\n"
        )
    return "\n".join(blocks)


@pytest.fixture
def net(client, admin_headers, project, app, ssh_factory):
    """R1 -- SW1 and R1 -- SW2 exist on the wire; SW1 -- SW2 is only designed."""
    devices = {
        "R1": _create_device("R1", "172.16.3.111", "core"),
        "SW1": _create_device("SW1", "172.16.3.121", "access"),
        "SW2": _create_device("SW2", "172.16.3.122", "access"),
    }
    ssh_factory.set_client("R1", responses={CDP_COMMAND: cdp(
        ("SW1", "GigabitEthernet0/0", "GigabitEthernet0/0"), ("SW2", "GigabitEthernet0/1", "GigabitEthernet0/0"))})
    ssh_factory.set_client("SW1", responses={CDP_COMMAND: cdp(("R1", "GigabitEthernet0/0", "GigabitEthernet0/0"))})
    ssh_factory.set_client("SW2", responses={CDP_COMMAND: cdp(("R1", "GigabitEthernet0/0", "GigabitEthernet0/1"))})
    return devices


def design(devices, a, if_a, b, if_b):
    db.session.add(TopologyLink(project_id=devices[a].project_id, device_a_id=devices[a].id, interface_a=if_a,
                                device_b_id=devices[b].id, interface_b=if_b, link_type="physical"))
    db.session.commit()


def run(client, headers, project, path="discover", body=None):
    return client.post(f"/api/projects/{project.id}/topology/{path}", headers=headers, json=body or {})


def test_discovery_finds_each_cable_once(client, admin_headers, project, net):
    body = run(client, admin_headers, project).get_json()
    pairs = {frozenset((l["hostname_a"], l["hostname_b"])) for l in body["discovered"]}
    assert pairs == {frozenset(("R1", "SW1")), frozenset(("R1", "SW2"))}
    assert body["errors"] == [] and body["polled"] == ["R1", "SW1", "SW2"]
    # Both ends reported the same cable, so it appears once.
    assert len(body["discovered"]) == 2


def test_drift_between_design_and_reality(client, admin_headers, project, net):
    design(net, "R1", "Gi0/0", "SW1", "Gi0/0")        # present: abbreviations still match
    design(net, "SW1", "GigabitEthernet0/1", "SW2", "GigabitEthernet0/1")  # designed, not on the wire
    body = run(client, admin_headers, project).get_json()

    ids = {l.interface_a: l.id for l in db.session.query(TopologyLink)}
    assert body["matched"] == [ids["Gi0/0"]]
    assert body["missing"] == [ids["GigabitEthernet0/1"]]
    assert [(u["hostname_a"], u["hostname_b"]) for u in body["unplanned"]] == [("R1", "SW2")]


def test_unreachable_devices_make_links_unknown_not_missing(client, admin_headers, project, net, ssh_factory):
    design(net, "SW1", "Gi0/1", "SW2", "Gi0/1")
    ssh_factory.set_failing("SW1", SSHConnectionError("refused"))
    ssh_factory.set_failing("SW2", SSHConnectionError("refused"))
    body = run(client, admin_headers, project).get_json()
    assert {e["device"] for e in body["errors"]} == {"SW1", "SW2"}
    assert len(body["unknown"]) == 1 and body["missing"] == []


def test_lldp_is_used_when_cdp_is_silent(client, admin_headers, project, ssh_factory):
    _create_device("R1", "172.16.3.111", "core")
    _create_device("SW1", "172.16.3.121", "access")
    lldp = ("------------------------------------------------\nLocal Intf: Gi0/0\nPort id: Gi0/0\nSystem Name: SW1\n")
    ssh_factory.set_client("R1", responses={CDP_COMMAND: "% CDP is not enabled", "show lldp neighbors detail": lldp})
    ssh_factory.set_client("SW1", responses={})
    body = run(client, admin_headers, project).get_json()
    assert [(l["hostname_a"], l["hostname_b"], l["protocol"]) for l in body["discovered"]] == [("R1", "SW1", "lldp")]


def test_neighbors_outside_the_project_are_listed_not_linked(client, admin_headers, project, ssh_factory):
    _create_device("R1", "172.16.3.111", "core")
    ssh_factory.set_client("R1", responses={CDP_COMMAND: cdp(("SOMEONE-ELSE", "GigabitEthernet0/0", "Gi0/1"))})
    body = run(client, admin_headers, project).get_json()
    assert body["discovered"] == [] and body["unmatched"][0]["neighbor"] == "SOMEONE-ELSE.lab"


def test_discovery_is_read_only(client, admin_headers, project, net, ssh_factory):
    run(client, admin_headers, project)
    for name in ("R1", "SW1", "SW2"):
        assert ssh_factory.get(name).config_batches == [] and ssh_factory.get(name).exec_batches == []
    assert db.session.query(TopologyLink).count() == 0


def test_apply_adds_chosen_links_and_skips_conflicts(client, admin_headers, project, net):
    discovered = run(client, admin_headers, project).get_json()["discovered"]
    design(net, "R1", "GigabitEthernet0/0", "SW1", "GigabitEthernet0/0")  # already designed
    body = run(client, admin_headers, project, "discover/apply", {"links": discovered}).get_json()
    assert len(body["created"]) == 1 and len(body["skipped"]) == 1
    assert db.session.query(TopologyLink).count() == 2


def test_discovery_needs_edit_rights_and_ios_devices(client, viewer_headers, admin_headers, project):
    assert run(client, viewer_headers, project).status_code == 403
    assert run(client, admin_headers, project).status_code == 422  # no devices at all
    assert run(client, admin_headers, project, "discover/apply", {"links": "x"}).status_code == 422
