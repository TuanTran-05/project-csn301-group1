"""Console bootstrap for Palo Alto PAN-OS firewalls (PA-VM in PNETLab).

The steps a person would type on a fresh PA-VM console:

1. wait for the ``login:`` prompt - a PA-VM takes 10-15 minutes to boot, and
   even after the prompt appears the CLI may say it is still loading;
2. log in with the factory password (``admin``/``admin``) and answer the
   forced first-login password change with the saved SSH password;
3. ``configure`` and give the management address:
   * **mgmt** (the default when the node's mgmt/eth0 port is cabled to
     Management(Cloud0)): ``set deviceconfig system ip-address <ip> netmask
     <mask>`` (+ ``default-gateway``) - no zone, router or profile;
   * **dataplane** (optional, e.g. ``ethernet1/3``): an interface-management
     profile allowing ssh/https/ping, the address, the ``default`` virtual
     router and a zone;
4. keep using ``admin`` (default) or create the saved user, then ``commit``.

Every step is recorded (passwords masked) and logged to the service journal.
An *essential* step that fails (the address itself) stops before the commit
and reverts the candidate; a secondary step that fails (hostname, gateway,
the extra user) is reported and the commit goes ahead. Typing is throttled
and the CLI is put in scripting mode, as PAN-OS asks for pasted commands:
a serial console drops characters typed too fast, which shows up as
"Invalid syntax".

This runs inside a background job (topology/bootstrap_jobs.py), so the boot
wait and the commit never hold an HTTP request open.
"""

import ipaddress
import logging
import re
import time

from flask import current_app

from ..integrations.console import ConsoleError, VNC_MESSAGE, looks_like_vnc

logger = logging.getLogger(__name__)

MGMT = "mgmt"
PROFILE = "AITOOL-MGMT"
ZONE = "AITOOL-MGMT"
ROUTER = "default"
SECRET_MASK = "******"
ACCOUNT_MODES = ("admin", "create")

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
    r"(?im)^\s*(?:invalid syntax|unknown command|server error|validation error|error:).*$"
)
COMMIT_OK = re.compile(r"configuration committed successfully", re.I)


class PanosBooting(Exception):
    """The firewall is not ready yet."""


def panos_password_problem(password: str) -> str | None:
    """Why a password would be refused by PAN-OS' default complexity, or None.

    At least 8 characters, an upper-case and a lower-case letter, and a digit
    or a special character.
    """
    password = password or ""
    if len(password) < 8:
        return "at least 8 characters"
    if not re.search(r"[A-Z]", password):
        return "an upper-case letter"
    if not re.search(r"[a-z]", password):
        return "a lower-case letter"
    if not re.search(r"[0-9]|[^A-Za-z0-9]", password):
        return "a digit or a special character"
    return None


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


def preferred_interface(found: list[str]) -> str:
    """mgmt wins when it is cabled to the cloud; otherwise the first port."""
    return MGMT if MGMT in found else found[0]


# -- the plan ---------------------------------------------------------------------
# Each step: (kind, text, essential). kind "set" is typed as is; "password" is
# the interactive ``set mgmt-config users <user> password``.


def plan_steps(device, interface: str, project, gateway: str | None, credential,
               account: str) -> list[tuple[str, str, bool]]:
    network = ipaddress.ip_network(project.management_network, strict=False)
    steps: list[tuple[str, str, bool]] = [
        ("set", f"set deviceconfig system hostname {device.hostname}", False),
    ]
    if interface == MGMT:
        steps.append(("set", f"set deviceconfig system ip-address {device.management_ip} "
                             f"netmask {network.netmask}", True))
        if gateway:
            steps.append(("set", f"set deviceconfig system default-gateway {gateway}", False))
    else:
        steps += [
            ("set", f"set network profiles interface-management-profile {PROFILE} "
                    "ssh yes https yes ping yes", True),
            ("set", f"set network interface ethernet {interface} layer3 ip "
                    f"{device.management_ip}/{network.prefixlen}", True),
            ("set", f"set network interface ethernet {interface} layer3 "
                    f"interface-management-profile {PROFILE}", True),
            ("set", f"set network virtual-router {ROUTER} interface {interface}", True),
            ("set", f"set zone {ZONE} network layer3 {interface}", True),
            ("set", f"set import network interface {interface}", False),
        ]
        if gateway:
            steps.append(("set", f"set network virtual-router {ROUTER} routing-table ip static-route "
                                 f"AITOOL-DEFAULT destination 0.0.0.0/0 nexthop ip-address {gateway}", False))
    if account == "create" and credential.username != "admin":
        # The documented order: the password creates the account, then its role.
        steps += [
            ("password", credential.username, False),
            ("set", f"set mgmt-config users {credential.username} permissions role-based superuser yes", False),
        ]
    return steps


