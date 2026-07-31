import socket

import paramiko
import pytest

from network_copilot.credentials.service import store_device_credential
from network_copilot.ssh.client import SSHClient, build_client_for_device
from network_copilot.ssh.exceptions import (
    SSHAuthenticationError,
    SSHConnectionError,
    SSHTimeoutError,
)
from network_copilot.ssh.types import SSHResult, SSHTarget

TARGET = SSHTarget(
    host="10.10.10.11", port=22, username="ai-automation", password="Secret123!"
)


# -- legacy Cisco IOSv compatibility ---------------------------------------
# Confirmed against a real IOSv device in the CSN301 lab: it offers only
# diffie-hellman-group14-sha1 for key exchange and ssh-rsa for the host key.
# Paramiko 5.x dropped both from its defaults, so a vanilla connection fails
# with "Incompatible ssh peer (no acceptable kex algorithm)" before
# authentication is ever attempted.


def test_legacy_kex_algorithm_is_enabled_for_old_cisco_devices():
    assert "diffie-hellman-group14-sha1" in paramiko.Transport._preferred_kex


def test_legacy_host_key_algorithm_is_enabled_for_old_cisco_devices():
    assert "ssh-rsa" in paramiko.Transport._preferred_keys


def test_legacy_kex_algorithm_has_a_real_implementation():
    """Paramiko 5.x kept the algorithm name in _preferred_kex but deleted the
    class that actually performs it from _kex_info. Negotiation then succeeds
    and the connection dies with a bare KeyError deep inside Transport.run(),
    confirmed against a real device. Being *offered* is not being *usable*;
    this guards against losing execution capability again on a future
    Paramiko upgrade."""
    assert "diffie-hellman-group14-sha1" in paramiko.Transport._kex_info


def test_legacy_algorithms_are_appended_not_preferred():
    """Modern algorithms must still win the negotiation when a device offers
    them: the legacy entries are appended, never inserted ahead of anything
    stronger."""
    kex = paramiko.Transport._preferred_kex
    assert kex.index("diffie-hellman-group14-sha1") > kex.index(
        "curve25519-sha256@libssh.org"
    )

    host_keys = paramiko.Transport._preferred_keys
    assert host_keys.index("ssh-rsa") > host_keys.index("ssh-ed25519")


class FakeChannelFile:
    def __init__(self, payload: bytes = b""):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class FakeShell:
    """Minimal stand-in for a paramiko interactive shell channel."""

    def __init__(self, banner: str = "CORE-SW1#"):
        self.sent: list[str] = []
        self._buffer = banner.encode()
        self.settimeout_called_with = None

    def settimeout(self, value):
        self.settimeout_called_with = value

    def send(self, data: str) -> int:
        self.sent.append(data)
        self._buffer += f"{data.strip()}\nCORE-SW1(config)#".encode()
        return len(data)

    def recv_ready(self) -> bool:
        return bool(self._buffer)

    def recv(self, size: int) -> bytes:
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    def close(self) -> None:
        pass


class FakeParamikoClient:
    """Stand-in for paramiko.SSHClient used to drive SSHClient in tests."""

    def __init__(
        self,
        exec_output: bytes = b"",
        exec_error: bytes = b"",
        connect_error: Exception | None = None,
        shell: FakeShell | None = None,
    ):
        self.exec_output = exec_output
        self.exec_error = exec_error
        self.connect_error = connect_error
        self.shell = shell or FakeShell()
        self.connect_kwargs: dict = {}
        self.missing_host_key_policy = None
        self.closed = False

    def set_missing_host_key_policy(self, policy):
        self.missing_host_key_policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error

    def exec_command(self, command, timeout=None):
        return (
            FakeChannelFile(),
            FakeChannelFile(self.exec_output),
            FakeChannelFile(self.exec_error),
        )

    def invoke_shell(self):
        return self.shell

    def close(self):
        self.closed = True


def make_client(**kwargs) -> tuple[SSHClient, FakeParamikoClient]:
    fake = FakeParamikoClient(**kwargs)
    return SSHClient(TARGET, client_factory=lambda: fake), fake


# -- test_connection ------------------------------------------------------


def test_test_connection_succeeds():
    client, fake = make_client()
    assert client.test_connection() is True
    assert fake.closed is True


def test_test_connection_raises_on_timeout():
    client, _ = make_client(connect_error=socket.timeout("timed out"))
    with pytest.raises(SSHTimeoutError):
        client.test_connection()


def test_test_connection_raises_on_auth_failure():
    client, _ = make_client(
        connect_error=paramiko.AuthenticationException("bad password")
    )
    with pytest.raises(SSHAuthenticationError):
        client.test_connection()


def test_test_connection_raises_on_refused_connection():
    client, _ = make_client(connect_error=ConnectionRefusedError("refused"))
    with pytest.raises(SSHConnectionError):
        client.test_connection()


def test_connect_disables_key_auth():
    client, fake = make_client()
    client.test_connection()
    assert fake.connect_kwargs["look_for_keys"] is False
    assert fake.connect_kwargs["allow_agent"] is False
    assert fake.connect_kwargs["hostname"] == "10.10.10.11"
    assert fake.connect_kwargs["port"] == 22


# -- run_show -------------------------------------------------------------


