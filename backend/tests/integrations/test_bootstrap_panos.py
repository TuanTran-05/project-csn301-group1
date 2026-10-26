"""Console bootstrap of a Palo Alto PA-VM (background job), and PAN-OS in the import."""

import logging
import re
import socket
import threading

import pytest

from network_copilot.audit.model import AuditLog
from network_copilot.credentials.model import DeviceCredential
from network_copilot.credentials.service import store_device_credential
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.integrations import console as console_mod
from network_copilot.topology import pnetlab_import
from network_copilot.topology.bootstrap_panos import panos_interface, panos_password_problem

SSH_PASSWORD = "Panos-Admin1"
SOURCE = {"url": "http://172.16.3.25", "username": "student1", "password": "pnet-pass", "lab": "/EIUSC_Network.unl"}
NETWORKS = {"1": {"id": 1, "name": "Cloud0", "type": "pnet0"}}


def node(console="telnet"):
    return {"3": {"id": 3, "name": "Firewall", "template": "paloalto", "status": 2, "console": console,
                  "url": "telnet://172.16.3.25:30056", "port": 30056, "left": "50%", "top": "20%"}}


def cable(label):
    return [{"source": "node3", "source_type": "node", "source_label": label,
             "destination": "network1", "destination_type": "network", "destination_label": ""}]


LAB = {"nodes": node(), "topology": cable("mgmt")}


class FakeLab:
    def __init__(self, params):
        self.base_url = params["url"]

    def nodes(self, lab):
        return LAB["nodes"]

    def topology(self, lab):
        return LAB["topology"]

    def networks(self, lab):
        return NETWORKS


class FakePanos:
    """A PA-VM console: boot, first-login password change, configure, commit."""

    def __init__(self, boot_after=0, loading_polls=0, reject=(), commit_ok=True, force_change=True,
                 vnc=False):
        self.boot_after, self.waits = boot_after, 0
        self.loading_polls = loading_polls
        self.reject = set(reject)
        self.commit_ok = commit_ok
        self.force_change = force_change
        self.passwords = {"admin": "admin"}
        self.buffer = "RFB 003.008\n" if vnc else ("\r\nPA-VM login: " if boot_after == 0 else "")
        self.state = "login"
        self.user = None
        self.config: list[str] = []
        self.typed: list[str] = []
        self.op: list[str] = []
        self.reverted = self.committed = False
        self.pending_user = None
        self.partial = ""

    def _prompt(self, mode=">"):
        return f"\r\n{self.user}@PA-VM{mode} "

    def send(self, text):
        # typing may arrive in chunks; act on whole lines
        self.partial += text
        while "\r" in self.partial:
            line, self.partial = self.partial.split("\r", 1)
            self.typed.append(line)
            self._line(line)

    def _line(self, line):
        if self.waits < self.boot_after:
            return
        state = self.state
        if state == "login":
            if line:
                self.user, self.state = line, "password"
                self.buffer += "\r\nPassword: "
            else:
                self.buffer += "\r\nPA-VM login: "
        elif state == "password":
            if self.passwords.get(self.user) != line:
                self.state = "login"
                self.buffer += "\r\nLogin incorrect\r\nPA-VM login: "
            elif self.user == "admin" and self.force_change and line == "admin":
                self.state, self.buffer = "old", self.buffer + "\r\nEnter old password : "
            else:
                self.state, self.buffer = "op", self.buffer + self._prompt()
        elif state == "old":
            self.state, self.buffer = "new", self.buffer + "\r\nEnter new password : "
        elif state == "new":
            self.state, self.buffer = "confirm", self.buffer + "\r\nConfirm password   : "
        elif state == "confirm":
            self.passwords["admin"], self.force_change = line, False
            self.state, self.buffer = "op", self.buffer + "\r\nPassword changed" + self._prompt()
        elif state == "op":
            self.op.append(line)
            if line == "show system info":
                if self.loading_polls > 0:
                    self.loading_polls -= 1
                    self.buffer += "\r\nServer error : Management server is not running; please wait" + self._prompt()
                else:
                    self.buffer += "\r\nhostname: PA-VM\r\nsw-version: 10.0.1\r\noperational-mode: normal" + self._prompt()
            elif line == "configure":
                self.state, self.buffer = "config", self.buffer + "\r\nEntering configuration mode\r\n[edit]" + self._prompt("#")
            else:
                self.buffer += self._prompt()
        elif state == "config":
            match = re.fullmatch(r"set mgmt-config users (\S+) password", line)
            if line in self.reject:
                self.config.append(line)
                self.buffer += "\r\n Invalid syntax.\r\n\r\n[edit]" + self._prompt("#")
            elif match:
                self.pending_user = match.group(1)
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
            else:
                self.config.append(line)
                self.buffer += "\r\n[edit]" + self._prompt("#")
        elif state == "user_pw":
            self.state, self.buffer = "user_confirm", self.buffer + "\r\nConfirm password : "
        elif state == "user_confirm":
            self.passwords[self.pending_user] = line
            self.state, self.buffer = "config", self.buffer + "\r\n[edit]" + self._prompt("#")

    def expect(self, patterns, timeout):
        if self.waits < self.boot_after:
            self.waits += 1
            if self.waits == self.boot_after:
                self.buffer += "\r\nPA-VM login: "
            return -1, ""
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
    LAB.update(nodes=node(), topology=cable("mgmt"))
    consoles = {}
    app.config.update(PNETLAB_CLIENT_FACTORY=FakeLab,
                      CONSOLE_CLIENT_FACTORY=lambda host, port: consoles.setdefault(port, FakePanos()))
    return consoles


