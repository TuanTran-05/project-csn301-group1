"""History of actual, user-owned, never-approved pending previews.

No deletes, device writes or approvals. DB updates use compare-and-swap and one
transaction for parents/children. RAM history is published only after commit.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
import logging
import json
import hashlib
import hmac
import re
import secrets

from flask import current_app, has_app_context
from sqlalchemy import bindparam, event, exists, or_, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.types import JSON

from ..changes.model import ChangeBatch, ChangeRequest
from ..changes.schemas import ChangePreviewSchema
from ..changes.service import prepare_change
from ..changes.verification import is_sensitive_verification_command
from ..devices.model import Device
from ..errors import ConflictError, ForbiddenError, ValidationError
from ..extensions import db
from .history_manager import HistoryManager, HistoryState

logger = logging.getLogger(__name__)


EDIT_FIELDS = (
    "description", "commands", "verification_commands", "rollback_commands", "warnings",
    "capability_tier", "verification_level", "operation_families", "operation_expectations",
    "verification_plan", "rollback_guidance", "risk_level", "requires_confirmation", "execution_mode",
)
SPEC_FIELDS = ("id", "device_id", "batch_id", "target_hostname", "target_management_ip",
               "target_ssh_port", "target_device_type", "commands", "verification_commands",
               "description", "execution_mode")
IDENTITY_FIELDS = SPEC_FIELDS[:7]


def _raw(row):
    return {c.key: (v.isoformat() if isinstance(v := getattr(row, c.key), datetime) else deepcopy(v))
            for c in row.__table__.columns}


def _spec(row):
    return {key: deepcopy(getattr(row, key)) for key in SPEC_FIELDS}


def _eligible(row):
    return row.status in {"pending_approval", "cancelled"} and all(
        getattr(row, key) is None for key in ("approved_by_id", "approved_at", "applied_at")) and (
        not isinstance(row, ChangeRequest) or all(getattr(row, key) is None for key in (
            "backup_id", "apply_output", "verification_output", "error_message")))


@dataclass(frozen=True, eq=False)
class PublicationAttempt:
    base: HistoryState
    target: HistoryState
    source: str


class PendingWorkspace:
    def __init__(self, owner):
        self.owner = owner
        self.history = HistoryManager()
        self.history._state = HistoryState(expected={})
        self.lock = RLock()
        self._revision_key = secrets.token_bytes(32)
        self._recovery_flag = False
        self._attempt = None

    @property
    def expected(self):
        return self.history._state.expected

    @expected.setter
    def expected(self, value):
        with self.lock, self.history._lock:
            base = self.history._state
            self.history._install_state(base, replace(base, histories=deepcopy(base.histories), expected=deepcopy(value),
                generation=base.generation + 1, needs_reconcile=True))

    @property
    def revision_epoch(self):
        return self.history._state.revision_epoch

    @revision_epoch.setter
    def revision_epoch(self, value):
        with self.lock, self.history._lock:
            base = self.history._state
            self.history._install_state(base, replace(base, histories=deepcopy(base.histories),
                expected=deepcopy(base.expected), revision_epoch=value,
                generation=base.generation + 1, needs_reconcile=True))

    @property
    def recovery_required(self):
        return self._recovery_flag or self._attempt is not None or self.history._state.needs_reconcile

    @recovery_required.setter
    def recovery_required(self, value):
        self._recovery_flag = bool(value)

    def _prepare_publication(self, history, expected, epoch, source="synchronize", base=None):
        base = self.history._state if base is None else base
        target = HistoryState(deepcopy(history._state.histories), deepcopy(expected), epoch,
                              base.generation + 1, False)
        return PublicationAttempt(base, target, source)

    def _publish(self, attempt):
        """No event generation or DB access. A retry uses this exact target object."""
        current = self.history._state
        if current is not attempt.base and current is not attempt.target:
            raise ConflictError("History publication generation changed")
        self._attempt = attempt
        self.history._install_state(attempt.base, attempt.target)
        self._acknowledge(attempt)

    def _acknowledge(self, attempt):
        if self.history._state is not attempt.target:
            raise ConflictError("History publication is incomplete")
        if self._attempt is attempt:
            self._attempt = None

    @staticmethod
    def _matches(state, snapshot, rows):
        history = state.histories.get("default")
        current = HistoryManager._snapshot(history) if history is not None else None
        keys = {("change", r["id"]) for r in snapshot["changes"]} | {("batch", r["id"]) for r in snapshot["batches"]}
        return (current == snapshot and keys.issubset(state.expected) and all(
            key in rows and _raw(rows[key]) == value for key, value in state.expected.items()))

    def read(self):
        # Refresh ORM identities: another request may have committed since the last read.
        changes = db.session.query(ChangeRequest).filter_by(requested_by_id=self.owner).populate_existing().all()
        batches = db.session.query(ChangeBatch).filter_by(requested_by_id=self.owner).populate_existing().all()
        rows = {("change", r.id): r for r in changes}
        rows.update({("batch", r.id): r for r in batches})
        active = [r for r in changes if r.status == "pending_approval" and _eligible(r)]
        parents = [r for r in batches if r.status == "pending_approval" and _eligible(r)]
        for batch in parents:
            children = db.session.query(ChangeRequest).filter_by(batch_id=batch.id).all()
            if not children or any(r not in active for r in children):
                raise ConflictError("Batch children are inconsistent; refresh pending work")
        parent_ids = {r.id for r in parents}
        active = [r for r in active if r.batch_id is None or r.batch_id in parent_ids]
        snapshot = {"schema": "pending-v1", "changes": [_spec(r) for r in sorted(active, key=lambda r: r.id)],
                    "batches": [{"id": r.id, "description": r.description} for r in sorted(parents, key=lambda r: r.id)]}
        return snapshot, rows

    def synchronize(self, actor):
        with self.lock, self.history._lock:
            snapshot, rows = self.read()
            base = self.history._state
            attempt = self._attempt
            seed = self.history
            epoch = base.revision_epoch + 1
            if attempt is not None:
                if base is not attempt.base and base is not attempt.target:
                    raise ConflictError("History publication generation changed")
                if self._matches(attempt.target, snapshot, rows):
                    self._publish(attempt)
                    return snapshot, rows, base is not attempt.target
                if attempt.source == "committed_db" and base is attempt.base:
                    # Retain the event of a known committed operation before an
                    # external DB change establishes a new barrier.
                    seed = self.history.fork()
                    seed._state = deepcopy(attempt.target)
                    epoch = max(epoch, attempt.target.revision_epoch + 1)
            current = self.history.current_snapshot()
            keys = {("change", r["id"]) for r in snapshot["changes"]} | {("batch", r["id"]) for r in snapshot["batches"]}
            # Extra cancelled rows remain tracked for Redo; only missing active keys
            # indicate a partially recorded create when the snapshots already match.
            changed = current is not None and (
                base.needs_reconcile or current != snapshot or not keys.issubset(base.expected) or
                any(key not in rows or _raw(rows[key]) != value for key, value in base.expected.items()))
            if current is None or changed or attempt is not None:
                expected = {key: _raw(rows[key]) for key in keys}
                staged = seed.fork()
                if current is None and attempt is None:
                    staged.push_state(snapshot, actor, "BASELINE", "Current unapproved pending previews")
                else:
                    staged.checkpoint(snapshot, actor, "Pending data changed externally; previous history is read-only")
                self._publish(self._prepare_publication(staged, expected, epoch, base=base))
            return snapshot, rows, changed

    def record(self, before, after, rows, actor, action):
        with self.lock, self.history._lock:
            return self._record(before, after, rows, actor, action)

    def _record(self, before, after, rows, actor, action):
        if before != after or self.history.current_snapshot() != before:
            self.revision_epoch += 1
        if self.history.current_snapshot() != before:
            self.history.checkpoint(after, actor, "Concurrent preview operation; establish a new baseline")
            self.expected = {}
            before = after
        if before != after:
            self.history.push_state(after, actor, action, "Update unapproved pending previews")
        keys = set(self.expected) | {( "change", r["id"]) for r in before["changes"] + after["changes"]}
        keys |= {( "batch", r["id"]) for r in before["batches"] + after["batches"]}
        expected = {key: _raw(rows[key]) for key in keys if key in rows}
        base = self.history._state
        if base.needs_reconcile or expected != base.expected:
            # Legacy core recording deliberately remains fail-soft. It may expose
            # a dirty generation before this final tracking publication succeeds.
            self.history._install_state(base, replace(base, histories=deepcopy(base.histories), expected=expected,
                generation=base.generation + 1, needs_reconcile=False))

    def _validate_target(self, target, rows):
        if not isinstance(target, dict) or set(target) != {"schema", "changes", "batches"} or target["schema"] != "pending-v1":
            raise ValidationError("Expected a pending-v1 snapshot")
        if not isinstance(target["changes"], list) or not isinstance(target["batches"], list):
            raise ValidationError("Pending changes and batches must be lists")
        desired = {}
        for kind, specs in (("change", target["changes"]), ("batch", target["batches"])):
            for spec in specs:
                fields = set(SPEC_FIELDS) if kind == "change" else {"id", "description"}
                if not isinstance(spec, dict) or set(spec) != fields or type(spec["id"]) is not int:
                    raise ValidationError("Invalid pending record shape")
                key = (kind, spec["id"])
                if key in desired:
                    raise ValidationError("Duplicate pending record")
                if key not in self.expected or key not in rows or rows[key].requested_by_id != self.owner:
                    raise ForbiddenError("Backup may only restore records already tracked by this user's history")
                row = rows[key]
                if not _eligible(row):
                    raise ConflictError("Approved or executed previews cannot be restored")
                if kind == "change":
                    for field in IDENTITY_FIELDS:
                        if spec[field] != getattr(row, field):
                            raise ConflictError("Frozen target identity or batch membership changed")
                    device = db.session.get(Device, row.device_id)
                    if device is None or (device.hostname, device.management_ip, device.ssh_port, device.device_type) != (
                        row.target_hostname, row.target_management_ip, row.target_ssh_port, row.target_device_type):
                        raise ConflictError("Device identity changed; create a new Preview")
                    try:
                        data = ChangePreviewSchema.model_validate({k: spec[k] for k in (
                            "device_id", "commands", "verification_commands", "description", "execution_mode")})
                    except Exception as exc:
                        raise ValidationError("Invalid draft commands or description") from exc
                    requested = [c for c in data.verification_commands if not is_sensitive_verification_command(c)]
                    candidate = prepare_change(self.owner, device, data.commands, requested,
                                               data.description, row.source, data.execution_mode)
                    # Stored snapshots must have the backend's canonical command/verification lists.
                    if candidate.commands != spec["commands"] or candidate.verification_commands != spec["verification_commands"]:
                        raise ValidationError("Backup contains noncanonical commands or verification")
                    desired[key] = {field: deepcopy(getattr(candidate, field)) for field in EDIT_FIELDS}
                else:
                    if spec["description"] is not None and (not isinstance(spec["description"], str) or len(spec["description"]) > 255):
                        raise ValidationError("Invalid batch description")
                    desired[key] = {"description": spec["description"]}
        active_ids = {spec["id"] for spec in target["changes"]}
        batch_ids = {spec["id"] for spec in target["batches"]}
        for key, row in rows.items():
            if key[0] == "change" and row.batch_id is not None:
                if (row.id in active_ids) != (row.batch_id in batch_ids):
                    raise ValidationError("Restore must include an entire batch and every child")
        return desired

    def _cas(self, row, values):
        expected = self.expected[("batch" if isinstance(row, ChangeBatch) else "change", row.id)]
        statement = update(type(row))
        for column in row.__table__.columns:
            value = expected[column.key]
            if isinstance(getattr(row, column.key), datetime) and value is not None:
                value = datetime.fromisoformat(value)
            if value is None and not isinstance(column.type, JSON):
                statement = statement.where(column.is_(None))
            else:
                comparison = column == bindparam("expected_" + column.key, value, type_=column.type)
                statement = statement.where(or_(column.is_(None), comparison) if value is None else comparison)
        if isinstance(row, ChangeRequest):
            # Guard current device identity in the same SQL statement, not the ORM cache.
            statement = statement.where(exists().where(
                Device.id == row.device_id, Device.hostname == row.target_hostname,
                Device.management_ip == row.target_management_ip,
                Device.ssh_port == row.target_ssh_port, Device.device_type == row.target_device_type))
        result = db.session.execute(statement.values(**values).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise ConflictError("Preview changed concurrently; no history state was applied")

    def apply(self, target, publish=None, owner=None):
        with self.lock, self.history._lock:
            return self._apply(target, publish, owner)

    def _apply(self, target, publish, owner):
        if db.session.get_bind().dialect.name != "sqlite":
            raise ConflictError("Pending history writes require a verified SQLite transaction")
        snapshot, rows = self.read()
        if any(key not in rows or _raw(rows[key]) != value for key, value in self.expected.items()):
            raise ConflictError("Pending data changed; refresh history before continuing")
        desired = self._validate_target(target, rows)
        session = db.session()
        owner = owner or self
        before_expected = deepcopy(self.expected)
        completed = False
        commit_confirmed = False
        commit_attempted = False
        prepared_expected = None
        receipt = None

        def committed(_session):
            nonlocal completed, commit_confirmed
            commit_confirmed = True
            # Retry only the prepared packet; never rerun Undo/Redo/Restore/events.
            owner._attempt = receipt
            owner._publish(receipt)
            completed = True

        event.listen(session, "after_commit", committed, insert=True)
        try:
            for key in sorted(self.expected, key=lambda k: (k[0] != "batch", k[1])):
                row = rows[key]
                if not _eligible(row):
                    raise ConflictError("Previously approved work cannot be undone")
                values = {"status": "pending_approval" if key in desired else "cancelled"}
                values.update(desired.get(key, {}))
                if key[0] == "batch" and key in desired:
                    children = [desired[("change", c["id"])] for c in target["changes"] if c["batch_id"] == row.id]
                    if not children:
                        raise ValidationError("Empty batch cannot be restored")
                    values["requires_confirmation"] = any(c["requires_confirmation"] for c in children)
                    values["risk_level"] = max((c["risk_level"] for c in children), key={"low": 0, "medium": 1, "high": 2}.get)
                self._cas(row, values)
            db.session.expire_all()
            actual, rows = self.read()
            if actual != target:
                raise ConflictError("Canonical pending snapshot differs from history")
            prepared_expected = {key: _raw(rows[key]) for key in self.expected}
            receipt = (publish(prepared_expected) if publish is not None else
                       owner._prepare_publication(self.history, prepared_expected,
                                                  self.revision_epoch, "committed_db"))
            commit_attempted = True
            db.session.commit()
        except Exception as exc:
            if commit_confirmed and not completed:
                try:
                    committed(session)
                except Exception:
                    owner.recovery_required = True
                    session.close()
                    raise ConflictError("Committed history publication requires refresh", {
                        "reason": "commit_outcome_unknown", "reload_required": True}) from None
            if completed:
                # A post-commit listener/wrapper failed. The operation already succeeded.
                # Close the session, never pretend rollback can undo the committed write.
                session.close()
                logger.error("Pending post-commit cleanup (%s); committed state retained", type(exc).__name__)
            else:
                # Rollback only cleans the Session; it is not evidence that a
                # driver exception means the physical commit did not happen.
                try:
                    db.session.rollback()
                except Exception:
                    session.close()
                if commit_attempted:
                    try:
                        db.session.expire_all()
                        actual, current_rows = self.read()
                        committed_values = actual == target and all(
                            key in current_rows and _raw(current_rows[key]) == value
                            for key, value in prepared_expected.items())
                        original_values = actual == snapshot and all(
                            key in current_rows and _raw(current_rows[key]) == value
                            for key, value in before_expected.items())
                    except Exception:
                        committed_values = original_values = False
                    if committed_values and not original_values:
                        try:
                            committed(session)
                        except Exception:
                            owner.recovery_required = True
                            session.close()
                            raise ConflictError("Committed history publication requires refresh", {
                                "reason": "commit_outcome_unknown", "reload_required": True}) from None
                        logger.error("Pending commit outcome recovered (%s)", type(exc).__name__)
                        return
                    if not original_values:
                        owner.recovery_required = True
                        raise ConflictError("Commit outcome requires an explicit history refresh", {
                            "reason": "commit_outcome_unknown", "reload_required": True}) from None
                if isinstance(exc, OperationalError) and (
                    ((getattr(exc.orig, "sqlite_errorcode", 0) or 0) & 255) in {5, 6}):
                    raise ConflictError("Concurrent SQLite writer; retry with a fresh editor") from None
                raise
        finally:
            event.remove(session, "after_commit", committed)

    def _fork(self):
        staged = PendingWorkspace(self.owner)
        staged.history = self.history.fork()
        staged._revision_key = self._revision_key
        return staged

    def move(self, actor, redo=False, backup=None):
        with self.lock, self.history._lock:
            if self.recovery_required:
                raise ConflictError("Refresh history before another mutation", {
                    "reason": "commit_outcome_unknown", "reload_required": True})
            # Even baseline/checkpoint changes are private until the operation commits.
            staged = self._fork()
            live_current = staged.history.current_snapshot()
            if backup is not None:
                if not isinstance(backup, dict) or backup.get("topology_id", "default") != "default":
                    raise ValidationError("Pending history supports only topology_id=default")
                target = staged.history.restore_from_backup(backup, actor=actor, require_topology_id="default")
            _, _, changed = staged.synchronize(actor) if backup is None else (None, None, False)
            # Restore must validate against live DB without adopting an external baseline.
            current, rows = self.read()
            if backup is not None and live_current is not None and (
                live_current != current or any(
                    key not in rows or _raw(rows[key]) != value for key, value in self.expected.items())):
                changed = True
            if changed:
                raise ConflictError("Pending history was invalidated by an external operation")
            if backup is not None:
                # Validate even inactive backup states now, rather than on a future redo.
                for snapshot in staged.history.active_snapshots():
                    staged._validate_target(snapshot, rows)
            else:
                target = staged.history.redo(actor=actor) if redo else staged.history.undo(actor=actor)
                if target is None:
                    raise ConflictError("No pending state to redo" if redo else "No pending state to undo")
            response = staged.history.status()
            epoch = self.revision_epoch + 1
            # Keep instance CAS hooks and the already-pinned expected values for moves.
            self.apply(target, publish=lambda expected: self._prepare_publication(
                staged.history, expected, epoch, "committed_db"))
            return response

    def status(self, actor):
        with self.lock, self.history._lock:
            self.synchronize(actor)
            if self._attempt is not None or self.history._state.needs_reconcile:
                raise ConflictError("History publication requires refresh")
            self.recovery_required = False
            return self.history.status()

    def _revision(self, kind, record_id, rows):
        keys = [(kind, record_id)]
        if kind == "batch":
            keys += sorted(key for key, row in rows.items()
                           if key[0] == "change" and row.batch_id == record_id)
        # Include device identity too: an editor cannot retarget a preview silently.
        content = [_raw(rows[key]) for key in keys]
        devices = sorted({rows[key].device_id for key in keys if key[0] == "change"})
        content += [{field: getattr(db.session.get(Device, did), field, None)
                     for field in ("hostname", "management_ip", "ssh_port", "device_type")} for did in devices]
        raw = json.dumps([self.revision_epoch, kind, record_id, content], sort_keys=True).encode()
        return hmac.new(self._revision_key, raw, hashlib.sha256).hexdigest()

    def editor(self, kind, record_id):
        with self.lock, self.history._lock:
            if self.recovery_required:
                raise ConflictError("Refresh history before another mutation", {
                    "reason": "commit_outcome_unknown", "reload_required": True})
            snapshot, rows = self.read()
            matches = [s for s in snapshot["changes" if kind == "change" else "batches"] if s["id"] == record_id]
            if not matches:
                raise ConflictError("Only your unapproved pending previews can be edited")
            if kind == "change" and matches[0]["batch_id"] is not None:
                raise ValidationError("Edit batch children through the batch endpoint")
            children = matches if kind == "change" else [s for s in snapshot["changes"] if s["batch_id"] == record_id]
            return {"revision": self._revision(kind, record_id, rows), "draft": {
                "kind": kind, "id": record_id, "description": matches[0]["description"],
                "changes": [{"id": s["id"], "hostname": s["target_hostname"],
                             "commands": s["commands"], "description": s["description"],
                             "execution_mode": s["execution_mode"]} for s in children]}}

    def edit(self, actor, kind, record_id, data):
        with self.lock, self.history._lock:
            if self.recovery_required:
                raise ConflictError("Refresh history before another mutation", {
                    "reason": "commit_outcome_unknown", "reload_required": True})
            # Read without synchronizing: invalid/stale saves must not move stacks.
            before, rows = self.read()
            observed = {key: _raw(row) for key, row in rows.items()}
            target = deepcopy(before)
            matches = [s for s in target["changes" if kind == "change" else "batches"] if s["id"] == record_id]
            if not matches:
                raise ConflictError("Only your unapproved pending previews can be edited")
            data = deepcopy(data)
            revision = data.pop("revision", None)
            if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
                raise ValidationError("Editor revision is required", {"revision": ["Open the editor again"]})
            if not hmac.compare_digest(revision, self._revision(kind, record_id, rows)):
                raise ConflictError("Draft changed since this editor opened; reopen before saving", {
                    "reason": "stale_revision", "reload_required": True})
            if kind == "change":
                if matches[0]["batch_id"] is not None:
                    raise ValidationError("Edit batch children through the batch endpoint")
                edits = [(matches[0], data)]
            else:
                if set(data) != {"description", "changes"} or not isinstance(data["changes"], list):
                    raise ValidationError("Batch edit requires description and every child")
                specs = [s for s in target["changes"] if s["batch_id"] == record_id]
                ids = []
                for index, item in enumerate(data["changes"]):
                    child_id = item.get("id") if isinstance(item, dict) else None
                    if type(child_id) is not int or child_id <= 0:
                        raise ValidationError("Invalid batch child ID", {
                            f"changes.{index}.id": ["Expected a positive integer"]})
                    ids.append(child_id)
                if len(ids) != len(specs) or set(ids) != {s["id"] for s in specs}:
                    raise ValidationError("Batch edit must provide each child exactly once")
                matches[0]["description"] = data["description"]
                edits = [(s, next(item for item in data["changes"] if item["id"] == s["id"])) for s in specs]
            for spec, patch in edits:
                allowed = {"commands", "verification_commands", "description", "execution_mode"} | ({"id"} if kind == "batch" else set())
                if not isinstance(patch, dict) or set(patch) - allowed:
                    raise ValidationError("Unknown draft edit fields")
                spec.update({k: v for k, v in patch.items() if k != "id"})
                try:
                    validated = ChangePreviewSchema.model_validate({k: spec[k] for k in (
                        "device_id", "commands", "verification_commands", "description", "execution_mode")})
                except Exception as exc:
                    raise ValidationError("Invalid draft edit") from exc
                row = rows[("change", spec["id"])]
                requested = validated.verification_commands if "verification_commands" in patch else [
                    c for c in validated.verification_commands if not is_sensitive_verification_command(c)]
                candidate = prepare_change(self.owner, row.device, validated.commands, requested,
                                           validated.description, row.source, validated.execution_mode)
                spec["commands"] = candidate.commands
                spec["verification_commands"] = candidate.verification_commands
            # Prepare both history and DB in isolation. Publish only after commit.
            staged = self._fork()
            fresh, fresh_rows, _ = staged.synchronize(actor)
            if fresh != before or {key: _raw(row) for key, row in fresh_rows.items()} != observed:
                raise ConflictError("Draft changed during staging", {"reason": "stale_revision", "reload_required": True})
            staged.record(before, target, rows, actor, "EDIT_PENDING")
            # Never let a later ORM refresh replace the values checked by the editor.
            staged.expected = {key: observed[key] for key in staged.expected}
            response = staged.history.status()
            staged.apply(target, publish=lambda expected: self._prepare_publication(
                staged.history, expected, staged.revision_epoch, "committed_db"), owner=self)
            return response


def workspace(owner):
    registry = current_app.extensions["pending_history"]
    with registry["lock"]:
        return registry["users"].setdefault(owner, PendingWorkspace(owner))


def capture_before(user_id):
    """Small opt-in service hook. No history behavior when extension is absent."""
    if user_id is None or not has_app_context() or "pending_history" not in current_app.extensions:
        return None
    try:
        work = workspace(user_id)
        with work.lock:
            before, _, _ = work.synchronize(_actor(user_id))
            return work, before
    except Exception as exc:
        logger.error("Cannot capture pending history baseline (%s); core operation continues", type(exc).__name__)
        return None


def _actor(user_id):
    from ..auth.model import User
    user = db.session.get(User, user_id)
    return user.username if user else str(user_id)


def record_after(token, action, record):
    if token is None:
        return
    try:
        work, before = token
        with work.lock:
            after, rows = work.read()
            affected = {("batch", record.id)} if isinstance(record, ChangeBatch) else {("change", record.id)}
            if isinstance(record, ChangeBatch):
                affected |= {("change", row.id) for row in record.changes}
            def specs(state):
                return {(kind, item["id"]): item for kind, field in (("change", "changes"), ("batch", "batches"))
                        for item in state[field]}
            a, b = specs(before), specs(after)
            delta = {key for key in a.keys() | b.keys() if a.get(key) != b.get(key)}
            if delta - affected:
                staged = work.history.fork()
                staged.checkpoint(after, _actor(work.owner), "Concurrent pending operations; history barrier")
                expected = {key: _raw(rows[key]) for key in b}
                work._publish(work._prepare_publication(staged, expected, work.revision_epoch + 1))
                return
            work.record(before, after, rows, _actor(work.owner), action)
    except Exception as exc:
        # A committed core preview/cancellation must not be reported as a failed operation.
        logger.error("Cannot record committed pending operation (%s); next status establishes a barrier", type(exc).__name__)
