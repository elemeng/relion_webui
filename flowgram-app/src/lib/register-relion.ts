/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

/**
 * Register RELION's job types with the canvas, twice:
 *
 * - with the node inspector, so a RELION node's form renders in the stock
 *   renderer;
 * - with the palette, grouped by the agent's own picker categories, so "add
 *   node" offers RELION's job types as the primary vocabulary.
 *
 * Called once at boot, before any pipeline renders. The data comes from
 * ``/fg/api/job-schema`` - the agent serves it, and the projection there uses
 * the same tables, so the canvas and the server cannot disagree about which
 * fields a job has.
 */
import { api } from './api';
import { registerNodeDefinitions } from '../components/node-inspector/definitions';
import { registerRelionCatalogEntries } from './node-catalog';
import {
  RELION_NODE_GROUP,
  RELION_OTHER_GROUP,
  jobDefinition,
  nodeTypeFor,
  paletteDescription,
  paletteTitle,
  type RelionJobType,
} from './relion-job-definitions';

let registered = false;

interface JobSchemaResponse {
  jobs: RelionJobType[];
  categories?: Array<{ title: string; job_types: string[] }>;
}

export async function registerRelionJobs(): Promise<void> {
  if (registered) return;
  registered = true;

  try {
    const schema = await api<JobSchemaResponse>('/api/job-schema');
    const definitions = Object.fromEntries(
      schema.jobs.map((job) => [nodeTypeFor(job.job_type), jobDefinition(job)]),
    );
    registerNodeDefinitions(definitions);

    // The palette's RELION groups come from the agent's own picker categories;
    // a job type absent from every category falls back to 其它.
    const categories = schema.categories ?? [];
    const orderOf = new Map<string, number>();
    categories.forEach((category, index) => {
      for (const [position, jobType] of category.job_types.entries()) {
        orderOf.set(
          jobType,
          Object.values(RELION_NODE_GROUP).indexOf(
            RELION_NODE_GROUP[category.title] ?? RELION_OTHER_GROUP,
          ) *
            1000 +
            index * 100 +
            position,
        );
      }
    });
    const categoryOf = new Map(
      categories.flatMap((category) =>
        category.job_types.map((jobType) => [jobType, category.title] as const),
      ),
    );
    registerRelionCatalogEntries(
      schema.jobs.map((job, position) => {
        const category = categoryOf.get(job.job_type);
        const group = RELION_NODE_GROUP[category ?? ''] ?? RELION_OTHER_GROUP;
        return {
          type: nodeTypeFor(job.job_type),
          title: paletteTitle(job),
          group,
          description: paletteDescription(job),
          order: orderOf.get(job.job_type) ?? categories.length * 1000 + position,
          defaults: Object.fromEntries(
            job.fields.map((field) => [
              field.variable,
              { type: 'constant', content: field.default ?? '' },
            ]),
          ),
        };
      }),
    );
  } catch (cause) {
    // A missing schema must not stop the app booting; pipelines still render,
    // their node forms are simply empty.
    console.warn('RELION job schema unavailable:', cause);
  }
}
