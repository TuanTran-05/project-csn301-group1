# Three-Device PNETLab AI Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the live nine-device Network Copilot inventory with `R1`, `SW1`, and `SW2`, then deploy and verify the clean three-device database on the existing Ubuntu AI Server.

**Architecture:** Keep the existing inventory, credential, SSH, monitoring, and AI flows; change only their lab-specific inputs and live-workflow fixtures. Treat `172.16.3.0/24` as the management plane, seed three `cisco_ios` devices, keep Ubuntu and Gunicorn on `172.16.3.28`, and deploy through a recoverable database reset with timestamped backups.

**Tech Stack:** Python 3.11+, Flask, SQLAlchemy/Flask-Migrate, Pydantic, Paramiko, pytest, SQLite, systemd/Gunicorn, Cisco IOS on PNETLab.

**Spec:** `docs/superpowers/specs/2026-08-27-three-device-pnetlab-ai-server-design.md`

## Global Constraints

- The management network is exactly `172.16.3.0/24`.
- Ubuntu remains `172.16.3.28/24`; do not change Netplan, the default gateway, public SSH forwarding, or the Gunicorn bind `172.16.3.28:5000`.
- The active device inventory is exactly `R1` (`172.16.3.111`, `core`), `SW1` (`172.16.3.121`, `access`), and `SW2` (`172.16.3.122`, `access`), all with device type `cisco_ios` and SSH port 22.
- Do not create, delete, reconnect, or reconfigure PNETLab nodes or links.
- The shared device password is a runtime secret. Never place it in tracked files, command-line arguments, test fixtures, terminal output, or deployment logs.
- Preserve the existing credential encryption, authorization, Preview/Approve/Apply/Verify, backup, audit, and monitoring behavior.
- Back up `.env`, the current database, and the deployed source revision before resetting live state.
- Do not update historical specs, slide decks, Word/PDF reports, or isolated parser samples merely because they contain the old lab names or addresses.
- Run implementation commands from `backend/` unless a step explicitly names the repository root.

## File Structure

### Files to modify

- `backend/src/network_copilot/config.py` — application-level management-network default.
- `backend/src/network_copilot/devices/schemas.py` — schema fallback used with and without a Flask app context.
- `backend/.env.example` — non-secret runtime configuration example.
- `backend/tests/test_config.py` — default configuration regression test.
- `backend/tests/conftest.py` — shared in-management-network device fixtures.
- `backend/tests/devices/test_devices.py` — device API validation fixtures.
- `backend/tests/test_security.py` — API security cases that create devices.
- `backend/tests/audit/test_audit.py` — audited device-creation fixture.
- `backend/tests/ai/test_topology.py` — management-plane redaction fixtures and assertions.
- `backend/tests/ai/test_ai.py` — AI-context management-plane redaction assertions.
- `backend/scripts/seed_lab.py` — authoritative three-device seed manifest.
- `backend/scripts/demo_check.py` — live router/switch targets and exact-inventory gate.
- `backend/scripts/smoke_test_lab.py` — current management-network copy and exact-inventory gate.
- `backend/tests/e2e/test_demo_check.py` — live-demo fake inventory.
- `backend/evaluation/pnetlab_scenario.example.json` — current live scenario.
- `backend/evaluation/prompt_corpus.json` — current target labels.
- `backend/tests/evaluation/test_corpus.py` — corpus/scenario inventory contract.
- `backend/README.md` — current Ubuntu network, inventory, and verification instructions.

### Files to create

- `backend/tests/e2e/test_seed_lab.py` — exact seed-manifest and idempotent database tests.
- `backend/tests/e2e/test_smoke_test_lab.py` — pure exact-inventory validation tests without sockets.

---

### Task 1: Move the management-plane contract to `172.16.3.0/24`

**Files:**

- Modify: `backend/src/network_copilot/config.py:54-55`
- Modify: `backend/src/network_copilot/devices/schemas.py:15-22`
- Modify: `backend/.env.example:14`
- Modify: `backend/tests/test_config.py:16-27,59-77`
- Modify: `backend/tests/conftest.py:112-130`
- Modify: `backend/tests/devices/test_devices.py:3-18,102-167`
- Modify: `backend/tests/test_security.py:107-144`
- Modify: `backend/tests/audit/test_audit.py:129-138`
- Modify: `backend/tests/ai/test_topology.py`
- Modify: `backend/tests/ai/test_ai.py` management-plane fixtures/assertions only

