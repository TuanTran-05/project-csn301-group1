"""JSON draft snapshots, branching undo/redo and an append-only event journal.

The first pushed snapshot is the baseline. This module never applies device
configuration. Times use UTC+07:00 explicitly, independent of host timezone.
"""

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
import math
from threading import RLock

from .protection import (BackupSizeError, MAX_BACKUP_BYTES, cipher_for, decode_backup, encode_backup,
                         protected_diff, public_event)


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LOCAL_TIME = timezone(timedelta(hours=7))


def _now():
    return datetime.now(LOCAL_TIME).strftime(TIME_FORMAT)


def _json_copy(value):
    def validate(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                validate(child)
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        raise ValueError("Snapshots must contain finite JSON values and string keys")
    try:
        validate(value)
    except RecursionError as exc:
        raise ValueError("Snapshot is cyclic or nested too deeply") from exc
    return deepcopy(value)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _diff(before, after, path=""):
    """JSON Pointer paths; lists are replaced as a whole to avoid index ambiguity."""
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before:
                changes.append({"op": "add", "path": child, "after": deepcopy(after[key])})
            elif key not in after:
                changes.append({"op": "remove", "path": child, "before": deepcopy(before[key])})
            else:
                changes.extend(_diff(before[key], after[key], child))
        return changes
    # Python considers True == 1 even inside lists; JSON types must stay distinct.
    if type(before) is type(after) and json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True):
        return []
    return [{"op": "replace", "path": path, "before": deepcopy(before), "after": deepcopy(after)}]


def _summary(changes):
    return "; ".join(f"{c['op']} {c['path'] or '/'}" for c in changes) or "No changes"


@dataclass(frozen=True, eq=False)
class HistoryState:
    """RAM payload only. Locks, ciphers and publication receipts are not copied."""

    histories: dict = field(default_factory=dict)
    expected: dict | None = None
    revision_epoch: int = 0
    generation: int = 0
    needs_reconcile: bool = False


