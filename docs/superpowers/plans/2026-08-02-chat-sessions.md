# Chat Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single ever-growing shared chat log with real, named, shared chat sessions (`chat_sessions` table + `chat_messages.session_id`), view/switch only, matching the spec at `docs/superpowers/specs/2026-08-02-chat-sessions-design.md`.

**Architecture:** A new `ChatSession` model and a `session_id` FK on `ChatMessage`, a new `chat/session_service.py` for session CRUD/listing/title-derivation, updated `chat/service.py` and `chat/routes.py` for session-scoped messages, `session_id` threaded through `POST /api/ai/chat`, and a frontend rewrite that replaces today's `localStorage`-cutoff mechanism with real session switching.

**Tech Stack:** Flask, SQLAlchemy, Alembic/Flask-Migrate, Alpine.js — no new dependency.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-02-chat-sessions-design.md` — this plan implements it, with one deliberate deviation noted below.
- **Deviation from the spec's literal wording:** the spec's "Backend API" section says `session_id` is a *required* field on `ChatRequest` and a required query parameter on `GET /api/chat/messages`. This plan makes it **optional** everywhere in the backend (`session_id: int | None = None`), auto-resolving to "the most recently created session, creating one if none exist at all" when omitted. Reason: roughly 20 existing test call sites across `tests/ai/test_ai.py`, `tests/ai/test_provider.py`, `tests/ai/test_chat_history.py`, `tests/e2e/test_complete_flow.py`, `tests/e2e/test_demo_check.py`, and `tests/test_security.py` call `POST /api/ai/chat` without a `session_id`; making it required would turn every one of those into a 422 and require rewriting all of them blind. Making it optional with a sensible default keeps every existing test passing unmodified while the new frontend (Task 6) always sends an explicit `session_id` once a real session exists, so the feature behaves identically from a user's perspective — the laxness only matters for callers this plan doesn't touch. The `chat_messages.session_id` **database column** is still `NOT NULL`, exactly as the spec's migration section describes; only the request-time parameter is optional, always resolved to a real id before any row is inserted.
- The "Thay đổi đang chờ" (pending changes/batches) panel and its endpoints are untouched — confirmed out of scope by the spec.
- No rename/delete of sessions this iteration — confirmed out of scope by the spec.
- This project's tests build their schema from the SQLAlchemy models via `db.create_all()` (`tests/conftest.py`), not from Alembic migrations — no prior migration has an automated test, and this one follows that same convention (manually verified in Task 1, not pytest-covered).
- Use the project's Python 3.13 venv (`../.venv/Scripts/python.exe` from `backend/`, or `../.venv/Scripts/flask.exe` for Flask CLI commands) for every command.

---

### Task 1: Data model — `ChatSession`, `ChatMessage.session_id`, and the migration

**Files:**
- Modify: `backend/src/network_copilot/chat/model.py` (full-file rewrite)
- Create: `backend/migrations/versions/1d6734caee3b_add_chat_sessions.py`
- Modify: `backend/tests/chat/test_chat.py:1-26` (the two tests that construct `ChatMessage` directly need a session)

**Interfaces:**
- Consumes: nothing new — `..extensions.db`, matching every other model in this codebase.
- Produces: `ChatSession` (columns: `id`, `created_by_id`, `created_at`; no `to_dict()` of its own — title is computed by `chat/session_service.py` in Task 2, not stored). `ChatMessage.session_id` (int, not nullable) and `ChatMessage.to_dict()` now includes `"session_id"`. Both are imported by every later task.

- [ ] **Step 1: Write the failing tests**

Replace the top of `backend/tests/chat/test_chat.py` (its first two tests, which construct `ChatMessage` directly) — find:

```python
from network_copilot.chat.model import ChatMessage
from network_copilot.chat.service import list_messages, record_message
from network_copilot.extensions import db


def test_to_dict_includes_every_field(app):
    message = ChatMessage(
        user_id=1, username="g1", role="user", content="hello", payload={"a": 1}
    )
    db.session.add(message)
    db.session.commit()

    data = message.to_dict()
    assert data["username"] == "g1"
    assert data["role"] == "user"
    assert data["content"] == "hello"
    assert data["payload"] == {"a": 1}
    assert data["created_at"] is not None


def test_allows_a_null_user(app):
    message = ChatMessage(user_id=None, username=None, role="system", content="x")
    db.session.add(message)
    db.session.commit()
    assert message.to_dict()["user_id"] is None
```

Replace with:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from network_copilot.chat.model import ChatMessage, ChatSession
from network_copilot.chat.service import list_messages, record_message
from network_copilot.extensions import db


@pytest.fixture
def chat_session(app):
    session = ChatSession()
    db.session.add(session)
    db.session.commit()
    return session


def test_to_dict_includes_every_field(app, chat_session):
    message = ChatMessage(
        session_id=chat_session.id,
        user_id=1,
        username="g1",
        role="user",
        content="hello",
        payload={"a": 1},
    )
    db.session.add(message)
    db.session.commit()

    data = message.to_dict()
    assert data["session_id"] == chat_session.id
    assert data["username"] == "g1"
    assert data["role"] == "user"
    assert data["content"] == "hello"
    assert data["payload"] == {"a": 1}
    assert data["created_at"] is not None


def test_allows_a_null_user(app, chat_session):
    message = ChatMessage(
        session_id=chat_session.id, user_id=None, username=None, role="system", content="x"
    )
    db.session.add(message)
    db.session.commit()
    assert message.to_dict()["user_id"] is None


def test_chat_session_has_created_at(app):
    session = ChatSession()
    db.session.add(session)
    db.session.commit()
    assert session.id is not None
    assert session.created_at is not None


def test_chat_message_requires_a_session(app):
    message = ChatMessage(user_id=1, username="g1", role="user", content="hi")
    db.session.add(message)
    with pytest.raises(IntegrityError):
        db.session.commit()
```

