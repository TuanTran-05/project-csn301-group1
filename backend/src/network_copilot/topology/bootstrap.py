"""Bootstrap SSH on PNETLab devices through their serial console.

A node freshly added to a PNETLab lab has a blank configuration: no
management address, no login, no SSH. Every other part of the backend talks
to devices over SSH, so the import gives each device a management IP and a
credential *in the database*, and this module puts the same values on the
device itself, through the telnet console PNETLab exposes on the lab host.

The management interface is the one the lab cables to a cloud network
(``pnet0`` = "Management(Cloud0)", bridged to the PNETLab host's LAN), unless
the operator names one. Nothing is done to the lab itself: nodes must already
be running, and the cloud cable must already exist.

A device that already carries another login or another address on that
interface is left alone unless the operator asks to overwrite it.
"""

import ipaddress
import re
import time
from urllib.parse import urlparse

from flask import current_app
from pydantic import BaseModel, ConfigDict, Field

from ..credentials.service import get_device_credential
from ..devices import service as device_service
from ..errors import AppError, NotFoundError, ValidationError
from ..integrations.console import ConsoleError, open_console
from ..integrations.pnetlab import build_client
from ..projects.service import validate
from .bootstrap_panos import bootstrap_panos, build_panos_commands, panos_interface
from .interfaces import normalize_label
from .pnetlab_import import LabSource, hostname_for

MAX_DEVICES = 50
DOMAIN_NAME = "netai.lab"
SECRET_MASK = "******"

# -- request ---------------------------------------------------------------------


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: int
    node_id: str | None = Field(default=None, max_length=32)
    interface: str | None = Field(default=None, max_length=64)


class BootstrapRequest(LabSource):
    devices: list[Target] | None = Field(default=None, max_length=MAX_DEVICES)
    gateway: str | None = Field(default=None, max_length=45)
    overwrite: bool = False
    # PAN-OS factory login (admin/admin); used once, never stored.
    default_password: str = Field(default="admin", min_length=1, max_length=128)


# -- planning --------------------------------------------------------------------


def cloud_interfaces(node_id: str, topology: list, networks: dict,
                     convert=normalize_label) -> list[str]:
    """Interfaces of a node that the lab cables to a cloud (pnetN) network.

    Cloud0 (pnet0) comes first: it is the one PNETLab bridges to its LAN.
    ``convert`` turns PNETLab's label into the device's own interface name.
    """
    found = []
    for entry in topology:
        if not isinstance(entry, dict):
            continue
        for side, other in (("source", "destination"), ("destination", "source")):
            if entry.get(f"{side}_type") != "node" or entry.get(f"{other}_type") != "network":
                continue
            if str(entry.get(side, "")).removeprefix("node") != str(node_id):
                continue
            network_id = str(entry.get(other, "")).removeprefix("network")
            network = networks.get(network_id) or {}
            kind = str(network.get("type", "")).lower()
            label = convert(entry.get(f"{side}_label"))
            if kind.startswith("pnet") and label:
                found.append((kind, label))
    return [label for _, label in sorted(found)]


def console_port(node: dict) -> int | None:
    """The telnet port of a node's console: its ``telnet://`` url, else ``port``.

    PNETLab's session API reports an HTML5 link in ``url`` when the account
    uses the web console, but still carries the telnet port in ``port``.
    """
    parsed = urlparse(str(node.get("url") or ""))
    if parsed.scheme == "telnet":
        try:
            if parsed.port:
                return parsed.port
        except ValueError:
            pass
    try:
        port = int(node.get("port") or 0)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _gateway(project, raw: str | None) -> str | None:
    if not raw:
        return None
    network = ipaddress.ip_network(project.management_network, strict=False)
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise ValidationError("The gateway must be an IP address.", {"gateway": ["Invalid IP."]}) from exc
    if address not in network:
        raise ValidationError(
            f"The gateway must be inside the project's management network {network}.",
            {"gateway": ["Outside the management network."]},
        )
    return str(address)


