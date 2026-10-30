"""The project list summary behind the projects page.

``summarize`` is what the project cards render: job count, status breakdown,
recent jobs and type count. It runs inside the agent's request path, so it must
also survive a directory that has gone away or is not a RELION project - the
list is the access boundary and must not fail as a whole because one entry did.
"""

from pathlib import Path

import pytest

from relion_webui import pipeline
from relion_webui.projects import ProjectRegistry

LABEL_IMPORT = 'relion.importtomo'
LABEL_CTF = 'relion.ctffind'


def make_project(root: Path, jobs):
    """Write a project with (job_type_label, status, alias) triples."""
    root.mkdir(parents=True, exist_ok=True)
    pipeline.write_pipeline(root, pipeline.Pipeline(job_counter=1))
    state = pipeline.read_pipeline(root)
    for index, (label, status, alias) in enumerate(jobs, start=1):
        process = pipeline.Process(
            name=f'Job{index:03d}/job{index:03d}',
            alias=alias,
            type_label=label,
            status=status,
        )
        node = pipeline.Node(
            name=f'{process.name}/out.star',
            type_label=label,
            type_depth=0,
        )
        pipeline.register_process(state, process, (), (node,))
    pipeline.write_pipeline(root, state)
    return root


def registry_with(tmp_path: Path, *paths: Path) -> ProjectRegistry:
    registry = ProjectRegistry(tmp_path / 'config')
    for path in paths:
        registry.add(str(path))
    return registry


def test_summary_reports_counts_and_statuses(tmp_path):
    project = make_project(
        tmp_path / 'proj',
        [
            (LABEL_IMPORT, 'Succeeded', 'import'),
            (LABEL_CTF, 'Running', 'ctf'),
            (LABEL_CTF, 'Failed', 'ctf again'),
        ],
    )
    summary = registry_with(tmp_path, project).summarize()

    assert len(summary) == 1
    entry = summary[0]
    assert entry['readable'] is True
    assert entry['job_count'] == 3
    assert entry['statuses'] == {'Succeeded': 1, 'Running': 1, 'Failed': 1}
    # Two type labels, so two distinct types - not three jobs.
    assert entry['types'] == 2


def test_summary_recent_jobs_are_newest_first(tmp_path):
    project = make_project(
        tmp_path / 'proj',
        [
            (LABEL_IMPORT, 'Succeeded', 'first'),
            (LABEL_CTF, 'Succeeded', 'second'),
            (LABEL_CTF, 'Succeeded', 'third'),
        ],
    )
    recent = registry_with(tmp_path, project).summarize()[0]['recent']

    assert [job['job_number'] for job in recent] == [3, 2, 1]
    assert [job['alias'] for job in recent] == ['third', 'second', 'first']


def test_summary_recent_is_capped(tmp_path):
    project = make_project(
        tmp_path / 'proj',
        [(LABEL_CTF, 'Succeeded', f'job {n}') for n in range(1, 8)],
    )
    recent = registry_with(tmp_path, project).summarize()[0]['recent']
    assert len(recent) == 3


def test_summary_marks_a_missing_directory_unreadable(tmp_path):
    project = make_project(tmp_path / 'gone', [(LABEL_CTF, 'Succeeded', 'x')])
    registry = registry_with(tmp_path, project)
    for child in sorted(project.iterdir()):
        child.unlink()
    project.rmdir()

    entry = registry.summarize()[0]
    assert entry['readable'] is False
    # No stale numbers: a card must not claim jobs that cannot be read.
    assert 'job_count' not in entry
    assert 'statuses' not in entry


def test_adding_an_empty_directory_creates_a_project(tmp_path):
    """An empty folder is the normal way to start a project."""
    empty = tmp_path / 'plain'
    empty.mkdir()
    registry = ProjectRegistry(tmp_path / 'config')

    registry.add(str(empty))
    assert (empty / pipeline.PIPELINE_FILENAME).is_file()
    # And it is now readable, with no jobs.
    summary = registry.summarize()[0]
    assert summary['readable'] is True
    assert summary['job_count'] == 0


def test_adding_a_directory_with_files_is_refused(tmp_path):
    """Writing a pipeline into an existing data directory is not recoverable."""
    used = tmp_path / 'plain'
    used.mkdir()
    (used / 'tomogram.mrc').write_bytes(b'x')
    registry = ProjectRegistry(tmp_path / 'config')

    with pytest.raises(ValueError, match='contains files but no'):
        registry.add(str(used))
    assert not (used / pipeline.PIPELINE_FILENAME).exists()


