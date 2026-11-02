# RELION pipeline → FlowGram desktop

Feed a RELION project's pipeline into [FlowGram](https://flowgram.ai)'s desktop
app **without modifying FlowGram at all**. Its canvas, node inspector, minimap,
toolbar, run panel, shortcuts and agent are used exactly as upstream ships them.

## Why this shape

FlowGram's desktop app reads workflows from its own Rust backend over HTTP. So
the integration is a client of that API, not a fork: upstream can be updated by
pulling, and the two codebases never have to be kept in step by hand.

What we supply is only the **document** — which nodes exist, where they sit, and
what connects them. Everything drawn from that document is FlowGram's.

## Requirements

- The FlowGram repository (with `apps/desktop` and `apps/desktop-backend`).
- Node 22+ (the layout engine is run through `--experimental-strip-types`, the
  same way `scripts/dev_test.sh` runs `layout.test.ts`).
- `cargo` for FlowGram's backend.

## Run FlowGram's desktop app

```bash
cd flowgram.ai
node common/scripts/install-run-rush.js install      # once, ~2 min
node common/scripts/install-run-rush.js build --to @flowgram.ai/desktop

# backend (Rust). Writes connection.json into FLOWGRAM_DATA_DIR.
cd apps/desktop-backend
FLOWGRAM_DATA_DIR=/tmp/fg-data cargo run --bin flowgram-server

# UI, pointed at that backend
cd ../desktop
mkdir -p ../../.flowgram-local
cp /tmp/fg-data/connection.json ../../.flowgram-local/
npx vite --host 127.0.0.1          # http://127.0.0.1:1420/
```

Create a project in the UI (or via `POST /api/projects`), which gives you a
project id at `<project dir>/flowgram.project.json`.

## Push a pipeline in

```bash
# 1. The graph, straight from the agent
curl 'http://127.0.0.1:8765/api/project/graph?project_directory=/data/proj' > graph.json

# 2. Convert and save. Creates the workflow when --workflow is omitted.
python flowgram/relion_to_flowgram.py \
    --graph graph.json \
    --project <project id> \
    --name "RELION pipeline"

# Inspect without touching the backend:
python flowgram/relion_to_flowgram.py --graph graph.json --project x --dry-run
```

The script reuses `frontend/src/lib/layout.ts` for positions, so the pipeline
has the same left-to-right reading order as the app's own graph view. A second
layout in this script would be a second thing to keep correct.

## What a node carries

One FlowGram node type (`custom`, its generic kind) per job — the RELION job kind
is data, not a schema entry, so adding support for a new job type needs no
registration here.

| FlowGram field | From |
|---|---|
| `id` | process name, e.g. `Tomograms/job012` |
| `meta.position` | `layout.ts` box |
| `data.title` | the job's alias, plus `(+N)` for a condensed loop |
| `data.content` | `process name · status` |

Condensed refinement loops become a single node, as they do in the app's own
canvas.

## Status

Demo, verified end to end on an earlier branch:

- 10-job RELION project → 12 nodes (10 jobs + FlowGram's `start`/`end`), 14 edges
- renders correctly in the stock desktop canvas at 52 % zoom
- `Validate`, `Add node`, undo/redo, minimap and the run panel all work — none
  of them were touched

Not done yet, deliberately: no custom node visuals, no status colours, no
RELION-specific inspector, no click-through to the job directory. The point of
this branch is to establish whether FlowGram's own model carries the pipeline.
It does. Customisation is the next conversation.
