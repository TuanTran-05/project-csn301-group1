# Full-Authority AI Batch Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an ADMIN ask the AI to preview and apply arbitrary Cisco CLI commands to one, several, or all inventory devices, with backend-derived risk and exact typed confirmation for dangerous changes.

**Architecture:** AI configuration responses contain one or more target operations with an explicit `exec` or `config` mode. A persisted `ChangeBatch` owns per-device `ChangeRequest` children; preview resolution is atomic and immutable, while Apply processes children sequentially and records partial success. The SSH adapter executes privileged-EXEC commands without configuration wrappers, and the backend—not the model—classifies danger and enforces hostname or `CONFIRM ALL` confirmation.

**Tech Stack:** Python 3.11+, Flask, Flask-SQLAlchemy, Alembic/Flask-Migrate, Pydantic 2, Paramiko, pytest, Alpine.js, server-rendered HTML/CSS.

## Global Constraints

- Only `ADMIN` may preview, approve, apply, or cancel configuration batches.
- Every configuration remains Preview → Approve → Apply; dangerous commands add typed confirmation rather than bypassing approval.
- One dangerous child makes the complete batch high-risk and confirmation-required.
- A dangerous one-device batch requires the exact hostname; a dangerous batch with two or more children requires exactly `CONFIRM ALL` after trimming surrounding whitespace.
- `"*"` resolves to the current inventory in hostname order during preview and is persisted as explicit children.
- A failed child never prevents later children from running; there is no automatic cross-device rollback.
- `config` commands receive exactly one `configure terminal ... end` wrapper; `exec` commands receive none.
- Reject command strings containing `;`, `|`, `&`, newline, carriage return, `$(`, backticks, `>`, or `<`; multiple commands must be separate array entries.
- AI output never controls risk, confirmation, approval, or authorization.
- No distributed queue, new frontend build system, or new runtime dependency is introduced.
- Preserve existing standalone `/api/changes/*`, monitor, and troubleshoot behavior.

---

### Task 1: Persist Batch Parents and Execution Mode

**Files:**
- Modify: `backend/src/network_copilot/changes/model.py`
- Create: `backend/migrations/versions/e4c7a9b1d2f0_add_change_batches_and_execution_mode.py`
- Create: `backend/tests/changes/test_batch_model.py`

**Interfaces:**
- Produces: `ChangeBatch`, `ChangeBatch.to_dict()`, `ChangeRequest.batch_id`, `ChangeRequest.execution_mode`.
- Consumes: existing SQLAlchemy `db`, `User`, `Device`, and `ChangeRequest` conventions.

- [ ] **Step 1: Write the failing model tests**

```python
from network_copilot.changes.model import ChangeBatch, ChangeRequest
from network_copilot.extensions import db


def test_batch_serializes_children_and_confirmation_text(
    app, admin_user, access_switch, dist_switch
):
    batch = ChangeBatch(
        status="pending_approval",
        risk_level="high",
        requires_confirmation=True,
        requested_by_id=admin_user.id,
        description="Save all configurations",
        source="ai",
    )
    batch.changes = [
        ChangeRequest(device_id=access_switch.id, commands=["write memory"], execution_mode="exec"),
        ChangeRequest(device_id=dist_switch.id, commands=["write memory"], execution_mode="exec"),
    ]
    db.session.add(batch)
    db.session.commit()

    payload = batch.to_dict()
    assert payload["confirmation_text"] == "CONFIRM ALL"
    assert [item["device"]["hostname"] for item in payload["changes"]] == [
        "ACC-SW1",
        "DIST-SW1",
    ]
    assert all(item["execution_mode"] == "exec" for item in payload["changes"])


def test_single_child_batch_uses_hostname_confirmation(app, admin_user, access_switch):
    batch = ChangeBatch(status="pending_approval", risk_level="high", requires_confirmation=True)
    batch.changes = [ChangeRequest(device_id=access_switch.id, commands=["reload"], execution_mode="exec")]
    db.session.add(batch)
    db.session.commit()
    assert batch.to_dict()["confirmation_text"] == "ACC-SW1"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes\test_batch_model.py -q
```

Expected: collection fails because `ChangeBatch` and the new child fields do not exist.

- [ ] **Step 3: Implement the model and migration**

Add the batch statuses and model in `changes/model.py`:

```python
BATCH_STATUSES = (
    "pending_approval", "approved", "running", "success",
    "partial_success", "failed", "cancelled",
)


class ChangeBatch(db.Model):
    __tablename__ = "change_batches"

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(32), nullable=False, default="pending_approval")
    risk_level = db.Column(db.String(16), nullable=False, default="low")
    requires_confirmation = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    description = db.Column(db.String(255))
    source = db.Column(db.String(16), nullable=False, default="ai")
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    approved_at = db.Column(db.DateTime)
    applied_at = db.Column(db.DateTime)

    changes = db.relationship(
        "ChangeRequest",
        back_populates="batch",
        order_by="ChangeRequest.id",
        cascade="all, delete-orphan",
    )

    @property
    def confirmation_text(self) -> str | None:
        if not self.requires_confirmation or not self.changes:
            return None
        if len(self.changes) == 1:
            return self.changes[0].device.hostname
        return "CONFIRM ALL"
```

