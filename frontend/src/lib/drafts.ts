// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import type { JobFieldValues } from "@/lib/types";

/**
 * A job being built, before it is submitted.
 *
 * ComfyUI's insight is that the graph is the document: a node you are editing
 * already exists on the canvas, it just is not queued. The same holds here - a
 * draft has a place in the pipeline and can be connected or configured, and
 * "Queue" is what turns it into a real RELION job directory.
 *
 * Deliberately separate from `Job` (a running process): a draft has no id, no
 * directory and no status, and nothing about it has touched the filesystem.
 */
export interface DraftJob {
  /** Client-side identity, stable while the draft lives. */
  id: string;
  jobType: string;
  values: JobFieldValues;
  /** Flags added through the advanced panel. */
  extraCli: { flag: string; value: string }[];
  /**
   * False once the user postpones it. A postponed draft stays on the canvas as
   * a placeholder rather than disappearing: it is a decision you made, not an
   * error, and it is the thing you come back to.
   */
  active: boolean;
  createdAt: number;
  /**
   * Which machine to run on. `null` means the agent's own; a name comes from
   * /api/hosts. Kept on the draft so postponing does not lose the choice.
   */
  host?: string | null;
}

let counter = 0;

export function newDraft(
  jobType: string,
  values: JobFieldValues = {},
): DraftJob {
  counter += 1;
  return {
    id: `draft-${Date.now().toString(36)}-${counter}`,
    jobType,
    values,
    extraCli: [],
    active: true,
    createdAt: Date.now(),
    host: null,
  };
}

/** The label a draft shows before it has an alias or a job number. */
export function draftLabel(draft: DraftJob, title: string): string {
  return draft.active ? title : `${title} (postponed)`;
}
