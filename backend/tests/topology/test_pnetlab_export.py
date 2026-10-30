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
            name = json.get("name") or json["path"].rsplit("/", 1)[-1].removesuffix(".unl")
            self.labinfo = {"name": name, "path": json["path"]}
            if f"{name}.unl" in self.existing:
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
    return {**LOGIN, "folder": "/NetAI", "lab_name": "csn301-lab", **extra}


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
    assert plan["lab_path"] == "/NetAI/csn301lab.unl"
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
    assert job["summary"]["cables"] == 3 and job["summary"]["lab_path"] == "/NetAI/csn301lab.unl"
    cloud = next(i for i, n in fake_pnetlab.networks.items() if n["type"] == "pnet0")
    r1 = fake_pnetlab.wired("R1")
    assert r1["Gi0/0"] == int(cloud) and "Gi0/1" in r1 and "Gi0/2" in r1
    assert fake_pnetlab.wired("SW2")["Gi0/1"] == int(cloud)  # Gi0/0 is cabled to SW1
    assert fake_pnetlab.wired("SW1")["Gi1/0"]                 # index 4 on IOSv-L2
    assert fake_pnetlab.wired("FW1")["Mgmt0/0"] == int(cloud)
    assert not fake_pnetlab.deleted
    assert not any(p.endswith("/nodes/start") for _, p, _ in fake_pnetlab.calls)


def test_a_taken_lab_name_is_refused_and_nothing_is_deleted(client, admin_headers, base, cabled, fake_pnetlab):
    fake_pnetlab.existing.add("csn301lab.unl")
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
    assert fake_pnetlab.deleted == ["/NetAI/csn301lab.unl"]


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
                       json=body(lab_name="---")).status_code == 422
    assert client.post(f"{base}/export/pnetlab", headers=viewer_headers, json=body()).status_code in (403, 404)


def test_a_login_that_is_not_pnetlab_json_is_explained_not_a_500(client, admin_headers, base, cabled, app):
    """http://172.16.3.25 answered the login with a redirect (to its https
    port) instead of JSON; session_login raised its internal _NoSessionAPI
    and the preview answered 500."""

    def factory(request):
        client_ = PNETLabClient(request.url, request.username, request.password, mode="session")
        client_._send = lambda method, path, **kw: (302, None, {"Location": "https://172.16.3.25:8443/"})
        return PNETLabWriter(client_)

    app.config["PNETLAB_WRITER_FACTORY"] = factory
    response = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers, json=body())
    assert response.status_code == 422, response.get_json()
    assert "https" in response.get_json()["message"]


class OpenOnlyPNETLab(FakePNETLab):
    """factory/create only opens a .unl that exists; POST /api/labs writes it.

    The CSN301 server refused both factory/create bodies that create a lab."""

    def __init__(self, accept_create=True):
        super().__init__()
        self.files = set()
        self.accept_create = accept_create

    def __call__(self, method, path, json=None, **kw):
        if path == "/api/labs/session/factory/create":
            self.calls.append((method, path, json))
            if json["path"] not in self.files:
                return 400, {"code": 400, "status": "fail", "message": "Lab does not exist (60038)."}, {}
            self.labinfo = {"name": json["path"].rsplit("/", 1)[-1][:-4], "path": json["path"]}
            return 200, {"code": 200, "status": "success"}, {}
        if method == "POST" and path == "/api/labs":
            self.calls.append((method, path, json))
            if not self.accept_create:
                return 403, {"code": 403, "status": "fail", "message": "Permission denied"}, {}
            self.files.add(f"{json['path'].rstrip('/')}/{json['name']}.unl")
            return 200, {"code": 200, "status": "success"}, {}
        return super().__call__(method, path, json=json, **kw)


