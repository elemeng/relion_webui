// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useStore,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { Boxes, Maximize2, Minus, Plus } from "lucide-react";

import { JobCard, MiniJobCard } from "@/components/JobCard";
import type { NodeAction } from "@/components/NodeMenu";
import { DraftStrip, type DraftChip } from "@/components/DraftStrip";
import {
  MIN_READABLE_SCALE,
  MINI_HEIGHT,
  MINI_SCALE,
  MINI_WIDTH,
  NODE_HEIGHT,
  NODE_WIDTH,
  edgeColour,
  layoutPipeline,
  type LayoutNode,
} from "@/lib/layout";
import type { PipelineGraph, PipelineProcess } from "@/lib/types";

const MIN_SCALE = 0.25;
const MAX_SCALE = 1.6;

/** Node data carried through React Flow; the card renders from this. */
interface NodeData extends Record<string, unknown> {
  layout: LayoutNode;
  process: PipelineProcess;
  selected: boolean;
  dimmed: boolean;
  stacked: boolean;
  onOpen: () => void;
  onAction?: (action: NodeAction) => void;
}

/** Edge data: the colour and emphasis are ours, not React Flow's. */
interface EdgeData extends Record<string, unknown> {
  colour: string;
  active: boolean;
  dimmed: boolean;
}

interface PipelineCanvasProps {
  graph: PipelineGraph;
  selected: string | null;
  onSelect: (process: PipelineProcess | null) => void;
  onOpen: (process: PipelineProcess) => void;
  /** Jobs the user filtered out stay in place as outlines. */
  hidden?: Set<string>;
  /**
   * Width taken by a panel on the right, so `fit` and centring use the space
   * that is actually visible rather than sliding content under the panel.
   */
  reservedRight?: number;
  /** Jobs being built. They sit in a strip above the graph until queued. */
  drafts?: DraftChip[];
  /** Rendered in the corner: the add-a-job control. */
  addControl?: React.ReactNode;
  /** Per-node menu actions, handled by the shell. */
  onNodeAction?: (process: PipelineProcess, action: NodeAction) => void;
}

/**
 * The project's pipeline as a graph.
 *
 * This is the primary surface: RELION's data model is a graph, and every
 * downstream job is started by naming an upstream node. A list of job names
 * throws that away, which is why the canvas is the default view rather than a
 * sidebar.
 *
 * React Flow owns the viewport - pan, wheel zoom, the transform, and edge
 * routing that stays attached to its nodes at any zoom. `layout.ts` still owns
 * where each node *is*: the DAG is laid out deterministically and nodes are not
 * draggable, so the left-to-right reading order cannot drift.
 *
 * Interaction: wheel zooms, drag pans, clicking a card opens it, clicking the
 * background deselects. Those match the app's global shortcuts, which is why
 * React Flow's own keyboard bindings are all disabled below.
 */
