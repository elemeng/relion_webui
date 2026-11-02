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
import { agentJson } from './agent';
import {
  eventsAfter,
  getRun,
  openRun,
  recordCancel,
  runsForProject,
  startJournal,
} from './journal';
import { layoutPipeline } from '../../frontend/src/lib/layout';

const AGENT = process.env.RELION_WEB_API ?? 'http://127.0.0.1:8765';

// Queue states that end an entry - mirrored from queue_system.TERMINAL_STATES.
const TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled']);
// Queue state -> FlowGram node-state vocabulary, matching journal's stateEvent.
const FLOWGRAM_STATE: Record<string, string> = {
  pending: 'waiting',
  running: 'running',
  succeeded: 'succeeded',
  failed: 'failed',
  cancelled: 'canceled',
};

/** Run records the journal knows, filtered to still-listed projects. */
function currentRuns(projects: Array<{ path: string }>): unknown[] {
  const known = new Set(projects.map((project) => project.path));
  const seen = new Set<string>();
  const out: unknown[] = [];
  for (const project of projects) {
    for (const run of runsForProject(project.path)) {
      if (seen.has(run.id)) continue;
      seen.add(run.id);
      out.push(run);
    }
  }
  void known;
  return out;
}

function safeParse(raw: string): Record<string, unknown> {
  try {
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return {};
  }
}

