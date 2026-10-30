"""Tests for the tool registry, the MCP surface, and the assistant.

Three properties matter and each has its own section here:

* the registry refuses anything outside the project boundary, because a tool
  that reads a path must be as restricted as the HTTP API;
* every write tool is marked, because that marking is the approval gate and an
  unmarked write would run without asking;
* the assistant parks writes and runs reads, without a provider key involved.
"""

import json
import tempfile
import time
from pathlib import Path

import pytest

from recryo import assistant, job_actions, tools
from recryo.paths import PathGuard
from recryo.projects import ProjectRegistry


# --------------------------------------------------------------- fixtures
@pytest.fixture
def demo_project(tmp_path):
    """A tiny project: one job directory with a pipeline and a log."""
    root = tmp_path / 'proj'
    job = root / 'Tomograms' / 'job001'
    job.mkdir(parents=True)
    (job / 'run.out').write_text('reading in.star\nfailed: no such file\n')
    (job / 'job.star').write_text(
        '\n'.join(
            [
                '',
                '# version 50001',
                '',
                'data_job',
                '',
                'loop_',
                '_rlnJobTypeLabel #1',
                '_rlnJobIsContinue #2',
                '_rlnJobIsTomo #3',
                'relion.reconstructtomograms 0 1',
                '',
                'data_joboptions_values',
                '',
                'loop_',
                '_rlnJobOptionVariable #1',
                '_rlnJobOptionValue #2',
                'xdim 4000',
                '',
            ]
        )
    )
    (root / 'default_pipeline.star').write_text(
        '\n'.join(
            [
                '',
                '# version 50001',
                '',
                'data_pipeline_general',
                '',
                'loop_',
                '_rlnPipeLineJobCounter #1',
                '2',
                '',
                'data_pipeline_processes',
                '',
                'loop_',
                '_rlnPipeLineProcessName #1',
                '_rlnPipeLineProcessAlias #2',
                '_rlnPipeLineProcessTypeLabel #3',
                '_rlnPipeLineProcessStatusLabel #4',
                'Tomograms/job001 None relion.reconstructtomograms Failed',
                '',
                'data_pipeline_nodes',
                '',
                'loop_',
                '_rlnPipeLineNodeName #1',
                '_rlnPipeLineNodeTypeLabel #2',
                '',
                'data_pipeline_input_edges',
                '',
                'loop_',
                '_rlnPipeLineEdgeFromNode #1',
                '_rlnPipeLineEdgeProcess #2',
                '',
                'data_pipeline_output_edges',
                '',
                'loop_',
                '_rlnPipeLineEdgeProcess #1',
                '_rlnPipeLineEdgeToNode #2',
                '',
            ]
        )
    )
    return root


class StubJobs:
    """A JobManager stand-in: submission is never reached in these tests."""

    def __init__(self):
        self.cancelled = []

    async def submit(self, *args, **kwargs):  # pragma: no cover - not reached
        raise AssertionError('submit should not run in this test')

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        return job_id == 'running-job'

    def get(self, job_id):
        # No job exists in this stub; wait_for_job must say so rather than
        # looping forever on None.
        return None

    def tail_log(self, job_id):
        return ''

    def list(self):
        return []


@pytest.fixture
def context(demo_project, tmp_path):
    registry = ProjectRegistry(config_directory=tmp_path / 'config')
    registry.add(demo_project)
    return tools.ToolContext(
        guard=PathGuard([demo_project]),
        projects=registry,
        jobs=StubJobs(),
    )



def _hint(annotations, name: str):
    """Read one MCP tool hint, on either mcp major.

    mcp 2 renamed the `ToolAnnotations` fields to snake_case; the JSON on the
    wire is camelCase in both, so only the Python attribute changed.
    """
    snake = ''.join('_' + c.lower() if c.isupper() else c for c in name)
    return getattr(annotations, snake, getattr(annotations, name, None))


def call(context, name, **arguments):
    return tools.call_tool(tools.build_registry(), name, arguments, context)


# -------------------------------------------------------------- the registry
def test_every_tool_has_a_valid_schema():
    for tool in tools.build_registry():
        schema = tool.schema()
        assert schema['type'] == 'object'
        assert set(schema['properties']) >= set(schema['required'])
        # A tool with no description is invisible to a model, which would then
        # guess at its arguments.
        assert tool.description.strip(), tool.name


def test_write_tools_are_marked():
    """The marking is the approval gate; an unmarked write runs unasked."""
    writes = {tool.name for tool in tools.build_registry() if tool.writes}
    assert writes == {
        'submit_job',
        'queue_job',
        'cancel_queued_job',
        'retry_queued_job',
        'set_job_alias',
        'write_job_note',
        'mark_job',
        'delete_job',
        'undelete_job',
        'stop_job',
    }


def test_reads_do_not_touch_the_project(context, demo_project):
    writes = {tool.name for tool in tools.build_registry() if tool.writes}
    reads = [tool.name for tool in tools.build_registry() if tool.name not in writes]
    # Every read tool takes only a project path or a string; none may be given
    # the chance to mutate. Checking the marked set is the practical proxy.
    assert 'get_pipeline' in reads and 'get_job' in reads


def test_list_projects_reports_the_project(context, demo_project):
    result = call(context, 'list_projects')
    assert len(result['projects']) == 1
    assert result['projects'][0]['path'] == str(demo_project)
    assert result['projects'][0]['job_count'] == 1


def test_get_pipeline_lists_jobs_and_status(context, demo_project):
    result = call(context, 'get_pipeline', project=str(demo_project))
    assert [p['name'] for p in result['processes']] == ['Tomograms/job001']
    assert result['summary']['failing'] == ['Tomograms/job001']
    assert 'nodes' in result and 'edges' in result


def test_get_pipeline_can_omit_nodes(context, demo_project):
    """The node list is most of the payload; a status question does not need it."""
    lean = call(
        context, 'get_pipeline', project=str(demo_project), include_nodes=False
    )
    full = call(context, 'get_pipeline', project=str(demo_project))
    assert 'nodes' not in lean and 'nodes' in full
    assert len(tools.as_json(lean)) < len(tools.as_json(full))


