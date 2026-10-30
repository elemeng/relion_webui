# RELION cryo-ET web agent

A long-running per-user service that serves a browser front end for RELION
cryo-ET data: browse tilt-series and tomograms, manage several projects, and
run any of the desktop GUI's 23 tomography job types from a browser.

Beyond the browser, the same 17 capabilities are exposed as an MCP server and to
an in-panel LLM assistant; both are opt-in and documented in the package
docstrings (`mcp_server.py`, `assistant.py`).

See [COVERAGE.md](COVERAGE.md) for an itemised comparison with
`relion_maingui --tomo`, including what is at parity and what is still missing.

Every job form has the GUI's tabs and fields, plus an **Advanced arguments**
panel listing the full command-line surface of the program that will run - flags
the desktop job window hides, renames or merges. Those flags are read from each
program's own argument parser rather than guessed, and they travel in
`other_args`, so the command keeps the shape RELION expects and `job.star`
records them for the desktop GUI.

One person runs one agent, once. It keeps running, serves on the LAN, and asks
the machine's own sshd to validate browser logins, so visitors sign in with
their normal account rather than a password this program stores.

## Install

This is a standalone Python distribution (`relion_webui`). It does **not** need
RELION to be compiled and does not need RELION's Python package: the pipeline
file format is written directly and the programs are driven through their
command lines and the sentinel files. RELION's binaries must be on `PATH` to
actually run jobs, and `relion_pipeliner` for the clean/scheduler actions; both
are detected at run time and their absence is reported per action.

From a RELION checkout:

```bash
cd relion_webui
uv pip install -e '.[dev]'
./scripts/dev_web.sh          # agent + Vite, both with reload
```

On a machine that already has RELION installed, point it at that installation
and run the installed command:

```bash
uv pip install relion-webui    # or: uv pip install /path/to/relion_webui
relion_webui --host 127.0.0.1 --root /data/projects
```

The UI is a Vite/React bundle built with shadcn/ui components, and the built
output is committed under `static/`, so running the agent needs no Node
toolchain. Only rebuilding it does (see "Front end" below).

`websockets` is a hard dependency in practice: without it uvicorn refuses the
upgrade request and the job panel falls back to polling with a console error on
every load.

## Front end

The interface lives in `frontend/`: React 19, TypeScript, Tailwind 4 and
shadcn/ui components (Radix primitives plus the shadcn token set in
`src/index.css`). Light is the default theme; dark is a toggle in the header and
is remembered per browser.

```bash
cd frontend
bun install     # reads bun.lock
bun run dev     # proxies /api and /ws to an agent on 127.0.0.1:8765
bun run build   # writes the bundle into backend/relion_webui/static
```

`bun run build` writes into `backend/relion_webui/static`, which is what the
agent serves; the result is committed, so a clone runs without any JS toolchain.
Building RELION itself never runs bun.

The components under `frontend/src/components/ui/` follow shadcn/ui's source,
copied in rather than pulled from a package, so they can be edited directly.

## Run

One agent per user, started once:

```bash
relion_webui --port 8765
```

The banner prints the address to open:

```
relion_webui running as meng
listening on 0.0.0.0:8765
open in a browser:
  http://192.168.1.42:8765/
log in with your account on this machine; sessions last 12h
project list: /home/meng/.config/relion-web
projects: 2
```

Sign in with your username and password for that machine. `systemd --user` works
well for keeping it up:

```ini
# ~/.config/systemd/user/relion-web-agent.service
[Unit]
Description=RELION cryo-ET web agent

[Service]
ExecStart=%h/relion/build/bin/relion_webui --port 8765
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now relion-web-agent
```

| Option | Meaning |
| --- | --- |
| `--host` | Interface to bind (default `0.0.0.0`). Use `127.0.0.1` with an SSH tunnel. |
| `--port` | Port to bind (default 8765). |
| `--project/-p` | Project to add to the list at startup; repeatable. |
| `--root/-r` | Extra directory to serve beyond the listed projects; repeatable. |
| `--session-hours` | How long a browser login stays valid (default 12). |
| `--ssh-host` / `--ssh-port` | Where to send credentials for checking (default `127.0.0.1:22`). |
| `--config-directory` | Where the project list is stored. |
| `--no-auth` | Disable login. Refused unless bound to loopback. |

## How login works