def test_run_show_returns_result():
    client, _ = make_client(exec_output=b"GigabitEthernet0/0 is up\n")
    result = client.run_show("show ip interface brief")
    assert isinstance(result, SSHResult)
    assert result.command == "show ip interface brief"
    assert "GigabitEthernet0/0 is up" in result.output
    assert result.duration_ms >= 0


def test_run_show_includes_stderr():
    client, _ = make_client(exec_output=b"", exec_error=b"% Invalid input detected")
    result = client.run_show("show bogus")
    assert "% Invalid input detected" in result.output


def test_run_show_wraps_socket_timeout():
    client, _ = make_client(connect_error=socket.timeout("timed out"))
    with pytest.raises(SSHTimeoutError):
        client.run_show("show ip interface brief")


# -- run_show on devices that only support an interactive shell -----------
# Confirmed against a real Cisco ASA in the CSN301 lab: its SSH server does
# not honour a non-interactive "exec" channel request the way IOS does, so
# client.exec_command() never returns and every read-only command times out
# after command_timeout even though an interactive `ssh` session works fine.


ASA_TARGET = SSHTarget(
    host="10.10.10.3",
    port=22,
    username="g1lab",
    password="Secret123!",
    device_type="cisco_asa",
)


def test_run_show_uses_an_interactive_shell_for_asa_devices():
    shell = FakeShell(banner="FW-01# ")
    fake = FakeParamikoClient(shell=shell)
    client = SSHClient(ASA_TARGET, client_factory=lambda: fake)

    result = client.run_show("show version")

    assert any("show version" in sent for sent in shell.sent)
    assert isinstance(result, SSHResult)
    assert result.command == "show version"


def test_run_show_still_uses_exec_command_for_non_asa_devices():
    """The default (no device_type, or anything other than cisco_asa) path
    is unchanged: a single non-interactive exec_command call, not a shell."""
    client, fake = make_client(exec_output=b"GigabitEthernet0/0 is up\n")
    result = client.run_show("show ip interface brief")
    assert fake.shell.sent == []
    assert "GigabitEthernet0/0 is up" in result.output


# -- run_config -----------------------------------------------------------


def test_run_config_sends_every_command_in_order():
    shell = FakeShell()
    client, _ = make_client(shell=shell)
    result = client.run_config(["configure terminal", "vlan 25", "name MARKETING", "end"])
    sent = [line.strip() for line in shell.sent if line.strip()]
    assert sent == ["configure terminal", "vlan 25", "name MARKETING", "end"]
    assert isinstance(result, SSHResult)
    assert result.output


def test_run_config_rejects_an_empty_batch():
    client, _ = make_client()
    with pytest.raises(ValueError):
        client.run_config([])


# -- secret hygiene -------------------------------------------------------


def test_repr_never_leaks_the_password():
    client, _ = make_client()
    assert "Secret123!" not in repr(client)
    assert "Secret123!" not in repr(TARGET)
    assert "Secret123!" not in str(TARGET)


def test_ssh_result_never_carries_credentials():
    client, _ = make_client(exec_output=b"ok")
    result = client.run_show("show clock")
    assert "Secret123!" not in str(result)


# -- build_client_for_device ---------------------------------------------


def test_build_client_for_device_uses_stored_credentials(app, device):
    store_device_credential(device.id, "ai-automation", "Secret123!")
    client = build_client_for_device(device)
    assert client.target.host == "10.10.10.11"
    assert client.target.port == 22
    assert client.target.username == "ai-automation"


def test_build_client_for_device_honours_the_configured_factory(app, device):
    sentinel = object()
    app.config["SSH_CLIENT_FACTORY"] = lambda _device: sentinel
    assert build_client_for_device(device) is sentinel


# -- POST /api/devices/<id>/test-connection -------------------------------


def test_test_connection_endpoint_marks_device_online(
    client, admin_headers, app, device, ssh_factory
):
    ssh_factory.set_client(device.hostname, reachable=True)
    response = client.post(
        f"/api/devices/{device.id}/test-connection", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "online"
    assert client.get(f"/api/devices/{device.id}", headers=admin_headers).get_json()[
        "status"
    ] == "online"


def test_test_connection_endpoint_marks_device_offline(
    client, admin_headers, app, device, ssh_factory
):
    ssh_factory.set_failing(device.hostname, SSHConnectionError("no route to host"))
    response = client.post(
        f"/api/devices/{device.id}/test-connection", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "offline"
    assert response.get_json()["reachable"] is False


def test_test_connection_endpoint_requires_admin(
    client, viewer_headers, device, ssh_factory
):
    response = client.post(
        f"/api/devices/{device.id}/test-connection", headers=viewer_headers
    )
    assert response.status_code == 403


def test_test_connection_endpoint_404s_for_unknown_device(client, admin_headers):
    assert (
        client.post("/api/devices/999/test-connection", headers=admin_headers).status_code
        == 404
    )


def test_test_connection_response_never_leaks_credentials(
    client, admin_headers, app, device, ssh_factory
):
    store_device_credential(device.id, "ai-automation", "Secret123!")
    ssh_factory.set_client(device.hostname, reachable=True)
    body = client.post(
        f"/api/devices/{device.id}/test-connection", headers=admin_headers
    ).get_data(as_text=True)
    assert "Secret123!" not in body
