from dataclasses import dataclass, field


@dataclass(frozen=True)
class SSHResult:
    command: str
    output: str
    duration_ms: int


@dataclass(frozen=True)
class SSHTarget:
    """Where and how to connect. Secrets are excluded from repr()."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    enable_secret: str | None = field(default=None, repr=False)
    device_type: str | None = None

    def __str__(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"
