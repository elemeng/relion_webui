# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""The journal: RELION queue states, recorded as FlowGram runtime events.

The vendored UI has one status pathway and one camera pathway, and both read
the same shape: a stream of *runtime events* whose kind is FlowGram's
vocabulary (``node_started``, ``node_finished``, ...). RELION does not emit
events - the queue computes each entry's state every tick from the job manager,
the sentinel files and the pipeline row.

So this module samples the queue, diffs each entry's state against the previous
sample, appends FlowGram-shaped events into a per-run journal, and the SSE
route replays that journal to whichever client asks.

It runs inside the agent, so a sample is a method call on the live
``JobQueue`` rather than an HTTP request to ourselves. It rides the queue's own
2 s tick, which is the cadence its states can change at.

Event vocabulary -> canvas badge (``CanvasNodeRunBadge``):

    node_waiting   Clock3    pending entry, optionally with reasons
    node_started   Loader2   running entry, live elapsed timer
    node_finished  Check     succeeded entry
    node_failed    CircleAlert   failed entry, with the queue's reason
    node_canceled  CircleMinus   cancelled entry
    log_appended   (log tab) incremental run.out bytes while running

Ported from ``flowgram-app/bridge/journal.ts``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .translate import TERMINAL_STATES

TICK_SECONDS = 2.0
#: Log reads ride the tick; a cap keeps one burst from flooding the stream.
LOG_CHUNK_MAX = 8_000
#: How many finished runs the journal remembers.
MAX_RUNS = 200


@dataclass
class JournalEvent:
    sequence: int
    kind: str
    objectId: str
    data: str
    createdAt: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            'sequence': self.sequence,
            'kind': self.kind,
            'objectId': self.objectId,
            'data': self.data,
            'createdAt': self.createdAt,
        }


@dataclass
class RunRecord:
    id: str
    workflowId: str
    projectId: str
    status: str = 'running'
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Any = None
    error: Optional[str] = None
    revision: int = 1
    createdAt: str = ''

    def as_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'workflowId': self.workflowId,
            'projectId': self.projectId,
            'status': self.status,
            'inputs': self.inputs,
            'outputs': self.outputs,
            'error': self.error,
            'revision': self.revision,
            'createdAt': self.createdAt,
        }


@dataclass
class _Journal:
    run: RunRecord
    events: List[JournalEvent] = field(default_factory=list)
    entries: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    log_offsets: Dict[str, int] = field(default_factory=dict)
    seen_states: Dict[str, str] = field(default_factory=dict)
    #: Queue entry ids this run submitted; empty means "watch the project".
    mine: Set[str] = field(default_factory=set)
    finished: bool = False


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _iso(seconds: Optional[float]) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(seconds or time.time(), tz=timezone.utc)
        .isoformat()
        .replace('+00:00', 'Z')
    )


