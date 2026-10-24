"""The real PNETLab client against a fake server, at the HTTP layer.

The fake answers like PNETLab 5 (session API, EVE-NG paths -> 60038) or like
EVE-NG (path API only), so both modes and the automatic fallback are covered
without a network.
"""

import io
import json

import pytest
import requests
from requests.structures import CaseInsensitiveDict
from urllib3.response import HTTPResponse

from network_copilot.credentials.service import store_device_credential
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.integrations import pnetlab

LAB = "/Courses/CSN301-StudentLab/Group-1/EIUSC_Network.unl"
HOST = "http://172.16.3.25"

NODES = {
    "1": {"id": 1, "name": "R1", "template": "iol", "type": "iol", "status": 2,
          "url": "telnet://172.16.3.25:30054", "port": 30054, "left": "30%", "top": "40%",
          "ethernets": {"0": {"name": "e0/0", "network_id": 2}, "48": {"name": "e0/3", "network_id": 1},
                        "16": {"name": "e0/1", "network_id": 0}}},
    # An account on the HTML5 console: url is a web link, the telnet port is in "port".
    "2": {"id": 2, "name": "S1", "template": "iol-l2", "type": "iol", "status": 2,
          "url": "/html5/#/client/Mg", "port": 30055, "left": "60%", "top": "40%",
          "ethernets": {"0": {"name": "e0/0", "network_id": 2}, "48": {"name": "e0/3", "network_id": 1}}},
}
NETWORKS = {
    "1": {"id": 1, "name": "Cloud0", "type": "pnet0"},
    "2": {"id": 2, "name": "Net-R1-S1", "type": "bridge"},
}


class FakeServer:
    def __init__(self, kind="pnetlab", open_lab=None):
        self.kind = kind
        self.open_lab = open_lab
        self.calls: list[tuple[str, str]] = []
        self.login_headers = None
        self.logged_in = False

    def __call__(self, session, method, url, **kwargs):
        path = url.removeprefix(HOST)
        self.calls.append((method, path))
        body = kwargs.get("json") or {}
        if self.kind == "eveng":
            return self._eveng(method, path, body)
        if method == "GET" and path == "/":
            session.cookies.set("XSRF-TOKEN", "tok%3D")
            return 200, "<html>PNETLab</html>"
        if path == "/store/public/auth/login/login":
            self.login_headers = kwargs.get("headers") or {}
            if body.get("password") != "pnet-pass":
                return 200, {"result": False, "message": "Incorrect username or password"}
            self.logged_in = True
            session.cookies.set("_session", "s1")
            return 200, {"result": True, "message": "Login successfully"}
        if path.startswith("/api/labs/session"):
            if not self.logged_in:
                return 412, {"code": 412, "status": "unauthorized", "message": "90001"}
            if path == "/api/labs/session/factory/create":
                if body.get("path") != LAB:
                    return 404, {"code": 404, "status": "fail", "message": "Lab not found (60038)"}
                self.open_lab = LAB
                return 200, {"code": 200, "status": "success", "message": "Lab opened"}
            if path == "/api/labs/session/topology":
                if self.open_lab is None:
                    return 400, {"code": 400, "status": "fail", "message": "No lab session"}
                return 200, {"code": 200, "status": "success", "data": {
                    "labinfo": {"name": "EIUSC_Network", "path": self.open_lab.lstrip("/")},
                    "nodes": NODES, "networks": NETWORKS}}
        # PNETLab 5 dropped the EVE-NG path API.
        return 404, {"code": 404, "status": "fail", "message": "Resource not found (60038)"}

    def _eveng(self, method, path, body):
        if path == "/api/auth/login":
            return 200, {"code": 200, "status": "success"}
        prefix = "/api/labs" + LAB
        eveng_nodes = {k: {kk: vv for kk, vv in v.items() if kk != "ethernets"} for k, v in NODES.items()}
        routes = {
            prefix + "/nodes": eveng_nodes,
            prefix + "/topology": pnetlab.session_links(NODES),
            prefix + "/networks": NETWORKS,
        }
        if path in routes:
            return 200, {"code": 200, "status": "success", "data": routes[path]}
        return 404, "<html>Not Found</html>"


@pytest.fixture
def server(monkeypatch):
    holder = {"server": FakeServer()}

    def request(session, method, url, **kwargs):
        status, body = holder["server"](session, method, url, **kwargs)
        raw = (json.dumps(body) if not isinstance(body, str) else body).encode()
        response = requests.models.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
        response.raw = HTTPResponse(body=io.BytesIO(raw), preload_content=False, status=status)
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return holder


