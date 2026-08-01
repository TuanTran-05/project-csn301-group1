"""Paramiko-backed SSH adapter.

Every network call in the application goes through this module. Credentials are
passed in memory only: they are never logged, never returned in an SSHResult and
never rendered by repr().
"""

import logging
import re
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

# Confirmation-style prompts that Cisco EXEC commands (write erase, reload,
# ...) show at the very end of their output while they wait for interactive
# input. Matched against the freshly drained response, never the whole
# transcript, so an anchored end-of-string search is enough.
_CONFIRM_PROMPTS = (
    (re.compile(r"\[confirm\]\s*$", re.I), "\n"),
    (re.compile(r"(?:\[yes/no\]|\(y/n\))\s*[:?]?\s*$", re.I), "yes\n"),
)

# "copy running-config startup-config" asks for a destination filename and
# offers a bracketed default. Accepting that default with a blank line is
# safe because the command's own name already fixes the target file.
_DESTINATION_FILENAME_PROMPT = re.compile(r"Destination filename.*\?\s*$", re.I)
_COPY_RUNNING_TO_STARTUP = "copy running-config startup-config"

# A shell that is back at its normal EXEC/config prompt ends with "#" or ">"
# (Cisco IOS/ASA convention). Anything else at the tail of a drained
# response means the device is still waiting on interactive input.
_READY_PROMPT = re.compile(r"[#>]\s*$")

# Cisco CLI reports command parsing failures as a percent-prefixed line before
# returning to the normal prompt. Match only that line form: prose that merely
# mentions one of these phrases is valid command output and must not fail a
# command batch.
_CISCO_COMMAND_FAILURE = re.compile(
    r"(?m)^% (?:Invalid input|Incomplete command|Ambiguous command)\b"
)


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

    # Device types whose SSH server does not honour a non-interactive "exec"
    # channel request (client.exec_command() then just hangs until timeout).
    # Confirmed against a real Cisco ASA in the CSN301 lab: an interactive
    # `ssh` session works fine, but exec_command() never returns. These
    # devices get the same interactive-shell approach run_config() already
    # uses instead.
    _SHELL_ONLY_DEVICE_TYPES = {"cisco_asa"}

    def run_show(self, command: str) -> SSHResult:
        """Run a single read-only command."""
        started = time.monotonic()
        client = self._connect()
        shell = None
        try:
            if self.target.device_type in self._SHELL_ONLY_DEVICE_TYPES:
                shell = client.invoke_shell()
                shell.settimeout(self.command_timeout)
                self._drain(shell)  # discard the login banner
                shell.send(f"{command}\n")
                output = self._drain(shell)
            else:
                _stdin, stdout, stderr = client.exec_command(
                    command, timeout=self.command_timeout
                )
                output = stdout.read().decode(errors="replace")
                errors = stderr.read().decode(errors="replace")
                if errors:
                    output = f"{output}{errors}" if output else errors
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
            if shell is not None:
                self._safe_close(shell)
            self._safe_close(client)

        return SSHResult(
            command=command,
            output=output,
            duration_ms=self._elapsed_ms(started),
        )

    # -- configuration commands -------------------------------------------
    def run_config(self, commands: list[str]) -> SSHResult:
        """Send configuration commands sequentially over an interactive shell."""
        return self._run_interactive(commands)

    # -- privileged EXEC commands -------------------------------------------
    def run_exec(self, commands: list[str], allow_confirm: bool = False) -> SSHResult:
        """Run privileged EXEC commands sequentially over an interactive shell.

        Some EXEC commands (``write erase``, ``reload``, ...) ask for an
        interactive confirmation before they act. Pass ``allow_confirm=True``
        to acknowledge those prompts; otherwise a batch never silently
        confirms something destructive and raises ``SSHCommandError`` instead.
        """
        return self._run_interactive(commands, allow_confirm=allow_confirm)

    # -- shared interactive-shell runner ------------------------------------
    def _run_interactive(
        self, commands: list[str], *, allow_confirm: bool = False
    ) -> SSHResult:
        """Send commands sequentially over an interactive shell.

        Shared by run_config() and run_exec(): both need the same
        login-banner drain, per-command send/drain loop, and interactive
        confirm-prompt handling.
        """
        if not commands:
            raise ValueError(
                "An interactive command batch requires at least one command."
            )

        started = time.monotonic()
        client = self._connect()
        shell = None
        try:
            shell = client.invoke_shell()
            shell.settimeout(self.command_timeout)
            transcript = self._drain(shell)

            for command in commands:
                shell.send(f"{command}\n")
                segment = self._respond_to_prompts(
                    shell, command, allow_confirm=allow_confirm
                )
                self._raise_for_cisco_command_failure(segment)
                transcript += segment
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
            if shell is not None:
                self._safe_close(shell)
            self._safe_close(client)

        return SSHResult(
            command="\n".join(commands),
            output=transcript,
            duration_ms=self._elapsed_ms(started),
        )

    def _respond_to_prompts(self, shell, command: str, *, allow_confirm: bool) -> str:
        """Drain a command's response, answering known interactive prompts.

        Returns everything the shell sent back for this command once it
        settles at its normal prompt. Raises SSHCommandError if the shell is
        left waiting on a prompt we don't recognise, rather than guessing.
        """
        segment = self._drain(shell)
        normalized = " ".join(command.split()).lower()

        while not _READY_PROMPT.search(segment.rstrip()):
            tail = segment.rstrip()

            for pattern, answer in _CONFIRM_PROMPTS:
                if pattern.search(tail):
                    if not allow_confirm:
                        raise SSHCommandError(
                            f"{self.target.host} is waiting for an interactive "
                            f"confirmation after '{command}': {tail!r}. Retry "
                            "with allow_confirm=True to acknowledge it."
                        )
                    shell.send(answer)
                    segment += self._drain(shell)
                    break
            else:
                if normalized == _COPY_RUNNING_TO_STARTUP and (
                    _DESTINATION_FILENAME_PROMPT.search(tail)
                ):
                    shell.send("\n")
                    segment += self._drain(shell)
                    continue

                raise SSHCommandError(
                    f"Unsupported interactive prompt from {self.target.host} "
                    f"after '{command}': {tail!r}"
                )

        return segment

    def _raise_for_cisco_command_failure(self, segment: str) -> None:
        """Raise for an anchored Cisco parser failure in one command response.

        Error messages deliberately exclude the command and raw device output:
        both can contain sensitive configuration values.
        """
        if _CISCO_COMMAND_FAILURE.search(segment):
            raise SSHCommandError(
                f"Cisco CLI rejected a command on {self.target.host}."
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
        device_type=device.device_type,
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
