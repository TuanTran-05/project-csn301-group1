"""Credential-safe audit views and authenticated, encrypted backup envelopes.

The process key is never exported. A durable key may be injected explicitly by
the caller; this module never reads/writes environment or key files.
"""
from copy import deepcopy
import json
import re

from cryptography.fernet import Fernet, InvalidToken

REDACTED = "***REDACTED***"
MAX_BACKUP_BYTES = 2 * 1024 * 1024


class BackupSizeError(ValueError):
    """No export is returned when it cannot be imported under the same limit."""


_PROCESS_CIPHER = Fernet(Fernet.generate_key())
_KEY_MARKERS = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
                "auth", "credential", "private_key", "session_key", "passphrase", "community", "pre_shared", "psk", "key_string")
_INLINE = re.compile(
    r"\b(password|passwd|secret|token|key|api[_-]?key|credential|community|passphrase|"
    r"pre[-_ ]shared[-_ ]key|key[-_ ]string|authentication[-_ ]key|private[-_ ]key)\b"
    r"(?=\s|[:=])\s*[:=]?\s*"
    r"(?:[0-9]\s+(?=[\"']|\S+[^\S\r\n]*(?:[\r\n]|\Z)))?"
    r'(?:"(?:\\[\s\S]|[^"\\])*(?:"|\\?\Z)'
    r"|'(?:\\[\s\S]|[^'\\])*(?:'|\\?\Z)"
    r"|\S[^\r\n]*)[^\r\n]*", re.I)


def redact_sensitive_data(value, force=False, hide_commands=False):
    """Mask secret values, including wrapped IOS encoding and quoted values.

    Separators may wrap; quoted values end at their unescaped closing quote.
    A one-digit encoding may precede a quoted value or one unquoted token.
    A multiword diagnostic line does not qualify as a wrapped encoded value.
    Unquoted values and closing-quote suffixes stop at the current line end.
    An unterminated quote masks the remaining text rather than leaking it.
    Audit commands are masked as a whole, including unknown vendor syntaxes.
    Authorized draft/configuration responses retain full data for editing.
    """
    if isinstance(value, dict):
        return {key: redact_sensitive_data(item, force or any(m in str(key).lower() for m in _KEY_MARKERS)
                    or (hide_commands and ("command" in str(key).lower() or str(key).lower() == "description")), hide_commands)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_data(item, force, hide_commands) for item in value]
    # A flag such as "has_credential" or an absent value cannot carry a secret.
    if force and not (value is None or isinstance(value, bool)):
        return REDACTED
    if isinstance(value, str):
        return _INLINE.sub(lambda match: match.group(1) + " " + REDACTED, value)
    return value


def protected_diff(changes):
    allowed_paths = {"schema", "changes", "batches", "id", "device_id", "batch_id", "description",
                     "commands", "verification_commands", "execution_mode", "interfaces", "nodes",
                     "vlan", "enabled", "values", "target_hostname", "target_management_ip",
                     "target_ssh_port", "target_device_type"}
    result = []
    for change in changes:
        item = {"op": change["op"], "path": "/".join(
            part if not part or part in allowed_paths else REDACTED
            for part in change["path"].split("/"))}
        for key in ("before", "after"):
            if key in change:
                item[key] = REDACTED
        result.append(item)
    return result


def cipher_for(key=None):
    return Fernet(key) if key is not None else _PROCESS_CIPHER


_ACTIONS = {"BASELINE", "CONFIG_INT", "ADD_NODE", "EDIT", "UNDO", "REDO", "RESTORE", "CHECKPOINT",
            "CREATE_PENDING", "CREATE_PENDING_BATCH", "CANCEL_PENDING", "CANCEL_PENDING_BATCH", "EDIT_PENDING"}


def public_event(event, snapshot, actor_alias, counts):
    """Allowlisted monitoring schema; never copy arbitrary metadata/configuration."""
    summary = {"kind": "json-object", "property_count": len(snapshot)}
    if snapshot.get("schema") == "pending-v1":
        summary = {"schema": "pending-v1"}
        for field in ("changes", "batches"):
            rows = snapshot.get(field, [])
            summary[field + "_count"] = len(rows) if isinstance(rows, list) else 0
    timestamp = event["timestamp"]
    if not isinstance(timestamp, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", timestamp) is None:
        timestamp = "invalid"
    action = event["action_type"] if isinstance(event["action_type"], str) and event["action_type"] in _ACTIONS else "OTHER"
    step = event["step"] if type(event["step"]) is int and event["step"] > 0 else 0
    return {"step": step, "timestamp": timestamp, "actor": actor_alias,
            "action_type": action, "description": "Draft history event",
            "snapshot": summary, "diff_from_previous": "; ".join(f"{op}={counts[op]}" for op in ("add", "remove", "replace"))}


def _change_counts(before, after):
    if isinstance(before, dict) and isinstance(after, dict):
        counts = {"add": len(after.keys() - before.keys()), "remove": len(before.keys() - after.keys()), "replace": 0}
        for key in before.keys() & after.keys():
            child = _change_counts(before[key], after[key])
            for op in counts:
                counts[op] += child[op]
        return counts
    return {"add": 0, "remove": 0, "replace": int(type(before) is not type(after) or
            json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True))}


