"""Scheduler, export/import and point saving.

These three all reuse RELION's conventions rather than inventing new ones:
the scheduler is ``relion_pipeliner --RunJobs --schedule`` started detached, the
export copies the same metadata files RELION does with the same ``expNNN``
renaming, and picks are written as the ``rlnCoordinateX/Y/Z`` table the import
and pick jobs already understand.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

from relion_webui import job_actions, job_artifacts
from relion_webui import pipeline as pipeline_module


@pytest.fixture
def project(tmp_path):
    project = tmp_path / 'proj'
    project.mkdir()
    p = pipeline_module.Pipeline()
    p.job_counter = 3
    # Two scheduled jobs, as the GUI leaves them before a scheduler run.
    for number, type_dir, label in (
        (1, 'Tomograms', 'relion.reconstructtomograms'),
        (2, 'Denoise', 'relion.denoisetomo'),
    ):
        name = f'{type_dir}/job{number:03d}'
        pipeline_module.register_process(
            p,
            pipeline_module.Process(
                name=name,
                type_label=label,
                status=pipeline_module.STATUS_SCHEDULED,
            ),
            output_nodes=[
                pipeline_module.Node(
                    f'{name}/tomograms.star',
                    'TomogramGroupMetadata.star.relion.tomo.reconstruct',
                )
            ],
        )
        directory = project / name
        directory.mkdir(parents=True)
        job_artifacts.write_job_star(directory, label, {'nr_mpi': 1})
        job_artifacts.write_output_nodes(
            directory,
            [
                (
                    f'{name}/tomograms.star',
                    'TomogramGroupMetadata.star.relion.tomo.reconstruct',
                )
            ],
        )
    pipeline_module.write_pipeline(project, p)
    return project


# ---------------------------------------------------------------- scheduler
def test_scheduler_requires_relion_pipeliner(project, monkeypatch):
    monkeypatch.setattr(job_actions.shutil, 'which', lambda name: None)
    with pytest.raises(job_actions.ActionError, match='relion_pipeliner'):
        job_actions.run_schedule(project, ['Tomograms/job001'])


def test_scheduler_refuses_an_unknown_job(project):
    with pytest.raises(job_actions.ActionError, match='not in the pipeline'):
        job_actions.run_schedule(project, ['Tomograms/job999'])


def test_scheduler_only_runs_scheduled_jobs(project):
    p = pipeline_module.read_pipeline(project)
    pipeline_module.update_process_status(p, 'Tomograms/job001', 'Succeeded')
    pipeline_module.write_pipeline(project, p)
    with pytest.raises(job_actions.ActionError, match='only Scheduled jobs'):
        job_actions.run_schedule(project, ['Tomograms/job001'])


def test_scheduler_needs_a_job(project):
    with pytest.raises(job_actions.ActionError, match='at least one'):
        job_actions.run_schedule(project, [])


def test_scheduler_launches_relion_pipeliner(project, monkeypatch):
    """It must call RELION's own scheduler, with the flags that select jobs."""
    calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(args, cwd=None, stdout=None, stderr=None, start_new_session=None):
        calls.append((args, cwd))
        return FakeProcess()

    monkeypatch.setattr(job_actions.shutil, 'which', lambda name: '/usr/bin/relion_pipeliner')
    monkeypatch.setattr(job_actions.subprocess, 'Popen', fake_popen)

    result = job_actions.run_schedule(
        project,
        ['Tomograms/job001', 'Denoise/job002'],
        name='nightly',
        repeat=3,
        wait_minutes=5,
    )
    args, cwd = calls[0]
    assert '--RunJobs' in args
    assert '--schedule' in args
    assert 'nightly' in args
    assert '--repeat' in args and '3' in args
    assert 'Tomograms/job001 Denoise/job002' in args
    assert Path(cwd) == project
    assert result['pid'] == 4321
    assert result['stop_file'].endswith('RUNNING_PIPELINER_nightly')


def test_stopping_removes_the_check_file(project):
    """The scheduler polls RUNNING_PIPELINER_<name> and stops when it is gone."""
    marker = project / 'RUNNING_PIPELINER_web'
    marker.write_text('')
    job_actions.stop_schedule(project, 'web')
    assert not marker.exists()


def test_stopping_a_stopped_scheduler_is_reported(project):
    with pytest.raises(job_actions.ActionError, match='no scheduler named'):
        job_actions.stop_schedule(project, 'nothing')


