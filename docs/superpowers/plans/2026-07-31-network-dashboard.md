# Network Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only, auto-refreshing `/dashboard` page (device status by role, OSPF neighbor health, pending/recent changes, recent audit activity) backed by one new `GET /api/dashboard/summary` endpoint that aggregates existing data.

**Architecture:** A new `network_copilot/dashboard/` module with a pure aggregation function (`service.build_summary()`) that calls the existing `devices`, `monitoring`, `changes`, and `audit` services — no new SQL, no new table, no SSH calls. One new route, `jwt_required()` only (any role). A new static page (`templates/dashboard.html` + `static/js/dashboard.js`) polls that endpoint every 10s using the same Alpine.js-without-a-build-step approach as the existing chat page, as its own small Alpine component rather than an extension of `app.js`.

**Tech Stack:** Flask, Flask-JWT-Extended, SQLAlchemy (existing patterns only, no new dependency), Alpine.js (already vendored at `static/vendor/alpine.min.js`), plain CSS.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-31-network-dashboard-design.md` — every task below implements one part of it.
- Use the project's Python 3.13 venv at `backend/.venv` for every command (`.venv/Scripts/python.exe` / `.venv/Scripts/pytest.exe` on Windows, or activate it) — the bare `python` on this machine resolves to 3.10 and will not run this backend.
- TDD throughout: write the failing test, confirm it fails for the expected reason, implement the minimal code, confirm it passes, then commit. One commit per task.
- No new frontend build tooling, no JavaScript test framework — this project deliberately has neither (see spec's Non-goals).
- Vietnamese UI copy, matching the existing chat page's language.
- Reuse existing CSS classes (`.status-dot`, `.risk-pill`, `.status-pill`) where the same concept already has a style; add new classes to the existing `static/css/app.css` (no second stylesheet).

---

### Task 1: Dashboard service — device role rollup + OSPF health classification

**Files:**
- Create: `backend/src/network_copilot/dashboard/__init__.py` (empty)
- Create: `backend/src/network_copilot/dashboard/service.py`
- Test: `backend/tests/dashboard/__init__.py` (empty)
- Test: `backend/tests/dashboard/test_dashboard.py`

**Interfaces:**
- Consumes: `network_copilot.devices.service.list_devices() -> list[Device]` (returns devices ordered by hostname, each with `.id`, `.hostname`, `.role`, `.status`); `network_copilot.monitoring.service.latest_snapshot(device_id: int) -> DeviceSnapshot | None` (has `.status`, `.parsed_data: dict`, `.created_at: datetime`); `network_copilot.monitoring.service.ROUTING_ROLES` (a `set` containing `"core"`, `"distribution"`); `DeviceSnapshot.parsed_data["show ip ospf neighbor"]` is a `list[dict]` with keys `neighbor_id`, `priority`, `state`, `dead_time`, `address`, `interface` (from `network_copilot/parsers/ospf.py::parse_ospf_neighbors`).
- Produces: `build_summary() -> dict` with (at minimum, this task only fills `devices` and `ospf`) `{"devices": {"by_role": {role: {"total", "online", "offline", "unknown"}}}, "ospf": [{"device_id", "hostname", "role", "health", "neighbor_count", "full_count", "neighbors", "snapshot_at"}]}`. Task 2 adds the `changes`, `audit`, and `generated_at` keys to the same function — do not return a different shape that Task 2 would need to change.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/dashboard/__init__.py` as an empty file.

Create `backend/tests/dashboard/test_dashboard.py`:

