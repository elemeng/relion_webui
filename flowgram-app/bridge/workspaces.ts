/**
 * Workspaces: RELION projects as FlowGram "workflows".
 *
 * A workflow in this app is a *draft overlay* on one RELION project, not a
 * second pipeline format. What it holds is the part of the canvas the project
 * does not: nodes the user placed but has not queued yet, their edges, and the
 * per-node parameter edits made on the canvas. None of that touches
 * `default_pipeline.star` or any job directory - a workflow is a soft grouping,
 * stored under the agent's config directory (`~/.relion_web`,
 * `RELION_WEB_HOME`), exactly where the project list already lives.
 *
 * On every read the overlay is merged with the project's live pipeline
 * projection, so:
 *
 * - a queued job's node comes from the pipeline (it can then be run, and its
 *   state is reported by the queue);
 * - a draft node exists only in the overlay until the user queues it, and is
 *   then *promoted*: the queue reserves its process, and the overlay drops it.
 *
 * The merge is keyed on node id, which is stable: a draft node is
 * `draft:<workspace id>:<n>` so it can never collide with a process name.
 */
import { promises as fs } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

/** One node as the UI stores it (FlowGram's shape). */
export interface DraftNode {
  id: string;
  type: string;
  meta?: { position?: { x: number; y: number } };
  data: Record<string, unknown>;
}

export interface DraftEdge {
  sourceNodeID: string;
  targetNodeID: string;
  sourcePortID?: string;
  targetPortID?: string;
}

export interface Workspace {
  id: string;
  projectId: string;
  name: string;
  revision: number;
  /** Draft-only nodes and edges; the project's own nodes are merged on read. */
  nodes: DraftNode[];
  edges: DraftEdge[];
  /** Canvas edits of pipeline nodes: titles and constant parameter values. */
  titles?: Record<string, string>;
  paramEdits?: Record<string, Record<string, unknown>>;
  createdAt: string;
  updatedAt: string;
  archived?: boolean;
}

interface Store {
  counter: number;
  workspaces: Record<string, Workspace>;
}

const CONFIG_DIR =
  process.env.RELION_WEB_HOME ?? join(homedir(), '.relion_web');
const STORE_FILE = join(CONFIG_DIR, 'flowgram-workspaces.json');

let cache: Store | null = null;

async function store(): Promise<Store> {
  if (cache) return cache;
  try {
    const raw = await fs.readFile(STORE_FILE, 'utf8');
    const parsed = JSON.parse(raw) as Partial<Store>;
    cache = {
      counter: Number(parsed.counter) || 0,
      workspaces: parsed.workspaces ?? {},
    };
  } catch {
    // A missing or unreadable file is an empty store, not an error: the
    // first read creates it.
    cache = { counter: 0, workspaces: {} };
  }
  return cache;
}

/**
 * Write the store atomically.
 *
 * A half-written file would lose every draft the user has, and the draft is
 * the only copy of a node that has not been queued yet.
 */
async function persist(data: Store): Promise<void> {
  cache = data;
  await fs.mkdir(CONFIG_DIR, { recursive: true });
  const temporary = `${STORE_FILE}.${process.pid}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(data, null, 2)}\n`, 'utf8');
  await fs.rename(temporary, STORE_FILE);
}

