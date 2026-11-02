# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""FlowGram's API shapes, answered from RELION's own data.

The vendored FlowGram UI speaks its own vocabulary - *projects*, *workflows*,
*runs*, *events* - and the RELION agent speaks another - *projects*,
*pipelines*, *jobs*, *queue entries*. This module is the only place that knows
both, so neither side has to learn the other's words.

It is a port of ``flowgram-app/bridge/translate.ts``. The bridge used to run as
Vite middleware, which meant the production build had no backend at all; it now
lives inside the agent, where it can read the queue and the job manager
directly instead of polling its own HTTP API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import layout as layout_module

EXECUTION_NODE_ID = 'relion.execution'

#: The run dialog's form. FlowGram renders a start node's `outputs` schema, so
#: these become ordinary controls and the execution *semantics* stay ours.
EXECUTION_SCHEMA: Dict[str, Any] = {
    'type': 'object',
    'properties': {
        'scope': {
            'type': 'string',
            'title': 'Run',
            'enum': ['all', 'selected', 'job'],
            'enumNames': ['Whole pipeline', 'Selected node', 'Single job'],
            'default': 'all',
        },
        'gpu': {
            'type': 'string',
            'title': 'GPU',
            'enum': ['auto', 'specific'],
            'enumNames': ['Any free card', 'Specific card'],
            'default': 'auto',
        },
        'gpuIndex': {
            'type': 'integer',
            'title': 'GPU index',
            'description': 'Used when GPU is "specific".',
            'default': 0,
        },
        'exclusive': {
            'type': 'boolean',
            'title': 'Whole node',
            'description': 'While this runs, nothing else starts on the node.',
            'default': False,
        },
    },
    'required': ['scope', 'gpu'],
}

#: RELION's process status -> FlowGram's node status vocabulary.
STATUS: Dict[str, str] = {
    'Succeeded': 'succeeded',
    'Running': 'running',
    'Failed': 'failed',
    'Aborted': 'canceled',
    'Scheduled': 'pending',
}

#: Queue state -> FlowGram node state, for the canvas's status map.
FLOWGRAM_STATE: Dict[str, str] = {
    'pending': 'waiting',
    'running': 'running',
    'succeeded': 'succeeded',
    'failed': 'failed',
    'cancelled': 'canceled',
}

#: Queue states that end an entry.
TERMINAL_STATES = frozenset({'succeeded', 'failed', 'cancelled'})


@dataclass
class JobType:
    """One job type as the UI needs it: fields, defaults and output names."""

    job_type: str
    title: str
    fields: List[Dict[str, Any]] = field(default_factory=list)
    required: List[str] = field(default_factory=list)
    tabs: List[Dict[str, Any]] = field(default_factory=list)
    cli_options: List[Dict[str, Any]] = field(default_factory=list)
    #: Output filenames (`tilt_series.star`, ...), from the job spec.
    outputs: List[str] = field(default_factory=list)


