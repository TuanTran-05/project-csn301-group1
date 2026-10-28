"""The project's default SSH login: typed once, used by every device."""

from network_copilot.credentials.service import get_device_credential

from conftest import default_project_id

DEVICE = {
    "hostname": "R1",
    "management_ip": "172.16.3.21",
    "device_type": "cisco_ios",
    "role": "core",
}


def _url(suffix=""):
    return f"/api/projects/{default_project_id()}/ssh-default{suffix}"


def _save_default(client, headers, **body):
    payload = {"username": "g1", "password": "group1-pw", **body}
    return client.put(_url(), headers=headers, json=payload)


def test_the_default_is_saved_without_ever_returning_the_password(client, admin_headers):
    assert client.get(_url(), headers=admin_headers).get_json()["default"] is None
    response = _save_default(client, admin_headers)
    assert response.status_code == 200
    assert response.get_json()["default"]["username"] == "g1"
    assert "group1-pw" not in response.get_data(as_text=True)
    assert "group1-pw" not in client.get(_url(), headers=admin_headers).get_data(as_text=True)


def test_the_first_save_needs_a_password_and_a_later_blank_one_keeps_it(client, admin_headers):
    assert client.put(_url(), headers=admin_headers, json={"username": "g1"}).status_code == 422
    _save_default(client, admin_headers)
    assert client.put(_url(), headers=admin_headers, json={"username": "g2"}).status_code == 200
    device = client.post("/api/devices", headers=admin_headers, json=DEVICE).get_json()
    stored = get_device_credential(device["id"])
    assert (stored.username, stored.password) == ("g2", "group1-pw")


def test_a_new_device_without_a_login_takes_the_default(client, admin_headers):
    _save_default(client, admin_headers)
    body = client.post("/api/devices", headers=admin_headers, json=DEVICE).get_json()
    assert body["has_credential"] is True
    assert "g1" not in str(body)  # like any device login, the name is not returned
    stored = get_device_credential(body["id"])
    assert (stored.username, stored.password) == ("g1", "group1-pw")


def test_an_explicit_login_still_wins(client, admin_headers):
    _save_default(client, admin_headers)
    body = client.post("/api/devices", headers=admin_headers, json={
        **DEVICE, "credential": {"username": "netops", "password": "other"},
    }).get_json()
    assert get_device_credential(body["id"]).username == "netops"


def test_a_device_can_switch_to_the_default_on_edit(client, admin_headers):
    device = client.post("/api/devices", headers=admin_headers, json={
        **DEVICE, "credential": {"username": "old", "password": "old-pw"},
    }).get_json()
    assert client.put(f"/api/devices/{device['id']}", headers=admin_headers,
                      json={"use_project_ssh": True}).status_code == 422  # no default yet
    _save_default(client, admin_headers)
    response = client.put(f"/api/devices/{device['id']}", headers=admin_headers,
                          json={"use_project_ssh": True})
    assert response.status_code == 200
    stored = get_device_credential(device["id"])
    assert (stored.username, stored.password) == ("g1", "group1-pw")


def test_apply_fills_only_devices_without_a_login_unless_asked(client, admin_headers):
    bare = client.post("/api/devices", headers=admin_headers, json=DEVICE).get_json()
    own = client.post("/api/devices", headers=admin_headers, json={
        **DEVICE, "hostname": "R2", "management_ip": "172.16.3.22",
        "credential": {"username": "own", "password": "own-pw"},
    }).get_json()
    _save_default(client, admin_headers)
    assert client.get(_url(), headers=admin_headers).get_json()["devices_without_login"] == 1

    result = client.post(_url("/apply"), headers=admin_headers, json={}).get_json()
    assert result["updated"] == ["R1"]
    assert result["devices_without_login"] == 0
    assert get_device_credential(bare["id"]).username == "g1"
    assert get_device_credential(own["id"]).username == "own"

    result = client.post(_url("/apply"), headers=admin_headers, json={"scope": "all"}).get_json()
    assert sorted(result["updated"]) == ["R1", "R2"]
    assert get_device_credential(own["id"]).username == "g1"


def test_viewers_cannot_read_change_or_apply_it(client, admin_headers, viewer_headers):
    _save_default(client, admin_headers)
    assert client.get(_url(), headers=viewer_headers).status_code == 403
    assert _save_default(client, viewer_headers).status_code == 403
    assert client.post(_url("/apply"), headers=viewer_headers, json={}).status_code == 403
    assert client.delete(_url(), headers=viewer_headers).status_code == 403


def test_deleting_the_default_leaves_device_logins_alone(client, admin_headers):
    _save_default(client, admin_headers)
    device = client.post("/api/devices", headers=admin_headers, json=DEVICE).get_json()
    assert client.delete(_url(), headers=admin_headers).get_json()["default"] is None
    assert get_device_credential(device["id"]).username == "g1"
    nothing = client.post("/api/devices", headers=admin_headers, json={
        **DEVICE, "hostname": "R3", "management_ip": "172.16.3.23",
    }).get_json()
    assert nothing["has_credential"] is False


def test_a_planned_device_takes_the_default_when_provisioned(client, admin_headers):
    _save_default(client, admin_headers)
    planned = client.post("/api/devices", headers=admin_headers, json={
        "hostname": "R9", "device_type": "cisco_ios", "role": "core", "lifecycle": "planned",
    }).get_json()
    assert planned["has_credential"] is False
    response = client.post(f"/api/devices/{planned['id']}/provision", headers=admin_headers,
                           json={"management_ip": "172.16.3.29"})
    assert response.status_code == 200, response.get_json()
    assert get_device_credential(planned["id"]).username == "g1"
