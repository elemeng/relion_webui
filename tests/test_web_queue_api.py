"""The queue over HTTP.

The routes are thin, but two things are worth pinning down: a bad request must
be a 400 with a readable reason rather than a 500 (the panel shows that text
verbatim), and the queue must be usable in a project with no jobs at all, which
is exactly the state a fresh project is in.
"""

import pytest
from fastapi.testclient import TestClient

from recryo import job_artifacts, pipeline as pipeline_module
from recryo.app import create_app


@pytest.fixture
def project(tmp_path):
    project = tmp_path / 'proj'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())
    return project


@pytest.fixture
def client(tmp_path, project):
    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        yield test_client


RECON = {'in_tiltseries': 'tilt_series.star', 'xdim': 10, 'ydim': 10, 'zdim': 10}


def enqueue(client, project, **overrides):
    body = {
        'project_directory': str(project),
        'job_type': 'tomo_reconstruct_tomograms',
        'arguments': RECON,
        'host': 'local',
    }
    body.update(overrides)
    return client.post('/api/queue', json=body)


def test_the_queue_starts_empty(client):
    response = client.get('/api/queue')
    assert response.status_code == 200
    payload = response.json()
    assert payload['entries'] == []
    assert payload['counts']['pending'] == 0
    # `local` is always present, so the panel always has a row per machine.
    assert [host['host'] for host in payload['hosts']] == ['local']


def test_enqueue_returns_a_pending_entry(client, project):
    response = enqueue(client, project)
    assert response.status_code == 201
    entry = response.json()
    assert entry['state'] == 'pending'
    assert entry['job_type'] == 'tomo_reconstruct_tomograms'
    assert entry['host'] == 'local'
    assert entry['id'].startswith('q')


def test_entries_lists_the_new_entry(client, project):
    entry = enqueue(client, project).json()
    payload = client.get('/api/queue').json()
    assert [item['id'] for item in payload['entries']] == [entry['id']]


def test_cancel_a_pending_entry(client, project):
    entry = enqueue(client, project).json()
    response = client.delete(f"/api/queue/{entry['id']}")
    assert response.status_code == 200
    assert response.json()['state'] == 'cancelled'


def test_cancelling_a_unknown_entry_is_a_400(client):
    response = client.delete('/api/queue/q9999')
    assert response.status_code == 400
    assert 'no queue entry' in response.json()['error']


def test_retry_puts_an_entry_back(client, project):
    entry = enqueue(client, project).json()
    client.delete(f"/api/queue/{entry['id']}")
    response = client.post(f"/api/queue/{entry['id']}/retry")
    assert response.status_code == 200
    assert response.json()['state'] == 'pending'


def test_an_unknown_host_is_refused_with_the_reason(client, project):
    response = enqueue(client, project, host='gpu9')
    assert response.status_code == 400
    assert 'unknown execution host' in response.json()['error']


def test_the_qsub_template_is_refused(client, project):
    """A queued job handed to qsub would bypass this queue entirely."""
    response = enqueue(client, project, arguments={**RECON, 'do_queue': True})
    assert response.status_code == 400
    assert 'replaces the qsub template' in response.json()['error']


def test_missing_required_options_are_refused(client, project):
    response = enqueue(client, project, arguments={})
    assert response.status_code == 400
    assert 'missing required options' in response.json()['error']


