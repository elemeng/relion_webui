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
interface RelionField {
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
  /** The file-pattern hint the C++ window shows ("path/to/*.star"). */
  pattern?: string;
  /** For input_node fields: the .Nodes type this slot accepts. */
  node_type?: string;
}

export interface RelionJobType {
  job_type: string;
  title: string;
  fields: RelionField[];
  required?: string[];
  /** RELION's job-window tabs; `fields` lists each tab's variables. */
  tabs?: Array<{ title: string; fields: string[] }>;
  /**
   * The program's full flag list (`cli_options`): the advanced panel's
   * vocabulary. Flags the form does not already cover are what "other
   * arguments" is for, so their usage text rides the other_args hint.
   */
  cli_options?: Array<{
    flag: string;
    kind: string;
    usage: string | null;
    default?: unknown;
    hidden?: boolean;
    used_by_form?: boolean;
  }>;
  cli_options_unavailable?: string[] | null;
}

/**
 * The palette's RELION categories, keyed by the agent's English titles.
 *
 * The agent already groups job types for its picker (`/api/job-schema`'s
 * `categories`); these are their Chinese labels - the palette's group
 * vocabulary is Chinese, so the two read as one surface.
 */
export const RELION_NODE_GROUP: Record<string, string> = {
  'Import and preprocess': '导入与预处理',
  'Align and reconstruct': '对齐与重建',
  'Enhance and pick': '增强与拾取',
  'Subtomogram averaging': '亚颗粒平均',
  'Refine and reconstruct': '精修与重建',
  Select: '筛选',
  'Post-process and interpret': '后处理与解读',
  Utilities: '工具',
  Other: '其它',
};

/** The fallback group for a category this table does not know. */
export const RELION_OTHER_GROUP = RELION_NODE_GROUP.Other;

/**
 * The editor vocabulary comes from the vendored inspector, not from a copy:
 * a second declaration here would silently drift from the one the form
 * renderer switches on.
 */
export type { FieldDefinition, NodeDefinition } from '../src/components/node-inspector/definitions';
import type { FieldDefinition, NodeDefinition } from '../src/components/node-inspector/definitions';
import { tx } from '../src/lib/i18n';

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