def test_summary_keeps_other_projects_when_one_loses_its_pipeline(tmp_path):
    good = make_project(tmp_path / 'good', [(LABEL_CTF, 'Running', 'ok')])
    other = make_project(tmp_path / 'other', [(LABEL_CTF, 'Failed', 'bad')])
    registry = registry_with(tmp_path, good, other)

    # A pipeline moved away under the agent: the entry stays listed, but must
    # report itself unreadable instead of failing the whole page.
    (other / pipeline.PIPELINE_FILENAME).unlink()

    summary = registry.summarize()
    assert [entry['readable'] for entry in summary] == [True, False]
    assert summary[0]['job_count'] == 1
    assert 'statuses' not in summary[1]


# ----------------------------------------------------------- job.star reading
def test_read_job_star_round_trips_options(tmp_path):
    """What the parameters tab shows comes from here, so it must round-trip."""
    from relion_webui import job_artifacts

    directory = tmp_path / 'Import' / 'job001'
    job_artifacts.write_job_star(
        directory,
        'relion.importtomo',
        {'fn_in': '/data/tilt.star', 'nr_mpi': 4, 'do_queue': True, 'angpix': 0.675},
        is_tomo=True,
    )

    options = job_artifacts.read_job_star(directory)
    assert options == {
        'fn_in': '/data/tilt.star',
        'nr_mpi': '4',
        # RELION stores booleans as Yes/No; the writer must not emit true/false,
        # or the GUI would read every flag as false.
        'do_queue': 'Yes',
        'angpix': '0.675',
    }
    assert job_artifacts.read_job_type_label(directory) == 'relion.importtomo'


def test_read_job_star_is_none_without_the_file(tmp_path):
    """A RELION 3.x job has run.job only; that is not an error, but not data."""
    from relion_webui import job_artifacts

    directory = tmp_path / 'Import' / 'job002'
    directory.mkdir(parents=True)
    assert job_artifacts.read_job_star(directory) is None
    assert job_artifacts.read_job_type_label(directory) is None


# ------------------------------------------------------------ config location
def test_the_project_list_lives_beside_relions_own_config(tmp_path, monkeypatch):
    """`$HOME/.relion_web`, next to the `$HOME/.relion` RELION itself writes."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('RELION_WEB_HOME', raising=False)
    from relion_webui.projects import default_config_directory

    assert default_config_directory() == tmp_path / '.relion_web'


def test_relion_web_home_still_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('RELION_WEB_HOME', str(tmp_path / 'custom'))
    from relion_webui.projects import default_config_directory

    assert default_config_directory() == tmp_path / 'custom'


def test_an_old_xdg_list_is_carried_over(tmp_path, monkeypatch):
    """A moved config path must not look like the agent forgot every project."""
    import json

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('RELION_WEB_HOME', raising=False)
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)

    legacy = tmp_path / '.config' / 'relion-web'
    legacy.mkdir(parents=True)
    (legacy / 'projects.json').write_text(
        json.dumps({'projects': [{'path': '/tmp/old', 'added_at': 1.0}]})
    )

    registry = ProjectRegistry()
    assert registry.config_directory == tmp_path / '.relion_web'
    assert [entry.path for entry in registry.list()] == ['/tmp/old']
    # Copied, not moved: the old file is still there if anything went wrong.
    assert (legacy / 'projects.json').is_file()


def test_an_existing_new_list_is_not_overwritten_by_the_old_one(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('RELION_WEB_HOME', raising=False)
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)

    new = tmp_path / '.relion_web'
    new.mkdir()
    (new / 'projects.json').write_text(
        json.dumps({'projects': [{'path': '/tmp/new', 'added_at': 2.0}]})
    )
    legacy = tmp_path / '.config' / 'relion-web'
    legacy.mkdir(parents=True)
    (legacy / 'projects.json').write_text(
        json.dumps({'projects': [{'path': '/tmp/old', 'added_at': 1.0}]})
    )

    registry = ProjectRegistry()
    assert [entry.path for entry in registry.list()] == ['/tmp/new']


def test_an_explicit_config_directory_does_not_adopt_the_legacy_list(tmp_path, monkeypatch):
    """Tests and embedders pass a path; it means "use this list"."""
    import json

    monkeypatch.setenv('HOME', str(tmp_path))
    legacy = tmp_path / '.config' / 'relion-web'
    legacy.mkdir(parents=True)
    (legacy / 'projects.json').write_text(
        json.dumps({'projects': [{'path': '/tmp/old', 'added_at': 1.0}]})
    )

    mine = tmp_path / 'mine'
    registry = ProjectRegistry(config_directory=mine)
    assert registry.list() == []
    assert not (mine / 'projects.json').exists()
