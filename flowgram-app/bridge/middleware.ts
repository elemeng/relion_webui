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
  mergeWorkspace,
  workflowFromPipeline,
} from './translate';
import { agentJson } from './agent';
import {
  allRuns,
  eventsAfter,
  getRun,
  openRun,
  recordCancel,
  runsForProject,
  startJournal,
} from './journal';
import { layoutPipeline } from '../../frontend/src/lib/layout';
import {
  archiveWorkspace,
  createWorkspace,
  ensureDefaultWorkspace,
  getWorkspace,
  listWorkspaces,
  promoteDraft,
  saveWorkspace,
} from './workspaces';

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

/**
 * A canvas parameter value as the queue expects it.
 *
 * The inspector writes RELION's own binding shape (`{type, content}`), and a
 * value may be a reference to an upstream node's declared output
 * (`["AlignTiltSeries/job002", "tilt_series.star"]`). The queue wants plain
 * option values: constants unwrap to their content, references resolve to the
 * project path the pipeline guarantees (`<process>/<filename>`), and
 * everything else passes through for RELION to reject with its own message.
 */
function unwrapArgument(
  value: unknown,
  all: Record<string, Record<string, unknown>>,
  depth = 0,
): unknown {
  if (!value || typeof value !== 'object') return value;
  const binding = value as { type?: string; content?: unknown };
  if (binding.type === 'constant') return binding.content;
  if (binding.type === 'ref' && Array.isArray(binding.content)) {
    const [nodeId, ...path] = binding.content as string[];
    if (nodeId && path.length === 1) {
      // `<process>/<filename>` - the .Nodes entry the queue reserved at
      // enqueue time, so the path is valid whether or not the file exists.
      const processName = nodeId.replace(/^pipeline:/, '');
      if (processName.includes('/')) return `${processName}/${path[0]}`;
    }
    if (nodeId && path.length >= 1 && depth < 4) {
      // Chained references: a ref whose leaf is another node's input field
      // resolves to *that field's* value, recursively - an input handed down
      // a chain of jobs keeps pointing at the file at the head of the chain.
      const upstream = all[nodeId]?.[path[0]];
      if (upstream && typeof upstream === 'object' && path.length === 1) {
        const resolved = unwrapArgument(upstream, all, depth + 1);
        if (typeof resolved === 'string') return resolved;
      }
    }
    return binding.content;
  }
  return value;
}

function resolveArguments(
  options: Record<string, unknown>,
  all: Record<string, Record<string, unknown>>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(options).map(([key, value]) => [
      key,
      unwrapArgument(value, all),
    ]),
  );
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
  return workflowFromPipeline(project, graph, layout, graph);
}

/**
 * One workspace as the UI sees it: the project's pipeline plus the overlay.
 *
 * Every read re-projects the pipeline, so a job queued from the desktop GUI or
 * the main web UI shows up here without a refresh of anything stored.
 */
async function workspaceWorkflow(projectPath: string, workspaceId?: string) {
  const projection = await pipelineWorkflow(projectPath);
  const workspace = workspaceId
    ? await getWorkspace(workspaceId)
    : await ensureDefaultWorkspace(projectPath);
  if (!workspace) return projection;
  return mergeWorkspace(projection as never, workspace as never);
}

