"""Write side of PNETLab's session API: build a new lab from NetAI.

The calls are the ones PNETLab's own web UI makes (captured on the CSN301
server, PNETLab 4/5) and those of pnetlab-mcp-server:

    POST /api/folders/add                    {path, name}
    POST /api/labs/session/factory/create    {path, name, version, ...}  creates and opens
    GET  /api/folders?path=<folder>          a folder's sub-folders and labs (PNETLab 5.3)
    GET  /api/list/templates/                available templates
    GET  /api/list/templates/<template>      a template's option defaults
    POST /api/labs/session/nodes/add         {template, type, image, name, ethernet, left, top, ...}
    POST /api/labs/session/networks/add      {type: "pnet0" | "bridge", name, left, top, ...} -> data.id
    POST /api/labs/session/networks/p2p      {src_id, src_if, dest_id, dest_if}
    POST /api/labs/session/interfaces/edit   {node_id, data: {"<interface index>": "<network id>"}}
    GET  /api/labs/session/topology          what the lab holds now
    GET  /api/auth                           data.lab: this session's lab id
    POST /api/labs/session/factory/destroy   {lab_session}
    DELETE /api/labs                         {path}

Every call targets the lab bound to the login session, so one writer builds
one lab. It never starts, stops or edits an existing node.
"""

from urllib.parse import quote

from .pnetlab import PNETLabClient, PNETLabError, _NoSessionAPI


def lab_file_name(name: str) -> str:
    """The name PNETLab keeps: it drops every character but letters and digits
    ("netai-probe-create" is saved as netaiprobecreate.unl)."""
    return "".join(ch for ch in name if ch.isascii() and ch.isalnum())


def _is_lab(labinfo: dict, name: str, path: str) -> bool:
    """Whether the session's labinfo describes the lab just created."""
    if not isinstance(labinfo, dict):
        return False
    if str(labinfo.get("name") or "").strip().lower() == name.lower():
        return True
    wanted = path.lower()
    for key in ("path", "filename", "file", "lab"):
        value = str(labinfo.get(key) or "").strip().lower()
        if value and (value == wanted or value.endswith("/" + wanted.rsplit("/", 1)[-1])
                      or value == wanted.rsplit("/", 1)[-1]):
            return True
    return False


class ExistingLabError(PNETLabError):
    """The lab name is taken; nothing was built and nothing must be deleted."""


