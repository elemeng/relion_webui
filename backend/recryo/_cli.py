# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Command line entry point: ``recryo``.

One agent per user, started once and left running. It serves on the LAN and asks
the host's sshd to validate any browser login, so visitors sign in with their
normal account rather than a password this program knows about.

This is a single-command Typer app, so options are passed directly
(``recryo --host 0.0.0.0``), with no subcommand.
"""

import getpass
import socket
from pathlib import Path
from typing import List, Optional

import typer

from .app import create_app
from .auth import AuthManager
from .projects import ProjectRegistry

cli = typer.Typer(add_completion=False, no_args_is_help=True)


def _lan_addresses() -> List[str]:
    """Best-effort list of non-loopback IPv4 addresses to print at startup."""
    addresses: List[str] = []
    try:
        # A UDP "connect" sends nothing but reveals the outbound interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(('10.255.255.255', 1))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith('127.'):
                addresses.append(address)
    except (socket.gaierror, OSError):
        pass
    return [a for a in addresses if not a.startswith('127.')]


@cli.command()
def serve(
    host: str = typer.Option(
        '0.0.0.0',
        help='interface to bind; use 127.0.0.1 when fronting it with an SSH tunnel',
    ),
    port: int = typer.Option(8765, help='port to bind'),
    root: List[Path] = typer.Option(
        None,
        '--root',
        '-r',
        help=(
            'extra directory the agent may serve beyond the added projects; '
            'repeatable'
        ),
    ),
    project: List[Path] = typer.Option(
        None,
        '--project',
        '-p',
        help='project directory to add to the list at startup; repeatable',
    ),
    session_hours: float = typer.Option(
        12.0, '--session-hours', help='how long a browser login stays valid'
    ),
    no_auth: bool = typer.Option(
        False,
        '--no-auth',
        help='disable login entirely; only safe on loopback or behind a tunnel',
    ),
    ssh_host: str = typer.Option('127.0.0.1', help='SSH server used to check credentials'),
    ssh_port: int = typer.Option(22, help='SSH server port'),
    config_directory: Optional[Path] = typer.Option(
        None, '--config-directory', help='where the project list is stored'
    ),
    poll_interval: float = typer.Option(
        0.5, help='seconds between job-status updates pushed to the browser'
    ),
    assistant: bool = typer.Option(
        False,
        '--assistant',
        help=(
            'enable the LLM assistant panel. Sends project names, job options and '
            'log lines to the configured provider; read the docs before enabling'
        ),
    ),
    mcp: bool = typer.Option(
        False,
        '--mcp',
        help=(
            'serve the tool registry over MCP at /mcp so an external agent can '
            'drive the pipeline; requires --assistant or standalone'
        ),
    ),
    host_node: List[str] = typer.Option(
        None,
        '--host-node',
        help=(
            'a machine jobs may run on, as name=hostname[:port] or '
            'name=user@hostname[:port]; repeatable. Needs key-based ssh from the '
            'agent account, and the project must be on storage both machines see'
        ),
    ),
    mcp_read_only: bool = typer.Option(
        False,
        '--mcp-read-only',
        help=(
            'with --mcp, publish only the read tools. Use this when the client is '
            'an autonomous agent: nothing it does can then change a project'
        ),
    ),
    llm_provider: str = typer.Option(
        '', '--llm-provider', help='openai | anthropic | google | local'
    ),
    llm_model: str = typer.Option('', '--llm-model', help='model name for that provider'),
    llm_base_url: str = typer.Option(
        '', '--llm-base-url', help='OpenAI-compatible endpoint, for --llm-provider local'
    ),
    reload: bool = typer.Option(
        False,
        '--reload',
        help=(
            'restart the agent when a Python source changes; for development. '
            'The process supervises a child, so the PID is not the server.'
        ),
    ),
):
    """Run the RELION cryo-ET web agent."""
    import uvicorn

    username = getpass.getuser()

    if no_auth and host not in ('127.0.0.1', 'localhost', '::1'):
        typer.echo(
            'refusing to disable authentication while listening on '
            f'{host}: anyone on the network could submit jobs. '
            'Bind 127.0.0.1, or drop --no-auth.',
            err=True,
        )
        raise typer.Exit(code=2)

    projects = ProjectRegistry(config_directory)

    # Roots bound what the file APIs can touch. The project list is the primary
    # boundary; these are the extra places named on the command line. Projects
    # are added to the list here and picked up by the guard, which reads the
    # list on every check - so a project added at runtime is usable at once.
    roots = [Path(r).expanduser().resolve() for r in (root or [])]
    for project_path in project or []:
        try:
            projects.add(project_path)
        except ValueError as exc:
            typer.echo(f'skipping --project {project_path}: {exc}', err=True)

    auth = None
    if not no_auth:
        auth = AuthManager(
            session_lifetime=session_hours * 3600,
            ssh_options={'host': ssh_host, 'port': ssh_port},
        )

    # Validate the node specs before the app is built: a typo in --host-node
    # should read as a usage error, not a traceback from deep inside startup.
    if host_node:
        from . import hosts as maybe_hosts

        try:
            maybe_hosts.registry_from_specs(list(host_node))
        except maybe_hosts.HostError as exc:
            typer.echo(f'bad --host-node: {exc}', err=True)
            raise typer.Exit(code=2) from exc

    # The assistant's provider comes from the CLI first, then the environment.
    # The key itself is never a CLI flag: an argument is visible in `ps` to
    # every user on the machine, so it comes from the environment only.
    assistant_config = None
    if assistant or mcp:
        from . import assistant as assistant_module

        assistant_config = assistant_module.config_from_env()
        if llm_provider:
            if llm_provider not in assistant_module.PROVIDERS:
                typer.echo(
                    f'unknown --llm-provider {llm_provider!r}; '
                    f'choose one of {", ".join(assistant_module.PROVIDERS)}',
                    err=True,
                )
                raise typer.Exit(code=2)
            assistant_config.provider = llm_provider
            assistant_config.api_key = assistant_config.api_key or __import__(
                'os'
            ).environ.get(
                assistant_module.PROVIDERS[llm_provider]['env'], ''
            )
        if llm_model:
            assistant_config.model = llm_model
        if llm_base_url:
            assistant_config.base_url = llm_base_url

    # An MCP client drives the pipeline with no human watching, so the login is
    # the only thing between it and the projects. Refuse the combination rather
    # than mount an unauthenticated write endpoint.
    if mcp and no_auth and host not in ('127.0.0.1', 'localhost', '::1'):
        typer.echo(
            'refusing --mcp with --no-auth on a non-loopback address: an MCP '
            'client could run jobs with no login. Bind 127.0.0.1 or drop --no-auth.',
            err=True,
        )
        raise typer.Exit(code=2)

    app = create_app(
        roots=roots or [Path.cwd()],
        poll_interval=poll_interval,
        auth=auth,
        projects=projects,
        enable_assistant=assistant,
        enable_mcp=mcp,
        assistant_config=assistant_config,
        mcp_hosts=[host, '127.0.0.1', 'localhost', '::1'],
        mcp_allow_writes=not mcp_read_only,
        host_nodes=list(host_node or []),
    )
    app.state.initial_project = str(project[0].expanduser().resolve()) if project else None

    typer.echo(f'recryo running as {username}')
    typer.echo(f'listening on {host}:{port}')
    if host in ('127.0.0.1', 'localhost', '::1'):
        typer.echo('reachable only from this machine')
    else:
        typer.echo('open in a browser:')
        for address in _lan_addresses():
            typer.echo(f'  http://{address}:{port}/')
    if auth is not None:
        typer.echo(
            f'log in with your account on this machine; sessions last '
            f'{session_hours:g}h'
        )
    else:
        typer.echo('WARNING: authentication disabled')
    typer.echo(f'project list: {projects.config_directory}')
    typer.echo(f'projects: {len(projects.list())}')
    if assistant:
        config = app.state.assistant_default_config
        state = 'configured' if config and config.api_key else 'NO API KEY'
        typer.echo(
            f'assistant: {config.provider}/{config.resolved_model} ({state})'
            if config
            else 'assistant: enabled'
        )
        if config and not config.api_key:
            typer.echo(
                f'  set {assistant_module.PROVIDERS[config.provider]["env"]} or '
                'use the panel settings to enter one'
            )
    if mcp:
        mode = 'read-only' if mcp_read_only else 'read and write'
        typer.echo(
            f'MCP: http://{host}:{port}/mcp (same login as the browser; {mode})'
        )
    if host_node:
        from . import hosts as hosts_module

        try:
            registry = hosts_module.registry_from_specs(list(host_node))
        except hosts_module.HostError as exc:
            typer.echo(f'bad --host-node: {exc}', err=True)
            raise typer.Exit(code=2) from exc
        remote = [entry for entry in registry.list() if not entry.is_local]
        typer.echo(f'execution hosts: local + {len(remote)} remote')
        for entry in remote:
            typer.echo(f'  {entry.name} -> {entry.ssh_target()}:{entry.port}')

    if reload:
        # uvicorn's reloader re-imports an import string in a new process, so
        # the app cannot be the object built above. Serialise the settings and
        # let _devapp rebuild it on each restart.
        _export_reload_environment(
            roots=[str(item) for item in (roots or [Path.cwd()])],
            config_directory=projects.config_directory,
            auth_enabled=auth is not None,
            session_hours=session_hours,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            poll_interval=poll_interval,
            initial_project=app.state.initial_project,
        )
        # The assistant flags travel the same way, because the reloaded process
        # rebuilds the app from the environment.
        import os as _os

        _os.environ['RECRYO_ASSISTANT'] = '1' if assistant else '0'
        _os.environ['RECRYO_MCP'] = '1' if mcp else '0'
        _os.environ['RECRYO_HOST'] = host
        _os.environ['RECRYO_MCP_READ_ONLY'] = '1' if mcp_read_only else '0'
        _os.environ['RECRYO_HOST_NODES'] = __import__('json').dumps(
            list(host_node or [])
        )
        typer.echo('reload enabled: watching the Python sources')
        uvicorn.run(
            'recryo._devapp:app',
            host=host,
            port=port,
            log_level='info',
            reload=True,
            factory=True,
            reload_dirs=[str(Path(__file__).resolve().parent.parent)],
        )
        return

    uvicorn.run(app, host=host, port=port, log_level='info')


def _export_reload_environment(**settings) -> None:
    """Publish the CLI settings for ``_devapp`` to rebuild the app from.

    Environment variables, not arguments, because uvicorn's reloader starts a
    fresh interpreter: nothing in this process survives except the environment.
    """
    import json
    import os

    os.environ['RECRYO_ROOTS'] = json.dumps(settings['roots'])
    os.environ['RECRYO_POLL'] = str(settings['poll_interval'])
    if settings['config_directory']:
        os.environ['RECRYO_CONFIG'] = str(settings['config_directory'])
    os.environ['RECRYO_AUTH'] = '1' if settings['auth_enabled'] else '0'
    os.environ['RECRYO_SESSION_HOURS'] = str(settings['session_hours'])
    os.environ['RECRYO_SSH_HOST'] = str(settings['ssh_host'])
    os.environ['RECRYO_SSH_PORT'] = str(settings['ssh_port'])
    if settings['initial_project']:
        os.environ['RECRYO_INITIAL_PROJECT'] = str(settings['initial_project'])


def main() -> None:
    cli()


if __name__ == '__main__':
    main()