```python
from network_copilot.dashboard.service import build_summary
from network_copilot.extensions import db
from network_copilot.monitoring.model import DeviceSnapshot

FULL_NEIGHBOR = {
    "neighbor_id": "3.3.3.3",
    "priority": 1,
    "state": "FULL/DR",
    "dead_time": "00:00:33",
    "address": "10.255.0.6",
    "interface": "GigabitEthernet0/1",
}

NOT_FULL_NEIGHBOR = {
    "neighbor_id": "4.4.4.4",
    "priority": 1,
    "state": "2WAY/DROTHER",
    "dead_time": "00:00:33",
    "address": "10.255.0.7",
    "interface": "GigabitEthernet0/2",
}


def _snapshot(device, status="online", parsed_data=None):
    snapshot = DeviceSnapshot(
        device_id=device.id,
        status=status,
        raw_output={},
        parsed_data=parsed_data if parsed_data is not None else {},
    )
    db.session.add(snapshot)
    db.session.commit()
    return snapshot


# -- device role rollup -----------------------------------------------------


def test_device_role_rollup_counts_by_status(app, make_device):
    core = make_device("CORE1", "10.0.0.1", "core")
    core.status = "online"
    dist_online = make_device("DIST1", "10.0.0.2", "distribution")
    dist_online.status = "online"
    dist_offline = make_device("DIST2", "10.0.0.3", "distribution")
    dist_offline.status = "offline"
    db.session.commit()

    summary = build_summary()

    assert summary["devices"]["by_role"] == {
        "core": {"total": 1, "online": 1, "offline": 0, "unknown": 0},
        "distribution": {"total": 2, "online": 1, "offline": 1, "unknown": 0},
    }


def test_device_role_rollup_defaults_to_unknown(app, make_device):
    make_device("ACC1", "10.0.0.4", "access")

    summary = build_summary()

    assert summary["devices"]["by_role"]["access"] == {
        "total": 1,
        "online": 0,
        "offline": 0,
        "unknown": 1,
    }


# -- OSPF panel membership ---------------------------------------------------


def test_ospf_panel_only_includes_core_and_distribution(app, make_device):
    make_device("ACC1", "10.0.0.5", "access")
    make_device("CORE1", "10.0.0.6", "core")

    summary = build_summary()

    hostnames = [entry["hostname"] for entry in summary["ospf"]]
    assert hostnames == ["CORE1"]


# -- OSPF health classification ----------------------------------------------


def test_ospf_health_is_no_data_without_a_snapshot(app, make_device):
    make_device("CORE1", "10.0.0.7", "core")

    summary = build_summary()

    entry = summary["ospf"][0]
    assert entry["health"] == "no_data"
    assert entry["neighbor_count"] == 0
    assert entry["snapshot_at"] is None


def test_ospf_health_is_no_data_when_the_device_is_offline(app, make_device):
    device = make_device("CORE1", "10.0.0.8", "core")
    _snapshot(device, status="offline", parsed_data={"show ip ospf neighbor": [FULL_NEIGHBOR]})

    summary = build_summary()

    assert summary["ospf"][0]["health"] == "no_data"


def test_ospf_health_is_no_data_when_snapshot_has_no_ospf_key(app, make_device):
    device = make_device("CORE1", "10.0.0.9", "core")
    _snapshot(device, parsed_data={"show ip route": []})

    summary = build_summary()

    assert summary["ospf"][0]["health"] == "no_data"


def test_ospf_health_is_down_with_zero_neighbors(app, make_device):
    device = make_device("CORE1", "10.0.0.10", "core")
    _snapshot(device, parsed_data={"show ip ospf neighbor": []})

    summary = build_summary()

    assert summary["ospf"][0]["health"] == "down"


def test_ospf_health_is_degraded_with_a_non_full_neighbor(app, make_device):
    device = make_device("CORE1", "10.0.0.11", "core")
    _snapshot(device, parsed_data={"show ip ospf neighbor": [NOT_FULL_NEIGHBOR]})

    summary = build_summary()

    entry = summary["ospf"][0]
    assert entry["health"] == "degraded"
    assert entry["neighbor_count"] == 1
    assert entry["full_count"] == 0


def test_ospf_health_is_ok_when_all_neighbors_are_full(app, make_device):
    device = make_device("CORE1", "10.0.0.12", "core")
    _snapshot(device, parsed_data={"show ip ospf neighbor": [FULL_NEIGHBOR]})

    summary = build_summary()

    entry = summary["ospf"][0]
    assert entry["health"] == "ok"
    assert entry["full_count"] == 1
    assert entry["neighbors"][0]["neighbor_id"] == "3.3.3.3"


def test_ospf_uses_the_most_recent_snapshot(app, make_device):
    device = make_device("CORE1", "10.0.0.13", "core")
    _snapshot(device, parsed_data={"show ip ospf neighbor": [NOT_FULL_NEIGHBOR]})
    _snapshot(device, parsed_data={"show ip ospf neighbor": [FULL_NEIGHBOR]})

    summary = build_summary()

    assert summary["ospf"][0]["health"] == "ok"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/dashboard/test_dashboard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'network_copilot.dashboard'`