class SchemaCache:
    """The job schema and the label -> job-type table, loaded once.

    ``workflow_from_pipeline`` is synchronous, so these tables have to already
    be here by the time a projection runs. ``refresh`` is what fills them; it
    is called at boot and again if a projection finds them empty.
    """

    def __init__(self) -> None:
        self.jobs: Dict[str, JobType] = {}
        self.label_to_job: Dict[str, str] = {}
        self.label_outputs: Dict[str, List[str]] = {}
        self.categories: List[Dict[str, Any]] = []

    # -- loading ---------------------------------------------------------
    def load(self, schemas: Sequence[Mapping[str, Any]], job_types: Sequence[Mapping[str, Any]]) -> None:
        self.jobs = {
            str(entry['job_type']): JobType(
                job_type=str(entry['job_type']),
                title=str(entry.get('title') or entry['job_type']),
                fields=list(entry.get('fields') or []),
                required=list(entry.get('required') or []),
                tabs=list(entry.get('tabs') or []),
                cli_options=list(entry.get('cli_options') or []),
            )
            for entry in schemas
        }

        # Prefer ids the schema actually has a row for: the job-types table also
        # carries abstract ids (`tomo_align_tilt_series`) that the schema splits
        # into concrete variants, and picking one of those would leave the node
        # with no form.
        known = set(self.jobs)
        mapping: Dict[str, str] = {}
        outputs: Dict[str, List[str]] = {}
        skipped: List[Tuple[str, Mapping[str, Any]]] = []
        for entry in job_types:
            label = entry.get('process_type_label')
            if not label:
                continue
            job_id = str(entry['id'])
            if known and job_id not in known:
                skipped.append((str(label), entry))
                continue
            if str(label) not in mapping:
                mapping[str(label)] = job_id
                outputs[str(label)] = [str(name) for name in entry.get('outputs') or []]
        # A label whose only ids are abstract still needs an answer, so the
        # family fallback in `job_for_label` can find its variants.
        for label, entry in skipped:
            if label not in mapping:
                mapping[label] = str(entry['id'])
                outputs[label] = [str(name) for name in entry.get('outputs') or []]
        self.label_to_job = mapping
        self.label_outputs = outputs

    # -- lookups ---------------------------------------------------------
    def job_type_for_label(self, type_label: str) -> Optional[str]:
        return self.label_to_job.get(type_label)

    def outputs_for_label(self, type_label: str) -> List[str]:
        return self.label_outputs.get(type_label, [])

    def job_for_label(self, type_label: str) -> Optional[JobType]:
        """The schema entry for a pipeline label, following variant families.

        The two tables disagree on granularity: ``/api/project/job-types`` lists
        one entry per *job* (``tomo_import``), while the schema splits that into
        variants (``tomo_import_serialem``). The first variant is the default
        one, which is what a node shows until the user picks otherwise.
        """
        job_id = self.job_type_for_label(type_label)
        if job_id is None:
            return None
        exact = self.jobs.get(job_id)
        if exact is not None:
            return exact
        prefix = f'{job_id}_'
        for candidate in self.jobs.values():
            if candidate.job_type.startswith(prefix):
                return candidate
        # `/api/project/job-types` also lists abstract entries no schema row
        # carries: `tomo_align_tilt_series` covers three concrete variants.
        family = '_'.join(job_id.split('_')[:3])
        for candidate in self.jobs.values():
            if candidate.job_type.startswith(f'{family}_'):
                return candidate
        return None

    def default_values(self, type_label: str) -> Dict[str, Any]:
        """A node's starting parameters, in FlowGram's binding shape."""
        job = self.job_for_label(type_label)
        if job is None:
            return {}
        return {
            str(field['variable']): {'type': 'constant', 'content': field.get('default', '')}
            for field in job.fields
        }

    def values_for_job_type(self, job_type: str) -> Dict[str, Any]:
        job = self.jobs.get(job_type)
        if job is None:
            return {}
        return {
            str(field['variable']): {'type': 'constant', 'content': field.get('default', '')}
            for field in job.fields
        }


SCHEMA = SchemaCache()


def job_node_type(cache: SchemaCache, type_label: str) -> str:
    """Node type id for a pipeline label.

    Uses the resolved schema job type, so the inspector's registered definition
    and the node on the canvas always agree. An unknown label keeps its own
    name, which renders a node with no fields rather than crashing.
    """
    resolved = cache.job_type_for_label(type_label)
    if resolved is not None:
        return f'relion.{resolved}'
    return f'relion.{type_label.removeprefix("relion.")}'


