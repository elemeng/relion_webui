"""Server-side 3D projection for the rotatable viewer.

The desktop GUI renders volumes in napari on the client. A browser cannot hold a
multi-gigabyte tomogram, so the same view is produced by projecting from the
requested angle on the server. These tests pin the geometry: a rotated view must
differ from the unrotated one, the two axes must be independent, and a uniform
volume must not render as black.
"""

import struct
import warnings

import numpy as np
import pytest

from relion_webui import mrc
from relion_webui.app import create_app

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402


def _write_mrc(path, volume: np.ndarray) -> None:
    nz, ny, nx = volume.shape
    header = np.zeros(1024, dtype=np.uint8)
    struct.pack_into('<3i', header, 0, nx, ny, nz)
    struct.pack_into('<i', header, 12, 2)          # mode 2 = float32
    struct.pack_into('<3f', header, 40, 1.0, 1.0, 1.0)
    path.write_bytes(header.tobytes() + np.ascontiguousarray(volume).tobytes())


@pytest.fixture
def client(tmp_path):
    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def rod(tmp_path):
    """A rod along x, off-centre in y and z, so every axis is distinguishable."""
    volume = np.zeros((64, 64, 64), dtype=np.float32)
    volume[20:28, 20:28, 8:56] = 1.0
    path = tmp_path / 'rod.mrc'
    _write_mrc(path, volume)
    return path


def _extent(image: np.ndarray) -> tuple:
    ys, xs = np.nonzero(image > 0.5)
    assert xs.size, 'nothing was projected'
    return int(xs.max() - xs.min()), int(ys.max() - ys.min())


def test_projection_has_content(rod):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        image = mrc.render_volume_projection(rod, width=64, height=64)
    assert image.shape == (64, 64)
    assert 0.0 <= image.min() and image.max() <= 1.0
    assert (image > 0.5).any()


def test_yaw_rotates_the_object(rod):
    """A rod along x must lie flat at yaw 0 and stand up at yaw 90."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        flat = mrc.render_volume_projection(rod, yaw_degrees=0, width=64, height=64)
        turned = mrc.render_volume_projection(rod, yaw_degrees=90, width=64, height=64)
    flat_w, flat_h = _extent(flat)
    turned_w, turned_h = _extent(turned)
    assert flat_w > flat_h, (flat_w, flat_h)
    assert turned_h > turned_w, (turned_w, turned_h)


def test_pitch_changes_the_view(rod):
    """Pitch is an independent axis, not a duplicate of yaw."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        level = mrc.render_volume_projection(rod, pitch_degrees=0, width=64, height=64)
        tilted = mrc.render_volume_projection(rod, pitch_degrees=30, width=64, height=64)
    assert np.abs(level - tilted).mean() > 1e-3


def test_opposite_yaw_mirrors_the_object(rod):
    """+45 and -45 are different views, not the same one."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        left = mrc.render_volume_projection(rod, yaw_degrees=-45, width=64, height=64)
        right = mrc.render_volume_projection(rod, yaw_degrees=45, width=64, height=64)
    assert np.abs(left - right).mean() > 1e-3


def test_mean_mode_differs_from_max(tmp_path):
    volume = np.zeros((32, 32, 32), dtype=np.float32)
    volume[8:24, 8:24, 8:24] = 1.0
    volume[0, 0, 0] = 5.0
    path = tmp_path / 'v.mrc'
    _write_mrc(path, volume)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        mip = mrc.render_volume_projection(path, width=32, height=32, mode='mip')
        mean = mrc.render_volume_projection(path, width=32, height=32, mode='mean')
    # MIP is dominated by the outlier; the mean spreads it out.
    assert not np.allclose(mip, mean)


def test_uniform_volume_is_not_black(tmp_path):
    """A flat projection carries no contrast; it must not look like a failure."""
    path = tmp_path / 'flat.mrc'
    _write_mrc(path, np.ones((16, 16, 16), dtype=np.float32))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        image = mrc.render_volume_projection(path, width=16, height=16)
    assert image.min() > 0.0


def test_explicit_value_range_is_honoured(rod):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        image = mrc.render_volume_projection(
            rod, width=32, height=32, value_range=(0.0, 1.0)
        )
    assert image.max() == pytest.approx(1.0)


def test_volume_endpoint_returns_png(client, tmp_path):
    volume = np.zeros((24, 32, 32), dtype=np.float32)
    volume[8:16, 10:22, 10:22] = 1.0
    path = tmp_path / 'tomo.mrc'
    _write_mrc(path, volume)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        response = client.get(
            '/api/render/volume',
            params={
                'path': str(path),
                'yaw': 30,
                'pitch': 20,
                'width': 64,
                'height': 64,
            },
        )
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/png'
    assert response.content[:4] == b'\x89PNG'


def test_volume_endpoint_rejects_a_bad_angle(client, tmp_path):
    path = tmp_path / 'm.mrc'
    _write_mrc(path, np.zeros((8, 8, 8), dtype=np.float32))
    response = client.get(
        '/api/render/volume', params={'path': str(path), 'pitch': 200}
    )
    assert response.status_code == 422


def test_volume_endpoint_rejects_a_bad_mode(client, tmp_path):
    path = tmp_path / 'm.mrc'
    _write_mrc(path, np.zeros((8, 8, 8), dtype=np.float32))
    response = client.get(
        '/api/render/volume', params={'path': str(path), 'mode': 'nope'}
    )
    assert response.status_code == 422
