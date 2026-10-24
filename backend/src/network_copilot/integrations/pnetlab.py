"""Read-only client for PNETLab and EVE-NG labs.

Only used to *import* what a lab already knows (its nodes, networks, cabling
and console ports); it never starts, stops, wires or edits a node.

Two APIs exist and the client speaks both:

* **PNETLab session API** (PNETLab 4/5; the EVE-NG path API answers
  "Resource not found (60038)" there). One lab is bound to the login session
  and every call targets it:
      POST /store/public/auth/login/login     {username, password, html, captcha}
      GET  /api/labs/session/topology         {labinfo, nodes, networks}
      POST /api/labs/session/factory/create   {path}  (opens the lab, if needed)
  PNETLab keeps one session per account and a running lab belongs to the
  account that opened it, so the lab is read with that same account; a lab
  already open in that session is reused rather than reopened.
* **EVE-NG path API** (EVE-NG, older PNETLab):
      POST /api/auth/login
      GET  /api/labs/<lab>.unl/nodes | topology | networks

``mode="auto"`` tries the session API first and falls back to EVE-NG when the
server does not have it.
"""

import ipaddress
import json
import socket
from urllib.parse import quote, unquote, urlparse

import requests

from ..errors import ValidationError

REQUEST_TIMEOUT = 10
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class PNETLabError(ValidationError):
    """The lab could not be read; the message is safe to show the user."""


