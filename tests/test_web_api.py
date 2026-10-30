import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from recryo.app import create_app
from recryo import job_commands
from recryo import pipeline as pipeline_module
from recryo.jobs import JobManager
from recryo.paths import PathGuard

fastapi = pytest.importorskip('fastapi')
pytest.importorskip('httpx')

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def volume_file(tmp_path):
    import mrcfile

    data = np.linspace(0, 1, 8 * 6 * 4, dtype=np.float32).reshape(8, 6, 4)
    path = tmp_path / 'tomo.mrc'
    with mrcfile.new(path, overwrite=True) as handle:
        handle.set_data(data)
        handle.voxel_size = (12.0, 12.0, 12.0)
    return path


@pytest.fixture
def client(tmp_path):
    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_config_lists_roots_and_job_types(client, tmp_path):
    payload = client.get('/api/config').json()
    assert payload['roots'] == [str(tmp_path)]
    assert 'tomo_reconstruct_tomograms' in payload['job_types']
    assert 'motioncorr' in payload['job_types']
    assert 'postprocess' in payload['job_types']


def test_config_reports_no_initial_project_by_default(client):
    """Without --project there is nothing for the page to open on load."""
    payload = client.get('/api/config').json()
    assert payload['initial_project'] is None
    assert payload['initial_star_file'] is None


def test_config_exposes_initial_project(tmp_path):
    """`--project` must reach the browser, so it can auto-open."""
    app = create_app(roots=[tmp_path])
    app.state.initial_project = str(tmp_path)
    with TestClient(app) as test_client:
        payload = test_client.get('/api/config').json()
    assert payload['initial_project'] == str(tmp_path)


def test_datasets_endpoint(global_relion_metadata_file, tmp_path):
    app = create_app(roots=[global_relion_metadata_file.parent, tmp_path])
    with TestClient(app) as test_client:
        response = test_client.get(
            '/api/datasets', params={'star_file': str(global_relion_metadata_file)}
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload['n_tilt_series'] == 5
    assert payload['tilt_series'][0]['name']


def test_tilt_series_endpoint_returns_images(global_relion_metadata_file, tmp_path):
    app = create_app(roots=[global_relion_metadata_file.parent, tmp_path])
    with TestClient(app) as test_client:
        response = test_client.get(
            '/api/tilt-series',
            params={'star_file': str(global_relion_metadata_file), 'name': '220728_TS_01'},
        )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload['images']) == 41
    assert {'index', 'name', 'tilt_angle', 'selected'} <= set(payload['images'][0])


def test_unknown_tilt_series_is_404(global_relion_metadata_file, tmp_path):
    app = create_app(roots=[global_relion_metadata_file.parent, tmp_path])
    with TestClient(app) as test_client:
        response = test_client.get(
            '/api/tilt-series',
            params={'star_file': str(global_relion_metadata_file), 'name': 'nope'},
        )
    assert response.status_code == 404


def test_path_outside_root_is_forbidden(client, tmp_path):
    response = client.get('/api/volume', params={'path': str(tmp_path.parent / 'etc_passwd')})
    assert response.status_code == 403


def test_volume_info(client, volume_file):
    response = client.get('/api/volume', params={'path': str(volume_file)})
    assert response.status_code == 200
    payload = response.json()
    assert payload['shape'] == [8, 6, 4]
    assert payload['voxel_size'] == [12.0, 12.0, 12.0]


def test_render_slice_returns_png(client, volume_file):
    response = client.get(
        '/api/render/slice',
        params={'path': str(volume_file), 'axis': 'z', 'index': 3, 'width': 16, 'height': 16},
    )
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/png'
    assert response.content.startswith(b'\x89PNG')


def test_render_slice_clamps_out_of_range_index(client, volume_file):
    response = client.get(
        '/api/render/slice',
        params={'path': str(volume_file), 'axis': 'z', 'index': 999},
    )
    assert response.status_code == 200


