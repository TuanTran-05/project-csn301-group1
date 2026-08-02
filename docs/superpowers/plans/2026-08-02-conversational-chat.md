# Conversational Chat Intent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fourth `chat` intent so greetings and general networking questions get a normal assistant reply instead of a 422 error bubble, plus a 10-message per-session conversation window for follow-up questions.

**Architecture:** The model classifies each message itself inside the existing single structured call — no extra API round-trip. `intent: "chat"` carries an empty `operations` list and puts the actual answer in `explanation`; `AIService.handle()` returns it before any device resolution, so no policy check, SSH session, change, batch, or audit row is ever created. Recent session turns (role + content only, never payloads) are added to the model context.

**Tech Stack:** Pydantic v2 (`model_validator`), Flask, the existing Gemini provider — no new dependency.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-02-conversational-chat-design.md` — this plan implements it, with one factoring deviation noted below.
- **Deviation from the spec's wording:** the spec says "`build_context()` gains the same optional `session_id` parameter". This plan instead leaves `build_context()`'s signature completely untouched and has `interpret()` add the `"conversation"` key to the context dict it already builds. Reason: `interpret()` already holds the `message`, which `_recent_history()` needs in order to drop the duplicate of the current turn — routing it through `build_context()` would mean threading a second unrelated parameter (`current_message`) into a method whose one job is "what the model may know about the lab". The resulting context payload sent to the provider is byte-for-byte the same either way.
- No frontend change of any kind. The chat bubble already renders `payload.explanation`, and the results table / action card are conditional on `payload.results` / `payload.change` / `payload.batch`, which a chat response never carries.
- No migration, no new dependency.
- A pure `chat` turn must write **no** `AuditLog` row — a deliberate decision recorded in the spec's Audit logging section, and pinned by a test in Task 2 so it cannot regress silently.
- Message `payload` values must never reach the model. Only `role` and `content` are forwarded.
- Use the project's Python 3.13 venv (`../.venv/Scripts/python.exe` from `backend/`) for every command.

---

### Task 1: Schema — add the `chat` intent

**Files:**
- Modify: `backend/src/network_copilot/ai/schemas.py:22-33` (the `AIAction` model) and `:63-66` (the `intent` enum inside `build_ai_action_schema`)
- Test: `backend/tests/ai/test_ai.py` (append)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `AIAction` accepting `intent="chat"` with an empty `operations` list (and rejecting every other combination); `build_ai_action_schema()` emitting `"chat"` in its `intent` enum. Task 2 depends on both.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/ai/test_ai.py`:

```python
CHAT_ACTION = {
    "intent": "chat",
    "operations": [],
    "explanation": "Chao ban! Toi co the giup kiem tra va cau hinh thiet bi mang.",
}


def test_ai_action_accepts_a_chat_intent_with_no_operations():
    action = AIAction(**CHAT_ACTION)
    assert action.intent == "chat"
    assert action.operations == []


def test_ai_action_rejects_a_chat_intent_carrying_operations():
    payload = deepcopy(CHAT_ACTION)
    payload["operations"] = [
        {
            "device_hostnames": ["DIST-SW1"],
            "execution_mode": "exec",
            "commands": ["show ip ospf neighbor"],
            "verification_commands": [],
        }
    ]
    with pytest.raises(PydanticValidationError):
        AIAction(**payload)


@pytest.mark.parametrize("intent", ["monitor", "configure", "troubleshoot"])
def test_ai_action_still_rejects_an_action_intent_with_no_operations(intent):
    """Relaxing operations for chat must not relax it for the intents that
    actually run commands."""
    with pytest.raises(PydanticValidationError):
        AIAction(intent=intent, operations=[], explanation="x")


def test_provider_schema_offers_the_chat_intent():
    from network_copilot.ai.schemas import build_ai_action_schema

    schema = build_ai_action_schema(["DIST-SW1"])
    assert "chat" in schema["properties"]["intent"]["enum"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "chat_intent or chat_action or action_intent_with_no_operations or provider_schema_offers"` (from `backend/`)