- [ ] **Step 3: Write the minimal implementation**

Create `backend/src/network_copilot/dashboard/__init__.py` (empty file).

Create `backend/src/network_copilot/dashboard/service.py`:

```python
"""Aggregate existing device/monitoring/changes/audit data for the dashboard."""

from ..devices import service as device_service
from ..monitoring.service import ROUTING_ROLES, latest_snapshot

OSPF_COMMAND = "show ip ospf neighbor"


def _device_role_rollup(devices) -> dict:
    rollup: dict[str, dict[str, int]] = {}
    for device in devices:
        bucket = rollup.setdefault(
            device.role, {"total": 0, "online": 0, "offline": 0, "unknown": 0}
        )
        bucket["total"] += 1
        bucket[device.status] += 1
    return rollup


def _ospf_health(snapshot) -> tuple[str, list[dict]]:
    if snapshot is None or snapshot.status != "online":
        return "no_data", []
    neighbors = snapshot.parsed_data.get(OSPF_COMMAND)
    if neighbors is None:
        return "no_data", []
    if len(neighbors) == 0:
        return "down", []
    if all("FULL" in neighbor["state"] for neighbor in neighbors):
        return "ok", neighbors
    return "degraded", neighbors


def _ospf_entry(device) -> dict:
    snapshot = latest_snapshot(device.id)
    health, neighbors = _ospf_health(snapshot)
    full_count = sum(1 for neighbor in neighbors if "FULL" in neighbor["state"])
    return {
        "device_id": device.id,
        "hostname": device.hostname,
        "role": device.role,
        "health": health,
        "neighbor_count": len(neighbors),
        "full_count": full_count,
        "neighbors": neighbors,
        "snapshot_at": snapshot.created_at.isoformat() if snapshot else None,
    }


def build_summary() -> dict:
    devices = device_service.list_devices()
    ospf_devices = [device for device in devices if device.role in ROUTING_ROLES]

    return {
        "devices": {"by_role": _device_role_rollup(devices)},
        "ospf": [_ospf_entry(device) for device in ospf_devices],
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/dashboard/test_dashboard.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/dashboard/__init__.py backend/src/network_copilot/dashboard/service.py backend/tests/dashboard/__init__.py backend/tests/dashboard/test_dashboard.py
git commit -m "feat: add dashboard device rollup and OSPF health classification"
```

---

### Task 2: Dashboard service — changes/audit passthrough

**Files:**
- Modify: `backend/src/network_copilot/dashboard/service.py`
- Test: `backend/tests/dashboard/test_dashboard.py` (append)

**Interfaces:**
- Consumes: `network_copilot.changes.service.list_changes(device_id=None, status=None, limit=100) -> list[ChangeRequest]` (already ordered newest-first); `network_copilot.changes.service.create_preview(user_id, device_id, commands, verification_commands=None, description=None, source="api") -> ChangeRequest` (test-only, does not touch SSH); `network_copilot.changes.service.cancel(change_id, user_id) -> ChangeRequest` (test-only); `network_copilot.audit.service.list_events(..., limit=100) -> list[AuditLog]` (already ordered newest-first); `network_copilot.audit.service.record_event(action, result, ...) -> AuditLog | None` (test-only). Both `ChangeRequest.to_dict()` and `AuditLog.to_dict()` already exist and are used unmodified.
- Produces: `build_summary()` now additionally returns `"changes": {"pending_approval": [...], "recent": [...]}`, `"audit": {"recent": [...]}`, and `"generated_at": str` (ISO-8601 UTC). This completes the shape consumed by Task 3's route and Task 4's frontend.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/dashboard/test_dashboard.py`:

```python
from network_copilot.audit import service as audit_service
from network_copilot.changes import service as change_service


# -- changes passthrough ------------------------------------------------------


def test_changes_pending_approval_bucket(app, admin_user, access_switch):
    change_service.create_preview(
        admin_user.id, device_id=access_switch.id, commands=["show version"]
    )

    summary = build_summary()

    assert len(summary["changes"]["pending_approval"]) == 1
    assert (
        summary["changes"]["pending_approval"][0]["device"]["hostname"]
        == "ACC-SW1"
    )


