"""The tool registry, spoken over MCP.

This module only translates. `tools.py` decides what a capability is and what
it may touch; this exposes those same functions as MCP tools over streamable
HTTP, so an MCP client - Claude Desktop, an editor, another agent - gets
exactly the surface the in-browser assistant gets. There is no second
implementation to drift.

Why streamable HTTP and not stdio: the agent already runs as a server holding
the project list, the path guard and the job manager in memory. A stdio server
would be a *second* process with its own copy of that state, and adding a
project in the browser would not make it visible to the MCP client. Mounting on
the same app means one state, one guard, one place where permissions are
decided.

The transport is session-based (the MCP default) rather than stateless, so a
client's initialization carries across calls. /mcp is registered as a public
path in app.py only when MCP is enabled *and* it reuses the same session cookie
as the rest of the API - see `MCP_TOOL_PERMISSIONS` below for what that means.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from . import mcp_content
from . import tools as tools_module

try:  # pragma: no cover - exercised by the import guard, imported once
    # mcp 2 renamed FastMCP to MCPServer and moved the transport options from the
    # constructor to `streamable_http_app()`. Import both names so this module
    # keeps working on either major; the call sites differ only in where those
    # options go, which `_server()` handles.
    from mcp.server.mcpserver import MCPServer as _MCPServer

    FastMCP = _MCPServer
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    try:
        # mcp 1.x
        from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]
        from mcp.types import ToolAnnotations
    except ImportError:
        FastMCP = None  # type: ignore[assignment]
        ToolAnnotations = None  # type: ignore[assignment]


def _context_class():
    """The MCP request context type, for either major.

    mcp 2 moved it from ``mcp.server.fastmcp`` to ``mcp.server.mcpserver``. It is
    looked up per call rather than imported at module level, so this module still
    imports when the optional extra is absent - which is what lets the agent run
    without it.
    """
    try:
        from mcp.server.mcpserver import Context  # mcp 2.x
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp import Context
    return Context


MCP_PATH = '/mcp'

INSTRUCTIONS = """\
This server drives RELION cryo-ET projects on the machine it runs on.

Start with `list_projects`, then `get_pipeline` for the one you care about. A
job is identified by its process name, e.g. `Tomograms/job004`.

Read tools are safe. Write tools change a project: `submit_job` starts a job,
`delete_job` moves a job directory to Trash, `stop_job` kills a running process.
Prefer `submit_job` with `dry_run=true` first to show the command that would run.

