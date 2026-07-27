# Network Copilot — AI Network Management Backend

Flask backend that manages a PNETLab Cisco topology over SSH: it monitors device
state, runs policy-checked read-only commands, and drives configuration changes
through a **Preview → Approve → Apply → Verify** workflow. An AI copilot can turn
plain Vietnamese or English into a *proposal*, but it can never execute anything
itself.

## Safety model

The rules below are enforced by code and covered by tests, not by convention:

| Rule | Where it is enforced |
|---|---|
| Unknown commands are denied by default | `commands/policy.py` — allowlist only |
| Destructive commands (`write erase`, `reload`, `debug`, …) never reach a device | `commands/policy.py`, checked before any SSH session opens |
| Only `ADMIN` may approve or apply a change | `auth/service.py::roles_required` |
| Every change is previewed and approved before it runs | `changes/service.py` |
| A `show running-config` backup is taken before every apply | `backups/service.py` |
| Verification runs after every apply; a failed check never reports success | `changes/service.py::run_verification` |
| Configuration is limited to three templates (VLAN, access port, description) | `changes/service.py::TEMPLATES` |
| Credentials are encrypted at rest and never serialised | `credentials/service.py` |
| The AI never receives credentials, management IPs or a full running-config | `ai/service.py::build_context` |
| Audit entries are redacted before they are stored | `audit/service.py::redact_sensitive` |

Not in this MVP: zero-touch provisioning, auto-discovery, automatic rollback
(rollback commands are surfaced, never executed), and full multi-vendor support.

## Requirements

- Python 3.11+
- Network reachability to the management network `10.10.10.0/24`

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on Linux
pip install -e "backend[dev]"
```

Copy `.env.example` to `.env` and fill it in. Generate the credential key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`CREDENTIAL_ENCRYPTION_KEY` is read only from the environment. Losing it makes
every stored device password unrecoverable.

## Running

```bash
cd backend
flask db upgrade
python scripts/seed_lab.py
flask --app wsgi run --host 0.0.0.0 --port 5000
```

Set `MONITORING_ENABLED=true` to start the 60-second polling scheduler. It is off
by default so tests and one-off CLI commands never spawn background jobs.

## Verifying against the real lab

Run this on the AI Server (management NIC `10.10.10.10/24`):

```bash
python scripts/smoke_test_lab.py
```

It checks TCP/22, opens an SSH session and runs `show clock` on all nine devices,
exiting non-zero if any of them fails.

## Tests

```bash
cd backend
pytest -v --cov=network_copilot --cov-report=term-missing
```

The suite never opens a socket or calls a real model: SSH and the AI provider are
injected through `SSH_CLIENT_FACTORY` and `AI_PROVIDER_INSTANCE`.

## API

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/health` | — | Liveness |
| POST | `/api/auth/login` | — | Obtain a JWT (5 req/min/IP) |
| GET | `/api/auth/me` | any | Current user |
| GET/POST | `/api/devices` | any / ADMIN | List / create devices |
| GET/PUT/DELETE | `/api/devices/<id>` | any / ADMIN | Read / update / delete |
| POST | `/api/devices/<id>/test-connection` | ADMIN | SSH reachability check |
| GET | `/api/devices/<id>/status` | any | Latest monitoring snapshot |
| GET | `/api/devices/<id>/snapshots` | any | Snapshot history |
| POST | `/api/devices/<id>/refresh` | any | Poll now |
| GET | `/api/devices/<id>/backups` | any | Config backups |
| GET | `/api/devices/<id>/backups/<backup_id>` | ADMIN | One backup, with config |
| POST | `/api/commands/execute-readonly` | any | Run an allowlisted command |
| GET | `/api/commands/history` | any | Past executions |
| POST | `/api/changes/preview` | ADMIN | Create a preview |
| GET | `/api/changes` | any | List changes |
| GET | `/api/changes/<id>` | any | One change |
| POST | `/api/changes/<id>/approve` | ADMIN | Approve |
| POST | `/api/changes/<id>/apply` | ADMIN | Backup → apply → verify (10 req/min) |
| POST | `/api/changes/<id>/cancel` | ADMIN | Cancel |
| GET | `/api/audit-logs` | ADMIN | Filterable audit trail |
| POST | `/api/ai/chat` | any* | AI copilot (20 req/min/user) |

\* `configure` intents additionally require `ADMIN`.

### Change states

`pending_approval → approved → running → success | failed`, plus `cancelled`
from either of the first two states.

### Error contract

Every error is JSON and carries the request id, so a client log line can be
matched to a server log line:

```json
{
  "error": "policy_violation",
  "message": "Command is blocked: write commands modify or erase device configuration.",
  "details": {"command": "write erase", "device": "ACC-SW1"},
  "request_id": "0f0a2f9c-..."
}
```

Unhandled exceptions always return a generic `internal_error`; tracebacks and
internal state go to the server log only.

## Architecture

Routes only handle HTTP. All SSH, policy, monitoring, backup, audit and AI logic
lives in services, so the same code paths are used whether a human or the AI
initiates an action.

```
src/network_copilot/
├── app.py            # factory, error contract, security headers, request id
├── config.py         # environment-driven config
├── extensions.py     # db, migrate, jwt, limiter
├── errors.py         # AppError hierarchy -> JSON
├── auth/             # users, JWT, roles_required
├── devices/          # inventory CRUD + validation
├── credentials/      # Fernet encryption at rest
├── ssh/              # Paramiko adapter (the only place sockets are opened)
├── commands/         # policy engine + read-only execution
├── parsers/          # Cisco output -> structured data
├── monitoring/       # polling service + APScheduler
├── changes/          # preview, approve, apply, verify
├── backups/          # running-config capture
├── audit/            # audit log + redaction
└── ai/               # provider, AIAction schema, copilot service
```

## Demo script

1. Backend SSHes into `CORE-SW1`.
2. `show ip interface brief` returns output.
3. Monitoring stores a snapshot.
4. "Kiểm tra OSPF của DIST-SW1" runs a read-only command.
5. "Tạo VLAN 25 MARKETING trên ACC-SW1" creates a Preview only.
6. Admin approves and applies.
7. `show vlan brief` confirms VLAN 25.
8. "write erase" is blocked and audited.

Steps 1–8 are covered end to end by `tests/e2e/test_complete_flow.py`.
