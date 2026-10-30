"""Read and write RELION's ``default_pipeline.star``.

The desktop GUI never inspects job directories to decide what exists; it reads
the pipeline file, which lists processes (jobs), nodes (data files produced or
consumed) and the edges connecting them. A job submitted from the web UI is
therefore invisible to the official GUI unless it is registered here.

The format is a STAR file with up to five tables:

``pipeline_general``
    One row: ``rlnPipeLineJobCounter``.
``pipeline_processes``
    One row per job: ``rlnPipeLineProcessName`` (e.g. ``Import/job001``),
    ``rlnPipeLineProcessAlias``, ``rlnPipeLineProcessTypeLabel``
    (e.g. ``relion.importtomo``) and ``rlnPipeLineProcessStatusLabel``
    (``Running``/``Scheduled``/``Succeeded``/``Failed``/``Aborted``).
``pipeline_nodes``
    One row per data file: ``rlnPipeLineNodeName``,
    ``rlnPipeLineNodeTypeLabel`` and ``rlnPipeLineNodeTypeLabelDepth``.
``pipeline_input_edges`` / ``pipeline_output_edges``
    ``rlnPipeLineEdgeFromNode`` + ``rlnPipeLineEdgeProcess``, and
    ``rlnPipeLineEdgeProcess`` + ``rlnPipeLineEdgeToNode``.

Writes take the same ``.relion_lock`` directory the GUI uses, so a web
submission and a GUI submission cannot interleave. Note the GUI waits up to a
minute on that lock, so it must be released promptly.
"""

import errno
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import pandas as pd
import starfile

PIPELINE_FILENAME = 'default_pipeline.star'
LOCK_DIRECTORY = '.relion_lock'
# Touched after every pipeline write so a running GUI refreshes its job list.
# The name is fixed by RELION (PIPELINE_HAS_CHANGED in pipeliner.h).
PIPELINE_HAS_CHANGED_FILENAME = '.pipeline_has_changed'

# Status labels accepted by the GUI (procstatus_label2type in pipeline_jobs.h).
STATUS_RUNNING = 'Running'
STATUS_SCHEDULED = 'Scheduled'
STATUS_SUCCEEDED = 'Succeeded'
STATUS_FAILED = 'Failed'
STATUS_ABORTED = 'Aborted'

VALID_STATUSES = (
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_ABORTED,
)

GENERAL_TABLE = 'pipeline_general'
PROCESS_TABLE = 'pipeline_processes'
NODE_TABLE = 'pipeline_nodes'
INPUT_EDGE_TABLE = 'pipeline_input_edges'
OUTPUT_EDGE_TABLE = 'pipeline_output_edges'

COL_JOB_COUNTER = 'rlnPipeLineJobCounter'
COL_PROCESS_NAME = 'rlnPipeLineProcessName'
COL_PROCESS_ALIAS = 'rlnPipeLineProcessAlias'
COL_PROCESS_TYPE = 'rlnPipeLineProcessTypeLabel'
COL_PROCESS_STATUS = 'rlnPipeLineProcessStatusLabel'
COL_NODE_NAME = 'rlnPipeLineNodeName'
COL_NODE_TYPE = 'rlnPipeLineNodeTypeLabel'
COL_NODE_TYPE_DEPTH = 'rlnPipeLineNodeTypeLabelDepth'
COL_EDGE_FROM = 'rlnPipeLineEdgeFromNode'
COL_EDGE_TO = 'rlnPipeLineEdgeToNode'
COL_EDGE_PROCESS = 'rlnPipeLineEdgeProcess'

# The pipeline file the GUI writes carries a version header; RELION 5 uses
# 50001. Tables written without it are still read, but matching avoids a
# conversion pass on the GUI side.
PIPELINE_VERSION = '50001'
NODE_TYPE_DEPTH_DEFAULT = 1


class PipelineLockedError(RuntimeError):
    """The project is locked by another RELION process."""


@dataclass
class Process:
    name: str
    alias: str = 'None'
    type_label: str = ''
    status: str = STATUS_SCHEDULED

    def as_dict(self) -> dict:
        return {
            'name': self.name,
            'alias': self.alias,
            'type_label': self.type_label,
            'status': self.status,
        }


@dataclass
class Node:
    name: str
    type_label: str
    type_depth: int = NODE_TYPE_DEPTH_DEFAULT


