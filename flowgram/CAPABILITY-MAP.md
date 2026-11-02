# Capability map: main branch UI ↔ FlowGram desktop

What every capability in `frontend/` needs to exist in the FlowGram-based branch,
and how much of it FlowGram's own UI already covers.

The rule for this branch: **reuse FlowGram's native UI wherever a mapping
exists**. Custom UI only where there is no native equivalent, and only after
both sides have been mapped.

Legend: **NATIVE** — FlowGram's own UI does it, no new component.
**ADAPTER** — FlowGram's UI does it, but a data/shape translation is needed.
**GAP** — no native equivalent; needs custom UI or is dropped.
**N/A** — not applicable to a RELION project.

---

## Three blockers to settle before mapping anything

### 1. FlowGram writes a side-car format into the project directory

Opening a project creates:

```
<project>/
├── flowgram.project.json          ← identity + name
├── workflows/<workflowId>/workflow.json
├── assets/
├── config/
└── .flowgram/state.sqlite         ← sessions, runs, events
```

`AGENTS.md` states the project's first principle as: *"Everything is read from
and written to the same files the GUI uses. There is no database and no
side-car format."* RELION's own GUI would see none of this, and a project that
has been through FlowGram is no longer byte-compatible with a project that has
not.

Options, in order of fidelity:

- **Shadow directory** — FlowGram's state lives outside the project (e.g.
  `~/.relion_web/flowgram/<project-hash>/`), the project stays pure RELION.
  Keeps the principle; needs the backend's project-open path changed.
- **Accept the side-car** — simplest, but abandons a documented invariant and
  the "move between GUI and browser without loss" goal.

This decision gates everything else. It is the one thing that could sink the
whole branch on principle rather than effort.

### 2. FlowGram cannot show data

There is no image, volume or slice rendering anywhere in the desktop app. The
tilt-series browser, `ImageViewer`, `VolumeViewer` (with pick-point placement)
and the job dialog's volume previews have **zero** native equivalent. That is
roughly a third of what the main branch is for.

### 3. FlowGram has no cluster model

No queue, no execution hosts, no GPU metrics, no admission control. The whole of
`NodesView`, `QueuePanel` and `hosts.py`/`queue_system.py` has nowhere to live in
FlowGram's model. Its `run_workflow` executes nodes in-process; it does not
submit work to a scheduler.

---

## Mapping by area

### Views

| Main | FlowGram | Class | Note |
|---|---|---|---|
| Projects browser | Project switcher + project home | ADAPTER | RELION project list ↔ FlowGram projects; see blocker 1 |
| Pipeline graph | `WorkflowCanvas` | ADAPTER | Node/edge shape maps directly; see "DAG mapping" below |
| Pipeline cards view | — | GAP | FlowGram has no card grid. Its run list is a table, not job cards |
| Tilt-series browser | — | GAP | No dataset/STAR browsing |
| Image viewer (slice/projection/3D) | — | GAP | No image rendering |
| Volume viewer + pick points | — | GAP | No volume rendering |
| GPU nodes view | — | GAP | No cluster model |

### Job lifecycle

| Main | FlowGram | Class | Note |
|---|---|---|---|
| Job type picker (33 types, grouped, searchable) | `NodePalette` | ADAPTER | Palette already does groups, search, favourites, recent. Map `generated_schema` job types → palette entries |
| Draft job on canvas before submit | Palette insert → node | ADAPTER | FlowGram's `insertNode` already creates nodes with defaults |
| Boolean param | `value` editor (boolean) | NATIVE | Main uses a Switch; FlowGram's boolean value editor is equivalent |
| Radio/enum param | `select` editor | NATIVE | Direct |
| Numeric/slider param | `number` editor (min/max) | NATIVE | Direct |
| Text param | `value` editor (string) | NATIVE | Direct |
| Multi-line text | `value` editor (object/array JSON) | ADAPTER | No plain textarea mode; JSON textarea is the closest |
| **File path param + filesystem browse** | — | GAP | FlowGram has no filesystem picker. Its `resource` editor only picks models/workflows |
| **Input-node param + slot picker** | — | GAP | Needs a custom editor: list pipeline `.Nodes` from `/api/project/input-candidates` |
| **Drag card onto input field** | — | GAP | FlowGram's DnD is node dragging; there is no cross-widget drop target |
| Advanced CLI arguments | — | GAP | No equivalent surface |
| Server validation (250 ms debounce) | `validate_workflow` + inspector footer | ADAPTER | Maps to `POST /api/job-validate` |
| Submit now | `run_workflow` | ADAPTER | Maps to `POST /api/jobs` |
| **Add to queue** | — | GAP | No queue concept |
| **Host selection** | — | GAP | No execution-host concept |
| Postpone / discard draft | — | NATIVE | Close without saving; drafts persist per session |

### Job inspection (main's six tabs)

| Main tab | FlowGram | Class |
|---|---|---|
| Overview (KPIs, previews, log, upstream/downstream) | Run inspector overview | ADAPTER |
| Log | Step detail → 日志 | ADAPTER |
| Inputs & parameters | Node inspector → 配置 | NATIVE |
| Outputs | Node inspector → 输出 | NATIVE |
| Volumes | — | GAP |
| Metadata | Run inspector facts | ADAPTER |

### Job actions

