# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""The tool surface, defined once and served two ways.

Both consumers of this module need the same capabilities: an MCP client (Claude
Desktop, an editor, another agent) and the in-browser assistant. Defining them
twice would let them drift - the assistant would learn about a capability the
MCP server does not expose, or worse, a write would be audited in one path and
not the other.

So a tool is a plain function with a JSON-schema-friendly signature, registered
in one table with its metadata (which project it needs, whether it writes, what
it costs). `mcp_server.py` turns the table into MCP tools; `assistant.py` turns
it into pydantic-ai tools. Neither knows anything about the other.

Read tools are safe to run in a loop. Write tools are the ones that change a
project, and they are explicitly marked, so a caller can gate them: the assistant
asks for confirmation, and the MCP client sees `destructiveHint` on them.

Everything goes through the same `ToolContext` - the path guard, the project
registry and the job manager the HTTP API uses - so a path outside the configured
roots is refused here exactly as it is there. There is no second security model.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from fastapi import HTTPException

from . import browse, job_actions, job_artifacts, job_commands, job_specs
from . import dataset as dataset_module
from . import pipeline as pipeline_module
from .paths import PathGuard
from .projects import ProjectRegistry


class ToolError(Exception):
    """A tool could not do what was asked; the message is for the user."""


@dataclass
class ToolContext:
    """What a tool is allowed to touch.

    Deliberately the same objects the HTTP routes use, not copies: a tool that
    resolves a path must hit the same guard that reads the current project list,
    or the two would disagree about what is reachable.
    """

    guard: PathGuard
    projects: ProjectRegistry
    jobs: Any  # JobManager; typed loosely to avoid an import cycle
    # JobQueue, eventually; also loose for the same reason. Optional so a test
    # can build a context without one and still use the read tools.
    queue: Any = None
    # Process names started in this session, which is what allows a stop.
    session_jobs: Callable[[], List[dict]] = field(
        default_factory=lambda: lambda: []
    )

    def project(self, path: str) -> Path:
        """Resolve a project directory, requiring a pipeline file."""
        if not path:
            raise ToolError('no project given; call list_projects first')
        directory = self.guard.resolve_existing(path, kind='directory')
        if not (directory / pipeline_module.PIPELINE_FILENAME).is_file():
            raise ToolError(
                f'{directory} is not a RELION project: no '
                f'{pipeline_module.PIPELINE_FILENAME}. Call list_projects for the '
                'projects this agent knows about.'
            )
        return directory

    def read_pipeline(self, path: str) -> pipeline_module.Pipeline:
        directory = self.project(path)
        try:
            return pipeline_module.read_pipeline(directory)
        except (FileNotFoundError, ValueError) as exc:
            raise ToolError(f'could not read the pipeline in {directory}: {exc}') from exc


# --------------------------------------------------------------------- schema


@dataclass(frozen=True)
class Tool:
    """One capability, with the metadata both front ends need.

    `writes` is not decoration. The assistant refuses to run a writing tool
    without a confirmation token, and the MCP server reports it as destructive,
    so a mistake here is a safety bug rather than a cosmetic one.
    """

    name: str
    description: str
    fn: Callable[..., Any]
    writes: bool = False
    # Whether the tool needs a `project` argument; the UI uses this to refuse a
    # call before it reaches the server when no project is open.
    needs_project: bool = False

    def schema(self) -> dict:
        return _json_schema(self.fn)


# Parameters the registry supplies itself, never the caller: the tool context,
# and callbacks a transport wires up (progress, streaming). They are stripped
# from the schema so a model is never asked to pass a function.
_INTERNAL_PARAMETERS = frozenset({'ctx', 'on_progress'})


def _json_schema(fn: Callable[..., Any]) -> dict:
    """A JSON schema for one tool's parameters.

    Built from the signature and the annotations, because the functions are
    ordinary Python: the registry stays readable and there is no third schema
    language to keep in sync. Only the types these tools actually use are
    handled, and an unsupported annotation fails loudly at registration rather
    than producing a schema an LLM would misuse.

    `get_type_hints` rather than `fn.__annotations__`: this module has
    `from __future__ import annotations`, so the raw annotations are source text
    (`'Optional[str]'`), not types.
    """
    import typing

    hints = typing.get_type_hints(fn)
    hints.pop('return', None)
    signature = inspect.signature(fn)
    properties: dict = {}
    required: List[str] = []

    for name, parameter in signature.parameters.items():
        if name in _INTERNAL_PARAMETERS:
            continue
        annotation = hints.get(name, str)
        origin = getattr(annotation, '__origin__', None)
        if origin is typing.Union and type(None) in getattr(annotation, '__args__', ()):
            annotation = _strip_optional(annotation)
            origin = getattr(annotation, '__origin__', None)

        if annotation is bool:
            spec: dict = {'type': 'boolean'}
        elif annotation is int:
            spec = {'type': 'integer'}
        elif annotation is float:
            spec = {'type': 'number'}
        elif annotation is str:
            spec = {'type': 'string'}
        elif origin in (list, List) or annotation is list:
            args = getattr(annotation, '__args__', (str,))
            item = args[0] if args else str
            item_type = {
                str: 'string', int: 'integer', float: 'number', bool: 'boolean'
            }
            spec = {'type': 'array', 'items': {'type': item_type.get(item, 'string')}}
        elif origin in (dict, Dict) or annotation is dict:
            spec = {'type': 'object'}
        else:
            raise ToolError(
                f'{fn.__name__}: unsupported annotation for {name!r}: {annotation!r}'
            )

        # A parameter is required unless it has a default. `None` as a default
        # means "optional, may be omitted", which is how these tools spell it.
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        elif parameter.default is not None:
            spec['default'] = parameter.default
        properties[name] = spec

    return {
        'type': 'object',
        'properties': properties,
        'required': required,
        'additionalProperties': False,
    }