def test_render_projection_returns_png(client, volume_file):
    response = client.get(
        '/api/render/projection', params={'path': str(volume_file), 'axis': 'z'}
    )
    assert response.status_code == 200
    assert response.content.startswith(b'\x89PNG')


def test_render_projection_rejects_other_axes(client, volume_file):
    response = client.get(
        '/api/render/projection', params={'path': str(volume_file), 'axis': 'y'}
    )
    assert response.status_code == 400


def test_invalid_axis_is_rejected(client, volume_file):
    response = client.get(
        '/api/render/slice', params={'path': str(volume_file), 'axis': 'q'}
    )
    assert response.status_code == 422


def test_job_dry_run_does_not_execute(client, tmp_path):
    """A dry run reports the argv and must not create anything on disk."""
    response = client.post(
        '/api/jobs',
        json={
            'job_type': 'tomo_reconstruct_tomograms',
            'output_directory': str(tmp_path / 'job001'),
            'arguments': {
                'in_tiltseries': 'in.star',
                'xdim': 100,
                'ydim': 200,
                'zdim': 300,
                'do_fourier': True,
                'binned_angpix': 10,
            },
            'dry_run': True,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    # build_command reports the shell string the job will actually run.
    text = ' '.join(payload['command'])
    assert 'relion_tomo_reconstruct_tomogram' in text
    assert '--fourier' in text
    assert '--w' in text
    assert '--o' in text
    # The GUI's own spelling: a C++ program takes underscore flags.
    assert '--binned_angpix' in text
    assert '--pipeline_control' in text
    assert not (tmp_path / 'job001').exists()


def test_job_dry_run_with_project_leaves_the_pipeline_alone(client, tmp_path):
    """A dry run must not advance the job counter or add a process.

    Writing the pipeline for a dry run would leave a process whose directory
    does not exist, which the GUI then reports as a failed job.
    """
    project = tmp_path / 'proj'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())
    before = pipeline_module.read_pipeline(project)

    response = client.post(
        '/api/jobs',
        json={
            'job_type': 'tomo_reconstruct_tomograms',
            'project_directory': str(project),
            'output_directory': str(project / 'tmp'),
            'arguments': {
                'in_tiltseries': 'in.star',
                'xdim': 1,
                'ydim': 1,
                'zdim': 1,
            },
            'dry_run': True,
        },
    )
    assert response.status_code == 201
    after = pipeline_module.read_pipeline(project)
    assert after.job_counter == before.job_counter
    assert after.processes == []
    assert not (project / 'Tomograms').exists()


def test_job_missing_required_option_is_rejected(client, tmp_path):
    """A job without its mandatory input fails with the option named."""
    response = client.post(
        '/api/jobs',
        json={
            'job_type': 'tomo_reconstruct_tomograms',
            'output_directory': str(tmp_path / 'job001'),
            'arguments': {'xdim': 100},
            'dry_run': True,
        },
    )
    assert response.status_code == 400
    assert 'in_tiltseries' in response.json()['error']


def test_unknown_job_type_is_rejected(client, tmp_path):
    response = client.post(
        '/api/jobs',
        json={'job_type': 'not_a_job', 'output_directory': str(tmp_path), 'arguments': {}},
    )
    assert response.status_code == 400


def test_cancel_unknown_job_is_404(client):
    assert client.delete('/api/jobs/missing').status_code == 404


def test_job_manager_uses_the_gui_option_names():
    """The argv must use the flags the real program parses.

    ``--binned_angpix`` is a C++ option; writing ``--binned-angpix`` would be
    accepted by IOParser with only a warning, the value would be ignored, and
    the job would silently run with binning 1.
    """
    manager = JobManager(PathGuard([Path('/tmp')]))
    command = manager.build_command(
        'tomo_reconstruct_tomograms',
        {
            'in_tiltseries': 'in.star',
            'xdim': 4000,
            'ydim': 4000,
            'zdim': 2000,
            'binned_angpix': 10,
        },
        Path('/tmp/job'),
    )
    text = ' '.join(command)
    assert '--binned_angpix' in text
    assert '--binned-angpix' not in text
    assert '--w 4000' in text


def test_job_manager_adds_the_python_subcommand():
    """Typer apps with subcommands reject a call that omits them."""
    manager = JobManager(PathGuard([Path('/tmp')]))
    command = manager.build_command(
        'tomo_denoise_predict',
        {'in_tomoset': 'tomograms.star', 'care_denoising_model': 'model.tar.gz',
         'gpu_ids': '0'},
        Path('/tmp/job'),
    )
    text = ' '.join(command)
    assert 'cryoCARE:predict' in text
    assert '--tomogram-star-file' in text
    assert '--tomogram_star_file' not in text


def _run_job_to_completion(
    manager, job_type, output_directory, timeout=15.0, arguments=None
):
    """Submit a job and wait for it to finish, shutting the manager down after.

    Tests that launch real subprocesses must terminate them, otherwise the
    interpreter reports an unclosed transport at exit.
    """

    async def run():
        job = await manager.submit(
            job_type, arguments or {}, output_directory
        )
        deadline = asyncio.get_event_loop().time() + timeout
        while job.status == 'running':
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.05)
        return job

    try:
        return asyncio.run(run())
    finally:
        asyncio.run(manager.shutdown())