Expected: FAIL — `test_ai_action_accepts_a_chat_intent_with_no_operations` raises `PydanticValidationError` because `"chat"` is not in the `intent` Literal, and `test_provider_schema_offers_the_chat_intent` fails its `assert` because the enum has only three values. (`test_ai_action_rejects_a_chat_intent_carrying_operations` and the parametrized one will already pass, for the wrong reason — the Literal rejects `"chat"` outright, and `min_length=1` rejects the empty list. Both must still pass after Step 3, when they start passing for the right reason.)

- [ ] **Step 3: Write the minimal implementation**

In `backend/src/network_copilot/ai/schemas.py`, find:

```python
class AIAction(BaseModel):
    """The only plan shape the model is allowed to return.

    The AI never executes anything: this object is a *proposal* that the policy
    engine and the change workflow then accept or refuse.
    """

    model_config = ConfigDict(extra="ignore")

    intent: Literal["monitor", "configure", "troubleshoot"]
    operations: list[AIOperation] = Field(min_length=1)
    explanation: str
```

Replace with:

```python
class AIAction(BaseModel):
    """The only plan shape the model is allowed to return.

    The AI never executes anything: this object is a *proposal* that the policy
    engine and the change workflow then accept or refuse.

    "chat" is the one intent that carries no operations: it is a plain
    conversational reply (a greeting, a capability question, general
    networking knowledge) whose answer is the explanation itself. Every
    other intent still requires at least one operation, so relaxing the
    field constraint is expressed as a validator rather than by weakening
    the field.
    """

    model_config = ConfigDict(extra="ignore")

    intent: Literal["monitor", "configure", "troubleshoot", "chat"]
    operations: list[AIOperation] = Field(default_factory=list)
    explanation: str

    @model_validator(mode="after")
    def validate_operations_match_intent(self):
        if self.intent == "chat":
            if self.operations:
                raise ValueError("a chat action must not carry operations")
        elif not self.operations:
            raise ValueError("at least one operation is required")
        return self
```

(`model_validator` is already imported at the top of this file — no import change needed.)

Then find, inside `build_ai_action_schema`:

```python
            "intent": {
                "type": "string",
                "enum": ["monitor", "configure", "troubleshoot"],
            },
```

Replace with:

```python
            "intent": {
                "type": "string",
                "enum": ["monitor", "configure", "troubleshoot", "chat"],
            },
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "chat_intent or chat_action or action_intent_with_no_operations or provider_schema_offers"`
Expected: PASS (6 tests — the 3 parametrized cases count separately)

Then run the whole AI schema/service file to confirm the validator rewrite broke nothing:

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/ai/schemas.py backend/tests/ai/test_ai.py
git commit -m "feat: allow a chat intent with no operations in AIAction"
```

---

### Task 2: Service — handle the `chat` intent

**Files:**
- Modify: `backend/src/network_copilot/ai/service.py:40-83` (`SYSTEM_PROMPT`), `:180-187` (the refusal branch in `interpret`), `:384-386` (the top of `handle`)
- Test: `backend/tests/ai/test_ai.py` (append)

**Interfaces:**
- Consumes: `AIAction` accepting `intent="chat"` with empty `operations` (Task 1).
- Produces: `AIService.handle()` returning `{"intent": "chat", "explanation": str, "requires_approval": False}` for a chat action. Task 3 extends the same `handle()` with a `session_id` parameter.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/ai/test_ai.py`:

```python
def test_handle_returns_a_chat_reply_without_touching_devices(
    app, admin_user, ssh_factory
):
    service, _ = service_with(app, CHAT_ACTION)

    result = service.handle("alo", admin_user.id)

    assert result["intent"] == "chat"
    assert result["explanation"] == CHAT_ACTION["explanation"]
    assert result["requires_approval"] is False
    assert "results" not in result
    assert "change" not in result
    assert "batch" not in result


def test_handle_chat_never_opens_an_ssh_session(app, admin_user, ssh_factory):
    service, _ = service_with(app, CHAT_ACTION)
    service.handle("alo", admin_user.id)
    assert ssh_factory.clients == {}


def test_handle_chat_creates_no_change_or_batch(app, admin_user, ssh_factory):
    from network_copilot.changes.model import ChangeBatch

    service, _ = service_with(app, CHAT_ACTION)
    service.handle("alo", admin_user.id)

    assert db.session.query(ChangeRequest).count() == 0
    assert db.session.query(ChangeBatch).count() == 0


def test_handle_chat_writes_no_audit_row(app, admin_user, ssh_factory):
    """Deliberate: audit_logs traces operations against devices, and a
    greeting performs none. The turn is still fully recorded in
    chat_messages."""
    from network_copilot.audit.model import AuditLog

    service, _ = service_with(app, CHAT_ACTION)
    service.handle("alo", admin_user.id)

    assert db.session.query(AuditLog).count() == 0


def test_interpret_still_refuses_an_empty_action_intent(app, admin_user):
    refusal = {
        "intent": "monitor",
        "operations": [],
        "explanation": "Khong tim thay thiet bi phu hop.",
    }
    service, _ = service_with(app, refusal)
    with pytest.raises(ValidationError):
        service.interpret("kiem tra thiet bi khong ton tai", admin_user.id)


def test_prompt_forbids_inventing_live_device_state_in_chat(app, admin_user):
    service, provider = service_with(app, CHAT_ACTION)
    service.interpret("alo", admin_user.id)
    prompt = provider.prompts[0]["system_prompt"]
    assert "invent the live state" in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "handle_chat or chat_reply or still_refuses or forbids_inventing"` (from `backend/`)
Expected: FAIL — the four `handle`/chat tests fail because `interpret()` still treats the empty `operations` list as a refusal and raises `ValidationError`; `test_prompt_forbids_inventing_live_device_state_in_chat` fails the same way before it ever reaches its assertion. (`test_interpret_still_refuses_an_empty_action_intent` already passes and must keep passing.)

- [ ] **Step 3: Write the minimal implementation**

In `backend/src/network_copilot/ai/service.py`, find this line inside `SYSTEM_PROMPT`:

```
  "intent": "monitor" | "configure" | "troubleshoot",
```

Replace with:

```
  "intent": "monitor" | "configure" | "troubleshoot" | "chat",
```

Then find, in the same prompt:

```
- Use "troubleshoot" when the user reports a problem. Put read-only diagnostic
  commands in "commands", not in "verification_commands".
```

Replace with:

```
- Use "troubleshoot" when the user reports a problem. Put read-only diagnostic
  commands in "commands", not in "verification_commands".
- Use "chat" for messages that are not a request to act on a device:
  greetings, small talk, questions about what you can do, and general or
  theoretical networking knowledge. For "chat", "operations" must be empty
  and "explanation" carries your actual answer, which may be a short
  paragraph rather than a single sentence.
- Never use "chat" to describe, guess at, or invent the live state of any
  device in this lab. Any question about what a device is actually doing
  right now must use "monitor" or "troubleshoot" and run a real command.
- If a question is outside networking entirely, reply with a brief "chat"
  answer saying it is outside what you cover. Never invent an answer.
```

Then find the last rule in the prompt:

```
- The user may write in Vietnamese or English. Reply with JSON either way, and
  keep "explanation" to a single short sentence.
```

Replace with:

```
- The user may write in Vietnamese or English. Reply with JSON either way. For
  "monitor", "configure" and "troubleshoot", keep "explanation" to a single
  short sentence.
```

Then find, in `interpret()`:

```python
        # A well-formed empty operation list is a deliberate refusal, not a
        # transient schema fault. Surface the model's explanation and do not
        # retry a real answer.
        if isinstance(payload.get("operations"), list) and not payload["operations"]:
            raise ValidationError(
                payload.get("explanation")
                or "The AI could not form a valid proposal for that request."
            )
```

