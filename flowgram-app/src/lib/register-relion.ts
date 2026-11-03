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
 * Called once at boot, before any pipeline renders. Both payloads come from
 * `/fg/api/job-schema`: the agent owns the mapping from RELION options to
 * FlowGram editors (backend/relion_webui/flowgram/forms.py), so there is one
 * definition of what a job's form looks like, not two that can drift.
 */
import { api } from './api';
import {
  registerNodeDefinitions,
  type NodeDefinition,
} from '../components/node-inspector/definitions';
import { registerRelionCatalogEntries } from './node-catalog';

/** RELION category -> palette group, in the palette's own vocabulary. */
const GROUP: Record<string, string> = {
  'Import and preprocess': '导入与预处理',
  'Align and reconstruct': '对齐与重建',
  'Enhance and pick': '增强与拾取',
  'Subtomogram averaging': '亚颗粒平均',
  'Refine and reconstruct': '精修与重建',
  Select: '筛选',
  'Post-process and interpret': '后处理与解读',
  Utilities: '工具',
};
const OTHER_GROUP = '其它';

interface SchemaJob {
  job_type: string;
  title: string;
  fields: Array<{
    variable: string;
    default?: unknown;
    help?: string;
    kind?: string;
    required?: boolean;
  }>;
  required?: string[];
}

interface JobSchemaResponse {
  jobs: SchemaJob[];
  /**
   * Node definitions, keyed by `relion.<job_type>`. What arrives is the data
   * half - fields, tips, sections - because `output` is a function; the caller
   * supplies the identity output below.
   */
  definitions?: Record<string, Omit<NodeDefinition, 'output'>>;
  categories?: Array<{ title: string; job_types: string[] }>;
}

let registered = false;

export async function registerRelionJobs(): Promise<void> {
  if (registered) return;
  registered = true;

  try {
    const schema = await api<JobSchemaResponse>('/api/job-schema/definitions');
    if (schema.definitions) {
      // The agent sends the data half of a node definition; `output` is a
      // function and cannot cross JSON. A RELION job's outputs are pipeline
      // nodes, not a value this node computes, so there is nothing to declare.
      registerNodeDefinitions(
        Object.fromEntries(
          Object.entries(schema.definitions).map(([type, definition]) => [
            type,
            {
              ...definition,
              output: () => ({ type: 'object', properties: {} }),
            },
          ]),
        ),
      );
    }

    const categories = schema.categories ?? [];
    const groupOrder = Object.values(GROUP);
    const categoryOf = new Map<string, string>();
    const orderOf = new Map<string, number>();
    categories.forEach((category, index) => {
      const group = GROUP[category.title] ?? OTHER_GROUP;
      for (const [position, jobType] of category.job_types.entries()) {
        categoryOf.set(jobType, group);
        orderOf.set(
          jobType,
          groupOrder.indexOf(group) * 1000 + index * 100 + position,
        );
      }
    });

    registerRelionCatalogEntries(
      schema.jobs.map((job, position) => ({
        type: `relion.${job.job_type}`,
        title: job.title || job.job_type,
        group: categoryOf.get(job.job_type) ?? OTHER_GROUP,
        // The first field's help is the most useful one-liner a palette row
        // has; falling back to the parameter count keeps rows uniform.
        description:
          (job.fields.find((field) => field.help && field.kind !== 'boolean')?.help ?? '')
            .trim()
            .slice(0, 90) || `${job.fields.length} 个参数`,
        order: orderOf.get(job.job_type) ?? categories.length * 1000 + position,
        defaults: Object.fromEntries(
          job.fields.map((field) => [
            field.variable,
            { type: 'constant', content: field.default ?? '' },
          ]),
        ),
      })),
    );
  } catch (cause) {
    // A missing schema must not stop the app booting; pipelines still render,
    // their node forms are simply empty.
    console.warn('RELION job schema unavailable:', cause);
  }
}
