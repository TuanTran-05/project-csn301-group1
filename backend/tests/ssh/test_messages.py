"""The user-facing wording for an SSH failure.

Each SSH failure sends the reader somewhere different: a dead device, a
slow device, or a wrong password. One blanket "device is offline" would
be wrong for the last of those, so the mapping is per exception class.
"""

import pytest

from network_copilot.ssh.exceptions import (
    SSHAuthenticationError,
    SSHCommandError,
    SSHConnectionError,
    SSHError,
    SSHTimeoutError,
)
from network_copilot.ssh.messages import friendly_error

# What the SSH layer actually raises today, credentials and all.
RAW_CONNECT = "Could not connect to g1lab@10.10.10.3:22."
RAW_AUTH = "Authentication failed for g1lab@10.10.10.3:22."
RAW_TIMEOUT = "Timed out connecting to g1lab@10.10.10.3:22 after 10s."


def test_a_dead_device_is_reported_as_offline():
    message = friendly_error(SSHConnectionError(RAW_CONNECT), "FW-01")

    assert "FW-01" in message
    assert "không kết nối được" in message.lower()
    assert "kiểm tra" in message.lower()


def test_a_timeout_says_the_device_did_not_answer_in_time():
    message = friendly_error(SSHTimeoutError(RAW_TIMEOUT), "FW-01")

    assert "FW-01" in message
    assert "không phản hồi" in message.lower()


def test_an_authentication_failure_is_not_reported_as_offline():
    """THE test. The device answered; the credentials were wrong. Saying
    "offline" here sends the reader to check the wrong thing entirely."""
    message = friendly_error(SSHAuthenticationError(RAW_AUTH), "FW-01")

    assert "đăng nhập" in message.lower()
    assert "tài khoản" in message.lower()
    assert "offline" not in message.lower()
    assert "không kết nối được" not in message.lower()


def test_a_command_failure_keeps_the_device_side_detail():
    """Unlike a connection failure, this text describes what the device did
    and carries no credential, so it is worth showing rather than replacing."""
    message = friendly_error(
        SSHCommandError("Cisco CLI rejected a command on 10.10.10.3."), "FW-01"
    )

    assert "FW-01" in message
    assert "Cisco CLI rejected a command" in message


def test_an_unknown_ssh_failure_still_gets_a_readable_message():
    message = friendly_error(SSHError(RAW_CONNECT), "FW-01")

    assert "FW-01" in message
    assert "@" not in message


@pytest.mark.parametrize(
    "exc",
    [
        SSHConnectionError(RAW_CONNECT),
        SSHTimeoutError(RAW_TIMEOUT),
        SSHAuthenticationError(RAW_AUTH),
        SSHError(RAW_CONNECT),
    ],
    ids=["connection", "timeout", "authentication", "unknown"],
)
def test_the_ssh_username_never_reaches_the_reader(exc):
    """The raw messages embed "username@host:port". A viewer reading chat
    has no use for the SSH account name, and it stays in the audit trail
    either way."""
    message = friendly_error(exc, "FW-01")

    assert "g1lab" not in message
    assert "@" not in message