/** All live workspaces of a project, newest first. */
export async function listWorkspaces(projectId: string): Promise<Workspace[]> {
  const data = await store();
  return Object.values(data.workspaces)
    .filter((workspace) => workspace.projectId === projectId && !workspace.archived)
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

export async function getWorkspace(id: string): Promise<Workspace | null> {
  const data = await store();
  const workspace = data.workspaces[id];
  return workspace && !workspace.archived ? workspace : null;
}

/** The default workspace of a project, created on first use. */
export async function ensureDefaultWorkspace(projectId: string): Promise<Workspace> {
  const existing = await listWorkspaces(projectId);
  const named = existing.find((workspace) => workspace.name === 'Main');
  if (named) return named;
  return createWorkspace(projectId, 'Main');
}

export async function createWorkspace(
  projectId: string,
  name: string,
): Promise<Workspace> {
  const data = await store();
  data.counter += 1;
  const id = `ws-${data.counter}`;
  const timestamp = new Date().toISOString();
  const workspace: Workspace = {
    id,
    projectId,
    name: name.trim() || `Workspace ${data.counter}`,
    revision: 1,
    nodes: [],
    edges: [],
    createdAt: timestamp,
    updatedAt: timestamp,
  };
  data.workspaces[id] = workspace;
  await persist(data);
  return workspace;
}

/**
 * Save a workspace.
 *
 * The caller sends the whole canvas (as FlowGram does); this keeps only what
 * the overlay owns - nodes that are not pipeline processes yet, the edges
 * between any nodes, and the title/parameter edits of process nodes, which are
 * kept in `paramEdits` so the next read can re-apply them to the projection.
 */
export async function saveWorkspace(
  id: string,
  patch: {
    name?: string;
    schema?: {
      nodes?: DraftNode[];
      edges?: DraftEdge[];
    };
  },
): Promise<Workspace | null> {
  const data = await store();
  const workspace = data.workspaces[id];
  if (!workspace || workspace.archived) return null;

  if (patch.name) workspace.name = patch.name;
  if (patch.schema?.nodes) {
    workspace.nodes = patch.schema.nodes.filter((node) => !isPipelineNode(node));
    workspace.paramEdits = collectParameterEdits(patch.schema.nodes);
    workspace.titles = collectTitles(patch.schema.nodes);
  }
  if (patch.schema?.edges) {
    workspace.edges = patch.schema.edges.filter(
      (edge) => !(edge as { id?: string }).id?.startsWith('e-start-'),
    );
  }
  workspace.revision += 1;
  workspace.updatedAt = new Date().toISOString();
  await persist(data);
  return workspace;
}

export async function archiveWorkspace(id: string): Promise<boolean> {
  const data = await store();
  const workspace = data.workspaces[id];
  if (!workspace) return false;
  workspace.archived = true;
  workspace.updatedAt = new Date().toISOString();
  await persist(data);
  return true;
}

/**
 * Promote a draft node to a queued job.
 *
 * Called after the queue accepted the job: the process name the queue reserved
 * becomes the node's id from then on, so the overlay must drop the draft and
 * record the mapping. The edges pointing at the draft are re-pointed, which is
 * what keeps the graph intact across the promotion.
 */
export async function promoteDraft(
  id: string,
  draftNodeId: string,
  processName: string,
): Promise<void> {
  const data = await store();
  const workspace = data.workspaces[id];
  if (!workspace) return;
  workspace.nodes = workspace.nodes.filter((node) => node.id !== draftNodeId);
  workspace.edges = workspace.edges.map((edge) => ({
    ...edge,
    sourceNodeID: edge.sourceNodeID === draftNodeId ? processName : edge.sourceNodeID,
    targetNodeID: edge.targetNodeID === draftNodeId ? processName : edge.targetNodeID,
  }));
  workspace.updatedAt = new Date().toISOString();
  await persist(data);
}

/** A draft node id is namespaced so it cannot collide with a process name. */
export function isPipelineNode(node: DraftNode): boolean {
  return !node.id.startsWith('draft:');
}

/** Titles the user renamed on the canvas, keyed by node id. */
export function collectTitles(nodes: DraftNode[]): Record<string, string> {
  const titles: Record<string, string> = {};
  for (const node of nodes) {
    if (isPipelineNode(node) && typeof node.data?.title === 'string')
      titles[node.id] = node.data.title;
  }
  return titles;
}

/**
 * Parameter edits made on the canvas, keyed by process name.
 *
 * Only constants are recorded: a reference depends on the graph around it and
 * is resolved at submit time, so it must not be frozen into the overlay.
 */
export function collectParameterEdits(
  nodes: DraftNode[],
): Record<string, Record<string, unknown>> {
  const edits: Record<string, Record<string, unknown>> = {};
  for (const node of nodes) {
    if (!isPipelineNode(node)) continue;
    const values = (node.data?.inputsValues ?? {}) as Record<string, unknown>;
    const constants: Record<string, unknown> = {};
    for (const [key, binding] of Object.entries(values)) {
      const value = binding as { type?: string; content?: unknown };
      if (value?.type === 'constant') constants[key] = value.content;
    }
    if (Object.keys(constants).length) edits[node.id] = constants;
  }
  return edits;
}
