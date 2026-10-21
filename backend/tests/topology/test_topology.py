import pytest

from network_copilot.changes.model import ChangeBatch
from network_copilot.chat.service import record_message
from network_copilot.chat.session_service import create_session
from network_copilot.extensions import db
from network_copilot.topology.model import TopologyLink

from .conftest import link_payload


def add(client, headers, base, lab, a, if_a, b, if_b, **extra):
    return client.post(
        f"{base}/links",
        headers=headers,
        json=link_payload(lab[a], if_a, lab[b], if_b, **extra),
    )


def test_topology_lists_devices_and_links(client, admin_headers, base, lab):
    add(client, admin_headers, base, lab, "r1", "GigabitEthernet0/1", "sw1", "GigabitEthernet0/0")
    body = client.get(base, headers=admin_headers).get_json()
    assert {n["hostname"] for n in body["nodes"]} == {"R1", "SW1", "SW2", "FW1"}
    assert len(body["links"]) == 1
    link = body["links"][0]
    assert (link["hostname_a"], link["hostname_b"]) == ("R1", "SW1")
    assert link["link_type"] == "physical"


def test_ai_suggestion_matches_the_requested_chat_session(
    client, viewer_headers, admin_user, project, base
):
    first = create_session(project.id, created_by_id=admin_user.id)
    second = create_session(project.id, created_by_id=admin_user.id)
    record_message(
        admin_user.id,
        admin_user.username,
        "assistant",
        "R1 connects to SW1.",
        {"diagram": {
            "title": "Chat topology",
            "nodes": [
                {"id": "r1", "label": "R1", "kind": "router", "ip_addresses": ["10.10.0.1/24"]},
                {"id": "sw1", "label": "SW1", "kind": "switch", "ip_addresses": ["10.10.0.2/24"]},
            ],
            "links": [{"source": "r1", "target": "sw1", "network": "10.10.0.0/24"}],
        }},
        session_id=first.id,
        project_id=project.id,
    )
    record_message(
        admin_user.id,
        admin_user.username,
        "assistant",
        "Another topology.",
        {"diagram": {
            "title": "Other session",
            "nodes": [
                {"id": "r2", "label": "R2", "kind": "router"},
                {"id": "sw2", "label": "SW2", "kind": "switch"},
            ],
            "links": [{"source": "r2", "target": "sw2"}],
        }},
        session_id=second.id,
        project_id=project.id,
    )

    response = client.get(
        f"{base}/ai-suggestion?session_id={first.id}", headers=viewer_headers
    )

    assert response.status_code == 200
    suggestion = response.get_json()["suggestion"]
    assert suggestion["message_id"] is not None
    assert suggestion["diagram"]["title"] == "Chat topology"
    assert suggestion["diagram"]["nodes"][0]["ip_addresses"] == ["10.10.0.1/24"]
    assert suggestion["diagram"]["links"][0]["network"] == "10.10.0.0/24"


def test_ai_suggestion_ignores_invalid_saved_diagrams(client, admin_headers, admin_user, project, base):
    session = create_session(project.id, created_by_id=admin_user.id)
    record_message(
        admin_user.id,
        admin_user.username,
        "assistant",
        "Invalid address.",
        {"diagram": {
            "title": "Invalid",
            "nodes": [
                {"id": "r1", "label": "R1", "kind": "router", "ip_addresses": ["not-an-ip"]},
                {"id": "sw1", "label": "SW1", "kind": "switch"},
            ],
            "links": [{"source": "r1", "target": "sw1"}],
        }},
        session_id=session.id,
        project_id=project.id,
    )

    response = client.get(
        f"{base}/ai-suggestion?session_id={session.id}", headers=admin_headers
    )

    assert response.status_code == 200
    assert response.get_json()["suggestion"] is None


def test_layout_is_saved_per_device(client, admin_headers, base, lab):
    response = client.put(
        f"{base}/layout",
        headers=admin_headers,
        json={"positions": [{"device_id": lab["r1"].id, "x": 120.5, "y": 40}]},
    )
    assert response.status_code == 200
    nodes = {n["hostname"]: n for n in response.get_json()["nodes"]}
    assert (nodes["R1"]["pos_x"], nodes["R1"]["pos_y"]) == (120.5, 40.0)
    assert nodes["SW1"]["pos_x"] is None


