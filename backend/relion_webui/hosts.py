# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Where a job runs: this machine, or a named GPU node on the local network.

The deployment this models is a small cluster of GPU machines that share a
storage mount and accept the same account: the agent can see a project, and so
can every node, at the same path. That shared path is the whole reason this is
tractable - the agent writes the job directory locally and the node reads the
same directory, so nothing has to be copied.

**Authentication is the constraint to understand.** Browser logins are checked
against sshd and the password is deliberately never retained (see `auth.py`), so
the agent *cannot* log in to a node on the user's behalf with a password. Remote
execution therefore requires key-based access from the agent's account to each
node - the operator runs `ssh-copy-id` once, as they would for any batch tool.
`BatchMode=yes` makes a missing key fail immediately with a clear message rather
than hanging on a password prompt nobody can answer.

A node is declared once and referred to by name thereafter:

    relion_webui --host-node gpu1=gpu1.lab --host-node gpu2=gpu2.lab:2222

`local` is always available and is the default, so an existing single-machine
install is unchanged.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

LOCAL_NAME = 'local'
# Letters, digits, underscore, dash - the same rule MCP uses for server names,
# so a node name can be passed through either protocol unchanged.
_VALID_NAME = re.compile(r'^[A-Za-z0-9_-]+$')


class HostError(RuntimeError):
    """A requested execution host is unknown or unusable; user-facing."""


@dataclass(frozen=True)
class ExecutionHost:
    """One machine a job may run on.

    `name` is what the user and the model say; `hostname` is what ssh resolves.
    Keeping them separate means a node can be renamed in DNS without changing
    saved preferences or agent prompts.
    """

    name: str
    hostname: str = ''
    port: int = 22
    user: Optional[str] = None
    # A disabled node stays defined but is not offered for new jobs and is not
    # polled for metrics. Turning it off is how a node is taken out of service
    # for maintenance without losing its definition.
    enabled: bool = True
    # Free-text, e.g. "2x A100, 40 GB".
    note: str = ''

    @property
    def is_local(self) -> bool:
        return self.name == LOCAL_NAME

    def ssh_target(self) -> str:
        """``user@host`` for ssh, or just ``host`` when the user matches."""
        return f'{self.user}@{self.hostname}' if self.user else self.hostname

    def as_dict(self) -> dict:
        return {
            'name': self.name,
            'hostname': self.hostname,
            'port': self.port,
            'user': self.user,
            'local': self.is_local,
            'enabled': self.enabled,
            'note': self.note,
        }


