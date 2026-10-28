# Network Copilot — AI Network Management Backend

Flask backend that manages Cisco networks, PNETLab labs and real hardware alike,
over SSH. Each network is a **project**: its devices, topology diagram, chat and
change history are isolated from every other project. It monitors device
state, runs policy-checked read-only commands, and drives configuration changes
through a **Preview → Approve → Apply → Verify** workflow. An AI copilot can turn
plain Vietnamese or English into a *proposal*, but it can never execute anything
itself.

The backend now supports scoped Admin/Teacher/Student permissions, delegation and
expiry, mandatory Admin MFA, teaching groups, assignments, draft versions,
reservations, notifications, and lab-provider adapters. See
[the setup and API guide](../docs/scoped-backend.md) before upgrading. Existing
Teacher/Student memberships no longer imply execution permission. Production and
shared-lab deployments require a captured baseline and a current approval.

## Safety model

The rules below are enforced by code and covered by tests, not by convention:

| Rule | Where it is enforced |
|---|---|
| A project's devices, changes, chat, audit and AI context never reach another project; `"*"` means "every device of *this* project" | `projects/scope.py`, `devices/service.py`, `changes/batch_service.py`, `ai/service.py` |
| A user only sees projects they own or that were shared with them; ADMIN sees all | `projects/service.py::access_level` |
| Unknown read-only commands are denied by default | `commands/policy.py` — allowlist only |
| Destructive configuration commands are never silently blocked or silently executed: they become high-risk previews and require typed confirmation before Apply | `changes/service.py`, `changes/batch_service.py` |
| Every deployment rechecks account, explicit permission, group/device/environment scope, expiry, approval digest, baseline and maintenance; a per-device lease prevents concurrent writes | `governance/access.py`, `governance/workflow.py` |
| A Student needs a separate deployment grant. Smart Approval applies only to Low Risk in the selected enabled group; otherwise an authorized Teacher/Admin must approve the exact configuration | `governance/workflow.py`, `changes/service.py` |
| Every change is previewed and approved before it runs, including Smart Approval changes | `changes/service.py` |
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
- **Adding devices like Packet Tracer.** In the *Sơ đồ mạng* tab, drag a Router,
  Switch L2/L3 or Firewall from the palette onto the canvas (or click it). The name
  (`R1`, `SW2`…) and a free management IP are chosen for you; an optional default
  SSH login, kept only in the page, is stored (encrypted) on each new device.
  *Nối dây* cables two devices with two clicks and picks the free ports itself;
  `Delete` removes the selected device or link, `Esc` leaves cabling mode.
- **Importing from PNETLab.** *Devices → Nhập từ PNETLab* reads a lab's nodes,
  networks and cabling. PNETLab 4/5 only has a session-bound API (its EVE-NG paths
  answer "Resource not found (60038)"): the backend logs in through
  `/store/public/auth/login/login`, reuses the lab already open in that account's
  session or opens it with `/api/labs/session/factory/create {path}`, and reads
  `/api/labs/session/topology`; cables come from each interface's `network_id`, and a
  cloud network (`pnet0` = Cloud0) is never taken for a cable. EVE-NG (`/api/auth/login`,
  `/api/labs/<lab>.unl/nodes|topology|networks`) is the fallback; *Kiểu API* forces
  either. Leave the lab path empty to use the lab open in the session. PNETLab keeps one
  session per account and a running lab belongs to the account that opened it, so use
  that account (its browser tab may need to log in again). No node is started or edited. Pick the
  nodes, fix the management IPs (PNETLab does not know them), and import: devices
  come with their canvas positions and the cables between them. Only private
  lab addresses are accepted as the PNETLab URL, redirects are refused, and the
  lab password is never stored or logged. vIOS/IOL/CSR nodes become routers,
  `*l2*` nodes switches, ASAv firewalls; other templates (VPCS, Linux…) are listed
  as unsupported.
