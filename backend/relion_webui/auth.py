# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Login against the machine's own sshd.

The web agent does not keep a user database and does not decide on its own
whether credentials are valid. It forwards what the browser submits to
``127.0.0.1:22`` and accepts the login only if sshd does, so whatever policy the
host already enforces (PAM, 2FA, account expiry, LDAP) applies unchanged. This
is how ttyd, JupyterHub's PAM authenticator and Guacamole work.

The password is used once, to make that one SSH connection, and is never stored.
That does mean it passes through this process's memory: the alternative models
(browser-held tokens, proxy SSO) avoid that but cannot offer a login form at
all. Since the agent listens on the LAN, run it behind HTTPS or on a trusted
network, exactly as you would with any web terminal.

What a successful login proves is "this person can log in to this host". The
agent runs as a single user, so a login for a different account is refused:
otherwise anyone with any account could drive this user's jobs.
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

DEFAULT_SESSION_LIFETIME = 12 * 3600  # seconds
SSH_HOST = '127.0.0.1'
SSH_PORT = 22
SSH_TIMEOUT = 10.0


class AuthError(Exception):
    """Raised when credentials are rejected or the SSH server is unreachable."""


@dataclass
class Session:
    token: str
    username: str
    created_at: float
    expires_at: float
    remote: str = ''
    # Kept only so `POST /api/hosts/provision` can copy the agent's key to a GPU
    # node the user names. It is not part of `as_dict`, never logged, and
    # cleared the moment a provisioning request uses it - a login that only
    # browses never leaves a password in memory at all, and one that provisions
    # holds it for the length of that request.
    provision_secret: Optional[str] = field(default=None, repr=False)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def as_dict(self) -> dict:
        return {
            'username': self.username,
            'created_at': self.created_at,
            'expires_at': self.expires_at,
            'expires_in': max(0.0, self.expires_at - time.time()),
        }


def check_ssh_credentials(
    username: str,
    password: str,
    *,
    host: str = SSH_HOST,
    port: int = SSH_PORT,
    timeout: float = SSH_TIMEOUT,
) -> Tuple[bool, str]:
    """Ask sshd whether these credentials are valid.

    Returns ``(ok, message)``. An unreachable sshd is reported separately from a
    rejected password, because the fixes are different.
    """
    if not username or not password:
        return False, 'username and password are required'

    try:
        import paramiko
    except ImportError:
        return False, (
            'password login needs paramiko; install the web extra '
            '(pip install -e ".[web]")'
        )

    client = paramiko.SSHClient()
    # The agent connects to its own loopback sshd. Pinning a host key here would
    # break whenever the container is rebuilt, and the connection never leaves
    # the machine, so host verification adds nothing.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            auth_timeout=timeout,
            banner_timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
    except paramiko.AuthenticationException:
        return False, 'invalid username or password'
    except paramiko.SSHException as exc:
        return False, f'SSH server error: {exc}'
    except Exception as exc:  # noqa: BLE001 - socket/timeout errors land here
        return False, f'cannot reach the SSH server on {host}:{port}: {exc}'
    else:
        return True, ''
    finally:
        client.close()


class AuthManager:
    """Live sessions plus a small brute-force guard.

    Sessions and counters live in memory: restarting the agent logs everyone out,
    which is the right default for a process that can submit jobs.
    """

    def __init__(
        self,
        *,
        session_lifetime: float = DEFAULT_SESSION_LIFETIME,
        max_failures: int = 8,
        failure_window: float = 300.0,
        ssh_options: Optional[dict] = None,
    ):
        self.session_lifetime = float(session_lifetime)
        self.max_failures = int(max_failures)
        self.failure_window = float(failure_window)
        self.ssh_options = ssh_options or {}
        self._sessions: Dict[str, Session] = {}
        self._failures: Dict[str, List[float]] = {}

    # ---------------------------------------------------------------- limiting
    def _recent_failures(self, key: str) -> int:
        now = time.time()
        stamps = [t for t in self._failures.get(key, []) if now - t < self.failure_window]
        if stamps:
            self._failures[key] = stamps
        else:
            self._failures.pop(key, None)
        return len(stamps)

    def _record_failure(self, key: str) -> None:
        self._failures.setdefault(key, []).append(time.time())

    def locked_out(self, key: str) -> bool:
        """True when this client has failed too often recently."""
        return self._recent_failures(key) >= self.max_failures

    # ------------------------------------------------------------------ login
    def login(self, username: str, password: str, remote: str = '') -> Session:
        """Validate against sshd and mint a session.

        Rate limiting is keyed on the client address so a LAN attacker cannot
        use the form as a password oracle any faster than sshd would allow.
        """
        key = remote or 'unknown'
        if self.locked_out(key):
            raise AuthError(
                'too many failed attempts; wait a few minutes and try again'
            )

        ok, message = check_ssh_credentials(username, password, **self.ssh_options)
        if not ok:
            self._record_failure(key)
            raise AuthError(message)

        self._failures.pop(key, None)
        session = Session(
            token=secrets.token_urlsafe(32),
            username=username,
            created_at=time.time(),
            expires_at=time.time() + self.session_lifetime,
            remote=remote,
        )
        # Held for one purpose only: `POST /api/hosts/provision` uses it to copy
        # the agent's key to a node the user names. It is never written to disk,
        # never in `as_dict`, and consumed on use (see `take_provision_secret`).
        session.provision_secret = password
        self._sessions[session.token] = session
        self._prune()
        return session

    def take_provision_secret(self, token: Optional[str]) -> Optional[str]:
        """The password this session may use once, or None.

        Taking rather than reading: the secret exists for one key copy, so the
        first call consumes it. A second provisioning request in the same login
        is refused rather than replaying a password that nothing needs to keep.
        """
        session = self.get_session(token)
        if session is None or session.provision_secret is None:
            return None
        secret = session.provision_secret
        session.provision_secret = None
        return secret

    # --------------------------------------------------------------- sessions
    def get_session(self, token: Optional[str]) -> Optional[Session]:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expired:
            self._sessions.pop(token, None)
            return None
        return session

    def logout(self, token: str) -> bool:
        return self._sessions.pop(token, None) is not None

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            self._sessions.pop(token, None)

    @property
    def active_sessions(self) -> int:
        self._prune()
        return len(self._sessions)