**Interfaces:**

- Consumes: Flask configuration key `MANAGEMENT_NETWORK: str`.
- Produces: `Config.MANAGEMENT_NETWORK == "172.16.3.0/24"` by default and `management_network() -> ipaddress.IPv4Network` with the same fallback.

- [ ] **Step 1: Add a failing default-configuration test**

Append this test to `backend/tests/test_config.py`:

```python
def test_default_management_network_matches_current_lab(monkeypatch):
    monkeypatch.delenv("MANAGEMENT_NETWORK", raising=False)
    module = importlib.import_module("network_copilot.config")
    config = importlib.reload(module).Config
    assert config.MANAGEMENT_NETWORK == "172.16.3.0/24"
```

- [ ] **Step 2: Move API-validation fixtures into the new management network**

Use these exact fixture mappings wherever the value represents a management
address rather than an isolated parser sample:

```python
MANAGEMENT_FIXTURE_IPS = {
    "CORE-SW1": "172.16.3.111",
    "DIST-SW1": "172.16.3.121",
    "DIST-SW2": "172.16.3.122",
    "ACC-SW1": "172.16.3.131",
    "FW-01": "172.16.3.103",
    "CORE-SW9": "172.16.3.119",
}
```

In `backend/tests/devices/test_devices.py`, make the valid/duplicate/inside
values `172.16.3.111`, `172.16.3.112`, and `172.16.3.254`. Use this invalid set:

```python
[
    "172.16.4.20",
    "192.168.1.1",
    "172.16.2.1",
    "not-an-ip",
    "172.16.3.0/24",
]
```

Update management-plane redaction fixtures in `test_topology.py` and
`test_ai.py` consistently:

```python
MANAGEMENT_NETWORK = "172.16.3.0/24"
ROUTER_MANAGEMENT_IP = "172.16.3.111"
DIST_SW1_MANAGEMENT_IP = "172.16.3.121"
DIST_SW2_MANAGEMENT_IP = "172.16.3.122"
ACCESS_SW_MANAGEMENT_IP = "172.16.3.131"
```

Keep unrelated production networks, transit networks, parser input, and error
wording unchanged.

- [ ] **Step 3: Run the focused tests and confirm they fail against the old defaults**

Run from `backend/`:

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/devices/test_devices.py tests/test_security.py tests/audit/test_audit.py tests/ai/test_topology.py tests/ai/test_ai.py -v
```

Expected: the new default assertion and new in-subnet API payloads fail while
the implementation still defaults to `10.10.10.0/24`.

- [ ] **Step 4: Change both source defaults and the environment example**

Apply these exact values:

```python
# backend/src/network_copilot/config.py
MANAGEMENT_NETWORK = os.environ.get("MANAGEMENT_NETWORK", "172.16.3.0/24")
```

```python
# backend/src/network_copilot/devices/schemas.py
def management_network() -> ipaddress.IPv4Network:
    """Read the management network from app config, falling back to the lab default."""
    from flask import current_app, has_app_context

    default = "172.16.3.0/24"
    if has_app_context():
        default = current_app.config.get("MANAGEMENT_NETWORK", default)
    return ipaddress.ip_network(default, strict=False)
```

```dotenv
# backend/.env.example
MANAGEMENT_NETWORK=172.16.3.0/24
```

- [ ] **Step 5: Run the focused tests and confirm they pass**

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/devices/test_devices.py tests/test_security.py tests/audit/test_audit.py tests/ai/test_topology.py tests/ai/test_ai.py -v
```

Expected: PASS with no real socket or AI-provider access.

- [ ] **Step 6: Commit the management-network change**

```powershell
git add backend/src/network_copilot/config.py backend/src/network_copilot/devices/schemas.py backend/.env.example backend/tests/test_config.py backend/tests/conftest.py backend/tests/devices/test_devices.py backend/tests/test_security.py backend/tests/audit/test_audit.py backend/tests/ai/test_topology.py backend/tests/ai/test_ai.py
git commit -m "feat: move management plane to 172.16.3.0"
```

### Task 2: Seed exactly `R1`, `SW1`, and `SW2`

**Files:**

- Create: `backend/tests/e2e/test_seed_lab.py`
- Modify: `backend/scripts/seed_lab.py:34-47`

