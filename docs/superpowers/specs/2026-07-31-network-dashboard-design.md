# Network Dashboard — Design Spec

**Date:** 2026-07-31
**Status:** Approved for planning

## Goal

Give any authenticated user (VIEWER, OPERATOR, ADMIN) a read-only page,
separate from the chat page, that shows the operational state of the network
at a glance: device status by role, OSPF neighbor health on core/distribution
devices, pending and recent configuration changes, and a recent audit
activity feed. The page refreshes itself every 10 seconds so it stays useful
left open on a wall monitor or a second screen during an incident.

This spec covers one cohesive feature: one new read-only aggregation endpoint
and one new page that polls it. It does not change the chat page's behavior
and does not add any write/action capability to the dashboard itself.

## Non-goals (explicitly out of scope for this iteration)

- Any Approve/Apply/Cancel action from the dashboard — those stay on the
  chat page; the dashboard links to it instead of duplicating the controls.
- WebSocket/SSE push — 10s polling is consistent with the rest of the app
  (chat/devices poll at 15s, chat messages at ~7s) and needs no new
  infrastructure.
- Actively triggering SSH polls from the dashboard. Freshness comes from the
  existing `monitoring` background scheduler (`MONITORING_ENABLED`), not from
  the dashboard itself.
- A concept of "expected OSPF neighbor count" per device/topology. No such
  configuration exists today; inventing one is out of scope. OSPF health is
  derived purely from the latest snapshot already stored.
- A JavaScript test framework or build tooling.
- Mobile-specific responsive design (must not visibly break on a laptop
  screen; no further polish required).

## Architecture

- **Backend:** a new `network_copilot/dashboard/` module (`service.py`,
  `routes.py`), same shape as every other domain module. It contains no new
  query logic beyond aggregation — it calls `devices.service`,
  `monitoring.service`, `changes.service`, and `audit.service` directly and
  reshapes their results.
- **Frontend:** a new page, `templates/dashboard.html` +
  `static/js/dashboard.js`, served by Flask the same way `/` is. A small,
  separate Alpine.js component (`dashboardApp()`) — not a mode of the
  existing `app()` component in `static/js/app.js`.
  `static/js/app.js` already carries significant hardening (session
  generation tracking, optimistic reconciliation, etc.) for the chat page's
  own needs; growing it to also drive an unrelated page raises its risk of
  regression for no benefit. `dashboard.js` duplicates the small
  (~15-line) `authFetch`/token-check/redirect-to-login helper rather than
  extracting a shared module in this iteration — an acceptable amount of
  duplication given the size, and safer than refactoring already-hardened
  code as a side effect of an unrelated feature.
- A link between `/` and `/dashboard` is added to both pages' top bars so a
  user can move between them.
- Operational dependency the dashboard relies on but does not control: the
  existing `monitoring` background scheduler must be enabled
  (`MONITORING_ENABLED=true`) on the deployed node for OSPF/device-status
  data to actually refresh over time. This is a deployment/ops step, called
  out in Rollout below, not something this feature turns on itself.

## Backend API addition

### `GET /api/dashboard/summary`

- `jwt_required()` only — no role restriction, so VIEWER/OPERATOR/ADMIN all
  see the same dashboard. Matches the existing "monitor" visibility level
  already used by `/api/devices` and `/api/chat/messages`.
- No query parameters; the payload is intentionally small and fixed-shape.
- Response:

```jsonc
{
  "devices": {
    "by_role": {
      "core": {"total": 1, "online": 1, "offline": 0, "unknown": 0},
      "distribution": {"total": 2, "online": 2, "offline": 0, "unknown": 0},
      "access": {"total": 2, "online": 1, "offline": 1, "unknown": 0}
    }
  },
  "ospf": [
    {
      "device_id": 2,
      "hostname": "INTERNAL-RTR",
      "role": "core",
      "health": "ok",
      "neighbor_count": 2,
      "full_count": 2,
      "neighbors": [
        {"neighbor_id": "3.3.3.3", "state": "FULL/DR", "interface": "GigabitEthernet0/1"}
      ],
      "snapshot_at": "2026-07-31T10:00:00+00:00"
    }
  ],
  "changes": {
    "pending_approval": [ /* ChangeRequest.to_dict(), newest first, limit 20 */ ],
    "recent": [ /* ChangeRequest.to_dict(), any status, newest first, limit 10 */ ]
  },
  "audit": {
    "recent": [ /* AuditLog.to_dict(), newest first, limit 20 */ ]
  },
  "generated_at": "2026-07-31T10:00:05+00:00"
}
```

### `network_copilot/dashboard/service.py`

```
build_summary() -> dict
```

Composed entirely from existing service functions — no new SQL:

- `devices.service.list_devices()` grouped by `role`, counted by `status`
  (`online`/`offline`/`unknown`) → `devices.by_role`.
- For each device whose `role` is in `monitoring.service.ROUTING_ROLES`
  (`core`, `distribution`): `monitoring.service.latest_snapshot(device.id)`.
  Read `parsed_data.get("show ip ospf neighbor")` to build `neighbors` and
  `neighbor_count`; `full_count` is how many have `"FULL"` in `state`.
  Classify `health` (see below). `snapshot_at` is the snapshot's
  `created_at`, or `null` if there is no snapshot yet.
- `changes.service.list_changes(status="pending_approval", limit=20)` and
  `changes.service.list_changes(limit=10)` → `changes.pending_approval` /
  `changes.recent`.