def make_firewall(project, username="netops", password=SSH_PASSWORD):
    device = Device(project_id=project.id, hostname="FIREWALL", management_ip="172.16.3.123",
                    device_type="paloalto_panos", role="firewall", kind="firewall", environment="pnetlab")
    db.session.add(device)
    db.session.commit()
    store_device_credential(device.id, username, password)
    return device


@pytest.fixture
def firewall(project):
    return make_firewall(project)


@pytest.fixture
def base(project):
    return f"/api/projects/{project.id}/topology/bootstrap/pnetlab"


def preview(client, headers, base, **extra):
    return client.post(f"{base}/preview", headers=headers, json={**SOURCE, **extra}).get_json()["devices"][0]


def run(client, headers, base, **extra):
    response = client.post(base, headers=headers, json={**SOURCE, **extra})
    assert response.status_code == 202, response.get_json()
    job = response.get_json()["job"]
    assert job["status"] == "done"
    return job, job["devices"][0]


# -- pure helpers -------------------------------------------------------------------


@pytest.mark.parametrize("label,expected", [
    ("eth1/3", "ethernet1/3"), ("e1/3", "ethernet1/3"), ("Ethernet1/3", "ethernet1/3"),
    ("eth3", "ethernet1/3"), ("mgmt", "mgmt"), ("eth0", "mgmt"), ("Gi0/1", None), ("", None),
])
def test_pnetlab_labels_map_to_panos_interfaces(label, expected):
    assert panos_interface(label) == expected


@pytest.mark.parametrize("password,problem", [
    ("Panos-Admin1", None), ("Abcdefg1", None), ("Abcdefg!", None),
    ("Abc12", "at least 8 characters"), ("abcdefg1", "an upper-case letter"),
    ("ABCDEFG1", "a lower-case letter"), ("Abcdefgh", "a digit or a special character"),
])
def test_passwords_are_checked_against_the_panos_rules(password, problem):
    assert panos_password_problem(password) == problem


def test_a_vnc_console_is_recognised_on_a_real_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        conn.sendall(b"RFB 003.008\n")
        conn.recv(16)
        conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    tty = console_mod.TelnetConsole("127.0.0.1", server.getsockname()[1], timeout=5)
    with pytest.raises(console_mod.ConsoleError, match="VNC, đổi sang TELNET"):
        tty.expect([re.compile(r"login:")], 5)
    tty.close()
    thread.join(5)
    server.close()


# -- import and preview ----------------------------------------------------------------


def test_paloalto_nodes_import_as_panos_firewalls(client, admin_headers, project, lab):
    assert pnetlab_import.classify("paloalto")["device_type"] == "paloalto_panos"
    base = f"/api/projects/{project.id}/topology/import/pnetlab"
    imported = client.post(f"{base}/preview", headers=admin_headers, json=SOURCE).get_json()["nodes"][0]
    assert imported["supported"] and (imported["device_type"], imported["role"]) == ("paloalto_panos", "firewall")
    body = client.post(base, headers=admin_headers, json={
        **SOURCE, "nodes": [{"id": "3", "management_ip": "172.16.3.123"}]}).get_json()
    assert body["devices"][0]["status"] == "created"
    device = db.session.query(Device).one()
    assert (device.device_type, device.kind, device.management_ip) == ("paloalto_panos", "firewall", "172.16.3.123")