def build_panos_commands(device, interface: str, project, gateway: str | None, credential,
                         account: str = "admin") -> list[str]:
    """The preview: configure, the steps, commit (no secret in it)."""
    lines = ["configure"]
    for kind, text, _essential in plan_steps(device, interface, project, gateway, credential, account):
        if kind == "password":
            lines.append(f"set mgmt-config users {text} password   (mật khẩu SSH đã lưu, nhập khi được hỏi)")
        else:
            lines.append(text)
    if account == "admin":
        lines.append("# SSH bằng admin / mật khẩu SSH đã lưu (đặt ở bước đổi mật khẩu lần đầu)")
    lines.append("commit")
    return lines


# -- the console session --------------------------------------------------------------


class PanosConsole:
    """Drive a PA-VM console: wait for boot, log in, configure, commit."""

    def __init__(self, console, credential, default_password: str, *, timeout: float = 30,
                 commit_timeout: float = 600, type_delay: float = 0.03, record=None):
        self.console = console
        self.credential = credential
        self.default_password = default_password
        self.timeout = timeout
        self.commit_timeout = commit_timeout
        self.type_delay = type_delay
        self.record = record or (lambda step: None)
        self.password_changed = False
        self.logged_in_as: tuple[str, str] | None = None
        self.steps: list[dict] = []

    # -- plumbing ------------------------------------------------------------
    def mask(self, text: str) -> str:
        # The factory "admin" password is public (and also the user name), so
        # only a default password someone changed counts as a secret.
        text = text or ""
        secrets = {self.credential.password}
        if self.default_password != "admin":
            secrets.add(self.default_password)
        for secret in secrets:
            if secret:
                text = text.replace(secret, SECRET_MASK)
        return text

    def type(self, text: str) -> None:
        """Send a line a few characters at a time: a serial console drops a burst."""
        if not self.type_delay:
            self.console.send(text + "\r")
            return
        for start in range(0, len(text), 8):
            self.console.send(text[start:start + 8])
            time.sleep(self.type_delay)
        self.console.send("\r")

    def expect(self, patterns, timeout):
        index, text = self.console.expect(patterns, timeout)
        if looks_like_vnc(text):
            raise ConsoleError(VNC_MESSAGE)
        return index, text

    def step(self, command: str, ok: bool, output: str = "", essential: bool = False,
             note: str | None = None) -> dict:
        lines = [line.strip() for line in self.mask(output).splitlines() if line.strip()]
        entry = {"command": self.mask(command), "ok": ok, "essential": essential,
                 "output": "\n".join(lines[-6:])}
        if note:
            entry["note"] = note
        self.steps.append(entry)
        self.record(entry)
        level = logging.INFO if ok else logging.WARNING
        logger.log(level, "PAN-OS bootstrap step %s: %s%s", "ok" if ok else "FAILED",
                   entry["command"], "" if ok else f" -> {entry['output']!r}")
        return entry

    def run(self, command: str, timeout: float | None = None) -> str:
        self.type(command)
        index, text = self.expect([PROMPT], timeout or self.timeout)
        if index == -1:
            raise ConsoleError(f"No prompt after {command.split()[0]!r} on the firewall console.")
        return text

    # -- phases ------------------------------------------------------------------
    def login(self, deadline: float) -> None:
        """Reach the operational prompt; PanosBooting when the deadline passes."""
        attempts = [("admin", self.default_password), (self.credential.username, self.credential.password)]
        if self.credential.username != "admin":
            attempts.append(("admin", self.credential.password))
        attempt = 0
        # "Login incorrect" arrives together with the next "login:" prompt, so it
        # is looked for first or the failed attempt would simply be repeated.
        patterns = [INCORRECT, LOGIN, OLD_PASSWORD, NEW_PASSWORD, CONFIRM_PASSWORD, PASSWORD, PROMPT]
        self.console.send("\r")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PanosBooting()
            index, text = self.expect(patterns, min(remaining, 15))
            if index == -1:
                self.console.send("\r")
            elif index == 0:
                attempt += 1
            elif index == 1:
                if attempt >= len(attempts):
                    raise ConsoleError("The firewall rejected the default and the saved password.")
                self.console.send(attempts[attempt][0] + "\r")
            elif index in (2, 5):
                self.console.send(attempts[attempt][1] + "\r")
            elif index in (3, 4):
                self.password_changed = True
                self.console.send(self.credential.password + "\r")
            else:
                if PROMPT.search(text).group(1) == "#":
                    self.console.send("exit\r")
                    continue
                user, password = attempts[min(attempt, len(attempts) - 1)]
                if self.password_changed:
                    password = self.credential.password
                self.logged_in_as = (user, password)
                self.step(f"login as {user}", True,
                          note="first-login password changed" if self.password_changed else None)
                return

    def ready(self) -> None:
        """The CLI answers; PanosBooting while PAN-OS is still loading."""
        self.run("set cli scripting-mode on")
        self.run("set cli pager off")
        info = self.run("show system info", timeout=60)
        if NOT_READY.search(info) or "operational-mode" not in info.lower():
            raise PanosBooting()

    def set_password(self, username: str) -> tuple[bool, str]:
        self.type(f"set mgmt-config users {username} password")
        transcript = ""
        for pattern in (ENTER_PASSWORD, CONFIRM_PASSWORD):
            index, text = self.expect([pattern, PROMPT], self.timeout)
            transcript += text
            if index != 0:
                return False, transcript
            self.console.send(self.credential.password + "\r")
        index, text = self.expect([PROMPT], self.timeout)
        transcript += text
        return index != -1 and not CONFIG_ERROR.search(text), transcript

    def configure(self, steps: list[tuple[str, str, bool]]) -> None:
        """Apply the steps and commit. Raises ConsoleError when nothing was committed."""
        self.run("configure")
        committed = False
        try:
            failed_essential = False
            for kind, text, essential in steps:
                if kind == "password":
                    ok, output = self.set_password(text)
                    self.step(f"set mgmt-config users {text} password", ok, output, essential)
                else:
                    output = self.run(text)
                    ok = not CONFIG_ERROR.search(output)
                    self.step(text, ok, output, essential)
                if not ok and essential:
                    failed_essential = True
                    break
            if failed_essential:
                raise ConsoleError("An essential step was rejected; nothing was committed.")
            output = self.run("commit", timeout=self.commit_timeout)
            committed = bool(COMMIT_OK.search(output))
            self.step("commit", committed, output, essential=True)
            if not committed:
                raise ConsoleError("PAN-OS commit failed; nothing was applied.")
        finally:
            if not committed:
                # Only a failed essential step or commit drops the candidate.
                try:
                    self.run("revert config", timeout=60)
                    self.step("revert config", True)
                except ConsoleError:
                    pass
            try:
                self.run("exit")
            except ConsoleError:
                pass


