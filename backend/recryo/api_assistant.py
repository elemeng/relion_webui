# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""HTTP surface for the assistant, and the MCP mount.

Kept out of `app.py` because that module is already the whole REST API; the
assistant has its own concerns (a conversation per session, streaming, a
provider key that must never leave the server) and mixing them in would make
the route table harder to read than it needs to be.

`register(app, ...)` adds:

    GET  /api/assistant/config     what provider/model is configured
    POST /api/assistant/settings   change provider/model for this session
    GET  /api/assistant/tools      the tool catalogue, for the UI
    POST /api/assistant/chat       one turn, streamed as server-sent events
    POST /api/assistant/approve    approve or deny a parked write

and mounts the MCP app at /mcp when enabled.

The key never appears in a response. `configured: true/false` is all the browser
learns, which is enough to render "set ANTHROPIC_API_KEY" instead of an input
box, and means a stolen session cookie does not leak a provider credential.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import assistant as assistant_module
from . import mcp_server
from . import tools as tools_module
from .paths import PathGuard
from .projects import ProjectRegistry


class AssistantSettings(BaseModel):
    """Per-session overrides.

    Session-scoped on purpose: the key is sent once, kept in memory for that
    browser session, and dropped when the server restarts. It is not written to
    the project list or any config file.
    """

    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    allow_writes: Optional[bool] = None


class ChatRequest(BaseModel):
    message: str
    # Calls the user accepted since the last turn.
    approved: List[Dict[str, Any]] = Field(default_factory=list)
    # Which project the conversation is about, so the tools do not have to ask.
    project: Optional[str] = None


class ApprovalRequest(BaseModel):
    key: str
    approve: bool


