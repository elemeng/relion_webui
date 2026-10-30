import { useCallback, useEffect, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import {
  AlertCircle,
  CircleSlash,
  Clock,
  Cpu,
  RotateCcw,
  Server,
  Zap,
} from "lucide-react";

import { StatusDot } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { QueueEntry, QueueSnapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

const REFRESH_MS = 3000;

const STATE_INK: Record<QueueEntry["state"], string> = {
  pending: "text-[var(--st-scheduled-text)]",
  running: "text-[var(--st-running-text)]",
  succeeded: "text-[var(--st-succeeded-text)]",
  failed: "text-[var(--st-failed-text)]",
  cancelled: "text-[var(--st-aborted-text)]",
};

/**
 * The queue, above the node cards.
 *
 * The queue exists because jobs now wait for two things the node cards cannot
 * show: an upstream job's output, and a free GPU. A card can say "gpu1 has a
 * free card"; only the queue can say "job006 is waiting for that card, and
 * job007 is waiting for job006".
 *
 * Pending and running entries come first and get the space: they are the only
 * ones where a decision is still being made. History is a count, not a list,
 * because a scroll of finished entries would push the live ones off screen.
 */
export function QueuePanel() {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSnapshot(await api.get<QueueSnapshot>("/api/queue"));
      setError(null);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "could not read the queue",
      );
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const act = async (id: string, action: () => Promise<unknown>) => {
    setBusy(id);
    try {
      await action();
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "that did not work");
    } finally {
      setBusy(null);
    }
  };

  const live = (snapshot?.entries ?? []).filter(
    (entry) => entry.state === "pending" || entry.state === "running",
  );
  const counts = snapshot?.counts;

  return (
    <section
      className="rounded-box border border-rule bg-base-100"
      aria-label="Job queue"
    >
      <header className="flex flex-wrap items-center gap-2 border-b border-rule px-3 py-2">
        <Clock className="size-3.5 text-ink-faint" />
        <h2 className="t-ui font-semibold">Queue</h2>
        {counts && (
          <div className="flex items-center gap-2 t-meta text-ink-muted">
            {counts.running > 0 && (
              <span className="flex items-center gap-1">
                <StatusDot status="Running" />
                {counts.running} running
              </span>
            )}
            {counts.pending > 0 && (
              <span className="flex items-center gap-1">
                <StatusDot status="Scheduled" />
                {counts.pending} waiting
              </span>
            )}
            {counts.failed + counts.cancelled > 0 && (
              <span className="flex items-center gap-1">
                <StatusDot status="Failed" />
                {counts.failed + counts.cancelled} finished badly
              </span>
            )}
            {counts.succeeded > 0 && (
              <span className="flex items-center gap-1 text-ink-faint">
                <StatusDot status="Succeeded" />
                {counts.succeeded} done
              </span>
            )}
          </div>
        )}
        <span className="flex-1" />
        {snapshot && snapshot.hosts.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            {snapshot.hosts
              .filter((host) => host.enabled)
              .map((host) => (
                <HostPill key={host.host} host={host} />
              ))}
          </div>
        )}
      </header>

      {error && (
        <p
          className="flex items-start gap-1.5 border-b border-rule px-3 py-2 t-label text-st-failed"
          role="alert"
        >
          <AlertCircle className="mt-px size-3.5 shrink-0" />
          {error}
        </p>
      )}

      {live.length === 0 ? (
        <p className="px-3 py-3 t-label text-ink-muted">
          Nothing queued. A job added with{" "}
          <span className="font-medium">Add to queue</span> waits here until its
          inputs exist and a node has room.
        </p>
      ) : (
        <ul className="flex flex-col">
          {live.map((entry) => (
            <QueueRow
              key={entry.id}
              entry={entry}
              busy={busy === entry.id}
              onCancel={() =>
                act(entry.id, () => api.delete(`/api/queue/${entry.id}`))
              }
              onRetry={() =>
                act(entry.id, () => api.post(`/api/queue/${entry.id}/retry`))
              }
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function HostPill({ host }: { host: QueueSnapshot["hosts"][number] }) {
  const free = host.free_cores;
  const freeGpus = host.gpus?.filter((gpu) => !gpu.busy).length ?? null;
  return (
    <span
      className={cn(
        "flex items-center gap-1 rounded-[3px] px-1.5 py-px font-mono t-meta",
        host.problem
          ? "bg-[var(--st-failed-fill)] text-[var(--st-failed-text)]"
          : "bg-base-200 text-ink-muted",
      )}
      title={
        host.problem ?? `${host.used_cores} of ${host.cpus ?? "?"} cores in use`
      }
    >
      <Server className="size-2.5" />
      {host.host}
      {free != null && (
        <span className="text-ink-faint">
          {free}c
          {freeGpus != null && ` · ${freeGpus}/${host.gpus?.length ?? 0}gpu`}
        </span>
      )}
    </span>
  );
}

function QueueRow({
  entry,
  busy,
  onCancel,
  onRetry,
}: {
  entry: QueueEntry;
  busy: boolean;
  onCancel: () => void;
  onRetry: () => void;
}) {
  const [open, setOpen] = useState(false);
  const reason = entry.blocked_by[0] ?? null;

  return (
    <li className="border-t border-rule-soft first:border-t-0">
      <div className="flex flex-wrap items-center gap-2 px-3 py-1.5">
        <StatusDot
          status={entry.state === "running" ? "Running" : "Scheduled"}
        />
        <span className="font-mono t-meta text-ink-faint">{entry.id}</span>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className={cn(
            "min-w-0 truncate t-ui font-medium hover:underline",
            STATE_INK[entry.state],
          )}
          title={entry.process_name ?? entry.job_type}
        >
          {entry.job_type}
        </button>
        {entry.process_name && (
          <span className="truncate font-mono t-meta text-ink-faint">
            {entry.process_name}
          </span>
        )}
        <span className="flex items-center gap-1 t-meta text-ink-muted">
          <Server className="size-2.5" />
          {entry.host}
        </span>
        {entry.leased_gpus.length > 0 ? (
          <span className="flex items-center gap-1 t-meta text-ink-muted">
            <Zap className="size-2.5" />
            GPU {entry.leased_gpus.join(",")}
          </span>
        ) : (
          entry.gpus.length > 0 && (
            <span className="t-meta text-ink-faint">
              GPU {entry.gpus.join(",")} pinned
            </span>
          )
        )}
        <span className="flex items-center gap-1 t-meta text-ink-faint">
          <Cpu className="size-2.5" />
          {entry.cores}c
        </span>
        {entry.exclusive && (
          <span
            className="rounded-[2px] bg-base-200 px-1 t-meta text-ink-muted"
            title="Nothing else starts on this node while it runs"
          >
            whole node
          </span>
        )}
        <span className="flex-1" />

        {entry.state === "pending" && reason && (
          <span
            className="min-w-0 truncate t-meta text-ink-muted"
            title={entry.blocked_by.join("; ")}
          >
            {reason}
          </span>
        )}
        {entry.state === "running" && (
          <span className="t-meta text-ink-faint">
            {duration(
              entry.started_at ? Date.now() / 1000 - entry.started_at : 0,
            )}
          </span>
        )}
        {entry.state === "pending" && entry.wait_seconds > 1 && (
          <span className="t-meta text-ink-faint">
            waited {duration(entry.wait_seconds)}
          </span>
        )}

        {entry.state === "pending" ? (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-1.5"
            disabled={busy}
            onClick={onCancel}
            title="Remove from the queue"
          >
            {busy ? (
              <Spinner size="xs" />
            ) : (
              <CircleSlash className="size-3" />
            )}
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-1.5"
            disabled={busy}
            onClick={onCancel}
            title="Stop this job"
          >
            {busy ? (
              <Spinner size="xs" />
            ) : (
              <CircleSlash className="size-3" />
            )}
          </Button>
        )}
      </div>

      {open && (
        <div className="flex flex-col gap-1.5 border-t border-rule-soft bg-base-200/50 px-3 py-2">
          <Field label="project">{entry.project}</Field>
          {entry.process_name && (
            <Field label="reserved as">
              <span title="Its number is fixed now, so its output paths are known before it runs">
                {entry.process_name}
              </span>
            </Field>
          )}
          {Object.keys(entry.predicted_outputs).length > 0 && (
            <Field label="will write">
              {Object.entries(entry.predicted_outputs)
                .map(([name, path]) => `${name} → ${path}`)
                .join("; ")}
            </Field>
          )}
          {entry.derived_dependencies.length > 0 && (
            <Field label="waits for jobs">
              {entry.derived_dependencies.join(", ")}
            </Field>
          )}
          {entry.depends_on.length > 0 && (
            <Field label="waits for entries">
              {entry.depends_on.join(", ")}
            </Field>
          )}
          {entry.after_jobs.length > 0 && (
            <Field label="declared after">{entry.after_jobs.join(", ")}</Field>
          )}
          {entry.after_files.length > 0 && (
            <Field label="waits for files">
              {entry.after_files.join(", ")}
            </Field>
          )}
          {entry.blocked_by.length > 0 && (
            <Field label="blocked">
              <span className="text-ink-muted">
                {entry.blocked_by.join("; ")}
              </span>
            </Field>
          )}
          {entry.warnings.length > 0 && (
            <Field label="warnings">
              <span className="text-[var(--st-scheduled-text)]">
                {entry.warnings.join("; ")}
              </span>
            </Field>
          )}
          {entry.error && (
            <Field label="error">
              <span className="text-st-failed">{entry.error}</span>
            </Field>
          )}
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-6"
              onClick={onRetry}
            >
              <RotateCcw className="size-3" />
              Queue again
            </Button>
            <span className="t-meta text-ink-faint">
              arguments: {Object.keys(entry.arguments).length} options
            </span>
          </div>
        </div>
      )}
    </li>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-24 shrink-0 t-meta text-ink-faint">{label}</span>
      <span className="min-w-0 flex-1 break-words font-mono t-meta">
        {children}
      </span>
    </div>
  );
}

function duration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
  return `${total}s`;
}
