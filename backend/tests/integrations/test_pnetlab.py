import socket

import pytest

from network_copilot.devices.model import Device
from network_copilot.integrations import pnetlab
from network_copilot.topology import pnetlab_import
from network_copilot.topology.model import TopologyLink
from network_copilot.extensions import db

NODES = {
    "1": {"id": 1, "name": "r1", "template": "vios", "left": "20%", "top": "30%"},
    "2": {"id": 2, "name": "Core Switch", "template": "viosl2", "left": "60%", "top": "30%"},
    "3": {"id": 3, "name": "FW", "template": "asav", "left": "60%", "top": "80%"},
    "4": {"id": 4, "name": "PC1", "template": "vpcs", "left": "10%", "top": "90%"},
}
TOPOLOGY = [
    {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
     "destination": "network1", "destination_type": "network", "destination_label": ""},
    {"source": "node2", "source_type": "node", "source_label": "e0/1",
     "destination": "network1", "destination_type": "network", "destination_label": ""},
    {"source": "node1", "source_type": "node", "source_label": "Gi0/1",
     "destination": "network2", "destination_type": "network", "destination_label": ""},
    {"source": "node3", "source_type": "node", "source_label": "Gi0/0",
     "destination": "network2", "destination_type": "network", "destination_label": ""},
    # a network with three nodes is a shared segment, not a cable
    {"source": "node1", "source_type": "node", "source_label": "Gi0/2",
     "destination": "network3", "destination_type": "network", "destination_label": ""},
    {"source": "node2", "source_type": "node", "source_label": "Gi0/2",
     "destination": "network3", "destination_type": "network", "destination_label": ""},
    {"source": "node3", "source_type": "node", "source_label": "Gi0/2",
     "destination": "network3", "destination_type": "network", "destination_label": ""},
]
SOURCE = {"url": "http://172.16.0.5", "username": "admin", "password": "pnet-pass", "lab": "/Course/Lab1.unl"}


class FakeLab:
    def __init__(self, params):
        self.params = params

    def nodes(self, lab):
        return NODES

    def topology(self, lab):
        return TOPOLOGY


@pytest.fixture
def fake_lab(app):
    app.config["PNETLAB_CLIENT_FACTORY"] = FakeLab


@pytest.fixture
def base(project):
    return f"/api/projects/{project.id}/topology/import/pnetlab"


# -- pure helpers --------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [("e0/1", "Ethernet0/1"), ("Gi0/0", "GigabitEthernet0/0"), ("gigabitethernet1/2", "GigabitEthernet1/2"),
     ("Fa0/3", "FastEthernet0/3"), ("Te1/0/1", "TenGigabitEthernet1/0/1"), ("mgmt", None), ("", None), (None, None)],
)
def test_interface_labels_are_expanded(label, expected):
    assert pnetlab_import.normalize_label(label) == expected


@pytest.mark.parametrize(
    "template,expected",
    [("vios", ("cisco_ios", "core")), ("viosl2", ("cisco_ios", "access")), ("iol", ("cisco_ios", "core")),
     ("asav", ("cisco_asa", "firewall")), ("vpcs", None), ("linux", None), (None, None)],
)
def test_templates_map_to_device_kinds(template, expected):
    kind = pnetlab_import.classify(template)
    assert (None if kind is None else (kind["device_type"], kind["role"])) == expected


def test_hostnames_are_made_valid():
    assert pnetlab_import.hostname_for("Core Switch #1", 3) == "CORE-SWITCH-1"
    assert pnetlab_import.hostname_for("***", 7) == "NODE7"


def test_links_come_from_two_node_networks_only():
    links = pnetlab_import.derive_links(NODES, TOPOLOGY)
    pairs = {(l["node_a"], l["node_b"]) for l in links}
    assert pairs == {("1", "2"), ("1", "3")}
    first = next(l for l in links if l["node_b"] == "2")
    assert (first["interface_a"], first["interface_b"]) == ("GigabitEthernet0/0", "Ethernet0/1")


def test_positions_scale_into_the_canvas():
    positions = pnetlab_import.layout_positions(NODES)
    assert all(60 <= x <= 900 and 50 <= y <= 510 for x, y in positions.values())
    pixel = pnetlab_import.layout_positions({"1": {"left": "2000", "top": "1000"}, "2": {"left": "100", "top": "100"}})
    assert pixel["1"][0] <= 900


# -- SSRF guard ------------------------------------------------------------------


def _resolve_to(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (address, 80))])


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "0.0.0.0", "224.0.0.1"])
def test_non_private_targets_are_refused(monkeypatch, address):
    _resolve_to(monkeypatch, address)
    with pytest.raises(pnetlab.PNETLabError):
        pnetlab.validate_base_url("http://lab.example")


def test_private_targets_are_accepted(monkeypatch):
    _resolve_to(monkeypatch, "172.16.0.5")
    assert pnetlab.validate_base_url("https://lab.example:8443/ignored/path") == "https://lab.example:8443"


@pytest.mark.parametrize("url", ["ftp://172.16.0.5", "172.16.0.5", "http://user:pw@172.16.0.5", ""])
def test_malformed_urls_are_refused(url):
    with pytest.raises(pnetlab.PNETLabError):
        pnetlab.validate_base_url(url)


