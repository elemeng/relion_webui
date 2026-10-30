import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Boxes, Maximize2, Minus, Plus } from "lucide-react";

import { JobCard, MiniJobCard } from "@/components/JobCard";
import type { NodeAction } from "@/components/NodeMenu";
import { DraftStrip, type DraftChip } from "@/components/DraftStrip";
import { cn } from "@/lib/utils";
import {
  MIN_READABLE_SCALE,
  MINI_SCALE,
  NODE_HEIGHT,
  NODE_WIDTH,
  edgeColour,
  fitScale,
  layoutPipeline,
  naturalFitScale,
  type LayoutNode,
} from "@/lib/layout";
import type { PipelineGraph, PipelineProcess } from "@/lib/types";

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

interface View {
  x: number;
  y: number;
  scale: number;
}

const MIN_SCALE = 0.25;
const MAX_SCALE = 1.6;

/** Short, stable per-job secondary text: the directory RELION wrote. */

/**
 * The project's pipeline as a graph.
 *
 * This is the primary surface: RELION's data model is a graph, and every
 * downstream job is started by naming an upstream node. A list of job names
 * throws that away, which is why the canvas is the default view rather than a
 * sidebar.
 *
 * Interaction: wheel zooms, drag pans, click selects, double-click or Enter
 * opens. Selection is the only state, so a click on the background deselects.
 */