def test_mgmt_cabled_to_cloud0_is_the_default_and_needs_no_zone(client, admin_headers, base, lab, firewall):
    LAB["topology"] = cable("mgmt") + cable("eth1/3")
    entry = preview(client, admin_headers, base, gateway="172.16.3.1")
    assert entry["ready"] and entry["interface"] == "mgmt" and entry["mode"] == "mgmt"
    commands = entry["commands"]
    assert commands[0] == "configure" and commands[-1] == "commit"
    ip_line = commands.index("set deviceconfig system ip-address 172.16.3.123 netmask 255.255.255.0")
    # the mgmt port is a DHCP client until it is made static
    assert commands[ip_line - 1] == "set deviceconfig system type static"
    assert "set deviceconfig system default-gateway 172.16.3.1" in commands
    assert not any(word in " ".join(commands) for word in ("zone", "virtual-router", "interface-management-profile"))
    # admin (default): no extra user is created
    assert not any(c.startswith("set mgmt-config") for c in commands)
    assert SSH_PASSWORD not in str(entry)


def test_a_dataplane_port_stays_available(client, admin_headers, base, lab, firewall):
    LAB["topology"] = cable("eth1/3")
    entry = preview(client, admin_headers, base)
    assert entry["interface"] == "ethernet1/3" and entry["mode"] == "dataplane"
    assert "set network interface ethernet ethernet1/3 layer3 ip 172.16.3.123/24" in entry["commands"]
    assert "set zone AITOOL-MGMT network layer3 ethernet1/3" in entry["commands"]
    named = preview(client, admin_headers, base, devices=[{"device_id": firewall.id, "interface": "ethernet1/3"}])
    assert named["interface"] == "ethernet1/3"


def test_creating_the_user_sets_its_password_before_its_role(client, admin_headers, base, lab, firewall):
    commands = preview(client, admin_headers, base, panos_account="create")["commands"]
    users = [c for c in commands if c.startswith("set mgmt-config users netops")]
    assert users[0].startswith("set mgmt-config users netops password")
    assert users[1] == "set mgmt-config users netops permissions role-based superuser yes"


def test_weak_passwords_and_vnc_consoles_are_caught_in_the_preview(client, admin_headers, base, lab, project):
    make_firewall(project, password="password1")
    assert "upper-case letter" in preview(client, admin_headers, base)["message"]
    db.session.query(DeviceCredential).delete()
    db.session.query(Device).delete()
    db.session.commit()
    make_firewall(project)
    LAB["nodes"] = node(console="vnc")
    assert preview(client, admin_headers, base)["message"] == "Console đang là VNC, đổi sang TELNET trong PNETLab."


# -- the job --------------------------------------------------------------------------------


def test_waits_for_the_boot_logs_in_as_admin_and_commits(client, admin_headers, base, lab, firewall, ssh_factory, caplog):
    lab[30056] = fw = FakePanos(boot_after=3, loading_polls=2)
    caplog.set_level(logging.INFO)
    job, result = run(client, admin_headers, base)
    assert result["status"] == "ok", result
    assert result["password_changed"] and result["account"] == "admin" and result["credential_user"] == "admin"
    assert fw.passwords["admin"] == SSH_PASSWORD and fw.committed and not fw.reverted
    assert fw.config[1:3] == ["set deviceconfig system type static",
                              "set deviceconfig system ip-address 172.16.3.123 netmask 255.255.255.0"]
    assert not any("mgmt-config" in line or "zone" in line for line in fw.config)
    assert "set cli scripting-mode on" in fw.op
    # SSH is checked, and from now on as admin with the saved password
    assert ("test_connection", None) in ssh_factory.get("FIREWALL").calls
    credential = db.session.query(DeviceCredential).filter_by(device_id=firewall.id).one()
    assert credential.username == "admin"
    # every step is in the job log and the journal, masked
    assert [s["command"] for s in job["log"] if s["command"] == "commit"] == ["commit"]
    assert job["log"][0]["command"] == "login as admin"
    assert any("PAN-OS bootstrap step ok: commit" in r.message for r in caplog.records)
    assert SSH_PASSWORD not in str(job) and SSH_PASSWORD not in caplog.text
    # the page reads the same job back
    again = client.get(f"/api/projects/{firewall.project_id}/topology/bootstrap/jobs/{job['id']}",
                       headers=admin_headers).get_json()["job"]
    assert again["id"] == job["id"] and again["devices"][0]["status"] == "ok"
    audit = db.session.query(AuditLog).filter_by(action="topology.pnetlab_bootstrap").one()
    assert audit.result == "success" and SSH_PASSWORD not in str(audit.details)


