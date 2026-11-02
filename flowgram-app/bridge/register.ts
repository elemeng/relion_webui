/**
 * Fetch RELION's job schema and register each job type with the inspector.
 *
 * Called once at boot, before any pipeline renders. Without it a RELION node
 * shows an empty form: the inspector looks up `definitionFor(node)`, finds
 * nothing for a `relion.*` type, and falls back to no fields.
 */
import { api } from '../src/lib/api';
import { registerNodeDefinitions } from '../src/components/node-inspector/definitions';

import { jobDefinition, nodeTypeFor, type RelionJobType } from './jobs';
import { rememberJobSchema, rememberJobTypes } from './translate';

let registered = false;

export async function registerRelionJobs(): Promise<void> {
  if (registered) return;
  registered = true;

  try {
    const [schema, types] = await Promise.all([
      api<{ jobs: RelionJobType[] }>('/api/job-schema'),
      api<{ job_types: Array<{ id: string; process_type_label?: string }> }>(
        '/api/project/job-types',
      ),
    ]);
    // Same tables, two consumers: the inspector's field definitions here, and
    // the projection's node types/defaults in the bridge.
    rememberJobSchema(schema as never);
    rememberJobTypes(types.job_types);
    const definitions = Object.fromEntries(
      schema.jobs.map((job) => [nodeTypeFor(job.job_type), jobDefinition(job)]),
    );
    registerNodeDefinitions(definitions);
  } catch (cause) {
    // A missing schema must not stop the app booting; pipelines still render,
    // their node forms are simply empty.
    console.warn('RELION job schema unavailable:', cause);
  }
}