def test_layout_rejects_devices_of_another_project(client, admin_headers, base, lab):
    from conftest import _create_device
    from network_copilot.projects.model import Project

    other = Project(name="Other", management_network="10.9.0.0/24")
    db.session.add(other)
    db.session.commit()
    foreign = _create_device("X1", "10.9.0.1", "core", project=other)
    response = client.put(
        f"{base}/layout",
        headers=admin_headers,
        json={"positions": [{"device_id": foreign.id, "x": 1, "y": 1}]},
    )
    assert response.status_code == 422


def test_link_endpoints_must_belong_to_the_project(client, admin_headers, base, lab):
    from conftest import _create_device
    from network_copilot.projects.model import Project

    other = Project(name="Other", management_network="10.9.0.0/24")
    db.session.add(other)
    db.session.commit()
    foreign = _create_device("X1", "10.9.0.1", "core", project=other)
    response = client.post(
        f"{base}/links",
        headers=admin_headers,
        json=link_payload(lab["r1"], "Gi0/1", foreign, "Gi0/1"),
    )
    assert response.status_code == 422


def test_a_link_needs_two_different_devices(client, admin_headers, base, lab):
    response = add(client, admin_headers, base, lab, "r1", "Gi0/1", "r1", "Gi0/2")
    assert response.status_code == 422


def test_an_interface_can_only_be_cabled_once(client, admin_headers, base, lab):
    assert add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0").status_code == 201
    # Same interface on either end, any capitalisation.
    assert add(client, admin_headers, base, lab, "r1", "gi0/1", "sw2", "Gi0/0").status_code == 409
    assert add(client, admin_headers, base, lab, "sw2", "Gi0/1", "sw1", "Gi0/0").status_code == 409


@pytest.mark.parametrize("bad", ["Gi0/1; reload", "Gi0/1\nshutdown", "0/1", "Gi", "Gi0/1 | x"])
def test_interface_names_cannot_smuggle_commands(client, admin_headers, base, lab, bad):
    response = add(client, admin_headers, base, lab, "r1", bad, "sw1", "Gi0/0")
    assert response.status_code == 422


def test_routed_link_derives_addresses_from_the_network(client, admin_headers, base, lab):
    response = add(
        client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0",
        link_type="routed", network="10.0.12.0/30",
    )
    assert response.status_code == 201
    body = response.get_json()
    assert (body["ip_a"], body["ip_b"]) == ("10.0.12.1", "10.0.12.2")


def test_routed_link_validation(client, admin_headers, base, lab):
    def routed(**extra):
        return add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0",
                   link_type="routed", **extra)

    assert routed().status_code == 422  # no network
    assert routed(network="nonsense").status_code == 422
    assert routed(network="10.0.12.0/32").status_code == 422
    assert routed(network="172.16.3.0/30").status_code == 422  # overlaps management
    assert routed(network="10.0.12.0/30", ip_a="10.0.12.3", ip_b="10.0.12.3").status_code == 422
    assert routed(network="10.0.12.0/30", ip_a="10.0.12.0").status_code == 422  # network address
    assert routed(network="10.0.12.0/30", ip_a="10.9.9.9").status_code == 422  # outside


def test_routed_links_cannot_overlap(client, admin_headers, base, lab):
    first = add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0",
                link_type="routed", network="10.0.12.0/30")
    assert first.status_code == 201
    clash = add(client, admin_headers, base, lab, "r1", "Gi0/2", "sw2", "Gi0/0",
                link_type="routed", network="10.0.12.0/24")
    assert clash.status_code == 409


def test_only_routed_links_carry_addresses(client, admin_headers, base, lab):
    assert add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0",
               network="10.0.12.0/30").status_code == 422
    assert add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0",
               link_type="trunk", ip_a="10.0.0.1").status_code == 422


