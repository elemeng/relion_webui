"""The one status vocabulary, and its mapping to RELION's labels.

Three layers described the same job in three spellings, and a mapping between
them drifted: the queue mapped a pipeline `Scheduled` row to `running` in one
place and treated it as "not started" in another. These tests pin the contract
that replaced those ad-hoc dicts.
"""

import pytest

from recryo import statuses


def test_every_status_has_a_pipeline_label():
    """A status without a label would be unrepresentable in the pipeline file."""
    for status in statuses.ALL:
        assert statuses.label(status), status


def test_labels_round_trip_where_the_meaning_is_unambiguous():
    """PENDING and QUEUED both mean Scheduled, so the inverse picks PENDING."""
    assert statuses.from_label('Scheduled') == statuses.PENDING
    for status in ('Running', 'Succeeded', 'Failed', 'Aborted'):
        assert statuses.label(statuses.from_label(status)) == status


def test_unknown_inputs_are_none_not_a_guess():
    """A pipeline file written by a future RELION must not be misread."""
    assert statuses.label('nonsense') is None
    assert statuses.from_label('Brand new label') is None
    assert statuses.label(None) is None


def test_terminality_matches_the_pipeline_labels():
    assert statuses.is_terminal(statuses.SUCCEEDED)
    assert statuses.is_terminal(statuses.FAILED)
    assert statuses.is_terminal(statuses.ABORTED)
    assert statuses.is_terminal(statuses.CANCELLED)
    assert not statuses.is_terminal(statuses.PENDING)
    assert not statuses.is_terminal(statuses.RUNNING)


def test_live_statuses_are_the_ones_holding_resources():
    """PENDING and QUEUED hold no device yet, but they do hold their place."""
    assert statuses.is_live(statuses.PENDING)
    assert statuses.is_live(statuses.QUEUED)
    assert statuses.is_live(statuses.RUNNING)
    assert not statuses.is_live(statuses.SUCCEEDED)


def test_a_cancelled_job_appears_aborted_in_relion():
    """RELION has no Cancelled label; Aborted is its word for a stopped job."""
    assert statuses.label(statuses.CANCELLED) == 'Aborted'


@pytest.mark.parametrize('status', statuses.ALL)
def test_describe_is_never_empty(status):
    assert statuses.describe(status).strip()
    assert statuses.describe(None) == 'unknown'
