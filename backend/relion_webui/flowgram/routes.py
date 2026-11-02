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

PREFIX = '/fg'

router = APIRouter(prefix=PREFIX)


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
    runnable = [
        process
        for process in processes
        if process.get('status') not in ('Succeeded', 'Running')
    ]
    scope = intent.get('scope', 'all')
    selected = intent.get('selectedNodeId')
    if scope in ('job', 'selected'):
        if not selected:
            return []
        return [process['name'] for process in runnable if process['name'] == selected]
    return [process['name'] for process in runnable]


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
            arguments = _resolve_arguments(parameters.get(item['name']) or {}, parameters)
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


@router.get('/api/projects')
async def list_projects(request: Request) -> List[Dict[str, Any]]:
    projects = request.app.state.projects.summarize()
    return [
        translate_module.project_from_relion(entry)
        for entry in projects
        if entry.get('readable') is not False
    ]


async def _bootstrap(request: Request) -> Dict[str, Any]:
    await _schema_loaded(request)
    projects = await list_projects(request)
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

    Called once from ``create_app``. The router answers ``/fg/api/...``; the
    static mount serves the built canvas at ``/fg`` when a build is present, so
    a deployment is the agent alone rather than the agent plus a Node process.
    """
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    app.state.flowgram_workspaces = workspaces_module.WorkspaceStore()
    app.state.flowgram_journal = journal_module.Journal(app.state.queue, app.state.jobs)
    app.state.flowgram_guard = getattr(app.state, 'guard', None)
    app.include_router(router)

    if static_directory is None:
        static_directory = Path(__file__).resolve().parent.parent / 'flowgram_static'
    static_directory = Path(static_directory)
    if static_directory.is_dir():
        # Mounted last, so /fg/api keeps winning the prefix match.
        app.mount('/fg', StaticFiles(directory=str(static_directory), html=True), name='flowgram')


@router.get('/api/browse')
async def browse(request: Request, path: Optional[str] = None, filter: str = 'all', hidden: bool = False) -> Dict[str, Any]:  # noqa: A002, N803 - the agent's own query names
    """The agent's filesystem, for the path picker.

    Same listing the main UI's picker uses; re-exported under /fg so the canvas
    needs one prefix rather than two.
    """
    from .. import browse as browse_module

    try:
        return browse_module.list_directory(
            path, filter_kind=filter, show_hidden=hidden
        ).as_dict()
    except ValueError as cause:
        raise HTTPException(400, detail=str(cause)) from cause


@router.get('/api/browse/locations')
async def browse_locations() -> Dict[str, Any]:
    from .. import browse as browse_module

    return {'locations': browse_module.quick_locations()}


@router.get('/api/job-schema')
async def job_schema(request: Request, job_type: Optional[str] = None) -> Dict[str, Any]:  # noqa: N803
    """RELION's job options, already shaped for the node inspector.

    The inspector renders a form from a data table; that table is built here
    (see ``forms.py``) rather than in the browser, so the mapping from RELION's
    option kinds to FlowGram's editors exists once.
    """
    from .. import job_commands
    from . import forms

    await _schema_loaded(request)
    if job_type is not None:
        try:
            schema = job_commands.form_schema(job_type)
        except KeyError as cause:
            raise HTTPException(404, detail=str(cause)) from cause
        return {**schema, 'definition': forms.node_definition(schema)}

    schemas = [
        job_commands.form_schema(name)
        for name in job_commands.selectable_job_types()
    ]
    return {
        'jobs': schemas,
        'definitions': forms.definitions_for(schemas),
        'categories': job_commands.job_categories(),
    }


@router.get('/api/project/job-types')
async def project_job_types() -> Dict[str, Any]:
    from .. import job_specs

    return {
        'job_types': [
            {
                'id': spec.job_type,
                'title': spec.tab_title,
                'executable': spec.executable,
                'process_type_label': spec.process_type_label,
                'directory': spec.directory_name,
                'inputs': spec.input_variable,
                'outputs': [node.filename for node in spec.output_nodes],
                'base_job_type': spec.base_job_type,
            }
            for spec in job_specs.JOB_SPECS.values()
        ]
    }


@router.get('/api/project/input-candidates')
async def input_candidates(request: Request, job_type: str, variable: str, project_directory: str) -> Dict[str, Any]:  # noqa: N803
    """The files a node-typed option may be filled with.

    RELION's own rule: the desktop GUI's picker lists
    ``.Nodes/<first type component>/``, so this lists the same directory. A
    queued job's output appears here because the queue reserves its node entry
    at enqueue time.
    """
    from .. import job_artifacts
    from .. import job_commands
    from .. import pipeline as pipeline_module

    project = _guard(request).resolve_existing(project_directory, kind='directory')
    try:
        schema = job_commands.form_schema(job_type)
    except KeyError as cause:
        raise HTTPException(400, detail=f'unknown job type {job_type!r}') from cause
    field = next(
        (item for item in schema.get('fields', []) if item['variable'] == variable),
        None,
    )
    if field is None:
        raise HTTPException(400, detail=f'{job_type} has no option {variable!r}')
    if field.get('kind') != 'input_node':
        raise HTTPException(
            400,
            detail=f'{variable!r} is a {field.get("kind")!r} option, not a pipeline node',
        )

    node_type = str(field.get('node_type') or '')
    depth = int(field.get('node_type_depth') or 1)
    names = job_artifacts.node_candidates(project, node_type, depth)

    try:
        graph = pipeline_module.read_pipeline(project)
    except (FileNotFoundError, ValueError):
        graph = pipeline_module.Pipeline()
    producers = {node: process for process, node in graph.output_edges}
    queue = _queue(request)
    queued = {
        item['process_name']: item['id']
        for item in queue.snapshot()['entries']
        if item.get('state') in ('pending', 'running') and item.get('process_name')
    }

    candidates = []
    for name in names:
        producer = producers.get(name)
        process = graph.find_process(producer) if producer else None
        exists = (project / name).exists()
        filename = name.rsplit('/', 1)[-1]
        entry_id = queued.get(producer) if producer else None
        candidates.append(
            {
                'node': name,
                'producer': producer,
                'producer_status': process.status if process is not None else None,
                'exists': exists,
                'pending': not exists,
                'queue_entry': entry_id,
                'slot': f'{entry_id}:{filename}' if entry_id else None,
            }
        )
    return {
        'job_type': job_type,
        'variable': variable,
        'node_type': node_type,
        'candidates': candidates,
    }


# ------------------------------------------------------------ cluster & queue
#
# The canvas's cluster page is the agent's own hosts/queue views under /fg, so
# one base URL answers everything the UI asks.


@router.get('/api/hosts')
async def list_hosts(request: Request, check: bool = False) -> Dict[str, Any]:
    from .. import hosts as hosts_module

    manager = request.app.state.jobs
    entries = []
    for host in manager.hosts.list():
        entry = host.as_dict()
        if check and not host.is_local and host.enabled:
            ok, message = hosts_module.check_reachable(host)
            entry['reachable'] = ok
            if not ok:
                entry['problem'] = message
        entries.append(entry)
    return {'hosts': entries, 'default': hosts_module.LOCAL_NAME}


@router.post('/api/hosts', status_code=201)
async def add_host(request: Request) -> Dict[str, Any]:
    from .. import hosts as hosts_module

    body = await request.json()
    registry = request.app.state.jobs.hosts
    try:
        if body.get('spec'):
            host = hosts_module.parse_host_spec(str(body['spec']))
        else:
            name = str(body.get('name') or '')
            hostname = str(body.get('hostname') or '')
            if not name or not hostname:
                raise ValueError('give a spec, or both a name and a hostname')
            host = hosts_module.ExecutionHost(
                name=name,
                hostname=hostname,
                port=int(body.get('port') or 22),
                user=body.get('user'),
                note=str(body.get('note') or ''),
            )
        registry.add(host)
    except ValueError as cause:
        raise HTTPException(400, detail=str(cause)) from cause
    return host.as_dict()


@router.patch('/api/hosts/{name}')
async def update_host(name: str, request: Request) -> Dict[str, Any]:
    body = await request.json()
    registry = request.app.state.jobs.hosts
    host = registry.get(name)
    if host is None:
        raise HTTPException(404, detail=f'no host {name!r}')
    if body.get('enabled') is not None:
        host.enabled = bool(body['enabled'])
    if body.get('note') is not None:
        host.note = str(body['note'])
    if body.get('hostname') is not None:
        host.hostname = str(body['hostname'])
    registry.add(host)
    return host.as_dict()


@router.delete('/api/hosts/{name}')
async def remove_host(name: str, request: Request) -> Dict[str, Any]:
    if not request.app.state.jobs.hosts.remove(name):
        raise HTTPException(404, detail=f'no host named {name!r}')
    return {'removed': name}


@router.get('/api/nodes')
async def node_samples(request: Request, local: bool = True) -> Dict[str, Any]:
    """Live snapshots of every enabled node.

    One ssh round trip per node, run concurrently, so a slow or down node does
    not delay the others. Collection blocks, so it runs in a worker thread -
    on the event loop one dead node would freeze every other request.
    """
    import anyio

    from .. import node_metrics

    wanted = [
        host
        for host in request.app.state.jobs.hosts.list()
        if host.enabled and (local or not host.is_local)
    ]
    snapshots = await anyio.to_thread.run_sync(node_metrics.collect_many, wanted)
    return {'nodes': [snapshot.as_dict() for snapshot in snapshots], 'count': len(snapshots)}


@router.get('/api/queue')
async def queue_snapshot(request: Request, project_directory: Optional[str] = None) -> Dict[str, Any]:  # noqa: N803
    queue = request.app.state.queue
    await queue.warm_capacity()
    return queue.snapshot(project_directory)


@router.delete('/api/queue/{entry_id}')
async def cancel_queue_entry(entry_id: str, request: Request) -> Dict[str, Any]:
    from .. import queue_system

    queue = request.app.state.queue
    try:
        entry = await queue.cancel(entry_id)
    except queue_system.QueueError as cause:
        raise HTTPException(400, detail=str(cause)) from cause
    return entry.as_dict()


# ------------------------------------------------------------------ projects
#
# The project management dialog reads a project's details and renames or
# removes it from the agent's list. Removing a project never touches its files:
# it only stops the agent serving it, which is what "delete from list" means.


@router.post('/api/projects', status_code=201)
async def add_project(request: Request) -> Dict[str, Any]:
    body = await request.json()
    path = str(body.get('path') or '').strip()
    if not path:
        raise HTTPException(400, detail='path is required')
    try:
        entry = request.app.state.projects.add(path, name=body.get('name'))
    except ValueError as cause:
        raise HTTPException(400, detail=str(cause)) from cause
    return translate_module.project_from_relion(entry.as_dict())


@router.post('/api/projects/create', status_code=201)
async def create_project(request: Request) -> Dict[str, Any]:
    """Create a RELION project directory.

    Writes only ``default_pipeline.star``, which is all RELION needs to open a
    directory as a project. Registering it in the list is the caller's next
    call, so creating and listing stay independent.
    """
    body = await request.json()
    path = str(body.get('path') or '').strip()
    if not path:
        raise HTTPException(400, detail='path is required')
    registry = request.app.state.projects
    try:
        return registry.create(path, name=body.get('name'))
    except ValueError as cause:
        raise HTTPException(400, detail=str(cause)) from cause


@router.get('/api/projects/{project_id:path}')
async def get_project(project_id: str, request: Request) -> Dict[str, Any]:
    summary = request.app.state.projects.summarize()
    entry = next((item for item in summary if item['path'] == project_id), None)
    if entry is None:
        raise HTTPException(404, detail=f'project not in the list: {project_id}')
    counts = {
        'jobs': entry.get('job_count') or 0,
        'archived': 0,
        'sessions': 0,
    }
    return {
        'project': translate_module.project_from_relion(entry),
        'counts': counts,
    }


@router.put('/api/projects/{project_id:path}')
async def rename_project(project_id: str, request: Request) -> Dict[str, Any]:
    body = await request.json()
    name = str(body.get('name') or '').strip()
    if not name:
        raise HTTPException(400, detail='name is required')
    registry = request.app.state.projects
    entry = next(
        (item for item in registry.summarize() if item['path'] == project_id), None
    )
    if entry is None:
        raise HTTPException(404, detail=f'project not in the list: {project_id}')
    try:
        updated = registry.add(project_id, name=name)
    except ValueError as cause:
        raise HTTPException(400, detail=str(cause)) from cause
    return translate_module.project_from_relion(updated.as_dict())


@router.delete('/api/projects/{project_id:path}')
async def remove_project(project_id: str, request: Request) -> Dict[str, Any]:
    """Remove a project from the list. Its directory is untouched."""
    if not request.app.state.projects.remove(project_id):
        raise HTTPException(404, detail=f'project not in the list: {project_id}')
    return {'removed': project_id}