The agent has no user database. When the form is submitted it opens one SSH
connection to the host's sshd with the supplied username and password, and
accepts the login only if sshd does. Whatever the machine already enforces —
PAM, 2FA, account expiry, LDAP, `AllowUsers` — applies unchanged. This is the
same model ttyd and JupyterHub's PAM authenticator use.

The password is used for that single connection and never stored; sessions are
in-memory tokens in an `HttpOnly`, `SameSite=Strict` cookie, so restarting the
agent logs everyone out. Failed attempts are rate limited per client address.

Because the password crosses the network to the agent, run this on a trusted
network or behind TLS. If that is not acceptable, bind to loopback and use a
tunnel, where no web password is needed at all:

```bash
ssh -L 8765:127.0.0.1:8765 user@server   # browse http://127.0.0.1:8765
```

Note that a login only says "this person can log in to this host". The agent
serves one user's files, so any authenticated session is that user; there is no
per-request permission check beyond the project list.

## Projects

The agent keeps a list of projects, not a single project. Add and remove them in
the sidebar or through the API:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/projects` | Listed projects with job counts and readability. |
| `POST /api/projects` | `{"path": "..."}` — must contain `default_pipeline.star`. |
| `DELETE /api/projects?path=` | Forget a project (its files are untouched). |

The list is stored in `~/.config/relion-web/projects.json` (override with
`--config-directory` or `RELION_WEB_HOME`). It is also the access
boundary: the file APIs only reach listed projects and `--root` directories, so
a client cannot point the agent at arbitrary paths even though the agent could
read them.

Switching projects in the UI re-reads that project's pipeline, so several
projects can be driven from one browser tab.

Every path field in the UI (project directory, STAR file, job output
directory, selection output) has a browse button that opens a directory picker
running on the agent, so it shows the filesystem of the machine running RELION
rather than the viewer's own computer. Typing a path still works.

Browsing deliberately ignores the allowed roots: the picker has to be able to
show a project directory that is not yet in the list, otherwise the first
project could never be added. The trade-off is that browsing is read-only and
shows whatever the agent's account can read — the same thing a file manager
would. Acting on a path is still restricted: only listed projects and `--root`
directories can be opened or written to.

## Accessing it from another computer

Binding `0.0.0.0` makes it reachable; the banner prints the addresses. Things
that commonly block it:

- **Firewall.** Open the port (`sudo firewall-cmd --add-port=8765/tcp` on
  firewalld, `sudo ufw allow 8765/tcp` on Ubuntu).
- **WSL2.** WSL2 puts the Linux VM behind NAT, so the printed address is the
  VM's, not the Windows host's. Either forward the port from Windows, or switch
  the VM to mirrored networking (`.wslconfig`: `networkingMode=mirrored`):

  ```powershell
  netsh interface portproxy add v4tov4 listenport=8765 listenaddress=0.0.0.0 `
      connectport=8765 connectaddress=(wsl hostname -I).Trim()
  ```

- **HTTP proxy.** If the client has `http_proxy` set, LAN requests may go through
  it and fail. Add the address to `no_proxy`, or test with
  `curl --noproxy '*' http://<host>:8765/api/health`.

The port must also be reachable by **the agent itself** over loopback SSH: if
`sshd` is not listening on `127.0.0.1:22`, pass `--ssh-host`.

## Working inside an existing RELION project

Opening a project (`Open project`, or `?project=/path/to/project`) reads
`default_pipeline.star` and shows every job the official GUI would show, with
its status.

Submitting with a project loaded registers the job so the **official GUI can
read it back**:

- the job directory is placed at `<Type>/job<NNN>`, because the GUI derives the
  process type from that prefix;
- `default_pipeline.star` gains a `pipeline_processes` row, a `pipeline_nodes`
  row for the output, and input/output edges, all under the same
  `.relion_lock` the GUI uses;
- `.pipeline_has_changed` is touched after each write; `gui_mainwindow.cpp`
  checks for that file on its timer, so an open GUI refreshes its job list
  rather than waiting for its own poll;
- the job counter is advanced so the next GUI job does not collide;
- the status is written back as `Running`, then `Succeeded`/`Failed`/`Aborted`
  when it ends, in both the project pipeline and the job's mini-pipeline;
- `job.star` is written so the GUI can open the job and show its parameters,
  `RELION_OUTPUT_NODES.star` so it can re-check completion, `.Nodes` symlinks
  so downstream jobs can pick the output up, and `<jobname>job_pipeline.star` —
  the single-job mini-pipeline RELION keeps in every job directory, with the
  name and contents `PipeLine::addJob` produces.

