/**
 * Execution: FlowGram's run request, translated into RELION queue calls.
 *
 * The vendored UI posts `{inputs}` to `/api/workflows/{id}/run` and then follows
 * the returned run's events. RELION has no equivalent - a "run" here is a set of
 * jobs queued into `queue_system`, each of which may wait for a GPU, a host or
 * an upstream file.
 *
 * So a run is modelled as *one queue submission per selected job*, plus a run
 * record whose id maps back to those queue entries. The canvas follows it the
 * same way it follows any other run: through node events.
 *
 * What the user chose in the run dialog (`scope`, `gpu`, `exclusive`) decides
 * which jobs are submitted and with which devices.
 */

/** One job to queue, resolved from the pipeline and the chosen scope. */
export interface QueuedJob {
  process: string;
  jobType: string;
  arguments: Record<string, unknown>;
}

/** What the run dialog collected. Mirrors EXECUTION_SCHEMA in translate.ts. */
export interface ExecutionIntent {
  scope: 'all' | 'selected' | 'job';
  gpu: 'auto' | 'specific';
  gpuIndex?: number;
  exclusive?: boolean;
  /** Which node "selected" and "job" refer to; a canvas state, not an input. */
  selectedNodeId?: string;
}

/**
 * Devices to lease.
 *
 * `auto` is the queue's own behaviour: an empty list means "choose a free
 * card", and it is the only safe default because a device is leased
 * exclusively - naming one that is busy makes the job wait for that specific
 * card rather than any card.
 */
export function devicesFor(intent: ExecutionIntent): number[] {
  if (intent.gpu === 'specific' && Number.isInteger(intent.gpuIndex)) {
    return [intent.gpuIndex as number];
  }
  return [];
}

/**
 * Which processes the scope selects.
 *
 * `all` is the whole pipeline in layout order - the order matters, because the
 * queue admits entries in submission order when several are ready at once.
 */
export function processesFor(
  intent: ExecutionIntent,
  pipeline: Array<{ name: string; status?: string }>,
): string[] {
  const runnable = pipeline.filter((process) => !isFinished(process.status));
  switch (intent.scope) {
    case 'job':
      return intent.selectedNodeId
        ? runnable.filter((process) => process.name === intent.selectedNodeId).map((p) => p.name)
        : [];
    case 'selected':
      return intent.selectedNodeId
        ? runnable
            .filter((process) => process.name === intent.selectedNodeId)
            .map((process) => process.name)
        : [];
    case 'all':
    default:
      return runnable.map((process) => process.name);
  }
}

/** A job that already ran does not need queueing again. */
function isFinished(status?: string): boolean {
  return status === 'Succeeded' || status === 'Running';
}

/**
 * The queue request body for one job.
 *
 * Field names are `queue_system`'s: `project_directory`, `job_type`,
 * `arguments`, `gpus`, `exclusive`, `after_jobs`.
 */
export function queueBody(
  projectPath: string,
  job: QueuedJob,
  intent: ExecutionIntent,
  dependencies: string[],
) {
  return {
    project_directory: projectPath,
    job_type: job.jobType,
    arguments: job.arguments,
    gpus: devicesFor(intent),
    exclusive: intent.exclusive ?? false,
    // A downstream job waits for its producers. The queue re-reads the
    // pipeline every tick, so naming them here is a hint, not a contract.
    after_jobs: dependencies,
  };
}

/**
 * A run id that maps back to what was queued.
 *
 * The UI stores it and later asks for `/api/runs/{id}`; encoding the project
 * and the queue entry ids in the id is enough to answer that without a table.
 */
export function runIdFor(projectPath: string, entryIds: string[]): string {
  return `relion:${encodeURIComponent(projectPath)}:${entryIds.join(',')}`;
}
