"""Find out which PNETLab write calls this server accepts, on a scratch lab.

Phase 0 of pushing a NetAI topology to PNETLab. Logs in with a dedicated
PNETLab account, creates a throw-away lab under /NetAI-probe, adds two nodes
(never started), cables them, tries to attach a Cloud0 (pnet0) network,
writes a startup-config, reads the topology back and deletes the lab again.
Every call and its answer go to a JSON report; the password never does.

Run it on the AI Server, which can reach PNETLab:

    cd backend
    .venv/bin/python scripts/probe_pnetlab_write.py --url http://172.16.3.25 --username netai

The password is asked for on the terminal (or read from PNETLAB_PASSWORD).
--keep leaves the scratch lab in place to look at it in the PNETLab UI.
"""

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from network_copilot.integrations.pnetlab import PNETLabClient, PNETLabError

# Router templates to try, best first: IOSv, then IOL L3.
ROUTER_TEMPLATES = ("vios", "i86bi_linux_l3", "iol")
# Templates NetAI would map its devices to; their options are recorded.
INTERESTING_TEMPLATES = (
    "vios", "viosl2", "asav", "paloalto", "vpcs", "linux", "docker",
    "i86bi_linux_l2", "i86bi_linux_l3", "iol",
)
EXCERPT = 3000


