/**
 * The bridge: FlowGram's API shape, answered from the RELION agent.
 *
 * The vendored UI calls FlowGram's own backend (`/api/bootstrap`,
 * `/api/workflows/{id}`, `/api/runs`, ...). Those endpoints do not exist in the
 * RELION agent, and adding them there would mean the agent serving two
 * different API vocabularies.
 *
 * So this module sits in front of the agent and translates. It is the only
 * place that knows both shapes; nothing in the vendored UI is touched, and
 * nothing in the agent is touched.
 *
 * Runs as Vite middleware in development and as a tiny Node server in
 * production (same file), so the behaviour cannot drift between the two.
 */

/**
 * RELION's process status -> FlowGram's node status vocabulary.
 *
 * FlowGram's run-follow camera keys off `node_started|node_finished|
 * node_failed|node_canceled`; the canvas colours nodes from `statuses`.
 */
export const STATUS = {
  Succeeded: 'succeeded',
  Running: 'running',
  Failed: 'failed',
  Aborted: 'canceled',
  Scheduled: 'pending',
};

/**
 * A RELION project as a FlowGram project.
 *
 * FlowGram's `Project` is `{id, name, createdAt, directory}`. The id has to be
 * stable across reloads, and the path is the only stable identity a RELION
 * project has, so it is the id.
 */
export function projectFromRelion(entry) {
  return {
    id: entry.path,
    name: entry.name,
    createdAt: new Date((entry.added_at ?? 0) * 1000).toISOString(),
    directory: entry.path,
  };
}

/**
 * A RELION pipeline as a FlowGram workflow.
 *
 * One workflow per project, named "Pipeline". Its schema is regenerated from
 * `/api/project/graph` on every read, so the pipeline file stays the single
 * source of truth and the canvas cannot drift from it.
 */
export function workflowFromPipeline(project, graph, layout) {
  const byName = new Map(graph.processes.map((p) => [p.name, p]));

  const nodes = layout.nodes.map((box) => {
    const process = byName.get(box.primary);
    const alias = process?.alias;
    const label = alias && alias !== 'None' ? alias : (process?.type_label ?? box.primary);
    const extra = box.processes.length > 1 ? ` (+${box.processes.length - 1})` : '';
    return {
      id: box.primary,
      // `assign` exists in FlowGram's catalogue but is hidden from the palette,
      // so a RELION job renders with the stock node component while the "add
      // node" panel keeps offering FlowGram's own node types. No custom node
      // component, which is the constraint for this branch.
      type: 'assign',
      meta: { position: { x: box.x, y: box.y } },
      data: {
        title: `${label}${extra}`,
        status: STATUS[process?.status] ?? 'pending',
        relion: {
          process: box.primary,
          kind: process?.type_label ?? '',
          pipelineStatus: process?.status ?? 'Scheduled',
          jobNumber: process?.job_number ?? null,
        },
      },
    };
  });

  const edges = layout.edges.map((edge, index) => ({
    id: `e${index}`,
    sourceNodeID: edge.from,
    targetNodeID: edge.to,
  }));

  return {
    id: `pipeline:${project.path}`,
    projectId: project.path,
    name: 'Pipeline',
    revision: 1,
    schema: { nodes, edges },
    updatedAt: new Date().toISOString(),
  };
}

/**
 * Rewrite a path to its RELION-agent equivalent, or return null to pass it
 * through untouched.
 *
 * Everything not listed here falls through to the agent verbatim. That is
 * deliberate: when FlowGram asks for something we have no opinion about, the
 * agent's own 404 is the honest answer.
 */
export function translate(method, path) {
  if (path === '/api/bootstrap') return { kind: 'bootstrap' };
  if (path === '/api/projects' && method === 'GET') return { kind: 'projects' };
  // The app calls this on boot to seed demo content. RELION projects are
  // already on disk, so there is nothing to seed - but the call must succeed
  // or the boot sequence aborts before it ever reads the project list.
  if (path === '/api/projects/initialize') return { kind: 'noop' };
  if (path === '/api/settings/model') return { kind: 'model-settings' };
  if (path === '/api/tools') return { kind: 'tools' };
  // Server-sent events. The agent pushes job state over its own WebSocket, so
  // this stream stays open and silent; a 404 here makes the client retry in a
  // tight loop and the boot sequence never completes.
  if (path === '/api/events') return { kind: 'events' };
  if (path === '/api/sessions') return { kind: 'empty-list' };
  if (path === '/api/runs') return { kind: 'empty-list' };

  const workflow = /^\/api\/workflows\/(.+)$/.exec(path);
  if (workflow && method === 'GET') {
    return { kind: 'workflow', id: decodeURIComponent(workflow[1]) };
  }
  if (path === '/api/workflows') return { kind: 'workflow-list' };

  return null;
}
