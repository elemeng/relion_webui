"""Tests for the web agent's login, session and multi-project handling.

The SSH check itself is exercised against the machine's own sshd, so these tests
need a working sshd and are skipped otherwise. A fake checker covers the logic
that does not depend on the network.
"""

import json
import time
from pathlib import Path

import pytest

from recryo import pipeline
from recryo.auth import (
    AuthError,
    AuthManager,
    check_ssh_credentials,
)
from recryo.projects import ProjectRegistry

fastapi = pytest.importorskip('fastapi')
pytest.importorskip('httpx')

from fastapi.testclient import TestClient  # noqa: E402

from recryo.app import create_app  # noqa: E402


def make_project(directory: Path, job_counter: int = 3) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    pipeline.write_pipeline(directory, pipeline.Pipeline(job_counter=job_counter))
    return directory


# ------------------------------------------------------------------- sessions
def test_login_mints_session_that_expires():
    manager = AuthManager(session_lifetime=0.05)
    manager.login = lambda *a, **k: _fake_login(manager, 0.05)
    session = manager.login('someone', 'secret')
    assert manager.get_session(session.token) is not None
    time.sleep(0.08)
    assert manager.get_session(session.token) is None


def _fake_login(manager, lifetime):
    from recryo.auth import Session

    session = Session(
        token='tok', username='someone', created_at=time.time(),
        expires_at=time.time() + lifetime,
    )
    manager._sessions[session.token] = session
    return session


def test_logout_revokes_session():
    manager = AuthManager(session_lifetime=60)
    manager.login = lambda *a, **k: _fake_login(manager, 60)
    session = manager.login('someone', 'secret')
    assert manager.logout(session.token) is True
    assert manager.get_session(session.token) is None


def test_unknown_token_has_no_session():
    assert AuthManager().get_session('nope') is None
    assert AuthManager().get_session(None) is None


def test_repeated_failures_lock_out_the_client(monkeypatch):
    manager = AuthManager(session_lifetime=60, max_failures=3, failure_window=60)

    def reject(*_args, **_kwargs):
        return False, 'invalid username or password'

    monkeypatch.setattr(
        'recryo.auth.check_ssh_credentials', reject
    )
    for _ in range(3):
        with pytest.raises(AuthError):
            manager.login('user', 'wrong', remote='10.0.0.9')

    # The next attempt is refused without even consulting sshd.
    called = []
    monkeypatch.setattr(
        'recryo.auth.check_ssh_credentials',
        lambda *a, **k: called.append(1) or (False, 'rejected'),
    )
    with pytest.raises(AuthError, match='too many failed attempts'):
        manager.login('user', 'wrong', remote='10.0.0.9')
    assert called == []


def test_successful_login_clears_failure_count(monkeypatch):
    manager = AuthManager(session_lifetime=60, max_failures=5, failure_window=60)
    results = [(False, 'bad'), (True, '')]
    monkeypatch.setattr(
        'recryo.auth.check_ssh_credentials',
        lambda *a, **k: results.pop(0),
    )
    with pytest.raises(AuthError):
        manager.login('user', 'wrong', remote='10.0.0.9')
    manager.login('user', 'right', remote='10.0.0.9')
    assert not manager.locked_out('10.0.0.9')


def test_missing_credentials_are_rejected_without_ssh():
    ok, message = check_ssh_credentials('', '')
    assert ok is False
    assert 'required' in message


def _sshd_available() -> bool:
    try:
        import paramiko
    except ImportError:
        return False
    import socket

    try:
        with socket.create_connection(('127.0.0.1', 22), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _sshd_available(), reason='needs a local sshd on port 22')
def test_sshd_rejects_a_wrong_password():
    """The check must be a real SSH login, not a stub that always passes."""
    ok, message = check_ssh_credentials(
        'definitely-not-a-user-relionweb', 'wrong-password', timeout=8
    )
    assert ok is False
    assert 'invalid' in message or 'SSH' in message


# ------------------------------------------------------------ app integration
@pytest.fixture
def auth_app(tmp_path):
    projects = ProjectRegistry(tmp_path / 'config')
    auth = AuthManager(session_lifetime=60)
    app = create_app(roots=[tmp_path], auth=auth, projects=projects)
    return app, auth, projects


def test_unauthenticated_api_is_rejected(auth_app):
    app, _, _ = auth_app
    with TestClient(app) as client:
        response = client.get('/api/config')
    assert response.status_code == 401


def test_health_needs_no_login(auth_app):
    """Monitors cannot log in, so liveness must stay open."""
    app, _, _ = auth_app
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200


def test_index_serves_the_spa_when_signed_out(auth_app):
    """The shell is served either way; the client shows the login view.

    Login is a React view rather than a separate document, so the HTML is the
    same signed in or out and the bundle decides. What must hold is that the
    API stays closed until a session exists (covered separately).
    """
    app, _, _ = auth_app
    with TestClient(app) as client:
        response = client.get('/')
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text
    assert '/assets/app.js' in response.text


def test_login_endpoint_sets_cookie(auth_app, monkeypatch):
    app, auth, _ = auth_app
    monkeypatch.setattr(
        AuthManager, 'login',
        lambda self, u, p, remote='': _fake_login(self, 60),
    )
    with TestClient(app) as client:
        response = client.post(
            '/api/login', json={'username': 'someone', 'password': 'secret'}
        )
        assert response.status_code == 200
        assert 'relion_web_session' in response.cookies

        # The cookie now unlocks the API.
        assert client.get('/api/config').status_code == 200
        assert client.get('/api/session').json()['username'] == 'someone'


