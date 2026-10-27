import pytest


def new_project(client, headers, name="P", net="10.30.0.0/24"):
    return client.post("/api/projects", headers=headers, json={"name": name, "management_network": net})


def test_project_quota_per_user_exempts_admins(app, client, alice_headers, admin_headers):
    app.config["MAX_PROJECTS_PER_USER"] = 2
    assert new_project(client, alice_headers, "A").status_code == 201
    assert new_project(client, alice_headers, "B").status_code == 201
    assert new_project(client, alice_headers, "C").status_code == 409
    for name in "XYZ":
        assert new_project(client, admin_headers, name).status_code == 201


def test_device_and_link_quotas(app, client, alice_headers):
    project = new_project(client, alice_headers).get_json()
    base = f"/api/projects/{project['id']}/topology"
    app.config["MAX_DEVICES_PER_PROJECT"] = 3
    app.config["MAX_LINKS_PER_PROJECT"] = 1
    ids = []
    for _ in range(3):
        res = client.post(f"{base}/quick-device", headers=alice_headers, json={"template": "pc"})
        assert res.status_code == 201
        ids.append(res.get_json()["id"])
    assert client.post(f"{base}/quick-device", headers=alice_headers, json={"template": "pc"}).status_code == 409
    assert client.post(f"{base}/links", headers=alice_headers,
                       json={"device_a_id": ids[0], "device_b_id": ids[1]}).status_code == 201
    assert client.post(f"{base}/links", headers=alice_headers,
                       json={"device_a_id": ids[1], "device_b_id": ids[2]}).status_code == 409


def test_blueprint_over_the_device_quota_leaves_nothing_behind(app, client, alice_headers):
    project = new_project(client, alice_headers).get_json()
    base = f"/api/projects/{project['id']}/topology"
    app.config["MAX_DEVICES_PER_PROJECT"] = 4
    assert client.post(f"{base}/blueprints/campus", headers=alice_headers, json={}).status_code == 409
    assert client.get(base, headers=alice_headers).get_json()["nodes"] == []


def test_share_candidates_search_and_leave(client, alice_headers, bob_headers, alice, bob):
    from conftest import _auth_headers, _create_user

    from .conftest import PASSWORD

    _create_user("bonnie", PASSWORD, "VIEWER")
    bonnie_headers = _auth_headers(client, "bonnie", PASSWORD)
    project = new_project(client, alice_headers).get_json()
    pid = project["id"]
    # An Operator may only add Viewer accounts, so the Operator "bob" is not offered.
    found = client.get(f"/api/projects/{pid}/share-candidates?q=bo", headers=alice_headers).get_json()["items"]
    assert [u["username"] for u in found] == ["bonnie"]
    assert client.get(f"/api/projects/{pid}/share-candidates?q=b", headers=alice_headers).get_json()["items"] == []
    assert client.get(f"/api/projects/{pid}/share-candidates?q=bo", headers=bob_headers).status_code == 404

    client.post(f"/api/projects/{pid}/members", headers=alice_headers, json={"username": "bonnie"})
    assert client.get(f"/api/projects/{pid}/share-candidates?q=bo", headers=alice_headers).get_json()["items"] == []
    assert client.delete(f"/api/projects/{pid}/membership", headers=alice_headers).status_code == 409  # owner
    assert client.delete(f"/api/projects/{pid}/membership", headers=bonnie_headers).status_code == 204
    assert client.get(f"/api/projects/{pid}", headers=bonnie_headers).status_code == 404