def test_a_failed_secondary_step_is_reported_and_the_rest_committed(client, admin_headers, base, lab, firewall):
    bad = "set mgmt-config users netops permissions role-based superuser yes"
    lab[30056] = fw = FakePanos(reject={bad, "set deviceconfig system hostname FIREWALL"})
    _, result = run(client, admin_headers, base, panos_account="create")
    assert fw.committed and not fw.reverted
    assert fw.passwords["netops"] == SSH_PASSWORD
    assert bad in result["warnings"] and "set deviceconfig system hostname FIREWALL" in result["warnings"]
    failed = [s for s in result["steps"] if not s["ok"]]
    assert all("Invalid syntax" in s["output"] for s in failed)


def test_a_failed_essential_step_reverts_and_does_not_commit(client, admin_headers, base, lab, firewall):
    bad = "set deviceconfig system ip-address 172.16.3.123 netmask 255.255.255.0"
    lab[30056] = fw = FakePanos(reject={bad})
    _, result = run(client, admin_headers, base)
    assert result["status"] == "error" and "nothing was committed" in result["message"]
    assert fw.reverted and not fw.committed and result["errors"] == [bad]


def test_a_failed_commit_is_reverted(client, admin_headers, base, lab, firewall):
    lab[30056] = fw = FakePanos(commit_ok=False)
    _, result = run(client, admin_headers, base)
    assert result["status"] == "error" and "commit failed" in result["message"]
    assert fw.reverted and not fw.committed
    assert any("Validation Error" in s["output"] for s in result["steps"] if s["command"] == "commit")


def test_admin_without_a_forced_change_gets_the_saved_password(client, admin_headers, base, lab, firewall):
    lab[30056] = fw = FakePanos(force_change=False)
    _, result = run(client, admin_headers, base)
    assert result["status"] == "ok" and not result.get("password_changed")
    assert fw.passwords["admin"] == SSH_PASSWORD


def test_a_firewall_that_never_boots_ends_in_a_clear_error(client, admin_headers, base, lab, firewall, app):
    app.config["BOOTSTRAP_PANOS_BOOT_SECONDS"] = 0.2
    lab[30056] = FakePanos(boot_after=10**9)
    _, result = run(client, admin_headers, base)
    assert result["status"] == "error" and "chưa khởi động xong" in result["message"]


def test_a_vnc_console_fails_at_once(client, admin_headers, base, lab, firewall):
    lab[30056] = FakePanos(vnc=True)
    _, result = run(client, admin_headers, base)
    assert result["status"] == "error" and result["message"] == "Console đang là VNC, đổi sang TELNET trong PNETLab."


def test_unknown_passwords_end_in_a_clear_error(client, admin_headers, base, lab, firewall):
    lab[30056] = fw = FakePanos(force_change=False)
    fw.passwords = {"admin": "someone-else", "netops": "other"}
    _, result = run(client, admin_headers, base)
    assert result["status"] == "error" and "rejected the default and the saved password" in result["message"]


def test_jobs_belong_to_their_project(client, admin_headers, base, lab, firewall, project):
    job, _ = run(client, admin_headers, base)
    assert client.get(f"/api/projects/{project.id}/topology/bootstrap/jobs/nope",
                      headers=admin_headers).status_code == 404
    from network_copilot.projects.model import Project
    other = Project(name="Other", management_network="10.9.0.0/24", environment="pnetlab")
    db.session.add(other)
    db.session.commit()
    assert client.get(f"/api/projects/{other.id}/topology/bootstrap/jobs/{job['id']}",
                      headers=admin_headers).status_code == 404


def test_a_bad_request_fails_before_any_job(client, admin_headers, base, lab, firewall):
    response = client.post(base, headers=admin_headers, json={**SOURCE, "panos_account": "root"})
    assert response.status_code == 422


def test_entering_with_the_saved_user_still_gives_admin_the_saved_password(client, admin_headers, base, lab, firewall):
    lab[30056] = fw = FakePanos(force_change=False)
    fw.passwords = {"admin": "factory-changed", "netops": SSH_PASSWORD}
    _, result = run(client, admin_headers, base)
    assert result["status"] == "ok" and fw.passwords["admin"] == SSH_PASSWORD
