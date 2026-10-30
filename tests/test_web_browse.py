"""Tests for the agent-side directory browser behind the path pickers.

The browser is intentionally not limited to the allowed roots (see browse.py);
these tests pin the behaviour that matters: filtering, project detection,
truncation and honest errors.
"""

from pathlib import Path

import pytest

from recryo import browse, pipeline

fastapi = pytest.importorskip('fastapi')
pytest.importorskip('httpx')

from fastapi.testclient import TestClient  # noqa: E402

from recryo.app import create_app  # noqa: E402


@pytest.fixture
def tree(tmp_path):
    """A small tree with a project, a plain directory and a STAR file."""
    project = tmp_path / 'myproject'
    project.mkdir()
    pipeline.write_pipeline(project, pipeline.Pipeline(job_counter=2))

    plain = tmp_path / 'plain'
    plain.mkdir()

    (tmp_path / 'data.star').write_text('# star\n')
    (tmp_path / 'notes.txt').write_text('hello\n')
    (tmp_path / '.hidden').mkdir()
    return tmp_path


# ------------------------------------------------------------------- listing
def test_lists_directories_and_files(tree):
    listing = browse.list_directory(str(tree))
    names = {entry.name for entry in listing.entries}
    assert 'myproject' in names
    assert 'plain' in names
    assert 'data.star' in names
    assert 'notes.txt' in names


def test_hidden_entries_are_skipped_by_default(tree):
    listing = browse.list_directory(str(tree))
    assert '.hidden' not in {entry.name for entry in listing.entries}

    shown = browse.list_directory(str(tree), show_hidden=True)
    assert '.hidden' in {entry.name for entry in shown.entries}


def test_project_filter_marks_projects_and_keeps_directories(tree):
    listing = browse.list_directory(str(tree), filter_kind='project')
    by_name = {entry.name: entry for entry in listing.entries}

    # Directories stay listed so the browser remains walkable: hiding them made
    # every directory unreachable and the picker showed "nothing here".
    assert set(by_name) == {'myproject', 'plain'}
    assert by_name['myproject'].is_project is True
    assert by_name['plain'].is_project is False
    # Files are what the filter removes.
    assert 'data.star' not in by_name
    assert 'notes.txt' not in by_name


def test_star_filter_marks_star_files_and_keeps_directories(tree):
    listing = browse.list_directory(str(tree), filter_kind='star')
    by_name = {entry.name: entry for entry in listing.entries}

    assert set(by_name) == {'data.star', 'myproject', 'plain'}
    assert by_name['data.star'].is_star is True
    assert by_name['myproject'].is_star is False
    assert by_name['myproject'].is_dir is True


def test_directory_filter_excludes_files(tree):
    listing = browse.list_directory(str(tree), filter_kind='directory')
    names = {entry.name for entry in listing.entries}
    assert names == {'myproject', 'plain'}


def test_every_filter_can_still_reach_a_nested_directory(tree):
    """The regression that prompted this: a filter must never strand you."""
    nested = tree / 'plain' / 'deeper'
    nested.mkdir()

    for wanted in ('all', 'directory', 'project', 'star'):
        listing = browse.list_directory(str(tree), filter_kind=wanted)
        reachable = {entry.name for entry in listing.entries if entry.is_dir}
        assert 'plain' in reachable, f'{wanted} filter hid a directory'

        inside = browse.list_directory(str(tree / 'plain'), filter_kind=wanted)
        assert 'deeper' in {entry.name for entry in inside.entries}, wanted


def test_parent_is_reported(tree):
    listing = browse.list_directory(str(tree / 'plain'))
    assert listing.parent == str(tree)


def test_root_has_no_parent():
    listing = browse.list_directory('/')
    assert listing.parent is None


def test_unknown_filter_is_rejected(tree):
    with pytest.raises(ValueError, match='unknown filter'):
        browse.list_directory(str(tree), filter_kind='nope')


def test_missing_directory_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='no such directory'):
        browse.list_directory(str(tmp_path / 'nope'))


def test_file_where_directory_expected_is_rejected(tree):
    with pytest.raises(ValueError, match='not a directory'):
        browse.list_directory(str(tree / 'notes.txt'))


def test_entries_sorted_case_insensitively(tree):
    (tree / 'Zebra').mkdir()
    (tree / 'apple').mkdir()
    names = [e.name for e in browse.list_directory(str(tree)).entries if e.is_dir]
    assert names == sorted(names, key=str.lower)


def test_truncation_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(browse, 'MAX_ENTRIES', 3)
    for index in range(10):
        (tmp_path / f'dir{index}').mkdir()
    listing = browse.list_directory(str(tmp_path))
    assert len(listing.entries) == 3
    assert listing.truncated is True


def test_stat_describes_paths(tree):
    info = browse.stat_path(str(tree / 'myproject'))
    assert info['exists'] is True
    assert info['is_dir'] is True
    assert info['is_project'] is True

    star = browse.stat_path(str(tree / 'data.star'))
    assert star['is_star'] is True
    assert star['is_dir'] is False

    missing = browse.stat_path(str(tree / 'ghost'))
    assert missing['exists'] is False


