# Three-Device PNETLab AI Server Design

**Date:** 2026-08-27

**Status:** Awaiting written-spec review

## 1. Objective

Replace the Network Copilot's current nine-device PNETLab inventory with the
existing three-device lab and deploy the updated application to the Ubuntu AI
Server. The change must leave the PNETLab nodes and links untouched, reset the
application database cleanly, and prove that the AI Server can manage all three
devices over SSH.

## 2. Confirmed topology

The management network is `172.16.3.0/24`.

| Node | Management address | Device type | Application role |
|---|---:|---|---|
| AI Server | `172.16.3.28/24` | Ubuntu 22.04 | Network Copilot host |
| `R1` | `172.16.3.111/24` | Cisco IOL L3 / IOS | `core` |
| `SW1` | `172.16.3.121/24` | Cisco IOL L2 / IOS | `access` |
| `SW2` | `172.16.3.122/24` | Cisco IOL L2 / IOS | `access` |

The AI Server reaches each managed device directly on TCP port 22. The Ubuntu
host keeps its current local address, default gateway, public SSH forwarding,
and Gunicorn bind address. No Netplan or systemd bind-address change is part of
this work.

The Cisco devices share one SSH account. Its username and password are runtime
secrets: they remain only in the Ubuntu `.env` file and the application's
encrypted credential records. They must not be written to source, tests,
documentation, command-line arguments, or deployment logs.

## 3. Scope

### 3.1 In scope

- Replace the seeded live inventory with `R1`, `SW1`, and `SW2`.
- Change the application's default allowed management network to
  `172.16.3.0/24`.
- Update the live demo, smoke check, evaluation scenario, and AI evaluation
  corpus so their targets exist in the new inventory.
- Update runtime documentation that describes the management network,
  inventory, deployment, and verification flow.
- Update automated tests that are coupled to the default management network or
  the seeded/live topology.
- Deploy the tested source to `/home/cyber/project-csn301-group1` on the Ubuntu
  AI Server.
- Back up and reset the application database, run migrations, seed the new
  inventory and shared device credential, restart the service, and verify the
  live devices.

### 3.2 Out of scope

- Creating, deleting, rewiring, or reconfiguring nodes in PNETLab.
- Changing the AI Server address `172.16.3.28`, its Netplan configuration,
  gateway, public SSH forwarding, or the Gunicorn bind address.
- Adding VPCS nodes or modifying production LAN/VLAN addressing.
- Introducing a general JSON/YAML-driven multi-topology inventory system.
- Updating historical design specs, PowerPoint decks, Word/PDF reports, or
  parser fixtures that intentionally use self-contained sample addresses.
- Redesigning device roles, monitoring behavior, SSH handling, the AI provider,
  or the Preview/Approve/Apply/Verify safety workflow.

## 4. Source changes

### 4.1 Management-network defaults

Update both configuration entry points so they agree on
`172.16.3.0/24`:

- `backend/src/network_copilot/config.py`
- `backend/src/network_copilot/devices/schemas.py`

The Ubuntu `.env` file will also set `MANAGEMENT_NETWORK=172.16.3.0/24`
explicitly. The environment value remains authoritative at runtime; the source
default prevents local tools and tests from silently falling back to the old
network.

### 4.2 Seed inventory

Replace `LAB_DEVICES` in `backend/scripts/seed_lab.py` with exactly:

```python
[
    ("R1", "172.16.3.111", "cisco_ios", "core"),
    ("SW1", "172.16.3.121", "cisco_ios", "access"),
    ("SW2", "172.16.3.122", "cisco_ios", "access"),
]
```

Hostnames must match the IOS hostnames in PNETLab exactly because the AI and
backend resolve target devices by hostname.

The existing shared-credential seeding mechanism remains in use. It reads the
runtime variables for the lab SSH account and stores an encrypted credential
record for each of the three devices.

### 4.3 Live workflows

Update `backend/scripts/demo_check.py` so its router, primary switch, and
secondary switch targets are `R1`, `SW1`, and `SW2`. Operations must remain
appropriate to each role: routing/interface checks may target `R1`, while VLAN
and switchport checks target `SW1` or `SW2`.

Keep `backend/scripts/smoke_test_lab.py` inventory-driven. Update its stale
network description and add or preserve a clear failure when the seeded
inventory is not exactly the expected three devices. The smoke test must check
TCP/22, authenticate, and run the existing harmless read-only probe on each
device.

