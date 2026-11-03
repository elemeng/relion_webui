"""The job queue: dependencies, GPU leases, core packing and restart behaviour.

The queue exists because there is no cluster scheduler on a lab network. That
makes three properties load-bearing, and each would fail silently:

* a downstream job must not start before the job producing its input succeeds -
  otherwise it reads a half-written STAR file and fails minutes later;
* a GPU must never be handed to two jobs - RELION's own documentation warns that
  shared cards produce broken output, not just slower output;
* the queue's own state must survive a restart, and must not resubmit a job that
  already ran, because a resubmitted refinement writes over its own results.

These tests drive the real ``JobManager`` where behaviour crosses into RELION's
directory protocol, and a stub where the point is purely the admission decision.
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from relion_webui import hosts as hosts_module
from relion_webui import job_artifacts
from relion_webui._vendor import JOB_SUCCESS_FILENAME
from relion_webui import node_metrics
from relion_webui import pipeline as pipeline_module
from relion_webui import job_artifacts, queue_system
from relion_webui.jobs import JobManager
from relion_webui.paths import PathGuard


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def project(tmp_path):
    project = tmp_path / 'proj'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())
    return project


@pytest.fixture
def host_registry():
    return hosts_module.HostRegistry([hosts_module.ExecutionHost('gpu1', 'gpu1.lab')])


class StubJob:
    def __init__(self, job_id: str, process_name: str):
        self.id = job_id
        self.process_name = process_name
        self.status = 'running'
        self.error = None


class StubManager:
    """A manager that records submissions instead of starting processes.

    Enough of ``JobManager`` for admission tests: the queue only calls
    ``submit``, ``get`` and ``cancel``.
    """

    def __init__(self, registry):
        self.hosts = registry
        self.jobs = []
        self.submitted = []
        self.n = 0

    def get(self, job_id):
        return next((job for job in self.jobs if job.id == job_id), None)

    async def cancel(self, job_id):
        job = self.get(job_id)
        if job is None:
            return False
        job.status = 'aborted'
        return True

    async def submit(self, job_type, arguments, output_directory, *, project_directory=None, host=None, process_name=None, **kwargs):
        self.n += 1
        job = StubJob(f'job-{self.n}', process_name or f'Tomograms/job{self.n:03d}')
        self.jobs.append(job)
        self.submitted.append(
            {
                'job_type': job_type,
                'arguments': dict(arguments),
                'host': host,
                'process_name': process_name,
                'job': job,
            }
        )
        return job


@pytest.fixture
def nodes(monkeypatch):
    """Two GPUs and eight cores per node, with a display server on card 0.

    Card 0 has memory in use but no compute tenant, which is exactly the case
    that makes a memory-based busyness check wrong.
    """

    def fake_collect(host, timeout=6.0):
        snapshot = node_metrics.NodeSnapshot(name=host.name, reachable=True)
        snapshot.cpus = 8
        snapshot.gpus = [
            node_metrics.Gpu(
                index=0, uuid='GPU-a', name='A100', memory_used_mb=300,
                memory_total_mb=40000,
            ),
            node_metrics.Gpu(
                index=1, uuid='GPU-b', name='A100', memory_used_mb=0,
                memory_total_mb=40000,
            ),
        ]
        return snapshot

    monkeypatch.setattr(node_metrics, 'collect', fake_collect)


def make_queue(manager, tmp_path, **kwargs):
    return queue_system.JobQueue(
        manager,
        PathGuard([tmp_path]),
        config_directory=tmp_path / 'config',
        **kwargs,
    )


RECON = {
    'in_tiltseries': 'tilt_series.star',
    'xdim': 100,
    'ydim': 100,
    'zdim': 100,
}
ARETOMO = {'in_tiltseries': 'tilt_series.star'}


# ------------------------------------------------------------- GPU detection
def test_motioncor_defaults_to_the_cpu_implementation(project):
    """`do_own_motioncor` defaults to yes, which is RELION's CPU program.

    Leasing a GPU here would strand a card for a job that cannot use it.
    """
    assert queue_system.JobQueue._needs_gpu('motioncorr', {}) is False
    assert (
        queue_system.JobQueue._needs_gpu('motioncorr', {'do_own_motioncor': False})
        is True
    )


def test_gpu_only_jobs_always_need_a_card():
    for job_type in ('tomo_align_aretomo', 'tomo_denoise_train', 'modelangelo_build'):
        assert queue_system.JobQueue._needs_gpu(job_type, {}) is True, job_type


def test_refine_needs_a_card_only_when_use_gpu_is_set():
    assert queue_system.JobQueue._needs_gpu('autorefine', {}) is False
    assert queue_system.JobQueue._needs_gpu('autorefine', {'use_gpu': True}) is True


def test_requested_devices_parses_relion_gpu_syntax():
    """`gpu_ids` uses ':' between MPI ranks and ',' between threads."""
    requested = queue_system.JobQueue._requested_devices(
        'autorefine', {'use_gpu': True, 'gpu_ids': '0,0:1,1:0,0'}
    )
    assert requested == [0, 1]


def test_a_cpu_job_reports_no_devices_even_with_the_field_present():
    """A form field is not a request: the CPU MotionCor path names card 0."""
    assert queue_system.JobQueue._requested_devices('motioncorr', {'gpu_ids': '0'}) is None


# ------------------------------------------------------------ admission: GPU
def test_two_gpu_jobs_take_one_card_each_and_the_third_waits(
    project, host_registry, nodes, tmp_path
):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)

    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    second = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    third = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert first.state == 'running' and first.leased_gpus == [0]
    assert second.state == 'running' and second.leased_gpus == [1]
    assert third.state == 'pending'
    assert any('GPU' in reason for reason in third.blocked_by)


def test_a_pinned_gpu_is_leased_exclusively(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)

    pinned = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1', gpus=[1])
    blind = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert pinned.leased_gpus == [1]
    # The unpinned one must not be given the card the pinned one holds.
    assert blind.leased_gpus == [0]


def test_freeing_a_gpu_starts_the_next_entry(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entries = [
        queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
        for _ in range(3)
    ]

    asyncio.run(queue.tick())
    assert entries[2].state == 'pending'

    manager.jobs[0].status = 'succeeded'
    asyncio.run(queue.tick())

    assert entries[2].state == 'running'
    assert entries[2].leased_gpus == [0]


def test_the_queue_writes_gpu_ids_it_was_not_given(project, host_registry, nodes, tmp_path):
    """The program has to be told which card it holds, or it takes all of them."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert manager.submitted[0]['arguments']['gpu_ids'] == '0'


