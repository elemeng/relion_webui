/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

export interface NodePreviewToken {
  kind: 'text' | 'reference' | 'field' | 'code' | 'resource' | 'count';
  text: string;
  detail?: string;
  description?: string;
}
export interface NodePreviewRow {
  label?: string;
  tokens: NodePreviewToken[];
}

/**
 * Which fields a step node's card previews.
 *
 * Empty: every remaining built-in step node (condition, delay) has its own
 * presentation branch, and RELION job nodes fall back to their first two
 * visible fields - so there is no table to keep here any more. The 29 data
 * nodes this used to describe are gone.
 */
export const summaryFields: Record<string, string[]> = {};

// Geometry is derived from declared content slots, never DOM measurements or locale.
export function previewRowCount(type: string): number {
  if (['block-start', 'block-end', 'control.break', 'control.continue'].includes(type)) return 0;
  if (
    [
      'start',
      'end',
      'assign',
      'wait',
      'delay',
      'condition',
      'control.switch',
      'control.parallel',
      'control.join',
      'control.merge-branches',
      'template',
      'json-parse',
      'json-stringify',
    ].includes(type)
  )
    return 1;
  return Math.min(2, summaryFields[type]?.length || 2);
}
