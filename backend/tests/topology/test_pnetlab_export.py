"""Pushing a project's topology to PNETLab, against a fake PNETLab that speaks
the session API calls captured from the CSN301 server's web UI."""

import pytest

from network_copilot.extensions import db
from network_copilot.integrations.pnetlab import PNETLabClient
from network_copilot.integrations.pnetlab_writer import PNETLabWriter
from network_copilot.topology.model import TopologyLink
from network_copilot.topology.pnetlab_export import interface_index

LOGIN = {"url": "http://172.16.3.25", "username": "ai.tool", "password": "Lab-Pass-123"}

TEMPLATES = {"vios": "Cisco vIOS", "viosl2": "Cisco vIOS L2", "asav": "Cisco ASAv",
             "paloalto": "Palo Alto.missing", "vpcs": "VPCS"}
PORTS = {"vios": ["Gi0/0", "Gi0/1", "Gi0/2", "Gi0/3"],
         "viosl2": ["Gi0/0", "Gi0/1", "Gi0/2", "Gi0/3", "Gi1/0", "Gi1/1"],
         "asav": ["Mgmt0/0", "Gi0/0", "Gi0/1", "Gi0/2"], "vpcs": ["eth0"]}


class FakePNETLab:
    def __init__(self, existing=(), fail_on=None):
        self.existing = set(existing)
        self.fail_on = fail_on
        self.labinfo = None
        self.nodes, self.networks = {}, {}
        self.calls = []
        self.deleted = []

    def __call__(self, method, path, json=None, **_):
        self.calls.append((method, path, json))
        ok = {"code": 200, "status": "success"}
        if self.fail_on and self.fail_on in path:
            return 500, {"code": 500, "status": "fail", "message": "boom"}, {}
        if path.startswith("/api/folders") and method == "GET":
            return 200, {**ok, "data": {"folders": [], "labs": [{"file": f} for f in self.existing]}}, {}
        if path == "/api/labs/session/factory/create":
            self.labinfo = {"name": json["name"], "path": json["path"]}
            if f"{json['name']}.unl" in self.existing:
                self.nodes["1"] = {"name": "SOMEONE-ELSE", "ethernets": {}}
            return 200, ok, {}
        if path == "/api/list/templates/":
            return 200, {**ok, "data": TEMPLATES}, {}
        if path.startswith("/api/list/templates/"):
            return 200, {**ok, "data": {"options": {"image": {"value": "img"}, "ethernet": {"value": 4},
                                                     "type": {"value": "qemu"}}}}, {}
        if path == "/api/labs/session/topology":
            return 200, {**ok, "data": {"labinfo": self.labinfo or {}, "nodes": dict(self.nodes),
                                        "networks": dict(self.networks)}}, {}
        if path == "/api/labs/session/nodes/add":
            node_id = str(len(self.nodes) + 1)
            ports = PORTS[json["template"]]
            self.nodes[node_id] = {"name": json["name"], "template": json["template"],
                                   "ethernets": {str(i): {"name": p, "network_id": 0} for i, p in enumerate(ports)}}
            return 201, ok, {}
        if path == "/api/labs/session/networks/add":
            network_id = str(len(self.networks) + 1)
            self.networks[network_id] = {"name": json["name"], "type": json["type"]}
            return 201, {**ok, "data": {"id": int(network_id)}}, {}
        if path == "/api/labs/session/networks/p2p":
            network_id = str(len(self.networks) + 1)
            self.networks[network_id] = {"name": "p2p", "type": "bridge"}
            for node, index in ((json["src_id"], json["src_if"]), (json["dest_id"], json["dest_if"])):
                self.nodes[str(node)]["ethernets"][str(index)]["network_id"] = int(network_id)
            return 201, ok, {}
        if path == "/api/labs/session/interfaces/edit":
            for index, network in json["data"].items():
                self.nodes[json["node_id"]]["ethernets"][index]["network_id"] = int(network)
            return 200, ok, {}
        if path == "/api/auth":
            return 200, {**ok, "data": {"lab": "sess-1"}}, {}
        if method == "DELETE" and path == "/api/labs":
            self.deleted.append(json["path"])
            return 200, ok, {}
        return 200, ok, {}

    def wired(self, hostname):
        node = next(n for n in self.nodes.values() if n["name"] == hostname)
        return {e["name"]: e["network_id"] for e in node["ethernets"].values() if e["network_id"]}


@pytest.fixture
def fake_pnetlab(app):
    fake = FakePNETLab()

    def factory(request):
        client = PNETLabClient(request.url, request.username, request.password, mode="session")
        client._send = fake
        client.session_login = lambda: None
        return PNETLabWriter(client)

    app.config["PNETLAB_WRITER_FACTORY"] = factory
    app.config["BOOTSTRAP_RUN_INLINE"] = True
    return fake


@pytest.fixture
def cabled(lab, project):
    """R1 Gi0/1 - SW1 Gi0/1, SW1 Gi1/0 - SW2 Gi0/0, R1 Gi0/2 - FW1 Gi0/1."""
    for a, if_a, b, if_b in (("r1", "GigabitEthernet0/1", "sw1", "GigabitEthernet0/1"),
                             ("sw1", "GigabitEthernet1/0", "sw2", "GigabitEthernet0/0"),
                             ("r1", "GigabitEthernet0/2", "fw", "GigabitEthernet0/1")):
        db.session.add(TopologyLink(project_id=project.id, device_a_id=lab[a].id, interface_a=if_a,
                                    device_b_id=lab[b].id, interface_b=if_b, link_type="physical"))
    db.session.commit()
    return lab


