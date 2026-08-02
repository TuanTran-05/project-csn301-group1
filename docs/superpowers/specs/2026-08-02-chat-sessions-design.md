# Chat Sessions — Design Spec

**Date:** 2026-08-02
**Status:** Approved for planning

## Goal

Replace the single, ever-growing shared chat log with real, named chat
sessions — the same mental model ChatGPT/Claude use (a list of past
conversations you can switch between, "New chat" starts a fresh one) —
while keeping this app's core design principle intact: the chat stays
**shared across the whole team**, not private per user. Every session and
every message in it is visible to any authenticated user; only *which
session your own browser is currently looking at* is a per-browser
preference.

This replaces the `localStorage`-only cutoff mechanism shipped earlier in
this same day's work (`chatCutoff`/`visibleMessages`/`startNewChat`/
`showFullHistory`, plus the change/batch-action-card exemption patched on
top of it). That approach was a display filter, not a real session model,
and could not give two different users a shared, named session list — it
also required a special-case exemption to keep change/batch action cards
visible, which a real session model makes unnecessary: those live in the
"Thay đổi đang chờ" panel, which was never session-scoped to begin with.

## Non-goals (explicitly out of scope for this iteration)

- Per-user private sessions. Explicitly rejected during brainstorming — the
  whole point of this app's chat is a shared team view.
- Renaming or deleting sessions. Confirmed during brainstorming: view and
  switch only, for this iteration.
- Any change to the "Thay đổi đang chờ" (pending changes/batches) panel or
  its underlying `/api/changes`, `/api/change-batches` endpoints. That
  panel already shows global, team-wide state independent of chat
  sessions, and stays exactly as it is.
- Cleaning up empty sessions (a session created via "New chat" that never
  received a message). Not addressed this iteration.
- Any concept of a server-side "current session for the team." Each
  browser tracks its own currently-viewed session; nothing forces one
  user's session switch onto anyone else.

## Data model

New table, `chat_sessions`:

| Column | Type | Notes |
|---|---|---|
| `id` | int, PK | |
| `created_at` | datetime | indexed |
| `created_by_id` | int, FK → users, `ondelete=SET NULL` | who clicked "New chat"; nullable, matches `chat_messages.user_id`'s existing pattern |

No `title` column: a session's display title is *derived*, not stored —
the content of its earliest message, truncated to 60 characters (with a
trailing `…` if truncated), or the literal string `"New chat"` if the
session has no messages yet. Computing this at read time (a single query
in `list_sessions()`, not a write-time side effect) means sending a
message never needs to also touch the session row, and a session's title
naturally "fills in" the first time something is said in it — exactly
ChatGPT's behavior.

`chat_messages` gains one new column: `session_id` (int, FK →
`chat_sessions.id`, `ondelete=CASCADE`, indexed, **not nullable** after
migration).

### Migration strategy

This is the one genuinely tricky part: existing `chat_messages` rows have
no session to belong to. The migration:

1. `ALTER TABLE chat_messages ADD COLUMN session_id INTEGER` (nullable, no
   default — a normal Alembic `op.add_column`).
2. Insert exactly one row into `chat_sessions` (no `created_by_id`, so it
   reads as system-created) — call it the *migration session*.
3. `UPDATE chat_messages SET session_id = <migration session's id> WHERE
   session_id IS NULL` — every pre-existing message becomes part of that
   one session, so nothing is lost; a user who existed before this change
   ships will see all of today's chat history the first time they open the
   session list.
4. `ALTER COLUMN session_id SET NOT NULL` and add the FK/index — safe now
   that every row has a value.

