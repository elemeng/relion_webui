// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Badge, on daisyUI's `badge`.
 *
 * `tone` maps onto daisyUI's semantic names, so a status badge and a plain
 * count badge look like they belong to the same system. Job status does not go
 * through here: it has its own five-value palette in Status.tsx, because
 * RELION's states are not the same set as daisyUI's semantic roles (and
 * "running" is not a warning).
 */
type Tone =
  "default" | "primary" | "success" | "warning" | "destructive" | "muted";

const TONES: Record<Tone, string> = {
  default: "badge-neutral badge-soft",
  primary: "badge-accent badge-soft",
  success: "badge-success badge-soft",
  warning: "badge-warning badge-soft",
  destructive: "badge-error badge-soft",
  muted: "badge-ghost text-ink-muted",
};

function Badge({
  className,
  tone = "default",
  ...props
}: React.ComponentProps<"span"> & { tone?: Tone }) {
  return (
    <span
      data-slot="badge"
      className={cn(
        "badge h-auto gap-1 rounded-selector px-1.5 py-0 t-meta font-medium whitespace-nowrap",
        TONES[tone],
        className,
      )}
      {...props}
    />
  );
}

/** Map a RELION job status onto a badge tone. */
export function statusTone(status: string): Tone {
  switch (status.toLowerCase()) {
    case "running":
      return "warning";
    case "succeeded":
      return "success";
    case "failed":
      return "destructive";
    case "aborted":
      return "muted";
    default:
      return "default";
  }
}

export { Badge };