**Interfaces:**

- Consumes: existing `seed_devices() -> tuple[int, int]` and `Device` model.
- Produces: `LAB_DEVICES: list[tuple[str, str, str, str]]` containing exactly three rows; repeated `seed_devices()` calls remain idempotent.

- [ ] **Step 1: Write failing seed-manifest tests**

Create `backend/tests/e2e/test_seed_lab.py`:

```python
from scripts import seed_lab
from network_copilot.devices.model import Device


EXPECTED_DEVICES = [
    ("R1", "172.16.3.111", "cisco_ios", "core"),
    ("SW1", "172.16.3.121", "cisco_ios", "access"),
    ("SW2", "172.16.3.122", "cisco_ios", "access"),
]


def rows():
    devices = Device.query.order_by(Device.hostname).all()
    return [
        (item.hostname, item.management_ip, item.device_type, item.role)
        for item in devices
    ]


def test_lab_device_manifest_matches_current_pnetlab_topology():
    assert seed_lab.LAB_DEVICES == EXPECTED_DEVICES


def test_seed_devices_creates_exact_inventory(app):
    created, updated = seed_lab.seed_devices()
    assert (created, updated) == (3, 0)
    assert rows() == EXPECTED_DEVICES


def test_seed_devices_is_idempotent(app):
    seed_lab.seed_devices()
    created, updated = seed_lab.seed_devices()
    assert (created, updated) == (0, 3)
    assert rows() == EXPECTED_DEVICES
```

- [ ] **Step 2: Run the new tests and verify they fail on the nine-device manifest**

```powershell
..\.venv\Scripts\python.exe -m pytest tests/e2e/test_seed_lab.py -v
```

Expected: FAIL because `LAB_DEVICES` still contains the old nine devices.

- [ ] **Step 3: Replace the seed manifest**

Replace only the topology comment and `LAB_DEVICES` value in
`backend/scripts/seed_lab.py`:

```python
# Names must match the device hostnames in PNETLab exactly: the AI copilot
# resolves a device by hostname, so a mismatch is a hard failure.
LAB_DEVICES = [
    ("R1", "172.16.3.111", "cisco_ios", "core"),
    ("SW1", "172.16.3.121", "cisco_ios", "access"),
    ("SW2", "172.16.3.122", "cisco_ios", "access"),
]
```

Do not add deletion logic to `seed_devices()`: the approved deployment resets
the live database first, and broad deletion during a normal idempotent seed
would endanger unrelated data.

- [ ] **Step 4: Run seed and credential regression tests**

```powershell
..\.venv\Scripts\python.exe -m pytest tests/e2e/test_seed_lab.py tests/credentials/test_credentials.py -v
```

Expected: PASS; the seed tests report three devices and the existing encrypted
credential tests remain green.

- [ ] **Step 5: Commit the seed manifest**

```powershell
git add backend/scripts/seed_lab.py backend/tests/e2e/test_seed_lab.py
git commit -m "feat: seed the three-device PNETLab inventory"
```

### Task 3: Make smoke and demo workflows enforce the current inventory

**Files:**

- Modify: `backend/scripts/demo_check.py:1-30,116-160`
- Modify: `backend/scripts/smoke_test_lab.py:1-11,33-34,85-95`
- Modify: `backend/tests/e2e/test_demo_check.py:24-28`
- Create: `backend/tests/e2e/test_smoke_test_lab.py`

**Interfaces:**

- Consumes: API device dictionaries with `id` and `hostname`; database `Device` rows.
- Produces: `EXPECTED_HOSTNAMES = ("R1", "SW1", "SW2")` in both live scripts and `inventory_error(devices: list[Device]) -> str | None` in the smoke script.

- [ ] **Step 1: Retarget the demo fixture and add an inventory-contract assertion**

Change `DEVICES` in `backend/tests/e2e/test_demo_check.py` and add this test:

```python
DEVICES = [
    {"id": 1, "hostname": "R1"},
    {"id": 2, "hostname": "SW1"},
    {"id": 3, "hostname": "SW2"},
]


def test_demo_targets_exact_current_inventory():
    assert demo_check.EXPECTED_HOSTNAMES == ("R1", "SW1", "SW2")
    assert [device["hostname"] for device in DEVICES] == list(
        demo_check.EXPECTED_HOSTNAMES
    )
```

