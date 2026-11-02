/**
 * Vite middleware that answers FlowGram's API from the RELION agent.
 *
 * Vite dev server only. A production build would need the same logic behind
 * nginx or in the agent; keeping it here for now means the demo has one moving
 * part instead of two.
 */
import type { Connect } from 'vite';

import {
  queueBody,
  parseRunId,
  processesFor,
  runIdFor,
  type ExecutionIntent,
} from './execution';
import { jobTypeForLabel } from './translate';
import {
  projectFromRelion,
  rememberJobSchema,
  rememberJobTypes,
  translate,
  workflowFromPipeline,
} from './translate';
import { layoutPipeline } from '../../frontend/src/lib/layout';

const AGENT = process.env.RELION_WEB_API ?? 'http://127.0.0.1:8765';

let schemaLoaded = false;

/**
 * Load the job schema once, before the first projection.
 *
 * `workflowFromPipeline` needs it to pick each node's type and starting
 * parameters, and it runs synchronously, so the fetch cannot happen there.
 */
async function ensureJobSchema(): Promise<void> {
  if (schemaLoaded) return;
  try {
    const [schema, types] = await Promise.all([
      agentJson('/api/job-schema'),
      agentJson('/api/project/job-types'),
    ]);
    rememberJobSchema(schema as never);
    rememberJobTypes((types as { job_types: Array<{ id: string; process_type_label?: string }> }).job_types);
    schemaLoaded = true;
  } catch {
    // Pipelines still project; their nodes fall back to no parameters.
  }
}

async function agentJson(
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<unknown> {
  const response = await fetch(`${AGENT}${path}`, {
    method: init?.method ?? 'GET',
    ...(init?.body === undefined
      ? {}
      : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(init.body),
        }),
  });
  if (!response.ok) {
    // Carry the agent's own message through. A 400 from /api/queue names the
    // missing option, and that is the only diagnosis the user gets.
    const detail = await response.text().catch(() => '');
    let message = detail;
    try {
      const parsed = JSON.parse(detail);
      message = parsed.detail ?? parsed.error ?? detail;
    } catch {
      /* not JSON; use the raw body */
    }
    throw new Error(`${path} -> ${response.status}: ${String(message).slice(0, 200)}`);
  }
  return response.json();
}

/** Read the pipeline and lay it out. Used by both bootstrap and a single read. */
async function pipelineWorkflow(projectPath: string) {
  await ensureJobSchema();
  const query = `?project_directory=${encodeURIComponent(projectPath)}`;
  const graph = (await agentJson(`/api/project/graph${query}`)) as never;
  const layout = layoutPipeline(graph);
  const project = { path: projectPath, name: projectPath.split('/').pop() ?? projectPath };
  return workflowFromPipeline(project, graph, layout);
}

/** Read and parse a JSON request body. */
function readBody(request: Connect.IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk: Buffer) => chunks.push(chunk));
    request.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString() || '{}'));
      } catch {
        resolve({});
      }
    });
  });
}

/**
 * Queue the jobs a run intent selects, and return a run record the UI can
 * follow.
 *
 * One POST per job, because that is what `queue_system` accepts and what makes
 * the queue's own admission control do the work: a job that cannot start yet
 * waits there, not in this process.
 */
async function startRun(
  projectPath: string,
  inputs: Record<string, unknown>,
  parameters: Record<string, Record<string, unknown>> = {},
) {
  const intent = inputs as unknown as ExecutionIntent;
  const graph = (await agentJson(
    `/api/project/graph?project_directory=${encodeURIComponent(projectPath)}`,
  )) as {
    processes: Array<{ name: string; status?: string; type_label?: string }>;
  };

  const wanted = new Set(processesFor(intent, graph.processes));
  const entryIds: string[] = [];
  const failures: string[] = [];

  if (!wanted.size) {
    // Nothing to do, and saying "running" would be a lie the UI then waits on.
    failures.push(
      intent.selectedNodeId
        ? `${intent.selectedNodeId} is already finished or running`
        : 'no runnable jobs in this pipeline',
    );
    return { runId: runIdFor(projectPath, entryIds), entryIds, failures };
  }

  for (const process of graph.processes) {
    if (!wanted.has(process.name)) continue;
    const jobType = jobTypeForLabel(process.type_label ?? '');
    if (!jobType) {
      failures.push(`${process.name}: no job type for ${process.type_label}`);
      continue;
    }
    try {
      const entry = (await agentJson('/api/queue', {
        method: 'POST',
        body: queueBody(
          projectPath,
          {
            process: process.name,
            jobType,
            arguments: parameters[process.name] ?? {},
          },
          intent,
          [],
        ),
      })) as { id: string };
      entryIds.push(entry.id);
    } catch (cause) {
      failures.push(`${process.name}: ${cause instanceof Error ? cause.message : cause}`);
    }
  }

  return { runId: runIdFor(projectPath, entryIds), entryIds, failures };
}

