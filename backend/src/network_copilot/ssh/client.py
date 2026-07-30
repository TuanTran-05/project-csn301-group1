"""Paramiko-backed SSH adapter.

Every network call in the application goes through this module. Credentials are
passed in memory only: they are never logged, never returned in an SSHResult and
never rendered by repr().
"""

import logging
import socket
import time

import paramiko

from .exceptions import (
    SSHAuthenticationError,
    SSHCommandError,
    SSHConnectionError,
    SSHTimeoutError,
)
from .types import SSHResult, SSHTarget

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_COMMAND_TIMEOUT = 30

# Cisco IOSv / IOSv-L2 images (the devices in this lab) only speak
# diffie-hellman-group14-sha1 for key exchange and ssh-rsa for the host key.
# Paramiko, like modern OpenSSH, dropped both from its defaults as weak, so a
# vanilla connection fails with "Incompatible ssh peer (no acceptable kex
# algorithm)" before authentication is ever attempted. Confirmed against a real
# device in the CSN301 lab. Appending — not replacing — keeps modern algorithms
# preferred whenever a device actually offers them.
_LEGACY_KEX = "diffie-hellman-group14-sha1"
if _LEGACY_KEX not in paramiko.Transport._preferred_kex:
    paramiko.Transport._preferred_kex = paramiko.Transport._preferred_kex + (
        _LEGACY_KEX,
    )

_LEGACY_HOST_KEY = "ssh-rsa"
if _LEGACY_HOST_KEY not in paramiko.Transport._preferred_keys:
    paramiko.Transport._preferred_keys = paramiko.Transport._preferred_keys + (
        _LEGACY_HOST_KEY,
    )

# How long to keep reading an interactive shell after the last byte arrived.
_IDLE_SECONDS = 0.2
_POLL_SECONDS = 0.05
_READ_CHUNK = 65535


class SSHClient:
    """Opens a short-lived SSH session per operation."""

    def __init__(
        self,
        target: SSHTarget,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
        client_factory=paramiko.SSHClient,
    ):
        self.target = target
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._client_factory = client_factory

    # -- connection --------------------------------------------------------
    def _connect(self):
        client = self._client_factory()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.target.host,
                port=self.target.port,
                username=self.target.username,
                password=self.target.password,
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                # Lab devices use password auth only. Never touch the local
                # key store or the SSH agent.
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.AuthenticationException as exc:
            raise SSHAuthenticationError(
                f"Authentication failed for {self.target}."
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise SSHTimeoutError(
                f"Timed out connecting to {self.target} after "
                f"{self.connect_timeout}s."
            ) from exc
        except (paramiko.SSHException, OSError) as exc:
            raise SSHConnectionError(f"Could not connect to {self.target}.") from exc
        return client

    def test_connection(self) -> bool:
        """Return True when a session can be established. Raises on failure."""
        client = self._connect()
        try:
            logger.info("SSH reachability check succeeded for %s", self.target.host)
            return True
        finally:
            self._safe_close(client)

    # -- read-only commands ------------------------------------------------
    def run_show(self, command: str) -> SSHResult:
        """Run a single read-only command over exec_command."""
        started = time.monotonic()
        client = self._connect()
        try:
            _stdin, stdout, stderr = client.exec_command(
                command, timeout=self.command_timeout
            )
            output = stdout.read().decode(errors="replace")
            errors = stderr.read().decode(errors="replace")
        except (socket.timeout, TimeoutError) as exc:
            raise SSHTimeoutError(
                f"Command timed out on {self.target.host} after "
                f"{self.command_timeout}s."
            ) from exc
        except paramiko.SSHException as exc:
            raise SSHCommandError(
                f"Command failed on {self.target.host}."
            ) from exc
        finally:
            self._safe_close(client)

        if errors:
            output = f"{output}{errors}" if output else errors

        return SSHResult(
            command=command,
            output=output,
            duration_ms=self._elapsed_ms(started),
        )

    # -- configuration commands -------------------------------------------
    def run_config(self, commands: list[str]) -> SSHResult:
        """Send configuration commands sequentially over an interactive shell."""
        if not commands:
            raise ValueError("run_config requires at least one command.")

        started = time.monotonic()
        client = self._connect()
        shell = None
        try:
            shell = client.invoke_shell()
            shell.settimeout(self.command_timeout)
            transcript = self._drain(shell)

            for command in commands:
                shell.send(f"{command}\n")
                transcript += self._drain(shell)
        except (socket.timeout, TimeoutError) as exc:
            raise SSHTimeoutError(
                f"Configuration timed out on {self.target.host} after "
                f"{self.command_timeout}s."
            ) from exc
        except paramiko.SSHException as exc:
            raise SSHCommandError(
                f"Configuration failed on {self.target.host}."
            ) from exc
        finally:
            if shell is not None:
                self._safe_close(shell)
            self._safe_close(client)

        return SSHResult(
            command="\n".join(commands),
            output=transcript,
            duration_ms=self._elapsed_ms(started),
        )

    # -- helpers -----------------------------------------------------------
    def _drain(self, shell) -> str:
        """Read from an interactive shell until it goes quiet."""
        buffer = bytearray()
        deadline = time.monotonic() + self.command_timeout
        last_data_at = time.monotonic()

        while time.monotonic() < deadline:
            if shell.recv_ready():
                chunk = shell.recv(_READ_CHUNK)
                if chunk:
                    buffer.extend(chunk)
                    last_data_at = time.monotonic()
                    continue
            if time.monotonic() - last_data_at >= _IDLE_SECONDS:
                break
            time.sleep(_POLL_SECONDS)

        return buffer.decode(errors="replace")

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _safe_close(resource) -> None:
        try:
            resource.close()
        except Exception:  # pragma: no cover - closing must never mask errors
            logger.debug("Ignoring error while closing an SSH resource.")

    def close(self) -> None:
        """Kept for interface symmetry: sessions are already short-lived."""

    def __repr__(self) -> str:
        return f"<SSHClient {self.target}>"


def build_client_for_device(device):
    """Build an SSH client for a device, honouring a test/override factory."""
    from flask import current_app

    factory = current_app.config.get("SSH_CLIENT_FACTORY")
    if factory is not None:
        return factory(device)

    from ..credentials.service import get_device_credential

    credential = get_device_credential(device.id)
    target = SSHTarget(
        host=device.management_ip,
        port=device.ssh_port,
        username=credential.username,
        password=credential.password,
        enable_secret=credential.enable_secret,
    )
    return SSHClient(
        target,
        connect_timeout=current_app.config.get(
            "SSH_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT
        ),
        command_timeout=current_app.config.get(
            "SSH_COMMAND_TIMEOUT", DEFAULT_COMMAND_TIMEOUT
        ),
    )
