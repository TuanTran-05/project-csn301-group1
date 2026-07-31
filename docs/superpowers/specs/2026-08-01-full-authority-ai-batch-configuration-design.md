# Full-Authority AI Batch Configuration — Design Spec

**Date:** 2026-08-01
**Status:** Approved for planning

## Goal

Allow an ADMIN to ask the AI copilot, in Vietnamese or English, to propose any
Cisco CLI command for one device, a selected group, or every device in the
inventory. The backend must show an immutable preview before execution, require
the existing Approve/Apply flow for every change, and add an explicit typed
confirmation for dangerous commands. A dangerous operation targeting multiple
devices requires the exact phrase `CONFIRM ALL` once for the batch.

The motivating request is `thuc hien lenh write tren toan bo thiet bi`. It must
produce a batch preview containing one EXEC-mode `write memory` change per
resolved device instead of the current AI refusal.

## Success criteria

- The AI is no longer restricted to the three legacy configuration templates.
- The AI can propose arbitrary Cisco CLI command sequences.
- The backend, not the AI, determines whether a command is dangerous.
- Configuration-mode and privileged-EXEC commands are executed in the correct
  CLI mode.
- The selector for “all devices” is frozen to explicit device IDs and hostnames
  when the preview is created.
- Each device keeps its own backup, execution result, verification result,
  rollback guidance, and failure state.
- A failure on one device never prevents the remaining devices from running.
- Existing single-device change APIs continue to work.
- Command injection through command separators remains blocked.

## Non-goals

- Atomic all-or-nothing configuration across network devices.
- Automatic rollback of successful devices when another device fails.
- A distributed queue, Celery, or parallel device execution.
- Letting non-ADMIN users propose, approve, or apply configuration.
- Blindly answering arbitrary interactive CLI questions. Only explicitly
  recognized confirmation prompts may be answered automatically.
- Treating shell-style command chaining as “full authority.” Multiple commands
  must be represented as separate strings in the structured command list.

## Root cause being fixed

The downstream change workflow was recently expanded to accept arbitrary
configuration and mark dangerous commands for typed confirmation. The AI system
prompt and `SUPPORTED_ACTIONS` context were not updated, so the model is still
instructed to emit an empty command list for commands such as `write`. The
interpreter converts that well-formed refusal into HTTP 422 before policy,
preview, or SSH execution is reached.

There are two additional structural mismatches:

1. `AIAction` contains one `device_hostname`, so “all devices” cannot be
   represented.
2. Every change is wrapped in `configure terminal ... end`, even for privileged
   EXEC commands such as `write memory`, `copy`, and `reload`.

The feature fixes all three layers together. Merely adding `write` to an
allowlist would remove the visible refusal but still execute it in the wrong
mode.

## Architecture

### AI proposal schema

Replace the single-target AI configuration shape with a plan containing one or
more operations:

```json
{
  "intent": "configure",
  "operations": [
    {
      "device_hostnames": ["*"],
      "execution_mode": "exec",
      "commands": ["write memory"],
      "verification_commands": []
    }
  ],
  "explanation": "Save the running configuration on every device."
}
```

`device_hostnames` accepts explicit inventory hostnames or the reserved value
`"*"`. The wildcard cannot be mixed with explicit hostnames. An operation means
“run this command sequence on each resolved target.” The AI may emit several
operations when different roles or device types require different commands.

`execution_mode` is an enum with two values:

- `config`: commands run inside one `configure terminal ... end` block.
- `exec`: commands run at privileged EXEC level without configuration wrappers.

The provider-facing schema remains strict and rejects missing operations,
empty target lists, empty command lists, and unknown execution modes. Monitor
and troubleshoot retain their existing read-only policy; multi-device batch
persistence is introduced only for `configure` in this iteration.

### Prompt and context

Remove the three-entry `SUPPORTED_ACTIONS` restriction. The prompt states that
the AI may propose any Cisco CLI configuration or privileged-EXEC command, but
that every proposal is only a preview and never bypasses backend approval.

The prompt must:

- Use `"*"` when the user explicitly requests every device.
- Split heterogeneous targets into separate operations.
- Select `exec` for privileged-EXEC commands and `config` for configuration
  commands.
- Never claim that a proposal was already executed.
- Never decide or emit risk, confirmation, approval, or authorization fields.
- Continue using only the read-only allowlist for monitor/troubleshoot.

Inventory context still excludes credentials, management IPs, and complete
running configurations.

### Batch and child changes

Add a `ChangeBatch` parent resource. Every resolved target becomes an existing
`ChangeRequest` child so current per-device backup, audit, verification, and
status behavior remains reusable.

`change_batches` stores:

| Column | Purpose |
|---|---|
| `id` | Primary key |
| `status` | `pending_approval`, `approved`, `running`, `success`, `partial_success`, `failed`, or `cancelled` |
| `risk_level` | Maximum risk of its children |
| `requires_confirmation` | True when any child is dangerous |
| `description` | AI explanation, capped consistently with changes |
| `source` | `ai` for this flow |
| `requested_by_id` / `approved_by_id` | Authorization and audit ownership |
| `created_at` / `approved_at` / `applied_at` | Lifecycle timestamps |

