/**
 * The journal: RELION queue states, recorded as FlowGram runtime events.
 *
 * The vendored UI has one status pathway and one camera pathway, and both read
 * the same shape: a stream of `RuntimeEvent`s whose `kind` is the FlowGram
 * vocabulary (`node_started`, `node_finished`, ...). RELION does not emit
 * events - the queue computes each entry's state every tick from the job
 * manager, the sentinel files and the pipeline row.
 *
 * So this module polls the agent's `/api/queue` snapshot at the queue's own
 * cadence, diffs each entry's state against the previous tick, appends
 * FlowGram-shaped events into a per-run journal, and the SSE route in
 * middleware.ts replays that journal to whichever client asks.
 *
 * Event vocabulary → canvas badge (`CanvasNodeRunBadge`):
 *
 *   node_waiting   Clock3    pending entry, optionally with reasons
 *   node_started   Loader2   running entry, live elapsed timer
 *   node_finished  Check     succeeded entry
 *   node_failed    CircleAlert   failed entry, with the queue's reason
 *   node_canceled  CircleMinus   cancelled entry
 *   log_appended   (log tab) incremental run.out bytes while running
 */
import { agentJson } from './agent';

/** Wire shapes of the agent's queue snapshot (`queue_system.QueueEntry.as_dict`). */
interface QueueEntry {
  id: string;
  project: string;
  job_type: string;
  host: string | null;
  gpus: number[];
  leased_gpus: number[];
  state: string;
  job_id: string | null;
  process_name: string | null;
  predicted_outputs: Record<string, string>;
  error: string | null;
  blocked_by: string[];
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

interface QueueSnapshot {
  entries: QueueEntry[];
}

interface JobLog {
  chunk: string;
  next_offset: number;
  truncated: boolean;
}

/** RuntimeEvent, vendored types.ts. Duplicated here: the bridge must not
 * depend on src/ (it runs as Vite middleware, outside the app bundle). */
export interface JournalEvent {
  sequence: number;
  kind: string;
  objectId: string;
  data: string;
  createdAt: string;
}

interface RunRecord {
  id: string;
  workflowId: string;
  projectId: string;
  status: string;
  inputs: Record<string, unknown>;
  outputs: unknown;
  error: string | null;
  revision: number;
  createdAt: string;
}

interface JournalRun {
  run: RunRecord;
  events: JournalEvent[];
  /** Queue fields we remember per entry, for diffs and outputs. */
  entries: Map<string, QueueEntry>;
  /** How far into each running job's log we have read. */
  logOffsets: Map<string, number>;
  /** The last state we emitted per entry, so transitions emit exactly once. */
  seenStates: Map<string, string>;
  finished: boolean;
}

const runs = new Map<string, JournalRun>();

let sequence = 0;
const now = () => new Date().toISOString();

function event(kind: string, runId: string, data: Record<string, unknown>): JournalEvent {
  return {
    sequence: ++sequence,
    kind,
    objectId: runId,
    data: JSON.stringify(data),
    createdAt: now(),
  };
}

/**
 * Open (or reopen) a journal for a run that was just submitted.
 *
 * Called from the run route with the exact run id the UI received, so the
 * journal's events carry the same `objectId` that route returned.
 */
export function openRun(
  runId: string,
  projectId: string,
  inputs: Record<string, unknown>,
): void {
  const existing = runs.get(runId);
  const run: RunRecord = existing
    ? { ...existing.run, status: 'running', error: null }
    : {
        id: runId,
        workflowId: `pipeline:${projectId}`,
        projectId,
        status: 'running',
        inputs,
        outputs: null,
        error: null,
        revision: 1,
        createdAt: now(),
      };
  runs.set(runId, {
    run,
    events: existing?.events ?? [],
    entries: existing?.entries ?? new Map(),
    logOffsets: existing?.logOffsets ?? new Map(),
    seenStates: existing?.seenStates ?? new Map(),
    finished: false,
  });
  if (!existing) append(runId, event('run_started', runId, { inputs }));
}

function append(runId: string, item: JournalEvent): void {
  runs.get(runId)?.events.push(item);
}

/** Journal for one run id, or null when unknown. */
export function getRun(runId: string): JournalRun | null {
  return runs.get(runId) ?? null;
}

/** Every run known for a project, newest first, capped. */
export function runsForProject(projectPath: string): RunRecord[] {
  const list = [...runs.values()]
    .filter((entry) => entry.run.projectId === projectPath)
    .map((entry) => entry.run);
  list.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  return list.slice(0, 50);
}

/** Events after a sequence, optionally filtered to one run. */
export function eventsAfter(after: number, objectId?: string): JournalEvent[] {
  const out: JournalEvent[] = [];
  for (const [runId, journal] of runs) {
    if (objectId && runId !== objectId) continue;
    for (const item of journal.events) {
      if (item.sequence > after) out.push(item);
    }
  }
  out.sort((a, b) => a.sequence - b.sequence);
  return out;
}

// ------------------------------------------------------------------- ticking

const TICK_MS = 2_000;
/** Log reads ride the tick; a chunk cap keeps one burst from flooding SSE. */
const LOG_CHUNK_MAX = 8_000;

let timer: ReturnType<typeof setInterval> | null = null;

/**
 * The FlowGram event for a queue state.
 *
 * Kept as a table so the mapping is declared once, in the plan's terms, not
 * recomputed in two places.
 */
function stateEvent(
  entry: QueueEntry,
): { kind: string; data: Record<string, unknown> } | null {
  const common = {
    nodeId: entry.process_name ?? entry.id,
    executionId: entry.id,
    workflowId: `pipeline:${entry.project}`,
  };
  switch (entry.state) {
    case 'pending':
      return { kind: 'node_waiting', data: { ...common, reasons: entry.blocked_by } };
    case 'running':
      return {
        kind: 'node_started',
        data: {
          ...common,
          startedAt: (entry.started_at ? new Date(entry.started_at * 1_000) : new Date()).toISOString(),
          gpus: entry.leased_gpus,
        },
      };
    case 'succeeded':
      return {
        kind: 'node_finished',
        data: {
          ...common,
          outputs: entry.predicted_outputs,
          endedAt: (entry.finished_at ? new Date(entry.finished_at * 1_000) : new Date()).toISOString(),
        },
      };
    case 'failed':
      return { kind: 'node_failed', data: { ...common, error: entry.error ?? 'job failed' } };
    case 'cancelled':
      return { kind: 'node_canceled', data: { ...common } };
    default:
      return null;
  }
}

/** Queue states that end an entry; anything else stays live. */
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

async function tick(): Promise<void> {
  let snapshot: QueueSnapshot;
  try {
    snapshot = await agentJson<QueueSnapshot>('/api/queue');
  } catch {
    // The agent is briefly unreachable (restarting, or a project mount went
    // away). The journal keeps what it has; the next tick reconciles.
    return;
  }

  const live = snapshot.entries.filter(
    (entry) => runs.has(runIdOf(entry.project)) || !TERMINAL.has(entry.state),
  );
  for (const entry of live) {
    const runId = runIdOf(entry.project);
    if (!runs.has(runId)) {
      // A job queued outside this app (the desktop GUI enqueues too, and the
      // queue survives restarts). Watch it under a derived id so the canvas
      // still sees it - the run list simply gains one entry.
      openRun(runId, entry.project, {});
    }
    const journal = runs.get(runId)!;
    const previous = journal.entries.get(entry.id);
    journal.entries.set(entry.id, entry);
    const known = journal.seenStates.get(entry.id);

    if (known === entry.state && previous !== undefined) {
      // Same state, but a pending entry's blockers can change - the user
      // watches *why* it is waiting as much as *that* it is waiting.
      if (entry.state === 'pending' && previous.blocked_by.join('|') !== entry.blocked_by.join('|')) {
        const builder = stateEvent(entry);
        if (builder) append(journal.run.id, event(builder.kind, journal.run.id, builder.data));
      }
      continue;
    }

    const builder = stateEvent(entry);
    if (builder) append(journal.run.id, event(builder.kind, journal.run.id, builder.data));
    journal.seenStates.set(entry.id, entry.state);

    // A running entry's log: read the incremental bytes since last tick.
    if (entry.state === 'running' && entry.job_id) void readLog(journal, entry);
  }

  // Close any journal whose entries have all reached a terminal state.
  for (const [runId, journal] of runs) {
    if (journal.finished || !journal.entries.size) continue;
    const mine = [...journal.entries.values()];
    if (!mine.every((entry) => TERMINAL.has(entry.state))) continue;
    journal.finished = true;
    journal.run.status = mine.some((entry) => entry.state === 'failed')
      ? 'failed'
      : 'succeeded';
    const error = mine.map((entry) => entry.error).filter(Boolean).join('; ');
    if (error) journal.run.error = error;
    append(
      runId,
      event('run_finished', runId, {
        status: journal.run.status,
        counts: Object.fromEntries(mine.map((entry) => [entry.id, entry.state])),
      }),
    );
  }
}

/** A stable run id for everything queued out of one project. */
function runIdOf(project: string): string {
  return `relion-project:${project}`;
}

async function readLog(journal: JournalRun, entry: QueueEntry): Promise<void> {
  const jobId = entry.job_id;
  if (!jobId) return;
  const offset = journal.logOffsets.get(jobId) ?? 0;
  try {
    const log = await agentJson<JobLog>(
      `/api/jobs/${encodeURIComponent(jobId)}/log?offset=${offset}`,
    );
    journal.logOffsets.set(jobId, log.truncated ? 0 : log.next_offset);
    if (!log.chunk) return;
    append(
      journal.run.id,
      event('log_appended', journal.run.id, {
        nodeId: entry.process_name ?? entry.id,
        executionId: entry.id,
        workflowId: `pipeline:${entry.project}`,
        chunk: log.chunk.length > LOG_CHUNK_MAX ? log.chunk.slice(-LOG_CHUNK_MAX) : log.chunk,
        truncated: log.truncated,
      }),
    );
  } catch {
    // A log read failure is not a job failure; the next tick retries.
  }
}

/** Start the poll loop (idempotent; middleware calls this at boot). */
export function startJournal(): void {
  if (timer) return;
  timer = setInterval(() => void tick(), TICK_MS);
}

/**
 * Record a client-driven cancellation.
 *
 * The queue drops a cancelled live entry on its next tick, so no transition
 * would ever report CANCELLED if we waited; the cancel route records the fact
 * itself, and the *next* tick only sees the entry gone - it emits nothing.
 */
export function recordCancel(runId: string, cancelledIds: string[]): void {
  const journal = runs.get(runId);
  if (!journal) return;
  for (const entryId of cancelledIds) {
    const entry = journal.entries.get(entryId);
    if (!entry) {
      // The journal has not seen this entry yet (the tick that follows the
      // POST has not run). Remember the decision so the first sighting of the
      // queue's own CANCELLED does not double-report it either.
      journal.seenStates.set(entryId, 'cancelled');
      continue;
    }
    if (TERMINAL.has(entry.state)) continue;
    entry.state = 'cancelled';
    const builder = stateEvent(entry);
    if (builder) append(journal.run.id, event(builder.kind, journal.run.id, builder.data));
    journal.seenStates.set(entryId, 'cancelled');
    journal.logOffsets.delete(entry.id);
  }
}