def test_changes_recent_bucket_includes_every_status(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id, device_id=access_switch.id, commands=["show version"]
    )
    change_service.cancel(change.id, admin_user.id)

    summary = build_summary()

    assert len(summary["changes"]["recent"]) == 1
    assert summary["changes"]["recent"][0]["status"] == "cancelled"


# -- audit passthrough --------------------------------------------------------


def test_audit_recent_bucket(app):
    audit_service.record_event("device.refresh", "success", message="ok")

    summary = build_summary()

    assert len(summary["audit"]["recent"]) == 1
    assert summary["audit"]["recent"][0]["action"] == "device.refresh"


# -- generated_at --------------------------------------------------------------


def test_generated_at_is_present(app):
    summary = build_summary()
    assert summary["generated_at"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/dashboard/test_dashboard.py -v`
Expected: FAIL with `KeyError: 'changes'` on the new tests; the 9 tests from Task 1 still pass.

- [ ] **Step 3: Write the minimal implementation**

Update `backend/src/network_copilot/dashboard/service.py` — add the imports and finish `build_summary()`:

```python
"""Aggregate existing device/monitoring/changes/audit data for the dashboard."""

from datetime import datetime, timezone

from ..audit import service as audit_service
from ..changes import service as changes_service
from ..devices import service as device_service
from ..monitoring.service import ROUTING_ROLES, latest_snapshot

OSPF_COMMAND = "show ip ospf neighbor"


def _device_role_rollup(devices) -> dict:
    rollup: dict[str, dict[str, int]] = {}
    for device in devices:
        bucket = rollup.setdefault(
            device.role, {"total": 0, "online": 0, "offline": 0, "unknown": 0}
        )
        bucket["total"] += 1
        bucket[device.status] += 1
    return rollup


def _ospf_health(snapshot) -> tuple[str, list[dict]]:
    if snapshot is None or snapshot.status != "online":
        return "no_data", []
    neighbors = snapshot.parsed_data.get(OSPF_COMMAND)
    if neighbors is None:
        return "no_data", []
    if len(neighbors) == 0:
        return "down", []
    if all("FULL" in neighbor["state"] for neighbor in neighbors):
        return "ok", neighbors
    return "degraded", neighbors


def _ospf_entry(device) -> dict:
    snapshot = latest_snapshot(device.id)
    health, neighbors = _ospf_health(snapshot)
    full_count = sum(1 for neighbor in neighbors if "FULL" in neighbor["state"])
    return {
        "device_id": device.id,
        "hostname": device.hostname,
        "role": device.role,
        "health": health,
        "neighbor_count": len(neighbors),
        "full_count": full_count,
        "neighbors": neighbors,
        "snapshot_at": snapshot.created_at.isoformat() if snapshot else None,
    }


def build_summary() -> dict:
    devices = device_service.list_devices()
    ospf_devices = [device for device in devices if device.role in ROUTING_ROLES]

    return {
        "devices": {"by_role": _device_role_rollup(devices)},
        "ospf": [_ospf_entry(device) for device in ospf_devices],
        "changes": {
            "pending_approval": [
                change.to_dict()
                for change in changes_service.list_changes(
                    status="pending_approval", limit=20
                )
            ],
            "recent": [
                change.to_dict()
                for change in changes_service.list_changes(limit=10)
            ],
        },
        "audit": {
            "recent": [
                event.to_dict() for event in audit_service.list_events(limit=20)
            ]
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/dashboard/test_dashboard.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/dashboard/service.py backend/tests/dashboard/test_dashboard.py
git commit -m "feat: add changes and audit passthrough to dashboard summary"
```

---

### Task 3: Dashboard route + page registration

**Files:**
- Create: `backend/src/network_copilot/dashboard/routes.py`
- Modify: `backend/src/network_copilot/app.py`
- Test: `backend/tests/dashboard/test_dashboard.py` (append)

**Interfaces:**
- Consumes: `network_copilot.dashboard.service.build_summary() -> dict` (from Tasks 1-2).
- Produces: `bp` (Flask Blueprint, `url_prefix="/api/dashboard"`) exporting `GET /api/dashboard/summary`; registered in `app.py`. A new page route `GET /dashboard` rendering `templates/dashboard.html` (created in Task 4 — this task's page-route test only checks the route returns 200; the template file must exist by the time this task's tests run, so create a minimal placeholder file in this task and let Task 4 replace it with the full page).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/dashboard/test_dashboard.py`:

```python
# -- API -----------------------------------------------------------------


def test_summary_endpoint_requires_authentication(client):
    assert client.get("/api/dashboard/summary").status_code == 401


def test_summary_endpoint_is_readable_by_viewer(client, viewer_headers):
    response = client.get("/api/dashboard/summary", headers=viewer_headers)
    assert response.status_code == 200
    body = response.get_json()
    assert "devices" in body
    assert "ospf" in body
    assert "changes" in body
    assert "audit" in body
    assert "generated_at" in body


def test_dashboard_page_is_served(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/dashboard/test_dashboard.py -v`
Expected: FAIL — `/api/dashboard/summary` and `/dashboard` both 404 (blueprint/route not registered yet).

- [ ] **Step 3: Write the minimal implementation**

Create `backend/src/network_copilot/dashboard/routes.py`:

```python
from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

from . import service

bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")


@bp.get("/summary")
@jwt_required()
def summary():
    return jsonify(service.build_summary()), 200
```

Create a placeholder `backend/src/network_copilot/templates/dashboard.html` (Task 4 replaces the body; this only has to exist and return 200):

```html
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <title>Network Copilot — Dashboard</title>
</head>
<body></body>
</html>
```

Modify `backend/src/network_copilot/app.py`:

In `_register_blueprints`, add the import and registration:

```python
def _register_blueprints(app: Flask) -> None:
    from .ai.routes import bp as ai_bp
    from .audit.routes import bp as audit_bp
    from .auth.routes import bp as auth_bp
    from .changes.routes import bp as changes_bp
    from .chat.routes import bp as chat_bp
    from .commands.routes import bp as commands_bp
    from .dashboard.routes import bp as dashboard_bp
    from .devices.routes import bp as devices_bp
    from .monitoring.routes import bp as monitoring_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(monitoring_bp)
    app.register_blueprint(changes_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(dashboard_bp)
```

In `create_app`, add the page route next to the existing `index()` route:

```python
    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/dashboard")
    def dashboard_page():
        return render_template("dashboard.html")

    return app
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/dashboard/test_dashboard.py -v`
Expected: PASS (16 tests)

Then run the full suite to confirm no regression from the blueprint/route registration change:

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass (no failures, no errors)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/dashboard/routes.py backend/src/network_copilot/app.py backend/src/network_copilot/templates/dashboard.html backend/tests/dashboard/test_dashboard.py
git commit -m "feat: add /api/dashboard/summary route and /dashboard page"
```

---

### Task 4: Dashboard frontend page

**Files:**
- Modify: `backend/src/network_copilot/templates/dashboard.html` (replace placeholder from Task 3)
- Create: `backend/src/network_copilot/static/js/dashboard.js`
- Modify: `backend/src/network_copilot/static/css/app.css` (append dashboard rules)
- Modify: `backend/src/network_copilot/templates/index.html` (add nav link to `/dashboard`)

**Interfaces:**
- Consumes: `GET /api/dashboard/summary` (Task 3) returning the shape from Task 2; `GET /api/auth/login` and `GET /api/auth/me` (existing, same contract `app.js` already uses: `POST /api/auth/login` body `{username, password}` returns `{access_token, user: {username, role}}`; `localStorage` keys `nc_token` / `nc_user`, matching `static/js/app.js` exactly so a token created on one page is valid on the other).
- Produces: a working `/dashboard` page. No other task depends on this one's internals.

- [ ] **Step 1: Write `static/js/dashboard.js`**

```javascript
function storedUser() {
  try {
    const user = JSON.parse(localStorage.getItem("nc_user") || "null");
    return user && typeof user === "object" ? user : null;
  } catch {
    localStorage.removeItem("nc_user");
    return null;
  }
}

document.addEventListener("alpine:init", () => {
  Alpine.data("dashboardApp", () => ({
    token: localStorage.getItem("nc_token") || null,
    currentUser: storedUser(),
    loginForm: { username: "", password: "" },
    loginError: "",
    _sessionGeneration: 0,

    summary: null,
    lastError: "",
    _summaryTimer: null,
    _summaryGeneration: 0,

    init() {
      if (!this.token) return;
      this.startApp();
    },

    async authFetch(path, options = {}) {
      const token = this.token;
      const generation = this._sessionGeneration;
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        options.headers || {},
        token ? { Authorization: `Bearer ${token}` } : {}
      );
      const response = await fetch(path, { ...options, headers });
      const data = await response.json().catch(() => ({}));
      if (
        response.status === 401 &&
        generation === this._sessionGeneration &&
        token === this.token
      ) {
        this.logout();
      }
      if (!response.ok) {
        const error = new Error(
          (data && data.message) || `Request failed (${response.status})`
        );
        error.status = response.status;
        throw error;
      }
      return data;
    },

    async login() {
      this.loginError = "";
      const generation = this._sessionGeneration;
      try {
        const data = await this.authFetch("/api/auth/login", {
          method: "POST",
          body: JSON.stringify(this.loginForm),
        });
        if (generation !== this._sessionGeneration) return;
        this.token = data.access_token;
        this.currentUser = data.user;
        localStorage.setItem("nc_token", this.token);
        localStorage.setItem("nc_user", JSON.stringify(this.currentUser));
        this.loginForm = { username: "", password: "" };
        await this.startApp();
      } catch (err) {
        this.loginError = err.message || "Đăng nhập thất bại.";
      }
    },

    logout() {
      this._sessionGeneration += 1;
      this._summaryGeneration += 1;
      this.token = null;
      this.currentUser = null;
      localStorage.removeItem("nc_token");
      localStorage.removeItem("nc_user");
      this.stopPolling();
      this.summary = null;
      this.lastError = "";
    },

    async startApp() {
      await this.refreshSummary().catch(() => {});
      this.startPolling();
    },

    startPolling() {
      this.stopPolling();
      this._summaryTimer = setInterval(() => {
        this.refreshSummary().catch(() => {});
      }, 10000);
    },

    stopPolling() {
      clearInterval(this._summaryTimer);
    },

    async refreshSummary() {
      const generation = this._summaryGeneration;
      try {
        const data = await this.authFetch("/api/dashboard/summary");
        if (generation === this._summaryGeneration) {
          this.summary = data;
          this.lastError = "";
        }
      } catch (err) {
        if (generation === this._summaryGeneration && this.currentUser) {
          this.lastError = new Date().toLocaleTimeString("vi-VN");
        }
        throw err;
      }
    },

    allOspfNoData() {
      return (
        this.summary &&
        this.summary.ospf.length > 0 &&
        this.summary.ospf.every((entry) => entry.health === "no_data")
      );
    },
  }));
});
```

- [ ] **Step 2: Write `templates/dashboard.html`** (replacing the Task 3 placeholder)

```html
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Network Copilot — Dashboard</title>
  <link rel="icon" href="data:," />
  <link rel="stylesheet" href="{{ url_for('static', filename='css/app.css') }}" />
</head>
<body x-data="dashboardApp()">

  <section class="login-screen" x-show="!currentUser" x-cloak>
    <form class="login-form" @submit.prevent="login()">
      <h1>Network Copilot</h1>
      <label>
        Tên đăng nhập
        <input type="text" x-model="loginForm.username" required autocomplete="username" />
      </label>
      <label>
        Mật khẩu
        <input type="password" x-model="loginForm.password" required autocomplete="current-password" />
      </label>
      <p class="error-text" x-show="loginError" x-text="loginError"></p>
      <button type="submit">Đăng nhập</button>
    </form>
  </section>

  <div class="app-shell" x-show="currentUser" x-cloak>
    <header class="top-bar">
      <span class="app-title">Network Copilot — Dashboard</span>
      <a class="nav-link" href="/">Chat</a>
      <span class="user-badge">
        <span x-text="currentUser && currentUser.username"></span>
        <span class="role-pill" x-text="currentUser && currentUser.role"></span>
      </span>
      <button class="logout-btn" @click="logout()">Đăng xuất</button>
    </header>

    <p class="dashboard-error" x-show="lastError" x-cloak>
      Không tải được dữ liệu mới nhất lúc <span x-text="lastError"></span>
    </p>

    <div class="dashboard-grid" x-show="summary" x-cloak>
      <section class="dashboard-panel">
        <h2>Trạng thái thiết bị</h2>
        <table class="dashboard-table">
          <thead>
            <tr><th>Vai trò</th><th>Online</th><th>Offline</th><th>Unknown</th><th>Tổng</th></tr>
          </thead>
          <tbody>
            <template x-for="[role, counts] in Object.entries(summary.devices.by_role)" :key="role">
              <tr>
                <td x-text="role"></td>
                <td><span class="status-dot status-online"></span><span x-text="counts.online"></span></td>
                <td><span class="status-dot status-offline"></span><span x-text="counts.offline"></span></td>
                <td><span class="status-dot status-unknown"></span><span x-text="counts.unknown"></span></td>
                <td x-text="counts.total"></td>
              </tr>
            </template>
          </tbody>
        </table>
      </section>

      <section class="dashboard-panel">
        <h2>Tình trạng OSPF</h2>
        <p class="dashboard-hint" x-show="allOspfNoData()">
          Chưa có dữ liệu OSPF — kiểm tra MONITORING_ENABLED trên máy chủ.
        </p>
        <template x-for="entry in summary.ospf" :key="entry.device_id">
          <div class="ospf-entry">
            <div class="ospf-entry-header">
              <strong x-text="entry.hostname"></strong>
              <span class="health-pill" :class="'health-' + entry.health" x-text="entry.health"></span>
              <span class="ospf-count" x-text="entry.neighbor_count + ' neighbor(s)'"></span>
            </div>
            <ul class="ospf-neighbor-list" x-show="entry.neighbors.length">
              <template x-for="neighbor in entry.neighbors" :key="neighbor.neighbor_id + neighbor.interface">
                <li>
                  <code x-text="neighbor.neighbor_id"></code>
                  <span x-text="neighbor.state"></span>
                  <span x-text="neighbor.interface"></span>
                </li>
              </template>
            </ul>
          </div>
        </template>
      </section>

      <section class="dashboard-panel">
        <h2>Thay đổi</h2>
        <h3>Đang chờ duyệt</h3>
        <ul class="dashboard-list">
          <template x-for="change in summary.changes.pending_approval" :key="'pending-' + change.id">
            <li>
              <a href="/">
                <span x-text="change.device.hostname"></span>
                <span class="risk-pill" x-text="change.risk_level"></span>
              </a>
            </li>
          </template>
        </ul>
        <h3>Gần đây</h3>
        <ul class="dashboard-list">
          <template x-for="change in summary.changes.recent" :key="'recent-' + change.id">
            <li>
              <a href="/">
                <span x-text="change.device.hostname"></span>
                <span class="status-pill" x-text="change.status"></span>
              </a>
            </li>
          </template>
        </ul>
      </section>

      <section class="dashboard-panel">
        <h2>Nhật ký hoạt động</h2>
        <ul class="dashboard-list">
          <template x-for="event in summary.audit.recent" :key="event.id">
            <li>
              <span class="audit-time" x-text="new Date(event.created_at).toLocaleTimeString('vi-VN')"></span>
              <span x-text="event.username || 'system'"></span>
              <span x-text="event.action"></span>
              <span class="result-status" :class="'result-' + event.result" x-text="event.result"></span>
            </li>
          </template>
        </ul>
      </section>
    </div>
  </div>

  <script src="{{ url_for('static', filename='js/dashboard.js') }}" defer></script>
  <script src="{{ url_for('static', filename='vendor/alpine.min.js') }}" defer></script>
</body>
</html>
```

- [ ] **Step 3: Append dashboard rules to `static/css/app.css`**

```css

/* -- Dashboard -- */

.nav-link {
  color: #cfe0f3;
  text-decoration: none;
  font-size: 13px;
  border: 1px solid #4b6b8c;
  padding: 6px 12px;
  border-radius: 4px;
}

.nav-link:hover {
  background: rgba(255, 255, 255, 0.08);
}

.dashboard-error {
  background: #fdecea;
  color: #8a1c14;
  font-size: 13px;
  margin: 0;
  padding: 8px 20px;
}

.dashboard-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 12px;
}

@media (max-width: 900px) {
  .dashboard-grid {
    grid-template-columns: 1fr;
  }
}

.dashboard-panel {
  background: #fff;
  border: 1px solid #d9e0e6;
  border-radius: 6px;
  padding: 14px;
  overflow-y: auto;
}

.dashboard-panel h2 {
  font-size: 14px;
  margin: 0 0 10px;
}

.dashboard-panel h3 {
  font-size: 12px;
  color: #6b7a89;
  text-transform: uppercase;
  margin: 12px 0 6px;
}

.dashboard-hint {
  color: #6b7a89;
  font-size: 12px;
}

.dashboard-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.dashboard-table th,
.dashboard-table td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid #eef1f4;
}