- [ ] **Step 2: Write pure smoke-inventory tests**

Create `backend/tests/e2e/test_smoke_test_lab.py`:

```python
from types import SimpleNamespace

from scripts import smoke_test_lab


def devices(*hostnames):
    return [SimpleNamespace(hostname=hostname) for hostname in hostnames]


def test_inventory_error_accepts_exact_current_inventory():
    assert smoke_test_lab.inventory_error(devices("R1", "SW1", "SW2")) is None


def test_inventory_error_rejects_missing_device():
    error = smoke_test_lab.inventory_error(devices("R1", "SW1"))
    assert error == "inventory mismatch: missing=['SW2'], unexpected=[]"


def test_inventory_error_rejects_retired_device():
    error = smoke_test_lab.inventory_error(
        devices("R1", "SW1", "SW2", "INTERNAL-RTR")
    )
    assert error == "inventory mismatch: missing=[], unexpected=['INTERNAL-RTR']"
```

- [ ] **Step 3: Run the focused tests and verify the new contract fails**

```powershell
..\.venv\Scripts\python.exe -m pytest tests/e2e/test_demo_check.py tests/e2e/test_smoke_test_lab.py -v
```

Expected: FAIL because neither script defines the new exact inventory yet.

- [ ] **Step 4: Retarget the demo without asking an access switch for OSPF**

Use these constants in `backend/scripts/demo_check.py`:

```python
ROUTER_HOSTNAME = "R1"
SWITCH_HOSTNAMES = ("SW1", "SW2")
EXPECTED_HOSTNAMES = (ROUTER_HOSTNAME, *SWITCH_HOSTNAMES)
WRITE_ALL_REQUEST = "thuc hien lenh write tren toan bo thiet bi"
```

Require exact inventory equality after `/api/devices`, use `R1` for
`show ip interface brief` and refresh, and replace the OSPF monitor prompt with:

```python
{"message": f"Kiem tra VLAN cua {SWITCH_HOSTNAMES[0]}"}
```

Use the exact-inventory gate before any read, AI, approval, or Apply call:

```python
expected = set(EXPECTED_HOSTNAMES)
actual = set(devices)
inventory_ok = status == 200 and actual == expected
step.check("list exact current inventory", inventory_ok, f"({len(devices)} devices)")
if not inventory_ok:
    print(
        f"\nERROR: inventory mismatch: missing={sorted(expected - actual)}, "
        f"unexpected={sorted(actual - expected)}"
    )
    return 1
```

- [ ] **Step 5: Add the smoke inventory validator and current network copy**

In `backend/scripts/smoke_test_lab.py`, change the docstring network to
`172.16.3.0/24`, define the expected hostnames, and validate before sockets:

```python
EXPECTED_HOSTNAMES = ("R1", "SW1", "SW2")
TCP_TIMEOUT = 5
PROBE_COMMAND = "show clock"


def inventory_error(devices: list[Device]) -> str | None:
    expected = set(EXPECTED_HOSTNAMES)
    actual = {device.hostname for device in devices}
    if actual == expected and len(devices) == len(expected):
        return None
    return (
        f"inventory mismatch: missing={sorted(expected - actual)}, "
        f"unexpected={sorted(actual - expected)}"
    )
```

In `main()`, call `inventory_error(devices)` immediately after the query. Print
the returned message to stderr and return 1 before `tcp_open()` when it is not
`None`.

- [ ] **Step 6: Run live-script tests**

```powershell
..\.venv\Scripts\python.exe -m pytest tests/e2e/test_demo_check.py tests/e2e/test_smoke_test_lab.py -v
```

Expected: PASS; all tests remain socket-free.

- [ ] **Step 7: Commit the live-workflow update**

```powershell
git add backend/scripts/demo_check.py backend/scripts/smoke_test_lab.py backend/tests/e2e/test_demo_check.py backend/tests/e2e/test_smoke_test_lab.py
git commit -m "feat: gate live checks on the three-device inventory"
```

### Task 4: Retarget the evaluation data to the current devices

**Files:**

- Modify: `backend/evaluation/pnetlab_scenario.example.json:1`
- Modify: `backend/evaluation/prompt_corpus.json`
- Modify: `backend/tests/evaluation/test_corpus.py:1-10`

**Interfaces:**

