import { useCallback, useEffect, useState } from "react";
import { FileText, Save, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import type { PipelineProcess } from "@/lib/types";

interface JobActionsProps {
  projectDirectory: string;
  process: PipelineProcess | null;
  onOpenChange: (open: boolean) => void;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}

/**
 * The desktop GUI's "Job actions" menu, for one job.
 *
 * Alias and clean delegate to RELION's own implementation (an alias changes the
 * node paths downstream jobs use; cleaning has per-job-type rules). Note editing
 * writes the same `note.txt` the GUI's note editor opens. Delete moves the job to
 * `Trash/`, which is what makes Undelete possible.
 */
export function JobActionsDialog({
  projectDirectory,
  process,
  onOpenChange,
  onDone,
  onError,
}: JobActionsProps) {
  const [alias, setAlias] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const processName = process?.name ?? null;

  /**
   * The REST path for one process.
   *
   * `process.name` contains a slash (`Tomograms/job004`), which a path parameter
   * carries only if it is not encoded - encoding would turn the slash into %2F and
   * the route would not match. The project directory therefore travels as a query
   * parameter instead, which keeps the path readable in a log.
   */
  function processPath(projectDirectory: string, name: string): string {
    return `/api/projects/processes/${name}?project_directory=${encodeURIComponent(
      projectDirectory,
    )}`;
  }

  useEffect(() => {
    if (!processName) return;
    setLoaded(false);
    setAlias(
      process?.alias && process.alias !== "None"
        ? (process.alias.replace(/\/$/, "").split("/").pop() ?? "")
        : "",
    );
    api
      .get<{ detail: { note: string } }>(
        `/api/project/job/note?project_directory=${encodeURIComponent(
          projectDirectory,
        )}&process_name=${encodeURIComponent(processName)}`,
      )
      .then((data) => setNote(data.detail.note))
      .catch(() => setNote(""))
      .finally(() => setLoaded(true));
  }, [processName, process?.alias, projectDirectory]);

  const run = useCallback(
    async (label: string, request: () => Promise<{ message: string }>) => {
      setBusy(true);
      try {
        const result = await request();
        onDone(result.message || label);
      } catch (error) {
        onError(error instanceof Error ? error.message : `${label} failed`);
      } finally {
        setBusy(false);
      }
    },
    [onDone, onError],
  );

  if (!process) return null;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="w-[min(520px,92vw)] gap-0">
        <DialogHeader>
          <DialogTitle className="font-mono">{process.name}</DialogTitle>
          <DialogDescription>
            Alias, note, cleanup and removal for this job.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 py-3.5">
          <div className="flex flex-col gap-1.5">
            <Label>Alias</Label>
            <div className="flex gap-2">
              <Input
                value={alias}
                onChange={(event) => setAlias(event.target.value)}
                placeholder="short name for downstream references"
                className="font-mono"
              />
              <Button
                size="sm"
                disabled={busy}
                onClick={() =>
                  run("alias", () =>
                    api
                      .patch<{ actions: { message: string }[] }>(
                        processPath(projectDirectory, process.name),
                        { alias },
                      )
                      .then((data) => data.actions[0]),
                  )
                }
              >
                Set
              </Button>
            </div>
            <p className="t-label text-ink-muted">
              An alias renames the node paths other jobs use; it is not just a
              label. Clear it to go back to the job number.
            </p>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Note</Label>
            <Textarea
              value={loaded ? note : "loading…"}
              onChange={(event) => setNote(event.target.value)}
              className="min-h-24 font-mono"
            />
            <Button
              size="sm"
              variant="outline"
              className="self-start"
              disabled={busy || !loaded}
              onClick={() =>
                run("note", () =>
                  api
                    .patch<{ actions: { message: string }[] }>(
                      processPath(projectDirectory, process.name),
                      { note },
                    )
                    .then((data) => data.actions[0]),
                )
              }
            >
              <Save className="size-3.5" />
              Save note
            </Button>
          </div>

          <div className="flex flex-wrap gap-2 border-t border-rule-soft pt-3.5">
            <Button
              size="sm"
              variant="outline"
              disabled={busy || process.status !== "Succeeded"}
              title={
                process.status !== "Succeeded"
                  ? "only successfully finished jobs can be cleaned"
                  : "uses RELION own cleanup rules"
              }
              onClick={() =>
                run("clean", () =>
                  api.post<{ message: string }>(
                    `${processPath(projectDirectory, process.name)}/clean?harsh=false`,
                  ),
                )
              }
            >
              Gentle clean
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || process.status !== "Succeeded"}
              onClick={() =>
                run("clean", () =>
                  api.post<{ message: string }>(
                    `${processPath(projectDirectory, process.name)}/clean?harsh=true`,
                  ),
                )
              }
            >
              Harsh clean
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() =>
                run("mark", () =>
                  api
                    .patch<{ actions: { message: string }[] }>(
                      processPath(projectDirectory, process.name),
                      { failed: process.status !== "Failed" },
                    )
                    .then((data) => data.actions[0]),
                )
              }
            >
              {process.status === "Failed" ? "Mark finished" : "Mark failed"}
            </Button>
            <Button
              size="sm"
              variant="destructive"
              className="ml-auto"
              disabled={busy}
              title="moves the job to Trash; Undelete brings it back"
              onClick={() => {
                if (
                  !window.confirm(
                    `Delete ${process.name}? Its directory moves to Trash/ and can be undeleted.`,
                  )
                ) {
                  return;
                }
                run("delete", () =>
                  api.delete<{ message: string }>(
                    processPath(projectDirectory, process.name),
                  ),
                );
              }}
            >
              <Trash2 className="size-3.5" />
              Delete
            </Button>
          </div>

          <p className="flex items-start gap-1.5 t-meta leading-relaxed text-ink-faint">
            <FileText className="mt-px size-3 shrink-0" />
            <span>
              Delete moves the job to <code className="font-mono">Trash/</code>{" "}
              and removes it from the pipeline; it is recoverable. The files
              themselves are not erased.
            </span>
          </p>
        </div>

        <DialogFooter>
          <span className="flex-1" />
          <Button
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
          >
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