Add nullable `batch_id`, relationship `batch`, and non-null `execution_mode`
with default/server default `config` to `ChangeRequest`. Include `batch_id` and
`execution_mode` in child serialization. Implement `ChangeBatch.to_dict()` with
all lifecycle fields and serialized children sorted by hostname.

Create an Alembic migration whose `down_revision` is the current head
`b2ace2e71682`. It creates `change_batches`, adds the two child columns and
foreign key/index, and reverses those operations in downgrade.

```python
revision = "e4c7a9b1d2f0"
down_revision = "b2ace2e71682"


def upgrade():
    op.create_table(
        "change_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("requires_confirmation", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("description", sa.String(255)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("requested_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("approved_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("applied_at", sa.DateTime()),
    )
    op.create_index("ix_change_batches_created_at", "change_batches", ["created_at"])
    op.create_index("ix_change_batches_requested_by_id", "change_batches", ["requested_by_id"])
    with op.batch_alter_table("change_requests") as batch_op:
        batch_op.add_column(sa.Column("batch_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("execution_mode", sa.String(16), server_default="config", nullable=False))
        batch_op.create_foreign_key("fk_change_requests_batch_id", "change_batches", ["batch_id"], ["id"], ondelete="CASCADE")
        batch_op.create_index("ix_change_requests_batch_id", ["batch_id"])


def downgrade():
    with op.batch_alter_table("change_requests") as batch_op:
        batch_op.drop_index("ix_change_requests_batch_id")
        batch_op.drop_constraint("fk_change_requests_batch_id", type_="foreignkey")
        batch_op.drop_column("execution_mode")
        batch_op.drop_column("batch_id")
    op.drop_index("ix_change_batches_requested_by_id", table_name="change_batches")
    op.drop_index("ix_change_batches_created_at", table_name="change_batches")
    op.drop_table("change_batches")
```

- [ ] **Step 4: Run model and migration tests GREEN**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes\test_batch_model.py tests\changes\test_preview.py -q
..\.venv\Scripts\python.exe -m flask --app wsgi db upgrade
```

Expected: tests pass and Alembic upgrades the development database without losing existing change rows.

- [ ] **Step 5: Commit**

```powershell
git add backend/src/network_copilot/changes/model.py backend/migrations/versions backend/tests/changes/test_batch_model.py
git commit -m "feat: persist configuration batches"
```

---

### Task 2: Separate EXEC and Configuration Change Preparation

**Files:**
- Modify: `backend/src/network_copilot/changes/service.py`
- Modify: `backend/src/network_copilot/changes/schemas.py`
- Modify: `backend/src/network_copilot/changes/routes.py`
- Modify: `backend/tests/changes/test_preview.py`
- Modify: `backend/tests/changes/test_apply.py`

**Interfaces:**
- Produces: `prepare_change(...) -> ChangeRequest`, `create_preview(..., execution_mode="config")`, `validate_execution_mode(...)`.
- Consumes: `ChangeRequest.execution_mode` from Task 1 and existing danger/injection policy constants.

- [ ] **Step 1: Write failing preview and apply tests**

```python
def test_exec_preview_keeps_write_memory_outside_config_mode(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        execution_mode="exec",
        commands=["write memory"],
    )
    assert change.commands == ["write memory"]
    assert change.requires_confirmation is True
    assert change.risk_level == "high"
    assert change.verification_commands == ["show startup-config"]


def test_known_exec_command_rejects_config_mode(app, admin_user, access_switch):
    with pytest.raises(ValidationError, match="EXEC mode"):
        change_service.create_preview(
            admin_user.id,
            device_id=access_switch.id,
            execution_mode="config",
            commands=["write memory"],
        )


def test_dangerous_detection_is_case_insensitive(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        execution_mode="exec",
        commands=["WRITE MEMORY"],
    )
    assert change.requires_confirmation is True


def test_newline_injection_is_checked_before_whitespace_normalization(
    app, admin_user, access_switch
):
    with pytest.raises(PolicyViolationError, match="forbidden"):
        change_service.create_preview(
            admin_user.id,
            device_id=access_switch.id,
            execution_mode="config",
            commands=["vlan 25\nwrite erase"],
        )


def test_apply_exec_change_calls_run_exec_without_wrappers(
    app, admin_user, access_switch, ssh_factory
):
    fake = ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show startup-config": "hostname ACC-SW1",
        },
    )
    change = change_service.create_preview(
        admin_user.id, access_switch.id, ["write memory"], execution_mode="exec"
    )
    change_service.approve(change.id, admin_user.id)
    change_service.apply(change.id, admin_user.id, confirm_hostname="ACC-SW1")
    assert ("run_exec", ["write memory"]) in fake.calls
    assert fake.config_batches == []
```

- [ ] **Step 2: Run these tests RED**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes\test_preview.py tests\changes\test_apply.py -k "exec or execution_mode" -q
```

Expected: failures show missing `execution_mode`, missing `prepare_change`, and missing `run_exec` dispatch.

- [ ] **Step 3: Implement mode-aware preparation**

Add:

