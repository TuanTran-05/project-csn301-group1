"""In-memory stand-in for network_copilot.ssh.client.SSHClient.

Records every command it is asked to run so tests can assert on ordering, and
raises the real SSH exceptions when configured to fail.
"""

from network_copilot.ssh.exceptions import SSHConnectionError, SSHTimeoutError
from network_copilot.ssh.types import SSHResult


class FakeSSHClient:
    def __init__(
        self,
        responses: dict[str, str] | None = None,
        default_output: str = "",
        reachable: bool = True,
        fail_with: Exception | None = None,
        config_output: str = "",
    ):
        self.responses = responses or {}
        self.default_output = default_output
        self.reachable = reachable
        self.fail_with = fail_with
        self.config_output = config_output

        # Call log, in execution order.
        self.show_commands: list[str] = []
        self.config_batches: list[list[str]] = []
        self.calls: list[tuple[str, object]] = []

    # -- helpers -----------------------------------------------------------
    def _raise_if_configured(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def _output_for(self, command: str) -> str:
        if command in self.responses:
            return self.responses[command]
        for key, value in self.responses.items():
            if command.startswith(key):
                return value
        return self.default_output

    # -- SSHClient interface ----------------------------------------------
    def test_connection(self) -> bool:
        self.calls.append(("test_connection", None))
        if self.fail_with is not None:
            raise self.fail_with
        return self.reachable

    def run_show(self, command: str) -> SSHResult:
        self.calls.append(("run_show", command))
        self._raise_if_configured()
        self.show_commands.append(command)
        return SSHResult(
            command=command, output=self._output_for(command), duration_ms=1
        )

    def run_config(self, commands: list[str]) -> SSHResult:
        self.calls.append(("run_config", list(commands)))
        self._raise_if_configured()
        self.config_batches.append(list(commands))
        return SSHResult(
            command="\n".join(commands), output=self.config_output, duration_ms=1
        )

    def close(self) -> None:
        self.calls.append(("close", None))


def unreachable_client(message: str = "Connection refused") -> FakeSSHClient:
    return FakeSSHClient(fail_with=SSHConnectionError(message))


def timing_out_client(message: str = "Timed out after 10s") -> FakeSSHClient:
    return FakeSSHClient(fail_with=SSHTimeoutError(message))
