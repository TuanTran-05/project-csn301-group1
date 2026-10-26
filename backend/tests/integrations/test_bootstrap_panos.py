"""Console bootstrap of a Palo Alto PA-VM, and PAN-OS in the PNETLab import."""

import pytest

from network_copilot.audit.model import AuditLog
from network_copilot.credentials.service import store_device_credential
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.topology import pnetlab_import
from network_copilot.topology.bootstrap_panos import panos_interface

SSH_PASSWORD = "Panos-Admin1"
SOURCE = {"url": "http://172.16.3.25", "username": "student1", "password": "pnet-pass", "lab": "/EIUSC_Network.unl"}
NODES = {
    "3": {"id": 3, "name": "Firewall", "template": "paloalto", "status": 2,
          "url": "telnet://172.16.3.25:30056", "port": 30056, "left": "50%", "top": "20%"},
}
NETWORKS = {"1": {"id": 1, "name": "Cloud0", "type": "pnet0"}}
TOPOLOGY = [
    {"source": "node3", "source_type": "node", "source_label": "eth1/3",
     "destination": "network1", "destination_type": "network", "destination_label": ""},
]


class FakeLab:
    def __init__(self, params):
        self.base_url = params["url"]

    def nodes(self, lab):
        return NODES

    def topology(self, lab):
        return TOPOLOGY

    def networks(self, lab):
        return NETWORKS


class FakePanos:
    """A PA-VM console: boot, first-login password change, configure, commit."""

    def __init__(self, booted=True, loading=False, reject=(), commit_ok=True):
        self.booted = booted
        self.loading = loading
        self.reject = set(reject)
        self.commit_ok = commit_ok
        self.passwords = {"admin": "admin"}
        self.force_change = True
        self.buffer = "\r\nPA-VM login: " if booted else ""
        self.state = "login"
        self.user = None
        self.config: list[str] = []
        self.typed: list[str] = []
        self.reverted = False
        self.committed = False
        self.pending_user = None

    def _prompt(self, mode=">"):
        return f"\r\n{self.user}@PA-VM{mode} "

    def send(self, text):
        for line in text.split("\r")[:-1]:
            self.typed.append(line)
            self._line(line)

    def _line(self, line):
        if not self.booted:
            return
        state = self.state
        if state == "login":
            if line:
                self.user, self.state, self.buffer = line, "password", self.buffer + "\r\nPassword: "
            else:
                self.buffer += "\r\nPA-VM login: "
        elif state == "password":
            if self.passwords.get(self.user) != line:
                self.state, self.buffer = "login", self.buffer + "\r\nLogin incorrect\r\nPA-VM login: "
            elif self.user == "admin" and self.force_change:
                self.state, self.buffer = "old", self.buffer + "\r\nEnter old password : "
            else:
                self.state, self.buffer = "op", self.buffer + self._prompt()
        elif state == "old":
            self.state, self.buffer = "new", self.buffer + "\r\nEnter new password : "
        elif state == "new":
            self.new_password = line
            self.state, self.buffer = "confirm", self.buffer + "\r\nConfirm password   : "
        elif state == "confirm":
            self.passwords["admin"], self.force_change = line, False
            self.state, self.buffer = "op", self.buffer + "\r\nPassword changed" + self._prompt()
        elif state == "op":
            if line == "show system info":
                body = ("\r\nServer error : Management server is not running; please wait"
                        if self.loading else "\r\nhostname: PA-VM\r\nsw-version: 10.1.0\r\noperational-mode: normal")
                self.buffer += body + self._prompt()
            elif line == "configure":
                self.state, self.buffer = "config", self.buffer + "\r\nEntering configuration mode\r\n[edit]" + self._prompt("#")
            else:
                self.buffer += self._prompt()
        elif state == "config":
            if line.startswith("set mgmt-config users") and line.endswith(" password"):
                self.pending_user = line.split()[3]
                self.state, self.buffer = "user_pw", self.buffer + "\r\nEnter password   : "
            elif line == "commit":
                if self.commit_ok:
                    self.committed = True
                    self.buffer += "\r\nCommit job 2 is in progress...100%\r\nConfiguration committed successfully\r\n[edit]" + self._prompt("#")
                else:
                    self.buffer += "\r\nCommit job 2 failed\r\nValidation Error: zone is invalid\r\n[edit]" + self._prompt("#")
            elif line == "revert config":
                self.reverted = True
                self.buffer += "\r\n[edit]" + self._prompt("#")
            elif line == "exit":
                self.state, self.buffer = "op", self.buffer + "\r\nExiting configuration mode" + self._prompt()
            elif line in self.reject:
                self.config.append(line)
                self.buffer += "\r\n Invalid syntax.\r\n\r\n[edit]" + self._prompt("#")
            else:
                self.config.append(line)
                self.buffer += "\r\n[edit]" + self._prompt("#")
        elif state == "user_pw":
            self.state, self.buffer = "user_confirm", self.buffer + "\r\nConfirm password : "
        elif state == "user_confirm":
            self.passwords[self.pending_user] = line
            self.state, self.buffer = "config", self.buffer + "\r\n[edit]" + self._prompt("#")

    def expect(self, patterns, timeout):
        for index, pattern in enumerate(patterns):
            match = pattern.search(self.buffer)
            if match:
                text, self.buffer = self.buffer[: match.end()], self.buffer[match.end():]
                return index, text
        text, self.buffer = self.buffer, ""
        return -1, text

    def close(self):
        pass