def body(**extra):
    return {**LOGIN, "lab_name": "csn301-lab", **extra}


@pytest.mark.parametrize("template, name, index", [
    ("vios", "GigabitEthernet0/2", 2), ("viosl2", "Gi1/0", 4), ("viosl2", "GigabitEthernet0/3", 3),
    ("asav", "GigabitEthernet0/1", 2), ("asav", "Management0/0", 0), ("vpcs", "eth0", 0),
    ("paloalto", "ethernet1/3", 3),
])
def test_interface_names_map_to_pnetlab_indexes(template, name, index):
    assert interface_index(template, name) == index


def test_preview_plans_without_building(client, admin_headers, base, cabled, fake_pnetlab):
    response = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers, json=body())
    assert response.status_code == 200, response.get_json()
    plan = response.get_json()
    assert plan["lab_path"] == "/NetAI/csn301-lab.unl"
    assert {n["hostname"]: n["template"] for n in plan["nodes"]} == {
        "R1": "vios", "SW1": "viosl2", "SW2": "viosl2", "FW1": "asav"}
    assert len(plan["cables"]) == 3
    # Management goes on the first port the cabling leaves free.
    assert {m["hostname"]: m["index"] for m in plan["management"]} == {
        "R1": 0, "SW1": 0, "SW2": 1, "FW1": 0}
    assert not any(p.endswith(("/nodes/add", "/factory/create")) for _, p, _ in fake_pnetlab.calls)


def test_run_builds_nodes_cables_and_cloud0(client, admin_headers, base, cabled, fake_pnetlab):
    response = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body())
    assert response.status_code == 202, response.get_json()
    job = client.get(f"{base}/export/jobs/{response.get_json()['job']['id']}",
                     headers=admin_headers).get_json()["job"]
    assert job["status"] == "done", job
    assert {d["hostname"]: d["status"] for d in job["devices"]} == {
        "R1": "created", "SW1": "created", "SW2": "created", "FW1": "created"}
    assert job["summary"]["cables"] == 3 and job["summary"]["lab_path"] == "/NetAI/csn301-lab.unl"
    cloud = next(i for i, n in fake_pnetlab.networks.items() if n["type"] == "pnet0")
    r1 = fake_pnetlab.wired("R1")
    assert r1["Gi0/0"] == int(cloud) and "Gi0/1" in r1 and "Gi0/2" in r1
    assert fake_pnetlab.wired("SW2")["Gi0/1"] == int(cloud)  # Gi0/0 is cabled to SW1
    assert fake_pnetlab.wired("SW1")["Gi1/0"]                 # index 4 on IOSv-L2
    assert fake_pnetlab.wired("FW1")["Mgmt0/0"] == int(cloud)
    assert not fake_pnetlab.deleted
    assert not any(p.endswith("/nodes/start") for _, p, _ in fake_pnetlab.calls)


def test_a_taken_lab_name_is_refused_and_nothing_is_deleted(client, admin_headers, base, cabled, fake_pnetlab):
    fake_pnetlab.existing.add("csn301-lab.unl")
    preview = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers, json=body()).get_json()
    assert preview["blocked"] is True
    job = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body()).get_json()["job"]
    job = client.get(f"{base}/export/jobs/{job['id']}", headers=admin_headers).get_json()["job"]
    assert job["status"] == "error" and "already exists" in job["message"]
    assert fake_pnetlab.deleted == []
    assert not any(p.endswith("/nodes/add") for _, p, _ in fake_pnetlab.calls)


def test_a_failure_part_way_deletes_the_half_built_lab(client, admin_headers, base, cabled, fake_pnetlab):
    fake_pnetlab.fail_on = "/networks/p2p"
    job = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body()).get_json()["job"]
    job = client.get(f"{base}/export/jobs/{job['id']}", headers=admin_headers).get_json()["job"]
    assert job["status"] == "error" and "was deleted" in job["message"]
    assert fake_pnetlab.deleted == ["/NetAI/csn301-lab.unl"]


def test_a_missing_template_leaves_that_device_out(client, admin_headers, base, cabled, fake_pnetlab):
    del TEMPLATES["asav"]
    try:
        plan = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers, json=body()).get_json()
    finally:
        TEMPLATES["asav"] = "Cisco ASAv"
    assert next(n for n in plan["nodes"] if n["hostname"] == "FW1")["ready"] is False
    assert len(plan["cables"]) == 2
    assert any("FW1" in w for w in plan["warnings"])


def test_bad_lab_names_and_viewers_are_refused(client, admin_headers, viewer_headers, base, cabled, fake_pnetlab):
    assert client.post(f"{base}/export/pnetlab/preview", headers=admin_headers,
                       json=body(lab_name="../etc")).status_code == 422
    assert client.post(f"{base}/export/pnetlab", headers=viewer_headers, json=body()).status_code in (403, 404)
