"""MPI wrapping and queue submission, mirroring prepareFinalCommand.

RELION swaps in an ``_mpi`` binary when ``nr_mpi > 1`` and wraps it in
``mpirun``, or else writes a submission script from the user's template and
starts that. Getting this wrong is quiet and expensive: without the ``_mpi``
swap every MPI rank would run the serial program and overwrite the others'
output, and a queue job whose submit command returns 0 would be reported as
finished long before the scheduler ran it.
"""

import asyncio
from pathlib import Path

import pytest

from recryo import job_queue, pipeline as pipeline_module
from recryo.jobs import JobManager
from recryo.paths import PathGuard
from recryo import pipeline as pl


# ------------------------------------------------------------------ helpers
def _manager(root: Path, **overrides) -> JobManager:
    return JobManager(PathGuard([root]), executable_overrides=overrides or None)


RECON_ARGS = {
    'in_tiltseries': 'tilt_series.star',
    'xdim': 100,
    'ydim': 100,
    'zdim': 100,
}


# ------------------------------------------------------------ command shape
def test_local_job_runs_through_a_shell(tmp_path):
    """Output is captured by piping, so run.out/run.err are filled by the
    agent rather than by a shell redirect (which would starve the live log)."""
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'tomo_reconstruct_tomograms', RECON_ARGS, tmp_path / 'job'
    )
    assert plan.mode == 'local'
    assert plan.argv[:2] == ['/bin/sh', '-c']
    command = plan.argv[-1]
    assert '--pipeline_control' in command
    assert 'run.out' not in command
    assert 'mpirun' not in command


def test_mpi_count_swaps_in_the_mpi_program(tmp_path):
    """nr_mpi > 1 must use relion_*_mpi, not the serial binary."""
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'tomo_reconstruct_tomograms',
        {**RECON_ARGS, 'nr_mpi': 4},
        tmp_path / 'job',
    )
    assert plan.mode == 'mpi'
    assert plan.nr_mpi == 4
    command = plan.argv[-1]
    assert 'relion_tomo_reconstruct_tomogram_mpi' in command
    assert 'mpirun -n 4' in command


def test_mpi_count_one_keeps_the_serial_program(tmp_path):
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'tomo_reconstruct_tomograms',
        {**RECON_ARGS, 'nr_mpi': 1},
        tmp_path / 'job',
    )
    command = plan.argv[-1]
    assert 'relion_tomo_reconstruct_tomogram --t' in command
    assert 'reconstruct_tomogram_mpi' not in command
    assert 'mpirun' not in command


def test_threads_reach_the_program(tmp_path):
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'tomo_reconstruct_tomograms',
        {**RECON_ARGS, 'nr_threads': 8},
        tmp_path / 'job',
    )
    assert plan.nr_threads == 8
    assert '--j 8' in plan.argv[-1]


def test_local_mpi_above_the_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv('RELION_ERROR_LOCAL_MPI', '2')
    manager = _manager(tmp_path)
    with pytest.raises(job_queue.QueueError, match='RELION_ERROR_LOCAL_MPI'):
        manager.build_run_plan(
            'tomo_reconstruct_tomograms',
            {**RECON_ARGS, 'nr_mpi': 8},
            tmp_path / 'job',
        )


def test_jobs_without_an_mpi_variant_are_not_wrapped(tmp_path):
    """relion_star_handler has no _mpi form, so mpirun must not be added."""
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'joinstar',
        {'fn_part1': 'a.star', 'fn_part2': 'b.star', 'nr_mpi': 4},
        tmp_path / 'job',
    )
    assert 'mpirun' not in plan.argv[-1]


def test_multi_program_job_keeps_its_chain(tmp_path):
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'multibody',
        {'fn_in': 'in.star', 'fn_bodies': 'bodies.star'},
        tmp_path / 'job',
    )
    assert len(plan.commands) == 2
    assert all('--pipeline_control' in command for command in plan.commands)


# -------------------------------------------------------------------- queue
TEMPLATE = """#!/bin/bash
#SBATCH --ntasks=XXXmpinodesXXX
#SBATCH --cpus-per-task=XXXthreadsXXX
#SBATCH --nodes=XXXnodesXXX
#SBATCH --partition=XXXqueueXXX
#SBATCH --output=XXXoutfileXXX
#SBATCH --error=XXXerrfileXXX
XXXcommandXXX
"""


@pytest.fixture
def template(tmp_path):
    path = tmp_path / 'template.sh'
    path.write_text(TEMPLATE)
    path.chmod(0o755)
    return path


