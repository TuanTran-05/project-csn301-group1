from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.extensions import db
from network_copilot.projects.model import Project
from network_copilot.topology.model import DesignSvi, DesignVlan, TopologyLink, TopologyVersion

EMPTY = {"vlans": [], "access_ports": [], "svis": [], "loopbacks": [], "subinterfaces": [], "routes": [],
         "ospf": []}

PROPOSAL = {
    "explanation": "Tạo 2 VLAN, gateway trên DSW1 và chạy OSPF.",
    "addressing_pool": None,
    "design": {
        **EMPTY,
        "vlans": [{"vlan_id": 10, "name": "KE_TOAN"}, {"vlan_id": 20, "name": "KY_THUAT"}],
        "access_ports": [{"hostname": "SW1", "interface": "GigabitEthernet0/5", "vlan_id": 10}],
        "svis": [
            {"hostname": "DSW1", "vlan_id": 10, "ip": "auto", "prefix_length": 24},
            {"hostname": "DSW1", "vlan_id": 20, "ip": "192.168.20.1", "prefix_length": 24},
        ],
        "ospf": [{"hostname": "DSW1", "process_id": 1, "router_id": None}],
    },
}


def _lab(make_device):
    dsw = make_device("DSW1", "172.16.3.21", "distribution")
    sw = make_device("SW1", "172.16.3.31", "access")
    db.session.add(TopologyLink(project_id=dsw.project_id, device_a_id=dsw.id, interface_a="GigabitEthernet0/1",
                                device_b_id=sw.id, interface_b="GigabitEthernet0/0", link_type="trunk"))
    db.session.commit()
    return dsw, sw


def _ask(client, app, headers, responses, message="Tạo VLAN kế toán và kỹ thuật"):
    provider = FakeAIProvider(responses=responses)
    app.config["AI_PROVIDER_INSTANCE"] = provider
    session = client.post("/api/chat/sessions", headers=headers).get_json()
    response = client.post("/api/ai/chat", headers=headers,
                           json={"message": message, "session_id": session["id"], "mode": "design"})
    return response, provider


def test_design_mode_returns_a_normalised_proposal_without_saving_it(client, app, admin_headers, make_device):
    _lab(make_device)
    response, provider = _ask(client, app, admin_headers, PROPOSAL)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["flow"] == "design_proposal"
    svis = {s["vlan_id"]: s for s in body["design"]["svis"]}
    assert svis[10]["ip"] != "auto" and svis[20]["ip"] == "192.168.20.1"
    assert body["changes"]["vlans"] == {"added": 2, "removed": 0}
    assert db.session.query(DesignVlan).count() == 0

    context = provider.prompts[0]["context"]
    assert {d["hostname"] for d in context["devices"]} == {"DSW1", "SW1"}
    sent = str(provider.prompts)
    assert "172.16.3.21" not in sent and "172.16.3.0/24" not in sent


def test_an_invalid_proposal_is_retried_with_the_reason(client, app, admin_headers, make_device):
    _lab(make_device)
    cabled = {**PROPOSAL, "design": {**PROPOSAL["design"], "access_ports": [
        {"hostname": "SW1", "interface": "GigabitEthernet0/0", "vlan_id": 10}]}}
    response, provider = _ask(client, app, admin_headers, [cabled, PROPOSAL])
    assert response.status_code == 200, response.get_json()
    assert provider.calls == 2
    assert "uplink to DSW1" in provider.prompts[1]["context"]["previous_attempt_error"]


def _small_lab(make_device):
    """Router EIU-R1 - switch EIU-S1, with switches PC1 and PC2 standing in for PCs."""
    r1 = make_device("EIU-R1", "172.16.3.45", "core")
    s1 = make_device("EIU-S1", "172.16.3.46", "access")
    pc1 = make_device("PC1", "172.16.3.47", "access")
    pc2 = make_device("PC2", "172.16.3.48", "access")
    for a, ia, b, ib in ((r1, "GigabitEthernet0/0", s1, "GigabitEthernet0/0"),
                         (s1, "GigabitEthernet0/1", pc1, "GigabitEthernet0/0"),
                         (s1, "GigabitEthernet0/2", pc2, "GigabitEthernet0/0")):
        db.session.add(TopologyLink(project_id=r1.project_id, device_a_id=a.id, interface_a=ia,
                                    device_b_id=b.id, interface_b=ib, link_type="physical"))
    db.session.commit()
    return r1, s1


