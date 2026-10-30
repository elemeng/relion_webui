# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Job actions, delegating to RELION's own programs wherever possible.

The desktop GUI's "Job actions" menu offers alias, note, abort, mark as
finished/failed, gentle/harsh clean, delete and undelete. Most of that already
exists in RELION, and re-implementing it would mean reproducing per-job cleanup
rules that change with every release. So the actions are split in two:

*Delegated.* ``relion_pipeliner`` implements alias, gentle clean and harsh clean
on the command line, and the cleanup rules live in ``PipeLine::cleanupJob``.
Calling it means the web UI cleans exactly what the GUI cleans.

*Written here.* Note editing is reading and writing ``<job>note.txt``, which is
a plain text file the GUI's note editor shows - there is nothing to delegate.
Delete and undelete are the one gap: ``PipeLine::deleteJobGetNodesAndProcesses``
and ``PipeLine::undeleteJob`` exist in the library, but neither is exposed by
``relion_pipeliner``'s options, so they are implemented against the pipeline
file using the same conventions as the rest of this package.

Mark as finished/failed is also library-only in the same way, and is here as
that is a single status write.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import job_artifacts
from . import pipeline as pipeline_module

# The GUI's own note file name: `<process name>note.txt` (gui_mainwindow.cpp:2161).
NOTES_SUFFIX = 'note.txt'
TRASH_DIRECTORY = 'Trash'


class ActionError(RuntimeError):
    """The action could not be performed; the message is user-facing."""


@dataclass
class ActionResult:
    action: str
    process_name: Optional[str]
    message: str
    # Extra facts the UI can show, e.g. the new alias or the note text.
    detail: Optional[dict] = None


# --------------------------------------------------------------- delegating
def pipeliner_available() -> bool:
    return shutil.which('relion_pipeliner') is not None