Every path is confined to the projects the agent has been given. A path outside
them is refused, so if a tool reports "outside the allowed roots", call
`list_projects` again rather than trying another spelling of the path.
"""


def available() -> bool:
    """Whether the MCP package is installed."""
    return FastMCP is not None


class MCPUnavailable(RuntimeError):
    """The MCP extra is not installed."""


def _annotations(tool: tools_module.Tool):
    """MCP hints for one tool.

    `destructive` is set for writes, which is what makes a client ask before
    running them, and `read_only` for the rest, which lets a client batch them.
    `submit_job` is additionally marked open-world because it starts a process
    outside this server.
    """
    if ToolAnnotations is None:  # pragma: no cover
        return None
    hints = {
        'read_only_hint': not tool.writes,
        'destructive_hint': tool.writes,
        'idempotent_hint': not tool.writes,
        'open_world_hint': tool.name in ('submit_job', 'stop_job', 'browse_directory'),
    }
    # mcp 2 renamed these fields from camelCase to snake_case. The JSON on the
    # wire is camelCase in both majors, so only the Python keyword changed. Built
    # from whichever names the installed major accepts, rather than pinning one.
    try:
        return ToolAnnotations(
            title=tool.name.replace('_', ' '),
            **hints,
        )
    except (TypeError, ValueError):  # pragma: no cover - mcp 1.x
        camel = {
            'readOnlyHint': hints['read_only_hint'],
            'destructiveHint': hints['destructive_hint'],
            'idempotentHint': hints['idempotent_hint'],
            'openWorldHint': hints['open_world_hint'],
        }
        return ToolAnnotations(
            title=tool.name.replace('_', ' '),
            **camel,
        )


# Tools that block for a long time must not run on the event loop.
#
# FastMCP calls a sync tool inline (`call_fn_with_arg_validation` awaits async
# functions but calls sync ones directly), so a `wait_for_job` that sleeps for
# ten minutes would stop the server answering anything else - including the
# client's own cancellation. These are pushed to a worker thread instead, which
# also lets them report progress while they wait.
_BLOCKING_TOOLS = frozenset({'wait_for_job'})


def _wrap(
    tool: tools_module.Tool,
    ctx: tools_module.ToolContext,
    *,
    allow_writes: bool = True,
) -> Callable[..., Any]:
    """One MCP-facing callable that delegates to the registry.

    `ctx` and `allow_writes` are bound here rather than passed by the client, so
    a client cannot choose which guard, project list or write policy the call
    runs against. The `destructiveHint` annotation is only advice - a client may
    ignore it - so the policy has to be enforced server-side, which `call_tool`
    does.
    """
    registry = build_registry()

    def call(**kwargs: Any) -> Any:
        return tools_module.call_tool(
            registry, tool.name, kwargs, ctx, allow_writes=allow_writes
        )

    call.__name__ = tool.name
    call.__doc__ = tool.description
    return call


_registry_cache: Optional[List[tools_module.Tool]] = None


def build_registry() -> List[tools_module.Tool]:
    global _registry_cache
    if _registry_cache is None:
        _registry_cache = tools_module.build_registry()
    return _registry_cache


def _transport_security(hosts: Sequence[str]):
    """Host allow-list for the MCP transport's DNS-rebinding guard.

    The guard is on by default and rejects any Host header not listed - which
    includes `localhost:8765`, the address this server is actually reached on.
    Left at its default, every real client gets 421 Misdirected Request.

    It is not switched off, though. The guard exists because a browser on the
    same machine can be tricked into posting to 127.0.0.1, and this endpoint
    writes to projects. Listing the hosts the agent is bound to keeps the
    protection for anything else; requests still have to pass the session
    cookie check in app.py, because an MCP client is not a special case.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    allowed = []
    for host in hosts:
        if not host or host in ('0.0.0.0', '::'):
            continue
        allowed.extend([host, f'{host}:*', f'127.0.0.1:{host}' if ':' not in host else host])
    # A wildcard port for the loopback names, plus any explicitly bound host.
    extra = ['localhost', 'localhost:*', '127.0.0.1', '127.0.0.1:*', '[::1]', '[::1]:*']
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed or extra),
        allowed_hosts=sorted({*allowed, *extra}),
    )


def create_server(
    ctx: tools_module.ToolContext,
    *,
    name: str = 'relion',
    hosts: Sequence[str] = (),
    allow_writes: bool = True,
) -> Any:
    """Build the MCP server for one agent instance.

    Each tool is registered with a hand-written signature rather than through
    `*args, **kwargs`: FastMCP derives the JSON schema from the signature, and
    the registry already has one, so the wrapper is generated from it.
    """
    if FastMCP is None:
        raise MCPUnavailable(
            "the MCP server needs the 'assistant' extra: pip install -e '.[assistant]'"
        )

    # mcp 2 added a security check on resource-template parameters that rejects
    # absolute paths and ".." by default. Our {path} parameters are absolute
    # project directories by design, and every handler resolves them through
    # `ToolContext.project` -> `PathGuard`, which is the actual boundary: it
    # refuses anything outside the configured roots. So the template check is
    # relaxed here and the guard does the enforcing; null bytes stay rejected.
    _resource_security = None
    try:
        from mcp.server.mcpserver import ResourceSecurity  # mcp 2.x

        _resource_security = ResourceSecurity(
            reject_path_traversal=False,
            reject_absolute_paths=False,
            reject_null_bytes=True,
        )
    except ImportError:  # pragma: no cover - mcp 1.x has no such check
        _resource_security = None

    server = FastMCP(
        name,
        instructions=INSTRUCTIONS,
        **({'resource_security': _resource_security} if _resource_security else {}),
    )
    # The transport options used to be constructor arguments. mcp 2 moved them to
    # `streamable_http_app()`, so they are carried on the server object and passed
    # there by `mount()`; that keeps one place that decides them and lets this
    # work on either major.
    server._webui_transport = {
        "json_response": True,
        "stateless_http": False,
        "transport_security": _transport_security(hosts),
    }

    for tool in build_registry():
        # A read-only server simply does not publish its write tools. Refusing
        # at call time as well would be belt and braces, but not listing them is
        # better: a client cannot propose an action it cannot see, so the model
        # never plans around a capability that will be refused. `call_tool`
        # still refuses, for a client that calls by name regardless.
        if tool.writes and not allow_writes:
            continue
        call = _wrap(tool, ctx, allow_writes=allow_writes)
        server.add_tool(
            _make_adapter(tool, call),
            name=tool.name,
            description=tool.description,
            annotations=_annotations(tool),
            structured_output=False,
        )

    # Resources and prompts are read-only by construction: a resource cannot
    # change anything, and a prompt is only text handed to the model. They stay
    # available in read-only mode, because a client that cannot act still needs
    # to be able to look.
    mcp_content.register_resources(server, ctx)
    mcp_content.register_prompts(server, ctx)
    return server


