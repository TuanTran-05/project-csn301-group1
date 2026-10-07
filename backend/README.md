# Network Copilot — AI Network Management Backend

Flask backend that manages Cisco networks, PNETLab labs and real hardware alike,
over SSH. Each network is a **project**: its devices, topology diagram, chat and
change history are isolated from every other project. It monitors device
state, runs policy-checked read-only commands, and drives configuration changes
through a **Preview → Approve → Apply → Verify** workflow. An AI copilot can turn
plain Vietnamese or English into a *proposal*, but it can never execute anything
itself.

## Safety model

The rules below are enforced by code and covered by tests, not by convention:

| Rule | Where it is enforced |
|---|---|
| A project's devices, changes, chat, audit and AI context never reach another project; `"*"` means "every device of *this* project" | `projects/scope.py`, `devices/service.py`, `changes/batch_service.py`, `ai/service.py` |
| A user only sees projects they own or that were shared with them; ADMIN sees all | `projects/service.py::access_level` |
| Unknown read-only commands are denied by default | `commands/policy.py` — allowlist only |
| Destructive configuration commands are never silently blocked or silently executed: they become high-risk previews and require typed confirmation before Apply | `changes/service.py`, `changes/batch_service.py` |
| Only `ADMIN` may approve or apply a change | `auth/service.py::roles_required` |
| Every change is previewed and approved before it runs | `changes/service.py` |
| A `show running-config` backup is taken before every apply | `backups/service.py` |
| Verification runs after every apply; a failed check never reports success | `changes/service.py::run_verification` |
| AI configuration proposals are validated and frozen into device-scoped changes before approval; the AI request itself never opens SSH | `ai/schemas.py`, `changes/batch_service.py` |
| Credentials are encrypted at rest and never serialised | `credentials/service.py` |
| The AI never receives credentials, management IPs or a full running-config | `ai/service.py::build_context` |
| Audit entries are redacted before they are stored | `audit/service.py::redact_sensitive` |

Not in this MVP: zero-touch provisioning, auto-discovery, automatic rollback
(rollback commands are surfaced, never executed), and full multi-vendor support.

## Projects, sharing and the network designer

Open <http://127.0.0.1:5000/projects>.

- **Projects.** Anyone except a read-only `VIEWER` can create projects. Each one
  declares its own management network (a private IPv4 range no wider than /16,
  e.g. `172.16.3.0/24` or `192.168.50.0/24`) and an environment: `pnetlab`,
  `physical` or `mixed`. Hostnames and management IPs are unique *within* a
  project, so two projects may both have an `R1`. Two projects that reuse the
  same address range cannot both be reachable from one backend at once; give
  each lab its own range if they run together.
- **Devices.** Add a device — environment (PNETLab or physical hardware), SSH
  port and its SSH login — from the page or `POST /api/devices`. The password is
  encrypted before it is stored and is never returned. There is no seed list to
  edit.
- **Sharing.** The owner shares a project with a username as `viewer` (read) or
  `editor` (devices and diagram). Sharing never grants more than the user's
  global role: a `VIEWER` account stays read-only everywhere. An ADMIN can see
  and manage every project. ADMINs create accounts at `POST /api/users` or in
  the page's account panel.
- **Selecting a project.** API calls name the project with the `X-Project-Id`
  header (or `?project_id=`). If the caller has exactly one project it is used
  automatically; with several and no header the API answers
  `400 project_required`. A project you cannot access is `404`, never `403`.
  The chat, dashboard and projects pages share the selection.
- **Network designer.** The *Sơ đồ mạng* tab is a canvas: drag devices, connect
  interfaces, and mark each link `physical`, `routed` (a point-to-point subnet,
  addresses derived or set by hand) or `trunk` (allowed VLANs). **Xem cấu hình
  sinh ra** shows the IOS commands per device. **Tạo bản xem trước** (ADMIN)
  freezes them as a change batch in that project, which is then approved and
  applied through the normal Preview → Approve → Apply → Verify flow — the
  design never touches a device by itself. Generated interfaces use
  `no shutdown`, which the safeguards treat as a negation, so these batches ask
  for the typed confirmation. ASA devices are listed as skipped (they need a
  `nameif`/security level the design does not carry).

Approving and applying a change is still ADMIN-only, in every project.

### User administration

ADMINs manage accounts at <http://127.0.0.1:5000/users> (or `/api/users`):
create (username, password ≥ 10 characters, role, optional name and email),
edit role / profile, reset a password, and deactivate or re-activate. Accounts
are deactivated, never deleted, so audit history keeps its author. Anyone can
change their own password on the same page.

* Changing a password, a role or the active flag logs that user out everywhere
  (their tokens are revoked), and the role is always read from the database, so
  a demotion applies immediately.
* The last active ADMIN cannot be demoted or deactivated, and an administrator
  cannot demote or deactivate themselves.
* The first administrator, or promoting an account that already exists, is done
  from the command line, with no server running:

  ```bash
  python scripts/manage_users.py ensure-admin g1    # create it, or promote it and keep its password
  python scripts/manage_users.py list
  python scripts/manage_users.py set-password g1    # prompts; NEW_USER_PASSWORD also works
  ```