def _strip_optional(annotation):
    args = [a for a in getattr(annotation, '__args__', ()) if a is not type(None)]
    return args[0] if args else str


# ---------------------------------------------------------------- read tools


def list_projects(ctx: ToolContext) -> dict:
    """List the RELION projects the agent knows about, with job counts.

    Start here: every other tool takes one of these paths as `project`.
    """
    entries = ctx.projects.summarize()
    return {
        'projects': [
            {
                'path': entry.get('path'),
                'name': entry.get('name'),
                'readable': entry.get('readable'),
                'job_count': entry.get('job_count'),
                'statuses': entry.get('statuses'),
                'types': entry.get('types'),
                'recent': entry.get('recent'),
            }
            for entry in entries
        ]
    }


def get_pipeline(ctx: ToolContext, project: str, include_nodes: bool = True) -> dict:
    """The project's pipeline: every job, its status, and how data flows.

    This is the project's shape. `processes` are the jobs; `nodes` are the data
    files; `edges` say which job consumed and produced which node. Use it to
    answer "what ran, in what order, and what feeds what" before suggesting a
    next job. Set `include_nodes=False` when you only need statuses: the node
    and edge lists grow with the project and are most of the payload.
    """
    graph = _graph(ctx, project)
    payload = {
        'project': project,
        'job_counter': graph['job_counter'],
        'processes': graph['processes'],
        'summary': _status_summary(graph['processes']),
    }
    if include_nodes:
        payload['nodes'] = graph['nodes']
        payload['edges'] = graph['edges']
    return payload


def _graph(ctx: ToolContext, project: str) -> dict:
    """The same shape /api/project/graph returns, built from the pipeline."""
    directory = ctx.project(project)
    pipeline = ctx.read_pipeline(project)
    produced_by = {node: name for name, node in pipeline.output_edges}
    consumed_by: Dict[str, List[str]] = {}
    for node, name in pipeline.input_edges:
        consumed_by.setdefault(node, []).append(name)
    return {
        'project_directory': str(directory),
        'job_counter': pipeline.job_counter,
        'processes': [
            {
                'name': process.name,
                'alias': process.alias,
                'type_label': process.type_label,
                'status': process.status,
                'job_number': pipeline.job_number_of(process.name),
                'directory': pipeline.directory_of(process.name).rstrip('/'),
            }
            for process in pipeline.processes
        ],
        'nodes': [
            {
                'name': node.name,
                'type_label': node.type_label,
                'produced_by': produced_by.get(node.name),
                'consumed_by': consumed_by.get(node.name, []),
            }
            for node in pipeline.nodes
        ],
        'edges': {
            'inputs': [{'node': node, 'process': name} for node, name in pipeline.input_edges],
            'outputs': [{'process': name, 'node': node} for name, node in pipeline.output_edges],
        },
    }


def _status_summary(processes: Sequence[dict]) -> dict:
    counts: Dict[str, int] = {}
    for process in processes:
        status = (process.get('status') or 'unknown').lower()
        counts[status] = counts.get(status, 0) + 1
    return {
        'total': len(processes),
        'by_status': counts,
        'failing': [
            p['name'] for p in processes if (p.get('status') or '').lower() == 'failed'
        ],
        'running': [
            p['name'] for p in processes if (p.get('status') or '').lower() == 'running'
        ],
    }


def get_job(ctx: ToolContext, project: str, process_name: str) -> dict:
    """Everything recorded about one job: status, options, note, log tail.

    The options come from the job's own job.star, so they are what it actually
    ran with rather than today's defaults. The log is the tail of run.out.
    """
    pipeline = ctx.read_pipeline(project)
    process = pipeline.find_process(process_name)
    if process is None:
        known = ', '.join(p.name for p in pipeline.processes[:12])
        raise ToolError(f'{process_name} is not in this project. Jobs include: {known}')

    job_directory = ctx.guard.resolve_existing(
        str(ctx.project(project) / process_name), kind='directory'
    )
    # Ask job_actions where the note lives rather than rebuilding the path:
    # it is inside the job directory, and the desktop GUI's own name for it is
    # the one thing both sides must agree on.
    note = None
    note_path = job_actions.note_path(ctx.project(project), process_name)
    if note_path.is_file():
        try:
            note = note_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            note = None

    return {
        'process_name': process.name,
        'alias': process.alias,
        'type_label': process.type_label,
        'status': process.status,
        'directory': str(job_directory),
        'job_number': pipeline.job_number_of(process.name),
        'options': job_artifacts.read_job_star(job_directory),
        'note': note,
        'log': job_artifacts.read_run_out(job_directory),
        'inputs': [
            node for node, name in pipeline.input_edges if name == process.name
        ],
        'outputs': [
            node for name, node in pipeline.output_edges if name == process.name
        ],
    }