class HostRegistry:
    """Named execution hosts, always including ``local``.

    Persisted as JSON the same way the project list is (write-then-rename, so a
    crash cannot truncate it), because a node definition is operator
    configuration that must survive a restart. `local` is never written out: it
    is implied, and a stale entry for it could only cause disagreement.
    """

    FILENAME = 'hosts.json'

    def __init__(
        self,
        hosts: Sequence[ExecutionHost] = (),
        config_directory=None,
    ):
        self._hosts: Dict[str, ExecutionHost] = {LOCAL_NAME: ExecutionHost(LOCAL_NAME)}
        self.config_directory = (
            Path(config_directory) if config_directory is not None else None
        )
        for host in hosts:
            self.add(host, persist=False)
        if self.config_directory is not None:
            self._load()

    # ------------------------------------------------------------ persistence
    @property
    def _file(self) -> Optional[Path]:
        if self.config_directory is None:
            return None
        return self.config_directory / self.FILENAME

    def _load(self) -> None:
        path = self._file
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        for item in raw.get('hosts', []) if isinstance(raw, dict) else []:
            try:
                host = ExecutionHost(
                    name=str(item['name']),
                    hostname=str(item.get('hostname') or ''),
                    port=int(item.get('port') or 22),
                    user=item.get('user') or None,
                    enabled=bool(item.get('enabled', True)),
                    note=str(item.get('note') or ''),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if host.name == LOCAL_NAME or not host.hostname:
                continue
            self._hosts[host.name] = host

    def _save(self) -> None:
        path = self._file
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'hosts': [
                {
                    'name': host.name,
                    'hostname': host.hostname,
                    'port': host.port,
                    'user': host.user,
                    'enabled': host.enabled,
                    'note': host.note,
                }
                for host in self.list()
                if not host.is_local
            ]
        }
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)

    # ------------------------------------------------------------------ mutate
    def add(self, host: ExecutionHost, *, persist: bool = True) -> ExecutionHost:
        if not host.name:
            raise HostError('a host needs a name')
        if host.name == LOCAL_NAME:
            # `local` cannot be redefined: it is what "run here" means, and a
            # remote node shadowing it would make a submission's behaviour depend
            # on configuration order.
            raise HostError(f'{LOCAL_NAME!r} is reserved for this machine')
        if host.name in self._hosts:
            raise HostError(f'duplicate host name {host.name!r}')
        # The name reaches the UI, the model and a JSON file; keep it to
        # something that cannot be confused with a path or a flag.
        if not _VALID_NAME.match(host.name):
            raise HostError(
                f'invalid host name {host.name!r}: use letters, digits, _ and -'
            )
        self._hosts[host.name] = host
        if persist:
            self._save()
        return host

    def update(
        self,
        name: str,
        *,
        enabled: Optional[bool] = None,
        note: Optional[str] = None,
        hostname: Optional[str] = None,
    ) -> ExecutionHost:
        """Change an existing node. `local` is not editable."""
        host = self._hosts.get(name)
        if host is None:
            raise HostError(f'unknown host {name!r}')
        if host.is_local:
            raise HostError('the local host cannot be changed')
        updated = ExecutionHost(
            name=host.name,
            hostname=hostname if hostname is not None else host.hostname,
            port=host.port,
            user=host.user,
            enabled=host.enabled if enabled is None else bool(enabled),
            note=host.note if note is None else note,
        )
        if not updated.hostname:
            raise HostError('a host needs a hostname')
        self._hosts[name] = updated
        self._save()
        return updated

    def remove(self, name: str) -> bool:
        """Drop a node. `local` is always present, so it cannot be removed."""
        if name == LOCAL_NAME:
            raise HostError('the local host cannot be removed')
        if name not in self._hosts:
            return False
        del self._hosts[name]
        self._save()
        return True

    def enabled(self) -> List[ExecutionHost]:
        """The hosts a new job may be sent to: local plus enabled remote nodes."""
        return [host for host in self.list() if host.is_local or host.enabled]

    def get(self, name: Optional[str]) -> ExecutionHost:
        """The host called `name`, or ``local`` when nothing was asked for."""
        if not name or name == LOCAL_NAME:
            return self._hosts[LOCAL_NAME]
        host = self._hosts.get(name)
        if host is None:
            known = ', '.join(sorted(self._hosts))
            raise HostError(
                f'unknown execution host {name!r}; known hosts are {known}'
            )
        if not host.enabled:
            raise HostError(
                f'execution host {name!r} is disabled; enable it in Nodes first'
            )
        return host

    def list(self) -> List[ExecutionHost]:
        # `local` first, then names alphabetically, so a menu is stable.
        return [self._hosts[LOCAL_NAME]] + [
            self._hosts[name]
            for name in sorted(self._hosts)
            if name != LOCAL_NAME
        ]

    def remote(self) -> List[ExecutionHost]:
        return [host for host in self.list() if not host.is_local]

    def __len__(self) -> int:
        return len(self._hosts)


def parse_host_spec(spec: str) -> ExecutionHost:
    """Parse ``name=hostname[:port]`` or ``name=user@hostname[:port]``.

    A bare ``name=hostname`` is the common case: the agent's own account exists
    on every node, so no user has to be named. The full form is accepted because
    a shared account with a different name on one node is a real setup.
    """
    if not spec or '=' not in spec:
        raise HostError(
            f'--host-node needs name=hostname[:port], got {spec!r} '
            '(for example --host-node gpu1=gpu1.lab)'
        )
    name, _, target = spec.partition('=')
    name = name.strip()
    target = target.strip()
    if not name or not target:
        raise HostError(f'--host-node needs both a name and a host, got {spec!r}')

    user = None
    if '@' in target:
        user, _, target = target.partition('@')
        user = user.strip() or None

    port = 22
    if ':' in target:
        hostname, _, port_text = target.rpartition(':')
        try:
            port = int(port_text)
        except ValueError as exc:
            raise HostError(f'bad port in {spec!r}: {port_text!r}') from exc
        target = hostname
    if not target:
        raise HostError(f'--host-node has no hostname in {spec!r}')

    return ExecutionHost(name=name, hostname=target, port=port, user=user)


def registry_from_specs(specs: Sequence[str]) -> HostRegistry:
    return HostRegistry([parse_host_spec(spec) for spec in specs or ()])


# ------------------------------------------------------------------- execution

# ssh options shared by every remote call.
#
# `BatchMode=yes` is the important one: without it ssh would block forever on a
# password prompt that has no terminal to answer it, and a job would appear to
# hang at submission. With it, a missing key is an immediate error the operator
# can act on.
#
# `StrictHostKeyChecking=accept-new` is trust-on-first-use: an unknown node is
# accepted once, and a *changed* key is still refused. That is the right default
# for a lab network where blocking a first submission on a prompt is worse than
# the risk; a site that pins keys can set RELION_WEB_SSH_STRICTNESS=yes.
_SSH_OPTIONS = (
    'BatchMode=yes',
    # Short: a node card must appear quickly, and a card that says "not
    # answering" is more useful than one that takes ten seconds to render.
    'ConnectTimeout=5',
    'ServerAliveInterval=20',
    'ServerAliveCountMax=3',
)


