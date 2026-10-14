from network_copilot.devices.model import Device
from network_copilot.extensions import db


def make_lab(client, headers, base):
    client.post(f"{base}/blueprints/branch", headers=headers, json={"params": {"hosts": 2}})
    client.post(f"{base}/notes", headers=headers, json={"text": "keep me"})


def topo(client, headers, base):
    return client.get(base, headers=headers).get_json()


def test_snapshot_diff_and_restore_round_trip(client, admin_headers, base):
    make_lab(client, admin_headers, base)
    before = topo(client, admin_headers, base)
    v = client.post(f"{base}/versions", headers=admin_headers, json={"name": "baseline"}).get_json()
    assert v["device_count"] == len(before["nodes"]) and v["link_count"] == len(before["links"])

    # change the design: drop a link, delete a note, add a device
    client.delete(f"{base}/links/{before['links'][0]['id']}", headers=admin_headers)
    client.delete(f"{base}/notes/{before['notes'][0]['id']}", headers=admin_headers)
    client.post(f"{base}/quick-device", headers=admin_headers, json={"template": "server"})
    d = client.get(f"{base}/versions/{v['id']}/diff", headers=admin_headers).get_json()
    assert len(d["links"]["missing_now"]) == 1 and len(d["devices"]["added_since"]) == 1
    assert d["notes"] == {"then": 1, "now": 0}

    res = client.post(f"{base}/versions/{v['id']}/restore", headers=admin_headers, json={})
    assert res.status_code == 200, res.get_json()
    after = topo(client, admin_headers, base)
    assert len(after["links"]) == len(before["links"]) and len(after["notes"]) == 1
    assert len(after["nodes"]) == len(before["nodes"]) + 1  # the extra server stays (no prune)

    # prune removes it
    client.post(f"{base}/versions/{v['id']}/restore", headers=admin_headers, json={"prune": True})
    assert len(topo(client, admin_headers, base)["nodes"]) == len(before["nodes"])
    # every restore left an automatic backup
    items = client.get(f"{base}/versions", headers=admin_headers).get_json()["items"]
    assert sum(1 for i in items if i["auto"]) == 2


def test_restore_recreates_deleted_devices_and_keeps_credentials(client, admin_headers, base):
    make_lab(client, admin_headers, base)
    nodes = topo(client, admin_headers, base)["nodes"]
    router = next(n for n in nodes if n["kind"] == "router")
    client.put(f"/api/devices/{router['id']}", headers=admin_headers, json={"monitoring_enabled": False})
    v = client.post(f"{base}/versions", headers=admin_headers, json={"name": "v"}).get_json()
    pc = next(n for n in nodes if n["kind"] == "pc")
    client.delete(f"/api/devices/{pc['id']}", headers=admin_headers)
    res = client.post(f"{base}/versions/{v['id']}/restore", headers=admin_headers, json={}).get_json()
    assert pc["hostname"] in res["created"]
    assert {n["hostname"] for n in topo(client, admin_headers, base)["nodes"]} == {n["hostname"] for n in nodes}


def test_versions_are_limited_and_scoped(client, admin_headers, viewer_headers, base):
    assert client.post(f"{base}/versions", headers=viewer_headers, json={"name": "x"}).status_code == 403
    assert client.post(f"{base}/versions", headers=admin_headers, json={"name": ""}).status_code == 422
    v = client.post(f"{base}/versions", headers=admin_headers, json={"name": "a"}).get_json()
    assert client.get(f"{base}/versions", headers=viewer_headers).status_code == 200
    assert client.delete(f"{base}/versions/{v['id']}", headers=admin_headers).status_code == 204
    assert client.get(f"{base}/versions/{v['id']}/diff", headers=admin_headers).status_code == 404


def test_version_limit(client, admin_headers, base):
    for i in range(30):
        assert client.post(f"{base}/versions", headers=admin_headers, json={"name": f"v{i}"}).status_code == 201
    assert client.post(f"{base}/versions", headers=admin_headers, json={"name": "one more"}).status_code == 409