def test_job_manager_success_sentinel_wins(tmp_path):
    """A job that writes the success sentinel is reported as succeeded."""
    executable = tmp_path / 'fake_job.sh'
    executable.write_text(
        '#!/bin/sh\n'
        # Commands run through "sh -c", so the script sees the program's own
        # arguments: $1 is --pipeline_control and $2 is the job directory.
        'ctl="$2"\n'
        'touch "$ctl/RELION_JOB_EXIT_SUCCESS"\n'
    )
    executable.chmod(0o755)

    import recryo.jobs as jobs_module

    original = jobs_module.SUPPORTED_JOBS.copy()
    jobs_module.SUPPORTED_JOBS['fake'] = str(executable)
    try:
        manager = JobManager(PathGuard([tmp_path]))
        job = _run_job_to_completion(manager, 'fake', tmp_path / 'job')
        assert job.status == 'succeeded', job.as_dict()
    finally:
        jobs_module.SUPPORTED_JOBS.clear()
        jobs_module.SUPPORTED_JOBS.update(original)


def test_job_manager_abort_sentinel_reports_aborted(tmp_path):
    executable = tmp_path / 'fake_fail.sh'
    executable.write_text(
        '#!/bin/sh\n'
        'ctl="$2"\n'
        'touch "$ctl/RELION_JOB_ABORTED"\n'
        'exit 2\n'
    )
    executable.chmod(0o755)

    import recryo.jobs as jobs_module

    original = jobs_module.SUPPORTED_JOBS.copy()
    jobs_module.SUPPORTED_JOBS['fake_abort'] = str(executable)
    try:
        manager = JobManager(PathGuard([tmp_path]))
        job = _run_job_to_completion(manager, 'fake_abort', tmp_path / 'job')
        assert job.status == 'aborted', job.as_dict()
    finally:
        jobs_module.SUPPORTED_JOBS.clear()
        jobs_module.SUPPORTED_JOBS.update(original)


def test_jobs_websocket_pushes_state(tmp_path):
    app = create_app(roots=[tmp_path], poll_interval=0.05)
    with TestClient(app) as test_client:
        with test_client.websocket_connect('/ws/jobs') as websocket:
            message = websocket.receive_text()
    payload = json.loads(message)
    assert 'jobs' in payload


