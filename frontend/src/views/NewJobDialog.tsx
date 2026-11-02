// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, Clock, ListPlus, Play, X } from "lucide-react";

import type { CliExtra } from "@/components/AdvancedCliPanel";
import { JobForm } from "@/components/JobForm";
import { SlotPicker } from "@/components/SlotPicker";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectItem } from "@/components/ui/select";
import { api } from "@/lib/api";
import type { DraftJob } from "@/lib/drafts";
import type {
  ExecutionHost,
  Job,
  JobFieldValues,
  JobFormSchema,
} from "@/lib/types";

interface NewJobDialogProps {
  draft: DraftJob;
  /** Machines this job can run on; `local` is always present. */
  hosts?: ExecutionHost[];
  schema: JobFormSchema | null;
  projectDirectory: string | null;
  onChange: (next: {
    values?: JobFieldValues;
    extraCli?: CliExtra[];
    host?: string | null;
  }) => void;
  onQueue: (dryRun: boolean) => Promise<Job | null>;
  /**
   * Put the job on the queue instead of starting it now. Omitted when there is
   * no project open, which is the only case where the queue cannot be used.
   */
  onEnqueue?: (options: {
    gpus: number[];
    exclusive: boolean;
    /** Option variable -> `<entry id>:<filename>` for slots bound to a queue job. */
    inputs: Record<string, string>;
  }) => Promise<string | null>;
  onPostpone: () => void;
  onDiscard: () => void;
  onClose: () => void;
}

/**
 * Configure a draft job.
 *
 * The node is already on the canvas, so this is not "create a job" - it is the
 * form for a node that exists. Queue submits it; Postpone keeps the node and
 * closes the dialog, so it stays visible in the graph as something still to do.
 *
 * A right-hand sheet rather than a centred modal: the graph stays visible
 * beside it, which is the point of building on the canvas, and a 70-field form
 * gets the full height of the window instead of 85% of it.
 */