function fieldDefinition(field: RelionField, required: boolean): FieldDefinition {
  const help = field.help?.trim() || undefined;
  const base = {
    // RELION options live under `data.inputsValues`, the same place FlowGram's
    // own nodes keep theirs. A bare key would make the form write
    // `data.<variable>` and the canvas read `data.inputsValues.<variable>` -
    // an edit would silently do nothing.
    key: `inputsValues.${field.variable}`,
    variable: field.variable,
    label: cleanLabel(field.label || field.variable),
    // RELION's help is a paragraph per option; inline it bury the value, so
    // it travels as `tip` (hover next to the label). A short first clause
    // stays inline as a hint when the text is more than a full sentence.
    hint: undefined as string | undefined,
    tip: help,
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

    case 'input_node':
      // A slot picker over the project's .Nodes, not free text: the editor
      // renders RELION's own candidate list, typed by node_type.
      return { ...base, editor: 'node-input', nodeType: field.node_type, placeholder: field.pattern };

    case 'path':
      return { ...base, editor: 'path-input', placeholder: field.pattern };

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

/** The RELION variable a field key names (`inputsValues.nr_mpi` -> `nr_mpi`). */
function variableOf(key: string): string {
  return key.startsWith('inputsValues.') ? key.slice('inputsValues.'.length) : key;
}

/**
 * The section a field belongs to, from RELION's own job-window tabs.
 *
 * The schema already groups every variable under a tab ("I/O", the
 * algorithm's tab, "Run"). The form honours that grouping as collapsible
 * sections instead of one 80-row list, with the resource tab (MPI, threads,
 * queue, GPU) renamed and forced last so parameters read I/O → algorithm →
 * resources.
 */
const RESOURCE_TAB = 'Run';
const RESOURCE_SECTION = '计算资源';
const COMPUTE_TAB = 'Compute';
/** Tabs in the compute tail, before 计算资源; selection ranks compute the same. */
const COMPUTE_HEAD = new Set([COMPUTE_TAB]);

function sectionsFor(
  job: RelionJobType & { tabs?: Array<{ title: string; fields: string[] }> },
): { sectionOf: (variable: string) => string | undefined; order: string[] } {
  const tabs = job.tabs ?? [];
  const sectionOf = new Map<string, string>();
  const ioSet = new Set(['I/O']);
  const io: string[] = [];
  const middle: string[] = [];
  const compute: string[] = [];
  let hasResources = false;
  for (const tab of tabs) {
    const title =
      tab.title === RESOURCE_TAB
        ? RESOURCE_SECTION
        : tab.title === COMPUTE_TAB
        ? '计算'
        : tab.title;
    for (const variable of tab.fields) {
      if (!sectionOf.has(variable)) sectionOf.set(variable, title);
    }
    if (title === RESOURCE_SECTION) hasResources = true;
    else if (ioSet.has(tab.title)) {
      if (!io.includes(title)) io.push(title);
    } else if (COMPUTE_HEAD.has(tab.title)) {
      if (!compute.includes(title)) compute.push(title);
    } else if (!middle.includes(title)) middle.push(title);
  }
  // Fixed reading order regardless of where the C++ table put each tab:
  // I/O first, the algorithm tabs in their own order, compute second-to-last
  // (RELION's "Compute" GPU section, renamed 计算)，计算资源 last.
  const sorted = [...io, ...middle, ...compute];
  if (hasResources) sorted.push(RESOURCE_SECTION);
  return { sectionOf: (variable) => sectionOf.get(variable), order: sorted };
}

/**
 * One job type as a node definition.
 *
 * `output` returns an empty object schema: a RELION job's outputs are pipeline
 * nodes, not a value this node computes, so there is nothing honest to declare.
 */
export function jobDefinition(
  job: RelionJobType & { tabs?: Array<{ title: string; fields: string[] }> },
): NodeDefinition {
  const required = new Set(job.required ?? []);
  const { sectionOf, order } = sectionsFor(job);
  // The Run tab's other_args line is RELION's advanced panel. Naming the
  // flags the form does not already cover - with their usage - turns it from
  // a free box into a discoverable one. Long, so it rides the hover tip.
  const advanced = (job.cli_options ?? [])
    .filter((option) => !option.hidden && !option.used_by_form)
    .map((option) => `${option.flag}${option.usage ? `  ${option.usage}` : ''}`);
  const advancedTip = advanced.length
    ? `${tx('除表单外的可用参数（写入 other_args）：\n')}  ${advanced.join('\n  ')}`
    : undefined;
  // Sort by section (I/O → algorithm → resources); within a section the
  // schema's own field order stands. Fields with no tab stay first, before
  // every section, which is where shared switches like a variant's branch
  // selector belong.
  const rank = new Map(order.map((title, index) => [title, index]));
  return {
    fields: job.fields
      .map((field, index) => ({
        definition: fieldDefinition(field, required.has(field.variable)),
        index,
      }))
      .sort((a, b) => {
        const sectionA = sectionOf(variableOf(a.definition.key));
        const sectionB = sectionOf(variableOf(b.definition.key));
        const rankA = sectionA === undefined ? -1 : (rank.get(sectionA) ?? order.length);
        const rankB = sectionB === undefined ? -1 : (rank.get(sectionB) ?? order.length);
        return rankA - rankB || a.index - b.index;
      })
      .map(({ definition }) => ({
        ...definition,
        // other_args collects the flags this form does not expose; its tip
        // is where their vocabulary lives.
        tip:
          variableOf(definition.key) === 'other_args' && advancedTip
            ? [definition.tip, advancedTip].filter(Boolean).join('\n\n')
            : definition.tip,
        section: sectionOf(variableOf(definition.key)),
      })),
    output: () => ({ type: 'object', properties: {} }),
    aliases: [job.title, job.job_type],
  };
}

/** Node type id for a RELION job type. Namespaced so it cannot collide. */
export function nodeTypeFor(jobType: string): string {
  return `relion.${jobType}`;
}

/**
 * The full title a variant label needs in the palette.
 *
 * Variants of one job share the pipeline label (`relion.importtomo`); the
 * schema title disambiguates them ("Import (SerialEM tilt-series)").
 */
export function paletteTitle(job: RelionJobType): string {
  return job.title || job.job_type;
}

/** A one-line description from the job's first text field's help, or its kind. */
export function paletteDescription(job: RelionField[] | RelionJobType): string {
  const fields = (job as RelionJobType).fields ?? [];
  const first = fields.find((field) => field.help && field.kind !== 'boolean');
  if (first) {
    const help = (first.help ?? '').trim();
    return help.length > 90 ? `${help.slice(0, 88)}…` : help;
  }
  return `${fields.length} 个参数`;
}
