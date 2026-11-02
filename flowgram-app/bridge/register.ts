/**
 * Fetch RELION's job schema and register each job type twice:
 *
 * - with the node inspector (`registerNodeDefinitions`), so a RELION node's
 *   form renders in the stock renderer;
 * - with the palette (`registerRelionCatalogEntries`), grouped by the agent's
 *   own picker categories, so "add node" offers RELION's job types as the
 *   primary vocabulary.
 *
 * Called once at boot, before any pipeline renders. Without it a RELION node
 * shows an empty form and the palette has no RELION groups.
 */
import { api } from '../src/lib/api';
import { registerNodeDefinitions } from '../src/components/node-inspector/definitions';
import { registerRelionCatalogEntries } from '../src/lib/node-catalog';

import {
  RELION_NODE_GROUP,
  RELION_OTHER_GROUP,
  jobDefinition,
  nodeTypeFor,
  paletteDescription,
  paletteTitle,
  type RelionJobType,
} from './jobs';
import {
  categoryForJobType,
  rememberJobCategories,
  rememberJobSchema,
  rememberJobTypes,
  valuesForJobType,
} from './translate';

let registered = false;

export async function registerRelionJobs(): Promise<void> {
  if (registered) return;
  registered = true;

  try {
    const [schema, types] = await Promise.all([
      api<{
        jobs: RelionJobType[];
        categories?: Array<{ title: string; job_types: string[] }>;
      }>('/api/job-schema'),
      api<{ job_types: Array<{ id: string; process_type_label?: string }> }>(
        '/api/project/job-types',
      ),
    ]);
    // Same tables, several consumers: the inspector's field definitions here,
    // the projection's node types/defaults in the bridge, and the palette
    // groups from categories below.
    rememberJobSchema(schema as never);
    rememberJobTypes(types.job_types);
    const definitions = Object.fromEntries(
      schema.jobs.map((job) => [nodeTypeFor(job.job_type), jobDefinition(job)]),
    );
    registerNodeDefinitions(definitions);

    // The palette's RELION groups come from the agent's own picker
    // categories; a job type absent from every category falls back to 其它.
    const categories = schema.categories ?? [];
    rememberJobCategories(categories);
    const orderOf = new Map<string, number>();
    categories.forEach((category, index) => {
      for (const [position, jobType] of category.job_types.entries()) {
        orderOf.set(
          jobType,
          Object.values(RELION_NODE_GROUP).indexOf(
            RELION_NODE_GROUP[category.title] ?? RELION_OTHER_GROUP,
          ) * 1000 + index * 100 + position,
        );
      }
    });
    registerRelionCatalogEntries(
      schema.jobs.map((job, position) => {
        const category = categoryForJobType(job.job_type);
        const group = RELION_NODE_GROUP[category ?? ''] ?? RELION_OTHER_GROUP;
        return {
          type: nodeTypeFor(job.job_type),
          title: paletteTitle(job),
          group,
          description: paletteDescription(job),
          order: orderOf.get(job.job_type) ?? categories.length * 1000 + position,
          defaults: valuesForJobType(job.job_type),
        };
      }),
    );
  } catch (cause) {
    // A missing schema must not stop the app booting; pipelines still render,
    // their node forms are simply empty.
    console.warn('RELION job schema unavailable:', cause);
  }
}
