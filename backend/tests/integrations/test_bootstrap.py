import re
import socket
import threading

import pytest

from network_copilot.audit.model import AuditLog
from network_copilot.auth.model import User
from network_copilot.credentials.service import store_device_credential
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.integrations import console as console_mod
from network_copilot.projects.model import Project, ProjectMember
from network_copilot.topology import bootstrap

SSH_PASSWORD = "Bootstr4p!pw"
SOURCE = {"url": "http://172.16.0.5", "username": "admin", "password": "pnet-pass", "lab": "/Course/Lab1.unl"}

NODES = {
    "1": {"id": 1, "name": "R1", "template": "iol", "status": 2, "url": "telnet://172.16.0.5:32769"},
    "2": {"id": 2, "name": "SW1", "template": "iol-l2", "status": 2, "url": "telnet://172.16.0.5:32770"},
    "3": {"id": 3, "name": "R2", "template": "iol", "status": 0, "url": "telnet://172.16.0.5:32771"},
    "4": {"id": 4, "name": "R3", "template": "iol", "status": 2, "url": "telnet://172.16.0.5:32772"},
}
NETWORKS = {
    "1": {"id": 1, "name": "Mgmt", "type": "pnet0"},
    "2": {"id": 2, "name": "Net-R1-SW1", "type": "bridge"},
}
TOPOLOGY = [
    {"source": "node1", "source_type": "node", "source_label": "e0/3",
     "destination": "network1", "destination_type": "network", "destination_label": ""},
    {"source": "node2", "source_type": "node", "source_label": "e0/3",
     "destination": "network1", "destination_type": "network", "destination_label": ""},
    {"source": "node1", "source_type": "node", "source_label": "e0/0",
     "destination": "network2", "destination_type": "network", "destination_label": ""},
    {"source": "node2", "source_type": "node", "source_label": "e0/0",
     "destination": "network2", "destination_type": "network", "destination_label": ""},
]


class FakeLab:
    def __init__(self, params):
        self.params = params
        self.base_url = params["url"]

    def nodes(self, lab):
        return NODES

    def topology(self, lab):
        return TOPOLOGY

    def networks(self, lab):
        return NETWORKS


class FakeIOS:
    """A console that behaves like a freshly booted IOS node."""

    def __init__(self, users=(), addresses=None, reject=(), has_keys=False):
        self.buffer = "IOS boot...\r\nWould you like to enter the initial configuration dialog? [yes/no]: "
        self.mode = "dialog"
        self.hostname = "Router"
        self.users = list(users)
        self.addresses = dict(addresses or {})
        self.reject = set(reject)
        self.has_keys = has_keys
        self.config: list[str] = []
        self.exec: list[str] = []
        self.awaiting = None
        self.replaced_keys = False
        self.closed = False

    def _prompt(self):
        suffix = {"user": ">", "priv": "#", "config": "(config)#",
                  "config-if": "(config-if)#", "config-line": "(config-line)#"}[self.mode]
        return f"\r\n{self.hostname}{suffix}"

    def send(self, text):
        for line in text.split("\r")[:-1]:
            self._line(line)

    def _line(self, line):
        if self.awaiting == "replace":
            self.awaiting = None
            self.replaced_keys = line == "yes"
            self.buffer += self._prompt()
            return
        if self.mode == "dialog":
            if line == "no":
                self.mode = "return"
                self.buffer += "\r\nPress RETURN to get started!\r\n"
            else:
                self.buffer += "\r\n% Please answer 'yes' or 'no'.\r\nWould you like to enter the initial configuration dialog? [yes/no]: "
            return
        if self.mode == "return":
            self.mode = "user"
            self.buffer += self._prompt()
            return
        if self.mode == "user":
            if line == "enable":
                self.mode = "priv"
            self.buffer += self._prompt()
            return
        if self.mode == "priv":
            self.exec.append(line)
            output = ""
            if line.startswith("show running-config | include ^username"):
                output = "".join(f"\r\nusername {u} privilege 15 secret 9 xyz" for u in self.users)
            elif line == "show ip interface brief":
                output = "\r\nInterface              IP-Address      OK? Method Status                Protocol"
                for name in ("Ethernet0/0", "Ethernet0/3", "Vlan1"):
                    output += f"\r\n{name:<22} {self.addresses.get(name, 'unassigned'):<15} YES unset  up                    up"
            elif line == "configure terminal":
                self.mode = "config"
                output = "\r\nEnter configuration commands, one per line.  End with CNTL/Z."
            elif line == "write memory":
                output = "\r\nBuilding configuration...\r\n[OK]"
            self.buffer += output + self._prompt()
            return
        # configuration modes
        self.config.append(line)
        if line in self.reject:
            self.buffer += "\r\n           ^\r\n% Invalid input detected at '^' marker.\r\n" + self._prompt()
            return
        if line == "end":
            self.mode = "priv"
        elif line == "exit":
            self.mode = "config"
        elif line.startswith("hostname "):
            self.hostname = line.split()[1]
        elif line.startswith("interface "):
            self.mode = "config-if"
        elif line.startswith("line vty"):
            self.mode = "config-line"
        elif line.startswith("crypto key generate") and self.has_keys:
            self.awaiting = "replace"
            self.buffer += "\r\n% You already have RSA keys defined named R1.lab.\r\nDo you really want to replace them? [yes/no]: "
            return
        self.buffer += self._prompt()

    def expect(self, patterns, timeout):
        for index, pattern in enumerate(patterns):
            match = pattern.search(self.buffer)
            if match:
                text, self.buffer = self.buffer[: match.end()], self.buffer[match.end():]
                return index, text
        text, self.buffer = self.buffer, ""
        return -1, text

    def close(self):
        self.closed = True


