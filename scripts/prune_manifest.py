#!/usr/bin/env python3
"""Reduce a dbt manifest to the fields the semantic server actually reads.

A full manifest is ~687 KB against a 1 MiB ConfigMap limit - it fits, but with
no headroom, and most of it is compiled SQL, lineage and macro definitions the
server never looks at. Pruned to the read set it is ~3.5 KB.

The read set, verified against `servers/semantic/registry.py`:

    nodes.<id>.name            resolving ref('x') to a node
    nodes.<id>.resource_type   filtering to models
    nodes.<id>.database        \\
    nodes.<id>.schema           > the fully-qualified table name
    nodes.<id>.alias           /
    nodes.<id>.columns         KEYS only - expr validation. Values are dropped:
                               descriptions, tests and meta are never read.

The server must keep working against a full OR a pruned manifest, so this is an
optional deployment-time optimisation and never a dependency. Nothing in
`servers/` knows whether it was run.

Usage:
    python scripts/prune_manifest.py target/manifest.json manifest.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

KEEP = ("name", "resource_type", "database", "schema", "alias")


def prune(manifest: dict) -> dict:
    nodes = {}
    for node_id, node in (manifest.get("nodes") or {}).items():
        if node.get("resource_type") != "model":
            continue
        kept = {key: node[key] for key in KEEP if key in node}
        # Keys only. `bind_tables` and the expr check read `set(node["columns"])`,
        # so the descriptions and tests hanging off each column are pure weight.
        kept["columns"] = {name: {} for name in (node.get("columns") or {})}
        nodes[node_id] = kept
    return {"nodes": nodes}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2

    source, destination = Path(argv[0]), Path(argv[1])
    manifest = json.loads(source.read_text())
    pruned = prune(manifest)

    # Sorted and newline-terminated so re-running on an unchanged manifest
    # produces an identical file and the ConfigMap does not churn.
    destination.write_text(json.dumps(pruned, indent=2, sort_keys=True) + "\n")

    print(
        f"{source} ({source.stat().st_size / 1024:.1f} KB) -> "
        f"{destination} ({destination.stat().st_size / 1024:.1f} KB), "
        f"{len(pruned['nodes'])} model(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