The process type labels, node type labels and directory names come from
`src/pipeline_jobs.h` and live in `job_specs.py`. If you target a different
RELION version, check those constants first.

### Why this is hand-written rather than shelling out to `relion_pipeliner`

`relion_pipeliner --addJobFromStar` and `--RunJobs` do cover most of the above,
and calling them would be less code. The pipeline is written directly instead
because:

- `relion_pipeliner` must be on `PATH`; this module only needs the project
  directory, so the viewer works against data without a full RELION install;
- the web layer needs per-job observability (live log bytes, PID, cancellation
  semantics) that one blocking `--RunJobs` call does not expose;
- submissions stay synchronous, so the API can report exactly what it wrote and
  return the process name immediately.

The cost is that the formats here must track RELION's own writer. Changes are
validated against RELION's `MetaDataTable` parser (see the pipeline tests), and
the constants are pinned in `job_specs.py`.

Submitting without a project still runs the job, but it will not appear in the
GUI — the panel says so explicitly.

## What it does

- **Browse** tilt-series from a global STAR file. Only the global table is read
  up front, so a dataset with hundreds of tilt-series opens quickly.
- **Inspect** tilt images with tilt angle, defocus and pixel size, plus a defocus
  plot rendered server-side.
- **Render** tomogram slices along x/y/z and z-projections. Slices are read from a
  memory map, so a multi-gigabyte tomogram is never loaded into RAM; projections
  accumulate in z-slabs.
- **Select** tilt images and save `selected_tilt_series.star` in the same layout
  the desktop excluder produces.
- **Run every job the desktop tomography GUI offers** (23 windows, from import to
  post-processing), each with the same option form, in the same tabs, with the
  same defaults and help text. A window whose modes are mutually exclusive is
  offered as one job type per mode - `select_split`, `tomo_denoise_train`,
  `tomo_align_aretomo`, `localres_resmap` and so on - so each form asks only for
  the options its branch uses. Every variant still registers as its base job's
  process, so the desktop GUI sees one kind of job.
- **Create and manage projects**, draw the pipeline as a job/node graph, and
  follow a running job's log live.
- **See and edit nothing you cannot see in RELION**: jobs submitted here are
  registered in `default_pipeline.star` with the GUI's own directory names,
  process labels, node types, `job.star` and mini-pipeline, so the desktop GUI
  opens them and they can be continued or cleaned there.

## The security boundary

The server runs shell commands and reads files, so it is deliberately narrow:

- Every path from a client is resolved and must fall under one of the allowed
  roots (`--root`). Anything else is a `403`.
- Job types are limited to the fixed schema in `job_commands.py`; clients cannot
  supply an arbitrary executable. (`external` is the deliberate exception: it
  runs the program named in its `fn_exe` option, which is what that job type
  means in RELION, so it is only safe because the server is per-user.)
- It binds to `127.0.0.1` by default and has no authentication. Do not expose it
  to a network you do not control.

Because it writes the project pipeline, running it against a shared project that
someone else has open in the GUI is not advisable: the lock serialises the
writes, but RELION itself warns about concurrent writers.

## How jobs work

Jobs use RELION's own protocol rather than a new one. The command gets
`--pipeline_control <job_dir>`, the program writes
`RELION_JOB_EXIT_SUCCESS` / `_FAILURE` / `_ABORTED` into that directory, and
cancellation touches `RELION_JOB_ABORT_NOW`. Sentinel files are cleared before a
run and reconciled after it exits, so jobs submitted here behave the same as jobs
submitted from the desktop GUI.

Cancellation is cooperative first: the abort file is written and the process gets
a grace period before the process group is terminated.

Output goes to `run.out` and `run.err`, which is what the desktop GUI's I/O view
reads, and to `web_job.log`, which is what the live tail follows. The web log
holds both streams so the browser sees one ordered narrative.

A dry run ("Check command") returns the argv without creating a directory,
touching the pipeline, or advancing the job counter — a project must not gain a
process whose directory does not exist.

### Threads, MPI and queues

The Run tab carries `nr_threads`, `nr_mpi`, `do_queue` and the queue settings,
taken from `RelionJob::initialise()` rather than from the job's own options —
they are shared by every job and absent from `initialise*Job`, which is why a
job-only extraction misses them.

