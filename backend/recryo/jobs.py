# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Job execution following RELION's own directory + sentinel-file protocol.

The desktop GUI does not talk to jobs over a socket. ``prepareFinalCommand``
appends ``--pipeline_control <job_dir>`` and the program writes one of
``RELION_JOB_EXIT_SUCCESS`` / ``RELION_JOB_EXIT_FAILURE`` /
``RELION_JOB_ABORTED`` into that directory; cancellation works by touching
``RELION_JOB_ABORT_NOW``. Reusing those exact names means jobs submitted from
the web UI behave identically to jobs submitted from the desktop GUI, including
how they appear in the RELION pipeline.

Command construction lives in ``job_commands.py`` because it mirrors a specific
C++ function per job; this module is only about running what it produces.
"""

import asyncio
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ._vendor import (
    ABORT_JOB_NOW_FILENAME,
    JOB_ABORTED_FILENAME,
    JOB_FAILURE_FILENAME,
    JOB_SUCCESS_FILENAME,
)
from . import hosts as hosts_module
from . import job_artifacts
from . import statuses
from . import job_commands, job_queue
from . import cli_options
from . import pipeline as pipeline_module
from .generated_schema import JOBS as SCHEMA_JOBS
from .job_specs import get_spec, output_node_names, process_name_for
from .paths import PathGuard

DEFAULT_POLL_INTERVAL = 0.5
MAX_LOG_BYTES = 256 * 1024

# The desktop GUI's I/O view reads these, so writing them is what makes a web
# job's output visible in the official GUI (gui_mainwindow.cpp:784).
RUN_OUT_FILENAME = 'run.out'
RUN_ERR_FILENAME = 'run.err'
# The log the web UI follows. Kept separate so a tail never has to reason about
# interleaved stdout and stderr.
WEB_LOG_FILENAME = 'web_job.log'

# Job types the API accepts. Derived from the command specs so a job cannot be
# offered without a way to build its command line. This includes API-only
# aliases (`select` for `select_values`); the picker uses
# `job_commands.selectable_job_types()` so the same job is not listed twice.
SUPPORTED_JOBS = {
    job_type: spec.executable
    for job_type, spec in job_commands.COMMAND_SPECS.items()
}

# What the UI offers to choose from.
SELECTABLE_JOBS = {
    job_type: SUPPORTED_JOBS[job_type]
    for job_type in job_commands.selectable_job_types()
}


@dataclass
class Job:
    id: str
    job_type: str
    command: List[str]
    output_directory: Path
    started_at: float
    pid: Optional[int] = None
    returncode: Optional[int] = None
    log_path: Optional[Path] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    # Set when the job is registered in a RELION project pipeline.
    project_directory: Optional[Path] = None
    process_name: Optional[str] = None
    registered_status: Optional[str] = None
    # The GUI's variable -> value mapping, kept so the status sync can rewrite
    # job.star without asking the client again.
    options: Dict[str, object] = field(default_factory=dict)
    # How the job was started: 'local', 'mpi' or 'queue'.
    run_mode: str = 'local'
    # Which machine it ran on: 'local', or a name from the host registry. The
    # project is on shared storage, so a remote job's directory is the same path
    # on both machines and nothing is copied.
    host: str = 'local'
    nr_mpi: int = 1
    nr_threads: int = 1
    submission_script: Optional[Path] = None

    @property
    def status(self) -> str:
        directory = self.output_directory
        # A queued job's process is only the submit command: its exit code says
        # the job was *accepted*, not that it ran. Its real outcome is the
        # sentinel file the program writes when the queue finally starts it, so
        # a successful submission stays 'queued' until that appears.
        if self.run_mode == 'queue' and not self.error:
            sentinel = sentinel_status(directory)
            if sentinel is not None:
                return sentinel
            if self.cancel_requested:
                return statuses.ABORTED
            if self.returncode is not None and self.returncode != 0:
                return statuses.FAILED
            return statuses.QUEUED
        if self.error is None and self.returncode is None:
            return statuses.RUNNING
        if self.error is not None:
            return statuses.FAILED
        if self.cancel_requested:
            return statuses.ABORTED
        if self.returncode == 0:
            return statuses.SUCCEEDED
        if (directory / JOB_ABORTED_FILENAME).exists():
            return statuses.ABORTED
        return statuses.FAILED

    @property
    def pipeline_status(self) -> Optional[str]:
        """The status label RELION's pipeline uses for this job."""
        return statuses.label(self.status)

    def as_dict(self) -> dict:
        return {
            'id': self.id,
            'job_type': self.job_type,
            'status': self.status,
            'command': self.command,
            'output_directory': str(self.output_directory),
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'pid': self.pid,
            'returncode': self.returncode,
            'error': self.error,
            'duration': (self.finished_at or time.time()) - self.started_at,
            'process_name': self.process_name,
            'pipeline_status': self.pipeline_status,
            'run_mode': self.run_mode,
            'host': self.host,
            'nr_mpi': self.nr_mpi,
            'nr_threads': self.nr_threads,
            'submission_script': (
                str(self.submission_script) if self.submission_script else None
            ),
        }