def test_a_gpu_the_user_named_is_not_overwritten(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    queue.add(
        project, 'tomo_align_aretomo', {**ARETOMO, 'gpu_ids': '1'}, host='gpu1'
    )

    asyncio.run(queue.tick())

    assert manager.submitted[0]['arguments']['gpu_ids'] == '1'


# ------------------------------------------------------- admission: capacity
def test_cores_are_reserved_per_job(project, host_registry, nodes, tmp_path):
    """Two 4-core jobs fit on eight cores; a third waits."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    arguments = {**RECON, 'nr_mpi': 2, 'nr_threads': 2}
    entries = [
        queue.add(project, 'tomo_reconstruct_tomograms', arguments, host='gpu1')
        for _ in range(3)
    ]

    asyncio.run(queue.tick())

    assert entries[0].cores_required == 4
    assert [entry.state for entry in entries] == ['running', 'running', 'pending']
    assert any('cores' in reason for reason in entries[2].blocked_by)


def test_exclusive_entry_keeps_the_node_to_itself(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    whole = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1', exclusive=True)
    other = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert whole.state == 'running'
    assert other.state == 'pending'


def test_an_unreachable_node_holds_its_entries_with_a_reason(
    project, host_registry, tmp_path, monkeypatch
):
    def down(host, timeout=6.0):
        return node_metrics.NodeSnapshot(name=host.name, problem='cannot reach gpu1')

    monkeypatch.setattr(node_metrics, 'collect', down)
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert entry.state == 'pending'
    assert any('cannot reach gpu1' in reason for reason in entry.blocked_by)


# -------------------------------------------------------- admission: waiting
def test_a_derived_dependency_blocks_until_the_producer_succeeds(
    project, host_registry, nodes, tmp_path
):
    """The whole point: queue the downstream job before its input exists.

    The input's producer is found from the pipeline, so the downstream job waits
    for that process rather than for a file that happens to appear.
    """
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)

    produced = project / 'Tomograms' / 'job001' / 'tomograms.star'
    downstream = queue.add(
        project, 'tomo_align_aretomo', {'in_tiltseries': str(produced)}, host='gpu1'
    )

    # Register the producer as running, without creating the file yet.
    graph = pipeline_module.read_pipeline(project)
    pipeline_module.register_process(
        graph,
        pipeline_module.Process(
            'Tomograms/job001',
            type_label='relion.reconstructtomograms',
            status=pipeline_module.STATUS_RUNNING,
        ),
        output_nodes=[
            pipeline_module.Node(
                'Tomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    pipeline_module.write_pipeline(project, graph)

    assert queue._derive_dependencies(downstream) == ['Tomograms/job001']
    asyncio.run(queue.tick())
    assert downstream.state == 'pending'
    assert any('Tomograms/job001' in reason for reason in downstream.blocked_by)

    produced.parent.mkdir(parents=True, exist_ok=True)
    produced.write_text('# empty\n')
    graph = pipeline_module.read_pipeline(project)
    pipeline_module.update_process_status(
        graph, 'Tomograms/job001', pipeline_module.STATUS_SUCCEEDED
    )
    pipeline_module.write_pipeline(project, graph)

    asyncio.run(queue.tick())
    assert downstream.state == 'running'


def test_a_failed_producer_also_holds_the_dependent(project, host_registry, nodes, tmp_path):
    """A failed upstream job must not let its consumer run on missing output."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(
        project,
        'tomo_align_aretomo',
        ARETOMO,
        host='gpu1',
        after_jobs=['Tomograms/job001'],
    )
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

    asyncio.run(queue.tick())

    assert entry.state == 'pending'
    assert any('failed' in reason for reason in entry.blocked_by)


