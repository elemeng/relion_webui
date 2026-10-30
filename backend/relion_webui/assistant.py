# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""The in-browser assistant: an LLM with the same tools the MCP server exposes.

The model comes from whichever provider the operator configured (OpenAI,
Anthropic, Google, or any OpenAI-compatible endpoint such as a local vLLM or
Ollama), because a cryo-ET lab is as likely to run a model on its own GPU as to
have an API key. `pydantic-ai` is the harness: it owns the tool-call loop, the
message history format and provider differences, which is the part that is
tedious and easy to get subtly wrong.

Two design decisions are worth stating, because both are safety properties:

*Reads run freely; writes stop and ask.* The model may inspect a project as much
as it likes. A tool marked `writes` is not executed: it returns a
`needs_approval` result and the run parks. The UI shows the exact call, the user
approves or denies, and the run resumes with that decision. This is why the tool
registry marks writes at all - the marker is the gate.

*The assistant has no more access than the browser.* Every tool goes through the
same `PathGuard` and `ProjectRegistry` the HTTP routes use, so an assistant
cannot reach a directory the person driving it could not open.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from . import tools as tools_module


# Providers the UI offers. The id is what the client sends; `kind` decides which
# pydantic-ai model class builds it, because an OpenAI-compatible local server
# and OpenAI itself take the same wire format but a different base URL.
PROVIDERS: Dict[str, dict] = {
    'openai': {
        'label': 'OpenAI',
        'kind': 'openai',
        'env': 'OPENAI_API_KEY',
        'default_model': 'gpt-4.1-mini',
        'models': ['gpt-4.1', 'gpt-4.1-mini', 'gpt-4o', 'gpt-4o-mini', 'o4-mini'],
        'base_url': None,
    },
    'anthropic': {
        'label': 'Anthropic',
        'kind': 'anthropic',
        'env': 'ANTHROPIC_API_KEY',
        'default_model': 'claude-sonnet-4-5',
        'models': [
            'claude-sonnet-4-5',
            'claude-opus-4-1',
            'claude-haiku-4-5',
        ],
        'base_url': None,
    },
    'google': {
        'label': 'Google',
        'kind': 'google',
        'env': 'GEMINI_API_KEY',
        'default_model': 'gemini-2.5-flash',
        'models': ['gemini-2.5-pro', 'gemini-2.5-flash'],
        'base_url': None,
    },
    'local': {
        'label': 'Local / OpenAI-compatible',
        'kind': 'openai',
        'env': 'RELION_WEB_LLM_API_KEY',
        'default_model': '',
        'models': [],
        # Any server speaking the OpenAI chat API: vLLM, Ollama, LM Studio, a
        # gateway. This is the option a lab with its own GPU uses.
        'base_url': 'http://127.0.0.1:8000/v1',
    },
}


SYSTEM_PROMPT = """\
You are the assistant inside a RELION cryo-ET web interface. You help someone
drive their project: understand what ran, diagnose why a job failed, and prepare
the next job.

The tools are the only way you can see or change anything. Call `list_projects`
before naming a project, and `get_pipeline` before reasoning about a job -
guessing at process names or paths wastes a turn and is usually wrong.

When a job failed, read its log before proposing a fix. RELION's programs state
the problem plainly near the end of run.out: a missing input, a parameter out of
range, out of memory. Quote the relevant line.

When you propose a change to a project, be concrete: the job type, the option
values that differ from the defaults, and why. Writing tools ask the user to
confirm each call in the interface, so a clear proposal is what makes that
prompt answerable. Prefer `submit_job` with dry_run first when the point is to
show what would run.

For more than one job, use the queue rather than a sequence of `submit_job` and
`wait_for_job` calls. A queued job gets its number immediately, so its output
paths are known before it runs and the whole chain can be queued in one pass:
pass `inputs={"in_tomoset": "q0003:tomograms.star"}` to name an earlier entry's
output instead of waiting for the file. Queue every stage, then call
`list_queue` once per check instead of polling each job; its `blocked_by` field
says why an entry has not started, in plain words.

Be brief. This is a tool used mid-task, not a report. Answer in the language the
user writes in.
"""


class ToolError(RuntimeError):
    """An approval refers to a call that is no longer pending."""