# ------------------------------------------------------------ export/import
def test_export_copies_scheduled_jobs(project):
    result = job_actions.export_jobs(
        project, ['Tomograms/job001', 'Denoise/job002'], 'share'
    )
    archive = Path(result['archive'])
    assert archive.is_dir()
    # RELION renames the jobs to <Type>/expNNN in the archive.
    assert (archive / 'Tomograms' / 'exp001' / 'job.star').is_file()
    assert (archive / 'Denoise' / 'exp002' / 'job.star').is_file()
    assert (archive / 'exported.star').is_file()


def test_export_rewrites_the_job_name_inside_the_files(project):
    """The node paths in RELION_OUTPUT_NODES.star carry the job name."""
    result = job_actions.export_jobs(project, ['Tomograms/job001'], 'share')
    nodes = (
        Path(result['archive']) / 'Tomograms' / 'exp001' / 'RELION_OUTPUT_NODES.star'
    ).read_text()
    assert 'Tomograms/exp001' in nodes
    assert 'Tomograms/job001' not in nodes


def test_export_refuses_unfinished_jobs(project):
    p = pipeline_module.read_pipeline(project)
    pipeline_module.update_process_status(p, 'Tomograms/job001', 'Succeeded')
    pipeline_module.write_pipeline(project, p)
    with pytest.raises(job_actions.ActionError, match='only scheduled jobs'):
        job_actions.export_jobs(project, ['Tomograms/job001'], 'share')


def test_export_refuses_an_empty_name(project):
    with pytest.raises(job_actions.ActionError, match='give the export a name'):
        job_actions.export_jobs(project, ['Tomograms/job001'], '   ')


def test_import_renumbers_the_jobs(project):
    result = job_actions.export_jobs(project, ['Tomograms/job001'], 'share')
    listing = Path(result['listing'])

    other = project.parent / 'other'
    other.mkdir()
    pipeline_module.write_pipeline(other, pipeline_module.Pipeline())

    imported = job_actions.import_jobs(other, listing)
    assert imported['imported'] == ['Tomograms/job001']
    restored = pipeline_module.read_pipeline(other)
    process = restored.find_process('Tomograms/job001')
    assert process is not None
    assert process.status == pipeline_module.STATUS_SCHEDULED
    assert process.type_label == 'relion.reconstructtomograms'


def test_import_refuses_a_missing_listing(project):
    with pytest.raises(job_actions.ActionError, match='no export listing'):
        job_actions.import_jobs(project, project / 'nope' / 'exported.star')


# ------------------------------------------------------------------- picks
def test_pick_star_is_written_in_tomogram_pixels(client, tmp_path):
    import numpy as np
    import struct
    import warnings

    header = np.zeros(1024, dtype=np.uint8)
    struct.pack_into('<3i', header, 0, 100, 80, 40)
    struct.pack_into('<i', header, 12, 2)
    struct.pack_into('<3f', header, 40, 1.0, 1.0, 1.0)
    tomogram = tmp_path / 'tomo.mrc'
    tomogram.write_bytes(header.tobytes() + np.zeros((40, 80, 100), np.float32).tobytes())

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        response = client.post(
            '/api/project/pick-points',
            json={
                'tomogram_path': str(tomogram),
                'output_directory': str(tmp_path / 'picks'),
                'points': [
                    {'x': 0.5, 'y': 0.5, 'z': 0.5},
                    {'x': 0.0, 'y': 1.0, 'z': 0.0},
                ],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload['n_points'] == 2
    text = Path(payload['star_file']).read_text()
    assert '_rlnCoordinateX' in text
    # Fractions map to pixel coordinates: 0.5 of 100 px is 49.5, 1.0 of 80 is 79.
    assert '49.500 39.500 19.500' in text
    assert '0.000 79.000 0.000' in text


def test_pick_points_rejects_an_empty_selection(client, tmp_path):
    import numpy as np
    import struct

    header = np.zeros(1024, dtype=np.uint8)
    struct.pack_into('<3i', header, 0, 8, 8, 8)
    struct.pack_into('<i', header, 12, 2)
    tomogram = tmp_path / 't.mrc'
    tomogram.write_bytes(header.tobytes() + np.zeros((8, 8, 8), np.float32).tobytes())
    response = client.post(
        '/api/project/pick-points',
        json={
            'tomogram_path': str(tomogram),
            'output_directory': str(tmp_path),
            'points': [],
        },
    )
    assert response.status_code == 400


@pytest.fixture
def client(tmp_path):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient

    from relion_webui.app import create_app

    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        yield test_client
