// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { Clock, X } from "lucide-react";

import { cn } from "@/lib/utils";

export interface DraftChip {
  id: string;
  label: string;
  active: boolean;
  onOpen: () => void;
  onDiscard: () => void;
}

/**
 * Drafts in progress, in one strip.
 *
 * The graph and the card grid both need to show them, and they were two copies
 * of the same markup - which is how a chip ends up looking one way on the
 * canvas and another in the grid. A draft is a job you decided to build and
 * have not queued: it stays visible, and Postpone marks it so the difference
 * between "thinking about it" and "forgot about it" is legible.
 */
export function DraftStrip({
  drafts,
  className,
  orientation = "horizontal",
}: {
  drafts: DraftChip[];
  className?: string;
  /** `wrap` is the grid's version: the strip is not overlaid, it flows. */
  orientation?: "horizontal" | "wrap";
}) {
  if (drafts.length === 0) return null;

  return (
    <div
      className={cn(
        // `w-fit`, not the block default: a strip of two chips should be as
        // wide as two chips, not as wide as the canvas.
        "flex w-fit max-w-full items-center gap-1.5",
        orientation === "wrap" ? "flex-wrap" : "flex-nowrap overflow-x-auto",
        className,
      )}
      aria-label="Drafts"
    >
      <span className="flex shrink-0 items-center gap-1 t-meta font-medium text-ink-faint">
        <Clock className="size-3" />
        Drafts
      </span>
      {drafts.map((draft) => (
        <div
          key={draft.id}
          className={cn(
            "group/draft flex shrink-0 items-center gap-1 rounded-full border py-0.5 pr-1 pl-2 t-label",
            draft.active
              ? "border-signal bg-base-100 text-base-content"
              : "border-dashed border-rule text-ink-muted",
          )}
        >
          <span
            className={cn(
              "size-1.5 shrink-0 rounded-full",
              draft.active ? "bg-signal" : "bg-rule-strong",
            )}
            aria-hidden
          />
          <button
            type="button"
            onClick={draft.onOpen}
            className="max-w-[12rem] truncate hover:underline"
            title={draft.active ? "Edit this job" : "Postponed — edit"}
          >
            {draft.label}
          </button>
          <button
            type="button"
            onClick={draft.onDiscard}
            className={cn(
              "grid size-4 shrink-0 place-items-center rounded-full text-ink-faint",
              "opacity-60 hover:bg-st-failed/10 hover:text-st-failed group-hover/draft:opacity-100",
            )}
            title="Discard this draft"
            aria-label={`Discard ${draft.label}`}
          >
            <X className="size-2.5" />
          </button>
        </div>
      ))}
    </div>
  );
}
