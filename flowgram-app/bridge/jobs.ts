/**
 * RELION job types as FlowGram node definitions.
 *
 * `nodeDefinitions` in the vendored app is a static table of *data*: which
 * fields a node has and which of FloGram's editors renders each one. Adding
 * entries to it is not writing custom UI - no component, no CSS, no layout -
 * it is telling the stock form renderer which controls to use. That is the
 * line this branch stays behind.
 *
 * The mapping from RELION's six field kinds to FlowGram's editors:
 *
 *   RELION      FlowGram        why
 *   ----------  --------------  ------------------------------------------
 *   boolean     value(boolean)  a typed constant row; the value editor's
 *                               boolean mode is the closest stock control
 *   radio       select          choices map 1:1
 *   slider      number          min/max/step all exist on the field
 *   string      value(string)   plain text; multiline when the help says so
 *   path        value(string)   RELION takes a filesystem path as text; there
 *                               is no stock file picker, and inventing one is
 *                               v2 work
 *   input_node  value(string)   a STAR file path chosen from the pipeline's
 *                               .Nodes; the slot picker is v2 work
 *
 * Fields are grouped into `section`s roughly following RELION's own job
 * window, so the form is not one 80-row list.
 */

/** One field as `/api/job-schema` returns it. */
export interface RelionField {
  variable: string;
  label: string;
  kind: 'boolean' | 'radio' | 'slider' | 'string' | 'path' | 'input_node' | string;
  default?: unknown;
  min?: number;
  max?: number;
  step?: number;
  options?: Array<{ value?: string; label?: string }> | string[];
  help?: string;
  section?: string;
}

export interface RelionJobType {
  job_type: string;
  title: string;
  fields: RelionField[];
  required?: string[];
}

/**
 * The editor vocabulary comes from the vendored inspector, not from a copy:
 * a second declaration here would silently drift from the one the form
 * renderer switches on.
 */
export type { FieldDefinition, NodeDefinition } from '../src/components/node-inspector/definitions';
import type { FieldDefinition, NodeDefinition } from '../src/components/node-inspector/definitions';

/** RELION labels carry a trailing colon; FlowGram's form adds its own. */
function cleanLabel(label: string): string {
  return label.replace(/\s*:\s*$/, '').trim();
}

/** `options` arrives as `{value,label}` objects or as bare strings. */
function choicesOf(field: RelionField): Array<[string, string]> | undefined {
  if (!field.options?.length) return undefined;
  return field.options.map((option) => {
    if (typeof option === 'string') return [option, option] as [string, string];
    const value = String(option.value ?? option.label ?? '');
    return [value, option.label ?? value] as [string, string];
  });
}

/**
 * Long free text gets a multiline editor: RELION's help text is the only
 * signal available, and a path or a regex is never that.
 */
function looksMultiline(field: RelionField): boolean {
  const help = (field.help ?? '').toLowerCase();
  return help.includes('comma') || help.includes('separated') || help.includes('one per line');
}

export function fieldDefinition(field: RelionField, required: boolean): FieldDefinition {
  const base = {
    key: field.variable,
    label: cleanLabel(field.label || field.variable),
    hint: field.help?.trim() || undefined,
    section: field.section,
    defaultValue: field.default,
    ...(required ? { required: true } : {}),
  };

  switch (field.kind) {
    case 'boolean':
      return { ...base, editor: 'value', type: 'boolean' };

    case 'radio': {
      const choices = choicesOf(field);
      // Without choices a select would render empty; fall back to text so the
      // field is still editable rather than silently broken.
      return choices
        ? { ...base, editor: 'select', choices }
        : { ...base, editor: 'value', type: 'string' };
    }

    case 'slider':
      return {
        ...base,
        editor: 'number',
        type: 'number',
        ...(typeof field.min === 'number' ? { min: field.min } : {}),
        ...(typeof field.max === 'number' ? { max: field.max } : {}),
      };

    case 'path':
    case 'input_node':
    case 'string':
    default:
      return {
        ...base,
        editor: 'value',
        type: 'string',
        ...(looksMultiline(field) ? { multiline: true } : {}),
      };
  }
}

/**
 * One job type as a node definition.
 *
 * `output` returns an empty object schema: a RELION job's outputs are pipeline
 * nodes, not a value this node computes, so there is nothing honest to declare.
 */
export function jobDefinition(job: RelionJobType): NodeDefinition {
  const required = new Set(job.required ?? []);
  return {
    fields: job.fields.map((field) => fieldDefinition(field, required.has(field.variable))),
    output: () => ({ type: 'object', properties: {} }),
    aliases: [job.title, job.job_type],
  };
}

/** Node type id for a RELION job type. Namespaced so it cannot collide. */
export function nodeTypeFor(jobType: string): string {
  return `relion.${jobType}`;
}
