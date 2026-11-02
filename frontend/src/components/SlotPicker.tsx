// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useEffect, useMemo, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  ChevronRight,
  FileOutput,
  X,
} from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { InputCandidate, PipelineProcess } from "@/lib/types";

/**
 * The file a dropped job contributes to one input slot.
 *
 * The shape mirrors the queue's slot syntax: an option may name a queued job's
 * output rather than a path, so a downstream stage can be queued before its
 * input exists. Picking a candidate that already exists can also be expressed
 * as a plain path, but going through the slot form is what records the
 * dependency, so both cases use it.
 */
export interface SlotChoice {
  variable: string;
  /** What the option field shows. */
  path: string;
  /** `q0003:tomograms.star` when this binds to a queued job, else null. */
  slot: string | null;
  node: string;
  producer: string | null;
  /** The file does not exist yet, so this choice waits for a job. */
  pending: boolean;
}

interface SlotPickerProps {
  /** The option being filled, e.g. `in_tomoset`. */
  variable: string;
  label: string;
  jobType: string;
  projectDirectory: string;
  /** The job dropped onto the slot, when the user dropped one. */
  droppedProducer: string | null;
  onPick: (choice: SlotChoice) => void;
  onClose: () => void;
}

/**
 * A list of the files that may go in one input slot.
 *
 * The candidates are not a guess at which files are "compatible": they are the
 * entries under `.Nodes/<coarse type>/`, which is exactly what the desktop GUI's
 * own picker lists (gui_entries.cp:cb_browse_node). Because a queued job
 * touches its `.Nodes` entry before it runs - as RELION does for a scheduled
 * job - a stage that has not run yet is offered here too, marked as pending.
 *
 * Choosing a pending one is the point: it binds this slot to that job's output,
 * so the queue starts this stage when the file actually appears.
 */