class AssistantUnavailable(RuntimeError):
    """No provider is configured, or its package is missing."""


@dataclass
class AssistantConfig:
    """Which model to use, resolved from the environment and the CLI.

    The API key is held here only for the lifetime of a request. It is never
    written to disk and never sent back to the browser - `public()` is what the
    client sees, and it says only whether a key exists.
    """

    provider: str = 'openai'
    model: str = ''
    api_key: str = ''
    base_url: Optional[str] = None

    @property
    def resolved_model(self) -> str:
        return self.model or PROVIDERS.get(self.provider, {}).get('default_model', '')

    def public(self) -> dict:
        # `api_key` is already resolved (CLI/env/session) by the caller; falling
        # back to the environment here again would report `configured: true` for
        # a provider whose key was never meant for this process.
        key = self.api_key
        return {
            'provider': self.provider,
            'model': self.resolved_model,
            'base_url': self.base_url,
            'configured': bool(key),
            'providers': [
                {
                    'id': pid,
                    'label': spec['label'],
                    'models': spec['models'],
                    'default_model': spec['default_model'],
                    'base_url': spec['base_url'],
                }
                for pid, spec in PROVIDERS.items()
            ],
        }


def config_from_env() -> AssistantConfig:
    """Read the assistant's defaults from the environment.

    `RELION_WEB_LLM_PROVIDER` and friends, so a site can configure the assistant
    in its service unit rather than in the browser. The key is deliberately
    server-side: it must not be shipped to every browser that opens the panel.
    """
    provider = os.environ.get('RELION_WEB_LLM_PROVIDER', 'openai').strip().lower()
    if provider not in PROVIDERS:
        provider = 'openai'
    spec = PROVIDERS[provider]
    return AssistantConfig(
        provider=provider,
        model=os.environ.get('RELION_WEB_LLM_MODEL', '') or spec['default_model'],
        api_key=os.environ.get(spec['env'], ''),
        base_url=os.environ.get('RELION_WEB_LLM_BASE_URL') or spec['base_url'],
    )


def build_model(config: AssistantConfig):
    """Construct the pydantic-ai model for a provider.

    Imported lazily so the agent runs without the `assistant` extra: a RELION
    site that does not want an LLM should not fail to start because a
    transitive import is missing.
    """
    key = config.api_key or os.environ.get(
        PROVIDERS.get(config.provider, {}).get('env', ''), ''
    )
    kind = PROVIDERS.get(config.provider, {}).get('kind', 'openai')
    model_name = config.resolved_model

    if not model_name:
        raise AssistantUnavailable(
            f'no model configured for provider {config.provider!r}'
        )
    if not key:
        raise AssistantUnavailable(
            f'no API key for {config.provider!r}; set '
            f'{PROVIDERS.get(config.provider, {}).get("env", "the key")} or enter '
            'one in the assistant settings'
        )

    try:
        if kind == 'anthropic':
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(
                model_name, provider=AnthropicProvider(api_key=key)
            )
        if kind == 'google':
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            return GoogleModel(model_name, provider=GoogleProvider(api_key=key))
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        kwargs: Dict[str, Any] = {'api_key': key}
        if config.base_url:
            kwargs['base_url'] = config.base_url
            kwargs['http_client'] = _http_client_for(config.base_url)
        return OpenAIChatModel(model_name, provider=OpenAIProvider(**kwargs))
    except ImportError as exc:
        raise AssistantUnavailable(
            "the assistant needs its provider package: "
            "pip install -e '.[assistant]'"
        ) from exc


def _http_client_for(base_url: str):
    """An HTTP client that ignores proxy environment variables for loopback.

    A lab that runs its model on the same machine is the case `--llm-provider
    local` exists for, and it is also the case a corporate `HTTP_PROXY` breaks:
    httpx honours those variables by default, so a request to 127.0.0.1 is sent
    to the proxy instead and either times out or is refused. `trust_env=False`
    is the documented way to say "this address is local, do not proxy it", and
    it is applied only when the host really is loopback - a remote gateway must
    still use the proxy.
    """
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or '').lower()
    local = host in ('127.0.0.1', 'localhost', '::1', '[::1]', '0.0.0.0')

    # httpx2 is what pydantic-ai 2.x uses; httpx still works but is deprecated
    # and will be dropped in v3. Pick whichever is installed so this does not
    # pin the caller to one client library.
    try:
        import httpx2 as httpx

        timeout = httpx.Timeout(120.0, connect=10.0)
    except ImportError:  # pragma: no cover - depends on the installed extra
        import httpx

        timeout = httpx.Timeout(120.0, connect=10.0)

    return httpx.AsyncClient(timeout=timeout, trust_env=not local)


