"""Produce the on-disk artefacts a RELION job directory needs.

For the official GUI to open a job created elsewhere, the directory must contain
more than the outputs:

``job.star``
    A ``data_job`` table with the type label, plus a ``data_joboptions_values``
    table listing every option. The GUI reads this to populate the job window.
``RELION_OUTPUT_NODES.star``
    Which nodes the job produced, used when the GUI re-checks completion.
``.Nodes/<Type>/...``
    Symlinks in the project's node directory so downstream jobs can pick the
    outputs up in the GUI browser.

``run.job`` is the legacy line-based format; RELION 4+ writes ``job.star``, and
the reader prefers ``run.job`` only when it exists. Writing ``job.star`` alone is
therefore correct for RELION 5.
"""

from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

JOB_TABLE = 'job'
JOB_OPTIONS_TABLE = 'joboptions_values'
COL_TYPE_LABEL = 'rlnJobTypeLabel'
COL_IS_CONTINUE = 'rlnJobIsContinue'
COL_IS_TOMO = 'rlnJobIsTomo'
COL_OPTION_VARIABLE = 'rlnJobOptionVariable'
COL_OPTION_VALUE = 'rlnJobOptionValue'

JOB_STAR_FILENAME = 'job.star'
OUTPUT_NODES_FILENAME = 'RELION_OUTPUT_NODES.star'
# The GUI builds the note path as `<process name>note.txt` (gui_mainwindow.cpp:2161)
# and a process name carries no trailing slash, so the note for
# `Tomograms/job001` is `Tomograms/job001note.txt` - beside the job directory,
# not inside it. Writing `job001/note.txt` produced a note the GUI never opened.
NOTES_SUFFIX = 'note.txt'
NODES_DIRECTORY = '.Nodes'

# RELION reads the output-node table as `output_nodes`
# (getOutputNodesFromStarFile, pipeliner.cpp:1157). A different table name was
# silently ignored, so the GUI never learned which nodes a web job created.
OUTPUT_NODES_TABLE = 'output_nodes'

OUTPUT_NODE_NAME_COLUMN = 'rlnPipeLineNodeName'
OUTPUT_NODE_TYPE_COLUMN = 'rlnPipeLineNodeTypeLabel'


def write_job_star(
    job_directory: Path,
    type_label: str,
    options: Dict[str, object],
    *,
    is_continue: bool = False,
    is_tomo: bool = True,
) -> Path:
    """Write ``job.star`` describing this job to the GUI.

    ``type_label`` must be a value the GUI recognises, e.g.
    ``relion.reconstructtomograms``. Unknown labels make the GUI report an
    unrecognised process type, so callers should use the labels in
    ``PROCESS_TYPE_LABELS``.
    """
    job_directory = Path(job_directory)
    job_directory.mkdir(parents=True, exist_ok=True)
    path = job_directory / JOB_STAR_FILENAME

    header = pd.DataFrame(
        {
            COL_TYPE_LABEL: [type_label],
            COL_IS_CONTINUE: [1 if is_continue else 0],
            COL_IS_TOMO: [1 if is_tomo else 0],
        }
    )
    values = pd.DataFrame(
        {
            COL_OPTION_VARIABLE: list(options.keys()),
            COL_OPTION_VALUE: [_render(value) for value in options.values()],
        }
    )

    blocks = [
        '',
        '# version 50001',
        '',
        f'data_{JOB_TABLE}',
        '',
        'loop_',
        *[
            f'_{column} #{index}'
            for index, column in enumerate(header.columns, start=1)
        ],
        ' '.join(str(header.iloc[0][column]) for column in header.columns),
        '',
        f'data_{JOB_OPTIONS_TABLE}',
        '',
        'loop_',
        *[
            f'_{column} #{index}'
            for index, column in enumerate(values.columns, start=1)
        ],
        *[
            f'{row[COL_OPTION_VARIABLE]} {_quote(str(row[COL_OPTION_VALUE]))}'
            for _, row in values.iterrows()
        ],
        '',
    ]
    path.write_text('\n'.join(blocks), encoding='utf-8')
    return path