export function PipelineCanvas({
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
  const byName = useMemo(
    () => new Map(graph.processes.map((process) => [process.name, process])),
    [graph],
  );

  const viewportRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>({ x: 0, y: 0, scale: 1 });
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [panning, setPanning] = useState(false);
  const panRef = useRef<{
    x: number;
    y: number;
    originX: number;
    originY: number;
  } | null>(null);
  // Set once the first layout is centred, so a streaming update does not snap
  // the view back while the user is reading.
  const centredRef = useRef(false);

  // Measure synchronously before the first paint, then keep up with resizes.
  // Waiting only for the observer leaves the first frame at scale 1 with a
  // too-wide graph, which reads as a layout bug rather than a settled view.
  useLayoutEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const measure = () =>
      setSize({ width: element.clientWidth, height: element.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const visibleWidth = Math.max(0, size.width - reservedRight);

  const centreOn = useCallback(
    (node: LayoutNode | undefined, animate = false) => {
      if (!node) return;
      setView({
        scale: 1,
        x: Math.round(visibleWidth / 2 - (node.x + node.width / 2)),
        y: Math.round(size.height / 2 - (node.y + node.height / 2)),
      });
      // The transition is CSS; the values themselves are final here.
      void animate;
    },
    [size.height, visibleWidth],
  );

  // First render: show the pipeline at a size that can be read.
  //
  // The test is whether the graph's *natural* fit is legible, not whether
  // `fitScale` returned something above a floor: `fitScale` clamps, so once it
  // has clamped the graph is already too wide to show whole. If the natural fit
  // is readable, open fitted and show the entire shape; otherwise open at 100%
  // centred on the newest job, because a field of 7px chips is not an overview
  // of anything. Fit (F) still offers the shrunk view on request.
  useEffect(() => {
    if (centredRef.current || size.width === 0 || layout.nodes.length === 0)
      return;
    centredRef.current = true;
    const natural = naturalFitScale(
      layout.width,
      layout.height,
      visibleWidth,
      size.height,
    );
    if (natural >= MIN_READABLE_SCALE) {
      setView({
        scale: natural,
        x: Math.round((visibleWidth - layout.width * natural) / 2),
        y: Math.round((size.height - layout.height * natural) / 2),
      });
      return;
    }
    const target =
      layout.nodes.find((node) => node.primary === selected) ??
      layout.nodes[layout.nodes.length - 1];
    centreOn(target);
  }, [
    centreOn,
    layout,
    layout.nodes,
    selected,
    size.height,
    size.width,
    visibleWidth,
  ]);

  // A panel opening or closing changes how much canvas is visible, so the view
  // is recomputed rather than nudged: otherwise `fit` is left centred on a width
  // that no longer exists and the graph slides under the inspector.
  //
  // Skipped until the opening view has been decided: the first measurement is a
  // width change from 0, and reacting to it would immediately refit the graph
  // after the opening effect had deliberately chosen 100% on the newest job.
  const prevVisible = useRef(visibleWidth);
  const settledRef = useRef(false);
  useEffect(() => {
    if (!settledRef.current) {
      prevVisible.current = visibleWidth;
      settledRef.current = size.width > 0;
      return;
    }
    if (size.width === 0) return;
    const changed = prevVisible.current !== visibleWidth;
    prevVisible.current = visibleWidth;
    if (!changed) return;

    const node = selected ? layout.nodeOf.get(selected) : undefined;
    setView((current) => {
      if (!node) {
        // Nothing selected: refit the whole graph into the new width.
        const scale = fitScale(
          layout.width,
          layout.height,
          visibleWidth,
          size.height,
        );
        return {
          scale,
          x: Math.round((visibleWidth - layout.width * scale) / 2),
          y: Math.round((size.height - layout.height * scale) / 2),
        };
      }
      // Keep the selected node where it is, but inside the visible region.
      const left = current.x + node.x * current.scale;
      const right = left + node.width * current.scale;
      const margin = 24;
      if (left >= margin && right <= visibleWidth - margin) return current;
      return {
        ...current,
        x: Math.round(
          visibleWidth / 2 - (node.x + node.width / 2) * current.scale,
        ),
      };
    });
  }, [
    layout.height,
    layout.nodeOf,
    layout.width,
    selected,
    size.height,
    size.width,
    visibleWidth,
  ]);

  const fit = useCallback(() => {
    if (size.width === 0) return;
    const scale = fitScale(
      layout.width,
      layout.height,
      visibleWidth,
      size.height,
    );
    setView({
      scale,
      x: Math.round((visibleWidth - layout.width * scale) / 2),
      y: Math.round((size.height - layout.height * scale) / 2),
    });
  }, [layout.height, layout.width, size.height, size.width, visibleWidth]);

  const zoomBy = useCallback(
    (factor: number) => {
      setView((current) => {
        const next = Math.min(
          MAX_SCALE,
          Math.max(MIN_SCALE, current.scale * factor),
        );
        // Zoom about the centre of the visible region.
        const cx = visibleWidth / 2;
        const cy = size.height / 2;
        return {
          scale: next,
          x: cx - ((cx - current.x) / current.scale) * next,
          y: cy - ((cy - current.y) / current.scale) * next,
        };
      });
    },
    [size.height, visibleWidth],
  );

  // Wheel zooms; with ctrl/cmd it is the same, which also prevents the browser
  // page zoom from firing inside the canvas.
  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      const cursorX = event.clientX - rect.left;
      const cursorY = event.clientY - rect.top;
      setView((current) => {
        const factor = Math.exp(-event.deltaY * 0.0016);
        const next = Math.min(
          MAX_SCALE,
          Math.max(MIN_SCALE, current.scale * factor),
        );
        return {
          scale: next,
          x: cursorX - ((cursorX - current.x) / current.scale) * next,
          y: cursorY - ((cursorY - current.y) / current.scale) * next,
        };
      });
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, []);

  const onPointerDown = (event: React.PointerEvent) => {
    if (event.button !== 0) return;
    // Only the background pans; a node handles its own click.
    if ((event.target as HTMLElement).closest("[data-node]")) return;
    panRef.current = {
      x: event.clientX,
      y: event.clientY,
      originX: view.x,
      originY: view.y,
    };
    setPanning(true);
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    onSelect(null);
  };

  const onPointerMove = (event: React.PointerEvent) => {
    const pan = panRef.current;
    if (!pan) return;
    setView((current) => ({
      ...current,
      x: pan.originX + (event.clientX - pan.x),
      y: pan.originY + (event.clientY - pan.y),
    }));
  };

  const endPan = () => {
    panRef.current = null;
    setPanning(false);
  };

  const mini = view.scale < MINI_SCALE;

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
            className="pointer-events-auto max-w-full rounded-full border border-rule bg-base-100/95 px-2 py-1 shadow-sm backdrop-blur-sm"
          />
        </div>
      )}

      <div
        ref={viewportRef}
        className={cn(
          "canvas-grid absolute inset-0",
          panning ? "cursor-grabbing" : "cursor-grab",
        )}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endPan}
        onPointerCancel={endPan}
        role="application"
        aria-label="Pipeline graph"
      >
        <div
          className="absolute top-0 left-0 origin-top-left"
          style={{
            width: layout.width,
            height: layout.height,
            transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
            transition: panning ? "none" : "transform 200ms ease-out",
          }}
        >
          <svg
            className="pointer-events-none absolute top-0 left-0 overflow-visible"
            width={layout.width}
            height={layout.height}
            aria-hidden
          >
            {layout.edges.map((edge) => {
              const source = byName.get(edge.from);
              const active = selected === edge.from || selected === edge.to;
              const hiddenEdge =
                (hidden?.has(edge.from) ?? false) ||
                (hidden?.has(edge.to) ?? false);
              return (
                <path
                  key={`${edge.from}->${edge.to}`}
                  d={edge.path}
                  fill="none"
                  stroke={
                    active
                      ? "var(--signal)"
                      : edgeColour(source?.type_label ?? "")
                  }
                  strokeWidth={active ? 1.6 : 1}
                  opacity={hiddenEdge ? 0.25 : 1}
                />
              );
            })}
          </svg>

          {layout.nodes.map((node) => {
            const process = byName.get(node.primary);
            if (!process) return null;
            const isSelected = selected === node.primary;
            const dimmed = hidden?.has(node.primary) ?? false;
            return (
              <CanvasNode
                key={node.primary}
                node={node}
                process={process}
                selected={isSelected}
                dimmed={dimmed}
                mini={mini}
                stacked={node.processes.length > 1}
                onOpen={() => onOpen(process)}
                onAction={
                  onNodeAction
                    ? (action) => onNodeAction(process, action)
                    : undefined
                }
              />
            );
          })}
        </div>
      </div>

      {addControl && (
        <div className="absolute bottom-3 left-3 z-0">{addControl}</div>
      )}

      <CanvasControls
        scale={view.scale}
        onZoomIn={() => zoomBy(1.25)}
        onZoomOut={() => zoomBy(0.8)}
        onFit={fit}
        onCentre={() => {
          const node = selected ? layout.nodeOf.get(selected) : undefined;
          if (node) centreOn(node);
          else fit();
        }}
      />
    </div>
  );
}

