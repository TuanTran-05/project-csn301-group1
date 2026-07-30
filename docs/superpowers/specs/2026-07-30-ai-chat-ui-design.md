# AI Chat UI — Design Spec

**Date:** 2026-07-30
**Status:** Approved for planning

## Goal

Give ADMIN and IT staff a web page where they log in, chat with the AI copilot
in plain Vietnamese or English, and — for changes the AI proposes — approve
and apply them without leaving the page. The page is a shared team view: any
authenticated user sees the same chat history, device list, and pending
changes, so nobody duplicates work or misses what a teammate just asked.

This spec covers one cohesive feature (login → chat → approve/apply, backed
by one new persisted resource). It does not introduce unrelated subsystems.

## Non-goals (explicitly out of scope for this iteration)

- Editing or deleting chat messages
- Rich markdown rendering (plain text is enough)
- Real-time push (WebSocket/SSE) — periodic polling is sufficient at this
  team's scale
- Searching or filtering chat history
- Mobile-specific responsive design (must not visibly break on a laptop
  screen; no further polish required)
- A JavaScript test framework or build tooling of any kind

## Architecture

- **Backend:** a new `network_copilot/chat/` module (`model.py`, `service.py`,
  `routes.py`), following the same shape as every other domain module in this
  codebase (`audit/`, `changes/`, etc.).
- **Frontend:** a single HTML page served directly by Flask
  (`templates/index.html` + `static/`), using Alpine.js for reactivity.
  Alpine.js is vendored into `static/vendor/alpine.min.js` — never loaded from
  a CDN, since this node's internet reachability has been unreliable during
  this project's setup.
- No Node.js, no build step, no new frontend dependency beyond the one
  vendored Alpine.js file. The page deploys as part of the existing Flask
  process — nothing new to install on the AI Server node.

## Data model

New table `chat_messages`:

| Column | Type | Notes |
|---|---|---|
| `id` | int, PK | |
| `created_at` | datetime | indexed, ascending order for display |
| `user_id` | int, FK → users, `ondelete=SET NULL` | who sent it (null for messages sent after the user is deleted) |
| `username` | string | snapshot at write time, so history survives a deleted account — same pattern as `audit_logs.username` |
| `role` | string | one of `user`, `assistant`, `system` |
| `content` | text | display text: the user's message, the AI's explanation, or the blocked/error reason |
| `payload` | JSON, nullable | the raw structured data — `AIAction` fields, `results`, or a `change` object with `status`/`id` — used by the frontend to render an inline preview/action card |

`role=system` covers everything `POST /api/ai/chat` returns as a non-2xx
response (blocked command, AI provider failure, rate limit) — these are real
conversation events a returning user should still see, not just a transient
error toast.

## Backend API additions

### `GET /api/chat/messages`

- `jwt_required()` only — no role restriction, matching the existing
  "monitor" visibility level (VIEWER and OPERATOR can read, not just ADMIN).
- Query param `limit` (default 200, capped at 500, same convention as
  `/api/audit-logs` and `/api/commands/history`).
- No cursor/`since_id` parameter. At this team's scale, the client simply
  re-fetches the most recent `limit` messages on each poll and dedupes by
  `id` client-side. Do not add pagination complexity beyond this.
- Response: `{"items": [{id, username, role, content, payload, created_at}]}`,
  ascending by `created_at`.

### `network_copilot/chat/service.py`

```
record_message(user_id, username, role, content, payload=None) -> ChatMessage | None
```
Takes `user_id`/`username` as separate primitives — the same convention
`audit.service.record_event` already uses — rather than a `User` object, so
a caller that only has an id (or no resolved user at all) can still call it.
Must never raise: wrap the write in try/except and log on failure, exactly
like `record_event` already does — a failure to persist chat history must
never break the AI response the user is waiting for.

```
list_messages(limit=200) -> list[ChatMessage]
```

### Modification to `network_copilot/ai/routes.py`

`ai/service.py` is not modified at all — this keeps the existing, heavily
tested AI logic untouched. The `chat()` view wraps the existing call:

```python
user = current_user()
user_id = user.id if user else None
username = user.username if user else None
chat_service.record_message(user_id, username, "user", data.message)
try:
    result = AIService().handle(data.message, user_id)
except AppError as exc:
    chat_service.record_message(user_id, username, "system", exc.message, {"error": exc.error})
    raise
chat_service.record_message(user_id, username, "assistant", result.get("explanation", ""), result)
return jsonify(result), 200
```

`user is None` is an existing, already-tolerated edge case (the JWT identity
no longer resolves to a row, e.g. the account was deleted mid-session) —
`chat_service.record_message` must accept `user_id=None, username=None` and
simply store nulls, not raise.