def get_job_log(
    ctx: ToolContext,
    project: str,
    process_name: str,
    tail_lines: int = 80,
) -> dict:
    """The end of a job's log, for diagnosing a failure.

    `tail_lines` caps what comes back; the log of a long refinement is large and
    the useful part is at the end.
    """
    job_directory = ctx.guard.resolve_existing(
        str(ctx.project(project) / process_name), kind='directory'
    )
    text = job_artifacts.read_run_out(job_directory)
    if text is None:
        raise ToolError(f'{process_name} wrote no run.out')
    lines = text.splitlines()
    limit = max(1, min(int(tail_lines), 2000))
    return {
        'process_name': process_name,
        'lines_total': len(lines),
        'shown': min(limit, len(lines)),
        'log': '\n'.join(lines[-limit:]),
    }


def wait_for_job(
    ctx: ToolContext,
    job_id: str,
    timeout_seconds: float = 900.0,
    poll_seconds: float = 5.0,
    on_progress: Optional[Callable[[float, float, str], None]] = None,
) -> dict:
    """Block until a job finishes, or until `timeout_seconds` passes.

    This is what makes an agent loop possible: after `submit_job` the caller
    must wait, and calling `get_job` in a loop burns one context window per poll.
    One call here replaces twenty of those.

    Returns `finished: true` with the final status and log tail when the job
    ends, or `finished: false` with how long it has been running when the timeout
    is reached - then call again to keep waiting. The job keeps running either
    way; waiting is not owning.

    `on_progress` is called with `(elapsed, timeout, message)` on every poll so a
    transport can forward progress to its client.
    """
    import time

    job = ctx.jobs.get(job_id)
    if job is None:
        raise ToolError(
            f'{job_id} is not a job in this session. Jobs started by the scheduler '
            'are not visible here; poll get_pipeline instead.'
        )

    limit = max(1.0, float(timeout_seconds))
    interval = max(0.2, min(float(poll_seconds), 60.0))
    started = time.monotonic()
    deadline = started + limit

    while True:
        # Re-fetch on every poll rather than holding the first object: a manager
        # may hand back an immutable snapshot per call, and a cached one would
        # report "running" forever.
        job = ctx.jobs.get(job_id)
        if job is None:
            raise ToolError(f'{job_id} disappeared while waiting')
        status = job.status
        elapsed = time.monotonic() - started
        if status not in ('running', 'queued'):
            return {
                'job_id': job_id,
                'finished': True,
                'status': status,
                'returncode': job.returncode,
                'elapsed_seconds': round(elapsed, 1),
                'error': job.error,
                'log': _tail(job_id, ctx),
            }
        if time.monotonic() >= deadline:
            return {
                'job_id': job_id,
                'finished': False,
                'status': status,
                'elapsed_seconds': round(elapsed, 1),
                'waited_seconds': round(limit, 1),
                'log': _tail(job_id, ctx, lines=20),
                'hint': (
                    'still running; call wait_for_job again to keep waiting, or '
                    'get_job_log to see progress'
                ),
            }
        if on_progress is not None:
            on_progress(
                elapsed,
                limit,
                f'{job_id} is {status} ({_duration(elapsed)} of at most '
                f'{_duration(limit)})',
            )
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def _tail(job_id: str, ctx: ToolContext, lines: int = 40) -> str:
    """The last few lines of a running job's log, for the wait result."""
    try:
        text = ctx.jobs.tail_log(job_id)
    except (KeyError, AttributeError, OSError):
        return ''
    return '\n'.join(text.splitlines()[-lines:])


def _duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours}h {minutes:02d}m'
    if minutes:
        return f'{minutes}m {secs:02d}s'
    return f'{secs}s'


