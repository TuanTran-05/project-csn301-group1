import pytest

from conftest import _auth_headers, _create_user
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.projects.model import Project

from .conftest import PASSWORD, scope

VALID = {
    "name": "Campus",
    "management_network": "192.168.50.0/24",
    "environment": "physical",
}


def test_operator_can_create_and_list_own_project(client, alice_headers):
    created = client.post("/api/projects", headers=alice_headers, json=VALID)
    assert created.status_code == 201
    body = created.get_json()
    assert body["access"] == "owner"
    assert body["owner"] == "alice"

    listing = client.get("/api/projects", headers=alice_headers).get_json()["items"]
    assert [p["name"] for p in listing] == ["Campus"]


def test_viewer_role_cannot_create_a_project(client, viewer_headers, viewer_user):
    response = client.post("/api/projects", headers=viewer_headers, json=VALID)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "network",
    [
        "8.8.8.0/24",  # public
        "127.0.0.0/24",  # loopback
        "169.254.1.0/24",  # link-local
        "10.0.0.0/8",  # far too wide
        "not-a-network",
        "fd00::/64",  # IPv6
    ],
)
def test_management_network_must_be_a_small_private_ipv4_range(
    client, alice_headers, network
):
    response = client.post(
        "/api/projects", headers=alice_headers, json={**VALID, "management_network": network}
    )
    assert response.status_code == 422
    assert "management_network" in response.get_json()["details"]


def test_duplicate_project_name_for_one_owner_conflicts(client, alice_headers):
    assert client.post("/api/projects", headers=alice_headers, json=VALID).status_code == 201
    assert client.post("/api/projects", headers=alice_headers, json=VALID).status_code == 409


def test_users_only_see_their_own_projects(client, two_projects, alice_headers, bob_headers):
    a, b, *_ = two_projects
    assert [p["id"] for p in client.get("/api/projects", headers=alice_headers).get_json()["items"]] == [a.id]
    assert [p["id"] for p in client.get("/api/projects", headers=bob_headers).get_json()["items"]] == [b.id]


