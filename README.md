# RELION web UI

A browser front end for RELION cryo-ET projects: browse tilt-series and
tomograms, submit any of the desktop GUI's job types, and follow the pipeline -
without building or installing the rest of RELION.

```
recryo/
├── backend/recryo/   the agent (FastAPI); a standalone Python package
├── frontend/               the React UI (Vite + Tailwind + daisyUI)
├── scripts/                one-command dev startup, the code generators,
│                           and the RELION-format compatibility check
├── tests/                  the agent's test suite
└── pyproject.toml          distribution "recryo"
```

This directory is self-contained so it can be copied to, or installed on, a
machine that already has RELION installed without touching that installation.

Optionally, an MCP server and an LLM assistant expose the same 17 tools over
streamable HTTP (`/mcp`) and in a side panel. Both are off by default.

## Run it

Needs [uv](https://docs.astral.sh/uv/) and [bun](https://bun.sh/) on `PATH`; the
scripts use them for the Python and JavaScript toolchains respectively.

```bash
uv venv                      # a virtualenv here, as dev_web.sh expects
uv pip install -e '.[dev]'   # backend + CLI, no RELION build needed
./scripts/dev_web.sh         # agent on :8765, UI on :5173, both with reload
```

`dev_web.sh` handles first-run setup (Python extras via `uv`, `bun install`), starts both
servers, and stops both on Ctrl-C - including the grandchildren that
`uvicorn --reload` and `bun run dev` spawn. Options:

```bash
./scripts/dev_web.sh -p /data/projects/xxx   # open a project at startup
./scripts/dev_web.sh --api-only              # agent only
./scripts/dev_web.sh --ui-only               # Vite only
./scripts/dev_web.sh --auth                  # require the sshd login
./scripts/dev_web.sh --host 0.0.0.0          # reachable from the LAN
```

In another terminal, `./scripts/dev_test.sh --quick` runs the agent's tests in a
couple of seconds; without `--quick` it also checks the generated tables, the
STAR format against RELION, and that the UI still builds (see "Tests" below).

Without `--auth` the agent binds loopback only and refuses to do otherwise: it
can submit jobs, so an unauthenticated instance belongs on 127.0.0.1 or behind a
tunnel.

## Install on a machine that has RELION

The agent needs RELION's *binaries* on `PATH` to run jobs, not RELION's Python
package and not a build tree:

```bash
uv pip install recryo    # or: uv pip install /path/to/recryo
recryo --host 127.0.0.1 --root /data/projects
```

`relion_pipeliner` is used for the clean and scheduler actions; when it is not
on `PATH` those actions say so and everything else still works.

The project list is kept in `$HOME/.relion_web/projects.json` - beside the
`$HOME/.relion` that RELION itself writes - so it is where you would look for
it. `--config-directory` or `RECRYO_HOME` point it somewhere else, which is
what you want on a shared machine or a read-only home. Older versions used
`~/.config/relion-web`; an existing list there is copied over on first start.

## What is generated, and from what

Three of the backend's modules are extracted from RELION's C++ so the forms and
command lines cannot drift from what the programs accept:

| File | Source |
| --- | --- |
| `generated_schema.py` | `src/pipeline_jobs.cpp`, `src/gui_jobwindow.cpp` |
| `generated_cli_options.py` | each program's own argument parser |
| `generated_commands.py` | `getCommands*` in `src/pipeline_jobs.cpp` |

Regenerate after changing RELION's sources:

```bash
python scripts/generate_web_job_schema.py
python scripts/generate_web_cli_options.py
python scripts/generate_web_job_commands.py
```

They read the RELION checkout at `RELION_SOURCE`, defaulting to the parent
directory. The generated output is committed, so an install without RELION's
sources still has every table.

## Tests

One command runs every check and prints one line per check, so a skip is
visible as a skip rather than looking like a pass:

```bash
uv pip install -e '.[dev]'
./scripts/dev_test.sh
```

```
STATUS CHECK                  DETAIL
------ ---------------------  ------------------------------
PASS   tests
PASS   generated              three tables in sync
PASS   format                 both directions
PASS   ui                     typechecked and built
PASS   ui-bundle              committed bundle is current

all checks passed
```

| Check | What it proves | Needs |
| --- | --- | --- |
| `tests` | the agent's pytest suite | the `[dev]` extras |
| `generated` | the three extracted tables match RELION's C++ | a RELION checkout |
| `format` | RELION reads the agent's STAR files and vice versa | checkout + `g++` |
| `ui` | the React app typechecks and builds | `node`/`npm` |
| `ui-bundle` | the committed bundle matches the build | `git` |

Useful variations:

```bash
./scripts/dev_test.sh --quick     # agent tests only (seconds)
./scripts/dev_test.sh --fix       # regenerate the tables, then check
./scripts/dev_test.sh --list      # what each check needs
./scripts/dev_test.sh ui          # one group: tests|generated|format|ui
```

The three drift checks skip when no RELION checkout is found (`RELION_SOURCE`,
defaulting to the parent directory), so the same command works in a checkout and
in a standalone clone. `ui-bundle` exists because the built bundle is committed:
a UI change that was never rebuilt, or a build that was never committed, would
otherwise ship a stale interface. `--quick` is what to run in a tight loop while
editing Python.

## The front end

```bash
cd frontend
bun install     # reads bun.lock
bun run dev     # hot reload, proxies /api and /ws to the agent
bun run build   # writes backend/recryo/static, which the agent serves
```

The built bundle is committed, so running the agent needs no JS toolchain at
all. `bun` is the project's toolchain; `npm` still works as a fallback if you
regenerate a lockfile from `package.json`, but the two lockfiles are not
interchangeable.

## Look at it without data

`scripts/make_demo_project.py` writes a small RELION project (10 jobs, real
`default_pipeline.star`, `job.star` and `run.out` in each job) so the canvas can
be exercised without real data:

```bash
python scripts/make_demo_project.py /tmp/relion-demo
./scripts/dev_web.sh -p /tmp/relion-demo
```

It goes through the same pipeline writer the agent uses, so what renders is what
the parser produced.

## Design notes

The interface design work - a study of a mature cryo-ET UI, what to adopt from
it, and the resulting design direction - is kept locally under `docs/` and is
not part of this repository. It informed the code rather than shipping with it.

The references in this README and in `frontend/src/index.css` point at those
local notes; they are deliberately absent from a clone.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE) for the copyright notice and the
reasoning, and [COPYING](COPYING) for the full text.

This project is a derivative work of [RELION](https://github.com/3dem/relion),
which is licensed GPL-2.0-or-later: the generated tables are extracted from
RELION's C++ sources and carry its option text, and the command builders port
its `getCommands*` functions. A permissive license is therefore not available.
"or later" rather than "2.0 only" because several dependencies
(`python-multipart`, and the OpenAI/Anthropic/Google SDKs in the assistant extra)
are Apache-2.0, which is compatible with GPL-3.0 but not with GPL-2.0-only.

The intent is that this could be offered to the RELION project as a web
platform, which is another reason the license matches RELION's exactly.