class Probe:
    def __init__(self, client: PNETLabClient, folder: str):
        self.client = client
        self.folder = "/" + folder.strip("/")
        self.name = time.strftime("probe-%Y%m%d-%H%M%S")
        self.lab_path = f"{self.folder}/{self.name}.unl"
        self.steps: list[dict] = []
        self.findings: dict = {}

    # -- recording -------------------------------------------------------------

    def call(self, step: str, method: str, path: str, **kwargs):
        """Send one request, record it, return (ok, data). Never raises."""
        try:
            status, data, _ = self.client._send(method, path, **kwargs)
        except PNETLabError as exc:
            self.steps.append({"step": step, "method": method, "path": path, "error": exc.message})
            return False, None
        ok = not self.client._failed(status, data)
        self.steps.append({
            "step": step, "method": method, "path": path, "status": status, "ok": ok,
            "request": kwargs.get("json"),
            "response": json.dumps(data, ensure_ascii=False)[:EXCERPT] if data is not None else None,
        })
        return ok, data

    @staticmethod
    def payload(data):
        return data.get("data") if isinstance(data, dict) else None

    # -- steps -----------------------------------------------------------------

    def login(self) -> bool:
        try:
            self.client.session_login()
        except PNETLabError as exc:
            self.steps.append({"step": "login", "error": exc.message})
            return False
        except Exception as exc:  # _NoSessionAPI: not a PNETLab 4/5 server
            self.steps.append({"step": "login", "error": type(exc).__name__})
            return False
        self.steps.append({"step": "login", "ok": True})
        return True

    def templates(self) -> dict:
        _, data = self.call("list templates", "GET", "/api/list/templates/")
        listing = self.payload(data) or {}
        available = {
            key: label for key, label in (listing.items() if isinstance(listing, dict) else [])
            if not str(label).endswith(".missing")
        }
        self.findings["templates_available"] = sorted(available)
        options = {}
        for template in INTERESTING_TEMPLATES:
            if template in available:
                _, data = self.call(f"template options {template}", "GET",
                                    f"/api/list/templates/{template}")
                options[template] = (self.payload(data) or {}).get("options", {})
        self.findings["template_options"] = {
            key: {name: opt.get("value") if isinstance(opt, dict) else opt
                  for name, opt in value.items()}
            if isinstance(value, dict) else value
            for key, value in options.items()
        }
        return options

    def create_lab(self) -> bool:
        self.call("create folder", "POST", "/api/folders/add",
                  json={"path": "/", "name": self.folder.strip("/")})
        meta = {"name": self.name, "version": "1", "author": "NetAI probe",
                "description": "Scratch lab of scripts/probe_pnetlab_write.py; safe to delete.",
                "body": ""}
        # The parent folder plus a name (pnetlab-mcp-server), then the full path.
        for label, path in (("folder+name", self.folder), ("full path", self.lab_path)):
            ok, _ = self.call(f"create lab ({label})", "POST",
                              "/api/labs/session/factory/create", json={"path": path, **meta})
            topology = self.topology(f"topology after create ({label})")
            if ok and topology is not None:
                self.findings["create_lab_body"] = label
                return True
        self.findings["create_lab_body"] = None
        return False

    def topology(self, step: str = "topology"):
        ok, data = self.call(step, "GET", "/api/labs/session/topology")
        payload = self.payload(data)
        return payload if ok and isinstance(payload, dict) else None

    def add_node(self, template: str, options: dict, name: str, left: int) -> str | None:
        def default(key, fallback):
            option = options.get(key)
            return option.get("value", fallback) if isinstance(option, dict) else fallback

        image_option = options.get("image") if isinstance(options.get("image"), dict) else {}
        images = image_option.get("list") or {}
        image = image_option.get("value") or (next(iter(images)) if images else "")
        body = {
            "type": default("type", "qemu"), "template": template, "name": name,
            "icon": default("icon", "Router.png"), "image": image,
            "ram": default("ram", 512), "cpu": default("cpu", 1),
            "ethernet": default("ethernet", 4), "serial": default("serial", 0),
            "delay": 0, "cpulimit": 1, "config": "1", "config_script": "embedded",
            "left": str(left), "top": "200", "count": "1",
        }
        if not body["image"]:
            body.pop("image")
        self.findings.setdefault("add_node_body", body)
        before = set((self.topology(f"topology before {name}") or {}).get("nodes") or {})
        ok, data = self.call(f"add node {name}", "POST", "/api/labs/session/nodes/add", json=body)
        nodes = (self.topology(f"topology after {name}") or {}).get("nodes") or {}
        new = [key for key in nodes if key not in before]
        return str(new[0]) if new else None

    def interfaces_of(self, node_id: str) -> dict:
        node = ((self.topology(f"interfaces of node {node_id}") or {}).get("nodes") or {}).get(node_id) or {}
        return {key: node.get(key) for key in ("ethernets", "interfaces") if key in node}

    def cable(self, a: str, b: str) -> None:
        ok, _ = self.call("p2p link", "POST", "/api/labs/session/networks/p2p",
                          json={"name": "", "src_id": int(a), "src_if": 1,
                                "dest_id": int(b), "dest_if": 1})
        self.findings["p2p_link"] = ok

    def cloud(self, node_id: str) -> None:
        """Experimental: no public source documents these on PNETLab 4/5."""
        ok, data = self.call("add pnet0 network", "POST", "/api/labs/session/networks/add",
                             json={"type": "pnet0", "name": "Net", "left": "400",
                                   "top": "50", "visibility": "1"})
        network = self.payload(data)
        network_id = network.get("id") if isinstance(network, dict) else None
        self.findings["cloud_network_add"] = {"ok": ok, "id": network_id}
        if network_id is None:
            return
        attempts = (
            ("POST", "/api/labs/session/interfaces/edit", {"id": int(node_id), "data": {"0": network_id}}),
            ("POST", "/api/labs/session/nodes/interfaces", {"id": int(node_id), "interface_id": 0,
                                                            "network_id": network_id}),
            ("PUT", f"/api/labs/session/nodes/{node_id}/interfaces", {"0": network_id}),
        )
        attached = []
        for method, path, body in attempts:
            ok, _ = self.call(f"attach to pnet0 via {path}", method, path, json=body)
            attached.append({"method": method, "path": path, "ok": ok})
        self.findings["cloud_attach_attempts"] = attached

    def startup_config(self, node_id: str) -> None:
        ok, _ = self.call("write startup-config", "POST", "/api/labs/session/configs/edit",
                          json={"id": int(node_id), "data": "hostname PROBE-R1\n"})
        _, data = self.call("read startup-config", "GET",
                            f"/api/labs/session/configs?id={int(node_id)}")
        self.findings["startup_config"] = {
            "write_ok": ok, "read_back": "PROBE-R1" in json.dumps(data or {}),
        }

    def cleanup(self) -> None:
        _, data = self.call("session info", "GET", "/api/auth")
        info = self.payload(data)
        session = info.get("lab") if isinstance(info, dict) else None
        if session:
            self.call("destroy session", "POST", "/api/labs/session/factory/destroy",
                      json={"lab_session": session})
        ok, _ = self.call("delete lab", "DELETE", "/api/labs", json={"path": self.lab_path})
        self.findings["deleted"] = ok

    # -- the whole run ---------------------------------------------------------

    def run(self, keep: bool) -> dict:
        self.findings["lab_path"] = self.lab_path
        if not self.login():
            return self.report()
        options = self.templates()
        router = next((t for t in ROUTER_TEMPLATES if t in options), None)
        self.findings["router_template"] = router
        if router is None or not self.create_lab():
            return self.report()
        try:
            first = self.add_node(router, options[router], "PROBE-R1", 100)
            second = self.add_node(router, options[router], "PROBE-R2", 300)
            self.findings["nodes_added"] = [first, second]
            if first:
                self.findings["interfaces"] = self.interfaces_of(first)
            if first and second:
                self.cable(first, second)
            if first:
                self.cloud(first)
                self.startup_config(first)
            final = self.topology("final topology") or {}
            self.findings["final_networks"] = final.get("networks")
        finally:
            if keep:
                self.findings["deleted"] = "kept (--keep)"
            else:
                self.cleanup()
        return self.report()

    def report(self) -> dict:
        return {"findings": self.findings, "steps": self.steps}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="PNETLab base URL, e.g. http://172.16.3.25")
    parser.add_argument("--username", required=True, help="The dedicated PNETLab account for NetAI")
    parser.add_argument("--folder", default="/NetAI-probe", help="Folder for the scratch lab")
    parser.add_argument("--keep", action="store_true", help="Do not delete the scratch lab")
    parser.add_argument("--insecure", action="store_true", help="Skip HTTPS certificate checks")
    parser.add_argument("--out", default=None, help="Report file (default: pnetlab_probe_<time>.json)")
    args = parser.parse_args(argv)

    password = os.environ.get("PNETLAB_PASSWORD") or getpass.getpass("PNETLab password: ")
    try:
        client = PNETLabClient(args.url, args.username, password, verify_tls=not args.insecure,
                               mode="session")
    except PNETLabError as exc:
        print(f"Cannot use {args.url}: {exc.message}", file=sys.stderr)
        return 2

    report = Probe(client, args.folder).run(keep=args.keep)
    out = Path(args.out or time.strftime("pnetlab_probe_%Y%m%d-%H%M%S.json"))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    findings = report["findings"]
    print(f"Report written to {out.resolve()}")
    for key in ("lab_path", "router_template", "create_lab_body", "nodes_added", "p2p_link",
                "cloud_network_add", "startup_config", "deleted"):
        print(f"  {key}: {findings.get(key)}")
    print(f"  templates_available: {', '.join(findings.get('templates_available', [])) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