def default_key_path() -> Path:
    """The agent's own SSH key.

    Beside the project list, not in ``~/.ssh``: this key belongs to the *agent*,
    not to the operator's interactive logins, and keeping it separate means
    revoking one does not disturb the other. ``RELION_WEB_HOME`` moves it with
    the rest of the agent's state.
    """
    import os

    home = os.environ.get('RELION_WEB_HOME')
    root = Path(home).expanduser() if home else Path.home() / '.relion_web'
    return root / 'id_ed25519'


def _identity_option() -> List[str]:
    """``-i`` pointing at the agent key, when it exists.

    Nodes are provisioned with this key, so it has to be offered explicitly:
    ssh only tries keys it knows about, and an operator's own ``~/.ssh/id_*``
    is a different key.
    """
    path = default_key_path()
    return ['-o', 'IdentitiesOnly=yes', '-i', str(path)] if path.is_file() else []


def _strictness_option() -> str:
    import os

    value = os.environ.get('RELION_WEB_SSH_STRICTNESS', '').strip().lower()
    if value in ('yes', 'strict', 'true', '1'):
        return 'StrictHostKeyChecking=yes'
    return 'StrictHostKeyChecking=accept-new'


def ssh_argv(host: ExecutionHost, remote_command: str) -> List[str]:
    """The local argv that runs ``remote_command`` on ``host``.

    Kept separate from argv construction so a test can assert the shape without
    a reachable machine.
    """
    # Every option needs its own `-o`: ssh treats a bare `BatchMode=yes` as the
    # hostname, which fails with "Could not resolve hostname batchmode=yes".
    argv = ['ssh']
    for option in (*_SSH_OPTIONS, _strictness_option()):
        argv += ['-o', option]
    argv += _identity_option()
    if host.port and host.port != 22:
        argv += ['-p', str(host.port)]
    argv += [host.ssh_target(), remote_command]
    return argv


def remote_command(argv: Sequence[str], directory: str) -> str:
    """A shell string that changes into ``directory`` and executes ``argv``.

    `cd && exec` rather than ssh's own command handling, because a job's argv is
    a program plus arguments that must not be re-split by a shell: every element
    is quoted individually, and `exec` replaces the remote shell so the exit code
    that comes back over ssh is the program's own.

    The exit code matters: the agent reconciles it with RELION's sentinel files,
    and ssh propagates it unchanged.
    """
    quoted = ' '.join(shlex.quote(part) for part in argv)
    return f'cd {shlex.quote(directory)} && exec {quoted}'


def wrap_for_host(
    argv: Sequence[str], directory: str, host: ExecutionHost
) -> List[str]:
    """The argv to execute, local or wrapped in ssh.

    A local host returns the argv unchanged, so there is exactly one code path
    for execution and no "remote mode" to keep in step.
    """
    if host.is_local:
        return list(argv)
    return ssh_argv(host, remote_command(argv, directory))


def check_reachable(host: ExecutionHost, timeout: float = 10.0) -> tuple:
    """Try one trivial command on ``host``; return ``(ok, message)``.

    Used before a submission when the user picked a node, so "wrong hostname" or
    "key not installed" is reported at submission instead of surfacing minutes
    later as a failed job with an ssh error in run.err.
    """
    import shutil
    import subprocess

    if host.is_local:
        return True, ''
    if shutil.which('ssh') is None:
        return False, 'ssh is not installed on the machine running the agent'

    try:
        result = subprocess.run(
            ssh_argv(host, 'true'),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f'{host.name} ({host.hostname}) did not answer within {timeout:g}s'
    except OSError as exc:
        return False, f'could not run ssh: {exc}'

    if result.returncode == 0:
        return True, ''
    detail = (result.stderr or result.stdout or '').strip().splitlines()
    message = detail[-1] if detail else f'exit code {result.returncode}'
    if 'Permission denied' in message or 'publickey' in message:
        return False, (
            f'cannot log in to {host.name} ({host.hostname}) without a password: '
            'the agent only keeps a browser session, never the password. Install '
            "a key from this account with 'ssh-copy-id " + host.ssh_target() + "'"
        )
    if 'Could not resolve hostname' in message:
        return False, f'unknown hostname for {host.name}: {host.hostname}'
    return False, f'{host.name}: {message}'