@dataclass
class Pipeline:
    """In-memory view of a RELION project pipeline."""

    job_counter: int = 1
    processes: List[Process] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)
    input_edges: List[tuple] = field(default_factory=list)   # (from_node, process)
    output_edges: List[tuple] = field(default_factory=list)  # (process, to_node)

    def next_job_number(self) -> int:
        """Return the number the GUI would use for the next job."""
        return max(1, int(self.job_counter))

    def find_process(self, name: str) -> Optional[Process]:
        for process in self.processes:
            if process.name == name:
                return process
        return None

    def find_node(self, name: str) -> Optional[Node]:
        for node in self.nodes:
            if node.name == name:
                return node
        return None

    def job_number_of(self, process_name: str) -> int:
        """Extract the integer from a name like ``Import/job013``."""
        tail = process_name.rsplit('/', 1)[-1]
        if tail.startswith('job') and tail[3:].isdigit():
            return int(tail[3:])
        return 0

    def directory_of(self, process_name: str) -> str:
        """The job directory prefix, e.g. ``Import/job013`` -> ``Import/``."""
        return process_name.rsplit('/', 1)[0] + '/' if '/' in process_name else ''


# ------------------------------------------------------------------- reading
def pipeline_path(project_directory) -> Path:
    return Path(project_directory) / PIPELINE_FILENAME


def _read_table(star: dict, name: str) -> Optional[pd.DataFrame]:
    """Return one STAR block as a DataFrame, however RELION wrote it.

    RELION's ``MetaDataTable::write`` emits a **single-row** table as bare
    key/column pairs, without ``loop_``:

        data_pipeline_processes
        _rlnPipeLineProcessName      Tomograms/job006
        _rlnPipeLineProcessAlias     None

    ``starfile`` parses that into a plain ``dict`` rather than a DataFrame, so a
    reader that only accepts DataFrame silently loses every table RELION wrote
    from a one-job pipeline - which is most pipelines. Multi-row tables (a
    project with several jobs) do come back as DataFrames, so the failure was
    intermittent and easy to miss.

    A one-row table is normalised to a one-row DataFrame, which is what the rest
    of this module expects.
    """
    table = star.get(name)
    if table is None:
        return None
    if isinstance(table, dict):
        # The `loop_`-less form: {label: value} for a single object.
        return pd.DataFrame([table])
    if isinstance(table, pd.Series):
        # starfile can hand back a single-row table as a Series in some versions.
        return table.to_frame().T
    if isinstance(table, pd.DataFrame):
        return table
    return None


def read_pipeline(project_directory) -> Pipeline:
    """Parse ``default_pipeline.star`` from a project directory."""
    path = pipeline_path(project_directory)
    if not path.is_file():
        raise FileNotFoundError(f'no {PIPELINE_FILENAME} in {project_directory}')
    return read_pipeline_file(path)


