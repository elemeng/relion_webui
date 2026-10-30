import numpy as np
import pytest

from recryo import dataset, mrc, png
from recryo._cli import main  # noqa: F401 - import check


@pytest.fixture
def volume_file(tmp_path):
    import mrcfile

    rng = np.random.default_rng(0)
    data = rng.normal(0.0, 1.0, (16, 12, 10)).astype(np.float32)
    path = tmp_path / 'volume.mrc'
    with mrcfile.new(path, overwrite=True) as handle:
        handle.set_data(data)
        handle.voxel_size = (10.0, 10.0, 10.0)
    return path, data


def test_list_tilt_series_does_not_need_tilt_series_files(global_relion_metadata_file):
    infos = dataset.list_tilt_series(global_relion_metadata_file)
    assert len(infos) == 5
    assert {info.name for info in infos} == {
        '220728_TS_01', '220728_TS_03', '220728_TS_43', '220728_TS_45', '220728_TS_54'
    }
    first = infos[0]
    assert first.tomogram_file is not None
    assert first.tomogram_file.name == 'rec_220728_TS_01.mrc'
    assert first.pixel_size == pytest.approx(1.35)


def test_tilt_image_records_sorted_by_tilt_angle(tilt_series_star_file):
    records = dataset.tilt_image_records(tilt_series_star_file)
    assert len(records) == 41
    angles = [record.tilt_angle for record in records if record.tilt_angle is not None]
    assert angles == sorted(angles)
    assert [record.index for record in records] == list(range(41))


def test_tilt_image_records_expose_filenames(tilt_series_star_file):
    records = dataset.tilt_image_records(tilt_series_star_file)
    assert records[0].filename is not None
    assert records[0].filename.name.endswith('.mrc') or records[0].filename.suffix


def test_read_header_reports_shape_and_voxel_size(volume_file):
    path, data = volume_file
    header = mrc.read_header(path)
    assert header.shape == data.shape
    assert header.nz == 16 and header.ny == 12 and header.nx == 10
    assert header.voxel_size == pytest.approx((10.0, 10.0, 10.0))


def test_open_volume_matches_written_data(volume_file):
    path, data = volume_file
    _, volume = mrc.open_volume(path)
    np.testing.assert_allclose(np.asarray(volume), data)


def test_iter_z_slabs_covers_every_plane(volume_file):
    path, data = volume_file
    seen = []
    for start, slab in mrc.iter_z_slabs(path, slab=5):
        expected = data[start:start + slab.shape[0]]
        np.testing.assert_allclose(np.asarray(slab), expected)
        seen.extend(range(start, start + slab.shape[0]))
    assert seen == list(range(data.shape[0]))


def test_iter_z_slabs_honours_range(volume_file):
    path, data = volume_file
    chunks = list(mrc.iter_z_slabs(path, slab=4, z_range=(4, 9)))
    assert chunk_start(chunks) == 4
    total = sum(slab.shape[0] for _, slab in chunks)
    assert total == 5


def chunk_start(chunks):
    return chunks[0][0]


def test_sample_stats_returns_ordered_range(volume_file):
    path, _ = volume_file
    low, high = mrc.sample_stats(path)
    assert high > low


def test_to_uint8_respects_explicit_range():
    data = np.array([[-1.0, 0.0, 1.0]], dtype=np.float32)
    result = mrc.to_uint8(data, value_range=(0.0, 1.0))
    assert result.dtype == np.uint8
    assert result[0, 0] == 0
    assert result[0, 2] == 255


def test_to_uint8_handles_constant_image():
    data = np.full((4, 4), 3.5, dtype=np.float32)
    result = mrc.to_uint8(data)
    assert result.min() == result.max()


def test_png_encoding_produces_valid_signature():
    image = np.zeros((4, 6), dtype=np.uint8)
    data = png.encode_gray8(image)
    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    assert b'IHDR' in data and b'IEND' in data


def test_png_overlay_encodes_rgb():
    image = np.zeros((4, 4), dtype=np.uint8)
    overlay = np.zeros((4, 4), dtype=bool)
    overlay[1, 1] = True
    data = png.encode_gray8_with_overlay(image, overlay)
    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    # IHDR colour type 2 is RGB.
    assert data[25] == 2


def test_read_tilt_image_rejects_stacks(volume_file):
    path, _ = volume_file
    with pytest.raises(ValueError):
        mrc.read_tilt_image(path)


def test_downsample_to_limits_dimensions(volume_file):
    path, _ = volume_file
    _, volume = mrc.open_volume(path)
    preview = mrc.downsample_to(np.asarray(volume[0]), width=5, height=6)
    assert preview.shape[0] <= 6
    assert preview.shape[1] <= 5


# ------------------------------------------------- RELION single-object tables
RELION_ONE_OBJECT_GLOBAL = """
# version 50001

data_global

_rlnTomoName                              TS_01
_rlnTomoTiltSeriesStarFile                TS_01.star
_rlnTomoTiltSeriesPixelSize               1.35
 """


def test_global_table_with_one_object(tmp_path):
    """RELION omits `loop_` for a single-object table; it must still parse.

    starfile returns a dict in that case, and a one-tilt-series file is exactly
    what a test dataset looks like, so this used to fail on the smallest input.
    """
    from recryo import dataset

    path = tmp_path / 'global.star'
    path.write_text(RELION_ONE_OBJECT_GLOBAL)
    table = dataset.read_global_table(path)
    assert table.iloc[0]['rlnTomoName'] == 'TS_01'
    assert 'rlnTomoTiltSeriesStarFile' in table.columns
