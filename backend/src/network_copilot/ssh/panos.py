"""SSH adapter for Palo Alto Networks firewalls (PAN-OS), built on Netmiko.

PAN-OS has its own CLI model: an operational prompt (``user@host>``), a
``configure`` mode (``user@host#``) where ``set``/``delete`` edit a *candidate*
configuration, and ``commit`` that makes the candidate live. Netmiko's
``paloalto_panos`` driver already handles the slow prompts, paging and the
commit dialogue, so this adapter only maps it onto the interface the rest of
the backend uses (``test_connection``, ``run_show``, ``run_config``).

A change is all or nothing: if any ``set`` line is rejected or the commit
fails, the candidate configuration is reverted (``revert config``) before the
error is raised, so nothing half-applied is left waiting for the next commit.
Errors never quote the device output: it can contain configuration values.
"""

import logging
import re
import time

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
# A PAN-OS commit routinely takes one to several minutes on a PA-VM.
COMMIT_TIMEOUT = 600

# Wrappers the change workflow (or an operator) may put around the body; the
# adapter enters configure mode and commits by itself.
PANOS_WRAPPERS = {"configure", "commit", "exit", "quit", "end", "configure terminal"}

# How PAN-OS reports a rejected configuration line.
PANOS_CONFIG_ERROR = re.compile(
    r"(?im)^\s*(?:invalid syntax|unknown command|server error|validation error|"
    r"error:|.*is not a valid reference|.*is an invalid)"
)
COMMIT_SUCCESS = "configuration committed successfully"


def panos_body(commands: list[str]) -> list[str]:
    """The configuration lines of a change, without configure/commit wrappers."""
    return [
        " ".join(str(command).split())
        for command in commands
        if str(command).strip() and " ".join(str(command).split()).lower() not in PANOS_WRAPPERS
    ]


def _connect_handler(**kwargs):
    from netmiko import ConnectHandler

    return ConnectHandler(**kwargs)


class PanosClient:
    """Opens a short-lived Netmiko session per operation."""

    def __init__(
        self,
        target: SSHTarget,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
        commit_timeout: int = COMMIT_TIMEOUT,
        connect_handler=_connect_handler,
    ):
        self.target = target
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.commit_timeout = commit_timeout
        self._connect_handler = connect_handler

    # -- connection --------------------------------------------------------
    def _connect(self):
        from netmiko.exceptions import (
            NetmikoAuthenticationException,
            NetmikoTimeoutException,
        )

        try:
            return self._connect_handler(
                device_type="paloalto_panos",
                host=self.target.host,
                port=self.target.port,
                username=self.target.username,
                password=self.target.password,
                conn_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                fast_cli=False,
                use_keys=False,
                allow_agent=False,
            )
        except NetmikoAuthenticationException as exc:
            raise SSHAuthenticationError(f"Authentication failed for {self.target}.") from exc
        except NetmikoTimeoutException as exc:
            raise SSHTimeoutError(
                f"Timed out connecting to {self.target} after {self.connect_timeout}s."
            ) from exc
        except Exception as exc:  # paramiko / socket errors surface as many types
            raise SSHConnectionError(f"Could not connect to {self.target}.") from exc

    @staticmethod
    def _close(connection) -> None:
        try:
            connection.disconnect()
        except Exception:  # pragma: no cover - closing must never mask errors
            logger.debug("Ignoring error while closing a PAN-OS session.")

    def test_connection(self) -> bool:
        connection = self._connect()
        self._close(connection)
        logger.info("SSH reachability check succeeded for %s", self.target.host)
        return True

    # -- read-only commands ------------------------------------------------
    def run_show(self, command: str) -> SSHResult:
        started = time.monotonic()
        connection = self._connect()
        try:
            output = connection.send_command(command, read_timeout=self.command_timeout)
        except Exception as exc:
            raise self._command_error(exc, f"Command failed on {self.target.host}.") from exc
        finally:
            self._close(connection)
        return SSHResult(command=command, output=output, duration_ms=_elapsed_ms(started))

    # -- configuration -----------------------------------------------------
    def run_config(self, commands: list[str]) -> SSHResult:
        """Apply ``set``/``delete`` lines in configure mode, then commit.

        Raises SSHCommandError (after reverting the candidate) when a line is
        rejected or the commit does not report success.
        """
        body = panos_body(commands)
        if not body:
            raise ValueError("A PAN-OS change requires at least one configuration line.")
        started = time.monotonic()
        connection = self._connect()
        transcript = ""
        try:
            transcript += connection.config_mode()
            for line in body:
                output = connection.send_command(
                    line, expect_string=r"#", read_timeout=self.command_timeout,
                    strip_prompt=False, strip_command=False,
                )
                transcript += output
                if PANOS_CONFIG_ERROR.search(output):
                    self._revert(connection)
                    raise SSHCommandError(
                        f"PAN-OS rejected a configuration line on {self.target.host}; "
                        "the candidate configuration was reverted and nothing was committed."
                    )
            try:
                transcript += connection.commit(read_timeout=self.commit_timeout)
            except ValueError as exc:
                self._revert(connection)
                raise SSHCommandError(
                    f"PAN-OS commit failed on {self.target.host}; the candidate "
                    "configuration was reverted and nothing was applied."
                ) from exc
        except SSHCommandError:
            raise
        except Exception as exc:
            self._revert(connection)
            raise self._command_error(exc, f"Configuration failed on {self.target.host}.") from exc
        finally:
            self._close(connection)
        return SSHResult(command="\n".join(body), output=transcript,
                         duration_ms=_elapsed_ms(started))

    def run_exec(self, commands: list[str], allow_confirm: bool = False) -> SSHResult:
        raise SSHCommandError(
            "PAN-OS changes are configuration only (set/delete, then commit); "
            "operational EXEC batches are not supported."
        )

    def _revert(self, connection) -> None:
        """Drop the uncommitted candidate so a failed change leaves no trace."""
        try:
            if not connection.check_config_mode():
                connection.config_mode()
            connection.send_command(
                "revert config", expect_string=r"#", read_timeout=self.command_timeout
            )
            connection.exit_config_mode()
        except Exception:
            logger.warning("Could not revert the PAN-OS candidate configuration on %s.",
                           self.target.host)

    def _command_error(self, exc, message: str):
        from netmiko.exceptions import ReadTimeout

        if isinstance(exc, (ReadTimeout, TimeoutError)):
            return SSHTimeoutError(
                f"Command timed out on {self.target.host} after {self.command_timeout}s."
            )
        return SSHCommandError(message)

    def close(self) -> None:
        """Kept for interface symmetry: sessions are already short-lived."""

    def __repr__(self) -> str:
        return f"<PanosClient {self.target}>"


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