def _run_pipeliner(
    project: Path, args: List[str], timeout: float = 120.0
) -> subprocess.CompletedProcess:
    """Run ``relion_pipeliner`` in the project directory.

    ``relion_pipeliner`` resolves ``--pipeline default`` relative to the working
    directory, exactly like the GUI, so the project directory is also the cwd.
    """
    executable = shutil.which('relion_pipeliner')
    if executable is None:
        raise ActionError(
            'relion_pipeliner is not on PATH; install RELION or put its bin/ '
            'directory on PATH to use this action'
        )
    try:
        return subprocess.run(
            [executable, *args],
            cwd=str(project),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionError(f'relion_pipeliner timed out: {exc}') from exc


# The characters `PipeLine::setAliasJob` refuses (pipeliner.cpp:1281).
_ALIAS_FORBIDDEN = set('*?()/\\"|#<>&%{}$')


def validate_alias(alias: str) -> str:
    """Normalise and check an alias the way ``PipeLine::setAliasJob`` does.

    The GUI puts the alias in the process name, and a downstream node link is
    named after it, so an alias with a slash or a wildcard would either break the
    link or be expanded by the shell.
    """
    alias = alias.strip()
    if not alias:
        return 'None'
    if len(alias) < 2:
        raise ActionError('alias must be at least 2 characters')
    if alias.lower().startswith('job'):
        raise ActionError("alias cannot start with 'job'")
    forbidden = sorted(_ALIAS_FORBIDDEN & set(alias))
    if forbidden:
        raise ActionError(
            'alias cannot contain: ' + ' '.join(forbidden)
        )
    return alias.replace(' ', '_')


def set_alias(project: Path, process_name: str, alias: str) -> ActionResult:
    """Set a job's alias.

    An alias is not just a display label: RELION stores it as a path prefix
    (``<type>/<alias>/``) and creates a symlink to the job directory at that
    path, which is how downstream jobs refer to an aliased output. The old link
    has to go and ``.Nodes`` be rebuilt, so this follows
    ``PipeLine::setAliasJob`` rather than writing the field alone.
    """
    project = Path(project)
    alias = validate_alias(alias)

    pipeline = pipeline_module.read_pipeline(project)
    process = pipeline.find_process(process_name)
    if process is None:
        raise ActionError(f'{process_name} is not in the pipeline')

    type_prefix = pipeline.directory_of(process_name)
    if alias != 'None':
        wanted = (type_prefix + alias).rstrip('/')
        for other in pipeline.processes:
            if other.name == process_name:
                continue
            if (other.alias or '').rstrip('/') == wanted:
                raise ActionError(f'alias {alias!r} is already used in {type_prefix}')

    # Remove the old node links, which are named after the old alias.
    old_alias = process.alias
    if old_alias not in (None, 'None', ''):
        old_link = project / old_alias.rstrip('/')
        if old_link.is_symlink():
            old_link.unlink()

    if alias == 'None':
        process.alias = 'None'
    else:
        # RELION stores the alias with a trailing slash, because it is used as a
        # path prefix when naming node links.
        process.alias = type_prefix + alias + '/'
        # RELION makes `<type>/<alias>` a symlink to `<type>/<jobname>`, so the
        # link and its target are siblings and the target is just the job name.
        link = project / process.alias.rstrip('/')
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(Path(process_name).name)

    pipeline_module.write_pipeline(project, pipeline)

    # Rebuild the links the GUI browses, now under the new name.
    for node in pipeline.nodes:
        if node.name.startswith(process_name + '/'):
            job_artifacts.create_node_symlink(
                project, node.name, node.type_label, alias=process.alias
            )

    return ActionResult(
        action='alias',
        process_name=process_name,
        message=f'alias set to {alias!r}',
        detail={'alias': process.alias},
    )


def clean(project: Path, process_name: str, *, harsh: bool) -> ActionResult:
    """Gentle or harsh clean, via ``relion_pipeliner --gentle_clean/--harsh_clean``.

    The cleanup rules differ per job type (which intermediates to keep for a
    possible continuation), so this is deliberately RELION's implementation.
    """
    job_number = _job_number(project, process_name)
    status = _process_status(project, process_name)
    if status != pipeline_module.STATUS_SUCCEEDED:
        raise ActionError(
            f'{process_name} is {status}; only successfully finished jobs can be cleaned'
        )
    flag = '--harsh_clean' if harsh else '--gentle_clean'
    result = _run_pipeliner(project, [flag, str(job_number)])
    if result.returncode != 0:
        raise ActionError(result.stderr.strip() or result.stdout.strip()
                          or 'relion_pipeliner failed')
    return ActionResult(
        action='harsh_clean' if harsh else 'gentle_clean',
        process_name=process_name,
        message=f'{"harsh" if harsh else "gentle"} cleaned {process_name}',
    )


# ------------------------------------------------------------------ notes
def note_path(project: Path, process_name: str) -> Path:
    return Path(project) / Path(process_name) / f'{Path(process_name).name}{NOTES_SUFFIX}'


def read_note(project: Path, process_name: str) -> ActionResult:
    path = note_path(project, process_name)
    text = path.read_text(encoding='utf-8', errors='replace') if path.is_file() else ''
    return ActionResult(
        action='note_read',
        process_name=process_name,
        message='note loaded',
        detail={'note': text, 'path': str(path)},
    )


def write_note(project: Path, process_name: str, text: str) -> ActionResult:
    """Replace a job's note.

    The file is the same one the GUI's note editor opens, so a note written here
    appears there and vice versa.
    """
    path = note_path(project, process_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return ActionResult(
        action='note_write',
        process_name=process_name,
        message='note saved',
        detail={'path': str(path), 'bytes': len(text.encode('utf-8'))},
    )


# ---------------------------------------------------------------- deletion
def _free_trash_destination(trash: Path, name: str) -> Path:
    """A Trash path for ``name`` that does not overwrite an earlier deletion.

    RELION's own layout is ``Trash/<Type>/jobNNN``, and ``undelete`` reads that
    name back. So a second deletion of the same job number must not clobber the
    first: the earlier directory is still the only copy of a job the user may
    have deleted by mistake, and overwriting it would destroy the one thing
    Trash exists to preserve.

    A suffix is appended until the path is free. The suffix is a dot followed by
    a counter - ``job005.1`` - because ``undelete`` treats the entry as a
    ``<Type>/<job>`` pair and ``Path.name`` still yields the whole thing, so the
    restore path needs no special case.
    """
    destination = trash / name
    if not destination.exists():
        return destination
    stem = destination
    counter = 1
    while True:
        candidate = stem.with_name(f'{stem.name}.{counter}')
        if not candidate.exists():
            return candidate
        counter += 1


def _refuse_if_running(project: Path, process_name: str) -> None:
    """Refuse to delete a job whose directory a process is currently using.

    Moving the working directory out from under a running RELION program is not
    recoverable: its outputs become writes to a moved tree, and RELION's own
    abort handshake (`ABORT_JOB_NOW`) is looked for in the old path. The desktop
    GUI disables Delete while a job runs; so does this.
    """
    status = _process_status(project, process_name)
    if status == pipeline_module.STATUS_RUNNING:
        raise ActionError(
            f'{process_name} is running; stop it before deleting it, '
            'otherwise its output directory moves while it is still writing'
        )


def delete(project: Path, process_name: str, *, recursive: bool = False) -> ActionResult:
    """Remove a job from the pipeline and move its directory to ``Trash/``.

    RELION deletes a job by dropping the process and its output nodes from
    ``default_pipeline.star`` and moving the directory into ``Trash/``; that is
    what makes ``Undelete`` possible afterwards. ``relion_pipeliner`` does not
    expose it, so the two steps are done here with the same conventions.
    """
    project = Path(project)
    pipeline = pipeline_module.read_pipeline(project)
    index = _process_index(pipeline, process_name)
    if index is None:
        raise ActionError(f'{process_name} is not in the pipeline')
    _refuse_if_running(project, process_name)

    doomed_processes = {process_name}
    if recursive:
        # Follow the GUI: everything downstream of a removed output goes too.
        frontier = [process_name]
        while frontier:
            current = frontier.pop()
            for source, target in pipeline.output_edges:
                if source != current:
                    continue
                for node_name, consumer in pipeline.input_edges:
                    if node_name != target or consumer in doomed_processes:
                        continue
                    doomed_processes.add(consumer)
                    frontier.append(consumer)

    # RELION writes `<first deleted job>deleted_pipeline.star` holding exactly
    # the rows it removed, which is what its Undelete dialog opens
    # (pipeliner.cpp:2177). Snapshot the doomed rows before mutating.
    deleted_snapshot = {
        'processes': [
            p for p in pipeline.processes if p.name in doomed_processes
        ],
        'nodes': [
            n
            for n in pipeline.nodes
            if any(
                node == n.name and process in doomed_processes
                for process, node in pipeline.output_edges
            )
        ],
        'input_edges': [
            (n, p) for n, p in pipeline.input_edges if p in doomed_processes
        ],
        'output_edges': [
            (p, n) for p, n in pipeline.output_edges if p in doomed_processes
        ],
        'job_counter': pipeline.job_counter,
    }

    doomed_nodes = {
        node for process, node in pipeline.output_edges if process in doomed_processes
    }
    still_used = {
        node for node, process in pipeline.input_edges if process not in doomed_processes
    }
    # A node produced by a deleted job but still consumed elsewhere stays.
    doomed_nodes -= still_used

    pipeline.processes = [p for p in pipeline.processes if p.name not in doomed_processes]
    pipeline.nodes = [n for n in pipeline.nodes if n.name not in doomed_nodes]
    pipeline.input_edges = [
        (n, p) for n, p in pipeline.input_edges if p not in doomed_processes
    ]
    pipeline.output_edges = [
        (p, n) for p, n in pipeline.output_edges if p not in doomed_processes
    ]
    pipeline_module.write_pipeline(project, pipeline)

    # Write the deleted-jobs pipeline next to the job that was removed, so
    # RELION's own Undelete finds it where it looks (Trash/<name>deleted_pipeline.star
    # after the move, which the move below preserves).
    _write_deleted_pipeline(project, deleted_snapshot, sorted(doomed_processes, key=len)[0])

    # RELION keeps the job-type directory inside Trash (Trash/<Type>/jobNNN),
    # which is what lets Undelete put the job back where it came from
    # (pipeliner.cpp:1132). Flattening it would lose the type.
    trash = project / TRASH_DIRECTORY
    moved = []
    renamed = []
    for name in sorted(doomed_processes, key=len, reverse=True):
        directory = project / name
        if not directory.exists():
            continue
        # Never overwrite an earlier deletion: that copy is the only remaining
        # one, and Trash exists precisely to keep it.
        destination = _free_trash_destination(trash, name)
        if destination.name != Path(name).name:
            renamed.append(
                {
                    'process_name': name,
                    'trash_entry': str(destination.relative_to(trash)),
                }
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(directory), str(destination))
        moved.append(name)
        _remove_node_links(project, name)

    return ActionResult(
        action='delete',
        process_name=process_name,
        message=(
            f'deleted {process_name}'
            + (f' and {len(doomed_processes) - 1} downstream job(s)' if recursive else '')
            + f'; moved to {TRASH_DIRECTORY}/'
            + (
                ' (an earlier copy of this job was already in Trash, so this one '
                'is stored beside it)'
                if renamed
                else ''
            )
        ),
        detail={
            'deleted': sorted(doomed_processes),
            'moved': moved,
            # Non-empty when a name collision forced a suffixed entry; the UI can
            # then say which name to use for undelete.
            'renamed': renamed,
        },
    )


def _write_deleted_pipeline(project: Path, snapshot: dict, process_name: str) -> None:
    """Write ``<job>deleted_pipeline.star`` with the removed rows.

    This is the file RELION's Undelete dialog opens; without it the GUI has no
    way to put a deleted job back. Its shape is a normal pipeline file.
    """
    if not snapshot['processes']:
        return
    pipeline = pipeline_module.Pipeline(
        job_counter=snapshot['job_counter'],
        processes=list(snapshot['processes']),
        nodes=list(snapshot['nodes']),
        input_edges=list(snapshot['input_edges']),
        output_edges=list(snapshot['output_edges']),
    )
    path = Path(project) / process_name / 'deleted_pipeline.star'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pipeline_module.write_pipeline_file(path, pipeline)
    except (OSError, AttributeError):
        return


def list_trash(project: Path) -> ActionResult:
    """What can be undeleted: the job directories sitting in ``Trash/``."""
    trash = Path(project) / TRASH_DIRECTORY
    entries = []
    if trash.is_dir():
        # Entries are reported as <Type>/jobNNN, the form undelete takes.
        for type_dir in sorted(trash.iterdir()):
            if not type_dir.is_dir():
                continue
            for item in sorted(type_dir.iterdir()):
                if item.is_dir():
                    entries.append(f'{type_dir.name}/{item.name}')
    return ActionResult(
        action='trash_list',
        process_name=None,
        message=f'{len(entries)} job(s) in Trash',
        detail={'entries': entries},
    )


def _process_name_from_trash_entry(trash_entry: str) -> str:
    """The pipeline process name a Trash entry came from.

    A repeated deletion lands at ``Tomograms/job005.1`` so it cannot overwrite
    the earlier copy. The suffix is an artifact of Trash, not part of the job
    name - ``deleted_pipeline.star`` and the pipeline both still say
    ``Tomograms/job005``, so it has to come off before anything looks the job up.

    RELION job directories are always ``job<NNN>``, so a dot in the last path
    component is never a legitimate part of a name.
    """
    head, _, tail = trash_entry.rpartition('/')
    stem = tail.split('.', 1)[0]
    return f'{head}/{stem}' if head else stem


def undelete(project: Path, trash_entry: str) -> ActionResult:
    """Move a job back out of ``Trash/`` and re-register it.

    Prefers the ``<job>deleted_pipeline.star`` written at delete time, because
    that is what RELION records and it carries the exact rows that were removed
    (including their edges and the old job counter). ``job.star`` is the
    fallback for a job that was moved to Trash by hand.
    """
    project = Path(project)
    directory = project / TRASH_DIRECTORY / trash_entry
    if not directory.is_dir():
        raise ActionError(f'no {trash_entry} in {TRASH_DIRECTORY}/')

    # The entry is <Type>/jobNNN, so the type directory is already part of it.
    if '/' not in trash_entry:
        raise ActionError(
            f'{trash_entry} is not inside a job-type directory; move it back by hand'
        )

    # `Tomograms/job005.1` is the second deleted copy of `Tomograms/job005`; the
    # suffix exists only to keep Trash from colliding, so it comes off here.
    process_name = _process_name_from_trash_entry(trash_entry)

    saved = _read_deleted_pipeline(directory)
    if saved is not None:
        process = saved.find_process(process_name)
        if process is None:
            saved = None

    if saved is None:
        job_star = directory / 'job.star'
        if not job_star.is_file():
            raise ActionError(
                f'{trash_entry} has no deleted_pipeline.star or job.star, so its '
                f'type cannot be recovered'
            )
        type_label = _job_star_type_label(job_star)
        if type_label is None:
            raise ActionError(f'{trash_entry}/job.star has no rlnJobTypeLabel')
    else:
        type_label = process.type_label

    destination = project / process_name
    if destination.exists():
        raise ActionError(f'{destination} already exists; remove it first')

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(directory), str(destination))

    pipeline = pipeline_module.read_pipeline(project)

    if saved is not None:
        # Restore from the recorded snapshot: process row plus its node and edge
        # rows, which is what RELION's undeleteJob copies back.
        restored_process = saved.find_process(process_name)
        restored_nodes = [
            node
            for edge_process, node in saved.output_edges
            if edge_process == process_name
        ]
        restored_in = [
            (node, target)
            for node, target in saved.input_edges
            if target == process_name
        ]
        pipeline_module.register_process(
            pipeline,
            restored_process,
            input_nodes=[
                saved.find_node(node)
                for node, _ in restored_in
                if saved.find_node(node) is not None
            ],
            output_nodes=[
                saved.find_node(node)
                for node in restored_nodes
                if saved.find_node(node) is not None
            ],
        )
        output_nodes = [
            saved.find_node(node)
            for node in restored_nodes
            if saved.find_node(node) is not None
        ]
    else:
        restored_process = pipeline_module.Process(
            name=process_name,
            alias='None',
            type_label=type_label,
            status=pipeline_module.STATUS_SUCCEEDED,
        )
        output_nodes = _output_nodes_from_job(destination, process_name)
        pipeline_module.register_process(
            pipeline, restored_process, output_nodes=output_nodes
        )

    pipeline_module.write_pipeline(project, pipeline)
    for node in output_nodes:
        job_artifacts.create_node_entry(
            project, node.name, node.type_label, alias=restored_process.alias
        )
    return ActionResult(
        action='undelete',
        process_name=process_name,
        message=f'restored {process_name}',
    )


# ------------------------------------------------------------- mark status
def mark_status(project: Path, process_name: str, *, failed: bool) -> ActionResult:
    """Set a job's status by hand, as the GUI's "Mark as finished/failed" does.

    The GUI writes the same sentinel file a program would, so the pipeline and
    the GUI agree without touching the pipeline writer twice.
    """
    directory = Path(project) / process_name
    if not directory.is_dir():
        raise ActionError(f'{process_name} has no job directory')
    from ._vendor import JOB_FAILURE_FILENAME, JOB_SUCCESS_FILENAME

    (directory / JOB_FAILURE_FILENAME).unlink(missing_ok=True)
    (directory / JOB_SUCCESS_FILENAME).unlink(missing_ok=True)
    sentinel = JOB_FAILURE_FILENAME if failed else JOB_SUCCESS_FILENAME
    (directory / sentinel).touch()

    status = pipeline_module.STATUS_FAILED if failed else pipeline_module.STATUS_SUCCEEDED
    pipeline = pipeline_module.read_pipeline(project)
    if not pipeline_module.update_process_status(pipeline, process_name, status):
        raise ActionError(f'{process_name} is not in the pipeline')
    pipeline_module.write_pipeline(project, pipeline)
    return ActionResult(
        action='mark_failed' if failed else 'mark_succeeded',
        process_name=process_name,
        message=f'{process_name} marked {status}',
    )


# ------------------------------------------------------------------ helpers
def _process_index(pipeline: pipeline_module.Pipeline, process_name: str) -> Optional[int]:
    for index, process in enumerate(pipeline.processes):
        if process.name == process_name:
            return index
    return None


def _process_status(project: Path, process_name: str) -> str:
    process = pipeline_module.read_pipeline(project).find_process(process_name)
    if process is None:
        raise ActionError(f'{process_name} is not in the pipeline')
    return process.status


def _job_number(project: Path, process_name: str) -> int:
    number = pipeline_module.read_pipeline(project).job_number_of(process_name)
    if number == 0:
        raise ActionError(f'cannot read a job number from {process_name}')
    return number


def _remove_node_links(project: Path, process_name: str) -> None:
    """Drop the ``.Nodes`` links for a job that no longer exists."""
    nodes_dir = Path(project) / job_artifacts.NODES_DIRECTORY
    if not nodes_dir.is_dir():
        return
    for link in nodes_dir.rglob(Path(process_name).name):
        try:
            if link.is_symlink() or link.is_file():
                link.unlink()
        except OSError:
            continue


def _job_star_type_label(job_star: Path) -> Optional[str]:
    import starfile

    try:
        tables = starfile.read(job_star, always_dict=True)
    except (OSError, ValueError):
        return None
    table = tables.get(job_artifacts.JOB_TABLE)
    if table is None or len(table) == 0:
        return None
    return str(table.iloc[0].get(job_artifacts.COL_TYPE_LABEL, ''))


def _read_deleted_pipeline(directory: Path):
    """Read ``<job>deleted_pipeline.star`` if the job carries one."""
    candidates = list(Path(directory).glob('*deleted_pipeline.star'))
    if not candidates:
        return None
    try:
        return pipeline_module.read_pipeline_file(candidates[0])
    except (OSError, ValueError):
        return None


def _output_nodes_from_job(
    directory: Path, process_name: str
) -> List[pipeline_module.Node]:
    """Read ``RELION_OUTPUT_NODES.star`` back into pipeline nodes."""
    path = directory / job_artifacts.OUTPUT_NODES_FILENAME
    if not path.is_file():
        return []
    import starfile

    try:
        tables = starfile.read(path, always_dict=True)
    except (OSError, ValueError):
        return []
    table = tables.get('pipeline_nodes')
    if table is None:
        return []
    nodes = []
    for _, row in table.iterrows():
        name = str(row.get(job_artifacts.OUTPUT_NODE_NAME_COLUMN, '') or '')
        label = str(row.get(job_artifacts.OUTPUT_NODE_TYPE_COLUMN, '') or '')
        if name and label:
            nodes.append(pipeline_module.Node(name, label))
    return nodes


# --------------------------------------------------------------- scheduler
def scheduler_available() -> bool:
    return pipeliner_available()


def run_schedule(
    project: Path,
    process_names: List[str],
    *,
    name: str = 'web',
    repeat: int = 1,
    wait_minutes: int = 0,
    wait_before_minutes: int = 0,
    wait_after_seconds: int = 10,
) -> dict:
    """Launch ``relion_pipeliner --schedule`` for a list of scheduled jobs.

    RELION's scheduler is a blocking loop that runs each job, waits for it,
    repeats, and writes ``pipeline_<name>.log``. Reimplementing it would mean
    copying the dependency checks, the repeat bookkeeping and the
    ``RUNNING_PIPELINER_*`` stop file. It is started detached instead, so the
    web server is not blocked, and its output goes to a log the UI can follow.
    """
    if not process_names:
        raise ActionError('select at least one scheduled job to run')

    project = Path(project)
    pipeline = pipeline_module.read_pipeline(project)
    # Only Scheduled jobs may be run: the scheduler re-runs them in place.
    for name_ in process_names:
        process = pipeline.find_process(name_)
        if process is None:
            raise ActionError(f'{name_} is not in the pipeline')
        if process.status != pipeline_module.STATUS_SCHEDULED:
            raise ActionError(
                f'{name_} is {process.status}; only Scheduled jobs can be run '
                f'by the scheduler'
            )

    executable = shutil.which('relion_pipeliner')
    if executable is None:
        raise ActionError(
            'relion_pipeliner is not on PATH; install RELION or put its bin/ '
            'directory on PATH to use the scheduler'
        )

    log_path = project / f'pipeline_{name}.log'
    args = [
        executable,
        '--RunJobs',
        ' '.join(process_names),
        '--schedule',
        name,
        '--repeat',
        str(max(1, int(repeat))),
        '--min_wait',
        str(max(0, int(wait_minutes))),
        '--min_wait_before',
        str(max(0, int(wait_before_minutes))),
        '--sec_wait_after',
        str(max(0, int(wait_after_seconds))),
    ]
    try:
        with open(log_path, 'ab') as log_handle:
            process = subprocess.Popen(
                args,
                cwd=str(project),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        raise ActionError(f'could not start the scheduler: {exc}') from exc

    return {
        'pid': process.pid,
        'log': str(log_path),
        'jobs': process_names,
        'repeat': repeat,
        'schedule': name,
        'stop_file': str(project / f'RUNNING_PIPELINER_{name}'),
    }


def stop_schedule(project: Path, name: str = 'web') -> dict:
    """Ask a running scheduler to stop, by removing its check file.

    ``runScheduledJobs`` polls for ``RUNNING_PIPELINER_<name>`` and stops when it
    disappears, which is how the GUI's "Stop running scheduled jobs" works.
    """
    marker = Path(project) / f'RUNNING_PIPELINER_{name}'
    if not marker.exists():
        raise ActionError(
            f'no scheduler named {name!r} appears to be running '
            f'(no {marker.name})'
        )
    marker.unlink()
    return {'stopped': name, 'marker': str(marker)}


def schedule_status(project: Path, name: str = 'web') -> dict:
    project = Path(project)
    marker = project / f'RUNNING_PIPELINER_{name}'
    log = project / f'pipeline_{name}.log'
    tail = ''
    if log.is_file():
        data = log.read_bytes()
        tail = data[-4000:].decode('utf-8', errors='replace')
    return {
        'schedule': name,
        'running': marker.exists(),
        'marker': str(marker),
        'log': str(log),
        'tail': tail,
    }


# ------------------------------------------------------- export / import
EXPORT_DIRECTORY = 'ExportJobs'


def export_jobs(project: Path, process_names: List[str], archive_name: str) -> dict:
    """Copy scheduled jobs out of the project so another one can import them.

    RELION's own export works from ``run.job``, ``note.txt`` and the
    mini-pipeline, rewriting the job names to ``<Type>/expNNN`` so the receiving
    project can renumber them (``PipeLine::exportAllScheduledJobs``). The web
    side writes ``job.star`` instead of ``run.job``, and RELION prefers
    ``job.star`` on read, so that is what gets copied and rewritten.

    Only scheduled jobs are exported: a finished job's outputs are not part of
    the copy, so importing one elsewhere would leave dangling nodes.
    """
    project = Path(project)
    if not process_names:
        raise ActionError('select at least one scheduled job to export')
    if not archive_name.strip():
        raise ActionError('give the export a name')
    safe_name = _safe_archive_name(archive_name)

    pipeline = pipeline_module.read_pipeline(project)
    for name in process_names:
        process = pipeline.find_process(name)
        if process is None:
            raise ActionError(f'{name} is not in the pipeline')
        if process.status != pipeline_module.STATUS_SCHEDULED:
            raise ActionError(
                f'{name} is {process.status}; only scheduled jobs can be exported'
            )
        if process.alias not in (None, 'None', ''):
            raise ActionError(
                f'{name} has an alias; RELION does not export aliased scheduled jobs'
            )

    export_root = project / EXPORT_DIRECTORY / safe_name
    if export_root.exists():
        shutil.rmtree(export_root)
    export_root.mkdir(parents=True)

    # <Type>/jobNNN -> <Type>/expNNN, the pattern RELION uses.
    replacements = {}
    entries = []
    for index, name in enumerate(process_names, start=1):
        type_dir = pipeline.directory_of(name).rstrip('/')
        exported = f'{type_dir}/exp{index:03d}/'
        replacements[name] = exported
        entries.append(exported)
        _copy_job_for_export(project, name, export_root, exported, replacements)

    # exported.star lists what the archive holds, as RELION writes it.
    listing = export_root / 'exported.star'
    lines = ['', '# version 50001', '', 'data_pipeline_processes', '', 'loop_',
             '_rlnPipeLineProcessName #1']
    lines.extend(entry.rstrip('/') for entry in entries)
    lines.append('')
    listing.write_text('\n'.join(lines), encoding='utf-8')

    return {
        'archive': str(export_root),
        'listing': str(listing),
        'jobs': process_names,
        'entries': entries,
    }


def import_jobs(project: Path, listing_path: Path) -> dict:
    """Register the jobs in an exported listing, renumbering them here.

    The receiving project assigns new job numbers from its own counter, and the
    paths inside the copied files are rewritten to match, which is what
    ``PipeLine::importJobs`` does.
    """
    project = Path(project)
    listing_path = Path(listing_path)
    if not listing_path.is_file():
        raise ActionError(f'no export listing at {listing_path}')

    listing = _read_export_listing(listing_path)
    if not listing:
        raise ActionError(f'{listing_path} lists no jobs')

    pipeline = pipeline_module.read_pipeline(project)
    export_root = listing_path.parent
    imported = []
    for entry in listing:
        source = export_root / entry
        if not source.is_dir():
            raise ActionError(f'{entry} is listed but not present in {export_root}')
        job_star = source / 'job.star'
        if not job_star.is_file():
            raise ActionError(f'{entry} has no job.star')

        number = pipeline.next_job_number()
        type_dir = entry.split('/', 1)[0]
        target_name = f'{type_dir}/job{number:03d}'
        target = project / target_name
        if target.exists():
            raise ActionError(f'{target} already exists')

        shutil.copytree(source, target)
        # Rewrite the exported name inside the copied metadata to the new one.
        _rewrite_paths(target, entry, target_name)

        type_label = _job_star_type_label(target / 'job.star') or ''
        output_nodes = _output_nodes_from_job(target, target_name)
        process = pipeline_module.Process(
            name=target_name,
            alias='None',
            type_label=type_label,
            status=pipeline_module.STATUS_SCHEDULED,
        )
        pipeline_module.register_process(
            pipeline, process, output_nodes=output_nodes
        )
        imported.append(target_name)

    pipeline_module.write_pipeline(project, pipeline)
    return {'imported': imported, 'listing': str(listing_path)}


def _safe_archive_name(name: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', name.strip())
    if not cleaned:
        raise ActionError('the export name has no usable characters')
    return cleaned


def _copy_job_for_export(
    project: Path,
    process_name: str,
    export_root: Path,
    exported_name: str,
    replacements: Dict[str, str],
) -> None:
    """Copy the metadata files a scheduled job needs, with paths rewritten."""
    source = project / process_name
    target = export_root / exported_name
    target.mkdir(parents=True, exist_ok=True)
    for filename in ('job.star', 'note.txt', 'RELION_OUTPUT_NODES.star'):
        origin = source / filename
        if not origin.is_file():
            continue
        text = origin.read_text(encoding='utf-8', errors='replace')
        for old, new in replacements.items():
            text = text.replace(old, new)
        (target / filename).write_text(text, encoding='utf-8')
    # The mini-pipeline is named after the job, so it is copied under its
    # exported name.
    for mini in source.glob('*job_pipeline.star'):
        text = mini.read_text(encoding='utf-8', errors='replace')
        for old, new in replacements.items():
            text = text.replace(old, new)
        (target / f'exp{exported_name.rstrip("/").split("exp")[-1]}job_pipeline.star').write_text(
            text, encoding='utf-8'
        )


def _read_export_listing(path: Path) -> List[str]:
    import starfile

    try:
        tables = starfile.read(path, always_dict=True)
    except (OSError, ValueError):
        return []
    table = tables.get('pipeline_processes')
    if table is None:
        return []
    column = pipeline_module.COL_PROCESS_NAME
    if column not in table.columns:
        return []
    return [str(value) for value in table[column] if str(value).strip()]


def _rewrite_paths(directory: Path, old_prefix: str, new_prefix: str) -> None:
    for path in directory.rglob('*'):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if old_prefix in text:
            path.write_text(
                text.replace(old_prefix, new_prefix), encoding='utf-8'
            )
