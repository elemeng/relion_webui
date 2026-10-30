// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useCallback, useEffect, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import {
  AlertCircle,
  ChevronDown,
  Cpu,
  Gauge,
  MemoryStick,
  MinusCircle,
  Plus,
  Power,
  RefreshCw,
  Server,
  Thermometer,
  Trash2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tip } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import type {
  ExecutionHost,
  NodeGpu,
  NodeJob,
  NodeSnapshot,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { QueuePanel } from "@/views/QueuePanel";

const REFRESH_MS = 5000;

/**
 * The cluster: one tall card per machine, with what is running on it.
 *
 * A card is deliberately a column, not a row: a node has a fixed set of GPUs and
 * a variable list of jobs, and a column lets the job list grow downward while the
 * GPU meters stay at eye level in the same place on every card. The alternative -
 * a table of all jobs - loses the grouping that makes "this node is out of
 * memory" answerable at a glance.
 *
 * Metrics are polled rather than pushed: one ssh round trip per node every few
 * seconds is cheap, and a websocket channel per node would be a lot of machinery
 * for data nobody reads between polls.
 */
export function NodesView() {
  const [hosts, setHosts] = useState<ExecutionHost[]>([]);
  const [snapshots, setSnapshots] = useState<Record<string, NodeSnapshot>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async (showSpinner = false) => {
    if (showSpinner) setLoading(true);
    try {
      const [hostList, nodes] = await Promise.all([
        api.get<{ hosts: ExecutionHost[] }>("/api/hosts"),
        api.get<{ nodes: NodeSnapshot[] }>("/api/nodes"),
      ]);
      setHosts(hostList.hosts);
      setSnapshots(
        Object.fromEntries(nodes.nodes.map((node) => [node.name, node])),
      );
      setError(null);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "could not read the cluster",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(true);
    const timer = window.setInterval(() => load(false), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const act = async (name: string, action: () => Promise<unknown>) => {
    setBusy(name);
    try {
      await action();
      await load(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "that did not work");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 px-5 py-5">
        <div className="flex flex-wrap items-center gap-2.5">
          <h1 className="t-head font-semibold">GPU nodes</h1>
          <span className="rounded-[3px] bg-secondary px-1.5 py-px font-mono t-meta text-ink-muted">
            {hosts.filter((host) => !host.local && host.enabled).length} remote
          </span>
          <span className="flex-1" />
          <Button
            variant="outline"
            size="sm"
            onClick={() => load(true)}
            title="Refresh now"
          >
            <RefreshCw className={cn("size-3.5", loading && "animate-spin")} />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setAdding((value) => !value)}>
            <Plus className="size-3.5" />
            Add node
          </Button>
        </div>

        {adding && (
          <AddNodeForm
            onCancel={() => setAdding(false)}
            onAdded={async () => {
              setAdding(false);
              await load(true);
            }}
            onError={setError}
          />
        )}

        {error && (
          <p
            className="flex items-start gap-1.5 rounded-[4px] border border-[color-mix(in_srgb,var(--st-failed-text)_35%,var(--rule))] bg-[var(--st-failed-fill)] px-2.5 py-2 t-label text-[var(--st-failed-text)]"
            role="alert"
          >
            <AlertCircle className="mt-px size-3.5 shrink-0" />
            {error}
          </p>
        )}

        {loading && hosts.length === 0 ? (
          <div className="flex items-center gap-2 p-8">
            <Spinner size="sm" className="text-ink-faint" />
            <span className="t-ui text-ink-muted">reading the cluster…</span>
          </div>
        ) : (
          <>
            <QueuePanel />
            <div className="flex flex-wrap items-start gap-4">
              {hosts
                .filter((host) => !host.local)
                .map((host) => (
                  <NodeCard
                    key={host.name}
                    host={host}
                    snapshot={snapshots[host.name]}
                    busy={busy === host.name}
                    onToggle={() =>
                      act(host.name, () =>
                        api.patch(`/api/hosts/${host.name}`, {
                          enabled: !host.enabled,
                        }),
                      )
                    }
                    onRemove={() =>
                      act(host.name, () =>
                        api.delete(`/api/hosts/${host.name}`),
                      )
                    }
                  />
                ))}
              <NodeCard
                host={
                  hosts.find((host) => host.local) ?? {
                    name: "local",
                    hostname: "",
                    port: 22,
                    user: null,
                    local: true,
                    enabled: true,
                    note: "the machine running the agent",
                  }
                }
                snapshot={snapshots.local}
                local
              />
            </div>
          </>
        )}

        {hosts.filter((host) => !host.local).length === 0 && !adding && (
          <div className="rounded-box border border-dashed border-rule px-5 py-8">
            <p className="t-ui font-medium">No remote nodes yet</p>
            <p className="mt-1.5 max-w-xl t-body leading-relaxed text-ink-muted">
              Add a GPU machine to send jobs to. It needs key-based ssh from
              this account (
              <code className="font-mono t-meta">ssh-copy-id</code>) and must
              see the project directories at the same paths — the agent and the
              node share storage rather than copying files.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- node card */

function NodeCard({
  host,
  snapshot,
  busy,
  local,
  onToggle,
  onRemove,
}: {
  host: ExecutionHost;
  snapshot?: NodeSnapshot;
  busy?: boolean;
  local?: boolean;
  onToggle?: () => void;
  onRemove?: () => void;
}) {
  const reachable = snapshot?.reachable ?? false;
  const jobs = snapshot?.jobs ?? [];
  const gpus = snapshot?.gpus ?? [];
  const problem = snapshot?.problem ?? null;

  return (
    <section
      className={cn(
        // A fixed width and a minimum height: a row of cards of different
        // heights reads as broken, and the job list is what varies, so the card
        // grows from a common floor rather than shrinking to its content.
        "flex min-h-[420px] w-[380px] max-w-full flex-col overflow-hidden rounded-box border bg-base-100",
        host.enabled
          ? "border-rule"
          : "border-dashed border-rule-strong opacity-70",
      )}
      aria-label={`Node ${host.name}`}
    >
      <header className="flex items-start gap-2 border-b border-rule px-3 py-2.5">
        <Server
          className={cn(
            "mt-0.5 size-4 shrink-0",
            !host.enabled
              ? "text-ink-faint"
              : reachable
                ? "text-st-succeeded"
                : "text-st-failed",
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-1.5">
            <span className="truncate t-ui font-semibold">{host.name}</span>
            {local && (
              <span className="shrink-0 rounded-[2px] bg-base-200 px-1 t-meta text-ink-muted">
                this machine
              </span>
            )}
            {!host.enabled && (
              <span className="shrink-0 rounded-[2px] bg-base-200 px-1 t-meta text-ink-muted">
                disabled
              </span>
            )}
          </div>
          <div className="truncate font-mono t-meta text-ink-faint">
            {local
              ? "local"
              : `${host.hostname}${host.port !== 22 ? `:${host.port}` : ""}`}
            {host.note ? ` · ${host.note}` : ""}
          </div>
        </div>
        {!local && (
          <div className="flex shrink-0 items-center gap-0.5">
            {onToggle && (
              <button
                type="button"
                onClick={onToggle}
                disabled={busy}
                className="grid size-6 place-items-center rounded-[3px] text-ink-faint hover:bg-base-200 hover:text-base-content disabled:opacity-40"
                title={host.enabled ? "Disable this node" : "Enable this node"}
                aria-label={host.enabled ? "Disable" : "Enable"}
              >
                {busy ? (
                  <Spinner size="sm" />
                ) : host.enabled ? (
                  <MinusCircle className="size-3.5" />
                ) : (
                  <Plus className="size-3.5" />
                )}
              </button>
            )}
            {onRemove && (
              <button
                type="button"
                onClick={onRemove}
                disabled={busy}
                className="grid size-6 place-items-center rounded-[3px] text-ink-faint hover:bg-st-failed/10 hover:text-st-failed disabled:opacity-40"
                title="Remove this node from the list"
                aria-label="Remove"
              >
                <Trash2 className="size-3.5" />
              </button>
            )}
          </div>
        )}
      </header>

      {!host.enabled ? (
        <p className="px-3 py-4 t-label text-ink-muted">
          Disabled: not offered for new jobs and not polled.
        </p>
      ) : !snapshot ? (
        <p className="flex items-center gap-1.5 px-3 py-4 t-label text-ink-faint">
          <Spinner size="xs" />
          waiting for the first poll
        </p>
      ) : !reachable ? (
        <div className="flex flex-1 flex-col gap-2 px-3 py-3">
          <p className="flex items-start gap-1.5 t-label leading-relaxed text-st-failed">
            <AlertCircle className="mt-px size-3.5 shrink-0" />
            {problem ?? "unreachable"}
          </p>
          <p className="t-meta leading-relaxed text-ink-faint">
            Its jobs cannot be seen while it is unreachable. Check that the
            machine is up and that ssh works from this account:
          </p>
          <code className="rounded-[3px] bg-base-200 px-1.5 py-1 font-mono t-meta">
            ssh {host.user ? `${host.user}@` : ""}
            {host.hostname}
          </code>
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-2 px-3 py-2.5">
            <Meter
              icon={<Cpu className="size-3" />}
              label="CPU"
              detail={`${snapshot.cpus} cores · load ${snapshot.load_1?.toFixed(2) ?? "—"}`}
              percent={snapshot.load_percent}
            />
            <Meter
              icon={<MemoryStick className="size-3" />}
              label="Memory"
              detail={`${gb(snapshot.memory_used_mb)} of ${gb(snapshot.memory_total_mb)}`}
              percent={percentOf(
                snapshot.memory_used_mb,
                snapshot.memory_total_mb,
              )}
            />
          </div>

          {gpus.length > 0 && (
            <div className="flex flex-col gap-1.5 border-t border-rule-soft px-3 py-2.5">
              <span className="t-section text-ink-faint">
                {gpus.length} GPU{gpus.length === 1 ? "" : "s"}
              </span>
              {gpus.map((gpu) => (
                <GpuRow key={gpu.index} gpu={gpu} />
              ))}
            </div>
          )}

          <div className="flex min-h-0 flex-1 flex-col border-t border-rule-soft">
            <div className="flex items-center gap-1.5 px-3 py-1.5">
              <span className="t-section text-ink-faint">
                {jobs.length > 0
                  ? `${jobs.length} job${jobs.length === 1 ? "" : "s"} running`
                  : "idle"}
              </span>
            </div>
            {jobs.length > 0 && (
              <ul className="flex flex-col">
                {jobs.map((job) => (
                  <JobRow key={job.job_directory} job={job} />
                ))}
              </ul>
            )}
          </div>
        </>
      )}

      {snapshot?.uptime_seconds != null && reachable && (
        <footer className="border-t border-rule-soft px-3 py-1 t-meta text-ink-faint">
          up {duration(snapshot.uptime_seconds)}
        </footer>
      )}
    </section>
  );
}

function GpuRow({ gpu }: { gpu: NodeGpu }) {
  const memoryPercent = percentOf(gpu.memory_used_mb, gpu.memory_total_mb);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline gap-1.5">
        <Gauge className="size-3 shrink-0 text-ink-faint" />
        <span className="min-w-0 flex-1 truncate t-meta" title={gpu.name}>
          {gpu.name.replace(/NVIDIA\s+|GeForce\s+/g, "")}
        </span>
        {gpu.temperature_c != null && (
          <span className="flex shrink-0 items-center gap-0.5 font-mono t-meta text-ink-faint">
            <Thermometer className="size-2.5" />
            {gpu.temperature_c}°
          </span>
        )}
        {gpu.power_watts != null && (
          <Tip
            text={
              gpu.power_limit_watts
                ? `${gpu.power_watts} W of ${gpu.power_limit_watts} W`
                : `${gpu.power_watts} W`
            }
          >
            <span className="flex shrink-0 items-center gap-0.5 font-mono t-meta text-ink-faint">
              <Power className="size-2.5" />
              {Math.round(gpu.power_watts)}W
            </span>
          </Tip>
        )}
      </div>
      <div className="flex items-center gap-2">
        <span className="w-8 shrink-0 font-mono t-meta text-ink-faint">
          {gpu.utilization_percent != null
            ? `${Math.round(gpu.utilization_percent)}%`
            : "—"}
        </span>
        <div className="h-1 flex-1 overflow-hidden rounded-full bg-base-300">
          <div
            className="h-full rounded-full bg-signal"
            style={{ width: `${clamp(gpu.utilization_percent ?? 0)}%` }}
          />
        </div>
        <span className="shrink-0 font-mono t-meta text-ink-muted">
          {gb(gpu.memory_used_mb)}/{gb(gpu.memory_total_mb)}
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-base-300">
        <div
          className="h-full rounded-full bg-st-aborted-text/70"
          style={{ width: `${clamp(memoryPercent ?? 0)}%` }}
          title="GPU memory in use"
        />
      </div>
    </div>
  );
}

function JobRow({ job }: { job: NodeJob }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="border-t border-rule-soft first:border-t-0">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left hover:bg-base-200"
        aria-expanded={open}
      >
        <ChevronDown
          className={cn(
            "size-3 shrink-0 text-ink-faint transition-transform",
            open && "rotate-180",
          )}
        />
        <span className="shrink-0 font-mono t-meta text-ink-muted">
          {job.process_name ?? "—"}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono t-meta text-ink-faint">
          {job.project ? job.project.split("/").pop() : ""}
        </span>
        <span className="shrink-0 font-mono t-meta text-ink-muted">
          {Math.round(job.cpu_percent)}%
        </span>
        <span className="w-14 shrink-0 text-right font-mono t-meta text-ink-muted">
          {gb(job.memory_mb)}
        </span>
        {job.gpu_memory_mb > 0 && (
          <span className="w-12 shrink-0 text-right font-mono t-meta text-signal">
            {gb(job.gpu_memory_mb)}
          </span>
        )}
      </button>
      {open && (
        <div className="bg-base-200/50 px-3 py-1.5">
          <dl className="flex flex-col gap-0.5 font-mono t-meta text-ink-muted">
            <div className="flex gap-2">
              <dt className="w-16 shrink-0 text-ink-faint">pids</dt>
              <dd className="min-w-0 break-all">{job.pids.join(", ")}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-16 shrink-0 text-ink-faint">dir</dt>
              <dd className="min-w-0 break-all">{job.job_directory}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-16 shrink-0 text-ink-faint">cpu</dt>
              <dd>
                {job.cpu_percent.toFixed(1)}% · {gb(job.memory_mb)} RAM
              </dd>
            </div>
          </dl>
        </div>
      )}
    </li>
  );
}

function Meter({
  icon,
  label,
  detail,
  percent,
}: {
  icon: React.ReactNode;
  label: string;
  detail: string;
  percent: number | null;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline gap-1.5">
        <span className="text-ink-faint">{icon}</span>
        <span className="t-meta font-medium">{label}</span>
        <span className="min-w-0 flex-1 truncate t-meta text-ink-faint">
          {detail}
        </span>
        <span className="shrink-0 font-mono t-meta text-ink-muted">
          {percent == null ? "—" : `${Math.round(percent)}%`}
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-base-300">
        <div
          className={cn(
            "h-full rounded-full",
            (percent ?? 0) > 90 ? "bg-st-failed-text" : "bg-signal",
          )}
          style={{ width: `${clamp(percent ?? 0)}%` }}
        />
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------- add form */

function AddNodeForm({
  onCancel,
  onAdded,
  onError,
}: {
  onCancel: () => void;
  onAdded: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [spec, setSpec] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!spec.trim()) return;
    setBusy(true);
    try {
      await api.post("/api/hosts", {
        spec: spec.trim(),
        note: note.trim() || undefined,
      });
      await onAdded();
    } catch (caught) {
      onError(
        caught instanceof Error ? caught.message : "could not add the node",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="flex flex-col gap-2 rounded-box border border-rule bg-base-100 p-3"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <Label htmlFor="node-spec">Node</Label>
          <Input
            id="node-spec"
            value={spec}
            onChange={(event) => setSpec(event.target.value)}
            placeholder="gpu1=gpu1.lab  or  gpu2=user@gpu2.lab:2222"
            className="font-mono"
            autoFocus
          />
        </div>
        <div className="flex w-56 flex-col gap-1">
          <Label htmlFor="node-note">Note (optional)</Label>
          <Input
            id="node-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="2× A100 40 GB"
          />
        </div>
        <Button
          type="submit"
          size="sm"
          className="h-8"
          disabled={busy || !spec.trim()}
        >
          {busy && <Spinner size="sm" />}
          Add
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8"
          onClick={onCancel}
        >
          Cancel
        </Button>
      </div>
      <p className="t-meta leading-relaxed text-ink-muted">
        <code className="font-mono">name=hostname[:port]</code>, optionally{" "}
        <code className="font-mono">user@</code>. The node needs key-based ssh
        from this account and must see the project directories at the same
        paths.
      </p>
    </form>
  );
}

/* ---------------------------------------------------------------------- helpers */

function percentOf(used: number | null, total: number | null): number | null {
  if (used == null || total == null || total <= 0) return null;
  return (100 * used) / total;
}

function clamp(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function gb(mb: number | null | undefined): string {
  if (mb == null) return "—";
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)}G`;
  return `${Math.round(mb)}M`;
}

function duration(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}
