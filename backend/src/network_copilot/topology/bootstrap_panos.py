"""Console bootstrap for Palo Alto PAN-OS firewalls (PA-VM in PNETLab).

The steps a person would type on a fresh PA-VM console:

1. wait for the ``login:`` prompt - a PA-VM takes 10-15 minutes to boot, and
   even after the prompt appears the CLI may say it is still loading;
2. log in with the factory password (``admin``/``admin``) and answer the
   forced first-login password change with the saved SSH password;
3. ``configure``, give the management address - either the dedicated ``mgmt``
   port (``deviceconfig system``) or a dataplane port such as ``ethernet1/3``
   with an interface-management-profile that allows ssh/https/ping - create
   the saved user, then ``commit``.

One HTTP request never waits for the whole boot: each call waits at most
``BOOTSTRAP_PANOS_POLL_SECONDS`` and reports ``waiting`` when the firewall is
not ready yet, so the page calls again until ``BOOTSTRAP_PANOS_BOOT_SECONDS``.
Nothing is kept between calls, which works with any number of workers.
"""

import ipaddress
import re
import time

from flask import current_app

from ..integrations.console import ConsoleError

MGMT = "mgmt"
PROFILE = "AITOOL-MGMT"
ZONE = "AITOOL-MGMT"
ROUTER = "default"
SECRET_MASK = "******"

LOGIN = re.compile(r"login:\s*\Z", re.I)
PASSWORD = re.compile(r"(?<!new )(?<!old )(?<!confirm )password\s*:\s*\Z", re.I)
OLD_PASSWORD = re.compile(r"(?:enter )?old password\s*:\s*\Z", re.I)
NEW_PASSWORD = re.compile(r"(?:enter )?new password\s*:\s*\Z", re.I)
CONFIRM_PASSWORD = re.compile(r"confirm (?:new )?password\s*:\s*\Z", re.I)
ENTER_PASSWORD = re.compile(r"enter password\s*:\s*\Z", re.I)
INCORRECT = re.compile(r"login incorrect|authentication failed|invalid (?:username|password)", re.I)
PROMPT = re.compile(r"(?:^|[\r\n])[\w.\-]+@[\w.\-()]+([>#])\s*\Z")
NOT_READY = re.compile(
    r"still in the process of loading|system initializing|please wait|not ready|"
    r"management server is not running",
    re.I,
)
CONFIG_ERROR = re.compile(
    r"(?im)^\s*(?:invalid syntax|unknown command|server error|validation error|error:)"
)
COMMIT_OK = re.compile(r"configuration committed successfully", re.I)


class PanosBooting(Exception):
    """The firewall is not ready yet; the caller should try again shortly."""


def panos_interface(label: str | None) -> str | None:
    """A PNETLab / user spelling -> the PAN-OS interface name.

    "mgmt", "management", "eth0" -> "mgmt"; "eth1/3", "e1/3", "Ethernet1/3"
    -> "ethernet1/3"; a bare "eth3" (PNETLab's PA-VM port 3) -> "ethernet1/3".
    """
    text = "".join((label or "").split()).lower()
    if text in ("mgmt", "management", "mgt", "eth0", "e0"):
        return MGMT
    match = re.fullmatch(r"(?:ethernet|eth|e)(\d+)/(\d+)", text)
    if match:
        return f"ethernet{int(match.group(1))}/{int(match.group(2))}"
    match = re.fullmatch(r"(?:eth|e)(\d+)", text)
    if match and int(match.group(1)) > 0:
        return f"ethernet1/{int(match.group(1))}"
    return None


def build_panos_commands(device, interface: str, project, gateway: str | None, credential,
                         redact: bool = True) -> list[str]:
    """Configure-mode lines (shown in the preview; ``commit`` follows them).

    The password is never part of a ``set`` line: ``set mgmt-config users
    <user> password`` prompts for it, so the preview shows the prompt step.
    """
    network = ipaddress.ip_network(project.management_network, strict=False)
    lines = [f"set deviceconfig system hostname {device.hostname}"]
    if interface == MGMT:
        lines += [
            f"set deviceconfig system ip-address {device.management_ip} netmask {network.netmask}",
        ]
        if gateway:
            lines.append(f"set deviceconfig system default-gateway {gateway}")
    else:
        lines += [
            f"set network profiles interface-management-profile {PROFILE} ssh yes https yes ping yes",
            f"set network interface ethernet {interface} layer3 ip {device.management_ip}/{network.prefixlen}",
            f"set network interface ethernet {interface} layer3 interface-management-profile {PROFILE}",
            f"set network virtual-router {ROUTER} interface {interface}",
            f"set zone {ZONE} network layer3 {interface}",
            f"set import network interface {interface}",
        ]
        if gateway:
            lines.append(
                f"set network virtual-router {ROUTER} routing-table ip static-route AITOOL-DEFAULT "
                f"destination 0.0.0.0/0 nexthop ip-address {gateway}"
            )
    if credential.username != "admin":
        lines.append(f"set mgmt-config users {credential.username} permissions role-based superuser yes")
    secret = SECRET_MASK if redact else credential.password
    lines.append(f"set mgmt-config users {credential.username} password  (prompt: {secret})")
    return lines


# Lines that older PAN-OS releases may not know; a rejection is not fatal.
_OPTIONAL = ("set import network interface",)


