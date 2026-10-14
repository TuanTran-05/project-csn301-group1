import json

import pytest

from conftest import _create_device
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.projects.model import Project
from network_copilot.topology.model import DesignVlan, TopologyLink

from .conftest import scope


@pytest.fixture
def lab(client, alice_headers, alice):
    """Alice's project with two devices, a routed link, a VLAN and OSPF."""
    created = client.post("/api/projects", headers=alice_headers, json={
        "name": "Campus", "management_network": "10.20.0.0/24", "environment": "mixed"}).get_json()
    headers = scope(alice_headers, type("P", (), {"id": created["id"]}))
    ids = {}
    for host, ip, role in (("R1", "10.20.0.11", "core"), ("SW1", "10.20.0.12", "access")):
        ids[host] = client.post("/api/devices", headers=headers, json={
            "hostname": host, "management_ip": ip, "device_type": "cisco_ios", "role": role,
            "credential": {"username": "netops", "password": "S3cret!pw"}}).get_json()["id"]
    base = f"/api/projects/{created['id']}/topology"
    client.post(f"{base}/links", headers=headers, json={
        "device_a_id": ids["R1"], "device_b_id": ids["SW1"], "link_type": "routed", "network": "10.0.12.0/30"})
    client.post(f"{base}/design/vlans", headers=headers, json={"vlan_id": 10, "name": "USERS"})
    client.post(f"{base}/design/ospf", headers=headers, json={"device_id": ids["R1"], "process_id": 5})
    client.put(f"{base}/layout", headers=headers, json={"positions": [{"device_id": ids["R1"], "x": 100, "y": 50}]})
    return {"project_id": created["id"], "headers": alice_headers, "ids": ids}


def test_export_contains_the_design_and_no_secrets(client, lab):
    response = client.get(f"/api/projects/{lab['project_id']}/export", headers=lab["headers"])
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    text = response.get_data(as_text=True)
    assert "S3cret!pw" not in text and "password" not in text and "credential" not in text
    doc = json.loads(text)
    assert doc["format"] == "network-copilot-project"
    assert [d["hostname"] for d in doc["devices"]] == ["R1", "SW1"]
    assert doc["devices"][0]["pos_x"] == 100.0
    assert doc["links"][0]["ip_a"] == "10.0.12.1"
    assert doc["design"]["vlans"] == [{"vlan_id": 10, "name": "USERS"}]
    assert doc["design"]["ospf"][0]["process_id"] == 5


def test_import_round_trips_into_a_new_project(client, lab, bob_headers, bob):
    document = client.get(f"/api/projects/{lab['project_id']}/export", headers=lab["headers"]).get_json()
    response = client.post("/api/projects/import", headers=bob_headers, json={"document": document})
    assert response.status_code == 201
    new = response.get_json()
    assert new["owner"] == "bob" and new["device_count"] == 2

    again = client.get(f"/api/projects/{new['id']}/export", headers=bob_headers).get_json()
    assert again["devices"] == document["devices"]
    assert again["links"] == document["links"]
    assert again["design"] == document["design"]


def test_clone_copies_structure_but_not_credentials_or_members(client, lab, alice_headers):
    cloned = client.post(f"/api/projects/{lab['project_id']}/clone", headers=alice_headers, json={}).get_json()
    assert cloned["name"] == "Campus (copy)"
    devices = db.session.query(Device).filter_by(project_id=cloned["id"]).all()
    assert len(devices) == 2 and all(d.credential is None for d in devices)
    assert db.session.query(TopologyLink).filter_by(project_id=cloned["id"]).count() == 1
    assert db.session.query(DesignVlan).filter_by(project_id=cloned["id"]).count() == 1
    again = client.post(f"/api/projects/{lab['project_id']}/clone", headers=alice_headers, json={}).get_json()
    assert again["name"] == "Campus (copy) (2)" or again["name"] != cloned["name"]