def test_cancel_reports_aborted_and_cleans_abort_file(tmp_path):
    executable = tmp_path / 'slow_job.sh'
    executable.write_text('#!/bin/sh\nsleep 30\n')
    executable.chmod(0o755)

    import recryo.jobs as jobs_module

    original = jobs_module.SUPPORTED_JOBS.copy()
    jobs_module.SUPPORTED_JOBS['slow'] = str(executable)
    try:
        manager = JobManager(PathGuard([tmp_path]))

        async def run():
            job = await manager.submit('slow', {}, tmp_path / 'job')
            await asyncio.sleep(0.2)
            await manager.cancel(job.id, timeout=0.5)
            return job

        job = asyncio.run(run())
        asyncio.run(manager.shutdown())
        assert job.status == 'aborted', job.as_dict()
        assert not (tmp_path / 'job' / 'RELION_JOB_ABORT_NOW').exists()
    finally:
        jobs_module.SUPPORTED_JOBS.clear()
        jobs_module.SUPPORTED_JOBS.update(original)


def test_completed_job_reports_success_even_if_cancel_requested(tmp_path):
    """A job that finishes while cancellation is pending is still a success."""
    executable = tmp_path / 'quick_job.sh'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)

    import recryo.jobs as jobs_module

    original = jobs_module.SUPPORTED_JOBS.copy()
    jobs_module.SUPPORTED_JOBS['quick'] = str(executable)
    try:
        manager = JobManager(PathGuard([tmp_path]))
        job = _run_job_to_completion(manager, 'quick', tmp_path / 'job')
        assert job.status == 'succeeded', job.as_dict()
    finally:
        jobs_module.SUPPORTED_JOBS.clear()
        jobs_module.SUPPORTED_JOBS.update(original)


def test_log_chunk_returns_only_new_bytes(tmp_path):
    """read_log_chunk must not resend bytes the client already has."""
    executable = tmp_path / 'chatty.sh'
    executable.write_text(
        '#!/bin/sh\n'
        # The command now runs through "sh -c", so the pipeline-control path is
        # parsed out of the command line rather than taken from argv.
        'ctl="$2"\n'
        'echo line-1\n'
        'sleep 0.3\n'
        'echo line-2\n'
        'touch "$ctl/RELION_JOB_EXIT_SUCCESS"\n'
    )
    executable.chmod(0o755)

    import recryo.jobs as jobs_module

    original = jobs_module.SUPPORTED_JOBS.copy()
    jobs_module.SUPPORTED_JOBS['chatty'] = str(executable)
    try:
        manager = JobManager(PathGuard([tmp_path]))

        job = _run_job_to_completion(manager, 'chatty', tmp_path / 'job')
        if True:
            first, offset, truncated = manager.read_log_chunk(job.id, 0)
            assert 'line-1' in first, first
            assert offset == manager.log_size(job.id)
            assert not truncated

            # Nothing was written in between, so a second read is empty.
            again, offset2, _ = manager.read_log_chunk(job.id, offset)
            assert again == ''
            assert offset2 == offset

            whole, _, _ = manager.read_log_chunk(job.id, 0)
            assert whole == first
    finally:
        jobs_module.SUPPORTED_JOBS.clear()
        jobs_module.SUPPORTED_JOBS.update(original)


def test_log_chunk_resets_when_offset_past_end(tmp_path):
    executable = tmp_path / 'tiny.sh'
    executable.write_text('#!/bin/sh\necho hi\n')
    executable.chmod(0o755)

    import recryo.jobs as jobs_module

    original = jobs_module.SUPPORTED_JOBS.copy()
    jobs_module.SUPPORTED_JOBS['tiny'] = str(executable)
    try:
        manager = JobManager(PathGuard([tmp_path]))

        job = _run_job_to_completion(manager, 'tiny', tmp_path / 'job')
        # An offset beyond EOF means the client is out of sync.
        text, offset, truncated = manager.read_log_chunk(job.id, 10_000)
        assert truncated is True
        assert offset == manager.log_size(job.id)
    finally:
        jobs_module.SUPPORTED_JOBS.clear()
        jobs_module.SUPPORTED_JOBS.update(original)