def build_commands(device, interface: str, project, gateway: str | None, credential,
                   redact: bool = True) -> list[str]:
    """IOS configuration that gives the device its management address and SSH."""
    network = ipaddress.ip_network(project.management_network, strict=False)
    address = f"{device.management_ip} {network.netmask}"
    secret = SECRET_MASK if redact else credential.password
    commands = [f"hostname {device.hostname}", "no ip domain-lookup", f"ip domain-name {DOMAIN_NAME}"]
    if device.kind == "switch":
        commands += [
            f"interface {interface}", "switchport mode access", "switchport access vlan 1",
            "no shutdown", "exit",
            "interface Vlan1", f"ip address {address}", "no shutdown", "exit",
        ]
        if gateway:
            commands.append(f"ip default-gateway {gateway}")
    else:
        commands.append(f"interface {interface}")
        if device.kind == "l3_switch":
            commands.append("no switchport")
        commands += [f"ip address {address}", "no shutdown", "exit"]
        if gateway:
            commands.append(f"ip route 0.0.0.0 0.0.0.0 {gateway}")
    commands.append(f"username {credential.username} privilege 15 secret {secret}")
    if credential.enable_secret:
        commands.append(f"enable secret {SECRET_MASK if redact else credential.enable_secret}")
    commands += [
        "crypto key generate rsa modulus 2048",
        "ip ssh version 2",
        "line vty 0 4", "login local", "transport input ssh", "exit",
    ]
    return commands


def address_interface(device, interface: str) -> str:
    """Where the management address lives (an L2 switch keeps it on Vlan1)."""
    return "Vlan1" if device.kind == "switch" else interface


# Typed into the IOS CLI: "?" opens help mid-line and a line break would start a
# new command, so such a login cannot be set through the console.
_TYPABLE_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_TYPABLE_SECRET = re.compile(r"^[!-~]{1,128}$")


def console_typable(credential) -> bool:
    secrets = [credential.password] + ([credential.enable_secret] if credential.enable_secret else [])
    return bool(_TYPABLE_USER.match(credential.username)) and all(
        _TYPABLE_SECRET.match(s) and "?" not in s for s in secrets
    )


def _candidates(project, request: BootstrapRequest) -> list[tuple]:
    if request.devices is None:
        devices = [
            d for d in device_service.list_devices(project_id=project.id)
            if d.managed and d.environment == "pnetlab"
        ]
        return [(d, Target(device_id=d.id)) for d in devices][:MAX_DEVICES]
    result = []
    for target in request.devices:
        try:
            result.append((device_service.get_device(target.device_id, project.id), target))
        except NotFoundError:
            result.append((None, target))
    return result


def _plan(project, request: BootstrapRequest) -> tuple[list[dict], dict]:
    client = build_client(request.model_dump())
    nodes, topology, networks = (
        client.nodes(request.lab), client.topology(request.lab), client.networks(request.lab)
    )
    by_name = {hostname_for(n.get("name"), key): str(key) for key, n in nodes.items()}
    gateway = _gateway(project, request.gateway)
    host = urlparse(getattr(client, "base_url", request.url)).hostname

    entries = []
    for device, target in _candidates(project, request):
        entry = {"device_id": target.device_id, "ready": False}
        entries.append(entry)
        if device is None:
            entry["message"] = "Device not found in this project."
            continue
        entry.update(hostname=device.hostname, management_ip=device.management_ip, kind=device.kind)
        if device.environment != "pnetlab":
            entry["message"] = "Only PNETLab devices are bootstrapped through the console."
            continue
        if not device.managed or device.device_type not in ("cisco_ios", "paloalto_panos"):
            entry["message"] = "Only Cisco IOS and Palo Alto PAN-OS devices are supported; configure ASA by hand."
            continue
        panos = device.device_type == "paloalto_panos"
        convert = panos_interface if panos else normalize_label
        if not device.deployable or not device.management_ip:
            entry["message"] = "Give the device a management IP (provision it) first."
            continue
        try:
            credential = get_device_credential(device.id)
        except NotFoundError:
            entry["message"] = "Save an SSH login for this device first."
            continue
        if not console_typable(credential):
            entry["message"] = (
                "The saved login cannot be typed on the console (spaces, '?' or "
                "unusual characters); change it or configure this device by hand."
            )
            continue

        node_id = target.node_id or by_name.get(device.hostname.upper())
        node = nodes.get(node_id) if node_id is not None else None
        if node is None:
            entry["message"] = "No node with this name in the lab; pick the node."
            continue
        entry.update(node_id=str(node_id), node_name=node.get("name"))
        if node.get("status") is not None and str(node.get("status")) == "0":
            entry["message"] = "The node is stopped in PNETLab; start it first."
            continue
        port = console_port(node)
        if port is None:
            entry["message"] = "The node has no telnet console (set its console to telnet)."
            continue
        entry["console"] = f"{host}:{port}"

        if target.interface:
            interface = convert(target.interface)
            if interface is None:
                entry["message"] = f"{target.interface!r} is not an interface name."
                continue
        else:
            found = cloud_interfaces(str(node_id), topology, networks, convert)
            if not found:
                entry["message"] = "No interface is cabled to a Management(Cloud0) network; cable one or name it."
                continue
            interface = found[0]
        entry["interface"] = interface
        builder = build_panos_commands if panos else build_commands
        entry["commands"] = builder(device, interface, project, gateway, credential)
        if panos:
            entry["commands"] = ["configure", *entry["commands"], "commit"]
        entry["ready"] = True
        entry["_run"] = (device, credential, host, port, interface, gateway)
    return entries, {"gateway": gateway, "api": getattr(client, "mode_used", None)}