A blocked command, an AI provider failure, and a normal monitor/configure/
troubleshoot result are all captured in the shared transcript, regardless of
whether the HTTP response was 200 or an error status.

### Migration

One new table. Standard `flask db migrate` + `flask db upgrade`, same as
every prior schema change in this project.

## Frontend design

### Screens

Two states inside one page, toggled by Alpine.js (`x-show`), no client-side
router needed:

**Login.** Username + password → `POST /api/auth/login`. On success, store
`access_token` and the user object (`username`, `role`) in `localStorage`,
switch to the main screen. On failure, show the error inline under the form.

**Main screen**, three columns:

- **Left — Devices.** Polls `GET /api/devices` every 15s. Each row: hostname,
  role, a status dot (green=online, red=offline, grey=unknown).
- **Right — Pending changes.** Polls `GET /api/changes?status=pending_approval`
  every 15s. Each row: device hostname, commands summary, risk level badge,
  and Approve / Apply / Cancel buttons — shown only when
  `currentUser.role === 'ADMIN'` (client-side UX convenience only; the
  backend's `roles_required` decorator remains the actual security boundary).
- **Center — Chat.** On load, fetch `GET /api/chat/messages` once to hydrate
  the visible history, then poll the same endpoint every ~7s to pick up
  messages from other users, appending only messages with an `id` not
  already rendered. Auto-scroll to the newest message only if the user is
  already scrolled to the bottom (don't yank their scroll position if they
  scrolled up to read history).

### Message bubbles

Styled by `role`: `user` right-aligned, `assistant` left-aligned, `system`
centered/muted. When an `assistant` message's `payload` contains a `change`
object, render an inline action card inside that bubble (device, commands,
risk level, Approve/Apply buttons, admin-only as above).

**Live status, not a frozen snapshot.** The action card must not render its
Approve/Apply state from the `payload` captured at message-creation time.
Instead, all change-related UI (the sidebar list and every inline chat card)
reads from one shared client-side map keyed by `change_id`, kept in sync by
whichever polling cycle or button click last touched that change. This way,
approving a change from the sidebar is reflected in its chat card too (and
vice versa), and other users polling see the update within ~15s. Implementing
the chat card against the frozen `payload` instead of this shared map is the
one mistake most likely to produce visibly inconsistent state between the
sidebar and the chat column — call this out explicitly in the implementation
plan so it is not discovered as a bug after the fact.

### Sending a message

Input box + Send button. While waiting for `POST /api/ai/chat` to return,
disable the input and show a transient "Đang xử lý..." bubble in the
assistant's position. On a non-2xx response, render the JSON error body's
`message` field immediately as a `system`-styled bubble — the same event is
durably recorded server-side (Backend API additions, above), but the UI does
not wait for the next poll cycle to show it.

### Auth lifecycle

On page load, read the token from `localStorage`; if present, call
`GET /api/auth/me` to validate it and fetch the current user. On any `401`
from any endpoint (expired/invalid token), clear `localStorage`, stop all
polling intervals, and return to the login screen. Logout button does the
same, deliberately.

## Testing

**Backend** (`tests/chat/`), TDD as used throughout this project — tests
written and confirmed failing before implementation:

- `record_message` persists each role correctly and never raises, even when
  the underlying write fails.
- `GET /api/chat/messages` requires authentication but not a specific role
  (VIEWER and OPERATOR can read).
- Calling `POST /api/ai/chat` with a monitor intent produces one `user` row
  and one `assistant` row.
- Calling it with a blocked command produces one `user` row and one `system`
  row whose `payload.error` matches the policy violation.
- The full existing suite (416 tests as of this spec) is re-run after the
  `ai/routes.py` edit to confirm no regression — `ai/service.py` itself is
  untouched, so this is a low-risk change, but it must be verified rather
  than assumed.

**Frontend.** No JavaScript test framework is introduced — adding one would
require Node.js/npm purely for test tooling, contradicting the reason this
approach was chosen (no build step, low resource use on the AI Server node).
Verification is manual, in a real browser, before the feature is reported
complete: login → send a monitor-intent message → send a configure-intent
message → Approve then Apply from inside the chat card → confirm the sidebar
reflects the applied change → logout. This matches the project's existing
rule that UI changes must be exercised in a browser, not just covered by
passing unit tests.

## Rollout

Purely additive: one new module, one new migration, one small edit to an
existing, well-tested route, and new template/static files. Deployment is
the same process already used for every prior change on the AI Server node:
`git pull` → `pip install -e ".[dev]"` (only if a new dependency was added,
which it is not) → `flask db upgrade` → restart the Flask process. No new
software (Node.js, a database, a queue) is introduced on that node.
