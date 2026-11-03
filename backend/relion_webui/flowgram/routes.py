# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""FlowGram's HTTP surface, served by the RELION agent.

The vendored UI calls FlowGram's own API: ``/api/bootstrap``,
``/api/workflows/{id}``, ``/api/runs/{id}``, ``/api/events``. Those live under
``/fg`` here so they cannot collide with the agent's own ``/api`` vocabulary -
the agent keeps answering ``/api/pipeline``-style routes for the main branch and
for MCP, while this router answers the canvas.

A run maps to *one queue submission per selected job* plus a journal record
whose id encodes those entries. Everything the canvas shows comes from the
projection plus the journal; there is no second store.

Ported from ``flowgram-app/bridge/middleware.ts``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import journal as journal_module
from . import layout as layout_module
from . import translate as translate_module
from . import workspaces as workspaces_module
from .translate import FLOWGRAM_STATE, SCHEMA, TERMINAL_STATES

router = APIRouter()


# --------------------------------------------------------------------- helpers


def _store(request: Request) -> workspaces_module.WorkspaceStore:
    return request.app.state.flowgram_workspaces


def _journal(request: Request) -> journal_module.Journal:
    return request.app.state.flowgram_journal


def _queue(request: Request):
    return request.app.state.queue


def _guard(request: Request):
    return request.app.state.flowgram_guard


def _project(project_directory: str) -> Any:
    from .. import pipeline as pipeline_module

    return pipeline_module.read_pipeline(project_directory)


def _graph(request: Request, project_path: str) -> Dict[str, Any]:
    """The pipeline as nodes and edges, the shape the projection consumes."""
    from .. import job_artifacts
    from .. import pipeline as pipeline_module
    from .. import queue_system

    project = _project(project_path)
    produced_by = {node: process for process, node in project.output_edges}
    consumed_by: Dict[str, List[str]] = {}
    for node, process in project.input_edges:
        consumed_by.setdefault(node, []).append(process)

    queue = _queue(request)
    queued_here = {
        item['process_name']: item['id']
        for item in queue.snapshot()['entries']
        if item.get('process_name') and item.get('state') in ('pending', 'running')
    }

    return {
        'project_directory': str(project_path),
        'job_counter': project.job_counter,
        'processes': [
            {
                'name': process.name,
                'alias': process.alias,
                'type_label': process.type_label,
                'status': process.status,
                'job_number': project.job_number_of(process.name),
                'directory': str(project.directory_of(process.name)).rstrip('/'),
                'queue_entry': queued_here.get(process.name),
            }
            for process in project.processes
        ],
        'nodes': [
            {
                'name': node.name,
                'type_label': node.type_label,
                'type_depth': node.type_depth,
                'produced_by': produced_by.get(node.name),
                'consumed_by': consumed_by.get(node.name, []),
            }
            for node in project.nodes
        ],
        'edges': {
            'inputs': [{'node': node, 'process': process} for node, process in project.input_edges],
            'outputs': [{'process': process, 'node': node} for process, node in project.output_edges],
        },
    }


async def _schema_loaded(request: Request) -> None:
    """Load the job tables once, from the agent's own modules.

    They are already in memory as generated tables, so this is a copy rather
    than a fetch - one less moving part than the bridge had.
    """
    if SCHEMA.jobs:
        return
    from .. import job_commands

    schemas = []
    for job_type in job_commands.selectable_job_types():
        try:
            schemas.append(job_commands.form_schema(job_type))
        except KeyError:
            continue
    # `/api/project/job-types` also carries the pipeline label and the output
    # filenames per job, which the projection needs.
    from .. import job_specs

    job_types = [
        {
            'id': spec.job_type,
            'process_type_label': spec.process_type_label,
            'outputs': [node.filename for node in spec.output_nodes],
        }
        for spec in job_specs.JOB_SPECS.values()
    ]
    SCHEMA.load(schemas, job_types)
    SCHEMA.categories = job_commands.job_categories()