def test_an_entry_dependency_waits_for_its_predecessor(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    second = queue.add(
        project, 'tomo_align_aretomo', ARETOMO, host='gpu1', depends_on=[first.id]
    )

    asyncio.run(queue.tick())
    assert second.state == 'pending'

    manager.jobs[0].status = 'succeeded'
    asyncio.run(queue.tick())
    assert second.state == 'running'


def test_an_upstream_failure_does_not_silently_unblock(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    second = queue.add(
        project, 'tomo_align_aretomo', ARETOMO, host='gpu1', depends_on=[first.id]
    )

    asyncio.run(queue.tick())
    manager.jobs[0].status = 'failed'
    asyncio.run(queue.tick())

    assert first.state == 'failed'
    assert second.state == 'pending'
    assert any(first.id in reason for reason in second.blocked_by)


def test_a_missing_input_with_no_producer_is_a_reason_not_a_start(
    project, host_registry, nodes, tmp_path
):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(
        project,
        'tomo_align_aretomo',
        {'in_tiltseries': str(project / 'never.star')},
        host='gpu1',
    )

    asyncio.run(queue.tick())

    assert entry.state == 'pending'
    assert any('never.star' in reason for reason in entry.blocked_by)
    assert manager.submitted == []


# --------------------------------------------------------- enqueue validation
def test_enqueue_refuses_the_qsub_template(project, host_registry, tmp_path):
    """A queued job handed to qsub would bypass this queue entirely."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    with pytest.raises(queue_system.QueueError, match='replaces the qsub template'):
        queue.add(
            project, 'tomo_reconstruct_tomograms', {**RECON, 'do_queue': True}
        )


def test_enqueue_refuses_a_unknown_host(project, host_registry, tmp_path):
    queue = make_queue(StubManager(host_registry), tmp_path)
    with pytest.raises(hosts_module.HostError, match='unknown execution host'):
        queue.add(project, 'tomo_align_aretomo', ARETOMO, host='nope')


def test_enqueue_refuses_missing_required_options(project, host_registry, tmp_path):
    queue = make_queue(StubManager(host_registry), tmp_path)
    with pytest.raises(queue_system.QueueError, match='missing required options'):
        queue.add(project, 'tomo_align_aretomo', {}, host='gpu1')


def test_enqueue_refuses_a_directory_that_is_not_a_project(tmp_path, host_registry):
    queue = make_queue(StubManager(host_registry), tmp_path)
    plain = tmp_path / 'plain'
    plain.mkdir()
    with pytest.raises(queue_system.QueueError, match='not a RELION project'):
        queue.add(plain, 'tomo_align_aretomo', ARETOMO, host='gpu1')


# ------------------------------------------------------------------ cancelling
def test_cancelling_a_running_entry_stops_the_job_and_frees_the_gpu(
    project, host_registry, nodes, tmp_path
):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    second = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    third = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())
    assert third.state == 'pending'

    asyncio.run(queue.cancel(first.id))
    assert first.state == 'cancelled'

    asyncio.run(queue.tick())
    assert third.state == 'running'
    assert third.leased_gpus == [0]


def test_cancelling_a_pending_entry_never_starts_it(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.cancel(entry.id))
    asyncio.run(queue.tick())

    assert entry.state == 'cancelled'
    assert manager.submitted == []


def test_retrying_a_finished_entry_queues_it_again(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())
    manager.jobs[0].status = 'failed'
    asyncio.run(queue.tick())
    assert entry.state == 'failed'

    queue.retry(entry.id)
    assert entry.state == 'pending'
    # The name is kept: the process is still in the pipeline, and a downstream
    # entry may already name its output as its input.
    assert entry.job_id is None
    assert entry.process_name == 'AlignTiltSeries/job001'

    asyncio.run(queue.tick())
    assert entry.state == 'running'
    assert len(manager.submitted) == 2


# ---------------------------------------------------------------- persistence
def test_pending_entries_survive_a_restart(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    second = queue.add(
        project, 'tomo_align_aretomo', ARETOMO, host='gpu1', depends_on=[first.id]
    )
    asyncio.run(queue.tick())
    manager.jobs[0].status = 'failed'
    asyncio.run(queue.tick())

    reloaded = make_queue(StubManager(host_registry), tmp_path)
    restored = {entry.id: entry for entry in reloaded.entries()}

    assert restored[first.id].state == 'failed'
    assert restored[second.id].state == 'pending'
    assert restored[second.id].depends_on == [first.id]


def test_a_finished_entry_is_not_resubmitted_after_a_restart(
    project, host_registry, nodes, tmp_path
):
    """Resubmitting a finished job would write over its own result."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    asyncio.run(queue.tick())
    manager.jobs[0].status = 'succeeded'
    asyncio.run(queue.tick())
    assert entry.state == 'succeeded'

    fresh_manager = StubManager(host_registry)
    reloaded = make_queue(fresh_manager, tmp_path)
    asyncio.run(reloaded.tick())

    assert reloaded.get(entry.id).state == 'succeeded'
    assert fresh_manager.submitted == []


def test_a_running_entry_is_adopted_not_resubmitted(project, host_registry, nodes, tmp_path):
    """A restart during a long job must not start a second copy of it."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    asyncio.run(queue.tick())
    assert entry.state == 'running'

    # The pipeline still says Running, so the true state is recoverable. The
    # process is already there - the queue reserved it as Scheduled - so this
    # promotes it rather than registering a duplicate.
    graph = pipeline_module.read_pipeline(project)
    pipeline_module.update_process_status(
        graph, entry.process_name, pipeline_module.STATUS_RUNNING
    )
    pipeline_module.write_pipeline(project, graph)

    fresh_manager = StubManager(host_registry)
    reloaded = make_queue(fresh_manager, tmp_path)
    asyncio.run(reloaded.tick())

    adopted = reloaded.get(entry.id)
    assert adopted.state == 'running'
    assert adopted.leased_gpus == [0]
    assert fresh_manager.submitted == []


def test_an_adopted_entry_that_finished_while_away_is_settled(
    project, host_registry, nodes, tmp_path
):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    asyncio.run(queue.tick())

    graph = pipeline_module.read_pipeline(project)
    pipeline_module.update_process_status(
        graph, entry.process_name, pipeline_module.STATUS_SUCCEEDED
    )
    pipeline_module.write_pipeline(project, graph)

    reloaded = make_queue(StubManager(host_registry), tmp_path)
    settled = reloaded.get(entry.id)
    assert settled.state == 'succeeded'
    assert settled.leased_gpus == []


def test_the_counter_survives_a_restart(project, host_registry, tmp_path):
    """Entry ids must not be reused: a dependency names one by id."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='local')
    assert first.id == 'q0001'

    reloaded = make_queue(StubManager(host_registry), tmp_path)
    second = reloaded.add(project, 'tomo_align_aretomo', ARETOMO, host='local')
    assert second.id == 'q0002'


# ------------------------------------------------------------------- snapshot
def test_snapshot_reports_capacity_and_reasons(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    asyncio.run(queue.tick())

    snapshot = queue.snapshot()
    gpu1 = next(host for host in snapshot['hosts'] if host['host'] == 'gpu1')

    assert gpu1['cpus'] == 8
    assert gpu1['used_cores'] == 1
    assert [gpu['index'] for gpu in gpu1['gpus']] == [0, 1]
    assert gpu1['gpus'][0]['busy'] is True
    assert snapshot['counts']['running'] == 1
    assert snapshot['entries'][0]['leased_gpus'] == [0]


def test_snapshot_filters_by_project(project, tmp_path, host_registry):
    other = tmp_path / 'other'
    other.mkdir()
    pipeline_module.write_pipeline(other, pipeline_module.Pipeline())
    queue = make_queue(StubManager(host_registry), tmp_path)
    queue.add(project, 'tomo_align_aretomo', ARETOMO, host='local')
    queue.add(other, 'tomo_align_aretomo', ARETOMO, host='local')

    assert len(queue.snapshot(str(project))['entries']) == 1
    assert len(queue.snapshot()['entries']) == 2


def test_a_display_server_does_not_make_a_card_busy(project, host_registry, tmp_path, monkeypatch):
    """Card 0 has memory in use but no compute tenant, as on a workstation."""

    def fake(host, timeout=6.0):
        snapshot = node_metrics.NodeSnapshot(name=host.name, reachable=True)
        snapshot.cpus = 8
        snapshot.gpus = [
            node_metrics.Gpu(
                index=0, uuid='GPU-a', name='RTX', memory_used_mb=900,
                memory_total_mb=8000,
            )
        ]
        snapshot.gpu_tenants = {}
        return snapshot

    monkeypatch.setattr(node_metrics, 'collect', fake)
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert entry.state == 'running'
    assert entry.leased_gpus == [0]


# --------------------------------------------------- end to end with the manager
def test_a_queued_job_is_a_real_pipeline_job(tmp_path, host_registry):
    """The queue must go through JobManager.submit, not around it.

    Otherwise a queued job would not appear in the pipeline, would have no
    sentinel handling, and would be invisible to the desktop GUI.
    """
    project = tmp_path / 'proj'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())

    executable = tmp_path / 'recon.sh'
    executable.write_text(
        '#!/bin/sh\n'
        'out=""; while [ $# -gt 0 ]; do case "$1" in --o) out="$2"; shift 2;; '
        '*) shift;; esac; done\n'
        'echo "# tomograms" > "${out}tomograms.star"\n'
        'touch "${out}RELION_JOB_EXIT_SUCCESS"\n'
    )
    executable.chmod(0o755)

    manager = JobManager(
        PathGuard([tmp_path]),
        executable_overrides={'tomo_reconstruct_tomograms': str(executable)},
        host_registry=host_registry,
    )
    queue = queue_system.JobQueue(
        manager, PathGuard([tmp_path]), config_directory=tmp_path / 'config'
    )
    entry = queue.add(project, 'tomo_reconstruct_tomograms', RECON, host='local')

    async def run():
        for _ in range(100):
            await queue.tick()
            if entry.state in queue_system.TERMINAL_STATES:
                break
            await asyncio.sleep(0.1)
        return entry

    try:
        entry = asyncio.run(run())
    finally:
        asyncio.run(manager.shutdown())

    assert entry.state == 'succeeded', entry.as_dict()
    graph = pipeline_module.read_pipeline(project)
    process = graph.find_process(entry.process_name)
    assert process is not None
    assert process.status == pipeline_module.STATUS_SUCCEEDED
    assert (project / entry.process_name / 'RELION_JOB_EXIT_SUCCESS').is_file()


def test_a_submission_failure_is_reported_on_the_entry(project, host_registry, nodes, tmp_path):
    class Broken(StubManager):
        async def submit(self, *args, **kwargs):
            raise ValueError('the queue template is bad')

    queue = make_queue(Broken(host_registry), tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert entry.state == 'failed'
    assert 'the queue template is bad' in entry.error
    assert entry.leased_gpus == []


# ------------------------------------------------------- capacity for the panel
def test_warm_capacity_fills_every_enabled_node(project, host_registry, nodes, tmp_path):
    """A queue with nothing in it must still show what each node has free.

    The scheduler only reads nodes it has work for, so without this the panel
    would show an empty capacity row on a fresh install.
    """
    queue = make_queue(StubManager(host_registry), tmp_path)

    asyncio.run(queue.warm_capacity())
    snapshot = queue.snapshot()

    gpu1 = next(host for host in snapshot['hosts'] if host['host'] == 'gpu1')
    assert gpu1['cpus'] == 8
    assert gpu1['free_cores'] == 8
    assert [gpu['busy'] for gpu in gpu1['gpus']] == [False, False]


def test_warm_capacity_is_cached(project, host_registry, tmp_path, monkeypatch):
    """Repeated polling must not mean repeated ssh round trips."""

    calls = []

    def counting(host, timeout=6.0):
        calls.append(host.name)
        snapshot = node_metrics.NodeSnapshot(name=host.name, reachable=True)
        snapshot.cpus = 4
        return snapshot

    monkeypatch.setattr(node_metrics, 'collect', counting)
    queue = make_queue(StubManager(host_registry), tmp_path)

    asyncio.run(queue.warm_capacity())
    asyncio.run(queue.warm_capacity())

    assert calls.count('gpu1') == 1


# ------------------------------------------------------------------- cycles
def test_a_self_waiting_chain_is_refused(project, host_registry, tmp_path):
    """Two entries waiting for each other would hang forever."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='local')
    second = queue.add(
        project, 'tomo_align_aretomo', ARETOMO, host='local', depends_on=[first.id]
    )

    with pytest.raises(queue_system.QueueError, match='cycle'):
        queue.add(
            project,
            'tomo_align_aretomo',
            ARETOMO,
            host='local',
            depends_on=[second.id, first.id],
        )


def test_a_declared_dependency_on_itself_is_allowed_only_once(project, host_registry, tmp_path):
    """Two independent dependencies are not a cycle."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='local')
    second = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='local')

    third = queue.add(
        project, 'tomo_align_aretomo', ARETOMO, host='local',
        depends_on=[first.id, second.id],
    )
    assert third.depends_on == [first.id, second.id]


# ------------------------------------------------------- input slots / reservation
def test_enqueue_assigns_the_process_name_and_predicted_outputs(
    project, host_registry, tmp_path
):
    """The number must be fixed at enqueue, not at dispatch.

    Every output path a later stage names depends on it, so allocating at
    dispatch would make a pre-built chain impossible.
    """
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    assert entry.process_name == 'AlignTiltSeries/job001'
    assert entry.predicted_outputs['aligned_tilt_series.star'] == (
        'AlignTiltSeries/job001/aligned_tilt_series.star'
    )


def test_a_queued_job_is_scheduled_in_the_pipeline(project, host_registry, tmp_path):
    """RELION's own model: the job exists as Scheduled before it runs."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    pipeline = pipeline_module.read_pipeline(project)
    process = pipeline.find_process(entry.process_name)
    assert process is not None
    assert process.status == pipeline_module.STATUS_SCHEDULED
    # Its output node is already there, which is what a downstream job names.
    assert (
        pipeline.find_node(entry.predicted_outputs['aligned_tilt_series.star'])
        is not None
    )


def test_numbers_are_allocated_in_order(project, host_registry, tmp_path):
    queue = make_queue(StubManager(host_registry), tmp_path)
    first = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    second = queue.add(
        project,
        'tomo_denoise_train',
        {'tomograms_for_training': 't.star', 'in_tomoset': str(project / 'a.star')},
        host='gpu1',
    )

    assert first.process_name == 'AlignTiltSeries/job001'
    assert second.process_name == 'Denoise/job002'


def test_a_downstream_job_names_an_upstream_slot(project, host_registry, nodes, tmp_path):
    """The point of the feature: queue stage two before stage one has run."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    upstream = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    downstream = queue.add(
        project,
        'tomo_denoise_train',
        {'tomograms_for_training': 'train.star'},
        host='gpu1',
        inputs={'in_tomoset': f'{upstream.id}:aligned_tilt_series.star'},
    )

    assert downstream.arguments['in_tomoset'] == str(
        project / 'AlignTiltSeries' / 'job001' / 'aligned_tilt_series.star'
    )
    assert downstream.depends_on == [upstream.id]


def test_a_slot_reference_to_an_unknown_entry_is_refused(project, host_registry, tmp_path):
    queue = make_queue(StubManager(host_registry), tmp_path)
    with pytest.raises(queue_system.QueueError, match='not in the queue'):
        queue.add(
            project,
            'tomo_denoise_train',
            {'tomograms_for_training': 't.star'},
            host='gpu1',
            inputs={'in_tomoset': 'q9999:tomograms.star'},
        )


def test_a_slot_reference_to_an_unknown_filename_is_refused(project, host_registry, tmp_path):
    """A typo must fail at enqueue, not when the job finally starts."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    upstream = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    with pytest.raises(queue_system.QueueError, match='does not write'):
        queue.add(
            project,
            'tomo_denoise_train',
            {'tomograms_for_training': 't.star'},
            host='gpu1',
            inputs={'in_tomoset': f'{upstream.id}:nope.star'},
        )


def test_a_malformed_slot_reference_is_refused(project, host_registry, tmp_path):
    queue = make_queue(StubManager(host_registry), tmp_path)
    with pytest.raises(queue_system.QueueError, match='entry_id:filename'):
        queue.add(
            project,
            'tomo_denoise_train',
            {'tomograms_for_training': 't.star'},
            host='gpu1',
            inputs={'in_tomoset': 'tomograms.star'},
        )


def test_a_reserved_output_is_not_a_missing_input(project, host_registry, nodes, tmp_path):
    """Without this the chain would never start: the file cannot exist yet."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    upstream = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    downstream = queue.add(
        project,
        'tomo_denoise_train',
        {'tomograms_for_training': 'train.star'},
        host='gpu1',
        inputs={'in_tomoset': f'{upstream.id}:aligned_tilt_series.star'},
    )

    missing = queue._missing_inputs(downstream)
    assert missing == {}