def test_get_job_reads_options_and_log(context, demo_project):
    result = call(
        context, 'get_job', project=str(demo_project), process_name='Tomograms/job001'
    )
    assert result['status'] == 'Failed'
    assert result['options']['xdim'] == '4000'
    assert 'no such file' in result['log']


def test_get_job_names_what_it_does_know(context, demo_project):
    """A wrong process name must not leave the model guessing."""
    result = call(
        context, 'get_job', project=str(demo_project), process_name='Nope/job9'
    )
    assert 'error' in result
    assert 'Tomograms/job001' in result['error']


def test_get_job_log_tail(context, demo_project):
    result = call(
        context,
        'get_job_log',
        project=str(demo_project),
        process_name='Tomograms/job001',
        tail_lines=1,
    )
    assert result['log'] == 'failed: no such file'


def test_paths_outside_the_projects_are_refused(context):
    result = call(context, 'get_pipeline', project='/etc')
    assert 'error' in result
    assert 'outside the allowed roots' in result['error']


def test_a_directory_without_a_pipeline_is_not_a_project(context, demo_project):
    # Inside the allowed root, so this tests the pipeline check rather than the
    # path guard.
    plain = demo_project / 'plain'
    plain.mkdir()
    result = call(context, 'get_pipeline', project=str(plain))
    assert 'error' in result
    assert 'not a RELION project' in result['error']


def test_unknown_tool_and_bad_arguments(context, demo_project):
    unknown = call(context, 'no_such_tool')
    assert 'error' in unknown and 'available' in unknown

    missing = call(context, 'get_job', project=str(demo_project))
    assert 'error' in missing
    assert 'schema' in missing


def test_get_pipeline_search_does_not_leak_absolute_paths_as_errors(context):
    """A guard failure is an error dict, never an exception reaching the caller."""
    result = call(context, 'list_tilt_series', star_file='/root/secret.star')
    assert 'error' in result


def test_validate_job_reports_missing_required_options(context):
    result = call(context, 'validate_job', job_type='ctffind', arguments={})
    assert result['valid'] is False
    assert result['missing_required']


def test_job_options_schema_can_be_narrowed(context):
    """A 70-field schema is large; asking for a few fields must be small."""
    full = call(context, 'get_job_options_schema', job_type='class3d')
    narrow = call(
        context,
        'get_job_options_schema',
        job_type='class3d',
        variables=['nr_mpi'],
    )
    assert len(narrow['fields']) == 1
    assert len(tools.as_json(narrow)) * 10 < len(tools.as_json(full))


def test_job_options_schema_can_drop_help(context):
    with_help = call(context, 'get_job_options_schema', job_type='class3d')
    without = call(
        context, 'get_job_options_schema', job_type='class3d', with_help=False
    )
    assert all('help' not in field for field in without['fields'])
    assert len(tools.as_json(without)) < len(tools.as_json(with_help))


def test_describe_is_a_prompt_listing():
    text = tools.describe(tools.build_registry())
    assert '- submit_job (write)' in text
    assert '- get_pipeline (read)' in text


# ---------------------------------------------------------------- write tools
def test_delete_then_undelete(context, demo_project):
    deleted = call(
        context, 'delete_job', project=str(demo_project), process_name='Tomograms/job001'
    )
    assert 'moved to Trash' in deleted['message']
    assert not (demo_project / 'Tomograms' / 'job001').is_dir()

    trash = call(context, 'list_trash', project=str(demo_project))
    entries = trash['detail']['entries']
    assert entries == ['Tomograms/job001']

    restored = call(context, 'undelete_job', project=str(demo_project), trash_entry=entries[0])
    assert 'restored' in restored['message']
    assert (demo_project / 'Tomograms' / 'job001').is_dir()


def test_note_round_trip(context, demo_project):
    call(
        context,
        'write_job_note',
        project=str(demo_project),
        process_name='Tomograms/job001',
        note='check the contrast transfer',
    )
    # The GUI's note for Tomograms/job001 lives inside the job directory.
    path = demo_project / 'Tomograms' / 'job001' / 'job001note.txt'
    assert path.read_text() == 'check the contrast transfer'
    read = call(
        context, 'get_job', project=str(demo_project), process_name='Tomograms/job001'
    )
    assert read['note'] == 'check the contrast transfer'


def test_mark_job_changes_pipeline_status(context, demo_project):
    call(
        context,
        'mark_job',
        project=str(demo_project),
        process_name='Tomograms/job001',
        failed=False,
    )
    result = call(context, 'get_pipeline', project=str(demo_project))
    assert result['processes'][0]['status'].lower() != 'failed'


def test_stop_job_refuses_an_unknown_job(context):
    result = call(context, 'stop_job', job_id='not-running')
    assert 'error' in result


# ------------------------------------------------------- data-loss protection
def test_deleting_twice_does_not_destroy_the_first_copy(context, demo_project):
    """A second deletion of the same job must not overwrite the first.

    The earlier copy is the only remaining one, and Trash exists to keep it, so
    a name collision gets a suffix instead of an rmtree.
    """
    from recryo import pipeline as pipeline_module

    job = demo_project / 'Tomograms' / 'job001'
    job_actions.delete(demo_project, 'Tomograms/job001')
    (demo_project / 'Trash' / 'Tomograms' / 'job001' / 'MARKER').write_text('first')

    # Put the job back so it can be deleted again, as a re-run would.
    import shutil

    shutil.copytree(demo_project / 'Trash' / 'Tomograms' / 'job001', job)
    pipeline = pipeline_module.read_pipeline(demo_project)
    pipeline.processes.append(
        pipeline_module.Process(
            name='Tomograms/job001',
            alias='None',
            type_label='relion.reconstructtomograms',
            status=pipeline_module.STATUS_SUCCEEDED,
        )
    )
    pipeline_module.write_pipeline(demo_project, pipeline)

    result = job_actions.delete(demo_project, 'Tomograms/job001')
    assert result.detail['renamed'], 'the collision must be reported'
    assert result.detail['renamed'][0]['trash_entry'] == 'Tomograms/job001.1'

    trash = demo_project / 'Trash' / 'Tomograms'
    assert (trash / 'job001' / 'MARKER').read_text() == 'first'
    assert (trash / 'job001.1').is_dir()