def pnet_client(mode="auto", password="pnet-pass"):
    return pnetlab.PNETLabClient(HOST, "student1", password, mode=mode)


# -- the client ------------------------------------------------------------------


def test_session_api_logs_in_opens_the_lab_and_reads_it(server):
    c = pnet_client()
    nodes, topology, networks = c.read_lab(LAB)
    fake = server["server"]
    assert c.mode_used == "session"
    assert set(nodes) == {"1", "2"} and networks["1"]["type"] == "pnet0"
    assert ("POST", "/api/labs/session/factory/create") in fake.calls
    assert fake.login_headers.get("X-XSRF-TOKEN") == "tok="
    # interfaces with a network become node -> network entries; unwired ones do not
    assert {(e["source"], e["source_label"], e["destination"]) for e in topology} == {
        ("node1", "e0/0", "network2"), ("node1", "e0/3", "network1"),
        ("node2", "e0/0", "network2"), ("node2", "e0/3", "network1")}
    # one login, one read, for all three accessors
    calls = len(fake.calls)
    c.nodes(LAB), c.topology(LAB), c.networks(LAB)
    assert len(fake.calls) == calls


def test_a_lab_already_open_in_the_session_is_reused(server):
    server["server"] = FakeServer(open_lab=LAB)
    c = pnet_client()
    c.read_lab(LAB.removesuffix(".unl").lstrip("/"))  # "Courses/.../EIUSC_Network" works too
    assert ("POST", "/api/labs/session/factory/create") not in server["server"].calls


def test_an_empty_path_means_the_lab_open_in_the_session(server):
    server["server"] = FakeServer(open_lab=LAB)
    assert set(pnet_client().nodes("")) == {"1", "2"}
    server["server"] = FakeServer()
    with pytest.raises(pnetlab.PNETLabError, match="No lab is open"):
        pnet_client().nodes("")


def test_errors_are_clear(server):
    with pytest.raises(pnetlab.PNETLabError, match="could not open /Courses/Other.unl"):
        pnet_client().nodes("/Courses/Other.unl")
    with pytest.raises(pnetlab.PNETLabError, match="login failed: Incorrect"):
        pnet_client(password="wrong").nodes(LAB)


def test_auto_falls_back_to_the_eveng_api(server):
    server["server"] = FakeServer(kind="eveng")
    c = pnet_client()
    nodes, topology, networks = c.read_lab(LAB)
    assert c.mode_used == "eveng" and set(nodes) == {"1", "2"} and len(topology) == 4
    with pytest.raises(pnetlab.PNETLabError, match="no PNETLab session API"):
        pnet_client(mode="session").nodes(LAB)


def test_eveng_mode_on_pnetlab5_reports_lab_not_found(server):
    with pytest.raises(pnetlab.PNETLabError, match="Lab not found"):
        pnet_client(mode="eveng").nodes(LAB)


# -- through the import and bootstrap endpoints ------------------------------------

SOURCE = {"url": HOST, "username": "student1", "password": "pnet-pass", "lab": LAB}


def test_import_preview_over_the_session_api_skips_the_cloud(client, admin_headers, project, server):
    base = f"/api/projects/{project.id}/topology/import/pnetlab"
    body = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE).get_json()
    assert body["api"] == "session"
    assert {n["hostname"] for n in body["nodes"]} == {"R1", "S1"}
    # Cloud0 holds both nodes but is not a cable; the bridge between them is.
    assert body["links"] == [{"node_a": "1", "interface_a": "Ethernet0/0",
                              "node_b": "2", "interface_b": "Ethernet0/0"}]


def test_bootstrap_preview_finds_cloud0_ports_and_console_ports(client, admin_headers, project, server):
    for hostname, ip, kind, role in (("R1", "172.16.3.111", "router", "core"),
                                     ("S1", "172.16.3.121", "switch", "access")):
        device = Device(project_id=project.id, hostname=hostname, management_ip=ip, device_type="cisco_ios",
                        role=role, kind=kind, environment="pnetlab")
        db.session.add(device)
        db.session.commit()
        store_device_credential(device.id, "netops", "Bootstr4p!pw")
    base = f"/api/projects/{project.id}/topology/bootstrap/pnetlab"
    body = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE).get_json()
    by_name = {d["hostname"]: d for d in body["devices"]}
    assert body["api"] == "session"
    assert by_name["R1"]["console"] == "172.16.3.25:30054" and by_name["R1"]["interface"] == "Ethernet0/3"
    assert by_name["S1"]["console"] == "172.16.3.25:30055" and by_name["S1"]["interface"] == "Ethernet0/3"
    assert all(d["ready"] for d in body["devices"])
