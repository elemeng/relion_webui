# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""A small job queue for a handful of shared-storage GPU nodes.

The alternative to this module is RELION's submission-script template
(``qsubscript`` + ``qsub``), which exists to hand a job to a *cluster scheduler*
- Slurm, SGE, Torque - that already knows about nodes, queues and GPU requests.
On a lab network of a few identical GPU machines there is no such scheduler, and
writing a template that pretends there is one only moves the problem: the script
still cannot see the other nodes, cannot wait for an upstream job, and cannot
tell that ``gpu1`` is busy.

So this is deliberately not a Slurm replacement. It is the smallest thing that
answers four questions the web UI actually asks:

* **Which machine?** ``host`` names a node from ``hosts.py``; ``None`` is ``local``.
* **Which card on it?** ``gpus`` is a list of device indices, leased exclusively.
  A GPU is never handed to two jobs: RELION's own help says two MotionCor2
  processes sharing a card produces broken output, and the same is true of the
  other GPU programs.
* **How many at once?** A node's capacity is its CPU count, and a job reserves
  ``nr_mpi * nr_threads`` cores. Two small jobs fit on a big node; one big job
  takes it. ``exclusive`` is the blunt version: while it runs, nothing else
  starts there.
* **After what?** Dependencies are derived, not declared. A job's inputs point
  at STAR files, and RELION's ``default_pipeline.star`` already records which
  process produced each node - so a queued downstream job waits for the upstream
  job automatically. Explicit ``depends_on``/``after_jobs``/``after_files``
  cover what the pipeline cannot express.

Three design choices worth knowing:

**Dependencies are re-read every tick, not fixed at enqueue.** A downstream job
is usually queued *before* its input exists, so at enqueue time the producer is
often not in the pipeline yet. Reading again once the producer has registered
is what makes "queue the whole pipeline, then walk away" work.

**The queue is the only submitter.** Once an entry is admitted it calls the same
``JobManager.submit`` the HTTP API calls, so a queued job is registered, logged
and abortable exactly like a directly submitted one. Nothing about RELION's
sentinel protocol changes.

**State is persisted** next to ``hosts.json``. A restart keeps the pending
entries. Entries that were running are *adopted* rather than resubmitted: their
job directory and pipeline row still exist, so their status is read back from
the pipeline instead of being guessed, and resubmitting - which would duplicate
a finished result - never happens.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import hosts as hosts_module
from . import cli_options
from . import job_artifacts
from . import job_commands, job_specs
from . import node_metrics
from . import jobs as jobs_module
from . import pipeline as pipeline_module
from . import statuses
from .generated_schema import JOBS as SCHEMA_JOBS


# How often the queue looks at its entries and the nodes.
TICK_SECONDS = 2.0
# A node snapshot costs an ssh round trip, so it is reused for this long. Long
# enough to keep the queue cheap, short enough that a job finishing elsewhere
# frees its GPUs quickly.
CAPACITY_TTL = 12.0

# How long a dispatched job may show neither a live process nor an exit file
# before it counts as lost. Long enough to cover a slow start (an MPI job may
# take a while to fork its ranks), short enough that a node reboot does not
# retire a card for the afternoon.
LOST_AFTER_SECONDS = 120.0

# History retention. A finished entry is small (~1 kB) but `GET /api/queue`
# returns all of them on every poll, so the list is capped in count as well as
# age: a long-lived service would otherwise ship a growing payload forever.
KEEP_FINISHED = 100
KEEP_FINISHED_SECONDS = 7 * 24 * 3600.0

# Queue states are the canonical ones (see `statuses`), aliased here so the rest
# of this module reads naturally. The queue has no use for QUEUED - that is a job
# handed to an external scheduler, which this queue does not do.
PENDING = statuses.PENDING
RUNNING = statuses.RUNNING
SUCCEEDED = statuses.SUCCEEDED
FAILED = statuses.FAILED
CANCELLED = statuses.CANCELLED

STATES = (PENDING, RUNNING, SUCCEEDED, FAILED, CANCELLED)
TERMINAL_STATES = tuple(state for state in STATES if state in statuses.TERMINAL)

# Jobs are only started through the queue, so a job that also asks for qsub
# would be handed to a scheduler this queue knows nothing about. Enqueue refuses
# it rather than silently running it locally with the template ignored.
_QUEUE_OPTIONS = ('do_queue',)


class QueueError(RuntimeError):
    """The entry cannot be queued, or the queue cannot act on it."""


@dataclass
class QueueEntry:
    """One job waiting to run, or one that has run."""

    id: str
    project: str
    job_type: str
    arguments: Dict[str, object]
    host: Optional[str] = None
    # Device indices this entry may use. Empty means "decide at dispatch".
    gpus: List[int] = field(default_factory=list)
    # Cores to reserve; None means derive from nr_mpi * nr_threads.
    cores: Optional[int] = None
    # GPU memory this job needs on each card it holds, in MB. Optional, and
    # deliberately so: RELION's job options say nothing about memory, and box
    # size, particle count and tomogram extent all drive it, so any figure the
    # queue derived would be a guess. When declared, admission checks the card
    # can hold it; when not, the queue stays silent rather than pretending.
    gpu_memory_mb: Optional[int] = None
    priority: int = 0
    # While this runs, no other entry starts on the same node.
    exclusive: bool = False
    # Entry ids this one waits for.
    depends_on: List[str] = field(default_factory=list)
    # Process names ("Tomograms/job005") that must reach Succeeded.
    after_jobs: List[str] = field(default_factory=list)
    # Paths that must exist before this can start.
    after_files: List[str] = field(default_factory=list)

    state: str = PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # Set once the job exists in RELION; this is what adoption after a restart
    # and cancellation of an adopted job both key on.
    job_id: Optional[str] = None
    # Assigned at enqueue, not at dispatch. A queued job is placed in the
    # pipeline as Scheduled, so its directory - and therefore every output path
    # a later job names as input - is known before anything runs.
    process_name: Optional[str] = None
    # ``{filename: project-relative path}`` as predicted at enqueue.
    predicted_outputs: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None
    # Devices actually held while running, persisted so a restart keeps them
    # reserved instead of handing them to the next entry.
    leased_gpus: List[int] = field(default_factory=list)
    # Dependencies the queue worked out from the pipeline, for the UI.
    derived_dependencies: List[str] = field(default_factory=list)
    # Why it has not started, refreshed every tick; empty for a running entry.
    blocked_by: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # ------------------------------------------------------------- reporting
    @property
    def host_name(self) -> str:
        return self.host or hosts_module.LOCAL_NAME

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def cores_required(self) -> int:
        if self.cores is not None:
            return max(1, int(self.cores))
        return 1

    @property
    def wait_seconds(self) -> float:
        end = self.started_at or time.time()
        return max(0.0, end - self.created_at)

    def as_dict(self) -> dict:
        return {
            'id': self.id,
            'project': self.project,
            'job_type': self.job_type,
            'arguments': self.arguments,
            'host': self.host_name,
            'gpus': list(self.gpus),
            'leased_gpus': list(self.leased_gpus),
            'cores': self.cores_required,
            'gpu_memory_mb': self.gpu_memory_mb,
            'priority': self.priority,
            'exclusive': self.exclusive,
            'depends_on': list(self.depends_on),
            'after_jobs': list(self.after_jobs),
            'after_files': list(self.after_files),
            'derived_dependencies': list(self.derived_dependencies),
            'state': self.state,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'wait_seconds': round(self.wait_seconds, 1),
            'duration': (
                round((self.finished_at or time.time()) - self.started_at, 1)
                if self.started_at
                else None
            ),
            'job_id': self.job_id,
            'process_name': self.process_name,
            'predicted_outputs': dict(self.predicted_outputs),
            'error': self.error,
            'blocked_by': list(self.blocked_by),
            'warnings': list(self.warnings),
        }

    def to_json(self) -> dict:
        payload = self.as_dict()
        payload.pop('blocked_by', None)
        return payload

    @classmethod
    def from_json(cls, raw: dict) -> 'QueueEntry':
        def _ints(value) -> List[int]:
            out: List[int] = []
            for item in value or []:
                try:
                    out.append(int(item))
                except (TypeError, ValueError):
                    continue
            return out

        return cls(
            id=str(raw['id']),
            project=str(raw['project']),
            job_type=str(raw['job_type']),
            arguments=dict(raw.get('arguments') or {}),
            host=raw.get('host') or None,
            gpus=_ints(raw.get('gpus')),
            cores=raw.get('cores'),
            gpu_memory_mb=raw.get('gpu_memory_mb'),
            priority=int(raw.get('priority') or 0),
            exclusive=bool(raw.get('exclusive')),
            depends_on=[str(x) for x in raw.get('depends_on') or []],
            after_jobs=[str(x) for x in raw.get('after_jobs') or []],
            after_files=[str(x) for x in raw.get('after_files') or []],
            state=str(raw.get('state') or PENDING),
            created_at=float(raw.get('created_at') or time.time()),
            started_at=raw.get('started_at'),
            finished_at=raw.get('finished_at'),
            job_id=raw.get('job_id'),
            process_name=raw.get('process_name'),
            predicted_outputs={
                str(key): str(value)
                for key, value in (raw.get('predicted_outputs') or {}).items()
            },
            error=raw.get('error'),
            leased_gpus=_ints(raw.get('leased_gpus')),
            derived_dependencies=[str(x) for x in raw.get('derived_dependencies') or []],
        )