def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def preview(project, payload: dict) -> dict:
    """What a bootstrap would do. Opens nothing but the PNETLab API."""
    request = validate(BootstrapRequest, payload, "Bootstrap")
    entries, extra = _plan(project, request)
    return {"devices": [_public(e) for e in entries], **extra}


# -- the console session -----------------------------------------------------------

PROMPT = re.compile(r"(?:^|[\r\n])([\w.\-]+)(\([\w.\-]+\))?([>#]) ?\Z")
MORE = re.compile(r"--More--")
CONFIRM = re.compile(r"\[confirm\]\s*\Z")
YES_NO = re.compile(r"\[yes/no\]:?\s*\Z", re.I)
FILENAME = re.compile(r"filename \[[^\]]*\]\?\s*\Z", re.I)
DIALOG = re.compile(r"initial configuration dialog\?\s*\[yes/no\]:", re.I)
RETURN = re.compile(r"Press RETURN to get started", re.I)
AUTOINSTALL = re.compile(r"terminate autoinstall\?\s*\[yes\]:", re.I)
USERNAME = re.compile(r"(?:^|[\r\n])\s*Username:\s*\Z", re.I)
PASSWORD = re.compile(r"Password:\s*\Z", re.I)
FAILED = re.compile(
    r"(?m)^\s*% ?(Invalid input|Incomplete command|Ambiguous command|Unknown command|"
    r"Unrecognized command|Bad mask|.*overlaps with)"
)


class IOSConsole:
    """Drive an IOS console into privileged EXEC and run commands there."""

    def __init__(self, console, credential, timeout: float = 15):
        self.console = console
        self.credential = credential
        self.timeout = timeout

    def wake(self) -> None:
        patterns = [DIALOG, RETURN, AUTOINSTALL, USERNAME, PASSWORD, MORE, PROMPT]
        enabling, passwords, idle = False, 0, 0
        self.console.send("\r")
        for _ in range(20):
            index, text = self.console.expect(patterns, self.timeout)
            if index == -1:
                idle += 1
                if idle > 3:
                    raise ConsoleError("The console did not answer; is the node fully booted?")
                self.console.send("\r")
            elif index == 0:
                self.console.send("no\r")
            elif index in (1, 2):
                self.console.send("\r")
            elif index == 3:
                self.console.send(self.credential.username + "\r")
            elif index == 4:
                passwords += 1
                if passwords > 2:
                    raise ConsoleError("The console rejected the saved login or enable secret.")
                secret = (self.credential.enable_secret or self.credential.password) if enabling \
                    else self.credential.password
                self.console.send(secret + "\r")
            elif index == 5:
                self.console.send(" ")
            else:
                match = PROMPT.search(text)
                if match.group(2):
                    self.console.send("end\r")
                elif match.group(3) == ">":
                    enabling = True
                    self.console.send("enable\r")
                else:
                    return
        raise ConsoleError("Could not reach privileged EXEC on the console.")

    def run(self, command: str, timeout: float | None = None) -> str:
        self.console.send(command + "\r")
        output, retried = "", False
        while True:
            index, text = self.console.expect(
                [MORE, CONFIRM, YES_NO, FILENAME, PROMPT], timeout or self.timeout
            )
            output += text
            if index == -1:
                if retried:
                    raise ConsoleError(f"No prompt after {command.split()[0]!r}.")
                retried = True
                self.console.send("\r")
            elif index == 0:
                self.console.send(" ")
            elif index in (1, 3):
                self.console.send("\r")
            elif index == 2:
                # e.g. "Do you really want to replace them?" for existing RSA keys:
                # keeping what is there is always the safe answer.
                self.console.send("no\r")
            else:
                return output