```python
EXEC_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"^write\b", r"^copy\b", r"^reload\b", r"^erase\b",
        r"^delete\b", r"^format\b", r"^clear\b", r"^debug\b",
        r"^undebug\b", r"^show\b", r"^ping\b", r"^traceroute\b",
    )
)


def validate_execution_mode(commands: list[str], execution_mode: str) -> None:
    if execution_mode not in {"config", "exec"}:
        raise ValidationError("execution_mode must be 'config' or 'exec'.")
    if execution_mode == "config" and any(
        pattern.search(command) for command in commands for pattern in EXEC_COMMAND_PATTERNS
    ):
        raise ValidationError("This command requires EXEC mode; it cannot run in config mode.")


def prepare_change(
    user_id: int | None,
    device: Device,
    commands: list[str],
    verification_commands: list[str] | None = None,
    description: str | None = None,
    source: str = "api",
    execution_mode: str = "config",
) -> ChangeRequest:
    body, requires_confirmation = validate_commands(commands, device)
    validate_execution_mode(body, execution_mode)
    canonical = _wrap(body) if execution_mode == "config" else body
    verification = (
        _validate_verification(verification_commands, device)
        if verification_commands
        else derive_verification_commands(canonical, device, execution_mode)
    )
    return ChangeRequest(
        device_id=device.id,
        requested_by_id=user_id,
        description=description,
        commands=canonical,
        execution_mode=execution_mode,
        verification_commands=verification,
        rollback_commands=derive_rollback_commands(canonical),
        warnings=build_warnings(canonical, device, requires_confirmation),
        risk_level=classify_risk(canonical, device, requires_confirmation),
        requires_confirmation=requires_confirmation,
        status="pending_approval",
        source=source,
    )
```

In `validate_commands()`, scan `SHELL_METACHARACTERS` on each original `raw`
string before calling `_normalise()`. Make `_dangerous_reason()` use
case-insensitive regex matching without lowercasing the stored command, because
descriptions and platform-specific arguments may be case-sensitive. Compare
configuration wrappers case-insensitively, remove them before validation, and
reject all wrappers in EXEC operations.

Refactor `create_preview()` to call `prepare_change()`, add/commit it, and keep
its existing signature compatible by making `execution_mode` optional with
default `config`. Add `execution_mode` to `ChangePreviewSchema` and pass it from
the route. Reject caller-supplied wrappers in EXEC mode and continue stripping
and applying exactly one wrapper pair in config mode.

Derive the backend-only verifier `show startup-config` for exact `write`,
`write memory`, and `copy running-config startup-config` commands. Accept that
verifier only when derived internally; do not add it to the AI-advertised
read-only allowlist. The verification result may be stored for the ADMIN UI but
must never be included in a later AI provider context.

In `apply()`, dispatch `client.run_config(...)` for `config` and
`client.run_exec(..., allow_confirm=change.requires_confirmation)` for `exec`.

- [ ] **Step 4: Run focused and existing change tests GREEN**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes -q
```

- [ ] **Step 5: Commit**

```powershell
git add backend/src/network_copilot/changes backend/tests/changes
git commit -m "feat: execute changes in explicit CLI modes"
```

---

### Task 3: Add Privileged-EXEC SSH Execution

**Files:**
- Modify: `backend/src/network_copilot/ssh/client.py`
- Modify: `backend/tests/ssh/test_ssh.py`
- Modify: `backend/tests/fakes/fake_ssh_client.py`

**Interfaces:**
- Produces: `SSHClient.run_exec(commands: list[str], allow_confirm: bool = False) -> SSHResult`.
- Consumes: the interactive shell transport already used by `run_config`.

- [ ] **Step 1: Write failing SSH tests**

```python
class PromptingFakeShell(FakeShell):
    def __init__(self, prompts: dict[str, str]):
        super().__init__()
        self.prompts = prompts

    def send(self, data: str) -> int:
        self.sent.append(data)
        command = data.strip()
        self._buffer += self.prompts.get(command, "CORE-SW1#").encode()
        return len(data)


def test_run_exec_sends_commands_without_config_wrappers():
    shell = FakeShell()
    client, _ = make_client(shell=shell)
    result = client.run_exec(["write memory"])
    assert [line.strip() for line in shell.sent] == ["write memory"]
    assert result.command == "write memory"


def test_run_exec_only_answers_confirm_prompt_when_authorized():
    shell = PromptingFakeShell({"write erase": "Erase nvram? [confirm]"})
    client, _ = make_client(shell=shell)
    with pytest.raises(SSHCommandError, match="interactive confirmation"):
        client.run_exec(["write erase"], allow_confirm=False)

    shell = PromptingFakeShell({"write erase": "Erase nvram? [confirm]"})
    client, _ = make_client(shell=shell)
    client.run_exec(["write erase"], allow_confirm=True)
    assert shell.sent == ["write erase\n", "\n"]


def test_run_exec_rejects_unknown_interactive_prompt():
    shell = PromptingFakeShell({"custom command": "Enter arbitrary value:"})
    client, _ = make_client(shell=shell)
    with pytest.raises(SSHCommandError, match="Unsupported interactive prompt"):
        client.run_exec(["custom command"], allow_confirm=True)
```

- [ ] **Step 2: Run SSH tests RED**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\ssh\test_ssh.py -k "run_exec" -q
```

- [ ] **Step 3: Implement one shared interactive runner**

