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
export /**
 * The execution intent, declared as a workflow-input schema.
 *
 * FlowGram renders a run dialog from the start node's `outputs`, so these two
 * fields arrive as ordinary form controls. That keeps the *semantics* ours
 * while the *surface* stays FlowGram's:
 *
 *   scope  which jobs to start - the whole pipeline, the selected node, or one
 *   gpu    let the queue pick a free card, or pin one
 *
 * `relionSelected` is carried alongside because "selected" is a canvas state,
 * not a dropdown choice.
 */
const EXECUTION_NODE_ID = 'relion.execution';

const EXECUTION_SCHEMA = {
  type: 'object',
  properties: {
    scope: {
      type: 'string',
      title: 'Run',
      enum: ['all', 'selected', 'job'],
      enumNames: ['Whole pipeline', 'Selected node', 'Single job'],
      default: 'all',
    },
    gpu: {
      type: 'string',
      title: 'GPU',
      enum: ['auto', 'specific'],
      enumNames: ['Any free card', 'Specific card'],
      default: 'auto',
    },
    gpuIndex: {
      type: 'integer',
      title: 'GPU index',
      description: 'Used when GPU is "specific".',
      default: 0,
    },
    exclusive: {
      type: 'boolean',
      title: 'Whole node',
      description: 'While this runs, nothing else starts on the node.',
      default: false,
    },
  },
  required: ['scope', 'gpu'],
};

