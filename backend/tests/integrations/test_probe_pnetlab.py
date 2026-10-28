"""The Phase 0 probe against a fake PNETLab: it walks every step on a scratch
lab, deletes it afterwards and never writes the password into its report."""

import importlib.util
import json
from pathlib import Path

from network_copilot.integrations.pnetlab import PNETLabClient

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_pnetlab_write.py"
PASSWORD = "Probe-Secret-123"


def load_probe():
    spec = importlib.util.spec_from_file_location("probe_pnetlab_write", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePNETLab:
    """Just enough of the session API for the probe to walk through."""

    def __init__(self):
        self.nodes: dict = {}
        self.calls: list[tuple[str, str]] = []
        self.lab_open = False

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path))
        ok = {"code": 200, "status": "success"}
        if path == "/api/list/templates/":
            return 200, {**ok, "data": {"vios": "Cisco vIOS", "asav": "ASAv.missing"}}, {}
        if path == "/api/list/templates/vios":
            return 200, {**ok, "data": {"options": {
                "image": {"value": "vios-15.6", "list": {"vios-15.6": "vios-15.6"}},
                "ethernet": {"value": 4}, "ram": {"value": 512}, "type": {"value": "qemu"}}}}, {}
        if path == "/api/labs/session/factory/create":
            self.lab_open = True
            return 200, ok, {}
        if path == "/api/labs/session/topology":
            if not self.lab_open:
                return 412, {"code": 412, "status": "fail"}, {}
            return 200, {**ok, "data": {"labinfo": {}, "nodes": dict(self.nodes), "networks": {}}}, {}
        if path == "/api/labs/session/nodes/add":
            node_id = str(len(self.nodes) + 1)
            self.nodes[node_id] = {"name": kwargs["json"]["name"], "ethernets": {"0": {"name": "Gi0/0"}}}
            return 201, ok, {}
        if path == "/api/labs/session/networks/add":
            return 200, {**ok, "data": {"id": 7}}, {}
        if path.startswith("/api/labs/session/configs"):
            return 200, {**ok, "data": "hostname PROBE-R1\n"}, {}
        if path == "/api/auth":
            return 200, {**ok, "data": {"lab": "abc"}}, {}
        return 200, ok, {}


def test_the_probe_walks_a_scratch_lab_and_deletes_it(app):
    probe_module = load_probe()
    client = PNETLabClient("http://172.16.3.25", "netai", PASSWORD, mode="session")
    fake = FakePNETLab()
    client._send = fake
    client.session_login = lambda: None

    report = probe_module.Probe(client, "/NetAI-probe").run(keep=False)

    findings = report["findings"]
    assert findings["router_template"] == "vios"
    assert findings["templates_available"] == ["vios"]  # ASAv is .missing
    assert findings["create_lab_body"] == "folder+name"
    assert findings["nodes_added"] == ["1", "2"]
    assert findings["p2p_link"] is True
    assert findings["startup_config"] == {"write_ok": True, "read_back": True}
    assert findings["deleted"] is True
    assert ("DELETE", "/api/labs") in fake.calls
    assert ("POST", "/api/labs/session/factory/destroy") in fake.calls
    # Nothing is ever started.
    assert not any(path.endswith("/nodes/start") for _, path in fake.calls)
    assert PASSWORD not in json.dumps(report)
