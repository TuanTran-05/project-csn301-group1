# Conversational Chat Intent — Design Spec

**Date:** 2026-08-02
**Status:** Approved for planning

## Goal

Let the AI copilot answer plain conversational messages — a greeting like
"alo", a question about what it can do, or a general networking-knowledge
question like "OSPF là gì?" — the way a normal chatbot would, instead of
returning an error. Every existing network-action behavior
(monitor/configure/troubleshoot, the policy engine, the approval workflow)
is unchanged.

Today `AIService.interpret()` treats an empty `operations` list as a
deliberate refusal and raises `ValidationError`
(`ai/service.py:183-187`), which becomes an HTTP 422 and renders as a red
error bubble. So a greeting is not "unanswered" — it is *misclassified as a
failure*.

## Non-goals (explicitly out of scope for this iteration)

- A general-purpose assistant. Confirmed during brainstorming: the scope is
  greetings, questions about the copilot's own capabilities, and general
  networking knowledge. Anything outside that (weather, homework unrelated
  to networking, coding help) is politely declined.
- Any change to the policy engine, the change/batch workflow, or the
  approval rules. A `chat` turn produces no commands and never opens an SSH
  session, so none of those code paths are reached.
- Any frontend change. The chat bubble already renders
  `payload.explanation` as its content, and the operational-results block
  and action card are conditional on `payload.results` / `payload.change` /
  `payload.batch`, none of which a chat response carries. A chat turn
  therefore renders as an ordinary assistant bubble with no template edit.
- Streaming responses, typing indicators, or any other chat-UX polish.
- Persisting conversation summaries or long-term memory beyond the recent
  window described below.

## Architecture

One new intent value, `"chat"`, in the existing single-call structured
response. The model classifies the message itself; no extra API call, no
added latency, and no second prompt to keep in sync.

### Schema changes (`ai/schemas.py`)

- `AIAction.intent` gains `"chat"`:
  `Literal["monitor", "configure", "troubleshoot", "chat"]`.
- `AIAction.operations` drops its unconditional `min_length=1` and gains a
  `model_validator(mode="after")` enforcing the real rule:
  - `intent == "chat"` → `operations` **must be empty**.
  - any other intent → `operations` **must have at least one entry**.

  Encoding it as a validator rather than a field constraint is what lets
  one model serve both shapes while keeping the action intents exactly as
  strict as they are today.
- `build_ai_action_schema()` adds `"chat"` to the `intent` enum it sends to
  the provider. Its `operations` array already declares `minItems: 0`, so
  no other change is needed there.

### Service changes (`ai/service.py`)

- `interpret()`'s refusal branch becomes conditional: an empty
  `operations` list is a refusal **only when the intent is not `"chat"`**.
  For `"chat"` it is the expected shape and passes through to `AIAction`.
- `handle()` dispatches `"chat"` before any device resolution, returning:

  ```python
  {"intent": "chat", "explanation": action.explanation,
   "requires_approval": False}
  ```

  No device lookup, no policy evaluation, no SSH client, no change or
  batch creation.
- `handle()` gains an optional `session_id: int | None = None` parameter,
  passed through from `ai/routes.py` as `data.session_id`. Keeping it
  optional means every existing caller and test that passes only
  `(message, user_id)` keeps working unchanged.

### System prompt changes

Two additions to `SYSTEM_PROMPT`:

1. **When to use `chat`** — greetings, small talk, questions about what the
   assistant can do, and general/theoretical networking knowledge. For
   `chat`, `operations` must be empty and `explanation` carries the actual
   answer.
2. **The anti-hallucination rule (the important one)** — any question about
   the *actual current state* of a device in this lab must use
   `monitor` or `troubleshoot` and run a real command. `chat` must never be
   used to describe, guess at, or invent live device state. A question the
   assistant cannot answer from general knowledge, and that is outside
   networking, gets a short `chat` reply saying so rather than a fabricated
   answer.

The existing "keep explanation to a single short sentence" instruction is
scoped to the action intents; a `chat` explanation may be a short
paragraph, since it is the answer itself rather than a label for a
proposal.

### Conversation history

A new private helper, `AIService._recent_history(session_id)`, returns the
recent turns of the current session for inclusion in the model context:

- Source: `chat.service.list_messages(session_id=session_id, limit=40)`,
  already session-scoped and already ordered oldest-first. The raw fetch is
  deliberately wider than the final window because the filters below remove
  rows; fetching exactly 10 could yield fewer than 10 usable turns.
- Window: after filtering, keep the **last 10** messages (oldest-first
  within that window).
- **Only `role` and `content` are sent. `payload` is never sent.** Message
  payloads carry raw command output, which can contain configuration
  detail; forwarding them would violate this module's standing design rule
  that the model never receives credentials, management IPs, or a full
  running-config. This is asserted by a test, not just by convention.
- `system` messages (error notices) are excluded — they are UI feedback,
  not conversation.
- The current message is excluded. `ai/routes.py` records the user's
  message *before* calling `handle()`, so it is already the newest row;
  including it would send the same text to the model twice. The helper
  drops a trailing `user` message whose content equals the message being
  handled.
- `session_id is None` (any caller that does not supply one) yields an
  empty list, so behavior is exactly as it is today.

The result is added to the context dict returned by `build_context()` under
a `"conversation"` key. `build_context()` gains the same optional
`session_id` parameter to do this.

## Audit logging

**A pure `chat` turn is deliberately not written to `audit_logs`.**

`audit_logs` exists to trace operations performed against devices; a
greeting performs none. The full conversation — question and answer alike —
is already persisted in `chat_messages` and is queryable there, so nothing
is lost for accountability. Writing chat turns to the audit log would also
dilute the dashboard's "recent activity" feed with greetings, hiding the
real operational events it exists to surface.

The `ai.action` audit events for monitor/configure/troubleshoot are
unchanged.

## Testing

**Backend** (`tests/ai/`), TDD as used throughout this project:

Schema (`tests/ai/test_ai.py`):
- `AIAction` accepts `intent="chat"` with an empty `operations` list.
- `AIAction` rejects `intent="chat"` carrying a non-empty `operations` list.
- `AIAction` still rejects each of `monitor`/`configure`/`troubleshoot`
  with an empty `operations` list — the existing strictness must survive
  the validator rewrite.
- `build_ai_action_schema()` includes `"chat"` in its `intent` enum.

Service behavior:
- `handle()` with a chat action returns `intent="chat"` and the model's
  explanation, and carries no `results`, `change`, or `batch` key.
- `handle()` with a chat action never opens an SSH session — asserted
  against the `ssh_factory` stub recording zero calls.
- `handle()` with a chat action creates no `ChangeRequest` and no
  `ChangeBatch` row.
- A chat turn writes no `AuditLog` row (the deliberate decision above,
  pinned by a test so it cannot regress silently).
- `interpret()` still raises `ValidationError` for an empty `operations`
  list when the intent is `monitor`/`configure`/`troubleshoot` — the
  refusal path is narrowed, not removed.

Conversation history:
- `_recent_history()` returns only `user` and `assistant` messages, oldest
  first, from the given session only (a second session's messages must not
  leak in).
- It returns at most 10 messages.
- It drops a trailing `user` message identical to the message being
  handled.
- It returns `[]` when `session_id` is `None`.
- The history reaches the provider: asserted via `FakeAIProvider.prompts`.
- **Security:** a stored message whose `payload` contains a sentinel secret
  string must not appear anywhere in what is sent to the model — asserted
  with the existing `FakeAIProvider.everything_sent()` helper, the same way
  the credential-hygiene tests already work.

Prompt content:
- `SYSTEM_PROMPT` states that live device state must go through
  monitor/troubleshoot and never through `chat` — a string assertion, so
  the anti-hallucination rule cannot be dropped from the prompt unnoticed.

**Frontend.** No JavaScript test framework, matching every prior
iteration, and no frontend code changes in this feature. Manual
verification in a real browser: send "alo" and confirm a normal assistant
bubble (not a red error bubble) with a friendly reply; ask "OSPF là gì?"
and confirm a substantive answer with no command execution; ask a real
monitor question ("kiểm tra OSPF trên DIST-SW1") and confirm it still runs
the command and renders the result table; ask a follow-up that depends on
the previous turn and confirm the answer reflects that context.

## Rollout

Purely additive backend change: no migration, no new dependency, no
frontend edit. Deployment is the same as every prior change on the AI
Server node: `git pull` → restart the Flask process.