const STATUS: Record<string, string> = {
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
export function projectFromRelion(entry: {
  path: string;
  name: string;
  added_at?: number;
}) {
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
export function workflowFromPipeline(
  project: { path: string; name: string },
  graph: {
    processes: Array<{
      name: string;
      alias?: string;
      type_label?: string;
      status?: string;
      job_number?: number;
    }>;
  },
  layout: {
    nodes: Array<{
      primary: string;
      processes: string[];
      x: number;
      y: number;
    }>;
    edges: Array<{ from: string; to: string }>;
  },
) {
  const byName = new Map(graph.processes.map((p) => [p.name, p]));

  const nodes = layout.nodes.map((box) => {
    const process = byName.get(box.primary);
    const alias = process?.alias;
    const label = alias && alias !== 'None' ? alias : (process?.type_label ?? box.primary);
    const extra = box.processes.length > 1 ? ` (+${box.processes.length - 1})` : '';
    return {
      id: box.primary,
      // One node type per RELION job type (`relion.tomo_import`, ...). The
      // definitions are generated from `/api/job-schema` at boot and registered
      // with the stock inspector, so each job's real parameters render in
      // FlowGram's own form. A shared `assign` node would have shown a generic
      // mapping row instead of the job's fields.
      type: jobNodeType(process?.type_label ?? ''),
      meta: { position: { x: box.x, y: box.y } },
      data: {
        title: `${label}${extra}`,
        // `inputsValues` is where FlowGram keeps a node's parameters. These
        // start at the schema defaults; editing them in the inspector writes
        // here, and the submit path reads them back out.
        inputsValues: defaultValues(process?.type_label ?? ''),
        status: STATUS[process?.status ?? ''] ?? 'pending',
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

  // Wire the pipeline's roots to a start node. Its `outputs` schema is what
  // FlowGram's own run dialog renders as a form, so the execution intent -
  // what to queue and which GPU - is expressed in stock UI rather than a
  // bespoke toolbar.
  const hasPred = new Set(edges.map((edge) => edge.targetNodeID));
  const roots = nodes.filter((node) => !hasPred.has(node.id)).map((node) => node.id);
  const first = layout.nodes[0];
  const startNode = {
    id: EXECUTION_NODE_ID,
    type: 'start',
    meta: {
      position: {
        x: (first?.x ?? 0) - 320,
        y: first?.y ?? 0,
      },
    },
    data: {
      title: 'Execution',
      outputs: EXECUTION_SCHEMA,
    },
  };
  for (const [index, root] of roots.entries()) {
    edges.push({
      id: `e-start-${index}`,
      sourceNodeID: EXECUTION_NODE_ID,
      targetNodeID: root,
    });
  }

  return {
    id: `pipeline:${project.path}`,
    projectId: project.path,
    name: 'Pipeline',
    revision: 1,
    schema: { nodes: [startNode, ...nodes], edges },
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
export function translate(method: string, path: string): { kind: string; id?: string } | null {
  if (path === '/api/bootstrap') return { kind: 'bootstrap' };
  if (path === '/api/projects' && method === 'GET') return { kind: 'projects' };
  // The app calls this on boot to seed demo content. RELION projects are
  // already on disk, so there is nothing to seed - but the call must succeed
  // or the boot sequence aborts before it ever reads the project list.
  if (path === '/api/projects/initialize') return { kind: 'noop' };
  if (path === '/api/settings/model') return { kind: 'model-settings' };
  if (path === '/api/tools') return { kind: 'tools' };
  // Server-sent events from the journal (see bridge/journal.ts).
  if (path === '/api/events') return { kind: 'events' };
  if (path === '/api/sessions') return { kind: 'empty-list' };
  if (path === '/api/runs') return { kind: 'run-list' };

  const workflowRun = /^\/api\/workflows\/(.+)\/run$/.exec(path);
  if (workflowRun && method === 'POST') {
    return { kind: 'workflow-run', id: decodeURIComponent(workflowRun[1] ?? '') };
  }
  const workflow = /^\/api\/workflows\/(.+)$/.exec(path);
  if (workflow && method === 'GET') {
    return { kind: 'workflow', id: decodeURIComponent(workflow[1] ?? '') };
  }
  const runCancel = /^\/api\/runs\/(.+)\/cancel$/.exec(path);
  if (runCancel && method === 'POST') {
    return { kind: 'run-cancel', id: decodeURIComponent(runCancel[1] ?? '') };
  }
  const run = /^\/api\/runs\/(.+)$/.exec(path);
  if (run && method === 'GET') {
    return { kind: 'run', id: decodeURIComponent(run[1] ?? '') };
  }
  if (path === '/api/workflows') return { kind: 'workflow-list' };

  return null;
}

/* ------------------------------------------------------------------ schema */

/**
 * RELION job types, cached for the projection.
 *
 * `/api/job-schema` is the authority on which types exist and what their
 * defaults are. The bridge fetches it once per boot; `workflowFromPipeline` is
 * synchronous, so the values have to already be here.
 */
interface JobSchemaEntry {
  job_type: string;
  fields: Array<{ variable: string; default?: unknown }>;
}

let jobSchema: { jobs: JobSchemaEntry[] } | null = null;

export function rememberJobSchema(schema: { jobs: JobSchemaEntry[] }): void {
  jobSchema = schema;
}

/**
 * Node type id for a pipeline label.
 *
 * Uses the resolved schema job type, so the inspector's registered definition
 * and the node on the canvas always agree. An unknown label keeps its own
 * name, which renders a node with no fields rather than crashing.
 */
export function jobNodeType(typeLabel: string): string {
  return `relion.${jobTypeForLabel(typeLabel) ?? typeLabel.replace(/^relion\./, '')}`;
}

/**
 * A node's starting parameters: the schema defaults, in FlowGram's binding
 * shape. An unknown type yields no bindings rather than throwing - a job kind
 * the schema does not list still has to render.
 */
export function defaultValues(typeLabel: string): Record<string, unknown> {
  const job = jobForLabel(typeLabel);
  if (!job) return {};
  return Object.fromEntries(
    job.fields.map((field) => [
      field.variable,
      { type: 'constant', content: field.default ?? '' },
    ]),
  );
}

/**
 * `type_label` -> schema `job_type`.
 *
 * Two vocabularies meet here. The pipeline records `relion.importtomo`; the
 * job schema keys on `tomo_import_serialem`. `/api/project/job-types` carries
 * both, so it is the authority - guessing from names fails (`relion.importtomo`
 * has no `cpp_name` that matches, and one label can map to several schema
 * entries differing only by subcommand).
 *
 * When several schema entries share a label, the first wins: it is the default
 * variant, and the user can still switch inside the form.
 */
let labelToJobType: Map<string, string> | null = null;

export function rememberJobTypes(entries: Array<{ id: string; process_type_label?: string }>): void {
  // Prefer ids the schema actually has a row for. The job-types table also
  // carries abstract ids (`tomo_align_tilt_series`, `tomo_denoise_tomograms`)
  // that the schema splits into concrete variants; picking one of those would
  // leave the node with no form.
  const known = new Set(jobSchema?.jobs.map((job) => job.job_type) ?? []);
  const map = new Map<string, string>();
  const skipped: Array<[string, string]> = [];

  for (const entry of entries) {
    const label = entry.process_type_label;
    if (!label) continue;
    if (known.size && !known.has(entry.id)) {
      skipped.push([label, entry.id]);
      continue;
    }
    if (!map.has(label)) map.set(label, entry.id);
  }

  // A label whose only ids are abstract still needs an answer: keep the first
  // one so the family fallback in `jobForLabel` can find its variants.
  for (const [label, id] of skipped) {
    if (!map.has(label)) map.set(label, id);
  }

  labelToJobType = map;
}

/** The schema job type for a pipeline label, or null when unknown. */
export function jobTypeForLabel(typeLabel: string): string | null {
  return labelToJobType?.get(typeLabel) ?? null;
}

/**
 * The schema entry for a pipeline label.
 *
 * The two tables disagree on granularity: `/api/project/job-types` lists one
 * entry per *job* (`tomo_import`), while the schema splits that into variants
 * (`tomo_import_serialem`, `tomo_import_coordinates`). The first variant is the
 * default one, which is what a node shows until the user picks otherwise.
 */
function jobForLabel(typeLabel: string) {
  const jobType = jobTypeForLabel(typeLabel);
  if (!jobType) return undefined;

  // Exact match, then a variant of the same job.
  const exact =
    jobSchema?.jobs.find((candidate) => candidate.job_type === jobType) ??
    jobSchema?.jobs.find((candidate) => candidate.job_type.startsWith(`${jobType}_`));
  if (exact) return exact;

  // `/api/project/job-types` also lists abstract entries that no schema row
  // carries: `tomo_align_tilt_series` is one id covering three concrete
  // variants (`_imod_fiducials`, `_imod_patchtrack`, `_aretomo`). Fall back to
  // the first member of the family so the node has a starting form; the user
  // picks the real variant in the inspector.
  const family = jobType.split('_').slice(0, 3).join('_');
  return jobSchema?.jobs.find((candidate) => candidate.job_type.startsWith(`${family}_`));
}
