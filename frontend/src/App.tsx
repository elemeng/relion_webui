// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import {
  Command,
  FolderTree,
  Image,
  LogOut,
  Moon,
  Network,
  Server,
  Settings2,
  Sparkles,
  Sun,
  X,
} from "lucide-react";

import {
  NotificationStack,
  useNotifications,
} from "@/components/Notifications";
import type { CliExtra } from "@/components/AdvancedCliPanel";
import { StatusDot } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useJobsSocket } from "@/hooks/useJobsSocket";
import { useTheme } from "@/hooks/useTheme";
import { api, ApiError, setUnauthorizedHandler } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  AppConfig,
  ProjectJobType,
  DatasetSummary,
  ExecutionHost,
  Job,
  JobFieldValues,
  JobCategory,
  JobFormSchema,
  PipelineGraph,
  PipelineNode,
  PipelineProcess,
  ProjectEntry,
  ProjectSummary,
  QueueEntry,
  SessionInfo,
  TiltSeriesDetail,
} from "@/lib/types";
import { JobActionsDialog } from "@/views/JobActionsDialog";
import { SchedulePanel } from "@/views/SchedulePanel";
import { JobDialog, type JobDialogTab } from "@/views/JobDialog";
import { JobTypePicker } from "@/components/JobTypePicker";
import type { NodeAction } from "@/components/NodeMenu";
import { JobCardsView } from "@/views/JobCardsView";
import { NewJobDialog } from "@/views/NewJobDialog";
import { type DraftJob, draftLabel, newDraft } from "@/lib/drafts";
import { LoginPage } from "@/views/LoginPage";
import { PipelineCanvas } from "@/views/PipelineCanvas";
import { AssistantPanel } from "@/views/AssistantPanel";
import { NodesView } from "@/views/NodesView";
import { ProjectBrowser } from "@/views/ProjectBrowser";
import { TiltBrowser } from "@/views/TiltBrowser";

type Mode = "projects" | "pipeline" | "tilt" | "nodes";

/**
 * Application shell.
 *
 * A dark frame around a working surface: the pipeline canvas is the centre and
 * the default, the inspector opens over it for one job, and the tilt-series
 * browser replaces the canvas rather than sharing its width. The job builder
 * lives in the inspector so the graph stays visible while options are edited.
 */