- `audit.service.list_events(limit=20)` → `audit.recent`.
- `generated_at` is `datetime.now(timezone.utc)` at call time.

### OSPF health classification

Given a device's latest snapshot (or none):

| Case | `health` |
|---|---|
| No snapshot ever, or latest snapshot's `status` is not `"online"`, or `parsed_data` has no `"show ip ospf neighbor"` key | `no_data` |
| Has the key, `neighbor_count == 0` | `down` |
| `neighbor_count > 0`, at least one neighbor's `state` does not contain `"FULL"` | `degraded` |
| `neighbor_count > 0`, all neighbors' `state` contain `"FULL"` | `ok` |

This is a direct function of stored data only — no per-device "expected
neighbor count" is introduced (see Non-goals).

### `network_copilot/dashboard/routes.py`

One route:

```python
@bp.get("/summary")
@jwt_required()
def summary():
    return jsonify(service.build_summary()), 200
```

Registered in `app.py` alongside the other blueprints, url_prefix
`/api/dashboard`.

## Frontend design

### Page

`GET /dashboard` (new Flask route in `app.py`, `render_template
("dashboard.html")`), guarded client-side the same way `/` already is: on
load, read the token from `localStorage`, call `GET /api/auth/me` to
validate; if invalid/absent, show the same login form pattern as the chat
page (a user could land on `/dashboard` directly without having logged in
via `/` first).

### Layout — four panels in a grid (reusing `app.css` tokens: `.sidebar`,
`.status-dot`, `.risk-pill`, `.status-pill` etc.; new dashboard-specific
rules added to `app.css` rather than a second stylesheet, to keep one shared
source of visual truth):

1. **Device status by role.** One row per role from `devices.by_role`:
   role name, and online/offline/unknown counts each with the existing
   `.status-dot` colors.
2. **OSPF health.** One row per entry in `ospf`: hostname, a health badge
   (`ok`=green, `degraded`=amber, `down`/`no_data`=grey/red, reusing the
   `.status-pill` styling with new modifier classes), neighbor count, and an
   expandable/inline list of neighbor id + state + interface.
3. **Changes.** Two short lists — "Pending approval" and "Recent" — each row
   showing device hostname, command summary, risk pill, status pill. Each row
   links to `/` (the chat page) rather than exposing Approve/Apply here.
4. **Audit feed.** A simple reverse-chronological list: timestamp, username,
   action, result, device (when present) — same fields already returned by
   `AuditLog.to_dict()`.

### Refresh

`dashboardApp()` calls `GET /api/dashboard/summary` once on load and every
10 seconds via `setInterval`, using a generation counter (same pattern as
`app.js`'s `_deviceRefreshGeneration`) so a slow response arriving after a
newer one has already landed is discarded instead of overwriting fresher
data.

### Empty/error states

- If `MONITORING_ENABLED` is off (or a device has simply never been polled),
  its `ospf` entry is `health: "no_data"`. The panel renders a small note —
  "Chưa có dữ liệu OSPF — kiểm tra MONITORING_ENABLED" — instead of treating
  this as an error.
- If a poll of `/api/dashboard/summary` itself fails (network error, 401),
  the page keeps showing the last successful data and shows a small inline
  banner: "Không tải được dữ liệu mới nhất lúc HH:MM:SS", retried
  automatically on the next interval tick. A 401 specifically clears the
  session and returns to the login form, matching the existing app-wide
  401 handling convention.

## Testing

**Backend** (`tests/dashboard/`), TDD as used throughout this project — tests
written and confirmed failing before implementation:

- `build_summary()` device role rollup: counts by role and status match a
  seeded set of devices with mixed `status` values.
- `build_summary()` OSPF classification: one fixture per case (`no_data` —
  no snapshot; `no_data` — snapshot present but no ospf key; `down` — zero
  neighbors; `degraded` — mixed states; `ok` — all `FULL`).
- `build_summary()` only includes `core`/`distribution` devices in `ospf`,
  never `access`.
- `build_summary()` changes/audit passthrough: seeded changes/audit rows
  appear in the right bucket (`pending_approval` vs `recent`), newest first,
  respecting the limits.
- `GET /api/dashboard/summary` requires authentication but not a specific
  role — a VIEWER token succeeds.
- Full existing suite re-run after adding the new blueprint registration in
  `app.py`, to confirm no regression.

**Frontend.** No JavaScript test framework introduced, matching the chat UI
spec's precedent. Verification is manual in a real browser: log in, open
`/dashboard`, confirm all four panels render with real lab data, confirm the
page updates within ~10s of a change happening elsewhere (e.g. approving a
change on `/`), confirm the OSPF panel shows `no_data` correctly when
`MONITORING_ENABLED` is off and real data once it is on, confirm navigation
links between `/` and `/dashboard` work, confirm a 401 (expired token)
returns to the login form.

## Rollout

Purely additive: one new module, one new route, one new template/static
pair, one edit to `app.py` to register the blueprint and the `/dashboard`
route, and a small addition to `app.css`. No new dependency, no migration
(no new table). Deployment is the same process already used for every prior
change on the AI Server node: `git pull` → `flask db upgrade` (no-op here,
but run for consistency) → restart the Flask process.

One additional operational step, not part of the code change itself: set
`MONITORING_ENABLED=true` (and optionally lower `MONITORING_INTERVAL_SECONDS`
below its 60s default) in the AI Server node's `.env`, so the dashboard has
continuously fresh data to display rather than a static snapshot from
whenever a device was last manually refreshed.