def public_audit(trail):
    actors, previous, result = {}, {}, []
    for event in trail:
        actor = actors.setdefault(event["actor"], f"actor-{len(actors) + 1}")
        snapshot = event["snapshot"]
        result.append(public_event(event, snapshot, actor, _change_counts(previous, snapshot)))
        previous = snapshot
    return result


# Frozen 1.1 rules: these authenticate old envelopes, not new audit output.
# Keep them independent from future changes to the active redaction policy.
_LEGACY_KEY_MARKERS = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
                       "auth", "credential", "private_key", "session_key", "passphrase", "community",
                       "pre_shared", "psk", "key_string")
_LEGACY_INLINE = re.compile(
    r"\b(password|passwd|secret|token|key|api[_-]?key|credential|community|passphrase|"
    r"pre[-_ ]shared[-_ ]key|key[-_ ]string|authentication[-_ ]key|private[-_ ]key)\b"
    r"(?=\s|[:=])\s*[:=]?\s*\S.*", re.I | re.S)


def _legacy_redact(value, force=False):
    """Historical recursive projection with hide_commands=True."""
    if isinstance(value, dict):
        return {key: _legacy_redact(item, force or any(m in str(key).lower() for m in _LEGACY_KEY_MARKERS)
                                   or "command" in str(key).lower() or str(key).lower() == "description")
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_legacy_redact(item, force) for item in value]
    if force:
        return REDACTED
    if isinstance(value, str):
        return _LEGACY_INLINE.sub(lambda match: match.group(1) + " " + REDACTED, value)
    return value


def _legacy_audit_view(data):
    """Exact 1.1 projection, only for authenticating existing encrypted backups."""
    result = deepcopy(data)
    result["backup_version"] = "1.1"
    result["backup_kind"] = "restorable_encrypted"
    result["audit_trail"] = _legacy_redact(data["audit_trail"])
    return result


def audit_view(data):
    timestamp = data["exported_at"]
    if not isinstance(timestamp, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", timestamp) is None:
        timestamp = "invalid"
    state = data["history_state"] if isinstance(data["history_state"], dict) else {}
    safe_state = {}
    for field in ("undo_stack", "redo_stack"):
        indices = state.get(field, [])
        safe_state[field] = [i for i in indices if type(i) is int and i >= 0] if isinstance(indices, list) else []
    return {"backup_version": "1.2", "backup_kind": "restorable_encrypted",
            "exported_at": timestamp, "timezone": "UTC+07:00",
            "key_scope": data.get("key_scope", "process") if data.get("key_scope", "process") in ("process", "caller") else "unknown",
            "audit_trail": public_audit(data["audit_trail"]),
            "history_state": safe_state}


def encode_backup(data, cipher):
    public = audit_view(data)
    public["encrypted_history"] = cipher.encrypt(json.dumps(
        data, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")).decode("ascii")
    return public


def decode_backup(data, cipher):
    if not isinstance(data, dict):
        raise ValueError("Invalid backup object")
    if data.get("backup_version") in {"1.1", "1.2"}:
        try:
            token = data["encrypted_history"]
            if not isinstance(token, str):
                raise ValueError("Invalid encrypted history")
            private = json.loads(cipher.decrypt(token.encode("ascii")))
        except (KeyError, ValueError, UnicodeError, InvalidToken, TypeError) as exc:
            raise ValueError("Backup key is unavailable or encrypted data is invalid") from None
        public = {key: value for key, value in data.items() if key != "encrypted_history"}
        expected = audit_view(private) if data["backup_version"] == "1.2" else _legacy_audit_view(private)
        if public != expected:
            raise ValueError("Backup metadata or audit was modified")
        return private
    if data.get("backup_version") != "1.0":
        raise ValueError("Unsupported backup version")
    # Never treat a redacted audit-only payload as a full configuration backup.
    trail = data.get("audit_trail", [])
    snapshots = [event.get("snapshot") for event in trail if isinstance(event, dict)] if isinstance(trail, list) else []
    if data.get("backup_kind") or REDACTED in json.dumps(snapshots, ensure_ascii=False):
        raise ValueError("Audit-only or redacted data cannot be restored")
    if redact_sensitive_data(data) != data:
        raise ValueError("Plaintext backups containing credentials cannot be restored")
    return data