def _use(app, fake):
    def factory(request):
        client_ = PNETLabClient(request.url, request.username, request.password, mode="session")
        client_._send = fake
        client_.session_login = lambda: None
        return PNETLabWriter(client_)
    app.config["PNETLAB_WRITER_FACTORY"] = factory
    app.config["BOOTSTRAP_RUN_INLINE"] = True


def test_a_lab_is_created_the_eve_ng_way_when_factory_create_only_opens(client, admin_headers, base, cabled, app):
    fake = OpenOnlyPNETLab()
    _use(app, fake)
    job = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body()).get_json()["job"]
    job = client.get(f"{base}/export/jobs/{job['id']}", headers=admin_headers).get_json()["job"]
    assert job["status"] == "done", job
    assert fake.files == {"/NetAI/csn301lab.unl"}
    assert len(fake.nodes) == 4


def test_a_lab_that_cannot_be_created_says_what_pnetlab_answered(client, admin_headers, base, cabled, app):
    fake = OpenOnlyPNETLab(accept_create=False)
    _use(app, fake)
    job = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body()).get_json()["job"]
    job = client.get(f"{base}/export/jobs/{job['id']}", headers=admin_headers).get_json()["job"]
    assert job["status"] == "error"
    assert "Lab does not exist (60038)" in job["message"] and "Permission denied" in job["message"]
    assert not fake.nodes



def test_the_lab_is_created_like_the_pnetlab_ui_in_the_configured_folder(client, admin_headers, base, cabled, app):
    """Captured on the CSN301 server: ai.tool may not write to "/", PNETLab's
    "Add new lab" posts /api/labs with the folder (trailing slash) and the
    account's user id, then opens the lab; the name keeps letters and digits."""
    fake = OpenOnlyPNETLab()
    original = fake.__call__

    def with_user(method, path, json=None, **kw):
        if path == "/api/auth":
            fake.calls.append((method, path, json))
            return 200, {"code": 200, "status": "success", "data": {"id": 38, "lab": None}}, {}
        return original(method, path, json=json, **kw)

    _use(app, with_user)
    app.config["PNETLAB_EXPORT_FOLDER"] = "/Courses/CSN301-StudentLab/Group-1"
    request_body = {**LOGIN, "lab_name": "Test-2026 10 30"}
    preview = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers, json=request_body).get_json()
    assert preview["lab_path"] == "/Courses/CSN301-StudentLab/Group-1/Test20261030.unl"
    job = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=request_body).get_json()["job"]
    job = client.get(f"{base}/export/jobs/{job['id']}", headers=admin_headers).get_json()["job"]
    assert job["status"] == "done", job
    created = next(j for m, p, j in fake.calls if m == "POST" and p == "/api/labs")
    assert created["path"] == "/Courses/CSN301-StudentLab/Group-1/"
    assert created["name"] == "Test20261030"
    assert created["author"] == "38" and created["editable_emails"] == ["38"]
    assert not any(p == "/api/labs/session/factory/create" and j.get("name") for m, p, j in fake.calls)


class StrictAccountPNETLab(FakePNETLab):
    """ai.tool on the CSN301 server: no access to some folders, and it may
    create labs but not delete them."""

    def __init__(self, forbidden=(), fail_on=None):
        super().__init__(fail_on=fail_on)
        self.forbidden = set(forbidden)

    def __call__(self, method, path, json=None, **kw):
        if method == "GET" and path.startswith("/api/folders"):
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(path)
            folder = parse_qs(parsed.query).get("path", [parsed.path[len("/api/folders"):] or "/"])[0]
            if folder in self.forbidden:
                self.calls.append((method, path, json))
                return 400, {"code": 400, "status": "fail",
                             "message": f"You do not have permission to access folder {folder}"}, {}
        if method == "DELETE" and path == "/api/labs":
            self.calls.append((method, path, json))
            return 400, {"code": 400, "status": "fail", "message": "You do not have permission to delete this lab"}, {}
        return super().__call__(method, path, json=json, **kw)