Update `backend/evaluation/pnetlab_scenario.example.json` and the live-target
references in `backend/evaluation/prompt_corpus.json`. Map router operations to
`R1` and switch operations to `SW1` or `SW2`. Do not weaken expected intent,
target, safety, or command semantics merely to make the renamed corpus pass.

### 4.4 Tests and documentation

Update tests that assert the default management network, seed inventory, live
demo targets, or live evaluation targets. Retain unrelated unit-test hostnames
and addresses when they are isolated fixtures for parsers, redaction, policy,
batch behavior, or error wording; those values are not a claim about the live
lab.

Update `backend/README.md` with the new management network, AI Server address,
three-device inventory, and current smoke/demo instructions. Do not rewrite
historical specs or presentation/report artifacts as part of this server-only
change.

## 5. Deployment and data lifecycle

The deployed repository is `/home/cyber/project-csn301-group1`; Gunicorn is
managed by `/etc/systemd/system/network-copilot.service` and continues to bind
to `172.16.3.28:5000`.

Deployment follows this order:

1. Run the complete automated suite locally against the changed source.
2. Record the local and remote source revisions.
3. On Ubuntu, create timestamped backups of `.env` and
   `backend/network_copilot.db` before changing either file.
4. Deploy the reviewed source while preserving the untracked/runtime `.env`.
5. Update only the required runtime variables: the management network and the
   shared lab SSH credential. Do not print secret values during the update.
6. Stop `network-copilot.service` before replacing the database.
7. Move the old database to its timestamped backup path rather than deleting
   it.
8. Create the new database with the existing migrations, then seed the admin,
   three devices, and encrypted credentials.
9. Start `network-copilot.service` and perform the live verification gates.

Resetting the database intentionally removes the old inventory, monitoring
snapshots, command history, backups, changes, chats, and audit records from the
active application. The timestamped database backup preserves a recovery path.

## 6. Verification gates

### 6.1 Automated verification

- The complete existing pytest suite passes after the topology-specific
  assertions are updated.
- New or adjusted tests prove that the default management network accepts
  `172.16.3.111`, `.121`, and `.122` and rejects addresses outside
  `172.16.3.0/24`.
- A seed test proves the inventory contains exactly `R1`, `SW1`, and `SW2` with
  the specified type and role values.
- Demo/evaluation tests prove that no live workflow still requires a removed
  hostname.
- A repository secret scan confirms that the supplied device password was not
  written to tracked files.

### 6.2 Ubuntu and live-lab verification

- `network-copilot.service` is active after restart.
- The health endpoint at `172.16.3.28:5000` responds successfully.
- The API inventory contains exactly three devices and no old topology
  hostname.
- From the AI Server, all three device addresses accept TCP/22.
- `backend/scripts/smoke_test_lab.py` authenticates to all three devices and
  runs its harmless read-only command successfully.
- A manual refresh produces a successful monitoring snapshot for each device.
- A read-only interface or route request resolves to `R1` and returns live IOS
  output.
- A read-only VLAN request resolves to `SW1` and `SW2` and returns live IOS
  output.
- One AI request for each device resolves only to the requested new hostname;
  removed hostnames are absent from the AI inventory context.

The deployment is not considered complete if any device cannot authenticate,
if the inventory count differs from three, or if a removed hostname remains in
a live workflow.

## 7. Failure handling and rollback

If source deployment, migration, seeding, service startup, or any required live
verification gate fails:

1. stop `network-copilot.service`;
2. restore the previous source revision;
3. restore the timestamped `.env` backup;
4. restore the timestamped database backup;
5. start the service; and
6. verify the previous health endpoint and service status.

The live service must not remain pointed at a partially seeded database. Device
connectivity failures are reported with the affected hostname and are not
masked by a successful health endpoint.

## 8. Acceptance criteria

The work is accepted when:

- the source of truth and Ubuntu runtime both use `172.16.3.0/24`;
- the active inventory is exactly `R1`, `SW1`, and `SW2` with the confirmed
  addresses and roles;
- Ubuntu remains reachable at its existing local and public addresses;
- the service remains bound to `172.16.3.28:5000`;
- all automated tests pass;
- live SSH and read-only monitoring succeed on all three devices;
- AI target resolution succeeds for all three new hostnames;
- no runtime secret is committed or printed; and
- the previous application state can be recovered from timestamped backups.