def filled_values(
    cache: SchemaCache,
    type_label: str,
    input_nodes: Sequence[str],
    producer_of: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Default values with the pipeline's own input paths filled in.

    For every ``input_node`` field, the node the pipeline edge names for that
    consumer becomes the value, when its type matches what the field accepts.
    Nothing is invented - a slot whose producer the pipeline does not declare
    stays at its schema default.
    """
    values = cache.default_values(type_label)
    job = cache.job_for_label(type_label)
    if job is None:
        return values
    for field in job.fields:
        if field.get('kind') != 'input_node':
            continue
        node_type = field.get('node_type')
        if not node_type:
            continue
        variable = str(field['variable'])
        current = values.get(variable)
        if isinstance(current, dict) and current.get('content'):
            continue
        for name in input_nodes:
            producer = producer_of.get(name)
            if producer and str(producer.get('type_label', '')).startswith(str(node_type)):
                values[variable] = {'type': 'constant', 'content': name}
                break
    return values


def workflow_from_pipeline(
    cache: SchemaCache,
    project: Mapping[str, str],
    graph: Mapping[str, Any],
    layout: layout_module.Layout,
) -> Dict[str, Any]:
    """One RELION project as a FlowGram workflow document."""
    by_name = {process['name']: process for process in graph['processes']}
    producer_of = {node['name']: node for node in graph['nodes']}
    consumed_by: Dict[str, List[str]] = {}
    for edge in graph['edges']['inputs']:
        consumed_by.setdefault(edge['process'], []).append(edge['node'])

    nodes = []
    for box in layout.nodes:
        process = by_name.get(box.primary)
        alias = process.get('alias') if process else None
        label = alias if alias and alias != 'None' else (process or {}).get(
            'type_label', box.primary
        )
        extra = f' (+{len(box.processes) - 1})' if len(box.processes) > 1 else ''
        type_label = (process or {}).get('type_label', '')
        nodes.append(
            {
                'id': box.primary,
                'type': job_node_type(cache, type_label),
                'meta': {'position': {'x': box.x, 'y': box.y}},
                'data': {
                    'title': f'{label}{extra}',
                    'inputsValues': filled_values(
                        cache,
                        type_label,
                        consumed_by.get(box.primary, []),
                        producer_of,
                    ),
                    'status': STATUS.get((process or {}).get('status', ''), 'pending'),
                    'relion': {
                        'process': box.primary,
                        'kind': type_label,
                        'pipelineStatus': (process or {}).get('status', 'Scheduled'),
                        'jobNumber': (process or {}).get('job_number'),
                    },
                    # What this job writes. Declaring it is what makes the
                    # variable picker offer real upstream files.
                    'outputs': {
                        'type': 'object',
                        'properties': {
                            name: {'type': 'string'}
                            for name in cache.outputs_for_label(type_label)
                        },
                    },
                },
            }
        )

    edges = [
        {'id': f'e{index}', 'sourceNodeID': edge.source, 'targetNodeID': edge.target}
        for index, edge in enumerate(layout.edges)
    ]

    # Wire the pipeline's roots to a start node: its `outputs` schema is what
    # the run dialog renders, so the execution intent is expressed in stock UI.
    has_predecessor = {edge['targetNodeID'] for edge in edges}
    roots = [node['id'] for node in nodes if node['id'] not in has_predecessor]
    first = layout.nodes[0] if layout.nodes else None
    start_node = {
        'id': EXECUTION_NODE_ID,
        'type': 'start',
        'meta': {
            'position': {
                'x': (first.x if first else 0) - 320,
                'y': first.y if first else 0,
            }
        },
        'data': {'title': 'Execution', 'outputs': EXECUTION_SCHEMA},
    }
    for index, root in enumerate(roots):
        edges.append(
            {
                'id': f'e-start-{index}',
                'sourceNodeID': EXECUTION_NODE_ID,
                'targetNodeID': root,
            }
        )

    return {
        'id': f'pipeline:{project["path"]}',
        'projectId': project['path'],
        'name': 'Pipeline',
        'revision': 1,
        'schema': {'nodes': [start_node, *nodes], 'edges': edges},
        'updatedAt': _now(),
    }


def project_from_relion(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """A RELION project as a FlowGram project.

    FlowGram's `Project` is `{id, name, createdAt, directory}`. The path is the
    only stable identity a RELION project has, so it is the id.
    """
    added = entry.get('added_at') or 0
    return {
        'id': entry['path'],
        'name': entry['name'],
        'createdAt': _iso(float(added)),
        'directory': entry['path'],
        'path': entry['path'],
    }


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _iso(seconds: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .isoformat()
        .replace('+00:00', 'Z')
    )


def state_runs_for(project_path: str) -> List[Dict[str, Any]]:
    """Run records for a project, as stored by the journal.

    Kept here rather than in the journal so the projection module has no
    dependency on the event loop.
    """
    from . import journal

    return [dict(run) for run in journal.runs_for_project(project_path)]


def agent_base() -> str:
    """Where the agent answers, for the handoff dialog's MCP URL."""
    return os.environ.get('RELION_WEB_BASE', 'http://127.0.0.1:8765')
