// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import type { PipelineGraph } from "@/lib/types";

/**
 * Lay out a RELION pipeline as a left-to-right layered graph.
 *
 * RELION's pipeline is a DAG *except* for refinement, which loops
 * (`Refine -> CTF refine -> Refine`). Longest-path ranking diverges on a cycle;
 * measured on a 13-job cryo-ET pipeline it produced 26 columns for 15 jobs. So
 * the graph is condensed into strongly connected components first, ranked as a
 * DAG, and each component is drawn as one node.
 *
 * The result is deliberately simple: fixed-width columns, so edges have a
 * predictable run, and the whole graph fits the same world coordinates the
 * canvas zooms.
 */

export const NODE_WIDTH = 208;
export const NODE_HEIGHT = 78;
export const MINI_WIDTH = 96;
export const MINI_HEIGHT = 38;
const COLUMN_GAP = 40;
const ROW_GAP = 14;
const PADDING = 24;

export interface LayoutNode {
  /** Process names in this node. More than one means a condensed cycle. */
  processes: string[];
  /** The process the node is labelled with. */
  primary: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LayoutEdge {
  from: string;
  to: string;
  /** SVG path in world coordinates. */
  path: string;
}

export interface Layout {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  /** World size, for fit-to-view and the scroll extent. */
  width: number;
  height: number;
  /** Process name -> the node it was placed in. */
  nodeOf: Map<string, LayoutNode>;
}

interface GraphInput {
  processes: { name: string }[];
  edges: {
    inputs: { node: string; process: string }[];
    outputs: { process: string; node: string }[];
  };
}

/** Collapse the edge list into process -> process dependencies. */
export function processEdges(graph: GraphInput): [string, string][] {
  const producerByNode = new Map<string, string>();
  for (const { process, node } of graph.edges.outputs) {
    producerByNode.set(node, process);
  }
  const seen = new Set<string>();
  const out: [string, string][] = [];
  for (const { node, process } of graph.edges.inputs) {
    const producer = producerByNode.get(node);
    // A node with no producer is external input, not an edge.
    if (!producer || producer === process) continue;
    const key = `${producer}\u0000${process}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push([producer, process]);
  }
  return out;
}

/** Tarjan's algorithm, iterative enough for a few hundred jobs. */
function strongComponents(
  names: string[],
  successors: Map<string, string[]>,
): string[][] {
  const index = new Map<string, number>();
  const low = new Map<string, number>();
  const onStack = new Set<string>();
  const stack: string[] = [];
  const components: string[][] = [];
  let counter = 0;

  // Explicit work stack: a real pipeline can be deep enough to overflow the
  // JS call stack, and a layout failure must not blank the canvas.
  for (const root of names) {
    if (index.has(root)) continue;
    const work: { node: string; next: number }[] = [{ node: root, next: 0 }];
    while (work.length > 0) {
      const frame = work[work.length - 1];
      const v = frame.node;

      if (frame.next === 0) {
        index.set(v, counter);
        low.set(v, counter);
        counter += 1;
        stack.push(v);
        onStack.add(v);
      }

      const children = successors.get(v) ?? [];
      if (frame.next < children.length) {
        const w = children[frame.next];
        frame.next += 1;
        if (!index.has(w)) {
          work.push({ node: w, next: 0 });
        } else if (onStack.has(w)) {
          low.set(v, Math.min(low.get(v)!, index.get(w)!));
        }
        continue;
      }

      work.pop();
      if (work.length > 0) {
        const parent = work[work.length - 1].node;
        low.set(parent, Math.min(low.get(parent)!, low.get(v)!));
      }
      if (low.get(v) === index.get(v)) {
        const members: string[] = [];
        for (;;) {
          const w = stack.pop()!;
          onStack.delete(w);
          members.push(w);
          if (w === v) break;
        }
        components.push(members);
      }
    }
  }
  return components;
}

export function layoutPipeline(graph: GraphInput): Layout {
  const names = graph.processes.map((process) => process.name);
  const edges = processEdges(graph);

  const successors = new Map<string, string[]>(names.map((n) => [n, []]));
  for (const [from, to] of edges) successors.get(from)?.push(to);

  // 1. Condense cycles, so refinement loops become one node.
  const components = strongComponents(names, successors);
  const componentOf = new Map<string, number>();
  components.forEach((members, i) =>
    members.forEach((name) => componentOf.set(name, i)),
  );

  const predecessors = new Map<number, Set<number>>(
    components.map((_, i) => [i, new Set<number>()]),
  );
  for (const [from, to] of edges) {
    const a = componentOf.get(from)!;
    const b = componentOf.get(to)!;
    if (a !== b) predecessors.get(b)!.add(a);
  }

  // 2. Longest-path rank on the condensation: a job sits one column right of
  //    its deepest dependency, which is what makes the flow read left-to-right.
  const rank = new Array<number>(components.length).fill(0);
  let changed = true;
  // Bounded by node count: the condensation is a DAG, so it converges.
  for (let pass = 0; changed && pass <= components.length; pass += 1) {
    changed = false;
    for (let i = 0; i < components.length; i += 1) {
      let next = 0;
      for (const p of predecessors.get(i)!) next = Math.max(next, rank[p] + 1);
      if (next !== rank[i]) {
        rank[i] = next;
        changed = true;
      }
    }
  }

  // 3. Order within a column by the mean column of its predecessors, which
  //    keeps fan-out readable without a full crossing-minimisation pass.
  const columns = new Map<number, number[]>();
  for (let i = 0; i < components.length; i += 1) {
    const key = rank[i];
    if (!columns.has(key)) columns.set(key, []);
    columns.get(key)!.push(i);
  }
  const rankOrder = [...columns.keys()].sort((a, b) => a - b);
  const columnOfRank = new Map(rankOrder.map((r, i) => [r, i]));
  for (const key of rankOrder) {
    columns.get(key)!.sort((a, b) => {
      const barycentre = (i: number) => {
        const preds = [...predecessors.get(i)!];
        if (preds.length === 0) return 0;
        return (
          preds.reduce((sum, p) => sum + columnOfRank.get(rank[p])!, 0) /
          preds.length
        );
      };
      return barycentre(a) - barycentre(b) || a - b;
    });
  }

  // 4. Place: fixed column width, rows centred vertically in their column.
  const tallest = Math.max(...rankOrder.map((r) => columns.get(r)!.length), 1);
  const width =
    PADDING * 2 +
    Math.max(1, rankOrder.length) * NODE_WIDTH +
    Math.max(0, rankOrder.length - 1) * COLUMN_GAP;
  const height = Math.max(
    320,
    PADDING * 2 + tallest * NODE_HEIGHT + (tallest - 1) * ROW_GAP,
  );

  const nodes: LayoutNode[] = [];
  const nodeOf = new Map<string, LayoutNode>();
  rankOrder.forEach((rankKey, columnIndex) => {
    const members = columns.get(rankKey)!;
    const total = members.length * NODE_HEIGHT + (members.length - 1) * ROW_GAP;
    let y = Math.max(PADDING, (height - total) / 2);
    for (const component of members) {
      const processes = components[component].slice().sort();
      // Label a condensed loop with the job that closes it (the last one
      // started), which is the one the user last touched.
      const primary = processes[processes.length - 1];
      const node: LayoutNode = {
        processes,
        primary,
        x: PADDING + columnIndex * (NODE_WIDTH + COLUMN_GAP),
        y,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
      };
      nodes.push(node);
      for (const name of processes) nodeOf.set(name, node);
      y += NODE_HEIGHT + ROW_GAP;
    }
  });

  // 5. Edges, de-duplicated after condensation and drawn as horizontal-tangent
  //    curves so they leave and enter nodes parallel to the reading direction.
  const drawn = new Set<string>();
  const layoutEdges: LayoutEdge[] = [];
  for (const [from, to] of edges) {
    const a = nodeOf.get(from);
    const b = nodeOf.get(to);
    if (!a || !b || a === b) continue;
    const key = `${a.primary}\u0000${b.primary}`;
    if (drawn.has(key)) continue;
    drawn.add(key);

    const x1 = a.x + a.width;
    const y1 = a.y + a.height / 2;
    const x2 = b.x;
    const y2 = b.y + b.height / 2;
    const mid = (x1 + x2) / 2;
    layoutEdges.push({
      from: a.primary,
      to: b.primary,
      path: `M${x1} ${y1} C${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
    });
  }

