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
    assert "already cabled" in provider.prompts[1]["context"]["previous_attempt_error"]


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