def validate_base_url(url: str) -> str:
    """Accept only a plain http(s) URL that points at a private network host.

    The backend makes this request on a user's behalf, so a URL that could reach
    loopback, link-local (cloud metadata) or public hosts is refused rather than
    fetched.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PNETLabError("The PNETLab URL must look like http://172.16.0.5 or https://host.")
    if parsed.username or parsed.password:
        raise PNETLabError("Put the login in the username/password fields, not in the URL.")

    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise PNETLabError(f"Cannot resolve {parsed.hostname}.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or not address.is_private
        ):
            raise PNETLabError(
                f"{parsed.hostname} resolves to {address}, which is not a private "
                "lab address; refusing to connect."
            )
    return f"{parsed.scheme}://{parsed.netloc}"


def _lab_path(lab: str) -> str:
    return quote(normalize_lab(lab), safe="/")


def normalize_lab(lab: str) -> str:
    """"Course/Lab1" -> "/Course/Lab1.unl"; refuses empty and ".." paths."""
    lab = (lab or "").strip()
    if not lab or ".." in lab.split("/"):
        raise PNETLabError("Enter the lab path, for example /Course/Lab1.unl")
    if not lab.startswith("/"):
        lab = "/" + lab
    if not lab.lower().endswith(".unl"):
        lab += ".unl"
    return lab


MODES = ("auto", "session", "eveng")


class _NoSessionAPI(Exception):
    """The server is not a PNETLab with the session API (auto mode falls back)."""


def session_links(nodes: dict) -> list[dict]:
    """EVE-NG style topology entries from the session API's node ``ethernets``.

    Every interface bound to a network (``network_id`` > 0) becomes a
    node -> network entry, the shape the importer already understands.
    """
    entries = []
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        ethernets = node.get("ethernets") or {}
        if isinstance(ethernets, list):
            ethernets = dict(enumerate(ethernets))
        for index, eth in ethernets.items():
            if not isinstance(eth, dict):
                continue
            try:
                network_id = int(eth.get("network_id") or 0)
            except (TypeError, ValueError):
                continue
            if network_id <= 0:
                continue
            entries.append({
                "source": f"node{node.get('id', key)}", "source_type": "node",
                "source_label": eth.get("name") or str(index),
                "destination": f"network{network_id}", "destination_type": "network",
                "destination_label": "",
            })
    return entries


def _same_lab(labinfo: dict, wanted: str) -> bool:
    """Whether the lab bound to the session is ``wanted`` (a normalised path)."""
    if not isinstance(labinfo, dict):
        return False
    wanted = wanted.lower()
    for key in ("path", "filename", "file", "lab"):
        value = str(labinfo.get(key) or "").strip().lower()
        if not value:
            continue
        value = "/" + value.lstrip("/")
        if not value.endswith(".unl"):
            value += ".unl"
        if value == wanted or wanted.endswith(value) or value.endswith(wanted):
            return True
    name = str(labinfo.get("name") or "").strip().lower()
    return bool(name) and wanted.rsplit("/", 1)[-1] == name + ".unl"


class PNETLabClient:
    def __init__(self, base_url: str, username: str, password: str, verify_tls: bool = True,
                 mode: str = "auto"):
        self.base_url = validate_base_url(base_url)
        if mode not in MODES:
            raise PNETLabError(f"Unknown PNETLab API mode {mode!r}.")
        self.mode = mode
        # The API that answered: "session" or "eveng" (shown to the operator).
        self.mode_used: str | None = None
        self._username = username
        self._password = password
        self._verify = verify_tls
        self._session = requests.Session()
        self._logged_in = False
        self._cache: dict[str, tuple[dict, list, dict]] = {}

    # -- transport -----------------------------------------------------------

    def _send(self, method: str, path: str, **kwargs) -> tuple[int, object, dict]:
        """(status, parsed JSON or None, response headers); never follows redirects."""
        try:
            response = self._session.request(
                method,
                self.base_url + path,
                timeout=REQUEST_TIMEOUT,
                verify=self._verify,
                allow_redirects=False,
                stream=True,
                **kwargs,
            )
            body = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
        except requests.RequestException as exc:
            raise PNETLabError(f"Cannot reach PNETLab: {type(exc).__name__}.") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise PNETLabError("PNETLab sent a response that is too large.")
        try:
            data = json.loads(body) if body else None
        except ValueError:
            data = None
        return response.status_code, data, response.headers

    @staticmethod
    def _message(data, status: int) -> str:
        if isinstance(data, dict):
            return str(data.get("message") or data.get("status") or status)
        return str(status)

    @staticmethod
    def _failed(status: int, data) -> bool:
        if status >= 400 or not isinstance(data, dict):
            return True
        try:
            code = int(data.get("code", status))
        except (TypeError, ValueError):
            code = status
        return code >= 400 or data.get("status") in ("fail", "error", "unauthorized")

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """An EVE-NG style call: JSON object or a PNETLabError."""
        status, data, _ = self._send(method, path, **kwargs)
        if 300 <= status < 400:
            raise PNETLabError("PNETLab redirected the request; use its final URL.")
        if data is None:
            raise PNETLabError("PNETLab did not return JSON; is the URL right?")
        if status in (401, 403):
            raise PNETLabError("PNETLab rejected the username or password.")
        if status == 404:
            raise PNETLabError("Lab not found; check the lab path.")
        if status >= 400 or not isinstance(data, dict):
            message = data.get("message") if isinstance(data, dict) else None
            raise PNETLabError(f"PNETLab error: {message or status}")
        return data

    # -- EVE-NG path API -------------------------------------------------------

    def login(self) -> None:
        self._request(
            "POST",
            "/api/auth/login",
            json={"username": self._username, "password": self._password, "html5": "-1"},
        )
        self._logged_in = True

    def _get(self, path: str) -> dict:
        if not self._logged_in:
            self.login()
        return self._request("GET", path)

    def _read_eveng(self, lab: str) -> tuple[dict, list, dict]:
        path = _lab_path(lab)
        nodes = self._get(f"/api/labs{path}/nodes").get("data")
        topology = self._get(f"/api/labs{path}/topology").get("data")
        try:
            networks = self._get(f"/api/labs{path}/networks").get("data")
        except PNETLabError:
            networks = {}
        return (nodes if isinstance(nodes, dict) else {},
                topology if isinstance(topology, list) else [],
                networks if isinstance(networks, dict) else {})

    # -- PNETLab session API ---------------------------------------------------

    def session_login(self) -> None:
        """Offline (local) PNETLab login; raises _NoSessionAPI on a non-PNETLab server."""
        # The landing page sets the Laravel XSRF cookie some builds expect back.
        try:
            self._send("GET", "/")
        except PNETLabError:
            pass
        token = self._session.cookies.get("XSRF-TOKEN")
        headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        if token:
            headers["X-XSRF-TOKEN"] = unquote(token)
        status, data, _ = self._send(
            "POST",
            "/store/public/auth/login/login",
            headers=headers,
            # html "0": consoles are reported as telnet://host:port, not HTML5 links.
            json={"username": self._username, "password": self._password,
                  "html": "0", "captcha": ""},
        )
        if status in (301, 302, 303) and self._session.cookies:
            self._logged_in = True  # some builds redirect after a good login
            return
        if status == 404 or data is None:
            raise _NoSessionAPI()
        ok = isinstance(data, dict) and (
            data.get("result") is True or data.get("status") == "success"
            or (not self._failed(status, data) and data.get("result") is not False)
        )
        if not ok:
            raise PNETLabError(f"PNETLab login failed: {self._message(data, status)}.")
        self._logged_in = True

    def _session_topology(self) -> dict | None:
        """The lab bound to this session, or None when no lab is open."""
        status, data, _ = self._send("GET", "/api/labs/session/topology")
        if self._failed(status, data):
            return None
        payload = data.get("data")
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), (dict, list)):
            return None
        return payload

    def _read_session(self, lab: str) -> tuple[dict, list, dict]:
        if not self._logged_in:
            self.session_login()
        wanted = normalize_lab(lab) if (lab or "").strip() else None
        current = self._session_topology()
        if wanted and not (current and _same_lab(current.get("labinfo") or {}, wanted)):
            status, data, _ = self._send(
                "POST", "/api/labs/session/factory/create", json={"path": wanted}
            )
            if status == 404 and not isinstance(data, dict):
                raise _NoSessionAPI()
            if self._failed(status, data):
                raise PNETLabError(
                    f"PNETLab could not open {wanted}: {self._message(data, status)}."
                )
            current = self._session_topology()
        if current is None:
            if wanted is None:
                raise PNETLabError(
                    "No lab is open in this PNETLab session; enter the lab path."
                )
            raise PNETLabError(f"PNETLab opened {wanted} but returned no topology.")
        nodes = current.get("nodes") or {}
        if isinstance(nodes, list):
            nodes = {str(n.get("id", i)): n for i, n in enumerate(nodes) if isinstance(n, dict)}
        networks = current.get("networks") or {}
        if isinstance(networks, list):
            networks = {str(n.get("id", i)): n for i, n in enumerate(networks) if isinstance(n, dict)}
        nodes = {str(k): v for k, v in nodes.items() if isinstance(v, dict)}
        networks = {str(k): v for k, v in networks.items() if isinstance(v, dict)}
        return nodes, session_links(nodes), networks

    # -- public ------------------------------------------------------------------

    def read_lab(self, lab: str) -> tuple[dict, list, dict]:
        """(nodes, topology entries, networks) of a lab, cached per client."""
        key = (lab or "").strip()
        if key in self._cache:
            return self._cache[key]
        if self.mode == "eveng":
            result, used = self._read_eveng(lab), "eveng"
        else:
            try:
                result, used = self._read_session(lab), "session"
            except _NoSessionAPI:
                if self.mode == "session":
                    raise PNETLabError(
                        "This server has no PNETLab session API; choose the EVE-NG mode."
                    ) from None
                self._session = requests.Session()
                self._logged_in = False
                result, used = self._read_eveng(lab), "eveng"
        self.mode_used = used
        self._cache[key] = result
        return result

    def nodes(self, lab: str) -> dict:
        return self.read_lab(lab)[0]

    def topology(self, lab: str) -> list:
        return self.read_lab(lab)[1]

    def networks(self, lab: str) -> dict:
        return self.read_lab(lab)[2]


def build_client(params: dict):
    """The real client, or the test factory when one is installed."""
    from flask import current_app

    factory = current_app.config.get("PNETLAB_CLIENT_FACTORY")
    if factory is not None:
        return factory(params)
    return PNETLabClient(
        params["url"],
        params["username"],
        params["password"],
        verify_tls=params.get("verify_tls", True),
        mode=params.get("api_mode", "auto"),
    )