.dashboard-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}

.dashboard-list a {
  display: flex;
  gap: 8px;
  align-items: center;
  color: inherit;
  text-decoration: none;
}

.dashboard-list a:hover {
  text-decoration: underline;
}

.ospf-entry {
  border-top: 1px solid #eef1f4;
  padding: 8px 0;
}

.ospf-entry-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.ospf-count {
  color: #6b7a89;
  font-size: 11px;
  margin-left: auto;
}

.health-pill {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  text-transform: uppercase;
}

.health-ok { background: #dcefe3; color: #176b35; }
.health-degraded { background: #fdf1d6; color: #8a5a1c; }
.health-down, .health-no_data { background: #f8dfdc; color: #8a1c14; }

.ospf-neighbor-list {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 11px;
  color: #4b5c6b;
}

.ospf-neighbor-list li {
  display: flex;
  gap: 8px;
}

.audit-time {
  color: #6b7a89;
  font-size: 11px;
  min-width: 70px;
}
```

- [ ] **Step 4: Add a nav link to `/dashboard` in `templates/index.html`**

Find the existing top bar:

```html
    <header class="top-bar">
      <span class="app-title">Network Copilot</span>
      <span class="user-badge">
```

Replace with:

```html
    <header class="top-bar">
      <span class="app-title">Network Copilot</span>
      <a class="nav-link" href="/dashboard">Dashboard</a>
      <span class="user-badge">
```

- [ ] **Step 5: Run the full backend test suite** (this task made no Python change, but confirms nothing else broke while the working tree was touched)

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add backend/src/network_copilot/templates/dashboard.html backend/src/network_copilot/static/js/dashboard.js backend/src/network_copilot/static/css/app.css backend/src/network_copilot/templates/index.html
git commit -m "feat: build the realtime network dashboard page"
```

---

### Task 5: End-to-end manual verification in a real browser

**Files:** none (verification only — no code changes expected; if a bug is found, fix it in the file it belongs to and note that in the commit)

**Interfaces:** none — this task exercises Tasks 1-4 together against the real (or lab) backend.

- [ ] **Step 1: Start the backend**

Run (from `backend/`): `.venv/Scripts/python.exe -m flask --app wsgi.py run` (or the project's existing run command/deployment if testing against the PNETLab lab node)

- [ ] **Step 2: Log in and open the dashboard**

In a browser, go to `/`, log in, click the new "Dashboard" nav link (or navigate directly to `/dashboard`). Confirm:
- The login form appears first if not authenticated, and works the same as on `/`.
- All four panels render: device status by role, OSPF health, changes, audit feed.

- [ ] **Step 3: Confirm live refresh**

With the dashboard open, approve or apply a pending change on `/` (or trigger any action that changes device/change/audit state). Confirm the dashboard reflects it within ~10 seconds without a manual page reload.

- [ ] **Step 4: Confirm the OSPF `no_data` state and its cause**

If `MONITORING_ENABLED` is currently `false` on the running node, confirm the OSPF panel shows `no_data` for every core/distribution device along with the "Chưa có dữ liệu OSPF" hint. If time allows, set `MONITORING_ENABLED=true` in `.env`, restart, and confirm real neighbor data appears after the next poll interval.

- [ ] **Step 5: Confirm session handling**

Confirm the nav link round-trips (`/` → `/dashboard` → `/`) without re-prompting for login (same `localStorage` token). Then clear the token (or wait for it to expire) and confirm the dashboard returns to the login form, matching the chat page's existing 401 behavior.

- [ ] **Step 6: Report results**

Note any bugs found and fixed as part of this step in a follow-up commit (`fix: <description>`), or confirm no issues were found — no separate commit needed if nothing changed.