def get_job_metrics(ctx: ToolContext, project: str, process_name: str) -> dict:
    """The numbers a job produced, for deciding whether to continue.

    A log says whether a job succeeded; it does not say whether the result is
    *usable*. After CTF estimation you need to know if the defocus range is sane;
    after refinement, the resolution; after picking, how many particles. Those
    live in STAR files, and reading them is the difference between an agent that
    chains jobs and one that can tell a good result from a bad one.

    Returns the metrics this job type reports, plus the log's timing lines. An
    unknown job type returns `metrics: {}` with a note rather than an error: not
    every job produces a number worth checking.
    """
    pipeline = ctx.read_pipeline(project)
    process = pipeline.find_process(process_name)
    if process is None:
        raise ToolError(f'{process_name} is not in this project')

    job_directory = ctx.guard.resolve_existing(
        str(ctx.project(project) / process_name), kind='directory'
    )
    payload: dict = {
        'process_name': process_name,
        'type_label': process.type_label,
        'status': process.status,
        'metrics': {},
        'notes': [],
    }

    kind = process.type_label.rsplit('.', 1)[-1]
    metrics = payload['metrics']

    if kind in ('ctffind', 'tomo_ctffind'):
        metrics.update(_defocus_metrics(job_directory, payload['notes']))
    elif kind in ('reconstructtomograms', 'tomo_reconstruct_tomograms'):
        metrics.update(_tomogram_metrics(job_directory, payload['notes']))
    elif kind in ('pseudosubtomo', 'tomo_subtomo'):
        metrics.update(_particle_metrics(job_directory, payload['notes']))
    elif kind in ('postprocess',):
        metrics.update(_resolution_metrics(job_directory, payload['notes']))
    elif kind in ('class3d', 'refine3d', 'autorefine'):
        metrics.update(_resolution_metrics(job_directory, payload['notes']))

    run_out = job_artifacts.read_run_out(job_directory)
    if run_out:
        payload['timing'] = _timing_lines(run_out)
    return payload


def _read_star(path: Path) -> Optional[dict]:
    """Read a STAR file into ``{table_name: DataFrame}``, or None."""
    import starfile

    if not path.is_file():
        return None
    try:
        tables = starfile.read(path, always_dict=True)
    except (OSError, ValueError):
        return None
    return tables if isinstance(tables, dict) else None


def _numeric_column(table, *candidates: str):
    """The first column present among ``candidates``, as floats."""
    import pandas as pd

    for name in candidates:
        if name in table.columns:
            series = pd.to_numeric(table[name], errors='coerce').dropna()
            if len(series):
                return series
    return None


def _defocus_metrics(job_directory: Path, notes: List[str]) -> dict:
    """Defocus statistics from a CTF job's STAR output.

    The per-image defocus is what tells you whether CTF estimation found
    anything: all-zero or a single outlier usually means the search range or the
    pixel size is wrong, not that the images are perfect.
    """
    for name in ('micrographs_ctf.star', 'tilt_series_ctf.star', 'particles.star'):
        tables = _read_star(job_directory / name)
        if not tables:
            continue
        for table in tables.values():
            values = _numeric_column(table, 'rlnDefocusU', 'rlnDefocus')
            if values is None:
                continue
            return {
                'n_images': int(len(values)),
                'defocus_min': round(float(values.min()), 1),
                'defocus_max': round(float(values.max()), 1),
                'defocus_median': round(float(values.median()), 1),
                'units': 'angstrom',
            }
    notes.append('no CTF STAR file with rlnDefocusU found in the job directory')
    return {}


def _tomogram_metrics(job_directory: Path, notes: List[str]) -> dict:
    """How many tomograms were reconstructed, and at what binning."""
    tables = _read_star(job_directory / 'tomograms.star')
    if not tables:
        notes.append('no tomograms.star in the job directory')
        return {}
    for table in tables.values():
        count = len(table)
        metrics = {'n_tomograms': int(count)}
        binning = _numeric_column(table, 'rlnTomoTomogramBinning')
        if binning is not None:
            metrics['binning'] = float(binning.iloc[0])
        size = _numeric_column(table, 'rlnTomoSizeX')
        if size is not None:
            metrics['size_x'] = int(size.iloc[0])
        return metrics
    return {}


def _particle_metrics(job_directory: Path, notes: List[str]) -> dict:
    """Particle count from an extraction job - the number that gates averaging."""
    tables = _read_star(job_directory / 'particles.star')
    if not tables:
        notes.append('no particles.star in the job directory')
        return {}
    for table in tables.values():
        return {'n_particles': int(len(table))}
    return {}


def _resolution_metrics(job_directory: Path, notes: List[str]) -> dict:
    """Reported resolution, which is what a refinement is run to find out."""
    import pandas as pd

    candidates = ('postprocess.star', 'final_resolution.star')
    for name in candidates:
        tables = _read_star(job_directory / name)
        if not tables:
            continue
        for table in tables.values():
            value = None
            for column in table.columns:
                if 'resolution' in column.lower():
                    series = pd.to_numeric(table[column], errors='coerce').dropna()
                    if len(series):
                        value = float(series.iloc[-1])
                        break
            if value is not None:
                return {'resolution_angstrom': round(value, 2)}
    notes.append('no postprocess.star with a resolution column found')
    return {}


def _timing_lines(run_out: str) -> List[str]:
    """The ``Finished in`` / ``Elapsed`` lines RELION prints, verbatim."""
    keep = []
    for line in run_out.splitlines():
        lowered = line.lower()
        if 'finished in' in lowered or 'elapsed' in lowered or 'wall clock' in lowered:
            keep.append(line.strip())
    return keep[-3:]