// Start the journal's tick loop; the middleware is instantiated once per dev
// server, so this runs exactly once.
startJournal();

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
  // The label -> job-type table is a boot-time cache; a cold run must have
  // loaded it before mapping, or every job reports "no job type".
  await ensureJobSchema();
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
          .map(projectFromRelion)
          .map((project) => ({ ...project, path: project.directory }));

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
          // A run per project the journal knows about, so the app restores the
          // canvas state after a reload instead of losing the run pill.
          send(200, { projects, sessions: [], workflows, runs: currentRuns(projects) });
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
        // The journal's SSE. The client passes ?after=<sequence> on reconnect;
        // anything beyond that point is replayed, then live events are pushed
        // as the tick writes them. A 404 here makes the client retry in a
        // tight loop and the boot sequence never completes.
        response.statusCode = 200;
        response.setHeader('Content-Type', 'text/event-stream');
        response.setHeader('Cache-Control', 'no-cache');
        response.setHeader('Connection', 'keep-alive');
        const params = new URLSearchParams(request.url?.split('?')[1] ?? '');
        const objectId = params.get('objectId') ?? undefined;
        let after = Number(params.get('after') ?? 0) || 0;
        let closed = false;
        response.write(': connected\n\n');
        const push = () => {
          if (closed) return;
          const latest = eventsAfter(after, objectId);
          for (const item of latest) {
            response.write(`id: ${item.sequence}\ndata: ${JSON.stringify(item)}\n\n`);
          }
          if (latest.length) after = latest[latest.length - 1]!.sequence;
        };
        push();
        // The journal writes only when its tick runs, so draining on the same
        // cadence delivers live events without a push hook through the module
        // boundary.
        const drain = setInterval(push, 1_000);
        const keepAlive = setInterval(() => {
          push();
          response.write(': keep-alive\n\n');
        }, 30_000);
        request.on('close', () => {
          closed = true;
          clearInterval(drain);
          clearInterval(keepAlive);
        });
        return;
      }

      if (route.kind === 'run-cancel') {
        const journalRun = getRun(route.id ?? '');
        if (!journalRun) {
          send(404, { error: { code: 'not_found', message: 'unknown run' } });
          return;
        }
        const cancelled = [...journalRun.entries.entries()]
          .filter(([, entry]) => !TERMINAL_STATES.has(entry.state))
          .map(([id]) => id);
        for (const entryId of cancelled) {
          await agentJson(`/api/queue/${encodeURIComponent(entryId)}`, { method: 'DELETE' }).catch(() => {});
        }
        recordCancel(route.id!, cancelled);
        send(200, { id: route.id, cancelled });
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
        const status =
          result.failures.length && !result.entryIds.length ? 'failed' : 'running';
        const error = result.failures.length ? result.failures.join('; ') : null;
        if (result.entryIds.length) {
          // The journal watches by project, so the id it derives must be the
          // same one the UI just received - otherwise its first tick would
          // open a second, anonymous journal for the same jobs.
          openRun(runIdFor(projectPath, result.entryIds), projectPath, (body.inputs ?? {}) as Record<string, unknown>);
        }
        send(200, {
          id: result.runId,
          workflowId: workflow.id,
          status,
          inputs: body.inputs ?? {},
          outputs: result.entryIds.length ? { queued: result.entryIds } : null,
          error,
          revision: workflow.revision,
          createdAt: new Date().toISOString(),
          sessionId: null,
        });
        return;
      }

      if (route.kind === 'run') {
        const journalRun = getRun(route.id ?? '');
        if (!journalRun) {
          send(404, { error: { code: 'not_found', message: 'unknown run' } });
          return;
        }
        const params = new URLSearchParams(request.url?.split('?')[1] ?? '');
        const view = params.get('view');
        const events = journalRun.events;

        if (view === 'overview') {
          // RunInspector's `useRunInspection` polls this while running and
          // builds its trace tree from `trace.nodes` + executions.
          const executions = [...journalRun.entries.entries()].map(([id, entry]) => ({
            id,
            parentId: null,
            parentPath: [] as number[],
            nodeId: entry.process_name ?? id,
            workflowId: journalRun.run.workflowId,
            scopeId: '',
            kind: 'node' as const,
            name: entry.process_name ?? entry.job_type,
            status: FLOWGRAM_STATE[entry.state] ?? 'running',
            firstSequence: 0,
            lastSequence: events.length,
            startedAt: entry.started_at
              ? new Date(entry.started_at * 1000).toISOString()
              : journalRun.run.createdAt,
            endedAt: entry.finished_at ? new Date(entry.finished_at * 1000).toISOString() : null,
            durationMs:
              entry.started_at && entry.finished_at
                ? Math.max(0, (entry.finished_at - entry.started_at) * 1000)
                : null,
            branchPath: [] as string[],
            iterationPath: [] as number[],
            attempt: 1,
            reason: entry.error,
            children: 0,
            inputsAvailable: false,
            outputsAvailable: entry.state === 'succeeded',
            links: [],
          }));
          const nodes = [...journalRun.entries.values()].map((entry) => ({
            workflowId: journalRun.run.workflowId,
            nodeId: entry.process_name ?? entry.id,
            total: 1,
            durationMs:
              entry.started_at && entry.finished_at
                ? Math.max(0, (entry.finished_at - entry.started_at) * 1000)
                : null,
            startedAt: entry.started_at
              ? new Date(entry.started_at * 1000).toISOString()
              : null,
            counts: { [FLOWGRAM_STATE[entry.state] ?? 'running']: 1 },
          }));
          send(200, {
            run: journalRun.run,
            snapshot: null,
            models: [],
            nodeTest: null,
            trace: {
              executions,
              nextCursor: null,
              total: executions.length,
              at: null,
              observedAt: new Date().toISOString(),
              outputsAvailable: executions.some((e) => e.outputsAvailable),
              origin: null,
              format: 'journal',
              runtime: 'relion-queue',
              source: 'manual',
              endedAt: journalRun.finished ? new Date().toISOString() : null,
              durationMs: null,
              watermark: events[events.length - 1]?.sequence ?? 0,
              rootTotal: executions.length,
              counts: {},
              failureId:
                executions.find((e) => e.status === 'failed')?.id ?? null,
              failurePath: [],
              focus: executions.filter((e) => e.status === 'failed'),
              links: [],
              nodes,
              publication: null,
            },
          });
          return;
        }

        if (view === 'execution') {
          const executionId = params.get('execution');
          const entry = executionId ? journalRun.entries.get(executionId) : null;
          if (!entry) {
            send(404, { error: { code: 'not_found', message: 'unknown execution' } });
            return;
          }
          send(200, {
            events: events.filter((item) => {
              const data = safeParse(item.data);
              return data.executionId === executionId && item.kind !== 'log_appended'
                ? true
                : item.kind === 'log_appended' && data.nodeId === (entry.process_name ?? entry.id);
            }),
            nextAfter: null,
          });
          return;
        }

        // Default shape: what workflow-panel needs - { run, events, nodeStates }.
        const nodeStates: Record<string, string> = {};
        for (const entry of journalRun.entries.values()) {
          nodeStates[entry.process_name ?? entry.id] = FLOWGRAM_STATE[entry.state] ?? 'running';
        }
        send(200, {
          run: journalRun.run,
          events,
          nodeStates,
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