@dataclass
class PendingCall:
    """A write the model wants to make, awaiting a human yes."""

    tool: str
    arguments: Dict[str, Any]
    reason: str = ''


@dataclass
class AssistantEvent:
    """One thing that happened, in the shape the browser consumes.

    A flat event rather than a message union: the UI is a chat transcript with
    inline tool activity, and every event maps to one line of it.
    """

    kind: str  # text | tool_start | tool_result | needs_approval | done | error
    text: str = ''
    tool: str = ''
    arguments: Optional[Dict[str, Any]] = None
    result: Any = None
    writes: bool = False

    def as_sse(self) -> dict:
        payload: Dict[str, Any] = {'kind': self.kind}
        if self.text:
            payload['text'] = self.text
        if self.tool:
            payload['tool'] = self.tool
        if self.arguments is not None:
            payload['arguments'] = self.arguments
        if self.result is not None:
            payload['result'] = self.result
        if self.writes:
            payload['writes'] = True
        return payload


class Assistant:
    """One conversation with a model, over the shared tool registry.

    Holds the message history so a follow-up question has context, and holds the
    approvals a run is waiting on. One instance per browser session, in memory:
    a conversation is not worth persisting, and losing it on restart is better
    than storing prompts and project details on disk.
    """

    def __init__(
        self,
        ctx: tools_module.ToolContext,
        config: AssistantConfig,
        *,
        allow_writes: bool = True,
        max_tool_calls: int = 12,
    ):
        self.ctx = ctx
        self.config = config
        self.allow_writes = allow_writes
        self.max_tool_calls = max_tool_calls
        self.registry = tools_module.build_registry()
        self.history: List[Any] = []
        self.pending: Dict[str, PendingCall] = {}
        self._approved: set = set()

    # ------------------------------------------------------------------ agent
    def _build_agent(self):
        from pydantic_ai import Agent

        model = build_model(self.config)
        agent = Agent(
            model,
            instructions=SYSTEM_PROMPT,
            # Passing the registry in as data rather than generating a function
            # per tool: one wrapper handles every tool, so adding a tool to
            # tools.py needs no change here or in the MCP server.
            tools=self._tool_functions(),
        )
        return agent

    def _tool_functions(self) -> List[Any]:
        """One callable per registry entry, sharing a single dispatcher.

        The name and docstring come from the registry, which is what the model
        sees; `_dispatch` decides whether to run or to park for approval.

        Each wrapper gets the tool's *resolved* schema, not a `**kwargs` catch-all
        and not string annotations: pydantic-ai builds the tool schema from the
        signature it finds, so an unresolved `RunContext` forward reference
        fails at registration. The schema is built once in tools.py, and this
        reuses it rather than describing the parameters a second time.
        """
        return [_ai_tool(tool, self) for tool in self.registry]

    def _dispatch(self, tool: tools_module.Tool, arguments: Dict[str, Any]) -> Any:
        """Run a tool, or refuse a write that has not been approved.

        Returning a dict rather than raising on refusal is deliberate: the model
        must see *why* nothing happened, so it can ask the user instead of
        retrying blindly.
        """
        if tool.writes and not self.allow_writes:
            return {
                'error': 'writes are disabled for this session',
                'hint': 'the user can enable them in the assistant settings',
            }
        if tool.writes:
            key = json.dumps([tool.name, arguments], sort_keys=True, default=str)
            if key not in self._approved:
                self.pending[key] = PendingCall(
                    tool=tool.name, arguments=arguments
                )
                return {
                    'status': 'needs_approval',
                    'tool': tool.name,
                    'arguments': arguments,
                    'message': (
                        'This changes the project and needs the user to confirm it '
                        'in the interface. Tell them what it will do and wait.'
                    ),
                }
            self._approved.discard(key)
        return tools_module.call_tool(self.registry, tool.name, arguments, self.ctx)

    def pending_calls(self) -> List[dict]:
        """The writes awaiting a decision, in the shape the browser sends back.

        The key is present so the client can echo exactly what it approved; the
        same key is what `_dispatch` checks against `_approved`, so the two
        cannot disagree about which call was allowed.
        """
        return [
            {
                'key': key,
                'tool': call.tool,
                'arguments': call.arguments,
            }
            for key, call in self.pending.items()
        ]

    def approve(self, key: str) -> bool:
        """Allow one pending call on the next run."""
        if key not in self.pending:
            return False
        self._approved.add(key)
        self.pending.pop(key, None)
        return True

    def deny(self, key: str) -> bool:
        return self.pending.pop(key, None) is not None

    def execute_approved(self, key: str) -> Any:
        """Run a parked call now that a human approved it.

        Deliberately not "approve, then ask the model to try again". Asking
        again spends a round trip and depends on the model repeating itself
        exactly - and a model that decides to do something slightly different on
        the retry would run something the user never saw. The approved call is
        the call that runs.
        """
        pending = self.pending.pop(key, None)
        if pending is None:
            raise ToolError(f'no pending call {key!r}')
        tool = next((entry for entry in self.registry if entry.name == pending.tool), None)
        if tool is None:
            raise ToolError(f'unknown tool {pending.tool!r}')
        # Marked allowed for the same reason as in `run`: if the model reissues
        # the identical call on the next turn, it executes instead of parking.
        self._approved.add(key)
        result = tools_module.call_tool(
            self.registry, pending.tool, pending.arguments, self.ctx
        )
        self._approved.discard(key)
        return {'tool': pending.tool, 'arguments': pending.arguments, 'result': result}

    # -------------------------------------------------------------------- run
    async def run(self, prompt: str, *, approved: Sequence[dict] = ()) -> AsyncIterator[AssistantEvent]:
        """Run one turn, streaming events as they happen.

        `approved` carries the calls the user accepted since the last turn, as
        `{"tool": ..., "arguments": {...}}`; they are marked allowed before the
        run so the model's retry executes rather than parking again.
        """
        for entry in approved:
            key = json.dumps(
                [entry.get('tool'), entry.get('arguments', {})],
                sort_keys=True,
                default=str,
            )
            self._approved.add(key)

        try:
            agent = self._build_agent()
        except AssistantUnavailable as exc:
            yield AssistantEvent(kind='error', text=str(exc))
            return

        from pydantic_ai.messages import (
            FunctionToolCallEvent,
            FunctionToolResultEvent,
            PartDeltaEvent,
            PartStartEvent,
            TextPart,
            TextPartDelta,
        )

        started = 0
        # A turn can contain several model responses: one that calls a tool,
        # then one that answers. Their text parts are separate, and joining
        # them with no separator runs the sentences together
        # ("Let me check.The project has..."). `wrote_text` tracks whether a
        # new response needs a paragraph break before its first word.
        wrote_text = False
        try:
            async with agent.run_stream_events(
                prompt, message_history=self.history or None
            ) as events:
                async for event in events:
                    if isinstance(event, PartStartEvent) and isinstance(
                        event.part, TextPart
                    ):
                        if event.part.content:
                            prefix = '\n\n' if wrote_text else ''
                            wrote_text = True
                            yield AssistantEvent(
                                kind='text', text=prefix + event.part.content
                            )
                    elif isinstance(event, PartDeltaEvent) and isinstance(
                        event.delta, TextPartDelta
                    ):
                        if event.delta.content_delta:
                            wrote_text = True
                            yield AssistantEvent(
                                kind='text', text=event.delta.content_delta
                            )
                    elif isinstance(event, FunctionToolCallEvent):
                        started += 1
                        tool = next(
                            (t for t in self.registry if t.name == event.part.tool_name),
                            None,
                        )
                        yield AssistantEvent(
                            kind='tool_start',
                            tool=event.part.tool_name,
                            arguments=_call_arguments(event.part),
                            writes=bool(tool and tool.writes),
                        )
                        if started > self.max_tool_calls:
                            yield AssistantEvent(
                                kind='error',
                                text=(
                                    f'stopped after {self.max_tool_calls} tool calls; '
                                    'ask again to continue'
                                ),
                            )
                            return
                    elif isinstance(event, FunctionToolResultEvent):
                        # The result lives on `event.part` (a ToolReturnPart),
                        # not on the event itself.
                        part = event.part
                        yield AssistantEvent(
                            kind='tool_result',
                            tool=part.tool_name,
                            result=part.content,
                        )
                    elif type(event).__name__ == 'AgentRunResultEvent':
                        # The result carries the full history, which is what the
                        # next turn continues from.
                        if hasattr(event, 'result'):
                            self.history = event.result.all_messages()
            yield AssistantEvent(kind='done')
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            yield AssistantEvent(kind='error', text=_friendly_error(exc))


