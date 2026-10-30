"""The two things this package used to import from RELION's Python tree.

The agent is meant to run on any machine that has RELION *installed* - meaning
the binaries on ``PATH`` - without also installing RELION's Python package. Only
two pieces were needed from it:

* four sentinel filenames, which are a protocol between the agent and the
  programs it starts, not logic;
* the writer for a filtered tilt-series set, which has to produce exactly the
  layout the desktop excluder does (a global STAR file plus one file per
  tilt-series under ``tilt_series/``).

Both are small and stable enough to carry here. Keeping them local is what lets
``pip install webui`` work on a machine where ``relion_refine`` exists but
``tomography_python_programs`` does not.
"""

from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import pandas as pd
import starfile

# The sentinel protocol. A RELION job writes exactly these names into its
# ``--pipeline_control`` directory, and cancellation works by touching
# ABORT_JOB_NOW; the agent must use the same strings to look native.
JOB_SUCCESS_FILENAME = 'RELION_JOB_EXIT_SUCCESS'
JOB_FAILURE_FILENAME = 'RELION_JOB_EXIT_FAILURE'
ABORT_JOB_NOW_FILENAME = 'RELION_JOB_ABORT_NOW'
JOB_ABORTED_FILENAME = 'RELION_JOB_ABORTED'

GLOBAL_TABLE = 'global'
TILT_SERIES_COLUMN = 'rlnTomoTiltSeriesStarFile'
NAME_COLUMN = 'rlnTomoName'
TILT_SERIES_DIRECTORY = 'tilt_series'


def read_tilt_series_set(star_file: Path) -> Tuple[pd.DataFrame, List[Tuple[str, pd.DataFrame]]]:
    """``(global table, [(name, per-tilt-series table), ...])``.

    Read as strings where it matters: ``rlnTomoName`` values are names, and
    starfile's default number sniffing would turn ``TS_01`` into something else
    if a project ever used numeric names.
    """
    tables = starfile.read(star_file, always_dict=True, parse_as_string=[NAME_COLUMN])
    global_df = tables[GLOBAL_TABLE].set_index(NAME_COLUMN, drop=False)
    series: List[Tuple[str, pd.DataFrame]] = []
    for star_name in global_df[TILT_SERIES_COLUMN]:
        tilt_series = starfile.read(
            Path(star_name), parse_as_string=[NAME_COLUMN]
        )
        if isinstance(tilt_series, dict):
            # A ``loop_``-less table comes back as a bare dict; starfile does
            # this for a one-object table.
            name = next(iter(tilt_series))
            data = pd.DataFrame([tilt_series[name]])
        else:
            name = Path(star_name).stem
            data = tilt_series
        series.append((name, data))
    return global_df, series


def write_tilt_series_set(
    global_df: pd.DataFrame,
    series: Iterable[Tuple[str, pd.DataFrame]],
    output_star_file: Path,
) -> Path:
    """Write a set the way the desktop excluder does.

    The global table keeps one row per tilt-series and points at a per-series
    file under ``tilt_series/``. A tilt-series whose table ended up empty is
    dropped from the global table rather than written as an empty file, which is
    what the desktop tool does when every image of a series is deselected.
    """
    output_star_file = Path(output_star_file)
    output_star_file.parent.mkdir(parents=True, exist_ok=True)
    series_directory = output_star_file.parent / TILT_SERIES_DIRECTORY
    series_directory.mkdir(parents=True, exist_ok=True)

    table = global_df.copy()
    paths: Dict[str, Path] = {}
    dropped: List[str] = []
    for name, data in series:
        if data.empty:
            dropped.append(name)
            continue
        path = series_directory / f'{name}.star'
        starfile.write({name: data}, path, overwrite=True)
        paths[name] = path

    for name in dropped:
        if name in table.index:
            table = table.drop(name)

    table[TILT_SERIES_COLUMN] = [
        paths.get(name) for name in table[NAME_COLUMN]
    ]
    starfile.write({GLOBAL_TABLE: table}, output_star_file, overwrite=True)
    return output_star_file