def test_queue_job_writes_a_submission_script(tmp_path, template):
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'tomo_reconstruct_tomograms',
        {
            **RECON_ARGS,
            'do_queue': True,
            'qsub': 'sbatch',
            'queuename': 'gpu',
            'qsubscript': str(template),
            'nr_mpi': 8,
            'nr_threads': 2,
            'min_dedicated': 4,
        },
        tmp_path / 'job',
    )
    assert plan.mode == 'queue'
    assert plan.argv[0] == 'sbatch'
    script = plan.submission_script
    assert script.is_file()
    text = script.read_text()
    # 8 processes x 2 threads = 16 cores, 4 per node -> 4 nodes.
    assert '#SBATCH --ntasks=8' in text
    assert '#SBATCH --cpus-per-task=2' in text
    assert '#SBATCH --nodes=4' in text
    assert '#SBATCH --partition=gpu' in text
    # The job line is the MPI variant, since nr_mpi is above one.
    assert 'relion_tomo_reconstruct_tomogram_mpi' in text
    assert 'mpirun' not in text  # the template asks for ranks instead


def test_queue_without_a_template_is_refused(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(job_queue.QueueError, match='submission script template'):
        manager.build_run_plan(
            'tomo_reconstruct_tomograms',
            {**RECON_ARGS, 'do_queue': True, 'qsub': 'sbatch'},
            tmp_path / 'job',
        )


def test_queue_with_a_missing_template_is_refused(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(job_queue.QueueError, match='does not exist'):
        manager.build_run_plan(
            'tomo_reconstruct_tomograms',
            {
                **RECON_ARGS,
                'do_queue': True,
                'qsubscript': str(tmp_path / 'nope.sh'),
            },
            tmp_path / 'job',
        )


def test_submitted_job_is_queued_not_finished(tmp_path, template):
    """sbatch returning 0 means accepted, not done."""
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'tomo_reconstruct_tomograms',
        {**RECON_ARGS, 'do_queue': True, 'qsubscript': str(template)},
        tmp_path / 'job',
    )
    from recryo.jobs import Job

    job = Job(
        id='x',
        job_type='tomo_reconstruct_tomograms',
        command=plan.argv,
        output_directory=tmp_path / 'job',
        started_at=0,
        run_mode=plan.mode,
        returncode=0,
    )
    assert job.status == 'queued'
    assert job.pipeline_status == pipeline_module.STATUS_SCHEDULED

    (tmp_path / 'job' / 'RELION_JOB_EXIT_SUCCESS').touch()
    assert job.status == 'succeeded'
    assert job.pipeline_status == pipeline_module.STATUS_SUCCEEDED


def test_failed_submission_is_reported(tmp_path, template):
    manager = _manager(tmp_path)
    plan = manager.build_run_plan(
        'tomo_reconstruct_tomograms',
        {**RECON_ARGS, 'do_queue': True, 'qsubscript': str(template)},
        tmp_path / 'job',
    )
    from recryo.jobs import Job

    job = Job(
        id='x',
        job_type='tomo_reconstruct_tomograms',
        command=plan.argv,
        output_directory=tmp_path / 'job',
        started_at=0,
        run_mode='queue',
        returncode=1,
    )
    assert job.status == 'failed'


# ------------------------------------------------------- end-to-end (fake qsub)
def test_queued_job_reaches_the_pipeline_as_succeeded(tmp_path):
    """A queued job's sentinel must move the pipeline off Scheduled."""
    project = tmp_path / 'proj'
    project.mkdir()
    pl.write_pipeline(project, pl.Pipeline())

    executable = tmp_path / 'recon.sh'
    executable.write_text(
        '#!/bin/sh\n'
        'out=""; while [ $# -gt 0 ]; do case "$1" in --o) out="$2"; shift 2;; '
        '*) shift;; esac; done\n'
        'echo "# tomograms" > "${out}tomograms.star"\n'
        'touch "${out}RELION_JOB_EXIT_SUCCESS"\n'
    )
    executable.chmod(0o755)
    fake_qsub = tmp_path / 'fake_qsub.sh'
    fake_qsub.write_text(
        '#!/bin/sh\n'
        'cd "$(dirname "$1")"\n'
        'sh "./$(basename "$1")" >/dev/null 2>&1 &\n'
    )
    fake_qsub.chmod(0o755)
    template = tmp_path / 't.sh'
    template.write_text('#!/bin/sh\nXXXcommandXXX\n')
    template.chmod(0o755)

    manager = JobManager(
        PathGuard([tmp_path]),
        executable_overrides={'tomo_reconstruct_tomograms': str(executable)},
    )

    async def run():
        job = await manager.submit(
            'tomo_reconstruct_tomograms',
            {
                **RECON_ARGS,
                'do_queue': True,
                'qsub': str(fake_qsub),
                'qsubscript': str(template),
            },
            tmp_path / 'tmp',
            project_directory=project,
        )
        status = None
        for _ in range(100):
            await asyncio.sleep(0.1)
            process = pl.read_pipeline(project).find_process(job.process_name)
            status = process.status if process else None
            if job.status == 'succeeded' and status == 'Succeeded':
                break
        return job, status

    try:
        job, status = asyncio.run(run())
    finally:
        asyncio.run(manager.shutdown())

    assert job.status == 'succeeded', job.as_dict()
    assert status == 'Succeeded'
