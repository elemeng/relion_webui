// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  ChevronRight,
  Copy,
  FileText,
  Square,
  Trash2,
  Wrench,
  X,
} from "lucide-react";

import { StatusDot, StatusPill } from "@/components/Status";
import { JobForm } from "@/components/JobForm";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  Job,
  JobDetail,
  JobFieldValues,
  JobFormSchema,
  LogChunk,
  PipelineGraph,
  PipelineNode,
  PipelineProcess,
} from "@/lib/types";

interface JobDialogProps {
  process: PipelineProcess;
  graph: PipelineGraph;
  liveJob?: Job;
  /** Which tab to open on; the node menu jumps straight to one. */
  initialTab?: JobDialogTab;
  schema: JobFormSchema | null;
  /**
   * The whole job's schema, for the parameters tab. A variant's job.star holds
   * the base job's option table, so this is what its recorded options are
   * keyed against.
   */
  parameterSchema: JobFormSchema | null;
  logChunk: LogChunk | null;
  onClose: () => void;
  onOpenProcess: (process: PipelineProcess) => void;
  projectDirectory: string | null;
  onStop?: (jobId: string) => void;
  onForget?: (jobId: string) => void;
  onActions?: () => void;
  onUseAsInput?: (node: PipelineNode) => void;
}

export type JobDialogTab =
  "overview" | "log" | "parameters" | "outputs" | "volumes" | "metadata";

const VOLUME_EXTENSIONS = [".mrc", ".map", ".mrcs", ".ccp4"];

