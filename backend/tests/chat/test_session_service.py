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
    # older gets a message after newer is created, so its activity time is
    # now the latest overall - it should sort above newer, which has no
    # messages and falls back to its own (earlier) created_at.
    _add_message(older.id, "hello")
    items = list_sessions()
    assert [item["id"] for item in items] == [older.id, newer.id]


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
