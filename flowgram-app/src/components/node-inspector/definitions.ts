/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

import type { WorkflowNode } from '../../lib/types';
import { localized } from '../../lib/i18n';
import { controlNodes } from '../../lib/control-nodes';
import { asValue, readPath, type Data, type Schema } from './model';

export type EditorId =
  | 'value'
  | 'select'
  | 'mapping'
  | 'schema'
  | 'conditions'
  | 'resource'
  | 'tools'
  | 'number'
  | 'key-value'
  | 'paths'
  | 'records'
  | 'predicate'
  | 'branches'
  | 'branch-mappings'
  | 'node-select'
  | 'node-input'
  | 'path-input';
export interface FieldDefinition {
  key: string;
  label: string;
  editor: EditorId;
  type?: string;
  required?: boolean;
  hint?: string;
  /** Extended help shown as a hover tip next to the label (RELION). */
  tip?: string;
  /** Grey placeholder for path inputs (RELION's C++ file pattern). */
  placeholder?: string;
  /** The .Nodes type an input_node slot accepts. */
  nodeType?: string;
  multiline?: boolean;
  defaultValue?: unknown;
  valueType?: string;
  resource?: 'model' | 'workflow';
  scope?: 'children' | 'items';
  columns?: Array<{ key: string; label: string; choices?: Array<[string, string]> }>;
  operators?: Array<[string, string]>;
  tools?: string[];
  row?: string;
  rowLabel?: string;
  choices?: Array<[string, string]>;
  min?: number;
  max?: number;
  section?: string;
  visible?: { field: string; values: unknown[] };
}
export interface NodeDefinition {
  fields: FieldDefinition[];
  output: (data: Data) => Schema;
  aliases?: string[];
}
const value = (
  key: string,
  label: string,
  type = 'string',
  extra: Partial<FieldDefinition> = {}
): FieldDefinition => ({ key: `inputsValues.${key}`, label, type, editor: 'value', ...extra });
const mapping: FieldDefinition = localized({
  key: 'inputsValues',
  label: '输出字段',
  editor: 'mapping',
});
const mappedOutput = (data: Data): Schema => ({
  type: 'object',
  properties: Object.fromEntries(
    Object.entries((data.inputsValues || {}) as Data).map(([key, v]) => {
      const content = asValue(v).content;
      return [
        key,
        {
          type:
            asValue(v).type !== 'constant'
              ? undefined
              : content === null
              ? 'null'
              : Array.isArray(content)
              ? 'array'
              : typeof content,
        },
      ];
    })
  ),
});
export const nodeDefinitions: Record<string, NodeDefinition> = localized({
  start: {
    fields: [
      { key: 'outputs', label: '输入字段', editor: 'schema', hint: '定义调用工作流时提供的数据。' },
    ],
    output: (d) => (d.outputs as Schema) || { type: 'object' },
  },
  'block-start': {
    fields: [],
    output: (d) =>
      (d.outputs as Schema) || {
        type: 'object',
        properties: { item: {}, index: { type: 'integer' } },
      },
  },
  end: { fields: [mapping], output: mappedOutput },
  'block-end': { fields: [mapping], output: mappedOutput },
  condition: {
    fields: [
      {
        key: 'conditions',
        label: '分支规则',
        editor: 'conditions',
        hint: '按顺序匹配，首个命中的分支执行；未命中时进入否则分支。',
      },
    ],
    output: () => ({ type: 'object', properties: { branch: { type: 'string' } } }),
  },
  delay: {
    fields: [
      {
        ...value('durationMs', '等待时长', 'integer', {
          required: true,
          hint: '单位为毫秒，单次最长 30 秒。',
        }),
        min: 0,
        max: 30_000,
      },
    ],
    output: () => ({ type: 'object', properties: { durationMs: { type: 'integer' } } }),
  },
});
controlNodes.forEach((node) => {
  nodeDefinitions[node.type] = {
    fields: node.fields,
    aliases: node.aliases,
    output: (data) => {
      if (node.type === 'loop')
        return {
          type: 'object',
          properties: {
            ...node.outputSchema.properties,
            ...Object.fromEntries(
              Object.keys((data.loopOutputs || {}) as Data).map((key) => [key, { type: 'array' }])
            ),
          },
        };
      if (node.type === 'control.merge-branches')
        return (data.outputs as Schema) || { type: 'object' };
      const operation =
        asValue((data.inputsValues as Data)?.operation).content ??
        asValue((node.defaults.inputsValues as Data)?.operation).content;
      return node.outputsByOperation?.[String(operation)] || node.outputSchema;
    },
  };
});
/**
 * Definitions contributed from outside this file.
 *
 * The table above covers FlowGram's own node types. RELION's job types are
 * generated at runtime from `/fg/api/job-schema` (see `lib/register-relion.ts`) and
 * registered here, so the stock inspector renders their forms without this
 * module knowing what a RELION job is.
 *
 * This is the only hook added to the vendored code: a lookup plus a setter.
 * No component, no styling, no layout.
 */
const externalDefinitions = new Map<string, NodeDefinition>();

export function registerNodeDefinitions(definitions: Record<string, NodeDefinition>): void {
  for (const [type, definition] of Object.entries(definitions)) {
    externalDefinitions.set(type, definition);
  }
}

export function definitionFor(node: WorkflowNode): NodeDefinition {
  return (
    nodeDefinitions[node.type] ||
    externalDefinitions.get(node.type) || { fields: [], output: () => ({ type: 'object' }) }
  );
}
export function visibleFields(node: WorkflowNode) {
  return definitionFor(node).fields.filter((field) => {
    if (!field.visible) return true;
    const source = definitionFor(node).fields.find(
      (candidate) =>
        candidate.key === field.visible!.field ||
        candidate.key === `inputsValues.${field.visible!.field}`
    );
    const value = source
      ? readPath(node.data, source.key.split('.'))
      : (node.data.inputsValues as Data)?.[field.visible.field];
    return field.visible.values.includes(asValue(value).content ?? source?.defaultValue);
  });
}
