# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Write a filtered tilt-series set, matching the desktop excluder's output.

The desktop excluder filters each tilt-series table and rewrites both the global
STAR file and one STAR file per tilt-series under ``tilt_series/``. This module
produces the same layout so files from the web UI and the desktop tool are
interchangeable.

The STAR handling lives in :mod:`._vendor`, which carries the pieces the agent
used to import from RELION's Python package; see that module for why.
"""

from pathlib import Path
from typing import Dict, Iterable

from ._vendor import read_tilt_series_set, write_tilt_series_set

# The column the desktop tool sorts by, so "first tilt image" means the same
# thing in both UIs.
TILT_ANGLE_COLUMN = 'rlnTomoNominalStageTiltAngle'


def write_selected_tilt_series(
    star_file: Path,
    selections: Dict[str, Iterable[int]],
    output_star_file: Path,
) -> Path:
    """Write a tilt-series set keeping only the selected images.

    Parameters
    ----------
    star_file
        Global tilt-series STAR file.
    selections
        Mapping of tilt-series name to selected image indices, in the sorted
        (by tilt angle) order returned by :func:`tilt_image_records`.
    output_star_file
        Destination global STAR file, e.g. ``selected_tilt_series.star``.
    """
    global_df, series = read_tilt_series_set(Path(star_file))

    filtered = []
    for name, data in series:
        if TILT_ANGLE_COLUMN in data.columns:
            data = data.sort_values(by=TILT_ANGLE_COLUMN).reset_index(drop=True)
        if name not in selections:
            # Not part of this selection: keep it untouched.
            filtered.append((name, data))
            continue
        selected = list(selections[name])
        if not selected:
            # Nothing kept; an empty table makes the writer drop the series.
            filtered.append((name, data.iloc[0:0]))
            continue
        filtered.append((name, data.iloc[selected].reset_index(drop=True)))

    return write_tilt_series_set(global_df, filtered, Path(output_star_file))
