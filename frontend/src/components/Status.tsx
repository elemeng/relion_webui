// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { cn } from "@/lib/utils";

/**
 * RELION's five pipeline statuses.
 *
 * The pipeline file only ever holds these, so the UI maps them exactly rather
 * than inventing intermediate states: a status the pipeline does not write
 * would be a status the desktop GUI disagrees with.
 */
export type JobStatus =
  "Scheduled" | "Running" | "Succeeded" | "Failed" | "Aborted";

export const STATUS_ORDER: JobStatus[] = [
  "Failed",
  "Running",
  "Aborted",
  "Scheduled",
  "Succeeded",
];

const KNOWN: Record<string, JobStatus> = {
  scheduled: "Scheduled",
  queued: "Scheduled",
  running: "Running",
  succeeded: "Succeeded",
  completed: "Succeeded",
  failed: "Failed",
  aborted: "Aborted",
  killed: "Aborted",
};

/** Normalise a status string from either the pipeline or the job manager. */
export function normaliseStatus(status: string | null | undefined): JobStatus {
  if (!status) return "Scheduled";
  return KNOWN[status.trim().toLowerCase()] ?? "Scheduled";
}

/**
 * Every status-driven class in the app, in one place.
 *
 * Each state owns a fill (a wash for surfaces), an ink (text, legible on paper
 * at >= 4.5:1) and a rail (the 3px edge on a card). All three come from the
 * tokens in index.css, which are checked per theme, so a card, a pill and a
 * dot cannot disagree about what "failed" looks like.
 */
const FILL: Record<JobStatus, string> = {
  Scheduled: "bg-[var(--st-scheduled-fill)] text-[var(--st-scheduled-text)]",
  Running: "bg-[var(--st-running-fill)] text-[var(--st-running-text)]",
  Succeeded: "bg-[var(--st-succeeded-fill)] text-[var(--st-succeeded-text)]",
  Failed: "bg-[var(--st-failed-fill)] text-[var(--st-failed-text)]",
  Aborted: "bg-[var(--st-aborted-fill)] text-[var(--st-aborted-text)]",
};

const DOT: Record<JobStatus, string> = {
  Scheduled: "bg-[var(--st-scheduled-text)]",
  Running: "bg-[var(--st-running-text)]",
  Succeeded: "bg-[var(--st-succeeded-text)]",
  Failed: "bg-[var(--st-failed-text)]",
  Aborted: "bg-[var(--st-aborted-text)]",
};

const RAIL: Record<JobStatus, string> = {
  Scheduled: "bg-[var(--st-scheduled-rail)]",
  Running: "bg-[var(--st-running-rail)]",
  Succeeded: "bg-[var(--st-succeeded-rail)]",
  Failed: "bg-[var(--st-failed-rail)]",
  Aborted: "bg-[var(--st-aborted-rail)]",
};

const INK: Record<JobStatus, string> = {
  Scheduled: "text-[var(--st-scheduled-text)]",
  Running: "text-[var(--st-running-text)]",
  Succeeded: "text-[var(--st-succeeded-text)]",
  Failed: "text-[var(--st-failed-text)]",
  Aborted: "text-[var(--st-aborted-text)]",
};

/** A wash over a card body, quiet enough for the label to stay readable. */
const WASH: Record<JobStatus, string> = {
  Scheduled:
    "bg-[color-mix(in_srgb,var(--st-scheduled-fill)_55%,var(--color-base-100))]",
  Running:
    "bg-[color-mix(in_srgb,var(--st-running-fill)_55%,var(--color-base-100))]",
  Succeeded:
    "bg-[color-mix(in_srgb,var(--st-succeeded-fill)_55%,var(--color-base-100))]",
  Failed:
    "bg-[color-mix(in_srgb,var(--st-failed-fill)_55%,var(--color-base-100))]",
  Aborted:
    "bg-[color-mix(in_srgb,var(--st-aborted-fill)_55%,var(--color-base-100))]",
};

export function statusFill(status: string): string {
  return FILL[normaliseStatus(status)];
}

export function statusDotClass(status: string): string {
  return DOT[normaliseStatus(status)];
}

export function statusRail(status: string): string {
  return RAIL[normaliseStatus(status)];
}

export function statusInk(status: string): string {
  return INK[normaliseStatus(status)];
}

export function statusWash(status: string): string {
  return WASH[normaliseStatus(status)];
}

/** The 6 px dot used in every node, row and dialog header. */
export function StatusDot({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "size-1.5 shrink-0 rounded-full",
        statusDotClass(status),
        className,
      )}
      aria-hidden
    />
  );
}

/** The filled pill used where the status must be readable as a word. */
export function StatusPill({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  const normalised = normaliseStatus(status);
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-[3px] px-1.5 py-px t-meta font-medium whitespace-nowrap",
        FILL[normalised],
        className,
      )}
    >
      {normalised.toLowerCase()}
    </span>
  );
}

/**
 * The bar of per-status counts used on a project card.
 *
 * A count with its status colour reads faster than "1 failed, 2 scheduled",
 * and keeping the order fixed means the same project always looks the same.
 */
export function StatusCounts({
  counts,
  className,
}: {
  counts: Record<string, number>;
  className?: string;
}) {
  const present = STATUS_ORDER.map(
    (status) => [status, counts[status] ?? 0] as const,
  ).filter(([, count]) => count > 0);

  if (present.length === 0) {
    return (
      <span className={cn("t-meta text-ink-faint", className)}>
        no jobs yet
      </span>
    );
  }

  return (
    <div className={cn("flex flex-wrap items-center gap-1", className)}>
      {present.map(([status, count]) => (
        <span
          key={status}
          className={cn(
            "inline-flex items-center gap-1 rounded-[3px] px-1.5 py-px t-meta font-medium",
            FILL[status],
          )}
          title={`${count} ${status.toLowerCase()}`}
        >
          <StatusDot status={status} className="size-1" />
          {count}
        </span>
      ))}
    </div>
  );
}