def test_a_port_cabled_to_a_pc_can_be_put_in_a_vlan(client, app, admin_headers, make_device):
    _small_lab(make_device)
    proposal = {
        "explanation": "PC1 vào VLAN 10, PC2 vào VLAN 20.",
        "addressing_pool": None,
        "design": {
            **EMPTY,
            "vlans": [{"vlan_id": 10, "name": "VLAN_10"}, {"vlan_id": 20, "name": "VLAN_20"}],
            "access_ports": [
                {"hostname": "EIU-S1", "interface": "GigabitEthernet0/1", "vlan_id": 10},
                {"hostname": "EIU-S1", "interface": "GigabitEthernet0/2", "vlan_id": 20},
            ],
        },
    }
    response, provider = _ask(client, app, admin_headers, proposal,
                              message="Put PC1 in VLAN 10 and PC2 in VLAN 20")
    assert response.status_code == 200, response.get_json()
    assert provider.calls == 1
    ports = {(p["interface"], p["vlan_id"]) for p in response.get_json()["design"]["access_ports"]}
    assert ports == {("GigabitEthernet0/1", 10), ("GigabitEthernet0/2", 20)}

    s1 = next(d for d in provider.prompts[0]["context"]["devices"] if d["hostname"] == "EIU-S1")
    assert s1["end_host_ports"] == [
        {"interface": "GigabitEthernet0/1", "connected_to": "PC1"},
        {"interface": "GigabitEthernet0/2", "connected_to": "PC2"},
    ]


def test_the_uplink_to_the_router_is_never_an_access_port(client, app, admin_headers, make_device):
    _small_lab(make_device)
    bad = {
        "explanation": "x", "addressing_pool": None,
        "design": {**EMPTY, "vlans": [{"vlan_id": 10, "name": "VLAN_10"}],
                   "access_ports": [{"hostname": "EIU-S1", "interface": "GigabitEthernet0/0", "vlan_id": 10}]},
    }
    response, provider = _ask(client, app, admin_headers, [bad, bad])
    assert response.status_code == 422
    assert "uplink to EIU-R1" in provider.prompts[1]["context"]["previous_attempt_error"]


def test_two_invalid_proposals_answer_a_clear_error(client, app, admin_headers, make_device):
    _lab(make_device)
    unknown = {**PROPOSAL, "design": {**EMPTY, "ospf": [{"hostname": "R99", "process_id": 1}]}}
    response, _ = _ask(client, app, admin_headers, unknown)
    assert response.status_code == 422
    assert db.session.query(DesignVlan).count() == 0


def test_management_network_never_reaches_the_model_in_errors(client, app, admin_headers, make_device):
    _lab(make_device)
    clash = {**PROPOSAL, "design": {**PROPOSAL["design"], "svis": [
        {"hostname": "DSW1", "vlan_id": 10, "ip": "172.16.3.1", "prefix_length": 24}]}}
    response, provider = _ask(client, app, admin_headers, [clash, PROPOSAL])
    assert response.status_code == 200
    error = provider.prompts[1]["context"]["previous_attempt_error"]
    assert "management network" in error and "172.16.3" not in error


def test_apply_replaces_the_design_and_saves_a_version(client, app, admin_headers, make_device):
    _lab(make_device)
    db.session.add(DesignVlan(project_id=db.session.query(Project).first().id, vlan_id=99, name="OLD"))
    db.session.commit()
    proposal = {**PROPOSAL, "addressing_pool": "192.168.0.0/16"}
    body = _ask(client, app, admin_headers, proposal)[0].get_json()
    assert body["changes"]["vlans"] == {"added": 2, "removed": 1}

    applied = client.post(f"/api/chat/messages/{body['chat_message_id']}/apply-design", headers=admin_headers)
    assert applied.status_code == 200, applied.get_json()
    assert sorted(v.vlan_id for v in db.session.query(DesignVlan)) == [10, 20]
    assert {s.ip for s in db.session.query(DesignSvi)} == {s["ip"] for s in body["design"]["svis"]}
    assert db.session.query(Project).first().addressing_pool == "192.168.0.0/16"
    version = db.session.get(TopologyVersion, applied.get_json()["version_id"])
    assert version.auto and '"OLD"' in version.document

    again = client.post(f"/api/chat/messages/{body['chat_message_id']}/apply-design", headers=admin_headers)
    assert again.status_code == 409