class HistoryManager:
    """One instance per user/workspace; each topology owns separate stacks.

    Empty undo/redo returns None. Public reads and returned snapshots are copies.
    In-memory storage is thread safe, but not shared between server processes.
    """

    def __init__(self, *, backup_key=None):
        self._state = HistoryState()
        self._lock = RLock()
        self._cipher = cipher_for(backup_key)
        self._key_scope = "caller" if backup_key is not None else "process"

    @property
    def _histories(self):
        # Compatibility view for existing private readers; never serialized as a holder.
        return self._state.histories

    def _install_state(self, base, target):
        """The only live payload publication; retries install the same object."""
        with self._lock:
            if self._state is target:
                return
            if self._state is not base:
                raise ValueError("History publication generation changed")
            self._state = target

    def _history_candidate(self, base, histories):
        return replace(base, histories=histories, expected=deepcopy(base.expected), generation=base.generation + 1,
                       needs_reconcile=base.expected is not None)

    def _history(self, topology_id, histories=None):
        _text(topology_id, "topology_id")
        empty = {"undo": [], "redo": [], "audit": [], "states": [], "events": [], "actors": {}}
        if histories is None:
            # Reads must not create a topology in a published generation.
            return self._state.histories.get(topology_id, empty)
        return histories.setdefault(topology_id, empty)

    @staticmethod
    def _snapshot(history):
        return deepcopy(history["states"][history["undo"][-1]]) if history["undo"] else None

    def _event(self, history, snapshot, actor, action_type, description):
        previous = history["states"][-1] if history["states"] else {}
        changes = _diff(previous, snapshot)
        metadata = {
            "step": len(history["audit"]) + 1, "timestamp": _now(),
            "actor": actor, "action_type": action_type, "description": description,
            "diff_from_previous": _summary(changes),
        }
        counts = {op: sum(c["op"] == op for c in changes) for op in ("add", "remove", "replace")}
        alias = history["actors"].setdefault(actor, f"actor-{len(history['actors']) + 1}")
        history["audit"].append(public_event(metadata, snapshot, alias, counts))
        history["events"].append(metadata)
        history["states"].append(deepcopy(snapshot))
        return len(history["audit"]) - 1

    def push_state(self, snapshot, actor, action_type, description, topology_id="default"):
        snapshot = _json_copy(snapshot)
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be a JSON object")
        for name, value in (("actor", actor), ("action_type", action_type), ("description", description)):
            _text(value, name)
        with self._lock:
            base = self._state
            histories = deepcopy(base.histories)
            history = self._history(topology_id, histories)
            index = self._event(history, snapshot, actor, action_type, description)
            history["undo"].append(index)
            history["redo"].clear()
            result = deepcopy(snapshot)
            self._install_state(base, self._history_candidate(base, histories))
            return result

    def current_snapshot(self, topology_id="default"):
        with self._lock:
            history = self._history(topology_id)
            return self._snapshot(history)

    def checkpoint(self, snapshot, actor, description, topology_id="default"):
        """Preserve audit while establishing a barrier against stale undo states."""
        snapshot = _json_copy(snapshot)
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be a JSON object")
        _text(actor, "actor")
        _text(description, "description")
        with self._lock:
            base = self._state
            histories = deepcopy(base.histories)
            history = self._history(topology_id, histories)
            index = self._event(history, snapshot, actor, "CHECKPOINT", description)
            history["undo"] = [index]
            history["redo"].clear()
            self._install_state(base, self._history_candidate(base, histories))

    def fork(self):
        """Stage changes without publishing stacks before a DB commit succeeds."""
        with self._lock:
            result = HistoryManager()
            result._state = deepcopy(self._state)
            result._cipher = self._cipher
            result._key_scope = self._key_scope
            return result

    def active_snapshots(self, topology_id="default"):
        """Private configuration views for backend validation, never an audit export."""
        with self._lock:
            history = self._history(topology_id)
            return [deepcopy(history["states"][i]) for i in history["undo"] + history["redo"]]

    def can_undo(self, topology_id="default"):
        with self._lock:
            return len(self._history(topology_id)["undo"]) > 1

    def can_redo(self, topology_id="default"):
        with self._lock:
            return bool(self._history(topology_id)["redo"])

    def _move(self, redo, topology_id, actor, description):
        _text(actor, "actor")
        _text(description, "description")
        with self._lock:
            base = self._state
            histories = deepcopy(base.histories)
            history = self._history(topology_id, histories)
            if redo:
                if not history["redo"]:
                    return None
                history["undo"].append(history["redo"].pop())
            else:
                if len(history["undo"]) < 2:
                    return None
                history["redo"].append(history["undo"].pop())
            snapshot = self._snapshot(history)
            self._event(history, snapshot, actor, "REDO" if redo else "UNDO", description)
            self._install_state(base, self._history_candidate(base, histories))
            return snapshot

    def undo(self, topology_id="default", actor="system", description="Undo draft configuration"):
        return self._move(False, topology_id, actor, description)

    def redo(self, topology_id="default", actor="system", description="Redo draft configuration"):
        return self._move(True, topology_id, actor, description)

    def get_diff(self, topology_id="default"):
        """Compare current draft to previous state on the active undo branch."""
        with self._lock:
            history = self._history(topology_id)
            if len(history["undo"]) < 2:
                return []
            return protected_diff(_diff(history["states"][history["undo"][-2]], self._snapshot(history)))

    def status(self, topology_id="default"):
        with self._lock:
            return {"can_undo": self.can_undo(topology_id), "can_redo": self.can_redo(topology_id),
                    "snapshot": self.current_snapshot(topology_id), "diff": self.get_diff(topology_id)}

    def export_backup_file(self, topology_id="default"):
        """Return JSON text for download/save; never write an original config file."""
        with self._lock:
            history = self._history(topology_id)
            private_audit = [{**entry, "snapshot": deepcopy(history["states"][i])}
                             for i, entry in enumerate(history["events"])]
            encoded = json.dumps(encode_backup({
                "backup_version": "1.0", "exported_at": _now(),
                "timezone": "UTC+07:00", "topology_id": topology_id,
                "key_scope": self._key_scope,
                "audit_trail": private_audit,
                "history_state": {"undo_stack": history["undo"], "redo_stack": history["redo"]},
            }, self._cipher), ensure_ascii=False, allow_nan=False, indent=2)
            if len(encoded.encode("utf-8")) > MAX_BACKUP_BYTES:
                raise BackupSizeError("Backup exceeds the restore size limit")
            return encoded

    def export_audit_log(self, topology_id="default"):
        """Monitoring view only; deliberately unsuitable for Restore."""
        with self._lock:
            return {"backup_kind": "audit_only", "restore_supported": False,
                    "audit_trail": deepcopy(self._history(topology_id)["audit"])}

    def restore_from_backup(self, json_data, topology_id="default", actor="system", require_topology_id=None):
        """Validate fully before replacing memory. Imported journal is untrusted.

        history_state contains zero-based audit indices, preserving redo/branches.
        Minimal 1.0 backups without history_state are treated as a linear history.
        """
        _text(actor, "actor")
        _text(topology_id, "topology_id")
        try:
            size = len(json_data.encode("utf-8")) if isinstance(json_data, str) else (
                len(json_data) if isinstance(json_data, bytes) else len(json.dumps(
                    json_data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")))
            if size > MAX_BACKUP_BYTES:
                raise BackupSizeError("Backup exceeds the restore size limit")
            data = json.loads(json_data) if isinstance(json_data, (str, bytes)) else json_data
            data = _json_copy(data)
            data = decode_backup(data, self._cipher)
            if require_topology_id is not None and data.get("topology_id", "default") != require_topology_id:
                raise ValueError("Backup topology is incompatible")
            if not isinstance(data, dict) or data.get("backup_version") != "1.0":
                raise ValueError("Unsupported backup version")
            datetime.strptime(data["exported_at"], TIME_FORMAT)
            trail = data["audit_trail"]
            if not isinstance(trail, list):
                raise ValueError("audit_trail must be a list")
            previous = {}
            for step, event in enumerate(trail, 1):
                if not isinstance(event, dict) or type(event.get("step")) is not int or event["step"] != step:
                    raise ValueError("Invalid audit step sequence")
                datetime.strptime(event["timestamp"], TIME_FORMAT)
                for key in ("actor", "action_type", "description"):
                    _text(event[key], key)
                if not isinstance(event["snapshot"], dict) or not isinstance(event["diff_from_previous"], str):
                    raise ValueError("Invalid audit snapshot/diff")
                event["diff_from_previous"] = _summary(_diff(previous, event["snapshot"]))
                previous = event["snapshot"]
            state = data.get("history_state", {"undo_stack": list(range(len(trail))), "redo_stack": []})
            undo, redo = state["undo_stack"], state["redo_stack"]
            if not isinstance(undo, list) or not isinstance(redo, list):
                raise ValueError("Stacks must be lists")
            indices = undo + redo
            if any(type(i) is not int or not 0 <= i < len(trail) for i in indices):
                raise ValueError("Invalid stack index")
            if len(indices) != len(set(indices)) or (not undo and redo):
                raise ValueError("Inconsistent history stacks")
            states = [deepcopy(event["snapshot"]) for event in trail]
        except BackupSizeError:
            raise
        except (KeyError, TypeError, OverflowError, RecursionError, ValueError):
            # Parsers can include untrusted input in their exception messages.
            raise ValueError("Invalid backup, unavailable key, or incompatible format") from None
        with self._lock:
            base = self._state
            histories = deepcopy(base.histories)
            snapshot = deepcopy(trail[undo[-1]]["snapshot"]) if undo else None
            # Preserve the receiving manager's journal too; restore never erases it.
            existing = self._history(topology_id, histories)
            offset = len(existing["audit"])
            combined = deepcopy(existing["events"]) + [{key: event[key] for key in (
                "step", "timestamp", "actor", "action_type", "description", "diff_from_previous")} for event in trail]
            combined_states = deepcopy(existing["states"]) + states
            previous = {}
            for step, event in enumerate(combined, 1):
                event["step"] = step
                event["diff_from_previous"] = _summary(_diff(previous, combined_states[step - 1]))
                previous = combined_states[step - 1]
            history = {"undo": [i + offset for i in undo], "redo": [i + offset for i in redo],
                       "audit": [], "states": [], "events": [], "actors": {}}
            for entry, state_snapshot in zip(combined, combined_states):
                self._event(history, state_snapshot, entry["actor"], entry["action_type"], entry["description"])
                # Imported timestamps remain validated but are not evidence of actor identity.
                history["events"][-1]["timestamp"] = entry["timestamp"]
                history["audit"][-1]["timestamp"] = entry["timestamp"]
            self._event(history, snapshot if snapshot is not None else {}, actor, "RESTORE", "Restore configuration history from JSON backup")
            histories[topology_id] = history
            self._install_state(base, self._history_candidate(base, histories))
            return snapshot
