import { useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import {
  AlertCircle,
  FolderOpen,
  FolderPlus,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";

import { StatusCounts, StatusDot } from "@/components/Status";
import { Button } from "@/components/ui/button";
import { PathInput } from "@/components/PathInput";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { ProjectEntry } from "@/lib/types";

interface ProjectBrowserProps {
  projects: ProjectEntry[];
  activePath: string | null;
  onOpen: (path: string) => Promise<void>;
  onAdd: (path: string) => Promise<void>;
  onCreate: (path: string) => Promise<void>;
  onRemove: (path: string) => Promise<void>;
  onRefresh: () => Promise<void>;
  onError: (message: string) => void;
}

/**
 * All projects, as cards.
 *
 * This is the level above a pipeline: pick a project, then see its jobs. A
 * card carries the state you would otherwise have to open it to find - how many
 * jobs, which statuses, and what was last worked on - because with several
 * projects in flight the question is usually "which one is running or broken".
 *
 * The path field is the only way in, so it sits at the top where the eye starts
 * and the cards are the result of using it.
 */
export function ProjectBrowser({
  projects,
  activePath,
  onOpen,
  onAdd,
  onCreate,
  onRemove,
  onRefresh,
  onError,
}: ProjectBrowserProps) {
  const [busyPath, setBusyPath] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [newPath, setNewPath] = useState("");
  const [submitting, setSubmitting] = useState<"open" | "create" | null>(null);

  const run = async (path: string | null, action: () => Promise<void>) => {
    setBusyPath(path);
    try {
      await action();
    } catch (error) {
      onError(error instanceof Error ? error.message : "that did not work");
    } finally {
      setBusyPath(null);
    }
  };

  const submit = (kind: "open" | "create") => {
    const path = newPath.trim();
    if (!path) return;
    setSubmitting(kind);
    run(null, () => (kind === "open" ? onAdd(path) : onCreate(path)))
      .then(() => setNewPath(""))
      .finally(() => setSubmitting(null));
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-5 px-5 py-6">
        <div className="flex items-center gap-2.5">
          <h1 className="t-head font-semibold">Projects</h1>
          <span className="rounded-[3px] bg-secondary px-1.5 py-px font-mono t-meta text-ink-muted">
            {projects.length}
          </span>
          <span className="flex-1" />
          <Button
            variant="outline"
            size="sm"
            disabled={refreshing}
            onClick={() => {
              setRefreshing(true);
              onRefresh()
                .catch(() => undefined)
                .finally(() => setRefreshing(false));
            }}
            title="Re-read every project's pipeline"
          >
            <RefreshCw
              className={cn("size-3.5", refreshing && "animate-spin")}
            />
            Refresh
          </Button>
        </div>

        {/* One field, two verbs. "Open" covers both reading an existing
            project and initialising an empty folder, which is what RELION's own
            GUI does; "Create" additionally makes the directory. */}
        <form
          className="flex flex-col gap-2 rounded-box border border-rule bg-base-100 p-3"
          onSubmit={(event) => {
            event.preventDefault();
            submit("open");
          }}
        >
          <div className="flex items-center gap-2">
            <PathInput
              value={newPath}
              onChange={setNewPath}
              placeholder="Open a project, or start one in an empty folder"
              mode="project"
              pickerTitle="Choose a RELION project"
              className="min-w-0 flex-1"
            />
            <Button
              type="submit"
              size="sm"
              className="h-8 shrink-0"
              disabled={submitting !== null || !newPath.trim()}
            >
              {submitting === "open" ? (
                <Spinner size="sm" />
              ) : (
                <Plus className="size-3.5" />
              )}
              Open
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 shrink-0"
              disabled={submitting !== null || !newPath.trim()}
              onClick={() => submit("create")}
              title="Create this directory and initialise it as a project"
            >
              {submitting === "create" ? (
                <Spinner size="sm" />
              ) : (
                <FolderPlus className="size-3.5" />
              )}
              Create
            </Button>
          </div>
          <p className="t-label leading-relaxed text-ink-muted">
            An existing project is read as it is. An <em>empty</em> folder is
            initialised with a{" "}
            <code className="font-mono t-meta">default_pipeline.star</code> the
            way RELION&apos;s own GUI starts one.
            <Tip text="This list is the agent's write boundary: jobs may only write inside these directories. Removing a project never deletes files.">
              <span className="ml-1 cursor-help border-b border-dotted border-rule-strong text-ink-faint">
                more
              </span>
            </Tip>
          </p>
        </form>

        {projects.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {projects.map((project) => (
              <ProjectCard
                key={project.path}
                project={project}
                active={project.path === activePath}
                busy={busyPath === project.path}
                onOpen={() => run(project.path, () => onOpen(project.path))}
                onRemove={() => run(project.path, () => onRemove(project.path))}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ProjectCard({
  project,
  active,
  busy,
  onOpen,
  onRemove,
}: {
  project: ProjectEntry;
  active: boolean;
  busy: boolean;
  onOpen: () => void;
  onRemove: () => void;
}) {
  const readable = project.readable !== false;

  return (
    // A div, not a button, because the card has two verbs: open the project and
    // remove it from the list. Nesting a button inside a button is invalid HTML
    // and makes the inner click ambiguous, so the open target is an overlay
    // button spanning the card's content and the menu sits above it.
    <div
      className={cn(
        "group relative flex flex-col gap-3 rounded-box border bg-base-100 p-3.5 transition-colors",
        active
          ? "border-signal shadow-[0_0_0_1px_var(--signal)]"
          : "border-rule hover:border-rule-strong",
        !readable && "opacity-65",
      )}
    >
      <div className="flex items-start gap-2.5">
        <span
          className={cn(
            "mt-0.5 grid size-7 shrink-0 place-items-center rounded-[4px]",
            active ? "bg-signal/10 text-signal" : "bg-base-200 text-ink-faint",
          )}
        >
          <FolderOpen className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <button
            type="button"
            onClick={onOpen}
            disabled={!readable || busy}
            className="block max-w-full truncate t-body font-semibold hover:underline disabled:cursor-not-allowed disabled:hover:no-underline"
            title={readable ? `Open ${project.path}` : project.path}
          >
            {project.name}
          </button>
          <div
            className="truncate font-mono t-meta text-ink-faint"
            title={project.path}
          >
            {project.path}
          </div>
        </div>
        {active && (
          <span className="mt-0.5 shrink-0 rounded-[3px] bg-[var(--st-running-fill)] px-1.5 py-px t-meta font-medium text-[var(--st-running-text)]">
            open
          </span>
        )}
        {busy ? (
          <Spinner size="sm" className="mt-1 shrink-0 text-ink-faint" />
        ) : (
          <button
            type="button"
            onClick={onRemove}
            className={cn(
              "z-10 mt-0.5 grid size-5 shrink-0 place-items-center rounded-[3px] text-ink-faint",
              "opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100",
              "hover:bg-st-failed/10 hover:text-st-failed",
            )}
            title="Remove from the list; files are not deleted"
            aria-label={`Remove ${project.name} from the list`}
          >
            <Trash2 className="size-3" />
          </button>
        )}
      </div>

      {readable ? (
        <>
          <div className="flex items-center gap-2.5">
            <StatusCounts counts={project.statuses ?? {}} />
            <span className="flex-1" />
            <span className="shrink-0 t-label text-ink-muted">
              <b className="font-semibold text-base-content">
                {project.job_count ?? 0}
              </b>{" "}
              jobs
              {project.types !== undefined && (
                <>
                  <span className="mx-1.5 text-rule-strong">·</span>
                  {project.types} types
                </>
              )}
            </span>
          </div>

          {project.recent && project.recent.length > 0 && (
            <div className="flex flex-col gap-1 border-t border-rule-soft pt-2.5">
              {project.recent.slice(0, 3).map((job) => (
                <div key={job.name} className="flex items-baseline gap-2">
                  <StatusDot status={job.status} className="size-1" />
                  <span className="w-8 shrink-0 font-mono t-meta text-ink-faint">
                    J{String(job.job_number).padStart(3, "0")}
                  </span>
                  <span className="min-w-0 flex-1 truncate t-label">
                    {job.alias !== "None" ? job.alias : job.type_label}
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      ) : (
        <div className="flex items-center gap-1.5 t-label text-st-failed">
          <AlertCircle className="size-3.5" />
          not readable on this machine
        </div>
      )}

      {readable && !busy && (
        // The whole card below the header opens the project, so a click
        // anywhere that is not the menu does the obvious thing.
        <button
          type="button"
          onClick={onOpen}
          className="absolute inset-0 rounded-box focus-visible:outline-offset-[-2px]"
          aria-label={`Open ${project.name}`}
          tabIndex={-1}
        />
      )}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="grid place-items-center rounded-box border border-dashed border-rule px-6 py-16">
      <div className="max-w-sm text-center">
        <FolderOpen className="mx-auto mb-3 size-6 text-ink-faint" />
        <p className="t-ui font-medium">No projects yet</p>
        <p className="mt-1.5 t-body leading-relaxed text-ink-muted">
          Paste a directory path above. An existing RELION project is opened as
          it is; an empty folder is initialised as a new project.
        </p>
      </div>
    </div>
  );
}
