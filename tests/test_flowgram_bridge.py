# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""The FlowGram bridge's pure parts: layout, projection and workspace overlay.

These are the pieces that used to live in TypeScript and had no tests beyond
the canvas's own; they are pure functions over data, so they are worth pinning
here - a wrong column rank or a lost draft is invisible until someone looks at
the canvas.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))

from relion_webui.flowgram import layout, translate, workspaces  # noqa: E402


def graph(processes, inputs=(), outputs=()):
    """The shape `/api/project/graph` returns.

    ``outputs`` is ``(process, node)`` - the pipeline's own direction, which is
    what the projection reads. ``inputs`` is ``(node, process)``.
    """
    return {
        'processes': [{'name': name} for name in processes],
        'nodes': [
            {'name': node, 'type_label': f'Node.star.relion.{process}', 'produced_by': process}
            for process, node in outputs
        ],
        'edges': {
            'inputs': [{'node': node, 'process': process} for node, process in inputs],
            'outputs': [
                {'process': process, 'node': node} for process, node in outputs
            ],
        },
    }


# ------------------------------------------------------------------- layout


def test_layout_reads_the_agents_edge_shape():
    """`edges` entries are dicts, not tuples - the first port got this wrong.

    Every node landing in column 0 is what a silent shape mismatch looks like:
    the only symptom is a canvas with one very tall column.
    """
    pipeline = graph(
        ['A', 'B', 'C'],
        inputs=[('a', 'B'), ('b', 'C')],
        outputs=[('A', 'a'), ('B', 'b')],
    )
    assert layout.process_edges(pipeline) == [('A', 'B'), ('B', 'C')]

    placed = layout.layout_pipeline(pipeline)
    columns = {node.primary: round(node.x) for node in placed.nodes}
    assert columns['A'] < columns['B'] < columns['C']
    assert len(placed.edges) == 2


def test_layout_condenses_a_refinement_loop_into_one_node():
    """RELION's refinement cycle must not stretch the graph sideways."""
    # Refine <-> CTF is the cycle; Polish consumes the loop's output.
    pipeline = graph(
        ['Refine', 'CTF', 'Polish'],
        inputs=[('r', 'CTF'), ('c', 'Refine'), ('c', 'Polish')],
        outputs=[('Refine', 'r'), ('CTF', 'c'), ('Polish', 'p')],
    )
    placed = layout.layout_pipeline(pipeline)
    loop = next(node for node in placed.nodes if len(node.processes) > 1)
    assert set(loop.processes) == {'Refine', 'CTF'}
    # Two columns: the loop, then Polish. A longest-path rank without
    # condensation would have tried to give each member its own column.
    assert len({round(node.x) for node in placed.nodes}) == 2


def test_layout_draws_an_edge_per_node_pair_not_per_file():
    """Several files produced by one job are one edge on the canvas."""
    pipeline = graph(
        ['A', 'B'],
        inputs=[('a1', 'B'), ('a2', 'B')],
        outputs=[('A', 'a1'), ('A', 'a2')],
    )
    placed = layout.layout_pipeline(pipeline)
    assert len(placed.edges) == 1


# ---------------------------------------------------------------- projection


class FakeJobType:
    def __init__(self, job_type, fields=(), outputs=()):
        self.job_type = job_type
        self.title = job_type
        self.fields = list(fields)
        self.required = []
        self.tabs = []
        self.cli_options = []
        self.outputs = list(outputs)


@pytest.fixture()
def cache():
    entry = translate.SchemaCache()
    entry.jobs = {
        'tomo_import_serialem': FakeJobType(
            'tomo_import_serialem',
            fields=[{'variable': 'fn_in', 'default': ''}],
            outputs=['tilt_series.star'],
        ),
        'tomo_align_aretomo': FakeJobType(
            'tomo_align_aretomo',
            fields=[
                {
                    'variable': 'in_tiltseries',
                    'kind': 'input_node',
                    'node_type': 'TomogramGroupMetadata.star.relion',
                    'default': '',
                }
            ],
            outputs=['aligned_tilt_series.star'],
        ),
    }
    entry.label_to_job = {
        'relion.importtomo': 'tomo_import_serialem',
        'relion.aligntiltseries': 'tomo_align_aretomo',
    }
    entry.label_outputs = {
        'relion.importtomo': ['tilt_series.star'],
        'relion.aligntiltseries': ['aligned_tilt_series.star'],
    }
    return entry