### Database

The schema is documented, with an entity diagram, in
[`docs/database.md`](../docs/database.md): foreign keys are enforced, enumerated
columns have CHECK constraints, and hostnames/IPs are unique per project.

### Upgrading an existing database

`flask db upgrade` adds the project tables and moves everything already in the
database into one **Default Lab** project (owned by the first ADMIN; existing
non-admin users become its viewers). Nothing is lost; hostnames and IPs remain
valid because the project's network defaults to `MANAGEMENT_NETWORK`.
Downgrading restores global hostname/IP uniqueness and fails if two projects now
reuse a hostname or an address.

## Requirements

- Python 3.11+
- Network reachability to the management network `172.16.3.0/24`

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
# Choose this secret yourself; the repository does not provide a demo password.
export SEED_ADMIN_PASSWORD='<chosen-password>'
python scripts/seed_lab.py     # optional: a "PNETLab" project with the three lab devices
flask --app wsgi run --host 0.0.0.0 --port 5000
```

On Windows PowerShell, set `$env:SEED_ADMIN_PASSWORD` instead of using
`export`. `SEED_ADMIN_USERNAME` is optional and defaults to `admin`; the seeded
account has the `ADMIN` role. Open <http://127.0.0.1:5000/> after the server
starts.

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

### Full-authority batch operator flow

The motivating demo deliberately separates the AI proposal from operator
authority:

1. Ask: `thuc hien lenh write tren toan bo thiet bi`
2. Inspect the frozen devices, execution modes, commands, and risk.
3. Approve the batch.
4. Type `CONFIRM ALL` exactly.
5. Apply and review every child result; `partial_success` requires manual
   follow-up before retrying failed devices.

For a live lab, configure `AI_API_KEY`, seed the ADMIN account and encrypted SSH
credentials, start the backend, then run:

```bash
python scripts/demo_check.py --username admin --password '<chosen-password>'
```

Use the username selected through `SEED_ADMIN_USERNAME` if it is not `admin`.
The password is the operator-chosen `SEED_ADMIN_PASSWORD`; no demo credential is
hard-coded in this repository. After displaying the frozen preview, the CLI
requires an interactive-terminal entry of `CONFIRM ALL` before it submits either
the batch approval or apply request; piped input and a wrong or missing entry
abort the run.

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

| Interface | Network | Address | Gateway |
|---|---|---|---|
| `enp6s18` | management | `172.16.3.28/24` | `172.16.3.1` |

The installed systemd service binds Gunicorn to `172.16.3.28:5000`.
This migration does not change the node netplan.

Check the real interface names with `ip link` — it is typically `enp6s18` on this host. Then, on Ubuntu, `/etc/netplan/01-lab.yaml` should include:

```yaml
network:
  version: 2
  ethernets:
    enp6s18:
      dhcp4: false
      addresses: [172.16.3.28/24]
      routes:
        - to: default
          via: 172.16.3.1