- Consumes: `load_corpus(path: Path) -> list[CorpusCase]`.
- Produces: scenario device order `R1`, `SW1`, `SW2`; every non-empty corpus target belongs to that set; all three devices receive coverage.

- [ ] **Step 1: Add failing corpus and scenario contracts**

Replace `backend/tests/evaluation/test_corpus.py` with the formatted existing
distribution test plus these tests:

```python
import json
from collections import Counter
from pathlib import Path

from network_copilot.evaluation.schemas import load_corpus


CORPUS_PATH = Path("evaluation/prompt_corpus.json")
SCENARIO_PATH = Path("evaluation/pnetlab_scenario.example.json")
LIVE_INVENTORY = {"R1", "SW1", "SW2"}
RETIRED_INVENTORY = {
    "ISP-RTR",
    "FW-01",
    "INTERNAL-RTR",
    "DIST-SW1",
    "DIST-SW2",
    "ACC-SW1",
    "ACC-SW2",
    "ACC-SW3",
    "DMZ-SW",
}


def test_approved_corpus_distribution():
    cases = load_corpus(CORPUS_PATH)
    assert len(cases) == 50
    assert Counter(case.category for case in cases) == {
        "chat": 5,
        "monitor": 6,
        "troubleshoot": 6,
        "switching_interface": 10,
        "ipv4_static_route": 8,
        "acl_dhcp_ospf": 7,
        "dangerous_unauthorized": 5,
        "ambiguous_invalid": 3,
    }
    assert sum(case.language == "vi" for case in cases) >= 25
    assert len({case.id for case in cases}) == 50


def test_corpus_targets_only_current_inventory_and_covers_every_device():
    cases = load_corpus(CORPUS_PATH)
    targets = {
        target
        for case in cases
        for target in case.expected_targets
    }
    assert targets == LIVE_INVENTORY
    assert targets.isdisjoint(RETIRED_INVENTORY)


def test_pnetlab_scenario_matches_current_inventory():
    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    assert scenario["devices"] == ["R1", "SW1", "SW2"]
```

- [ ] **Step 2: Run the evaluation contracts and verify they fail**

```powershell
..\.venv\Scripts\python.exe -m pytest tests/evaluation/test_corpus.py -v
```

Expected: FAIL because the scenario and corpus still reference retired devices.

- [ ] **Step 3: Apply the exact corpus target mapping**

Keep all 50 case IDs, languages, categories, outcomes, approval flags, and
capability labels unchanged. Change only `expected_targets` using this complete
mapping:

| Case IDs | Target |
|---|---|
| `monitor-vi-01`, `monitor-vi-04`, `troubleshoot-vi-01`, `troubleshoot-en-04` | `R1` |
| `monitor-vi-02`, `monitor-en-05`, `troubleshoot-vi-02`, `troubleshoot-vi-05` | `SW1` |
| `monitor-vi-03`, `monitor-vi-06`, `troubleshoot-vi-03`, `troubleshoot-vi-06` | `SW2` |
| `switch-vi-01`, `switch-vi-03`, `switch-vi-05`, `switch-vi-07`, `switch-en-09` | `SW1` |
| `switch-vi-02`, `switch-vi-04`, `switch-en-06`, `switch-vi-08`, `switch-en-10` | `SW2` |
| all `route-*` cases | `R1` |
| all `extension-*` cases | `R1` |
| `danger-vi-01`, `danger-vi-04` | `R1` |
| `danger-en-02`, `danger-en-05` | `SW1` |
| `danger-vi-03` | `SW2` |
| all `chat-*` and `invalid-*` cases | no targets (`[]`) |

The JSON shape remains:

```json
"expected_targets": [
  "R1"
]
```

or the corresponding switch hostname.

- [ ] **Step 4: Replace the scenario inventory**

Set `backend/evaluation/pnetlab_scenario.example.json` to:

```json
{
  "name": "course-core",
  "devices": ["R1", "SW1", "SW2"],
  "extension_mode": "none"
}
```

- [ ] **Step 5: Run corpus, runner, and course-evidence tests**

```powershell
..\.venv\Scripts\python.exe -m pytest tests/evaluation/test_corpus.py tests/evaluation/test_runner.py tests/e2e/test_course_evidence.py -v
```

Expected: PASS with 50 cases and zero unsafe SSH calls.

- [ ] **Step 6: Commit the evaluation-data update**

