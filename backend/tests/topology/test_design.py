import pytest

from network_copilot.extensions import db
from network_copilot.topology.model import DesignAccessPort, DesignSvi, DesignVlan

from .conftest import link_payload


def add(client, headers, base, kind, **body):
    return client.post(f"{base}/design/{kind}", headers=headers, json=body)


@pytest.fixture
def d(base):
    return base + "/design"


def vlan(client, headers, base, vlan_id=10, name="USERS"):
    return add(client, headers, base, "vlans", vlan_id=vlan_id, name=name)


# -- VLANs -----------------------------------------------------------------------


def test_vlans_are_created_listed_and_unique(client, admin_headers, base):
    assert vlan(client, admin_headers, base).status_code == 201
    assert vlan(client, admin_headers, base).status_code == 409
    listing = client.get(f"{base}/design", headers=admin_headers).get_json()
    assert listing["vlans"] == [{"id": listing["vlans"][0]["id"], "vlan_id": 10, "name": "USERS"}]


@pytest.mark.parametrize("vlan_id", [1, 1002, 1005, 0, 4095])
def test_reserved_and_out_of_range_vlans_are_rejected(client, admin_headers, base, vlan_id):
    assert vlan(client, admin_headers, base, vlan_id=vlan_id).status_code == 422


def test_vlan_names_cannot_carry_commands(client, admin_headers, base):
    for name in ("a b", "x;reload", ""):
        assert vlan(client, admin_headers, base, name=name).status_code == 422


# -- access ports ------------------------------------------------------------------


def test_access_port_needs_a_defined_vlan_and_a_switch(client, admin_headers, base, lab):
    body = dict(device_id=lab["sw1"].id, interface="Gi0/5", vlan_id=10)
    assert add(client, admin_headers, base, "access-ports", **body).status_code == 422  # VLAN undefined
    vlan(client, admin_headers, base)
    assert add(client, admin_headers, base, "access-ports", **body).status_code == 201
    router = dict(body, device_id=lab["r1"].id)
    assert add(client, admin_headers, base, "access-ports", **router).status_code == 422  # not a switch
    asa = dict(body, device_id=lab["fw"].id)
    assert add(client, admin_headers, base, "access-ports", **asa).status_code == 422


def test_access_port_cannot_reuse_a_cabled_or_configured_port(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["sw1"], "GigabitEthernet0/1", lab["sw2"], "GigabitEthernet0/1"))
    body = dict(device_id=lab["sw1"].id, vlan_id=10)
    assert add(client, admin_headers, base, "access-ports", interface="gi0/1", **body).status_code == 409
    assert add(client, admin_headers, base, "access-ports", interface="Gi0/7", **body).status_code == 201
    assert add(client, admin_headers, base, "access-ports", interface="GI0/7", **body).status_code == 409


def test_a_vlan_in_use_cannot_be_deleted(client, admin_headers, base, lab):
    vid = vlan(client, admin_headers, base).get_json()["id"]
    port = add(client, admin_headers, base, "access-ports", device_id=lab["sw1"].id, interface="Gi0/5", vlan_id=10).get_json()
    blocked = client.delete(f"{base}/design/vlans/{vid}", headers=admin_headers)
    assert blocked.status_code == 409 and blocked.get_json()["details"]["used_by"]
    client.delete(f"{base}/design/access-ports/{port['id']}", headers=admin_headers)
    assert client.delete(f"{base}/design/vlans/{vid}", headers=admin_headers).status_code == 204


# -- SVIs -------------------------------------------------------------------------