def test_a_stranger_cannot_tell_a_project_exists(client, two_projects, alice_headers):
    _, b, *_ = two_projects
    assert client.get(f"/api/projects/{b.id}", headers=alice_headers).status_code == 404
    assert client.put(f"/api/projects/{b.id}", headers=alice_headers, json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/projects/{b.id}", headers=alice_headers).status_code == 404


def test_admin_sees_every_project(client, two_projects, admin_headers):
    ids = {p["id"] for p in client.get("/api/projects", headers=admin_headers).get_json()["items"]}
    assert {two_projects[0].id, two_projects[1].id} <= ids


def test_share_gives_read_access_only_to_the_target(client, two_projects, alice_headers, bob_headers):
    a, *_ = two_projects
    assert client.get(f"/api/projects/{a.id}", headers=bob_headers).status_code == 404

    shared = client.post(
        f"/api/projects/{a.id}/members",
        headers=alice_headers,
        json={"username": "bob", "access": "viewer"},
    )
    assert shared.status_code == 201

    assert client.get(f"/api/projects/{a.id}", headers=bob_headers).get_json()["access"] == "viewer"
    devices = client.get("/api/devices", headers=scope(bob_headers, a))
    assert devices.status_code == 200
    assert [d["hostname"] for d in devices.get_json()["items"]] == ["R1"]


def test_viewer_share_cannot_change_inventory(client, two_projects, alice_headers, bob_headers):
    a, *_ = two_projects
    client.post(f"/api/projects/{a.id}/members", headers=alice_headers, json={"username": "bob"})
    response = client.post(
        "/api/devices",
        headers=scope(bob_headers, a),
        json={"hostname": "R2", "management_ip": "10.10.0.2", "device_type": "cisco_ios", "role": "core"},
    )
    assert response.status_code == 403


def test_editor_share_can_add_devices_but_not_share_or_delete(
    client, two_projects, alice_headers, bob_headers
):
    a, *_ = two_projects
    client.post(
        f"/api/projects/{a.id}/members",
        headers=alice_headers,
        json={"username": "bob", "access": "editor"},
    )
    created = client.post(
        "/api/devices",
        headers=scope(bob_headers, a),
        json={"hostname": "R2", "management_ip": "10.10.0.2", "device_type": "cisco_ios", "role": "core"},
    )
    assert created.status_code == 201

    assert client.post(
        f"/api/projects/{a.id}/members", headers=bob_headers, json={"username": "alice"}
    ).status_code == 403
    assert client.delete(f"/api/projects/{a.id}", headers=bob_headers).status_code == 403


def test_global_viewer_stays_read_only_even_with_an_editor_share(
    client, two_projects, alice_headers
):
    a, *_ = two_projects
    _create_user("carol", PASSWORD, "VIEWER")
    client.post(
        f"/api/projects/{a.id}/members",
        headers=alice_headers,
        json={"username": "carol", "access": "editor"},
    )
    carol = _auth_headers(client, "carol", PASSWORD)
    assert client.get("/api/devices", headers=scope(carol, a)).status_code == 200
    response = client.post(
        "/api/devices",
        headers=scope(carol, a),
        json={"hostname": "R9", "management_ip": "10.10.0.9", "device_type": "cisco_ios", "role": "core"},
    )
    assert response.status_code == 403


def test_unsharing_removes_access(client, two_projects, alice_headers, bob_headers, bob):
    a, *_ = two_projects
    client.post(f"/api/projects/{a.id}/members", headers=alice_headers, json={"username": "bob"})
    assert client.delete(f"/api/projects/{a.id}/members/{bob.id}", headers=alice_headers).status_code == 204
    assert client.get(f"/api/projects/{a.id}", headers=bob_headers).status_code == 404


def test_cannot_share_with_an_unknown_user_or_the_owner(client, two_projects, alice_headers):
    a, *_ = two_projects
    assert client.post(f"/api/projects/{a.id}/members", headers=alice_headers, json={"username": "ghost"}).status_code == 404
    assert client.post(f"/api/projects/{a.id}/members", headers=alice_headers, json={"username": "alice"}).status_code == 409


def test_member_listing_is_visible_to_a_viewer(client, two_projects, alice_headers, bob_headers):
    a, *_ = two_projects
    client.post(f"/api/projects/{a.id}/members", headers=alice_headers, json={"username": "bob"})
    members = client.get(f"/api/projects/{a.id}/members", headers=bob_headers).get_json()["items"]
    assert [(m["username"], m["access"]) for m in members] == [("bob", "viewer")]


def test_shrinking_the_network_below_existing_devices_is_refused(client, two_projects, alice_headers):
    a, *_ = two_projects
    response = client.put(
        f"/api/projects/{a.id}", headers=alice_headers, json={"management_network": "192.168.9.0/24"}
    )
    assert response.status_code == 409


def test_delete_project_removes_its_devices_and_data(client, app, two_projects, alice_headers, ssh_factory):
    a, b, ra, rb = two_projects
    from network_copilot.credentials.service import store_device_credential

    store_device_credential(ra.id, "u", "p")
    assert client.delete(f"/api/projects/{a.id}", headers=alice_headers).status_code == 204

    assert db.session.get(Project, a.id) is None
    assert db.session.query(Device).filter_by(project_id=a.id).count() == 0
    # The other project is untouched.
    assert db.session.get(Device, rb.id) is not None


def test_active_batch_blocks_project_deletion(client, two_projects, alice_headers, admin_user):
    from network_copilot.changes.model import ChangeBatch

    a, *_ = two_projects
    db.session.add(ChangeBatch(project_id=a.id, status="pending_approval", risk_level="low", source="ai"))
    db.session.commit()
    assert client.delete(f"/api/projects/{a.id}", headers=alice_headers).status_code == 409


def test_request_needs_a_project_when_several_are_available(client, admin_headers, two_projects):
    response = client.get("/api/devices", headers=admin_headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "project_required"


def test_single_project_is_used_when_none_is_named(client, two_projects, alice_headers):
    response = client.get("/api/devices", headers=alice_headers)
    assert response.status_code == 200
    assert len(response.get_json()["items"]) == 1


def test_naming_an_inaccessible_project_is_not_found(client, two_projects, alice_headers):
    _, b, *_ = two_projects
    assert client.get("/api/devices", headers=scope(alice_headers, b)).status_code == 404


def test_project_id_must_be_numeric(client, alice_headers, two_projects):
    response = client.get("/api/devices", headers={**alice_headers, "X-Project-Id": "abc"})
    assert response.status_code == 422


def test_admin_can_create_users_and_they_can_log_in(client, admin_headers):
    created = client.post(
        "/api/users",
        headers=admin_headers,
        json={"username": "dave", "password": "A-long-password-1", "role": "OPERATOR"},
    )
    assert created.status_code == 201
    assert "password" not in created.get_json()
    login = client.post("/api/auth/login", json={"username": "dave", "password": "A-long-password-1"})
    assert login.status_code == 200


def test_user_administration_is_admin_only_and_validated(client, admin_headers, alice_headers):
    assert client.get("/api/users", headers=alice_headers).status_code == 403
    assert client.post("/api/users", headers=alice_headers, json={}).status_code == 403
    bad = client.post("/api/users", headers=admin_headers, json={"username": "x", "password": "short"})
    assert bad.status_code == 422


# -- pages ---------------------------------------------------------------------


def test_projects_page_is_served_and_wires_the_designer(client):
    response = client.get("/projects")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'x-data="projectsApp()"' in html
    assert "js/projects.js" in html
    assert "/api/projects" not in html  # data comes from the API, not the template


def test_chat_page_offers_a_project_switcher(client):
    html = client.get("/").get_data(as_text=True)
    assert "switchProject" in html
    assert 'href="/projects"' in html


def test_every_page_sends_the_selected_project_with_api_calls():
    from pathlib import Path

    static = Path("src/network_copilot/static/js")
    for name in ("app.js", "dashboard.js", "projects.js"):
        source = (static / name).read_text(encoding="utf-8")
        assert "X-Project-Id" in source, name


def test_users_page_is_served(client):
    html = client.get("/users").get_data(as_text=True)
    assert 'x-data="usersApp()"' in html
    assert "js/users.js" in html
