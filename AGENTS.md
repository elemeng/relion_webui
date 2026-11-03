# AGENTS.md

Orientation for anyone (human or agent) working on this repository. It records
what the project is for, how it is put together, and — most usefully — why
several things are the way they are, because those reasons are not visible in
the code and were expensive to find.

Keep it current. A rule here that has stopped being true is worse than no rule.

---

## What this is

`relion_webui` is a browser interface for RELION cryo-ET projects. You can browse
tilt-series and tomograms, render slices and volumes, build and submit jobs,
watch them run, and inspect what they produced.

**Goal: a project should be drivable from the browser without the desktop GUI,
and a project should move between the two without loss.** Everything is read
from and written to the same files the GUI uses. There is no database and no
side-car format.

**It is a distribution, not a plugin.** RELION's binaries are the only runtime
requirement. Nothing here is imported by RELION's Python tree, and RELION is not
a Python dependency — this package reimplements the pipeline file format and
talks to the programs over their command lines and sentinel files.

Non-goals, so they are not proposed again: replacing RELION's algorithms,
becoming a cluster scheduler, or becoming a general workflow engine.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.13, FastAPI, uvicorn | async, and the pydantic models double as the API schema |
| Python toolchain | **uv** | resolves from `pyproject.toml`; a cold install of the full set is ~14 s where pip took over a minute |
| Frontend | React 19 + TypeScript 5.8, vendored FlowGram desktop UI | see *UI rules* |
| CSS | Tailwind 4 | utility layer; components are Radix wrappers |
| Type | Geist + Geist Mono, eight palettes | one source of colour: `lib/themes.ts` |
| Frontend toolchain | **bun** | `bun install` / `bun run build`; `bun.lock` is the lockfile |
| Bundler | **Vite 6** | builds `flowgram-app/` into `backend/relion_webui/static` |
| Agent surface | `mcp` (MCP server) + `pydantic-ai` (optional assistant) | one registry, two exports |
| Canvas API | FastAPI router in `flowgram/routes.py` | one `/api` namespace, no second process |
| Testing | pytest (backend + canvas), vitest (UI behaviour) | |

`requires-python = ">=3.13"`. Licensed **GPL-2.0-or-later**; every source file
carries an SPDX header, and the generated tables carry one their generator emits
(so regenerating never drops it). `mcp` 2 and 1.x both work; the code detects which
is installed (see *Gated decisions*).

### Toolchain traps

- **`bun build` cannot replace Vite here.** It does not process Tailwind 4
  directives (`@apply`, `@theme`, `@plugin`) and emits them verbatim: 32 kB of
  broken CSS instead of the compiled bundle. Bun is the package manager and
  script runner; Vite is the bundler.
- **The canvas layout is Python, not TypeScript.** It moved into
  `backend/relion_webui/flowgram/layout.py` when the canvas became a static
  bundle the agent serves; `dev_test.sh`'s `ui-layout` check runs
  `tests/test_flowgram_bridge.py`, which pins the column ranks and the
  cycle-condensation rule.
- **`bun install` rewrites `package.json`** to the newest in-range versions. Use
  `bun add` for a deliberate change; a bare `bun install` once silently jumped
  TypeScript 5.7→7 and Vite 6→8 and broke the build.
- **PATH must point at a directory, not a binary.** `~/.bashrc` had
  `export PATH=/home/meng/.bun/bin/bun:$PATH`, which shadows nothing and lets a
  Windows `bun` from `/mnt/c/...` win — it then writes a cache Linux bun cannot
  read (`ENOENT: failed copying files from cache`).

---

## Layout

```
backend/relion_webui/          the package
backend/relion_webui/flowgram/ the canvas: its API, projection, journal, workspaces
backend/relion_webui/static/   the built canvas bundle (committed, so uv run works)
flowgram-app/            the vendored FlowGram UI source (`bun run build` writes the bundle)
scripts/                 dev_flowgram.sh, dev_test.sh, the three table generators
tests/                   pytest suite
docs/                    design notes (local only, not committed)
```