def test_import_validates_everything_and_leaves_nothing_behind(client, lab, bob_headers):
    document = client.get(f"/api/projects/{lab['project_id']}/export", headers=lab["headers"]).get_json()
    document["devices"][1]["management_ip"] = "192.168.99.99"  # outside the project's network
    before = db.session.query(Project).count()
    response = client.post("/api/projects/import", headers=bob_headers, json={"document": document})
    assert response.status_code == 422 and "SW1" in response.get_json()["message"]
    assert db.session.query(Project).count() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="something-else"),
        lambda d: d.update(version=99),
        lambda d: d["links"][0].update(hostname_b="GHOST"),
        lambda d: d["design"]["ospf"][0].update(hostname="GHOST"),
        lambda d: d["project"].update(management_network="8.8.8.0/24"),
    ],
)
def test_malformed_documents_are_rejected(client, lab, bob_headers, mutate):
    document = client.get(f"/api/projects/{lab['project_id']}/export", headers=lab["headers"]).get_json()
    mutate(document)
    before = db.session.query(Project).count()
    assert client.post("/api/projects/import", headers=bob_headers, json={"document": document}).status_code == 422
    assert db.session.query(Project).count() == before


def test_import_requires_a_document_and_a_writable_account(client, bob_headers, viewer_headers):
    assert client.post("/api/projects/import", headers=bob_headers, json={}).status_code == 422
    assert client.post("/api/projects/import", headers=viewer_headers, json={"document": {}}).status_code in (403, 422)


def test_a_stranger_cannot_export_or_clone(client, lab, bob_headers):
    assert client.get(f"/api/projects/{lab['project_id']}/export", headers=bob_headers).status_code == 404
    assert client.post(f"/api/projects/{lab['project_id']}/clone", headers=bob_headers, json={}).status_code == 404


# -- CSV ---------------------------------------------------------------------------


def csv_import(client, headers, project_id, text):
    return client.post(f"/api/projects/{project_id}/devices/import-csv", headers=headers, json={"csv": text})


def test_csv_import_creates_devices_with_defaults_and_credentials(client, lab, alice_headers):
    text = ("hostname,management_ip,role,environment,ssh_port,username,password\n"
            "ACC1,10.20.0.21,access,physical,2222,netops,Pw-12345\n"
            "ACC2,10.20.0.22,,,,,\n")
    response = csv_import(client, alice_headers, lab["project_id"], text)
    assert response.status_code == 201
    made = {d["hostname"]: d for d in response.get_json()["created"]}
    assert (made["ACC1"]["environment"], made["ACC1"]["ssh_port"], made["ACC1"]["has_credential"]) == ("physical", 2222, True)
    assert (made["ACC2"]["device_type"], made["ACC2"]["role"], made["ACC2"]["has_credential"]) == ("cisco_ios", "access", False)


def test_csv_import_is_all_or_nothing_with_line_numbers(client, lab, alice_headers):
    text = ("hostname,management_ip\nOK1,10.20.0.31\nBAD,not-an-ip\nR1,10.20.0.33\nOK2,10.20.0.31\n")
    before = db.session.query(Device).count()
    response = csv_import(client, alice_headers, lab["project_id"], text)
    assert response.status_code == 422
    details = response.get_json()["details"]
    assert set(details) == {"line 3", "line 4", "line 5"}  # invalid IP, existing hostname, repeated IP
    assert db.session.query(Device).count() == before


@pytest.mark.parametrize("text", ["", "name,ip\nA,1.1.1.1", "hostname,management_ip,evil\nA,10.20.0.40,x", "hostname,management_ip\n"])
def test_csv_structure_errors(client, lab, alice_headers, text):
    assert csv_import(client, alice_headers, lab["project_id"], text).status_code == 422


def test_csv_export_neutralises_formulas_and_omits_secrets(client, lab, alice_headers):
    base = f"/api/projects/{lab['project_id']}"
    device_id = lab["ids"]["R1"]
    client.put(f"/api/devices/{device_id}", headers=scope(alice_headers, type("P", (), {"id": lab["project_id"]})),
               json={"description": "=HYPERLINK(\"http://evil\")"})
    response = client.get(f"{base}/devices.csv", headers=alice_headers)
    text = response.get_data(as_text=True)
    assert text.startswith("hostname,management_ip,device_type")
    assert "S3cret" not in text and "'=HYPERLINK" in text and response.mimetype == "text/csv"


def test_csv_import_needs_edit_rights(client, lab, alice_headers, bob_headers):
    assert csv_import(client, bob_headers, lab["project_id"], "hostname,management_ip\nA,10.20.0.50").status_code == 404
