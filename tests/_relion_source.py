"""Locate the RELION source tree the generated tables were extracted from.

Three tests compare ``generated_schema.py`` / ``generated_cli_options.py`` /
``generated_commands.py`` against ``pipeline_jobs.cpp`` and friends, to catch a
generated table that has drifted from upstream. That comparison needs RELION's
sources, which are not part of this package: the agent runs against an
*installed* RELION, which may be a system prefix with no C++ anywhere.

So the path comes from ``RELION_SOURCE`` and the tests skip when it is unset.
In a checkout of the RELION repository it defaults to the parent directory,
which is where it normally sits; a standalone install simply does not run them.

    RELION_SOURCE=/path/to/relion pytest
"""

import os
from pathlib import Path
from typing import Optional

# This package is developed beside a RELION checkout rather than inside one, so
# the default candidates are: the working directory, its parent, and a `relion`
# sibling. `RELION_SOURCE` overrides all of them.
_HERE = Path(__file__).resolve()
_CANDIDATES = (
    Path.cwd(),
    Path.cwd().parent,
    _HERE.parents[1] / 'relion',          # ./relion beside this checkout
    _HERE.parents[1].parent / 'relion',   # ../relion beside this checkout
)


def relion_source() -> Optional[Path]:
    """The RELION checkout, or None when it is not available."""
    configured = os.environ.get('RELION_SOURCE')
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if (candidate / 'src' / 'pipeline_jobs.cpp').is_file() else None
    for candidate in _CANDIDATES:
        if (candidate / 'src' / 'pipeline_jobs.cpp').is_file():
            return candidate
    return None


def source_file(*parts: str) -> Optional[Path]:
    """One file inside the RELION checkout, or None when it is not there."""
    root = relion_source()
    if root is None:
        return None
    path = root.joinpath(*parts)
    return path if path.is_file() else None