def read_job_star(job_directory) -> Optional[Dict[str, str]]:
    """Read the options a job recorded, as ``{variable: value}``.

    This is the inverse of :func:`write_job_star`. ``job.star`` is the only place
    a finished job's options survive, so it is what the UI must show for a job
    that was not started in this browser session - showing the schema's defaults
    instead would look like a record and be wrong.

    Values come back as the strings RELION wrote, because that is what they are:
    ``Yes``/``No`` for booleans, a number or a path otherwise. Callers compare
    them against the schema's defaults to tell custom from default.

    Returns ``None`` when there is no job.star, which is not an error: a job
    from RELION 3.x has only ``run.job``.
    """
    import starfile

    path = Path(job_directory) / JOB_STAR_FILENAME
    if not path.is_file():
        return None
    try:
        tables = starfile.read(path, always_dict=True)
    except (OSError, ValueError):
        return None
    table = tables.get(JOB_OPTIONS_TABLE)
    if table is None or len(table) == 0:
        return {}
    if COL_OPTION_VARIABLE not in table.columns or COL_OPTION_VALUE not in table.columns:
        return None
    return {
        str(row[COL_OPTION_VARIABLE]): str(row[COL_OPTION_VALUE])
        for _, row in table.iterrows()
        if pd.notna(row[COL_OPTION_VARIABLE])
    }


def read_job_type_label(job_directory) -> Optional[str]:
    """The pipeline type label recorded in job.star, or None."""
    import starfile

    path = Path(job_directory) / JOB_STAR_FILENAME
    if not path.is_file():
        return None
    try:
        tables = starfile.read(path, always_dict=True)
    except (OSError, ValueError):
        return None
    table = tables.get(JOB_TABLE)
    if table is None or len(table) == 0:
        return None
    label = table.iloc[0].get(COL_TYPE_LABEL)
    return None if pd.isna(label) else str(label)


RUN_OUT_FILENAME = 'run.out'
# A job's log is only ever read to find out what happened near the end - the
# error, the last iteration, the crash. A tomogram refinement writes tens of
# megabytes of progress lines, so the browser gets the tail; the full file stays
# on disk and the truncation is stated rather than silent.
LOG_TAIL_BYTES = 256 * 1024


def read_run_out(job_directory) -> Optional[str]:
    """The tail of a job's ``run.out``, or None if the job wrote none.

    RELION redirects a job's stdout and stderr here, so it is the only log a
    finished job has: the in-memory job object is gone once the browser
    reloads. Reading from disk is what makes the Log tab useful for every job in
    the pipeline rather than only the one just started.

    Decoded with ``errors='replace'`` on purpose: a job killed mid-write leaves
    a partial UTF-8 sequence, and raising UnicodeDecodeError would turn "the job
    crashed" into "the log cannot be shown" - the wrong message at the wrong
    moment.
    """
    path = Path(job_directory) / RUN_OUT_FILENAME
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
        with path.open('rb') as handle:
            if size > LOG_TAIL_BYTES:
                handle.seek(size - LOG_TAIL_BYTES)
                prefix = f'[... {size - LOG_TAIL_BYTES} bytes omitted ...]\n'
            else:
                prefix = ''
            raw = handle.read()
    except OSError:
        return None
    return prefix + raw.decode('utf-8', errors='replace')


def _render(value) -> str:
    """Serialise an option value the way ``JobOption`` does.

    Booleans are the important case: RELION stores them as ``Yes``/``No``
    (``JobOption::JobOption(label, bool, help)``), and ``getBoolean`` compares
    the string against ``"Yes"``. Writing ``true``/``false`` here would make the
    GUI read every flag as false.
    """
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    return str(value)


