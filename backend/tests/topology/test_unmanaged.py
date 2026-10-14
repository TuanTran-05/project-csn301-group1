import pytest

from network_copilot.devices.model import Device
from network_copilot.projects import exchange

from .conftest import link_payload


def drop(client, headers, base, template, **extra):
    return client.post(f"{base}/quick-device", headers=headers, json={"template": template, **extra})


def test_unmanaged_devices_have_no_management_ip(client, admin_headers, base):
    pc = drop(client, admin_headers, base, "pc").get_json()
    assert pc["kind"] == "pc" and pc["managed"] is False
    assert pc["management_ip"] is None and pc["deployable"] is False


def test_planned_managed_device_is_not_deployable_until_provisioned(client, admin_headers, base):
    planned = drop(client, admin_headers, base, "router", planned=True).get_json()
    assert planned["lifecycle"] == "planned" and planned["deployable"] is False
    assert client.post(f"/api/devices/{planned['id']}/test-connection", headers=admin_headers).status_code >= 400

    done = client.post(f"/api/devices/{planned['id']}/provision", headers=admin_headers,
                       json={"management_ip": "172.16.3.190"})
    assert done.status_code == 200
    body = done.get_json()
    assert body["lifecycle"] == "provisioned" and body["deployable"] is True


def test_generic_device_rejects_management_ip_and_credentials(client, admin_headers):
    res = client.post("/api/devices", headers=admin_headers, json={
        "hostname": "PC9", "device_type": "generic", "kind": "pc", "management_ip": "172.16.3.99"})
    assert res.status_code == 422


def test_unmanaged_devices_round_trip_through_export(client, admin_headers, base, project):
    drop(client, admin_headers, base, "server")
    doc = client.get(f"/api/projects/{project.id}/export", headers=admin_headers).get_json()
    kinds = {d["kind"] for d in doc["devices"]}
    assert "server" in kinds
    imported = client.post("/api/projects/import", headers=admin_headers, json={"document": doc, "name": "Copy"})
    assert imported.status_code in (200, 201), imported.get_json()


def test_csv_import_accepts_unmanaged_rows(client, admin_headers, project):
    text = "hostname,kind\nPC-A,pc\nSRV-A,server\n"
    res = client.post(f"/api/projects/{project.id}/devices/import-csv", headers=admin_headers, json={"csv": text})
    assert res.status_code in (200, 201), res.get_json()


def test_pnetlab_classifies_hosts_as_unmanaged():
    from network_copilot.topology.pnetlab_import import classify
    assert classify("vpcs")["device_type"] == "generic"
    assert classify("linux")["kind"] == "server"
    assert classify("iol")["device_type"] == "cisco_ios"
    assert classify("mystery-box") is None