def test_a_real_typo_outside_the_pipeline_is_still_missing(project, host_registry, nodes, tmp_path):
    """Existence checking must not be weakened by the slot support."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(
        project,
        'tomo_denoise_train',
        {
            'tomograms_for_training': 't.star',
            'in_tomoset': str(project / 'never.star'),
        },
        host='gpu1',
    )
    assert 'in_tomoset' in queue._missing_inputs(entry)


def test_a_chain_starts_stage_by_stage(project, host_registry, nodes, tmp_path):
    """Two queued stages: the second only starts after the first succeeds."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    upstream = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    downstream = queue.add(
        project,
        'tomo_denoise_train',
        {'tomograms_for_training': 'train.star'},
        host='gpu1',
        inputs={'in_tomoset': f'{upstream.id}:aligned_tilt_series.star'},
    )

    asyncio.run(queue.tick())
    assert upstream.state == 'running'
    assert downstream.state == 'pending'

    manager.jobs[0].status = 'succeeded'
    graph = pipeline_module.read_pipeline(project)
    pipeline_module.update_process_status(
        graph, upstream.process_name, pipeline_module.STATUS_SUCCEEDED
    )
    pipeline_module.write_pipeline(project, graph)
    asyncio.run(queue.tick())

    assert downstream.state == 'running'
    # And it ran under the name reserved for it, not a fresh number.
    assert manager.submitted[1]['process_name'] == downstream.process_name