def test_trunk_vlan_list_is_normalised_and_validated(client, admin_headers, base, lab):
    ok = add(client, admin_headers, base, lab, "sw1", "Gi0/1", "sw2", "Gi0/1",
             link_type="trunk", allowed_vlans="30, 10,20,11-13,12")
    assert ok.status_code == 201
    assert ok.get_json()["allowed_vlans"] == "10-13,20,30"
    for bad in ("0", "4095", "5-3", "a", "10,,20"):
        assert add(client, admin_headers, base, lab, "sw1", "Gi0/2", "sw2", "Gi0/2",
                   link_type="trunk", allowed_vlans=bad).status_code == 422


def test_update_link_rederives_addresses_when_the_network_changes(client, admin_headers, base, lab):
    link = add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0",
               link_type="routed", network="10.0.12.0/30").get_json()
    response = client.put(
        f"{base}/links/{link['id']}", headers=admin_headers, json={"network": "10.0.99.0/30"}
    )
    assert response.status_code == 200
    assert (response.get_json()["ip_a"], response.get_json()["ip_b"]) == ("10.0.99.1", "10.0.99.2")


def test_switching_a_link_to_trunk_clears_routed_fields(client, admin_headers, base, lab):
    link = add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0",
               link_type="routed", network="10.0.12.0/30").get_json()
    response = client.put(
        f"{base}/links/{link['id']}", headers=admin_headers,
        json={"link_type": "trunk", "allowed_vlans": "10,20"},
    )
    body = response.get_json()
    assert response.status_code == 200
    assert (body["network"], body["ip_a"], body["allowed_vlans"]) == (None, None, "10,20")


def test_delete_link_and_unknown_link(client, admin_headers, base, lab):
    link = add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0").get_json()
    assert client.delete(f"{base}/links/{link['id']}", headers=admin_headers).status_code == 204
    assert client.delete(f"{base}/links/{link['id']}", headers=admin_headers).status_code == 404


def test_deleting_a_device_removes_its_links(client, admin_headers, lab):
    db.session.add(TopologyLink(
        project_id=lab["r1"].project_id, device_a_id=lab["r1"].id, interface_a="Gi0/1",
        device_b_id=lab["sw1"].id, interface_b="Gi0/0", link_type="physical",
    ))
    db.session.commit()
    assert client.delete(f"/api/devices/{lab['r1'].id}", headers=admin_headers).status_code == 204
    assert db.session.query(TopologyLink).count() == 0