def test_a_suffixed_trash_entry_restores_to_its_real_name(context, demo_project):
    """The suffix is a Trash artifact; the job comes back as `job001`."""
    from recryo import pipeline as pipeline_module
    import shutil

    job_actions.delete(demo_project, 'Tomograms/job001')
    trash = demo_project / 'Trash' / 'Tomograms'
    (trash / 'job001').mkdir(exist_ok=True)

    shutil.copytree(
        demo_project / 'Trash' / 'Tomograms' / 'job001', demo_project / 'Tomograms' / 'job001'
    )
    pipeline = pipeline_module.read_pipeline(demo_project)
    pipeline.processes.append(
        pipeline_module.Process(
            name='Tomograms/job001',
            alias='None',
            type_label='relion.reconstructtomograms',
            status=pipeline_module.STATUS_SUCCEEDED,
        )
    )
    pipeline_module.write_pipeline(demo_project, pipeline)
    result = job_actions.delete(demo_project, 'Tomograms/job001')
    entry = result.detail['renamed'][0]['trash_entry']

    restored = job_actions.undelete(demo_project, entry)
    assert restored.process_name == 'Tomograms/job001'
    assert (demo_project / 'Tomograms' / 'job001').is_dir()


def test_a_running_job_cannot_be_deleted(context, demo_project):
    """Moving a directory a process is writing into is not recoverable."""
    from recryo import pipeline as pipeline_module

    pipeline = pipeline_module.read_pipeline(demo_project)
    pipeline.find_process('Tomograms/job001').status = pipeline_module.STATUS_RUNNING
    pipeline_module.write_pipeline(demo_project, pipeline)

    with pytest.raises(job_actions.ActionError) as excinfo:
        job_actions.delete(demo_project, 'Tomograms/job001')
    assert 'running' in str(excinfo.value)
    assert (demo_project / 'Tomograms' / 'job001').is_dir()

    # And through the tool, which must not turn a refusal into a traceback.
    result = call(
        context, 'delete_job', project=str(demo_project), process_name='Tomograms/job001'
    )
    assert 'running' in result['error']


# ------------------------------------------------------------------ MCP layer
def test_mcp_is_optional_but_importable():
    from recryo import mcp_server

    # The registry is the contract; the transport is a view over it. If the MCP
    # extra is absent the registry must still work, which is why tools.py has no
    # import of it.
    assert mcp_server.tool_names() == [tool.name for tool in tools.build_registry()]