Extract the current `run_config` loop into a private `_run_interactive()` and
make both public methods delegate to it. Detect only these output-tail prompts:

```python
_CONFIRM_PROMPTS = (
    (re.compile(r"\[confirm\]\s*$", re.I), "\n"),
    (re.compile(r"(?:\[yes/no\]|\(y/n\))\s*[:?]?\s*$", re.I), "yes\n"),
)


def run_exec(self, commands: list[str], allow_confirm: bool = False) -> SSHResult:
    return self._run_interactive(commands, allow_confirm=allow_confirm)
```

Allow the destination-filename default only when the active command normalizes
to `copy running-config startup-config`; send a blank line. Raise
`SSHCommandError` for any other prompt-like tail instead of guessing an answer.
Update `FakeSSHClient` with `exec_batches` and the same `run_exec` signature.

- [ ] **Step 4: Run SSH and change execution tests GREEN**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\ssh tests\changes\test_apply.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add backend/src/network_copilot/ssh/client.py backend/tests/ssh/test_ssh.py backend/tests/fakes/fake_ssh_client.py
git commit -m "feat: run privileged EXEC command batches"
```

---

### Task 4: Build Atomic Batch Previews

**Files:**
- Create: `backend/src/network_copilot/changes/batch_service.py`
- Create: `backend/tests/changes/test_batch_preview.py`
- Modify: `backend/src/network_copilot/changes/service.py`

**Interfaces:**
- Consumes: `ChangeBatch`, `prepare_change(...)`, `Device`, `db`.
- Produces: `BatchOperation`, `create_batch_preview(...)`, `get_batch(...)`, `list_batches(...)`.

- [ ] **Step 1: Write failing target-resolution and atomicity tests**

```python
from network_copilot.changes import batch_service
from network_copilot.changes.batch_service import BatchOperation
from network_copilot.errors import ValidationError


def write_all():
    return BatchOperation(
        device_hostnames=["*"],
        execution_mode="exec",
        commands=["write memory"],
        verification_commands=[],
    )


def test_wildcard_preview_freezes_devices_in_hostname_order(
    app, admin_user, access_switch, dist_switch, core_switch
):
    batch = batch_service.create_batch_preview(
        admin_user.id, [write_all()], "Save every device"
    )
    assert [change.device.hostname for change in batch.changes] == [
        "ACC-SW1", "CORE-SW1", "DIST-SW1"
    ]
    assert all(change.execution_mode == "exec" for change in batch.changes)
    assert batch.requires_confirmation is True
    assert batch.risk_level == "high"


def test_wildcard_snapshot_does_not_gain_later_device(
    app, admin_user, access_switch, make_device
):
    batch = batch_service.create_batch_preview(admin_user.id, [write_all()], "Save")
    make_device("LATE-SW1", "10.10.10.99", "access")
    assert [change.device.hostname for change in batch.changes] == ["ACC-SW1"]


def test_conflicting_duplicate_target_rolls_back_entire_preview(
    app, admin_user, access_switch, db
):
    operations = [
        BatchOperation(["ACC-SW1"], "exec", ["write memory"], []),
        BatchOperation(["ACC-SW1"], "exec", ["reload"], []),
    ]
    with pytest.raises(ValidationError, match="conflicting"):
        batch_service.create_batch_preview(admin_user.id, operations, "Conflict")
    assert db.session.query(ChangeBatch).count() == 0
    assert db.session.query(ChangeRequest).count() == 0
```

- [ ] **Step 2: Run batch-preview tests RED**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes\test_batch_preview.py -q
```

- [ ] **Step 3: Implement operation and preview services**

```python
@dataclass(frozen=True)
class BatchOperation:
    device_hostnames: list[str]
    execution_mode: str
    commands: list[str]
    verification_commands: list[str]


def _resolve_targets(hostnames: list[str]) -> list[Device]:
    if hostnames == ["*"]:
        return db.session.query(Device).order_by(Device.hostname).all()
    if "*" in hostnames:
        raise ValidationError("'*' cannot be mixed with explicit hostnames.")
    unique = sorted(set(hostnames))
    devices = db.session.query(Device).filter(Device.hostname.in_(unique)).all()
    found = {device.hostname for device in devices}
    missing = [hostname for hostname in unique if hostname not in found]
    if missing:
        raise ValidationError("Unknown batch targets.", {"device_hostnames": missing})
    return sorted(devices, key=lambda device: device.hostname)


def _risk_rank(level: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}[level]


def create_batch_preview(
    user_id: int | None,
    operations: list[BatchOperation],
    description: str | None,
    source: str = "ai",
) -> ChangeBatch:
    if not operations:
        raise ValidationError("At least one batch operation is required.")
    resolved: dict[str, tuple[Device, BatchOperation]] = {}
    for operation in operations:
        devices = _resolve_targets(operation.device_hostnames)
        for device in devices:
            previous = resolved.get(device.hostname)
            if previous and previous[1] != operation:
                raise ValidationError(
                    f"Device '{device.hostname}' has conflicting batch operations."
                )
            resolved[device.hostname] = (device, operation)
    batch = ChangeBatch(
        requested_by_id=user_id,
        description=(description or "")[:255] or None,
        status="pending_approval",
        source=source,
    )
    for hostname in sorted(resolved):
        device, operation = resolved[hostname]
        batch.changes.append(
            prepare_change(
                user_id,
                device,
                operation.commands,
                operation.verification_commands,
                description,
                source,
                operation.execution_mode,
            )
        )
    batch.requires_confirmation = any(c.requires_confirmation for c in batch.changes)
    batch.risk_level = max((c.risk_level for c in batch.changes), key=_risk_rank)
    db.session.add(batch)
    db.session.commit()
    return batch
```