`job_queue.py` mirrors `prepareFinalCommand`:

- `nr_mpi > 1` substitutes the `_mpi` binary (14 jobs have one) and prefixes
  `mpirun -n N`; wrapping the serial program instead would start several full
  copies writing over each other;
- `do_queue` renders `run_submit.script` from the user's `qsubscript` template
  with RELION's placeholders and starts it with `qsub`;
- a queued job is reported `queued` / `Scheduled` until the sentinel appears,
  because the submit command's exit status only says the cluster accepted it.

Output is written to `run.out`/`run.err` by the agent rather than by a shell
redirect, so the live log and the GUI's I/O view see the same bytes.

### Job actions: what is RELION's and what is ours

Most of the "Job actions" menu already exists upstream, so it is delegated where a
command-line entry exists and reimplemented only where it does not:

- **Gentle and harsh clean** shell out to `relion_pipeliner
  --gentle_clean/--harsh_clean`. The per-job-type rules (which intermediates to
  keep for a possible continuation) are RELION's, and copied rules would rot.
- **Note** is `<jobdir>/<jobname>note.txt`, which the GUI's note editor opens, so
  reading and writing it needs no RELION support.
- **Alias, delete, undelete and mark-status** exist only as library calls
  (`PipeLine::setAliasJob`, `deleteJobGetNodesAndProcesses`, `undeleteJob`,
  `markAsFinishedJob`) with no `relion_pipeliner` flag, so they are implemented
  here against the pipeline file. The conventions they follow — the alias as a
  `<Type>/<alias>/` path with a symlink, `Trash/<Type>/jobNNN`, the sentinel
  files — are the GUI's, so the results stay readable both ways.

### Where the job definitions come from

There are 23 job windows and 645 options, offered as 34 job types because six
windows hold mutually exclusive modes (import, tilt-series alignment, denoising,
select, local resolution, ModelAngelo) that are split into one job type each.
Hand-copying their command lines out of `pipeline_jobs.cpp` would drift the first
time RELION changed an option, so the metadata is generated from RELION's own
sources:

```bash
python relion_webui/scripts/generate_web_job_schema.py   # rewrites generated_schema.py
```

`generated_schema.py` holds, per job, every option's label, default, range,
radio choices and help text, plus the tab layout taken from
`gui_jobwindow.cpp` and the pipeline constants (directory name, process label)
taken from `pipeline_jobs.h`. `test_web_schema.py` re-runs the extraction and
fails if the checked-in file is stale, so an upstream change cannot go unnoticed.

What the generator cannot supply is the argv translation, because it is code:
the same option becomes `--binned_angpix` for a C++ program and
`--tomogram-star-file` for a Python one, some flags only exist inside a
conditional, and several jobs prepend a subcommand. Those rules live in
`job_commands.py`, one builder per job, each annotated with the C++ function it
mirrors. `test_web_commands.py` checks the properties that made the earlier
generic builder produce unusable commands:

- every job passes its output flag (`--o` or `--output-directory`);
- Typer apps carry their subcommand (`SerialEM`, `cryoCARE:predict`, the pick
  mode, …);
- C++ programs get underscore flags — a hyphen there is only a warning, so the
  value would be silently ignored;
- integers are rendered without `.0`, which Typer's `int` options reject;
- false booleans are omitted, since the C++ treats presence as true.

## Live logs

`GET /api/jobs/{id}/log?offset=N` returns only bytes written past `N`. Over the
WebSocket, send `{"watch": "<job_id>", "offset": N}` and each push then carries a
`log` chunk. Chunks are 256 KB at most; when the server drops older bytes it sets
`truncated`, and the client resets its buffer.

## API