def _quote(text: str) -> str:
    if text == '' or any(character.isspace() for character in text):
        return '"' + text.replace('"', '') + '"'
    return text


def write_output_nodes(
    job_directory: Path, nodes: Sequence[tuple]
) -> Optional[Path]:
    """Record which nodes a job created, as ``RELION_OUTPUT_NODES.star``.

    ``nodes`` is a sequence of ``(name, type_label)`` pairs. Returns None when
    there are no outputs, since the GUI treats a missing file as "no nodes".
    """
    job_directory = Path(job_directory)
    if not nodes:
        return None
    job_directory.mkdir(parents=True, exist_ok=True)
    path = job_directory / OUTPUT_NODES_FILENAME

    frame = pd.DataFrame(
        {
            OUTPUT_NODE_NAME_COLUMN: [name for name, _ in nodes],
            OUTPUT_NODE_TYPE_COLUMN: [label for _, label in nodes],
        }
    )
    blocks = [
        '',
        '# version 50001',
        '',
        f'data_{OUTPUT_NODES_TABLE}',
        '',
        'loop_',
        *[
            f'_{column} #{index}'
            for index, column in enumerate(frame.columns, start=1)
        ],
        *[
            f'{row[OUTPUT_NODE_NAME_COLUMN]} {row[OUTPUT_NODE_TYPE_COLUMN]}'
            for _, row in frame.iterrows()
        ],
        '',
    ]
    path.write_text('\n'.join(blocks), encoding='utf-8')
    return path


def note_path(job_directory: Path) -> Path:
    """`<jobdir>/<jobname>note.txt`, the path the GUI's note editor opens."""
    job_directory = Path(job_directory)
    return job_directory.parent / f'{job_directory.name}{NOTES_SUFFIX}'


def write_notes(job_directory: Path, type_label: str, command: Sequence[str]) -> Path:
    """Write the note where the GUI looks for it."""
    job_directory = Path(job_directory)
    job_directory.mkdir(parents=True, exist_ok=True)
    path = note_path(job_directory)
    lines = [
        f'Created by relion_tomo_web ({type_label})',
        '',
        'command:',
        ' '.join(command),
        '',
    ]
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def node_directory_name(
    node_type_label: str,
    node_name: str,
    alias: str = 'None',
    type_depth: int = 1,
) -> str:
    """Path of the node's entry inside ``.Nodes``.

    The GUI groups entries by the first component of the type label and, when a
    process has an alias, names the entry after the matching path suffix
    (``PipeLine::touchTemporaryNodeFile``, pipeliner.cpp:207).
    """
    type_directory = node_type_label.split('.')[0]
    if alias and alias != 'None':
        alias = alias.rstrip('/')
        suffix = node_name.split('/', 1)[1] if '/' in node_name else node_name
        return f'{type_directory}/{alias}/{suffix}'
    return f'{type_directory}/{node_name}'


def deeper_node_directory_name(
    node_type_label: str, node_name: str, alias: str, type_depth: int
) -> str:
    """The second path RELION creates when ``type_depth > 1``.

    A node can be published under a deeper type prefix as well, e.g.
    ``TomogramGroupMetadata.star`` and
    ``TomogramGroupMetadata.star.relion`` both get an entry, so the GUI's input
    browser offers the same file at both levels.
    """
    parts = node_type_label.split('.')
    deeper = '.'.join(parts[: max(1, type_depth)])
    return node_directory_name(deeper, node_name, alias)