def test_svi_validation(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    def svi(**extra):
        body = dict(device_id=lab["sw1"].id, vlan_id=10, ip="192.168.10.1", prefix_length=24)
        body.update(extra)
        return add(client, admin_headers, base, "svis", **body)

    assert svi(ip="192.168.10.0").status_code == 422        # network address
    assert svi(ip="192.168.10.255").status_code == 422      # broadcast
    assert svi(ip="172.16.3.50").status_code == 409         # inside the management network
    assert svi(prefix_length=31).status_code == 422
    assert svi(device_id=lab["r1"].id).status_code == 422   # a router has no SVI here
    assert svi().status_code == 201
    assert svi().status_code == 409                          # same VLAN twice on a device


def test_two_switches_may_share_a_vlans_subnet_but_not_overlap_others(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    vlan(client, admin_headers, base, 20, "VOICE")
    assert add(client, admin_headers, base, "svis", device_id=lab["sw1"].id, vlan_id=10, ip="192.168.10.1", prefix_length=24).status_code == 201
    assert add(client, admin_headers, base, "svis", device_id=lab["sw2"].id, vlan_id=10, ip="192.168.10.2", prefix_length=24).status_code == 201
    assert add(client, admin_headers, base, "svis", device_id=lab["sw2"].id, vlan_id=20, ip="192.168.10.9", prefix_length=24).status_code == 409


def test_svi_cannot_overlap_a_routed_link(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["r1"], "Gi0/1", lab["sw1"], "Gi0/0", link_type="routed", network="10.0.12.0/30"))
    response = add(client, admin_headers, base, "svis", device_id=lab["sw1"].id, vlan_id=10, ip="10.0.12.1", prefix_length=24)
    assert response.status_code == 409


# -- routes and OSPF -----------------------------------------------------------------


def test_route_validation(client, admin_headers, base, lab):
    def route(**extra):
        body = dict(device_id=lab["r1"].id, network="10.9.0.0/16", next_hop="10.0.12.2")
        body.update(extra)
        return add(client, admin_headers, base, "routes", **body)

    assert route(network="nonsense").status_code == 422
    assert route(next_hop="nope").status_code == 422
    assert route().status_code == 201
    assert route().status_code == 409
    assert route(device_id=lab["fw"].id).status_code == 201  # ASA routes are allowed


def test_ospf_is_one_process_per_device_and_editable(client, admin_headers, base, lab):
    first = add(client, admin_headers, base, "ospf", device_id=lab["r1"].id, process_id=1).get_json()
    again = add(client, admin_headers, base, "ospf", device_id=lab["r1"].id, process_id=7, router_id="1.1.1.1").get_json()
    assert again["id"] == first["id"] and again["process_id"] == 7
    assert add(client, admin_headers, base, "ospf", device_id=lab["r1"].id, router_id="bad").status_code == 422
    assert len(client.get(f"{base}/design", headers=admin_headers).get_json()["ospf"]) == 1


def test_unknown_kind_and_foreign_items(client, admin_headers, base, lab):
    assert add(client, admin_headers, base, "toasters").status_code == 404
    assert client.delete(f"{base}/design/vlans/999", headers=admin_headers).status_code == 404


def test_viewers_can_read_but_not_change_the_design(client, viewer_headers, base):
    assert client.get(f"{base}/design", headers=viewer_headers).status_code == 200
    assert add(client, viewer_headers, base, "vlans", vlan_id=10, name="X").status_code == 403


def test_deleting_a_device_removes_its_design_items(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    add(client, admin_headers, base, "access-ports", device_id=lab["sw1"].id, interface="Gi0/5", vlan_id=10)
    add(client, admin_headers, base, "svis", device_id=lab["sw1"].id, vlan_id=10, ip="192.168.10.1", prefix_length=24)
    assert client.delete(f"/api/devices/{lab['sw1'].id}", headers=admin_headers).status_code == 204
    assert db.session.query(DesignAccessPort).count() == 0 and db.session.query(DesignSvi).count() == 0
    assert db.session.query(DesignVlan).count() == 1  # the VLAN itself stays


# -- generated configuration ---------------------------------------------------------


def plan(client, headers, base, **body):
    response = client.post(f"{base}/config-plan", headers=headers, json=body)
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()
    return {e["hostname"]: e["commands"] for e in data["devices"]}, data


def test_plan_covers_vlans_ports_svis_routes_and_ospf(client, admin_headers, base, lab):
    vlan(client, admin_headers, base, 10, "USERS")
    add(client, admin_headers, base, "access-ports", device_id=lab["sw1"].id, interface="GigabitEthernet0/5", vlan_id=10)
    add(client, admin_headers, base, "svis", device_id=lab["sw1"].id, vlan_id=10, ip="192.168.10.1", prefix_length=24)
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["r1"], "GigabitEthernet0/1", lab["sw1"], "GigabitEthernet0/0",
                                  link_type="routed", network="10.0.12.0/30"))
    add(client, admin_headers, base, "routes", device_id=lab["r1"].id, network="192.168.99.0/24", next_hop="10.0.12.2")
    add(client, admin_headers, base, "ospf", device_id=lab["sw1"].id, process_id=1, router_id="2.2.2.2")
    add(client, admin_headers, base, "ospf", device_id=lab["r1"].id, process_id=1)

    commands, _ = plan(client, admin_headers, base)

    assert commands["SW1"] == [
        "vlan 10", "name USERS",
        "interface GigabitEthernet0/5", "switchport mode access", "switchport access vlan 10",
        "interface GigabitEthernet0/0", "description LINK TO R1 GigabitEthernet0/1",
        "ip address 10.0.12.2 255.255.255.252", "no shutdown",
        "interface Vlan10", "ip address 192.168.10.1 255.255.255.0", "no shutdown",
        "router ospf 1", "router-id 2.2.2.2",
        "network 10.0.12.0 0.0.0.3 area 0", "network 192.168.10.0 0.0.0.255 area 0",
    ]
    assert commands["R1"] == [
        "interface GigabitEthernet0/1", "description LINK TO SW1 GigabitEthernet0/0",
        "ip address 10.0.12.1 255.255.255.252", "no shutdown",
        "ip route 192.168.99.0 255.255.255.0 10.0.12.2",
        "router ospf 1", "network 10.0.12.0 0.0.0.3 area 0",
    ]
    assert "SW2" in commands and commands["SW2"] == ["vlan 10", "name USERS"]  # VLAN database on every switch
    assert "vlan 10" not in commands["R1"]