Replace with:

```python
        # A well-formed empty operation list is a deliberate refusal, not a
        # transient schema fault. Surface the model's explanation and do not
        # retry a real answer. "chat" is the exception: an empty list is its
        # expected shape, and the explanation is the answer itself.
        if (
            payload.get("intent") != "chat"
            and isinstance(payload.get("operations"), list)
            and not payload["operations"]
        ):
            raise ValidationError(
                payload.get("explanation")
                or "The AI could not form a valid proposal for that request."
            )
```

Then find the top of `handle()`:

```python
    def handle(self, message: str, user_id: int | None) -> dict:
        action = self.interpret(message, user_id)

        if action.intent == "configure":
```

Replace with:

```python
    def handle(self, message: str, user_id: int | None) -> dict:
        action = self.interpret(message, user_id)

        # A conversational turn touches nothing: no device is resolved, no
        # policy is evaluated, no SSH session is opened, and no audit row is
        # written (the transcript in chat_messages is the record).
        if action.intent == "chat":
            return {
                "intent": "chat",
                "explanation": action.explanation,
                "requires_approval": False,
            }

        if action.intent == "configure":
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "handle_chat or chat_reply or still_refuses or forbids_inventing"`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full backend test suite to confirm no regression**

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass (618 plus the tests added in Tasks 1-2; the count may have grown since this plan was written, but none should fail)

- [ ] **Step 6: Commit**

```bash
git add backend/src/network_copilot/ai/service.py backend/tests/ai/test_ai.py
git commit -m "feat: answer conversational messages with a chat intent"
```

---

### Task 3: Conversation history for follow-up questions

**Files:**
- Modify: `backend/src/network_copilot/ai/service.py` (add `_recent_history`, extend `interpret()` and `handle()` with `session_id`, add the `chat_service` import)
- Modify: `backend/src/network_copilot/ai/routes.py` (pass `data.session_id` into `handle`)
- Test: `backend/tests/ai/test_ai.py` (append)

**Interfaces:**
- Consumes: `AIService.handle()` returning a chat reply (Task 2); `chat.service.list_messages(session_id: int | None = None, limit: int = 200) -> list[ChatMessage]` (already exists, session-scoped, oldest-first); `chat.service.record_message(user_id, username, role, content, payload=None, session_id=None)` (already exists, used by the tests here); `ChatRequest.session_id: int | None` (already exists on the request schema).
- Produces: `AIService._recent_history(session_id: int | None, current_message: str) -> list[dict]` (each dict `{"role": str, "content": str}`), and `interpret`/`handle` both accepting `session_id: int | None = None`. Nothing later depends on these — this is the last task.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/ai/test_ai.py`:

```python
def test_recent_history_returns_user_and_assistant_turns_oldest_first(app):
    from network_copilot.chat.service import record_message
    from network_copilot.chat.session_service import create_session

    session = create_session()
    record_message(1, "g1", "user", "cau hoi 1", session_id=session.id)
    record_message(1, "g1", "assistant", "tra loi 1", session_id=session.id)

    history = AIService()._recent_history(session.id, "cau hoi moi")

    assert history == [
        {"role": "user", "content": "cau hoi 1"},
        {"role": "assistant", "content": "tra loi 1"},
    ]


def test_recent_history_excludes_system_messages(app):
    from network_copilot.chat.service import record_message
    from network_copilot.chat.session_service import create_session

    session = create_session()
    record_message(1, "g1", "user", "cau hoi", session_id=session.id)
    record_message(None, None, "system", "Request failed.", session_id=session.id)

    history = AIService()._recent_history(session.id, "cau hoi moi")

    assert [turn["role"] for turn in history] == ["user"]