def test_dispatch_uses_the_reserved_name(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert manager.submitted[0]['process_name'] == entry.process_name


def test_cancelling_a_queued_entry_removes_its_scheduled_process(
    project, host_registry, nodes, tmp_path
):
    """A job that will never run must not keep its outputs reserved."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    assert pipeline_module.read_pipeline(project).find_process(entry.process_name)

    asyncio.run(queue.cancel(entry.id))

    assert pipeline_module.read_pipeline(project).find_process(entry.process_name) is None
    assert entry.process_name is None


def test_cancelling_a_running_entry_keeps_its_pipeline_row(
    project, host_registry, nodes, tmp_path
):
    """A started job's directory may hold real output; only its status changes."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    asyncio.run(queue.tick())
    name = entry.process_name

    asyncio.run(queue.cancel(entry.id))

    assert entry.state == 'cancelled'
    assert pipeline_module.read_pipeline(project).find_process(name) is not None
    assert entry.process_name == name


def test_retry_keeps_the_reservation_so_dependents_still_resolve(
    project, host_registry, nodes, tmp_path
):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    upstream = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    downstream = queue.add(
        project,
        'tomo_denoise_train',
        {'tomograms_for_training': 'train.star'},
        host='gpu1',
        inputs={'in_tomoset': f'{upstream.id}:aligned_tilt_series.star'},
    )
    name = upstream.process_name

    queue.retry(upstream.id)

    assert upstream.process_name == name
    assert upstream.predicted_outputs['aligned_tilt_series.star'] == (
        f'{name}/aligned_tilt_series.star'
    )
    assert downstream.depends_on == [upstream.id]


def test_retry_after_cancel_takes_a_new_number(project, host_registry, nodes, tmp_path):
    """The old reservation was released, so the name must be re-reserved."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    asyncio.run(queue.cancel(entry.id))
    assert entry.process_name is None

    queue.retry(entry.id)

    assert entry.process_name is not None
    assert pipeline_module.read_pipeline(project).find_process(entry.process_name)


def test_dispatch_re_reserves_when_the_slot_was_deleted_elsewhere(
    project, host_registry, nodes, tmp_path
):
    """Someone may delete the scheduled job in the desktop GUI.

    Submitting under the vanished name would register a job at a number nothing
    points at, so the queue takes a fresh number and says so.
    """
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    reserved = entry.process_name

    # Simulate a deletion in the GUI: the process row is gone.
    graph = pipeline_module.read_pipeline(project)
    graph.processes = [
        p for p in graph.processes if p.name != reserved
    ]
    pipeline_module.write_pipeline(project, graph)

    asyncio.run(queue.tick())

    assert entry.state == 'running'
    assert entry.process_name != reserved
    assert manager.submitted[0]['process_name'] == entry.process_name
    assert any('was gone' in warning for warning in entry.warnings)


def test_a_reserved_job_has_a_directory_and_job_star(project, host_registry, tmp_path):
    """RELION creates the job directory when it schedules a job.

    `prepareFinalCommand` calls `mktree(outputname)` before building the commands
    (pipeline_jobs.cpp:699), so a job directory always exists before its pipeline
    row does. The rest of this package - and the GUI - assumes that: without the
    directory, opening a queued job is a 404 and the canvas points at nothing.
    """
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    directory = project / entry.process_name
    assert directory.is_dir()
    assert (directory / 'job.star').is_file()
    # The GUI reads the output-node list to know what the job will produce.
    assert (directory / 'RELION_OUTPUT_NODES.star').is_file()


def test_the_reserved_job_star_carries_the_full_option_table(project, host_registry, tmp_path):
    """The GUI opens the job window from job.star, so a queued job must open
    with its parameters rather than blank fields."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(
        project,
        'tomo_reconstruct_tomograms',
        {'in_tiltseries': 't.star', 'xdim': 512, 'ydim': 512, 'zdim': 200},
        host='gpu1',
    )

    options = job_artifacts.read_job_star(project / entry.process_name)
    # STAR values are strings; 512 must not have become 512.0 either.
    assert options.get('xdim') == '512'
    # A value the caller never set is filled from the schema default.
    assert options.get('nr_threads') is not None