function isVolume(name: string): boolean {
  const lower = name.toLowerCase();
  return VOLUME_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

/**
 * Everything about one job, in tabs.
 *
 * Opening a card gives the whole picture rather than a summary: the desktop
 * GUI's job window shows options and then hands off to `logfile.pdf`, so the
 * results are only reachable by leaving the app. Here the previews, the log, the
 * parameters and the recorded metadata are tabs of the same surface.
 *
 * The tab set job dialog, minus the parts RELION gives us
 * no data for (no per-iteration charts), and with Metadata carrying the
 * pipeline record rather than a database row.
 */
export function JobDialog({
  process,
  graph,
  liveJob,
  initialTab = "overview",
  schema,
  parameterSchema,
  logChunk,
  onClose,
  onOpenProcess,
  projectDirectory,
  onStop,
  onForget,
  onActions,
  onUseAsInput,
}: JobDialogProps) {
  const produced = useMemo(
    () => graph.nodes.filter((node) => node.produced_by === process.name),
    [graph.nodes, process.name],
  );
  const consumed = useMemo(
    () => graph.nodes.filter((node) => node.consumed_by.includes(process.name)),
    [graph.nodes, process.name],
  );
  const volumes = produced.filter((node) => isVolume(node.name));

  const upstream = useMemo(
    () => relatedProcesses(graph, process.name, "upstream"),
    [graph, process.name],
  );
  const downstream = useMemo(
    () => relatedProcesses(graph, process.name, "downstream"),
    [graph, process.name],
  );

  // Volumes is a conditional tab, so a request for it has to fall back when
  // this job writes none rather than leaving the dialog on a missing tab.
  const [tab, setTab] = useState<JobDialogTab>(() => {
    if (initialTab === "volumes" && volumes.length === 0) return "overview";
    return initialTab;
  });

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

  const tabs: { value: JobDialogTab; label: string; count?: number }[] = [
    { value: "overview", label: "Overview" },
    { value: "log", label: "Log" },
    { value: "parameters", label: "Inputs & parameters" },
    { value: "outputs", label: "Outputs", count: produced.length },
    ...(volumes.length > 0
      ? [
          {
            value: "volumes" as JobDialogTab,
            label: "Volumes",
            count: volumes.length,
          },
        ]
      : []),
    { value: "metadata", label: "Metadata" },
  ];

  // Portalled to <body>: the shell's main grid is `overflow-hidden`, so an
  // absolutely positioned overlay only covers the grid cell and anything
  // outside it - the rail, the canvas' own floating controls - stays on top.
  return createPortal(
    <div
      className="fixed inset-0 z-40 flex items-stretch bg-[color-mix(in_srgb,var(--frame)_45%,transparent)] p-5"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal
      aria-label={`Job ${process.name}`}
    >
      <div className="mica-card mica-edge flex min-w-0 flex-1 flex-col overflow-hidden rounded-box border border-rule shadow-2xl">
        <div className="flex items-center gap-2.5 border-b border-rule px-4 py-2.5">
          <StatusDot status={process.status} className="size-2" />
          <span className="shrink-0 rounded-selector bg-base-200 px-1.5 py-0.5 font-mono t-meta text-ink-muted">
            {jobNumber(process.name)}
          </span>
          <h2 className="min-w-0 truncate t-head font-semibold">
            {process.alias !== "None" ? process.alias : process.type_label}
          </h2>
          <span className="hidden min-w-0 truncate font-mono t-meta text-ink-faint sm:inline">
            {process.name}
          </span>
          <span className="flex-1" />
          {liveJob && liveJob.duration > 0 && (
            <span className="shrink-0 font-mono t-meta text-ink-muted">
              {formatDuration(liveJob.duration)}
            </span>
          )}
          <StatusPill status={process.status} />
          {onActions && (
            <Button
              variant="outline"
              size="sm"
              className="h-6 shrink-0"
              onClick={onActions}
              title="Alias, clean, note, delete"
            >
              <Wrench className="size-3" />
              Actions
            </Button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="grid size-7 shrink-0 place-items-center rounded-field text-ink-faint hover:bg-base-200 hover:text-base-content"
            title="Close (Esc)"
            aria-label="Close"
          >
            <X className="size-4" />
          </button>
        </div>

        <Tabs
          value={tab}
          onValueChange={(value) => setTab(value as JobDialogTab)}
          className="flex min-h-0 flex-1 flex-col gap-0"
        >
          <TabsList className="h-auto w-full shrink-0 justify-start gap-0 overflow-x-auto rounded-none border-b border-rule bg-transparent px-3">
            {tabs.map(({ value, label, count }) => (
              <TabsTrigger key={value} value={value}>
                {label}
                {count !== undefined && count > 0 && (
                  <span className="ml-1.5 rounded-[0.125rem] bg-base-200 px-1 font-mono t-meta text-ink-muted">
                    {count}
                  </span>
                )}
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent
            value="overview"
            className="min-h-0 flex-1 overflow-hidden"
          >
            <Overview
              process={process}
              liveJob={liveJob}
              volumes={volumes}
              produced={produced}
              consumed={consumed}
              upstream={upstream}
              downstream={downstream}
              logChunk={logChunk}
              projectDirectory={projectDirectory}
              onOpenProcess={onOpenProcess}
            />
          </TabsContent>

          <TabsContent value="log" className="min-h-0 flex-1 overflow-hidden">
            <DialogLog
              process={process}
              liveJob={liveJob}
              logChunk={logChunk}
              projectDirectory={projectDirectory}
            />
          </TabsContent>

          <TabsContent
            value="parameters"
            className="min-h-0 flex-1 overflow-y-auto p-4"
          >
            <ParametersPanel
              schema={parameterSchema ?? schema}
              consumed={consumed}
              processName={process.name}
              projectDirectory={projectDirectory}
            />
          </TabsContent>

          <TabsContent
            value="outputs"
            className="min-h-0 flex-1 overflow-y-auto p-4"
          >
            <OutputsPanel
              produced={produced}
              graph={graph}
              onUseAsInput={onUseAsInput}
            />
          </TabsContent>

          <TabsContent
            value="volumes"
            className="min-h-0 flex-1 overflow-y-auto p-4"
          >
            <VolumesPanel volumes={volumes} />
          </TabsContent>

          <TabsContent
            value="metadata"
            className="min-h-0 flex-1 overflow-y-auto p-4"
          >
            <MetadataPanel
              process={process}
              liveJob={liveJob}
              onStop={onStop}
              onForget={onForget}
              onActions={onActions}
            />
          </TabsContent>
        </Tabs>
      </div>
    </div>,
    document.body,
  );
}

function Overview({
  process,
  liveJob,
  volumes,
  produced,
  consumed,
  upstream,
  downstream,
  logChunk,
  projectDirectory,
  onOpenProcess,
}: {
  process: PipelineProcess;
  liveJob?: Job;
  volumes: PipelineNode[];
  produced: PipelineNode[];
  consumed: PipelineNode[];
  upstream: PipelineProcess[];
  downstream: PipelineProcess[];
  logChunk: LogChunk | null;
  projectDirectory: string | null;
  onOpenProcess: (process: PipelineProcess) => void;
}) {
  return (
    <div className="grid h-full grid-cols-[minmax(0,1fr)_15.5rem] overflow-hidden">
      <div className="flex min-h-0 flex-col gap-3 overflow-y-auto p-4">
        <div className="flex flex-wrap gap-2">
          <Kpi label="Pipeline">
            <StatusPill status={process.status} />
          </Kpi>
          {liveJob && (
            <Kpi label="Process">
              <StatusPill status={liveJob.status} />
            </Kpi>
          )}
          {liveJob && liveJob.duration > 0 && (
            <Kpi label="Elapsed">
              <span className="t-ui font-semibold">
                {formatDuration(liveJob.duration)}
              </span>
            </Kpi>
          )}
          <Kpi label="Inputs">
            <span className="t-ui font-semibold">{consumed.length}</span>
          </Kpi>
          <Kpi label="Outputs">
            <span className="t-ui font-semibold">{produced.length}</span>
          </Kpi>
        </div>

        {volumes.length > 0 ? (
          <div
            className={cn(
              "grid gap-2.5",
              volumes.length > 1 ? "grid-cols-2" : "grid-cols-1",
            )}
          >
            {volumes.slice(0, 2).map((node) => (
              <Preview key={node.name} node={node} />
            ))}
          </div>
        ) : (
          <div className="rounded-box border border-rule bg-base-200/60 px-3.5 py-3">
            <p className="t-label leading-relaxed text-ink-muted">
              {produced.length === 0
                ? "No output nodes yet. Previews appear here when the job writes a volume."
                : "This job writes no volumes, so there is nothing to render. Its outputs are listed on the right."}
            </p>
          </div>
        )}

        {/* The log grows into whatever height the KPI row and preview leave,
            rather than sitting at a fixed 176px with a void beneath it. */}
        <div className="flex min-h-[7.5rem] flex-1 flex-col">
          <DialogLog
            process={process}
            liveJob={liveJob}
            logChunk={logChunk}
            projectDirectory={projectDirectory}
            compact
          />
        </div>
      </div>

      <div className="overflow-y-auto border-l border-rule bg-base-200/40 p-3">
        <SideSection title="Outputs" count={produced.length}>
          {produced.length === 0 ? (
            <p className="t-meta text-ink-faint">none yet</p>
          ) : (
            produced.map((node) => (
              <div
                key={node.name}
                className="truncate rounded-selector border border-rule bg-base-100 px-2 py-1.5 font-mono t-meta"
                title={node.name}
              >
                {basename(node.name)}
              </div>
            ))
          )}
        </SideSection>

        <SideSection title="Upstream" count={upstream.length}>
          {upstream.length === 0 ? (
            <p className="t-meta text-ink-faint">external data</p>
          ) : (
            upstream.map((entry) => (
              <RelatedRow
                key={entry.name}
                process={entry}
                onOpen={() => onOpenProcess(entry)}
              />
            ))
          )}
        </SideSection>

        <SideSection title="Downstream" count={downstream.length}>
          {downstream.length === 0 ? (
            <p className="t-meta text-ink-faint">nothing consumes it yet</p>
          ) : (
            downstream.map((entry) => (
              <RelatedRow
                key={entry.name}
                process={entry}
                onOpen={() => onOpenProcess(entry)}
              />
            ))
          )}
        </SideSection>
      </div>
    </div>
  );
}

function OutputsPanel({
  produced,
  graph,
  onUseAsInput,
}: {
  produced: PipelineNode[];
  graph: PipelineGraph;
  onUseAsInput?: (node: PipelineNode) => void;
}) {
  if (produced.length === 0) {
    return (
      <p className="t-body leading-relaxed text-ink-muted">
        This job has not written any nodes. RELION records outputs in{" "}
        <code className="font-mono t-label">RELION_OUTPUT_NODES.star</code> when
        the job finishes.
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-1.5">
      {produced.map((node) => {
        const consumers = node.consumed_by
          .map((name) => graph.processes.find((entry) => entry.name === name))
          .filter((entry): entry is PipelineProcess => Boolean(entry));
        return (
          <li
            key={node.name}
            className="flex items-center gap-2 rounded-field border border-rule px-2.5 py-2"
          >
            <FileText className="size-3.5 shrink-0 text-ink-faint" />
            <span className="min-w-0 flex-1 truncate font-mono t-meta">
              {basename(node.name)}
            </span>
            <span className="hidden shrink-0 t-meta text-ink-faint md:inline">
              {node.type_label}
            </span>
            <span className="shrink-0 t-meta text-ink-faint">
              {consumers.length === 0
                ? "unused"
                : `${consumers.length} consumer${consumers.length === 1 ? "" : "s"}`}
            </span>
            {onUseAsInput && (
              <button
                type="button"
                onClick={() => onUseAsInput(node)}
                className="shrink-0 t-meta text-signal hover:underline"
                title="Attach this output to a new job"
              >
                use as input
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function VolumesPanel({ volumes }: { volumes: PipelineNode[] }) {
  const [axis, setAxis] = useState<"z" | "y" | "x">("z");
  const [mode, setMode] = useState<"slice" | "projection">("slice");

  if (volumes.length === 0) {
    return (
      <p className="t-body text-ink-muted">
        This job writes no volumes, so there is nothing to render.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          value={mode}
          options={[
            ["slice", "Central slice"],
            ["projection", "Projection"],
          ]}
          onChange={setMode}
          ariaLabel="Render mode"
        />
        {mode === "slice" && (
          <Segmented
            value={axis}
            options={[
              ["z", "z"],
              ["y", "y"],
              ["x", "x"],
            ]}
            onChange={setAxis}
            ariaLabel="Slice axis"
          />
        )}
        <span className="flex-1" />
        <span className="font-mono t-meta text-ink-faint">
          {volumes.length} volume{volumes.length === 1 ? "" : "s"}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {volumes.map((node) => (
          <Preview
            key={node.name}
            node={node}
            axis={mode === "slice" ? axis : null}
          />
        ))}
      </div>
    </div>
  );
}

function ParametersPanel({
  schema,
  consumed,
  processName,
  projectDirectory,
}: {
  schema: JobFormSchema | null;
  consumed: PipelineNode[];
  processName: string;
  projectDirectory: string | null;
}) {
  const [recorded, setRecorded] = useState<Record<string, string> | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "missing" | "error">(
    "loading",
  );

  // What the job actually ran with. This exists only in job.star, so it has to
  // be read back; the schema defaults are a fallback, never the answer.
  useEffect(() => {
    if (!projectDirectory) {
      setState("missing");
      return;
    }
    let cancelled = false;
    setState("loading");
    api
      .get<{ options: Record<string, string> | null }>(
        `/api/project/job/options?project_directory=${encodeURIComponent(
          projectDirectory,
        )}&process_name=${encodeURIComponent(processName)}`,
      )
      .then((data) => {
        if (cancelled) return;
        if (data.options === null) {
          setState("missing");
          return;
        }
        setRecorded(data.options);
        setState("ready");
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [processName, projectDirectory]);

  if (!schema) {
    return (
      <p className="t-body leading-relaxed text-ink-muted">
        No form schema for this job type, so its options cannot be laid out. The
        values it ran with are in its{" "}
        <code className="font-mono">job.star</code>.
      </p>
    );
  }

  const defaults: JobFieldValues = {};
  for (const field of schema.fields) {
    if (field.default !== undefined) defaults[field.variable] = field.default;
  }

  // RELION records booleans as Yes/No and numbers as text; coerce back so the
  // form's controls (switch, number input) see the type they expect.
  const values: JobFieldValues = { ...defaults };
  let customCount = 0;
  let recordedCount = 0;
  if (recorded) {
    for (const field of schema.fields) {
      if (!(field.variable in recorded)) continue;
      recordedCount += 1;
      const raw = recorded[field.variable];
      const coerced = coerce(field, raw);
      values[field.variable] = coerced;
      if (!sameValue(coerced, field.default)) customCount += 1;
    }
  }

  const banner =
    state === "ready"
      ? `${recordedCount} option${recordedCount === 1 ? "" : "s"} recorded in job.star` +
        (customCount > 0
          ? `, of which ${customCount} differ${customCount === 1 ? "s" : ""} from the default.`
          : ", all at their defaults.")
      : state === "loading"
        ? "Reading job.star…"
        : state === "missing"
          ? "No job.star in this job directory (RELION 3.x wrote run.job instead), so the values shown are the defaults of this form, not what the job used."
          : "Could not read job.star, so the values shown are the defaults of this form, not what the job used.";

  return (
    <div className="flex flex-col gap-4">
      <div
        className={cn(
          "rounded-field border px-3 py-2 t-label leading-relaxed",
          state === "ready"
            ? "border-rule bg-base-200 text-ink-muted"
            : "border-[color-mix(in_srgb,var(--st-aborted-text)_35%,var(--rule))] bg-[var(--st-aborted-fill)] text-[var(--st-aborted-text)]",
        )}
      >
        {banner}
      </div>

      {consumed.length > 0 && (
        <div>
          <div className="mb-1.5 t-section text-ink-faint">Inputs</div>
          <div className="flex flex-col gap-1">
            {consumed.map((node) => (
              <div
                key={node.name}
                className="truncate rounded-selector border border-rule px-2 py-1.5 font-mono t-meta"
                title={node.name}
              >
                {node.name}
              </div>
            ))}
          </div>
        </div>
      )}

      <JobForm
        schema={schema}
        values={values}
        onChange={() => undefined}
        missing={[]}
        extraCli={[]}
        onExtraCliChange={() => undefined}
      />
    </div>
  );
}

/** Turn a recorded job.star string back into the type the control expects. */
function coerce(
  field: JobFormSchema["fields"][number],
  raw: string,
): string | number | boolean {
  if (field.kind === "boolean") return raw.trim().toLowerCase() === "yes";
  if (field.kind === "slider") {
    const number = Number(raw);
    return Number.isFinite(number) ? number : raw;
  }
  return raw;
}

/** Compare a recorded value with a schema default, across Yes/No and numbers. */
function sameValue(
  actual: string | number | boolean,
  fallback: string | number | boolean | undefined,
): boolean {
  if (fallback === undefined) return false;
  if (typeof actual === "boolean" || typeof fallback === "boolean") {
    return asBool(actual) === asBool(fallback);
  }
  if (typeof actual === "number" || typeof fallback === "number") {
    const a = Number(actual);
    const b = Number(fallback);
    if (Number.isFinite(a) && Number.isFinite(b)) return a === b;
  }
  return String(actual) === String(fallback);
}

function asBool(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  const text = String(value).trim().toLowerCase();
  return text === "yes" || text === "true" || text === "1";
}

function MetadataPanel({
  process,
  liveJob,
  onStop,
  onForget,
  onActions,
}: {
  process: PipelineProcess;
  liveJob?: Job;
  onStop?: (jobId: string) => void;
  onForget?: (jobId: string) => void;
  onActions?: () => void;
}) {
  const rows: [string, string][] = [
    ["Process name", process.name],
    ["Job number", String(jobNumber(process.name))],
    ["Type label", process.type_label],
    ["Directory", process.directory],
    ["Alias", process.alias === "None" ? "—" : process.alias],
    ["Pipeline status", process.status],
  ];
  if (liveJob) {
    rows.push(
      ["Session job id", liveJob.id],
      ["Process status", liveJob.status],
      ["PID", liveJob.pid === null ? "—" : String(liveJob.pid)],
      [
        "Exit code",
        liveJob.returncode === null ? "—" : String(liveJob.returncode),
      ],
      ["Output directory", liveJob.output_directory],
    );
    if (liveJob.error) rows.push(["Error", liveJob.error]);
  }

  return (
    <div className="flex flex-col gap-3">
      <dl className="overflow-hidden rounded-box border border-rule">
        {rows.map(([label, value], index) => (
          <div
            key={label}
            className={cn(
              "flex items-start gap-3 px-3 py-1.5",
              index % 2 === 1 && "bg-base-200/70",
            )}
          >
            <dt className="w-36 shrink-0 t-label text-ink-muted">{label}</dt>
            <dd className="min-w-0 flex-1">
              <Copyable value={value} mono />
            </dd>
          </div>
        ))}
      </dl>

      {(onActions || onStop || onForget) && (
        <div className="flex flex-wrap items-center gap-1.5">
          {onActions && (
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              onClick={onActions}
            >
              Job actions
            </Button>
          )}
          {onStop && liveJob?.status === "running" && (
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              onClick={() => onStop(liveJob.id)}
            >
              <Square className="size-3" />
              Stop
            </Button>
          )}
          {onForget && liveJob && liveJob.status !== "running" && (
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              onClick={() => onForget(liveJob.id)}
              title="Forget this session process (files are kept)"
            >
              <Trash2 className="size-3" />
              Forget process
            </Button>
          )}
        </div>
      )}

      {liveJob && liveJob.command.length > 0 && (
        <div>
          <div className="mb-1.5 t-section text-ink-faint">Command</div>
          <pre className="overflow-x-auto rounded-field border border-rule bg-base-200 p-2 font-mono t-meta whitespace-pre-wrap">
            {liveJob.command.join(" ")}
          </pre>
        </div>
      )}
    </div>
  );
}

function Preview({
  node,
  axis = "z",
}: {
  node: PipelineNode;
  axis?: "z" | "y" | "x" | null;
}) {
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  // A slice at index 0 is the edge of the volume, which for a tomogram is
  // empty. The middle is where the data is, so ask for it once the shape is
  // known; until then render nothing rather than a misleading black square.
  const [centre, setCentre] = useState<number | null>(null);

  useEffect(() => {
    if (axis === null) return;
    let cancelled = false;
    setCentre(null);
    api
      .get<{ nx: number; ny: number; nz: number; shape: number[] }>(
        `/api/volume?path=${encodeURIComponent(node.name)}`,
      )
      .then((info) => {
        if (cancelled) return;
        const extent =
          axis === "z" ? info.nz : axis === "y" ? info.ny : info.nx;
        setCentre(Math.floor(extent / 2));
      })
      .catch(() => {
        // No header: fall back to the server's default rather than nothing.
        if (!cancelled) setCentre(0);
      });
    return () => {
      cancelled = true;
    };
  }, [axis, node.name]);

  const url =
    axis === null
      ? `/api/render/projection?path=${encodeURIComponent(node.name)}&axis=z&width=900&height=900`
      : centre === null
        ? null
        : `/api/render/slice?path=${encodeURIComponent(node.name)}&axis=${axis}&index=${centre}&width=900&height=900`;

  useEffect(() => {
    setError(null);
    setLoaded(false);
  }, [url]);

  return (
    <div className="relative w-full overflow-hidden rounded-box border border-rule bg-base-200">
      <div className="relative h-[min(48vh,28.75rem)] w-full">
        {!error && url && (
          <img
            key={url}
            src={url}
            alt={`${axis === null ? "projection" : `${axis} slice`} of ${basename(node.name)}`}
            className="image-pixelated absolute inset-0 size-full object-contain"
            onLoad={() => setLoaded(true)}
            onError={() => setError("could not render this volume")}
          />
        )}
        {!loaded && !error && (
          <span className="absolute inset-0 grid place-items-center font-mono t-meta text-ink-faint">
            rendering…
          </span>
        )}
        {error && (
          <span className="absolute inset-0 grid place-items-center px-3 text-center font-mono t-meta text-ink-faint">
            {error}
          </span>
        )}
      </div>
      <span className="absolute bottom-1.5 left-2 rounded-[0.125rem] bg-base-100/85 px-1 font-mono t-meta text-ink-muted">
        {basename(node.name)} ·{" "}
        {axis === null ? "max proj" : `${axis}=${centre ?? "…"}`}
      </span>
    </div>
  );
}

function DialogLog({
  process,
  liveJob,
  logChunk,
  projectDirectory,
  compact,
}: {
  process: PipelineProcess;
  liveJob?: Job;
  logChunk: LogChunk | null;
  projectDirectory: string | null;
  compact?: boolean;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<"live" | "disk" | null>(null);

  // A job started in this session streams; one that finished before the page
  // loaded has its log on disk only, in run.out. Both paths land in `text`, so
  // the tab reads the same either way - and before this, every finished job's
  // Log tab said there was nothing to show.
  useEffect(() => {
    setText("");
    setError(null);
    setSource(null);
    let cancelled = false;

    if (liveJob) {
      api
        .get<JobDetail>(`/api/jobs/${liveJob.id}`)
        .then((detail) => {
          if (cancelled) return;
          setText(detail.log);
          setSource("live");
        })
        .catch((caught: unknown) => {
          if (!cancelled) {
            setError(
              caught instanceof Error
                ? caught.message
                : "could not load the log",
            );
          }
        });
      return () => {
        cancelled = true;
      };
    }

    if (!projectDirectory) return;
    api
      .get<{ log: string | null }>(
        `/api/project/job/options?project_directory=${encodeURIComponent(
          projectDirectory,
        )}&process_name=${encodeURIComponent(process.name)}`,
      )
      .then((data) => {
        if (cancelled) return;
        if (data.log === null) {
          setError("this job wrote no run.out");
          return;
        }
        setText(data.log);
        setSource("disk");
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(
            caught instanceof Error ? caught.message : "could not read run.out",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [liveJob, process.name, projectDirectory]);

  useEffect(() => {
    if (!logChunk || !liveJob || logChunk.id !== liveJob.id) return;
    if (!logChunk.chunk && !logChunk.truncated) return;
    setText(
      (previous) => (logChunk.truncated ? "" : previous) + logChunk.chunk,
    );
  }, [liveJob, logChunk]);

  const body = (
    <pre
      className={cn(
        "overflow-auto bg-base-200 font-mono leading-[1.4] whitespace-pre-wrap text-ink-muted",
        compact
          ? "h-full min-h-0 rounded-field border border-rule px-2.5 py-2 t-meta"
          : "h-full px-4 py-3 t-label",
      )}
    >
      {error ??
        (text ||
          (source === "disk" ? "run.out is empty" : "waiting for output…"))}
    </pre>
  );

  if (compact) return body;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b border-rule px-4 py-1.5 t-meta text-ink-faint">
        <span className="font-mono">
          {source === "live" ? "session process" : "run.out"}
        </span>
        <span className="text-rule-strong">·</span>
        <span className="min-w-0 truncate font-mono">{process.name}</span>
        <span className="flex-1" />
        {text && (
          <span className="shrink-0">{text.split("\n").length} lines</span>
        )}
      </div>
      <div className="min-h-0 flex-1">{body}</div>
    </div>
  );
}

function SideSection({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-3.5 last:mb-0">
      <div className="mb-1.5 flex items-center gap-1.5">
        <span className="t-section text-ink-faint">{title}</span>
        <span className="rounded-[0.125rem] bg-base-100 px-1 font-mono t-meta text-ink-faint">
          {count}
        </span>
      </div>
      <div className="flex flex-col gap-1">{children}</div>
    </div>
  );
}

function RelatedRow({
  process,
  onOpen,
}: {
  process: PipelineProcess;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group flex items-center gap-1.5 rounded-selector border border-rule bg-base-100 px-2 py-1.5 text-left hover:border-signal"
      title={`Open ${process.name}`}
    >
      <StatusDot status={process.status} className="size-1" />
      <span className="shrink-0 font-mono t-meta text-ink-faint">
        {jobNumber(process.name)}
      </span>
      <span className="min-w-0 flex-1 truncate t-label">
        {process.alias !== "None" ? process.alias : process.type_label}
      </span>
      <ChevronRight className="size-3 shrink-0 text-ink-faint opacity-0 group-hover:opacity-100" />
    </button>
  );
}

function Kpi({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-[5.75rem] rounded-field border border-rule bg-base-100 px-2.5 py-1.5">
      <div className="t-meta text-ink-faint">{label}</div>
      <div className="mt-0.5">{children}</div>
    </div>
  );
}

function Copyable({ value, mono }: { value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(value).then(
          () => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1200);
          },
          () => undefined,
        );
      }}
      className={cn(
        "group flex w-full items-start gap-1 text-left hover:underline",
        mono && "font-mono t-meta",
      )}
      title={`copy ${value}`}
    >
      <span className="min-w-0 flex-1 break-all">{value}</span>
      {copied ? (
        <span className="shrink-0 t-meta text-st-succeeded">copied</span>
      ) : (
        <Copy className="mt-0.5 size-3 shrink-0 text-ink-faint opacity-0 group-hover:opacity-100" />
      )}
    </button>
  );
}

/** Jobs adjacent to this one, which are the ones worth jumping to. */
function relatedProcesses(
  graph: PipelineGraph,
  name: string,
  direction: "upstream" | "downstream",
): PipelineProcess[] {
  const byName = new Map(
    graph.processes.map((process) => [process.name, process]),
  );
  const outputOf = new Map<string, string>();
  for (const { process, node } of graph.edges.outputs)
    outputOf.set(node, process);

  const out: PipelineProcess[] = [];
  const seen = new Set<string>();
  for (const { node, process } of graph.edges.inputs) {
    let other: string | undefined;
    if (direction === "upstream" && process === name) {
      other = outputOf.get(node);
    } else if (direction === "downstream") {
      const producer = outputOf.get(node);
      if (producer === name) other = process;
    }
    if (!other || other === name || seen.has(other)) continue;
    const entry = byName.get(other);
    if (!entry) continue;
    seen.add(other);
    out.push(entry);
  }
  return out;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}

function basename(name: string): string {
  const slash = name.lastIndexOf("/");
  return slash === -1 ? name : name.slice(slash + 1);
}

function jobNumber(name: string): string {
  const match = /job(\d+)/.exec(name);
  return match ? `J${match[1]}` : name;
}

export { formatDuration };
