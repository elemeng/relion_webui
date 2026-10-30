import { NodeMenu, type NodeAction } from "@/components/NodeMenu";
import {
  StatusDot,
  StatusPill,
  normaliseStatus,
  statusRail,
} from "@/components/Status";
import { cn } from "@/lib/utils";
import type { PipelineProcess } from "@/lib/types";

/**
 * A job as a card.
 *
 * Deliberately layout-free: the graph positions it absolutely at world
 * coordinates, the cards view puts it in a grid. Both show the same thing, so
 * the two views of one project cannot drift apart.
 *
 * State is carried three times over, at three distances: a 3px rail for the
 * edge of the eye, a dot beside the number for the row, and a pill with the
 * word for when you are actually reading. The body stays paper-coloured - an
 * earlier version washed the whole middle band in status colour, which at 70%
 * zoom became a large empty pastel rectangle carrying no information.
 */
export function JobCard({
  process,
  selected,
  dimmed,
  stackedCount = 0,
  onOpen,
  onAction,
  className,
  style,
  compact,
  draggable = false,
}: {
  process: PipelineProcess;
  selected: boolean;
  dimmed?: boolean;
  /** Extra jobs condensed into this one, for a refinement loop. */
  stackedCount?: number;
  onOpen: () => void;
  onAction?: (action: NodeAction) => void;
  className?: string;
  style?: React.CSSProperties;
  /** Cell-sized variant for dense grids: no preview area. */
  compact?: boolean;
  /**
   * Drag this card onto an input field to fill that field with one of its
   * outputs. The payload is the process name, which is how the drop target
   * knows which job produced what.
   */
  draggable?: boolean;
}) {
  const status = normaliseStatus(process.status);
  const title = process.alias && process.alias !== "None" ? process.alias : "";
  const kind = process.type_label.replace(/^relion\./, "");
  const queuedHere = Boolean(process.queue_entry);

  return (
    <div
      data-node
      className={cn(
        "group/card relative flex flex-col overflow-hidden rounded-box border bg-base-100",
        "transition-[border-color,box-shadow] duration-150",
        selected
          ? "border-signal shadow-[0_0_0_1px_var(--signal)]"
          : "border-rule",
        !selected && !dimmed && "hover:border-rule-strong",
        dimmed && "border-dashed border-rule opacity-45",
        className,
      )}
      style={style}
      draggable={draggable}
      onDragStart={
        draggable
          ? (event) => {
              event.dataTransfer.setData(
                "application/x-relion-job",
                process.name,
              );
              event.dataTransfer.setData("text/plain", process.name);
              event.dataTransfer.effectAllowed = "link";
            }
          : undefined
      }
      onClick={(event) => {
        event.stopPropagation();
        onOpen();
      }}
      role="button"
      tabIndex={0}
      aria-label={`${jobNumber(process)} ${title || process.type_label}, ${status}`}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
    >
      <span
        className={cn(
          "absolute inset-y-0 left-0 w-[3px]",
          statusRail(process.status),
        )}
        aria-hidden
      />

      <div className="flex items-center gap-1.5 py-1.5 pr-1.5 pl-2.5">
        <StatusDot status={process.status} />
        <span className="shrink-0 font-mono t-meta text-ink-muted">
          {jobNumber(process)}
        </span>
        <span className="min-w-0 flex-1 truncate t-ui font-medium">
          {title || kind}
        </span>
        {onAction && (
          <NodeMenu
            actions={nodeActions(process)}
            onAction={onAction}
            // Faintly present rather than hidden: a hover-only affordance is
            // one nobody finds on a canvas they scroll past.
            className="-mr-0.5 opacity-40 transition-opacity group-hover/card:opacity-100 focus-within:opacity-100"
          />
        )}
      </div>

      {!compact && (
        <div className="mx-2.5 mb-1.5 flex min-w-0 items-baseline gap-1.5">
          <span className="truncate font-mono t-meta text-ink-faint">
            {kind}
          </span>
        </div>
      )}

      <div
        className={cn(
          "mt-auto flex items-center gap-1.5 border-t border-rule-soft py-1 pr-2 pl-2.5",
          compact && "border-t-0",
        )}
      >
        <StatusPill status={process.status} />
        {/* A Scheduled row can mean two different things: queued here, or
            scheduled in RELION. Saying which is the difference between "waiting
            for a GPU" and "waiting for someone to press Run in the GUI", and
            only this app's queue can start the first. */}
        {queuedHere && (
          <span
            className="shrink-0 rounded-[2px] border border-[var(--st-scheduled-text)] px-1 t-meta text-[var(--st-scheduled-text)]"
            title="Queued here: starts on its own when its inputs exist and a GPU is free"
          >
            queue
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-right font-mono t-meta text-ink-faint">
          {process.name.split("/")[0]}
          {stackedCount > 0 && (
            <span
              className="text-signal"
              title={`${stackedCount} more in this loop`}
            >
              {" "}
              +{stackedCount}
            </span>
          )}
        </span>
      </div>
    </div>
  );
}

/**
 * The glyph-only card used when a graph is zoomed out past reading size.
 *
 * At this size the only job left is telling cards apart, so it carries the
 * number, the status and the status colour - and nothing that would be
 * illegible.
 */
export function MiniJobCard({
  process,
  selected,
  dimmed,
  onOpen,
  className,
  style,
}: {
  process: PipelineProcess;
  selected: boolean;
  dimmed?: boolean;
  onOpen: () => void;
  className?: string;
  style?: React.CSSProperties;
}) {
  const status = normaliseStatus(process.status);
  return (
    <div
      data-node
      className={cn(
        "relative flex flex-col gap-1 overflow-hidden rounded-box border bg-base-100 px-1.5 py-1",
        selected
          ? "border-signal shadow-[0_0_0_1px_var(--signal)]"
          : "border-rule",
        dimmed && "border-dashed opacity-45",
        className,
      )}
      style={style}
      onClick={(event) => {
        event.stopPropagation();
        onOpen();
      }}
      role="button"
      tabIndex={0}
      aria-label={`${jobNumber(process)} ${process.type_label}, ${status}`}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
    >
      <span
        className={cn(
          "absolute inset-y-0 left-0 w-[2px]",
          statusRail(process.status),
        )}
        aria-hidden
      />
      <div className="flex items-center gap-1 pl-1">
        <StatusDot status={process.status} className="size-1" />
        <span className="font-mono t-meta">{jobNumber(process)}</span>
      </div>
      <span className="truncate pl-1 t-meta text-ink-muted">
        {status.toLowerCase()}
      </span>
    </div>
  );
}

/** Which entries a job's menu shows, from its pipeline status. */
export function nodeActions(process: PipelineProcess): NodeAction[] {
  const running = normaliseStatus(process.status) === "Running";
  const actions: NodeAction[] = [
    "open",
    "details",
    "outputs",
    "log",
    "use-as-input",
  ];
  if (running) actions.push("stop");
  actions.push("actions");
  return actions;
}

function jobNumber(process: PipelineProcess): string {
  const match = /job(\d+)/.exec(process.name);
  return match ? `J${match[1]}` : process.name;
}