def test_reserving_needs_no_job_manager(project, host_registry, tmp_path):
    """A reservation is a pipeline write, not an execution.

    The compat harness and these tests enqueue against a stub manager; if
    reserving reached into it, neither could.
    """
    class Bare:
        hosts = host_registry

    queue = queue_system.JobQueue(
        Bare(), PathGuard([tmp_path]), config_directory=tmp_path / 'cfg'
    )
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    assert entry.process_name == 'AlignTiltSeries/job001'
    assert (project / entry.process_name / 'job.star').is_file()


def test_a_reserved_output_gets_a_nodes_entry(project, host_registry, tmp_path):
    """Without this the output is invisible to any input picker.

    RELION touches the `.Nodes` entry for a scheduled job even though the file
    does not exist yet (`touch_if_not_exist = status == PROC_SCHEDULED`,
    pipeliner.cpp:120), and its own input picker is a listing of
    `.Nodes/<coarse type>/` (gui_entries.cpp:347). A reserved job that skipped
    this would have an output nothing could link to.
    """
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    node = entry.predicted_outputs['aligned_tilt_series.star']
    slot_directory = project / '.Nodes' / 'TomogramGroupMetadata'
    assert slot_directory.is_dir(), sorted(p.name for p in (project / '.Nodes').iterdir())
    assert (slot_directory / node).is_file()