class Consoles:
    def __init__(self):
        self.by_port: dict[int, FakeIOS] = {}
        self.opened: list[tuple[str, int]] = []

    def __call__(self, host, port):
        self.opened.append((host, port))
        return self.by_port.setdefault(port, FakeIOS())


@pytest.fixture
def lab(app, ssh_factory):
    consoles = Consoles()
    app.config["PNETLAB_CLIENT_FACTORY"] = FakeLab
    app.config["CONSOLE_CLIENT_FACTORY"] = consoles
    app.config["BOOTSTRAP_SSH_RETRY_SECONDS"] = 0
    return consoles


def _device(project, hostname, ip, kind, device_type="cisco_ios", credential=True):
    role = {"router": "core", "switch": "access", "firewall": "firewall"}[kind]
    device = Device(project_id=project.id, hostname=hostname, management_ip=ip, device_type=device_type,
                    role=role, kind=kind, environment="pnetlab")
    db.session.add(device)
    db.session.commit()
    if credential:
        store_device_credential(device.id, "netops", SSH_PASSWORD)
    return device


@pytest.fixture
def devices(project):
    return {
        "R1": _device(project, "R1", "172.16.3.111", "router"),
        "SW1": _device(project, "SW1", "172.16.3.121", "switch"),
    }


@pytest.fixture
def base(project):
    return f"/api/projects/{project.id}/topology/bootstrap/pnetlab"


# -- telnet plumbing ---------------------------------------------------------------


def test_telnet_options_are_answered_and_stripped():
    data = bytes([255, 251, 1, 255, 251, 3, 255, 253, 31]) + b"Router>" + bytes([255, 255])
    text, replies, tail = console_mod.strip_telnet(data)
    assert text == b"Router>\xff"
    assert replies == bytes([255, 253, 1, 255, 253, 3, 255, 252, 31])
    assert tail == b""


def test_split_and_subnegotiation_sequences():
    text, replies, tail = console_mod.strip_telnet(b"ab" + bytes([255, 251]))
    assert (text, replies, tail) == (b"ab", b"", bytes([255, 251]))
    text, replies, _ = console_mod.strip_telnet(bytes([24]) + b"cd", tail)
    assert text == b"cd" and replies == bytes([255, 254, 24])
    text, _, _ = console_mod.strip_telnet(bytes([255, 250, 24, 0, 1, 255, 240]) + b"ok")
    assert text == b"ok"


def test_telnet_console_over_a_real_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    received = []

    def serve():
        conn, _ = server.accept()
        conn.sendall(bytes([255, 251, 1]) + b"\r\nRouter>")
        received.append(conn.recv(64))
        received.append(conn.recv(64))
        conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    tty = console_mod.TelnetConsole("127.0.0.1", server.getsockname()[1], timeout=5)
    index, text = tty.expect([re.compile(r"Router>\Z")], 5)
    tty.send("enable\r")
    thread.join(5)
    tty.close()
    server.close()
    assert index == 0 and text.endswith("Router>")
    assert b"".join(received) == bytes([255, 253, 1]) + b"enable\r"


# -- planning ----------------------------------------------------------------------------


def test_the_cloud0_interface_is_found_from_the_lab():
    assert bootstrap.cloud_interfaces("1", TOPOLOGY, NETWORKS) == ["Ethernet0/3"]
    assert bootstrap.cloud_interfaces("3", TOPOLOGY, NETWORKS) == []