Steps 2-3 are a data migration (raw SQL or SQLAlchemy Core `execute()`
inside the Alembic script's `upgrade()`), not a schema-only change — this
project has not needed one before, so the implementation task must show
the exact code, not just describe it.

## Backend API

- **`POST /api/chat/sessions`** — creates a session (`created_by_id` = the
  caller). No request body needed. `jwt_required()` only, matching the
  existing "monitor" visibility level (any authenticated role). Returns
  `{id, title, created_at}` (`title` is `"New chat"` for a session with no
  messages, i.e. always, since it was just created).
- **`GET /api/chat/sessions`** — lists every session, most-recently-active
  first (ordered by the `MAX(created_at)` of its messages, falling back to
  the session's own `created_at` when it has none — so a just-created empty
  session still sorts to the top). `jwt_required()` only. Returns
  `{"items": [{id, title, created_at}, ...]}`.
- **`GET /api/chat/messages`** — gains a required query parameter,
  `session_id` (int). Missing or non-numeric raises the existing
  `ValidationError` (422) convention used elsewhere in this codebase (e.g.
  `/api/audit-logs`'s timestamp parsing). Behavior otherwise unchanged:
  same `limit` param, same ordering, same `jwt_required()`-only access.
- **`POST /api/ai/chat`** — `ChatRequest` gains a required field,
  `session_id: int`. Every call — success or failure — records its
  `user`/`assistant`/`system` message(s) against that session, exactly as
  today but with `session_id` now included in `chat.service.record_message`'s
  signature (an added required parameter, not optional — every message
  belongs to exactly one session from this point on).

## Frontend design

### Removed

The three pieces added earlier today: `chatCutoff` state,
`visibleMessages`/`hiddenMessageCount` getters, `startNewChat()`/
`showFullHistory()` methods, the `.chat-toolbar`/`.new-chat-btn`/
`.chat-hidden-banner` template markup and CSS, and the change/batch
payload exemption inside `visibleMessages`. All superseded by real
sessions; keeping the old filter alongside real sessions would be two
competing hiding mechanisms for the same problem.

### Added

- `sessions: []` and `currentSessionId` (state). `currentSessionId`
  initializes from `localStorage.getItem("nc_session_id")` — a per-browser
  bookmark of which session this browser was last looking at, the same
  role `nc_token`/`nc_user` already play for auth state. It is **not**
  sent anywhere that would make it a server-wide concept.
- On `startApp()`: fetch `GET /api/chat/sessions`. If the list is empty
  (first-ever use of the feature, or a fresh migration with zero legacy
  messages — impossible in practice given the migration always creates
  one, but the client must not assume a non-empty list), create one via
  `POST /api/chat/sessions` and use it. Otherwise, use
  `currentSessionId` if it still appears in the fetched list, else default
  to the most recent session (`sessions[0]`, since the list is already
  sorted newest-first). Persist the resolved id back to `localStorage`.
  Then hydrate messages for that session.
- **Session list UI**: a new small panel/dropdown listing `sessions`
  (title, relative or short time), each clickable. Clicking a session:
  bumps `_messagesRefreshGeneration` (invalidating any in-flight fetch for
  the previously active session, same pattern already used for the
  device/changes refresh generations), sets `currentSessionId`, persists
  it to `localStorage`, clears `messages`, and calls `hydrateMessages()`
  for the new session.
- **"New chat" button**: calls `POST /api/chat/sessions`, unshifts the
  result into `sessions`, then performs the same switch-session sequence
  above (targeting the new session, whose message list is empty by
  construction — no fetch is even necessary, but reusing the same code
  path keeps this simple rather than a special case).
- `hydrateMessages()`, `pollMessages()`, and `sendMessage()` all read
  `this.currentSessionId` at call time (not a value captured once at
  startup) and pass it as the `session_id` query/body parameter, so a
  session switch is picked up by whichever of these fires next — including
  an in-flight poll interval, which continues running unmodified and just
  targets whatever `currentSessionId` currently is.

## Testing

**Backend** (`tests/chat/`), TDD as used throughout this project:

- `ChatSession.to_dict()` includes every field.
- `list_sessions()` sorts by most-recent-message time, falling back to
  session `created_at` for a session with no messages; a session with a
  message sorts above one created earlier but never used.
- A session's derived title: `"New chat"` with no messages, the first
  message's content (verbatim, under 60 chars) with one message, truncated
  with `…` for a longer first message.
- `POST /api/chat/sessions` requires authentication but not a specific
  role; returns the new session.
- `GET /api/chat/sessions` requires authentication; returns sessions
  ordered as above.
- `GET /api/chat/messages` without `session_id` returns 422; with it,
  returns only that session's messages (a second session's messages must
  not leak in).
- `POST /api/ai/chat` without `session_id` returns 422 (schema
  validation); with it, both the recorded `user` and `assistant` rows
  carry that `session_id`.
- No automated test covers the migration script's `upgrade()`/`downgrade()`
  directly — every test in this project builds its schema straight from
  the SQLAlchemy models via `db.create_all()` (see `tests/conftest.py`),
  bypassing Alembic entirely, and none of the prior migrations have a test
  either. This one is verified manually instead: run `flask db upgrade`
  against a copy of the real (or seeded-lab) database that has pre-existing
  `chat_messages` rows, then confirm every row now has a `session_id`
  pointing at one newly-created session and the column rejects a `NULL`
  insert.

**Frontend.** No JavaScript test framework, matching every prior
iteration. Manual verification in a real browser: "New chat" creates and
switches to an empty session that appears at the top of the session list;
sending a message in it gives the session a real title; switching to an
older session shows that session's own history only; a change/batch
approved from the sidebar still shows its outcome regardless of which
chat session is currently active, since that panel was never session-scoped;
refreshing the page returns to the same session (via the `localStorage`
bookmark); two different browser sessions (simulating two team members)
both see the same session list and the same messages within a shared
session.

## Rollout

Requires a migration (the data-backfill one described above) and a
backend restart, plus the frontend files. Deployment is the same as every
prior change on the AI Server node: `git pull` → `flask db upgrade` →
restart the Flask process. Unlike every purely-additive change earlier
today, this one is not risk-free to roll back: reverting the code after
the migration has run would leave `chat_messages.session_id` NOT NULL
while old code never sets it, breaking every future insert. If a rollback
is ever needed, the migration's `downgrade()` must be run first.