def test_a_directory_that_is_not_a_project_is_refused(client, tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    response = enqueue(client, plain)
    assert response.status_code == 400
    assert 'not a RELION project' in response.json()['error']


def test_the_queue_filters_by_project(client, project, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    pipeline_module.write_pipeline(other, pipeline_module.Pipeline())
    enqueue(client, project)
    enqueue(client, other)

    assert len(client.get('/api/queue').json()['entries']) == 2
    filtered = client.get(
        '/api/queue', params={'project_directory': str(project)}
    ).json()
    assert len(filtered['entries']) == 1
    assert filtered['entries'][0]['project'] == str(project)


def test_an_entry_carries_its_arguments(client, project):
    entry = enqueue(client, project).json()
    assert entry['arguments']['xdim'] == 10
    assert entry['cores'] == 1


def test_the_queue_is_wired_into_the_job_manager(client, project):
    """The queue must submit through the manager, not around it."""
    assert client.app.state.queue.manager is client.app.state.jobs


def test_forget_drops_a_finished_entry(client, project):
    """Without this the queue file grows forever."""
    entry = enqueue(client, project).json()
    client.delete(f"/api/queue/{entry['id']}")  # cancel it first
    response = client.delete(f"/api/queue/{entry['id']}?forget=true")
    assert response.status_code == 200
    assert response.json()['removed'] is True
    assert client.get('/api/queue').json()['entries'] == []


def test_cancel_does_not_forget(client, project):
    """Cancelling and forgetting are different: retry needs the entry."""
    entry = enqueue(client, project).json()
    client.delete(f"/api/queue/{entry['id']}")
    payload = client.get('/api/queue').json()
    assert [item['state'] for item in payload['entries']] == ['cancelled']


# --------------------------------------------------------------- input slots
def test_an_entry_is_reserved_at_enqueue(client, project):
    """The number is fixed on enqueue, which is what makes paths predictable."""
    entry = enqueue(client, project).json()

    assert entry['process_name'] == 'Tomograms/job001'
    assert entry['predicted_outputs'] == {
        'tomograms.star': 'Tomograms/job001/tomograms.star'
    }
    graph = pipeline_module.read_pipeline(project)
    assert graph.find_process('Tomograms/job001').status == 'Scheduled'


def test_a_chain_can_be_queued_in_one_pass(client, project):
    """Stage two names stage one's output before anything has run."""
    upstream = enqueue(client, project).json()
    downstream = client.post(
        '/api/queue',
        json={
            'project_directory': str(project),
            'job_type': 'tomo_denoise_train',
            'arguments': {'tomograms_for_training': 'train.star'},
            'host': 'local',
            'inputs': {'in_tomoset': f"{upstream['id']}:tomograms.star"},
        },
    )
    assert downstream.status_code == 201
    payload = downstream.json()

    assert payload['depends_on'] == [upstream['id']]
    assert payload['arguments']['in_tomoset'] == str(
        project / 'Tomograms' / 'job001' / 'tomograms.star'
    )
    assert payload['process_name'] == 'Denoise/job002'


def test_a_bad_slot_reference_is_a_400(client, project):
    response = client.post(
        '/api/queue',
        json={
            'project_directory': str(project),
            'job_type': 'tomo_denoise_train',
            'arguments': {'tomograms_for_training': 'train.star'},
            'inputs': {'in_tomoset': 'q9999:tomograms.star'},
        },
    )
    assert response.status_code == 400
    assert 'not in the queue' in response.json()['error']


def test_cancelling_releases_the_reservation(client, project):
    entry = enqueue(client, project).json()
    assert pipeline_module.read_pipeline(project).find_process('Tomograms/job001')

    client.delete(f"/api/queue/{entry['id']}")

    assert pipeline_module.read_pipeline(project).find_process('Tomograms/job001') is None


def test_two_enqueues_get_different_numbers(client, project):
    """A duplicate number would make two jobs write to one directory."""
    first = enqueue(client, project).json()
    second = enqueue(client, project).json()

    assert first['process_name'] != second['process_name']
    assert second['process_name'] == 'Tomograms/job002'


# --------------------------------------------------------- input slot picker
def test_a_reserved_output_appears_as_a_slot_candidate(client, project):
    """The picker lists .Nodes/<coarse type>/, which is RELION's own rule.

    A queued job touches its .Nodes entry before it runs (as RELION does for a
    scheduled job), so a stage that has not run yet is offered too - marked
    pending, and carrying the slot spelling that makes the queue wait.
    """
    upstream = enqueue(client, project).json()

    response = client.get(
        '/api/project/input-candidates',
        params={
            'job_type': 'tomo_denoise_train',
            'variable': 'in_tomoset',
            'project_directory': str(project),
        },
    )
    assert response.status_code == 200
    candidates = response.json()['candidates']

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate['node'] == 'Tomograms/job001/tomograms.star'
    assert candidate['producer'] == 'Tomograms/job001'
    assert candidate['producer_status'] == 'Scheduled'
    assert candidate['exists'] is False
    assert candidate['pending'] is True
    # The slot is what the UI sends; the entry id is what makes it a dependency.
    assert candidate['slot'] == f"{upstream['id']}:tomograms.star"
    assert candidate['queue_entry'] == upstream['id']


def test_a_finished_output_is_offered_as_a_plain_path(client, project):
    """Something on disk has nothing to wait for, so it is not a slot."""
    graph = pipeline_module.read_pipeline(project)
    pipeline_module.register_process(
        graph,
        pipeline_module.Process(
            'Tomograms/job001',
            type_label='relion.reconstructtomograms',
            status=pipeline_module.STATUS_SUCCEEDED,
        ),
        output_nodes=[
            pipeline_module.Node(
                'Tomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    pipeline_module.write_pipeline(project, graph)
    (project / 'Tomograms' / 'job001').mkdir(parents=True)
    (project / 'Tomograms' / 'job001' / 'tomograms.star').write_text('# x\n')
    job_artifacts.create_node_entry(
        project,
        'Tomograms/job001/tomograms.star',
        'TomogramGroupMetadata.star.relion.tomo.reconstruct',
    )

    candidates = client.get(
        '/api/project/input-candidates',
        params={
            'job_type': 'tomo_denoise_train',
            'variable': 'in_tomoset',
            'project_directory': str(project),
        },
    ).json()['candidates']

    assert [c['exists'] for c in candidates] == [True]
    assert candidates[0]['pending'] is False
    # Nothing to wait for, so no slot.
    assert candidates[0]['slot'] is None


def test_a_path_option_is_not_a_slot(client, project):
    """Only node-typed options have candidates; a path is browsed, not listed."""
    response = client.get(
        '/api/project/input-candidates',
        params={
            'job_type': 'tomo_denoise_train',
            'variable': 'tomograms_for_training',
            'project_directory': str(project),
        },
    )
    assert response.status_code == 400
    assert 'not a pipeline node' in response.json()['error']


def test_an_unknown_variable_is_refused(client, project):
    response = client.get(
        '/api/project/input-candidates',
        params={
            'job_type': 'tomo_denoise_train',
            'variable': 'no_such_option',
            'project_directory': str(project),
        },
    )
    assert response.status_code == 400


def test_the_picked_slot_queues_as_a_dependency(client, project):
    """The whole gesture: pick a queued output, get a chained job."""
    upstream = enqueue(client, project).json()
    candidates = client.get(
        '/api/project/input-candidates',
        params={
            'job_type': 'tomo_denoise_train',
            'variable': 'in_tomoset',
            'project_directory': str(project),
        },
    ).json()['candidates']
    slot = candidates[0]['slot']

    downstream = client.post(
        '/api/queue',
        json={
            'project_directory': str(project),
            'job_type': 'tomo_denoise_train',
            'arguments': {'tomograms_for_training': 'train.star'},
            'host': 'local',
            'inputs': {'in_tomoset': slot},
        },
    )
    assert downstream.status_code == 201
    payload = downstream.json()
    assert payload['depends_on'] == [upstream['id']]
    assert payload['arguments']['in_tomoset'] == str(
        project / 'Tomograms' / 'job001' / 'tomograms.star'
    )


# ------------------------------------------------- the three ways to fill a field
def test_browsing_for_a_node_still_offers_files_and_directories(client, project, tmp_path):
    """The folder browser is the pre-existing way to fill a node field.

    Adding the candidate list must not replace it: typing a path, browsing the
    filesystem, and picking a produced file are three separate affordances on
    the same field.
    """
    (project / 'Tomograms').mkdir()
    (project / 'Tomograms' / 'tomograms.star').write_text('# x\n')

    listing = client.get(
        '/api/browse', params={'path': str(project), 'filter': 'star'}
    ).json()

    names = {entry['name'] for entry in listing['entries']}
    assert 'default_pipeline.star' in names
    # Directories are always listed, so a directory is still reachable.
    assert 'Tomograms' in names
    assert any(entry['is_dir'] for entry in listing['entries'])


def test_a_typed_path_validates_without_any_slot(client, project):
    """Manual entry keeps working and never needs a queue entry."""
    (project / 'Tomograms').mkdir()
    (project / 'Tomograms' / 'tomograms.star').write_text('# x\n')

    response = client.post(
        '/api/job-validate',
        json={
            'job_type': 'tomo_denoise_train',
            'arguments': {
                'in_tomoset': str(project / 'Tomograms' / 'tomograms.star'),
                'tomograms_for_training': 't.star',
            },
        },
    )
    assert response.status_code == 200
    assert response.json()['problems'] == []