def test_viewers_can_look_but_not_edit_the_design(client, viewer_headers, base, lab):
    assert client.get(base, headers=viewer_headers).status_code == 200
    assert add(client, viewer_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0").status_code == 403
    assert client.put(f"{base}/layout", headers=viewer_headers, json={"positions": []}).status_code == 403
    assert client.post(f"{base}/config-plan", headers=viewer_headers, json={}).status_code == 403


# -- configuration generation -------------------------------------------------


def build_design(client, headers, base, lab):
    add(client, headers, base, lab, "r1", "GigabitEthernet0/1", "sw1", "GigabitEthernet0/0",
        link_type="routed", network="10.0.12.0/30")
    add(client, headers, base, lab, "sw1", "GigabitEthernet0/1", "sw2", "GigabitEthernet0/1",
        link_type="trunk", allowed_vlans="10,20")


def test_plan_lists_commands_per_device_without_side_effects(client, admin_headers, base, lab):
    build_design(client, admin_headers, base, lab)
    response = client.post(f"{base}/config-plan", headers=admin_headers, json={})
    assert response.status_code == 200
    plan = {d["hostname"]: d["commands"] for d in response.get_json()["devices"]}

    assert plan["R1"] == [
        "interface GigabitEthernet0/1",
        "description LINK TO SW1 GigabitEthernet0/0",
        "ip address 10.0.12.1 255.255.255.252",
        "no shutdown",
    ]
    assert plan["SW1"] == [
        "interface GigabitEthernet0/0",
        "description LINK TO R1 GigabitEthernet0/1",
        "ip address 10.0.12.2 255.255.255.252",
        "no shutdown",
        "interface GigabitEthernet0/1",
        "description LINK TO SW2 GigabitEthernet0/1",
        "switchport mode trunk",
        "switchport trunk allowed vlan 10,20",
        "no shutdown",
    ]
    assert plan["SW2"][2] == "switchport mode trunk"
    assert db.session.query(ChangeBatch).count() == 0


def test_bring_up_false_leaves_the_interface_alone(client, admin_headers, base, lab):
    add(client, admin_headers, base, lab, "sw1", "Gi0/1", "sw2", "Gi0/1", bring_up=False)
    plan = client.post(f"{base}/config-plan", headers=admin_headers, json={}).get_json()
    assert all("no shutdown" not in d["commands"] for d in plan["devices"])


def test_asa_devices_are_reported_not_guessed(client, admin_headers, base, lab):
    add(client, admin_headers, base, lab, "r1", "Gi0/1", "fw", "GigabitEthernet0/0")
    plan = client.post(f"{base}/config-plan", headers=admin_headers, json={}).get_json()
    assert [d["hostname"] for d in plan["devices"]] == ["R1"]
    assert [s["hostname"] for s in plan["skipped"]] == ["FW1"]


def test_plan_can_be_limited_to_chosen_links(client, admin_headers, base, lab):
    first = add(client, admin_headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0").get_json()
    add(client, admin_headers, base, lab, "sw1", "Gi0/1", "sw2", "Gi0/1")
    plan = client.post(f"{base}/config-plan", headers=admin_headers, json={"link_ids": [first["id"]]}).get_json()
    assert {d["hostname"] for d in plan["devices"]} == {"R1", "SW1"}
    unknown = client.post(f"{base}/config-plan", headers=admin_headers, json={"link_ids": [9999]})
    assert unknown.status_code == 422


def test_plan_without_links_is_a_validation_error(client, admin_headers, base, lab):
    assert client.post(f"{base}/config-plan", headers=admin_headers, json={}).status_code == 422


def test_preview_freezes_a_pending_batch_per_project(client, admin_headers, base, lab, project):
    build_design(client, admin_headers, base, lab)
    response = client.post(f"{base}/config-preview", headers=admin_headers, json={})
    assert response.status_code == 201
    batch = response.get_json()["batch"]
    assert batch["status"] == "pending_approval"
    assert batch["source"] == "design"
    assert batch["project_id"] == project.id
    assert {c["device"]["hostname"] for c in batch["changes"]} == {"R1", "SW1", "SW2"}
    # `no shutdown` is a negation, so the existing safeguards demand confirmation.
    assert batch["requires_confirmation"] is True
    assert db.session.query(ChangeBatch).count() == 1


def test_preview_never_touches_a_device(client, admin_headers, base, lab, ssh_factory):
    build_design(client, admin_headers, base, lab)
    client.post(f"{base}/config-preview", headers=admin_headers, json={})
    assert ssh_factory.clients == {}


def test_an_editor_can_propose_but_only_an_admin_can_approve(client, admin_headers, base, lab, project):
    from conftest import _auth_headers, _create_user
    from network_copilot.projects.model import ProjectMember

    user = _create_user("opop2", "OperatorPass123!", "OPERATOR")
    db.session.add(ProjectMember(project_id=project.id, user_id=user.id, access="editor"))
    db.session.commit()
    headers = _auth_headers(client, "opop2", "OperatorPass123!")
    add(client, headers, base, lab, "r1", "Gi0/1", "sw1", "Gi0/0")

    response = client.post(f"{base}/config-preview", headers=headers, json={"note": "please review"})
    assert response.status_code == 201
    batch = response.get_json()["batch"]
    assert batch["status"] == "pending_approval" and "please review" in batch["description"]
    assert batch["requested_by_id"] == user.id
    # The proposal changes nothing until an admin approves it.
    assert client.post(f"/api/change-batches/{batch['id']}/approve", headers=headers).status_code == 403
    assert client.post(f"/api/change-batches/{batch['id']}/approve", headers=admin_headers).status_code == 200


def test_a_viewer_cannot_propose(client, viewer_headers, base, lab):
    assert client.post(f"{base}/config-preview", headers=viewer_headers, json={}).status_code == 403


def test_preview_with_only_unsupported_devices_is_refused(client, admin_headers, base, lab):
    from conftest import _create_device

    asa2 = _create_device("FW2", "172.16.3.11", "firewall", device_type="cisco_asa")
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["fw"], "Gi0/0", asa2, "Gi0/0"))
    assert client.post(f"{base}/config-preview", headers=admin_headers, json={}).status_code == 422