# ----------------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path):
    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        yield test_client


def test_browse_endpoint(client, tree):
    response = client.get('/api/browse', params={'path': str(tree)})
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == str(tree)
    assert any(entry['name'] == 'myproject' for entry in payload['entries'])


def test_browse_endpoint_filters(client, tree):
    response = client.get(
        '/api/browse', params={'path': str(tree), 'filter': 'project'}
    )
    payload = response.json()
    by_name = {entry['name']: entry for entry in payload['entries']}
    # Directories remain navigable; the filter marks which are projects.
    assert set(by_name) == {'myproject', 'plain'}
    assert by_name['myproject']['is_project'] is True
    assert by_name['plain']['is_project'] is False


def test_browse_endpoint_reports_bad_path(client, tmp_path):
    response = client.get('/api/browse', params={'path': str(tmp_path / 'ghost')})
    assert response.status_code == 400


def test_browse_stat_endpoint(client, tree):
    response = client.get('/api/browse/stat', params={'path': str(tree / 'data.star')})
    assert response.status_code == 200
    assert response.json()['is_star'] is True


def test_browse_locations_endpoint(client):
    response = client.get('/api/browse/locations')
    assert response.status_code == 200
    labels = {location['label'] for location in response.json()['locations']}
    assert 'home' in labels


def test_browse_requires_login_when_auth_is_on(tmp_path):
    from recryo.auth import AuthManager
    from recryo.projects import ProjectRegistry

    app = create_app(
        roots=[tmp_path],
        auth=AuthManager(session_lifetime=60),
        projects=ProjectRegistry(tmp_path / 'cfg'),
    )
    with TestClient(app) as test_client:
        response = test_client.get('/api/browse', params={'path': str(tmp_path)})
    assert response.status_code == 401


def test_browse_can_see_paths_outside_the_roots(tmp_path):
    """Adding the first project requires browsing where no root exists yet."""
    inside = tmp_path / 'roots'
    inside.mkdir()
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    pipeline.write_pipeline(outside, pipeline.Pipeline(job_counter=1))

    app = create_app(roots=[inside])
    with TestClient(app) as test_client:
        response = test_client.get(
            '/api/browse', params={'path': str(tmp_path), 'filter': 'project'}
        )
    assert response.status_code == 200
    by_name = {entry['name']: entry for entry in response.json()['entries']}
    assert by_name['elsewhere']['is_project'] is True
    # And it is reachable: `inside` is a directory, so it is listed too.
    assert by_name['roots']['is_dir'] is True


# ------------------------------------------------------------- new folder
def test_make_directory_creates_one_level(tmp_path):
    created = browse.make_directory(str(tmp_path), 'new-project')
    assert created['name'] == 'new-project'
    assert Path(created['path']).is_dir()
    # Empty: creating a folder must not seed it with anything.
    assert list(Path(created['path']).iterdir()) == []


def test_make_directory_refuses_an_existing_entry(tmp_path):
    (tmp_path / 'taken').mkdir()
    with pytest.raises(ValueError, match='already exists'):
        browse.make_directory(str(tmp_path), 'taken')


def test_make_directory_refuses_an_existing_file(tmp_path):
    (tmp_path / 'file.txt').write_text('x')
    with pytest.raises(ValueError, match='already exists'):
        browse.make_directory(str(tmp_path), 'file.txt')


@pytest.mark.parametrize('name', ['', '   ', '.', '..', 'a/b', '.hidden'])
def test_make_directory_refuses_anything_but_a_plain_name(tmp_path, name):
    """The picker creates one folder in the directory it is showing."""
    with pytest.raises(ValueError):
        browse.make_directory(str(tmp_path), name)


def test_make_directory_requires_an_existing_parent(tmp_path):
    with pytest.raises(ValueError, match='not a directory'):
        browse.make_directory(str(tmp_path / 'missing'), 'child')


def test_hidden_entries_can_be_listed(tmp_path):
    (tmp_path / '.cache').mkdir()
    (tmp_path / 'visible').mkdir()

    shown = browse.list_directory(str(tmp_path), show_hidden=True)
    assert '.cache' in {entry.name for entry in shown.entries}

    default = browse.list_directory(str(tmp_path))
    assert '.cache' not in {entry.name for entry in default.entries}


def test_browse_folder_endpoint(client, tmp_path):
    response = client.post(
        '/api/browse/folder', json={'parent': str(tmp_path), 'name': 'made-here'}
    )
    assert response.status_code == 201
    assert Path(response.json()['path']).is_dir()


def test_browse_folder_endpoint_reports_a_bad_name(client, tmp_path):
    response = client.post(
        '/api/browse/folder', json={'parent': str(tmp_path), 'name': '../escape'}
    )
    assert response.status_code == 400
    assert 'not a folder name' in response.json()['error']


def test_browse_endpoint_passes_hidden_through(client, tree):
    (tree / '.dotdir').mkdir()
    payload = client.get(
        '/api/browse', params={'path': str(tree), 'hidden': 'true'}
    ).json()
    assert '.dotdir' in {entry['name'] for entry in payload['entries']}
