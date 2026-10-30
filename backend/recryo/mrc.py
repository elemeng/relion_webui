"""Memory-mapped access to MRC volumes and tilt images.

The existing helpers in ``_utils/mrc.py`` read whole files with
``mrcfile.read``. That is fine for single tilt images but pulls a full
tomogram into RAM, which is exactly the thing a browser viewer cannot afford.
Here the header is parsed with ``mrcfile`` (which already handles byte order
and the various broken headers found in the wild) and the data is then
accessed through ``numpy.memmap`` so only the requested slab is touched.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional, Tuple

import mrcfile
import numpy as np

# MRC mode -> numpy dtype. Modes 3 and 4 hold complex values.
_MODE_TO_DTYPE = {
    0: np.dtype('int8'),
    1: np.dtype('int16'),
    2: np.dtype('float32'),
    3: np.dtype('int16'),   # complex int16, two components per value
    4: np.dtype('float32'),  # complex float32, two components per value
    6: np.dtype('uint16'),
    12: np.dtype('float16'),
}
_MODE_N_COMPONENTS = {3: 2, 4: 2}

HEADER_SIZE = 1024


@dataclass(frozen=True)
class MrcHeader:
    path: Path
    nx: int
    ny: int
    nz: int
    mode: int
    dtype: np.dtype
    data_offset: int
    voxel_size: Tuple[float, float, float]

    @property
    def shape(self) -> Tuple[int, int, int]:
        return self.nz, self.ny, self.nx

    @property
    def pixel_size(self) -> Optional[float]:
        return self.voxel_size[0] or None

    @property
    def n_bytes(self) -> int:
        return int(np.prod(self.shape)) * self.dtype.itemsize * _MODE_N_COMPONENTS.get(self.mode, 1)


def read_header(path: os.PathLike) -> MrcHeader:
    """Parse just the 1024-byte MRC header."""
    path = Path(path)
    with mrcfile.open(path, header_only=True, permissive=True) as mrc:
        header = mrc.header
        nx, ny, nz = int(header.nx), int(header.ny), int(header.nz)
        mode = int(header.mode)
        nsymbt = int(header.nsymbt)
        cell = (float(header.cella.x), float(header.cella.y), float(header.cella.z))

    if mode not in _MODE_TO_DTYPE:
        raise ValueError(f'unsupported MRC mode {mode} in {path}')
    dtype = _MODE_TO_DTYPE[mode]

    # Cell dimensions are in angstroms, voxel size is per axis.
    n_per_axis = (nx, ny, nz)
    voxel_size = tuple(
        (cell[i] / n_per_axis[i]) if n_per_axis[i] else 0.0 for i in range(3)
    )

    return MrcHeader(
        path=path,
        nx=nx,
        ny=ny,
        nz=nz,
        mode=mode,
        dtype=dtype,
        data_offset=HEADER_SIZE + max(nsymbt, 0),
        voxel_size=voxel_size,
    )


@lru_cache(maxsize=32)
def _open_memmap(path_str: str, mtime_ns: int) -> np.ndarray:
    """Open a memory map, keyed on path+mtime so rewrites are picked up."""
    header = read_header(path_str)
    expected = header.n_bytes
    actual = Path(path_str).stat().st_size - header.data_offset
    if actual < expected:
        raise ValueError(
            f'{path_str}: truncated data block, header implies {expected} bytes '
            f'but only {actual} are present'
        )
    # Complex modes are viewed as a flat real dtype of twice the length.
    count = int(np.prod(header.shape))
    n_components = _MODE_N_COMPONENTS.get(header.mode, 1)
    array = np.memmap(
        path_str,
        dtype=header.dtype,
        mode='r',
        offset=header.data_offset,
        shape=(count * n_components,),
    )
    if n_components == 2:
        array = array.reshape(count, 2)
    return array


def open_volume(path: os.PathLike) -> Tuple[MrcHeader, np.ndarray]:
    """Return header and a lazily-read (nz, ny, nx) view of the file."""
    path = Path(path)
    header = read_header(path)
    flat = _open_memmap(str(path), path.stat().st_mtime_ns)
    volume = flat
    if _MODE_N_COMPONENTS.get(header.mode):
        # Complex data is not viewable; expose the real part.
        volume = flat[:, 0]
    return header, volume.reshape(header.shape)


def iter_z_slabs(
    path: os.PathLike, slab: int = 32, z_range: Optional[Tuple[int, int]] = None
) -> Iterator[Tuple[int, np.ndarray]]:
    """Yield (z_start, slab) pairs so callers never hold a whole volume."""
    header, volume = open_volume(path)
    z0, z1 = (0, header.nz) if z_range is None else z_range
    z0 = max(0, z0)
    z1 = min(header.nz, z1)
    for start in range(z0, z1, slab):
        yield start, np.asarray(volume[start:min(start + slab, z1)])


def read_tilt_image(path: os.PathLike) -> np.ndarray:
    """Read a single (2D) tilt image into memory."""
    header, volume = open_volume(path)
    if header.nz != 1:
        raise ValueError(f'{path} holds {header.nz} images, expected a single 2D image')
    return np.array(volume[0], dtype=np.float32)


def read_image_from_stack(path: os.PathLike, index: int) -> np.ndarray:
    """Read one plane out of a tomogram or image stack."""
    header, volume = open_volume(path)
    if not 0 <= index < header.nz:
        raise IndexError(f'image index {index} out of range for {header.nz} planes')
    return np.array(volume[index], dtype=np.float32)


def sample_stats(path: os.PathLike, max_samples: int = 200_000) -> Tuple[float, float]:
    """Estimate value range from a strided subsample, never reading it all."""
    header, volume = open_volume(path)
    stride_z = max(1, header.nz // 8)
    stride_y = max(1, header.ny // 128)
    stride_x = max(1, header.nx // 128)
    sample = np.asarray(
        volume[::stride_z, ::stride_y, ::stride_x], dtype=np.float32
    ).ravel()
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return 0.0, 1.0
    if sample.size > max_samples:
        step = sample.size // max_samples
        sample = sample[::step]
    low, high = np.percentile(sample, [0.5, 99.5])
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        low, high = float(np.min(sample)), float(np.max(sample))
    if high <= low:
        high = low + 1.0
    return float(low), float(high)


def to_uint8(
    data: np.ndarray,
    value_range: Optional[Tuple[float, float]] = None,
    percentiles: Tuple[float, float] = (0.5, 99.5),
) -> np.ndarray:
    """Map image data to uint8 for display."""
    data = np.asarray(data, dtype=np.float32)
    if value_range is not None:
        low, high = value_range
    else:
        finite = data[np.isfinite(data)]
        if finite.size == 0:
            return np.zeros(data.shape, dtype=np.uint8)
        low, high = np.percentile(finite, percentiles)
    if not np.isfinite(low) or not np.isfinite(high):
        low, high = 0.0, 1.0
    if high <= low:
        high = low + 1e-6
    scaled = (data - low) * (255.0 / (high - low))
    return np.clip(scaled, 0, 255).astype(np.uint8)


def downsample_to(data: np.ndarray, width: int, height: int) -> np.ndarray:
    """Decimate ``data`` to at most width x height by strided sampling.

    Strided subsampling is used rather than averaging because tomograms are
    large and the browser only needs a preview; it keeps per-request cost low.
    """
    ny, nx = data.shape[-2:]
    step_y = max(1, ny // max(1, height))
    step_x = max(1, nx // max(1, width))
    return np.asarray(data[..., ::step_y, ::step_x])


def render_volume_projection(
    path: os.PathLike,
    *,
    yaw_degrees: float = 0.0,
    pitch_degrees: float = 0.0,
    width: int = 512,
    height: int = 512,
    mode: str = 'mip',
    value_range: Optional[Tuple[float, float]] = None,
) -> np.ndarray:
    """Project the volume from an arbitrary viewing direction.

    The desktop GUI's 3D view is napari's volume renderer, which can be rotated
    freely. A browser cannot hold a multi-gigabyte volume, so the same effect is
    produced by projecting server-side from the requested angle:

    * the volume is first binned down until it fits a work limit (a tomogram is
      4000x4000x2000, and rotating that per frame is not affordable);
    * the viewing direction is built from yaw and pitch, and the projection is
      computed with a rotation applied to the sampling grid rather than by
      rotating the data;
    * ``mip`` keeps the maximum along each ray, which is what makes membranes
      and particles legible; ``mean`` is the smoother alternative.

    Returns a float array shaped ``(height, width)``.
    """
    header, volume = open_volume(path)
    nz, ny, nx = volume.shape

    # Bin so the rotated sample count stays bounded. The projection is
    # resampled to (width, height) anyway, so binning to roughly that size
    # loses nothing visible.
    bin_factor = max(1, int(max(nx, ny, nz) // max(width, height, 1)))
    if bin_factor > 1:
        volume = np.asarray(volume[::bin_factor, ::bin_factor, ::bin_factor])
    data = np.asarray(volume, dtype=np.float32)
    nz, ny, nx = data.shape
    if min(nz, ny, nx) == 0:
        return np.zeros((height, width), dtype=np.float32)

    yaw = np.deg2rad(yaw_degrees)
    pitch = np.deg2rad(pitch_degrees)

    # Rotation from the camera frame to the volume frame. A point p_cam =
    # (u, v, w) has u, v on the image plane and w along the viewing direction,
    # so a ray is all w for fixed (u, v). The matrix is R = Rz(yaw) * Rx(pitch),
    # applied as p_vol = R @ p_cam; writing it out keeps the volume axis order
    # (z, y, x) explicit, because the array is indexed data[iz, iy, ix].
    cos_y, sin_y = np.cos(yaw), np.sin(yaw)
    cos_p, sin_p = np.cos(pitch), np.sin(pitch)

    # Output pixel grid, u along the width and v along the height.
    u = np.linspace(-1.0, 1.0, width, dtype=np.float32)
    v = np.linspace(-1.0, 1.0, height, dtype=np.float32)
    uu, vv = np.meshgrid(u, v)

    n_max = int(max(nz, ny, nx))
    # Half-extents: +-1 in the camera frame must reach the volume border.
    half_z = (nz - 1) / 2.0
    half_y = (ny - 1) / 2.0
    half_x = (nx - 1) / 2.0
    centre_z = half_z
    centre_y = half_y
    centre_x = half_x

    projected = np.full((height, width), -np.inf, dtype=np.float32)
    accumulated = np.zeros((height, width), dtype=np.float32)
    counted = 0

    # A step of 1 sample along w stays below the voxel spacing for every axis,
    # so no gaps: 2 / (n_max) per step over n_max steps.
    for w in np.linspace(-1.0, 1.0, num=2 * n_max + 1, dtype=np.float32):
        # p_vol = Rz(yaw) . Rx(pitch) . (u, v, w)
        # Rx(pitch) first: y' = cos_p*v - sin_p*w ; z' = sin_p*v + cos_p*w
        px = uu
        py = cos_p * vv - sin_p * w
        pz = sin_p * vv + cos_p * w
        # Then Rz(yaw): x'' = cos_y*px - sin_y*py ; y'' = sin_y*px + cos_y*py
        cx = cos_y * px - sin_y * py
        cy = sin_y * px + cos_y * py
        cz = pz

        iz = np.rint(centre_z + cz * half_z).astype(np.int32)
        iy = np.rint(centre_y + cy * half_y).astype(np.int32)
        ix = np.rint(centre_x + cx * half_x).astype(np.int32)
        inside = (
            (iz >= 0) & (iz < nz) & (iy >= 0) & (iy < ny) & (ix >= 0) & (ix < nx)
        )
        if not inside.any():
            continue
        samples = data[iz[inside], iy[inside], ix[inside]]
        if mode == 'mean':
            accumulated[inside] = accumulated[inside] + samples
            counted += 1
        else:
            projected[inside] = np.maximum(projected[inside], samples)

    if mode == 'mean':
        result = np.divide(
            accumulated,
            max(counted, 1),
            out=np.zeros_like(accumulated),
        )
    else:
        result = np.where(np.isfinite(projected), projected, 0.0)

    if value_range is not None:
        low, high = value_range
        if high <= low:
            high = low + 1.0
        result = np.clip((result - low) / (high - low), 0.0, 1.0)
    else:
        # Percentiles are the right default for a tomogram, but a view that is
        # mostly empty has its 99th percentile at the background level, which
        # would flatten everything. Take the scale from the non-background
        # pixels instead, and only fall back to min/max when there are none.
        # The floor is the 1st percentile, which is the background in a
        # tomogram; the ceiling comes from the brighter part of the histogram.
        # Using min() as the floor fails when the view is entirely filled with
        # signal, where min == max.
        low = float(np.percentile(result, 1.0))
        high = float(np.percentile(result, 99.0))
        if high <= low:
            low = float(result.min())
            high = float(result.max())
        if high <= low:
            # A completely uniform projection carries no contrast; render it as
            # a flat mid-grey rather than black, so the view is recognisably
            # "nothing to see" instead of looking like a failed render.
            return np.full_like(result, 0.5, dtype=np.float32)
        result = np.clip((result - low) / (high - low), 0.0, 1.0)
    return result.astype(np.float32)