@dataclass
class _Capacity:
    """What a node can take, at one moment."""

    cpus: int = 0
    gpus: Dict[int, bool] = field(default_factory=dict)  # index -> busy
    # index -> total MB, when nvidia-smi reports it. Used to decide whether a
    # job's GPU memory request fits, not to decide whether a card is free.
    total_memory_mb: Dict[int, Optional[float]] = field(default_factory=dict)
    problem: Optional[str] = None
    fetched_at: float = 0.0

    @property
    def known(self) -> bool:
        return self.problem is None

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)


class JobQueue:
    """The queue: definitions, admission, dispatch, and their state.

    It owns no RELION logic. Starting a job is ``JobManager.submit``; checking a
    job is the pipeline file or the manager; finding a producer is
    ``default_pipeline.star``. This module only decides *when* to call those.
    """

    FILENAME = 'queue.json'

    def __init__(
        self,
        manager: Any,
        guard: Any,
        config_directory=None,
        *,
        tick_seconds: float = TICK_SECONDS,
        capacity_ttl: float = CAPACITY_TTL,
        lost_after: float = LOST_AFTER_SECONDS,
        keep_finished: int = KEEP_FINISHED,
        keep_finished_seconds: float = KEEP_FINISHED_SECONDS,
    ):
        self.manager = manager
        self.guard = guard
        self.config_directory = (
            Path(config_directory) if config_directory is not None else None
        )
        self.tick_seconds = float(tick_seconds)
        self.capacity_ttl = float(capacity_ttl)
        # How long a dispatched job may show no process and no exit file before
        # it is treated as lost. Generous: a job that is starting looks the same.
        self.lost_after = float(lost_after)
        # History retention. Both limits apply; whichever frees more wins.
        self.keep_finished = max(0, int(keep_finished))
        self.keep_finished_seconds = float(keep_finished_seconds)

        self._entries: Dict[str, QueueEntry] = {}
        self._counter = 0
        self._capacity: Dict[str, _Capacity] = {}
        self._task: Any = None
        self._busy = False
        self._dirty = False

        self._load()
        # Reconcile entries that were running when the agent last stopped, so a
        # listing is accurate even before the scheduler loop starts.
        self._adopt_running()
        # And apply retention once at start, so a queue file that grew while the
        # agent was down is trimmed before anything reads it.
        self._prune()
        self._save()

    # ---------------------------------------------------------------- state
    def entries(self, project: Optional[str] = None) -> List[QueueEntry]:
        """Entries in the order they should be read: live first, then history."""
        order = {PENDING: 0, RUNNING: 1, FAILED: 2, CANCELLED: 3, SUCCEEDED: 4}
        selection = [
            entry
            for entry in self._entries.values()
            if project is None or Path(entry.project) == Path(project)
        ]
        selection.sort(
            key=lambda entry: (
                order.get(entry.state, 5),
                -entry.priority,
                entry.created_at,
            )
        )
        return selection

    def get(self, entry_id: str) -> Optional[QueueEntry]:
        return self._entries.get(entry_id)

    def snapshot(self, project: Optional[str] = None) -> dict:
        """Everything the UI needs: the entries and each node's spare capacity."""
        entries = self.entries(project)
        hosts = list(getattr(self.manager.hosts, 'list', lambda: [])())
        # A host with no cached capacity is reported as unknown rather than
        # omitted: the panel shows every node, and a blank row is a prompt to
        # look at the node card rather than a mystery.
        capacity = []
        for host in hosts:
            cached = self._capacity.get(host.name)
            used = self._used_cores(host.name)
            capacity.append(
                {
                    'host': host.name,
                    'enabled': host.enabled,
                    'cpus': cached.cpus if cached else None,
                    'used_cores': used,
                    'free_cores': (
                        max(0, cached.cpus - used) if cached and cached.cpus else None
                    ),
                    'gpus': (
                        [
                            {
                                'index': index,
                                'busy': busy
                                or self._gpu_leased(host.name, index),
                            }
                            for index, busy in sorted(cached.gpus.items())
                        ]
                        if cached
                        else None
                    ),
                    'problem': cached.problem if cached else None,
                    'queued': sum(
                        1
                        for entry in entries
                        if entry.host_name == host.name and entry.state == PENDING
                    ),
                }
            )
        return {
            'entries': [entry.as_dict() for entry in entries],
            'hosts': capacity,
            'counts': {
                state: sum(1 for entry in entries if entry.state == state)
                for state in STATES
            },
        }

    # ------------------------------------------------------------ enqueueing
    def add(
        self,
        project,
        job_type: str,
        arguments: Dict[str, object],
        *,
        host: Optional[str] = None,
        gpus: Optional[Sequence[int]] = None,
        cores: Optional[int] = None,
        gpu_memory_mb: Optional[int] = None,
        priority: int = 0,
        exclusive: bool = False,
        depends_on: Optional[Sequence[str]] = None,
        after_jobs: Optional[Sequence[str]] = None,
        after_files: Optional[Sequence[str]] = None,
        inputs: Optional[Dict[str, str]] = None,
    ) -> QueueEntry:
        """Queue a job. Validates everything that can be checked now.

        Input *existence* is deliberately not an error: the common use is to
        queue a downstream job before its input exists.

        `inputs` is the input-slot form of that: instead of a path, an option
        may name a queued entry and one of its output filenames, as
        ``{"in_tiltseries": "q0003:tomograms.star"}``. The queue resolves it to
        the producer's real path - which is known now, because a queued job is
        reserved in the pipeline with its output nodes - and records the
        dependency. That is what lets a whole chain be built in one go rather
        than waiting for each stage to exist before naming the next.
        """
        project_directory = self.guard.resolve_existing(project, kind='directory')
        if not (project_directory / pipeline_module.PIPELINE_FILENAME).is_file():
            raise QueueError(
                f'{project_directory} is not a RELION project: no '
                f'{pipeline_module.PIPELINE_FILENAME}'
            )
        try:
            schema = job_commands.form_schema(job_type)
        except KeyError as exc:
            raise QueueError(f'unknown job type {job_type!r}') from exc

        options = dict(arguments or {})
        slot_dependencies = self._resolve_slots(inputs or {}, options)
        for variable in _QUEUE_OPTIONS:
            if _truthy(options.get(variable)):
                raise QueueError(
                    f'{variable} is set: this queue replaces the qsub template. '
                    f'Unset it and let the queue pick the node.'
                )

        missing = [
            variable
            for variable in schema.get('required', [])
            if not _present(options.get(variable))
        ]
        if missing:
            raise QueueError(f'missing required options: {", ".join(missing)}')
        problems = job_commands.validate(job_type, options)
        if problems:
            raise QueueError('; '.join(problems))

        # A disabled or unknown host is refused now: a job that can never start
        # should not sit in the queue looking healthy.
        execution_host = self.manager.hosts.get(host)

        requested: List[int] = []
        for item in gpus or ():
            try:
                index = int(item)
            except (TypeError, ValueError):
                raise QueueError(f'GPU indices must be integers, got {item!r}') from None
            if index < 0:
                raise QueueError('GPU indices start at 0')
            if index not in requested:
                requested.append(index)
        requested.sort()

        known = [
            entry_id
            for entry_id in (depends_on or ())
            if entry_id not in self._entries
        ]
        if known:
            raise QueueError(f'no queue entry with id {known[0]!r}')
        self._refuse_cycle(depends_on or ())

        warnings: List[str] = []
        devices = self._requested_devices(job_type, options)
        if devices is not None and requested and set(devices) - set(requested):
            warnings.append(
                f'the options ask for GPU {sorted(set(devices) - set(requested))}; '
                f'the queue pinned {requested}. The queue will override gpu_ids.'
            )

        # Reserve the job's place in the pipeline now, so its number - and so
        # every output path - is fixed before anything runs. This is what lets a
        # later job name this one's output as its input while this is still
        # waiting, which is the whole point of queuing a chain in advance.
        #
        # It is also RELION's own model rather than a new one: the process is
        # added as Scheduled with its output nodes, exactly as
        # PipeLine::addJob(job, PROC_SCHEDULED) does. The desktop GUI shows it
        # as a scheduled job it can run, edit or delete.
        process_name, predicted = self._reserve(project_directory, job_type, options)

        self._counter += 1
        entry = QueueEntry(
            id=f'q{self._counter:04d}',
            project=str(project_directory),
            job_type=job_type,
            arguments=options,
            process_name=process_name,
            predicted_outputs=predicted,
            host=None if execution_host.is_local else execution_host.name,
            gpus=requested,
            cores=max(1, int(cores)) if cores is not None else self._derive_cores(
                job_type, options
            ),
            gpu_memory_mb=(
                max(1, int(gpu_memory_mb)) if gpu_memory_mb is not None else None
            ),
            priority=int(priority),
            exclusive=bool(exclusive),
            depends_on=list(
                dict.fromkeys([*(depends_on or ()), *slot_dependencies])
            ),
            after_jobs=list(dict.fromkeys(after_jobs or ())),
            after_files=list(dict.fromkeys(after_files or ())),
            warnings=warnings,
        )
        for path in entry.after_files:
            if not Path(path).expanduser().exists():
                entry.warnings.append(f'waiting for {path}, which does not exist yet')

        self._entries[entry.id] = entry
        self._mark_dirty()
        self._save()
        return entry

    async def cancel(self, entry_id: str) -> QueueEntry:
        """Cancel an entry, stopping its job if it has already started.

        A queued entry is simply marked cancelled - the scheduler never starts
        it - and its Scheduled process is taken out of the pipeline, otherwise
        it would sit there forever showing a job that will never run, and a
        later entry naming its output would wait for a file that never appears.
        A running one goes through RELION's own abort handshake via the job
        manager, so the program stops the same way a Stop in the UI stops it,
        and the devices it held are released immediately rather than at the next
        tick.
        """
        entry = self._require(entry_id)
        if entry.is_terminal:
            raise QueueError(f'{entry_id} already finished ({entry.state})')
        started = entry.state == RUNNING
        entry.state = CANCELLED
        entry.finished_at = time.time()
        entry.blocked_by = []
        entry.leased_gpus = []

        if entry.job_id:
            job = self.manager.get(entry.job_id)
            if job is not None and statuses.is_live(job.status) and job.status != statuses.PENDING:
                entry.error = 'cancelled while running'
                try:
                    await self.manager.cancel(entry.job_id)
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    entry.error = f'cancelled, but stopping the job failed: {exc}'
        if not started:
            # Only a never-started entry still holds a bare reservation. A
            # cancelled running job keeps its directory and its pipeline row:
            # RELION marks it Aborted, and deleting it is the user's decision.
            self._release(entry)
            entry.process_name = None
            entry.predicted_outputs = {}
        self._mark_dirty()
        self._save()
        return entry

    def retry(self, entry_id: str) -> QueueEntry:
        """Put a finished entry back at the end of the queue as a new job.

        The job keeps its process name if the reservation is still in the
        pipeline, so a downstream entry that named this job's output as its
        input does not have to be re-pointed. A new number is taken only when
        the old reservation was released.
        """
        entry = self._require(entry_id)
        if entry.state == RUNNING:
            raise QueueError(f'{entry_id} is running; cancel it first')
        entry.state = PENDING
        entry.error = None
        entry.started_at = None
        entry.finished_at = None
        entry.job_id = None
        entry.leased_gpus = []
        entry.derived_dependencies = []
        entry.blocked_by = []
        if not self._reservation_alive(entry):
            entry.process_name, entry.predicted_outputs = self._reserve(
                Path(entry.project), entry.job_type, entry.arguments
            )
        self._mark_dirty()
        self._save()
        return entry

    def _reservation_alive(self, entry: QueueEntry) -> bool:
        """Whether the entry's Scheduled process is still in the pipeline."""
        if not entry.process_name:
            return False
        try:
            pipeline = pipeline_module.read_pipeline(Path(entry.project))
        except (FileNotFoundError, ValueError):
            return False
        process = pipeline.find_process(entry.process_name)
        return process is not None and process.status == pipeline_module.STATUS_SCHEDULED

    def remove(self, entry_id: str) -> bool:
        """Forget a finished entry. A live one is cancelled first."""
        entry = self._require(entry_id)
        if entry.state == RUNNING:
            raise QueueError(f'{entry_id} is running; cancel it first')
        del self._entries[entry_id]
        self._mark_dirty()
        self._save()
        return True

    def _require(self, entry_id: str) -> QueueEntry:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise QueueError(f'no queue entry with id {entry_id!r}')
        return entry

    def _refuse_cycle(self, depends_on: Sequence[str]) -> None:
        """Refuse a new entry that would wait for itself.

        The new entry has no id yet, so it cannot already be in a chain: the
        only cycle it can create is one that reaches back to one of its own
        dependencies. Following each dependency's chain and finding another
        dependency of the new entry is therefore exactly the check.
        """
        declared = set(depends_on)
        for dependency in declared:
            seen: set = set()
            frontier = [dependency]
            while frontier:
                current = frontier.pop()
                if current in seen:
                    continue
                seen.add(current)
                if current != dependency and current in declared:
                    raise QueueError(
                        f'depends_on forms a cycle: {dependency} already waits '
                        f'(directly or indirectly) for {current}'
                    )
                entry = self._entries.get(current)
                if entry is not None:
                    frontier.extend(entry.depends_on)

    def _resolve_slots(
        self, inputs: Dict[str, str], options: Dict[str, object]
    ) -> List[str]:
        """Fill in option values that name a queued entry's output.

        Each value is ``<entry id>:<output filename>``, e.g.
        ``q0003:tomograms.star``. It resolves to the absolute path the producer
        will write, which is known because the producer was reserved in the
        pipeline with that output node at enqueue time.

        Rejecting a bad reference here rather than at dispatch matters: a
        mistyped filename would otherwise sit in the queue looking healthy and
        fail only when it started.
        """
        dependencies: List[str] = []
        for variable, reference in inputs.items():
            entry_id, _, filename = str(reference).partition(':')
            entry_id = entry_id.strip()
            filename = filename.strip()
            if not entry_id or not filename:
                raise QueueError(
                    f'inputs["{variable}"] must be "entry_id:filename", '
                    f'for example "q0003:tomograms.star"; got {reference!r}'
                )
            producer = self._entries.get(entry_id)
            if producer is None:
                raise QueueError(
                    f'inputs["{variable}"] names queue entry {entry_id!r}, '
                    'which is not in the queue'
                )
            predicted = producer.predicted_outputs.get(filename)
            if predicted is None:
                available = ', '.join(sorted(producer.predicted_outputs)) or 'none'
                raise QueueError(
                    f'{entry_id} does not write {filename!r}; it writes: {available}'
                )
            options[variable] = str(Path(producer.project) / predicted)
            if entry_id not in dependencies:
                dependencies.append(entry_id)
        return dependencies

    def _reserve(
        self, project_directory: Path, job_type: str, options: Dict[str, object]
    ) -> tuple:
        """Place the job in the pipeline as Scheduled; return its name and outputs.

        Two things are written, and both are what RELION itself does for a
        scheduled job:

        * the process row and its output nodes
          (``PipeLine::addJob(job, PROC_SCHEDULED)``, pipeliner.cpp:481);
        * the job directory and its ``job.star``, which
          ``prepareFinalCommand`` creates through ``mktree(outputname)`` before
          it builds the commands (pipeline_jobs.cpp:699).

        The second matters more than it looks. A job directory always exists
        before its row does, so the rest of this package - and the desktop GUI -
        assumes it. A reserved row with no directory makes "open this job" a 404
        and leaves the canvas pointing at nothing.

        The output paths are *derived* from the job spec rather than guessed:
        ``job_specs.OUTPUT_NODES`` is the same table used to register the nodes
        once the job finishes, so the prediction and the eventual registration
        cannot disagree.
        """
        try:
            spec = job_specs.get_spec(job_type)
        except KeyError:
            # A job type outside the schema (a test double, or a job RELION
            # added before the table was regenerated) has no known outputs.
            # Reserving it would put a process in the pipeline with no nodes,
            # which is worse than leaving it unnamed until it runs.
            return None, {}

        reservation = pipeline_module.Reservation(
            directory=spec.directory_name,
            type_label=spec.process_type_label,
            outputs=[
                (node.filename, node.type_label) for node in spec.output_nodes
            ],
        )
        try:
            assigned = pipeline_module.reserve_scheduled(
                project_directory, [reservation]
            )
        except (OSError, ValueError, pipeline_module.PipelineLockedError) as exc:
            raise QueueError(
                f'could not reserve a job number in {project_directory}: {exc}'
            ) from exc
        process_name = assigned[0]
        predicted = {
            node.filename: f'{process_name}/{node.filename}'
            for node in spec.output_nodes
        }
        self._write_reserved_directory(
            project_directory, process_name, job_type, spec, options
        )
        return process_name, predicted

    def _write_reserved_directory(
        self,
        project_directory: Path,
        process_name: str,
        job_type: str,
        spec,
        options: Dict[str, object],
    ) -> None:
        """Create the job directory and ``job.star`` for a reserved job.

        Written with the same helpers a submitted job uses, so the GUI opens a
        queued job exactly as it opens a finished one: the full option table
        from the schema, the variant's branch switch, and the output-node list.

        The option table is built here rather than by asking the job manager, so
        reserving needs no manager at all: a reservation is a pipeline write,
        not an execution. The compat harness and the queue's own tests both
        enqueue against a stub.

        Only an I/O failure is tolerated. Swallowing a programming error here
        once hid a missing helper and produced a reserved job with no
        ``job.star`` - exactly the kind of silent gap this is meant to prevent.
        """
        directory = Path(project_directory) / process_name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            merged, _unknown = cli_options.apply(job_type, dict(options))
            job_artifacts.write_job_star(
                directory, spec.process_type_label, _gui_options(job_type, merged)
            )
            outputs = job_specs.output_node_names(spec, process_name)
            job_artifacts.write_output_nodes(directory, outputs)
            # The .Nodes entry is what makes the output *findable as an input*.
            # RELION touches these for a scheduled job even though the file does
            # not exist yet (`touch_if_not_exist = status == PROC_SCHEDULED`,
            # pipeliner.cpp:120), and its own input picker is just a listing of
            # `.Nodes/<coarse type>/` (gui_entries.cpp:347). Without it a queued
            # job's output is invisible to that picker - and to ours.
            for node_name, node_label in outputs:
                job_artifacts.create_node_entry(
                    project_directory,
                    node_name,
                    node_label,
                    even_if_missing=True,
                )
        except OSError:
            # A read-only or full filesystem. The reservation still stands and
            # the job still runs, so this must not fail the enqueue.
            return

    def _release(self, entry: QueueEntry) -> None:
        """Take a cancelled entry's Scheduled process out of the pipeline.

        Leaving it there would show a job that will never run, and would keep
        its output nodes reserved - so a later job naming them as input would
        wait for something that is never going to appear.
        """
        if not entry.process_name:
            return
        try:
            pipeline = pipeline_module.read_pipeline(Path(entry.project))
        except (FileNotFoundError, ValueError):
            return
        process = pipeline.find_process(entry.process_name)
        if process is None or process.status != pipeline_module.STATUS_SCHEDULED:
            # A running or finished job is not the queue's to remove: its
            # directory may hold real output.
            return

        doomed = {
            node
            for producer, node in pipeline.output_edges
            if producer == entry.process_name
        }
        still_used = {
            node
            for node, consumer in pipeline.input_edges
            if consumer != entry.process_name
        }
        doomed -= still_used
        # Read the labels before the nodes are dropped from the pipeline: the
        # removal below filters them out, and `_node_label` would then return an
        # empty string and the `.Nodes` entry would survive.
        labels = {node: _node_label(pipeline, node) for node in doomed}
        pipeline.processes = [
            item for item in pipeline.processes if item.name != entry.process_name
        ]
        pipeline.nodes = [node for node in pipeline.nodes if node.name not in doomed]
        pipeline.input_edges = [
            (node, consumer)
            for node, consumer in pipeline.input_edges
            if consumer != entry.process_name
        ]
        pipeline.output_edges = [
            (producer, node)
            for producer, node in pipeline.output_edges
            if producer != entry.process_name
        ]
        try:
            pipeline_module.write_pipeline(Path(entry.project), pipeline)
        except (OSError, ValueError, pipeline_module.PipelineLockedError):
            return
        for node, label in labels.items():
            if label:
                job_artifacts.remove_node_entry(Path(entry.project), node, label)

    # ------------------------------------------------------------ scheduling
    async def start(self) -> None:
        """Begin the scheduler loop. Idempotent."""
        import asyncio

        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop. Queued entries stay queued for the next start."""
        import asyncio

        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._save()

    async def _loop(self) -> None:
        import asyncio

        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a scheduler must not die
                pass
            await asyncio.sleep(self.tick_seconds)

    async def tick(self) -> None:
        """One scheduling pass. Exposed so tests can drive it deterministically."""
        if self._busy:
            return
        self._busy = True
        try:
            await self._collect_finished()
            await self._start_ready()
        finally:
            self._busy = False
            if self._dirty:
                self._save()

    async def _collect_finished(self) -> None:
        for entry in list(self._entries.values()):
            if entry.state != RUNNING:
                continue
            status = self._job_state(entry)
            if status is None:
                # No manager record, no sentinel and no pipeline row. That means
                # the job was never registered after all; leaving it running
                # would hold its devices for good.
                self._fail(entry, 'the job left no trace: no exit file and no '
                                  'pipeline row, so it never started')
                continue
            if not statuses.is_terminal(status):
                self._check_lost(entry)
                continue
            if status == statuses.SUCCEEDED:
                entry.state = SUCCEEDED
            else:
                entry.state = FAILED
                entry.error = entry.error or self._failure_reason(entry)
            entry.finished_at = time.time()
            entry.leased_gpus = []
            entry.blocked_by = []
            self._mark_dirty()

    def _check_lost(self, entry: QueueEntry) -> None:
        """Fail an entry whose process vanished without writing an exit file.

        A RELION program always writes one of the three sentinels, so a job that
        is gone with no sentinel did not exit normally - it was killed, the node
        rebooted, or the agent's own shutdown took it. Without this the entry
        stays "running" and its devices stay leased, which is the leak this
        closes.

        The grace period matters: right after dispatch the manager may not have
        the process yet, and a job that is starting looks identical to one that
        died. `lost_after` is generous enough to cover a slow start.
        """
        if entry.job_id and self.manager.get(entry.job_id) is not None:
            return
        started = entry.started_at or entry.created_at
        if time.time() - started < self.lost_after:
            return
        self._fail(
            entry,
            'the process is gone and wrote no exit file (killed, or the node '
            'went down); its devices were freed',
        )

    def _fail(self, entry: QueueEntry, reason: str) -> None:
        entry.state = FAILED
        entry.error = entry.error or reason
        entry.finished_at = time.time()
        entry.leased_gpus = []
        entry.blocked_by = []
        self._mark_dirty()

    async def _start_ready(self) -> None:
        pending = [entry for entry in self._entries.values() if entry.state == PENDING]
        # Refresh capacity for hosts with work in flight, not only for hosts
        # with work waiting: the panel shows what else is on a card, and a card
        # that was busy when the job started may have been freed since.
        watched = {
            entry.host_name
            for entry in self._entries.values()
            if entry.state in (PENDING, RUNNING)
        }
        await self._refresh_capacities(sorted(watched))
        if not pending:
            return
        pending.sort(key=lambda entry: (-entry.priority, entry.created_at))

        for entry in pending:
            if entry.state != PENDING:
                continue
            reasons = self._blocking_reasons(entry)
            entry.blocked_by = reasons
            if reasons:
                continue
            await self._dispatch(entry)

    async def _dispatch(self, entry: QueueEntry) -> None:
        """Admit an entry, allocate its devices and hand it to the manager."""
        devices = self._allocate_devices(entry)
        if devices is _NO_DEVICES:
            entry.blocked_by = ['no free GPU on this node']
            self._mark_dirty()
            return

        # The reservation can disappear between enqueue and dispatch: someone
        # deletes the scheduled job in the desktop GUI, or removes the project
        # directory. Submitting under a name that is gone would register a job
        # at a number nothing points at, so the dependents that named its output
        # would wait forever. Re-reserve first, and rewrite this entry's inputs
        # that refer to *its own* outputs if the number changed.
        if not self._reservation_alive(entry):
            before = entry.process_name
            entry.process_name, entry.predicted_outputs = self._reserve(
                Path(entry.project), entry.job_type, entry.arguments
            )
            if entry.process_name and entry.process_name != before:
                entry.warnings.append(
                    f'its reserved slot {before or "(none)"} was gone, so it was '
                    f're-numbered as {entry.process_name}; later jobs naming '
                    f'{before} now wait for that missing job'
                )

        entry.leased_gpus = list(devices)
        entry.blocked_by = []

        arguments = dict(entry.arguments)
        if devices:
            self._apply_devices(entry.job_type, arguments, list(devices))

        try:
            job = await self.manager.submit(
                entry.job_type,
                arguments,
                Path(entry.project),
                project_directory=Path(entry.project),
                host=entry.host,
                # The number was fixed at enqueue; handing it over is what makes
                # the predicted paths authoritative.
                process_name=entry.process_name,
            )
        except Exception as exc:  # noqa: BLE001 - reported on the entry
            entry.state = FAILED
            entry.started_at = entry.started_at or time.time()
            entry.finished_at = time.time()
            entry.leased_gpus = []
            entry.error = f'could not submit: {exc}'
            self._mark_dirty()
            return

        entry.state = RUNNING
        entry.started_at = time.time()
        entry.job_id = job.id
        entry.process_name = job.process_name
        entry.error = job.error
        if job.error:
            entry.state = FAILED
            entry.finished_at = time.time()
            entry.leased_gpus = []
        self._mark_dirty()

    # ------------------------------------------------------------- admission
    def _blocking_reasons(self, entry: QueueEntry) -> List[str]:
        reasons: List[str] = []
        # Process names already reported through an entry dependency, so the
        # same wait is not printed twice ("waiting for q0002" and "waiting for
        # Tomograms/job002 (scheduled)" are one fact).
        covered: set = set()

        # 1. Entry dependencies.
        for other_id in entry.depends_on:
            other = self._entries.get(other_id)
            if other is None:
                reasons.append(f'{other_id} is no longer queued')
                continue
            if other.state == RUNNING:
                reasons.append(f'waiting for {other_id} ({other.process_name or "running"})')
            elif other.state == PENDING:
                reasons.append(f'waiting for {other_id}')
                if other.process_name:
                    covered.add(other.process_name)
            elif other.state in (FAILED, CANCELLED):
                reasons.append(f'{other_id} {other.state}')
            if other.process_name:
                covered.add(other.process_name)

        # 2. Upstream RELION jobs, both declared and discovered.
        upstream = list(entry.after_jobs)
        derived = self._derive_dependencies(entry)
        entry.derived_dependencies = derived
        for name in derived:
            if name not in upstream:
                upstream.append(name)
        for name in upstream:
            if name in covered:
                continue
            status = self._process_status(entry.project, name)
            if status is None:
                reasons.append(f'{name} is not in the pipeline yet')
            elif status == pipeline_module.STATUS_SUCCEEDED:
                continue
            elif status in (pipeline_module.STATUS_FAILED, pipeline_module.STATUS_ABORTED):
                reasons.append(f'upstream {name} {status.lower()}')
            else:
                reasons.append(f'waiting for {name} ({status.lower()})')

        # 2b. Nothing else is needed for a job whose input names a queued job's
        # output: `_reserve` put that output in the pipeline as a Scheduled
        # node, so the pass above already derived the dependency and
        # `_missing_inputs` below already knows not to call the file missing.
        # The pipeline *is* the virtual file list.

        # 3. Files that must exist, and the job's own required inputs.
        for path in entry.after_files:
            if not Path(path).expanduser().exists():
                reasons.append(f'waiting for {path}')
        for variable, path in self._missing_inputs(entry, upstream).items():
            reasons.append(f'waiting for {variable}={path}')

        # 4. Node capacity. Only reported when the entry is otherwise ready:
        # "queued behind a dependency" is the answer that matters, and adding
        # "no free GPU" to it would describe a node the job cannot start on yet
        # anyway. Keeping them apart is what makes the reason actionable.
        if reasons:
            return reasons

        host_name = entry.host_name
        capacity = self._capacity.get(host_name)
        if capacity is None:
            return [f'{host_name}: capacity not read yet']
        if not capacity.known:
            return [f'{host_name}: {capacity.problem}']

        if entry.exclusive and self._host_occupants(host_name):
            return [f'{host_name} is busy; this job wants it to itself']
        holder = self._exclusive_holder(host_name)
        if holder is not None:
            return [f'{host_name} is reserved by {holder.id}']

        cpus = capacity.cpus or 1
        used = self._used_cores(host_name)
        if used + entry.cores_required > cpus:
            return [
                f'needs {entry.cores_required} cores; '
                f'{max(0, cpus - used)} free on {host_name}'
            ]

        if self._allocate_devices(entry) is _NO_DEVICES:
            too_small = self._too_small_for(entry)
            if too_small:
                return [too_small]
            wanted = entry.gpus or self._requested_devices(
                entry.job_type, entry.arguments
            )
            if wanted:
                return [f'GPU {list(wanted)} is in use on {host_name}']
            if self._needs_gpu(entry.job_type, entry.arguments):
                return [f'no free GPU on {host_name}']
            return [f'{host_name} reports no GPUs to lease']

        return []

    def _too_small_for(self, entry: QueueEntry) -> Optional[str]:
        """Why every free card is too small for this job, if that is the reason.

        Only consulted when the entry declared a memory need, and only to
        *explain* a refusal `_card_fits` already made. Reporting it separately
        from "in use" matters because the fix differs: wait for a big card,
        rather than wait at all.
        """
        if not entry.gpu_memory_mb:
            return None
        capacity = self._capacity.get(entry.host_name)
        if capacity is None or not capacity.known:
            return None
        free = [
            index
            for index, busy in capacity.gpus.items()
            if not busy and not self._gpu_leased(entry.host_name, index)
        ]
        if not free:
            return None
        if any(
            self._card_fits(entry.host_name, index, entry.gpu_memory_mb)
            for index in free
        ):
            return None
        largest = max(
            (
                capacity.total_memory_mb.get(index) or 0
                for index in free
            ),
            default=0,
        )
        return (
            f'needs {entry.gpu_memory_mb} MB of GPU memory; the free cards on '
            f'{entry.host_name} hold at most {int(largest)} MB'
        )

    def _allocate_devices(self, entry: QueueEntry):
        """The devices this entry may hold now: a list, or ``_NO_DEVICES``.

        Pinned entries must get exactly their list. Everything else is resolved
        from the job's own options: an explicit ``gpu_ids``/``gpu_id`` is a
        statement of intent, so it must be free; a GPU job with nothing stated
        takes the lowest free card.
        """
        capacity = self._capacity.get(entry.host_name)
        if capacity is None or not capacity.known:
            return _NO_DEVICES

        if entry.gpus:
            if all(self._gpu_free(entry.host_name, index) for index in entry.gpus):
                return list(entry.gpus)
            return _NO_DEVICES

        wanted = self._requested_devices(entry.job_type, entry.arguments)
        if wanted:
            if all(self._gpu_free(entry.host_name, index) for index in wanted):
                return sorted(wanted)
            return _NO_DEVICES

        if not self._needs_gpu(entry.job_type, entry.arguments):
            return []

        free = [
            index
            for index, busy in sorted(capacity.gpus.items())
            if not busy and not self._gpu_leased(entry.host_name, index)
        ]
        # A declared memory need filters the candidates here rather than after:
        # handing out a card that cannot hold the job would take the "allocated"
        # path, and RELION would start it and die with an out-of-memory error
        # mid-run. An undeclared need is not filtered - there is nothing to
        # filter on, and refusing would reject work RELION itself accepts.
        if entry.gpu_memory_mb:
            free = [
                index
                for index in free
                if self._card_fits(entry.host_name, index, entry.gpu_memory_mb)
            ]
        if not free:
            return _NO_DEVICES
        return [free[0]]

    def _card_fits(self, host_name: str, index: int, needed_mb: int) -> bool:
        """Whether a card can hold ``needed_mb``, or is unknown and assumed able.

        Unknown counts as "fits" on purpose: a node that reports no card size
        should not block every GPU job, and RELION will report the real problem
        if the job genuinely does not fit.
        """
        capacity = self._capacity.get(host_name)
        if capacity is None or not capacity.total_memory_mb:
            return True
        total = capacity.total_memory_mb.get(index)
        if total is None:
            return True
        return total >= needed_mb

    def _gpu_free(self, host_name: str, index: int) -> bool:
        capacity = self._capacity.get(host_name)
        if capacity is None or not capacity.known:
            return False
        if index not in capacity.gpus:
            return False
        if capacity.gpus[index]:
            return False
        return not self._gpu_leased(host_name, index)

    def _gpu_leased(self, host_name: str, index: int) -> bool:
        return any(
            entry.state == RUNNING
            and entry.host_name == host_name
            and index in entry.leased_gpus
            for entry in self._entries.values()
        )

    def _host_occupants(self, host_name: str) -> List[QueueEntry]:
        return [
            entry
            for entry in self._entries.values()
            if entry.state == RUNNING and entry.host_name == host_name
        ]

    def _exclusive_holder(self, host_name: str) -> Optional[QueueEntry]:
        for entry in self._host_occupants(host_name):
            if entry.exclusive:
                return entry
        return None

    def _used_cores(self, host_name: str) -> int:
        return sum(
            entry.cores_required for entry in self._host_occupants(host_name)
        )

    async def warm_capacity(self) -> None:
        """Read capacity for every enabled node, so a listing can show it.

        The scheduler only reads the nodes it has work for; a queue panel with
        nothing queued would otherwise show no free cores anywhere. The TTL is
        shared with the scheduler, so this is at most one extra round trip per
        node per cache window rather than one per request.
        """
        hosts = list(getattr(self.manager.hosts, 'list', lambda: [])())
        await self._refresh_capacities([host.name for host in hosts if host.enabled])

    async def _refresh_capacity(self, host_name: str) -> None:
        """Refresh one host's capacity, honouring the cache."""
        await self._refresh_capacities([host_name])

    async def _refresh_capacities(self, host_names: Sequence[str]) -> None:
        """Refresh several hosts' capacity in one concurrent round.

        Concurrently because that is the difference between a usable panel and
        an unusable one: `node_metrics.collect_many` runs one thread per node, so
        three slow nodes cost one timeout rather than three. Awaiting them in
        sequence made `GET /api/queue` take 8 s with three answering nodes, on a
        poll that fires every 3 s - the next request queued behind the last.

        The cache is applied here, per host, so a caller passing a list gets the
        same TTL behaviour a single refresh had.
        """
        import anyio

        now = time.monotonic()
        due = []
        for host_name in host_names:
            cached = self._capacity.get(host_name)
            if cached is not None and now - cached.fetched_at < self.capacity_ttl:
                continue
            if host_name not in due:
                due.append(host_name)

        hosts = []
        for host_name in due:
            try:
                hosts.append(self.manager.hosts.get(host_name))
            except Exception as exc:  # noqa: BLE001 - disabled or removed
                self._capacity[host_name] = _Capacity(problem=str(exc), fetched_at=now)
        if not hosts:
            return

        # Blocking ssh, so it goes off the event loop: one slow node must not
        # stall the API or every other node's scheduling decision.
        snapshots = await anyio.to_thread.run_sync(node_metrics.collect_many, hosts)
        for snapshot in snapshots:
            capacity = _Capacity(
                cpus=int(snapshot.cpus or 0),
                problem=snapshot.problem,
                fetched_at=now,
            )
            if snapshot.problem is None:
                # Busyness is a *compute tenant*, not memory in use: a desktop
                # session keeps a few hundred MB on card 0 forever and is not a
                # job the queue must wait for. When nvidia-smi reports no compute
                # apps at all, no card counts as busy.
                busy = set(snapshot.gpu_tenants.values())
                for gpu in snapshot.gpus:
                    capacity.gpus[gpu.index] = gpu.index in busy
                capacity.total_memory_mb = {
                    gpu.index: gpu.memory_total_mb
                    for gpu in snapshot.gpus
                    if gpu.memory_total_mb
                }
            self._capacity[snapshot.name] = capacity

    # --------------------------------------------------------- dependency IO
    def _missing_inputs(
        self, entry: QueueEntry, upstream: Sequence[str] = ()
    ) -> Dict[str, str]:
        """Required inputs that are plain paths and do not exist yet.

        A pipeline node name is resolved by RELION and is covered by the
        dependency check instead. This is the last gate that stops the queue
        from starting a job against a file an upstream job is still writing -
        the same check `submit_job` makes, done before anything is started
        rather than after.

        An input whose producer is already in ``upstream`` is skipped: "waiting
        for Tomograms/job001" and "waiting for in_tiltseries=...job001/..." are
        the same fact, and printing both makes the reason harder to read.

        A path that is a *reserved* output - a node the pipeline lists as
        produced by a Scheduled job - is likewise not missing: the file will be
        written when that job runs. That is the input-slot case, and treating it
        as absent would stop a chain from ever starting.
        """
        try:
            schema = job_commands.form_schema(entry.job_type)
        except KeyError:
            return {}
        producers = set(upstream)
        missing: Dict[str, str] = {}
        pipeline = None
        for field in schema.get('fields', []):
            if field.get('kind') not in ('path', 'input_node'):
                continue
            value = entry.arguments.get(field['variable'])
            if not _present(value):
                continue
            text = str(value).strip()
            # A node name is relative to the project; only plain filesystem
            # paths can be checked here.
            if not (text.startswith('/') or text.startswith('~') or text.startswith('.')):
                continue
            path = Path(text).expanduser()
            if not path.is_absolute():
                path = Path(entry.project) / path
            if path.exists():
                continue
            if producers and self._producer_matches(entry.project, text, producers):
                continue
            if pipeline is None:
                try:
                    pipeline = pipeline_module.read_pipeline(Path(entry.project))
                except (FileNotFoundError, ValueError):
                    pipeline = False
            if pipeline and self._producer_of(pipeline, entry.project, text):
                # Reserved by a queued job: the path is real, the file is not
                # written yet.
                continue
            missing[field['variable']] = text
        return missing

    def _producer_matches(
        self, project: str, value: str, producers: set
    ) -> bool:
        try:
            pipeline = pipeline_module.read_pipeline(Path(project))
        except (FileNotFoundError, ValueError):
            return False
        name = self._producer_of(pipeline, project, value)
        return name is not None and name in producers

    def _derive_dependencies(self, entry: QueueEntry) -> List[str]:
        """Upstream processes this entry's inputs come from.

        Read from the pipeline on every call, because a downstream entry is
        normally queued before its producer exists. Inputs that are already
        finished drop out; inputs whose producer is still running (or failed)
        become a wait.
        """
        try:
            pipeline = pipeline_module.read_pipeline(Path(entry.project))
        except (FileNotFoundError, ValueError):
            return []
        try:
            schema = job_commands.form_schema(entry.job_type)
        except KeyError:
            return []

        producers: List[str] = []
        for field in schema.get('fields', []):
            if field.get('kind') not in ('path', 'input_node'):
                continue
            value = entry.arguments.get(field['variable'])
            if not _present(value):
                continue
            process_name = self._producer_of(pipeline, entry.project, str(value))
            if process_name and process_name not in producers:
                # A finished producer is not a wait; skipping it keeps the
                # blocked list to things that are actually blocking.
                status = self._status_of_process(pipeline, process_name)
                if status != pipeline_module.STATUS_SUCCEEDED:
                    producers.append(process_name)
        return producers

    @staticmethod
    def _producer_of(pipeline, project: str, value: str) -> Optional[str]:
        """The process that produced ``value``, as the pipeline records it."""
        text = value.strip()
        candidate = text
        if text.startswith(str(project)):
            candidate = str(Path(text).relative_to(project))
        # The GUI keys nodes by project-relative path; an absolute path outside
        # the project has no producer to find.
        for source, node in pipeline.output_edges:
            if node == candidate:
                return source
        # A path into a job directory (".../Tomograms/job005/tomograms.star")
        # is also a node; try the normalised relative form.
        normalised = candidate.rstrip('/')
        for source, node in pipeline.output_edges:
            if node.rstrip('/') == normalised:
                return source
        return None

    @staticmethod
    def _status_of_process(pipeline, name: str) -> Optional[str]:
        process = pipeline.find_process(name)
        return process.status if process is not None else None

    def _process_status(self, project: str, name: str) -> Optional[str]:
        try:
            pipeline = pipeline_module.read_pipeline(Path(project))
        except (FileNotFoundError, ValueError):
            return None
        return self._status_of_process(pipeline, name)

    def _job_state(self, entry: QueueEntry) -> Optional[str]:
        """The entry's live status.

        Three sources, in order of authority:

        1. the job manager, while it still holds the job (this process started
           it, so its exit code is known);
        2. the **sentinel files**. These are RELION's own protocol and the only
           durable record a job leaves behind: a program writes
           ``RELION_JOB_EXIT_SUCCESS``, ``_FAILURE`` or ``_ABORTED`` into its
           ``--pipeline_control`` directory. They outlive the agent, so they are
           what settles an entry adopted after a restart;
        3. the pipeline row, last, because it is a *summary* the agent itself
           writes - and `sync_pipeline_status` deliberately swallows write
           failures (`jobs.py`), so a row can lag indefinitely behind reality.

        Reading the sentinels before the row is the fix for a real leak: an
        adopted entry whose job had finished stayed ``running`` and kept its GPU
        leased forever, because nothing looked at the evidence sitting in the
        job directory.
        """
        if entry.job_id:
            job = self.manager.get(entry.job_id)
            if job is not None:
                return job.status

        sentinel = self._sentinel_state(entry)
        if sentinel is not None:
            return sentinel

        if entry.process_name:
            status = self._process_status(entry.project, entry.process_name)
            if status == pipeline_module.STATUS_SCHEDULED:
                # A row still saying Scheduled cannot be read as "not started":
                # this entry was dispatched, so the sync simply did not happen
                # yet. The honest answer is "running".
                return statuses.RUNNING
            return statuses.from_label(status)
        return None

    def _sentinel_state(self, entry: QueueEntry) -> Optional[str]:
        """The status RELION's sentinel files imply, if any are present.

        Delegated to `jobs_module.sentinel_status` so the protocol has exactly
        one reader: the job manager and the queue must never disagree about what
        an exit file means.
        """
        if not entry.process_name:
            return None
        return jobs_module.sentinel_status(Path(entry.project) / entry.process_name)

    def _failure_reason(self, entry: QueueEntry) -> Optional[str]:
        if entry.job_id:
            job = self.manager.get(entry.job_id)
            if job is not None and job.error:
                return job.error
        return None

    # ----------------------------------------------------- option inspection
    @staticmethod
    def _schema_fields(job_type: str) -> Dict[str, dict]:
        base = job_commands.base_job_type(job_type)
        job = SCHEMA_JOBS.get(base)
        if job is None:
            return {}
        return {field['variable']: field for field in job['fields']}

    def _derive_cores(self, job_type: str, arguments: Dict[str, object]) -> int:
        """Cores a job reserves: ``nr_mpi * nr_threads``, with RELION's defaults."""
        fields = self._schema_fields(job_type)
        values: Dict[str, object] = {}
        for variable in ('nr_mpi', 'nr_threads'):
            field = fields.get(variable)
            if field is not None and 'default' in field:
                values[variable] = field['default']
        values.update(
            {key: value for key, value in arguments.items() if value is not None}
        )
        mpi = _as_int(values.get('nr_mpi'), 1)
        threads = _as_int(values.get('nr_threads'), 1)
        return max(1, mpi) * max(1, threads)

    @staticmethod
    def _requested_devices(
        job_type: str, arguments: Dict[str, object]
    ) -> Optional[List[int]]:
        """Device indices the job's own options name, if any.

        ``gpu_ids`` is a RELION mini-language: ``:`` separates MPI ranks and
        ``,`` separates threads. For admission only the set of card numbers
        matters, so every integer in the string is taken.

        Empty for a job that does not use a GPU at all, so a CPU job never
        reserves - or is blocked by - a card even if its form has the field.
        """
        if not JobQueue._needs_gpu(job_type, arguments):
            return None
        fields = JobQueue._schema_fields(job_type)
        variable = _gpu_option(job_type)
        if variable is None or variable not in fields:
            return None
        value = arguments.get(variable)
        if not _present(value):
            return None
        found: List[int] = []
        for token in str(value).replace(':', ',').split(','):
            token = token.strip()
            if not token:
                continue
            try:
                index = int(token)
            except ValueError:
                continue
            if index not in found:
                found.append(index)
        return sorted(found) or None

    @staticmethod
    def _needs_gpu(job_type: str, arguments: Dict[str, object]) -> bool:
        """Whether this job will occupy a card.

        Deliberately a table rather than "the form has a gpu_ids field": several
        jobs carry the field while defaulting to a CPU path. MotionCor2 is the
        trap - ``do_own_motioncor`` defaults to yes, which is RELION's own CPU
        implementation, and the ``--gpu`` flag is only emitted for the external
        one. Leasing a card for that job would strand a GPU for no reason.
        """
        base = job_commands.base_job_type(job_type)
        if base in _GPU_ALWAYS:
            return True
        if base in _GPU_SWITCHED:
            variable, gpu_value = _GPU_SWITCHED[base]
            # The option may be absent from the request, in which case the
            # schema default decides - and that default is what makes
            # MotionCor2 CPU-only unless asked otherwise.
            return JobQueue._boolean(job_type, arguments, variable) == gpu_value
        if base in _GPU_FLAGGED:
            return JobQueue._boolean(job_type, arguments, 'use_gpu')
        return False

    @staticmethod
    def _boolean(job_type: str, arguments: Dict[str, object], variable: str) -> bool:
        """``variable`` as a bool, falling back to the schema's default."""
        if _present(arguments.get(variable)):
            return _truthy(arguments.get(variable))
        field = JobQueue._schema_fields(job_type).get(variable)
        if field is None or 'default' not in field:
            return False
        return _truthy(field['default'])

    @staticmethod
    def _apply_devices(
        job_type: str, arguments: Dict[str, object], devices: List[int]
    ) -> None:
        """Tell the program which cards it was given.

        Only written when the job does not already name its own cards: a value
        the user typed is a deliberate choice, and the queue's job is to keep
        that card free, not to overrule it.
        """
        variable = _gpu_option(job_type)
        if variable is None:
            return
        fields = JobQueue._schema_fields(job_type)
        if variable not in fields or _present(arguments.get(variable)):
            return
        if variable == 'gpu_id':
            arguments[variable] = str(devices[0])
        else:
            arguments[variable] = ','.join(str(index) for index in devices)
        if 'use_gpu' in fields and not _truthy(arguments.get('use_gpu')):
            arguments['use_gpu'] = True

    # ------------------------------------------------------------ persistence
    @property
    def _file(self) -> Optional[Path]:
        if self.config_directory is None:
            return None
        return self.config_directory / self.FILENAME

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _prune(self) -> None:
        """Drop old finished entries, keeping the queue file bounded.

        Only terminal entries are eligible. A live entry is the record of a job
        that still exists - its directory, its pipeline row and anything waiting
        on it - so it is never dropped.

        Two limits, because each catches what the other misses: an age limit
        handles a queue that ran briefly and hard, and a count limit handles a
        queue that ran slowly for months. Whichever would free more is applied.

        Note this prunes the *queue's* record, not the jobs. A finished job keeps
        its directory and its pipeline row, exactly as a job started from the
        desktop GUI does; only the queue's own listing forgets it.
        """
        finished = [
            entry for entry in self._entries.values() if entry.is_terminal
        ]
        if not finished:
            return
        now = time.time()
        expired = {
            entry.id
            for entry in finished
            if (entry.finished_at or entry.created_at)
            < now - self.keep_finished_seconds
        }
        # Oldest first, so the count limit drops the least useful entries.
        by_age = sorted(finished, key=lambda entry: entry.finished_at or entry.created_at)
        overflow = by_age[: max(0, len(by_age) - self.keep_finished)]
        doomed = expired | {entry.id for entry in overflow}
        for entry_id in doomed:
            self._entries.pop(entry_id, None)

    def _save(self) -> None:
        path = self._file
        if path is None:
            return
        self._prune()
        payload = {
            'counter': self._counter,
            'entries': [entry.to_json() for entry in self._entries.values()],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(
                json.dumps(payload, indent=2) + '\n', encoding='utf-8'
            )
            temporary.replace(path)
        except OSError:
            # A queue that cannot write its state still functions for this run.
            return
        self._dirty = False

    def _load(self) -> None:
        path = self._file
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        self._counter = int(payload.get('counter') or 0)
        for raw in payload.get('entries') or []:
            try:
                entry = QueueEntry.from_json(raw)
            except (KeyError, TypeError, ValueError):
                continue
            self._entries[entry.id] = entry
            try:
                number = int(entry.id.lstrip('q'))
            except ValueError:
                continue
            self._counter = max(self._counter, number)

    def _adopt_running(self) -> None:
        """Resolve entries that were running when the agent last stopped.

        They are not resubmitted: their job directory still exists, so the real
        status is read from the job's own sentinel files first - the durable
        record RELION leaves behind - and only then from the pipeline row, which
        the agent writes and may have failed to update.

        An entry that is genuinely still running keeps its devices reserved and
        stays cancellable, which is what makes a restart during a long job safe.
        """
        for entry in self._entries.values():
            if entry.state != RUNNING:
                continue
            status = self._job_state(entry)
            if status == statuses.SUCCEEDED:
                entry.state = SUCCEEDED
                entry.finished_at = entry.finished_at or time.time()
                entry.leased_gpus = []
            elif status in (statuses.FAILED, statuses.ABORTED):
                entry.state = FAILED
                entry.finished_at = entry.finished_at or time.time()
                entry.leased_gpus = []
                entry.error = entry.error or 'the job failed while the agent was away'
            elif status is None:
                # Nothing can be learned: the job was never registered.
                entry.state = FAILED
                entry.finished_at = time.time()
                entry.leased_gpus = []
                entry.error = (
                    'the agent restarted before this job was registered; '
                    'its directory may be incomplete'
                )
            else:
                entry.leased_gpus = entry.leased_gpus or list(entry.gpus)
                # Still running, so say where that was concluded from: a row
                # claiming Running with no process behind it is worth knowing
                # about, because cancelling is then the only way to free it.
                if self._sentinel_state(entry) is None:
                    entry.warnings.append(
                        'adopted as running after a restart: no exit file yet, so '
                        'it is treated as still running and keeps its devices'
                    )
        self._save()


# A sentinel for "the devices could not be allocated", distinct from "no GPUs
# are wanted", which is an empty list.
_NO_DEVICES = object()

# Which jobs occupy a card, by base job type.
#
# `_GPU_ALWAYS`: the program only runs on a GPU, whatever the options say.
# `_GPU_SWITCHED`: one option decides, and its value says which way - MotionCor2
#   is the case that matters, since RELION's own implementation is CPU-only and
#   is the default.
# `_GPU_FLAGGED`: `relion_refine` and friends, where `use_gpu` is explicit and
#   defaults to no.
_GPU_ALWAYS = frozenset(
    {
        'tomo_align_aretomo',
        'tomo_align_imod_fiducials',
        'tomo_align_imod_patchtrack',
        'tomo_align_tilt_series',
        'tomo_denoise_tomograms',
        'tomo_denoise_train',
        'tomo_denoise_predict',
        'modelangelo',
        'modelangelo_build',
        'modelangelo_hmm',
    }
)

_GPU_SWITCHED = {
    # do_own_motioncor=True is RELION's CPU implementation; the GPU is used only
    # by the external MotionCor2 (`--use_motioncor2`).
    'motioncorr': ('do_own_motioncor', False),
}

_GPU_FLAGGED = frozenset({'autorefine', 'class3d', 'inimodel', 'multibody'})


def _gui_options(job_type: str, arguments: Dict[str, object]) -> Dict[str, object]:
    """The option table ``job.star`` must carry for a reserved job.

    The same shape the job manager writes when a job finishes: every option the
    schema knows, filled from its own defaults for the ones the caller did not
    set, plus the variant's branch switch. The GUI reads this to open the job
    window, so a queued job opens with the parameters it was queued with rather
    than with blank fields.

    `JobManager._gui_options` is this function; it is kept here as well so that
    reserving a job needs no manager, and delegating means the two cannot drift.
    """
    base = job_commands.base_job_type(job_type)
    schema = SCHEMA_JOBS.get(base)
    if schema is None:
        return dict(arguments)
    options: Dict[str, object] = {}
    for field in schema['fields']:
        variable = field['variable']
        if variable in arguments:
            options[variable] = arguments[variable]
        elif 'default' in field:
            options[variable] = field['default']
    for key, value in arguments.items():
        options.setdefault(key, value)
    # A variant pins its branch through the option that branch tests. Applied
    # last, because the base job's defaults (every other mode switched off)
    # would otherwise win and the GUI would open the wrong branch.
    spec = job_commands.COMMAND_SPECS.get(job_type)
    if spec is not None:
        for key, value in spec.static.items():
            options[key] = value
    return options


def _node_label(pipeline, node_name: str) -> str:
    """The type label of a pipeline node, for its ``.Nodes`` entry.

    Read back from the pipeline rather than carried on the entry: the label is
    RELION's, and a job type the schema changed under us would otherwise delete
    the wrong placeholder.
    """
    node = pipeline.find_node(node_name)
    return node.type_label if node is not None else ''


def _gpu_option(job_type: str) -> Optional[str]:
    """The option naming device indices for this job: ``gpu_ids`` or ``gpu_id``.

    Both spellings exist in RELION. ModelAngelo and DynaMight take one card and
    call it ``gpu_id``; everything else takes a list.
    """
    base = job_commands.base_job_type(job_type)
    fields = {
        field['variable']
        for field in SCHEMA_JOBS.get(base, {}).get('fields', [])
    }
    if 'gpu_id' in fields:
        return 'gpu_id'
    if 'gpu_ids' in fields:
        return 'gpu_ids'
    return None


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('yes', 'true', '1', 'on')


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