def test_lab_paths_cannot_escape():
    with pytest.raises(pnetlab.PNETLabError):
        pnetlab._lab_path("/../etc/passwd")
    assert pnetlab._lab_path("Course/Lab 1") == "/Course/Lab%201.unl"


# -- endpoints ---------------------------------------------------------------------


def test_preview_lists_nodes_links_and_free_addresses(client, admin_headers, base, fake_lab, project):
    body = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE).get_json()
    by_id = {n["id"]: n for n in body["nodes"]}
    assert by_id["1"]["hostname"] == "R1" and by_id["1"]["supported"]
    assert by_id["2"]["hostname"] == "CORE-SWITCH" and by_id["2"]["role"] == "access"
    assert by_id["3"]["device_type"] == "cisco_asa"
    assert by_id["4"]["supported"] is False and by_id["4"]["management_ip"] is None
    ips = [by_id[k]["management_ip"] for k in "123"]
    assert len(set(ips)) == 3 and all(ip.startswith("172.16.3.") for ip in ips)
    assert len(body["links"]) == 2
    assert db.session.query(Device).count() == 0  # nothing was created


def test_import_creates_devices_positions_and_links(client, admin_headers, base, fake_lab, project):
    payload = {**SOURCE, "nodes": [{"id": "1"}, {"id": "2"}, {"id": "3"}, {"id": "4"}],
               "credential": {"username": "netops", "password": "S3cret!pw"}}
    body = client.post(base, headers=admin_headers, json=payload).get_json()

    statuses = {d["id"]: d["status"] for d in body["devices"]}
    assert statuses == {"1": "created", "2": "created", "3": "created", "4": "skipped"}
    devices = {d.hostname: d for d in db.session.query(Device)}
    assert set(devices) == {"R1", "CORE-SWITCH", "FW"}
    assert devices["R1"].environment == "pnetlab" and devices["R1"].pos_x is not None
    assert devices["R1"].credential is not None
    assert [l["status"] for l in body["links"]] == ["created", "created"]
    link = db.session.query(TopologyLink).filter_by(device_a_id=devices["R1"].id, device_b_id=devices["CORE-SWITCH"].id).one()
    assert (link.interface_a, link.interface_b) == ("GigabitEthernet0/0", "Ethernet0/1")


def test_import_honours_chosen_names_and_addresses(client, admin_headers, base, fake_lab):
    payload = {**SOURCE, "nodes": [{"id": "1", "hostname": "EDGE", "management_ip": "172.16.3.99"}], "import_links": False}
    body = client.post(base, headers=admin_headers, json=payload).get_json()
    assert body["devices"][0]["management_ip"] == "172.16.3.99"
    assert db.session.query(Device).one().hostname == "EDGE"


def test_import_is_repeatable_and_reports_conflicts(client, admin_headers, base, fake_lab):
    payload = {**SOURCE, "nodes": [{"id": "1"}, {"id": "2"}]}
    client.post(base, headers=admin_headers, json=payload)
    again = client.post(base, headers=admin_headers, json=payload).get_json()
    assert [d["status"] for d in again["devices"]] == ["skipped", "skipped"]
    assert db.session.query(Device).count() == 2
    assert db.session.query(TopologyLink).count() == 1  # not duplicated


def test_a_bad_address_fails_only_that_node(client, admin_headers, base, fake_lab):
    payload = {**SOURCE, "nodes": [{"id": "1", "management_ip": "10.99.99.99"}, {"id": "2"}]}
    body = client.post(base, headers=admin_headers, json=payload).get_json()
    assert [d["status"] for d in body["devices"]] == ["error", "created"]


def test_unknown_node_is_reported(client, admin_headers, base, fake_lab):
    body = client.post(base, headers=admin_headers, json={**SOURCE, "nodes": [{"id": "99"}]}).get_json()
    assert body["devices"][0]["status"] == "error"


def test_import_needs_edit_rights_and_valid_input(client, viewer_headers, admin_headers, base, fake_lab):
    assert client.post(f"{base}/preview", headers=viewer_headers, json=SOURCE).status_code == 403
    assert client.post(base, headers=admin_headers, json={**SOURCE, "nodes": []}).status_code == 422
    assert client.post(f"{base}/preview", headers=admin_headers, json={"url": "x"}).status_code == 422


def test_import_never_audits_the_lab_password(client, admin_headers, base, fake_lab):
    from network_copilot.audit.model import AuditLog

    client.post(base, headers=admin_headers, json={**SOURCE, "nodes": [{"id": "1"}]})
    logged = " ".join(str(r.details) for r in db.session.query(AuditLog))
    assert "pnet-pass" not in logged


def test_unreachable_lab_is_a_clean_error(client, admin_headers, base, app):
    def boom(params):
        raise pnetlab.PNETLabError("Cannot reach PNETLab: ConnectTimeout.")

    app.config["PNETLAB_CLIENT_FACTORY"] = boom
    response = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE)
    assert response.status_code == 422
    assert "Cannot reach PNETLab" in response.get_json()["message"]