def list_hosts(ctx: ToolContext, check: bool = False) -> dict:
    """The machines a job can run on.

    `local` is the agent's own machine and is always available. A named node runs
    the job over ssh; the project is on shared storage, so nothing is copied.
    With `check` each remote node is contacted once, which reports a wrong
    hostname or a missing ssh key now rather than at submission.
    """
    from . import hosts as hosts_module

    registry = getattr(ctx.jobs, 'hosts', None) or hosts_module.HostRegistry()
    entries = []
    for host in registry.list():
        entry = host.as_dict()
        if check:
            ok, message = hosts_module.check_reachable(host)
            entry['reachable'] = ok
            if not ok:
                entry['problem'] = message
        entries.append(entry)
    return {
        'hosts': entries,
        'default': hosts_module.LOCAL_NAME,
        'note': (
            'remote nodes need key-based ssh from the agent account; the browser '
            'password is never stored'
        ),
    }


def list_job_types(ctx: ToolContext) -> dict:
    """The job types this agent can build, grouped by workflow stage."""
    from . import job_commands as commands

    categories = commands.job_categories()
    return {
        'job_types': sorted(commands.SCHEMA_JOBS),
        'categories': [
            {'title': category['title'], 'job_types': category['job_types']}
            for category in categories
        ],
    }


def get_job_options_schema(
    ctx: ToolContext,
    job_type: str,
    variables: Optional[List[str]] = None,
    with_help: bool = True,
) -> dict:
    """The form for one job type: every option, its type, default and help.

    Call this before submitting a job; the returned `variable` names are the
    keys `submit_job`'s `arguments` takes. Pass `variables` to fetch only the
    options you are about to set. A large job has 70 of them and the help text
    is most of the payload, so `with_help=False` trims it when you only need
    names and defaults.
    """
    try:
        schema = job_commands.form_schema(job_type)
    except KeyError as exc:
        raise ToolError(f'unknown job type {job_type!r}') from exc

    required = schema.get('required', [])
    wanted = set(variables) if variables else None
    fields = []
    for field in schema.get('fields', []):
        if wanted is not None and field['variable'] not in wanted:
            continue
        entry = {
            'variable': field['variable'],
            'label': field.get('label', '').rstrip(':'),
            'kind': field.get('kind'),
            'required': field['variable'] in required,
        }
        if field.get('default') is not None:
            entry['default'] = field['default']
        if field.get('choices'):
            entry['choices'] = field['choices']
        if with_help and field.get('help'):
            entry['help'] = field['help']
        fields.append(entry)

    return {
        'job_type': job_type,
        'title': schema.get('title'),
        'executable': schema.get('executable'),
        'n_fields': len(schema.get('fields', [])),
        'fields': fields,
        'required': required,
    }


def validate_job(
    ctx: ToolContext,
    job_type: str,
    arguments: dict,
    check_inputs: bool = True,
) -> dict:
    """Check a job's options without submitting it.

    Returns the cross-option problems RELION would refuse the job for, the
    missing required options, and - with `check_inputs` - any input file that
    does not exist yet.

    That last check matters for an agent chaining jobs: option validation cannot
    know whether an upstream job has finished, so without it a downstream job is
    submitted against a file that is still being written, runs for a while, and
    fails. Checking existence turns that into an immediate answer. Path options
    are only checked when they are plain filesystem paths; a pipeline node name
    is resolved by RELION, not here.
    """
    try:
        schema = job_commands.form_schema(job_type)
    except KeyError as exc:
        raise ToolError(f'unknown job type {job_type!r}') from exc

    required = schema.get('required', [])
    missing = [
        variable for variable in required if not _present(arguments.get(variable))
    ]
    problems = job_commands.validate(job_type, dict(arguments))

    absent: List[dict] = []
    if check_inputs:
        for field in schema.get('fields', []):
            variable = field['variable']
            if field.get('kind') not in ('path', 'input_node'):
                continue
            value = arguments.get(variable)
            if not _present(value):
                continue
            text = str(value)
            # A RELION node name (`Tomograms/job005/tomograms.star`) is relative
            # to the project, and the guard would refuse it as an unknown path;
            # only absolute or home-relative values are checked here.
            if not (text.startswith('/') or text.startswith('~') or text.startswith('.')):
                continue
            try:
                path = ctx.guard.resolve(text)
            except HTTPException as exc:
                absent.append(
                    {
                        'variable': variable,
                        'value': text,
                        'reason': str(getattr(exc, 'detail', exc)),
                    }
                )
                continue
            if not path.exists():
                absent.append(
                    {'variable': variable, 'value': text, 'reason': 'does not exist'}
                )

    return {
        'job_type': job_type,
        'valid': not missing and not problems and not absent,
        'missing_required': missing,
        'problems': problems,
        'missing_inputs': absent,
    }


def list_tilt_series(ctx: ToolContext, star_file: str) -> dict:
    """The tilt-series listed by a STAR file, without opening each one."""
    path = ctx.guard.resolve_existing(star_file)
    try:
        infos = dataset_module.list_tilt_series(path)
    except (ValueError, OSError) as exc:
        raise ToolError(f'could not read {path}: {exc}') from exc
    return {
        'star_file': str(path),
        'n_tilt_series': len(infos),
        'tilt_series': [info.as_dict() for info in infos],
    }