class PanosConsole:
    """Drive a PA-VM console: wait for boot, log in, configure, commit."""

    def __init__(self, console, credential, default_password: str, poll_seconds: float,
                 timeout: float = 30, commit_timeout: float = 600):
        self.console = console
        self.credential = credential
        self.default_password = default_password
        self.poll_seconds = poll_seconds
        self.timeout = timeout
        self.commit_timeout = commit_timeout
        self.password_changed = False
        self.errors: list[str] = []

    def login(self) -> None:
        """Reach the operational prompt, or raise PanosBooting / ConsoleError."""
        attempts = [("admin", self.default_password), (self.credential.username, self.credential.password)]
        if self.credential.username != "admin":
            attempts.append(("admin", self.credential.password))
        attempt = 0
        deadline = time.monotonic() + self.poll_seconds
        # "Login incorrect" arrives together with the next "login:" prompt, so it
        # is looked for first or the failed attempt would simply be repeated.
        patterns = [INCORRECT, LOGIN, OLD_PASSWORD, NEW_PASSWORD, CONFIRM_PASSWORD, PASSWORD, PROMPT]
        self.console.send("\r")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PanosBooting()
            index, text = self.console.expect(patterns, min(remaining, 15))
            if index == -1:
                self.console.send("\r")
            elif index == 0:
                attempt += 1
            elif index == 1:
                if attempt >= len(attempts):
                    raise ConsoleError("The firewall rejected the default and the saved password.")
                self.console.send(attempts[attempt][0] + "\r")
            elif index == 2:
                self.console.send(attempts[attempt][1] + "\r")
            elif index in (3, 4):
                self.password_changed = True
                self.console.send(self.credential.password + "\r")
            elif index == 5:
                self.console.send(attempts[attempt][1] + "\r")
            else:
                if PROMPT.search(text).group(1) == "#":
                    self.console.send("exit\r")
                    continue
                return

    def run(self, command: str, timeout: float | None = None) -> str:
        self.console.send(command + "\r")
        index, text = self.console.expect([PROMPT], timeout or self.timeout)
        if index == -1:
            raise ConsoleError(f"No prompt after {command.split()[0]!r} on the firewall console.")
        return text

    def ready(self) -> None:
        """The CLI answers; raise PanosBooting while PAN-OS is still loading."""
        self.run("set cli pager off")
        info = self.run("show system info", timeout=60)
        if NOT_READY.search(info) or "operational-mode" not in info.lower():
            raise PanosBooting()

    def set_password(self, username: str) -> None:
        self.console.send(f"set mgmt-config users {username} password\r")
        for pattern in (ENTER_PASSWORD, CONFIRM_PASSWORD):
            index, _ = self.console.expect([pattern, PROMPT], self.timeout)
            if index != 0:
                raise ConsoleError("The firewall did not ask for the new user's password.")
            self.console.send(self.credential.password + "\r")
        index, text = self.console.expect([PROMPT], self.timeout)
        if index == -1 or CONFIG_ERROR.search(text):
            raise ConsoleError("The firewall rejected the new user's password.")

    def configure(self, lines: list[str]) -> None:
        """Enter configure mode, apply the lines and commit; revert on any failure.

        Rejected lines are kept in ``self.errors`` (they never hold a secret:
        the password goes through its own prompt).
        """
        errors = self.errors
        self.run("configure")
        try:
            for line in lines:
                if line.startswith("set mgmt-config users") and " password" in line:
                    self.set_password(self.credential.username)
                    continue
                output = self.run(line)
                if CONFIG_ERROR.search(output) and not line.startswith(_OPTIONAL):
                    errors.append(f"{line}: {CONFIG_ERROR.search(output).group(0).strip()}")
            if errors:
                raise ConsoleError("The firewall rejected some configuration lines.")
            output = self.run("commit", timeout=self.commit_timeout)
            if not COMMIT_OK.search(output):
                raise ConsoleError("PAN-OS commit failed; nothing was applied.")
        except ConsoleError:
            try:
                self.run("revert config", timeout=60)
            except ConsoleError:
                pass
            raise
        finally:
            try:
                self.run("exit")
            except ConsoleError:
                pass


def bootstrap_panos(entry: dict, open_console, verify) -> dict:
    """Bootstrap one PA-VM; returns the result record for the page."""
    device, credential, host, port, interface, gateway = entry["_run"]
    project, default_password = entry["_project"], entry["_default_password"]
    result = {"device_id": device.id, "hostname": device.hostname, "console": entry["console"]}
    config = current_app.config
    console = open_console(host, port)
    try:
        panos = PanosConsole(
            console, credential, default_password,
            poll_seconds=float(config.get("BOOTSTRAP_PANOS_POLL_SECONDS", 90)),
            commit_timeout=float(config.get("PANOS_COMMIT_TIMEOUT", 600)),
        )
        try:
            panos.login()
            panos.ready()
        except PanosBooting:
            result.update(status="waiting", message="PAN-OS is still booting; trying again shortly.",
                          boot_seconds=int(config.get("BOOTSTRAP_PANOS_BOOT_SECONDS", 900)))
            return result
        lines = build_panos_commands(device, interface, project, gateway, credential, redact=False)
        try:
            panos.configure(lines)
        except ConsoleError as exc:
            result.update(status="error", message=exc.message)
            if panos.errors:
                result["errors"] = panos.errors
            return result
    except ConsoleError as exc:
        result.update(status="error", message=exc.message)
        return result
    finally:
        console.close()
    if panos.password_changed:
        result["password_changed"] = True
    return verify(device, result, panos=True)
