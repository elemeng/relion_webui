import { useMemo, useState } from "react";

import { JobCard } from "@/components/JobCard";
import type { NodeAction } from "@/components/NodeMenu";
import { DraftStrip, type DraftChip } from "@/components/DraftStrip";
import { Segmented } from "@/components/ui/segmented";
import { normaliseStatus } from "@/components/Status";
import { cn } from "@/lib/utils";
import type { PipelineGraph, PipelineProcess } from "@/lib/types";

interface JobCardsViewProps {
  graph: PipelineGraph;
  selected: string | null;
  onOpen: (process: PipelineProcess) => void;
  onNodeAction?: (process: PipelineProcess, action: NodeAction) => void;
  drafts?: DraftChip[];
  addControl?: React.ReactNode;
  /** Width reserved on the right, so the grid does not run under a panel. */
  reservedRight?: number;
}

type Density = "comfortable" | "dense";
type Sort = "number" | "status" | "name";

const SORTS = [
  ["number", "Number"],
  ["status", "Status"],
  ["name", "Name"],
] as const;

const DENSITIES = [
  ["comfortable", "Grid"],
  ["dense", "Dense"],
] as const;

/**
 * The project's jobs as a tight grid of cards.
 *
 * The graph answers "what feeds what"; this answers "what is here and what is
 * its state", which is the question when a project has fifty jobs and you are
 * looking for the failed one. Both render the same `JobCard`, so a change to
 * the card shows up in both views.
 *
 * Cards view than to a table: small cards,
 * left-to-right, oldest first, so the grid reads in the order the project was
 * built rather than by whatever sorted on top.
 *
 * Columns are a function of the window, not of the card count: the track width
 * is fixed per breakpoint (`--card-w`) and `auto-fill` decides how many fit, so
 * the grid never stretches two cards across a 27" display.
 */
export function JobCardsView({
  graph,
  selected,
  onOpen,
  onNodeAction,
  drafts = [],
  addControl,
  reservedRight = 0,
}: JobCardsViewProps) {
  const [density, setDensity] = useState<Density>("comfortable");
  const [sort, setSort] = useState<Sort>("number");

  const jobs = useMemo(() => {
    const copy = [...graph.processes];
    if (sort === "status") {
      // Exceptions first: failed, then running, then the rest.
      const rank = (status: string) => {
        const normal = normaliseStatus(status);
        if (normal === "Failed") return 0;
        if (normal === "Running") return 1;
        if (normal === "Aborted") return 2;
        if (normal === "Scheduled") return 3;
        return 4;
      };
      copy.sort(
        (a, b) => rank(a.status) - rank(b.status) || number(a) - number(b),
      );
      return copy;
    }
    if (sort === "name") {
      copy.sort((a, b) =>
        (a.alias === "None" ? a.type_label : a.alias).localeCompare(
          b.alias === "None" ? b.type_label : b.alias,
        ),
      );
      return copy;
    }
    copy.sort((a, b) => number(a) - number(b));
    return copy;
  }, [graph.processes, sort]);

  return (
    <div className="relative h-full min-w-0 flex-1">
      <div className="h-full overflow-y-auto">
        <div
          className="flex w-full flex-col gap-3 p-4"
          style={{
            paddingRight: reservedRight ? reservedRight + 16 : undefined,
          }}
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span className="t-ui font-medium">
              {jobs.length} job{jobs.length === 1 ? "" : "s"}
            </span>
            <span className="flex-1" />

            <label className="flex items-center gap-1.5 t-label text-ink-faint">
              sort
              <Segmented
                value={sort}
                options={SORTS}
                onChange={setSort}
                ariaLabel="Sort jobs"
              />
            </label>
            <label className="flex items-center gap-1.5 t-label text-ink-faint">
              size
              <Segmented
                value={density}
                options={DENSITIES}
                onChange={setDensity}
                ariaLabel="Card density"
              />
            </label>
          </div>

          <DraftStrip drafts={drafts} orientation="wrap" />

          <div
            className={cn(
              "grid w-full",
              // Fixed-width tracks, not `1fr`: the number of columns follows the
              // window, and the last row ends where it ends instead of stretching
              // two cards across the whole width.
              density === "dense"
                ? "grid-cols-[repeat(auto-fill,var(--card-mini-w))] gap-2"
                : "grid-cols-[repeat(auto-fill,var(--card-w))] gap-3",
            )}
          >
            {jobs.map((process) => (
              <JobCard
                key={process.name}
                process={process}
                selected={selected === process.name}
                compact={density === "dense"}
                onOpen={() => onOpen(process)}
                draggable
                onAction={
                  onNodeAction
                    ? (action) => onNodeAction(process, action)
                    : undefined
                }
              />
            ))}
          </div>

          {jobs.length === 0 && (
            <div className="flex flex-col items-start gap-2 rounded-box border border-dashed border-rule px-4 py-8">
              <p className="t-ui font-medium">No jobs yet</p>
              <p className="max-w-sm t-body text-ink-muted">
                Use the <span className="font-semibold">+</span> button to build
                one. It appears here as soon as it is queued.
              </p>
            </div>
          )}
        </div>
      </div>

      {addControl && (
        <div className="absolute bottom-3 left-3 z-10">{addControl}</div>
      )}
    </div>
  );
}

/** The job number, for the default chronological order. */
function number(process: PipelineProcess): number {
  const match = /job(\d+)/.exec(process.name);
  return match ? Number(match[1]) : 0;
}