export function PipelineCanvas(props: PipelineCanvasProps) {
  // The viewport helpers need the provider, so the real canvas is a child.
  return (
    <ReactFlowProvider>
      <PipelineCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function PipelineCanvasInner({
  graph,
  selected,
  onSelect,
  onOpen,
  hidden,
  reservedRight = 0,
  drafts = [],
  addControl,
  onNodeAction,
}: PipelineCanvasProps) {
  const layout = useMemo(() => layoutPipeline(graph), [graph]);
  const flow = useReactFlow();

  // `true` until the opening view has been chosen, so a streaming graph update
  // does not snap the viewport back while the user is reading.
  const framedRef = useRef(false);
  const [mini, setMini] = useState(false);
  const [zoom, setZoom] = useState(1);

  // Callbacks kept in refs. They are recreated on every render by the shell, and
  // React Flow compares node/edge arrays by identity: if a fresh closure leaked
  // into the `data` of every node, the arrays would be rebuilt on each render
  // and React Flow would keep resetting its internal node store - which silently
  // drops edges (they render only once both endpoints are measured).
  const onOpenRef = useRef(onOpen);
  const onActionRef = useRef(onNodeAction);
  useEffect(() => {
    onOpenRef.current = onOpen;
    onActionRef.current = onNodeAction;
  });

  const nodes = useMemo<Node<NodeData>[]>(() => {
    const built: Node<NodeData>[] = [];
    for (const node of layout.nodes) {
      const process = graph.processes.find((p) => p.name === node.primary);
      if (!process) continue;
      built.push({
        id: node.primary,
        type: mini ? "mini" : "job",
        position: { x: node.x, y: node.y },
        width: mini ? MINI_WIDTH : NODE_WIDTH,
        height: mini ? MINI_HEIGHT : NODE_HEIGHT,
        draggable: false,
        // `selectable` must not be false: React Flow writes pointer-events:none
        // onto a node that is neither draggable nor selectable, which makes the
        // card click-through. Selection here means "the card was clicked", and
        // the visual state comes from the `selected` prop, not from React Flow.
        selectable: true,
        connectable: false,
        data: {
          layout: node,
          process,
          selected: selected === node.primary,
          dimmed: hidden?.has(node.primary) ?? false,
          stacked: node.processes.length > 1,
          // Stable identity: read through the ref so `nodes` does not change
          // when the shell passes new closures.
          onOpen: () => onOpenRef.current(process),
          onAction: (action: NodeAction) =>
            onActionRef.current?.(process, action),
        },
      });
    }
    return built;
  }, [graph.processes, hidden, layout.nodes, mini, selected]);

  const edges = useMemo<Edge<EdgeData>[]>(() => {
    const byName = new Map(graph.processes.map((p) => [p.name, p]));
    return layout.edges.map((edge) => {
      const source = byName.get(edge.from);
      const active = selected === edge.from || selected === edge.to;
      const dimmed =
        (hidden?.has(edge.from) ?? false) || (hidden?.has(edge.to) ?? false);
      return {
        id: `${edge.from}->${edge.to}`,
        source: edge.from,
        target: edge.to,
        type: "smoothstep",
        data: {
          colour: active ? "var(--signal)" : edgeColour(source?.type_label ?? ""),
          active,
          dimmed,
        },
        // React Flow routes it; the path is never built by hand.
        style: {
          stroke: active ? "var(--signal)" : edgeColour(source?.type_label ?? ""),
          strokeWidth: active ? 1.6 : 1,
          opacity: dimmed ? 0.25 : 1,
        },
        // Arrows need a marker in world units so they scale with the canvas.
        markerEnd: {
          type: "arrowclosed" as const,
          width: 12,
          height: 12,
          color: active ? "var(--signal)" : edgeColour(source?.type_label ?? ""),
        },
      };
    });
  }, [graph.processes, hidden, layout.edges, selected]);

  // The mini card is smaller than a full one, so the switchover is driven by the
  // live zoom rather than by a re-render of the whole graph on every wheel tick.
  const liveZoom = useStore((state) => state.transform[2]);
  useEffect(() => {
    const wantMini = liveZoom < MINI_SCALE;
    setMini((current) => (current === wantMini ? current : wantMini));
    setZoom((current) => (current === liveZoom ? current : liveZoom));
  }, [liveZoom]);

  const fit = useCallback(() => {
    void flow.fitView({
      minZoom: MIN_READABLE_SCALE,
      maxZoom: 1,
      duration: 200,
    });
  }, [flow]);

  const centre = useCallback(() => {
    const node = selected ? layout.nodeOf.get(selected) : undefined;
    if (!node) {
      fit();
      return;
    }
    const width = Math.max(0, window.innerWidth - reservedRight);
    void flow.setCenter(
      node.x + node.width / 2,
      node.y + node.height / 2,
      { zoom: 1, duration: 200 },
    );
    void width;
  }, [fit, flow, layout.nodeOf, reservedRight, selected]);

  const zoomBy = useCallback(
    (factor: number) => {
      const current = flow.getZoom();
      const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, current * factor));
      void flow.zoomTo(next, { duration: 150 });
    },
    [flow],
  );

  // Opening view: show the whole shape only when it would still be legible.
  // Otherwise open at 100% on the job the user is looking at, because a field
  // of 7px chips is not an overview of anything. Fit (F) still offers the
  // shrunk view on request.
  useEffect(() => {
    if (framedRef.current || layout.nodes.length === 0) return;
    framedRef.current = true;
    const viewport = flow.getViewport();
    const natural = Math.min(
      (viewport.zoom * window.innerWidth) / Math.max(1, layout.width),
      (viewport.zoom * window.innerHeight) / Math.max(1, layout.height),
      1,
    );
    if (natural >= MIN_READABLE_SCALE) {
      void flow.fitView({ minZoom: MIN_READABLE_SCALE, maxZoom: 1 });
      return;
    }
    const target =
      layout.nodes.find((node) => node.primary === selected) ??
      layout.nodes[layout.nodes.length - 1];
    if (target) {
      void flow.setCenter(
        target.x + target.width / 2,
        target.y + target.height / 2,
        { zoom: 1 },
      );
    }
  }, [flow, layout.height, layout.nodes, layout.width, selected]);

  const nodeTypes = useMemo(
    () => ({ job: JobNode, mini: MiniNode }),
    [],
  );

  if (graph.processes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-sm text-center">
          <Boxes className="mx-auto mb-3 size-6 text-ink-faint" />
          <p className="t-ui font-medium">No jobs yet</p>
          <p className="mx-auto mt-1.5 max-w-xs t-body leading-relaxed text-ink-muted">
            Use the <span className="font-semibold">+</span> button to build
            one. It appears here as soon as it is registered in{" "}
            <code className="font-mono t-label">default_pipeline.star</code>.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full overflow-hidden">
      {drafts.length > 0 && (
        // Centred by the flex row, not by a translated fixed-width box: the
        // strip should be as wide as its chips.
        <div className="pointer-events-none absolute inset-x-0 top-2.5 z-10 flex justify-center px-3">
          <DraftStrip
            drafts={drafts}
            className="pointer-events-auto max-w-full rounded-full border border-rule bg-base-100 px-2 py-1 shadow-sm"
          />
        </div>
      )}

      {/* The dot grid stays on this wrapper and does not pan with the world, so
          React Flow's own background stays off. */}
      <div className="canvas-grid absolute inset-0">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          minZoom={MIN_SCALE}
          maxZoom={MAX_SCALE}
          nodesDraggable={false}
          nodesConnectable={false}
          // Must stay true: React Flow writes `pointer-events: none` onto a node
          // when it is neither selectable nor draggable, and the card inside
          // becomes click-through - clicks land on the pane instead. Our cards
          // handle their own click, so this only re-enables hit-testing.
          elementsSelectable
          // Nothing here is a React Flow selection; the app's `selected` prop
          // drives the card styling.
          selectionOnDrag={false}
          selectNodesOnDrag={false}
          // The app owns these keys globally (App.tsx); React Flow must not
          // steal Space, Delete, arrows or Escape.
          deleteKeyCode={null}
          selectionKeyCode={null}
          multiSelectionKeyCode={null}
          panActivationKeyCode={null}
          zoomActivationKeyCode={null}
          // Performances/QoL: no double-click zoom, no help lines for a graph
          // nobody connects by hand.
          zoomOnDoubleClick={false}
          edgesFocusable={false}
          nodesFocusable={false}
          proOptions={{ hideAttribution: false }}
          onPaneClick={() => onSelect(null)}
          className="[&_.react-flow__pane]:cursor-grab [&_.react-flow__pane.dragging]:cursor-grabbing"
        />
      </div>

      {addControl && (
        <div className="absolute bottom-3 left-3 z-0">{addControl}</div>
      )}

      <CanvasControls
        scale={zoom}
        onZoomIn={() => zoomBy(1.25)}
        onZoomOut={() => zoomBy(0.8)}
        onFit={fit}
        onCentre={centre}
      />
    </div>
  );
}

