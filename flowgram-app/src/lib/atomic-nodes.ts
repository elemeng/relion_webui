/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

/**
 * The shape a catalogue entry from `catalog/*.json` has.
 *
 * This module used to export `atomicNodes` - 29 FlowGram data-processing nodes
 * (JSON mapping, list operations, text transforms). They were hidden from the
 * palette in this branch and no RELION pipeline can produce them, so the data
 * file and its loader are gone; only the type remains, because the control-node
 * catalogue is the same shape.
 */

import type { FieldDefinition } from '../components/node-inspector/definitions';
import type { Data, Schema } from '../components/node-inspector/model';

export interface AtomicNodeDefinition {
  type: string;
  title: string;
  group: string;
  description: string;
  fields: FieldDefinition[];
  defaults: Data;
  inputSchema: Schema;
  outputSchema: Schema;
  outputsByOperation?: Record<string, Schema>;
  aliases?: string[];
}