def test_preview_shows_commands_without_the_password(client, admin_headers, base, lab, devices):
    response = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE)
    assert response.status_code == 200
    body = response.get_json()
    by_name = {d["hostname"]: d for d in body["devices"]}
    r1, sw1 = by_name["R1"], by_name["SW1"]
    assert r1["ready"] and r1["interface"] == "Ethernet0/3" and r1["console"] == "172.16.0.5:32769"
    assert "ip address 172.16.3.111 255.255.255.0" in r1["commands"]
    assert "transport input ssh" in r1["commands"]
    assert "username netops privilege 15 secret ******" in r1["commands"]
    # an L2 switch keeps its address on Vlan1 and its cloud port in VLAN 1
    assert sw1["commands"][sw1["commands"].index("interface Vlan1") + 1] == "ip address 172.16.3.121 255.255.255.0"
    assert "switchport access vlan 1" in sw1["commands"]
    assert SSH_PASSWORD not in response.get_data(as_text=True)
    assert lab.opened == []  # a preview never opens a console


def test_preview_explains_what_cannot_be_bootstrapped(client, admin_headers, base, lab, project):
    _device(project, "R2", "172.16.3.112", "router")             # node stopped
    _device(project, "R3", "172.16.3.113", "router")             # no cloud cable
    _device(project, "R4", "172.16.3.114", "router")             # not in the lab
    _device(project, "FW", "172.16.3.115", "firewall", "cisco_asa")
    _device(project, "R9", "172.16.3.119", "router", credential=False)
    body = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE).get_json()
    reasons = {d["hostname"]: d["message"] for d in body["devices"]}
    assert not any(d["ready"] for d in body["devices"])
    assert "stopped" in reasons["R2"]
    assert "Cloud0" in reasons["R3"]
    assert "No node" in reasons["R4"]
    assert "ASA" in reasons["FW"]
    assert "SSH login" in reasons["R9"]


def test_a_named_node_and_interface_override_the_lab(client, admin_headers, base, lab, project):
    device = _device(project, "EDGE", "172.16.3.130", "router")
    payload = {**SOURCE, "devices": [{"device_id": device.id, "node_id": "4", "interface": "e0/1"}]}
    entry = client.post(f"{base}/preview", headers=admin_headers, json=payload).get_json()["devices"][0]
    assert entry["ready"] and entry["interface"] == "Ethernet0/1" and entry["node_id"] == "4"


def test_gateway_must_be_inside_the_management_network(client, admin_headers, base, lab, devices):
    bad = client.post(f"{base}/preview", headers=admin_headers, json={**SOURCE, "gateway": "10.0.0.1"})
    assert bad.status_code == 422
    good = client.post(f"{base}/preview", headers=admin_headers, json={**SOURCE, "gateway": "172.16.3.1"}).get_json()
    by_name = {d["hostname"]: d for d in good["devices"]}
    assert "ip route 0.0.0.0 0.0.0.0 172.16.3.1" in by_name["R1"]["commands"]
    assert "ip default-gateway 172.16.3.1" in by_name["SW1"]["commands"]


# -- running -------------------------------------------------------------------------------


def test_run_configures_each_node_on_its_console_and_checks_ssh(client, admin_headers, base, lab, devices, ssh_factory):
    response = client.post(base, headers=admin_headers, json=SOURCE)
    assert response.status_code == 202
    body = response.get_json()["job"]
    assert body["status"] == "done"
    assert {d["hostname"]: d["status"] for d in body["devices"]} == {"R1": "ok", "SW1": "ok"}

    r1 = lab.by_port[32769]
    assert r1.closed and r1.hostname == "R1"
    assert r1.config[:3] == ["hostname R1", "no ip domain-lookup", "ip domain-name netai.lab"]
    assert f"username netops privilege 15 secret {SSH_PASSWORD}" in r1.config
    assert r1.exec[-1] == "write memory"
    assert ("test_connection", None) in ssh_factory.get("R1").calls
    assert all(host == "172.16.0.5" for host, _ in lab.opened)

    assert SSH_PASSWORD not in response.get_data(as_text=True)
    audit = db.session.query(AuditLog).filter_by(action="topology.pnetlab_bootstrap").one()
    assert audit.result == "success"
    assert SSH_PASSWORD not in str(audit.details) and "pnet-pass" not in str(audit.details)


