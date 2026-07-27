from ..errors import DeviceConnectionError


class SSHError(DeviceConnectionError):
    """Base class for every SSH transport failure."""

    error = "ssh_error"


class SSHConnectionError(SSHError):
    error = "ssh_connection_error"


class SSHTimeoutError(SSHError):
    error = "ssh_timeout"


class SSHAuthenticationError(SSHError):
    error = "ssh_authentication_error"


class SSHCommandError(SSHError):
    error = "ssh_command_error"