# -- palette: templated devices, automatic names, addresses and ports -----------


def drop(client, headers, base, template="router", **extra):
    return client.post(f"{base}/quick-device", headers=headers, json={"template": template, **extra})


def test_templates_are_listed(client, admin_headers, base):
    items = client.get(f"{base}/templates", headers=admin_headers).get_json()["items"]
    assert {t["key"] for t in items} == {
        "router", "switch_l3", "switch", "firewall", "pc", "server", "cloud", "ap", "segment",
    }


def test_dropping_devices_picks_free_names_and_addresses(client, admin_headers, base, project):
    first = drop(client, admin_headers, base, "router", x=120, y=80).get_json()
    second = drop(client, admin_headers, base, "router").get_json()
    switch = drop(client, admin_headers, base, "switch").get_json()

    assert (first["hostname"], second["hostname"], switch["hostname"]) == ("R1", "R2", "SW1")
    assert (first["role"], switch["role"]) == ("core", "access")
    assert (first["pos_x"], first["pos_y"]) == (120.0, 80.0)
    assert second["pos_x"] is None
    # Inside the project network, from the 10th host up, never twice the same.
    ips = [first["management_ip"], second["management_ip"], switch["management_ip"]]
    assert ips == ["172.16.3.10", "172.16.3.11", "172.16.3.12"]


def test_names_and_addresses_reuse_gaps(client, admin_headers, base):
    a = drop(client, admin_headers, base).get_json()
    b = drop(client, admin_headers, base).get_json()
    client.delete(f"/api/devices/{a['id']}", headers=admin_headers)
    again = drop(client, admin_headers, base).get_json()
    assert (again["hostname"], again["management_ip"]) == (a["hostname"], a["management_ip"])
    assert b["hostname"] == "R2"


def test_firewall_template_is_an_asa(client, admin_headers, base):
    body = drop(client, admin_headers, base, "firewall").get_json()
    assert (body["device_type"], body["role"], body["hostname"]) == ("cisco_asa", "firewall", "FW1")


def test_quick_device_stores_the_ssh_credential(client, admin_headers, base):
    from network_copilot.credentials.service import get_device_credential

    body = drop(client, admin_headers, base, credential={"username": "netops", "password": "S3cret!pw"}).get_json()
    assert body["has_credential"] is True
    assert get_device_credential(body["id"]).username == "netops"


def test_quick_device_respects_the_environment_and_unknown_templates(client, admin_headers, base):
    assert drop(client, admin_headers, base, environment="physical").get_json()["environment"] == "physical"
    assert drop(client, admin_headers, base, "toaster").status_code == 404
    assert drop(client, admin_headers, base, bogus=1).status_code == 422


def test_quick_device_reports_a_full_network(client, admin_headers, base, project):
    project.management_network = "192.168.9.0/30"  # two usable hosts
    db.session.commit()
    assert drop(client, admin_headers, base).status_code == 201
    assert drop(client, admin_headers, base).status_code == 201
    assert drop(client, admin_headers, base).status_code == 409


def test_viewers_cannot_drop_devices(client, viewer_headers, base):
    assert drop(client, viewer_headers, base).status_code == 403


def test_links_choose_free_ports_when_none_are_given(client, admin_headers, base, lab):
    def connect(a, b):
        response = client.post(
            f"{base}/links", headers=admin_headers,
            json={"device_a_id": lab[a].id, "device_b_id": lab[b].id},
        )
        assert response.status_code == 201
        return response.get_json()

    one = connect("r1", "sw1")
    two = connect("r1", "sw2")
    three = connect("sw1", "sw2")
    assert (one["interface_a"], one["interface_b"]) == ("GigabitEthernet0/0", "GigabitEthernet0/0")
    assert two["interface_a"] == "GigabitEthernet0/1"          # r1 port 0 is taken
    assert (three["interface_a"], three["interface_b"]) == ("GigabitEthernet0/1", "GigabitEthernet0/1")