  return { nodes, edges: layoutEdges, width, height, nodeOf };
}

/**
 * Fit a graph into a viewport without letting cards become unreadable.
 *
 * Measured: below ~72% a card's 12px title renders under 9px and stops being
 * readable at a glance, so `fit` stops there and the canvas pans instead.
 * Returning a floor rather than shrinking to zero is the difference between an
 * overview and a grid of coloured chips.
 */
export const MIN_READABLE_SCALE = 0.72;

/**
 * The scale the whole graph fits at, ignoring readability.
 *
 * Separate from :func:`fitScale` so the canvas can ask "would fitting be
 * legible?" without a second magic threshold: if the natural fit is at or above
 * `MIN_READABLE_SCALE`, fitting shows the whole pipeline and is worth doing.
 */
export function naturalFitScale(
  layoutWidth: number,
  layoutHeight: number,
  viewportWidth: number,
  viewportHeight: number,
): number {
  if (layoutWidth <= 0 || layoutHeight <= 0) return 1;
  return Math.min(
    viewportWidth / layoutWidth,
    viewportHeight / layoutHeight,
    1,
  );
}

export function fitScale(
  layoutWidth: number,
  layoutHeight: number,
  viewportWidth: number,
  viewportHeight: number,
): number {
  return Math.max(
    MIN_READABLE_SCALE,
    naturalFitScale(layoutWidth, layoutHeight, viewportWidth, viewportHeight),
  );
}

/** Below this a node renders as the zoomed-out status chip. */
export const MINI_SCALE = 0.55;

/** A stable hue per node type, kept in a calm band so edges never shout. */
export function edgeColour(typeLabel: string): string {
  let hash = 0;
  for (let i = 0; i < typeLabel.length; i += 1) {
    hash = (hash * 31 + typeLabel.charCodeAt(i)) | 0;
  }
  const hue = 232 + (Math.abs(hash) % 34);
  return `oklch(0.65 0.074 ${hue})`;
}

export type { PipelineGraph };