def read_pipeline_file(path) -> Pipeline:
    """Parse a pipeline file at an arbitrary path.

    A job's ``<job>deleted_pipeline.star`` has the same five tables as the
    project pipeline, so the same reader applies.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f'no pipeline file at {path}')

    star = starfile.read(path, always_dict=True)
    pipeline = Pipeline()

    general = _read_table(star, GENERAL_TABLE)
    if general is not None and len(general) > 0:
        if COL_JOB_COUNTER in general.columns:
            try:
                pipeline.job_counter = int(float(general.iloc[0][COL_JOB_COUNTER]))
            except (TypeError, ValueError):
                pipeline.job_counter = 1

    processes = _read_table(star, PROCESS_TABLE)
    if processes is not None:
        for _, row in processes.iterrows():
            pipeline.processes.append(
                Process(
                    name=str(row.get(COL_PROCESS_NAME, '')),
                    alias=str(row.get(COL_PROCESS_ALIAS, 'None') or 'None'),
                    type_label=str(row.get(COL_PROCESS_TYPE, '')),
                    status=str(row.get(COL_PROCESS_STATUS, STATUS_SCHEDULED)),
                )
            )

    nodes = _read_table(star, NODE_TABLE)
    if nodes is not None:
        for _, row in nodes.iterrows():
            depth = row.get(COL_NODE_TYPE_DEPTH, NODE_TYPE_DEPTH_DEFAULT)
            try:
                depth = int(float(depth))
            except (TypeError, ValueError):
                depth = NODE_TYPE_DEPTH_DEFAULT
            pipeline.nodes.append(
                Node(
                    name=str(row.get(COL_NODE_NAME, '')),
                    type_label=str(row.get(COL_NODE_TYPE, '')),
                    type_depth=depth,
                )
            )

    inputs = _read_table(star, INPUT_EDGE_TABLE)
    if inputs is not None:
        for _, row in inputs.iterrows():
            pipeline.input_edges.append(
                (str(row.get(COL_EDGE_FROM, '')), str(row.get(COL_EDGE_PROCESS, '')))
            )

    outputs = _read_table(star, OUTPUT_EDGE_TABLE)
    if outputs is not None:
        for _, row in outputs.iterrows():
            pipeline.output_edges.append(
                (str(row.get(COL_EDGE_PROCESS, '')), str(row.get(COL_EDGE_TO, '')))
            )

    return pipeline


# ------------------------------------------------------------------- writing
@contextmanager
def pipeline_lock(project_directory, timeout: float = 20.0):
    """Hold RELION's project lock while writing the pipeline.

    Mirrors ``PipeLine::read(DO_LOCK)``: create ``.relion_lock`` atomically and
    write ``lock_<name>_pipeline.star`` inside it. The GUI aborts after a
    minute, so the timeout here is deliberately shorter.
    """
    project = Path(project_directory)
    lock_dir = project / LOCK_DIRECTORY
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise PipelineLockedError(
                    f'project {project} is locked by another RELION process; '
                    f'if nothing is running, remove {lock_dir}'
                )
            time.sleep(0.2)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EROFS, errno.ENOSPC):
                raise PipelineLockedError(f'cannot lock {project}: {exc}') from exc
            raise

    try:
        (lock_dir / f'lock_{PIPELINE_FILENAME}').write_text(
            'relion_tomo_web is updating the pipeline\n'
        )
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def _write_star(path: Path, tables: Sequence[tuple]) -> None:
    """Write tables in order, as raw starfile blocks.

    ``starfile.write`` builds its own frame set and does not preserve an
    explicit ordering plus the leading version comment that RELION emits, so
    the blocks are rendered directly.
    """
    blocks: List[str] = ['', f'# version {PIPELINE_VERSION}', '']
    for name, frame in tables:
        blocks.append(f'data_{name}')
        blocks.append('')
        blocks.append('loop_')
        for index, column in enumerate(frame.columns, start=1):
            blocks.append(f'_{column} #{index}')
        for _, row in frame.iterrows():
            blocks.append(' '.join(_format_value(row[column]) for column in frame.columns))
        blocks.append('')
    path.write_text('\n'.join(blocks), encoding='utf-8')


def _format_value(value) -> str:
    if value is None:
        return 'None'
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, float):
        if value != value:  # NaN
            return 'nan'
        return f'{value:.6f}'
    if isinstance(value, int):
        return str(value)
    text = str(value)
    # STAR treats whitespace as a delimiter.
    if text == '' or any(character.isspace() for character in text):
        return '"' + text.replace('"', '') + '"'
    return text


def pipeline_tables(pipeline: Pipeline) -> Sequence[tuple]:
    """The five STAR tables that describe a pipeline, in RELION's order."""
    general = pd.DataFrame({COL_JOB_COUNTER: [pipeline.job_counter]})
    tables = [(GENERAL_TABLE, general)]

    processes = pd.DataFrame(
        [
            {
                COL_PROCESS_NAME: process.name,
                COL_PROCESS_ALIAS: process.alias,
                COL_PROCESS_TYPE: process.type_label,
                COL_PROCESS_STATUS: process.status,
            }
            for process in pipeline.processes
        ],
        columns=[
            COL_PROCESS_NAME,
            COL_PROCESS_ALIAS,
            COL_PROCESS_TYPE,
            COL_PROCESS_STATUS,
        ],
    )
    tables.append((PROCESS_TABLE, processes))

    nodes = pd.DataFrame(
        [
            {
                COL_NODE_NAME: node.name,
                COL_NODE_TYPE: node.type_label,
                COL_NODE_TYPE_DEPTH: node.type_depth,
            }
            for node in pipeline.nodes
        ],
        columns=[COL_NODE_NAME, COL_NODE_TYPE, COL_NODE_TYPE_DEPTH],
    )
    tables.append((NODE_TABLE, nodes))

    inputs = pd.DataFrame(
        [{COL_EDGE_FROM: source, COL_EDGE_PROCESS: process} for source, process in pipeline.input_edges],
        columns=[COL_EDGE_FROM, COL_EDGE_PROCESS],
    )
    tables.append((INPUT_EDGE_TABLE, inputs))

    outputs = pd.DataFrame(
        [{COL_EDGE_PROCESS: process, COL_EDGE_TO: target} for process, target in pipeline.output_edges],
        columns=[COL_EDGE_PROCESS, COL_EDGE_TO],
    )
    tables.append((OUTPUT_EDGE_TABLE, outputs))
    return tables