export function SlotPicker({
  variable,
  label,
  jobType,
  projectDirectory,
  droppedProducer,
  onPick,
  onClose,
}: SlotPickerProps) {
  const [candidates, setCandidates] = useState<InputCandidate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .get<{ candidates: InputCandidate[] }>(
        `/api/project/input-candidates?job_type=${encodeURIComponent(
          jobType,
        )}&variable=${encodeURIComponent(variable)}&project_directory=${encodeURIComponent(
          projectDirectory,
        )}`,
      )
      .then((data) => {
        if (!cancelled) setCandidates(data.candidates);
      })
      .catch((caught) => {
        if (!cancelled) {
          setError(
            caught instanceof Error ? caught.message : "could not list files",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [jobType, variable, projectDirectory]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // A drop on the field pre-selects the files that job produces; the rest stay
  // one click away, because the dropped job may not be the right one.
  const fromDropped = useMemo(
    () =>
      droppedProducer
        ? (candidates ?? []).filter((item) => item.producer === droppedProducer)
        : [],
    [candidates, droppedProducer],
  );
  const shown = droppedProducer && !expanded ? fromDropped : (candidates ?? []);

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-scrim p-4"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal
      aria-label={`Files for ${label}`}
    >
      <div className="acrylic-panel flex max-h-[min(35rem,90vh)] w-[min(35rem,100%)] flex-col overflow-hidden rounded-box border border-rule-strong shadow-[0_24px_64px_-16px_rgb(0_0_0/0.45)]">
        <header className="flex items-start gap-2 border-b border-rule px-3.5 py-2.5">
          <FileOutput className="mt-0.5 size-4 shrink-0 text-ink-faint" />
          <div className="min-w-0 flex-1">
            <h2 className="truncate t-ui font-semibold">{label}</h2>
            <p className="truncate font-mono t-meta text-ink-faint">
              {variable}
              {droppedProducer ? ` ← ${droppedProducer}` : ""}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="grid size-6 shrink-0 place-items-center rounded-selector text-ink-faint hover:bg-base-200 hover:text-base-content"
            aria-label="Close"
          >
            <X className="size-3.5" />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {error ? (
            <p
              className="flex items-start gap-1.5 px-3.5 py-3 t-label text-st-failed"
              role="alert"
            >
              <AlertCircle className="mt-px size-3.5 shrink-0" />
              {error}
            </p>
          ) : candidates === null ? (
            <p className="flex items-center gap-1.5 px-3.5 py-3 t-label text-ink-faint">
              <Spinner size="xs" />
              listing files…
            </p>
          ) : shown.length === 0 ? (
            <div className="px-3.5 py-3">
              <p className="t-label text-ink-muted">
                {droppedProducer && !expanded
                  ? `${droppedProducer} produces nothing of this type.`
                  : "No job has produced a file of this type yet."}
              </p>
              {droppedProducer &&
                !expanded &&
                (candidates?.length ?? 0) > 0 && (
                  <button
                    type="button"
                    onClick={() => setExpanded(true)}
                    className="mt-1.5 flex items-center gap-0.5 t-label text-signal hover:underline"
                  >
                    show all {candidates?.length} files
                    <ChevronRight className="size-3" />
                  </button>
                )}
            </div>
          ) : (
            <ul className="flex flex-col">
              {shown.map((candidate) => (
                <li key={candidate.node}>
                  <button
                    type="button"
                    onClick={() =>
                      onPick({ ...slotReference(candidate), variable })
                    }
                    className="flex w-full items-center gap-2 px-3.5 py-1.5 text-left hover:bg-base-200"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-mono t-ui">
                        {candidate.node}
                      </div>
                      <div className="truncate t-meta text-ink-faint">
                        {candidate.producer ?? "no producer"}
                        {candidate.pending ? " · not written yet" : ""}
                      </div>
                    </div>
                    <StatusWord candidate={candidate} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <footer className="border-t border-rule bg-base-200/60 px-3.5 py-2">
          <p className="t-meta leading-relaxed text-ink-muted">
            A file from a <span className="font-medium">queued</span> job
            becomes an input slot: this job waits for that one and starts when
            the file appears.
          </p>
          {droppedProducer &&
            !expanded &&
            (candidates?.length ?? 0) > shown.length && (
              <button
                type="button"
                onClick={() => setExpanded(true)}
                className="mt-1 flex items-center gap-0.5 t-meta text-signal hover:underline"
              >
                show all files, not just {droppedProducer}&rsquo;s
                <ChevronRight className="size-2.5" />
              </button>
            )}
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function StatusWord({ candidate }: { candidate: InputCandidate }) {
  if (candidate.exists) {
    return <span className="shrink-0 t-meta text-ink-faint">ready</span>;
  }
  return (
    <span
      className={cn(
        "shrink-0 rounded-[0.125rem] px-1 t-meta",
        candidate.producer_status === "Scheduled"
          ? "bg-[var(--st-scheduled-fill)] text-[var(--st-scheduled-text)]"
          : "bg-[var(--st-running-fill)] text-[var(--st-running-text)]",
      )}
      title="The file does not exist yet; picking it waits for that job"
    >
      {candidate.producer_status === "Scheduled" ? "queued" : "pending"}
    </span>
  );
}

/**
 * What to store for a candidate.
 *
 * A file that already exists is a plain path - there is nothing to wait for.
 * A file that has not been written yet is a queue slot (`q0003:tomograms.star`),
 * which is the only spelling that makes the queue wait for the producer instead
 * of failing against a missing file. If the producer is not a live queue entry
 * there is nothing to reference, so the path is used and the queue's own
 * missing-input check will say so.
 */
function slotReference(
  candidate: InputCandidate,
): Omit<SlotChoice, "variable"> {
  return {
    path: candidate.node,
    // A file that already exists is a plain path: there is nothing to wait for,
    // and a path is what RELION itself would record. One that has not been
    // written yet is a queue slot, the only spelling that makes the queue wait
    // for the producer instead of failing against a missing file.
    slot: candidate.exists ? null : candidate.slot,
    node: candidate.node,
    producer: candidate.producer,
    pending: !candidate.exists,
  };
}

/** A process that can be dragged onto an input slot. */
export function draggableJobProps(process: PipelineProcess) {
  return {
    draggable: true,
    onDragStart: (event: React.DragEvent) => {
      // The MIME type carries the whole point of the gesture: which job was
      // dragged, so the slot picker can offer *that job's* outputs first.
      event.dataTransfer.setData("application/x-relion-job", process.name);
      event.dataTransfer.setData("text/plain", process.name);
      event.dataTransfer.effectAllowed = "link";
    },
  };
}