```

```bash
sudo netplan apply
```

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

Run this on the AI Server (management NIC `172.16.3.28/24`):

```bash
./.venv/bin/python scripts/smoke_test_lab.py
```

The command verifies exact inventory matching (`R1`, `SW1`, `SW2` in the `PNETLab` project) before any TCP or SSH checks, then opens SSH and runs `show clock` on all three devices.

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
| GET/POST | `/api/projects` | any / not VIEWER | List accessible projects / create one |
| GET/PUT/DELETE | `/api/projects/<pid>` | viewer / owner | Read / update / delete (owner or ADMIN) |
| GET/POST | `/api/projects/<pid>/members` | viewer / owner | List / share |
| PUT/DELETE | `/api/projects/<pid>/members/<uid>` | owner | Change access / unshare |
| GET | `/api/projects/<pid>/topology` | viewer | Devices (with canvas positions) and links |
| PUT | `/api/projects/<pid>/topology/layout` | editor | Save node positions |
| POST | `/api/projects/<pid>/topology/links` | editor | Add a link |
| PUT/DELETE | `/api/projects/<pid>/topology/links/<id>` | editor | Edit / delete a link |
| POST | `/api/projects/<pid>/topology/config-plan` | editor | Commands the design produces (no side effects) |
| POST | `/api/projects/<pid>/topology/config-preview` | ADMIN | Freeze them as a change batch |
| GET/POST | `/api/users` | ADMIN | List / create accounts |
| GET/PUT | `/api/users/<id>` | ADMIN | Read / edit role, profile, active flag |
| POST | `/api/users/<id>/reset-password` | ADMIN | Set a new password (revokes their tokens) |
| POST | `/api/auth/change-password` | any | Change your own password |
| GET/POST | `/api/devices` | viewer / editor | List / create devices in the current project |
| GET/PUT/DELETE | `/api/devices/<id>` | viewer / editor | Read / update / delete |
| POST | `/api/devices/<id>/test-connection` | editor | SSH reachability check |
| GET | `/api/devices/<id>/status` | any | Latest monitoring snapshot |
| GET | `/api/devices/<id>/snapshots` | any | Snapshot history |
| POST | `/api/devices/<id>/refresh` | any | Poll now |
| GET | `/api/devices/<id>/backups` | any | Config backups |
| GET | `/api/devices/<id>/backups/<backup_id>` | editor | One backup, with config |
| POST | `/api/commands/execute-readonly` | any | Run an allowlisted command |
| GET | `/api/commands/history` | any | Past executions |
| POST | `/api/changes/preview` | ADMIN | Create a preview |
| GET | `/api/changes` | any | List changes |
| GET | `/api/changes/<id>` | any | One change |
| POST | `/api/changes/<id>/approve` | ADMIN | Approve |
| POST | `/api/changes/<id>/apply` | ADMIN | Backup → apply → verify (10 req/min) |
| POST | `/api/changes/<id>/cancel` | ADMIN | Cancel |
| GET | `/api/change-batches` | any | List frozen multi-device batches |
| GET | `/api/change-batches/<id>` | any | One batch with every child result |
| POST | `/api/change-batches/<id>/approve` | ADMIN | Approve all frozen children |
| POST | `/api/change-batches/<id>/apply` | ADMIN | Apply all children; high risk requires typed confirmation |
| POST | `/api/change-batches/<id>/cancel` | ADMIN | Cancel the batch |
| GET | `/api/audit-logs` | ADMIN | Filterable audit trail of the current project (`?scope=all` for every project) |
| POST | `/api/ai/chat` | any* | AI copilot (20 req/min/user), scoped to the current project |

\* `configure` intents additionally require `ADMIN`.

Everything below `/api/devices`, `/api/commands`, `/api/changes`,
`/api/change-batches`, `/api/chat`, `/api/ai`, `/api/dashboard` and
`/api/audit-logs` acts on the current project (see *Selecting a project*). The
role column above is the minimum **project access** (viewer < editor < owner);
the change workflow keeps its global ADMIN requirement on top.

### Change states

`pending_approval → approved → running → success | failed`, plus `cancelled`
from either of the first two states.

Batch changes add the terminal `partial_success` state. It means processing
continued after one or more child failures; operators must inspect every child
result and manually follow up on failed devices.

### Error contract

Every error is JSON and carries the request id, so a client log line can be
matched to a server log line:

```json
{
  "error": "policy_violation",
  "message": "Command is blocked: write commands modify or erase device configuration.",
  "details": {"command": "write erase", "device": "SW1"},
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
| 400 | `project_required` (no project chosen and none can be inferred) |
| 409 | `invalid_state` (wrong change state), `conflict` (duplicate hostname or IP in the project, overlapping link subnet, interface already cabled) |
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
├── auth/             # users, JWT, token revocation, roles_required
├── projects/         # projects, sharing, request scoping (X-Project-Id)
├── topology/         # diagram links, layout, config generation
├── devices/          # inventory CRUD + validation (per project)
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

`scripts/seed_lab.py` seeds these three devices into a `PNETLab` project
(`SEED_PROJECT_NAME` to rename it). It is only a convenience for the course lab:
any other network is added from the Projects page. **The hostnames must match the
device hostnames in PNETLab exactly** — the copilot resolves a device by
hostname, so a mismatch fails the request.

| Hostname | Management IP | Device type | Role |
|---|---:|---|---|
| `R1` | `172.16.3.111` | `cisco_ios` | `core` |
| `SW1` | `172.16.3.121` | `cisco_ios` | `access` |
| `SW2` | `172.16.3.122` | `cisco_ios` | `access` |

`R1` is a router that fills the `core` role in this topology. `R1` receives
routing checks while VLAN checks target `SW1` and `SW2`.

## Demo script

1. The backend logs in an ADMIN, lists inventory, and checks the live lab.
2. `show ip interface brief` and the VLAN monitor request for `SW1` exercise read-only
   paths.
3. `thuc hien lenh write tren toan bo thiet bi` creates a frozen batch preview;
   no SSH write occurs during the AI request.
4. The script inspects every target, execution mode, command, and risk before it
   approves anything.
5. It requires an interactive operator to type `CONFIRM ALL` before it submits
   approval or apply, then reviews every child result against the frozen preview.
   A `partial_success` or `failed` result prints a manual-follow-up warning and
   exits non-zero through the failed child checks.

The workflow and credential-redaction surfaces are covered end to end by
`tests/e2e/test_complete_flow.py`.

## AI configuration capability status

The current course scope provides frozen semantic verification for VLAN,
access/trunk switchports, interface description/admin/IPv4, static routes and
configuration save operations. Bounded standard ACL, IOS DHCP-pool and
single-area OSPF recognizers are available as extended capabilities and still
require reviewed live-lab evidence before being claimed as production-ready.
Full NAT, advanced dynamic routing, ASA configuration, multi-vendor support,
auto-discovery, automatic rollback and production orchestration remain
Preview-only/out of scope.

See [`docs/evidence/2026-08-03-ai-network-copilot-evaluation.md`](../docs/evidence/2026-08-03-ai-network-copilot-evaluation.md)
for the evidence-based status and reproducible commands.
