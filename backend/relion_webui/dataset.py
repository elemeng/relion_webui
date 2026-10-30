# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Read-only dataset access built on top of the existing RELION metadata models.

``RlnTiltSeriesSet.from_star_file`` eagerly reads every tilt-series STAR file
referenced by the global table. That is fine for a job that is about to touch
all of them, but a web listing must not do it: a dataset with 500 tilt-series
turns one page request into 500 file reads.

So this module reads the global table only, and resolves individual
tilt-series on demand, reusing ``RlnTiltSeries`` for the per-tilt-series parse.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import starfile

TILT_SERIES_STAR_COLUMN = 'rlnTomoTiltSeriesStarFile'
TOMOGRAM_COLUMN = 'rlnTomoReconstructedTomogram'
DENOISED_COLUMN = 'rlnTomoReconstructedTomogramDenoised'
TOMO_NAME_COLUMN = 'rlnTomoName'


@dataclass(frozen=True)
class TiltSeriesInfo:
    name: str
    star_file: Optional[Path]
    tomogram_file: Optional[Path]
    denoised_tomogram_file: Optional[Path]
    half1_file: Optional[Path]
    half2_file: Optional[Path]
    pixel_size: Optional[float]
    n_tilt_images: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            'name': self.name,
            'star_file': str(self.star_file) if self.star_file else None,
            'tomogram_file': str(self.tomogram_file) if self.tomogram_file else None,
            'denoised_tomogram_file': (
                str(self.denoised_tomogram_file) if self.denoised_tomogram_file else None
            ),
            'half1_file': str(self.half1_file) if self.half1_file else None,
            'half2_file': str(self.half2_file) if self.half2_file else None,
            'pixel_size': self.pixel_size,
            'n_tilt_images': self.n_tilt_images,
        }


def _as_path(value) -> Optional[Path]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return Path(text) if text else None


def _resolve_reference(value, base: Path) -> Optional[Path]:
    """Resolve a path from a STAR table to an absolute path.

    RELION writes paths in pipeline and job STAR files relative to the project
    directory, not to the STAR file that contains them; the desktop GUI relies
    on its working directory for this. A server may be started from anywhere, so
    a relative reference is tried against the working directory and then the two
    directories above the STAR file. The candidate list is deliberately bounded
    rather than walking to the filesystem root.

    These are resolution candidates only. Whether the result may actually be
    read is decided by :class:`PathGuard` at request time.
    """
    path = _as_path(value)
    if path is None or path.is_absolute():
        return path

    candidates = [Path.cwd(), base, base.parent]
    seen = set()
    for candidate_root in candidates:
        if candidate_root in seen:
            continue
        seen.add(candidate_root)
        candidate = candidate_root / path
        if candidate.exists():
            return candidate
    return base / path


def _find_column(frame: pd.DataFrame, *candidates: str) -> Optional[str]:
    for column in candidates:
        if column in frame.columns:
            return column
    return None


def _optional_number(value) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=64)
def _read_global_table_cached(path_str: str, mtime_ns: int) -> pd.DataFrame:
    star = starfile.read(
        path_str, always_dict=True, parse_as_string=[TOMO_NAME_COLUMN]
    )
    if not isinstance(star, dict) or 'global' not in star:
        raise ValueError(f'{path_str} has no "global" table')
    table = star['global']
    # RELION writes a single-object table as bare `_label value` lines with no
    # `loop_`, which starfile returns as a dict rather than a DataFrame. A
    # one-tilt-series file is common, so normalise it here as pipeline.py does.
    if isinstance(table, dict):
        table = pd.DataFrame([table])
    elif isinstance(table, pd.Series):
        table = table.to_frame().T
    if TOMO_NAME_COLUMN not in table.columns:
        raise ValueError(f'{path_str} has no {TOMO_NAME_COLUMN} column')
    return table


def read_global_table(star_file: os.PathLike) -> pd.DataFrame:
    """Read the global tomogram table, cached on path + mtime."""
    path = Path(star_file)
    return _read_global_table_cached(str(path), path.stat().st_mtime_ns)


