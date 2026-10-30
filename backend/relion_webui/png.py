# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Minimal PNG encoder.

Pillow is not part of the RELION conda environment, and adding an image stack
just to convert a uint8 array into a PNG is not worth it. PNG's format allows
a lowest-common-denominator encoder in a few dozen lines: 8-bit greyscale,
no filtering, one zlib stream.
"""

import struct
import zlib

import numpy as np

_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack('>I', len(payload))
        + tag
        + payload
        + struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_gray8(image: np.ndarray) -> bytes:
    """Encode a 2D uint8 array as PNG bytes."""
    image = np.ascontiguousarray(image, dtype=np.uint8)
    if image.ndim != 2:
        raise ValueError(f'expected a 2D array, got shape {image.shape}')
    height, width = image.shape

    # Each scanline is prefixed with a filter-type byte; 0 means "no filter".
    scanlines = np.hstack(
        [np.zeros((height, 1), dtype=np.uint8), image]
    ).tobytes()
    compressed = zlib.compress(scanlines, level=6)

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0)
    return b''.join(
        [
            _PNG_SIGNATURE,
            _chunk(b'IHDR', ihdr),
            _chunk(b'IDAT', compressed),
            _chunk(b'IEND', b''),
        ]
    )


def encode_gray8_with_overlay(
    image: np.ndarray,
    overlay: np.ndarray,
    overlay_rgb=(255, 80, 80),
) -> bytes:
    """Encode greyscale with a boolean overlay drawn in colour (RGB PNG)."""
    image = np.asarray(image, dtype=np.uint8)
    mask = np.asarray(overlay, dtype=bool)
    if image.shape != mask.shape:
        raise ValueError('overlay must match image shape')

    rgb = np.repeat(image[..., None], 3, axis=-1)
    rgb[mask] = overlay_rgb
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    height, width, _ = rgb.shape

    scanlines = np.hstack(
        [np.zeros((height, 1), dtype=np.uint8), rgb.reshape(height, width * 3)]
    ).tobytes()
    compressed = zlib.compress(scanlines, level=6)

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return b''.join(
        [
            _PNG_SIGNATURE,
            _chunk(b'IHDR', ihdr),
            _chunk(b'IDAT', compressed),
            _chunk(b'IEND', b''),
        ]
    )