export function App() {
  const { theme, toggle } = useTheme();
  const { jobs, logChunk, watchLog, unwatchLog } = useJobsSocket();
  const { items: notifications, notify, dismiss } = useNotifications();

  // Kept for the boot sequence (auth flag, initial project); nothing renders it.
  const [, setConfig] = useState<AppConfig | null>(null);
  const [assistantAvailable, setAssistantAvailable] = useState(false);
  // Execution hosts; `local` is always present and is the default.
  const [hosts, setHosts] = useState<ExecutionHost[]>([]);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [booted, setBooted] = useState(false);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [projects, setProjects] = useState<ProjectEntry[]>([]);
  const [activeProject, setActiveProject] = useState<ProjectSummary | null>(
    null,
  );
  const [graph, setGraph] = useState<PipelineGraph | null>(null);

  const [mode, setMode] = useState<Mode>("projects");
  const [selected, setSelected] = useState<PipelineProcess | null>(null);
  // The assistant is a panel over the canvas, not a mode: a conversation about
  // the pipeline needs the pipeline visible while it is read.
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);

  // Attaching an upstream node preselects a job type and fills its input.
  /** Jobs being built. They live on the canvas until queued or discarded. */
  const [drafts, setDrafts] = useState<DraftJob[]>([]);
  const [editingDraft, setEditingDraft] = useState<string | null>(null);
  const [categories, setCategories] = useState<JobCategory[]>([]);
  /** How the project is laid out: the graph, or a tight card grid. */
  const [layout, setLayout] = useState<"graph" | "cards">("graph");
  // Bumped so the inspector switches to its Builder tab when something else
  // decides the user is about to build a job.

  const [schemas, setSchemas] = useState<Record<string, JobFormSchema>>({});
  // process type label -> the agent's job-type declarations.
  const [jobTypeTable, setJobTypeTable] = useState<ProjectJobType[]>([]);
  // Base job types are not all in /api/job-schema (a variant replaces some of
  // them), but a finished job's job.star records the base job's option table,
  // so the parameters tab needs it. Fetched on demand, kept separate from
  // `schemas` so the picker keeps offering only selectable types.
  const [baseSchemas, setBaseSchemas] = useState<Record<string, JobFormSchema>>(
    {},
  );

  // Tilt-series state
  const [dataset, setDataset] = useState<DatasetSummary | null>(null);
  const [detail, setDetail] = useState<TiltSeriesDetail | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [imageIndex, setImageIndex] = useState(0);

  const loadProjects = useCallback(async () => {
    const data = await api.get<{ projects: ProjectEntry[] }>("/api/projects");
    setProjects(data.projects);
    return data.projects;
  }, []);

  const openProject = useCallback(
    async (path: string) => {
      const summary = await api.get<ProjectSummary>(
        `/api/project?project_directory=${encodeURIComponent(path)}`,
      );
      setActiveProject(summary);
      const flow = await api.get<PipelineGraph>(
        `/api/project/graph?project_directory=${encodeURIComponent(path)}`,
      );
      setGraph(flow);
      await loadProjects();
      return summary;
    },
    [loadProjects],
  );

  // ---------------------------------------------------------------- tilt data
  // ---------------------------------------------------------------- tilt data
  const loadStar = useCallback(
    async (path: string) => {
      const data = await api.get<DatasetSummary>(
        `/api/datasets?star_file=${encodeURIComponent(path)}`,
      );
      setDataset(data);
      setSelectedName(null);
      setDetail(null);
      notify(`${data.n_tilt_series} tilt-series`);
    },
    [notify],
  );

  useEffect(() => {
    setUnauthorizedHandler(() => setNeedsLogin(true));
    let cancelled = false;

    (async () => {
      try {
        const loadedConfig = await api.get<AppConfig>("/api/config");
        if (cancelled) return;
        setConfig(loadedConfig);
        setAssistantAvailable(Boolean(loadedConfig.assistant_enabled));

        if (loadedConfig.auth_required) {
          const info = await api.get<SessionInfo>("/api/session");
          if (cancelled) return;
          setSession(info);
        }

        await loadProjects();
        // A failure here is not worth blocking the app: the form simply offers
        // no remote node and every job runs locally, which is the default.
        api
          .get<{ hosts: ExecutionHost[] }>("/api/hosts")
          .then((data) => {
            if (!cancelled) setHosts(data.hosts);
          })
          .catch(() => undefined);
        const mapping = await api.get<{ job_types: ProjectJobType[] }>(
          "/api/project/job-types",
        );
        if (cancelled) return;
        setJobTypeTable(mapping.job_types);

        const loadedSchemas = await api.get<{
          jobs: JobFormSchema[];
          categories?: JobCategory[];
        }>("/api/job-schema");
        if (cancelled) return;
        setSchemas(
          Object.fromEntries(
            loadedSchemas.jobs.map((schema) => [schema.job_type, schema]),
          ),
        );
        setCategories(loadedSchemas.categories ?? []);

        const params = new URLSearchParams(window.location.search);
        const initialProject =
          params.get("project") || loadedConfig.initial_project;
        if (initialProject) {
          await openProject(initialProject);
          // Boot restores state without navigating, so landing on the pipeline
          // is this branch's job - openProject itself no longer switches modes.
          setMode(
            params.get("view") === "tilt"
              ? "tilt"
              : params.get("view") === "nodes"
                ? "nodes"
                : "pipeline",
          );
        } else {
          setMode("projects");
        }
        // The STAR file is independent of the project: a job's output often
        // lives outside it. Load it and switch to its mode; the previous
        // version switched the mode without ever reading the file.
        const initialStar =
          params.get("star_file") || loadedConfig.initial_star_file;
        if (initialStar) {
          await loadStar(initialStar);
          setMode("tilt");
        }
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          setNeedsLogin(true);
          return;
        }
        notify(
          error instanceof Error ? error.message : "could not reach the server",
          true,
        );
      } finally {
        setBooted(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [loadProjects, notify, openProject]);

  // Mirror the location in the URL: a reload returns to the same view, and a
  // link to a project's pipeline can be shared.
  useEffect(() => {
    if (!booted) return;
    const params = new URLSearchParams(window.location.search);
    const path = activeProject?.project_directory;
    if (path && params.get("project") !== path) {
      params.set("project", path);
    } else if (!path) {
      params.delete("project");
    }
    if (mode === "tilt") params.set("view", "tilt");
    else if (mode === "nodes") params.set("view", "nodes");
    else params.delete("view");
    const search = params.toString();
    const next = `${window.location.pathname}${search ? `?${search}` : ""}`;
    if (next !== `${window.location.pathname}${window.location.search}`) {
      window.history.replaceState(null, "", next);
    }
  }, [activeProject?.project_directory, booted, mode]);

  const selectTiltSeries = useCallback(
    async (name: string) => {
      if (!dataset) return;
      const data = await api.get<TiltSeriesDetail>(
        `/api/tilt-series?star_file=${encodeURIComponent(
          dataset.star_file,
        )}&name=${encodeURIComponent(name)}`,
      );
      setSelectedName(name);
      setDetail(data);
      setImageIndex(0);
    },
    [dataset],
  );

  const toggleImage = useCallback((index: number) => {
    setDetail((current) =>
      current
        ? {
            ...current,
            images: current.images.map((image) =>
              image.index === index
                ? { ...image, selected: !image.selected }
                : image,
            ),
          }
        : current,
    );
  }, []);

  const setAllImages = useCallback((selectedValue: boolean) => {
    setDetail((current) =>
      current
        ? {
            ...current,
            images: current.images.map((image) => ({
              ...image,
              selected: selectedValue,
            })),
          }
        : current,
    );
  }, []);

  const invertImages = useCallback(() => {
    setDetail((current) =>
      current
        ? {
            ...current,
            images: current.images.map((image) => ({
              ...image,
              selected: !image.selected,
            })),
          }
        : current,
    );
  }, []);

  const saveSelection = useCallback(
    async (outputDirectory: string) => {
      if (!detail || !dataset || !selectedName) return;
      const selections: Record<string, number[]> = {
        [selectedName]: detail.images
          .filter((image) => image.selected)
          .map((image) => image.index),
      };
      const result = await api.post<{ star_file: string }>(
        "/api/tilt-series/selection",
        {
          star_file: dataset.star_file,
          selections,
          output_directory: outputDirectory,
        },
      );
      notify(`saved ${result.star_file}`);
    },
    [dataset, detail, notify, selectedName],
  );

  // --------------------------------------------------------------------- jobs
  const submitJob = useCallback(
    async (request: {
      jobType: string;
      values: JobFieldValues;
      extraCli: CliExtra[];
      dryRun: boolean;
      host?: string | null;
    }) => {
      const args: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(request.values)) {
        if (value === "" || value === undefined || value === null) continue;
        args[key] = value;
      }
      const extra = request.extraCli.filter((row) => row.flag.trim() !== "");
      if (extra.length > 0) args.cli_extra = extra;

      const job = await api.post<Job>("/api/jobs", {
        job_type: request.jobType,
        output_directory: ".",
        arguments: args,
        dry_run: request.dryRun,
        host: request.host ?? null,
        project_directory: activeProject?.project_directory ?? null,
      });

      if (request.dryRun) {
        notify("command checked — see the Log tab after starting it");
      } else {
        notify(`started ${job.process_name ?? job.id}`);
        // The pipeline changes only after the job registers itself, so the
        // canvas is refreshed rather than patched.
        if (activeProject) {
          await openProject(activeProject.project_directory);
        }
      }
      return job;
    },
    [activeProject, notify, openProject],
  );

  const enqueueJob = useCallback(
    async (request: {
      jobType: string;
      values: JobFieldValues;
      extraCli: CliExtra[];
      host?: string | null;
      gpus?: number[];
      exclusive?: boolean;
      inputs?: Record<string, string>;
    }) => {
      const args: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(request.values)) {
        if (value === "" || value === undefined || value === null) continue;
        args[key] = value;
      }
      const extra = request.extraCli.filter((row) => row.flag.trim() !== "");
      if (extra.length > 0) args.cli_extra = extra;

      // The queue picks the node's free card unless the caller pinned one, so
      // it is not only a "later" mechanism: it is also how a job avoids racing
      // another job for the same GPU.
      return api.post<QueueEntry>("/api/queue", {
        project_directory: activeProject?.project_directory ?? "",
        job_type: request.jobType,
        arguments: args,
        host: request.host ?? null,
        gpus: request.gpus ?? [],
        exclusive: Boolean(request.exclusive),
        // Slots bound to a queued job's output. Sent as `inputs` so the queue
        // records the dependency instead of a path that does not exist yet.
        inputs: request.inputs ?? {},
      });
    },
    [activeProject],
  );

  const cancelJob = useCallback(
    async (jobId: string) => {
      await api.delete(`/api/jobs/${jobId}`);
      notify(`stopping ${jobId}`);
    },
    [notify],
  );

  const deleteJob = useCallback(
    async (jobId: string) => {
      await api.delete(`/api/jobs/${jobId}?forget=true`);
      notify(`removed ${jobId}`);
    },
    [notify],
  );

  // ------------------------------------------------------------------- lookup
  // ------------------------------------------------------------ interactions
  // A card click opens the dialog, which is where everything about a job lives.
  // The canvas selection is kept in step so the keyboard and the canvas agree
  // about which job is current.
  const [dialogTab, setDialogTab] = useState<JobDialogTab>("overview");

  const openProcess = useCallback(
    (process: PipelineProcess, tab: JobDialogTab = "overview") => {
      setSelected(process);
      setDialogTab(tab);
      setDialogOpen(true);
    },
    [],
  );

  const focusProcess = useCallback((process: PipelineProcess | null) => {
    setSelected(process);
    setDialogOpen(false);
  }, []);

  // "Use as input" from a job's outputs: start a draft with that path filled
  // in, and let the picker choose what runs next. Which job type consumes it is
  // the user's decision, not something the producer's label can answer.
  const pendingInput = useRef<string | null>(null);
  const useNodeAsInput = useCallback(
    (node: PipelineNode) => {
      pendingInput.current = node.name;
      window.dispatchEvent(new CustomEvent("relion:pick-job"));
      notify(`pick a job to consume ${node.name.split("/").pop()}`);
    },
    [notify],
  );

  // ------------------------------------------------------------------ drafts
  const editing = useMemo(
    () => drafts.find((draft) => draft.id === editingDraft) ?? null,
    [drafts, editingDraft],
  );

  const draftTitle = useCallback(
    (draft: DraftJob) =>
      draftLabel(draft, schemas[draft.jobType]?.title ?? draft.jobType),
    [schemas],
  );

  /** Create a draft node and open its form; the node exists immediately. */
  const addDraft = useCallback(
    (jobType: string) => {
      const schema = schemas[jobType];
      const values: Record<string, string | number | boolean> = {};
      for (const field of schema?.fields ?? []) {
        if (field.default !== undefined) values[field.variable] = field.default;
      }
      // A file chosen from a job's outputs seeds the input the form takes.
      const pending = pendingInput.current;
      if (pending && schema) {
        const inputField = schema.fields.find(
          (field) => field.kind === "input_node",
        );
        if (inputField) values[inputField.variable] = pending;
      }
      pendingInput.current = null;

      const draft = newDraft(jobType, values);
      setDrafts((current) => [...current, draft]);
      setEditingDraft(draft.id);
    },
    [schemas],
  );

  const discardDraft = useCallback((id: string) => {
    setDrafts((current) => current.filter((draft) => draft.id !== id));
    setEditingDraft((current) => (current === id ? null : current));
  }, []);

  // Shared by the graph and the card grid: one card component, one action
  // handler, one set of drafts, so the two views stay in step.
  const draftChips = useMemo(
    () =>
      drafts.map((draft) => ({
        id: draft.id,
        label: draftTitle(draft),
        active: draft.active,
        onOpen: () => setEditingDraft(draft.id),
        onDiscard: () => discardDraft(draft.id),
      })),
    [drafts, draftTitle, discardDraft],
  );

  const addControl = useMemo(
    () => (
      <JobTypePicker
        categories={categories}
        schemas={schemas}
        onPick={addDraft}
      />
    ),
    [addDraft, categories, schemas],
  );

  const handleNodeAction = useCallback(
    (process: PipelineProcess, action: NodeAction) => {
      // Menu entries act directly instead of routing through the dialog, which
      // is the point of having a menu: "Log" opens on the log tab, not on a
      // summary you then have to click past.
      if (action === "open") {
        openProcess(process);
        return;
      }
      if (
        action === "details" ||
        action === "outputs" ||
        action === "log" ||
        action === "parameters"
      ) {
        // `details` is the menu's name for the overview tab.
        openProcess(process, action === "details" ? "overview" : action);
        return;
      }
      if (action === "use-as-input") {
        const first = graph?.nodes.find(
          (node) => node.produced_by === process.name,
        );
        if (first) useNodeAsInput(first);
        else notify("this job has no outputs to use yet", true);
        return;
      }
      if (action === "stop") {
        const live = jobs.find((job) => job.process_name === process.name);
        if (live) {
          cancelJob(live.id).catch((error) => notify(error.message, true));
        } else {
          notify("that job is not running in this session", true);
        }
        return;
      }
      if (action === "actions") {
        setSelected(process);
        setActionsOpen(true);
        return;
      }
    },
    [cancelJob, graph, jobs, notify, openProcess, useNodeAsInput],
  );

  // The picker's `+` is inside the canvas; these events are how the keyboard
  // shortcut and "use as input" reach it.
  useEffect(() => {
    const open = () => {
      document.querySelector<HTMLElement>("[data-add-job]")?.click();
    };
    window.addEventListener("relion:pick-job", open);
    return () => window.removeEventListener("relion:pick-job", open);
  }, []);

  // A process label can name several web job types: `relion.importtomo` covers
  // the base import and both its variants (29 / 6 / 14 options). RELION records
  // only the label in job.star and resolves it with get_proc_type(), which
  // strips the suffix - so the variants are genuinely indistinguishable on disk,
  // and so is this. Pick the declaration with the most options: it is a superset,
  // so no field the job could have used is hidden.
  const labelToType = useMemo(() => {
    const best = new Map<string, { id: string; fields: number }>();
    for (const entry of jobTypeTable) {
      const fields = schemas[entry.id]?.fields.length ?? 0;
      const current = best.get(entry.process_type_label);
      if (!current || fields > current.fields) {
        best.set(entry.process_type_label, { id: entry.id, fields });
      }
    }
    return Object.fromEntries(
      [...best].map(([label, pick]) => [label, pick.id]),
    );
  }, [jobTypeTable, schemas]);

  /** Base job type for the current selection, from the agent's own mapping. */
  const selectedBaseType = useMemo(() => {
    if (!selected) return null;
    const entry = jobTypeTable.find(
      (candidate) => candidate.process_type_label === selected.type_label,
    );
    return entry?.base_job_type ?? null;
  }, [jobTypeTable, selected]);

  // Fetch the base schema once, when a job dialog needs it.
  useEffect(() => {
    if (!dialogOpen || !selectedBaseType) return;
    if (schemas[selectedBaseType] || baseSchemas[selectedBaseType]) return;
    let cancelled = false;
    api
      .get<JobFormSchema>(
        `/api/job-schema?job_type=${encodeURIComponent(selectedBaseType)}`,
      )
      .then((schema) => {
        if (!cancelled) {
          setBaseSchemas((current) => ({
            ...current,
            [selectedBaseType]: schema,
          }));
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [baseSchemas, dialogOpen, schemas, selectedBaseType]);

  /** The live job behind a pipeline process, when this session started it. */
  const liveJobFor = useCallback(
    (process: PipelineProcess | null): Job | undefined => {
      if (!process) return undefined;
      return jobs.find(
        (job) =>
          job.process_name === process.name ||
          job.output_directory === process.directory,
      );
    },
    [jobs],
  );

  const selectedLiveJob = liveJobFor(selected);

  // The log panel follows whichever job is open, so the socket subscription has
  // to move with it. Without this the log only streams for a job started in
  // this tab and then discarded.
  useEffect(() => {
    if (!selectedLiveJob) {
      unwatchLog();
      return;
    }
    watchLog(selectedLiveJob.id, 0);
    return () => unwatchLog();
  }, [selectedLiveJob, unwatchLog, watchLog]);

  // ------------------------------------------------------------- keyboard
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const processesRef = useRef<PipelineProcess[]>([]);
  processesRef.current = graph?.processes ?? [];

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      // Never steal keys from a field.
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (event.metaKey || event.ctrlKey) {
        if (event.key.toLowerCase() === "k") {
          event.preventDefault();
          setMode("projects");
        }
        return;
      }

      const processes = processesRef.current;
      const index = processes.findIndex(
        (p) => p.name === selectedRef.current?.name,
      );

      if (event.key === "Escape") {
        if (shortcutsOpen) setShortcutsOpen(false);
        else if (dialogOpen) setDialogOpen(false);
        else setSelected(null);
        return;
      }
      if (event.key === "?" && !event.shiftKey) {
        setShortcutsOpen(true);
        return;
      }
      if (event.key === "f") {
        // The canvas owns fit; a synthetic click keeps one implementation.
        window.dispatchEvent(new CustomEvent("relion:fit"));
        return;
      }
      if (event.key === "t" && selectedRef.current) {
        window.dispatchEvent(new CustomEvent("relion:centre"));
        return;
      }
      if (event.key === " " && selectedRef.current) {
        event.preventDefault();
        setDialogOpen(true);
        return;
      }
      if (event.key === "p") {
        event.preventDefault();
        setMode("projects");
        return;
      }
      if (event.key === "n") {
        event.preventDefault();
        window.dispatchEvent(new CustomEvent("relion:pick-job"));
        return;
      }
      if (event.key === "ArrowDown" || event.key === "j") {
        event.preventDefault();
        const next = processes[Math.min(processes.length - 1, index + 1)];
        if (next) setSelected(next);
        return;
      }
      if (event.key === "ArrowUp" || event.key === "k") {
        event.preventDefault();
        const prev = processes[Math.max(0, index - 1)];
        if (prev) setSelected(prev);
        return;
      }
      if (event.key === "Enter" && selectedRef.current) {
        setDialogOpen(true);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [dialogOpen, shortcutsOpen]);

  // The canvas listens for these because `fit` needs its measured viewport.
  useEffect(() => {
    const onFit = () => {
      document.querySelector<HTMLElement>("[data-canvas-fit]")?.click();
    };
    const onCentre = () => {
      document.querySelector<HTMLElement>("[data-canvas-centre]")?.click();
    };
    window.addEventListener("relion:fit", onFit);
    window.addEventListener("relion:centre", onCentre);
    return () => {
      window.removeEventListener("relion:fit", onFit);
      window.removeEventListener("relion:centre", onCentre);
    };
  }, []);

  if (needsLogin) {
    return <LoginPage onSignIn={() => window.location.reload()} />;
  }

  if (!booted) {
    return (
      <div className="bg-base-200 flex min-h-svh items-center justify-center">
        <Spinner size="lg" className="text-base-content/60" />
      </div>
    );
  }

  const counts = countStatuses(graph);

  return (
    <div className="grid h-svh grid-rows-[2.25rem_minmax(0,1fr)_1.5rem] overflow-hidden">
      <TopBar
        project={activeProject}
        onProjects={() => setMode("projects")}
        onSchedule={() => setScheduleOpen(true)}
        onNewJob={() =>
          window.dispatchEvent(new CustomEvent("relion:pick-job"))
        }
        mode={mode}
        layout={layout}
        onLayout={setLayout}
      />

      <div className="grid min-h-0 grid-cols-[2.5rem_minmax(0,1fr)] overflow-hidden">
        <Rail
          mode={mode}
          onMode={setMode}
          onProjects={() => setMode("projects")}
          onShortcuts={() => setShortcutsOpen(true)}
          onToggleTheme={toggle}
          assistantAvailable={assistantAvailable}
          assistantOpen={assistantOpen}
          onAssistant={() => setAssistantOpen((value) => !value)}
          theme={theme}
          username={session?.username ?? null}
          onSignOut={() => {
            api
              .post("/api/logout")
              .catch(() => undefined)
              .finally(() => window.location.replace("/"));
          }}
        />

        <main className="relative min-h-0 overflow-hidden">
          {mode === "projects" ? (
            <ProjectBrowser
              projects={projects}
              activePath={activeProject?.project_directory ?? null}
              onOpen={async (path) => {
                await openProject(path);
                // Entering a project is navigation into its pipeline.
                setMode("pipeline");
              }}
              onAdd={async (path) => {
                await api.post("/api/projects", { path });
                await loadProjects();
                notify(`added ${path}`);
              }}
              onCreate={async (path) => {
                const created = await api.post<{ path: string }>(
                  "/api/projects/create",
                  { path },
                );
                await api.post("/api/projects", { path: created.path });
                await loadProjects();
                await openProject(created.path);
                notify(`created ${created.path}`);
              }}
              onRemove={async (path) => {
                await api.delete(
                  `/api/projects?path=${encodeURIComponent(path)}`,
                );
                await loadProjects();
                if (activeProject?.project_directory === path) {
                  setActiveProject(null);
                  setGraph(null);
                  setSelected(null);
                }
                notify(`removed ${path} from the list; files are kept`);
              }}
              onRefresh={async () => {
                await loadProjects();
              }}
              onError={(message) => notify(message, true)}
            />
          ) : mode === "pipeline" ? (
            graph && activeProject ? (
              <div className="relative flex h-full min-h-0">
                {layout === "cards" ? (
                  <JobCardsView
                    graph={graph}
                    selected={selected?.name ?? null}
                    onOpen={openProcess}
                    drafts={draftChips}
                    addControl={addControl}
                    onNodeAction={handleNodeAction}
                  />
                ) : (
                  <div className="relative min-w-0 flex-1">
                    <PipelineCanvas
                      graph={graph}
                      selected={selected?.name ?? null}
                      onSelect={focusProcess}
                      onOpen={openProcess}
                      drafts={draftChips}
                      addControl={addControl}
                      onNodeAction={handleNodeAction}
                    />
                  </div>
                )}

                {editing && (
                  <NewJobDialog
                    draft={editing}
                    hosts={hosts}
                    schema={schemas[editing.jobType] ?? null}
                    projectDirectory={activeProject.project_directory}
                    onChange={(next) =>
                      setDrafts((current) =>
                        current.map((draft) =>
                          draft.id === editing.id
                            ? { ...draft, ...next }
                            : draft,
                        ),
                      )
                    }
                    onQueue={async (dryRun) => {
                      try {
                        const job = await submitJob({
                          jobType: editing.jobType,
                          values: editing.values,
                          extraCli: editing.extraCli,
                          dryRun,
                          host: editing.host ?? null,
                        });
                        // Only a real submission retires the draft; a dry run
                        // just shows the command.
                        if (!dryRun) discardDraft(editing.id);
                        return job;
                      } catch (error) {
                        notify(
                          error instanceof Error
                            ? error.message
                            : "submit failed",
                          true,
                        );
                        throw error;
                      }
                    }}
                    onEnqueue={async ({ gpus, exclusive, inputs }) => {
                      // A queued job retires the draft: the entry holds
                      // everything needed, so there is nothing left on the
                      // canvas to come back to.
                      try {
                        const entry = await enqueueJob({
                          jobType: editing.jobType,
                          values: editing.values,
                          extraCli: editing.extraCli,
                          host: editing.host ?? null,
                          gpus,
                          exclusive,
                          inputs,
                        });
                        discardDraft(editing.id);
                        notify(`queued ${entry.id} — watch it on GPU nodes`);
                        return entry.id;
                      } catch (error) {
                        notify(
                          error instanceof Error
                            ? error.message
                            : "could not queue",
                          true,
                        );
                        throw error;
                      }
                    }}
                    onPostpone={() => {
                      setDrafts((current) =>
                        current.map((draft) =>
                          draft.id === editing.id
                            ? { ...draft, active: false }
                            : draft,
                        ),
                      );
                      setEditingDraft(null);
                    }}
                    onDiscard={() => discardDraft(editing.id)}
                    onClose={() => setEditingDraft(null)}
                  />
                )}
              </div>
            ) : (
              <EmptyProject onPick={() => setMode("projects")} />
            )
          ) : mode === "nodes" ? (
            <NodesView />
          ) : (
            <TiltBrowser
              dataset={dataset}
              detail={detail}
              selectedName={selectedName}
              imageIndex={imageIndex}
              defaultOutputDirectory={
                activeProject
                  ? `${activeProject.project_directory}/exclude`
                  : ""
              }
              onLoadStar={loadStar}
              onSelectSeries={selectTiltSeries}
              onImageIndexChange={setImageIndex}
              onToggleImage={toggleImage}
              onSelectAll={() => setAllImages(true)}
              onSelectNone={() => setAllImages(false)}
              onInvert={invertImages}
              onSaveSelection={async (output) => {
                try {
                  await saveSelection(output);
                } catch (error) {
                  notify(
                    error instanceof Error ? error.message : "save failed",
                    true,
                  );
                }
              }}
            />
          )}

          {actionsOpen && selected && activeProject && (
            <JobActionsDialog
              projectDirectory={activeProject.project_directory}
              process={selected}
              onOpenChange={(open) => {
                if (!open) setActionsOpen(false);
              }}
              onDone={(message) => {
                notify(message);
                setActionsOpen(false);
                openProject(activeProject.project_directory).catch(
                  () => undefined,
                );
              }}
              onError={(message) => notify(message, true)}
            />
          )}

          {scheduleOpen && activeProject && graph && (
            <div
              className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-[color-mix(in_srgb,var(--frame)_38%,transparent)] p-6"
              onPointerDown={(event) => {
                if (event.target === event.currentTarget)
                  setScheduleOpen(false);
              }}
              role="dialog"
              aria-modal
              aria-label="Scheduler and data transfer"
            >
              <div className="glass-card glass-edge flex max-h-full w-[37.5rem] flex-col overflow-hidden rounded-box border border-rule-strong shadow-[0_24px_64px_-16px_rgb(0_0_0/0.45)]">
                <div className="flex shrink-0 items-center gap-2 border-b border-rule px-4 py-2.5">
                  <h2 className="flex-1 t-head font-semibold">
                    Scheduler and data transfer
                  </h2>
                  <button
                    type="button"
                    onClick={() => setScheduleOpen(false)}
                    className="grid size-7 place-items-center rounded-field text-ink-faint hover:bg-base-200 hover:text-base-content"
                    title="Close"
                    aria-label="Close"
                  >
                    <X className="size-4" />
                  </button>
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto p-4">
                  <SchedulePanel
                    projectDirectory={activeProject.project_directory}
                    processes={graph.processes}
                    onDone={(message) => {
                      notify(message);
                      openProject(activeProject.project_directory).catch(
                        () => undefined,
                      );
                    }}
                    onError={(message) => notify(message, true)}
                  />
                </div>
              </div>
            </div>
          )}

          {assistantAvailable && (
            <AssistantPanel
              open={assistantOpen}
              onClose={() => setAssistantOpen(false)}
              projectDirectory={activeProject?.project_directory ?? null}
              onProjectChanged={() => {
                // A turn may have submitted, renamed or deleted a job; the
                // canvas is a snapshot, so it is re-read rather than patched.
                if (activeProject) {
                  openProject(activeProject.project_directory).catch(
                    () => undefined,
                  );
                }
              }}
            />
          )}

          {dialogOpen && selected && graph && (
            <JobDialog
              process={selected}
              graph={graph}
              liveJob={selectedLiveJob}
              initialTab={dialogTab}
              schema={
                schemas[
                  labelToType[selected.type_label] ??
                    processJobType(selected, schemas) ??
                    ""
                ] ?? null
              }
              parameterSchema={
                selectedBaseType
                  ? (schemas[selectedBaseType] ??
                    baseSchemas[selectedBaseType] ??
                    null)
                  : null
              }
              logChunk={logChunk}
              onClose={() => setDialogOpen(false)}
              onOpenProcess={openProcess}
              projectDirectory={activeProject?.project_directory ?? null}
              onActions={() => setActionsOpen(true)}
              onUseAsInput={useNodeAsInput}
              onStop={(jobId) =>
                cancelJob(jobId).catch((error) => notify(error.message, true))
              }
              onForget={(jobId) =>
                deleteJob(jobId).catch((error) => notify(error.message, true))
              }
            />
          )}
        </main>
      </div>

      <StatusBar
        jobs={graph?.processes.length ?? 0}
        types={counts.types}
        running={counts.running}
        failed={counts.failed}
        projectPath={activeProject?.project_directory ?? null}
        mode={mode}
        projects={projects.length}
      />

      <NotificationStack items={notifications} onDismiss={dismiss} />

      {shortcutsOpen && (
        <ShortcutsSheet onClose={() => setShortcutsOpen(false)} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ pieces */

function Rail({
  mode,
  onMode,
  onProjects,
  onShortcuts,
  onToggleTheme,
  assistantAvailable,
  assistantOpen,
  onAssistant,
  theme,
  username,
  onSignOut,
}: {
  mode: Mode;
  onMode: (mode: Mode) => void;
  onProjects: () => void;
  onShortcuts: () => void;
  onToggleTheme: () => void;
  assistantAvailable: boolean;
  assistantOpen: boolean;
  onAssistant: () => void;
  theme: string;
  username: string | null;
  onSignOut: () => void;
}) {
  const item = (active: boolean) =>
    cn(
      "relative grid size-7 place-items-center rounded-field transition-colors",
      active
        ? "bg-frame-raise text-frame-ink"
        : "text-frame-muted hover:bg-frame-raise hover:text-frame-ink",
    );

  return (
    <nav
      className="glass-chrome flex flex-col items-center gap-0.5 border-r border-frame-rule py-1"
      aria-label="Sections"
    >

      <RailButton
        active={mode === "projects"}
        onClick={onProjects}
        label="All projects"
        hint="⌘K"
        className={item(mode === "projects")}
      >
        <FolderTree className="size-4" />
      </RailButton>
      <RailButton
        active={mode === "pipeline"}
        onClick={() => onMode("pipeline")}
        label="Pipeline"
        className={item(mode === "pipeline")}
      >
        <Network className="size-4" />
      </RailButton>
      <RailButton
        active={mode === "tilt"}
        onClick={() => onMode("tilt")}
        label="Tilt-series"
        className={item(mode === "tilt")}
      >
        <Image className="size-4" />
      </RailButton>
      <RailButton
        active={mode === "nodes"}
        onClick={() => onMode("nodes")}
        label="GPU nodes"
        className={item(mode === "nodes")}
      >
        <Server className="size-4" />
      </RailButton>
      <span className="flex-1" />
      {assistantAvailable && (
        <RailButton
          active={assistantOpen}
          onClick={onAssistant}
          label="Assistant"
          hint="AI"
          className={item(assistantOpen)}
        >
          <Sparkles className="size-4" />
        </RailButton>
      )}
      <RailButton
        onClick={onShortcuts}
        label="Keyboard shortcuts"
        hint="?"
        className={item(false)}
      >
        <Command className="size-4" />
      </RailButton>
      <RailButton
        onClick={onToggleTheme}
        label={theme === "dark" ? "Light theme" : "Dark theme"}
        className={item(false)}
      >
        {theme === "dark" ? (
          <Sun className="size-4" />
        ) : (
          <Moon className="size-4" />
        )}
      </RailButton>
      {username && (
        <RailButton
          onClick={onSignOut}
          label={`Sign out ${username}`}
          className={item(false)}
        >
          <LogOut className="size-4" />
        </RailButton>
      )}
    </nav>
  );
}

/** One rail item: a real popover tip, because the native title is slow. */
function RailButton({
  active,
  onClick,
  label,
  hint,
  className,
  children,
}: {
  active?: boolean;
  onClick: () => void;
  label: string;
  hint?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      aria-label={label}
      className={cn(className, "group/rail")}
    >
      {children}
      <span
        className={cn(
          "pointer-events-none absolute left-[calc(100%+0.5rem)] z-50 hidden whitespace-nowrap rounded-field",
          "border border-frame-rule bg-frame-raise px-1.5 py-1 t-meta text-frame-ink shadow-lg",
          "group-hover/rail:block",
        )}
        role="tooltip"
      >
        {label}
        {hint && <span className="ml-1.5 text-frame-muted">{hint}</span>}
      </span>
    </button>
  );
}

function TopBar({
  project,
  onProjects,
  onSchedule,
  onNewJob,
  mode,
  layout,
  onLayout,
}: {
  project: ProjectSummary | null;
  onProjects: () => void;
  onSchedule: () => void;
  onNewJob: () => void;
  mode: Mode;
  layout: "graph" | "cards";
  onLayout: (layout: "graph" | "cards") => void;
}) {
  const onPipeline = mode === "pipeline";

  return (
    <header className="glass-chrome flex items-center gap-1.5 border-b border-frame-rule px-2.5">
      <button
        type="button"
        onClick={onProjects}
        className="rounded-selector t-ui font-semibold tracking-tight text-frame-ink hover:underline"
        title="All projects (P)"
      >
        RELION cryo-ET
      </button>
      <Separator orientation="vertical" className="h-3 bg-frame-rule" />

      {/* Breadcrumb: projects / <project>. The first crumb is the way back up. */}
      <nav aria-label="Breadcrumb" className="flex items-center gap-1">
        <button
          type="button"
          onClick={onProjects}
          className="rounded-selector px-1.5 py-0.5 t-label text-frame-muted hover:bg-frame-raise hover:text-frame-ink"
          title="All projects"
        >
          projects
        </button>
        {project && (
          <>
            <span className="t-meta text-frame-muted" aria-hidden>
              /
            </span>
            <span className="max-w-[20rem] truncate rounded-selector bg-frame-raise px-1.5 py-0.5 t-label text-frame-ink">
              {basename(project.project_directory)}
            </span>
          </>
        )}
      </nav>

      <span className="flex-1" />

      {mode !== "projects" && (
        <>
          <TopBarButton onClick={onNewJob} title="Build a new job (N)" primary>
            New job
          </TopBarButton>
          <TopBarButton
            onClick={onSchedule}
            title="Scheduler, export and import"
          >
            Scheduler
          </TopBarButton>
        </>
      )}
      {onPipeline && (
        <div
          className="flex overflow-hidden rounded-field border border-frame-rule"
          role="group"
          aria-label="Pipeline layout"
        >
          {(["graph", "cards"] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => onLayout(value)}
              aria-pressed={layout === value}
              title={value === "graph" ? "Graph layout" : "Card grid layout"}
              className={cn(
                "px-2 py-1 t-label capitalize",
                layout === value
                  ? "bg-frame-raise text-frame-ink"
                  : "text-frame-muted hover:text-frame-ink",
              )}
            >
              {value}
            </button>
          ))}
        </div>
      )}
    </header>
  );
}

function TopBarButton({
  onClick,
  title,
  primary,
  children,
}: {
  onClick: () => void;
  title: string;
  primary?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(
        "rounded-field border px-2 py-0.5 t-label",
        primary
          ? "border-transparent bg-signal font-medium text-white hover:bg-[oklch(34%_0.09_255)]"
          : "border-frame-rule text-frame-muted hover:border-frame-muted hover:text-frame-ink",
      )}
    >
      {children}
    </button>
  );
}

function StatusBar({
  jobs,
  types,
  running,
  failed,
  projectPath,
  mode,
  projects,
}: {
  jobs: number;
  types: number;
  running: number;
  failed: number;
  projectPath: string | null;
  mode: Mode;
  projects: number;
}) {
  return (
    <footer className="glass-chrome flex items-center gap-3 border-t border-rule px-3 t-meta text-ink-faint">
      {mode === "pipeline" ? (
        <>
          <Stat value={jobs} label="jobs" />
          <Stat value={types} label="types" />
          {running > 0 && (
            <span className="flex items-center gap-1">
              <StatusDot status="Running" />
              <b className="font-semibold text-st-running">{running}</b> running
            </span>
          )}
          {failed > 0 && (
            <span className="flex items-center gap-1">
              <StatusDot status="Failed" />
              <b className="font-semibold text-st-failed">{failed}</b> failed
            </span>
          )}
        </>
      ) : mode === "tilt" ? (
        <span>tilt-series</span>
      ) : mode === "nodes" ? (
        <span>cluster</span>
      ) : (
        <Stat
          value={projects}
          label={projects === 1 ? "project" : "projects"}
        />
      )}
      <span className="flex-1" />
      <span
        className="max-w-[60%] truncate font-mono t-meta"
        title={projectPath ?? undefined}
      >
        {projectPath ?? "no project open"}
      </span>
    </footer>
  );
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <span>
      <b className="font-semibold text-base-content">{value}</b> {label}
    </span>
  );
}

function EmptyProject({ onPick }: { onPick: () => void }) {
  return (
    <div className="grid h-full place-items-center p-8">
      <div className="max-w-sm text-center">
        <Network className="mx-auto mb-3 size-6 text-ink-faint" />
        <p className="t-ui font-medium">No project open</p>
        <p className="mt-1 t-body leading-relaxed text-ink-muted">
          Pick a project to see its pipeline.
        </p>
        <Button size="sm" className="mt-3" onClick={onPick}>
          <Settings2 className="size-3.5" />
          All projects
        </Button>
      </div>
    </div>
  );
}

function ShortcutsSheet({ onClose }: { onClose: () => void }) {
  const rows: [string, string][] = [
    ["↑ ↓  /  j k", "move the selection along the pipeline"],
    ["Enter / Space", "open the job dialog"],
    ["Esc", "close the dialog, then clear the selection"],
    ["T", "centre the canvas on the selection"],
    ["F", "fit the whole pipeline in view"],
    ["N", "build a new job"],
    ["P / ⌘K", "switch project"],
    ["?", "this sheet"],
  ];
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-[color-mix(in_srgb,var(--frame)_50%,transparent)] p-6"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal
      aria-label="Keyboard shortcuts"
    >
      <div className="w-full max-w-md overflow-hidden rounded-box border border-rule bg-base-100">
        <div className="flex items-center gap-2 border-b border-rule px-3.5 py-2.5">
          <span className="t-ui font-medium">Keyboard</span>
          <span className="flex-1" />
          <button
            type="button"
            onClick={onClose}
            className="grid size-6 place-items-center rounded-selector text-ink-faint hover:bg-base-200 hover:text-base-content"
            title="Close"
          >
            <X className="size-3.5" />
          </button>
        </div>
        <dl className="p-3.5">
          {rows.map(([keys, description]) => (
            <div key={keys} className="flex items-baseline gap-3 py-1">
              <dt className="w-28 shrink-0">
                <kbd className="rounded-selector border border-rule bg-base-200 px-1.5 py-0.5 font-mono t-meta">
                  {keys}
                </kbd>
              </dt>
              <dd className="t-label text-ink-muted">{description}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- helpers */

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const slash = trimmed.lastIndexOf("/");
  return slash === -1 ? trimmed : trimmed.slice(slash + 1);
}

function countStatuses(graph: PipelineGraph | null) {
  let running = 0;
  let failed = 0;
  const types = new Set<string>();
  for (const process of graph?.processes ?? []) {
    types.add(process.type_label);
    const status = process.status.toLowerCase();
    if (status === "running") running += 1;
    if (status === "failed") failed += 1;
  }
  return { running, failed, types: types.size };
}

/**
 * A pipeline process does not record which web job type built it, and one
 * process type label can be several forms (the split branch variants). Resolve
 * by process type label, falling back to the base type of a matching schema.
 */
function processJobType(
  process: PipelineProcess,
  schemas: Record<string, JobFormSchema>,
): string | null {
  const candidates = Object.values(schemas).filter(
    (schema) =>
      schema.job_type === process.type_label ||
      schema.base_job_type === process.type_label,
  );
  if (candidates.length === 0) {
    // A type label like `relion.reconstructtomograms` still names the job: the
    // last segment is the C++ program, which is what the base type records.
    const tail = process.type_label.split(".").pop();
    const byName = Object.values(schemas).find(
      (schema) => schema.job_type === tail,
    );
    return byName?.job_type ?? null;
  }
  return candidates[0].job_type;
}