`ChangeRequest` gains:

- nullable `batch_id`, indexed and foreign-keyed to `change_batches`;
- `execution_mode`, defaulting to `config` for backward compatibility.

No confirmation phrase or secret is persisted. The expected phrase is derived
from the frozen children: the exact hostname for one child, and `CONFIRM ALL`
for two or more children.

AI configuration always creates a batch, including a one-device plan. Direct
calls to the existing `/api/changes/preview` endpoint continue creating a
standalone `ChangeRequest`.

### Target resolution and immutable preview

At preview creation, the service resolves each operation hostname against the
database. `"*"` resolves all devices in deterministic hostname order. Duplicate
targets across operations are rejected when they would receive conflicting
command sequences; exact duplicate operations are collapsed.

The resolved device IDs, hostnames, modes, and commands are persisted in child
changes. Later inventory edits do not change an approved batch. A device added
after preview is therefore not affected, and a renamed or deleted target causes
that child to fail safely rather than redirecting commands to another device.

Batch creation is transactional: either every child preview is stored, or none
is. No SSH connection is opened during interpretation, validation, or preview.

## Policy and risk classification

“Full authority” means arbitrary Cisco CLI proposals are accepted after
structural validation. It does not move trust into the model.

Backend rules remain authoritative:

- Empty commands and command strings containing `;`, `|`, `&`, newlines,
  carriage returns, `$(`, backticks, `>`, or `<` are rejected.
- Existing dangerous prefixes remain dangerous: `write`, `erase`, `reload`,
  `delete`, `format`, `debug`, `undebug`, `copy`, `configure`, `conf t`, `no`,
  `shutdown`, `clear`, `reset`, `boot`, `archive`, `setup`, `tclsh`, and
  `squeeze`.
- Operations touching reserved VLANs 1 and 1002–1005 are dangerous.
- A child is `high` risk and requires confirmation when any of its commands is
  dangerous. The batch inherits the maximum child risk and requires
  confirmation when any child does.
- The AI cannot lower risk or suppress confirmation because those fields are
  absent from the AI schema.

Safe changes still require normal Approve and Apply actions. Dangerous changes
require an additional exact typed confirmation at Apply time.

### Execution-mode validation

The backend owns a list of unambiguous privileged-EXEC command families,
including `write`, `copy`, `reload`, `erase`, `delete`, `format`, `clear`,
`debug`, `undebug`, `show`, `ping`, and `traceroute`. Preview is rejected if the
AI assigns one of these to `config` mode. Known configuration wrappers are also
rejected inside an operation body because the backend applies wrappers exactly
once.

For command families the backend cannot classify reliably, it accepts the
AI-selected mode and relies on the immutable human-readable preview. This is
necessary to support arbitrary platform-specific CLI without pretending the
backend has a complete Cisco grammar.

## Approval and apply flow

New endpoints:

```text
GET  /api/change-batches
GET  /api/change-batches/<id>
POST /api/change-batches/<id>/approve
POST /api/change-batches/<id>/apply
POST /api/change-batches/<id>/cancel
```

All mutating endpoints require `ADMIN`. Apply accepts:

```json
{"confirmation": "CONFIRM ALL"}
```

For a one-child dangerous batch, `confirmation` must instead equal that
child’s exact hostname. Comparison is case-sensitive after trimming surrounding
whitespace. Safe batches ignore an omitted confirmation and do not require one.

Approving a batch transitions all pending children to `approved` in one
transaction. Applying a batch validates authorization, state, and typed
confirmation before any SSH connection opens, marks the batch `running`, then
processes children sequentially in deterministic hostname order.

For each child:

1. Open the device connection.
2. Capture the existing running configuration backup. If backup fails, mark
   only that child failed and continue.
3. Run the commands using the persisted execution mode.
4. Run the persisted or derived read-only verification commands.
5. Store output and mark the child `success` or `failed`.

The final batch status is:

- `success` when every child succeeds;
- `partial_success` when at least one succeeds and at least one fails;
- `failed` when no child succeeds;
- `cancelled` only when cancelled before Apply.

The operation is deliberately not atomic. Successful devices are never rolled
back because another device failed. Each child retains best-effort rollback
guidance for manual review. Reapplying a completed or running batch is rejected
by the state machine.

Apply is synchronous and sequential for the current lab scale, matching the
existing change workflow and avoiding a new queue dependency. The UI keeps its
busy state until the response arrives and then renders the complete per-device
result.

## SSH execution

The SSH adapter exposes separate semantic operations:

- `run_config(commands)` for one backend-wrapped configuration block;
- `run_exec(commands, allow_confirm=False)` for sequential privileged-EXEC
  commands over the existing interactive shell.

`write memory` therefore runs as `write memory`, never as `configure terminal`,
`write memory`, `end`.

