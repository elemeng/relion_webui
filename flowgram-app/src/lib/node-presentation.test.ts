/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */
import { describe, expect, it } from 'vitest';

import { presentNode, previewPredicate, type NodeResources } from './node-presentation';
import { staticPorts } from './control-nodes';
import type { WorkflowNode } from './types';
import { tx } from './i18n';

const constant = (content: unknown) => ({ type: 'constant', content });
const resources: NodeResources = {
  names: { start: '采购单' },
  workflows: { dependency: '审核订单' },
};
const node = (type: string, data: WorkflowNode['data'] = {}): WorkflowNode => ({
  id: 'node',
  type,
  data,
});
describe('canvas node presentation', () => {
  it('includes units for waits, preserves zero and never treats references as numbers', () => {
    for (const [amount, display] of [
      [0, '0 ms'],
      [40, '40 ms'],
      [1500, '1.5 s'],
    ] as const) {
      const view = presentNode(
        node('wait', { inputsValues: { durationMs: constant(amount) } }),
        resources
      );
      expect(view.rows[0].tokens).toEqual([{ kind: 'code', text: display }]);
    }
    expect(
      presentNode(
        node('wait', {
          inputsValues: {
            durationMs: { type: 'ref', content: ['start', 'delay'] },
          },
        }),
        resources
      ).rows[0].tokens
    ).toEqual([
      { kind: 'reference', text: '采购单', detail: 'delay' },
      { kind: 'text', text: 'ms' },
    ]);
  });
  it('preserves grouping in nested condition previews', () => {
    const comparison = (left: number) => ({
      left: constant(left),
      operator: 'eq',
      right: constant(1),
    });
    const tokens = previewPredicate(
      {
        combinator: 'and',
        rules: [{ combinator: 'or', rules: [comparison(1), comparison(2)] }, comparison(3)],
      },
      resources
    );
    expect(tokens.map((token) => token.text).join(' ')).toBe(
      `( 1 = 1 ${tx('或')} 2 = 1 ) ${tx('且')} 3 = 1`
    );
  });
});

describe('static canvas geometry', () => {
  it('uses semantic branch rows on the corresponding side while preserving port IDs', () => {
    const fork = node('control.switch', {
      branches: [
        { id: 'high', label: '高额' },
        { id: 'low', label: '普通' },
      ],
    });
    const join = { ...node('control.join', { forkId: fork.id }), id: 'join' };
    const graph = { nodes: [fork, join], edges: [] };
    const first = staticPorts(fork, graph),
      second = staticPorts(join, graph);
    expect(
      first.ports
        .filter((port) => port.type === 'output')
        .map((port) => [port.portID, port.locationConfig.top])
    ).toEqual([
      ['high', 102],
      ['low', 146],
      ['else', 190],
    ]);
    expect(first.ports.find((port) => port.type === 'input')!.locationConfig.top).toBe(first.height / 2);
    expect(
      second.ports.filter((port) => port.type === 'input').map((port) => port.locationConfig.top)
    ).toEqual([102, 146, 190]);
    expect(second.ports.find((port) => port.type === 'output')!.locationConfig.top).toBe(second.height / 2);
    expect(first.ports.every((port) => !('targetElement' in port))).toBe(true);
  });
  it('uses compact content slots and aligns step ports without measuring content', () => {
    const graph = { nodes: [], edges: [] };
    const wait = staticPorts(node('wait'), graph);
    const http = staticPorts(node('http'), graph);
    expect(wait.height).toBe(84);
    expect(http.height).toBe(110);
    expect(wait.ports.map((port) => port.locationConfig.top)).toEqual([42, 42]);
    expect(http.ports.map((port) => port.locationConfig.top)).toEqual([55, 55]);
    for (const type of ['start', 'end', 'block-start', 'block-end', 'control.break', 'control.continue']) {
      const layout = staticPorts(node(type), graph);
      expect(layout.ports.every((port) => port.locationConfig.top === layout.height / 2)).toBe(true);
    }
    expect(staticPorts(node('wait', { title: 'Very long name '.repeat(20) }), graph)).toEqual(wait);
  });
  it('keeps terminal controls compact and never invents a forward output', () => {
    for (const type of ['end', 'block-end', 'control.break', 'control.continue'])
      expect(
        staticPorts(node(type), { nodes: [], edges: [] }).ports.filter(
          (port) => port.type === 'output'
        )
      ).toEqual([]);
    expect(staticPorts(node('block-start'), { nodes: [], edges: [] })).toMatchObject({
      width: 168,
      height: 36,
    });
    for (const type of ['loop', 'control.repeat']) {
      const loop = staticPorts(node(type), { nodes: [], edges: [] });
      expect(loop.ports.map((port) => port.locationConfig)).toEqual([
        { left: 0, top: '50%' }, { right: 0, top: '50%' },
      ]);
      expect(loop.ports.every((port) => !('targetElement' in port))).toBe(true);
    }
  });
});
