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
  /**
   * The pipeline's data nodes (`Import/job001/tilt_series.star`) and who
   * consumes them. This is what lets a projection pre-fill an input_node
   * field with the exact upstream path the pipeline edge already declares,
   * instead of leaving every input empty until the user points it somewhere.
   */
  inputs?: {
    nodes: Array<{ name: string; type_label: string; produced_by?: string }>;
    edges?: { inputs?: Array<{ node: string; process: string }> };
  },
) {
  const byName = new Map(graph.processes.map((p) => [p.name, p]));

  // node name -> the process that writes it (per the pipeline's own record).
  const producerOf = new Map((inputs?.nodes ?? []).map((node) => [node.name, node]));
  // consumer process -> the pipeline nodes it names as inputs.
  const consumedBy = new Map<string, string[]>();
  for (const edge of inputs?.edges?.inputs ?? []) {
    const list = consumedBy.get(edge.process) ?? [];
    list.push(edge.node);
    consumedBy.set(edge.process, list);
  }

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
        // here, and the submit path reads them back out. Input slots the
        // pipeline edge already names start at that exact path - the user can
        // change it, but "which file does this consume" is not a guess.
        inputsValues: filledValues(
          process?.type_label ?? '',
          consumedBy.get(box.primary) ?? [],
          producerOf,
        ),
        status: STATUS[process?.status ?? ''] ?? 'pending',
        relion: {
          process: box.primary,
          kind: process?.type_label ?? '',
          pipelineStatus: process?.status ?? 'Scheduled',
          jobNumber: process?.job_number ?? null,
        },
        // The files this job writes (`<process>/<filename>` per the job spec).
        // Declaring them as the node's outputs is what makes the variable
        // picker offer real upstream files, so an input field can point at
        // "AlignTiltSeries/job002 / tilt_series.star" instead of free text.
        outputs: {
          type: 'object',
          properties: Object.fromEntries(
            labelOutputs(process?.type_label ?? '').map((filename) => [
              filename,
              { type: 'string' },
            ]),
          ),
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
  const workflowSave = /^\/api\/workflows\/(.+)$/.exec(path);
  if (workflowSave && method === 'PUT') {
    return { kind: 'workflow-save', id: decodeURIComponent(workflowSave[1] ?? '') };
  }
  const workflowArchive = /^\/api\/workflows\/(.+)\/archive$/.exec(path);
  if (workflowArchive && method === 'POST') {
    return { kind: 'workflow-archive', id: decodeURIComponent(workflowArchive[1] ?? '') };
  }
  const workflowValidate = /^\/api\/workflows\/(.+)\/validate$/.exec(path);
  if (workflowValidate && method === 'POST') {
    return { kind: 'workflow-validate', id: decodeURIComponent(workflowValidate[1] ?? '') };
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
  if (path === '/api/workflows' && method === 'POST') return { kind: 'workflow-create' };
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
  fields: Array<{
    variable: string;
    default?: unknown;
    kind?: string;
    /** For input_node fields: the .Nodes type this slot accepts. */
    node_type?: string;
  }>;
  /** RELION's own job-window tabs; `fields` name the variables per tab. */
  tabs?: Array<{ title: string; fields: string[] }>;
}

let jobSchema: { jobs: JobSchemaEntry[] } | null = null;

/** The tab titles of a job type, in the schema's order ("I/O", "Run", ...). */
export function tabsForJobType(jobType: string): Array<{ title: string; fields: string[] }> {
  return (
    jobSchema?.jobs.find((candidate) => candidate.job_type === jobType)?.tabs ?? []
  );
}

export function rememberJobSchema(schema: {
  jobs: JobSchemaEntry[];
  categories?: Array<{ title: string; job_types: string[] }>;
}): void {
  jobSchema = schema;
  jobCategories = schema.categories ?? null;
}

/** The agent's picker categories (`{title, job_types}`), or null before boot. */
let jobCategories: Array<{ title: string; job_types: string[] }> | null = null;

export function rememberJobCategories(
  categories: Array<{ title: string; job_types: string[] }>,
): void {
  jobCategories = categories;
}

/** RELION's category per job type, in schema order. */
export function categoryForJobType(jobType: string): string | null {
  return jobCategories?.find((category) => category.job_types.includes(jobType))?.title ?? null;
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

/** The same defaults, keyed by the schema's own job type (palette registry). */
export function valuesForJobType(jobType: string): Record<string, unknown> {
  const job = jobSchema?.jobs.find((candidate) => candidate.job_type === jobType);
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

/** Process label -> the filenames a job of that label writes. */
let jobOutputs: Map<string, string[]> = new Map();

/** A label's output filenames (empty when the job-types table has not loaded). */
export function labelOutputs(typeLabel: string): string[] {
  return jobOutputs.get(typeLabel) ?? [];
}

/**
 * Default values with the pipeline's own input paths filled in.
 *
 * For every `input_node` field, the node the pipeline edge names for that
 * consumer becomes the value, when its type matches what the field accepts.
 * The user edits as usual; nothing is invented - a slot whose producer the
 * pipeline does not declare stays at its schema default (usually empty).
 */
export function filledValues(
  typeLabel: string,
  inputNodes: string[],
  producerOf: Map<string, { name: string; type_label: string; produced_by?: string }>,
): Record<string, unknown> {
  const values = defaultValues(typeLabel);
  const job = jobForLabel(typeLabel);
  if (!job) return values;

  for (const field of job.fields) {
    if (field.kind !== 'input_node' || !field.node_type) continue;
    const binding = values[field.variable] as { content: unknown } | undefined;
    if (binding && typeof binding.content === 'string' && binding.content) continue;
    const match = inputNodes.find((name) => {
      const node = producerOf.get(name);
      // A field's `node_type` is a prefix of the node's full type label
      // (`TomogramGroupMetadata.star.relion` ⊂ `…relion.tomo.import`).
      return node && node.type_label.startsWith(field.node_type!);
    });
    if (match) values[field.variable] = { type: 'constant', content: match };
  }
  return values;
}

export function rememberJobTypes(
  entries: Array<{
    id: string;
    process_type_label?: string;
    /** Output filenames this job writes (`tilt_series.star`, ...). */
    outputs?: string[];
    directory?: string;
    title?: string;
  }>,
): void {
  // Prefer ids the schema actually has a row for. The job-types table also
  // carries abstract ids (`tomo_align_tilt_series`, `tomo_denoise_tomograms`)
  // that the schema splits into concrete variants; picking one of those would
  // leave the node with no form.
  const known = new Set(jobSchema?.jobs.map((job) => job.job_type) ?? []);
  const map = new Map<string, string>();
  const skipped: Array<[string, string]> = [];

  jobOutputs = new Map();
  for (const entry of entries) {
    const label = entry.process_type_label;
    if (!label) continue;
    if (known.size && !known.has(entry.id)) {
      skipped.push([label, entry.id]);
      continue;
    }
    if (!map.has(label)) {
      map.set(label, entry.id);
      // First variant of a label wins the defaults - and its output list,
      // which is what the canvas nodes declare as their outputs so an
      // upstream input can point at a real file rather than free text.
      jobOutputs.set(label, entry.outputs ?? []);
    }
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

/* --------------------------------------------------------------- workspace */

interface DraftLike {
  id: string;
  type: string;
  meta?: { position?: { x: number; y: number } };
  data: Record<string, unknown>;
}

interface EdgeLike {
  id?: string;
  sourceNodeID: string;
  targetNodeID: string;
  sourcePortID?: string;
  targetPortID?: string;
}

/**
 * A workspace: the project's live pipeline, plus what the canvas adds.
 *
 * The pipeline is read fresh on every call, so a job queued in RELION's own GUI
 * appears here too. The overlay contributes only what the project cannot know:
 * draft nodes (id `draft:…`, not queued yet), their edges, and the canvas
 * edits to pipeline nodes. Merging is by node id, so promotion of a draft (the
 * queue reserving a process name) collapses the two into one node without any
 * bookkeeping here.
 */
export function mergeWorkspace(
  workflow: {
    id: string;
    projectId: string;
    name: string;
    revision: number;
    schema: { nodes: DraftLike[]; edges: EdgeLike[] };
    updatedAt: string;
  },
  workspace: {
    id: string;
    name: string;
    revision: number;
    nodes: DraftLike[];
    edges: EdgeLike[];
    titles?: Record<string, string>;
    paramEdits?: Record<string, Record<string, unknown>>;
    updatedAt: string;
  },
): {
  id: string;
  projectId: string;
  name: string;
  revision: number;
  schema: { nodes: DraftLike[]; edges: EdgeLike[] };
  updatedAt: string;
} {
  const edits = workspace.paramEdits ?? {};
  const titles = workspace.titles ?? {};
  const nodes = workflow.schema.nodes.map((node) => {
    if (node.id === EXECUTION_NODE_ID) return node;
    const parameterEdits = edits[node.id];
    const values = (node.data.inputsValues ?? {}) as Record<string, unknown>;
    return {
      ...node,
      data: {
        ...node.data,
        title: titles[node.id] ?? node.data.title,
        ...(parameterEdits
          ? {
              inputsValues: {
                ...values,
                ...Object.fromEntries(
                  Object.entries(parameterEdits).map(([key, content]) => [
                    key,
                    { type: 'constant', content },
                  ]),
                ),
              },
            }
          : {}),
      },
    };
  });
  // Draft nodes keep their canvas position; a draft has no pipeline layout to
  // derive it from, so the stored position is the only one there is.
  const drafts = workspace.nodes.map((node) => ({
    ...node,
    data: {
      ...node.data,
      title: titles[node.id] ?? node.data.title,
      ...(edits[node.id]
        ? {
            inputsValues: {
              ...((node.data.inputsValues ?? {}) as Record<string, unknown>),
              ...Object.fromEntries(
                Object.entries(edits[node.id]!).map(([key, content]) => [
                  key,
                  { type: 'constant', content },
                ]),
              ),
            },
          }
        : {}),
    },
  }));
  return {
    ...workflow,
    id: workspace.id,
    name: workspace.name,
    revision: workspace.revision,
    schema: {
      // The execution start node stays first; draft nodes follow the pipeline.
      nodes: [...nodes, ...drafts],
      edges: [...workflow.schema.edges, ...(workspace.edges as EdgeLike[])],
    },
    updatedAt: workspace.updatedAt,
  };
}
