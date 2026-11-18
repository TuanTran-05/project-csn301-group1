"""Show how a PNETLab lab reports its cables, to debug imports without links.

Read-only: it logs in, reads the lab and a few interface endpoints, prints a
summary and writes everything PNETLab answered to a JSON file. Nothing in the
lab is changed and the password is never printed or saved.

Run it on the AI Server, which can reach PNETLab:

    cd backend
    .venv/bin/python scripts/diagnose_pnetlab_links.py --url http://172.16.3.25 --username <account> --lab "/path/lab.unl"

Leave --lab out to read the lab already open in the account's session.
The password is asked for on the terminal (or read from PNETLAB_PASSWORD).
"""

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from network_copilot.integrations.pnetlab import PNETLabClient, PNETLabError, _lab_path
from network_copilot.topology.pnetlab_import import derive_links


def get(client, path):
    try:
        status, data, _ = client._send("GET", path)
    except PNETLabError as exc:
        return {"error": exc.message}
    return {"status": status, "data": data}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--lab", default="")
    parser.add_argument("--mode", default="auto", choices=("auto", "session", "eveng"))
    parser.add_argument("--no-verify-tls", action="store_true")
    args = parser.parse_args()
    password = os.environ.get("PNETLAB_PASSWORD") or getpass.getpass("PNETLab password: ")

    client = PNETLabClient(args.url, args.username, password,
                           verify_tls=not args.no_verify_tls, mode=args.mode)
    try:
        nodes, topology, networks = client.read_lab(args.lab)
    except PNETLabError as exc:
        print(f"ERROR: {exc.message}")
        return 1

    report = {"mode_used": client.mode_used, "nodes": nodes, "networks": networks,
              "topology_entries": topology, "raw": {}}
    if client.mode_used == "session":
        report["raw"]["session_topology"] = get(client, "/api/labs/session/topology")
        first = next(iter(nodes.values()), {}) if nodes else {}
        node_id = first.get("id")
        if node_id is not None:
            for path in (f"/api/labs/session/nodes/{node_id}",
                         f"/api/labs/session/nodes/{node_id}/interfaces",
                         "/api/labs/session/links", "/api/labs/session/networks"):
                report["raw"][path] = get(client, path)
    else:
        path = _lab_path(args.lab)
        first = next(iter(nodes), None)
        if first is not None:
            report["raw"]["interfaces"] = get(client, f"/api/labs{path}/nodes/{first}/interfaces")

    links = derive_links(nodes, topology, networks)
    report["derived_links"] = links

    print(f"API used: {client.mode_used}")
    print(f"Nodes: {len(nodes)}   networks: {len(networks)}   "
          f"topology entries: {len(topology)}   cables found: {len(links)}")
    raw = report["raw"].get("session_topology", {}).get("data")
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        print("Session topology keys:", sorted(raw["data"].keys()))
    for key, node in list(nodes.items())[:3]:
        print(f"\nNode {key} {node.get('name')!r} ({node.get('template')}) keys: {sorted(node.keys())}")
        for field in ("ethernets", "ethernet", "interfaces", "serials"):
            if field in node:
                print(f"  {field}: {json.dumps(node[field], ensure_ascii=False)[:800]}")
    for key, net in list(networks.items())[:5]:
        print(f"Network {key}: {json.dumps(net, ensure_ascii=False)[:300]}")

    out = Path(time.strftime("pnetlab-links-%Y%m%d-%H%M%S.json"))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\nFull answer saved to {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