export function NewJobDialog({
  draft,
  hosts = [],
  schema,
  projectDirectory,
  onChange,
  onQueue,
  onEnqueue,
  onPostpone,
  onDiscard,
  onClose,
}: NewJobDialogProps) {
  const [busy, setBusy] = useState<"queue" | "check" | "enqueue" | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [queued, setQueued] = useState<string | null>(null);
  const [exclusive, setExclusive] = useState(false);
  // Slots bound to a queued job's output. These travel as `inputs` rather than
  // as the option value, because that is what records the dependency.
  const [slots, setSlots] = useState<Record<string, string>>({});
  const [picker, setPicker] = useState<{
    variable: string;
    label: string;
    producer: string | null;
  } | null>(null);

  const values = draft.values;
  const extraCli = draft.extraCli;

  // Cross-option rules live on the server, because they are the same ones the
  // argv builder applies. Asking it keeps the form and the command in step.
  useEffect(() => {
    if (!schema) {
      setProblems([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      const extra = extraCli.filter((row) => row.flag.trim() !== "");
      api
        .post<{ problems: string[]; warnings?: string[] }>(
          "/api/job-validate",
          {
            job_type: draft.jobType,
            arguments: {
              ...Object.fromEntries(
                Object.entries(values).filter(
                  ([, value]) => value !== "" && value != null,
                ),
              ),
              ...(extra.length > 0 ? { cli_extra: extra } : {}),
            },
          },
        )
        .then((data) => {
          if (!cancelled)
            setProblems([...data.problems, ...(data.warnings ?? [])]);
        })
        .catch(() => {
          if (!cancelled) setProblems([]);
        });
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [draft.jobType, extraCli, schema, values]);

  const missing = useMemo(() => {
    if (!schema) return [];
    return schema.required.filter((variable) => {
      const value = values[variable];
      return (
        value === undefined || value === null || String(value).trim() === ""
      );
    });
  }, [schema, values]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        // Esc postpones rather than discards: the draft is a decision the user
        // made by opening the form, and throwing it away on a stray key press
        // would lose their typing.
        onPostpone();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onPostpone]);

  const blocked = missing.length > 0 || problems.length > 0;

  async function run(dryRun: boolean) {
    setBusy(dryRun ? "check" : "queue");
    try {
      const job = await onQueue(dryRun);
      if (!dryRun && job) setQueued(job.process_name ?? job.id);
    } finally {
      setBusy(null);
    }
  }

  async function enqueue() {
    if (!onEnqueue) return;
    setBusy("enqueue");
    try {
      const entryId = await onEnqueue({ gpus: [], exclusive, inputs: slots });
      if (entryId) setQueued(`queue entry ${entryId}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-stretch justify-end bg-[color-mix(in_srgb,var(--frame)_30%,transparent)] p-3"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onPostpone();
      }}
      role="dialog"
      aria-modal
      aria-label={`New ${schema?.title ?? draft.jobType}`}
    >
      <div className="mica-card mica-edge flex w-[min(32.5rem,100%)] flex-col overflow-hidden rounded-box border border-rule-strong shadow-[0_24px_64px_-16px_rgb(0_0_0/0.45)]">
        <div className="flex items-center gap-2 border-b border-rule px-4 py-2.5">
          <span className="rounded-selector border border-dashed border-rule-strong px-1.5 py-px font-mono t-meta text-ink-muted">
            draft
          </span>
          <h2 className="min-w-0 flex-1 truncate t-head font-semibold">
            {schema?.title ?? draft.jobType}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="grid size-7 shrink-0 place-items-center rounded-field text-ink-faint hover:bg-base-200 hover:text-base-content"
            title="Close, keeping the draft"
            aria-label="Close"
          >
            <X className="size-4" />
          </button>
        </div>

        {schema?.executable && (
          <div className="truncate border-b border-rule-soft bg-base-200/60 px-4 py-1 font-mono t-meta text-ink-faint">
            {schema.executable}
            {schema.subcommand ? ` ${schema.subcommand}` : ""}
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {hosts.length > 1 && (
            <div className="mb-3 flex items-center gap-2">
              <Label htmlFor="execution-host" className="shrink-0">
                Run on
              </Label>
              <Select
                id="execution-host"
                value={draft.host ?? "local"}
                onValueChange={(value) =>
                  onChange({ host: value === "local" ? null : value })
                }
              >
                {hosts.map((host) => (
                  <SelectItem key={host.name} value={host.name}>
                    {host.local
                      ? "this machine"
                      : `${host.name} (${host.hostname})`}
                  </SelectItem>
                ))}
              </Select>
            </div>
          )}
          {!projectDirectory && (
            <p className="mb-3 rounded-field border border-rule bg-base-200 px-2.5 py-1.5 t-label leading-snug text-ink-muted">
              No project is open, so this job cannot be queued. It can still be
              configured and postponed.
            </p>
          )}
          {schema ? (
            <JobForm
              schema={schema}
              values={values}
              onChange={(variable, value) => {
                // Editing a slot by hand replaces whatever was picked from a
                // queued job: the typed value is the intent now.
                if (variable in slots) {
                  setSlots((current) => {
                    const next = { ...current };
                    delete next[variable];
                    return next;
                  });
                }
                onChange({ values: { ...values, [variable]: value } });
              }}
              missing={missing}
              extraCli={extraCli}
              onExtraCliChange={(rows) => onChange({ extraCli: rows })}
              onBrowseNodes={(variable, label) =>
                setPicker({ variable, label, producer: null })
              }
              onJobDropped={(variable, label, producer) =>
                setPicker({ variable, label, producer })
              }
            />
          ) : (
            <p className="t-body text-ink-muted">
              No form is available for {draft.jobType}.
            </p>
          )}
        </div>

        {(problems.length > 0 || queued) && (
          <div className="border-t border-rule px-4 py-2">
            {problems.length > 0 ? (
              <div
                className="flex flex-col gap-1 t-label text-st-failed"
                role="alert"
              >
                {problems.map((problem) => (
                  <span key={problem} className="flex items-start gap-1.5">
                    <AlertCircle className="mt-px size-3 shrink-0" />
                    {problem}
                  </span>
                ))}
              </div>
            ) : (
              <span className="flex items-center gap-1.5 t-label text-st-succeeded">
                <Check className="size-3" />
                Queued as {queued}
              </span>
            )}
          </div>
        )}

        <div className="flex items-center gap-2 border-t border-rule bg-base-200/60 px-4 py-2.5">
          <Button
            size="sm"
            className="h-7"
            disabled={
              Boolean(busy) || !projectDirectory || blocked || Boolean(queued)
            }
            onClick={() => run(false)}
            title={
              !projectDirectory
                ? "open a project first"
                : missing.length > 0
                  ? "fill the required options"
                  : problems.length > 0
                    ? "resolve the option conflicts first"
                    : "Start this job now, on the chosen node"
            }
          >
            <Play className="size-3.5" />
            Start now
          </Button>
          {onEnqueue && (
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              disabled={
                Boolean(busy) || !projectDirectory || blocked || Boolean(queued)
              }
              onClick={enqueue}
              title="Wait for its inputs and a free GPU on the chosen node"
            >
              <ListPlus className="size-3.5" />
              Add to queue
            </Button>
          )}
          <label
            className="flex cursor-pointer items-center gap-1.5 t-meta text-ink-muted"
            title="Nothing else starts on this node while this job runs"
          >
            <input
              type="checkbox"
              className="checkbox checkbox-xs"
              checked={exclusive}
              onChange={(event) => setExclusive(event.target.checked)}
              disabled={!onEnqueue}
            />
            whole node
          </label>
          <span className="flex-1" />
          <Button
            variant="ghost"
            size="sm"
            className="h-7"
            disabled={Boolean(busy)}
            onClick={() => run(true)}
            title="Build the command without running it"
          >
            Check
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-7"
            disabled={Boolean(busy)}
            onClick={onPostpone}
            title="Keep the node on the canvas and close (Esc)"
          >
            <Clock className="size-3.5" />
            Postpone
          </Button>
          <button
            type="button"
            onClick={onDiscard}
            className="rounded-selector px-1.5 py-0.5 t-label text-ink-faint hover:bg-st-failed/10 hover:text-st-failed"
            title="Remove this node from the canvas"
          >
            Discard
          </button>
        </div>
      </div>

      {picker && projectDirectory && (
        <SlotPicker
          variable={picker.variable}
          label={picker.label}
          jobType={draft.jobType}
          projectDirectory={projectDirectory}
          droppedProducer={picker.producer}
          onPick={(choice) => {
            // The field shows the path, so the value is readable and still
            // works if the dialog is used without the queue; the slot is what
            // makes the queue wait for it.
            onChange({
              values: { ...values, [choice.variable]: choice.path },
            });
            setSlots((current) => {
              const next = { ...current };
              if (choice.slot) next[choice.variable] = choice.slot;
              else delete next[choice.variable];
              return next;
            });
            setPicker(null);
          }}
          onClose={() => setPicker(null)}
        />
      )}
    </div>
  );
}