def _make_adapter(
    tool: tools_module.Tool,
    call: Callable[..., Any],
    ctx: Optional[tools_module.ToolContext] = None,
) -> Callable[..., Any]:
    """Build a function with the tool's real parameter list.

    FastMCP reads `__signature__` to build the schema, and the registry already
    has one, so the signature is copied rather than restated: the assistant and
    the MCP client see the same parameters, and a change to a tool cannot update
    one and not the other.

    `__annotations__` must hold resolved types, not strings. This package uses
    `from __future__ import annotations`, and FastMCP builds a pydantic model
    from the annotations without the module's globals, so `'Optional[str]'`
    would fail to resolve there ("is not fully defined"). `get_type_hints`
    resolves them in the module that defines them, which is the right scope.

    A tool in `_BLOCKING_TOOLS` gets an *async* adapter: it runs the work in a
    worker thread and forwards progress notifications from there. A sync tool
    would be called inline on the event loop, so a wait of several minutes would
    freeze the server - including the client's attempt to cancel.
    """
    import inspect
    import typing

    hints = typing.get_type_hints(tool.fn)
    hints.pop('return', None)
    signature = inspect.signature(tool.fn)
    # FastMCP calls `inspect.signature(adapter, eval_str=True)`, which re-parses
    # string annotations against *the adapter's* globals. Replacing each
    # Parameter's annotation with the resolved type means there is nothing left
    # to evaluate, so the adapter does not need the tool module's names in scope.
    # `_INTERNAL_PARAMETERS` (ctx, on_progress and friends) are supplied by the
    # registry, not the client; exposing one would ask pydantic for a schema of a
    # Callable, which it cannot express.
    parameters = [
        parameter.replace(annotation=hints.get(name, parameter.annotation))
        for name, parameter in signature.parameters.items()
        if name not in tools_module._INTERNAL_PARAMETERS
    ]
    resolved = signature.replace(parameters=parameters)

    if tool.name in _BLOCKING_TOOLS:
        adapter = _blocking_adapter(tool, call, signature, ctx)
    else:

        def adapter(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
            bound = signature.bind_partial(None, *args, **kwargs)
            bound.arguments.pop('ctx', None)
            return call(**bound.arguments)

    adapter.__name__ = tool.name
    adapter.__doc__ = tool.description
    if tool.name in _BLOCKING_TOOLS:
        # Add the injected Context parameter so FastMCP finds it.
        import inspect as _inspect

        _Context = _context_class()

        adapter.__signature__ = resolved.replace(  # type: ignore[attr-defined]
            parameters=[
                _inspect.Parameter(
                    'mcp_context',
                    _inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=_Context,
                ),
                *parameters,
            ]
        )
        adapter.__annotations__ = {  # type: ignore[attr-defined]
            'mcp_context': _Context,
            **{p.name: hints.get(p.name, str) for p in parameters},
        }
    else:
        adapter.__signature__ = resolved  # type: ignore[attr-defined]
        adapter.__annotations__ = {  # type: ignore[attr-defined]
            name: hints.get(name, str)
            for name in (p.name for p in parameters)
        }
    return adapter


def _blocking_adapter(
    tool: tools_module.Tool,
    call: Callable[..., Any],
    signature: Any,
    ctx: Optional[tools_module.ToolContext],
) -> Callable[..., Any]:
    """An async adapter that moves a long wait off the event loop.

    FastMCP finds the parameter annotated `Context` and injects the session's
    context there (`find_context_parameter`). So the adapter takes an explicit
    `mcp_context: Context` rather than scraping it out of `kwargs`, and uses it
    to report progress while the wait runs: the client sees "job005 is running
    (12m of at most 30m)" instead of a silent connection.

    `report_progress` is a coroutine and the wait runs on a worker thread, so
    each notification is queued back onto the event loop with
    `run_coroutine_threadsafe`. They are awaited before returning: a progress
    message arriving after the call has finished would confuse a client that has
    already moved on.
    """
    import asyncio
    import contextlib

    Context = _context_class()

    async def adapter(mcp_context: Context, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        pending: List[Any] = []

        def progress(elapsed: float, total: float, message: str) -> None:
            try:
                coroutine = mcp_context.report_progress(elapsed, total, message)
            except AttributeError:  # pragma: no cover - older context
                return
            pending.append(asyncio.run_coroutine_threadsafe(coroutine, loop))

        def work() -> Any:
            return call(on_progress=progress, **kwargs)

        try:
            return await asyncio.to_thread(work)
        finally:
            for future in pending:
                with contextlib.suppress(Exception):
                    future.result(timeout=1)

    adapter.__name__ = tool.name
    return adapter


def mount(app: Any, server: Any, path: str = MCP_PATH) -> None:
    """Mount the MCP app on the agent's FastAPI app.

    `streamable_http_app()` returns a Starlette app with its own lifespan that
    starts the session manager. Mounting a sub-app does *not* run that lifespan,
    so the session manager is started here from the parent's lifespan instead;
    without it every request waits on a task group that was never created.
    """
    from starlette.routing import Mount

    # The server builds a Starlette app whose own route is `/mcp` by default.
    # Mounting that at `/mcp` would make the real url `/mcp/mcp`, so the mounted
    # root is used as its path instead.
    #
    # mcp 2 takes the transport options as arguments here; mcp 1 read them from
    # `server.settings`. Try the 2.x call and fall back, so the same mount works
    # on either major.
    transport = getattr(server, "_webui_transport", {})
    try:
        mcp_app = server.streamable_http_app(
            streamable_http_path="/",
            **transport,
        )
    except TypeError:  # pragma: no cover - mcp 1.x
        server.settings.streamable_http_path = "/"
        mcp_app = server.streamable_http_app()

    class _NoSlashMount(Mount):
        """A mount that also matches the bare prefix, without redirecting.

        Starlette's Mount only matches `/mcp/...`; a request to exactly `/mcp`
        falls through to the router's trailing-slash redirect, which answers 307.
        An MCP client POSTs to the exact url it was given and does not follow a
        redirect with a body, so that becomes a 404.

        Overriding `matches` (not `__call__`: a route that does not match never
        reaches its handler) makes the bare prefix match directly, with the
        sub-app seeing `/`.
        """

        def matches(self, scope):
            from starlette.routing import Match, get_route_path

            if scope['type'] in ('http', 'websocket'):
                route_path = get_route_path(scope)
                prefix = self.path.rstrip('/')
                if route_path.rstrip('/') == prefix:
                    child = {
                        'path_params': dict(scope.get('path_params', {})),
                        'app_root_path': scope.get('app_root_path', ''),
                        'root_path': scope.get('root_path', '') + prefix,
                        'path': '/',
                        'endpoint': self.app,
                    }
                    return Match.FULL, child
            return super().matches(scope)

    app.router.routes.append(_NoSlashMount(path, app=mcp_app))
    app.state.mcp_session_manager = server.session_manager
    app.state.mcp_enabled = True


def describe_registry() -> List[Dict[str, Any]]:
    """A JSON-able listing of the tools, for the UI's capability panel."""
    return [
        {
            'name': tool.name,
            'description': (tool.description or '').strip().split('\n\n')[0],
            'writes': tool.writes,
            'needs_project': tool.needs_project,
            'schema': tool.schema(),
        }
        for tool in build_registry()
    ]


def tool_names() -> Sequence[str]:
    return [tool.name for tool in build_registry()]