Use `db.session.flush()` only after all operations and targets validate. On an
exception, call `db.session.rollback()` and re-raise. `get_batch()` raises the
existing `NotFoundError`; `list_batches()` mirrors the existing bounded list
pattern. Add a `standalone_only` option to `changes.service.list_changes()` so
the UI can exclude batch children without changing the default API behavior.

- [ ] **Step 4: Run preview, policy, and model tests GREEN**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes\test_batch_model.py tests\changes\test_batch_preview.py tests\changes\test_preview.py tests\commands\test_policy.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add backend/src/network_copilot/changes backend/tests/changes
git commit -m "feat: create immutable multi-device previews"
```

---

### Task 5: Approve and Apply Batches with Partial Success

**Files:**
- Modify: `backend/src/network_copilot/changes/batch_service.py`
- Modify: `backend/src/network_copilot/changes/service.py`
- Create: `backend/tests/changes/test_batch_apply.py`

**Interfaces:**
- Produces: `approve_batch`, `cancel_batch`, `apply_batch`, and private per-child execution that cannot bypass public confirmation.
- Consumes: batch preview from Task 4 and mode-aware child execution from Tasks 2–3.

- [ ] **Step 1: Write failing confirmation and continuation tests**

```python
def make_write_batch(user_id, devices):
    return batch_service.create_batch_preview(
        user_id,
        [BatchOperation(
            [device.hostname for device in devices],
            "exec",
            ["write memory"],
            [],
        )],
        "Save configurations",
    )


def statuses(batch):
    return {change.device.hostname: change.status for change in batch.changes}


def test_dangerous_multi_device_batch_requires_confirm_all(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    with pytest.raises(ValidationError):
        batch_service.apply_batch(batch.id, admin_user.id, confirmation="ACC-SW1")
    assert ssh_factory.clients == {}


def test_confirm_all_continues_after_one_device_fails(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    ssh_factory.set_failing(access_switch.hostname, SSHConnectionError("offline"))
    ssh_factory.set_client(
        dist_switch.hostname,
        responses={
            "show running-config": "hostname DIST-SW1",
            "show startup-config": "hostname DIST-SW1",
        },
    )
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="CONFIRM ALL"
    )
    assert result.status == "partial_success"
    assert statuses(result) == {"ACC-SW1": "failed", "DIST-SW1": "success"}
    assert ssh_factory.get(dist_switch.hostname).exec_batches == [["write memory"]]


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [(["success", "success"], "success"), (["success", "failed"], "partial_success"), (["failed", "failed"], "failed")],
)
def test_aggregate_status(outcomes, expected):
    assert batch_service.aggregate_status(outcomes) == expected
```

- [ ] **Step 2: Run lifecycle tests RED**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes\test_batch_apply.py -q
```

- [ ] **Step 3: Refactor reusable child application and implement lifecycle**

Extract a private function in `changes/service.py`:

```python
def _apply_approved_change(change: ChangeRequest, user_id: int | None) -> ChangeRequest:
    """Execute an already-authorized child; callers must enforce confirmation."""
    device = device_service.get_device(change.device_id)
    change.status = "running"
    db.session.commit()
    try:
        client = build_client_for_device(device)
        backup = capture_backup(device, change_request_id=change.id, client=client)
        change.backup_id = backup.id
        result = (
            client.run_exec(list(change.commands or []), allow_confirm=change.requires_confirmation)
            if change.execution_mode == "exec"
            else client.run_config(list(change.commands or []))
        )
        change.apply_output = result.output
        passed, results = run_verification(change, client)
    except SSHError as exc:
        return _fail(change, exc.message, user_id)
    change.verification_output = results
    if not passed:
        return _fail(change, "Verification failed.", user_id)
    change.status = "success"
    change.applied_at = _now()
    db.session.commit()
    return change
```

The public standalone `apply()` retains hostname confirmation and delegates to
this private function. `apply_batch()` validates batch confirmation once before
any SSH work, marks the parent running, and calls the private child function in
hostname order inside a per-child `try/except`. It must expire/refresh child
state between iterations, aggregate after all attempts, set `applied_at`, and
record batch-level audit events without storing the typed phrase.

`approve_batch()` and `cancel_batch()` update parent and eligible children in a
single transaction and reuse the existing state error wording conventions.