def test_a_folder_the_account_cannot_use_blocks_the_preview_and_the_run(client, admin_headers, base, cabled, app):
    fake = StrictAccountPNETLab(forbidden={"/NetAI"})
    _use(app, fake)
    preview = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers, json=body()).get_json()
    assert preview["blocked"] is True
    assert "permission to access folder /NetAI" in preview["warnings"][0]
    job = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body()).get_json()["job"]
    job = client.get(f"{base}/export/jobs/{job['id']}", headers=admin_headers).get_json()["job"]
    assert job["status"] == "error" and "/NetAI" in job["message"]
    assert not any(p in ("/api/labs", "/api/labs/session/factory/create") for _, p, _ in fake.calls)


def test_a_rollback_the_account_may_not_delete_names_the_lab_left_behind(client, admin_headers, base, cabled, app):
    fake = StrictAccountPNETLab(fail_on="/networks/p2p")
    _use(app, fake)
    job = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body()).get_json()["job"]
    job = client.get(f"{base}/export/jobs/{job['id']}", headers=admin_headers).get_json()["job"]
    assert job["status"] == "error"
    assert "/NetAI/csn301lab.unl KHÔNG xoá được" in job["message"]
    assert "permission to delete" in job["message"]


def test_a_template_without_an_image_is_left_out(client, admin_headers, base, cabled, app):
    fake = FakePNETLab()
    original = fake.__call__

    def no_asav_image(method, path, json=None, **kw):
        if path == "/api/list/templates/asav":
            return 200, {"code": 200, "status": "success",
                         "data": {"options": {"image": {"value": "", "list": {}}}}}, {}
        return original(method, path, json=json, **kw)

    _use(app, no_asav_image)
    plan = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers, json=body()).get_json()
    assert next(n for n in plan["nodes"] if n["hostname"] == "FW1")["ready"] is False
    assert next(n for n in plan["nodes"] if n["hostname"] == "R1")["ready"] is True



class PNETLab53(FakePNETLab):
    """PNETLab 5.3.11 on the CSN301 server: folders are listed with
    ?path=..., the EVE-NG path form answers 404 "Resource not found (60038)"."""

    def __call__(self, method, path, json=None, **kw):
        if method == "GET" and path.startswith("/api/folders"):
            self.calls.append((method, path, json))
            if not path.startswith("/api/folders?path="):
                return 404, {"code": 404, "status": "fail", "message": "Resource not found (60038)."}, {}
            return 200, {"code": 200, "status": "success", "message": "Successfully listed path (60007).",
                         "data": {"folders": [{"name": "..", "path": "/Courses/CSN301-StudentLab"}],
                                  "labs": [{"file": "Group1Lab1.unl", "owner": True,
                                            "path": "/Courses/CSN301-StudentLab/Group-1/Group1Lab1.unl"}]}}, {}
        return super().__call__(method, path, json=json, **kw)


def test_pnetlab_5_3_lists_the_folder_with_a_query_string(client, admin_headers, base, cabled, app):
    fake = PNETLab53()
    _use(app, fake)
    folder = {"folder": "/Courses/CSN301-StudentLab/Group-1"}
    plan = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers,
                       json=body(**folder, lab_name="Test202610301153")).get_json()
    assert not plan.get("blocked"), plan["warnings"]
    assert ("GET", "/api/folders?path=%2FCourses%2FCSN301-StudentLab%2FGroup-1", None) in fake.calls
    # An existing lab in that folder is still recognised by its file name.
    taken = client.post(f"{base}/export/pnetlab/preview", headers=admin_headers,
                        json=body(**folder, lab_name="Group1Lab1")).get_json()
    assert taken["blocked"] is True and "already exists" in taken["warnings"][0]


# -- start the nodes and bootstrap SSH ------------------------------------------------