- **Bootstrapping SSH through the console.** A node freshly started in PNETLab has
  a blank configuration, so the address and login the import stored cannot be used
  yet. *Khởi tạo SSH qua console* (same panel) opens each running node's telnet
  console on the PNETLab host and sets the hostname, the management IP (on the
  port the lab cables to **Management(Cloud0)**, or one you name; an L2 switch gets
  it on `Vlan1`), an optional gateway, the device's saved login, RSA keys, SSH v2
  and `transport input ssh` on the VTY lines, then `write memory` and an SSH check.
  The preview shows the exact commands (secrets masked) and opens no console.
  Running it is limited to the project owner or an ADMIN and is audited. A device
  that already has another login, or another address on that interface, is left
  untouched unless *Ghi đè* is ticked; existing RSA keys are kept. Only Cisco IOS
  PNETLab devices are handled (ASA by hand), and the lab is never edited: nodes
  must be running and the Cloud0 cable must already exist. After that, everything
  goes over SSH as before.
- **Palo Alto firewalls (PAN-OS).** Device type `paloalto_panos` (palette *Firewall Palo
  Alto*; PNETLab templates `paloalto`/`pa-vm` import as it, role `firewall`). It is reached
  with Netmiko's `paloalto_panos` driver over SSH. Monitoring and the read-only policy use
  `show system info`, `show interface all`, `show interface <name>` and `show routing route`
  (parsed into facts, interfaces with zone/router/address, and routes per virtual router);
  backups use `show config running`. A change is PAN-OS configure-mode lines (`set`,
  `delete`, `rename`, `move`) that go through Preview → Approve → Apply: the backend enters
  `configure`, sends the lines and runs `commit`. If a line is rejected or the commit does not
  report success, the candidate is reverted (`revert config`), the change fails and nothing is
  applied. Rollback suggestions turn each `set` into a `delete` (never run automatically).
  *Khởi tạo SSH qua console* also handles a fresh PA-VM, as a **background job**: the
  request returns `202` with a job at once and the page polls
  `GET /api/projects/<pid>/topology/bootstrap/jobs/<id>`, so neither a reverse proxy nor
  Gunicorn cuts the minutes a PA-VM needs to boot (`BOOTSTRAP_PANOS_BOOT_SECONDS`, 900)
  and commit (`PANOS_COMMIT_TIMEOUT`, 600). Jobs live in the process' memory: run one
  Gunicorn worker with threads. The job logs in as `admin` with the factory password
  (*Mật khẩu mặc định PAN-OS*, not stored) and answers the forced first-login change with
  the saved SSH password, which must meet PAN-OS' rules (8+ characters, upper and lower
  case, a digit or a special character; checked in the preview). It turns on
  `set cli scripting-mode on`, types slowly (a serial console drops a burst, which reads as
  "Invalid syntax"), then `configure`s: **mgmt** (default when mgmt/eth0 is cabled to
  Cloud0) is `set deviceconfig system type static` (a PA-VM's mgmt port starts as a DHCP
  client) then `set deviceconfig system ip-address <ip> netmask <mask>` (+ `default-gateway`)
  and nothing else; **dataplane** (optional, e.g. `ethernet1/3`) adds an `AITOOL-MGMT`
  interface-management-profile for ssh/https/ping, the `default` virtual router and a
  zone. *Tài khoản PAN-OS* keeps using `admin` (default; the device's SSH login becomes
  `admin` + the saved password) or creates the saved user (password first, then
  `permissions role-based superuser yes`). Every step is recorded in the job and the
  service journal with passwords masked; a failed essential step (the address) or commit
  reverts the candidate, a failed secondary step (hostname, gateway, the extra user) is
  reported and the commit still happens. A console answering `RFB` (VNC) stops at once
  with "Console đang là VNC, đổi sang TELNET trong PNETLab".
- **Discovering the real cabling.** *Khám phá (CDP/LLDP)* asks every IOS device for
  `show cdp neighbors detail` (LLDP when CDP says nothing), four at a time, read-only,
  and compares the answer with the diagram: green = matches, red dashed = designed
  but not reported by a device that answered, grey = the device did not answer.
  Cables the devices report that the diagram lacks can be added in one click.