def test_node_candidates_lists_only_the_slot_type(project, host_registry, tmp_path):
    """Two outputs of different coarse types must not bleed into one slot."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    star = job_artifacts.node_candidates(
        project, 'TomogramGroupMetadata.star.relion'
    )
    pdf = job_artifacts.node_candidates(project, 'LogFile.pdf.relion')
    assert star == ['AlignTiltSeries/job001/aligned_tilt_series.star']
    assert pdf == ['AlignTiltSeries/job001/logfile.pdf']


def test_cancelling_removes_the_nodes_entry(project, host_registry, nodes, tmp_path):
    """A cancelled job's output must stop being offered as an input."""
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    node = entry.predicted_outputs['aligned_tilt_series.star']
    assert (project / '.Nodes' / 'TomogramGroupMetadata' / node).is_file()

    asyncio.run(queue.cancel(entry.id))

    assert not (project / '.Nodes' / 'TomogramGroupMetadata' / node).exists()


# ------------------------------------------------- sentinels settle a lost job
def test_an_adopted_entry_reads_its_sentinel(project, host_registry, nodes, tmp_path):
    """The sentinel is the durable record; the pipeline row is a summary.

    `sync_pipeline_status` swallows write failures, so a row can lag behind a
    job that finished. Before this, an adopted entry stayed `running` and kept
    its GPU leased for good, with the proof sitting in the job directory.
    """
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    # A job that was recorded as running, then finished while the agent was down.
    entry.state = 'running'
    entry.leased_gpus = [0]
    entry.job_id = 'gone'
    (project / entry.process_name / JOB_SUCCESS_FILENAME).touch()
    queue._save()

    reloaded = make_queue(StubManager(host_registry), tmp_path)
    settled = reloaded.get(entry.id)

    assert settled.state == 'succeeded'
    assert settled.leased_gpus == []


def test_a_lost_process_with_no_sentinel_is_failed_and_frees_its_gpu(
    project, host_registry, nodes, tmp_path
):
    """A killed job writes nothing. Without this its card stays leased.

    The grace period exists because a job that is starting looks identical to
    one that died; it is set to 0 here so the test does not sleep.
    """
    queue = make_queue(
        StubManager(host_registry), tmp_path, lost_after=0.0
    )
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    entry.state = 'running'
    entry.leased_gpus = [0]
    entry.started_at = time.time() - 3600
    entry.job_id = 'never-registered'
    # The pipeline row still says Running, and nothing wrote an exit file.
    graph = pipeline_module.read_pipeline(project)
    pipeline_module.update_process_status(
        graph, entry.process_name, pipeline_module.STATUS_RUNNING
    )
    pipeline_module.write_pipeline(project, graph)

    asyncio.run(queue._collect_finished())

    assert entry.state == 'failed'
    assert entry.leased_gpus == []
    assert 'no exit file' in (entry.error or '')


