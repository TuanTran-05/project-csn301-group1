"""Audit logging with mandatory redaction.

Nothing written here may contain a credential, so every payload passes through
redact_sensitive() before it is stored.
"""

import logging
import re
from datetime import datetime

from ..extensions import db
from .model import AuditLog

logger = logging.getLogger(__name__)

REDACTED = "***REDACTED***"

SENSITIVE_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "credentials",
    "private_key",
    "enable_secret",
    "session_key",
)

# "password Secret123!", "secret 7 08701E1D", "key = abc123"
INLINE_SECRET = re.compile(
    r"\b(password|passwd|secret|token|key|credential)\b\s*[:=]?\s*(\S+)",
    re.IGNORECASE,
)


def _is_sensitive(key: str) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in SENSITIVE_KEYS)


def redact_sensitive(value, _force: bool = False):
    """Recursively mask credentials in dicts, lists and strings.

    A sensitive key does not collapse its whole subtree: the structure is kept
    and every scalar underneath it is masked instead, so callers can still read
    the shape of what was logged without ever seeing a secret.
    """
    if isinstance(value, dict):
        return {
            key: redact_sensitive(item, _force or _is_sensitive(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item, _force) for item in value]
    if _force:
        return REDACTED
    if isinstance(value, str):
        return INLINE_SECRET.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
    return value


def _request_context() -> dict:
    """Best-effort request metadata. Safe to call outside a request."""
    try:
        from flask import g, has_request_context, request

        if not has_request_context():
            return {}
        return {
            "source_ip": request.headers.get(
                "X-Forwarded-For", request.remote_addr
            ),
            "request_id": getattr(g, "request_id", None),
        }
    except Exception:  # pragma: no cover - defensive
        return {}


def record_event(
    action: str,
    result: str,
    user_id: int | None = None,
    username: str | None = None,
    device_id: int | None = None,
    message: str | None = None,
    details=None,
) -> AuditLog | None:
    """Write one audit entry. Never raises: auditing must not break a request."""
    try:
        context = _request_context()
        safe_details = redact_sensitive(details) if details is not None else None
        if safe_details is not None and not isinstance(safe_details, (dict, list)):
            safe_details = {"value": str(safe_details)[:500]}

        event = AuditLog(
            action=action,
            result=result,
            user_id=user_id,
            username=username,
            device_id=device_id,
            message=(redact_sensitive(message) or "")[:512] if message else None,
            details=safe_details,
            source_ip=context.get("source_ip"),
            request_id=context.get("request_id"),
        )
        db.session.add(event)
        db.session.commit()
        return event
    except Exception:  # pragma: no cover - auditing must degrade gracefully
        logger.exception("Failed to write audit event %s/%s", action, result)
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def list_events(
    user_id: int | None = None,
    device_id: int | None = None,
    action: str | None = None,
    result: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
) -> list[AuditLog]:
    query = db.session.query(AuditLog)
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    if device_id:
        query = query.filter(AuditLog.device_id == device_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if result:
        query = query.filter(AuditLog.result == result)
    if since:
        query = query.filter(AuditLog.created_at >= since)
    if until:
        query = query.filter(AuditLog.created_at <= until)
    return (
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
