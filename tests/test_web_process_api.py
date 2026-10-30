"""The REST routes for one process, and the deprecated aliases they replace.

The job routes used to be RPC-shaped (`POST /api/project/job/delete` for a
deletion), which made every new action a new path and disagreed with
`/api/jobs/{id}`, a proper resource. These tests pin the canonical shape, the
deprecated aliases that still answer, and the one tricky part: `process_name`
contains a slash.
"""

import pytest
from fastapi.testclient import TestClient

from recryo import pipeline as pipeline_module
from recryo.app import create_app


@pytest.fixture
def project(tmp_path):
    project = tmp_path / 'proj'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())
    graph = pipeline_module.read_pipeline(project)
    pipeline_module.register_process(
        graph,
        pipeline_module.Process(
            'Tomograms/job001',
            type_label='relion.reconstructtomograms',
            status=pipeline_module.STATUS_FAILED,
        ),
    )
    pipeline_module.write_pipeline(project, graph)
    (project / 'Tomograms' / 'job001').mkdir(parents=True)
    return project


@pytest.fixture
def client(tmp_path, project):
    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        yield test_client


def path(project, name='Tomograms/job001'):
    return f'/api/projects/processes/{name}?project_directory={project}'


def test_patch_sets_alias_and_note_in_one_call(client, project):
    response = client.patch(
        path(project), json={'alias': 'recon', 'note': 'checked'}
    )
    assert response.status_code == 200
    assert [item['action'] for item in response.json()['actions']] == [
        'alias',
        'note_write',
    ]
    graph = pipeline_module.read_pipeline(project)
    assert graph.find_process('Tomograms/job001').alias == 'Tomograms/recon/'


def test_patch_marks_a_job_finished(client, project):
    response = client.patch(path(project), json={'failed': False})
    assert response.status_code == 200
    graph = pipeline_module.read_pipeline(project)
    assert graph.find_process('Tomograms/job001').status == 'Succeeded'


def test_patch_with_nothing_to_change_is_a_400(client, project):
    response = client.patch(path(project), json={})
    assert response.status_code == 400
    assert 'nothing to change' in response.json()['error']


def test_delete_moves_the_job_to_trash_and_restore_brings_it_back(client, project):
    deleted = client.delete(path(project))
    assert deleted.status_code == 200
    assert 'Trash' in deleted.json()['message']
    assert not (project / 'Tomograms' / 'job001').exists()

    trash = client.get(
        '/api/projects/trash', params={'project_directory': str(project)}
    )
    assert trash.status_code == 200
    assert trash.json()['detail']['entries'] == ['Tomograms/job001']

    restored = client.post(
        f'/api/projects/trash/Tomograms/job001/restore',
        params={'project_directory': str(project)},
    )
    assert restored.status_code == 200
    assert pipeline_module.read_pipeline(project).find_process('Tomograms/job001')


def test_clean_is_its_own_endpoint(client, project):
    """Clean deletes files rather than editing the record, so it keeps a verb."""
    response = client.post(
        '/api/projects/processes/Tomograms/job001/clean',
        params={'project_directory': str(project), 'harsh': 'false'},
    )
    # A failed job cannot be cleaned; the point is that the route resolves and
    # reaches the action, which then refuses with a reason.
    assert response.status_code == 400
    assert 'clean' in response.json()['error'].lower()


def test_the_deprecated_alias_still_answers(client, project):
    """An open browser tab must keep working across the change."""
    response = client.post(
        '/api/project/job/alias',
        json={
            'project_directory': str(project),
            'process_name': 'Tomograms/job001',
            'alias': 'r2',
        },
    )
    assert response.status_code == 200
    graph = pipeline_module.read_pipeline(project)
    assert graph.find_process('Tomograms/job001').alias == 'Tomograms/r2/'


def test_the_graph_says_which_scheduled_jobs_are_queued_here(client, project, tmp_path):
    """The canvas must distinguish "queued here" from "scheduled in RELION".

    Only the first starts on its own, so showing them identically would be a
    lie about what happens next.
    """
    response = client.post(
        '/api/queue',
        json={
            'project_directory': str(project),
            'job_type': 'tomo_denoise_train',
            'arguments': {'tomograms_for_training': 't.star', 'in_tomoset': 'a.star'},
            'host': 'local',
        },
    )
    entry = response.json()

    graph = client.get(
        '/api/project/graph', params={'project_directory': str(project)}
    ).json()
    by_name = {process['name']: process for process in graph['processes']}

    assert by_name[entry['process_name']]['queue_entry'] == entry['id']
    # A job that is not queued here has no entry id.
    assert by_name['Tomograms/job001']['queue_entry'] is None
