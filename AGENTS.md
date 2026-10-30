# AGENTS.md

Orientation for anyone (human or agent) working on this repository. It records
what the project is for, how it is put together, and — most usefully — why
several things are the way they are, because those reasons are not visible in
the code and were expensive to find.

Keep it current. A rule here that has stopped being true is worse than no rule.

---

## What this is

`recryo` is a browser interface for RELION cryo-ET projects. You can browse
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
| Frontend | React 19 + TypeScript 7 | |
| CSS | Tailwind 4 + daisyUI 5 | daisyUI owns the component layer; see *UI rules* |
| Frontend toolchain | **bun** | `bun install` / `bun run build`; `bun.lock` is the lockfile |
| Bundler | **Vite 8** | |
| Agent surface | pydantic-ai (assistant) + `mcp` (MCP server) | one registry, two exports |
| Testing | pytest (backend), committed layout checks (frontend) | |

`requires-python = ">=3.13"`. `mcp` 2 and 1.x both work; the code detects which
is installed (see *Gated decisions*).

### Toolchain traps

- **`bun build` cannot replace Vite here.** It does not process Tailwind 4 /
  daisyUI directives (`@apply`, `@theme`, `@plugin`) and emits them verbatim:
  32 kB of broken CSS instead of 154 kB compiled. Bun is the package manager and
  script runner; Vite is the bundler.
- **`bun test` does not run `frontend/src/lib/layout.test.ts` usefully.** That
  file uses `node:assert` and a hand-rolled harness, so Bun's runner reports
  "0 pass / 0 fail" while the 12 checks have in fact passed. `dev_test.sh` runs
  it with `node --experimental-strip-types`.
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
backend/recryo/          the package
frontend/                React app; `npm`/`bun run build` writes into backend/recryo/static
scripts/                 dev_web.sh, dev_test.sh, the three table generators
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
| `app.py` | FastAPI routes (64) and the WebSocket |

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

`frontend/src/components/ui/` are thin wrappers over **daisyUI** components:
`button`→`btn`, `badge`→`badge`, `tabs`→`tabs`, `dialog`→`modal`,
`tooltip`→`tooltip`, `spinner`→`loading`. Do not reimplement a component daisyUI
provides. Use daisyUI colour names (`base-100`, `text-base-content/60`) rather
than Tailwind palette colours, so both themes follow.

Status colours are the exception: RELION's five states are not daisyUI's semantic
roles ("running" is not a warning), so `components/Status.tsx` owns them. Every
fill/text pair is verified at ≥ 4.5:1 in both themes.

The **pipeline graph is the product**, not a sidebar list. A draft job exists on
the canvas before it is queued.

---

## Working on it

```bash
export PATH="$HOME/.bun/bin:$HOME/miniforge3/bin:$PATH"

uv venv && uv pip install -e '.[dev,assistant]'
./scripts/dev_web.sh          # agent :8765, UI :5173, both with reload
./scripts/dev_test.sh         # every check; --quick for tests only
```

`dev_test.sh` runs six checks and each skips itself with a reason when its
prerequisite is absent, so a standalone clone is usable without RELION:

| Check | What it proves |
|---|---|
| `tests` | the pytest suite (766 pass) |
| `generated` | the three tables still match RELION's C++ |
| `format` | RELION's own `metadata_table.cpp` reads our files, and ours reads its |
| `ui` | typechecks and builds |
| `ui-bundle` | the committed bundle matches the source |
| `ui-layout` | the graph layout maths |

**If you change `frontend/`, rebuild the bundle** — `ui-bundle` fails otherwise,
because the bundle is committed so a deployment needs no JS toolchain.

**If you change a generated table, regenerate it with `--relion-source`** and
commit the result; never edit it directly.

---

## Known open items

- **License.** `pyproject.toml` declares BSD-3-Clause, but the generated schema
  carries RELION's help text essentially verbatim (330 of 335 strings), and
  `_vendor.py` transcribes RELION's Python writer. That makes this a derivative
  work of a GPL-2.0-or-later project, so BSD-3-Clause is not a valid declaration
  for a published release. **This blocks a public release.** Not yet decided.
- **The drag gesture is not browser-verified.** Dragging a job card onto an input
  slot is covered by typecheck and the API contract underneath, but has not been
  exercised in a real browser.
- **Multi-GPU / A100-class nodes are untested.** Node metrics were verified on a
  single RTX 4060 and against simulated processes, not on a multi-GPU node
  running real RELION jobs.
- **23 generated defaults point at `/public/EM/RELION/...`**, a path from the
  machine the tables were extracted on. Cosmetic, but odd in a public package.
