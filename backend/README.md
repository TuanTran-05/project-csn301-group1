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

### AI provider

The copilot defaults to **Gemini** — Flash-class models are cheap enough to leave
enabled during a demo. Get a key at <https://aistudio.google.com/apikey> and set
`AI_API_KEY`. The `google-genai` SDK is a normal dependency, so nothing extra is
needed.

| `AI_MODEL` | Notes |
|---|---|
| `gemini-2.5-flash-lite` | Cheapest; fine for this command-selection task |
| `gemini-2.5-flash` | Default |
| `gemini-3.5-flash` | Verified against the lab inventory |
| `gemini-2.5-pro` | Only if the Flash models mis-read requests |

Three settings make the copilot reliable, and were each chosen from measurements
against `gemini-3.5-flash`, not from taste:

- **`response_schema`** — asking only for `response_mime_type: application/json`
  was not enough: responses occasionally repeated a fragment mid-string and
  stopped being parseable. A schema constrains decoding, so the shape is a
  guarantee.
- **Thinking disabled** for command selection. With it on, 1 response in 4 was
  unparseable and every call burned 190–315 extra tokens. Off, the answer is
  byte-identical across runs. Free-text troubleshooting analysis still allows
  thinking, where reasoning actually helps. A model that refuses to disable
  thinking (`gemini-2.5-pro`) is retried once without the setting.
- **One retry** when a response still fails to parse. A refusal is never
  retried: it is a real answer, and asking again only costs money.

To use Claude instead: `AI_PROVIDER=anthropic`, `AI_MODEL=claude-sonnet-5`, and
`pip install "network-copilot[anthropic]"`.

Without a key the API stays up and every non-AI endpoint works; `/api/ai/chat`
answers `503 ai_not_configured` rather than failing as a server error.

## Deploying to the AI Server node

The AI Server is a Linux node inside PNETLab. `scripts/setup_ai_server.sh`
handles the software side; the network side is manual.

### 1. Pick a node image with Python 3.11+

```bash
python3 --version
```

The backend uses `X | None` annotations that Pydantic evaluates at runtime, so
**3.10 fails at import**. Ubuntu 22.04 ships 3.10 — use Ubuntu 24.04 (3.12) or
Debian 12 (3.11), or install `python3.11` alongside. The setup script checks
this first and refuses to continue rather than failing later in a confusing way.

### 2. Interfaces

| NIC | Network | Address | Gateway |
|---|---|---|---|
| 1 | management (`MGMT-NET` bridge) | `10.10.10.10/24` | **none** |
| 2 | production | `10.10.70.20/24` | `10.10.70.1` |
| 3 | temporary: `Cloud0`/`pnet0` for package installs | DHCP | — |

The management NIC has no gateway on purpose: every device sits in the same
broadcast domain, so nothing needs routing.

Check the real interface names with `ip link` — they are often `ens3/ens4/ens5`,
not `eth0`. Then, on Ubuntu, `/etc/netplan/01-lab.yaml`:

```yaml
network:
  version: 2
  ethernets:
    ens3:
      dhcp4: true          # temporary, for installing packages
    ens4:
      dhcp4: false
      addresses: [10.10.10.10/24]
    ens5:
      dhcp4: false
      addresses: [10.10.70.20/24]
      routes:
        - to: default
          via: 10.10.70.1
```

```bash
sudo netplan apply
```

Detach NIC 3 once setup is done. Leaving it attached gives the node two default
routes — the DHCP one and the production one — and traffic then follows
whichever won, which is not something you want to debug mid-demo.

### 3. Copy the code

`git archive` ships exactly the committed files, so `.venv`, `.env` and the
database are all left behind automatically. From the project root on your
workstation:

```bash
git archive --format=tar.gz -o network-copilot.tar.gz HEAD
```

```bash
scp network-copilot.tar.gz user@<node-ip>:~/
```

Then on the node:

```bash
mkdir -p ~/network-copilot && tar -xzf ~/network-copilot.tar.gz -C ~/network-copilot
```

### 4. Run setup

```bash
cd ~/network-copilot/backend && ./scripts/setup_ai_server.sh
```

It verifies the interpreter, builds the virtualenv, installs dependencies,
writes a `.env` with freshly generated keys (mode 600), applies migrations and
runs the test suite. It stops before seeding and lists what you still need to
fill in — passwords are yours to choose, so it never invents them.

`.env` is deliberately not transferred: the node generates its own keys. If you
want to reuse a database seeded elsewhere, copy that `CREDENTIAL_ENCRYPTION_KEY`
across as well, or the stored device passwords cannot be decrypted. Re-seeding
is usually simpler.

## Verifying against the real lab

Run this on the AI Server (management NIC `10.10.10.10/24`):

```bash
python scripts/smoke_test_lab.py
```

It checks TCP/22, opens an SSH session and runs `show clock` on all eight devices,
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

Several failures share one HTTP status, so `error` is more specific than the
status alone and is what a client should branch on:

| Status | `error` values |
|---|---|
| 403 | `policy_violation` (the policy engine refused the command), `forbidden` (your role is not allowed) |
| 409 | `invalid_state` (wrong change state), `conflict` (duplicate hostname or IP) |
| 502 | `ssh_timeout`, `ssh_connection_error`, `ssh_authentication_error`, `device_unreachable`, `ai_provider_error` |
| 503 | `ai_not_configured` (no `AI_API_KEY`, or the provider SDK is missing) |

### Database path

`DATABASE_URL` accepts a relative sqlite path (`sqlite:///network_copilot.db`)
and anchors it to the `backend/` directory. This is deliberate: Flask would
otherwise resolve it against its instance folder, so `flask db upgrade` and a
script that forgot `load_dotenv()` would silently use two different files.

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

## Lab inventory

`scripts/seed_lab.py` seeds these eight devices. **The hostnames must match the
device hostnames in PNETLab exactly** — the copilot resolves a device by
hostname, so a mismatch fails the request.

| Hostname | Management IP | Role |
|---|---|---|
| `ISP-RTR` | 10.10.10.4 | isp |
| `FW-01` | 10.10.10.3 | firewall |
| `INTERNAL-RTR` | 10.10.10.11 | core |
| `DIST-SW1` | 10.10.10.21 | distribution |
| `DIST-SW2` | 10.10.10.22 | distribution |
| `ACC-SW1` | 10.10.10.31 | access |
| `ACC-SW3` | 10.10.10.33 | access |
| `DMZ-SW` | 10.10.10.34 | dmz |

`INTERNAL-RTR` is a router that fills the `core` role in this topology. Role
drives behaviour, not the device type: monitoring polls OSPF on core and
distribution devices, and VLANs on access and distribution ones.

## Demo script

1. Backend SSHes into `INTERNAL-RTR`.
2. `show ip interface brief` returns output.
3. Monitoring stores a snapshot.
4. "Kiểm tra OSPF của DIST-SW1" runs a read-only command.
5. "Tạo VLAN 25 MARKETING trên ACC-SW1" creates a Preview only.
6. Admin approves and applies.
7. `show vlan brief` confirms VLAN 25.
8. "write erase" is blocked and audited.

Steps 1–8 are covered end to end by `tests/e2e/test_complete_flow.py`.