The UI is a static client over a JSON API; the same endpoints can be scripted.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/login` | `{"username", "password"}`; sets the session cookie. |
| `POST /api/logout` | Revoke the session. |
| `GET /api/session` | Who is signed in, and when the session expires. |
| `GET /api/health` | Liveness. No login required, so monitors work. |
| `GET /api/config` | Roots, job types, poll interval, whether login is required. |
| `GET /api/browse?path=&filter=` | Directory listing for the path pickers (`all`, `directory`, `project`, `star`). |
| `GET /api/browse/stat?path=` | Describe one path (exists, is a project, is a STAR file). |
| `GET /api/browse/locations` | Starting points for the picker. |
| `GET /api/projects` | The project list with job counts. |
| `POST /api/projects`, `DELETE /api/projects?path=` | Add or forget a project. |
| `GET /api/project?project_directory=` | Jobs, statuses, nodes and job counter from `default_pipeline.star`. |
| `GET /api/project/graph?project_directory=` | The pipeline as processes, nodes and edges, with each node's producer and consumers. |
| `GET /api/project/job-types` | Job types that can be registered in a project, with their inputs and outputs. |
| `POST /api/projects/create` | Create a new project directory (writes `default_pipeline.star`). |
| `GET /api/job-schema?job_type=` | Option form for one job, or all jobs: fields, defaults, tabs, field gates, required options. |
| `POST /api/job-validate` | Check a job's options against RELION's cross-option rules before submitting. |
| `POST /api/project/job/alias`, `GET`/`PUT /api/project/job/note` | Job actions: alias, note. |
| `POST /api/project/job/delete`, `GET /api/project/trash`, `POST /api/project/undelete` | Delete to `Trash/` and restore. |
| `POST /api/project/job/mark`, `POST /api/project/job/clean` | Mark finished/failed, gentle/harsh clean. |
| `POST /api/project/scheduler/run`, `POST /api/project/scheduler/stop`, `GET /api/project/scheduler` | Start, stop and poll RELION's scheduler. |
| `POST /api/project/export`, `POST /api/project/import` | Export scheduled jobs to `ExportJobs/<name>/`, import them elsewhere. |
| `GET /api/datasets?star_file=` | List tilt-series in a global STAR file. |
| `GET /api/tilt-series?star_file=&name=` | Per-image metadata for one tilt-series. |
| `GET /api/tilt-series/defocus-plot` | PNG defocus plot. |
| `GET /api/volume?path=` | MRC header, shape, voxel size, suggested contrast range. |
| `GET /api/render/slice?path=&axis=&index=` | PNG slice. |
| `GET /api/render/projection?path=&axis=z` | PNG mean projection. |
| `GET /api/render/tilt-image` | PNG tilt image. |
| `POST /api/tilt-series/selection` | Write `selected_tilt_series.star`. |
| `POST /api/jobs` | Submit a job (`dry_run` returns the argv without running). |
| `GET /api/jobs`, `GET /api/jobs/{id}` | List jobs / one job with log. |
| `GET /api/jobs/{id}/log?offset=` | Live log tail. |
| `DELETE /api/jobs/{id}` | Cancel a running job. |
| `DELETE /api/jobs/{id}?forget=true` | Drop a finished job from the list (files and pipeline entry keep). |
| `WS /ws/jobs` | Pushed job state, optional log subscription. |

Everything except the login flow and `/api/health` needs the session cookie;
the WebSocket closes with 1008 when it is missing or expired.

`POST /api/jobs` accepts
`{"job_type": ..., "output_directory": ..., "arguments": {...}, "project_directory": ...}`.
`arguments` uses the GUI's variable names (`in_tiltseries`, `xdim`,
`do_fourier`), which is also what `job.star` stores; the per-job builder maps
them to the program's flags. Options omitted from `arguments` are filled from
the schema defaults, so the resulting `job.star` always holds the complete
option set the GUI expects. With a project open, the job directory is chosen by
the backend as `<Type>/job<NNN>`, so `output_directory` may be left empty.

## Tests

```bash
cd relion_webui
uv pip install -e '.[dev]'
pytest
```

Three of those tests compare the generated tables against RELION's C++ and skip
when no checkout is available (`RELION_SOURCE`, or the parent directory).

Format compatibility is checked separately, against RELION's own parser:

```bash
relion_webui/scripts/relion_compat_check/run.sh
```

It compiles a small tool from `src/metadata_table.cpp` plus three writers, then
verifies that RELION reads everything the agent writes and that the agent reads
what RELION writes. Only a C++ compiler is needed - it does not build all of
RELION. See [COVERAGE.md](COVERAGE.md) for the four incompatibilities this
found.

`test_web_schema.py` and `test_web_commands.py` read RELION's own C++ sources
from the repository, so they have to run inside a checkout (the pip package
alone cannot regenerate the schema).

The API tests skip when `fastapi` is not installed. The SSH check itself is
tested against the local sshd and skipped when nothing listens on port 22; the
session, rate-limiting and project-list logic is covered without it. Note that
the existing `test_metadata_model.py` tests must be run from
`_tests/test_project_directory`, since they rely on STAR files with relative
paths.
