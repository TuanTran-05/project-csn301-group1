import pytest

VALID_DEVICE = {
    "hostname": "CORE-SW1",
    "management_ip": "10.10.10.11",
    "device_type": "cisco_ios",
    "role": "core",
    "ssh_port": 22,
}


def test_admin_creates_device(client, admin_headers):
    response = client.post(
        "/api/devices",
        headers=admin_headers,
        json={
            "hostname": "CORE-SW1",
            "management_ip": "10.10.10.11",
            "device_type": "cisco_ios",
            "role": "core",
            "ssh_port": 22,
        },
    )
    assert response.status_code == 201


def test_created_device_defaults(client, admin_headers):
    body = client.post(
        "/api/devices", headers=admin_headers, json=VALID_DEVICE
    ).get_json()
    assert body["status"] == "unknown"
    assert body["monitoring_enabled"] is True
    assert "password" not in body


def test_viewer_cannot_create_device(client, viewer_headers):
    response = client.post(
        "/api/devices", headers=viewer_headers, json=VALID_DEVICE
    )
    assert response.status_code == 403


def test_anonymous_cannot_list_devices(client):
    assert client.get("/api/devices").status_code == 401


def test_viewer_can_list_devices(client, admin_headers, viewer_headers):
    client.post("/api/devices", headers=admin_headers, json=VALID_DEVICE)
    response = client.get("/api/devices", headers=viewer_headers)
    assert response.status_code == 200
    assert len(response.get_json()["items"]) == 1


def test_get_device_by_id(client, admin_headers):
    device_id = client.post(
        "/api/devices", headers=admin_headers, json=VALID_DEVICE
    ).get_json()["id"]
    response = client.get(f"/api/devices/{device_id}", headers=admin_headers)
    assert response.status_code == 200
    assert response.get_json()["hostname"] == "CORE-SW1"


def test_get_missing_device_returns_404(client, admin_headers):
    assert client.get("/api/devices/999", headers=admin_headers).status_code == 404


def test_admin_updates_device(client, admin_headers):
    device_id = client.post(
        "/api/devices", headers=admin_headers, json=VALID_DEVICE
    ).get_json()["id"]
    response = client.put(
        f"/api/devices/{device_id}",
        headers=admin_headers,
        json={"role": "distribution", "monitoring_enabled": False},
    )
    assert response.status_code == 200
    assert response.get_json()["role"] == "distribution"
    assert response.get_json()["monitoring_enabled"] is False


def test_admin_deletes_device(client, admin_headers):
    device_id = client.post(
        "/api/devices", headers=admin_headers, json=VALID_DEVICE
    ).get_json()["id"]
    assert (
        client.delete(f"/api/devices/{device_id}", headers=admin_headers).status_code
        == 204
    )
    assert client.get(f"/api/devices/{device_id}", headers=admin_headers).status_code == 404


def test_viewer_cannot_delete_device(client, admin_headers, viewer_headers):
    device_id = client.post(
        "/api/devices", headers=admin_headers, json=VALID_DEVICE
    ).get_json()["id"]
    assert (
        client.delete(f"/api/devices/{device_id}", headers=viewer_headers).status_code
        == 403
    )


def test_duplicate_hostname_rejected(client, admin_headers):
    client.post("/api/devices", headers=admin_headers, json=VALID_DEVICE)
    duplicate = dict(VALID_DEVICE, management_ip="10.10.10.12")
    response = client.post("/api/devices", headers=admin_headers, json=duplicate)
    assert response.status_code == 409


def test_duplicate_management_ip_rejected(client, admin_headers):
    client.post("/api/devices", headers=admin_headers, json=VALID_DEVICE)
    duplicate = dict(VALID_DEVICE, hostname="CORE-SW2")
    response = client.post("/api/devices", headers=admin_headers, json=duplicate)
    assert response.status_code == 409


@pytest.mark.parametrize(
    "hostname",
    ["core sw1", "core-sw1", "CORE_SW1", "CORE-SW1!", ""],
)
def test_invalid_hostname_rejected(client, admin_headers, hostname):
    response = client.post(
        "/api/devices", headers=admin_headers, json=dict(VALID_DEVICE, hostname=hostname)
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "management_ip",
    ["10.10.70.20", "192.168.1.1", "10.10.11.1", "not-an-ip", "10.10.10.0/24"],
)
def test_management_ip_must_be_in_management_network(
    client, admin_headers, management_ip
):
    response = client.post(
        "/api/devices",
        headers=admin_headers,
        json=dict(VALID_DEVICE, management_ip=management_ip),
    )
    assert response.status_code == 422


def test_management_ip_inside_subnet_accepted(client, admin_headers):
    response = client.post(
        "/api/devices",
        headers=admin_headers,
        json=dict(VALID_DEVICE, management_ip="10.10.10.254"),
    )
    assert response.status_code == 201


@pytest.mark.parametrize("device_type", ["juniper_junos", "cisco_nxos", "linux", ""])
def test_invalid_device_type_rejected(client, admin_headers, device_type):
    response = client.post(
        "/api/devices",
        headers=admin_headers,
        json=dict(VALID_DEVICE, device_type=device_type),
    )
    assert response.status_code == 422


def test_cisco_asa_device_type_accepted(client, admin_headers):
    response = client.post(
        "/api/devices",
        headers=admin_headers,
        json={
            "hostname": "FW-01",
            "management_ip": "10.10.10.3",
            "device_type": "cisco_asa",
            "role": "firewall",
        },
    )
    assert response.status_code == 201


@pytest.mark.parametrize("role", ["spine", "leaf", "unknown", ""])
def test_invalid_role_rejected(client, admin_headers, role):
    response = client.post(
        "/api/devices", headers=admin_headers, json=dict(VALID_DEVICE, role=role)
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "role",
    ["isp", "firewall", "core", "distribution", "access", "dmz", "management"],
)
def test_all_supported_roles_accepted(client, admin_headers, role):
    response = client.post(
        "/api/devices",
        headers=admin_headers,
        json=dict(VALID_DEVICE, role=role, hostname=f"DEV-{role.upper()}"),
    )
    assert response.status_code == 201


@pytest.mark.parametrize("ssh_port", [0, -1, 70000, "twenty-two"])
def test_invalid_ssh_port_rejected(client, admin_headers, ssh_port):
    response = client.post(
        "/api/devices", headers=admin_headers, json=dict(VALID_DEVICE, ssh_port=ssh_port)
    )
    assert response.status_code == 422


def test_ssh_port_defaults_to_22(client, admin_headers):
    payload = {k: v for k, v in VALID_DEVICE.items() if k != "ssh_port"}
    body = client.post("/api/devices", headers=admin_headers, json=payload).get_json()
    assert body["ssh_port"] == 22