def _projection(request: Request, project_path: str) -> Dict[str, Any]:
    graph = _graph(request, project_path)
    layout = layout_module.layout_pipeline(graph)
    project = {
        'path': project_path,
        'name': project_path.rstrip('/').rsplit('/', 1)[-1] or project_path,
    }
    return translate_module.workflow_from_pipeline(SCHEMA, project, graph, layout)


def _workspace_workflow(
    request: Request, project_path: str, workspace_id: Optional[str] = None
) -> Dict[str, Any]:
    """One workspace as the UI sees it: the pipeline plus the overlay."""
    from .. import pipeline as pipeline_module

    projection = _projection(request, project_path)
    store = _store(request)
    workspace = store.get(workspace_id) if workspace_id else store.ensure_default(project_path)
    if workspace is None:
        return projection
    return workspaces_module.merge(projection, workspace)


def _resolve_workspace(
    request: Request, workflow_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    store = _store(request)
    if workflow_id and not workflow_id.startswith('pipeline:'):
        workspace = store.get(workflow_id)
        if workspace is not None:
            return workspace
    project_id = (workflow_id or '').removeprefix('pipeline:')
    if not project_id:
        return None
    return store.ensure_default(project_id)


# ----------------------------------------------------------------- run submission


def _processes_for(intent: Mapping[str, Any], processes: Sequence[Mapping[str, Any]]) -> List[str]:
    """Which processes the run scope selects.

    `all` is the whole pipeline in layout order - the order matters, because the
    queue admits entries in submission order when several are ready at once.
    """
    scope = intent.get('scope', 'all')
    selected = intent.get('selectedNodeId')
    if scope in ('job', 'selected'):
        if not selected:
            return []
        # A single named job is allowed to be finished: that is what re-running
        # is. A batch never includes one, because "run everything" must not
        # silently repeat work that already succeeded.
        return [process['name'] for process in processes if process['name'] == selected]
    runnable = [
        process
        for process in processes
        if process.get('status') not in ('Succeeded', 'Running')
    ]
    return [process['name'] for process in runnable]


def _parameters_from_job_star(request: Request, project_path: str, process_name: str) -> Dict[str, Any]:
    """A finished job's options, as the queue wants them.

    A RELION job records what it ran with in ``job.star`` and nowhere else, so a
    re-run has to start from that file. Only the options the job's form owns are
    kept: the file also carries the shared run settings and the output directory,
    and letting those through would re-submit a job pointing at its own old
    directory.
    """
    from .. import job_artifacts
    from .. import job_commands
    from .. import pipeline as pipeline_module

    guard = _guard(request)
    project = guard.resolve_existing(project_path, kind='directory')
    try:
        pipeline = pipeline_module.read_pipeline(project)
    except (FileNotFoundError, ValueError):
        return {}
    process = pipeline.find_process(process_name)
    if process is None:
        return {}
    try:
        directory = guard.resolve_existing(str(project / process_name), kind='directory')
    except Exception:  # noqa: BLE001 - a missing directory is an empty form
        return {}
    options = job_artifacts.read_job_star(directory) or {}
    label = job_artifacts.read_job_type_label(directory) or process.type_label
    job_type = SCHEMA.job_type_for_label(label)
    if not job_type:
        return {}
    schema = job_commands.form_schema(job_type)
    wanted = {field['variable'] for field in schema.get('fields', [])}
    arguments = {
        key: value
        for key, value in options.items()
        if key in wanted and value not in (None, '')
    }
    # RELION records the input under `fn_in` for several jobs while the form
    # calls it `input_star_mics` and leaves that field empty. `fn_in` is not a
    # form field, so it was filtered out above - read it from the raw table,
    # or a re-run fails as "missing required options" with the path sitting in
    # job.star the whole time.
    for field in schema.get('fields', []):
        variable = field['variable']
        if field.get('kind') != 'input_node' or variable in arguments:
            continue
        fallback = options.get('fn_in') or arguments.get('fn_in')
        if fallback:
            arguments[variable] = fallback
    return arguments


def _devices_for(intent: Mapping[str, Any]) -> List[int]:
    """Devices to lease.

    ``auto`` is the queue's own behaviour: an empty list means "choose a free
    card", and it is the only safe default because a device is leased
    exclusively - naming one that is busy makes the job wait for that specific
    card rather than any card.
    """
    if intent.get('gpu') == 'specific' and isinstance(intent.get('gpuIndex'), int):
        return [int(intent['gpuIndex'])]
    return []


def _unwrap_argument(value: Any, all_parameters: Mapping[str, Any], depth: int = 0) -> Any:
    """A canvas parameter value as the queue expects it.

    Two shapes meet here. The inspector writes RELION's binding shape
    (``{type, content}``), and a value may be a reference to an upstream node's
    declared output (``["AlignTiltSeries/job002", "tilt_series.star"]``). The
    queue wants plain option values: constants unwrap, references resolve to the
    project path the pipeline guarantees, everything else passes through for
    RELION to reject with its own message.
    """
    if not isinstance(value, dict):
        return value
    kind = value.get('type')
    content = value.get('content')
    if kind == 'constant':
        return content
    if kind == 'ref' and isinstance(content, list) and content:
        node_id, *path = content
        if node_id and len(path) == 1:
            process_name = str(node_id).removeprefix('pipeline:')
            if '/' in process_name:
                return f'{process_name}/{path[0]}'
        if node_id and path and depth < 4:
            # Chained references: a ref whose leaf is another node's field
            # resolves to that field's value, recursively.
            upstream = (all_parameters.get(str(node_id)) or {}).get(path[0])
            if isinstance(upstream, dict) and len(path) == 1:
                resolved = _unwrap_argument(upstream, all_parameters, depth + 1)
                if isinstance(resolved, str):
                    return resolved
        return content
    return value


def _resolve_arguments(
    options: Mapping[str, Any], all_parameters: Mapping[str, Any]
) -> Dict[str, Any]:
    return {
        str(key): _unwrap_argument(value, all_parameters)
        for key, value in options.items()
    }


def _run_id(project_path: str, entries: Sequence[str]) -> str:
    """A run id that survives a URL path segment.

    The project path is part of the id, and it contains slashes; percent-encoding
    them does not help because FastAPI's ``{run_id:path}`` decodes the segment
    before the handler sees it. A dot-separated digest keeps the id opaque and
    path-safe - the journal is the only thing that needs to read it.
    """
    import hashlib

    digest = hashlib.sha1(project_path.encode('utf-8')).hexdigest()[:12]
    return f'relion-{digest}-{",".join(entries) or "none"}'


async def _start_run(
    request: Request,
    project_path: str,
    inputs: Mapping[str, Any],
    parameters: Mapping[str, Mapping[str, Any]],
    workspace_id: Optional[str],
) -> Dict[str, Any]:
    """Queue the jobs a run intent selects, and return a record the UI follows.

    One submission per job: that is what makes the queue's own admission control
    do the work, so a job that cannot start yet waits there, not in this
    request.
    """
    from .. import pipeline as pipeline_module
    from .. import queue_system

    await _schema_loaded(request)
    wanted_graph = _graph(request, project_path)
    store = _store(request)
    workspace = store.get(workspace_id) if workspace_id else None

    runnable: List[Dict[str, Any]] = [
        {
            'name': process['name'],
            'typeLabel': process.get('type_label') or '',
            'draft': False,
            'status': process.get('status'),
        }
        for process in wanted_graph['processes']
    ]
    # Draft nodes are jobs that do not exist in the pipeline yet. They take part
    # in `all`/selected/job runs exactly like a process: selecting one queues it,
    # which is what promotes it.
    for node in (workspace or {}).get('nodes', []):
        runnable.append(
            {
                'name': node['id'],
                'typeLabel': node['type'],
                'draft': True,
                'status': 'Scheduled',
            }
        )

    wanted = set(_processes_for(inputs, runnable))
    entry_ids: List[str] = []
    failures: List[str] = []
    if not wanted:
        selected = inputs.get('selectedNodeId')
        failures.append(
            f'{selected} is already finished or running'
            if selected
            else 'no runnable jobs in this pipeline'
        )
        return {'runId': _run_id(project_path, entry_ids), 'entryIds': entry_ids, 'failures': failures}

    devices = _devices_for(inputs)
    exclusive = bool(inputs.get('exclusive'))
    queue = _queue(request)

    for item in runnable:
        if item['name'] not in wanted:
            continue
        if item['draft']:
            job_type = str(item['typeLabel']).removeprefix('relion.')
        else:
            job_type = SCHEMA.job_type_for_label(str(item['typeLabel'])) or ''
        if not job_type:
            failures.append(f'{item["name"]}: no job type for {item["typeLabel"]}')
            continue
        try:
            supplied = parameters.get(item['name']) or {}
            if not supplied and not item['draft']:
                # Re-running a finished job: its options exist only in its
                # job.star, so read them back rather than queueing an empty form.
                supplied = _parameters_from_job_star(request, project_path, item['name'])
            arguments = _resolve_arguments(supplied, parameters)
            entry = queue.add(
                project_path,
                job_type,
                arguments,
                gpus=devices,
                exclusive=exclusive,
            )
            entry_ids.append(entry.id)
            if item['draft'] and workspace_id and entry.process_name:
                store.promote(workspace_id, str(item['name']), entry.process_name)
        except Exception as cause:  # noqa: BLE001 - the queue's message is the diagnosis
            failures.append(f'{item["name"]}: {cause}')

    return {
        'runId': _run_id(project_path, entry_ids),
        'entryIds': entry_ids,
        'failures': failures,
    }


# ------------------------------------------------------------------- routes


def _projects(request: Request) -> List[Dict[str, Any]]:
    """The project list in FlowGram's shape.

    The agent answers `/api/projects` for everything else; this is only the
    projection the canvas needs (`{id, name, createdAt, directory}`), so it is a
    local helper rather than a second route.
    """
    listing = request.app.state.projects.summarize()
    return [
        translate_module.project_from_relion(entry)
        for entry in listing
        if entry.get('readable') is not False
    ]


async def _bootstrap(request: Request) -> Dict[str, Any]:
    await _schema_loaded(request)
    projects = _projects(request)
    store = _store(request)
    workflows: List[Dict[str, Any]] = []
    for project in projects:
        try:
            listed = store.list(project['path'])
            if not listed:
                workflows.append(_workspace_workflow(request, project['path']))
                continue
            for workspace in listed:
                workflows.append(_workspace_workflow(request, project['path'], workspace['id']))
        except Exception:  # noqa: BLE001 - an unreadable project contributes no workflow
            continue
    journal = _journal(request)
    runs = [
        {**run.as_dict(), 'workflowName': name}
        for run, name in journal.all_runs()
    ]
    return {'projects': projects, 'sessions': [], 'workflows': workflows, 'runs': runs}


@router.get('/api/bootstrap')
async def bootstrap(request: Request) -> Dict[str, Any]:
    return await _bootstrap(request)


@router.post('/api/projects/initialize')
async def initialize() -> Dict[str, Any]:
    """The app seeds demo content on boot; RELION projects are already on disk."""
    return {'initialized': False}


@router.get('/api/workflows')
async def list_workflows(request: Request) -> List[Dict[str, Any]]:
    return (await _bootstrap(request))['workflows']


@router.post('/api/workflows', status_code=201)
async def create_workflow(request: Request) -> Dict[str, Any]:
    body = await request.json()
    project_id = str(body.get('projectId') or '')
    if not project_id:
        raise HTTPException(400, detail='projectId is required')
    workspace = _store(request).create(project_id, str(body.get('name') or ''))
    return _workspace_workflow(request, project_id, workspace['id'])


@router.get('/api/workflows/{workflow_id:path}/run')
async def workflow_run_cannot_be_read(workflow_id: str) -> None:
    raise HTTPException(405, detail='use POST to start a run')


@router.post('/api/workflows/{workflow_id:path}/run')
async def run_workflow(workflow_id: str, request: Request) -> Dict[str, Any]:
    await _schema_loaded(request)
    workspace = _resolve_workspace(request, workflow_id)
    if workspace is None:
        raise HTTPException(404, detail='unknown workflow')
    project_path = workspace['projectId']
    body = await request.json()
    inputs = body.get('inputs') or {}
    parameters = body.get('parameters') or {}

    result = await _start_run(request, project_path, inputs, parameters, workspace['id'])
    merged = _workspace_workflow(request, project_path, workspace['id'])
    status = 'failed' if result['failures'] and not result['entryIds'] else 'running'
    error = '; '.join(result['failures']) or None
    if result['entryIds']:
        _journal(request).open_run(
            result['runId'],
            project_path,
            inputs,
            workflow_id=merged['id'],
            entries=result['entryIds'],
            revision=merged['revision'],
        )
    return {
        'id': result['runId'],
        'workflowId': merged['id'],
        'status': status,
        'inputs': inputs,
        'outputs': {'queued': result['entryIds']} if result['entryIds'] else None,
        'error': error,
        'revision': merged['revision'],
        'createdAt': translate_module._now(),
        'sessionId': None,
    }


@router.put('/api/workflows/{workflow_id:path}')
async def save_workflow(workflow_id: str, request: Request) -> Dict[str, Any]:
    body = await request.json()
    store = _store(request)
    saved = store.save(
        workflow_id,
        name=body.get('name'),
        schema=body.get('schema'),
    )
    if saved is None:
        raise HTTPException(404, detail='unknown workspace')
    return _workspace_workflow(request, saved['projectId'], saved['id'])


@router.post('/api/workflows/{workflow_id:path}/archive')
async def archive_workflow(workflow_id: str, request: Request) -> Dict[str, Any]:
    if not _store(request).archive(workflow_id):
        raise HTTPException(404, detail='unknown workspace')
    return {'id': workflow_id, 'archived': True}


@router.post('/api/workflows/{workflow_id:path}/validate')
async def validate_workflow(workflow_id: str, request: Request) -> Dict[str, Any]:
    """Check each draft job's parameters with RELION's own validator."""
    from .. import job_commands

    store = _store(request)
    workspace = store.get(workflow_id)
    if workspace is None:
        raise HTTPException(404, detail='unknown workspace')
    problems: List[str] = []
    for node in workspace.get('nodes', []):
        job_type = str(node.get('type', '')).removeprefix('relion.')
        if not job_type or job_type == node.get('type'):
            continue
        values = (node.get('data') or {}).get('inputsValues') or {}
        arguments = {
            key: value.get('content')
            for key, value in values.items()
            if isinstance(value, dict) and value.get('type') == 'constant'
        }
        try:
            problems.extend(
                f'{node["id"]}: {problem}'
                for problem in job_commands.validate(job_type, arguments)
            )
        except KeyError:
            problems.append(f'{node["id"]}: unknown job type {job_type}')
    return {'valid': not problems, 'errors': problems}


@router.get('/api/workflows/{workflow_id:path}')
async def get_workflow(workflow_id: str, request: Request) -> Dict[str, Any]:
    await _schema_loaded(request)
    workspace = _resolve_workspace(request, workflow_id)
    if workspace is None:
        raise HTTPException(404, detail='unknown workflow')
    return _workspace_workflow(request, workspace['projectId'], workspace['id'])


@router.get('/api/runs')
async def list_runs(request: Request) -> Dict[str, Any]:
    journal = _journal(request)
    pairs = journal.all_runs()
    return {
        'runs': [
            {
                'id': run.id,
                'workflowId': run.workflowId,
                'workflowName': name,
                'revision': run.revision,
                'testNodeId': None,
                'status': run.status,
                'source': 'manual',
                'createdAt': run.createdAt,
                'endedAt': None if run.status == 'running' else run.createdAt,
                'durationMs': None,
                'error': run.error,
                'callbackStatus': None,
                'callbackAttempts': None,
                'callbackError': None,
                'environment': None,
                'releaseVersion': None,
                'operation': None,
                'requestId': None,
                'businessId': run.id,
            }
            for run, name in pairs
        ],
        'total': len(pairs),
        'next': None,
        'workflows': [
            {'id': workflow_id, 'name': journal_module.workflow_name(workflow_id)}
            for workflow_id in {run.workflowId for run, _ in pairs}
        ],
        'environments': [],
    }


@router.post('/api/runs/{run_id:path}/cancel')
async def cancel_run(run_id: str, request: Request) -> Dict[str, Any]:
    journal = _journal(request)
    record = journal.run(run_id)
    if record is None:
        raise HTTPException(404, detail='unknown run')
    queue = _queue(request)
    cancelled: List[str] = []
    for entry_id, entry in record.entries.items():
        if entry.get('state') in TERMINAL_STATES:
            continue
        try:
            await queue.cancel(entry_id)
        except Exception:  # noqa: BLE001 - a lost entry is still "cancelled" to the UI
            pass
        cancelled.append(entry_id)
    journal.record_cancel(run_id, cancelled)
    return {'id': run_id, 'cancelled': cancelled}


@router.get('/api/runs/{run_id:path}')
async def get_run(run_id: str, request: Request, view: Optional[str] = None, workflowId: Optional[str] = None) -> Dict[str, Any]:  # noqa: N803 - FlowGram's casing
    journal = _journal(request)
    record = journal.run(run_id)
    if record is None:
        raise HTTPException(404, detail='unknown run')
    events = [event.as_dict() for event in record.events]
    run = record.run.as_dict()

    if view == 'overview':
        executions = []
        for entry_id, entry in record.entries.items():
            state = entry.get('state')
            executions.append(
                {
                    'id': entry_id,
                    'parentId': None,
                    'parentPath': [],
                    'nodeId': entry.get('process_name') or entry_id,
                    'workflowId': run['workflowId'],
                    'scopeId': '',
                    'kind': 'node',
                    'name': entry.get('process_name') or entry.get('job_type'),
                    'status': FLOWGRAM_STATE.get(str(state), 'running'),
                    'firstSequence': 0,
                    'lastSequence': len(events),
                    'startedAt': translate_module._iso(entry.get('started_at')) if entry.get('started_at') else run['createdAt'],
                    'endedAt': translate_module._iso(entry.get('finished_at')) if entry.get('finished_at') else None,
                    'durationMs': (
                        max(0.0, (entry['finished_at'] - entry['started_at']) * 1000)
                        if entry.get('started_at') and entry.get('finished_at')
                        else None
                    ),
                    'branchPath': [],
                    'iterationPath': [],
                    'attempt': 1,
                    'reason': entry.get('error'),
                    'children': 0,
                    'inputsAvailable': False,
                    'outputsAvailable': state == 'succeeded',
                    'links': [],
                }
            )
        nodes = [
            {
                'workflowId': run['workflowId'],
                'nodeId': entry.get('process_name') or entry_id,
                'total': 1,
                'durationMs': (
                    max(0.0, (entry['finished_at'] - entry['started_at']) * 1000)
                    if entry.get('started_at') and entry.get('finished_at')
                    else None
                ),
                'startedAt': translate_module._iso(entry.get('started_at')) if entry.get('started_at') else None,
                'counts': {FLOWGRAM_STATE.get(str(entry.get('state')), 'running'): 1},
            }
            for entry_id, entry in record.entries.items()
        ]
        return {
            'run': run,
            'snapshot': None,
            'models': [],
            'nodeTest': None,
            'trace': {
                'executions': executions,
                'nextCursor': None,
                'total': len(executions),
                'at': None,
                'observedAt': translate_module._now(),
                'outputsAvailable': any(e['outputsAvailable'] for e in executions),
                'origin': None,
                'format': 'journal',
                'runtime': 'relion-queue',
                'source': 'manual',
                'endedAt': None if record.run.status == 'running' else translate_module._now(),
                'durationMs': None,
                'watermark': events[-1]['sequence'] if events else 0,
                'rootTotal': len(executions),
                'counts': {},
                'failureId': next((e['id'] for e in executions if e['status'] == 'failed'), None),
                'failurePath': [],
                'focus': [e for e in executions if e['status'] == 'failed'],
                'links': [],
                'nodes': nodes,
                'publication': None,
            },
        }

    if view == 'execution':
        execution_id = request.query_params.get('execution')
        entry = record.entries.get(execution_id or '')
        if entry is None:
            raise HTTPException(404, detail='unknown execution')
        selected = []
        for event in record.events:
            data = json.loads(event.data)
            if event.kind == 'log_appended':
                if data.get('nodeId') == (entry.get('process_name') or execution_id):
                    selected.append(event.as_dict())
            elif data.get('executionId') == execution_id:
                selected.append(event.as_dict())
        return {'events': selected, 'nextAfter': None}

    node_states = {
        entry.get('process_name') or entry_id: FLOWGRAM_STATE.get(
            str(entry.get('state')), 'running'
        )
        for entry_id, entry in record.entries.items()
    }
    return {'run': run, 'events': events, 'nodeStates': node_states}


@router.get('/api/events')
async def events(request: Request) -> StreamingResponse:
    """The journal's SSE stream.

    The client passes ``?after=<sequence>`` on reconnect; anything beyond that
    is replayed, then live events are pushed as the sampler writes them.
    """
    journal = _journal(request)
    after = int(request.query_params.get('after') or 0)
    run_id = request.query_params.get('objectId') or None

    async def stream():
        nonlocal after
        yield ': connected\n\n'
        while True:
            if await request.is_disconnected():
                return
            pending = journal.events_after(after, run_id)
            for event in pending:
                yield f'id: {event.sequence}\ndata: {json.dumps(event.as_dict())}\n\n'
            if pending:
                after = pending[-1].sequence
            else:
                yield ': keep-alive\n\n'
            await asyncio.sleep(1.0)

    return StreamingResponse(
        stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'},
    )


@router.get('/api/job-schema/definitions')
async def job_definitions(request: Request) -> Dict[str, Any]:
    """The job schema plus each job type's *node definition*.

    The inspector needs more than the raw option table: which FlowGram editor
    renders each field, which section it belongs in, and the hover tip. That
    mapping is RELION-specific and lives in `forms.py`; the agent's own
    `/api/job-schema` stays as it is, because the main REST surface and MCP do
    not need an editor name.

    `definitions` is keyed by node type (`relion.tomo_import_serialem`), which
    is what `definitionFor(node)` looks up.
    """
    from .. import job_commands

    from . import forms

    schemas = []
    for job_type in job_commands.selectable_job_types():
        try:
            schemas.append(job_commands.form_schema(job_type))
        except KeyError:
            continue
    return {
        'jobs': schemas,
        'categories': job_commands.job_categories(),
        'definitions': forms.definitions_for(schemas),
    }


@router.get('/api/projects/processes/{process_name:path}/rerun-parameters')
async def rerun_parameters(process_name: str, request: Request, project_directory: str) -> Dict[str, Any]:  # noqa: N803
    """The parameters a finished job ran with, ready to queue again.

    A RELION job's options exist only in its ``job.star`` once it has run, and
    re-running it means feeding those same options back through the queue. This
    reads them in the shape the queue accepts, so the canvas does not have to
    know how a job directory is laid out.

    Only the options the job's own form exposes are returned: ``job.star``
    carries the whole GUI table including shared run settings, and queueing a
    third-party template path or another job's directory would be wrong.
    """
    from .. import job_artifacts
    from .. import job_commands
    from .. import pipeline as pipeline_module

    await _schema_loaded(request)
    guard = _guard(request)
    project = guard.resolve_existing(project_directory, kind='directory')
    try:
        pipeline = pipeline_module.read_pipeline(project)
    except (FileNotFoundError, ValueError) as cause:
        raise HTTPException(404, detail=str(cause)) from cause
    process = pipeline.find_process(process_name)
    if process is None:
        raise HTTPException(404, detail=f'{process_name} is not in this project')

    directory = guard.resolve_existing(str(project / process_name), kind='directory')
    options = job_artifacts.read_job_star(directory) or {}
    label = job_artifacts.read_job_type_label(directory) or ''
    job_type = SCHEMA.job_type_for_label(label)
    if not job_type:
        raise HTTPException(
            400, detail=f'no job type for {label or "an unknown label"}'
        )

    # One implementation of "what a job's options look like as queue arguments",
    # shared with the re-run path so the preview and the submission cannot
    # disagree about `fn_in` or about which fields the form owns.
    return {
        'process_name': process_name,
        'job_type': job_type,
        'type_label': label,
        'arguments': _parameters_from_job_star(request, project_directory, process_name),
    }


@router.get('/api/settings/model')
async def model_settings() -> Dict[str, Any]:
    """No model connection: the assistant is an external harness."""
    return {'connections': [], 'entries': [], 'defaults': {}}


@router.get('/api/settings/models')
async def model_inventory() -> Dict[str, Any]:
    return {
        'connections': [],
        'models': [],
        'credentials': [],
        'defaults': {'chat': None, 'workflow': None, 'revision': 0},
        'verifications': {},
    }


@router.get('/api/tools')
async def tools() -> Dict[str, Any]:
    """The MCP tool inventory, in the shape the settings page lists."""
    from .. import tools as tools_module

    registry = getattr(tools_module, 'TOOL_REGISTRY', None) or []
    return {
        'tools': [
            {
                'name': tool.name,
                'description': tool.description,
                'writes': bool(getattr(tool, 'writes', False)),
            }
            for tool in registry
        ]
    }


# ------------------------------------------------------------------- mounting


def install(app, *, static_directory=None) -> None:
    """Attach the FlowGram router (and its built UI) to the agent.

    Called once from ``create_app``. The router adds the canvas-only routes to
    the agent's `/api`; the static mount serves the built canvas at `/`. One
    process, one namespace, one front end.
    """
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    app.state.flowgram_workspaces = workspaces_module.WorkspaceStore()
    app.state.flowgram_journal = journal_module.Journal(app.state.queue, app.state.jobs)
    app.state.flowgram_guard = getattr(app.state, 'guard', None)
    app.include_router(router)

    # The static mount is deliberately NOT done here.
    #
    # A mount on '/' swallows every route registered after it, and the agent
    # registers most of its API below `create_app`'s state setup. `create_app`
    # calls `mount_static(app)` at the very end instead.


def mount_static(app, static_directory=None) -> None:
    """Serve the built canvas at `/`.

    Must run after every route is registered: a `/` mount matches everything
    that follows it, so doing this any earlier would 404 the agent's own API.
    """
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    if static_directory is None:
        static_directory = Path(__file__).resolve().parent.parent / 'static'
    static_directory = Path(static_directory)
    if static_directory.is_dir():
        app.mount('/', StaticFiles(directory=str(static_directory), html=True), name='canvas')