### The generated tables — do not edit by hand

Three files are **extracted from RELION's C++ sources** and must be regenerated,
never patched:

| File | Generated from |
|---|---|
| `generated_schema.py` | `pipeline_jobs.cpp`, `pipeline_jobs.h`, `gui_jobwindow.cpp` |
| `generated_commands.py` | `pipeline_jobs.cpp` |
| `generated_cli_options.py` | RELION's C++ and Typer sources |

```bash
python scripts/generate_web_job_schema.py --relion-source /path/to/relion
python scripts/generate_web_job_commands.py --relion-source /path/to/relion
python scripts/generate_web_cli_options.py --relion-source /path/to/relion
```

**`--relion-source` is the contract.** Resolution is: the flag, then
`$RELION_SOURCE`, then a `relion/` sibling, then the parent directory. A path
that resolves to nothing fails immediately with a message naming the flag.
Earlier this path was resolved at *import* time from a parent directory, so
moving the repo produced a bare `FileNotFoundError` on an arbitrary `.cpp` in
the middle of extraction.

`dev_test.sh` re-runs the generators in `--check` mode and fails if a table has
drifted. That is the guard against editing them by hand.

### Core modules

| Module | Responsibility |
|---|---|
| `job_commands.py` | builds each job's argv, mirroring `getCommands*` in the C++ |
| `job_queue.py` | MPI wrapping and queue submission, mirroring `prepareFinalCommand` |
| `jobs.py` | runs a job; reads the sentinel files; syncs the pipeline |
| `pipeline.py` | reads/writes `default_pipeline.star` under RELION's own lock |
| `job_actions.py` | alias, note, delete (→ Trash), undelete, clean, mark |
| `queue_system.py` | the native queue: admission, GPU leases, dependencies |
| `hosts.py` | execution hosts; ssh argv construction |
| `node_metrics.py` | one ssh round trip per node: load, memory, GPUs, our jobs |
| `statuses.py` | the one status vocabulary and its mapping to RELION's labels |
| `tools.py` | the tool registry — 24 tools, 10 of them writes |
| `mcp_server.py` | transport only: exposes the registry over MCP at `/mcp` |
| `assistant.py` | the same registry to pydantic-ai, with approval for writes |
| `flowgram/routes.py` | the canvas API: `/api/workflows`, `/api/runs`, `/api/events`, plus static |
| `flowgram/translate.py` | a RELION pipeline as a FlowGram workflow document |
| `flowgram/layout.py` | pipeline → canvas geometry, cycles condensed |
| `flowgram/journal.py` | queue states → runtime events (the canvas's badges and camera) |
| `flowgram/workspaces.py` | draft overlays in the config directory, never in the project |
| `flowgram/provision.py` | add a GPU node from an address: key, registration, reachability |
| `app.py` | FastAPI routes and the WebSocket |

---

## Architecture decisions that must not be undone

These were each found the hard way. Changing one will look harmless and won't be.

### 1. RELION's protocol, not a parallel one

Job control uses `--pipeline_control <job dir>` plus the sentinel files
(`RELION_JOB_EXIT_SUCCESS` / `_FAILURE` / `_ABORTED`), and cancellation touches
`RELION_JOB_ABORT_NOW`. Reading an exit code is *not* enough: a queued job's
process is the submit command, whose code means "accepted", not "finished".

**Sentinels are the durable record; the pipeline row is a summary.** On restart,
read the sentinels *before* the pipeline row. `sync_pipeline_status` deliberately
swallows write failures, so a row can lag forever — that once left a finished job
reported as running with its GPU leased indefinitely. `jobs.sentinel_status()` is
the single reader; do not add a second.

### 2. A queued job is RELION's `PROC_SCHEDULED`, not a web-only table

A job's number is assigned **at enqueue**, not at dispatch, so a downstream stage
can name its output path before it exists. The mechanism is RELION's own:
`PipeLine::addJob(job, PROC_SCHEDULED)` registers the process and its output
nodes before the files exist, and RELION touches the `.Nodes` placeholder for a
scheduled job. So "the virtual file list" *is* the pipeline, and the desktop GUI
can run, edit or delete a queued job.

Consequences that are easy to break:
- A reserved job also gets its **directory and `job.star`**, because
  `prepareFinalCommand` does `mktree(outputname)` before building commands. Skip
  that and opening the job 404s.
- A reserved job's **`.Nodes` entry must exist**, or the output is invisible to
  every input picker — including RELION's own.
- Cancelling a never-started job must **remove both**, or a cancelled job is
  offered as an input forever.

### 3. GPU devices are leased, never shared

Two RELION programs on one card produce broken output, not just slow output.
Devices are leased exclusively.

**Busyness comes from compute tenants (`nvidia-smi` compute-apps UUIDs), not
memory in use.** A workstation's display server holds memory on card 0 from boot
and is not a job to wait for.

Whether a job uses a card is a **table**, not a guess from the form:
`tomo_align_*`, `tomo_denoise_*`, ModelAngelo are GPU-only; `refine`-family only
with `use_gpu`; **`motioncorr` only with the external MotionCor2** — its
`do_own_motioncor` defaults to yes, which is RELION's CPU implementation, and
`--gpu` is emitted only for the external one.

GPU memory requirements are **declared, not derived**: RELION's options carry no
memory figure, so `gpu_memory_mb` is opt-in. An unknown card size counts as
fitting.

### 4. One tool registry, two surfaces

`tools.py` defines each capability once; `mcp_server.py` and `assistant.py` both
consume it. Never define a capability in one surface only — that is how the two
drift. Tools are marked `writes=True` and both surfaces refuse writes without
approval. `allow_writes` is enforced in `call_tool`, the one place every call
passes through.

### 5. Paths are confined to the project list

`PathGuard` resolves every client-supplied path against the configured roots,
read dynamically so adding a project takes effect at once. Directory browsing is
deliberately root-unrestricted (read-only); the *project list* is the mutation
boundary.

Note the deliberate exception: a path handed to RELION as a job option is not
guard-checked, because RELION must be able to open any file the user names. The
guard applies to what *this agent* reads.

### 6. One status vocabulary

`statuses.py` owns the constants, the mapping to RELION's labels, and
terminality. RELION's labels are the only vocabulary that leaves the process.
Three layers previously had three hand-written mappings, one of which mapped a
`Scheduled` row to "running" while another treated it as "not started".

### 7. MCP version support

`mcp` 2 renamed `FastMCP` → `MCPServer`, moved transport options to
`streamable_http_app()`, snake_cased `ToolAnnotations` fields, and added a
resource-template check rejecting absolute paths. All four are handled by
detecting the installed major. The template check is relaxed **only because**
every resource handler resolves through `PathGuard`, which is the real boundary;
null-byte rejection is kept.

---

## UI rules

The UI is the vendored FlowGram desktop app (`flowgram-app/`), restyled to this
repository's tokens. Its `components/ui/` are shadcn-style wrappers over Radix
(`button`, `dialog`, `select`, `popover`); do not add a second component library
beside them.

**Eight palettes, defined as data.** `flowgram-app/src/lib/themes.ts` holds every
colour and `styles.css` maps them onto `--background`, `--surface-*`,
`--foreground`, `--muted-foreground`, `--border`, `--destructive`. New colours go
through a theme entry, not a literal in a component. The measured contrast of
the weakest pair per mode is recorded in that file: changing a value means
recomputing its pair, because 4.5:1 is the floor.

**No px font sizes.** Every `font-size` in the three stylesheets is `rem`, and
the sub-`text-xs` steps are theme tokens (`--text-2xs`, `--text-3xs`) rather than
`text-[10px]`. That is what lets browser zoom and a user's font setting work.

**Surfaces are opaque. No translucency, no `backdrop-filter`.** Mica and Acrylic
were tried and removed: both are designed against a *photographic* backdrop (a
Windows wallpaper), where blur turns detail into pleasant colour. Nothing here
has a wallpaper — the thing behind a panel is job cards, pipeline edges and
text, so blurring it produces grey noise and pulls the panel's own text towards
it. Elevation is carried by the **tone ladder** instead: page < chrome < card <
floating panel (light 95.6 → 96.4 → 99.3 → 100%; dark 19 → 16.5 → 25.5 → 30%,
where "elevated" means lighter), plus the hairline and the shadow.

Type is **Geist + Geist Mono**. Do not swap to a UI kit's default font; the mono
is what makes job numbers and paths read as data. New colours go through a theme
entry in `lib/themes.ts`, never a literal in a component.

Status colours are RELION's own vocabulary, not a design system's semantic roles
("running" is not a warning): the canvas reads them from the queue's state and
renders `.node-run-badge[data-status=...]`. Every text/surface pair is ≥ 4.5:1 in
both modes — the measured ratios sit in `lib/themes.ts`; re-measure when a value
changes.

The **pipeline graph is the product**, not a sidebar list. A draft job exists on
the canvas before it is queued, and nothing reaches RELION until the queue
reserves it. Geometry comes from the agent (`flowgram/layout.py`), so the canvas
and anything else that lays out a pipeline agree; the node sizes it emits are
plain pixel world coordinates inside the zoom transform, deliberately not rem.

---

## Working on it

```bash
export PATH="$HOME/.bun/bin:$HOME/miniforge3/bin:$PATH"

uv venv && uv pip install -e '.[dev,assistant]'
./scripts/dev_flowgram.sh     # agent :8765 + Vite :1420 with hot reload
./scripts/dev_flowgram.sh --build   # build the bundle, then agent only
./scripts/dev_test.sh         # every check; --quick for tests only
```

`dev_test.sh` runs six checks and each skips itself with a reason when its
prerequisite is absent, so a standalone clone is usable without RELION:

| Check | What it proves |
|---|---|
| `tests` | the pytest suite (789 pass) |
| `generated` | the three tables still match RELION's C++ |
| `format` | RELION's own `metadata_table.cpp` reads our files, and ours reads its |
| `ui` | typechecks and builds `flowgram-app/` |
| `ui-bundle` | the committed bundle matches the source |
| `ui-layout` | the pipeline layout (Python now) |

**If you change `flowgram-app/`, rebuild the bundle** — `ui-bundle` fails
otherwise, because the bundle is committed so a deployment needs no JS
toolchain. `./scripts/dev_flowgram.sh --build` does it and then serves the
canvas from the agent alone.

**If you change a generated table, regenerate it with `--relion-source`** and
commit the result; never edit it directly.

---

## Known open items

- **Licensing is settled: GPL-2.0-or-later.** See `LICENSE` for the reasoning in
  full. Briefly: ~30% of the backend is derived from RELION (the generated tables
  carry 330 of 335 help strings verbatim; `job_commands.py` ports `getCommands*`),
  so a permissive license is not available. "or-later" rather than "-only" because
  34 dependencies are Apache-2.0 (`python-multipart`, plus the OpenAI/Anthropic/
  Google SDKs in the assistant extra), which are compatible with GPL-3.0 but not
  GPL-2.0-only. **Do not relicense to MIT or BSD without rewriting the derived
  30%**, and do not accept a contribution under a different license.
- **The drag gesture is not browser-verified.** Dragging a job card onto an input
  slot is covered by typecheck and the API contract underneath, but has not been
  exercised in a real browser.
- **Multi-GPU / A100-class nodes are untested.** Node metrics were verified on a
  single RTX 4060 and against simulated processes, not on a multi-GPU node
  running real RELION jobs.
- **23 generated defaults point at `/public/EM/RELION/...`**, a path from the
  machine the tables were extracted on. Cosmetic, but odd in a public package.
