"""Job actions: alias, note, delete/undelete, mark, clean.

Most of these already exist in RELION. ``relion_pipeliner`` exposes alias, gentle
clean and harsh clean on the command line; note editing is a text file; delete,
undelete and mark-as-finished are library calls with no CLI flag, so they are
implemented here against the pipeline file.

These tests cover the conventions that make the results readable by the desktop
GUI: the note file name, the Trash layout, the alias-as-path form, and the node
links that have to be rebuilt when an alias changes.
"""

import shutil
from pathlib import Path

import pytest

from relion_webui import job_actions, job_artifacts
from relion_webui import pipeline as pipeline_module


@pytest.fixture
def project(tmp_path):
    """A project with one finished reconstruct-tomograms job."""
    project = tmp_path / 'proj'
    project.mkdir()
    p = pipeline_module.Pipeline()
    p.job_counter = 2
    pipeline_module.register_process(
        p,
        pipeline_module.Process(
            name='Tomograms/job001',
            type_label='relion.reconstructtomograms',
            status=pipeline_module.STATUS_SUCCEEDED,
        ),
        input_nodes=[
            pipeline_module.Node(
                'tilt_series.star', 'TomogramGroupMetadata.star.relion'
            )
        ],
        output_nodes=[
            pipeline_module.Node(
                'Tomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    pipeline_module.write_pipeline(project, p)

    job_dir = project / 'Tomograms' / 'job001'
    job_dir.mkdir(parents=True)
    job_artifacts.write_job_star(
        job_dir, 'relion.reconstructtomograms', {'in_tiltseries': 'tilt_series.star'}
    )
    job_artifacts.write_output_nodes(
        job_dir,
        [
            (
                'Tomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    (job_dir / 'tomograms.star').write_text('# tomograms\n')
    (project / 'tilt_series.star').write_text('# tilt series\n')
    job_artifacts.create_node_symlink(
        project,
        'Tomograms/job001/tomograms.star',
        'TomogramGroupMetadata.star.relion.tomo.reconstruct',
    )
    return project


# ------------------------------------------------------------------ notes
def test_note_path_is_the_gui_name(project):
    """The GUI opens ``<jobdir>/<jobname>note.txt``, not note.txt."""
    path = job_actions.note_path(project, 'Tomograms/job001')
    assert path == project / 'Tomograms' / 'job001' / 'job001note.txt'


def test_note_roundtrip(project):
    job_actions.write_note(project, 'Tomograms/job001', 'picked by hand')
    result = job_actions.read_note(project, 'Tomograms/job001')
    assert result.detail['note'] == 'picked by hand'
    # The same file the GUI's note editor would show.
    assert (
        project / 'Tomograms' / 'job001' / 'job001note.txt'
    ).read_text() == 'picked by hand'


def test_reading_a_missing_note_is_empty(project):
    assert job_actions.read_note(project, 'Tomograms/job001').detail['note'] == ''


# ------------------------------------------------------------------ alias
def test_alias_becomes_a_type_relative_path(project):
    """RELION stores the alias as ``<Type>/<alias>``, with a trailing slash."""
    result = job_actions.set_alias(project, 'Tomograms/job001', 'good')
    assert result.detail['alias'] == 'Tomograms/good/'
    process = pipeline_module.read_pipeline(project).find_process('Tomograms/job001')
    assert process.alias == 'Tomograms/good/'


def test_alias_creates_the_gui_symlink(project):
    """The readme refers to aliased outputs by path, so the link must exist."""
    job_actions.set_alias(project, 'Tomograms/job001', 'good')
    link = project / 'Tomograms' / 'good'
    assert link.is_symlink()
    assert link.resolve() == (project / 'Tomograms' / 'job001').resolve()


def test_alias_replaces_the_old_link(project):
    job_actions.set_alias(project, 'Tomograms/job001', 'first')
    job_actions.set_alias(project, 'Tomograms/job001', 'second')
    assert not (project / 'Tomograms' / 'first').is_symlink()
    assert (project / 'Tomograms' / 'second').is_symlink()
    process = pipeline_module.read_pipeline(project).find_process('Tomograms/job001')
    assert process.alias == 'Tomograms/second/'


def test_alias_turns_spaces_into_underscores(project):
    result = job_actions.set_alias(project, 'Tomograms/job001', 'my job')
    assert result.detail['alias'] == 'Tomograms/my_job/'


@pytest.mark.parametrize(
    'alias, reason',
    [
        ('x', 'at least 2 characters'),
        ('job9', "start with 'job'"),
        ('a/b', 'cannot contain'),
        ('a*b', 'cannot contain'),
        ('a?b', 'cannot contain'),
    ],
)
def test_invalid_aliases_are_rejected(project, alias, reason):
    """The same rules PipeLine::setAliasJob applies."""
    with pytest.raises(job_actions.ActionError, match=reason):
        job_actions.set_alias(project, 'Tomograms/job001', alias)


def test_duplicate_alias_is_refused(project):
    job_actions.set_alias(project, 'Tomograms/job001', 'taken')
    p = pipeline_module.read_pipeline(project)
    pipeline_module.register_process(
        p,
        pipeline_module.Process(
            name='Tomograms/job002', type_label='relion.reconstructtomograms'
        ),
    )
    pipeline_module.write_pipeline(project, p)
    with pytest.raises(job_actions.ActionError, match='already used'):
        job_actions.set_alias(project, 'Tomograms/job002', 'taken')


def test_clearing_the_alias_removes_the_link(project):
    job_actions.set_alias(project, 'Tomograms/job001', 'temp')
    result = job_actions.set_alias(project, 'Tomograms/job001', '')
    assert result.detail['alias'] == 'None'
    assert not (project / 'Tomograms' / 'temp').is_symlink()


# ----------------------------------------------------------------- delete
def test_delete_moves_the_job_to_trash_with_its_type(project):
    """Trash keeps ``<Type>/jobNNN`` so undelete can put it back."""
    job_actions.delete(project, 'Tomograms/job001')
    assert not (project / 'Tomograms' / 'job001').exists()
    assert (project / 'Trash' / 'Tomograms' / 'job001').is_dir()
    assert pipeline_module.read_pipeline(project).processes == []


def test_delete_keeps_a_node_that_another_job_still_uses(project):
    """A node produced by the deleted job but consumed elsewhere is not removed."""
    p = pipeline_module.read_pipeline(project)
    pipeline_module.register_process(
        p,
        pipeline_module.Process(
            name='Denoise/job002', type_label='relion.denoisetomo'
        ),
        input_nodes=[
            pipeline_module.Node(
                'Tomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    pipeline_module.write_pipeline(project, p)

    job_actions.delete(project, 'Tomograms/job001')
    after = pipeline_module.read_pipeline(project)
    assert after.find_process('Tomograms/job001') is None
    assert after.find_node('Tomograms/job001/tomograms.star') is not None


def test_recursive_delete_takes_downstream_jobs(project):
    p = pipeline_module.read_pipeline(project)
    pipeline_module.register_process(
        p,
        pipeline_module.Process(
            name='Denoise/job002', type_label='relion.denoisetomo'
        ),
        input_nodes=[
            pipeline_module.Node(
                'Tomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    pipeline_module.write_pipeline(project, p)
    (project / 'Denoise' / 'job002').mkdir(parents=True)

    result = job_actions.delete(project, 'Tomograms/job001', recursive=True)
    assert set(result.detail['deleted']) == {'Tomograms/job001', 'Denoise/job002'}
    after = pipeline_module.read_pipeline(project)
    assert after.processes == []


def test_delete_unknown_job_is_reported(project):
    with pytest.raises(job_actions.ActionError, match='not in the pipeline'):
        job_actions.delete(project, 'Tomograms/job999')


# --------------------------------------------------------------- undelete
def test_delete_then_undelete_restores_process_and_nodes(project):
    job_actions.delete(project, 'Tomograms/job001')
    assert job_actions.list_trash(project).detail['entries'] == ['Tomograms/job001']

    job_actions.undelete(project, 'Tomograms/job001')
    restored = pipeline_module.read_pipeline(project)
    process = restored.find_process('Tomograms/job001')
    assert process is not None
    assert process.type_label == 'relion.reconstructtomograms'
    assert process.status == pipeline_module.STATUS_SUCCEEDED
    assert restored.find_node('Tomograms/job001/tomograms.star') is not None
    assert (project / 'Tomograms' / 'job001').is_dir()
    # The node link is recreated, or the GUI cannot offer the output.
    assert job_actions.list_trash(project).detail['entries'] == []


def test_undelete_refuses_a_missing_entry(project):
    with pytest.raises(job_actions.ActionError, match='no .* in Trash'):
        job_actions.undelete(project, 'Tomograms/job404')


def test_undelete_needs_the_type_directory(project):
    """An entry without a type prefix cannot be placed back in the pipeline."""
    (project / 'Trash' / 'loose').mkdir(parents=True)
    with pytest.raises(job_actions.ActionError, match='job-type directory'):
        job_actions.undelete(project, 'loose')


# ------------------------------------------------------------------ mark
def test_mark_failed_writes_the_sentinel_the_gui_reads(project):
    from relion_webui._vendor import JOB_FAILURE_FILENAME

    job_actions.mark_status(project, 'Tomograms/job001', failed=True)
    assert (project / 'Tomograms' / 'job001' / JOB_FAILURE_FILENAME).is_file()
    process = pipeline_module.read_pipeline(project).find_process('Tomograms/job001')
    assert process.status == pipeline_module.STATUS_FAILED


def test_mark_finished_clears_a_previous_failure(project):
    from relion_webui._vendor import (
        JOB_FAILURE_FILENAME,
        JOB_SUCCESS_FILENAME,
    )

    job_actions.mark_status(project, 'Tomograms/job001', failed=True)
    job_actions.mark_status(project, 'Tomograms/job001', failed=False)
    directory = project / 'Tomograms' / 'job001'
    assert (directory / JOB_SUCCESS_FILENAME).is_file()
    assert not (directory / JOB_FAILURE_FILENAME).exists()
    process = pipeline_module.read_pipeline(project).find_process('Tomograms/job001')
    assert process.status == pipeline_module.STATUS_SUCCEEDED


# ----------------------------------------------------------------- clean
def test_clean_requires_relion_pipeliner(project, monkeypatch):
    """Cleanup rules live in RELION; without the program the action says so."""
    monkeypatch.setattr(job_actions.shutil, 'which', lambda name: None)
    with pytest.raises(job_actions.ActionError, match='relion_pipeliner'):
        job_actions.clean(project, 'Tomograms/job001', harsh=False)


def test_clean_refuses_an_unfinished_job(project):
    p = pipeline_module.read_pipeline(project)
    pipeline_module.update_process_status(
        p, 'Tomograms/job001', pipeline_module.STATUS_RUNNING
    )
    pipeline_module.write_pipeline(project, p)
    with pytest.raises(job_actions.ActionError, match='only successfully finished'):
        job_actions.clean(project, 'Tomograms/job001', harsh=True)


def test_clean_delegates_to_relion_pipeliner(project, monkeypatch):
    """When the program is present, its flags are what get used."""
    calls = []

    class Done:
        returncode = 0
        stderr = ''
        stdout = ''

    def fake_run(args, cwd=None, capture_output=None, text=None, timeout=None):
        calls.append((args, cwd))
        return Done()

    monkeypatch.setattr(job_actions.shutil, 'which', lambda name: '/usr/bin/relion_pipeliner')
    monkeypatch.setattr(job_actions.subprocess, 'run', fake_run)

    job_actions.clean(project, 'Tomograms/job001', harsh=True)
    args, cwd = calls[0]
    assert '--harsh_clean' in args
    assert '1' in args
    # relion_pipeliner resolves --pipeline relative to the working directory.
    assert Path(cwd) == project
