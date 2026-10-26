"""PanosClient over a fake Netmiko connection: configure -> set lines -> commit."""

import pytest
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from network_copilot.ssh.exceptions import (
    SSHAuthenticationError,
    SSHCommandError,
    SSHTimeoutError,
)
from network_copilot.ssh.panos import PanosClient, panos_body
from network_copilot.ssh.types import SSHTarget

TARGET = SSHTarget(host="172.16.3.123", port=22, username="netops", password="Panos-Pw1",
                   device_type="paloalto_panos")


class FakeConnection:
    def __init__(self, reject=(), commit_ok=True):
        self.reject = set(reject)
        self.commit_ok = commit_ok
        self.sent: list[str] = []
        self.in_config = False
        self.committed = False
        self.reverted = False
        self.disconnected = False
        self.kwargs = None

    def config_mode(self):
        self.in_config = True
        return "admin@PA-FW1# "

    def check_config_mode(self):
        return self.in_config

    def exit_config_mode(self):
        self.in_config = False
        return "admin@PA-FW1> "

    def send_command(self, command, **kwargs):
        self.sent.append(command)
        if command == "revert config":
            self.reverted = True
            return "Candidate configuration reverted.\n[edit]\nadmin@PA-FW1# "
        if command in self.reject:
            return f"{command}\n Invalid syntax.\n\n[edit]\nadmin@PA-FW1# "
        if command.startswith("show system info"):
            return "hostname: PA-FW1\nsw-version: 10.1.0\noperational-mode: normal\n"
        return f"{command}\n[edit]\nadmin@PA-FW1# "

    def commit(self, read_timeout=120):
        if not self.commit_ok:
            raise ValueError("Commit failed with the following errors:\n\n Validation Error: ...")
        self.committed = True
        return "Commit job 2 is in progress...100%\nConfiguration committed successfully\n"

    def disconnect(self):
        self.disconnected = True


def client_for(connection):
    def handler(**kwargs):
        connection.kwargs = kwargs
        return connection
    return PanosClient(TARGET, connect_handler=handler)


def test_wrappers_are_stripped_from_the_body():
    assert panos_body(["configure", " set  zone trust ", "commit", "exit", ""]) == ["set zone trust"]


def test_set_lines_are_entered_in_configure_mode_and_committed():
    connection = FakeConnection()
    result = client_for(connection).run_config(
        ["configure", "set network interface ethernet ethernet1/2 layer3 ip 10.0.0.1/24", "commit"])
    assert connection.kwargs["device_type"] == "paloalto_panos"
    assert connection.sent == ["set network interface ethernet ethernet1/2 layer3 ip 10.0.0.1/24"]
    assert connection.committed and not connection.reverted and connection.disconnected
    assert "Configuration committed successfully" in result.output


def test_a_rejected_line_reverts_and_never_commits():
    bad = "set zone trust network layer3 ethernet9/9"
    connection = FakeConnection(reject={bad})
    with pytest.raises(SSHCommandError, match="reverted and nothing was committed") as raised:
        client_for(connection).run_config(["set zone trust", bad, "set zone dmz"])
    assert connection.reverted and not connection.committed
    assert "set zone dmz" not in connection.sent  # stopped at the first rejection
    assert bad not in str(raised.value)  # device output and commands stay out of errors


def test_a_failed_commit_reverts_the_candidate():
    connection = FakeConnection(commit_ok=False)
    with pytest.raises(SSHCommandError, match="commit failed"):
        client_for(connection).run_config(["set zone trust"])
    assert connection.reverted and not connection.committed and connection.disconnected


def test_show_commands_and_errors_are_mapped():
    connection = FakeConnection()
    assert "PA-FW1" in client_for(connection).run_show("show system info").output
    with pytest.raises(SSHCommandError):
        client_for(FakeConnection()).run_exec(["request restart system"])

    def refuse(**kwargs):
        raise NetmikoAuthenticationException("nope")

    with pytest.raises(SSHAuthenticationError):
        PanosClient(TARGET, connect_handler=refuse).test_connection()

    def slow(**kwargs):
        raise NetmikoTimeoutException("slow")

    with pytest.raises(SSHTimeoutError):
        PanosClient(TARGET, connect_handler=slow).run_show("show system info")
