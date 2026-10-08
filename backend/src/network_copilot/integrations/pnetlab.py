"""Read-only client for the PNETLab / EVE-NG style REST API.

Only used to *import* what a lab already knows (its nodes and cabling); it
never starts, stops or edits anything in the lab.

Endpoints used (the EVE-NG compatible API PNETLab exposes):
    POST /api/auth/login
    GET  /api/labs/<lab>.unl/nodes
    GET  /api/labs/<lab>.unl/topology
"""

import ipaddress
import socket
from urllib.parse import quote, urlparse

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
    lab = (lab or "").strip()
    if not lab or ".." in lab.split("/"):
        raise PNETLabError("Enter the lab path, for example /Course/Lab1.unl")
    if not lab.startswith("/"):
        lab = "/" + lab
    if not lab.endswith(".unl"):
        lab += ".unl"
    return quote(lab, safe="/")


class PNETLabClient:
    def __init__(self, base_url: str, username: str, password: str, verify_tls: bool = True):
        self.base_url = validate_base_url(base_url)
        self._username = username
        self._password = password
        self._verify = verify_tls
        self._session = requests.Session()
        self._logged_in = False

    def _request(self, method: str, path: str, **kwargs) -> dict:
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
        if 300 <= response.status_code < 400:
            raise PNETLabError("PNETLab redirected the request; use its final URL.")
        try:
            data = requests.models.complexjson.loads(body or b"{}")
        except ValueError as exc:
            raise PNETLabError("PNETLab did not return JSON; is the URL right?") from exc
        if response.status_code in (401, 403):
            raise PNETLabError("PNETLab rejected the username or password.")
        if response.status_code == 404:
            raise PNETLabError("Lab not found; check the lab path.")
        if response.status_code >= 400 or not isinstance(data, dict):
            message = data.get("message") if isinstance(data, dict) else None
            raise PNETLabError(f"PNETLab error: {message or response.status_code}")
        return data

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

    def nodes(self, lab: str) -> dict:
        data = self._get(f"/api/labs{_lab_path(lab)}/nodes").get("data")
        return data if isinstance(data, dict) else {}

    def topology(self, lab: str) -> list:
        data = self._get(f"/api/labs{_lab_path(lab)}/topology").get("data")
        return data if isinstance(data, list) else []


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
    )