def test_recent_history_is_scoped_to_one_session(app):
    from network_copilot.chat.service import record_message
    from network_copilot.chat.session_service import create_session

    session_a = create_session()
    session_b = create_session()
    record_message(1, "g1", "user", "trong phien A", session_id=session_a.id)
    record_message(1, "g1", "user", "trong phien B", session_id=session_b.id)

    history = AIService()._recent_history(session_a.id, "cau hoi moi")

    assert [turn["content"] for turn in history] == ["trong phien A"]


def test_recent_history_keeps_only_the_last_ten_turns(app):
    from network_copilot.chat.service import record_message
    from network_copilot.chat.session_service import create_session

    session = create_session()
    for index in range(14):
        record_message(1, "g1", "user", f"tin {index}", session_id=session.id)

    history = AIService()._recent_history(session.id, "cau hoi moi")

    assert len(history) == 10
    assert history[0]["content"] == "tin 4"
    assert history[-1]["content"] == "tin 13"


def test_recent_history_drops_the_message_being_handled(app):
    """ai/routes.py records the incoming message before calling handle(), so
    it is already the newest row: sending it again would duplicate it."""
    from network_copilot.chat.service import record_message
    from network_copilot.chat.session_service import create_session

    session = create_session()
    record_message(1, "g1", "user", "cau hoi cu", session_id=session.id)
    record_message(1, "g1", "user", "cau hoi moi", session_id=session.id)

    history = AIService()._recent_history(session.id, "cau hoi moi")

    assert [turn["content"] for turn in history] == ["cau hoi cu"]


def test_recent_history_is_empty_without_a_session(app):
    assert AIService()._recent_history(None, "cau hoi moi") == []


def test_conversation_history_reaches_the_model(app, admin_user, ssh_factory):
    from network_copilot.chat.service import record_message
    from network_copilot.chat.session_service import create_session

    session = create_session()
    record_message(1, "g1", "user", "OSPF la gi?", session_id=session.id)

    service, provider = service_with(app, CHAT_ACTION)
    service.handle("con VLAN thi sao?", admin_user.id, session_id=session.id)

    conversation = provider.prompts[0]["context"]["conversation"]
    assert conversation == [{"role": "user", "content": "OSPF la gi?"}]


def test_conversation_history_never_leaks_message_payloads(
    app, admin_user, ssh_factory
):
    """Payloads carry raw command output. Only role and content may be sent."""
    from network_copilot.chat.service import record_message
    from network_copilot.chat.session_service import create_session

    session = create_session()
    record_message(
        1,
        "g1",
        "assistant",
        "Da chay xong.",
        {"results": [{"output": "SENTINEL-SECRET-XYZ"}]},
        session_id=session.id,
    )

    service, provider = service_with(app, CHAT_ACTION)
    service.handle("alo", admin_user.id, session_id=session.id)

    assert "SENTINEL-SECRET-XYZ" not in provider.everything_sent()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "recent_history or conversation_history"` (from `backend/`)
Expected: FAIL — `AttributeError: 'AIService' object has no attribute '_recent_history'` for the six `_recent_history` tests, and `TypeError: handle() got an unexpected keyword argument 'session_id'` for the two that call `handle(..., session_id=...)`.

- [ ] **Step 3: Write the minimal implementation**

In `backend/src/network_copilot/ai/service.py`, find the import block line:

```python
from ..changes import batch_service
```

Add the chat-service import immediately above it, keeping alphabetical order:

```python
from ..chat import service as chat_service
from ..changes import batch_service
```

(`chat.service` imports nothing from the `ai` package, so this is not a circular import — `ai/routes.py` already imports it at module level.)

Then find:

```python
    # -- interpret --------------------------------------------------------
    @staticmethod
    def _extract_json(raw: str) -> dict:
```

Insert the history helper immediately above that comment line:

```python
    # -- conversation history ---------------------------------------------
    # How many recent turns the model sees, and how many rows to fetch to
    # find them: the fetch is wider than the window because system messages
    # and the duplicate of the current turn are filtered out below.
    HISTORY_LIMIT = 10
    _HISTORY_FETCH_LIMIT = 40

    @classmethod
    def _recent_history(
        cls, session_id: int | None, current_message: str
    ) -> list[dict]:
        """Recent turns of this chat session, for conversational follow-ups.

        Only role and content are returned. A message's payload carries raw
        command output, which can contain configuration detail, and this
        module's standing rule is that the model never receives credentials,
        management IPs or a running-config.
        """
        if session_id is None:
            return []

        rows = chat_service.list_messages(
            session_id=session_id, limit=cls._HISTORY_FETCH_LIMIT
        )
        turns = [
            {"role": row.role, "content": row.content}
            for row in rows
            if row.role in ("user", "assistant")
        ]

        # ai/routes.py records the incoming message before calling handle(),
        # so it is already the newest row here.
        if (
            turns
            and turns[-1]["role"] == "user"
            and turns[-1]["content"] == current_message
        ):
            turns.pop()

        return turns[-cls.HISTORY_LIMIT :]

    # -- interpret --------------------------------------------------------
    @staticmethod
    def _extract_json(raw: str) -> dict:
```

Then find the start of `interpret()`:

```python
    def interpret(self, message: str, user_id: int | None) -> AIAction:
        """Ask the model for a structured action. No side effects."""
        context = self.build_context()
        schema = build_ai_action_schema(
            device["hostname"] for device in context["devices"]
        )
```

Replace with:

```python
    def interpret(
        self, message: str, user_id: int | None, session_id: int | None = None
    ) -> AIAction:
        """Ask the model for a structured action. No side effects."""
        context = self.build_context()
        context["conversation"] = self._recent_history(session_id, message)
        schema = build_ai_action_schema(
            device["hostname"] for device in context["devices"]
        )
```

Then find the start of `handle()`:

```python
    def handle(self, message: str, user_id: int | None) -> dict:
        action = self.interpret(message, user_id)
```

Replace with:

```python
    def handle(
        self, message: str, user_id: int | None, session_id: int | None = None
    ) -> dict:
        action = self.interpret(message, user_id, session_id=session_id)
```

Finally, in `backend/src/network_copilot/ai/routes.py`, find:

```python
    result = AIService().handle(data.message, user_id)
```

Replace with:

```python
    result = AIService().handle(data.message, user_id, session_id=data.session_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "recent_history or conversation_history"`
Expected: PASS (8 tests)

- [ ] **Step 5: Run the full backend test suite to confirm no regression**

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass — this specifically confirms that every existing caller of `interpret()`/`handle()` that passes only `(message, user_id)` still works with `session_id` defaulting to `None`.

- [ ] **Step 6: Manually verify in a real browser**

Start the backend (from `backend/`): `../.venv/Scripts/python.exe -m flask --app wsgi.py run` (or the `.claude/launch.json` "dashboard-check" preview config).

- Log in and send `alo`. Confirm a normal assistant bubble with a friendly reply — **not** a red error bubble.
- Ask `OSPF là gì?`. Confirm a substantive explanation, and that no command result table appears (nothing was executed).
- Ask a real monitor question, e.g. `kiểm tra OSPF trên DIST-SW1`. Confirm it still runs the command and renders the result table exactly as before — the action path must be untouched.
- Ask a follow-up that only makes sense with context, e.g. after the OSPF question ask `còn VLAN thì sao?`. Confirm the answer follows on from the previous turn rather than treating it as a fresh, contextless question.
- Ask something clearly outside networking, e.g. `thời tiết hôm nay thế nào?`. Confirm a brief reply saying it is outside scope, rather than an invented answer.
- Click "New chat", then ask a follow-up referring to something from the previous session. Confirm the assistant does **not** have that context — history is per-session.

- [ ] **Step 7: Commit**

```bash
git add backend/src/network_copilot/ai/service.py backend/src/network_copilot/ai/routes.py backend/tests/ai/test_ai.py
git commit -m "feat: give the AI recent session context for follow-up questions"
```