def write_pipeline_file(path, pipeline: Pipeline) -> Path:
    """Write a pipeline to an arbitrary path, without the project lock.

    Used for the ``<job>deleted_pipeline.star`` snapshot, which lives inside a
    job directory rather than at the project root.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_star(path, pipeline_tables(pipeline))
    return path


def write_pipeline(project_directory, pipeline: Pipeline) -> Path:
    """Write ``default_pipeline.star``, taking the project lock."""
    project = Path(project_directory)
    path = pipeline_path(project)

    tables = pipeline_tables(pipeline)

    with pipeline_lock(project):
        _write_star(path, tables)

    # RELION touches this after releasing the lock, and the GUI watches for it
    # to know the pipeline changed externally (gui_mainwindow.cpp:865). Without
    # it the GUI keeps showing the old job list until its own poll comes round.
    changed_marker = project / PIPELINE_HAS_CHANGED_FILENAME
    try:
        changed_marker.touch()
    except OSError:
        pass
    return path


def write_mini_pipeline(
    job_directory,
    process: Process,
    input_nodes: Sequence[Node] = (),
    output_nodes: Sequence[Node] = (),
) -> Optional[Path]:
    """Write the ``job_pipeline.star`` RELION keeps inside each job directory.

    ``PipeLine::addJob`` builds this from the single job plus its nodes and
    writes it as ``<job name>job_pipeline.star``; the GUI offers it as a
    pipeline-STAR filter and uses it when importing/exporting scheduled jobs.
    The edge tables are filled the same way RELION fills them: process index 0
    is the only process.
    """
    job_directory = Path(job_directory)
    if not job_directory.is_dir():
        return None
    name = process.name.rsplit('/', 1)[-1]
    path = job_directory / f'{name}job_pipeline.star'

    general = pd.DataFrame({COL_JOB_COUNTER: [1]})
    processes = pd.DataFrame(
        [
            {
                COL_PROCESS_NAME: process.name,
                COL_PROCESS_ALIAS: process.alias,
                COL_PROCESS_TYPE: process.type_label,
                COL_PROCESS_STATUS: process.status,
            }
        ]
    )
    nodes = [*input_nodes, *output_nodes]
    node_frame = pd.DataFrame(
        [
            {
                COL_NODE_NAME: node.name,
                COL_NODE_TYPE: node.type_label,
                COL_NODE_TYPE_DEPTH: node.type_depth,
            }
            for node in nodes
        ],
        columns=[COL_NODE_NAME, COL_NODE_TYPE, COL_NODE_TYPE_DEPTH],
    )
    tables = [
        (GENERAL_TABLE, general),
        (PROCESS_TABLE, processes),
        (NODE_TABLE, node_frame),
        (
            INPUT_EDGE_TABLE,
            pd.DataFrame(
                [{COL_EDGE_FROM: node.name, COL_EDGE_PROCESS: process.name} for node in input_nodes],
                columns=[COL_EDGE_FROM, COL_EDGE_PROCESS],
            ),
        ),
        (
            OUTPUT_EDGE_TABLE,
            pd.DataFrame(
                [{COL_EDGE_PROCESS: process.name, COL_EDGE_TO: node.name} for node in output_nodes],
                columns=[COL_EDGE_PROCESS, COL_EDGE_TO],
            ),
        ),
    ]
    try:
        _write_star(path, tables)
    except OSError:
        return None
    return path


# ------------------------------------------------------------- modification
def register_process(
    pipeline: Pipeline,
    process: Process,
    input_nodes: Iterable[Node] = (),
    output_nodes: Iterable[Node] = (),
) -> Pipeline:
    """Add a process, its nodes and edges, following GUI conventions.

    ``job_counter`` is advanced past the new job so the next job created in the
    GUI does not collide with this one.
    """
    if pipeline.find_process(process.name) is not None:
        raise ValueError(f'process {process.name} already exists in the pipeline')

    pipeline.processes.append(process)

    for node in input_nodes:
        existing = pipeline.find_node(node.name)
        if existing is None:
            pipeline.nodes.append(node)
        pipeline.input_edges.append((node.name, process.name))

    for node in output_nodes:
        existing = pipeline.find_node(node.name)
        if existing is None:
            pipeline.nodes.append(node)
        pipeline.output_edges.append((process.name, node.name))

    number = pipeline.job_number_of(process.name)
    if number >= pipeline.job_counter:
        pipeline.job_counter = number + 1
    return pipeline


def update_process_status(
    pipeline: Pipeline, process_name: str, status: str
) -> bool:
    """Set a process status; returns False when the process is unknown."""
    if status not in VALID_STATUSES:
        raise ValueError(f'invalid status {status!r}; expected one of {VALID_STATUSES}')
    process = pipeline.find_process(process_name)
    if process is None:
        return False
    process.status = status
    return True


def promote_process(
    pipeline: Pipeline,
    process: Process,
    input_nodes: Iterable[Node] = (),
    output_nodes: Optional[Iterable[Node]] = None,
) -> Pipeline:
    """Fill in a process that is already in the pipeline.

    This is the second half of the scheduled-job model. A job queued ahead of
    time is registered as ``Scheduled`` with its predicted outputs
    (:func:`reserve_scheduled`); when it actually starts, its status changes and
    its *real* input edges are added.

    ``output_nodes=None`` keeps the outputs reserved earlier, which is the
    normal case: they are the same names, so a downstream job that already named
    them as its input keeps working. Pass a sequence to replace them.

    Input edges are rebuilt rather than appended, so promoting twice cannot
    leave the same edge in the pipeline twice.
    """
    index = next(
        (i for i, item in enumerate(pipeline.processes) if item.name == process.name),
        None,
    )
    if index is None:
        return register_process(
            pipeline,
            process,
            input_nodes=input_nodes,
            output_nodes=output_nodes or (),
        )

    pipeline.processes[index] = process
    pipeline.input_edges = [
        (node, consumer)
        for node, consumer in pipeline.input_edges
        if consumer != process.name
    ]
    for node in input_nodes:
        if pipeline.find_node(node.name) is None:
            pipeline.nodes.append(node)
        pipeline.input_edges.append((node.name, process.name))

    if output_nodes is not None:
        pipeline.output_edges = [
            (producer, node)
            for producer, node in pipeline.output_edges
            if producer != process.name
        ]
        for node in output_nodes:
            if pipeline.find_node(node.name) is None:
                pipeline.nodes.append(node)
            pipeline.output_edges.append((process.name, node.name))
    return pipeline


def update_job_counter(pipeline: Pipeline, value: int) -> None:
    pipeline.job_counter = max(int(value), 1)


@dataclass
class Reservation:
    """One job to place in the pipeline before it runs."""

    # The RELION directory for this job type, e.g. ``Tomograms``. The number is
    # assigned here, so the resulting process name is ``Tomograms/job013``.
    directory: str
    type_label: str
    # ``(filename, type_label)`` pairs, relative to the job directory.
    outputs: Sequence[tuple] = ()


def reserve_scheduled(
    project_directory, reservations: Sequence[Reservation]
) -> List[str]:
    """Place unstarted jobs in the pipeline as Scheduled, naming their outputs.

    This is RELION's own model for a job that exists but has not run:
    ``PipeLine::addJob(job, PROC_SCHEDULED)`` adds the process *and its output
    nodes* to the pipeline (pipeliner.cpp:481), and ``touchTemporaryNodeFile``
    creates the ``.Nodes`` placeholder even though the file does not exist yet
    (pipeliner.cpp:120, :207). A downstream job may then name that node as its
    input, which is what building a chain ahead of time requires.

    Doing it this way rather than inventing a web-only table is the whole point:
    the desktop GUI shows these jobs as Scheduled and can run, edit or delete
    them exactly like ones it created itself.

    The whole list is allocated in one read-modify-write under the project lock,
    because two concurrent enqueues must not be handed the same number, and the
    GUI may be writing the pipeline at the same moment. Returns the assigned
    process names, in the order given.
    """
    if not reservations:
        return []
    project = Path(project_directory)
    assigned: List[str] = []
    with pipeline_lock(project):
        pipeline = read_pipeline(project)
        for reservation in reservations:
            # Each job takes the next number, so a batch queued in one call is
            # numbered in the order it was given - which is the order the caller
            # declared its dependencies in.
            number = pipeline.next_job_number()
            process_name = f'{reservation.directory}/job{number:03d}'
            register_process(
                pipeline,
                Process(
                    name=process_name,
                    type_label=reservation.type_label,
                    status=STATUS_SCHEDULED,
                ),
                output_nodes=[
                    Node(f'{process_name}/{filename}', label)
                    for filename, label in reservation.outputs
                ],
            )
            assigned.append(process_name)
        _write_star(pipeline_path(project), pipeline_tables(pipeline))
    marker = project / PIPELINE_HAS_CHANGED_FILENAME
    try:
        marker.touch()
    except OSError:
        pass
    return assigned


def read_job_counter(project_directory) -> int:
    """The job counter the GUI would use next, defaulting to 1."""
    try:
        return read_pipeline(project_directory).job_counter
    except (FileNotFoundError, ValueError):
        return 1