- **Layer 2/3 design.** *Thiết kế L2/L3* holds VLANs, access ports, SVIs, static
  routes and single-area OSPF. The configuration plan (*Xem cấu hình sinh ra*) now
  covers, per IOS device: VLANs (on switches), access ports, link interfaces, SVIs,
  static routes and OSPF networks taken from the device's routed links and SVIs. ASA
  devices get routed interfaces (`nameif`, `security-level`, address — set per link
  end, defaults `to-<neighbour>` / 0) and static routes. A VLAN that a port or SVI
  uses cannot be deleted, and SVI subnets cannot overlap other links or SVIs.
- **Import, export and templates.** Export a project as JSON (no credentials) and
  import it as a new project, or *Nhân bản* it; export the diagram as SVG/PNG; import
  and export the device list as CSV (`hostname`, `management_ip` required; a CSV with
  any bad row imports nothing and names the lines).
- **Editing the canvas.** Drag on empty space to select several devices (or
  Shift/Ctrl+click, Ctrl+A), drag them together, align or distribute them, and
  undo/redo cabling, moves and additions with Ctrl+Z / Ctrl+Y. Deleting an existing
  device cannot be undone (its SSH login is gone).
- **Network designer.** The same tab is a canvas: drag devices, connect
  interfaces, and mark each link `physical`, `routed` (a point-to-point subnet,
  addresses derived or set by hand) or `trunk` (allowed VLANs). **Xem cấu hình
  sinh ra** shows the IOS commands per device. **Tạo bản xem trước** (ADMIN)
  freezes them as a change batch in that project, which is then approved and
  applied through the normal Preview → Approve → Apply → Verify flow — the
  design never touches a device by itself. Generated interfaces use
  `no shutdown`, which the safeguards treat as a negation, so these batches ask
  for the typed confirmation. ASA devices are listed as skipped (they need a
  `nameif`/security level the design does not carry).

- **Device kinds and lifecycle.** Besides managed Cisco devices (router, L3
  switch, switch, firewall) the palette has **unmanaged** PC, server, cloud, access
  point and shared segment: they are drawn, cabled and given a data IP/gateway, but
  the backend never SSHes to them, polls them or configures them. A managed device
  can be added as **planned** (no management IP or login yet); it is skipped by
  polling, chat targeting and configuration (its commands are listed as
  `planned_commands`) until **Triển khai** (`POST /api/devices/<id>/provision`).
  Only managed, provisioned devices are `deployable`.
- **Blueprints, addressing and checks.** *Mẫu* builds a whole topology in one call
  (devices, cables, positions, `/30`s). Routed links take `network: "auto"` and SVIs
  / subinterfaces `ip: "auto"` from the project's *addressing pool* (default
  `10.100.0.0/16`, set in project settings). *Kiểm tra* lists design problems and
  addressing use. Notes and group boxes document the diagram (never sent to a device).
- **Chat configuration files and AI Management.** Viewer/Operator advice can
  include a suggested diagram and one configuration snippet per Cisco device.
  *Đồng ý, tải file để nhập vào PNETLab* downloads a ZIP with one `.cfg` per
  device plus a review note. For live application, the project owner first
  enables *AI Management* in project settings. Each target must be an imported,
  provisioned Cisco PNETLab device marked as isolated, with saved SSH credentials.
  After each approval, the server captures the current running config; just
  before writing it takes another backup and refuses if the device changed.
  The chat's *Duyệt và áp dụng trên PNETLab* button is a separate,
  per-suggestion approval; commands then use the
  existing SSH backup, policy, verification and audit workflow. Dangerous
  commands ask for an additional exact phrase. The AI Server is the management
  host; it must be deployed inside PNETLab with network reachability to the
  devices. The PNETLab API integration imports lab inventory; device commands
  are sent over SSH.
- **Versions.** *Phiên bản* saves the diagram and design (no credentials), compares
  a version with the current state and restores it. Restore keeps devices that still
  exist (matched by hostname) so their SSH logins survive, recreates missing ones,
  replaces links, design and notes, and optionally prunes extra devices; the state
  before a restore is saved automatically. Other people's edits to the diagram show
  up within a few seconds (a `topology` fingerprint in `/state`).