class PNETLabWriter:
    def __init__(self, client: PNETLabClient):
        self.client = client

    # -- transport -------------------------------------------------------------

    def login(self) -> None:
        try:
            self.client.session_login()
        except _NoSessionAPI as exc:
            # No JSON came back: a redirect (often http -> https) or a page
            # that is not PNETLab's login API.
            raise PNETLabError(
                f"{self.client.base_url} không trả lời API đăng nhập của PNETLab "
                "(thường do bị chuyển hướng sang https). Thử URL https://… với cổng "
                "PNETLab đang dùng, và bỏ tick \"Kiểm tra chứng chỉ HTTPS\" nếu "
                "chứng chỉ là tự ký."
            ) from exc

    def _call(self, method: str, path: str, body: dict | None = None, *, what: str) -> dict:
        status, data, _ = self.client._send(method, path, json=body) if body is not None \
            else self.client._send(method, path)
        if self.client._failed(status, data):
            raise PNETLabError(f"PNETLab could not {what}: {self.client._message(data, status)}.")
        return data

    @staticmethod
    def _data(response: dict):
        return response.get("data") if isinstance(response, dict) else None

    # -- reading -----------------------------------------------------------------

    def templates(self) -> list[str]:
        """Template keys this server can run (".missing" ones have no image)."""
        listing = self._data(self._call("GET", "/api/list/templates/", what="list its templates"))
        if not isinstance(listing, dict):
            return []
        return sorted(key for key, label in listing.items() if not str(label).endswith(".missing"))

    def template_options(self, template: str) -> dict:
        data = self._data(self._call("GET", f"/api/list/templates/{template}",
                                     what=f"read template {template}"))
        options = data.get("options") if isinstance(data, dict) else None
        return options if isinstance(options, dict) else {}

    def topology(self) -> dict:
        data = self._data(self._call("GET", "/api/labs/session/topology", what="read the lab"))
        if not isinstance(data, dict):
            raise PNETLabError("PNETLab returned no topology for the new lab.")
        return data

    def _node_ids(self) -> set[str]:
        nodes = self.topology().get("nodes") or {}
        if isinstance(nodes, list):
            return {str(n.get("id")) for n in nodes if isinstance(n, dict)}
        return {str(key) for key in nodes}

    # -- building ---------------------------------------------------------------

    def create_lab(self, folder: str, name: str) -> str:
        """Create ``<folder>/<name>.unl``, bind it to the session, return its path.

        PNETLab builds differ in how a lab is created, so the known ways are
        tried in turn, each checked by reading the session's lab back:

        * what PNETLab's web UI does: ``POST /api/labs`` writes the .unl,
          then ``factory/create`` with its path opens it (the call the
          import already uses);
        * ``factory/create`` with the parent folder and a name
          (pnetlab-mcp-server), then with the file path.

        ``name`` must already be what PNETLab keeps (see ``lab_file_name``).

        A failure reports what PNETLab answered to each attempt.
        """
        folder = "/" + folder.strip("/")
        if folder != "/":
            parent, _, leaf = folder.rpartition("/")
            # An existing folder answers with an error; the lab step decides.
            for route in ("/api/folders/add", "/api/folders"):
                status, data, _ = self.client._send("POST", route, json={"path": parent or "/", "name": leaf})
                if not self.client._failed(status, data):
                    break
        path = (folder.rstrip("/") + "/" + name + ".unl")
        if self.lab_exists(folder, name):
            raise PNETLabError(f"{path} already exists in PNETLab; choose another lab name.")
        meta = {"name": name, "version": "1", "author": "NetAI",
                "description": "Built by NetAI from a project topology.", "body": ""}
        answers: list[str] = []

        def attempt(label: str, method: str, route: str, body: dict) -> bool:
            status, data, _ = self.client._send(method, route, json=body)
            failed = self.client._failed(status, data)
            answers.append(f"{label}: {status} {self.client._message(data, status)}")
            return not failed

        def opened() -> bool:
            status, data, _ = self.client._send("GET", "/api/labs/session/topology")
            current = data.get("data") if isinstance(data, dict) and not self.client._failed(status, data) else None
            if not isinstance(current, dict):
                answers.append("topology: no lab open")
                return False
            labinfo = current.get("labinfo") or {}
            if not _is_lab(labinfo, name, path):
                answers.append(f"topology: lab open is {labinfo.get('name') or labinfo.get('path') or '?'}")
                return False
            if current.get("nodes") or current.get("networks"):
                # factory/create opens a lab that already exists: never
                # build into (or later delete) someone else's lab.
                raise ExistingLabError(f"{path} already exists in PNETLab; choose another lab name.")
            return True

        create = "/api/labs/session/factory/create"
        # What PNETLab's own "Add new lab" sends (captured on the CSN301
        # server): the folder with a trailing slash, the account's user id as
        # author, and access for that account only; then the lab is opened.
        user_id = self.user_id()
        ui_body = {**meta, "path": folder.rstrip("/") + "/", "scripttimeout": 300, "countdown": 60}
        if user_id:
            ui_body.update(author=user_id, openable=2, openable_emails=[user_id],
                           joinable=2, joinable_emails=[user_id], editable=2, editable_emails=[user_id])
        if attempt("POST /api/labs", "POST", "/api/labs", ui_body) \
                and attempt("open", "POST", create, {"path": path}) and opened():
            return path
        # Older builds and pnetlab-mcp-server create through factory/create.
        if attempt("factory/create folder+name", "POST", create, {"path": folder, **meta}) and opened():
            return path
        if attempt("factory/create path", "POST", create, {"path": path, **meta}) and opened():
            return path
        raise PNETLabError(f"PNETLab could not create the lab {path}. Answers: " + "; ".join(answers))

    def user_id(self) -> str | None:
        """This account's PNETLab user id (the UI sends it as the lab author)."""
        status, data, _ = self.client._send("GET", "/api/auth")
        info = data.get("data") if isinstance(data, dict) and not self.client._failed(status, data) else None
        if not isinstance(info, dict):
            return None
        for key in ("id", "user_id", "uid", "user"):
            value = info.get(key)
            if isinstance(value, (int, str)) and str(value).isdigit():
                return str(value)
        return None

    def list_folder(self, folder: str) -> tuple[dict | None, str | None]:
        """(the folder's listing, None) or (None, why it could not be read).

        PNETLab 5.3 lists a folder with a query string,
        ``GET /api/folders?path=/Courses/...`` (captured on the CSN301
        server); the EVE-NG form ``GET /api/folders/Courses/...`` answers
        "Resource not found (60038)" there and is only the fallback.
        """
        folder = "/" + folder.strip("/") if folder.strip("/") else "/"
        problems = []
        for route in (f"/api/folders?path={quote(folder, safe='')}",
                      "/api/folders" + (folder if folder != "/" else "")):
            status, data, _ = self.client._send("GET", route)
            listing = data.get("data") if isinstance(data, dict) else None
            if not self.client._failed(status, data) and isinstance(listing, dict):
                return listing, None
            problems.append(self.client._message(data, status))
        # The query form's answer is the one that describes this server.
        return None, problems[0]

    def folder_problem(self, folder: str) -> str | None:
        """Why this account cannot use the folder, or None when it can list it.

        ai.tool may not even open "/" on the CSN301 server ("You do not have
        permission to access folder /"); checking first keeps a build from
        failing after it started.
        """
        return self.list_folder(folder)[1]

    def lab_exists(self, folder: str, name: str) -> bool:
        """Whether the folder already lists ``<name>.unl`` (False if unreadable)."""
        listing, _ = self.list_folder(folder)
        labs = listing.get("labs") if isinstance(listing, dict) else None
        wanted = f"{name}.unl".lower()
        for lab in labs or []:
            names = (lab.get(key) for key in ("file", "name", "path")) if isinstance(lab, dict) else (lab,)
            if any(str(value or "").lower().rsplit("/", 1)[-1] in (wanted, name.lower()) for value in names):
                return True
        return False

    def add_node(self, body: dict) -> str:
        """Add one node and return its id (read from the topology, not guessed)."""
        before = self._node_ids()
        self._call("POST", "/api/labs/session/nodes/add", body, what=f"add node {body.get('name')}")
        new = sorted(self._node_ids() - before, key=lambda key: int(key) if key.isdigit() else 0)
        if not new:
            raise PNETLabError(f"PNETLab accepted node {body.get('name')} but it is not in the lab.")
        return new[-1]

    def add_network(self, kind: str, name: str, left: int, top: int, icon: str) -> str:
        response = self._call("POST", "/api/labs/session/networks/add", {
            "count": "1", "visibility": "1", "name": name, "type": kind,
            "left": int(left), "top": str(int(top)), "size": "", "icon": icon, "postfix": 0,
        }, what=f"add network {name}")
        data = self._data(response)
        network_id = data.get("id") if isinstance(data, dict) else None
        if network_id is None:
            raise PNETLabError(f"PNETLab added network {name} without telling its id.")
        return str(network_id)

    def connect(self, src_id: str, src_if: int, dest_id: str, dest_if: int) -> None:
        self._call("POST", "/api/labs/session/networks/p2p", {
            "name": "", "src_id": int(src_id), "src_if": int(src_if),
            "dest_id": int(dest_id), "dest_if": int(dest_if),
        }, what="cable two nodes")

    def attach(self, node_id: str, index: int, network_id: str) -> None:
        self._call("POST", "/api/labs/session/interfaces/edit", {
            "node_id": str(node_id), "data": {str(int(index)): str(network_id)},
        }, what=f"attach node {node_id} to network {network_id}")

    def delete_lab(self, path: str) -> str | None:
        """Close the session's lab and delete its file. Never raises.

        Returns None once the lab is gone, else why not: an account may
        create labs yet not delete them (ai.tool on the CSN301 server), and
        the half-built lab must then be named so someone can delete it.
        """
        try:
            status, data, _ = self.client._send("GET", "/api/auth")
            info = data.get("data") if isinstance(data, dict) else None
            session = info.get("lab") if isinstance(info, dict) else None
            if session:
                self.client._send("POST", "/api/labs/session/factory/destroy",
                                  json={"lab_session": session})
            status, data, _ = self.client._send("DELETE", "/api/labs", json={"path": path})
        except PNETLabError as exc:
            return exc.message
        if self.client._failed(status, data):
            return self.client._message(data, status)
        return None