/** A full job card as a React Flow node. The card itself is unchanged. */
function JobNode({ data }: NodeProps<Node<NodeData>>) {
  const { layout, process, selected, dimmed, stacked, onOpen, onAction } = data;
  return (
    <>
      {/* Handles are where edges attach. They are invisible (the CSS in
          index.css zeroes and hides them): nothing here connects by hand, but
          React Flow still needs both ends to route an edge. */}
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <JobCard
        process={process}
        selected={selected}
        dimmed={dimmed}
        stackedCount={stacked ? layout.processes.length - 1 : 0}
        onOpen={onOpen}
        onAction={onAction}
        // Dragging a card onto an input field fills that field from this job's
        // outputs. It is the graph equivalent of RELION's "input to this job"
        // browser, done in the direction people expect in a canvas.
        draggable
      />
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </>
  );
}

/** The glyph-only card used when zoomed out past reading size. */
function MiniNode({ data }: NodeProps<Node<NodeData>>) {
  const { process, selected, dimmed, onOpen } = data;
  return (
    <>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <MiniJobCard
        process={process}
        selected={selected}
        dimmed={dimmed}
        onOpen={onOpen}
      />
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </>
  );
}

function CanvasButton({
  onClick,
  label,
  children,
}: {
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="grid size-7 place-items-center rounded-selector text-ink-muted hover:bg-base-200 hover:text-base-content"
      title={label}
      aria-label={label}
    >
      {children}
    </button>
  );
}

function CanvasControls({
  scale,
  onZoomIn,
  onZoomOut,
  onFit,
  onCentre,
}: {
  scale: number;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFit: () => void;
  onCentre: () => void;
}) {
  return (
    <div className="absolute right-3 bottom-3 flex items-center gap-0.5 rounded-field border border-rule bg-base-100 p-0.5 shadow-sm">
      <CanvasButton onClick={onZoomOut} label="Zoom out">
        <Minus className="size-3.5" />
      </CanvasButton>
      <button
        type="button"
        onClick={onFit}
        className="w-10 rounded-selector py-1 text-center font-mono t-meta text-ink-muted hover:bg-base-200"
        title="Reset to fit (F)"
      >
        {Math.round(scale * 100)}%
      </button>
      <CanvasButton onClick={onZoomIn} label="Zoom in">
        <Plus className="size-3.5" />
      </CanvasButton>
      <span className="mx-0.5 h-4 w-px bg-rule" />
      <button
        type="button"
        data-canvas-centre
        onClick={onCentre}
        className="grid size-7 place-items-center rounded-selector text-ink-muted hover:bg-base-200 hover:text-base-content"
        title="Centre on selection (T)"
      >
        <Boxes className="size-3.5" />
      </button>
      <button
        type="button"
        data-canvas-fit
        onClick={onFit}
        className="grid size-7 place-items-center rounded-selector text-ink-muted hover:bg-base-200 hover:text-base-content"
        title="Fit to view (F)"
      >
        <Maximize2 className="size-3.5" />
      </button>
    </div>
  );
}

export { NODE_HEIGHT, NODE_WIDTH };