@pytest.fixture
def lab(app, ssh_factory):
    consoles = {}
    app.config.update(PNETLAB_CLIENT_FACTORY=FakeLab, BOOTSTRAP_PANOS_POLL_SECONDS=0.05,
                      BOOTSTRAP_PANOS_SSH_RETRY_SECONDS=0,
                      CONSOLE_CLIENT_FACTORY=lambda host, port: consoles.setdefault(port, FakePanos()))
    return consoles


@pytest.fixture
def firewall(project):
    device = Device(project_id=project.id, hostname="FIREWALL", management_ip="172.16.3.123",
                    device_type="paloalto_panos", role="firewall", kind="firewall", environment="pnetlab")
    db.session.add(device)
    db.session.commit()
    store_device_credential(device.id, "netops", SSH_PASSWORD)
    return device


@pytest.fixture
def base(project):
    return f"/api/projects/{project.id}/topology/bootstrap/pnetlab"


@pytest.mark.parametrize("label,expected", [
    ("eth1/3", "ethernet1/3"), ("e1/3", "ethernet1/3"), ("Ethernet1/3", "ethernet1/3"),
    ("eth3", "ethernet1/3"), ("mgmt", "mgmt"), ("eth0", "mgmt"), ("Gi0/1", None), ("", None),
])
def test_pnetlab_labels_map_to_panos_interfaces(label, expected):
    assert panos_interface(label) == expected


def test_paloalto_nodes_import_as_panos_firewalls(client, admin_headers, project, lab):
    assert pnetlab_import.classify("paloalto")["device_type"] == "paloalto_panos"
    base = f"/api/projects/{project.id}/topology/import/pnetlab"
    node = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE).get_json()["nodes"][0]
    assert node["supported"] and (node["device_type"], node["role"]) == ("paloalto_panos", "firewall")
    body = client.post(base, headers=admin_headers, json={
        **SOURCE, "nodes": [{"id": "3", "management_ip": "172.16.3.123"}]}).get_json()
    assert body["devices"][0]["status"] == "created"
    device = db.session.query(Device).one()
    assert (device.device_type, device.kind, device.management_ip) == ("paloalto_panos", "firewall", "172.16.3.123")


def test_preview_uses_the_cloud0_dataplane_port(client, admin_headers, base, lab, firewall):
    entry = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE).get_json()["devices"][0]
    assert entry["ready"] and entry["interface"] == "ethernet1/3" and entry["console"] == "172.16.3.25:30056"
    commands = entry["commands"]
    assert commands[0] == "configure" and commands[-1] == "commit"
    assert "set network profiles interface-management-profile AITOOL-MGMT ssh yes https yes ping yes" in commands
    assert "set network interface ethernet ethernet1/3 layer3 ip 172.16.3.123/24" in commands
    assert "set network interface ethernet ethernet1/3 layer3 interface-management-profile AITOOL-MGMT" in commands
    assert "set zone AITOOL-MGMT network layer3 ethernet1/3" in commands
    assert SSH_PASSWORD not in str(entry)


