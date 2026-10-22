# Lab Bootstrap Safe Demo: offline acceptance

This note describes the behavior covered by the Lab Bootstrap offline tests. It
does not certify compatibility with every PNETLab release, an importable
topology, or a configuration that is ready to apply to a device.

## Current data flow

1. The upload route accepts a bounded ZIP in memory and calls
   `analyze_pnetlab_zip`; archive members are never extracted to a shared
   directory or executed.
2. The parser validates a narrow `.unl` XML layout and returns nodes,
   interfaces, explicit links, network objects, interface-to-network
   attachments, warnings, and source checksums.
3. `inspect_archive_analysis` preserves direct node-to-node links separately
   from node-to-network attachments. A shared `network_id` is a segment
   attachment, not evidence of a direct cable.
4. The current ZIP parser always reports `ANALYZED_UNVERIFIED_SCHEMA` and
   `preview_compatible=false`. The Safe Demo reports `planner_status=NOT_RUN`
   and never calls `build_plan` for an uploaded archive.
5. `POST /api/lab-bootstrap-demo/export-inspection` re-parses the uploaded ZIP
   and emits a separate review-only summary only when inspection has no issues.
   The ZIP contains `README.txt`, `manifest.json`, and `checksums.txt`; the
   manifest includes source hashes and record/message counts, not the source
   archive, XML, topology labels, or addresses. It always states
   `import_compatible=false`, `planner_status=NOT_RUN`,
   `gateway_status=NOT_SUPPORTED`, and `applied=false`/`verified=false`.
   Parser errors, inspection issues, missing JWT/CSRF, non-local Origin, and
   oversized bodies are rejected; export does not invoke the planner.
6. `build_plan` is an offline planner for normalized, explicitly supplied
   topology data. It requires an explicitly selected discovered network as the
   management segment. It does not prove that segment is reachable from an
   agent or device.
7. `safe_export.build_safe_export_package` creates a different review-only
   IP-plan archive for a valid `PLANNED` result. It contains a README,
   manifest, CSV plan, and checksums; it does not contain an importable `.unl`,
   startup configuration, or device commands. The Safe Demo inspection route
   does not call this planner-dependent exporter. Exported status does not
   mean applied or verified.

## Compatibility and planner gates

The inline synthetic shared-bridge XML exercises attachment inspection and
planner gating only; it is not evidence that a PNETLab schema is verified.
`qemu_version` is retained as source metadata and is never substituted for
`image_build`. Missing image-build evidence, omitted XML metadata, missing
relationship evidence, and an unverified schema remain warnings or blockers.
Do not change the archive status or planner gate without a reviewed schema
contract and representative sanitized exports.

The offline planner validates IPv4 CIDRs, overlapping pools, reserved
addresses/subnets, endpoint/interface relationships, and available host
capacity. Gateway assignment is intentionally unsupported and is surfaced as
`NOT_SUPPORTED`; no gateway is inferred. A planned address is a review result,
not device configuration.

## Offline verification

Run from the `backend` directory with the project Python environment:

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
python -m pytest tests/lab_bootstrap -q
node --check src/network_copilot/static/js/lab_bootstrap_demo.js
node extension_tests/lab_bootstrap_safe_demo_ui_test.cjs
```

The pytest command should complete with no failures or skips; report the actual
collected/pass count from the test run rather than relying on a fixed historical
count. `node --check` should exit zero with no output, and the UI harness should
print `Safe Demo UI assertions passed.`

The pytest suite uses inline synthetic XML and in-memory ZIP fixtures; it does
not require an external PNETLab `.unl` fixture. Do not use an unreviewed
`pnetlab-original-export.zip` in automated tests or share it outside the
workspace unless it has been separately reviewed and sanitized.

The UI harness verifies response validation, safe text rendering, export gating,
export rejection messaging, stale-file invalidation, and download request
construction; it is not a browser end-to-end test. Flask
test-client route tests verify cookie/JWT, CSRF, same-origin checks, upload
size, malformed archive, review-only report contents, and response-state
behavior without opening a socket.

## Explicitly unverified

- Compatibility with other `.unl` schema versions or PNETLab releases.
- Exact VM image/build compatibility and startup-config behavior.
- Which network is reachable as a management segment.
- Gateway, routing, device configuration, import, Apply/Verify, and post-change
  state.
- Browser behavior outside the static UI harness and Flask test client.

Those items require sanitized representative exports, an agreed schema/profile
contract, or a separately approved runtime/device validation. The current
Safe Demo is inspection-only and does not perform those actions.