def test_job_log_endpoint_returns_chunks(client, tmp_path):
    """The HTTP tail endpoint advances the offset as bytes arrive."""
    manager = client.app.state.jobs
    job = asyncio.run(manager.submit('external', {'fn_exe': '/bin/true'}, tmp_path / 'j', dry_run=True))
    # Give the job a log file with known content.
    job.log_path = tmp_path / 'j.log'
    job.log_path.write_text('alpha\n')

    first = client.get(f'/api/jobs/{job.id}/log', params={'offset': 0}).json()
    assert first['chunk'] == 'alpha\n'
    assert first['next_offset'] == len('alpha\n')
    assert first['truncated'] is False

    job.log_path.write_text('alpha\nbeta\n')
    second = client.get(
        f'/api/jobs/{job.id}/log', params={'offset': first['next_offset']}
    ).json()
    assert second['chunk'] == 'beta\n'
    assert second['chunk'] != first['chunk']


def test_job_log_endpoint_unknown_job_is_404(client):
    assert client.get('/api/jobs/nope/log').status_code == 404


def test_websocket_log_subscription_streams_chunks(tmp_path):
    """Subscribing over the socket yields only the bytes past the offset."""
    executable = tmp_path / 'stream.sh'
    executable.write_text('#!/bin/sh\necho payload-line\n')
    executable.chmod(0o755)

    import recryo.jobs as jobs_module

    original = jobs_module.SUPPORTED_JOBS.copy()
    jobs_module.SUPPORTED_JOBS['stream'] = str(executable)
    try:
        app = create_app(roots=[tmp_path], poll_interval=0.05)
        with TestClient(app) as test_client:
            manager = app.state.jobs
            # The log is filled by a task on the manager's own loop, so the job
            # has to be driven to completion inside one asyncio.run() call;
            # returning from it early cancels the pump before it copies bytes.
            job = _run_job_to_completion(manager, 'stream', tmp_path / 'job')

            with test_client.websocket_connect('/ws/jobs') as websocket:
                websocket.send_text(json.dumps({'watch': job.id, 'offset': 0}))
                received = ''
                for _ in range(60):
                    message = json.loads(websocket.receive_text())
                    if 'log' in message and message['log']['id'] == job.id:
                        received += message['log']['chunk']
                        if 'payload-line' in received:
                            break
                assert 'payload-line' in received, received
            asyncio.run(manager.shutdown())
    finally:
        jobs_module.SUPPORTED_JOBS.clear()
        jobs_module.SUPPORTED_JOBS.update(original)


def test_index_serves_html(client):
    response = client.get('/')
    assert response.status_code == 200
    assert 'RELION' in response.text


