import pytest

from network_copilot.topology import blueprints


def test_catalog_lists_every_blueprint(client, admin_headers, base):
    items = client.get(f"{base}/blueprints", headers=admin_headers).get_json()["items"]
    assert {i["key"] for i in items} == set(blueprints.BLUEPRINTS)


@pytest.mark.parametrize("key", sorted(blueprints.BLUEPRINTS))
def test_every_blueprint_builds_a_connected_topology(client, admin_headers, base, key):
    res = client.post(f"{base}/blueprints/{key}", headers=admin_headers, json={})
    assert res.status_code == 201, res.get_json()
    body = res.get_json()
    ids = {d["id"] for d in body["devices"]}
    assert len(ids) >= 3 and body["links"]
    # connected: union-find over the links
    parent = {i: i for i in ids}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for link in body["links"]:
        parent[find(link["device_a_id"])] = find(link["device_b_id"])
    assert len({find(i) for i in ids}) == 1
    routed = [l["network"] for l in body["links"] if l["link_type"] == "routed"]
    assert len(routed) == len(set(routed))


def test_planned_blueprint_creates_planned_managed_devices(client, admin_headers, base):
    body = client.post(f"{base}/blueprints/branch", headers=admin_headers, json={"planned": True}).get_json()
    managed = [d for d in body["devices"] if d["managed"]]
    assert managed and all(d["lifecycle"] == "planned" for d in managed)


def test_parameters_are_clamped_and_unknown_blueprints_404(client, admin_headers, base):
    body = client.post(f"{base}/blueprints/ring", headers=admin_headers, json={"params": {"routers": 99}}).get_json()
    assert len(body["devices"]) == 10
    assert client.post(f"{base}/blueprints/nope", headers=admin_headers, json={}).status_code == 404


def test_viewer_cannot_build(client, viewer_headers, base):
    assert client.post(f"{base}/blueprints/branch", headers=viewer_headers, json={}).status_code == 403


def test_ipam_avoids_used_networks(client, admin_headers, base):
    client.post(f"{base}/blueprints/ring", headers=admin_headers, json={"params": {"routers": 3}})
    info = client.get(f"{base}/ipam", headers=admin_headers).get_json()
    assert len(info["used"]) == 3 and info["next_link"] not in info["used"]
    assert info["next_lan"] == "10.100.1.0/24"


def test_auto_svi_shares_a_vlan_subnet_and_follows_the_pool(client, admin_headers, base, project):
    assert client.put(f"/api/projects/{project.id}", headers=admin_headers,
                      json={"addressing_pool": "192.168.0.0/16"}).status_code == 200
    a = client.post(f"{base}/quick-device", headers=admin_headers, json={"template": "switch_l3"}).get_json()
    b = client.post(f"{base}/quick-device", headers=admin_headers, json={"template": "switch_l3"}).get_json()
    client.post(f"{base}/design/vlans", headers=admin_headers, json={"vlan_id": 10, "name": "USERS"})
    first = client.post(f"{base}/design/svis", headers=admin_headers,
                        json={"device_id": a["id"], "vlan_id": 10, "ip": "auto"})
    second = client.post(f"{base}/design/svis", headers=admin_headers,
                         json={"device_id": b["id"], "vlan_id": 10, "ip": "auto"})
    assert first.status_code == 201 and second.status_code == 201, (first.get_json(), second.get_json())
    assert first.get_json()["ip"] == "192.168.0.1" and second.get_json()["ip"] == "192.168.0.2"
    assert second.get_json()["prefix_length"] == 24


def test_invalid_pool_is_rejected(client, admin_headers, project):
    res = client.put(f"/api/projects/{project.id}", headers=admin_headers, json={"addressing_pool": "8.8.8.0/24"})
    assert res.status_code == 422


def codes(client, headers, base):
    return {i["code"] for i in client.get(f"{base}/lint", headers=headers).get_json()["issues"]}


def test_lint_reports_isolated_planned_and_missing_routing(client, admin_headers, base):
    client.post(f"{base}/blueprints/ring", headers=admin_headers, json={"params": {"routers": 3}})
    client.post(f"{base}/quick-device", headers=admin_headers, json={"template": "router", "planned": True})
    found = codes(client, admin_headers, base)
    assert {"isolated", "planned", "no_routing"} <= found


def test_lint_is_clean_for_a_routed_design_with_ospf(client, admin_headers, base):
    body = client.post(f"{base}/blueprints/ring", headers=admin_headers, json={"params": {"routers": 3}}).get_json()
    for device in body["devices"]:
        client.post(f"{base}/design/ospf", headers=admin_headers, json={"device_id": device["id"], "process_id": 1})
    result = client.get(f"{base}/lint", headers=admin_headers).get_json()
    assert result["ok"] and "no_routing" not in {i["code"] for i in result["issues"]}


def test_lint_flags_duplicate_host_addresses(client, admin_headers, base):
    ids = [client.post(f"{base}/quick-device", headers=admin_headers, json={"template": "pc"}).get_json()["id"]
           for _ in range(2)]
    for device_id in ids:
        client.put(f"/api/devices/{device_id}", headers=admin_headers, json={"data_ip": "10.1.1.5"})
    result = client.get(f"{base}/lint", headers=admin_headers).get_json()
    assert not result["ok"] and "duplicate_ip" in {i["code"] for i in result["issues"]}


def test_notes_crud_and_validation(client, admin_headers, base, viewer_headers):
    made = client.post(f"{base}/notes", headers=admin_headers,
                       json={"kind": "group", "text": "Building A", "x": 40, "y": 40, "width": 300, "height": 200,
                             "color": "#3366cc"})
    assert made.status_code == 201
    note_id = made.get_json()["id"]
    assert client.put(f"{base}/notes/{note_id}", headers=admin_headers, json={"text": "HQ"}).get_json()["text"] == "HQ"
    assert client.get(f"{base}", headers=admin_headers).get_json()["notes"][0]["text"] == "HQ"
    assert client.post(f"{base}/notes", headers=admin_headers, json={"text": "x", "color": "red"}).status_code == 422
    assert client.post(f"{base}/notes", headers=viewer_headers, json={"text": "x"}).status_code == 403
    assert client.delete(f"{base}/notes/{note_id}", headers=admin_headers).status_code == 204
    assert client.delete(f"{base}/notes/{note_id}", headers=admin_headers).status_code == 404


def test_notes_survive_export_and_clone(client, admin_headers, base, project):
    client.post(f"{base}/notes", headers=admin_headers, json={"text": "Remember", "x": 5, "y": 6})
    doc = client.get(f"/api/projects/{project.id}/export", headers=admin_headers).get_json()
    assert doc["notes"][0]["text"] == "Remember"
    copy = client.post(f"/api/projects/{project.id}/clone", headers=admin_headers, json={}).get_json()
    topo = client.get(f"/api/projects/{copy['id']}/topology", headers=admin_headers).get_json()
    assert [n["text"] for n in topo["notes"]] == ["Remember"]