class JobManager:
    """Track running jobs and their sentinel files."""

    def __init__(
        self,
        guard: PathGuard,
        executable_overrides: Optional[Dict[str, str]] = None,
        host_registry: Optional[hosts_module.HostRegistry] = None,
    ):
        self.guard = guard
        # Where a job may run. Always contains 'local', so a single-machine
        # install behaves exactly as before.
        self.hosts = host_registry or hosts_module.HostRegistry()
        # Job type -> executable to run instead of the schema's. This is how the
        # tests run a shell script in place of a real RELION program, and how a
        # deployment can point a job at an MPI wrapper without a code change.
        self.executable_overrides: Dict[str, str] = dict(executable_overrides or {})
        self._jobs: Dict[str, Job] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._counter = 0

    # ------------------------------------------------------------ building
    def _with_extra_cli(
        self, job_type: str, arguments: Dict[str, object]
    ) -> Dict[str, object]:
        """Fold the advanced panel's arguments into ``other_args``.

        Every builder already appends ``other_args`` at the place its program
        expects, so the extra flags travel through that one channel instead of
        a second one.
        """
        merged, _unknown = cli_options.apply(job_type, arguments)
        return merged

    def build_program_commands(
        self,
        job_type: str,
        arguments: Dict[str, object],
        output_directory: Path,
        project_directory: Optional[Path] = None,
        process_name: Optional[str] = None,
    ) -> List[str]:
        """The per-job program commands, before MPI wrapping or queueing.

        These are what ``getCommands*`` produce. ``job_queue.build_run_plan``
        then adds ``--pipeline_control``, any ``mpirun``, the redirection, or
        the submission script, exactly as ``prepareFinalCommand`` does.

        When a project is given the job directory is known to be
        ``<Type>/job<NNN>``, so the command is built against that path rather
        than the caller's temporary directory. This is what makes a dry run show
        the command that will actually be executed.
        """
        prefix = self.job_output_prefix(
            job_type, output_directory, project_directory, process_name
        )
        override = self.executable_overrides.get(job_type)
        if job_type not in job_commands.COMMAND_SPECS:
            # A registered type outside the schema is a test double (or a future
            # job the schema has not learned yet): run the executable directly.
            executable = override or SUPPORTED_JOBS.get(job_type)
            if executable is None:
                raise ValueError(f'unsupported job type {job_type!r}')
            return [executable]
        arguments = self._with_extra_cli(job_type, arguments)
        raw = job_commands.build_command(
            job_type, arguments, prefix, executable=override
        )
        # job_commands returns argv tokens for a single-program job
        # (["relion_x", "--t", "t.star", ...]) and one full command string per
        # program for the multi-program jobs. The run planner works in whole
        # command strings, so a token list is joined here, where the distinction
        # is known: any element that starts with '-' or does not look like a
        # program name means this is a token list.
        if raw and any(part.startswith('-') for part in raw):
            return [' '.join(raw)]
        return list(raw)

    def build_command(
        self,
        job_type: str,
        arguments: Dict[str, object],
        output_directory: Path,
        project_directory: Optional[Path] = None,
    ) -> List[str]:
        """The argv that will be executed, including MPI and queue handling.

        Kept as the single entry point for dry runs and for tests, so what a dry
        run reports is by construction what ``submit`` will start.
        """
        prefix = self.job_output_prefix(job_type, output_directory, project_directory)
        commands = self.build_program_commands(
            job_type, arguments, output_directory, project_directory
        )
        plan = job_queue.build_run_plan(
            commands, prefix, self._run_options(job_type, arguments)
        )
        return plan.argv

    def build_run_plan(
        self,
        job_type: str,
        arguments: Dict[str, object],
        output_directory: Path,
        project_directory: Optional[Path] = None,
        process_name: Optional[str] = None,
    ) -> job_queue.RunPlan:
        prefix = self.job_output_prefix(
            job_type, output_directory, project_directory, process_name
        )
        commands = self.build_program_commands(
            job_type,
            arguments,
            output_directory,
            project_directory,
            process_name=process_name,
        )
        return job_queue.build_run_plan(
            commands, prefix, self._run_options(job_type, arguments)
        )

    @staticmethod
    def _run_options(job_type: str, arguments: Dict[str, object]) -> Dict[str, object]:
        """The shared run options (threads, MPI, queue) with their defaults.

        These live in ``RelionJob::initialise`` rather than in the job's own
        options, so a request that does not mention them still has to get the
        GUI's defaults instead of "unset".
        """
        options: Dict[str, object] = {}
        schema = SCHEMA_JOBS.get(job_type)
        if schema is not None:
            for field in schema['fields']:
                if field.get('shared') and 'default' in field:
                    options[field['variable']] = field['default']
        options.update({k: v for k, v in arguments.items() if v is not None})
        return options

    def job_output_prefix(
        self,
        job_type: str,
        output_directory: Path,
        project_directory: Optional[Path] = None,
        process_name: Optional[str] = None,
    ) -> str:
        """The ``outputname`` RELION would pass: a job directory, trailing slash.

        Jobs outside a project keep whatever directory the caller chose. Jobs
        inside one always live at ``<Type>/job<NNN>``, which is decided here so
        the command and the pipeline registration agree.

        `process_name` is passed by the queue, which reserved the name when the
        job was enqueued. Without it the number would be read again - and
        because the reservation already advanced the counter, the command would
        write to the *next* job directory while registration used the reserved
        one, so the job would fail to find its own output directory.
        """
        if project_directory is None:
            return str(output_directory).rstrip('/') + '/'
        if process_name:
            return str(project_directory / process_name).rstrip('/') + '/'
        spec = get_spec(job_type)
        try:
            pipeline = pipeline_module.read_pipeline(project_directory)
            number = pipeline.next_job_number()
        except (FileNotFoundError, ValueError):
            number = 1
        return str(project_directory / process_name_for(spec, number)).rstrip('/') + '/'

    # ----------------------------------------------------------- submitting
    async def submit(
        self,
        job_type: str,
        arguments: Dict[str, object],
        output_directory: Path,
        *,
        project_directory: Optional[Path] = None,
        dry_run: bool = False,
        host: Optional[str] = None,
        process_name: Optional[str] = None,
    ) -> Job:
        # Resolved up front: an unknown host is a client error, and the job
        # should not be registered in the pipeline only to fail at spawn time.
        execution_host = self.hosts.get(host)
        output_directory = self.guard.resolve(output_directory)
        if project_directory is not None:
            project_directory = self.guard.resolve(project_directory)

        # The advanced panel's flags belong to the job, not to one command: they
        # have to reach the argv, the pipeline registration and job.star, so
        # they are folded in once here rather than in each of those places.
        arguments = self._with_extra_cli(job_type, arguments)

        # A malformed submission (a missing required option, an unknown job
        # type) is a client error, so it is raised rather than turned into a
        # failed job: the API maps it to a 400 with the reason.
        plan = self.build_run_plan(
            job_type,
            arguments,
            output_directory,
            project_directory,
            process_name=process_name,
        )
        if plan.mode == 'queue' and not execution_host.is_local:
            raise ValueError(
                f'cannot submit a queued job to {execution_host.name}: the queue '
                'template is rendered and submitted on the agent machine, not the '
                'node. Run the job locally with do_queue=No and the host set, or '
                'use the scheduler'
            )
        command = plan.argv

        self._counter += 1
        job_id = f'{job_type}-{int(time.time())}-{self._counter}'
        directory = output_directory
        if project_directory is not None:
            if process_name is not None:
                # The queue reserved this name when the job was enqueued, so the
                # command, the directory and the pipeline row all agree without
                # asking the counter again - which another queued job may have
                # advanced in the meantime.
                directory = project_directory / process_name
            else:
                spec = get_spec(job_type)
                try:
                    number = pipeline_module.read_pipeline(
                        project_directory
                    ).next_job_number()
                except (FileNotFoundError, ValueError):
                    number = 1
                directory = project_directory / process_name_for(spec, number)

        if not dry_run:
            directory.mkdir(parents=True, exist_ok=True)
            # Clear stale sentinels so a new run is not reported as finished
            # because of files left behind by a previous one.
            for sentinel in (
                JOB_SUCCESS_FILENAME,
                JOB_FAILURE_FILENAME,
                JOB_ABORTED_FILENAME,
                ABORT_JOB_NOW_FILENAME,
            ):
                (directory / sentinel).unlink(missing_ok=True)

        job = Job(
            id=job_id,
            job_type=job_type,
            command=command,
            output_directory=directory,
            started_at=time.time(),
            # A queued job's program is started by the scheduler, not by us, so
            # its output goes to run.out; watching that file is what makes the
            # tail live. A local job is pumped through pipes into web_job.log.
            log_path=(
                None
                if dry_run
                else directory
                / (
                    RUN_OUT_FILENAME
                    if plan.mode == 'queue'
                    else WEB_LOG_FILENAME
                )
            ),
            project_directory=project_directory,
            options=dict(arguments),
            run_mode=plan.mode,
            host=execution_host.name,
            nr_mpi=plan.nr_mpi,
            nr_threads=plan.nr_threads,
            submission_script=plan.submission_script,
            # Set when the queue reserved this job's place in the pipeline at
            # enqueue time. registration promotes it rather than allocating a
            # new number, so the output paths other jobs already name stay
            # correct.
            process_name=process_name,
        )

        # A dry run must not touch the project: writing the pipeline advances
        # the job counter and leaves a process with no directory behind it.
        if dry_run:
            job.returncode = 0
            job.finished_at = time.time()
            self._jobs[job_id] = job
            return job

        if project_directory is not None:
            try:
                self.register_in_pipeline(job, arguments, directory, project_directory)
            except (ValueError, OSError, KeyError) as exc:
                job.error = f'could not register job in the RELION project: {exc}'
                job.finished_at = time.time()
                self._jobs[job_id] = job
                return job

        self._jobs[job_id] = job

        try:
            log_handle = open(job.log_path, 'wb')
            stdout_handle = open(directory / RUN_OUT_FILENAME, 'wb')
            stderr_handle = open(directory / RUN_ERR_FILENAME, 'wb')
        except OSError as exc:
            job.error = f'could not open log file: {exc}'
            job.finished_at = time.time()
            return job

        # A remote job runs the same argv through ssh. `exec` on the far side
        # replaces the remote shell, so the exit code is the program's own - which
        # is what `_finish` reconciles with the sentinel files.
        launch = hosts_module.wrap_for_host(command, str(directory), execution_host)
        try:
            process = await asyncio.create_subprocess_exec(
                *launch,
                cwd=str(directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            log_handle.close()
            stdout_handle.close()
            stderr_handle.close()
            job.error = f'executable not found: {job_commands.COMMAND_SPECS[job_type].executable}'
            job.finished_at = time.time()
            return job
        except OSError as exc:
            log_handle.close()
            stdout_handle.close()
            stderr_handle.close()
            job.error = f'could not start job: {exc}'
            job.finished_at = time.time()
            return job

        job.pid = process.pid
        self._processes[job_id] = process
        self._tasks[job_id] = asyncio.create_task(
            self._pump_output(
                job, process, log_handle, stdout_handle, stderr_handle
            )
        )
        if plan.mode == 'queue':
            # The submit command has returned, but the job is now the queue's.
            # Watch its directory for the sentinel the program writes when it
            # eventually runs, so the status and the pipeline do not stay
            # "Scheduled" after it finished.
            self._watch_queued_job(job)
        return job

    def _watch_queued_job(self, job: Job, interval: float = 2.0) -> None:
        """Poll a queued job's sentinels until one appears.

        The submit command has already exited by the time this starts, so the
        job's status is driven entirely by the sentinel files the scheduled
        program writes. Each time the status moves, the pipeline is updated,
        which is what turns the GUI's Scheduled entry into Succeeded or Failed.
        """

        async def watch() -> None:
            last = job.registered_status
            try:
                while True:
                    status = job.status
                    if status != 'queued' or job.registered_status != last:
                        self.sync_pipeline_status(job)
                        last = job.registered_status
                    if status in ('succeeded', 'failed', 'aborted'):
                        if status == 'succeeded':
                            self._create_output_node_links(job)
                        return
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a watcher must not crash the server
                return

        self._tasks[f'{job.id}:queue'] = asyncio.create_task(watch())

    async def _pump_output(
        self,
        job: Job,
        process: asyncio.subprocess.Process,
        log_handle,
        stdout_handle,
        stderr_handle,
    ) -> None:
        """Copy the child's output to the web log and to run.out/run.err.

        RELION redirects a job's output to ``run.out`` and ``run.err``, and its
        I/O view reads those. The web UI also keeps ``web_job.log``, which holds
        both streams so the live tail has one ordered narrative instead of two
        files the browser would have to interleave.

        Both pipes are drained concurrently: reading them in sequence can
        deadlock once the unread pipe fills its buffer.
        """

        async def copy(stream, *targets) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break
                for target in targets:
                    target.write(chunk)
                    target.flush()

        try:
            await asyncio.gather(
                copy(process.stdout, log_handle, stdout_handle),
                copy(process.stderr, log_handle, stderr_handle),
            )
        finally:
            returncode = await process.wait()
            for handle in (log_handle, stdout_handle, stderr_handle):
                try:
                    handle.close()
                except OSError:
                    pass
            await self._finish(job, returncode)

    async def _finish(self, job: Job, returncode: int) -> None:
        """Reconcile the exit code with the sentinel files, then sync status."""
        job.returncode = returncode
        job.finished_at = time.time()

        directory = job.output_directory
        if job.run_mode == 'queue':
            # The process that just exited is the submit command. A non-zero
            # code means the scheduler refused it; zero means it is queued and
            # the job itself has not run yet, so the job is not finished.
            self._processes.pop(job.id, None)
            if returncode != 0:
                job.error = (
                    'the queue submission command failed; see web_job.log'
                )
            else:
                job.finished_at = None
            self.sync_pipeline_status(job)
            return
        if (directory / JOB_SUCCESS_FILENAME).exists():
            job.returncode = 0
        elif (directory / JOB_ABORTED_FILENAME).exists():
            # 2 is what pipeline_control_relion_exit() uses for "aborted".
            if job.returncode in (0, None):
                job.returncode = 2
        elif (directory / JOB_FAILURE_FILENAME).exists() and job.returncode == 0:
            job.error = 'job wrote a failure sentinel'
        self._processes.pop(job.id, None)
        # Leaving the abort request behind would make a later run in the same
        # directory stop immediately, so remove it once nothing is running.
        (directory / ABORT_JOB_NOW_FILENAME).unlink(missing_ok=True)

        # Tell the official GUI the job finished, and light up its output nodes.
        self.sync_pipeline_status(job)
        if job.status == 'succeeded':
            self._create_output_node_links(job)

    # --------------------------------------------------------- registration
    def register_in_pipeline(
        self,
        job: Job,
        arguments: Dict[str, object],
        output_directory: Path,
        project_directory: Path,
    ) -> None:
        """Make the job visible to the official GUI.

        Three things must agree with what the GUI expects: the job directory
        name carries the process type, ``default_pipeline.star`` lists the
        process with a status label, and the job directory holds ``job.star``
        so the GUI can open it and show its parameters.
        """
        project = Path(project_directory)
        spec = get_spec(job.job_type)

        pipeline = pipeline_module.read_pipeline(project)
        reserved = job.process_name is not None
        if reserved:
            # The queue reserved this process as Scheduled when it enqueued the
            # job, together with the output nodes a later job may already name
            # as its input. Promoting fills in the real status and input edges
            # rather than adding a second process under a new number, which
            # would orphan those inputs.
            process_name = job.process_name
        else:
            process_name = process_name_for(spec, pipeline.next_job_number())

        # The job must live at <Type>/job<NNN>, which the GUI derives the type
        # from. Relocate if the caller asked for a different directory.
        expected_directory = project / process_name
        if Path(output_directory).resolve() != expected_directory.resolve():
            self._relocate_job_directory(Path(output_directory), expected_directory)
            job.output_directory = expected_directory
            if job.log_path is not None:
                job.log_path = expected_directory / job.log_path.name
            job.command = [
                part.replace(str(output_directory), str(expected_directory))
                for part in job.command
            ]

        output_nodes = [
            pipeline_module.Node(name, label)
            for name, label in output_node_names(spec, process_name)
        ]
        input_node_objs = self._input_nodes(arguments, spec, project, pipeline)

        process_obj = pipeline_module.Process(
            name=process_name,
            alias='None',
            type_label=spec.process_type_label,
            status=pipeline_module.STATUS_RUNNING,
        )
        if reserved:
            # promote_process, not register_process: a reserved job is already in
            # the pipeline as Scheduled, and registering it again would raise
            # (duplicate process) or leave its reserved output nodes unattached.
            # Output nodes are left as reserved, since their names are identical.
            pipeline_module.promote_process(
                pipeline,
                process_obj,
                input_nodes=input_node_objs,
                output_nodes=None,
            )
        else:
            # An ordinary submit allocates a fresh number, so a name collision
            # means the pipeline changed underneath us. register_process raises
            # rather than overwriting another job's row, which is the behaviour
            # a direct submission had before the queue existed.
            pipeline_module.register_process(
                pipeline,
                process_obj,
                input_nodes=input_node_objs,
                output_nodes=output_nodes,
            )
        pipeline_module.write_pipeline(project, pipeline)

        # The GUI reads these to open the job and to re-check completion.
        options = job_artifacts.remap_job_options(arguments, {})
        options = self._gui_options(job.job_type, options)
        # A variant pins its branch through the option that branch tests, so
        # job.star must carry it: without it the GUI would open the base job
        # window on whichever mode its defaults select, not on the mode that ran.
        spec_static = job_commands.COMMAND_SPECS[job.job_type].static
        for key, value in spec_static.items():
            options[key] = value
        options.setdefault('output_directory', str(expected_directory))
        job_artifacts.write_job_star(
            expected_directory, spec.process_type_label, options
        )
        job_artifacts.write_output_nodes(
            expected_directory, output_node_names(spec, process_name)
        )
        job_artifacts.write_notes(
            expected_directory, spec.process_type_label, job.command
        )
        # RELION keeps a single-job pipeline inside every job directory.
        pipeline_module.write_mini_pipeline(
            expected_directory,
            process_obj,
            input_nodes=input_node_objs,
            output_nodes=output_nodes,
        )

        job.project_directory = project
        job.process_name = process_name
        job.registered_status = pipeline_module.STATUS_RUNNING
        job.options = options

    @staticmethod
    def _gui_options(job_type: str, arguments: Dict[str, object]) -> Dict[str, object]:
        """Fill in every option the GUI expects, from its own defaults.

        ``job.star`` is what the GUI loads into the job window, so it needs the
        full option set, not only the ones the web form exposed. Missing options
        become the C++ defaults, which keeps the window consistent and means a
        value the web UI did not set is not silently blank.

        A variant is one branch of a multi-mode job, so its ``job.star`` has to
        carry the *base* job's whole option table: the desktop GUI opens the base
        job window and reads the branch from the switch the variant pinned. The
        variant's own switch is written explicitly (it lives in the spec's
        ``static`` table), so the window opens on the branch that actually ran.

        The implementation lives in ``queue_system`` because a job queued ahead
        of time also needs a ``job.star``, and it must be the same one. This
        delegates rather than duplicating, so the two cannot drift.
        """
        from .queue_system import _gui_options as build

        return build(job_type, arguments)

    def _input_nodes(
        self,
        arguments: Dict[str, object],
        spec,
        project: Path,
        pipeline: pipeline_module.Pipeline,
    ):
        """Resolve the job's input option to a pipeline node.

        The node is named the way the GUI names it: project-relative when the
        file is inside the project, absolute otherwise. An existing pipeline
        node is reused rather than duplicated, so edges point at the same data
        file the earlier job produced.

        A variant may accept either of two inputs (a particles or a micrographs
        STAR file), so its declared candidates are tried in order and the first
        one set wins, mirroring the C++'s if/else-if chain.
        """
        variables = spec.input_candidates or (
            (spec.input_variable,) if spec.input_variable else ()
        )
        variable = next(
            (name for name in variables if _present(arguments.get(name))),
            None,
        )
        if variable is None:
            return []
        value = arguments.get(variable)
        if not value:
            return []
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = project / candidate
        try:
            name = str(candidate.resolve().relative_to(project.resolve()))
        except ValueError:
            name = str(candidate.resolve())

        existing = pipeline.find_node(name)
        if existing is not None:
            return [existing]
        label = spec.input_node_label or ''
        return [pipeline_module.Node(name, label)]

    def _relocate_job_directory(self, source: Path, destination: Path) -> None:
        """Move a job directory to where the GUI expects to find it."""
        if not source.exists():
            destination.mkdir(parents=True, exist_ok=True)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError(
                f'job directory {destination} already exists; refusing to overwrite'
            )
        shutil.move(str(source), str(destination))

    def sync_pipeline_status(self, job: Job) -> None:
        """Write the job's current status back into the project pipeline.

        Both the project pipeline and the job's own mini-pipeline are updated:
        the GUI reads the former for its job list and the latter when a job
        directory is imported or exported.
        """
        if job.project_directory is None or job.process_name is None:
            return
        status = job.pipeline_status
        if status is None or status == job.registered_status:
            return
        try:
            pipeline = pipeline_module.read_pipeline(job.project_directory)
            if not pipeline_module.update_process_status(
                pipeline, job.process_name, status
            ):
                return
            pipeline_module.write_pipeline(job.project_directory, pipeline)
            job.registered_status = status

            process = pipeline.find_process(job.process_name)
            if process is not None:
                input_nodes = [
                    pipeline.find_node(source)
                    for source, target in pipeline.input_edges
                    if target == job.process_name
                ]
                output_nodes = [
                    pipeline.find_node(target)
                    for source, target in pipeline.output_edges
                    if source == job.process_name
                ]
                pipeline_module.write_mini_pipeline(
                    job.output_directory,
                    process,
                    input_nodes=[n for n in input_nodes if n is not None],
                    output_nodes=[n for n in output_nodes if n is not None],
                )
        except Exception:
            # A pipeline write must never take down a finished job; the GUI
            # will simply show the job as still running until the next sync.
            return

    def _create_output_node_links(self, job: Job) -> None:
        """Create ``.Nodes`` symlinks so the GUI can consume this job's output."""
        if job.project_directory is None or job.process_name is None:
            return
        try:
            spec = get_spec(job.job_type)
            for name, label in output_node_names(spec, job.process_name):
                job_artifacts.create_node_symlink(
                    job.project_directory, name, label, alias='None'
                )
        except Exception:
            return

    # ------------------------------------------------------------ cancelling
    async def cancel(self, job_id: str, timeout: float = 10.0) -> bool:
        """Ask a job to stop, using the same file-based handshake as RELION."""
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        process = self._processes.get(job_id)
        if process is None:
            return False

        job.cancel_requested = True
        (job.output_directory / ABORT_JOB_NOW_FILENAME).touch()
        self.sync_pipeline_status(job)
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            # The job ignored the cooperative signal; kill the whole group.
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            return True

    # ---------------------------------------------------------------- state
    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def forget(self, job_id: str) -> bool:
        """Drop a finished job from the in-memory list.

        This only affects the web UI's view: the job's directory, notes and
        pipeline entry are untouched, so DELETING a job from a RELION project
        stays a separate, deliberate action.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status == 'running':
            return False
        self._jobs.pop(job_id, None)
        return True

    def list(self) -> List[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)

    def tail_log(self, job_id: str, max_bytes: int = MAX_LOG_BYTES) -> str:
        job = self._jobs.get(job_id)
        if job is None or job.log_path is None or not job.log_path.exists():
            return ''
        size = job.log_path.stat().st_size
        with open(job.log_path, 'rb') as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
        return data.decode('utf-8', errors='replace')

    def read_log_chunk(
        self, job_id: str, offset: int, max_bytes: int = MAX_LOG_BYTES
    ) -> Tuple[str, int, bool]:
        """Read new log bytes starting at ``offset``.

        Returns ``(text, next_offset, truncated)``. ``text`` holds only bytes
        written since ``offset``; ``next_offset`` is the position to pass on the
        next call. ``truncated`` is True when the log grew past ``max_bytes``
        and older content was skipped, so a client can reset its buffer.

        Reading from an offset is what makes the log tail live: the browser
        polls this instead of refetching the whole file on every update.
        """
        job = self._jobs.get(job_id)
        if job is None or job.log_path is None or not job.log_path.exists():
            return '', offset, False

        size = job.log_path.stat().st_size
        truncated = False
        start = offset
        if start > size:
            # The file was replaced or truncated underneath us.
            start = 0
            truncated = True
        if size - start > max_bytes:
            start = size - max_bytes
            truncated = True

        if size <= start:
            return '', start, truncated

        with open(job.log_path, 'rb') as handle:
            handle.seek(start)
            data = handle.read()
        return data.decode('utf-8', errors='replace'), start + len(data), truncated

    def log_size(self, job_id: str) -> int:
        job = self._jobs.get(job_id)
        if job is None or job.log_path is None or not job.log_path.exists():
            return 0
        return job.log_path.stat().st_size

    async def shutdown(self) -> None:
        """Terminate every running job; called when the server stops."""
        for job_id in list(self._processes):
            try:
                await self.cancel(job_id, timeout=3.0)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()


def sentinel_status(directory: Path) -> Optional[str]:
    """The status RELION's sentinel files imply, or None if none is present.

    The sentinels are the job's own record of how it ended, and they outlive the
    agent - which is what makes them the authority when reconciling after a
    restart. This is the one place that reads them, so a change to the protocol
    is a change to one function rather than to every caller that guesses.

    Success wins over failure: RELION removes the success file before a re-run,
    but a stale failure file from an earlier attempt can survive, and a job that
    wrote success did finish.
    """
    try:
        if (Path(directory) / JOB_SUCCESS_FILENAME).exists():
            return statuses.SUCCEEDED
        if (Path(directory) / JOB_ABORTED_FILENAME).exists():
            return statuses.ABORTED
        if (Path(directory) / JOB_FAILURE_FILENAME).exists():
            return statuses.FAILED
    except OSError:
        return None
    return None


def _present(value) -> bool:
    """A value the job's option set actually carries."""
    return value is not None and str(value).strip() != ''