- [ ] **Step 4: Run all change tests GREEN**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes -q
```

- [ ] **Step 5: Commit**

```powershell
git add backend/src/network_copilot/changes backend/tests/changes/test_batch_apply.py
git commit -m "feat: apply confirmed batches with partial results"
```

---

### Task 6: Expose Authorized Batch APIs

**Files:**
- Create: `backend/src/network_copilot/changes/batch_routes.py`
- Modify: `backend/src/network_copilot/app.py`
- Modify: `backend/src/network_copilot/changes/routes.py`
- Create: `backend/tests/changes/test_batch_api.py`

**Interfaces:**
- Produces: `/api/change-batches` list/get/approve/apply/cancel endpoints.
- Consumes: lifecycle functions from Tasks 4–5 and existing auth/error contracts.

- [ ] **Step 1: Write failing API authorization and payload tests**

```python
def make_write_batch(user_id, devices):
    return batch_service.create_batch_preview(
        user_id,
        [BatchOperation(
            [device.hostname for device in devices],
            "exec",
            ["write memory"],
            [],
        )],
        "Save configurations",
    )


@pytest.fixture
def batch(app, admin_user, access_switch):
    return batch_service.create_batch_preview(
        admin_user.id,
        [BatchOperation([access_switch.hostname], "config", ["hostname NEW-SW"], [])],
        "Rename switch",
    )


@pytest.fixture
def approved_write_batch(app, admin_user, access_switch, dist_switch, ssh_factory):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    for device in (access_switch, dist_switch):
        ssh_factory.set_client(
            device.hostname,
            responses={
                "show running-config": f"hostname {device.hostname}",
                "show startup-config": f"hostname {device.hostname}",
            },
        )
    return batch_service.approve_batch(batch.id, admin_user.id)


def test_batch_get_requires_authentication(client, batch):
    assert client.get(f"/api/change-batches/{batch.id}").status_code == 401


@pytest.mark.parametrize("suffix", ["approve", "apply", "cancel"])
def test_batch_mutation_requires_admin(client, viewer_headers, batch, suffix):
    response = client.post(
        f"/api/change-batches/{batch.id}/{suffix}", headers=viewer_headers, json={}
    )
    assert response.status_code == 403


def test_batch_apply_accepts_confirmation_field(client, admin_headers, approved_write_batch):
    response = client.post(
        f"/api/change-batches/{approved_write_batch.id}/apply",
        headers=admin_headers,
        json={"confirmation": "CONFIRM ALL"},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] in {"success", "partial_success", "failed"}


def test_standalone_filter_excludes_batch_children(client, admin_headers, batch):
    body = client.get(
        "/api/changes?standalone_only=true&limit=500", headers=admin_headers
    ).get_json()
    assert all(item["batch_id"] is None for item in body["items"])
```

- [ ] **Step 2: Run API tests RED**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes\test_batch_api.py -q
```

- [ ] **Step 3: Implement routes and registration**

Create `batch_routes.py` with blueprint name `change_batches`, URL prefix
`/api/change-batches`, `jwt_required()` on reads, `roles_required("ADMIN")` on
mutations, and the existing `10 per minute` limiter on Apply. Parse
`confirmation` from JSON and return `ChangeBatch.to_dict()`.

Register the blueprint in `app._register_blueprints()`. Extend the standalone
list route with strict boolean parsing for `standalone_only`; invalid values
return 422 rather than silently changing the query.

- [ ] **Step 4: Run API/security tests GREEN**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\changes\test_batch_api.py tests\test_security.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add backend/src/network_copilot/app.py backend/src/network_copilot/changes backend/tests/changes/test_batch_api.py
git commit -m "feat: add configuration batch API"
```

---

### Task 7: Give the AI Full Configuration Authority and Multi-Target Output

**Files:**
- Modify: `backend/src/network_copilot/ai/schemas.py`
- Modify: `backend/src/network_copilot/ai/service.py`
- Modify: `backend/tests/ai/test_ai.py`
- Modify: `backend/tests/ai/test_chat_history.py`
- Modify: `backend/tests/ai/test_provider.py`
- Modify: `backend/tests/fakes/fake_ai_provider.py` only if its schema recording needs extension

**Interfaces:**
- Produces: `AIOperation`, operation-based `AIAction`, provider-facing operation schema, AI-to-batch integration.
- Consumes: `BatchOperation` and `create_batch_preview()` from Task 4.

- [ ] **Step 1: Write failing schema, prompt, and motivating-request tests**

```python
from copy import deepcopy

import pytest
from pydantic import ValidationError as PydanticValidationError

WRITE_ALL_ACTION = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["*"],
        "execution_mode": "exec",
        "commands": ["write memory"],
        "verification_commands": [],
    }],
    "explanation": "Luu cau hinh tren tat ca thiet bi.",
}


def test_action_schema_accepts_multi_target_operations():
    action = AIAction(**WRITE_ALL_ACTION)
    assert action.operations[0].device_hostnames == ["*"]
    assert action.operations[0].execution_mode == "exec"


def test_wildcard_cannot_be_mixed_with_explicit_hostname():
    payload = deepcopy(WRITE_ALL_ACTION)
    payload["operations"][0]["device_hostnames"] = ["*", "ACC-SW1"]
    with pytest.raises(PydanticValidationError):
        AIAction(**payload)


def test_prompt_allows_arbitrary_configuration_and_never_delegates_risk(app, admin_user):
    service, provider = service_with(app, WRITE_ALL_ACTION)
    service.interpret("thuc hien lenh write tren toan bo thiet bi", admin_user.id)
    prompt = provider.prompts[0]["system_prompt"]
    assert "any Cisco CLI" in prompt
    assert "risk" in prompt and "backend" in prompt
    assert "only for the changes listed in supported_actions" not in prompt