Leave the rest of `test_chat.py` (the `record_message`/`list_messages`/endpoint tests further down) unchanged for now — Task 3 updates those.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_chat.py -v` (from `backend/`)
Expected: FAIL — `ImportError: cannot import name 'ChatSession'` (it doesn't exist yet), and the later tests in the file will also error since `record_message`/`list_messages` haven't changed yet but `ChatMessage` import itself fails first, aborting collection.

- [ ] **Step 3: Write the minimal implementation**

Replace the entire contents of `backend/src/network_copilot/chat/model.py`:

```python
"""Shared team chat: named sessions, each holding an ordered transcript."""

from datetime import datetime, timezone

from ..extensions import db

CHAT_ROLES = ("user", "assistant", "system")


class ChatSession(db.Model):
    """A named conversation thread within the shared team chat.

    Sessions are shared across the whole team, not private per user: any
    authenticated user can see and switch to any session. created_by_id
    only records who started it - it does not restrict who can read it.
    There is no stored title: it is derived from the session's first
    message by chat/session_service.py, computed at read time.
    """

    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ChatSession #{self.id}>"


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    username = db.Column(db.String(64))
    role = db.Column(db.String(16), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    payload = db.Column(db.JSON)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "content": self.content,
            "payload": self.payload,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ChatMessage {self.role} #{self.id}>"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_chat.py -v`
Expected: the 4 tests touched/added in Step 1 pass. The remaining tests in the same file (`test_record_message_*`, `test_list_messages_*`, the endpoint tests) will now FAIL, because `record_message()`/`list_messages()` still construct/query `ChatMessage` without a `session_id` and the column is now `NOT NULL` — this is expected and is fixed in Task 3, not here. Confirm specifically that the 4 new/changed tests pass:

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_chat.py -v -k "to_dict_includes_every_field or allows_a_null_user or chat_session_has_created_at or chat_message_requires_a_session"`
Expected: PASS (4 passed)

- [ ] **Step 5: Create the migration**

Create `backend/migrations/versions/1d6734caee3b_add_chat_sessions.py`:

```python
"""add chat sessions

Revision ID: 1d6734caee3b
Revises: 9bae573911ac
Create Date: 2026-08-02 00:00:00.000000

"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1d6734caee3b'
down_revision = '9bae573911ac'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'chat_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('chat_sessions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_chat_sessions_created_at'), ['created_at'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_chat_sessions_created_by_id'), ['created_by_id'], unique=False
        )

    # session_id starts nullable so existing rows can be backfilled below; it
    # is made NOT NULL once every row has a value.
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('session_id', sa.Integer(), nullable=True))

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    # .lastrowid is SQLite-specific, matching this project's only supported
    # database backend (see config.py).
    migration_session_id = bind.execute(
        sa.text(
            "INSERT INTO chat_sessions (created_by_id, created_at) "
            "VALUES (NULL, :created_at)"
        ).bindparams(created_at=now)
    ).lastrowid
    bind.execute(
        sa.text(
            "UPDATE chat_messages SET session_id = :session_id "
            "WHERE session_id IS NULL"
        ).bindparams(session_id=migration_session_id)
    )

    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.alter_column('session_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(
            batch_op.f('ix_chat_messages_session_id'), ['session_id'], unique=False
        )
        batch_op.create_foreign_key(
            'fk_chat_messages_session_id_chat_sessions',
            'chat_sessions',
            ['session_id'],
            ['id'],
            ondelete='CASCADE',
        )


def downgrade():
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.drop_constraint(
            'fk_chat_messages_session_id_chat_sessions', type_='foreignkey'
        )
        batch_op.drop_index(batch_op.f('ix_chat_messages_session_id'))
        batch_op.drop_column('session_id')

    with op.batch_alter_table('chat_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_sessions_created_by_id'))
        batch_op.drop_index(batch_op.f('ix_chat_sessions_created_at'))
    op.drop_table('chat_sessions')
```

`9bae573911ac` is the current migration head (confirmed via `flask db heads` while writing this plan) — if another migration has landed on `main` since, change `down_revision` to whatever `flask db heads` reports at implementation time, and rename the file/`revision` value is fine to leave as-is (it only needs to be unique).

- [ ] **Step 6: Manually verify the migration** (no automated test — see Global Constraints)

Run, from `backend/`, against a throwaway copy of a database that already has `chat_messages` rows (copy `network_copilot.db` to a scratch file first if you want to avoid touching the real dev database, and point `DATABASE_URL`/the app config at the copy):

```bash
../.venv/Scripts/flask.exe db upgrade
```

Then confirm:
```bash
../.venv/Scripts/python.exe -c "
from network_copilot.app import create_app
from network_copilot.chat.model import ChatMessage, ChatSession
app = create_app()
with app.app_context():
    total = ChatMessage.query.count()
    orphaned = ChatMessage.query.filter(ChatMessage.session_id.is_(None)).count()
    sessions = ChatSession.query.count()
    print(f'messages={total} orphaned={orphaned} sessions={sessions}')
"
```
Expected: `orphaned=0` and `sessions >= 1` (exactly 1 if the database had no sessions before this migration, which is always true pre-feature).

- [ ] **Step 7: Commit**

```bash
git add backend/src/network_copilot/chat/model.py backend/migrations/versions/1d6734caee3b_add_chat_sessions.py backend/tests/chat/test_chat.py
git commit -m "feat: add ChatSession and chat_messages.session_id"
```

---

### Task 2: `chat/session_service.py` — create, list, resolve, and title derivation

**Files:**
- Create: `backend/src/network_copilot/chat/session_service.py`
- Create: `backend/tests/chat/test_session_service.py`

**Interfaces:**
- Consumes: `ChatSession`, `ChatMessage` (Task 1).
- Produces: `create_session(created_by_id: int | None = None) -> ChatSession`,
  `resolve_or_create_session(session_id: int | None) -> ChatSession`,
  `list_sessions() -> list[dict]` (each dict: `{id, title, created_at}`),
  `session_to_dict(session: ChatSession) -> dict` (same shape as one item
  of `list_sessions()`'s result). Task 3 (`chat/service.py`) calls
  `resolve_or_create_session`; Task 4 (`chat/routes.py`) calls
  `create_session`, `list_sessions`, `session_to_dict`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/chat/test_session_service.py`:

```python
from network_copilot.chat.model import ChatMessage, ChatSession
from network_copilot.chat.session_service import (
    create_session,
    list_sessions,
    resolve_or_create_session,
    session_to_dict,
)
from network_copilot.extensions import db


def _add_message(session_id: int, content: str, role: str = "user"):
    message = ChatMessage(
        session_id=session_id, user_id=1, username="g1", role=role, content=content
    )
    db.session.add(message)
    db.session.commit()
    return message


# -- create_session ---------------------------------------------------------


def test_create_session_persists_a_row(app):
    session = create_session(created_by_id=7)
    assert session.id is not None
    assert session.created_by_id == 7


def test_create_session_allows_no_creator(app):
    session = create_session()
    assert session.created_by_id is None


# -- session_to_dict / title derivation --------------------------------------


def test_session_to_dict_titles_an_empty_session_new_chat(app):
    session = create_session()
    assert session_to_dict(session)["title"] == "New chat"


def test_session_to_dict_titles_from_the_first_message(app):
    session = create_session()
    _add_message(session.id, "Kiem tra OSPF cua DIST-SW1")
    assert session_to_dict(session)["title"] == "Kiem tra OSPF cua DIST-SW1"


def test_session_to_dict_truncates_a_long_first_message(app):
    session = create_session()
    long_content = "a" * 80
    _add_message(session.id, long_content)
    title = session_to_dict(session)["title"]
    assert title == ("a" * 60) + "…"


def test_session_to_dict_uses_the_earliest_message_not_the_latest(app):
    session = create_session()
    _add_message(session.id, "first message")
    _add_message(session.id, "second message")
    assert session_to_dict(session)["title"] == "first message"


def test_session_to_dict_includes_id_and_created_at(app):
    session = create_session()
    data = session_to_dict(session)
    assert data["id"] == session.id
    assert data["created_at"] is not None


# -- list_sessions ------------------------------------------------------------


def test_list_sessions_orders_most_recently_active_first(app):
    older = create_session()
    newer = create_session()
    _add_message(older.id, "hello")
    items = list_sessions()
    # newer has no messages but was created after older, so it still sorts
    # first: activity time falls back to the session's own created_at.
    assert [item["id"] for item in items] == [newer.id, older.id]


def test_list_sessions_activity_beats_creation_order(app):
    first_created = create_session()
    second_created = create_session()
    # first_created gets a message after second_created was created, so it
    # should now sort above second_created (which has no messages).
    _add_message(first_created.id, "hello")
    items = list_sessions()
    assert [item["id"] for item in items] == [first_created.id, second_created.id]


def test_list_sessions_returns_an_empty_list_with_no_sessions(app):
    assert list_sessions() == []


# -- resolve_or_create_session ------------------------------------------------


def test_resolve_returns_the_session_for_a_known_id(app):
    session = create_session()
    resolved = resolve_or_create_session(session.id)
    assert resolved.id == session.id


def test_resolve_falls_back_to_the_most_recently_created_session(app):
    create_session()
    latest = create_session()
    resolved = resolve_or_create_session(None)
    assert resolved.id == latest.id


def test_resolve_falls_back_for_an_unknown_id(app):
    create_session()
    resolved = resolve_or_create_session(999999)
    assert resolved is not None


def test_resolve_creates_a_session_when_none_exist(app):
    assert db.session.query(ChatSession).count() == 0
    resolved = resolve_or_create_session(None)
    assert resolved.id is not None
    assert db.session.query(ChatSession).count() == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_session_service.py -v` (from `backend/`)
Expected: FAIL with `ModuleNotFoundError: No module named 'network_copilot.chat.session_service'`

- [ ] **Step 3: Write the minimal implementation**

Create `backend/src/network_copilot/chat/session_service.py`:

```python
"""Session CRUD and listing for the shared team chat."""

from ..extensions import db
from .model import ChatMessage, ChatSession

_TITLE_MAX_LENGTH = 60


def create_session(created_by_id: int | None = None) -> ChatSession:
    session = ChatSession(created_by_id=created_by_id)
    db.session.add(session)
    db.session.commit()
    return session


def resolve_or_create_session(session_id: int | None) -> ChatSession:
    """Return the session for session_id, or a sensible default.

    Used wherever a session_id is optional at the API layer (see the
    "Deviation from the spec" note in the plan's Global Constraints): an
    unknown or omitted id falls back to the most recently created session,
    creating a brand new one only if none exist at all.
    """
    if session_id is not None:
        session = db.session.get(ChatSession, session_id)
        if session is not None:
            return session

    latest = (
        db.session.query(ChatSession)
        .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
        .first()
    )
    if latest is not None:
        return latest
    return create_session()


def _title_for_session(session_id: int) -> str:
    first = (
        db.session.query(ChatMessage.content)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .first()
    )
    if first is None or not first[0]:
        return "New chat"
    content = first[0].strip()
    if len(content) <= _TITLE_MAX_LENGTH:
        return content
    return content[:_TITLE_MAX_LENGTH].rstrip() + "…"


def session_to_dict(session: ChatSession) -> dict:
    return {
        "id": session.id,
        "title": _title_for_session(session.id),
        "created_at": session.created_at.isoformat() if session.created_at else None,
    }


def _last_activity(session: ChatSession):
    last = (
        db.session.query(db.func.max(ChatMessage.created_at))
        .filter(ChatMessage.session_id == session.id)
        .scalar()
    )
    return last or session.created_at


def list_sessions() -> list[dict]:
    sessions = db.session.query(ChatSession).all()
    sessions.sort(key=_last_activity, reverse=True)
    return [session_to_dict(session) for session in sessions]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_session_service.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/chat/session_service.py backend/tests/chat/test_session_service.py
git commit -m "feat: add chat session create/list/resolve and title derivation"
```

---

### Task 3: `chat/service.py` — session-scoped `record_message`/`list_messages`

**Files:**
- Modify: `backend/src/network_copilot/chat/service.py` (full-file rewrite)
- Modify: `backend/tests/chat/test_chat.py` (append; the pre-existing `record_message`/`list_messages`/endpoint tests further down this file, broken since Task 1, start passing again once this task's implementation lands — no changes needed to those tests themselves, since `session_id` is optional and they never passed one)

**Interfaces:**
- Consumes: `session_service.resolve_or_create_session` (Task 2).
- Produces: `record_message(user_id, username, role, content, payload=None, session_id=None) -> ChatMessage | None` and `list_messages(session_id=None, limit=200) -> list[ChatMessage]` — both now resolve a real session via `session_service` before touching `ChatMessage` rows. Used by Task 4 (`chat/routes.py`) and Task 5 (`ai/routes.py`).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/chat/test_chat.py`:

```python
from network_copilot.chat.session_service import create_session


def test_record_message_uses_the_given_session(app):
    session = create_session()
    record_message(1, "g1", "user", "hello", session_id=session.id)
    row = db.session.query(ChatMessage).one()
    assert row.session_id == session.id


def test_record_message_resolves_a_session_when_omitted(app):
    record_message(1, "g1", "user", "hello")
    row = db.session.query(ChatMessage).one()
    assert row.session_id is not None


def test_list_messages_only_returns_the_given_session(app):
    session_a = create_session()
    session_b = create_session()
    record_message(1, "g1", "user", "in A", session_id=session_a.id)
    record_message(1, "g1", "user", "in B", session_id=session_b.id)

    rows = list_messages(session_id=session_a.id)

    assert [row.content for row in rows] == ["in A"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_chat.py -v` (from `backend/`)
Expected: FAIL — every `record_message`/`list_messages` call raises `TypeError: record_message() got an unexpected keyword argument 'session_id'` (or, for the pre-existing calls with no `session_id` kwarg at all, the underlying `IntegrityError` from Task 1's now-`NOT NULL` column), since `chat/service.py` hasn't changed yet.

- [ ] **Step 3: Write the minimal implementation**

Replace the entire contents of `backend/src/network_copilot/chat/service.py`:

```python
"""Shared team chat transcript with the AI copilot.

Every exchange through POST /api/ai/chat is recorded here, whether it
succeeded, was blocked by the policy engine, or failed upstream, so anyone
reopening the page sees exactly what happened, not just the calls that
returned 200. Every message belongs to exactly one chat session (see
chat/session_service.py); a caller that does not know/care which session
gets a sensible default rather than being forced to resolve one itself.
"""

import logging

from ..extensions import db
from . import session_service
from .model import ChatMessage

logger = logging.getLogger(__name__)


def record_message(
    user_id: int | None,
    username: str | None,
    role: str,
    content: str,
    payload: dict | None = None,
    session_id: int | None = None,
) -> ChatMessage | None:
    """Persist one chat message. Never raises: a failure to record history
    must not break the AI response the user is waiting for."""
    try:
        session = session_service.resolve_or_create_session(session_id)
        message = ChatMessage(
            session_id=session.id,
            user_id=user_id,
            username=username,
            role=role,
            content=content or "",
            payload=payload,
        )
        db.session.add(message)
        db.session.commit()
        return message
    except Exception:  # pragma: no cover - defensive, matches audit.service
        logger.exception("Failed to record chat message (role=%s)", role)
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def list_messages(session_id: int | None = None, limit: int = 200) -> list[ChatMessage]:
    session = session_service.resolve_or_create_session(session_id)
    bounded_ids = (
        db.session.query(ChatMessage.id.label("id"))
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(min(max(limit, 1), 500))
        .subquery()
    )
    return (
        db.session.query(ChatMessage)
        .join(bounded_ids, ChatMessage.id == bounded_ids.c.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_chat.py tests/chat/test_session_service.py -v`
Expected: PASS (every test in both files, including the pre-existing ones from before this feature — they never passed `session_id`, and now resolve to an implicit default session instead of erroring)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/chat/service.py backend/tests/chat/test_chat.py
git commit -m "feat: make chat message recording and listing session-scoped"
```

---

### Task 4: `chat/routes.py` — session endpoints

**Files:**
- Modify: `backend/src/network_copilot/chat/routes.py` (full-file rewrite)
- Modify: `backend/tests/chat/test_chat.py` (append)

**Interfaces:**
- Consumes: `session_service.create_session`, `session_service.list_sessions`, `session_service.session_to_dict` (Task 2); `service.list_messages` (Task 3); `..auth.service.current_user` (existing, same import other blueprints already use, e.g. `ai/routes.py`).
- Produces: `POST /api/chat/sessions`, `GET /api/chat/sessions`, and `GET /api/chat/messages` now accepting an optional `session_id` query param. No other task depends on this route module directly (the frontend, Task 6, calls these HTTP endpoints, not Python functions).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/chat/test_chat.py`:

```python
def test_create_session_endpoint_requires_authentication(client):
    assert client.post("/api/chat/sessions").status_code == 401


def test_create_session_endpoint_is_usable_by_viewer(client, viewer_headers):
    response = client.post("/api/chat/sessions", headers=viewer_headers)
    assert response.status_code == 201
    body = response.get_json()
    assert body["title"] == "New chat"
    assert body["id"] is not None


def test_list_sessions_endpoint_requires_authentication(client):
    assert client.get("/api/chat/sessions").status_code == 401


def test_list_sessions_endpoint_returns_created_sessions(client, admin_headers):
    created = client.post("/api/chat/sessions", headers=admin_headers).get_json()
    response = client.get("/api/chat/sessions", headers=admin_headers)
    assert response.status_code == 200
    ids = [item["id"] for item in response.get_json()["items"]]
    assert created["id"] in ids


def test_messages_endpoint_filters_by_session_id(client, admin_headers, app):
    session_a = client.post("/api/chat/sessions", headers=admin_headers).get_json()
    session_b = client.post("/api/chat/sessions", headers=admin_headers).get_json()
    record_message(1, "g1", "user", "in A", session_id=session_a["id"])
    record_message(1, "g1", "user", "in B", session_id=session_b["id"])

    response = client.get(
        f"/api/chat/messages?session_id={session_a['id']}", headers=admin_headers
    )

    items = response.get_json()["items"]
    assert [item["content"] for item in items] == ["in A"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_chat.py -v -k "session"` (from `backend/`)
Expected: FAIL — `POST /api/chat/sessions` and `GET /api/chat/sessions` both 404 (routes don't exist yet); `test_messages_endpoint_filters_by_session_id` fails because the plain `GET /api/chat/messages` route doesn't read a `session_id` query param yet.

- [ ] **Step 3: Write the minimal implementation**

Replace the entire contents of `backend/src/network_copilot/chat/routes.py`:

```python
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..auth.service import current_user
from . import service, session_service

bp = Blueprint("chat", __name__, url_prefix="/api/chat")


@bp.get("/messages")
@jwt_required()
def list_messages():
    limit = request.args.get("limit", default=200, type=int)
    session_id = request.args.get("session_id", type=int)
    messages = service.list_messages(session_id=session_id, limit=limit)
    return jsonify({"items": [message.to_dict() for message in messages]}), 200


@bp.get("/sessions")
@jwt_required()
def list_sessions():
    return jsonify({"items": session_service.list_sessions()}), 200


@bp.post("/sessions")
@jwt_required()
def create_session():
    user = current_user()
    session = session_service.create_session(
        created_by_id=user.id if user else None
    )
    return jsonify(session_service.session_to_dict(session)), 201
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/chat/test_chat.py -v`
Expected: PASS (every test in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/chat/routes.py backend/tests/chat/test_chat.py
git commit -m "feat: add chat session endpoints and session-filtered messages"
```

---

### Task 5: Thread `session_id` through `POST /api/ai/chat`

**Files:**
- Modify: `backend/src/network_copilot/ai/schemas.py:104-107`
- Modify: `backend/src/network_copilot/ai/routes.py` (full-file rewrite)
- Test: `backend/tests/ai/test_chat_history.py` (append)

**Interfaces:**
- Consumes: `chat.service.record_message` (now accepting `session_id`, Task 3).
- Produces: `ChatRequest.session_id: int | None` — consumed only by `ai/routes.py` in this same task; no later task depends on it.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/ai/test_chat_history.py`. Confirmed present already at the top of that file: `MONITOR_ACTION` (module-level dict, line 13), and imports for `ChatMessage`, `db`, `FakeAIProvider` — this task's tests use only those plus the `dist_switch`/`admin_headers`/`ssh_factory`/`app`/`client` fixtures already used by other tests in the same file:

```python
def test_chat_endpoint_records_messages_against_the_given_session(
    client, admin_headers, app, dist_switch, ssh_factory
):
    from network_copilot.chat.session_service import create_session

    ssh_factory.set_client(dist_switch.hostname, default_output="ok")
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=MONITOR_ACTION)
    session = create_session()

    client.post(
        "/api/ai/chat",
        headers=admin_headers,
        json={"message": "check routes", "session_id": session.id},
    )

    rows = db.session.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [row.session_id for row in rows] == [session.id, session.id]


def test_chat_endpoint_rejects_a_non_integer_session_id(client, admin_headers):
    response = client.post(
        "/api/ai/chat",
        headers=admin_headers,
        json={"message": "hi", "session_id": "not-a-number"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_chat_history.py -v -k session` (from `backend/`)
Expected: FAIL — `test_chat_endpoint_records_messages_against_the_given_session` fails because `ChatRequest` rejects the extra `session_id` key (`extra="forbid"`, so this currently 422s with a "not permitted" validation error instead of recording against the given session); `test_chat_endpoint_rejects_a_non_integer_session_id` currently fails too, but for the same reason as an accidental pass — confirm by reading the actual failure output rather than assuming, since a wrong-reason pass would hide a bug.

- [ ] **Step 3: Write the minimal implementation**

In `backend/src/network_copilot/ai/schemas.py`, find:

```python
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
```

Replace with:

```python
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    session_id: int | None = None
```

Replace the entire contents of `backend/src/network_copilot/ai/routes.py`:

```python
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from ..auth.service import current_user
from ..chat.service import record_message as record_chat_message
from ..errors import ValidationError
from ..extensions import limiter
from .schemas import ChatRequest
from .service import AIService

bp = Blueprint("ai", __name__, url_prefix="/api/ai")


@bp.after_app_request
def record_failed_chat_response(response):
    """Store one safe transcript entry for each authenticated chat failure."""
    if request.endpoint != "ai.chat" or 200 <= response.status_code < 300:
        return response

    try:
        identity = get_jwt_identity()
    except RuntimeError:
        # JWT verification did not complete, so this is an unauthenticated 401.
        return response
    if identity is None:
        return response

    user = current_user()
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {"error": "request_failed", "message": "Request failed."}
    content = payload.get("message")
    if not isinstance(content, str):
        content = "Request failed."
    body = request.get_json(silent=True) or {}
    raw_session_id = body.get("session_id") if isinstance(body, dict) else None
    session_id = raw_session_id if isinstance(raw_session_id, int) else None
    record_chat_message(
        user.id if user else None,
        user.username if user else None,
        "system",
        content,
        payload,
        session_id=session_id,
    )
    return response


@bp.post("/chat")
@jwt_required()
@limiter.limit("20 per minute")
def chat():
    try:
        data = ChatRequest.model_validate(request.get_json(silent=True) or {})
    except PydanticValidationError as exc:
        details: dict[str, list[str]] = {}
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "_root"
            details.setdefault(field, []).append(error["msg"])
        raise ValidationError("A non-empty 'message' is required.", details) from exc

    user = current_user()
    user_id = user.id if user else None
    username = user.username if user else None

    record_chat_message(
        user_id, username, "user", data.message, session_id=data.session_id
    )
    result = AIService().handle(data.message, user_id)
    record_chat_message(
        user_id,
        username,
        "assistant",
        result.get("explanation", ""),
        result,
        session_id=data.session_id,
    )
    return jsonify(result), 200
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_chat_history.py -v`
Expected: PASS (every test in the file, including the 2 new ones and every pre-existing one that never sent `session_id`)

- [ ] **Step 5: Run the full backend test suite to confirm no regression**

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass (592 as of this plan; the count may have grown since, but none should fail — this specifically confirms `tests/ai/test_ai.py`, `tests/ai/test_provider.py`, `tests/e2e/test_complete_flow.py`, `tests/e2e/test_demo_check.py`, and `tests/test_security.py`, none of which are modified by this plan, still pass with `session_id` now optional on `ChatRequest`)

- [ ] **Step 6: Commit**

```bash
git add backend/src/network_copilot/ai/schemas.py backend/src/network_copilot/ai/routes.py backend/tests/ai/test_chat_history.py
git commit -m "feat: thread session_id through POST /api/ai/chat"
```

---

### Task 6: Frontend — real session switching, replacing the cutoff mechanism

**Files:**
- Modify: `backend/src/network_copilot/static/js/app.js`
- Modify: `backend/src/network_copilot/templates/index.html:56-67`
- Modify: `backend/src/network_copilot/static/css/app.css:944-977`

**Interfaces:**
- Consumes: `POST /api/chat/sessions`, `GET /api/chat/sessions`, `GET /api/chat/messages?session_id=`, `POST /api/ai/chat` with `session_id` in the body (Tasks 4-5).
- Produces: nothing consumed by another task — this is the last task in the plan.

- [ ] **Step 1: Remove the cutoff state, getters, and methods from `app.js`**

Find (the chat state block, `chatCutoff` comment and field):

```javascript
    // -- chat --
    messages: [],
    draftMessage: "",
    sending: false,
    _messagesRefreshGeneration: 0,
    _clientMessageSequence: 0,
    // ISO timestamp string, or null. Set by startNewChat(), cleared by
    // showFullHistory() and by logout(). Persisted to localStorage so it
    // survives a page refresh, but never sent to the server: this hides
    // history in this browser only, per the design's explicit scope.
    chatCutoff: localStorage.getItem("nc_chat_cutoff") || null,
```

Replace with:

```javascript
    // -- chat --
    messages: [],
    draftMessage: "",
    sending: false,
    _messagesRefreshGeneration: 0,
    _clientMessageSequence: 0,
    sessions: [],
    // A per-browser bookmark of which shared session this browser was last
    // looking at (see the design spec: sessions are shared team data, but
    // which one *you* are currently viewing is a per-browser preference,
    // same role nc_token/nc_user already play for auth state).
    currentSessionId: (() => {
      const stored = localStorage.getItem("nc_session_id");
      return stored ? Number(stored) : null;
    })(),
```

Find (the `visibleMessages`/`hiddenMessageCount` getters and the old `startNewChat`/`showFullHistory` methods):

```javascript
    get visibleMessages() {
      if (!this.chatCutoff) return this.messages;
      // Reuse _messageTimestamp() (defined further down in this component)
      // rather than comparing created_at strings directly: it normalises
      // server timestamps that arrive without a timezone suffix, which a
      // naive string comparison against an always-suffixed chatCutoff
      // (from toISOString()) would get wrong.
      const cutoffTime = Date.parse(this.chatCutoff);
      return this.messages.filter((message) => {
        // A change/batch action card lives inside the message that first
        // proposed it, and keeps updating in place (via changesById /
        // batchesById) as it moves through approve/apply/results - even
        // long after "New chat" was clicked. Hiding that message would
        // hide the only place its outcome is shown, so these are exempt
        // from the cutoff; only plain conversational messages are hidden.
        const payload = message.payload;
        if (payload && (payload.change || payload.batch)) return true;
        const timestamp = this._messageTimestamp(message);
        return timestamp === null || timestamp > cutoffTime;
      });
    },

    get hiddenMessageCount() {
      return this.messages.length - this.visibleMessages.length;
    },

    startNewChat() {
      this.chatCutoff = new Date().toISOString();
      localStorage.setItem("nc_chat_cutoff", this.chatCutoff);
    },

    showFullHistory() {
      this.chatCutoff = null;
      localStorage.removeItem("nc_chat_cutoff");
    },
```

Replace with (empty — the getters are gone entirely; `startNewChat`/session switching are added as full methods in Step 3, further down the file, to keep them near the other `async` chat methods):

```javascript
```

(That is: delete this whole block, leaving nothing in its place. The `pendingChanges`/`pendingBatches` getters immediately above it are untouched.)

- [ ] **Step 2: Update `logout()` to clear session state instead of the cutoff**

Find:

```javascript
      this.messages = [];
      this.draftMessage = "";
      this.sending = false;
      this.chatCutoff = null;
      localStorage.removeItem("nc_chat_cutoff");
    },
```

Replace with:

```javascript
      this.messages = [];
      this.draftMessage = "";
      this.sending = false;
      this.sessions = [];
      this.currentSessionId = null;
      localStorage.removeItem("nc_session_id");
    },
```

- [ ] **Step 3: Load sessions during `startApp()`, and add session switch/create methods**

Find:

```javascript
    async startApp() {
      const generation = this._sessionGeneration;
      this.changesLoading = true;
      this.batchesLoading = true;
      const bootstrap = [
        () => this.hydrateMessages(),
        () => this.refreshDevices(),
        () => this.refreshChanges(),
        () => this.refreshBatches(),
      ];
```

Replace with:

```javascript
    async startApp() {
      const generation = this._sessionGeneration;
      this.changesLoading = true;
      this.batchesLoading = true;
      const bootstrap = [
        () => this.loadSessions(),
        () => this.hydrateMessages(),
        () => this.refreshDevices(),
        () => this.refreshChanges(),
        () => this.refreshBatches(),
      ];
```

Then find the `_scrollToBottom()` method (just before `hydrateMessages()`):

```javascript
    _scrollToBottom() {
      const el = this.$refs.chatLog;
      if (el) el.scrollTop = el.scrollHeight;
    },

    async hydrateMessages() {
```

Insert the new session methods between them:

```javascript
    _scrollToBottom() {
      const el = this.$refs.chatLog;
      if (el) el.scrollTop = el.scrollHeight;
    },

    async loadSessions() {
      const data = await this.authFetch("/api/chat/sessions");
      this.sessions = data.items;
      if (this.sessions.length === 0) {
        const session = await this.authFetch("/api/chat/sessions", {
          method: "POST",
        });
        this.sessions = [session];
      }
      const stillExists =
        this.currentSessionId != null &&
        this.sessions.some((session) => session.id === this.currentSessionId);
      if (!stillExists) {
        this.currentSessionId = this.sessions[0].id;
      }
      localStorage.setItem("nc_session_id", String(this.currentSessionId));
    },

    async switchSession(sessionId) {
      if (sessionId === this.currentSessionId) return;
      this._messagesRefreshGeneration += 1;
      this.currentSessionId = sessionId;
      localStorage.setItem("nc_session_id", String(sessionId));
      this.messages = [];
      try {
        await this.hydrateMessages();
      } catch {
        // A later scheduled poll can retry.
      }
    },

    async startNewChat() {
      try {
        const session = await this.authFetch("/api/chat/sessions", {
          method: "POST",
        });
        this.sessions.unshift(session);
        await this.switchSession(session.id);
      } catch (err) {
        alert(err.message);
      }
    },

    async hydrateMessages() {
```

(The `async hydrateMessages() {` line at the end is the original line already in the file — this step only inserts new content above it, it does not duplicate that line.)

- [ ] **Step 4: Pass `session_id` in `hydrateMessages()`, `pollMessages()`, and `sendMessage()`**

Find:

```javascript
    async hydrateMessages() {
      const generation = this._messagesRefreshGeneration;
      const data = await this.authFetch("/api/chat/messages");
```

Replace with:

```javascript
    async hydrateMessages() {
      const generation = this._messagesRefreshGeneration;
      const data = await this.authFetch(
        `/api/chat/messages?session_id=${this.currentSessionId}`
      );
```

Find:

```javascript
    async pollMessages() {
      const generation = this._messagesRefreshGeneration;
      const wasAtBottom = this._isScrolledToBottom();
      const data = await this.authFetch("/api/chat/messages");
```

Replace with:

```javascript
    async pollMessages() {
      const generation = this._messagesRefreshGeneration;
      const wasAtBottom = this._isScrolledToBottom();
      const data = await this.authFetch(
        `/api/chat/messages?session_id=${this.currentSessionId}`
      );
```

Find:

```javascript
        const payload = await this.authFetch("/api/ai/chat", {
          method: "POST",
          body: JSON.stringify({ message: text }),
        });
```

Replace with:

```javascript
        const payload = await this.authFetch("/api/ai/chat", {
          method: "POST",
          body: JSON.stringify({
            message: text,
            session_id: this.currentSessionId,
          }),
        });
```

- [ ] **Step 5: Replace the toolbar markup in `templates/index.html`**

Find:

```html
      <main class="chat-panel">
        <div class="chat-toolbar">
          <button type="button" class="new-chat-btn" @click="startNewChat()">
            New chat
          </button>
          <span class="chat-hidden-banner" x-show="chatCutoff" x-cloak>
            Đã ẩn <span x-text="hiddenMessageCount"></span> tin nhắn cũ ·
            <a href="#" @click.prevent="showFullHistory()">Xem toàn bộ lịch sử</a>
          </span>
        </div>
        <div class="chat-log" x-ref="chatLog">
          <template x-for="message in visibleMessages" :key="message.id">
```

Replace with:

```html
      <main class="chat-panel">
        <div class="chat-toolbar">
          <select
            class="session-select"
            x-model.number="currentSessionId"
            @change="switchSession(currentSessionId)"
          >
            <template x-for="session in sessions" :key="session.id">
              <option :value="session.id" x-text="session.title"></option>
            </template>
          </select>
          <button type="button" class="new-chat-btn" @click="startNewChat()">
            New chat
          </button>
        </div>
        <div class="chat-log" x-ref="chatLog">
          <template x-for="message in messages" :key="message.id">
```

- [ ] **Step 6: Update the toolbar CSS**

Find (`static/css/app.css`, lines 944-977):

```css
/* -- Chat toolbar (New chat / hidden history) -- */

.chat-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}

.new-chat-btn {
  border: 1px solid var(--border);
  border-radius: 4px;
  background: transparent;
  color: var(--text);
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
}

.new-chat-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.chat-hidden-banner {
  color: var(--text-muted);
}

.chat-hidden-banner a {
  color: var(--accent);
}
```

Replace with:

```css
/* -- Chat toolbar (session switcher / New chat) -- */

.chat-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}

.session-select {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface);
  color: var(--text);
  padding: 5px 8px;
  font-size: 12px;
}

.new-chat-btn {
  border: 1px solid var(--border);
  border-radius: 4px;
  background: transparent;
  color: var(--text);
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.new-chat-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}
```

- [ ] **Step 7: Run the full backend test suite to confirm no regression**

No Python file changed in this task, but this confirms the working tree edit didn't break anything else.

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass

- [ ] **Step 8: Manually verify in a real browser**

Start the backend (from `backend/`): `../.venv/Scripts/python.exe -m flask --app wsgi.py run` (or the `.claude/launch.json` "dashboard-check" preview config).

- Log in for the first time on a browser with no `nc_session_id` in `localStorage`: confirm a session is created automatically (the dropdown shows one entry, "New chat") and the chat log is empty (or shows the migrated legacy history, if this is the first login against a database that had pre-existing chat messages — Task 1's migration session should appear).
- Send a message: confirm the session's entry in the dropdown updates its title to that message's text on the next `loadSessions()`/page load (title derivation is read-time only, so it will not update live without a reload or session switch — confirm this matches the spec's design, not a bug).
- Click "New chat": confirm a new "New chat" entry appears at the top of the dropdown, is auto-selected, and the chat log goes empty.
- Send a message in the new session, then switch back to the older session via the dropdown: confirm only that session's own messages show, not the ones just sent in the other session.
- Approve a batch from the "Thay đổi đang chờ" sidebar while viewing a session that is not the one where the batch was originally proposed: confirm the sidebar still reflects the outcome correctly (it was never session-scoped) — this is the scenario that motivated this whole feature; confirm it is genuinely resolved.
- Refresh the page: confirm it returns to the same session (via the `nc_session_id` `localStorage` bookmark).
- Log out and back in: confirm the session list still shows every session (they are shared/team data, not cleared by `logout()` beyond the local bookmark).

- [ ] **Step 9: Commit**

```bash
git add backend/src/network_copilot/static/js/app.js backend/src/network_copilot/templates/index.html backend/src/network_copilot/static/css/app.css
git commit -m "feat: replace the chat cutoff filter with real, shared chat sessions"
```