```powershell
git add backend/evaluation/prompt_corpus.json backend/evaluation/pnetlab_scenario.example.json backend/tests/evaluation/test_corpus.py
git commit -m "test: retarget evaluation data to the current lab"
```

### Task 5: Document the current Ubuntu deployment and inventory

**Files:**

- Modify: `backend/README.md:29-32,131-177,222-229,291-296,344-364`

**Interfaces:**

- Consumes: the approved addresses, roles, service bind, and live verification commands.
- Produces: operator documentation that matches the deployed three-device lab.

- [ ] **Step 1: Update the runtime network and deployment description**

Document the actual single-interface Ubuntu layout:

```markdown
| Interface | Network | Address | Gateway |
|---|---|---|---|
| `enp6s18` | management | `172.16.3.28/24` | `172.16.3.1` |
```

State explicitly that the installed systemd service binds Gunicorn to
`172.16.3.28:5000` and that this migration does not change Netplan.

- [ ] **Step 2: Replace the live inventory section**

Use this exact table:

```markdown
| Hostname | Management IP | Device type | Role |
|---|---:|---|---|
| `R1` | `172.16.3.111` | `cisco_ios` | `core` |
| `SW1` | `172.16.3.121` | `cisco_ios` | `access` |
| `SW2` | `172.16.3.122` | `cisco_ios` | `access` |
```

Explain that `R1` receives routing checks while VLAN/switchport checks target
`SW1` and `SW2`. Replace the old `ACC-SW1` error-contract example with `SW1`.

- [ ] **Step 3: Update live verification copy**

Keep the command:

```bash
.venv/bin/python scripts/smoke_test_lab.py
```

Describe its new gate: the database must contain exactly the three documented
hostnames before any TCP or SSH probe runs.

- [ ] **Step 4: Check runtime-facing files for retired topology references**

Run from the repository root:

```powershell
rg --line-number "10\.10\.10|ISP-RTR|FW-01|INTERNAL-RTR|DIST-SW1|DIST-SW2|ACC-SW1|ACC-SW2|ACC-SW3|DMZ-SW" backend/README.md backend/.env.example backend/scripts/seed_lab.py backend/scripts/demo_check.py backend/scripts/smoke_test_lab.py backend/evaluation/pnetlab_scenario.example.json backend/evaluation/prompt_corpus.json
```

Expected: exit code 1 with no matches. Historical specs, parser samples, and
isolated unit fixtures are intentionally outside this check.

- [ ] **Step 5: Commit the runtime documentation**

```powershell
git add backend/README.md
git commit -m "docs: describe the three-device Ubuntu lab"
```

### Task 6: Run the complete local release gate

**Files:**

- Verify only; modify a file only if a failing test proves it is still coupled to the retired live topology.

**Interfaces:**

- Consumes: Tasks 1-5.
- Produces: a tested commit set safe to push and deploy.

- [ ] **Step 1: Check formatting and the staged/tracked secret boundary**

Run from the repository root:

```powershell
git diff --check HEAD~5..HEAD
git ls-files --error-unmatch backend/.env
```

Expected: `git diff --check` exits 0. `git ls-files --error-unmatch
backend/.env` exits non-zero, proving the runtime secret file is not tracked.

- [ ] **Step 2: Run the entire automated suite**

Run from `backend/`:

```powershell
..\.venv\Scripts\python.exe -m pytest -v
```

Expected: every test passes; no test opens a real socket or calls a real model.

- [ ] **Step 3: Verify the final tracked topology manifest**

Run from the repository root:

```powershell
git grep -n -E "R1|SW1|SW2|172\.16\.3\.0/24|172\.16\.3\.(111|121|122)" -- backend/src/network_copilot/config.py backend/src/network_copilot/devices/schemas.py backend/scripts/seed_lab.py backend/scripts/demo_check.py backend/scripts/smoke_test_lab.py backend/evaluation/pnetlab_scenario.example.json backend/README.md
git status --short
```

Expected: the first command shows the approved topology in every runtime-facing
surface. The second command shows no staged or tracked implementation changes;
pre-existing unrelated untracked files may remain.

- [ ] **Step 4: Record the deploy candidate**

```powershell
git log -6 --oneline
git rev-parse HEAD
```

Expected: five implementation commits follow the approved design commit, and
the printed HEAD is the exact revision to deploy.

### Task 7: Deploy a recoverable clean database to Ubuntu