def _existing_state(ios: IOSConsole, interface: str) -> tuple[list[str], str | None]:
    users = re.findall(r"(?m)^username (\S+)", ios.run("show running-config | include ^username"))
    brief = ios.run("show ip interface brief", timeout=30)
    address = None
    for line in brief.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == interface.lower():
            address = parts[1]
    return users, address


def _bootstrap_one(entry: dict, overwrite: bool) -> dict:
    device, credential, host, port, interface, gateway = entry["_run"]
    if device.device_type == "paloalto_panos":
        return bootstrap_panos(entry, open_console, _verify)
    result = {"device_id": device.id, "hostname": device.hostname, "console": entry["console"]}
    console = open_console(host, port)
    try:
        ios = IOSConsole(console, credential)
        ios.wake()
        ios.run("terminal length 0")
        users, current = _existing_state(ios, address_interface(device, interface))
        conflicts = [f"login {u!r} already exists" for u in users if u != credential.username]
        if current and current.lower() != "unassigned" and current != device.management_ip:
            conflicts.append(f"{address_interface(device, interface)} already has {current}")
        if conflicts and not overwrite:
            result.update(status="refused", message="; ".join(conflicts) + ". Tick overwrite to replace.")
            return result

        commands = build_commands(device, interface, entry["_project"], gateway, credential, redact=False)
        errors = []
        ios.run("configure terminal")
        for command in commands:
            output = ios.run(command, timeout=90 if command.startswith("crypto key") else None)
            if FAILED.search(output):
                shown = re.sub(r"secret \S+", f"secret {SECRET_MASK}", command)
                errors.append(f"{shown}: {FAILED.search(output).group(0).strip()}")
        ios.run("end")
        ios.run("write memory", timeout=60)
        if errors:
            result.update(status="error", message="Some commands were rejected.", errors=errors)
            return result
    except ConsoleError as exc:
        result.update(status="error", message=exc.message)
        return result
    finally:
        console.close()
    return _verify(device, result)


def _verify(device, result: dict, panos: bool = False) -> dict:
    attempts = int(current_app.config.get("BOOTSTRAP_SSH_ATTEMPTS", 3))
    delay = float(current_app.config.get("BOOTSTRAP_SSH_RETRY_SECONDS", 3))
    if panos:
        # The PAN-OS management server restarts its SSH service after a commit.
        attempts = int(current_app.config.get("BOOTSTRAP_PANOS_SSH_ATTEMPTS", 10))
        delay = float(current_app.config.get("BOOTSTRAP_PANOS_SSH_RETRY_SECONDS", 6))
    detail = ""
    for attempt in range(max(attempts, 1)):
        if attempt:
            time.sleep(delay)
        reachable, detail = device_service.check_reachability(device)
        if reachable:
            result.update(status="ok", message="Configured on the console; SSH answers.")
            return result
    result.update(status="configured", message=f"Configured on the console, but SSH failed: {detail}")
    return result


def run(project, payload: dict) -> dict:
    """Configure every ready device through its console, one after another."""
    request = validate(BootstrapRequest, payload, "Bootstrap")
    entries, extra = _plan(project, request)
    results = []
    for entry in entries:
        if not entry["ready"]:
            results.append({**_public(entry), "status": "skipped"})
            continue
        entry["_project"] = project
        entry["_default_password"] = request.default_password
        try:
            results.append({**_public(entry), **_bootstrap_one(entry, request.overwrite)})
        except AppError as exc:
            results.append({**_public(entry), "status": "error", "message": exc.message})
    return {"devices": results, **extra}
