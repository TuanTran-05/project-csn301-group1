"""A minimal telnet client for PNETLab node consoles.

PNETLab serves every running node's serial console as a telnet port on the
lab host. This is the only way into a node whose configuration is still
blank (no management address, no SSH), so it is used once per device to
bootstrap SSH; everything afterwards goes over SSH as usual.

``telnetlib`` left the standard library in Python 3.13, so the little that is
needed (option refusal and stripping of IAC sequences) is done here.
"""

import re
import socket
import time

from ..errors import ValidationError

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA = 1, 3
MAX_BUFFER = 256 * 1024


class ConsoleError(ValidationError):
    """The console could not be used; the message is safe to show the user."""


def strip_telnet(data: bytes, pending: bytes = b"") -> tuple[bytes, bytes, bytes]:
    """Split raw bytes into (text, replies to send, incomplete tail).

    The server's WILL ECHO / WILL SGA are accepted (a normal interactive
    console); every other option is refused. Subnegotiations are dropped.
    """
    data = pending + data
    text, replies = bytearray(), bytearray()
    i = 0
    while i < len(data):
        byte = data[i]
        if byte != IAC:
            text.append(byte)
            i += 1
            continue
        if i + 1 >= len(data):
            return bytes(text), bytes(replies), data[i:]
        command = data[i + 1]
        if command == IAC:
            text.append(IAC)
            i += 2
        elif command in (DO, DONT, WILL, WONT):
            if i + 2 >= len(data):
                return bytes(text), bytes(replies), data[i:]
            option = data[i + 2]
            if command == WILL:
                replies += bytes((IAC, DO if option in (ECHO, SGA) else DONT, option))
            elif command == DO:
                replies += bytes((IAC, WONT, option))
            i += 3
        elif command == SB:
            end = data.find(bytes((IAC, SE)), i + 2)
            if end < 0:
                return bytes(text), bytes(replies), data[i:]
            i = end + 2
        else:
            i += 2
    return bytes(text), bytes(replies), b""


class TelnetConsole:
    """send() text and expect() regular expressions on a telnet console."""

    def __init__(self, host: str, port: int, timeout: float = 10):
        self.host, self.port = host, port
        self._timeout = timeout
        self._buffer = ""
        self._pending = b""
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise ConsoleError(
                f"Cannot open the console {host}:{port}: {type(exc).__name__}."
            ) from exc

    def send(self, text: str) -> None:
        try:
            self._sock.sendall(text.replace("\xff", "").encode("utf-8", "replace"))
        except OSError as exc:
            raise ConsoleError(f"The console {self.host}:{self.port} closed.") from exc

    def expect(self, patterns: list[re.Pattern], timeout: float) -> tuple[int, str]:
        """Read until one pattern matches; return (its index, text read so far).

        The returned text is consumed. On timeout the index is -1 and the text
        read so far is returned (and consumed) as well.
        """
        deadline = time.monotonic() + timeout
        while True:
            for index, pattern in enumerate(patterns):
                match = pattern.search(self._buffer)
                if match:
                    text, self._buffer = self._buffer[: match.end()], self._buffer[match.end():]
                    return index, text
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                text, self._buffer = self._buffer, ""
                return -1, text
            self._sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                raise ConsoleError(f"The console {self.host}:{self.port} closed.") from exc
            if not chunk:
                raise ConsoleError(f"The console {self.host}:{self.port} closed.")
            text, replies, self._pending = strip_telnet(chunk, self._pending)
            if replies:
                self._sock.sendall(replies)
            self._buffer = (self._buffer + text.decode("utf-8", "replace"))[-MAX_BUFFER:]

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:  # pragma: no cover
            pass


def open_console(host: str, port: int):
    """The real console, or the test factory when one is installed."""
    from flask import current_app

    factory = current_app.config.get("CONSOLE_CLIENT_FACTORY")
    if factory is not None:
        return factory(host, port)
    return TelnetConsole(host, port, timeout=current_app.config.get("SSH_CONNECT_TIMEOUT", 10))
