"""Fixtures for the web agent's tests.

The STAR fixtures are a small synthetic tilt-series set committed under
``data/``, so these tests do not depend on RELION's own test data (or on a
RELION checkout at all - the three drift tests skip themselves when
``RELION_SOURCE`` is unset; see ``_relion_source.py``).
"""

from pathlib import Path

import pytest

DATA_DIRECTORY = Path(__file__).parent / 'data'
TILT_SERIES_METADATA = DATA_DIRECTORY / 'tilt_series_metadata'


@pytest.fixture(scope='session')
def test_project_directory() -> Path:
    return DATA_DIRECTORY


@pytest.fixture(scope='session')
def global_relion_metadata_file() -> Path:
    """Global tilt-series STAR file for the synthetic project."""
    return TILT_SERIES_METADATA / 'tilt_series.star'


@pytest.fixture(scope='session')
def tilt_series_star_file() -> Path:
    """One per-tilt-series STAR file, as the global one names them."""
    return TILT_SERIES_METADATA / 'tilt_series' / '220728_TS_01.star'