def test_a_freshly_dispatched_entry_is_not_declared_lost(project, host_registry, nodes, tmp_path):
    """The grace period must not fire on a job that is simply starting."""
    queue = make_queue(StubManager(host_registry), tmp_path, lost_after=600.0)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    asyncio.run(queue.tick())
    assert entry.state == 'running'

    # Pretend the manager lost it immediately, as a restart would look.
    queue.manager.jobs.clear()
    asyncio.run(queue._collect_finished())

    assert entry.state == 'running', 'inside the grace period, so not lost yet'
    assert entry.leased_gpus == [0]


# ------------------------------------------------------------------ retention
def test_finished_entries_are_pruned_by_count(project, host_registry, nodes, tmp_path):
    """`GET /api/queue` returns every entry, so the list must stay bounded."""
    queue = make_queue(
        StubManager(host_registry), tmp_path, keep_finished=3
    )
    entries = [
        queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
        for _ in range(8)
    ]
    for index, entry in enumerate(entries):
        entry.state = 'succeeded'
        entry.finished_at = time.time() + index
    queue._save()

    assert len(queue.entries()) == 3
    # The newest survive: they are the ones a user looks at.
    assert {entry.id for entry in queue.entries()} == {
        entry.id for entry in entries[-3:]
    }


def test_live_entries_are_never_pruned(project, host_registry, nodes, tmp_path):
    """A pending entry is the record of a job that still exists."""
    queue = make_queue(
        StubManager(host_registry), tmp_path, keep_finished=0
    )
    live = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    done = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    done.state = 'succeeded'
    done.finished_at = time.time()
    queue._save()

    assert queue.get(live.id) is not None
    assert queue.get(done.id) is None


def test_old_finished_entries_expire(project, host_registry, nodes, tmp_path):
    queue = make_queue(
        StubManager(host_registry), tmp_path, keep_finished_seconds=60.0
    )
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')
    entry.state = 'succeeded'
    entry.finished_at = time.time() - 3600
    queue._save()

    assert queue.get(entry.id) is None


# ------------------------------------------------------------ gpu memory hint
def test_a_declared_gpu_memory_need_waits_for_a_big_enough_card(
    project, host_registry, tmp_path, monkeypatch
):
    """RELION's options carry no memory figure, so this is opt-in.

    When declared, the queue must not start the job on a card that cannot hold
    it: RELION would accept it and fail with an out-of-memory error mid-run.
    """

    def small_cards(host, timeout=6.0):
        snapshot = node_metrics.NodeSnapshot(name=host.name, reachable=True)
        snapshot.cpus = 8
        snapshot.gpus = [
            node_metrics.Gpu(
                index=0, uuid='GPU-a', name='RTX', memory_total_mb=8000
            )
        ]
        return snapshot

    monkeypatch.setattr(node_metrics, 'collect', small_cards)
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(
        project,
        'tomo_align_aretomo',
        ARETOMO,
        host='gpu1',
        gpu_memory_mb=40000,
    )

    asyncio.run(queue.tick())

    assert entry.state == 'pending'
    assert any('40000 MB' in reason for reason in entry.blocked_by)
    assert manager.submitted == []


def test_an_undeclared_memory_need_is_not_guessed(project, host_registry, nodes, tmp_path):
    """No figure means no check: a guess would reject work RELION would accept."""
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(project, 'tomo_align_aretomo', ARETOMO, host='gpu1')

    asyncio.run(queue.tick())

    assert entry.state == 'running'
    assert entry.gpu_memory_mb is None


def test_a_big_enough_card_is_accepted(project, host_registry, nodes, tmp_path):
    manager = StubManager(host_registry)
    queue = make_queue(manager, tmp_path)
    entry = queue.add(
        project, 'tomo_align_aretomo', ARETOMO, host='gpu1', gpu_memory_mb=1000
    )

    asyncio.run(queue.tick())

    assert entry.state == 'running'


def test_a_program_path_is_not_treated_as_a_waiting_input(
    project, host_registry, tmp_path
):
    """`qsubscript` names a template, not data; waiting for it never ends.

    A job.star copied from another machine carries
    ``qsubscript=/public/EM/RELION/.../relion_qsub.csh``. That file is not an
    input - it lives on the cluster, and on a node without RELION it never
    appears. Treating it as a dependency left the job pending forever with a
    reason that named the wrong problem.
    """
    queue = make_queue(StubManager(host_registry), tmp_path)
    entry = queue.add(
        project,
        'ctffind',
        {
            'input_star_mics': str(project / 'tilt_series.star'),
            'qsubscript': '/public/EM/RELION/relion/bin/relion_qsub.csh',
            'nr_mpi': 1,
            'nr_threads': 1,
        },
        host='gpu1',
    )
    (project / 'tilt_series.star').write_text('data_\n\nloop_\n')

    reasons = queue._missing_inputs(entry)  # noqa: SLF001 - the gate under test

    assert reasons == {}
