import pytest


def device(client, headers, base, template="router"):
    return client.post(f"{base}/quick-device", headers=headers, json={"template": template}).get_json()


def plan(client, headers, base):
    return client.post(f"{base}/config-plan", headers=headers, json={}).get_json()


def commands_for(plan_body, hostname):
    return next(d["commands"] for d in plan_body["devices"] if d["hostname"] == hostname)


def test_port_channel_config_and_rules(client, admin_headers, base):
    a, b, c = (device(client, admin_headers, base, "switch") for _ in range(3))
    first = client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": a["id"], "device_b_id": b["id"], "link_type": "trunk", "allowed_vlans": "10,20", "channel_id": 1})
    second = client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": a["id"], "device_b_id": b["id"], "link_type": "trunk", "allowed_vlans": "10,20", "channel_id": 1})
    assert first.status_code == 201 and second.status_code == 201
    # same channel number towards another neighbour is refused
    assert client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": a["id"], "device_b_id": c["id"], "link_type": "trunk", "channel_id": 1}).status_code == 409
    # only trunks can be bundled; members carry the same VLANs
    assert client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": b["id"], "device_b_id": c["id"], "channel_id": 2}).status_code == 422
    assert client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": a["id"], "device_b_id": b["id"], "link_type": "trunk", "allowed_vlans": "30", "channel_id": 1}).status_code == 422

    cmds = commands_for(plan(client, admin_headers, base), a["hostname"])
    assert cmds.count("interface Port-channel1") == 1
    assert cmds.count("channel-group 1 mode active") == 2
    assert "switchport trunk allowed vlan 10,20" in cmds


def test_changing_a_bundled_link_to_routed_leaves_the_channel(client, admin_headers, base):
    a, b = device(client, admin_headers, base, "switch_l3"), device(client, admin_headers, base, "switch_l3")
    link = client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": a["id"], "device_b_id": b["id"], "link_type": "trunk", "channel_id": 3}).get_json()
    updated = client.put(f"{base}/links/{link['id']}", headers=admin_headers,
                         json={"link_type": "routed", "network": "auto"})
    assert updated.status_code == 200 and updated.get_json()["channel_id"] is None


def test_loopback_is_advertised_by_ospf(client, admin_headers, base):
    r1, r2 = device(client, admin_headers, base), device(client, admin_headers, base)
    client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": r1["id"], "device_b_id": r2["id"], "link_type": "routed", "network": "auto"})
    made = client.post(f"{base}/design/loopbacks", headers=admin_headers,
                       json={"device_id": r1["id"], "number": 0, "ip": "1.1.1.1"})
    assert made.status_code == 201
    assert client.post(f"{base}/design/loopbacks", headers=admin_headers,
                       json={"device_id": r2["id"], "number": 0, "ip": "1.1.1.1"}).status_code == 409
    client.post(f"{base}/design/ospf", headers=admin_headers, json={"device_id": r1["id"]})
    cmds = commands_for(plan(client, admin_headers, base), r1["hostname"])
    assert "interface Loopback0" in cmds and "ip address 1.1.1.1 255.255.255.255" in cmds
    assert "network 1.1.1.1 0.0.0.0 area 0" in cmds


def test_router_on_a_stick_subinterfaces(client, admin_headers, base):
    router, sw, pc = device(client, admin_headers, base), device(client, admin_headers, base, "switch"), None
    client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": router["id"], "interface_a": "Gi0/0", "device_b_id": sw["id"], "link_type": "trunk"})
    client.post(f"{base}/design/vlans", headers=admin_headers, json={"vlan_id": 10, "name": "USERS"})
    client.post(f"{base}/design/vlans", headers=admin_headers, json={"vlan_id": 20, "name": "VOICE"})
    for vlan, ip in ((10, "192.168.10.1"), (20, "192.168.20.1")):
        res = client.post(f"{base}/design/subinterfaces", headers=admin_headers, json={
            "device_id": router["id"], "interface": "Gi0/0", "vlan_id": vlan, "ip": ip, "prefix_length": 24})
        assert res.status_code == 201, res.get_json()
    # a switch cannot host subinterfaces; undefined VLANs are refused
    assert client.post(f"{base}/design/subinterfaces", headers=admin_headers, json={
        "device_id": sw["id"], "interface": "Gi0/5", "vlan_id": 10, "ip": "192.168.99.1"}).status_code == 422
    assert client.post(f"{base}/design/subinterfaces", headers=admin_headers, json={
        "device_id": router["id"], "interface": "Gi0/0", "vlan_id": 77, "ip": "192.168.77.1"}).status_code in (404, 409, 422)

    cmds = commands_for(plan(client, admin_headers, base), router["hostname"])
    assert "interface GigabitEthernet0/0.10" in cmds and "encapsulation dot1Q 10" in cmds
    assert "ip address 192.168.20.1 255.255.255.0" in cmds
    # the VLAN is in use now
    vlan = next(v for v in client.get(f"{base}/design", headers=admin_headers).get_json()["vlans"] if v["vlan_id"] == 10)
    assert client.delete(f"{base}/design/vlans/{vlan['id']}", headers=admin_headers).status_code == 409


def test_new_design_items_survive_export_import_and_versions(client, admin_headers, base, project):
    r = device(client, admin_headers, base)
    client.post(f"{base}/design/loopbacks", headers=admin_headers, json={"device_id": r["id"], "ip": "9.9.9.9"})
    client.post(f"{base}/design/vlans", headers=admin_headers, json={"vlan_id": 30, "name": "X"})
    client.post(f"{base}/design/subinterfaces", headers=admin_headers, json={
        "device_id": r["id"], "interface": "Gi0/1", "vlan_id": 30, "ip": "10.77.0.1"})
    doc = client.get(f"/api/projects/{project.id}/export", headers=admin_headers).get_json()
    assert doc["design"]["loopbacks"] and doc["design"]["subinterfaces"]
    copy = client.post(f"/api/projects/{project.id}/clone", headers=admin_headers, json={}).get_json()
    design = client.get(f"/api/projects/{copy['id']}/topology/design", headers=admin_headers).get_json()
    assert len(design["loopbacks"]) == 1 and len(design["subinterfaces"]) == 1

    v = client.post(f"{base}/versions", headers=admin_headers, json={"name": "s"}).get_json()
    assert client.post(f"{base}/versions/{v['id']}/restore", headers=admin_headers, json={}).status_code == 200
    again = client.get(f"{base}/design", headers=admin_headers).get_json()
    assert len(again["loopbacks"]) == 1 and len(again["subinterfaces"]) == 1


def test_lint_flags_single_member_channels(client, admin_headers, base):
    a, b = device(client, admin_headers, base, "switch"), device(client, admin_headers, base, "switch")
    client.post(f"{base}/links", headers=admin_headers, json={
        "device_a_id": a["id"], "device_b_id": b["id"], "link_type": "trunk", "channel_id": 4})
    issues = client.get(f"{base}/lint", headers=admin_headers).get_json()["issues"]
    assert "channel_single" in {i["code"] for i in issues}