def test_declined_operation_list_surfaces_model_explanation(app, admin_user):
    service, _ = service_with(app, {
        "intent": "configure",
        "operations": [],
        "explanation": "Khong the tao de xuat.",
    })
    with pytest.raises(ValidationError, match="Khong the tao de xuat"):
        service.interpret("configure", admin_user.id)


def test_vietnamese_write_all_request_creates_batch_without_ssh(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    service, _ = service_with(app, WRITE_ALL_ACTION)
    result = service.handle(
        "thuc hien lenh write tren toan bo thiet bi", admin_user.id
    )
    assert result["intent"] == "configure"
    assert result["batch"]["confirmation_text"] == "CONFIRM ALL"
    assert len(result["batch"]["changes"]) == 2
    assert ssh_factory.clients == {}
```

- [ ] **Step 2: Run AI tests RED**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\ai\test_ai.py -k "operation or arbitrary or write_all" -q
```

- [ ] **Step 3: Implement the strict AI operation schema**

```python
class AIOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_hostnames: list[str] = Field(min_length=1)
    execution_mode: Literal["config", "exec"]
    commands: list[str] = Field(min_length=1)
    verification_commands: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_targets(self):
        if "*" in self.device_hostnames and self.device_hostnames != ["*"]:
            raise ValueError("'*' cannot be mixed with explicit device hostnames")
        return self


class AIAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    intent: Literal["monitor", "configure", "troubleshoot"]
    operations: list[AIOperation] = Field(min_length=1)
    explanation: str
```

Update `AI_ACTION_SCHEMA` to the equivalent nested provider schema. Replace
`SUPPORTED_ACTIONS` and the legacy prompt rules with the approved full-authority
rules. Keep the read-only command context for monitor/troubleshoot.

Update the pre-Pydantic refusal branch in `interpret()` to check a well-formed
empty `operations` list and surface `explanation`, preserving the existing
one-attempt refusal behavior.

Update `AIService.handle()`:

- configure: require ADMIN, convert every `AIOperation` to `BatchOperation`,
  call `create_batch_preview()`, and return `batch` plus `requires_approval`;
- monitor/troubleshoot: require exactly one operation, one explicit hostname,
  and `exec` mode, then adapt it to the existing read-only handlers;
- never trust an operation for danger or confirmation.

Update all existing fake AI payload constants to the operation shape. Preserve
their original intent and assertions rather than deleting coverage.

`tests/ai/test_provider.py` hard-codes the retired single-device shape in two
places and will fail once the schema changes: update
`test_the_action_schema_matches_the_ai_action_model` to assert the nested
`operations` field (and its `required` set) instead of `device_hostname`, and
update `test_interpret_asks_the_provider_to_enforce_the_schema`'s fake payload
to the `operations`-based `AIAction` shape.

- [ ] **Step 4: Run all AI/chat tests GREEN**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\ai tests\chat -q
```

- [ ] **Step 5: Commit**

```powershell
git add backend/src/network_copilot/ai backend/tests/ai backend/tests/fakes/fake_ai_provider.py
git commit -m "feat: let AI propose full-authority device batches"
```

---

### Task 8: Render and Control Batch Cards in the UI

**Files:**
- Modify: `backend/src/network_copilot/static/js/app.js`
- Modify: `backend/src/network_copilot/templates/index.html`
- Modify: `backend/src/network_copilot/static/css/app.css`
- Create: `backend/tests/chat/test_batch_ui.py`

**Interfaces:**
- Consumes: live batch API and chat `payload.batch` from Tasks 6–7.
- Produces: `batchesById`, `batchConfirmInputs`, batch polling/actions, batch card and sidebar rendering.

- [ ] **Step 1: Write failing static UI contract tests**

```python
def test_index_contains_live_batch_card_contract(client):
    html = client.get("/").get_data(as_text=True)
    assert "message.payload.batch" in html
    assert "batchesById" in html
    assert "batch.confirmation_text" in html
    assert "approveBatch(batch.id)" in html
    assert "applyBatch(batch.id)" in html


def test_app_js_fetches_standalone_changes_and_batches():
    source = Path("src/network_copilot/static/js/app.js").read_text(encoding="utf-8")
    assert "/api/changes?standalone_only=true&limit=500" in source
    assert "/api/change-batches?limit=500" in source
    assert "body.confirmation" in source
```

- [ ] **Step 2: Run UI contract tests RED**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\chat\test_batch_ui.py -q
```

- [ ] **Step 3: Implement live batch state and controls**

In Alpine state add:

```javascript
batchesById: {},
batchConfirmInputs: {},
_batchesRefreshGeneration: 0,

get pendingBatches() {
  return Object.values(this.batchesById).filter(
    (batch) => batch.status === "pending_approval" || batch.status === "approved"
  );
},
```

Fetch standalone changes and batches during bootstrap and every 15 seconds.
In `_ingestMessage`, hydrate `payload.batch` into `batchesById`. Implement
`approveBatch`, `applyBatch`, `cancelBatch`, and `batchConfirmationMatches` with
the new endpoints. Clear all batch state and timers on logout.

Add a batch action card that reads live state by ID and renders risk/status,
confirmation warning, exact phrase, expandable child device/mode/commands,
output/error, and aggregate successful/failed counts. The sidebar renders batch
parents alongside standalone changes and never renders batch children twice.
Extend existing NOC-theme classes without changing the overall page layout.

- [ ] **Step 4: Run UI contracts and chat regressions GREEN**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\chat tests\ai\test_chat_history.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add backend/src/network_copilot/static backend/src/network_copilot/templates/index.html backend/tests/chat/test_batch_ui.py
git commit -m "feat: add multi-device batch controls to chat"
```

---

### Task 9: End-to-End Regression and Operational Documentation

**Files:**
- Modify: `backend/tests/e2e/test_complete_flow.py`
- Modify: `backend/README.md`
- Modify: `backend/scripts/demo_check.py`

**Interfaces:**
- Consumes: complete feature from Tasks 1–8.
- Produces: one automated motivating flow and deploy/demo instructions.

- [ ] **Step 1: Add the end-to-end regression test**

This file already defines `AI_VLAN_ACTION` and `AI_WRITE_ERASE_ACTION` in the
retired single-device `device_hostname` shape, consumed by the pre-existing
`test_complete_demo_flow` and `test_flow_never_leaks_credentials`. Task 7
changed `AIAction` to the `operations`-based shape, so update both constants
to the new `operations` shape (matching `WRITE_ALL_ACTION` below) before
adding the new test, otherwise those two existing tests fail in Step 4.

```python
WRITE_ALL_ACTION = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["*"],
        "execution_mode": "exec",
        "commands": ["write memory"],
        "verification_commands": [],
    }],
    "explanation": "Luu cau hinh tren tat ca thiet bi.",
}