- **Port-channels, loopbacks, router-on-a-stick.** Parallel trunk links sharing a
  `channel_id` generate `interface Port-channelN` + `channel-group N mode active`;
  loopbacks (`/32`) and dot1Q subinterfaces are design items and are advertised by
  OSPF.
- **Limits.** `MAX_PROJECTS_PER_USER` (20, admins exempt), `MAX_DEVICES_PER_PROJECT`
  (300), `MAX_LINKS_PER_PROJECT` (1000); 0 turns a limit off. Blueprints, versions,
  restores, imports and CSV imports are rate limited. A shared user can leave a
  project; owners get username suggestions when sharing.

Ordinary configuration changes continue through the app's existing approval
and role rules. The owner-gated AI Management flow is limited to one explicit
chat approval at a time and isolated PNETLab devices; it cannot
target physical or production devices. The AI Server described below can act as
the management host when deployed inside PNETLab with SSH reachability to the
lab devices.

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

### Static files and browser caches

Every `url_for('static', ...)` URL carries a hash of the file's content
(`/static/js/projects.js?v=1a2b3c4d5e6f`), and pages are sent with
`Cache-Control: no-cache`, so after a deploy the browser loads the new scripts
instead of a cached copy.

### Live updates and monitoring load

The chat page is told when something changed over a server-sent event stream
(`GET /api/projects/<id>/events`, with a conditional `GET .../state` as a
cheaper polling alternative) and refetches only the part that moved; if the
stream is unavailable it falls back to polling by itself. A stream holds one
server thread for up to `REALTIME_MAX_SECONDS` (55), so run Gunicorn with
threads, e.g. `gunicorn --worker-class gthread --threads 16 ...`; with plain
synchronous workers set `REALTIME_ENABLED=false`. `REALTIME_MAX_STREAMS` (20)
caps the streams per process.

Monitoring polls up to `MONITORING_MAX_WORKERS` (4) devices at once; the
database work stays on one thread. `GET /api/dashboard/monitoring` (ADMIN)
shows the last cycle's duration and warns when it exceeds
`MONITORING_INTERVAL_SECONDS`.

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
| GET | `/api/projects/<pid>/topology/templates` | viewer | Device types for the palette |
| POST | `/api/projects/<pid>/topology/import/pnetlab/preview` · `/import/pnetlab` | editor | Read / import a PNETLab lab (10 req/min) |
| POST | `/api/projects/<pid>/topology/bootstrap/pnetlab/preview` | editor | Console commands that would bootstrap SSH on PNETLab nodes (10 req/min) |
| POST | `/api/projects/<pid>/topology/bootstrap/pnetlab` | owner | Start a background job that configures management IP + SSH on the nodes through their console (`202` + job; 10 req/min) |
| GET | `/api/projects/<pid>/topology/bootstrap/jobs/<id>` | owner | Progress of that job: current device and phase, masked step log, results |
| POST | `/api/projects/<pid>/topology/discover` · `/discover/apply` | editor | CDP/LLDP discovery and drift; add found cables |
| GET | `/api/projects/<pid>/topology/design` | viewer | VLANs, access ports, SVIs, loopbacks, subinterfaces, routes, OSPF |
| POST / DELETE | `/api/projects/<pid>/topology/design/<kind>[/<id>]` | editor | `vlans`, `access-ports`, `svis` (`ip: "auto"`), `loopbacks`, `subinterfaces`, `routes`, `ospf` |
| GET / POST | `/api/projects/<pid>/topology/blueprints[/<key>]` | viewer / editor | Ready-made topologies (star, branch, campus, dmz, ring, hub_spoke); `planned: true` builds Cisco devices without IPs (20 req/min) |
| GET | `/api/projects/<pid>/topology/ipam` | viewer | Addressing pool, used ranges, next free /30 and LAN |
| GET | `/api/projects/<pid>/topology/lint` | viewer | Design checks (isolated devices, missing routing, duplicate IPs, single-member port-channels…) |
| GET/POST/PUT/DELETE | `/api/projects/<pid>/topology/notes[/<id>]` | viewer / editor | Notes and group boxes drawn on the canvas |
| GET/POST/DELETE | `/api/projects/<pid>/topology/versions[/<id>]` | viewer / editor | Saved versions of the diagram and design (max 30) |
| GET | `/api/projects/<pid>/topology/versions/<id>/diff` | viewer | What restoring a version would change |
| POST | `/api/projects/<pid>/topology/versions/<id>/restore` | editor | Restore (`prune` removes extra devices); the current state is saved first (10 req/min) |
| POST | `/api/devices/<id>/provision` | editor | Turn a planned Cisco device into a provisioned one (management IP, SSH login) |
| GET | `/api/projects/<pid>/share-candidates?q=` | owner | Usernames to share with (2+ characters) |
| DELETE | `/api/projects/<pid>/membership` | shared user | Leave a project you were shared into |
| GET | `/api/projects/<pid>/export` · `/devices.csv` | viewer | Project JSON / device CSV (no credentials) |
| POST | `/api/projects/import` · `/api/projects/<pid>/clone` | not VIEWER | New project from a JSON document / copy a project |
| POST | `/api/projects/<pid>/devices/import-csv` | editor | Add devices from CSV (all or nothing) |
| GET | `/api/projects/<pid>/state` · `/events` | viewer | Change fingerprints (ETag) / server-sent events |
| GET | `/api/dashboard/monitoring` | ADMIN | Last monitoring cycle |
| POST | `/api/projects/<pid>/topology/quick-device` | editor | Create a device from a template (free name and IP) |
| POST | `/api/projects/<pid>/topology/links` | editor | Add a link (ports optional: free ones are chosen) |
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
| POST | `/api/devices/<id>/baseline` | permitted Admin/Teacher | Capture a running-config baseline |
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
| POST | `/api/chat/messages/<id>/config-files` | project viewer | Download an explicitly approved ZIP of suggested configs |
| POST | `/api/chat/messages/<id>/apply-configurations` | Operator/Viewer, owner-enabled PNETLab | Per-suggestion approval and live apply to isolated devices |
| POST | `/api/chat/batches/<id>/apply` · `/cancel` | submitting Operator/Viewer | Confirm a high-risk AI Management apply / cancel it |
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