def browse_directory(
    ctx: ToolContext,
    path: Optional[str] = None,
    filter: str = 'all',
    show_hidden: bool = False,
) -> dict:
    """List a directory on the machine running RELION.

    Use it to find inputs (a STAR file, a project directory) without guessing at
    paths. `filter` is one of all, directory, project, star.
    """
    if filter not in ('all', 'directory', 'project', 'star'):
        raise ToolError(f'unknown filter {filter!r}')
    if path:
        start = ctx.guard.resolve_existing(path, kind='directory')
    else:
        start = Path(browse.default_start_directory())
    try:
        listing = browse.list_directory(
            str(start), wanted=filter, include_hidden=show_hidden
        )
    except (OSError, ValueError) as exc:
        raise ToolError(f'could not list {start}: {exc}') from exc
    return listing.as_dict()


# --------------------------------------------------------------- write tools


def submit_job(
    ctx: ToolContext,
    project: str,
    job_type: str,
    arguments: dict,
    dry_run: bool = False,
    host: Optional[str] = None,
) -> dict:
    """Build and queue a job in the project.

    `arguments` maps option variables to values; call get_job_options_schema
    first to see them. With `dry_run` the command is built and returned but
    nothing is written or started - use that to show the user what would run.

    `host` names the machine to run on; omit it for the agent's own machine.
    Call list_hosts to see the names. Projects live on shared storage, so the
    job directory has the same path on every node.
    """
    import anyio

    directory = ctx.project(project)
    try:
        schema = job_commands.form_schema(job_type)
    except KeyError as exc:
        raise ToolError(f'unknown job type {job_type!r}') from exc

    missing = [
        variable
        for variable in schema.get('required', [])
        if not _present(arguments.get(variable))
    ]
    if missing:
        raise ToolError(f'missing required options: {", ".join(missing)}')
    problems = job_commands.validate(job_type, dict(arguments))
    if problems:
        raise ToolError('; '.join(problems))

    # Submit does the same input check, so an agent cannot skip validate_job and
    # land in the submit-then-fail loop this guards against.
    checked = validate_job(ctx, job_type, arguments, check_inputs=True)
    if checked['missing_inputs']:
        described = ', '.join(
            f'{item["variable"]}={item["value"]} ({item["reason"]})'
            for item in checked['missing_inputs']
        )
        raise ToolError(
            f'input not available yet: {described}. If an upstream job produces '
            'it, wait_for_job on that job first'
        )

    async def run():
        return await ctx.jobs.submit(
            job_type,
            dict(arguments),
            directory,
            project_directory=directory,
            dry_run=dry_run,
            host=host,
        )

    job = anyio.from_thread.run(run) if _in_worker_thread() else anyio.run(run)
    return {
        'job_id': job.id,
        'process_name': job.process_name,
        'status': job.status,
        'command': job.command,
        'output_directory': str(job.output_directory),
        'host': job.host,
        'dry_run': dry_run,
        'error': job.error,
    }


def _in_worker_thread() -> bool:
    """Whether a tool is running somewhere `anyio.run` would raise.

    pydantic-ai runs sync tools in a worker thread; the MCP server may too. In a
    worker there is already a loop, so a new one has to be started on that
    thread rather than nested.
    """
    import threading

    return threading.current_thread() is not threading.main_thread()


def _queue(ctx: ToolContext):
    queue = ctx.queue
    if queue is None:
        raise ToolError('no job queue is running in this agent')
    return queue


def queue_job(
    ctx: ToolContext,
    project: str,
    job_type: str,
    arguments: dict,
    host: Optional[str] = None,
    gpus: Optional[List[int]] = None,
    gpu_memory_mb: Optional[int] = None,
    priority: int = 0,
    exclusive: bool = False,
    depends_on: Optional[List[str]] = None,
    after_jobs: Optional[List[str]] = None,
    inputs: Optional[Dict[str, str]] = None,
) -> dict:
    """Put a job on the queue instead of starting it now.

    This is what makes a multi-step pipeline one call: queue every stage, and
    each starts when its input exists and a GPU is free. Dependencies are
    derived from the arguments - an input that names a STAR file produced by
    another job becomes a wait for that job automatically - so
    `depends_on`/`after_jobs` are only needed for a wait the options do not
    express.

    `inputs` is how a stage is queued before its input exists: name the entry
    and one of its output files, as `{"in_tiltseries": "q0003:tomograms.star"}`.
    The queue fills in the real path and waits for that entry, because a queued
    job already has a fixed number and therefore predictable output paths.

    Differs from `submit_job` in three ways: it never starts immediately, it can
    wait for a GPU, and it returns an entry id (`q0007`) rather than a job id.
    Use `list_queue` to watch it; the entry reports `blocked_by` when it is not
    starting, in plain language.

    `gpus` pins device indices and leases them exclusively - no other queued job
    gets those cards. Leave it empty to let the queue choose a free card, which
    is the right default. `exclusive` means "nothing else on this node while I
    run", for a job that saturates it.

    `gpu_memory_mb` is the GPU memory the job needs per card. RELION's options
    carry no memory figure, so set it only when you know the job needs a large
    card (a big box size, many particles); the queue then waits for a card that
    big instead of starting on a too-small one and failing.
    """
    result = _queue(ctx).add(
        project,
        job_type,
        dict(arguments),
        host=host,
        gpus=gpus or (),
        gpu_memory_mb=gpu_memory_mb,
        priority=priority,
        exclusive=exclusive,
        depends_on=depends_on or (),
        after_jobs=after_jobs or (),
        inputs=inputs or {},
    )
    return result.as_dict()


