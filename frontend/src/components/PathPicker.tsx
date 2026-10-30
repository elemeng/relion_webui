// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useCallback, useEffect, useMemo, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import {
  Check,
  ChevronUp,
  Eye,
  EyeOff,
  FileCode2,
  Folder,
  FolderOpen,
  FolderPlus,
  HardDrive,
  Home,
  Plus,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
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
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

export type BrowseFilter = "all" | "directory" | "project" | "star";

interface Entry {
  name: string;
  path: string;
  is_dir: boolean;
  is_project: boolean;
  is_star: boolean;
  size?: number;
  error?: string;
}

interface Listing {
  path: string;
  parent: string | null;
  entries: Entry[];
  truncated: boolean;
  error?: string;
}

interface Location {
  label: string;
  path: string;
}

interface PathPickerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (path: string) => void;
  /** What the caller is looking for; controls filtering and which button says "select". */
  mode: BrowseFilter;
  title?: string;
  description?: string;
  /** Path to open the dialog at. */
  initialPath?: string;
}

/**
 * Remote directory browser.
 *
 * Browsing happens on the agent, so it shows the filesystem of the machine
 * running RELION rather than the viewer's own machine. Selecting is a separate
 * explicit action; nothing is written here.
 */
export function PathPicker({
  open,
  onOpenChange,
  onPick,
  mode,
  title,
  description,
  initialPath,
}: PathPickerProps) {
  const [listing, setListing] = useState<Listing | null>(null);
  const [locations, setLocations] = useState<Location[]>([]);
  const [typed, setTyped] = useState("");
  const [selected, setSelected] = useState<Entry | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [creating, setCreating] = useState(false);
  const [submittingFolder, setSubmittingFolder] = useState(false);
  const [newName, setNewName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);

  const load = useCallback(
    async (path?: string) => {
      setLoading(true);
      setError(null);
      setSelected(null);
      try {
        const params = new URLSearchParams({ filter: mode });
        if (path) params.set("path", path);
        if (showHidden) params.set("hidden", "true");
        const data = await api.get<Listing>(`/api/browse?${params.toString()}`);
        setListing(data);
        setTyped(data.path);
        if (data.error) setError(data.error);
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "could not list directory",
        );
      } finally {
        setLoading(false);
      }
    },
    [mode, showHidden],
  );

  // Opening always starts from the caller's hint, else the agent's home.
  useEffect(() => {
    if (!open) return;
    load(initialPath || undefined);
    api
      .get<{ locations: Location[] }>("/api/browse/locations")
      .then((data) => setLocations(data.locations))
      .catch(() => setLocations([]));
  }, [open, initialPath, load]);

  // Toggling hidden entries re-reads the current directory rather than
  // returning to the start, so the toggle acts on what is in front of you.
  useEffect(() => {
    if (!open || !listing) return;
    load(listing.path);
    // Intentionally only on the toggle; `listing` changes on every load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showHidden]);

  const createFolder = useCallback(async () => {
    if (!listing || !newName.trim() || submittingFolder) return;
    setSubmittingFolder(true);
    setCreateError(null);
    try {
      const created = await api.post<{ path: string; name: string }>(
        "/api/browse/folder",
        { parent: listing.path, name: newName.trim() },
      );
      setNewName("");
      setCreating(false);
      await load(listing.path);
      // Keep the new folder visible: select it so the footer names it.
      setSelected({
        name: created.name,
        path: created.path,
        is_dir: true,
        is_project: false,
        is_star: false,
      });
    } catch (caught) {
      setCreateError(
        caught instanceof Error
          ? caught.message
          : "could not create the folder",
      );
    } finally {
      setSubmittingFolder(false);
    }
  }, [listing, load, newName, submittingFolder]);

  const canSelectHere = mode === "directory" ? listing?.path : null;
  const pickedPath = useMemo(() => {
    // A row the user clicked always wins: clicking a project badge should pick
    // that project, not the directory currently being browsed.
    if (selected) return selected.path;
    if (mode === "project") return listing?.path ?? null;
    return null;
  }, [selected, mode, listing]);

  function choose() {
    if (!pickedPath) return;
    onPick(pickedPath);
    onOpenChange(false);
  }

  // What the filter actually found here. Directories are always listed, so
  // without this the picker looks unfiltered.
  const matchCount = useMemo(() => {
    if (!listing) return 0;
    if (mode === "project")
      return listing.entries.filter((e) => e.is_project).length;
    if (mode === "star") return listing.entries.filter((e) => e.is_star).length;
    return 0;
  }, [listing, mode]);

  const matchLabel =
    mode === "project"
      ? `project${matchCount === 1 ? "" : "s"}`
      : `STAR file${matchCount === 1 ? "" : "s"}`;

  const heading =
    title ??
    (mode === "star"
      ? "Choose a STAR file"
      : mode === "project"
        ? "Choose a RELION project"
        : "Choose a directory");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85svh] flex-col gap-3 sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{heading}</DialogTitle>
          <DialogDescription>
            {description ??
              "Browsing the filesystem of the machine running the agent."}
          </DialogDescription>
        </DialogHeader>

        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            load(typed.trim() || undefined);
          }}
        >
          <Input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            spellCheck={false}
            className="h-8 font-mono text-xs"
            placeholder="/path/to/open"
          />
          <Button
            type="submit"
            size="sm"
            variant="outline"
            className="shrink-0"
          >
            Go
          </Button>
        </form>

        <div className="flex flex-wrap gap-1.5">
          {locations.map((location) => (
            <Button
              key={location.path}
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 gap-1.5 px-2 text-xs"
              onClick={() => load(location.path)}
            >
              {location.label === "home" ? (
                <Home className="size-3.5" />
              ) : (
                <HardDrive className="size-3.5" />
              )}
              {location.label}
            </Button>
          ))}
          {listing?.parent && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 gap-1.5 px-2 text-xs"
              onClick={() => load(listing.parent ?? undefined)}
            >
              <ChevronUp className="size-3.5" />
              up
            </Button>
          )}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 gap-1.5 px-2 text-xs"
            onClick={() => setShowHidden((value) => !value)}
            aria-pressed={showHidden}
            title={
              showHidden
                ? "Hide entries that start with a dot"
                : "Show entries that start with a dot"
            }
          >
            {showHidden ? (
              <Eye className="size-3.5" />
            ) : (
              <EyeOff className="size-3.5" />
            )}
            {showHidden ? "hidden on" : "hidden"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 gap-1.5 px-2 text-xs"
            onClick={() => setCreating((value) => !value)}
            aria-pressed={creating}
            title="Create a folder in this directory"
          >
            <FolderPlus className="size-3.5" />
            new folder
          </Button>
        </div>

        {creating && (
          <form
            className="flex flex-col gap-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              createFolder();
            }}
          >
            <div className="flex items-center gap-2">
              <Input
                autoFocus
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                placeholder="folder name"
                spellCheck={false}
                className="h-8 min-w-0 flex-1 font-mono text-xs"
              />
              <Button
                type="submit"
                size="sm"
                className="h-8 shrink-0 text-xs"
                disabled={submittingFolder || !newName.trim()}
              >
                {submittingFolder ? (
                  <Spinner size="xs" />
                ) : (
                  <Plus className="size-3" />
                )}
                Create
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 shrink-0 text-xs"
                onClick={() => {
                  setCreating(false);
                  setNewName("");
                  setCreateError(null);
                }}
              >
                Cancel
              </Button>
            </div>
            {createError ? (
              <p className="t-label text-[var(--st-failed-text)]" role="alert">
                {createError}
              </p>
            ) : (
              <p className="t-meta leading-relaxed text-ink-muted">
                One level, inside {listing?.path ?? "this directory"}. Creating
                selects it; it is not written to until you choose it.
              </p>
            )}
          </form>
        )}

        {(mode === "project" || mode === "star") && listing && (
          <p className="t-meta leading-relaxed text-ink-muted">
            {matchCount > 0
              ? `${matchCount} ${matchLabel} here; every directory is listed so you can keep browsing.`
              : `No ${matchLabel} in this directory. Directories are still listed so you can browse further.`}
          </p>
        )}

        <div className="min-h-0 flex-1 overflow-auto rounded-[4px] border border-rule">
          {loading && (
            <div className="flex items-center justify-center gap-2 p-8">
              <Spinner size="sm" className="text-base-content/60" />
              <span className="t-ui text-ink-muted">loading…</span>
            </div>
          )}

          {!loading && error && (
            <p className="p-4 t-ui text-st-failed">{error}</p>
          )}

          {!loading && !error && listing && (
            <ul>
              {listing.entries.map((entry) => {
                const isSelected = selected?.path === entry.path;
                return (
                  <li key={entry.path}>
                    <button
                      type="button"
                      onClick={() => {
                        // A single click on a directory descends into it, in
                        // every mode. Selecting is the footer button's job,
                        // so a project can always be opened and inspected
                        // before being chosen - and a project's subdirectories
                        // stay reachable.
                        if (entry.is_dir) {
                          load(entry.path);
                          return;
                        }
                        setSelected(entry);
                      }}
                      onDoubleClick={() => {
                        if (entry.is_dir) {
                          load(entry.path);
                          return;
                        }
                        onPick(entry.path);
                        onOpenChange(false);
                      }}
                      className={cn(
                        "flex w-full items-center gap-2 px-3 py-1.5 text-left t-ui transition-colors",
                        isSelected
                          ? "bg-[color-mix(in_srgb,var(--signal)_10%,transparent)]"
                          : "hover:bg-base-200",
                      )}
                    >
                      {entry.is_dir ? (
                        entry.is_project ? (
                          <FolderOpen className="text-accent size-4 shrink-0" />
                        ) : (
                          <Folder className="size-4 shrink-0 text-ink-faint" />
                        )
                      ) : (
                        <FileCode2 className="size-4 shrink-0 text-ink-faint" />
                      )}

                      <span className="min-w-0 flex-1 truncate">
                        {entry.name}
                      </span>

                      {entry.is_project && (
                        <Badge tone="primary">project</Badge>
                      )}
                      {entry.is_star && <Badge tone="muted">star</Badge>}
                      {isSelected && (
                        <Check className="size-4 shrink-0 text-signal" />
                      )}
                    </button>
                  </li>
                );
              })}

              {listing.entries.length === 0 && (
                <li className="p-4 t-ui text-ink-muted">
                  this directory is empty
                </li>
              )}
              {listing.truncated && (
                <li className="border-t border-rule-soft p-2 t-label text-ink-muted">
                  listing truncated; use the path field to narrow it down
                </li>
              )}
            </ul>
          )}
        </div>

        <DialogFooter className="items-center gap-2 sm:justify-between">
          <span className="truncate font-mono t-meta text-ink-muted">
            {pickedPath ?? listing?.path ?? ""}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={!pickedPath && !canSelectHere}
              onClick={choose}
            >
              {mode === "project" ? "Use this directory" : "Select"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