def test_projection_fills_an_input_slot_from_the_pipeline_edge(cache):
    """`in_tiltseries` must start at the file the edge already names."""
    pipeline = {
        'processes': [
            {'name': 'Import/job001', 'type_label': 'relion.importtomo', 'status': 'Succeeded'},
            {'name': 'Align/job002', 'type_label': 'relion.aligntiltseries', 'status': 'Scheduled'},
        ],
        'nodes': [
            {
                'name': 'Import/job001/tilt_series.star',
                'type_label': 'TomogramGroupMetadata.star.relion.tomo.import',
            }
        ],
        'edges': {
            'inputs': [
                {'node': 'Import/job001/tilt_series.star', 'process': 'Align/job002'}
            ],
            'outputs': [
                {'process': 'Import/job001', 'node': 'Import/job001/tilt_series.star'}
            ],
        },
    }
    placed = layout.layout_pipeline(pipeline)
    document = translate.workflow_from_pipeline(
        cache,
        {'path': '/tmp/p', 'name': 'p'},
        pipeline,
        placed,
    )
    align = next(node for node in document['schema']['nodes'] if node['id'] == 'Align/job002')
    assert align['data']['inputsValues']['in_tiltseries'] == {
        'type': 'constant',
        'content': 'Import/job001/tilt_series.star',
    }
    # The node type follows the schema, not the pipeline label.
    assert align['type'] == 'relion.tomo_align_aretomo'


def test_projection_declares_each_nodes_outputs(cache):
    """The variable picker offers these, so they have to be real filenames."""
    pipeline = {
        'processes': [{'name': 'Import/job001', 'type_label': 'relion.importtomo', 'status': 'Succeeded'}],
        'nodes': [],
        'edges': {'inputs': [], 'outputs': []},
    }
    placed = layout.layout_pipeline(pipeline)
    document = translate.workflow_from_pipeline(
        cache, {'path': '/tmp/p', 'name': 'p'}, pipeline, placed
    )
    node = document['schema']['nodes'][1]
    assert node['data']['outputs']['properties'] == {'tilt_series.star': {'type': 'string'}}


def test_projection_wires_roots_to_the_execution_start_node(cache):
    pipeline = {
        'processes': [
            {'name': 'A/job001', 'type_label': 'relion.importtomo', 'status': 'Scheduled'},
            {'name': 'B/job002', 'type_label': 'relion.importtomo', 'status': 'Scheduled'},
        ],
        'nodes': [],
        'edges': {'inputs': [], 'outputs': []},
    }
    placed = layout.layout_pipeline(pipeline)
    document = translate.workflow_from_pipeline(
        cache, {'path': '/tmp/p', 'name': 'p'}, pipeline, placed
    )
    starts = [
        edge
        for edge in document['schema']['edges']
        if edge['sourceNodeID'] == translate.EXECUTION_NODE_ID
    ]
    # Two independent roots, two start edges - not one, and not zero.
    assert {edge['targetNodeID'] for edge in starts} == {'A/job001', 'B/job002'}


# ---------------------------------------------------------------- workspaces


def test_workspace_keeps_drafts_out_of_the_pipeline(tmp_path):
    store = workspaces.WorkspaceStore(tmp_path)
    workspace = store.create('/tmp/project', 'Main')

    canvas = {
        'nodes': [
            {'id': 'Import/job001', 'type': 'relion.tomo_import_serialem', 'data': {'title': 'import'}},
            {
                'id': 'draft:ext_1',
                'type': 'relion.external',
                'data': {
                    'title': 'draft',
                    'inputsValues': {'fn_exe': {'type': 'constant', 'content': '/bin/true'}},
                },
            },
        ],
        'edges': [],
    }
    store.save(workspace['id'], schema=canvas)

    saved = store.get(workspace['id'])
    # Only the draft is stored as a node; the pipeline node is the project's.
    assert [node['id'] for node in saved['nodes']] == ['draft:ext_1']
    assert saved['paramEdits'] == {}


def test_workspace_records_constant_edits_but_not_references(tmp_path):
    """A reference depends on the graph around it and must not be frozen."""
    store = workspaces.WorkspaceStore(tmp_path)
    workspace = store.create('/tmp/project', 'Main')
    store.save(
        workspace['id'],
        schema={
            'nodes': [
                {
                    'id': 'Import/job001',
                    'type': 'relion.tomo_import_serialem',
                    'data': {
                        'title': 'import',
                        'inputsValues': {
                            'nr_mpi': {'type': 'constant', 'content': 4},
                            'fn_in': {'type': 'ref', 'content': ['A/job001', 'x.star']},
                        },
                    },
                }
            ],
            'edges': [],
        },
    )
    saved = store.get(workspace['id'])
    assert saved['paramEdits'] == {'Import/job001': {'nr_mpi': 4}}


def test_promotion_repoints_edges_at_the_reserved_process(tmp_path):
    store = workspaces.WorkspaceStore(tmp_path)
    workspace = store.create('/tmp/project', 'Main')
    store.save(
        workspace['id'],
        schema={
            'nodes': [{'id': 'draft:ext_1', 'type': 'relion.external', 'data': {}}],
            'edges': [
                {'id': 'e0', 'sourceNodeID': 'draft:ext_1', 'targetNodeID': 'Next/job009'}
            ],
        },
    )
    store.promote(workspace['id'], 'draft:ext_1', 'External/job008')

    saved = store.get(workspace['id'])
    assert saved['nodes'] == []
    assert saved['edges'][0]['sourceNodeID'] == 'External/job008'