class Journal:
    """The event store, and the sampler that fills it.

    One instance lives on ``app.state``. ``start`` launches the sampler task;
    ``stop`` cancels it. Both are idempotent.
    """

    def __init__(self, queue=None, manager=None) -> None:
        self.queue = queue
        self.manager = manager
        self._runs: Dict[str, _Journal] = {}
        self._sequence = 0
        self._task: Optional[asyncio.Task] = None
        self._listeners: List[asyncio.Queue] = []

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a sampler must not die
                pass
            await asyncio.sleep(TICK_SECONDS)

    # -- recording -------------------------------------------------------
    def _event(self, kind: str, run_id: str, data: Mapping[str, Any]) -> JournalEvent:
        self._sequence += 1
        return JournalEvent(
            sequence=self._sequence,
            kind=kind,
            objectId=run_id,
            data=json.dumps(data, default=str),
            createdAt=_now(),
        )

    def open_run(
        self,
        run_id: str,
        project_id: str,
        inputs: Mapping[str, Any],
        *,
        workflow_id: Optional[str] = None,
        entries: Optional[Sequence[str]] = None,
        revision: int = 1,
    ) -> None:
        """Open (or reopen) a journal for a run that was just submitted."""
        existing = self._runs.get(run_id)
        if existing is None:
            run = RunRecord(
                id=run_id,
                workflowId=workflow_id or f'pipeline:{project_id}',
                projectId=project_id,
                inputs=dict(inputs),
                revision=revision,
                createdAt=_now(),
            )
            journal = _Journal(run=run)
            self._runs[run_id] = journal
            self._append(run_id, self._event('run_started', run_id, {'inputs': dict(inputs)}))
        else:
            journal = existing
            journal.run.status = 'running'
            journal.run.error = None
            journal.finished = False
        if entries is not None:
            journal.mine = set(entries)

    def _append(self, run_id: str, event: JournalEvent) -> None:
        journal = self._runs.get(run_id)
        if journal is not None:
            journal.events.append(event)

    def record_cancel(self, run_id: str, cancelled: Sequence[str]) -> None:
        """Record a client-driven cancellation.

        The queue drops a cancelled live entry on its next tick, so no
        transition would ever report CANCELLED; the cancel route records the
        fact itself and the next tick sees the entry simply gone.
        """
        journal = self._runs.get(run_id)
        if journal is None:
            return
        for entry_id in cancelled:
            entry = journal.entries.get(entry_id)
            if entry is None:
                journal.seen_states[entry_id] = 'cancelled'
                continue
            if entry.get('state') in TERMINAL_STATES:
                continue
            entry['state'] = 'cancelled'
            event = self._state_event({**entry, 'state': 'cancelled'})
            if event is not None:
                kind, data = event
                self._append(run_id, self._event(kind, run_id, data))
            journal.seen_states[entry_id] = 'cancelled'
            journal.log_offsets.pop(str(entry.get('job_id') or ''), None)

    # -- sampling --------------------------------------------------------
    def _state_event(
        self, entry: Mapping[str, Any]
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        common = {
            'nodeId': entry.get('process_name') or entry['id'],
            'executionId': entry['id'],
            'workflowId': f'pipeline:{entry.get("project", "")}',
        }
        state = entry.get('state')
        if state == 'pending':
            return 'node_waiting', {**common, 'reasons': list(entry.get('blocked_by') or [])}
        if state == 'running':
            return 'node_started', {
                **common,
                'startedAt': _iso(entry.get('started_at')),
                'gpus': list(entry.get('leased_gpus') or []),
            }
        if state == 'succeeded':
            return 'node_finished', {
                **common,
                'outputs': dict(entry.get('predicted_outputs') or {}),
                'endedAt': _iso(entry.get('finished_at')),
            }
        if state == 'failed':
            return 'node_failed', {
                **common,
                'error': entry.get('error') or 'job failed',
            }
        if state == 'cancelled':
            return 'node_canceled', dict(common)
        return None

    async def sample(self) -> None:
        """Diff the queue against what the journals last saw."""
        if self.queue is None:
            return
        try:
            snapshot = self.queue.snapshot()
        except Exception:  # noqa: BLE001 - a queue that cannot be read is a tick skipped
            return

        for entry in snapshot.get('entries') or []:
            owner = next(
                (journal for journal in self._runs.values() if entry['id'] in journal.mine),
                None,
            )
            if owner is not None:
                await self._apply(owner, entry)
                continue
            if entry.get('state') in TERMINAL_STATES:
                # A finished entry nobody owns: nothing to report, and creating
                # a run for it would invent history.
                continue
            run_id = f'relion-project:{entry.get("project", "")}'
            journal = self._runs.get(run_id)
            if journal is None:
                self.open_run(run_id, str(entry.get('project', '')), {})
                journal = self._runs[run_id]
            await self._apply(journal, entry)

        for run_id, journal in list(self._runs.items()):
            if journal.finished or not journal.entries:
                continue
            entries = list(journal.entries.values())
            if not all(entry.get('state') in TERMINAL_STATES for entry in entries):
                continue
            journal.finished = True
            journal.run.status = (
                'failed'
                if any(entry.get('state') == 'failed' for entry in entries)
                else 'succeeded'
            )
            error = '; '.join(
                str(entry.get('error')) for entry in entries if entry.get('error')
            )
            if error:
                journal.run.error = error
            self._append(
                run_id,
                self._event(
                    'run_finished',
                    run_id,
                    {
                        'status': journal.run.status,
                        'counts': {
                            entry['id']: entry.get('state') for entry in entries
                        },
                    },
                ),
            )
        self._prune()

    async def _apply(self, journal: _Journal, entry: Mapping[str, Any]) -> None:
        previous = journal.entries.get(str(entry['id']))
        journal.entries[str(entry['id'])] = dict(entry)
        known = journal.seen_states.get(str(entry['id']))

        if known == entry.get('state') and previous is not None:
            # Same state, but a pending entry's blockers can change - the user
            # watches *why* it is waiting as much as *that* it is waiting.
            if entry.get('state') == 'pending' and list(
                previous.get('blocked_by') or []
            ) != list(entry.get('blocked_by') or []):
                event = self._state_event(entry)
                if event is not None:
                    self._append(journal.run.id, self._event(event[0], journal.run.id, event[1]))
            return

        event = self._state_event(entry)
        if event is not None:
            self._append(journal.run.id, self._event(event[0], journal.run.id, event[1]))
        journal.seen_states[str(entry['id'])] = str(entry.get('state'))
        if entry.get('state') == 'running':
            await self._read_log(journal, entry)

    async def _read_log(self, journal: _Journal, entry: Mapping[str, Any]) -> None:
        job_id = entry.get('job_id')
        if not job_id or self.manager is None:
            return
        offset = journal.log_offsets.get(str(job_id), 0)
        try:
            chunk, next_offset, truncated = self.manager.read_log_chunk(str(job_id), offset)
        except Exception:  # noqa: BLE001 - a log read failure is not a job failure
            return
        journal.log_offsets[str(job_id)] = 0 if truncated else next_offset
        if not chunk:
            return
        self._append(
            journal.run.id,
            self._event(
                'log_appended',
                journal.run.id,
                {
                    'nodeId': entry.get('process_name') or entry['id'],
                    'executionId': entry['id'],
                    'workflowId': f'pipeline:{entry.get("project", "")}',
                    'chunk': chunk[-LOG_CHUNK_MAX:],
                    'truncated': truncated,
                },
            ),
        )

    def _prune(self) -> None:
        if len(self._runs) <= MAX_RUNS:
            return
        finished = [
            (run_id, journal)
            for run_id, journal in self._runs.items()
            if journal.finished
        ]
        finished.sort(key=lambda pair: pair[1].run.createdAt)
        for run_id, _ in finished[: len(self._runs) - MAX_RUNS]:
            self._runs.pop(run_id, None)

    # -- reads -----------------------------------------------------------
    def run(self, run_id: str) -> Optional[_Journal]:
        return self._runs.get(run_id)

    def runs_for_project(self, project_path: str) -> List[RunRecord]:
        records = [
            journal.run
            for journal in self._runs.values()
            if journal.run.projectId == project_path
        ]
        records.sort(key=lambda record: record.createdAt, reverse=True)
        return records[:50]

    def all_runs(self) -> List[Tuple[RunRecord, str]]:
        """Every run, newest first, with a display name for its workflow."""
        pairs = [
            (journal.run, workflow_name(journal.run.workflowId))
            for journal in self._runs.values()
        ]
        pairs.sort(key=lambda pair: pair[0].createdAt, reverse=True)
        return pairs[:MAX_RUNS]

    def events_after(
        self, after: int, run_id: Optional[str] = None
    ) -> List[JournalEvent]:
        events: List[JournalEvent] = []
        for key, journal in self._runs.items():
            if run_id is not None and key != run_id:
                continue
            events.extend(event for event in journal.events if event.sequence > after)
        events.sort(key=lambda event: event.sequence)
        return events


def workflow_name(workflow_id: str) -> str:
    tail = workflow_id.removeprefix('pipeline:').rstrip('/')
    return tail.rsplit('/', 1)[-1] or 'Pipeline'