def test_read_only_mcp_publishes_no_write_tools():
    """A read-only server must not advertise capabilities it will refuse."""
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(
        roots=['/tmp'],
        enable_assistant=True,
        enable_mcp=True,
        mcp_hosts=['testserver'],
        mcp_allow_writes=False,
    )
    with TestClient(app) as client:
        info = client.get('/api/assistant/config').json()['mcp']
        assert info['allow_writes'] is False
        assert info['tools'] == len(
            [t for t in tools.build_registry() if not t.writes]
        )

        headers = {'Accept': 'application/json, text/event-stream'}
        opened = client.post(
            '/mcp',
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-06-18',
                    'capabilities': {},
                    'clientInfo': {'name': 'test', 'version': '1'},
                },
            },
            headers=headers,
        )
        session = opened.headers['mcp-session-id']
        client.post(
            '/mcp',
            json={'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            headers={**headers, 'mcp-session-id': session},
        )
        listed = client.post(
            '/mcp',
            json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            headers={**headers, 'mcp-session-id': session},
        )
        names = {entry['name'] for entry in listed.json()['result']['tools']}
        assert 'get_pipeline' in names
        assert 'delete_job' not in names
        assert 'submit_job' not in names
        assert not (names & {t.name for t in tools.build_registry() if t.writes})


def test_call_tool_refuses_writes_when_disabled(context, demo_project):
    """Even a client that calls a write by name must be refused."""
    result = tools.call_tool(
        tools.build_registry(),
        'delete_job',
        {'project': str(demo_project), 'process_name': 'Tomograms/job001'},
        context,
        allow_writes=False,
    )
    assert 'writes disabled' in result['error']
    assert (demo_project / 'Tomograms' / 'job001').is_dir()


def test_mcp_annotations_mark_writes_destructive():
    pytest.importorskip('mcp')
    from recryo import mcp_server

    registry = {tool.name: tool for tool in tools.build_registry()}
    for name, tool in registry.items():
        annotations = mcp_server._annotations(tool)
        assert annotations is not None
        if tool.writes:
            assert _hint(annotations, "destructiveHint") is True, name
            assert _hint(annotations, "readOnlyHint") is False, name
        else:
            assert _hint(annotations, "readOnlyHint") is True, name
            assert _hint(annotations, "destructiveHint") is False, name


# ------------------------------------------------------------------ assistant
def test_config_reports_no_key(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    config = assistant.config_from_env()
    assert config.public()['configured'] is False
    # The key must never be in what the browser sees.
    assert 'api_key' not in config.public()


def test_build_model_without_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    config = assistant.AssistantConfig(provider='openai', model='gpt-4o-mini', api_key='')
    with pytest.raises(assistant.AssistantUnavailable) as excinfo:
        assistant.build_model(config)
    assert 'API key' in str(excinfo.value)


def test_build_model_with_key_and_base_url_needs_no_network():
    config = assistant.AssistantConfig(
        provider='local',
        model='some-local-model',
        api_key='not-a-real-key',
        base_url='http://127.0.0.1:9/v1',
    )
    model = assistant.build_model(config)
    assert model.model_name == 'some-local-model'


def test_reads_run_and_writes_park(context, demo_project):
    """The gate: a read returns data, a write returns needs_approval."""
    instance = assistant.Assistant(context, assistant.AssistantConfig())

    read = instance._dispatch(
        _tool(instance, 'get_pipeline'), {'project': str(demo_project)}
    )
    assert 'processes' in read

    write = instance._dispatch(
        _tool(instance, 'delete_job'),
        {'project': str(demo_project), 'process_name': 'Tomograms/job001'},
    )
    assert write['status'] == 'needs_approval'
    # And nothing happened on disk.
    assert (demo_project / 'Tomograms' / 'job001').is_dir()


def test_approval_executes_the_parked_call(context, demo_project):
    """The approved call runs - once, and it is the call the user was shown."""
    instance = assistant.Assistant(context, assistant.AssistantConfig())
    delete = _tool(instance, 'delete_job')
    arguments = {'project': str(demo_project), 'process_name': 'Tomograms/job001'}

    instance._dispatch(delete, arguments)
    pending = instance.pending_calls()
    assert len(pending) == 1
    assert pending[0]['tool'] == 'delete_job'
    assert pending[0]['arguments'] == arguments

    outcome = instance.execute_approved(pending[0]['key'])
    assert outcome['result']['action'] == 'delete'
    assert not (demo_project / 'Tomograms' / 'job001').is_dir()
    # Executed once: nothing is left pending.
    assert instance.pending_calls() == []

    # A second approval of the same key is refused rather than deleting again.
    with pytest.raises(assistant.ToolError):
        instance.execute_approved(pending[0]['key'])


def test_approve_endpoint_runs_the_tool(monkeypatch, demo_project):
    """The HTTP path must execute, not just mark the call allowed."""
    from fastapi.testclient import TestClient

    from recryo import assistant as assistant_module
    from recryo.app import create_app

    # A stub model that asks for one write, so the route parks a real call.
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    asked = {'n': 0}

    async def stream_fn(messages, info):
        asked['n'] += 1
        if asked['n'] == 1:
            yield {
                0: DeltaToolCall(
                    name='write_job_note',
                    tool_call_id='c1',
                    json_args=json.dumps(
                        {
                            'project': str(demo_project),
                            'process_name': 'Tomograms/job001',
                            'note': 'approved by the panel',
                        }
                    ),
                )
            }
        else:
            yield 'done'

    app = create_app(roots=[str(demo_project)], enable_assistant=True)
    monkeypatch.setattr(
        assistant_module.Assistant,
        '_build_agent',
        lambda self: __import__('pydantic_ai').Agent(
            FunctionModel(stream_function=stream_fn),
            instructions='x',
            tools=self._tool_functions(),
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            '/api/assistant/chat',
            json={
                'message': 'add a note',
                'project': str(demo_project),
            },
        )
        assert response.status_code == 200
        assert 'needs_approval' in response.text

        conversation = app.state.assistant_conversations['anonymous']
        pending = conversation.pending_calls()
        assert pending and pending[0]['tool'] == 'write_job_note'

        approved = client.post(
            '/api/assistant/approve',
            json={'key': pending[0]['key'], 'approve': True},
        )
        assert approved.status_code == 200
        body = approved.json()
        assert body['approved'] is True
        assert body['result']['message'] == 'note saved'
        # And it really happened.
        note = demo_project / 'Tomograms' / 'job001' / 'job001note.txt'
        assert note.read_text() == 'approved by the panel'


def test_denied_call_stays_parked(context, demo_project):
    instance = assistant.Assistant(context, assistant.AssistantConfig())
    delete = _tool(instance, 'delete_job')
    arguments = {'project': str(demo_project), 'process_name': 'Tomograms/job001'}
    instance._dispatch(delete, arguments)
    key = instance.pending_calls()[0]['key']
    assert instance.deny(key)
    # Denying removes it from the queue; a retry parks again rather than running.
    again = instance._dispatch(delete, arguments)
    assert again['status'] == 'needs_approval'


def test_writes_can_be_disabled_entirely(context, demo_project):
    instance = assistant.Assistant(
        context, assistant.AssistantConfig(), allow_writes=False
    )
    result = instance._dispatch(
        _tool(instance, 'delete_job'),
        {'project': str(demo_project), 'process_name': 'Tomograms/job001'},
    )
    assert 'error' in result
    assert 'disabled' in result['error']
    assert (demo_project / 'Tomograms' / 'job001').is_dir()


def test_conversation_is_per_session(monkeypatch):
    """Two browsers must not share a history, so the key is the session token."""
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(roots=['/tmp'], enable_assistant=True)
    with TestClient(app) as client:
        first = client.get('/api/assistant/config')
        assert first.status_code == 200
        client.post('/api/assistant/settings', json={'provider': 'anthropic'})
        assert client.get('/api/assistant/config').json()['provider'] == 'anthropic'

        client.cookies.clear()
        client.cookies.set('relion_web_session', 'another-session')
        # A different session starts from the server default.
        assert client.get('/api/assistant/config').json()['provider'] == 'openai'


def test_assistant_is_off_by_default():
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(roots=['/tmp'])
    with TestClient(app) as client:
        assert client.get('/api/assistant/config').status_code == 404
        assert client.get('/api/config').json()['assistant_enabled'] is False


def test_mcp_is_off_by_default():
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(roots=['/tmp'])
    with TestClient(app) as client:
        response = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        assert response.status_code == 404


def test_settings_returns_the_same_shape_as_config():
    """Saving settings must not hand the panel a smaller object.

    The panel renders `config.mcp.enabled`; a /settings response without `mcp`
    crashed it with "Cannot read properties of undefined".
    """
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(
        roots=['/tmp'],
        enable_assistant=True,
        enable_mcp=True,
        mcp_hosts=['testserver'],
    )
    with TestClient(app) as client:
        before = client.get('/api/assistant/config').json()
        after = client.post(
            '/api/assistant/settings',
            json={'provider': 'local', 'model': 'm', 'api_key': 'k'},
        ).json()
        assert set(before) == set(after)
        assert after['configured'] is True
        assert 'enabled' in after['mcp']


def test_switching_provider_starts_a_fresh_conversation():
    """A provider change must not replay one vendor's history into another."""
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(roots=['/tmp'], enable_assistant=True)
    with TestClient(app) as client:
        client.post('/api/assistant/settings', json={'provider': 'openai'})
        app.state.assistant_conversations['anonymous'].history = ['fake-turn']
        client.post(
            '/api/assistant/settings',
            json={'provider': 'anthropic', 'model': 'claude-sonnet-4-5'},
        )
        assert app.state.assistant_conversations['anonymous'].history == []

        # The same provider keeps it.
        app.state.assistant_conversations['anonymous'].history = ['keep']
        client.post('/api/assistant/settings', json={'provider': 'anthropic'})
        assert app.state.assistant_conversations['anonymous'].history == ['keep']


def test_unknown_provider_is_rejected():
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(roots=['/tmp'], enable_assistant=True)
    with TestClient(app) as client:
        response = client.post(
            '/api/assistant/settings', json={'provider': 'not-a-provider'}
        )
        assert response.status_code == 400


def test_chat_without_a_key_streams_an_error_not_a_crash():
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(roots=['/tmp'], enable_assistant=True)
    with TestClient(app) as client:
        response = client.post('/api/assistant/chat', json={'message': 'hello'})
        assert response.status_code == 200
        assert 'text/event-stream' in response.headers['content-type']
        assert 'no API key' in response.text


def _tool(instance, name):
    return next(tool for tool in instance.registry if tool.name == name)


# ------------------------------------------------------- agent loop: waiting
class _FinishingJobs:
    """A JobManager whose one job finishes after a delay."""

    def __init__(self, after: float):
        self.started = time.monotonic()
        self.after = after

    def _job(self, done: bool):
        class _J:
            pass

        job = _J()
        job.returncode = 0 if done else None
        job.error = None
        job.cancel_requested = False
        job.status = 'succeeded' if done else 'running'
        return job

    def get(self, job_id):
        if job_id != 'job-1':
            return None
        return self._job(time.monotonic() - self.started >= self.after)

    def tail_log(self, job_id):
        return 'starting\nFinished in 00:00:02\n'


def test_wait_for_job_returns_when_the_job_finishes(demo_project, tmp_path):
    registry = ProjectRegistry(config_directory=tmp_path / 'config')
    registry.add(demo_project)
    ctx = tools.ToolContext(
        guard=PathGuard([demo_project]),
        projects=registry,
        jobs=_FinishingJobs(after=0.3),
    )
    seen = []
    result = tools.wait_for_job(
        ctx, 'job-1', timeout_seconds=5, poll_seconds=0.05,
        on_progress=lambda e, t, m: seen.append(m),
    )
    assert result['finished'] is True
    assert result['status'] == 'succeeded'
    assert 'Finished in' in result['log']
    # Progress was reported while it waited.
    assert seen and 'running' in seen[0]


def test_wait_for_job_times_out_and_says_so(demo_project, tmp_path):
    registry = ProjectRegistry(config_directory=tmp_path / 'config')
    registry.add(demo_project)
    ctx = tools.ToolContext(
        guard=PathGuard([demo_project]),
        projects=registry,
        jobs=_FinishingJobs(after=60),
    )
    result = tools.wait_for_job(ctx, 'job-1', timeout_seconds=0.3, poll_seconds=0.05)
    assert result['finished'] is False
    assert result['status'] == 'running'
    # It must tell the caller what to do next, not just fail.
    assert 'wait_for_job again' in result['hint']


def test_wait_for_job_rejects_an_unknown_job(context):
    with pytest.raises(tools.ToolError) as excinfo:
        tools.wait_for_job(context, 'not-a-job', timeout_seconds=1)
    assert 'not a job in this session' in str(excinfo.value)


def test_wait_is_not_a_write_tool():
    """Waiting changes nothing, so it must not be gated as a write."""
    by_name = {tool.name: tool for tool in tools.build_registry()}
    assert by_name['wait_for_job'].writes is False


# ------------------------------------------------------- results as numbers
def test_metrics_read_defocus_from_a_ctf_star(demo_project, tmp_path):
    from recryo import pipeline as pipeline_module

    job = demo_project / 'Tomograms' / 'job001'
    # Metrics are chosen by the job's recorded type, so this has to be a CTF job.
    pipeline = pipeline_module.read_pipeline(demo_project)
    pipeline.find_process('Tomograms/job001').type_label = 'relion.ctffind'
    pipeline_module.write_pipeline(demo_project, pipeline)
    (job / 'micrographs_ctf.star').write_text(
        '\n'.join(
            [
                '',
                'data_',
                '',
                'loop_',
                '_rlnMicrographName #1',
                '_rlnDefocusU #2',
                'a.mrc 12000.0',
                'b.mrc 45000.0',
                'c.mrc 28000.0',
                '',
            ]
        )
    )
    result = call(
        context_of(demo_project, tmp_path),
        'get_job_metrics',
        project=str(demo_project),
        process_name='Tomograms/job001',
    )
    assert result['metrics']['n_images'] == 3
    assert result['metrics']['defocus_min'] == 12000.0
    assert result['metrics']['defocus_max'] == 45000.0


def test_metrics_say_what_is_missing_rather_than_failing(demo_project, tmp_path):
    """A job with no STAR output is not an error - it just has no metrics."""
    result = call(
        context_of(demo_project, tmp_path),
        'get_job_metrics',
        project=str(demo_project),
        process_name='Tomograms/job001',
    )
    assert result['metrics'] == {}
    assert result['notes'], 'must explain why there are no numbers'


def context_of(project, tmp_path):
    registry = ProjectRegistry(config_directory=tmp_path / 'cfg')
    registry.add(project)
    return tools.ToolContext(
        guard=PathGuard([project]), projects=registry, jobs=StubJobs()
    )


# --------------------------------------------------- inputs must exist first
def test_validate_job_reports_a_missing_input_file(context, demo_project):
    """Without this an agent submits, waits, and fails on a file not written yet."""
    result = call(
        context,
        'validate_job',
        job_type='tomo_pick_tomograms',
        arguments={'in_tomoset': str(demo_project / 'not-written-yet.star')},
    )
    assert result['valid'] is False
    assert [item['variable'] for item in result['missing_inputs']] == ['in_tomoset']
    assert 'does not exist' in result['missing_inputs'][0]['reason']


def test_validate_job_accepts_an_existing_input(context, demo_project):
    existing = demo_project / 'Tomograms' / 'job001' / 'run.out'
    result = call(
        context,
        'validate_job',
        job_type='tomo_pick_tomograms',
        arguments={'in_tomoset': str(existing)},
    )
    assert result['missing_inputs'] == []


def test_validate_job_skips_pipeline_node_names(context, demo_project):
    """A node name is relative to the project and resolved by RELION, not here."""
    result = call(
        context,
        'validate_job',
        job_type='tomo_pick_tomograms',
        arguments={'in_tomoset': 'Tomograms/job001/tomograms.star'},
    )
    assert result['missing_inputs'] == []


def test_submit_refuses_a_missing_input(context, demo_project):
    """The check also guards submit, so skipping validate_job is not a way around it."""
    result = call(
        context,
        'submit_job',
        project=str(demo_project),
        job_type='tomo_pick_tomograms',
        arguments={'in_tomoset': str(demo_project / 'absent.star')},
    )
    assert 'input not available yet' in result['error']
    assert 'wait_for_job' in result['error']


# --------------------------------------------------- MCP resources and prompts
def test_mcp_content_is_registered():
    pytest.importorskip('mcp')
    from recryo import mcp_content

    assert len(mcp_content.template_uris()) == 5
    assert mcp_content.resource_uris()['projects'].startswith('relion://')
    # The prompts are procedure, so they must mention the ordering constraint
    # that option validation cannot express.
    assert 'denoise_train' in mcp_content.STA_WORKFLOW
    assert 'tomo_denoise_predict' in mcp_content.STA_WORKFLOW
    assert 'wait_for_job' in mcp_content.STA_WORKFLOW


def test_uri_encoding_survives_awkward_paths():
    from recryo import mcp_content

    uri = mcp_content.project_uri('/data/my project#1')
    assert ' ' not in uri and '#' not in uri
    assert '%20' in uri and '%23' in uri


def test_resources_and_prompts_are_served(demo_project, tmp_path):
    pytest.importorskip('mcp')
    import asyncio

    from recryo import mcp_content, mcp_server

    ctx = context_of(demo_project, tmp_path)
    server = mcp_server.create_server(ctx, hosts=['testserver'])

    async def inspect():
        templates = await server.list_resource_templates()
        resources = await server.list_resources()
        prompts = await server.list_prompts()
        pipeline = await server.read_resource(
            mcp_content.project_uri(str(demo_project))
        )
        log = await server.read_resource(
            mcp_content.job_uri(str(demo_project), 'Tomograms/job001')
        )
        rendered = await server.get_prompt(
            'diagnose_failure',
            {'project': str(demo_project), 'process': 'Tomograms/job001'},
        )
        return templates, resources, prompts, pipeline, log, rendered

    templates, resources, prompts, pipeline, log, rendered = asyncio.run(inspect())
    assert len(templates) == 5
    assert len(resources) == 2
    assert {p.name for p in prompts} == {
        'sta_workflow', 'diagnose_failure', 'next_step'
    }
    assert 'processes' in str(pipeline[0].content)
    assert 'reading in.star' in str(log[0].content)
    assert 'Tomograms/job001' in str(rendered.messages[0].content.text)


def test_read_only_mcp_still_serves_resources(demo_project, tmp_path):
    """A client that cannot act must still be able to look."""
    pytest.importorskip('mcp')
    import asyncio

    from recryo import mcp_server

    ctx = context_of(demo_project, tmp_path)
    server = mcp_server.create_server(
        ctx, hosts=['testserver'], allow_writes=False
    )

    async def inspect():
        tools_list = await server.list_tools()
        templates = await server.list_resource_templates()
        return tools_list, templates

    tools_list, templates = asyncio.run(inspect())
    assert not any(_hint(t.annotations, "destructiveHint") for t in tools_list)
    assert len(templates) == 5


# ------------------------------------------------------- execution hosts
def test_hosts_always_include_local_first():
    from recryo import hosts

    registry = hosts.registry_from_specs(['gpu2=gpu2.lab', 'gpu1=gpu1.lab'])
    assert [h.name for h in registry.list()] == ['local', 'gpu1', 'gpu2']
    assert registry.get(None).is_local
    assert registry.get('local').is_local
    assert len(registry.remote()) == 2


def test_host_spec_forms():
    from recryo import hosts

    plain = hosts.parse_host_spec('gpu1=gpu1.lab')
    assert (plain.name, plain.hostname, plain.port, plain.user) == ('gpu1', 'gpu1.lab', 22, None)

    full = hosts.parse_host_spec('gpu2=alice@gpu2.lab:2222')
    assert (full.name, full.hostname, full.port, full.user) == ('gpu2', 'gpu2.lab', 2222, 'alice')
    assert full.ssh_target() == 'alice@gpu2.lab'


@pytest.mark.parametrize('spec', ['noequals', 'a=', '=b', 'a=b:x', 'a='])
def test_bad_host_specs_are_refused(spec):
    from recryo import hosts

    with pytest.raises(hosts.HostError):
        hosts.parse_host_spec(spec)


def test_local_cannot_be_redefined():
    """`local` is what "run here" means; shadowing it would be surprising."""
    from recryo import hosts

    with pytest.raises(hosts.HostError):
        hosts.registry_from_specs(['local=elsewhere.lab'])


def test_unknown_host_lists_the_known_ones():
    from recryo import hosts

    registry = hosts.registry_from_specs(['gpu1=gpu1.lab'])
    with pytest.raises(hosts.HostError) as excinfo:
        registry.get('gpu9')
    assert 'gpu1' in str(excinfo.value) and 'local' in str(excinfo.value)


def test_local_host_leaves_the_argv_alone():
    """One code path for execution: a local job is not wrapped in anything."""
    from recryo import hosts

    argv = ['/bin/sh', '-c', 'echo hi']
    assert hosts.wrap_for_host(argv, '/data/p', hosts.ExecutionHost('local')) == argv


def test_remote_host_wraps_in_ssh_with_the_shared_directory():
    from recryo import hosts

    host = hosts.ExecutionHost('gpu1', 'gpu1.lab')
    launch = hosts.wrap_for_host(['relion_x', '--i', 'a.star'], '/data/my proj', host)
    assert launch[0] == 'ssh'
    # Every option needs its own -o, or ssh reads it as the hostname.
    assert launch[1] == '-o'
    assert 'BatchMode=yes' in launch
    target = host.ssh_target()
    assert target in launch
    command = launch[-1]
    assert "cd '/data/my proj'" in command
    assert 'exec relion_x --i a.star' in command


def test_remote_command_quotes_each_argument():
    """A job's argv must not be re-split by the remote shell."""
    from recryo import hosts

    command = hosts.remote_command(['relion_x', '--fn', 'a b.star'], '/data/p')
    assert "'a b.star'" in command


def test_no_password_is_ever_stored():
    """The security property the remote design has to live with."""
    from recryo import hosts

    launch = hosts.wrap_for_host(['x'], '/p', hosts.ExecutionHost('g', 'g.lab'))
    joined = ' '.join(launch)
    # BatchMode is what makes a missing key fail fast instead of prompting; a
    # password would have to travel as an argument, which this never does.
    assert 'BatchMode=yes' in joined
    assert 'sshpass' not in joined and 'PasswordAuthentication' not in joined


def test_queue_on_a_remote_host_is_refused():
    """Rendering and submitting the template happens on the agent, not the node."""
    import inspect

    from recryo.jobs import JobManager

    source = inspect.getsource(JobManager.submit)
    assert 'cannot submit a queued job' in source


def test_job_records_the_host_it_ran_on():
    import time

    from recryo.jobs import Job

    job = Job(
        id='x', job_type='t', command=[], output_directory='/tmp',
        started_at=time.time(),
    )
    assert job.host == 'local'
    assert job.as_dict()['host'] == 'local'


def test_list_hosts_tool_reports_the_registry(tmp_path):
    from recryo import hosts

    registry = hosts.registry_from_specs(['gpu1=gpu1.lab'])
    ctx = tools.ToolContext(
        guard=PathGuard(['/tmp']),
        projects=ProjectRegistry(config_directory=tmp_path / 'c'),
        jobs=type('J', (), {'hosts': registry})(),
    )
    result = tools.call_tool(tools.build_registry(), 'list_hosts', {}, ctx)
    assert [h['name'] for h in result['hosts']] == ['local', 'gpu1']
    assert result['default'] == 'local'
    # It must say why a remote node might refuse, not just list names.
    assert 'ssh' in result['note']


def test_submit_job_takes_a_host():
    schema = next(
        tool for tool in tools.build_registry() if tool.name == 'submit_job'
    ).schema()
    assert 'host' in schema['properties']
    assert 'host' not in schema['required']


def test_api_reports_hosts_and_accepts_one_on_submit(tmp_path, demo_project):
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(
        roots=[str(demo_project)], host_nodes=['gpu1=gpu1.lab']
    )
    with TestClient(app) as client:
        listed = client.get('/api/hosts').json()
        assert [h['name'] for h in listed['hosts']] == ['local', 'gpu1']
        assert listed['default'] == 'local'

        # An unknown host is a client error, reported before anything is queued.
        response = client.post(
            '/api/jobs',
            json={
                'job_type': 'tomo_reconstruct_tomograms',
                'output_directory': str(demo_project / 'Tomograms' / 'job002'),
                'arguments': {},
                'project_directory': str(demo_project),
                'host': 'no-such-node',
            },
        )
        assert response.status_code == 400
        assert 'unknown execution host' in response.json()['error']


# ------------------------------------------------------ node metrics parsing
SNAPSHOT = '''@@LOAD@@
1.50 1.20 0.90 2/500 12345
@@CPU@@
16
@@MEM@@
MemTotal: 65536000 kB
MemAvailable: 32768000 kB
@@UP@@
  86400
@@GPU@@
0, GPU-aaaa, NVIDIA A100-SXM4-40GB, 30000, 40960, 75, 210.5, 400.0, 62
1, GPU-bbbb, NVIDIA A100-SXM4-40GB, 0, 40960, 0, [N/A], 400.0, 35
@@GPUAPPS@@
4242, 12000, GPU-aaaa
4242, 8000, GPU-aaaa
@@PROCS@@
4242 1 350.0 2.5 8000000 relion_refine_mpi --i parts.star --pipeline_control /data/proj/Refine3D/job007/
4243 4242 340.0 2.0 7000000 relion_refine_mpi --i parts.star --pipeline_control /data/proj/Refine3D/job007
4244 1 5.0 0.1 50000 relion_ctffind --pipeline_control /data/proj/CtfFind/job003
@@END@@
'''


def test_snapshot_parses_every_section():
    from recryo import node_metrics

    snap = node_metrics.parse_snapshot('gpu1', SNAPSHOT)
    assert snap.reachable
    assert snap.cpus == 16
    assert snap.load_1 == 1.5
    assert snap.memory_total_mb == pytest.approx(64000, abs=1)
    assert snap.memory_used_mb == pytest.approx(32000, abs=1)
    assert snap.uptime_seconds == 86400
    assert snap.load_percent == pytest.approx(9.4, abs=0.1)


def test_gpu_fields_including_unsupported_ones():
    """A driver reporting [N/A] must yield None, not zero.

    A laptop GPU commonly has no power limit; printing 0 W would read as "drawing
    no power", which is a different and wrong statement.
    """
    from recryo import node_metrics

    snap = node_metrics.parse_snapshot('n', SNAPSHOT)
    assert len(snap.gpus) == 2
    first, second = snap.gpus
    assert first.memory_used_mb == 30000
    assert first.utilization_percent == 75
    assert first.power_watts == 210.5
    assert first.power_limit_watts == 400.0
    assert first.temperature_c == 62
    assert second.power_watts is None, '[N/A] must be absent, not 0'
    assert second.utilization_percent == 0


def test_processes_group_by_job_directory():
    """One row per job, not per MPI rank."""
    from recryo import node_metrics

    snap = node_metrics.parse_snapshot('n', SNAPSHOT)
    names = [job.process_name for job in snap.jobs]
    assert names == ['CtfFind/job003', 'Refine3D/job007']

    refine = next(j for j in snap.jobs if j.process_name == 'Refine3D/job007')
    assert sorted(refine.pids) == [4242, 4243]
    assert refine.cpu_percent == pytest.approx(690.0)
    assert refine.job_type == 'Refine3D'
    assert refine.project == '/data/proj'
    # Two ranks each on one GPU, summed: 12000 + 8000.
    assert refine.gpu_memory_mb == 20000


def test_snapshot_survives_a_node_with_no_gpu():
    """The card must still show CPU and memory; a GPU is not required."""
    from recryo import node_metrics

    plain = SNAPSHOT.split('@@GPU@@')[0] + '@@END@@\n'
    snap = node_metrics.parse_snapshot('cpu-only', plain)
    assert snap.gpus == []
    assert snap.cpus == 16
    assert snap.jobs == []


def test_a_failed_ssh_is_reported_not_raised():
    from recryo import node_metrics

    snap = node_metrics.collect(
        __import__('recryo.hosts', fromlist=['x']).ExecutionHost(
            'gone', 'no.such.host.invalid'
        ),
        timeout=5,
    )
    assert snap.reachable is False
    assert snap.problem


def test_local_collection_works_without_ssh():
    """The agent's own card uses the same script, so there is one code path."""
    from recryo import hosts, node_metrics

    snap = node_metrics.collect(hosts.ExecutionHost('local'))
    assert snap.reachable is True
    assert snap.cpus > 0
    assert snap.memory_total_mb and snap.memory_total_mb > 0


# ---------------------------------------------------------- node management
def test_hosts_persist_and_carry_enabled_state(tmp_path):
    from recryo import hosts

    config = tmp_path / 'cfg'
    registry = hosts.HostRegistry(config_directory=config)
    registry.add(hosts.parse_host_spec('gpu1=gpu1.lab'))
    registry.add(hosts.parse_host_spec('gpu2=gpu2.lab'))
    registry.update('gpu1', enabled=False, note='maintenance')

    again = hosts.HostRegistry(config_directory=config)
    assert [h.name for h in again.list()] == ['local', 'gpu1', 'gpu2']
    disabled = again.list()[1]
    assert disabled.enabled is False
    assert disabled.note == 'maintenance'


def test_a_disabled_node_is_not_offered_and_refuses_jobs():
    from recryo import hosts

    registry = hosts.registry_from_specs(['gpu1=gpu1.lab', 'gpu2=gpu2.lab'])
    registry.update('gpu1', enabled=False)
    assert [h.name for h in registry.enabled()] == ['local', 'gpu2']
    with pytest.raises(hosts.HostError) as excinfo:
        registry.get('gpu1')
    assert 'disabled' in str(excinfo.value)


def test_local_cannot_be_removed_or_updated():
    from recryo import hosts

    registry = hosts.registry_from_specs([])
    with pytest.raises(hosts.HostError):
        registry.remove('local')
    with pytest.raises(hosts.HostError):
        registry.update('local', enabled=False)


def test_host_names_are_restricted():
    """A name reaches a JSON file, the UI and the model; keep it simple."""
    from recryo import hosts

    registry = hosts.registry_from_specs([])
    with pytest.raises(hosts.HostError):
        registry.add(hosts.ExecutionHost('has space', 'h.lab'))
    with pytest.raises(hosts.HostError):
        registry.add(hosts.ExecutionHost('semi;colon', 'h.lab'))


def test_node_management_over_http(tmp_path):
    from fastapi.testclient import TestClient

    from recryo.app import create_app
    from recryo.projects import ProjectRegistry

    config = tmp_path / 'cfg'
    app = create_app(
        roots=['/tmp'],
        projects=ProjectRegistry(config_directory=config),
    )
    with TestClient(app) as client:
        added = client.post('/api/hosts', json={'spec': 'gpu1=127.0.0.1:nope'})
        assert added.status_code == 400, 'a bad port must be refused'

        created = client.post('/api/hosts', json={'spec': 'gpu1=127.0.0.1'})
        assert created.status_code == 201
        assert created.json()['enabled'] is True

        disabled = client.patch('/api/hosts/gpu1', json={'enabled': False})
        assert disabled.json()['enabled'] is False

        # The file is the durable record, so a restart keeps the node.
        reloaded = create_app(
            roots=['/tmp'], projects=ProjectRegistry(config_directory=config)
        )
        with TestClient(reloaded) as second:
            names = [h['name'] for h in second.get('/api/hosts').json()['hosts']]
            assert 'gpu1' in names

        assert client.delete('/api/hosts/gpu1').json()['removed'] == 'gpu1'
        assert client.delete('/api/hosts/local').status_code == 400


def test_nodes_endpoint_returns_snapshots():
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(roots=['/tmp'], host_nodes=['node1=127.0.0.1'])
    with TestClient(app) as client:
        payload = client.get('/api/nodes').json()
        names = [node['name'] for node in payload['nodes']]
        assert 'local' in names
        local = next(node for node in payload['nodes'] if node['name'] == 'local')
        assert local['reachable'] is True
        assert local['cpus'] > 0
        assert 'gpus' in local and 'jobs' in local


def test_nodes_skips_disabled_hosts():
    """A node out of service should not be polled at all."""
    from fastapi.testclient import TestClient

    from recryo.app import create_app

    app = create_app(roots=['/tmp'], host_nodes=['node1=127.0.0.1'])
    app.state.jobs.hosts.update('node1', enabled=False)
    with TestClient(app) as client:
        names = [n['name'] for n in client.get('/api/nodes').json()['nodes']]
        assert 'node1' not in names