Interactive prompt handling is allowlisted. After the typed dangerous
confirmation has passed, the adapter may answer recognized `[confirm]`,
yes/no, and the default destination prompt for the exact
`copy running-config startup-config` operation. Any unrecognized prompt fails
that child safely. The backend never forwards an arbitrary AI-generated prompt
answer. A disruptive command that drops the session may be reported as failed
or unverifiable unless the adapter can positively determine dispatch; the UI
must not claim success without evidence.

For save-configuration operations, verification is derived by the backend and
is not delegated to the model. Any sensitive configuration output used for
verification remains local and is not sent back to the AI provider.

## API response and compatibility

A successful AI configuration response contains:

```json
{
  "intent": "configure",
  "batch": {
    "id": 12,
    "status": "pending_approval",
    "risk_level": "high",
    "requires_confirmation": true,
    "confirmation_text": "CONFIRM ALL",
    "changes": []
  },
  "requires_approval": true,
  "explanation": "Save the running configuration on every device."
}
```

Existing standalone change endpoints and response shapes remain valid. The
change listing API gains a filter that lets the frontend request standalone
changes only, preventing batch children from appearing twice. Existing chat
history continues storing the complete AI result as message payload.

## Frontend

Add a live client-side batch map keyed by batch ID, parallel to the existing
change map. Chat payloads are historical snapshots only; action cards resolve
their current state from the live map so actions taken in the sidebar and chat
remain consistent.

A batch card displays:

- description, device count, risk badge, and aggregate status;
- the exact required confirmation phrase without pre-filling the input;
- expandable child rows containing hostname, execution mode, commands,
  verification commands, status, output, and error;
- Approve, Apply, and Cancel controls for ADMIN users;
- a typed confirmation input after approval when confirmation is required;
- an explicit `partial_success` summary with successful and failed counts.

The pending-changes sidebar displays both standalone changes and batch parents,
not individual batch children. Buttons are hidden for non-ADMIN users as a UX
convenience; backend role checks remain the security boundary.

## Audit

Record batch-level events for preview, approve, confirmation failure, apply
start, aggregate completion, and cancel. Continue recording existing
per-device backup/change events. Audit details include batch ID, frozen device
IDs/hostnames, modes, commands, child result counts, and user ID. They never
include credentials or typed confirmation values.

## Error handling

- Invalid AI JSON or schema remains HTTP 422 and is never executed.
- Unknown or duplicate-conflicting targets reject the entire preview.
- Missing or incorrect typed confirmation returns HTTP 422 before SSH.
- A failure during batch creation rolls back the database transaction.
- A connection, backup, command, or verification failure is stored on that
  child and does not interrupt remaining children.
- An unexpected server exception marks the active child failed, continues when
  safe, and leaves an audit event with a redacted message.
- Chat records the same user-visible error/success payload as the current flow.

## Testing strategy

Implementation follows red-green-refactor TDD. Required automated coverage:

### AI and schema

- The prompt advertises full configuration authority and no legacy three-action
  restriction.
- A Vietnamese request for `write` on every device produces an EXEC operation
  targeting `"*"`.
- Multiple operations and explicit hostname groups validate.
- Empty operations, invalid modes, mixed wildcard/explicit targets, and malformed
  provider output are rejected.
- Monitor/troubleshoot remain constrained to the read-only policy.

### Batch service and policy

- Wildcard expansion freezes all current devices in hostname order.
- Preview creates one parent and one child per resolved target atomically.
- Exact duplicate targets collapse; conflicting duplicates reject.
- Risk and confirmation are derived by the backend and aggregate to the batch.
- Safe batches do not require typed confirmation.
- Dangerous single-child batches require the exact hostname.
- Dangerous multi-child batches reject missing/wrong confirmation and accept
  only `CONFIRM ALL`.
- No SSH occurs before Apply.
- Command separators remain blocked.
- Known EXEC/config mode mismatches reject preview.

### Execution

- EXEC commands receive no configuration wrappers.
- Config commands receive exactly one wrapper pair.
- `write memory` uses EXEC mode.
- Unrecognized interactive prompts fail safely.
- A child connection, backup, command, or verification failure does not prevent
  the next child from running.
- Aggregate statuses cover all-success, mixed, and all-failed batches.
- A second Apply is rejected.

### API, authorization, audit, and UI

- Viewer and operator accounts cannot preview, approve, apply, or cancel a
  configuration batch.
- Batch endpoints serialize live child state and expected confirmation text.
- Audit events are emitted without credentials or confirmation values.
- Chat history stores batch payloads.
- Batch cards render device/mode/command previews, confirmation UI, and partial
  results.
- Existing standalone change tests and the full backend suite remain green.

Manual browser verification covers login, the Vietnamese motivating request,
batch preview, approval, rejected confirmation, accepted `CONFIRM ALL`,
per-device results, sidebar/chat synchronization, refresh persistence, and
logout.

## Rollout

The rollout adds one table and two columns through an Alembic migration. Deploy
with the existing process: update code, install unchanged Python dependencies,
run `flask db upgrade`, restart the Flask process, and exercise one safe test
batch before a dangerous batch. No new service or frontend build system is
introduced.