def register(
    app: FastAPI,
    *,
    guard: PathGuard,
    projects: ProjectRegistry,
    jobs: Any,
    enable_mcp: bool,
    enable_assistant: bool,
    default_config: Optional[assistant_module.AssistantConfig] = None,
    mcp_hosts: Optional[List[str]] = None,
    mcp_allow_writes: bool = True,
) -> None:
    """Attach the assistant and (optionally) the MCP server to ``app``."""
    default_config = default_config or assistant_module.config_from_env()

    # One conversation per session token. Without auth there is a single
    # implicit session, which is the same rule the rest of the API follows.
    conversations: Dict[str, assistant_module.Assistant] = {}
    settings: Dict[str, AssistantSettings] = {}

    def session_key(request: Request) -> str:
        token = request.cookies.get('relion_web_session')
        return token or 'anonymous'

    def tool_context(request: Request) -> tools_module.ToolContext:
        return tools_module.ToolContext(
            guard=guard,
            projects=projects,
            jobs=jobs,
            queue=getattr(app.state, 'queue', None),
            session_jobs=lambda: [
                job.as_dict() for job in jobs.list() if job.status == 'running'
            ],
        )

    def public_config(key: str) -> dict:
        """The whole shape the panel expects.

        Both /config and /settings return this: a client that saved settings and
        got a *smaller* object would keep rendering the old state, and one that
        read a missing key would crash - which is what happened when /settings
        omitted `mcp`.
        """
        override = settings.get(key, AssistantSettings())
        payload = config_for(key).public()
        payload['enabled'] = True
        payload['writes_allowed'] = (
            override.allow_writes if override.allow_writes is not None else True
        )
        payload['mcp'] = {
            'enabled': bool(getattr(app.state, 'mcp_enabled', False)),
            'path': mcp_server.MCP_PATH,
            'error': getattr(app.state, 'mcp_error', None),
            # The count reflects what is actually published, which is smaller
            # when the server is read-only.
            'tools': len(getattr(app.state, 'mcp_tool_names', []) or []),
            'allow_writes': bool(getattr(app.state, 'mcp_allow_writes', False)),
        }
        return payload

    def config_for(key: str) -> assistant_module.AssistantConfig:
        override = settings.get(key)
        if override is None:
            return default_config
        return assistant_module.AssistantConfig(
            provider=override.provider or default_config.provider,
            model=override.model or default_config.resolved_model,
            api_key=override.api_key or default_config.api_key,
            base_url=(
                override.base_url
                if override.base_url is not None
                else default_config.base_url
            ),
        )

    def conversation(request: Request) -> assistant_module.Assistant:
        key = session_key(request)
        current = conversations.get(key)
        if current is None:
            override = settings.get(key)
            current = assistant_module.Assistant(
                tool_context(request),
                config_for(key),
                allow_writes=(
                    override.allow_writes if override and override.allow_writes is not None
                    else True
                ),
            )
            conversations[key] = current
        return current

    app.state.assistant_conversations = conversations
    app.state.assistant_settings = settings
    app.state.assistant_enabled = enable_assistant
    app.state.assistant_default_config = default_config

    # ------------------------------------------------------------------- MCP
    if enable_mcp:
        ctx = tools_module.ToolContext(
            guard=guard,
            projects=projects,
            jobs=jobs,
            queue=getattr(app.state, 'queue', None),
            session_jobs=lambda: [
                job.as_dict() for job in jobs.list() if job.status == 'running'
            ],
        )
        try:
            server = mcp_server.create_server(
                ctx, hosts=mcp_hosts or [], allow_writes=mcp_allow_writes
            )
        except mcp_server.MCPUnavailable as exc:  # pragma: no cover
            app.state.mcp_error = str(exc)
        else:
            mcp_server.mount(app, server)
            app.state.mcp_error = None
            app.state.mcp_allow_writes = mcp_allow_writes
            app.state.mcp_tool_names = [
                tool.name
                for tool in mcp_server.build_registry()
                if mcp_allow_writes or not tool.writes
            ]

    # -------------------------------------------------------------- assistant
    @app.get('/api/assistant/config')
    async def assistant_config(request: Request) -> dict:
        if not enable_assistant:
            raise HTTPException(404, detail='the assistant is not enabled')
        return public_config(session_key(request))

    @app.post('/api/assistant/settings')
    async def assistant_settings(
        request: Request, body: AssistantSettings
    ) -> dict:
        if not enable_assistant:
            raise HTTPException(404, detail='the assistant is not enabled')
        key = session_key(request)
        if body.provider and body.provider not in assistant_module.PROVIDERS:
            raise HTTPException(400, detail=f'unknown provider {body.provider!r}')

        current = settings.get(key, AssistantSettings())
        merged = AssistantSettings(
            provider=body.provider or current.provider,
            model=body.model if body.model is not None else current.model,
            # An empty string clears the stored key; None leaves it alone.
            api_key=body.api_key if body.api_key is not None else current.api_key,
            base_url=body.base_url if body.base_url is not None else current.base_url,
            allow_writes=(
                body.allow_writes
                if body.allow_writes is not None
                else current.allow_writes
            ),
        )
        settings[key] = merged

        # Rebuild the conversation so the change takes effect.
        #
        # The history is carried over only when the model itself is unchanged:
        # switching provider mid-conversation would replay one vendor's message
        # objects into another's request format, and a history that ends in a
        # completed turn also tells the model the work is already done. A
        # provider change is a fresh start, which is what a user changing their
        # key expects anyway.
        existing = conversations.get(key)
        same_model = existing is not None and (
            existing.config.provider == config_for(key).provider
            and existing.config.resolved_model == config_for(key).resolved_model
        )
        fresh = assistant_module.Assistant(
            tool_context(request),
            config_for(key),
            allow_writes=(
                merged.allow_writes if merged.allow_writes is not None else True
            ),
        )
        if same_model:
            fresh.history = existing.history
        conversations[key] = fresh

        return public_config(key)

    @app.get('/api/assistant/tools')
    async def assistant_tools() -> dict:
        if not enable_assistant:
            raise HTTPException(404, detail='the assistant is not enabled')
        return {'tools': mcp_server.describe_registry()}

    @app.post('/api/assistant/chat')
    async def assistant_chat(request: Request, body: ChatRequest) -> StreamingResponse:
        if not enable_assistant:
            raise HTTPException(404, detail='the assistant is not enabled')
        if not body.message.strip():
            raise HTTPException(400, detail='message is empty')

        current = conversation(request)
        # A project given with the message is the one the tools assume, so the
        # model does not spend a turn asking which project was meant.
        prompt = body.message
        if body.project:
            prompt = (
                f'[The open project is {body.project}]\n\n{body.message}'
            )

        async def events():
            yield _sse('start', {'tools': len(current.registry)})
            try:
                async for event in current.run(prompt, approved=body.approved):
                    yield _sse(event.kind, event.as_sse())
                    if event.kind == 'done':
                        for pending in current.pending_calls():
                            yield _sse('needs_approval', pending)
            except Exception as exc:  # noqa: BLE001 - the browser must see it
                yield _sse('error', {'text': assistant_module._friendly_error(exc)})

        return StreamingResponse(
            events(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache, no-transform',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            },
        )

    @app.post('/api/assistant/approve')
    async def assistant_approve(request: Request, body: ApprovalRequest) -> dict:
        """Approve or deny a parked write.

        Approval executes the call here rather than telling the model to try
        again: the call the user saw is the call that runs, and it runs once.
        """
        if not enable_assistant:
            raise HTTPException(404, detail='the assistant is not enabled')
        current = conversation(request)
        if not body.approve:
            if not current.deny(body.key):
                raise HTTPException(404, detail='no such pending call')
            return {'approved': False, 'key': body.key}

        try:
            outcome = current.execute_approved(body.key)
        except assistant_module.ToolError as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        return {'approved': True, 'key': body.key, **outcome}

    @app.post('/api/assistant/reset')
    async def assistant_reset(request: Request) -> dict:
        """Start a new conversation, dropping history and approvals."""
        if not enable_assistant:
            raise HTTPException(404, detail='the assistant is not enabled')
        conversations.pop(session_key(request), None)
        return {'reset': True}


def _sse(event: str, payload: Dict[str, Any]) -> str:
    """One server-sent event.

    The event name is the `kind`, so the browser can switch on it without
    parsing, and the payload is the whole assistant event so nothing is lost.
    """
    return f'event: {event}\ndata: {json.dumps(payload, default=str)}\n\n'