def test_an_explicit_port_can_be_mixed_with_an_automatic_one(client, admin_headers, base, lab):
    response = client.post(
        f"{base}/links", headers=admin_headers,
        json={"device_a_id": lab["r1"].id, "interface_a": "GigabitEthernet0/5", "device_b_id": lab["sw1"].id},
    )
    assert response.status_code == 201
    assert response.get_json()["interface_b"] == "GigabitEthernet0/0"


# -- ports and duplication ----------------------------------------------------------


def test_port_inventory_marks_used_ports(client, admin_headers, base, lab):
    client.post(f"{base}/links", headers=admin_headers, json=link_payload(lab["r1"], "Gi0/0", lab["sw1"], "Gi0/3"))
    router = client.get(f"{base}/devices/{lab['r1'].id}/ports", headers=admin_headers).get_json()["ports"]
    assert len(router) == 8 and router[0] == {"name": "GigabitEthernet0/0", "used": True, "used_by": "link to SW1"}
    assert router[1]["used"] is False
    switch = client.get(f"{base}/devices/{lab['sw1'].id}/ports", headers=admin_headers).get_json()["ports"]
    assert len(switch) == 24 and switch[3]["used"] and not switch[0]["used"]
    assert client.get(f"{base}/devices/9999/ports", headers=admin_headers).status_code == 404


def test_duplicate_copies_devices_and_inner_cables_only(client, admin_headers, base, lab):
    client.put(f"{base}/layout", headers=admin_headers,
               json={"positions": [{"device_id": lab["sw1"].id, "x": 100, "y": 100}, {"device_id": lab["sw2"].id, "x": 300, "y": 100}]})
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["sw1"], "Gi0/1", lab["sw2"], "Gi0/1", link_type="trunk", allowed_vlans="10,20"))
    client.post(f"{base}/links", headers=admin_headers, json=link_payload(lab["r1"], "Gi0/0", lab["sw1"], "Gi0/0"))

    body = client.post(f"{base}/duplicate", headers=admin_headers,
                       json={"device_ids": [lab["sw1"].id, lab["sw2"].id], "dx": 0, "dy": 200}).get_json()
    assert [d["hostname"] for d in body["devices"]] == ["SW3", "SW4"]
    assert [(d["pos_x"], d["pos_y"]) for d in body["devices"]] == [(100.0, 300.0), (300.0, 300.0)]
    assert all(not d["has_credential"] for d in body["devices"])
    assert len({d["management_ip"] for d in body["devices"]} | {lab["sw1"].management_ip, lab["sw2"].management_ip}) == 4
    assert len(body["links"]) == 1 and body["links"][0]["link_type"] == "trunk"   # the R1 cable stays out
    assert body["links"][0]["allowed_vlans"] == "10,20"


def test_duplicating_a_routed_link_makes_a_plain_cable_and_says_so(client, admin_headers, base, lab):
    client.post(f"{base}/links", headers=admin_headers,
                json=link_payload(lab["r1"], "Gi0/1", lab["sw1"], "Gi0/0", link_type="routed", network="10.0.12.0/30"))
    body = client.post(f"{base}/duplicate", headers=admin_headers, json={"device_ids": [lab["r1"].id, lab["sw1"].id]}).get_json()
    assert body["links"][0]["link_type"] == "physical" and body["notes"]


def test_duplicate_validates_and_is_atomic(client, admin_headers, base, lab, project):
    before = db.session.query(type(lab["r1"])).count()
    for body in ({}, {"device_ids": []}, {"device_ids": ["x"]}, {"device_ids": [999999]}, {"device_ids": [lab["r1"].id], "dx": "far"}):
        assert client.post(f"{base}/duplicate", headers=admin_headers, json=body).status_code == 422
    assert db.session.query(type(lab["r1"])).count() == before


def test_viewers_cannot_duplicate(client, viewer_headers, base, lab):
    assert client.post(f"{base}/duplicate", headers=viewer_headers, json={"device_ids": [lab["r1"].id]}).status_code == 403
