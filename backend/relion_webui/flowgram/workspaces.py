# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Workspaces: RELION projects as FlowGram "workflows".

A workflow in this UI is a *draft overlay* on one RELION project, not a second
pipeline format. What it holds is the part of the canvas the project does not:
nodes the user placed but has not queued yet, their edges, and the per-node
parameter edits made on the canvas. None of that touches
``default_pipeline.star`` or any job directory - a workflow is a soft grouping,
stored under the agent's config directory (``~/.relion_web``,
``RELION_WEB_HOME``), exactly where the project list already lives.

On every read the overlay is merged with the project's live pipeline
projection, so a job queued in RELION's own GUI appears too, and a draft node
is promoted the moment the queue reserves a process for it.

Ported from ``flowgram-app/bridge/workspaces.ts``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

STORE_FILENAME = 'flowgram-workspaces.json'


def config_directory() -> Path:
    """The agent's per-user config directory.

    Mirrors ``projects.default_config_directory`` rather than importing it, so
    this module stays usable without a ProjectRegistry.
    """
    override = os.environ.get('RELION_WEB_HOME')
    return Path(override).expanduser() if override else Path.home() / '.relion_web'


class WorkspaceStore:
    """Draft overlays, persisted as one JSON file.

    Writes are atomic: the draft is the only copy of a node that has not been
    queued yet, so a half-written file would lose work.
    """

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = Path(directory) if directory else config_directory()
        self.file = self.directory / STORE_FILENAME
        self._lock = threading.Lock()
        self._data: Optional[Dict[str, Any]] = None

    # -- storage ---------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data
        try:
            payload = json.loads(self.file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        self._data = {
            'counter': int(payload.get('counter') or 0),
            'workspaces': dict(payload.get('workspaces') or {}),
        }
        return self._data

    def _save(self) -> None:
        data = self._load()
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix(f'.{os.getpid()}.tmp')
        temporary.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.file)

    # -- reads -----------------------------------------------------------
    def list(self, project_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            items = [
                workspace
                for workspace in data['workspaces'].values()
                if workspace.get('projectId') == project_id and not workspace.get('archived')
            ]
        items.sort(key=lambda workspace: workspace.get('updatedAt', ''), reverse=True)
        return items

    def get(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            workspace = self._load()['workspaces'].get(workspace_id)
        if workspace is None or workspace.get('archived'):
            return None
        return workspace

    def ensure_default(self, project_id: str) -> Dict[str, Any]:
        """The project's default workspace, created on first use."""
        existing = self.list(project_id)
        for workspace in existing:
            if workspace.get('name') == 'Main':
                return workspace
        return self.create(project_id, 'Main')

    # -- writes ----------------------------------------------------------
    def create(self, project_id: str, name: str) -> Dict[str, Any]:
        with self._lock:
            data = self._load()
            data['counter'] += 1
            workspace_id = f'ws-{data["counter"]}'
            timestamp = _now()
            workspace = {
                'id': workspace_id,
                'projectId': project_id,
                'name': name.strip() or f'Workspace {data["counter"]}',
                'revision': 1,
                'nodes': [],
                'edges': [],
                'titles': {},
                'paramEdits': {},
                'createdAt': timestamp,
                'updatedAt': timestamp,
            }
            data['workspaces'][workspace_id] = workspace
            self._save()
        return workspace

    def save(
        self,
        workspace_id: str,
        *,
        name: Optional[str] = None,
        schema: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Save a canvas.

        The caller sends the whole canvas (as FlowGram does); this keeps only
        what the overlay owns - nodes that are not pipeline processes yet, the
        edges between any nodes, and the title/parameter edits of process
        nodes, so the next read can re-apply them to the projection.
        """
        with self._lock:
            data = self._load()
            workspace = data['workspaces'].get(workspace_id)
            if workspace is None or workspace.get('archived'):
                return None

            if name:
                workspace['name'] = name
            if schema is not None:
                nodes = list(schema.get('nodes') or [])
                workspace['nodes'] = [node for node in nodes if is_draft(node)]
                workspace['paramEdits'] = collect_parameter_edits(nodes)
                workspace['titles'] = collect_titles(nodes)
                edges = list(schema.get('edges') or [])
                workspace['edges'] = [
                    edge
                    for edge in edges
                    if not str(edge.get('id', '')).startswith('e-start-')
                ]
            workspace['revision'] = int(workspace.get('revision', 1)) + 1
            workspace['updatedAt'] = _now()
            self._save()
        return workspace

    def archive(self, workspace_id: str) -> bool:
        with self._lock:
            data = self._load()
            workspace = data['workspaces'].get(workspace_id)
            if workspace is None:
                return False
            workspace['archived'] = True
            workspace['updatedAt'] = _now()
            self._save()
        return True

    def promote(self, workspace_id: str, draft_id: str, process_name: str) -> None:
        """Turn a queued draft into its reserved process.

        The overlay drops the draft and re-points the edges that named it, so
        the canvas shows one node across the transition rather than two.
        """
        with self._lock:
            data = self._load()
            workspace = data['workspaces'].get(workspace_id)
            if workspace is None:
                return
            workspace['nodes'] = [
                node for node in workspace.get('nodes', []) if node.get('id') != draft_id
            ]
            workspace['edges'] = [
                {
                    **edge,
                    'sourceNodeID': process_name
                    if edge.get('sourceNodeID') == draft_id
                    else edge.get('sourceNodeID'),
                    'targetNodeID': process_name
                    if edge.get('targetNodeID') == draft_id
                    else edge.get('targetNodeID'),
                }
                for edge in workspace.get('edges', [])
            ]
            workspace['updatedAt'] = _now()
            self._save()


def is_draft(node: Mapping[str, Any]) -> bool:
    """A draft node id is namespaced so it cannot collide with a process name."""
    return str(node.get('id', '')).startswith('draft:')


def collect_titles(nodes: List[Mapping[str, Any]]) -> Dict[str, str]:
    """Titles the user renamed on the canvas, keyed by node id."""
    titles: Dict[str, str] = {}
    for node in nodes:
        if is_draft(node):
            continue
        data = node.get('data') or {}
        title = data.get('title')
        if isinstance(title, str):
            titles[str(node['id'])] = title
    return titles


def collect_parameter_edits(nodes: List[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Constant parameter edits, keyed by process name.

    Only constants are recorded: a reference depends on the graph around it and
    is resolved at submit time, so it must not be frozen into the overlay.
    """
    edits: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        if is_draft(node):
            continue
        values = (node.get('data') or {}).get('inputsValues') or {}
        constants = {
            str(key): value.get('content')
            for key, value in values.items()
            if isinstance(value, dict) and value.get('type') == 'constant'
        }
        if constants:
            edits[str(node['id'])] = constants
    return edits


def merge(
    workflow: Mapping[str, Any],
    workspace: Mapping[str, Any],
) -> Dict[str, Any]:
    """A workspace: the project's live pipeline, plus what the canvas adds.

    The pipeline is projected fresh on every read, so a job queued in RELION's
    own GUI appears here too. The overlay contributes only what the project
    cannot know. Merging is by node id, so promotion collapses the two into one
    node without any bookkeeping here.
    """
    edits: Mapping[str, Mapping[str, Any]] = workspace.get('paramEdits') or {}
    titles: Mapping[str, str] = workspace.get('titles') or {}

    def apply(node: Mapping[str, Any], key: str) -> Dict[str, Any]:
        data = dict(node.get('data') or {})
        if key in titles:
            data['title'] = titles[key]
        parameter_edits = edits.get(key)
        if parameter_edits:
            values = dict(data.get('inputsValues') or {})
            values.update(
                {
                    variable: {'type': 'constant', 'content': content}
                    for variable, content in parameter_edits.items()
                }
            )
            data['inputsValues'] = values
        return {**node, 'data': data}

    pipeline_nodes = [
        node
        if node.get('id') == 'relion.execution'
        else apply(node, str(node['id']))
        for node in workflow['schema']['nodes']
    ]
    drafts = [
        apply(node, str(node['id'])) for node in workspace.get('nodes', [])
    ]
    return {
        **workflow,
        'id': workspace['id'],
        'name': workspace.get('name', 'Main'),
        'revision': workspace.get('revision', 1),
        'schema': {
            'nodes': [*pipeline_nodes, *drafts],
            'edges': [*workflow['schema']['edges'], *workspace.get('edges', [])],
        },
        'updatedAt': workspace.get('updatedAt') or _now(),
    }


def _now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(time.time(), tz=timezone.utc)
        .isoformat()
        .replace('+00:00', 'Z')
    )