_TOOL_CACHE: Dict[str, Any] = {}


def _call_arguments(part: Any) -> Optional[Dict[str, Any]]:
    """The arguments of a streamed tool call, as a dict.

    They arrive as a JSON string; a malformed one is worth showing as-is rather
    than dropping, because it is the reason the call failed.
    """
    getter = getattr(part, 'args_as_dict', None)
    if callable(getter):
        try:
            return getter()
        except Exception:  # noqa: BLE001 - malformed JSON from the model
            pass
    raw = getattr(part, 'args', None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {'_raw': raw}
    return None


def _ai_tool(tool: tools_module.Tool, owner: 'Assistant') -> Any:
    """Build a pydantic-ai tool with a real, resolved signature.

    pydantic-ai calls `typing.get_type_hints(func)`, which reads
    `func.__annotations__` and evaluates any strings against
    `func.__globals__`. Since this package uses `from __future__ import
    annotations`, the tool functions' annotations are strings from *tools.py*,
    so this wrapper:

    * sets `__annotations__` to the resolved types, so there is nothing for
      pydantic-ai to evaluate, and
    * sets `__signature__` to match, with `RunContext` spelled as a real class.

    Without both, registration fails with `NameError: RunContext` or a
    `KeyError` on the context parameter - the wrapper's globals are this module,
    not the tool's.
    """
    import inspect
    import typing

    from pydantic_ai import RunContext

    hints = typing.get_type_hints(tool.fn)
    hints.pop('return', None)
    signature = inspect.signature(tool.fn)
    # Internal parameters (ctx, on_progress) are bound by the registry, not
    # supplied by the model. Exposing one asks pydantic for a Callable schema,
    # which it cannot express.
    field_names = [
        name
        for name in signature.parameters
        if name not in tools_module._INTERNAL_PARAMETERS
    ]

    def call(run_context: Any, **kwargs: Any) -> Any:
        return owner._dispatch(tool, kwargs)

    parameter_list = [
        inspect.Parameter(
            'run_context',
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=RunContext,
        ),
        *[
            signature.parameters[name].replace(annotation=hints.get(name, str))
            for name in field_names
        ],
    ]

    call.__name__ = tool.name
    call.__doc__ = tool.description
    call.__signature__ = signature.replace(parameters=parameter_list)  # type: ignore[attr-defined]
    call.__annotations__ = {  # type: ignore[attr-defined]
        'run_context': RunContext,
        **{name: hints.get(name, str) for name in field_names},
    }
    return call


def _friendly_error(exc: Exception) -> str:
    """Turn a provider failure into something a user can act on.

    A traceback in a chat panel is noise; the four failures that actually happen
    are a wrong key, a wrong model name, no network, and a rate limit.
    """
    text = str(exc)
    lowered = text.lower()
    if 'api key' in lowered or '401' in lowered or 'unauthorized' in lowered:
        return 'the provider rejected the API key; check it in the assistant settings'
    if '404' in lowered and 'model' in lowered:
        return f'the provider does not recognise that model: {text}'
    if 'rate limit' in lowered or '429' in lowered:
        return 'the provider is rate-limiting this key; wait a moment and retry'
    if 'connect' in lowered or 'timeout' in lowered or 'resolve' in lowered:
        return f'could not reach the provider: {text}'
    return text or type(exc).__name__