export function bridge(): Connect.NextHandleFunction {
  return async (request, response, next) => {
    const url = request.url ?? '';
    const path = url.split('?')[0];
    const method = request.method ?? 'GET';
    const route = translate(method, path);
    if (!route) {
      next();
      return;
    }

    const send = (status: number, body: unknown) => {
      response.statusCode = status;
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify(body));
    };

    try {
      if (route.kind === 'bootstrap' || route.kind === 'projects') {
        const listing = (await agentJson('/api/projects')) as {
          projects: { path: string; name: string; added_at: number; readable?: boolean }[];
        };
        const projects = listing.projects
          .filter((entry) => entry.readable !== false)
          .map(projectFromRelion);

        // A workflow per open project, so the sidebar has something to show.
        const workflows = [];
        for (const project of projects) {
          try {
            workflows.push(await pipelineWorkflow(project.directory));
          } catch {
            // An unreadable project simply contributes no workflow; the
            // project row still appears so the user can see it failed.
          }
        }

        if (route.kind === 'projects') {
          send(200, projects);
        } else {
          send(200, { projects, sessions: [], workflows, runs: [] });
        }
        return;
      }

      if (route.kind === 'empty-list') {
        send(200, []);
        return;
      }

      if (route.kind === 'noop') {
        send(200, { initialized: false });
        return;
      }

      if (route.kind === 'model-settings') {
        // No model connection is configured; the UI reads this to decide
        // whether the assistant can run. An empty shape is the honest answer.
        send(200, { connections: [], entries: [], defaults: {} });
        return;
      }

      if (route.kind === 'tools') {
        // The MCP/tool inventory the settings page lists. Our agent has one, in
        // a different shape; reporting it is a later step.
        send(200, { tools: [] });
        return;
      }

      if (route.kind === 'events') {
        // A long-lived SSE stream with no events. Kept open (not ended) so the
        // client's reconnect loop stops, and so the shape matches what it
        // expects if we later forward RELION job changes through it.
        response.statusCode = 200;
        response.setHeader('Content-Type', 'text/event-stream');
        response.setHeader('Cache-Control', 'no-cache');
        response.setHeader('Connection', 'keep-alive');
        response.write(': connected\n\n');
        const keepAlive = setInterval(() => response.write(': keep-alive\n\n'), 30_000);
        request.on('close', () => clearInterval(keepAlive));
        return;
      }

      if (route.kind === 'workflow-list') {
        const listing = (await agentJson('/api/projects')) as {
          projects: { path: string; readable?: boolean }[];
        };
        const workflows = [];
        for (const entry of listing.projects) {
          if (entry.readable === false) continue;
          try {
            workflows.push(await pipelineWorkflow(entry.path));
          } catch {
            /* skip */
          }
        }
        send(200, workflows);
        return;
      }

      if (route.kind === 'workflow-run') {
        const projectPath = (route.id ?? '').replace(/^pipeline:/, '');
        const body = await readBody(request);
        const result = await startRun(
          projectPath,
          (body.inputs ?? {}) as Record<string, unknown>,
          // The node's edited parameters, keyed by process name. The run dialog
          // only collects the execution intent; the parameters live on the
          // canvas where the user edited them.
          (body.parameters ?? {}) as Record<string, Record<string, unknown>>,
        );
        await ensureJobSchema();
        const workflow = await pipelineWorkflow(projectPath);
        send(200, {
          id: result.runId,
          workflowId: workflow.id,
          status: result.failures.length && !result.entryIds.length ? 'failed' : 'running',
          inputs: body.inputs ?? {},
          outputs: result.entryIds.length ? { queued: result.entryIds } : null,
          error: result.failures.length ? result.failures.join('; ') : null,
          revision: workflow.revision,
          createdAt: new Date().toISOString(),
          sessionId: null,
        });
        return;
      }

      if (route.kind === 'run') {
        const parsed = parseRunId(route.id ?? '');
        if (!parsed) {
          send(404, { error: { code: 'not_found', message: 'unknown run' } });
          return;
        }
        const queue = (await agentJson('/api/queue')) as {
          entries: Array<{ id: string; status?: string; error?: string | null }>;
        };
        const mine = (queue.entries ?? []).filter((entry) =>
          parsed.entries.includes(entry.id),
        );
        const failed = mine.filter((entry) => entry.status === 'failed');
        const done = mine.filter((entry) =>
          ['done', 'cancelled'].includes(entry.status ?? ''),
        );
        send(200, {
          id: route.id,
          status: failed.length ? 'failed' : done.length === mine.length ? 'succeeded' : 'running',
          outputs: { entries: mine },
          error: failed[0]?.error ?? null,
        });
        return;
      }

      if (route.kind === 'workflow') {
        // `id` is `pipeline:<project path>`, assigned in workflowFromPipeline.
        const projectPath = (route.id ?? '').replace(/^pipeline:/, '');
        send(200, await pipelineWorkflow(projectPath));
        return;
      }

      next();
    } catch (cause) {
      send(502, {
        error: {
          code: 'agent_unreachable',
          message: cause instanceof Error ? cause.message : String(cause),
        },
      });
    }
  };
}
