#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Push a RELION pipeline into a FlowGram desktop workflow.

This is the whole integration. FlowGram's desktop app (apps/desktop in the
FlowGram repository) is used unchanged - its canvas, toolbar, minimap,
inspector, run panel and shortcuts are all stock. Only the *document* is ours:
a RELION project's pipeline, positioned by this repository's own layout engine.

Why a separate process rather than a fork of FlowGram: the desktop app reads
workflows from its Rust backend over HTTP, so feeding it data needs no changes
to its source at all. That keeps the upstream tree pristine and lets it be
updated by pulling, rather than by replaying a patch series.

    # FlowGram's backend and UI running (see flowgram/README.md)
    python flowgram/relion_to_flowgram.py \\
        --graph graph.json --project /path/to/flowgram-project

The graph is what the agent already serves:

    curl 'http://127.0.0.1:8765/api/project/graph?project_directory=/data/proj' \\
        > graph.json
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# The same layout the graph view uses, so a project looks the same in both
# places. Imported rather than reimplemented: a second layout would drift.
LAYOUT_SCRIPT = r"""
import { layoutPipeline } from "LAYOUT_PATH";
import fs from "node:fs";
const graph = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const layout = layoutPipeline(graph);
process.stdout.write(JSON.stringify(layout));
"""


def run_layout(graph_path: Path) -> dict[str, Any]:
    """Run the app's TypeScript layout engine over the graph.

    Node 22 strips the types natively (`--experimental-strip-types`), which is
    how `dev_test.sh` already runs `layout.test.ts` - no build step, no bundler.
    """
    import subprocess
    import tempfile

    root = Path(__file__).resolve().parent.parent
    layout_ts = root / "frontend" / "src" / "lib" / "layout.ts"
    if not layout_ts.is_file():
        raise SystemExit(f"no layout engine at {layout_ts}")

    script = LAYOUT_SCRIPT.replace("LAYOUT_PATH", str(layout_ts))
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)

    try:
        result = subprocess.run(
            ["node", "--experimental-strip-types", str(script_path), str(graph_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise SystemExit(f"layout failed:\n{result.stderr}")
    return json.loads(result.stdout)


def to_schema(graph: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any]:
    """RELION graph + layout -> FlowGram `WorkflowSchema`."""
    by_name = {process["name"]: process for process in graph["processes"]}

    nodes = []
    for box in layout["nodes"]:
        process = by_name.get(box["primary"], {})
        alias = process.get("alias") or ""
        label = alias if alias and alias != "None" else box["primary"]
        extra = f" (+{len(box['processes']) - 1})" if len(box["processes"]) > 1 else ""
        nodes.append(
            {
                "id": box["primary"],
                # One type for every job. `custom` is FlowGram's generic node;
                # the RELION job kind is data, not a schema entry.
                "type": "custom",
                "meta": {"position": {"x": box["x"], "y": box["y"]}},
                "data": {
                    "title": f"{label}{extra}",
                    "content": f"{box['primary']} · {process.get('status', 'Scheduled')}",
                },
            }
        )

    edges = [
        {"sourceNodeID": edge["from"], "targetNodeID": edge["to"]}
        for edge in layout["edges"]
    ]

    # FlowGram's default document starts from `start` and ends at `end`; wire
    # the pipeline's roots and leaves to them so the graph is connected.
    has_pred = {edge["targetNodeID"] for edge in edges}
    has_succ = {edge["sourceNodeID"] for edge in edges}
    roots = [node["id"] for node in nodes if node["id"] not in has_pred]
    leaves = [node["id"] for node in nodes if node["id"] not in has_succ]

    root_node = None
    for box in layout["nodes"]:
        if box["primary"] in roots:
            root_node = box
            break

    start = {
        "id": "start",
        "type": "start",
        "meta": {
            "position": {
                "x": min(node["meta"]["position"]["x"] for node in nodes) - 320,
                "y": root_node["y"] if root_node else 0,
            }
        },
        "data": {"title": "Input"},
    }
    end = {
        "id": "end",
        "type": "end",
        "meta": {
            "position": {
                "x": max(
                    node["meta"]["position"]["x"]
                    for node in nodes
                )
                + 320,
                "y": root_node["y"] if root_node else 0,
            }
        },
        "data": {"title": "Output"},
    }

    for name in roots:
        edges.append({"sourceNodeID": "start", "targetNodeID": name})
    for name in leaves:
        edges.append({"sourceNodeID": name, "targetNodeID": "end"})

    return {"nodes": [start, *nodes, end], "edges": edges}


def flowgram_call(endpoint: str, token: str, method: str, path: str, body=None, project=None):
    request = urllib.request.Request(
        f"{endpoint}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **({"X-FlowGram-Project": project} if project else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read().decode()}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", required=True, type=Path, help="pipeline graph JSON")
    parser.add_argument("--project", required=True, help="FlowGram project id")
    parser.add_argument("--workflow", help="existing workflow id; created when omitted")
    parser.add_argument("--name", default="RELION pipeline", help="workflow name when creating")
    parser.add_argument(
        "--connection",
        type=Path,
        default=Path(os.environ.get("FLOWGRAM_CONNECTION_FILE", ".flowgram-local/connection.json")),
        help="FlowGram backend connection file",
    )
    parser.add_argument("--out", type=Path, help="also write the schema here")
    parser.add_argument("--dry-run", action="store_true", help="print the schema, do not save")
    args = parser.parse_args()

    graph = json.loads(args.graph.read_text())
    layout = run_layout(args.graph)
    schema = to_schema(graph, layout)

    if args.out:
        args.out.write_text(json.dumps(schema, indent=2) + "\n")

    if args.dry_run:
        print(json.dumps(schema, indent=2))
        return

    connection = json.loads(args.connection.read_text())
    endpoint, token = connection["endpoint"], connection["token"]

    workflow_id = args.workflow
    if not workflow_id:
        created = flowgram_call(
            endpoint,
            token,
            "POST",
            "/api/workflows",
            {"projectId": args.project, "name": args.name},
            project=args.project,
        )
        workflow_id = created["id"]

    # The save is optimistic-concurrency: the backend rejects the write unless
    # the revision matches what it holds, which is what stops two editors from
    # silently overwriting each other.
    current = flowgram_call(
        endpoint, token, "GET", f"/api/workflows/{workflow_id}", project=args.project
    )

    saved = flowgram_call(
        endpoint,
        token,
        "PUT",
        f"/api/workflows/{workflow_id}",
        {
            "name": current["name"],
            "schema": schema,
            "revision": current["revision"],
        },
        project=args.project,
    )
    print(f"saved {saved.get('name', '')} revision {saved.get('revision', '?')}: "
          f"{len(schema['nodes'])} nodes, {len(schema['edges'])} edges")


if __name__ == "__main__":
    main()