function CanvasNode({
  node,
  process,
  selected,
  dimmed,
  mini,
  stacked,
  onOpen,
  onAction,
}: {
  node: LayoutNode;
  process: PipelineProcess;
  selected: boolean;
  dimmed: boolean;
  mini: boolean;
  stacked: boolean;
  onOpen: () => void;
  onAction?: (action: NodeAction) => void;
}) {
  // The card is shared with the grid view, so the two cannot drift apart; only
  // the positioning differs.
  const position = { left: node.x, top: node.y, width: mini ? 96 : NODE_WIDTH };

  if (mini) {
    return (
      <MiniJobCard
        process={process}
        selected={selected}
        dimmed={dimmed}
        onOpen={onOpen}
        className="absolute"
        style={position}
      />
    );
  }
  return (
    <JobCard
      process={process}
      selected={selected}
      dimmed={dimmed}
      stackedCount={stacked ? node.processes.length - 1 : 0}
      onOpen={onOpen}
      onAction={onAction}
      className="absolute"
      style={position}
      // Dragging a card onto an input field fills that field from this job's
      // outputs. It is the graph equivalent of RELION's "input to this job"
      // browser, done in the direction people expect in a canvas.
      draggable
    />
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
      className="grid size-7 place-items-center rounded-[3px] text-ink-muted hover:bg-base-200 hover:text-base-content"
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
    <div className="absolute right-3 bottom-3 flex items-center gap-0.5 rounded-[4px] border border-rule bg-base-100 p-0.5 shadow-sm">
      <CanvasButton onClick={onZoomOut} label="Zoom out">
        <Minus className="size-3.5" />
      </CanvasButton>
      <button
        type="button"
        onClick={onFit}
        className="w-10 rounded-[3px] py-1 text-center font-mono t-meta text-ink-muted hover:bg-base-200"
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
        className="grid size-7 place-items-center rounded-[3px] text-ink-muted hover:bg-base-200 hover:text-base-content"
        title="Centre on selection (T)"
      >
        <Boxes className="size-3.5" />
      </button>
      <button
        type="button"
        data-canvas-fit
        onClick={onFit}
        className="grid size-7 place-items-center rounded-[3px] text-ink-muted hover:bg-base-200 hover:text-base-content"
        title="Fit to view (F)"
      >
        <Maximize2 className="size-3.5" />
      </button>
    </div>
  );
}

export { NODE_HEIGHT, NODE_WIDTH };