/** The workspace a workflow id names, or the project's default. */
async function resolveWorkspace(id: string | undefined, fallbackProject?: string) {
  if (id && !id.startsWith('pipeline:')) {
    const workspace = await getWorkspace(id);
    if (workspace) return { workspace, projectId: workspace.projectId };
  }
  // `pipeline:<path>` is what an older client (and the projection itself) uses.
  const projectId = (id ?? fallbackProject ?? '').replace(/^pipeline:/, '');
  if (!projectId) return null;
  return { workspace: await ensureDefaultWorkspace(projectId), projectId };
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
  workspaceId?: string,
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

  const workspace = workspaceId ? await getWorkspace(workspaceId) : null;
  // Draft nodes are jobs that do not exist in the pipeline yet. They take part
  // in `all`/selected/job runs exactly like a process: selecting one queues it,
  // which is what promotes it.
  const draftTypes = new Map(
    (workspace?.nodes ?? []).map((node) => [node.id, node.type] as const),
  );

  const runnable = [
    ...graph.processes.map((process) => ({
      name: process.name,
      typeLabel: process.type_label ?? '',
      draft: false,
      status: process.status,
    })),
    ...[...draftTypes].map(([id, type]) => ({
      name: id,
      typeLabel: `relion.${type.replace(/^relion\./, '')}`,
      draft: true,
      status: 'Scheduled',
    })),
  ];

  const wanted = new Set(processesFor(intent, runnable));
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

  for (const item of runnable) {
    if (!wanted.has(item.name)) continue;
    const jobType = item.draft
      ? item.typeLabel.replace(/^relion\./, '')
      : jobTypeForLabel(item.typeLabel);
    if (!jobType) {
      failures.push(`${item.name}: no job type for ${item.typeLabel}`);
      continue;
    }
    try {
      const entry = (await agentJson('/api/queue', {
        method: 'POST',
        body: queueBody(
          projectPath,
          {
            process: item.name,
            jobType,
            arguments: resolveArguments(
              parameters[item.name] ?? {},
              parameters as never,
            ),
          },
          intent,
          [],
        ),
      })) as { id: string; process_name?: string };
      entryIds.push(entry.id);
      // A promoted draft becomes a pipeline process: the overlay drops it and
      // re-points its edges at the reserved name, so the next read shows one
      // node where the user drew one.
      if (item.draft && workspaceId && entry.process_name) {
        await promoteDraft(workspaceId, item.name, entry.process_name).catch(() => {});
      }
    } catch (cause) {
      failures.push(`${item.name}: ${cause instanceof Error ? cause.message : cause}`);
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

        // Every workspace of every open project: a workspace is the soft
        // grouping over one project's pipeline (see bridge/workspaces.ts), so
        // one project can carry several canvases without touching its files.
        const workflows = [];
        for (const project of projects) {
          try {
            const listed = await listWorkspaces(project.directory);
            if (!listed.length) {
              // A project nobody has opened yet gets its default workspace, so
              // the sidebar always offers a canvas for every project.
              workflows.push(await workspaceWorkflow(project.directory));
              continue;
            }
            for (const workspace of listed) {
              workflows.push(await workspaceWorkflow(project.directory, workspace.id));
            }
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
            const listed = await listWorkspaces(entry.path);
            if (!listed.length) {
              workflows.push(await workspaceWorkflow(entry.path));
              continue;
            }
            for (const workspace of listed)
              workflows.push(await workspaceWorkflow(entry.path, workspace.id));
          } catch {
            /* skip */
          }
        }
        send(200, workflows);
        return;
      }

      if (route.kind === 'workflow-create') {
        const body = await readBody(request);
        const projectId = String(body.projectId ?? '');
        const workspace = await createWorkspace(projectId, String(body.name ?? ''));
        send(201, await workspaceWorkflow(projectId, workspace.id));
        return;
      }

      if (route.kind === 'workflow-save') {
        const body = await readBody(request);
        const saved = await saveWorkspace(route.id ?? '', {
          name: body.name === undefined ? undefined : String(body.name),
          schema: body.schema as never,
        });
        if (!saved) {
          send(404, { error: { code: 'not_found', message: 'unknown workspace' } });
          return;
        }
        // The overlay is saved; the response is the merged view, so the canvas
        // gets the authoritative revision and every node in one read.
        send(200, await workspaceWorkflow(saved.projectId, saved.id));
        return;
      }

      if (route.kind === 'workflow-archive') {
        const archived = await archiveWorkspace(route.id ?? '');
        if (!archived) {
          send(404, { error: { code: 'not_found', message: 'unknown workspace' } });
          return;
        }
        send(200, { id: route.id, archived: true });
        return;
      }

      if (route.kind === 'workflow-validate') {
        // Validation is RELION's own, run against the draft jobs' parameters.
        // A workspace has no job of its own yet, so each draft node is checked
        // with its job type and edited arguments.
        const workspace = await getWorkspace(route.id ?? '');
        if (!workspace) {
          send(404, { error: { code: 'not_found', message: 'unknown workspace' } });
          return;
        }
        const problems: string[] = [];
        for (const node of workspace.nodes) {
          const jobType = node.type.replace(/^relion\./, '');
          if (!jobType || jobType === node.type) continue;
          const values = (node.data?.inputsValues ?? {}) as Record<string, unknown>;
          const args = Object.fromEntries(
            Object.entries(values)
              .filter(([, binding]) => (binding as { type?: string }).type === 'constant')
              .map(([key, binding]) => [key, (binding as { content?: unknown }).content]),
          );
          try {
            const result = (await agentJson('/api/job-validate', {
              method: 'POST',
              body: { job_type: jobType, arguments: args, project_directory: workspace.projectId },
            })) as { problems?: string[]; warnings?: string[] };
            for (const problem of result.problems ?? []) problems.push(`${node.id}: ${problem}`);
            for (const warning of result.warnings ?? []) problems.push(`${node.id}: ${warning}`);
          } catch (cause) {
            problems.push(`${node.id}: ${cause instanceof Error ? cause.message : cause}`);
          }
        }
        send(200, { valid: !problems.length, errors: problems });
        return;
      }

      if (route.kind === 'workflow-run') {
        const resolved = await resolveWorkspace(route.id);
        if (!resolved) {
          send(404, { error: { code: 'not_found', message: 'unknown workflow' } });
          return;
        }
        const projectPath = resolved.projectId;
        const body = await readBody(request);
        const result = await startRun(
          projectPath,
          (body.inputs ?? {}) as Record<string, unknown>,
          // The node's edited parameters, keyed by process name. The run dialog
          // only collects the execution intent; the parameters live on the
          // canvas where the user edited them.
          (body.parameters ?? {}) as Record<string, Record<string, unknown>>,
          resolved.workspace.id,
        );
        await ensureJobSchema();
        const workflow = await workspaceWorkflow(projectPath, resolved.workspace.id);
        const status =
          result.failures.length && !result.entryIds.length ? 'failed' : 'running';
        const error = result.failures.length ? result.failures.join('; ') : null;
        if (result.entryIds.length) {
          // The journal watches by project, so the id it derives must be the
          // same one the UI just received - otherwise its first tick would
          // open a second, anonymous journal for the same jobs.
          openRun(
            runIdFor(projectPath, result.entryIds),
            projectPath,
            (body.inputs ?? {}) as Record<string, unknown>,
            {
              workflowId: workflow.id,
              entries: result.entryIds,
              revision: workflow.revision,
            },
          );
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

      if (route.kind === 'run-list') {
        // The run list is the journal's own record: runs this bridge submitted,
        // plus jobs queued elsewhere that it watched. Shape matches what the
        // vendored list expects (`{runs, total, next, workflows, environments}`).
        const runs = allRuns();
        send(200, {
          runs: runs.map((run) => ({
            id: run.id,
            workflowId: run.workflowId,
            workflowName: run.workflowName,
            revision: run.revision,
            testNodeId: null,
            status: run.status,
            source: 'manual',
            createdAt: run.createdAt,
            endedAt: run.status === 'running' ? null : run.createdAt,
            durationMs: null,
            error: run.error,
            callbackStatus: null,
            callbackAttempts: null,
            callbackError: null,
            environment: null,
            releaseVersion: null,
            operation: null,
            requestId: null,
            businessId: run.id,
          })),
          total: runs.length,
          next: null,
          workflows: [...new Set(runs.map((run) => run.workflowId))].map((id) => ({
            id,
            name: id.replace(/^pipeline:/, '').split('/').pop() ?? 'Pipeline',
          })),
          environments: [],
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
        const resolved = await resolveWorkspace(route.id);
        if (!resolved) {
          send(404, { error: { code: 'not_found', message: 'unknown workflow' } });
          return;
        }
        send(200, await workspaceWorkflow(resolved.projectId, resolved.workspace.id));
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