def test_viewer_gets_a_proposal_but_cannot_apply_it(client, app, admin_headers, viewer_headers, make_device):
    _lab(make_device)
    body = _ask(client, app, admin_headers, PROPOSAL)[0].get_json()
    response = client.post(f"/api/chat/messages/{body['chat_message_id']}/apply-design", headers=viewer_headers)
    assert response.status_code == 403
    assert db.session.query(DesignVlan).count() == 0


def test_apply_refuses_a_message_that_is_not_a_design(client, app, admin_headers, make_device):
    _lab(make_device)
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses={
        "intent": "chat", "operations": [], "explanation": "Xin chào"})
    body = client.post("/api/ai/chat", headers=admin_headers, json={"message": "xin chào"}).get_json()
    response = client.post(f"/api/chat/messages/{body['chat_message_id']}/apply-design", headers=admin_headers)
    assert response.status_code == 404


def test_a_failed_design_answers_in_english_on_the_english_page(client, app, admin_headers, make_device):
    _lab(make_device)
    unknown = {**PROPOSAL, "design": {**EMPTY, "ospf": [{"hostname": "R99", "process_id": 1}]}}
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=unknown)
    session = client.post("/api/chat/sessions", headers=admin_headers).get_json()
    response = client.post("/api/ai/chat", headers=admin_headers, json={
        "message": "Run OSPF on R99", "session_id": session["id"], "mode": "design", "language": "en"})
    assert response.status_code == 422
    assert response.get_json()["message"].startswith("The AI could not produce a valid design")


def test_the_demo_flow_designs_ports_gateways_and_the_trunk(client, app, admin_headers, make_device):
    """Small lab: design PC1/PC2 into VLANs with gateways on EIU-R1, apply it, generate the config."""
    r1, s1 = _small_lab(make_device)
    proposal = {
        "explanation": "PC1 vào VLAN 10, PC2 vào VLAN 20, gateway trên EIU-R1.",
        "addressing_pool": None,
        "design": {
            **EMPTY,
            "vlans": [{"vlan_id": 10, "name": "VLAN_10"}, {"vlan_id": 20, "name": "VLAN_20"}],
            "access_ports": [
                {"hostname": "EIU-S1", "interface": "GigabitEthernet0/1", "vlan_id": 10},
                {"hostname": "EIU-S1", "interface": "GigabitEthernet0/2", "vlan_id": 20},
            ],
            "subinterfaces": [
                {"hostname": "EIU-R1", "interface": "GigabitEthernet0/0", "vlan_id": 10,
                 "ip": "172.20.5.1", "prefix_length": 26},
                {"hostname": "EIU-R1", "interface": "GigabitEthernet0/0", "vlan_id": 20,
                 "ip": "172.20.5.65", "prefix_length": 26},
            ],
        },
    }
    body = _ask(client, app, admin_headers, proposal, message="Put PC1 in VLAN 10 and PC2 in VLAN 20")[0]
    assert body.status_code == 200, body.get_json()
    applied = client.post(f"/api/chat/messages/{body.get_json()['chat_message_id']}/apply-design",
                          headers=admin_headers)
    assert applied.status_code == 200, applied.get_json()

    plan = client.post(f"/api/projects/{r1.project_id}/topology/config-plan", headers=admin_headers, json={})
    assert plan.status_code == 200, plan.get_json()
    commands = {d["hostname"]: d["commands"] for d in plan.get_json()["devices"]}
    s1_cmds = commands["EIU-S1"]
    assert "switchport access vlan 10" in s1_cmds and "switchport access vlan 20" in s1_cmds
    assert s1_cmds[s1_cmds.index("switchport mode trunk") - 1] == "interface GigabitEthernet0/0"
    r1_cmds = commands["EIU-R1"]
    assert "interface GigabitEthernet0/0.10" in r1_cmds and "ip address 172.20.5.65 255.255.255.192" in r1_cmds
    preview = client.post(f"/api/projects/{r1.project_id}/topology/config-preview", headers=admin_headers, json={})
    assert preview.status_code in (200, 201), preview.get_json()
    assert len(preview.get_json()["batch"]["changes"]) == len(commands)
    s1_change = next(c for c in preview.get_json()["batch"]["changes"]
                     if "switchport access vlan 10" in c["commands"])
    # the PNETLab switch gets dot1q before trunk mode at preview time
    assert "switchport trunk encapsulation dot1q" not in s1_change["commands"] or \
        s1_change["commands"].index("switchport trunk encapsulation dot1q") < s1_change["commands"].index("switchport mode trunk")