def test_merge_applies_edits_without_losing_pipeline_nodes(tmp_path):
    workflow = {
        'id': 'pipeline:/tmp/project',
        'projectId': '/tmp/project',
        'name': 'Pipeline',
        'revision': 1,
        'updatedAt': '',
        'schema': {
            'nodes': [
                {'id': 'relion.execution', 'type': 'start', 'data': {}},
                {
                    'id': 'Import/job001',
                    'type': 'relion.tomo_import_serialem',
                    'data': {
                        'title': 'import',
                        'inputsValues': {'nr_mpi': {'type': 'constant', 'content': 1}},
                    },
                },
            ],
            'edges': [],
        },
    }
    workspace = {
        'id': 'ws-1',
        'name': 'Trial',
        'revision': 7,
        'updatedAt': '2026-01-01T00:00:00Z',
        'nodes': [{'id': 'draft:x', 'type': 'relion.external', 'data': {'title': 'x'}}],
        'edges': [],
        'titles': {'Import/job001': 'import renamed'},
        'paramEdits': {'Import/job001': {'nr_mpi': 8}},
    }
    merged = workspaces.merge(workflow, workspace)

    assert merged['id'] == 'ws-1'
    assert merged['revision'] == 7
    imported = next(node for node in merged['schema']['nodes'] if node['id'] == 'Import/job001')
    assert imported['data']['title'] == 'import renamed'
    assert imported['data']['inputsValues']['nr_mpi']['content'] == 8
    # The draft comes after the pipeline's own nodes.
    assert merged['schema']['nodes'][-1]['id'] == 'draft:x'


# --------------------------------------------------------------------- forms


def test_forms_map_relion_kinds_to_flowgram_editors():
    """The mapping lives in one place now: Python, not TypeScript.

    Every RELION option kind has exactly one FlowGram control, and a field the
    form cannot render is a bug - the canvas would show an empty row.
    """
    from relion_webui.flowgram import forms

    cases = {
        'boolean': ('value', 'boolean'),
        'radio': ('select', None),
        'slider': ('number', 'number'),
        'path': ('path-input', None),
        'input_node': ('node-input', None),
        'string': ('value', 'string'),
    }
    for kind, (editor, value_type) in cases.items():
        field = {
            'variable': 'v',
            'label': 'V:',
            'kind': kind,
            'options': [{'value': 'a', 'label': 'A'}] if kind == 'radio' else None,
            'min': 0,
            'max': 10,
        }
        definition = forms.field_definition(field, required=False)
        assert definition['editor'] == editor, kind
        if value_type is not None:
            assert definition['type'] == value_type, kind
    # Labels lose RELION's trailing colon, and keys sit under inputsValues.
    definition = forms.field_definition({'variable': 'nr_mpi', 'label': 'Number: ', 'kind': 'slider'}, False)
    assert definition['label'] == 'Number'
    assert definition['key'] == 'inputsValues.nr_mpi'


def test_forms_order_sections_io_first_and_resources_last():
    """A form reads I/O -> algorithm -> resources, whatever the C++ table says."""
    from relion_webui.flowgram import forms

    job = {
        'job_type': 'x',
        'fields': [
            {'variable': 'nr_mpi', 'label': 'MPI', 'kind': 'slider'},
            {'variable': 'box', 'label': 'Box', 'kind': 'slider'},
            {'variable': 'in_tiltseries', 'label': 'In', 'kind': 'input_node',
             'node_type': 'TomogramGroupMetadata.star.relion'},
        ],
        'tabs': [
            {'title': 'Run', 'fields': ['nr_mpi']},
            {'title': 'CTFFIND-4.1', 'fields': ['box']},
            {'title': 'I/O', 'fields': ['in_tiltseries']},
        ],
    }
    definition = forms.node_definition(job)
    assert [field['section'] for field in definition['fields']] == [
        'I/O',
        'CTFFIND-4.1',
        '计算资源',
    ]


def test_forms_put_the_advanced_flag_list_on_other_args():
    """`other_args` is the advanced panel; its tip names what it accepts."""
    from relion_webui.flowgram import forms

    job = {
        'job_type': 'x',
        'fields': [
            {'variable': 'other_args', 'label': 'Other', 'kind': 'string'},
        ],
        'tabs': [{'title': 'Run', 'fields': ['other_args']}],
        'cli_options': [
            {'flag': '--wanted', 'usage': 'does a thing', 'used_by_form': False},
            {'flag': '--covered', 'usage': 'in the form', 'used_by_form': True},
            {'flag': '--hidden', 'usage': 'internal', 'hidden': True},
        ],
    }
    field = forms.node_definition(job)['fields'][0]
    assert '--wanted' in field['tip']
    assert 'does a thing' in field['tip']
    assert '--covered' not in field['tip']
    assert '--hidden' not in field['tip']