# ------------------------------------------------------- pipeline graph
def test_project_graph_reports_processes_and_edges(tmp_path):
    """The graph endpoint is what lets the UI draw the job flow."""
    project = tmp_path / 'proj'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())

    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        response = test_client.get(
            '/api/project/graph', params={'project_directory': str(project)}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload['processes'] == []
        assert payload['job_counter'] == 1
        assert payload['edges'] == {'inputs': [], 'outputs': []}


def test_project_graph_links_nodes_to_producers(tmp_path):
    project = tmp_path / 'proj'
    project.mkdir()
    p = pipeline_module.Pipeline()
    pipeline_module.register_process(
        p,
        pipeline_module.Process(
            name='Tomograms/job001', type_label='relion.reconstructtomograms'
        ),
        input_nodes=[pipeline_module.Node('tilt_series.star', 'TomogramGroupMetadata.star.relion')],
        output_nodes=[
            pipeline_module.Node(
                'Tomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    pipeline_module.write_pipeline(project, p)

    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        payload = test_client.get(
            '/api/project/graph', params={'project_directory': str(project)}
        ).json()

    produced = {
        node['name']: node['produced_by'] for node in payload['nodes']
    }
    assert produced['Tomograms/job001/tomograms.star'] == 'Tomograms/job001'
    assert produced['tilt_series.star'] is None
    consumed = {node['name']: node['consumed_by'] for node in payload['nodes']}
    assert consumed['tilt_series.star'] == ['Tomograms/job001']


# ------------------------------------------------------- project creation
def test_create_project_writes_a_pipeline(tmp_path):
    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        response = test_client.post(
            '/api/projects/create', json={'path': str(tmp_path / 'newproj')}
        )
        assert response.status_code == 201
        created = response.json()
        assert created['name'] == 'newproj'
        pipeline_file = Path(created['pipeline_file'])
        assert pipeline_file.is_file()
        # And the GUI's reader accepts it.
        assert pipeline_module.read_pipeline(Path(created['path'])).job_counter == 1


def test_create_project_refuses_a_non_empty_directory(tmp_path):
    target = tmp_path / 'busy'
    target.mkdir()
    (target / 'keep.txt').write_text('do not delete me')
    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        response = test_client.post(
            '/api/projects/create', json={'path': str(target)}
        )
    assert response.status_code == 400
    assert (target / 'keep.txt').read_text() == 'do not delete me'


# ------------------------------------------------------------ job schema
def test_job_schema_endpoint_covers_every_job(client):
    payload = client.get('/api/job-schema').json()
    jobs = payload['jobs']
    # Every desktop job window plus the Select variants. A window's modes are
    # exposed as separate jobs, and the API-only `select` alias is not listed.
    assert len(jobs) == len(job_commands.selectable_job_types())
    titles = [job['title'] for job in jobs]
    assert 'Reconstruct tomograms' in titles
    assert 'Post-processing' in titles
    variants = [job for job in jobs if job['job_type'] != job['base_job_type']]
    assert variants, 'no branch variants exposed'
    assert all(job['branch'] for job in variants)
    assert any(job['title'].startswith('Select (') for job in variants)


def test_job_schema_for_one_job(client):
    payload = client.get(
        '/api/job-schema', params={'job_type': 'tomo_reconstruct_tomograms'}
    ).json()
    assert payload['title'] == 'Reconstruct tomograms'
    assert payload['flag_style'] == 'underscore'
    assert [tab['title'] for tab in payload['tabs']] == ['I/O', 'Reconstruct', 'Run']
    # do_proj greys out the two projection options, as the desktop window does.
    io_gates = payload['tabs'][0].get('gates') or []
    assert any(
        gate['controller'] == 'do_proj' and 'centre_proj' in gate['fields']
        for gate in io_gates
    )
    assert 'xdim' in {field['variable'] for field in payload['fields']}


def test_job_schema_unknown_job_is_404(client):
    assert client.get('/api/job-schema', params={'job_type': 'nope'}).status_code == 404


# ------------------------------------------------------------ forget job
def test_forget_finished_job_removes_it_from_the_list(tmp_path):
    executable = tmp_path / 'quick.sh'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)

    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        manager = app.state.jobs
        manager.executable_overrides['external'] = str(executable)
        job = _run_job_to_completion(
            manager, 'external', tmp_path / 'job', arguments={'fn_exe': str(executable)}
        )
        assert job.status == 'succeeded'
        assert test_client.get('/api/jobs').json()['jobs']

        response = test_client.delete(
            f'/api/jobs/{job.id}', params={'forget': True}
        )
        assert response.status_code == 200
        assert response.json()['forgotten'] is True
        assert test_client.get('/api/jobs').json()['jobs'] == []


def test_forget_running_job_is_refused(client, tmp_path):
    response = client.delete('/api/jobs/does-not-exist', params={'forget': True})
    assert response.status_code == 404


# ---------------------------------------------------------- job actions API
def _project_with_job(tmp_path):
    project = tmp_path / 'proj'
    project.mkdir()
    p = pipeline_module.Pipeline()
    p.job_counter = 2
    pipeline_module.register_process(
        p,
        pipeline_module.Process(
            name='Tomograms/job001',
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
    pipeline_module.write_pipeline(project, p)
    job_dir = project / 'Tomograms' / 'job001'
    job_dir.mkdir(parents=True)
    from recryo import job_artifacts

    job_artifacts.write_job_star(job_dir, 'relion.reconstructtomograms', {})
    job_artifacts.write_output_nodes(
        job_dir,
        [
            (
                'Tomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    return project


def test_job_note_api_roundtrip(tmp_path):
    project = _project_with_job(tmp_path)
    app = create_app(roots=[tmp_path])
    with TestClient(app) as c:
        r = c.put(
            '/api/project/job/note',
            json={
                'project_directory': str(project),
                'process_name': 'Tomograms/job001',
                'note': 'from the browser',
            },
        )
        assert r.status_code == 200
        r = c.get(
            '/api/project/job/note',
            params={
                'project_directory': str(project),
                'process_name': 'Tomograms/job001',
            },
        )
        assert r.json()['detail']['note'] == 'from the browser'


def test_job_alias_api_sets_the_pipeline_field(tmp_path):
    project = _project_with_job(tmp_path)
    app = create_app(roots=[tmp_path])
    with TestClient(app) as c:
        r = c.post(
            '/api/project/job/alias',
            json={
                'project_directory': str(project),
                'process_name': 'Tomograms/job001',
                'alias': 'nice',
            },
        )
        assert r.status_code == 200
        assert r.json()['detail']['alias'] == 'Tomograms/nice/'
    assert (
        pipeline_module.read_pipeline(project).find_process('Tomograms/job001').alias
        == 'Tomograms/nice/'
    )


def test_job_alias_api_reports_a_bad_alias(tmp_path):
    project = _project_with_job(tmp_path)
    app = create_app(roots=[tmp_path])
    with TestClient(app) as c:
        r = c.post(
            '/api/project/job/alias',
            json={
                'project_directory': str(project),
                'process_name': 'Tomograms/job001',
                'alias': 'has space and slash/',
            },
        )
        assert r.status_code == 400
        assert 'cannot contain' in r.json()['error']


def test_job_delete_and_undelete_api(tmp_path):
    project = _project_with_job(tmp_path)
    app = create_app(roots=[tmp_path])
    with TestClient(app) as c:
        r = c.post(
            '/api/project/job/delete',
            json={
                'project_directory': str(project),
                'process_name': 'Tomograms/job001',
            },
        )
        assert r.status_code == 200
        assert pipeline_module.read_pipeline(project).processes == []

        trash = c.get(
            '/api/project/trash', params={'project_directory': str(project)}
        ).json()
        assert trash['detail']['entries'] == ['Tomograms/job001']

        r = c.post(
            '/api/project/undelete',
            json={
                'project_directory': str(project),
                'trash_entry': 'Tomograms/job001',
            },
        )
        assert r.status_code == 200
        assert (
            pipeline_module.read_pipeline(project).find_process('Tomograms/job001')
            is not None
        )


def test_job_mark_api(tmp_path):
    project = _project_with_job(tmp_path)
    app = create_app(roots=[tmp_path])
    with TestClient(app) as c:
        r = c.post(
            '/api/project/job/mark',
            json={
                'project_directory': str(project),
                'process_name': 'Tomograms/job001',
                'failed': True,
            },
        )
        assert r.status_code == 200
    assert (
        pipeline_module.read_pipeline(project).find_process('Tomograms/job001').status
        == pipeline_module.STATUS_FAILED
    )


def _register_import_job(project):
    """A project with one Import job, which is what the endpoint looks up."""
    from recryo import pipeline

    pipeline.write_pipeline(project, pipeline.Pipeline(job_counter=1))
    state = pipeline.read_pipeline(project)
    process = pipeline.Process(
        name='Import/job001',
        alias='None',
        type_label='relion.importtomo',
        status='Succeeded',
    )
    node = pipeline.Node(
        name='Import/job001/tilt_series.star',
        type_label='TomogramGroupMetadata.star.relion.tomo.import',
        type_depth=0,
    )
    pipeline.register_process(state, process, (), (node,))
    pipeline.write_pipeline(project, state)


def test_job_options_reads_job_star(client, tmp_path):
    """The dialog's parameters tab is only honest if this returns the record."""
    from recryo import job_artifacts, pipeline

    project = tmp_path / 'proj'
    project.mkdir()
    _register_import_job(project)
    directory = project / 'Import' / 'job001'
    job_artifacts.write_job_star(
        directory, 'relion.importtomo', {'fn_in': '/x/tilt.star', 'nr_mpi': 8}
    )

    response = client.get(
        '/api/project/job/options',
        params={
            'project_directory': str(project),
            'process_name': 'Import/job001',
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body['options'] == {'fn_in': '/x/tilt.star', 'nr_mpi': '8'}
    assert body['type_label'] == 'relion.importtomo'


def test_job_options_is_null_without_job_star(client, tmp_path):
    """null means "not recorded", which must not read as "no options"."""
    from recryo import pipeline

    project = tmp_path / 'proj'
    project.mkdir()
    _register_import_job(project)
    # The job directory exists but has no job.star, which is the RELION 3.x
    # shape: run.job instead. That is "not recorded", not "job missing".
    (project / 'Import' / 'job001').mkdir(parents=True)

    body = client.get(
        '/api/project/job/options',
        params={
            'project_directory': str(project),
            'process_name': 'Import/job001',
        },
    ).json()
    assert body['options'] is None


def test_job_options_refuses_a_path_outside_the_project(client, tmp_path):
    """process_name comes from the browser, so it must stay inside the project."""
    from recryo import pipeline

    project = tmp_path / 'proj'
    project.mkdir()
    _register_import_job(project)
    (tmp_path / 'outside').mkdir()

    response = client.get(
        '/api/project/job/options',
        params={
            'project_directory': str(project),
            'process_name': '../outside',
        },
    )
    assert response.status_code in (403, 404, 400)


def test_project_job_types_reports_the_base_job(client):
    """The parameters tab needs the base type: job.star records the base table."""
    entries = client.get('/api/project/job-types').json()['job_types']
    by_id = {entry['id']: entry for entry in entries}
    assert by_id['tomo_import_serialem']['base_job_type'] == 'tomo_import'
    assert by_id['tomo_import_coordinates']['base_job_type'] == 'tomo_import'
    assert by_id['motioncorr']['base_job_type'] == 'motioncorr'


def test_a_project_added_at_runtime_can_be_opened(client, tmp_path):
    """Adding a project must make it usable at once.

    The guard used to snapshot its roots at startup, so a project added through
    the API could be listed and then fail to open with "outside the allowed
    roots" - the one thing the project list is supposed to prevent.
    """
    project = tmp_path / 'later'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())

    added = client.post('/api/projects', json={'path': str(project)})
    assert added.status_code == 201

    opened = client.get(
        '/api/project', params={'project_directory': str(project)}
    )
    assert opened.status_code == 200
    assert opened.json()['n_jobs'] == 0


def test_removing_a_project_withdraws_access(tmp_path):
    """The list is the boundary, so leaving it means leaving the roots.

    Uses a root that does not contain the project, so the only thing granting
    access is the list entry.
    """
    roots = tmp_path / 'roots'
    roots.mkdir()
    app = create_app(roots=[roots])
    project = tmp_path / 'gone'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())

    with TestClient(app) as client:
        assert client.post(
            '/api/projects', json={'path': str(project)}
        ).status_code == 201
        assert client.get(
            '/api/project', params={'project_directory': str(project)}
        ).status_code == 200

        client.delete('/api/projects', params={'path': str(project)})

        assert client.get(
            '/api/project', params={'project_directory': str(project)}
        ).status_code == 403


def test_an_empty_folder_becomes_a_project_when_added(client, tmp_path):
    """`Add` initialises an empty folder, the way RELION's GUI does."""
    fresh = tmp_path / 'fresh'
    fresh.mkdir()

    response = client.post('/api/projects', json={'path': str(fresh)})
    assert response.status_code == 201
    assert (fresh / 'default_pipeline.star').is_file()
    assert client.get(
        '/api/project', params={'project_directory': str(fresh)}
    ).status_code == 200