def test_write_all_preview_confirm_and_partial_result(
    client, admin_headers, app, access_switch, dist_switch, ssh_factory
):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=WRITE_ALL_ACTION)
    ssh_factory.set_failing(access_switch.hostname, SSHConnectionError("offline"))
    ssh_factory.set_client(
        dist_switch.hostname,
        responses={
            "show running-config": "hostname DIST-SW1",
            "show startup-config": "hostname DIST-SW1",
        },
    )

    preview = client.post(
        "/api/ai/chat",
        headers=admin_headers,
        json={"message": "thuc hien lenh write tren toan bo thiet bi"},
    )
    assert preview.status_code == 200
    batch = preview.get_json()["batch"]
    assert batch["confirmation_text"] == "CONFIRM ALL"

    assert client.post(
        f"/api/change-batches/{batch['id']}/approve", headers=admin_headers
    ).status_code == 200
    applied = client.post(
        f"/api/change-batches/{batch['id']}/apply",
        headers=admin_headers,
        json={"confirmation": "CONFIRM ALL"},
    )
    assert applied.status_code == 200
    assert applied.get_json()["status"] == "partial_success"
```

- [ ] **Step 2: Run the end-to-end regression test**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests\e2e\test_complete_flow.py -k "write_all" -q
```

Expected: PASS because every behavior was introduced through failing focused
tests in Tasks 1–8. If it fails, identify the exact integration boundary, add a
focused failing regression test beside the owning component, then make the
minimal production change that turns both tests green.

- [ ] **Step 3: Update README and demo check with exact operator flow**

Document:

```text
1. Ask: thuc hien lenh write tren toan bo thiet bi
2. Inspect frozen devices, execution modes, commands, and risk.
3. Approve the batch.
4. Type CONFIRM ALL exactly.
5. Apply and review every child result; partial_success requires manual follow-up.
```

Update the security statement: destructive commands are not silently blocked or
silently executed; they create high-risk previews and require typed confirmation.

- [ ] **Step 4: Run formatting, migration, and full automated verification**

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest -q
..\.venv\Scripts\python.exe -m flask --app wsgi db upgrade
..\.venv\Scripts\python.exe -m flask --app wsgi db downgrade
..\.venv\Scripts\python.exe -m flask --app wsgi db upgrade
git diff --check
```

Expected: all tests pass; migration round-trip succeeds; diff check is clean.

- [ ] **Step 5: Verify the real browser flow**

Start the app with the repository’s configured development command. In the
in-app browser: log in as ADMIN, submit the Vietnamese motivating request,
inspect all child previews, approve, confirm that wrong text keeps Apply
disabled, enter `CONFIRM ALL`, apply, inspect per-device results, refresh the
page to verify persisted live state, and log out. Capture any browser console or
network errors and fix them with a new failing regression test first.

- [ ] **Step 6: Commit**

```powershell
git add backend/tests/e2e/test_complete_flow.py backend/README.md backend/scripts/demo_check.py
git commit -m "test: verify full-authority batch configuration flow"
```

---

## Final verification checklist

- [ ] Every new behavior was first observed failing in its focused test.
- [ ] Full backend test suite passes with pristine output.
- [ ] Alembic upgrade/downgrade/upgrade round-trip passes.
- [ ] No unapproved files such as `.claude/` are staged.
- [ ] `git diff --check` passes.
- [ ] The browser flow works with `CONFIRM ALL` and persists after refresh.
- [ ] No credentials, full configurations, or confirmation values appear in AI context or audit details.
- [ ] `write memory` is recorded as EXEC mode and never wrapped in configuration mode.
- [ ] Final changes receive code review before branch integration.