| Main | FlowGram | Class | Note |
|---|---|---|---|
| Alias | Canvas inline rename (`F2`) | NATIVE | Maps to the alias endpoint |
| Note | — | GAP | No note surface |
| Gentle / harsh clean | — | GAP | No equivalent |
| Mark failed / finished | — | GAP | No equivalent |
| Delete (→ Trash) | Canvas delete (`Delete`) | ADAPTER | Semantics match; endpoint is `DELETE /api/projects/processes/{name}` |
| Undelete | — | GAP | Main also has no UI for this |
| Stop running job | Run stop button | NATIVE | Maps to `DELETE /api/jobs/{id}` |
| Use as input | Canvas connect (drag port) | NATIVE | **Better than main** — main uses a menu + picker |

### Data / cluster

| Main | FlowGram | Class |
|---|---|---|
| Tilt-series selection → `selected_tilt_series.star` | — | GAP |
| Pick points → `picks/` | — | GAP |
| Node metrics, enable/disable, add/remove host | — | GAP |
| Queue list, cancel, retry | — | GAP |

### Assistant

| Main | FlowGram | Class | Note |
|---|---|---|---|
| Streaming chat | Conversation timeline | ADAPTER | FlowGram's is richer (mentions, queue/steer, tool groups) |
| Tool activity rows | `ToolRow` / `ToolGroup` | NATIVE | FlowGram's is more detailed |
| **Approval gate for writes** | — | GAP | FlowGram runs tools immediately. Our `writes=True` + approval must be preserved — this is a safety property, not a nicety |
| Provider settings | Model settings | NATIVE | FlowGram's is far more complete |
| New conversation / reset | Session actions | NATIVE | |

### Global

| Main | FlowGram | Class | Note |
|---|---|---|---|
| Dock navigation | Sidebar + tabs | NATIVE | Different shape, same job |
| Keyboard shortcuts | Richer set | NATIVE | FlowGram has more; ours wins on `j/k` selection, `f`/`t` |
| Light/dark theme | 8 palettes × light/dark | NATIVE | **Better than main** |
| Language | en + zh-CN | NATIVE | **Main has none** |
| Notifications | Alert banner | ADAPTER | FlowGram has no toast stack |
| Login (sshd) | — | GAP | Single bearer token, no user model |
| Layout persistence | Resizable panels, persisted | NATIVE | **Better than main** |

---

## What FlowGram gives us that main lacks

This is the other half of the ledger, and it is substantial:

- **Canvas editing of any kind** — main's graph is a rendered DAG you cannot
  edit. FlowGram gives drag, connect, multi-select, copy/paste/duplicate,
  undo/redo, auto-layout, inline rename, port-level insertion.
- **Run history** — execution tree, waterfall timeline, history scrubbing by
  event watermark, compare two runs, debug with historical inputs. Main shows
  one job at a time and has no cross-run view.
- **Sessions** — `@`-mention workflow references, queue-next-turn vs
  steer-current-turn, live tool groups, artifacts (saved-workflow and run cards).
- **Test a node in isolation** — with editable inputs and upstream samples.
- **Publishing** — versions, deployment targets, credentials, HTTP + MCP
  exposure. Main has nothing equivalent.
- **i18n, 8 themes, motion preferences, persisted layout.**

---

## The DAG mapping (the crux)

Main's model and FlowGram's model disagree on what an edge *is*.

| | Main (RELION) | FlowGram |
|---|---|---|
| Node | a job directory + `job.star` | a step in a workflow document |
| Edge | a **file node** produced by one job and consumed by another | a port-to-port connection |
| Truth | `default_pipeline.star` on disk | the workflow JSON in the project |
| Cardinality | one output file can feed many jobs | one output port, many lines |
| Cycles | refinement loops, condensed into one node | loop containers with boundary nodes |

Two ways to reconcile:

**A. Projection (recommended).** RELION's pipeline stays authoritative. FlowGram's
schema is regenerated from `/api/project/graph` on every read; edits go through
our endpoints (`queue_job`, `set_job_alias`, `delete_job`) and the projection is
refreshed. FlowGram's canvas becomes a **view + command surface**, not a second
store.

- Pro: one source of truth; no conflict resolution; keeps the GUI↔web invariant.
- Con: nodes cannot be freely rearranged (positions come from our layout), and
  FlowGram's `create_workflow`/`save_workflow` tools are not used.

**B. Two-way sync.** FlowGram's workflow JSON is the editing model, reconciled
against the pipeline.

- Pro: free positioning, uses FlowGram's full editing model.
- Con: two stores, conflict handling, and a project can drift out of RELION's
  view. Directly contradicts the "no side-car" principle.

Given blocker 1, A is the only self-consistent choice.

---

## Scorecard

Counting the capabilities listed above:

| Class | Count | Share |
|---|---|---|
| NATIVE | 19 | 38% |
| ADAPTER | 17 | 34% |
| GAP | 14 | 28% |

The 28% is not evenly distributed. It clusters in three places:
**data rendering** (5), **cluster/queue** (4), **RELION-specific job actions and
input binding** (5).

---

## Recommended sequence

1. Settle blocker 1 (side-car). Nothing else is worth building until this is
   decided.
2. Projection mapping of the DAG (option A) + palette from `generated_schema`.
3. Bring up the four high-value gaps that block daily use: file-path picker,
   input-node picker, queue submission, GPU/queue view.
4. Leave the data viewers (tilt/volume) in the main branch's own UI for now —
   they are self-contained and need no container from FlowGram.
