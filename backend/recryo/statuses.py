"""The one status vocabulary, and the mappings to and from RELION's.

Three layers described the same job in three spellings, and every boundary
between them was a hand-written dict:

* ``pipeline.py`` — RELION's own labels (``Scheduled``, ``Running``, ...). This
  is the only vocabulary that leaves the process: it is what the desktop GUI
  reads from ``default_pipeline.star``, so it cannot be renamed.
* ``jobs.py`` — a ``Job``'s live status (``queued``, ``running``, ...), derived
  from the process and the sentinel files.
* ``queue_system.py`` — a queue entry's state (``pending``, ``cancelled``).

The mappings between them drifted: the queue's view of a pipeline row mapped
``Scheduled`` to ``running`` in one place and treated it as "not started" in
another. Keeping all three vocabularies is unavoidable - they answer different
questions - but keeping three *mappings* was not. They live here now.

Design rules, so the next change has somewhere to go:

* A new status is added here first, with its pipeline label and its terminality.
* Code compares against the named constants, never a string literal.
* Anything user-facing reads ``PIPELINE_LABEL``, so the wire format stays
  RELION's whatever the internal spelling is.
"""

from __future__ import annotations

from typing import Dict, Final, Optional

# --- the canonical vocabulary -------------------------------------------------
#
# One name per state a job can be in, whatever layer is speaking. `QUEUED` is
# distinct from `PENDING`: `PENDING` is a queue entry nobody has started, while
# `QUEUED` is a job handed to an external scheduler (RELION's qsub path) that
# has not run yet.

PENDING: Final = 'pending'
QUEUED: Final = 'queued'
RUNNING: Final = 'running'
SUCCEEDED: Final = 'succeeded'
FAILED: Final = 'failed'
ABORTED: Final = 'aborted'
CANCELLED: Final = 'cancelled'

ALL: Final = (PENDING, QUEUED, RUNNING, SUCCEEDED, FAILED, ABORTED, CANCELLED)

#: A job in one of these will not change again without being retried.
TERMINAL: Final = frozenset({SUCCEEDED, FAILED, ABORTED, CANCELLED})

#: A job in one of these is occupying a node (or waiting to).
LIVE: Final = frozenset({PENDING, QUEUED, RUNNING})

# --- the mapping to RELION's labels ------------------------------------------
#
# RELION's five labels are the wire format. Every internal state has exactly one
# label, so a job never appears differently in the GUI than in this app.

PIPELINE_LABEL: Final[Dict[str, str]] = {
    PENDING: 'Scheduled',
    QUEUED: 'Scheduled',
    RUNNING: 'Running',
    SUCCEEDED: 'Succeeded',
    FAILED: 'Failed',
    ABORTED: 'Aborted',
    CANCELLED: 'Aborted',
}

#: The inverse, for reading a pipeline file back.
FROM_PIPELINE_LABEL: Final[Dict[str, str]] = {
    'Scheduled': PENDING,
    'Running': RUNNING,
    'Succeeded': SUCCEEDED,
    'Failed': FAILED,
    'Aborted': ABORTED,
}


def is_terminal(status: Optional[str]) -> bool:
    return status in TERMINAL


def is_live(status: Optional[str]) -> bool:
    return status in LIVE


def label(status: Optional[str]) -> Optional[str]:
    """RELION's label for an internal status; None for an unknown one."""
    if status is None:
        return None
    return PIPELINE_LABEL.get(status)


def from_label(pipeline_label: Optional[str]) -> Optional[str]:
    """The internal status a pipeline label means.

    ``Scheduled`` maps to ``PENDING``: a row saying Scheduled with nothing
    running is a job that has not started. A caller that *knows* the job was
    dispatched should not ask this - the queue does, and treats the answer as
    "still running" - which is why the distinction is named rather than folded
    into the mapping.
    """
    if pipeline_label is None:
        return None
    return FROM_PIPELINE_LABEL.get(pipeline_label)


def describe(status: Optional[str]) -> str:
    """A short phrase for a UI or a model, in RELION's vocabulary."""
    if status is None:
        return 'unknown'
    if status == PENDING:
        return 'not started yet'
    if status == QUEUED:
        return 'waiting for the scheduler'
    if status == RUNNING:
        return 'running'
    if status == SUCCEEDED:
        return 'succeeded'
    if status == FAILED:
        return 'failed'
    if status == ABORTED:
        return 'aborted'
    if status == CANCELLED:
        return 'cancelled'
    return status