def create_node_entry(
    project_directory: Path,
    node_name: str,
    node_type_label: str,
    alias: str = 'None',
    type_depth: int = 1,
    even_if_missing: bool = False,
) -> Optional[Path]:
    """Create the ``.Nodes`` entry the GUI browses for input files.

    RELION does not symlink here: ``touchTemporaryNodeFile`` creates an *empty
    file* whose name is the node's project-relative path, and the GUI lists that
    directory to offer inputs. A symlink also lists, but a dangling one is
    removed by tools like ``rsync`` and ``git clean``, and the extra path for
    ``type_depth > 1`` was missing entirely.

    ``even_if_missing`` mirrors RELION's behaviour for scheduled jobs, which are
    listed before their outputs exist.
    """
    project = Path(project_directory)
    target = project / node_name
    if not target.exists() and not even_if_missing:
        return None

    created = None
    levels = [type_depth] if type_depth <= 1 else [type_depth]
    for depth in levels:
        relative = (
            node_directory_name(node_type_label, node_name, alias)
            if depth <= 1
            else deeper_node_directory_name(node_type_label, node_name, alias, depth)
        )
        entry = project / NODES_DIRECTORY / relative
        try:
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.touch(exist_ok=True)
        except OSError:
            continue
        created = created or entry
    return created


def create_node_symlink(
    project_directory: Path,
    node_name: str,
    node_type_label: str,
    alias: str = 'None',
    type_depth: int = 1,
) -> Optional[Path]:
    """Backwards-compatible name for :func:`create_node_entry`."""
    return create_node_entry(
        project_directory, node_name, node_type_label, alias, type_depth
    )


def node_candidates(
    project_directory: Path, node_type: str, type_depth: int = 1
) -> List[str]:
    """The node names a value of type ``node_type`` could be filled with.

    This is RELION's own rule, not an approximation of it. The desktop GUI's
    picker for a node-typed option opens a file chooser rooted in
    ``.Nodes/<first type component>/`` (``GuiEntry::cb_browse_node_i``,
    gui_entries.cpp:347) and lists whatever is there. So "which files may go in
    this slot" has exactly one answer: the entries in that directory.

    Returning the names rather than paths is deliberate - a node name is what a
    RELION option holds, and what the queue's slot syntax refers to.

    ``type_depth`` mirrors the field's own: a node published under a deeper type
    prefix lives in a deeper directory, and the chooser is rooted there instead.
    """
    project = Path(project_directory)
    parts = node_type.split('.')
    if not node_type:
        return []
    if type_depth <= 1:
        relative = parts[0]
    else:
        relative = '.'.join(parts[:max(1, min(type_depth, len(parts)))])
    root = project / NODES_DIRECTORY / relative
    if not root.is_dir():
        return []

    names: List[str] = []
    for entry in sorted(root.rglob('*')):
        if not entry.is_file() and not entry.is_symlink():
            continue
        # The entry's path below the type directory *is* the node name, unless an
        # alias was used, in which case the first component is the alias and the
        # job name has to be recovered from the pipeline - handled by the caller
        # that has the pipeline in hand.
        names.append(str(entry.relative_to(root)))
    return names


def remove_node_entry(
    project_directory: Path, node_name: str, node_type_label: str, alias: str = 'None'
) -> None:
    """Drop a node's ``.Nodes`` entry, as ``deleteTemporaryNodeFile`` does."""
    project = Path(project_directory)
    relative = node_directory_name(node_type_label, node_name, alias)
    entry = project / NODES_DIRECTORY / relative
    try:
        if entry.is_file() or entry.is_symlink():
            entry.unlink()
        # RELION also removes the now-empty type directory.
        if entry.parent.is_dir() and not any(entry.parent.iterdir()):
            entry.parent.rmdir()
    except OSError:
        return


def remap_job_options(
    options: Dict[str, object], overrides: Dict[str, str]
) -> Dict[str, object]:
    """Rename web-style option keys to the GUI's option names.

    The web API uses CLI flag names; ``job.star`` stores the GUI's variable
    names. ``overrides`` maps one to the other, and unmapped keys pass through
    unchanged so unknown options still round-trip.
    """
    remapped: Dict[str, object] = {}
    for key, value in options.items():
        remapped[overrides.get(key, key)] = value
    return remapped
