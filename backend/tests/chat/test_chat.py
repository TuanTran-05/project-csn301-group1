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


def test_record_message_persists_a_row(app):
    record_message(1, "g1", "user", "hello")
    assert db.session.query(ChatMessage).count() == 1


def test_record_message_stores_the_payload(app):
    record_message(1, "g1", "assistant", "done", {"intent": "monitor"})
    row = db.session.query(ChatMessage).one()
    assert row.payload == {"intent": "monitor"}


def test_record_message_accepts_a_missing_user(app):
    record_message(None, None, "system", "blocked")
    row = db.session.query(ChatMessage).one()
    assert row.user_id is None
    assert row.username is None


def test_record_message_never_raises(app, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(db.session, "commit", boom)
    result = record_message(1, "g1", "user", "hello")
    assert result is None


def test_list_messages_orders_oldest_first(app):
    record_message(1, "g1", "user", "first")
    record_message(1, "g1", "assistant", "second")
    rows = list_messages()
    assert [row.content for row in rows] == ["first", "second"]


def test_list_messages_returns_the_most_recent_window_oldest_first(app):
    for i in range(5):
        record_message(1, "g1", "user", f"message {i}")

    rows = list_messages(limit=2)

    assert [row.content for row in rows] == ["message 3", "message 4"]


def test_messages_endpoint_requires_authentication(client):
    assert client.get("/api/chat/messages").status_code == 401


def test_messages_endpoint_is_readable_by_viewer(client, viewer_headers):
    response = client.get("/api/chat/messages", headers=viewer_headers)
    assert response.status_code == 200
    assert response.get_json()["items"] == []


def test_messages_endpoint_returns_recorded_messages(client, admin_headers, app):
    record_message(1, "g1", "user", "hello")
    record_message(1, "g1", "assistant", "hi there")
    response = client.get("/api/chat/messages", headers=admin_headers)
    items = response.get_json()["items"]
    assert len(items) == 2
    assert items[0]["content"] == "hello"
    assert items[1]["content"] == "hi there"