def list_tilt_series(star_file: os.PathLike) -> List[TiltSeriesInfo]:
    """List every tilt-series in a global STAR file without opening each one."""
    table = read_global_table(star_file)
    ts_column = _find_column(table, TILT_SERIES_STAR_COLUMN)
    tomo_column = _find_column(table, TOMOGRAM_COLUMN)
    denoised_column = _find_column(table, DENOISED_COLUMN)
    half1_column = _find_column(table, 'rlnTomoReconstructedTomogramHalf1')
    half2_column = _find_column(table, 'rlnTomoReconstructedTomogramHalf2')
    pixel_column = _find_column(table, 'rlnTomoTiltSeriesPixelSize')
    base = Path(star_file).parent

    infos: List[TiltSeriesInfo] = []
    for _, row in table.iterrows():
        star_path = (
            _resolve_reference(row[ts_column], base) if ts_column else None
        )
        tomo_path = (
            _resolve_reference(row[tomo_column], base) if tomo_column else None
        )
        denoised_path = (
            _resolve_reference(row[denoised_column], base)
            if denoised_column
            else None
        )
        half1 = (
            _resolve_reference(row[half1_column], base) if half1_column else None
        )
        half2 = (
            _resolve_reference(row[half2_column], base) if half2_column else None
        )

        infos.append(
            TiltSeriesInfo(
                name=str(row[TOMO_NAME_COLUMN]),
                star_file=star_path,
                tomogram_file=tomo_path,
                denoised_tomogram_file=denoised_path,
                half1_file=half1,
                half2_file=half2,
                pixel_size=_optional_number(row[pixel_column]) if pixel_column else None,
            )
        )
    return infos


@lru_cache(maxsize=32)
def _read_tilt_series_table_cached(
    path_str: str, mtime_ns: int
) -> Tuple[str, pd.DataFrame]:
    star = starfile.read(
        path_str, always_dict=True, parse_as_string=[TOMO_NAME_COLUMN]
    )
    if not isinstance(star, dict) or not star:
        raise ValueError(f'{path_str} contains no tables')
    name, table = next(iter(star.items()))
    # Same one-object form as the pipeline: a dict, not a DataFrame.
    if isinstance(table, dict):
        table = pd.DataFrame([table])
    elif isinstance(table, pd.Series):
        table = table.to_frame().T
    return name, table


def read_tilt_series_table(star_file: os.PathLike) -> Tuple[str, pd.DataFrame]:
    """Read a single tilt-series STAR file, cached on path + mtime."""
    path = Path(star_file)
    return _read_tilt_series_table_cached(str(path), path.stat().st_mtime_ns)


@dataclass
class TiltImageRecord:
    index: int
    filename: Optional[Path]
    tilt_angle: Optional[float]
    defocus: Optional[float]
    selected: bool = True

    def as_dict(self) -> dict:
        return {
            'index': self.index,
            'filename': str(self.filename) if self.filename else None,
            'name': self.filename.name if self.filename else None,
            'tilt_angle': self.tilt_angle,
            'defocus': self.defocus,
            'selected': self.selected,
        }


def tilt_image_records(star_file: os.PathLike) -> List[TiltImageRecord]:
    """Return per-tilt-image metadata, sorted by nominal tilt angle.

    Sorting matches what the Qt excluder does before presenting a tilt-series,
    so the browser and the desktop tool agree on image order.
    """
    _, table = read_tilt_series_table(star_file)
    base = Path(star_file).parent

    name_column = _find_column(
        table, 'rlnMicrographName', 'rlnImageName', 'rlnTomoTiltImageName'
    )
    tilt_column = _find_column(table, 'rlnTomoNominalStageTiltAngle', 'rlnTiltAngle')
    defocus_column = _find_column(table, 'rlnDefocusU')
    selected_column = _find_column(table, 'rlnImageSelected')

    records: List[TiltImageRecord] = []
    for position, (_, row) in enumerate(table.iterrows()):
        filename = (
            _resolve_reference(row[name_column], base) if name_column else None
        )
        selected = True
        if selected_column:
            value = row[selected_column]
            try:
                selected = bool(int(value))
            except (TypeError, ValueError):
                selected = bool(value)
        records.append(
            TiltImageRecord(
                index=position,
                filename=filename,
                tilt_angle=_optional_number(row[tilt_column]) if tilt_column else None,
                defocus=_optional_number(row[defocus_column]) if defocus_column else None,
                selected=selected,
            )
        )

    if any(record.tilt_angle is not None for record in records):
        records.sort(
            key=lambda record: (
                record.tilt_angle is None,
                record.tilt_angle if record.tilt_angle is not None else 0.0,
            )
        )
        for position, record in enumerate(records):
            record.index = position
    return records