**Files:**

- Remote modify: `/home/cyber/project-csn301-group1/backend/.env`
- Remote replace with backup: `/home/cyber/project-csn301-group1/backend/network_copilot.db`
- Remote preserve: `/etc/systemd/system/network-copilot.service`
- Remote create: `/home/cyber/project-csn301-group1/backend/artifacts/topology-migration-YYYYMMDDTHHMMSSZ/`, where the suffix is generated in UTC by the backup command below.

**Interfaces:**

- Consumes: tested Git HEAD from Task 6, remote systemd service `network-copilot.service`, existing `.env` encryption/admin secrets, and the interactively entered shared lab SSH password.
- Produces: a running Ubuntu service with exactly three seeded devices and encrypted credentials, plus a timestamped rollback bundle.

- [ ] **Step 1: Push only the reviewed commit history**

From the repository root, inspect the outgoing commits and fast-forward main:

```powershell
git log --oneline origin/main..HEAD
git push origin HEAD:main
```

Expected: only the design, plan, and five reviewed implementation commits are
pushed; unrelated untracked files are not transferred.

- [ ] **Step 2: Open an interactive SSH session and verify the untouched host network**

```powershell
ssh.exe -o StrictHostKeyChecking=accept-new -p 328 cyber@180.148.6.94
```

After entering the Ubuntu password interactively, run:

```bash
hostname
ip -brief -4 address show enp6s18
ip route
systemctl is-active network-copilot.service
```

Expected: `Cyber-U22-AI-Net`, `172.16.3.28/24`, default gateway
`172.16.3.1`, and `active`.

- [ ] **Step 3: Create the rollback bundle before pulling or resetting data**

Run on Ubuntu:

```bash
cd /home/cyber/project-csn301-group1
migration_dir="backend/artifacts/topology-migration-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 -p "$migration_dir"
git rev-parse HEAD > "$migration_dir/source-revision"
cp -p backend/.env "$migration_dir/.env.backup"
cp -p backend/network_copilot.db "$migration_dir/network_copilot.db.backup"
printf '%s\n' "$migration_dir"
```

Expected: one absolute/relative backup directory is printed. Keep the same SSH
session open so `migration_dir` remains available for rollback.

- [ ] **Step 4: Pull the tested source without touching runtime secrets**

```bash
git status --short
git pull --ff-only origin main
git rev-parse HEAD
```

Expected: the pre-pull status is clean, pull fast-forwards, and remote HEAD
matches the Task 6 deploy candidate. `.env` remains untracked and present.

- [ ] **Step 5: Update `.env` without placing the password in shell history or process arguments**

From `backend/`, read the shared device password silently, export it only for
the current session, and use the installed Python interpreter to update the
three keys:

```bash
cd /home/cyber/project-csn301-group1/backend
read -rsp 'Shared lab SSH password: ' LAB_SSH_PASSWORD
printf '\n'
export LAB_SSH_PASSWORD
.venv/bin/python -c '
import os
from pathlib import Path

path = Path(".env")
updates = {
    "MANAGEMENT_NETWORK": "172.16.3.0/24",
    "LAB_SSH_USERNAME": "g1",
    "LAB_SSH_PASSWORD": os.environ["LAB_SSH_PASSWORD"],
}
seen = set()
output = []
for line in path.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in updates:
        output.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        output.append(line)
for key, value in updates.items():
    if key not in seen:
        output.append(f"{key}={value}")
path.write_text("\n".join(output) + "\n", encoding="utf-8")
'
unset LAB_SSH_PASSWORD
chmod 600 .env
grep '^MANAGEMENT_NETWORK=' .env
grep '^LAB_SSH_USERNAME=' .env
stat -c '%a %n' .env
```

Expected: the two non-secret values print correctly and `.env` mode is `600`.
Do not print or grep `LAB_SSH_PASSWORD`.

- [ ] **Step 6: Stop the service and replace the active database recoverably**

```bash
cd /home/cyber/project-csn301-group1
sudo systemctl stop network-copilot.service
systemctl is-active network-copilot.service
mv backend/network_copilot.db "$migration_dir/network_copilot.db.pre-reset"
cd backend
.venv/bin/flask db upgrade
.venv/bin/python scripts/seed_lab.py
```

Expected: systemd reports `inactive`; migrations succeed; seeding reports one
admin, three created devices, and credentials stored for three devices. The
secret value is never printed.

