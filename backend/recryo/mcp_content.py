"""MCP resources and prompts.

Tools are what an agent *calls*; resources and prompts are the other half of the
protocol:

* A **resource** is addressable read-only content the client can attach without
  deciding to fetch it - the pipeline, a job's options, a log. Claude Desktop and
  similar clients show these as things a person can pin to the conversation, and
  a coding agent can pull one in rather than spending a tool call.
* A **prompt** is a reusable instruction template the *user* invokes (`/sta-workflow`
  in a client's menu), with arguments the client collects. It is how a domain
  expert hands their know-how to whoever is driving the agent, without that
  person having to know RELION's job order.

Both are thin: they call the same registry functions the tools do, so there is
no second source of truth about what a pipeline looks like.

URIs are `relion://` and encode absolute paths, because a resource must be
addressable without a tool call. Paths are percent-encoded so a directory with a
space or a `#` in it still forms a valid URI.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

from . import tools as tools_module


_SCHEME = 'relion://'


def project_uri(path: str, leaf: str = 'pipeline') -> str:
    """``relion://project/<encoded path>/<leaf>``."""
    return f'{_SCHEME}project/{quote(str(path), safe="")}/{leaf}'


def job_uri(project: str, process_name: str, leaf: str = 'log') -> str:
    """``relion://job/<encoded project>/<process>/<leaf>``."""
    return (
        f'{_SCHEME}job/{quote(str(project), safe="")}/'
        f'{quote(str(process_name), safe="")}/{leaf}'
    )


# ------------------------------------------------------------------ resources
#
# A template's parameters are named in the URI. FastMCP turns
# `relion://project/{path}/pipeline` into a resource with one argument, so the
# functions below take exactly the names in their URI.

def _context(ctx: tools_module.ToolContext):
    """Bind the registry and context once, for every resource and prompt."""
    registry = tools_module.build_registry()

    def call(name: str, **arguments: Any) -> Any:
        return tools_module.call_tool(registry, name, arguments, ctx)

    return call


def register_resources(server: Any, ctx: tools_module.ToolContext) -> None:
    """Add the read-only resources.

    The decorator form is used rather than `FunctionResource.from_function`
    because only the decorator recognises a `{param}` URI as a *template*: the
    factory wraps the string in `AnyUrl`, which treats the braces as literal
    text, so the resource registers a fixed URI nobody can address. A template is
    required here because a project path is part of the URI.

    Each resource calls the same registry function the matching tool does, so a
    resource and its tool cannot disagree about what a pipeline looks like.
    """
    call = _context(ctx)

    @server.resource(
        f'{_SCHEME}project/{{path}}/pipeline',
        name='Project pipeline',
        description='Every job, its status, and the data that flows between them.',
        mime_type='application/json',
    )
    def pipeline_resource(path: str) -> str:
        return _json(call('get_pipeline', project=unquote(path)))

    @server.resource(
        f'{_SCHEME}project/{{path}}/summary',
        name='Project status summary',
        description='Job counts by status, without the node graph.',
        mime_type='application/json',
    )
    def summary_resource(path: str) -> str:
        return _json(call('get_pipeline', project=unquote(path), include_nodes=False))

    @server.resource(
        f'{_SCHEME}job/{{path}}/{{process}}/log',
        name='Job log',
        description="A job's run.out tail.",
        mime_type='text/plain',
    )
    def log_resource(path: str, process: str) -> str:
        result = call(
            'get_job_log', project=unquote(path), process_name=unquote(process)
        )
        if isinstance(result, dict) and 'error' in result:
            return str(result['error'])
        return str(result.get('log', ''))

    @server.resource(
        f'{_SCHEME}job/{{path}}/{{process}}/options',
        name='Job options',
        description='The options this job recorded in job.star.',
        mime_type='application/json',
    )
    def options_resource(path: str, process: str) -> str:
        return _json(
            call('get_job', project=unquote(path), process_name=unquote(process))
        )

    @server.resource(
        f'{_SCHEME}job/{{path}}/{{process}}/metrics',
        name='Job metrics',
        description='Resolution, particle counts and other results.',
        mime_type='application/json',
    )
    def metrics_resource(path: str, process: str) -> str:
        return _json(
            call('get_job_metrics', project=unquote(path), process_name=unquote(process))
        )

    @server.resource(
        f'{_SCHEME}projects',
        name='Projects',
        description='Every RELION project this agent can reach.',
        mime_type='application/json',
    )
    def projects_resource() -> str:
        return _json(call('list_projects'))

    @server.resource(
        f'{_SCHEME}job-types',
        name='Job types',
        description='Job types grouped by workflow stage.',
        mime_type='application/json',
    )
    def job_types_resource() -> str:
        return _json(call('list_job_types'))


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=2)


# -------------------------------------------------------------------- prompts
#
# A prompt is the place to put domain knowledge that is *policy*, not data: the
# order RELION jobs go in, what to check before continuing, how to read a
# failure. The agent gets it as instructions; the user picks it from their
# client's slash menu.