def test_selecting_links_limits_the_plan_to_those_cables(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    link = client.post(f"{base}/links", headers=admin_headers,
                       json=link_payload(lab["r1"], "Gi0/1", lab["sw1"], "Gi0/0")).get_json()
    commands, _ = plan(client, admin_headers, base, link_ids=[link["id"]])
    assert set(commands) == {"R1", "SW1"} and "vlan 10" not in commands["SW1"]


def test_plan_works_from_settings_alone(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    commands, _ = plan(client, admin_headers, base)
    assert set(commands) == {"SW1", "SW2"}


def test_asa_gets_interfaces_with_names_and_routes(client, admin_headers, base, lab):
    link = client.post(f"{base}/links", headers=admin_headers,
                       json=link_payload(lab["r1"], "GigabitEthernet0/1", lab["fw"], "GigabitEthernet0/0",
                                         link_type="routed", network="10.0.20.0/30",
                                         nameif_b="outside", security_b=0)).get_json()
    assert link["nameif_b"] == "outside"
    add(client, admin_headers, base, "routes", device_id=lab["fw"].id, network="0.0.0.0/0", next_hop="10.0.20.1")
    commands, _ = plan(client, admin_headers, base)
    assert commands["FW1"] == [
        "interface GigabitEthernet0/0", "nameif outside", "security-level 0",
        "ip address 10.0.20.2 255.255.255.252", "no shutdown",
        "route outside 0.0.0.0 0.0.0.0 10.0.20.1",
    ]


def test_asa_defaults_and_unsupported_parts_are_reported(client, admin_headers, base, lab):
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["r1"], "Gi0/1", lab["fw"], "Gi0/0", link_type="routed", network="10.0.20.0/30"))
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["sw1"], "Gi0/1", lab["fw"], "Gi0/1", link_type="trunk"))
    add(client, admin_headers, base, "routes", device_id=lab["fw"].id, network="10.9.0.0/16", next_hop="172.30.0.1")
    commands, data = plan(client, admin_headers, base)
    assert "nameif to-r1" in commands["FW1"] and "security-level 0" in commands["FW1"]
    notes = next(d["notes"] for d in data["devices"] if d["hostname"] == "FW1")
    assert any("only routed links" in n for n in notes) and any("next hop" in n for n in notes)


def test_design_preview_freezes_every_device_into_one_batch(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["r1"], "Gi0/1", lab["sw1"], "Gi0/0", link_type="routed", network="10.0.12.0/30"))
    add(client, admin_headers, base, "ospf", device_id=lab["r1"].id)
    body = client.post(f"{base}/config-preview", headers=admin_headers, json={}).get_json()
    by_host = {c["device"]["hostname"]: c["commands"] for c in body["batch"]["changes"]}
    assert set(by_host) == {"R1", "SW1", "SW2"}
    assert "router ospf 1" in by_host["R1"] and "vlan 10" in by_host["SW2"]


def test_automatic_ports_skip_abbreviated_and_access_ports(client, admin_headers, base, lab):
    vlan(client, admin_headers, base)
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["r1"], "Gi0/0", lab["sw1"], "Gi0/9"))
    add(client, admin_headers, base, "access-ports", device_id=lab["sw1"].id, interface="Gi0/0", vlan_id=10)
    link = client.post(f"{base}/links", headers=admin_headers,
                       json={"device_a_id": lab["r1"].id, "device_b_id": lab["sw1"].id}).get_json()
    assert link["interface_a"] == "GigabitEthernet0/1"   # Gi0/0 is taken, whatever its spelling
    assert link["interface_b"] == "GigabitEthernet0/1"   # Gi0/9 is cabled, Gi0/0 is an access port