def test_start_and_bootstrap_waits_for_each_node_to_boot(client, admin_headers, base, cabled, fake_pnetlab,
                                                          app, monkeypatch):
    from network_copilot.credentials.service import get_device_credential
    from network_copilot.topology import bootstrap

    app.config.update(PNETLAB_BOOT_TIMEOUT=60, PNETLAB_BOOT_RETRY_SECONDS=0)
    rounds = []

    def fake_run(project, payload, progress=None):
        rounds.append(payload)
        devices = []
        for target in payload["devices"]:
            device = db.session.get(type(cabled["r1"]), target["device_id"])
            if device.hostname == "R1" and len(rounds) == 1:
                devices.append({"device_id": device.id, "status": "error",
                                "message": "The console did not answer; is the node fully booted?"})
            else:
                devices.append({"device_id": device.id, "status": "ok", "message": "SSH answers."})
        return {"devices": devices}

    monkeypatch.setattr(bootstrap, "run", fake_run)
    response = client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body(start_and_bootstrap=True))
    assert response.status_code == 202, response.get_json()
    job = response.get_json()["job"]
    assert job["status"] == "done", job

    started = [json["id"] for _, path, json in fake_pnetlab.calls if path.endswith("/nodes/start")]
    assert sorted(started) == [1, 2, 3, 4]  # the ASA starts too
    # The ASA is not bootstrapped; R1 was retried once it had booted.
    assert len(rounds) == 2 and len(rounds[0]["devices"]) == 3 and len(rounds[1]["devices"]) == 1
    assert rounds[0]["lab"] == "/NetAI/csn301lab.unl" and rounds[0]["api_mode"] == "session"
    ssh = {d["hostname"]: d.get("ssh") for d in job["devices"]}
    assert ssh == {"R1": "ok", "SW1": "ok", "SW2": "ok", "FW1": None}
    assert job["summary"]["ssh_ok"] == 3 and job["summary"]["started"] == 4
    assert job["summary"]["ssh_user"] == "g1"
    credential = get_device_credential(cabled["r1"].id)
    assert (credential.username, credential.password) == ("g1", "group1")
    db.session.refresh(cabled["r1"])
    assert cabled["r1"].environment == "pnetlab"


def test_a_typed_ssh_login_wins_and_a_node_that_never_boots_is_reported(client, admin_headers, base, cabled,
                                                                       fake_pnetlab, monkeypatch):
    from network_copilot.credentials.service import get_device_credential
    from network_copilot.topology import bootstrap

    def fake_run(project, payload, progress=None):
        return {"devices": [{"device_id": t["device_id"], "status": "error",
                             "message": "The console did not answer; is the node fully booted?"}
                            for t in payload["devices"]]}

    monkeypatch.setattr(bootstrap, "run", fake_run)
    job = client.post(f"{base}/export/pnetlab", headers=admin_headers,
                      json=body(start_and_bootstrap=True, ssh_username="netai", ssh_password="Secret-1")).get_json()["job"]
    assert job["status"] == "done", job  # the lab stays; the rows say what is left to do
    r1 = next(d for d in job["devices"] if d["hostname"] == "R1")
    assert r1["ssh"] == "error" and "Khởi tạo later" in r1["ssh_message"]
    assert not fake_pnetlab.deleted
    assert get_device_credential(cabled["r1"].id).username == "netai"


def test_start_and_bootstrap_takes_the_project_owner(client, admin_headers, base, cabled, fake_pnetlab,
                                                    monkeypatch):
    from network_copilot.projects import service as project_service
    from network_copilot.topology import bootstrap

    needs = []
    original = project_service.require_project

    def spy(user, project_id, need):
        needs.append(need)
        return original(user, project_id, need)

    monkeypatch.setattr(project_service, "require_project", spy)
    monkeypatch.setattr(bootstrap, "run", lambda project, payload, progress=None: {"devices": []})
    client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body())
    client.post(f"{base}/export/pnetlab", headers=admin_headers, json=body(lab_name="other", start_and_bootstrap=True))
    assert needs == ["editor", "owner"]