STA_WORKFLOW = """\
Run subtomogram averaging on {project}.

Follow this order. Each step consumes what the previous one produced; do not
start a step until its input exists.

1. **Inspect first.** Call `get_pipeline` with include_nodes=false, then
   `get_job_log` on anything failed. Do not assume the project is empty or that
   the expected jobs are present - read what is there.

2. **Reconstruct tomograms**, if they are missing or stale:
   `tomo_reconstruct_tomograms`, input `in_tiltseries` = the aligned tilt-series
   STAR file. Binning of 4-8 gives a workable tomogram for picking.

3. **Denoise**, if you want better picking: this is two jobs.
   - `tomo_denoise_train` - needs `in_tomoset` pointing at tomograms that carry
     `rlnTomoReconstructedTomogramHalf1`/`Half2`. Produces a model.
   - `tomo_denoise_predict` - needs the *same* `in_tomoset` plus
     `care_denoising_model` set to the .tar.gz the train job wrote.
   Wait for train before submitting predict; `validate_job` now reports a missing
   input file, so use it.

4. **Pick particles**: `tomo_pick_tomograms` on the denoised (or plain) tomogram
   set. Check `get_job_metrics` for the particle count before going further - a
   count near zero means picking failed, not that the data is clean.

5. **Extract subtomograms**: `tomo_subtomo` with `in_optimisation` set to the
   optimisation set the picking job wrote.

6. **Average**: `inimodel` then `class3d` then `autorefine`. Check
   `get_job_metrics` after each: a resolution that gets *worse* means the
   previous step is not converged, not that the next one is needed.

Rules that apply throughout:
- After every `submit_job`, call `wait_for_job` before deciding anything. Read the
  log of a failed job before retrying it.
- Use `get_job_metrics` to judge whether a result is usable, not just whether the
  job exited zero.
- State what you are about to submit and why, then submit. If a step fails
  twice, stop and report instead of trying variations.
"""

DIAGNOSE_FAILURE = """\
Find out why {process} failed in {project}, and propose one fix.

1. `get_job_log` and read the *end* of it. RELION's programs state the problem
   plainly: a missing input, a parameter out of range, out of memory, a mask
   that does not cover the particle.
2. `get_job` for the options it actually ran with - not today's defaults. The
   values are in its job.star.
3. Check whether its inputs exist and look sane: `get_pipeline` for the producer
   job, `get_job_metrics` for what it reported.
4. Quote the decisive log line, then say what you would change and why.

Do not resubmit yet. Say what you found, and let the person confirm.
"""

NEXT_STEP = """\
Look at {project} and say what should run next.

Read `get_pipeline` for what exists and its statuses, then `get_job_metrics` on
the most recent successful job. Answer in terms of what is missing for the goal
the person states, not in terms of what would be nice.

If something is running, say what and how long it has been going rather than
proposing work that depends on it. If something failed, say that first: a
downstream job on a failed input is wasted time.

Give one recommendation with a reason, and the option values that matter. Then
stop and let the person decide - do not submit anything in this turn.
"""


def register_prompts(server: Any, ctx: tools_module.ToolContext) -> None:
    """Add the workflow prompts.

    Each is a template with arguments the client fills in. They are deliberately
    written as *procedure* rather than as tool documentation: the tools describe
    themselves, so repeating that here would be duplication that goes stale.
    """
    # mcp 2 moved this module; the class and its from_function() are unchanged.
    try:
        from mcp.server.mcpserver.prompts import Prompt  # mcp 2.x
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp.prompts import Prompt

    def sta_workflow(project: str) -> str:
        """Subtomogram averaging, end to end, in the order RELION requires."""
        return STA_WORKFLOW.format(project=project)

    def diagnose_failure(project: str, process: str) -> str:
        """Work out why one job failed, without resubmitting it."""
        return DIAGNOSE_FAILURE.format(project=project, process=process)

    def next_step(project: str) -> str:
        """Recommend the next job from the project's current state."""
        return NEXT_STEP.format(project=project)

    for function, name, description in (
        (sta_workflow, 'sta_workflow', STA_WORKFLOW.splitlines()[0]),
        (diagnose_failure, 'diagnose_failure', DIAGNOSE_FAILURE.splitlines()[0]),
        (next_step, 'next_step', NEXT_STEP.splitlines()[0]),
    ):
        server.add_prompt(Prompt.from_function(function, name=name, description=description))


def resource_uris() -> Dict[str, str]:
    """The fixed URIs, for documentation and tests."""
    return {
        'projects': f'{_SCHEME}projects',
        'job_types': f'{_SCHEME}job-types',
    }


def template_uris() -> List[str]:
    return [
        f'{_SCHEME}project/{{path}}/pipeline',
        f'{_SCHEME}project/{{path}}/summary',
        f'{_SCHEME}job/{{path}}/{{process}}/log',
        f'{_SCHEME}job/{{path}}/{{process}}/options',
        f'{_SCHEME}job/{{path}}/{{process}}/metrics',
    ]