def list_queue(ctx: ToolContext, project: Optional[str] = None) -> dict:
    """The queue: every entry, its state, and why a waiting entry is waiting.

    Use this instead of polling `get_pipeline` while a batch runs: a pending
    entry says `blocked_by: ["waiting for Tomograms/job005 (running)"]` or
    `["needs 4 cores; 0 free on gpu1"]`, which is the fact you would otherwise
    have to infer from process lists and node metrics.
    """
    return _queue(ctx).snapshot(project)


def cancel_queued_job(ctx: ToolContext, entry_id: str) -> dict:
    """Drop an entry from the queue, or stop it if it has already started.

    A running entry is stopped through RELION's own abort handshake, and its
    GPUs are released for the next entry.
    """
    import anyio

    queue = _queue(ctx)

    async def run():
        return await queue.cancel(entry_id)

    entry = anyio.from_thread.run(run) if _in_worker_thread() else anyio.run(run)
    return entry.as_dict()


def retry_queued_job(ctx: ToolContext, entry_id: str) -> dict:
    """Queue a finished entry again as a new job.

    The entry keeps its project, job type and arguments, so this is the right
    way to re-run a stage after fixing an input, rather than rebuilding it.
    """
    return _queue(ctx).retry(entry_id).as_dict()


def set_job_alias(ctx: ToolContext, project: str, process_name: str, alias: str) -> dict:
    """Rename a job's alias, which is what downstream jobs call its output."""
    result = job_actions.set_alias(ctx.project(project), process_name, alias)
    return _action_dict(result)


def write_job_note(ctx: ToolContext, project: str, process_name: str, note: str) -> dict:
    """Write the note RELION's own job window shows for this job."""
    result = job_actions.write_note(ctx.project(project), process_name, note)
    return _action_dict(result)


def mark_job(
    ctx: ToolContext,
    project: str,
    process_name: str,
    failed: bool = False,
) -> dict:
    """Set a job's pipeline status to failed (or back to finished).

    Useful when a job crashed in a way RELION could not detect, so downstream
    jobs stay blocked instead of running on empty output.
    """
    result = job_actions.mark_status(ctx.project(project), process_name, failed=failed)
    return _action_dict(result)


def list_trash(ctx: ToolContext, project: str) -> dict:
    """Jobs that were deleted from the pipeline and can be restored.

    Each entry is a `trash_entry` name that `undelete_job` takes.
    """
    result = job_actions.list_trash(ctx.project(project))
    return _action_dict(result)


def delete_job(ctx: ToolContext, project: str, process_name: str) -> dict:
    """Remove a job from the pipeline, moving its directory to Trash/.

    Recoverable: the files are moved, not erased, and `undelete_job` brings the
    job back. Call `list_trash` to find the entry name for the restore.
    """
    result = job_actions.delete(ctx.project(project), process_name)
    return _action_dict(result)


def undelete_job(ctx: ToolContext, project: str, trash_entry: str) -> dict:
    """Restore a job from Trash/ into the pipeline.

    `trash_entry` is the name `list_trash` reports, e.g. `Tomograms/job004`.
    """
    result = job_actions.undelete(ctx.project(project), trash_entry)
    return _action_dict(result)


def stop_job(ctx: ToolContext, job_id: str) -> dict:
    """Stop a job that this session is running.

    Only jobs started from this agent can be stopped here; a job started by the
    scheduler is stopped through the scheduler.
    """
    import anyio

    async def run():
        return await ctx.jobs.cancel(job_id)

    stopped = anyio.from_thread.run(run) if _in_worker_thread() else anyio.run(run)
    if not stopped:
        raise ToolError(f'{job_id} is not a running job in this session')
    return {'job_id': job_id, 'stopped': True}


def _action_dict(result: job_actions.ActionResult) -> dict:
    return {
        'action': result.action,
        'process_name': result.process_name,
        'message': result.message,
        'detail': result.detail or {},
    }


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


# ------------------------------------------------------------------ registry


