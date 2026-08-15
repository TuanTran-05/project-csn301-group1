"""User-facing wording for SSH failures.

The SSH layer words its errors for an operator reading a log: they name
the transport target, "g1lab@10.10.10.3:22", because that is what you
need to reproduce the failure by hand. In chat that is the wrong
audience. The reader there wants to know which *device* is unhappy and
what to go and check, and has no use for the SSH account name.

So this module restates a failure for that reader. The original text is
not lost - callers record it in the audit trail and the server log
before rewriting - it simply stops being the thing a viewer sees.

The mapping is per exception class on purpose. A dead device, a slow
device and a wrong password send the reader to three different places,
and a single "device is offline" would be actively misleading for the
last of them.
"""

from .exceptions import (
    SSHAuthenticationError,
    SSHCommandError,
    SSHConnectionError,
    SSHError,
    SSHTimeoutError,
)

_CONNECTION = (
    "Không kết nối được tới {hostname}. Thiết bị có thể đang tắt hoặc mất "
    "kết nối mạng - vui lòng kiểm tra thiết bị rồi thử lại."
)

_TIMEOUT = (
    "{hostname} không phản hồi kịp thời gian chờ. Thiết bị có thể đang quá "
    "tải hoặc đường mạng không ổn định - vui lòng kiểm tra rồi thử lại."
)

# Deliberately says nothing about the device being offline: it answered.
_AUTHENTICATION = (
    "Đăng nhập vào {hostname} thất bại. Thiết bị vẫn phản hồi nhưng tài "
    "khoản SSH không được chấp nhận - vui lòng kiểm tra lại thông tin đăng "
    "nhập đã lưu cho thiết bị này."
)

# The session opened, so the detail describes what the device did rather
# than how we reached it, and is worth keeping.
_COMMAND = (
    "{hostname} đã kết nối được nhưng không thực hiện được lệnh. "
    "Chi tiết: {detail}"
)

_UNKNOWN = (
    "Có lỗi khi làm việc với {hostname} qua SSH - vui lòng kiểm tra thiết "
    "bị rồi thử lại."
)

_TEMPLATES = {
    SSHConnectionError: _CONNECTION,
    SSHTimeoutError: _TIMEOUT,
    SSHAuthenticationError: _AUTHENTICATION,
    SSHCommandError: _COMMAND,
}


def friendly_error(exc: SSHError, hostname: str) -> str:
    """Restate an SSH failure for whoever is reading the chat.

    Matches on the exact class rather than isinstance so that a future
    subclass falls through to the generic wording instead of silently
    inheriting a message that may not describe it.
    """
    template = _TEMPLATES.get(type(exc), _UNKNOWN)
    return template.format(hostname=hostname, detail=exc.message)