def test_failed_login_returns_401(auth_app, monkeypatch):
    app, _, _ = auth_app

    def reject(self, u, p, remote=''):
        raise AuthError('invalid username or password')

    monkeypatch.setattr(AuthManager, 'login', reject)
    with TestClient(app) as client:
        response = client.post(
            '/api/login', json={'username': 'someone', 'password': 'nope'}
        )
    assert response.status_code == 401
    assert 'invalid' in response.json()['error']


def test_logout_clears_the_session(auth_app, monkeypatch):
    app, _, _ = auth_app
    monkeypatch.setattr(
        AuthManager, 'login',
        lambda self, u, p, remote='': _fake_login(self, 60),
    )
    with TestClient(app) as client:
        client.post('/api/login', json={'username': 'someone', 'password': 'x'})
        assert client.get('/api/config').status_code == 200
        client.post('/api/logout')
        assert client.get('/api/config').status_code == 401


def test_websocket_requires_a_session(auth_app):
    app, _, _ = auth_app
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect('/ws/jobs') as websocket:
                websocket.receive_text()


# ------------------------------------------------------------------- projects
def test_project_registry_add_and_persist(tmp_path):
    config = tmp_path / 'config'
    project = make_project(tmp_path / 'projA')
    registry = ProjectRegistry(config)
    registry.add(project)

    # A fresh instance re-reads the saved list.
    reloaded = ProjectRegistry(config)
    assert [e.path for e in reloaded.list()] == [str(project.resolve())]


def test_project_registry_initialises_an_empty_directory(tmp_path):
    """An empty folder is a project in waiting; RELION's GUI does the same."""
    registry = ProjectRegistry(tmp_path / 'config')
    empty = tmp_path / 'empty_project'
    empty.mkdir()

    entry = registry.add(empty)
    assert entry.path == str(empty)
    assert (empty / 'default_pipeline.star').is_file()


def test_project_registry_rejects_a_non_empty_directory(tmp_path):
    """Files but no pipeline: refuse rather than write into someone's data."""
    registry = ProjectRegistry(tmp_path / 'config')
    used = tmp_path / 'has_files'
    used.mkdir()
    (used / 'notes.txt').write_text('data')

    with pytest.raises(ValueError, match='contains files but no'):
        registry.add(used)
    # And nothing was written on the way out.
    assert sorted(p.name for p in used.iterdir()) == ['notes.txt']


def test_project_registry_remove(tmp_path):
    config = tmp_path / 'config'
    project = make_project(tmp_path / 'projA')
    registry = ProjectRegistry(config)
    registry.add(project)
    assert registry.remove(project) is True
    assert registry.list() == []
    assert ProjectRegistry(config).list() == []


def test_projects_api_lists_and_adds(auth_app, tmp_path, monkeypatch):
    app, auth, _ = auth_app
    monkeypatch.setattr(
        AuthManager, 'login',
        lambda self, u, p, remote='': _fake_login(self, 60),
    )
    project = make_project(tmp_path / 'projA')
    with TestClient(app) as client:
        client.post('/api/login', json={'username': 'someone', 'password': 'x'})

        assert client.get('/api/projects').json()['projects'] == []

        response = client.post('/api/projects', json={'path': str(project)})
        assert response.status_code == 201

        listed = client.get('/api/projects').json()['projects']
        assert len(listed) == 1
        assert listed[0]['path'] == str(project.resolve())
        assert listed[0]['readable'] is True
        assert listed[0]['job_count'] == 0

        client.delete('/api/projects', params={'path': str(project)})
        assert client.get('/api/projects').json()['projects'] == []


def test_projects_api_initialises_an_empty_directory(auth_app, tmp_path, monkeypatch):
    app, _, _ = auth_app
    monkeypatch.setattr(
        AuthManager, 'login',
        lambda self, u, p, remote='': _fake_login(self, 60),
    )
    empty = tmp_path / 'fresh'
    empty.mkdir()
    with TestClient(app) as client:
        client.post('/api/login', json={'username': 'someone', 'password': 'x'})
        response = client.post('/api/projects', json={'path': str(empty)})

    assert response.status_code == 201
    assert (empty / 'default_pipeline.star').is_file()


def test_projects_api_rejects_a_directory_with_files(auth_app, tmp_path, monkeypatch):
    app, _, _ = auth_app
    monkeypatch.setattr(
        AuthManager, 'login',
        lambda self, u, p, remote='': _fake_login(self, 60),
    )
    used = tmp_path / 'plain'
    used.mkdir()
    (used / 'notes.txt').write_text('x')
    with TestClient(app) as client:
        client.post('/api/login', json={'username': 'someone', 'password': 'x'})
        response = client.post('/api/projects', json={'path': str(used)})
    assert response.status_code == 400
    assert 'default_pipeline.star' in response.json()['error']


def test_switching_projects_returns_each_pipeline(auth_app, tmp_path, monkeypatch):
    """The point of the project list: several projects, one agent."""
    app, _, _ = auth_app
    monkeypatch.setattr(
        AuthManager, 'login',
        lambda self, u, p, remote='': _fake_login(self, 60),
    )
    first = make_project(tmp_path / 'projA', job_counter=2)
    second = make_project(tmp_path / 'projB', job_counter=7)

    with TestClient(app) as client:
        client.post('/api/login', json={'username': 'someone', 'password': 'x'})
        for project in (first, second):
            client.post('/api/projects', json={'path': str(project)})

        a = client.get(
            '/api/project', params={'project_directory': str(first)}
        ).json()
        b = client.get(
            '/api/project', params={'project_directory': str(second)}
        ).json()
    assert a['job_counter'] == 2
    assert b['job_counter'] == 7