def bootstrap_panos(entry: dict, open_console, verify, progress=None) -> dict:
    """Bootstrap one PA-VM inside a background job; returns its result record."""
    device, credential, host, port, interface, gateway = entry["_run"]
    project, default_password = entry["_project"], entry["_default_password"]
    account = entry.get("_account", "admin")
    progress = progress or (lambda **kw: None)
    result = {"device_id": device.id, "hostname": device.hostname, "console": entry["console"],
              "interface": interface, "account": account}
    config = current_app.config
    boot_seconds = float(config.get("BOOTSTRAP_PANOS_BOOT_SECONDS", 900))
    retry_seconds = float(config.get("BOOTSTRAP_PANOS_RETRY_SECONDS", 10))
    deadline = time.monotonic() + boot_seconds
    console = open_console(host, port)
    panos = PanosConsole(
        console, credential, default_password,
        commit_timeout=float(config.get("PANOS_COMMIT_TIMEOUT", 600)),
        type_delay=float(config.get("BOOTSTRAP_TYPE_DELAY", 0.03)),
        record=lambda step: progress(step=step),
    )
    try:
        progress(phase="booting", message="Đang chờ PAN-OS khởi động và đăng nhập…")
        while True:
            try:
                if panos.logged_in_as is None:
                    panos.login(deadline)
                panos.ready()
                break
            except PanosBooting:
                if time.monotonic() >= deadline:
                    result.update(status="error", steps=panos.steps, message=(
                        f"PAN-OS chưa khởi động xong sau {int(boot_seconds // 60)} phút."))
                    return result
                progress(phase="booting", message="PAN-OS vẫn đang khởi động…")
                time.sleep(retry_seconds)
        progress(phase="configuring", message="Đang cấu hình và commit…")
        steps = plan_steps(device, interface, project, gateway, credential, account)
        if account == "admin" and panos.logged_in_as != ("admin", credential.password):
            # admin still has another password (no forced change on this release,
            # or the console was entered with the saved user): give admin the
            # saved one, since SSH will use it.
            steps.append(("password", "admin", True))
        try:
            panos.configure(steps)
        except ConsoleError as exc:
            result.update(status="error", message=exc.message, steps=panos.steps,
                          errors=[s["command"] for s in panos.steps if not s["ok"]])
            return result
    except ConsoleError as exc:
        result.update(status="error", message=exc.message, steps=panos.steps)
        return result
    finally:
        console.close()
    result["steps"] = panos.steps
    failed = [s["command"] for s in panos.steps if not s["ok"]]
    if failed:
        result["warnings"] = failed
    if panos.password_changed:
        result["password_changed"] = True
    progress(phase="verifying", message="Đang kiểm tra SSH…")
    return verify(device, result, panos=True, account=account, credential=credential)
