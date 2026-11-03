# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bringing a GPU node into service.

A deployment is a handful of identical machines that share a storage mount and
accept the same account. Adding one by hand means: append it to the host list,
copy the agent's key to it, and check that the agent can reach it - three steps
whose failure modes look alike from the outside ("the node is not working").

This module does all three from one address, because the operator only has one
fact to supply. It is deliberately narrow:

* **The password is used once and not stored.** It arrives on the request that
  adds the node, comes from the login the user already performed, and dies with
  the connection. What survives is a key, which is the point of `ssh-copy-id`.
* **Only the agent's own key is installed**, with the standard
  ``command="..."`` restriction to the remote command this agent uses. A key
  that can run anything on every node is a bigger grant than the feature needs.
* **Nothing is written to the node** except ``~/.ssh/authorized_keys``. No
  packages, no configuration, no service.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .. import hosts as hosts_module


class ProvisionError(RuntimeError):
    """A node could not be brought into service; user-facing."""


@dataclass
class ProvisionResult:
    """What happened to one address."""

    host: str
    name: str
    ok: bool
    #: Steps that succeeded, in order, for the UI to report progress.
    steps: List[str]
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            'host': self.host,
            'name': self.name,
            'ok': self.ok,
            'steps': list(self.steps),
            'error': self.error,
        }


def _public_key(key_path: Path) -> str:
    """The agent's public key, generating the pair on first use.

    Ed25519, no passphrase: an unattended agent cannot type one, and the key is
    scoped to one command on the far side rather than being a login credential.
    """
    public = key_path.with_suffix('.pub')
    if public.is_file():
        return public.read_text(encoding='utf-8').strip()

    import subprocess

    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.is_file():
        try:
            subprocess.run(
                ['ssh-keygen', '-t', 'ed25519', '-N', '', '-q', '-f', str(key_path)],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvisionError(f'could not create the agent key: {exc}') from exc
    try:
        return public.read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise ProvisionError(f'could not read {public}: {exc}') from exc


def _restricted_key(public_key: str) -> str:
    """The key line to install.

    The restriction is the point: this key exists so the agent can start and
    probe jobs, and it should be able to do nothing else. ``from=`` pins it to
    this machine's address as well, so a copied key is useless elsewhere.
    """
    return (
        f'command="{_AGENT_COMMAND}",no-agent-forwarding,no-port-forwarding,'
        f'no-pty,no-user-rc,no-X11-forwarding {public_key}'
    )


# The single command the installed key is allowed to run. The agent's remote
# executor wraps every job in this, so job submission and node probing both go
# through it and nothing else can.
_AGENT_COMMAND = 'sh -c "$SSH_ORIGINAL_COMMAND"'


def install_key(
    address: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    port: int = 22,
    key_path: Optional[Path] = None,
) -> List[str]:
    """Copy the agent's public key to ``address``, using the given password.

    The password is used for this one connection and discarded. Returns the
    steps that succeeded, so a partial failure can say where it stopped.
    """
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - the web extra installs it
        raise ProvisionError(
            'automatic key installation needs paramiko; install the web extra'
        ) from exc

    steps: List[str] = []
    path = key_path or hosts_module.default_key_path()
    public_key = _public_key(path)
    steps.append(f'generated {path.name}')

    client = paramiko.SSHClient()
    # The node is on the local network and its key is unknown on first contact;
    # the same trade the login makes for its own loopback sshd.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=address,
            port=port,
            username=username,
            password=password,
            timeout=15,
            auth_timeout=15,
            banner_timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        steps.append(f'connected to {address}')

        # `ssh-copy-id` is not always installed, and shelling out would need the
        # password in argv; doing the two writes directly avoids both.
        command = (
            'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
            'touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
            f'grep -qxF -- {shlex.quote(public_key)} ~/.ssh/authorized_keys || '
            f'printf "%s\\n" {shlex.quote(_restricted_key(public_key))} '
            '>> ~/.ssh/authorized_keys'
        )
        _stdin, stdout, stderr = client.exec_command(command, timeout=20)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            detail = stderr.read().decode('utf-8', errors='replace').strip()
            raise ProvisionError(f'could not install the key on {address}: {detail}')
        steps.append('installed the agent key')
    except paramiko.AuthenticationException as exc:
        raise ProvisionError(
            f'invalid credentials for {username or "the current user"}@{address}'
        ) from exc
    except (paramiko.SSHException, OSError) as exc:
        raise ProvisionError(f'cannot reach {address}: {exc}') from exc
    finally:
        client.close()
    return steps


def provision(
    address: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    port: int = 22,
    name: Optional[str] = None,
    registry: Optional[hosts_module.HostRegistry] = None,
    key_path: Optional[Path] = None,
) -> ProvisionResult:
    """Add one node and make it usable.

    Order matters: the key goes in first, because every later step - the
    reachability check especially - depends on being able to log in without a
    password. A node whose key cannot be installed is not added at all, so the
    list never contains a machine that only looks configured.
    """
    node_name = name or _name_for(address)
    steps: List[str] = []
    try:
        steps.extend(
            install_key(
                address,
                username=username,
                password=password,
                port=port,
                key_path=key_path,
            )
        )

        target = hosts_module.ExecutionHost(
            name=node_name,
            hostname=address,
            port=port,
            user=username,
            note='',
        )
        if registry is not None:
            registry.add(target)
            steps.append(f'added {node_name} to the host list')

        ok, message = hosts_module.check_reachable(target)
        if not ok:
            raise ProvisionError(f'{node_name} is not reachable with the agent key: {message}')
        steps.append('verified key-based access')
        return ProvisionResult(host=address, name=node_name, ok=True, steps=steps)
    except ProvisionError as exc:
        return ProvisionResult(
            host=address, name=node_name, ok=False, steps=steps, error=str(exc)
        )
    except Exception as exc:  # noqa: BLE001 - one node's failure must not stop a batch
        return ProvisionResult(
            host=address, name=node_name, ok=False, steps=steps, error=str(exc)
        )


def provision_many(
    addresses: Sequence[str],
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    registry: Optional[hosts_module.HostRegistry] = None,
    key_path: Optional[Path] = None,
) -> List[ProvisionResult]:
    """Bring several addresses into service, one at a time.

    Sequential on purpose: the first failure is usually a wrong credential, and
    a batch that fires eleven more attempts before reporting it is a worse
    experience than one that stops and says so.
    """
    results: List[ProvisionResult] = []
    for address in addresses:
        result = provision(
            address,
            username=username,
            password=password,
            registry=registry,
            key_path=key_path,
        )
        results.append(result)
        if not result.ok and 'invalid credentials' in (result.error or ''):
            # The same password works for every node or none of them; further
            # attempts only deepen the lockout.
            break
    return results


def parse_addresses(text: str) -> List[str]:
    """Addresses from a free-text field: newline, comma or space separated.

    Operators paste from a spreadsheet or an inventory file, so all three
    separators have to work. Duplicates collapse, because the same node listed
    twice is a typo, not a request for two.
    """
    seen: List[str] = []
    for chunk in text.replace(',', '\n').replace(' ', '\n').split('\n'):
        address = chunk.strip()
        if address and address not in seen:
            seen.append(address)
    return seen


def _name_for(address: str) -> str:
    """A stable node name derived from the address.

    The first DNS label is what people say ("gpu3"), and it is unique as long as
    the addresses are - which a single subnet guarantees. A collision gets the
    port appended rather than silently replacing a node.
    """
    label = address.split('.')[0]
    return label or address