def test_existing_rsa_keys_are_kept(client, admin_headers, base, lab, devices):
    lab.by_port[32769] = FakeIOS(has_keys=True)
    body = client.post(base, headers=admin_headers, json=SOURCE).get_json()["job"]
    assert {d["hostname"]: d["status"] for d in body["devices"]}["R1"] == "ok"
    r1 = lab.by_port[32769]
    assert not r1.replaced_keys and "ip ssh version 2" in r1.config


def test_a_configured_device_is_left_alone_unless_overwrite(client, admin_headers, base, lab, devices):
    lab.by_port[32769] = FakeIOS(users=["student"], addresses={"Ethernet0/3": "192.168.1.5"})
    body = client.post(base, headers=admin_headers, json=SOURCE).get_json()["job"]
    r1 = next(d for d in body["devices"] if d["hostname"] == "R1")
    assert r1["status"] == "refused"
    assert "student" in r1["message"] and "192.168.1.5" in r1["message"]
    assert lab.by_port[32769].config == []

    lab.by_port[32769] = FakeIOS(users=["student"])
    body = client.post(base, headers=admin_headers, json={**SOURCE, "overwrite": True}).get_json()["job"]
    assert next(d for d in body["devices"] if d["hostname"] == "R1")["status"] == "ok"


def test_rejected_commands_are_reported_with_the_secret_masked(client, admin_headers, base, lab, devices):
    lab.by_port[32769] = FakeIOS(reject={"ip ssh version 2",
                                         f"username netops privilege 15 secret {SSH_PASSWORD}"})
    body = client.post(base, headers=admin_headers, json=SOURCE).get_json()["job"]
    r1 = next(d for d in body["devices"] if d["hostname"] == "R1")
    assert r1["status"] == "error"
    assert any(e.startswith("ip ssh version 2") for e in r1["errors"])
    assert SSH_PASSWORD not in str(r1["errors"])
    audit = db.session.query(AuditLog).filter_by(action="topology.pnetlab_bootstrap").one()
    assert audit.result == "failure"


def test_ssh_still_failing_after_the_console_is_reported(client, admin_headers, base, lab, devices, ssh_factory):
    ssh_factory.set_client("R1", reachable=False)
    body = client.post(base, headers=admin_headers, json=SOURCE).get_json()["job"]
    assert next(d for d in body["devices"] if d["hostname"] == "R1")["status"] == "configured"


def test_an_unreachable_console_fails_only_that_device(client, admin_headers, base, lab, devices, app):
    def factory(host, port):
        if port == 32769:
            raise console_mod.ConsoleError("Cannot open the console 172.16.0.5:32769: ConnectionRefusedError.")
        return lab(host, port)

    app.config["CONSOLE_CLIENT_FACTORY"] = factory
    body = client.post(base, headers=admin_headers, json=SOURCE).get_json()["job"]
    assert {d["hostname"]: d["status"] for d in body["devices"]} == {"R1": "error", "SW1": "ok"}


# -- permissions -------------------------------------------------------------------------------


def _login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {response.get_json()['access_token']}"}


def test_editors_preview_but_cannot_run(client, admin_user, lab):
    editor = User(username="editor1", role="OPERATOR")
    editor.set_password("EditorPass123!")
    db.session.add(editor)
    lab_project = Project(name="Lab2", management_network="172.16.3.0/24", environment="pnetlab",
                          owner_id=admin_user.id)
    db.session.add(lab_project)
    db.session.commit()
    db.session.add(ProjectMember(project_id=lab_project.id, user_id=editor.id, access="editor"))
    db.session.commit()
    _device(lab_project, "R1", "172.16.3.111", "router")

    headers = _login(client, "editor1", "EditorPass123!")
    url = f"/api/projects/{lab_project.id}/topology/bootstrap/pnetlab"
    assert client.post(f"{url}/preview", headers=headers, json=SOURCE).status_code == 200
    assert client.post(url, headers=headers, json=SOURCE).status_code == 403
    assert lab.opened == []


def test_viewers_cannot_preview(client, viewer_headers, base, lab, devices):
    assert client.post(f"{base}/preview", headers=viewer_headers, json=SOURCE).status_code == 403
    assert client.post(base, headers=viewer_headers, json=SOURCE).status_code == 403


@pytest.mark.parametrize("username,password", [("net ops", "Abc12345!"), ("netops", "what?ever1"),
                                               ("netops", "two words"), ("netops", "line\rbreak")])
def test_a_login_the_console_cannot_type_is_refused(client, admin_headers, base, lab, project, username, password):
    device = _device(project, "R1", "172.16.3.111", "router", credential=False)
    store_device_credential(device.id, username, password)
    entry = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE).get_json()["devices"][0]
    assert not entry["ready"] and "cannot be typed" in entry["message"]