def test_preview_can_use_the_mgmt_port_instead(client, admin_headers, base, lab, firewall):
    payload = {**SOURCE, "gateway": "172.16.3.1", "devices": [{"device_id": firewall.id, "interface": "mgmt"}]}
    commands = client.post(f"{base}/preview", headers=admin_headers, json=payload).get_json()["devices"][0]["commands"]
    assert "set deviceconfig system ip-address 172.16.3.123 netmask 255.255.255.0" in commands
    assert "set deviceconfig system default-gateway 172.16.3.1" in commands
    assert not any("interface-management-profile" in c for c in commands)


def test_waits_while_booting_then_logs_in_changes_the_password_and_commits(
        client, admin_headers, base, lab, firewall, ssh_factory):
    lab[30056] = FakePanos(booted=False)
    first = client.post(base, headers=admin_headers, json=SOURCE).get_json()["devices"][0]
    assert first["status"] == "waiting" and first["boot_seconds"] == 900

    lab[30056] = fw = FakePanos(loading=True)
    assert client.post(base, headers=admin_headers, json=SOURCE).get_json()["devices"][0]["status"] == "waiting"

    lab[30056] = fw = FakePanos()
    done = client.post(base, headers=admin_headers, json=SOURCE).get_json()["devices"][0]
    assert done["status"] == "ok", done
    assert done["password_changed"]
    assert fw.passwords["admin"] == SSH_PASSWORD and fw.passwords["netops"] == SSH_PASSWORD
    assert fw.committed and not fw.reverted
    assert "set network interface ethernet ethernet1/3 layer3 ip 172.16.3.123/24" in fw.config
    assert "set mgmt-config users netops permissions role-based superuser yes" in fw.config
    assert ("test_connection", None) in ssh_factory.get("FIREWALL").calls
    audit = db.session.query(AuditLog).filter_by(action="topology.pnetlab_bootstrap").all()
    assert audit and all(SSH_PASSWORD not in str(row.details) for row in audit)


def test_a_failed_commit_is_reverted(client, admin_headers, base, lab, firewall):
    lab[30056] = fw = FakePanos(commit_ok=False)
    entry = client.post(base, headers=admin_headers, json=SOURCE).get_json()["devices"][0]
    assert entry["status"] == "error" and "commit failed" in entry["message"]
    assert fw.reverted and not fw.committed


def test_a_rejected_line_is_reported_and_reverted(client, admin_headers, base, lab, firewall):
    bad = "set zone AITOOL-MGMT network layer3 ethernet1/3"
    lab[30056] = fw = FakePanos(reject={bad})
    entry = client.post(base, headers=admin_headers, json=SOURCE).get_json()["devices"][0]
    assert entry["status"] == "error" and any(e.startswith(bad) for e in entry["errors"])
    assert fw.reverted and not fw.committed


def test_an_already_initialised_firewall_logs_in_with_the_saved_password(
        client, admin_headers, base, lab, firewall):
    lab[30056] = fw = FakePanos()
    fw.passwords = {"admin": "someone-else", "netops": SSH_PASSWORD}
    fw.force_change = False
    entry = client.post(base, headers=admin_headers, json=SOURCE).get_json()["devices"][0]
    assert entry["status"] == "ok" and not entry.get("password_changed")
    assert fw.typed[:2] == ["", "admin"]  # tried the factory login first


def test_unknown_passwords_end_in_a_clear_error(client, admin_headers, base, lab, firewall):
    lab[30056] = fw = FakePanos()
    fw.passwords = {"admin": "someone-else", "netops": "other"}
    fw.force_change = False
    entry = client.post(base, headers=admin_headers, json=SOURCE).get_json()["devices"][0]
    assert entry["status"] == "error" and "rejected the default and the saved password" in entry["message"]
