import re

from ..audit.service import redact_sensitive
from ..extensions import db
from ..history_extension.protection import redact_sensitive_data

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----", re.S
)
# Treat the whole credential-bearing IOS/ASA line as sensitive, including
# numbered/typed password hashes that a one-token substitution would miss.
_CREDENTIAL_LINE = re.compile(
    r"(?im)^.*\b(?:password|secret|community|key|private-key|pre-shared-key|key-string|"
    r"authentication-key|encryption-key)\b.*$"
)


# Prose that only mentions a credential word ("### Key concepts", "Key points")
# is not a credential line; masking it only garbles AI answers.
_PROSE_LINE = re.compile(
    r"^\s*#|\bkey\s+(?:concepts?|points?|takeaways?|terms?|ideas?|features?|differences?|steps?|benefits?)\b",
    re.I,
)


def _mask_credential_line(match: re.Match) -> str:
    line = match.group(0)
    return line if _PROSE_LINE.search(line) else "[REDACTED credential line]"


def _redact_except_prose(text: str) -> str:
    """Inline masking for every line except prose headings such as "Key concepts"."""
    lines = text.split("\n")
    kept = {index: line for index, line in enumerate(lines) if _PROSE_LINE.search(line)}
    if not kept:
        return redact_sensitive_data(text)
    marker = "\x00prose{}\x00"
    masked = redact_sensitive_data("\n".join(
        marker.format(index) if index in kept else line for index, line in enumerate(lines)))
    for index, line in kept.items():
        masked = masked.replace(marker.format(index), line, 1)
    return masked


def may_read_commands(user) -> bool:
    """Whether a non-Admin may read the commands of changes and batches.

    An Operator, or the owner of the project the request is about: they
    approve those changes, so they must see what will be sent. Everyone else
    (Viewer, Teacher, Student) keeps seeing command lists masked.
    """
    from flask import g, has_request_context

    from .access import role

    if role(user) == "OPERATOR":
        return True
    if not has_request_context():
        return False
    from ..projects.model import Project
    from ..projects.scope import _requested_project_id

    try:
        project_id = g.get("project_id") or _requested_project_id()
    except Exception:  # a malformed header: the route already refused it
        return False
    project = db.session.get(Project, project_id) if project_id else None
    return project is not None and project.owner_id == user.id


def redact_output(value, commands: bool = False):
    """Mask secrets in what the API returns to a non-Admin.

    Credential lines, private keys and values under credential-like keys
    always go. With ``commands`` (see may_read_commands) ordinary commands stay
    readable, so "interface Gi0/0 / ip address ..." can be checked in a batch
    before it is approved; without it every command list is masked whole, as
    the audit log does (audit.redact_sensitive).
    """
    if isinstance(value, str):
        value = _PRIVATE_KEY.sub("[REDACTED private key]", value)
        value = _CREDENTIAL_LINE.sub(_mask_credential_line, value)
        return _redact_except_prose(value)
    if isinstance(value, dict):
        safe = redact_sensitive_data(value) if commands else redact_sensitive(value)
        return {key: redact_output(item, commands) for key, item in safe.items()}
    if isinstance(value, list):
        return [redact_output(item, commands) for item in value]
    return value
