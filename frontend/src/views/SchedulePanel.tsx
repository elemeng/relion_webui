import { useCallback, useEffect, useState } from "react";
import {
  CalendarClock,
  FolderInput,
  FolderOutput,
  Play,
  Square,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { PipelineProcess } from "@/lib/types";

interface SchedulePanelProps {
  projectDirectory: string;
  processes: PipelineProcess[];
  onDone: (message: string) => void;
  onError: (message: string) => void;
}

interface SchedulerStatus {
  schedule: string;
  running: boolean;
  log: string;
  tail: string;
}

/**
 * The scheduler, plus export and import.
 *
 * The scheduler is RELION's own: `relion_pipeliner --RunJobs --schedule` is
 * started detached, so the server is not blocked, and stopping it removes the
 * `RUNNING_PIPELINER_<name>` file the scheduler polls for. Export writes the
 * same archive layout RELION does (scheduled jobs renamed to `<Type>/expNNN`),
 * and import renumbers them into this project.
 */
export function SchedulePanel({
  projectDirectory,
  processes,
  onDone,
  onError,
}: SchedulePanelProps) {
  const scheduled = processes.filter((p) => p.status === "Scheduled");

  const [selected, setSelected] = useState<string[]>([]);
  const [name, setName] = useState("web");
  const [repeat, setRepeat] = useState(1);
  const [waitMinutes, setWaitMinutes] = useState(0);
  const [archive, setArchive] = useState("share");
  const [listing, setListing] = useState("");
  const [status, setStatus] = useState<SchedulerStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshStatus = useCallback(async () => {
    try {
      const data = await api.get<SchedulerStatus>(
        `/api/project/scheduler?project_directory=${encodeURIComponent(
          projectDirectory,
        )}&name=${encodeURIComponent(name)}`,
      );
      setStatus(data);
    } catch {
      setStatus(null);
    }
  }, [projectDirectory, name]);

  useEffect(() => {
    refreshStatus();
    // The scheduler runs outside this process, so its state is polled rather
    // than pushed; a few seconds is enough for a progress view.
    const timer = window.setInterval(refreshStatus, 5000);
    return () => window.clearInterval(timer);
  }, [refreshStatus]);

  const run = useCallback(
    async (action: () => Promise<{ message: string }>) => {
      setBusy(true);
      try {
        const result = await action();
        onDone(result.message);
        await refreshStatus();
      } catch (error) {
        onError(error instanceof Error ? error.message : "action failed");
      } finally {
        setBusy(false);
      }
    },
    [onDone, onError, refreshStatus],
  );

  const toggle = (processName: string) => {
    setSelected((current) =>
      current.includes(processName)
        ? current.filter((item) => item !== processName)
        : [...current, processName],
    );
  };

  return (
    <div className="flex flex-col gap-3 t-ui">
      <section className="overflow-hidden rounded-box border border-rule">
        <h3 className="flex items-center gap-1.5 border-b border-rule-soft bg-base-200/70 px-3 py-1.5 t-section text-ink-faint">
          <CalendarClock className="size-3" />
          Scheduler
        </h3>
        <div className="flex flex-col gap-2.5 p-3">
          {scheduled.length === 0 ? (
            <p className="t-label leading-relaxed text-ink-muted">
              No scheduled jobs. A job is scheduled when it is registered but
              not yet run.
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {scheduled.map((process) => (
                <li key={process.name}>
                  <label className="flex cursor-pointer items-center gap-2 rounded-[3px] px-1.5 py-1 t-ui hover:bg-base-200">
                    <input
                      type="checkbox"
                      checked={selected.includes(process.name)}
                      onChange={() => toggle(process.name)}
                      className="checkbox checkbox-xs"
                    />
                    <span className="font-mono t-label">{process.name}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}

          <div className="grid grid-cols-3 gap-2">
            <div className="flex flex-col gap-1">
              <Label>name</Label>
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="font-mono"
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label>repeats</Label>
              <Input
                type="number"
                min={1}
                value={repeat}
                onChange={(event) => setRepeat(Number(event.target.value) || 1)}
                className="font-mono"
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label>wait (min)</Label>
              <Input
                type="number"
                min={0}
                value={waitMinutes}
                onChange={(event) =>
                  setWaitMinutes(Number(event.target.value) || 0)
                }
                className="font-mono"
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={busy || selected.length === 0}
              onClick={() =>
                run(async () => {
                  const response = await api.post<{ message: string }>(
                    "/api/project/scheduler/run",
                    {
                      project_directory: projectDirectory,
                      process_names: selected,
                      name,
                      repeat,
                      wait_minutes: waitMinutes,
                    },
                  );
                  return {
                    message: response.message || `started scheduler ${name}`,
                  };
                })
              }
            >
              <Play className="size-3.5" />
              Run scheduled
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || !status?.running}
              onClick={() =>
                run(async () => {
                  const response = await api.post<{ message: string }>(
                    "/api/project/scheduler/stop",
                    { project_directory: projectDirectory, name },
                  );
                  return { message: response.message || "stop requested" };
                })
              }
            >
              <Square className="size-3.5" />
              Stop
            </Button>
            <span
              className={cn(
                "rounded-[3px] px-1.5 py-0.5 font-mono t-meta",
                status?.running
                  ? "bg-[var(--st-succeeded-fill)] text-[var(--st-succeeded-text)]"
                  : "bg-base-200 text-ink-faint",
              )}
            >
              {status?.running ? "running" : "idle"}
            </span>
          </div>

          {status?.tail && (
            <pre className="max-h-32 overflow-auto rounded-[4px] border border-rule bg-base-200 p-2 font-mono t-meta whitespace-pre-wrap">
              {status.tail}
            </pre>
          )}
        </div>
      </section>

      <section className="overflow-hidden rounded-box border border-rule">
        <h3 className="flex items-center gap-1.5 border-b border-rule-soft bg-base-200/70 px-3 py-1.5 t-section text-ink-faint">
          <FolderOutput className="size-3" />
          Export / import
        </h3>
        <div className="flex flex-col gap-2.5 p-3">
          <p className="t-label leading-relaxed text-ink-muted">
            Export writes the selected scheduled jobs to{" "}
            <code>ExportJobs/&lt;name&gt;</code>, renamed to{" "}
            <code>&lt;Type&gt;/expNNN</code> as RELION does. Import copies them
            into this project with new job numbers.
          </p>
          <div className="flex flex-wrap items-end gap-2">
            <div className="flex flex-col gap-1">
              <Label>archive name</Label>
              <Input
                value={archive}
                onChange={(event) => setArchive(event.target.value)}
                className="w-32 font-mono"
              />
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || selected.length === 0}
              onClick={() =>
                run(async () => {
                  const response = await api.post<{ message: string }>(
                    "/api/project/export",
                    {
                      project_directory: projectDirectory,
                      process_names: selected,
                      name: archive,
                    },
                  );
                  return { message: response.message || "exported" };
                })
              }
            >
              <FolderOutput className="size-3.5" />
              Export
            </Button>
          </div>

          <div className="flex flex-wrap items-end gap-2">
            <div className="flex flex-1 flex-col gap-1">
              <Label>exported.star to import</Label>
              <Input
                value={listing}
                onChange={(event) => setListing(event.target.value)}
                placeholder="path/to/ExportJobs/share/exported.star"
                className="font-mono"
              />
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || !listing.trim()}
              onClick={() =>
                run(async () => {
                  const response = await api.post<{ message: string }>(
                    "/api/project/import",
                    {
                      project_directory: projectDirectory,
                      listing: listing.trim(),
                    },
                  );
                  return { message: response.message || "imported" };
                })
              }
            >
              <FolderInput className="size-3.5" />
              Import
            </Button>
          </div>
        </div>
      </section>
    </div>
  );
}
