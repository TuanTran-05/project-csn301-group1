from network_copilot.chat.model import ChatMessage
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