- [ ] **Step 7: Start the service and verify health plus exact database inventory**

```bash
sudo systemctl start network-copilot.service
systemctl is-active network-copilot.service
curl --fail --silent --show-error http://172.16.3.28:5000/api/health
.venv/bin/python -c '
from network_copilot.app import create_app
from network_copilot.devices.model import Device

app = create_app()
with app.app_context():
    rows = [
        (item.hostname, item.management_ip, item.device_type, item.role)
        for item in Device.query.order_by(Device.hostname).all()
    ]
expected = [
    ("R1", "172.16.3.111", "cisco_ios", "core"),
    ("SW1", "172.16.3.121", "cisco_ios", "access"),
    ("SW2", "172.16.3.122", "cisco_ios", "access"),
]
assert rows == expected, rows
print("inventory verified: R1, SW1, SW2")
'
```

Expected: `active`, healthy JSON, and `inventory verified: R1, SW1, SW2`.

- [ ] **Step 8: Run the live read-only SSH gate**

```bash
.venv/bin/python scripts/smoke_test_lab.py
```

Expected: TCP/22, authentication, and `show clock` pass for all three devices;
the script exits 0. Stop deployment verification if any device fails.

- [ ] **Step 9: Verify service logs do not contain secrets or startup errors**

```bash
sudo journalctl -u network-copilot.service --since '10 minutes ago' --no-pager
```

Expected: no traceback, database error, SSH credential value, or bind failure.
The log may contain hostnames and non-secret management addresses.

- [ ] **Step 10: Verify read-only target resolution through the dashboard**

From a second Windows terminal, create a local tunnel without changing the
remote bind:

```powershell
ssh.exe -o StrictHostKeyChecking=accept-new -L 5500:172.16.3.28:5000 -p 328 cyber@180.148.6.94
```

Keep that SSH session open, browse to `http://127.0.0.1:5500`, sign in with the
existing seeded admin account, and issue these exact requests one at a time:

```text
Kiem tra interface cua R1
Kiem tra VLAN cua SW1
Kiem tra VLAN cua SW2
```

Expected: each response targets only the named device, produces live read-only
IOS output, and creates no configuration Preview or Apply action. Confirm that
the inventory/dashboard contains no retired hostname.

- [ ] **Step 11: Roll back immediately if any required deployment gate fails**

In the same Ubuntu SSH session, use the recorded `migration_dir`:

```bash
cd /home/cyber/project-csn301-group1
sudo systemctl stop network-copilot.service
cp -p "$migration_dir/.env.backup" backend/.env
cp -p "$migration_dir/network_copilot.db.backup" backend/network_copilot.db
previous_revision="$(cat "$migration_dir/source-revision")"
git switch --detach "$previous_revision"
sudo systemctl start network-copilot.service
systemctl is-active network-copilot.service
curl --fail --silent --show-error http://172.16.3.28:5000/api/health
```

Expected: the previous service revision and database return to `active` and
healthy. Do not use `git reset --hard`; the detached revision is an explicit,
recoverable emergency rollback state.

### Task 8: Capture final evidence and handoff

**Files:**

- Verify only; do not commit runtime secrets, database files, or journal output.

**Interfaces:**

- Consumes: successful Task 7 service, inventory, smoke test, and dashboard checks.
- Produces: a concise deployment record containing non-secret revisions and gate results.

- [ ] **Step 1: Record non-secret deployment facts**

On Ubuntu:

```bash
cd /home/cyber/project-csn301-group1/backend
git rev-parse HEAD
systemctl is-active network-copilot.service
ip -brief -4 address show enp6s18
.venv/bin/python scripts/smoke_test_lab.py
```

Expected: deployed commit, `active`, `172.16.3.28/24`, and three passing device
checks.

- [ ] **Step 2: Confirm no runtime artifacts entered Git**

```bash
git status --short
git check-ignore backend/.env backend/network_copilot.db backend/artifacts
```

Expected: no tracked modification; all three runtime paths are ignored.

- [ ] **Step 3: Report completion**

Report the deployed commit, backup directory, service health, exact inventory,
three smoke-test results, three read-only AI target-resolution results, and any
remaining operational limitation. Never include passwords, `.env` contents,
JWTs, API keys, encrypted credential blobs, or full device configurations.