### Undo / redo of applied configuration

Every change that applies and verifies in configuration mode on a Cisco IOS
device becomes a named step in **Lịch sử cấu hình thiết bị**, under the
Pending column of the chat page (`config_checkpoints`). A step keeps the
running-config captured just before the change (`pre_change` backup) and the
one captured right after it (`post_change` backup); its default name is
`<hostname>: <description>` and can be renamed (✎).

* **Undo / Hoàn tác** reads the device's current running-config, computes the
  lines that bring it back to the step's pre-change config
  (`checkpoints/diff.py`: lines only in the current config are negated, lines
  only in the saved one are added, sections are compared line by line) and
  stores them as an ordinary pending change (`source: restore`). Nothing is
  written yet: the change goes through Approve → typed hostname → Apply →
  Verify like any other, with its own backup first. Only when it succeeds does
  the step become `undone`.
* **Redo / Làm lại** does the same towards the step's post-change config.
* Per device the steps are a stack: only the newest active step can be undone,
  only the most recently undone one redone, and a new change on the device
  discards its redo steps. While a device has a change pending or running, no
  undo/redo can be staged for it.
* An undo restores the *whole* saved config, so any other edit made on the
  device after that step is removed too (the preview says so). Secrets
  (`username`, `enable secret`, keys), banners and certificates are never
  emitted and are listed as left untouched. PAN-OS and ASA are not supported yet.

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/checkpoints` | project viewer | Steps of the current project, with `undo_next_id` / `redo_next_id` |
| PATCH | `/api/checkpoints/<id>` | project editor | Rename (`{"name": ...}`) |
| POST | `/api/checkpoints/<id>/undo` · `/redo` | may preview changes | Stage the restore as a pending change (10 req/min) |

`CONFIG_CHECKPOINTS_ENABLED=false` turns step recording off.

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
├── topology/         # diagram links, layout, design items, config generation, PNETLab import, discovery
├── integrations/     # read-only PNETLab client
├── realtime/         # change fingerprints and the event stream
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
