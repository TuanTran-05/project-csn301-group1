"""Remembered PNETLab login per project (Thiết bị -> Nhập từ PNETLab).

The import and console-bootstrap requests carry the PNETLab address and
account. When a request leaves the password empty, the project's saved one is
used; when it asks to ``remember``, a login that just worked is saved
(encrypted) for next time. The password never goes back to the browser.
"""

from ..credentials.model import ProjectPnetlabLogin
from ..credentials.service import get_cipher
from ..errors import ValidationError
from ..extensions import db

_FORM_KEYS = ("url", "username", "lab", "api_mode", "verify_tls")


def get_saved(project_id: int) -> ProjectPnetlabLogin | None:
    return db.session.query(ProjectPnetlabLogin).filter_by(project_id=project_id).one_or_none()


def fill_payload(project, payload: dict) -> tuple[dict, bool]:
    """The request with the saved login filled in, and whether to remember it.

    Only an empty password is filled: a typed one always wins, and the saved
    password is used only for the address and account it was saved with, so
    it is never sent to a different server.
    """
    payload = dict(payload or {})
    remember = bool(payload.pop("remember", False))
    if payload.get("password"):
        return payload, remember
    saved = get_saved(project.id)
    if saved is None:
        return payload, remember
    url = (payload.get("url") or "").strip() or saved.url
    username = (payload.get("username") or "").strip() or saved.username
    if url.rstrip("/") != saved.url.rstrip("/") or username != saved.username:
        raise ValidationError(
            "Nhập mật khẩu PNETLab: mật khẩu đã lưu chỉ dùng cho "
            f"{saved.username} @ {saved.url}.",
            {"password": ["Required."]},
        )
    try:
        password = get_cipher().decrypt(saved.password_encrypted)
    except ValueError as exc:
        raise ValidationError(
            "Không đọc được mật khẩu PNETLab đã lưu (khoá mã hoá của server đã đổi); nhập lại.",
            {"password": ["Required."]},
        ) from exc
    payload.update(url=url, username=username, password=password)
    payload.setdefault("lab", saved.lab or "")
    payload.setdefault("api_mode", saved.api_mode)
    payload.setdefault("verify_tls", saved.verify_tls)
    return payload, remember


def remember(project, payload: dict, user_id: int | None) -> ProjectPnetlabLogin:
    """Save a login that just worked."""
    record = get_saved(project.id)
    if record is None:
        record = ProjectPnetlabLogin(project_id=project.id)
        db.session.add(record)
    record.url = payload["url"].strip()
    record.username = payload["username"].strip()
    record.password_encrypted = get_cipher().encrypt(payload["password"])
    record.lab = (payload.get("lab") or "").strip()
    record.api_mode = payload.get("api_mode") or "auto"
    record.verify_tls = bool(payload.get("verify_tls", True))
    record.updated_by_id = user_id
    db.session.commit()
    return record


def forget(project) -> bool:
    record = get_saved(project.id)
    if record is None:
        return False
    db.session.delete(record)
    db.session.commit()
    return True