def build_registry() -> List[Tool]:
    """The tools, in the order a reader should meet them.

    Ordered read-first: discovery, then the project, then one job, then the
    build path, then the writes. An agent that stops after the reads has done
    nothing harmful, which is the right default for an LLM at the wheel.
    """
    return [
        Tool('list_projects', list_projects.__doc__ or '', list_projects),
        Tool('browse_directory', browse_directory.__doc__ or '', browse_directory),
        Tool('list_job_types', list_job_types.__doc__ or '', list_job_types),
        Tool('list_hosts', list_hosts.__doc__ or '', list_hosts),
        Tool('get_pipeline', get_pipeline.__doc__ or '', get_pipeline, needs_project=True),
        Tool('get_job', get_job.__doc__ or '', get_job, needs_project=True),
        Tool('get_job_log', get_job_log.__doc__ or '', get_job_log, needs_project=True),
        Tool(
            'list_tilt_series',
            list_tilt_series.__doc__ or '',
            list_tilt_series,
        ),
        Tool(
            'get_job_metrics',
            get_job_metrics.__doc__ or '',
            get_job_metrics,
            needs_project=True,
        ),
        Tool(
            'get_job_options_schema',
            get_job_options_schema.__doc__ or '',
            get_job_options_schema,
        ),
        Tool('validate_job', validate_job.__doc__ or '', validate_job),
        Tool(
            'submit_job',
            submit_job.__doc__ or '',
            submit_job,
            writes=True,
            needs_project=True,
        ),
        # Waiting is not a write: it changes nothing and can be interrupted by
        # its own timeout. It sits after submit_job because that is when an
        # agent needs it.
        Tool('wait_for_job', wait_for_job.__doc__ or '', wait_for_job),
        # The queue is the batch path: submit everything, then watch one place
        # instead of polling each job. Listing is a read; the rest write.
        Tool('list_queue', list_queue.__doc__ or '', list_queue),
        Tool(
            'queue_job',
            queue_job.__doc__ or '',
            queue_job,
            writes=True,
            needs_project=True,
        ),
        Tool(
            'cancel_queued_job',
            cancel_queued_job.__doc__ or '',
            cancel_queued_job,
            writes=True,
        ),
        Tool(
            'retry_queued_job',
            retry_queued_job.__doc__ or '',
            retry_queued_job,
            writes=True,
        ),
        Tool(
            'set_job_alias',
            set_job_alias.__doc__ or '',
            set_job_alias,
            writes=True,
            needs_project=True,
        ),
        Tool(
            'write_job_note',
            write_job_note.__doc__ or '',
            write_job_note,
            writes=True,
            needs_project=True,
        ),
        Tool(
            'mark_job',
            mark_job.__doc__ or '',
            mark_job,
            writes=True,
            needs_project=True,
        ),
        Tool('list_trash', list_trash.__doc__ or '', list_trash, needs_project=True),
        Tool(
            'delete_job',
            delete_job.__doc__ or '',
            delete_job,
            writes=True,
            needs_project=True,
        ),
        Tool(
            'undelete_job',
            undelete_job.__doc__ or '',
            undelete_job,
            writes=True,
            needs_project=True,
        ),
        Tool('stop_job', stop_job.__doc__ or '', stop_job, writes=True),
    ]


def call_tool(
    registry: Sequence[Tool],
    name: str,
    arguments: dict,
    ctx: ToolContext,
    *,
    allow_writes: bool = True,
) -> Any:
    """Run one tool by name, turning its errors into one shape.

    The callers are a JSON-RPC handler and an LLM loop, and both need a
    structured failure rather than a traceback: `{"error": "..."}` is something
    the model can read and correct from.

    `allow_writes` is enforced here rather than in each caller because this is
    the one place every invocation passes through. The assistant has its own
    approval gate on top (a write parks for a human), but a caller with no human
    in the loop - an MCP client - needs the blunt version: refuse. Putting it at
    the choke point means a new transportation surface inherits it instead of
    having to remember it.
    """
    tool = next((entry for entry in registry if entry.name == name), None)
    if tool is None:
        return {'error': f'unknown tool {name!r}', 'available': [t.name for t in registry]}
    if tool.writes and not allow_writes:
        return {
            'error': (
                f'{name} changes the project, and this server is running with '
                'writes disabled'
            ),
            'tool': name,
            'writes': True,
            'hint': (
                'read-only tools are still available; restart the agent without '
                '--mcp-read-only to allow changes'
            ),
        }
    try:
        return tool.fn(ctx, **arguments)
    except ToolError as exc:
        return {'error': str(exc)}
    except TypeError as exc:
        # A wrong argument name or a missing one; the schema should have caught
        # it, so say which so the caller can fix the call rather than the tool.
        return {'error': f'bad arguments for {name}: {exc}', 'schema': tool.schema()}
    except HTTPException as exc:
        # A path outside the allowed roots arrives as an HTTPException from the
        # guard. Turning it into a plain error keeps the boundary visible to the
        # caller instead of surfacing a framework type name.
        return {'error': str(exc.detail), 'status': exc.status_code}
    except Exception as exc:  # noqa: BLE001 - the model must see the failure
        return {'error': f'{type(exc).__name__}: {exc}'}


def describe(registry: Sequence[Tool]) -> str:
    """A compact listing for a system prompt: name, kind, first sentence."""
    lines = []
    for tool in registry:
        first = (tool.description or '').strip().split('\n\n')[0].replace('\n', ' ')
        first = first.split('. ')[0].rstrip('.')
        kind = 'write' if tool.writes else 'read'
        lines.append(f'- {tool.name} ({kind}): {first}.')
    return '\n'.join(lines)


def as_json(value: Any) -> str:
    """Serialise a tool result for a model, compactly and without NaN."""
    try:
        return json.dumps(value, default=str, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return json.dumps({'result': str(value)}, ensure_ascii=False)
