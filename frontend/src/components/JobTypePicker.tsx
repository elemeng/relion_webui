// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useEffect, useMemo, useRef, useState } from "react";
import { Plus, Search, X } from "lucide-react";

import { cn } from "@/lib/utils";
import type { JobCategory, JobFormSchema } from "@/lib/types";

interface JobTypePickerProps {
  categories: JobCategory[];
  schemas: Record<string, JobFormSchema>;
  onPick: (jobType: string) => void;
  /** A place to drop it; the canvas decides where the dock lives. */
  className?: string;
}

/**
 * The add-a-job control: a round + that opens a searchable list.
 *
 * ComfyUI's pattern: the graph stays the interface, and adding a node is a
 * transient popup rather than a permanent panel. The search field is focused on
 * open, so the common case - you know the job name - is type, arrow, Enter.
 *
 * Grouping follows the workflow stage, because RELION's own list is flat and
 * 34 entries, half of them variants of another, is a lot to scan.
 */
export function JobTypePicker({
  categories,
  schemas,
  onPick,
  className,
}: JobTypePickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const title = (jobType: string) => schemas[jobType]?.title ?? jobType;
  const isVariant = (jobType: string) =>
    schemas[jobType] ? schemas[jobType].base_job_type !== jobType : false;

  // Flattened, in display order, so arrow keys match what the eye sees.
  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows: { jobType: string; group: string }[] = [];
    for (const category of categories) {
      for (const jobType of category.job_types) {
        if (!needle) {
          rows.push({ jobType, group: category.title });
          continue;
        }
        const haystack = [
          jobType,
          title(jobType),
          category.title,
          schemas[jobType]?.executable ?? "",
        ]
          .join(" ")
          .toLowerCase();
        if (haystack.includes(needle))
          rows.push({ jobType, group: category.title });
      }
    }
    return rows;
    // `title` derives from schemas, which is a dependency already.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [categories, query, schemas]);

  const grouped = useMemo(() => {
    const out: { title: string; jobTypes: string[] }[] = [];
    for (const row of matches) {
      const last = out[out.length - 1];
      if (last && last.title === row.group) last.jobTypes.push(row.jobType);
      else out.push({ title: row.group, jobTypes: [row.jobType] });
    }
    return out;
  }, [matches]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      // Radix-free on purpose: the picker is one input, and a focus race with a
      // portal is worse than focusing on the next frame.
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => {
    setActive(0);
  }, [query]);

  // Keep the highlighted row in view as the selection moves.
  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(
      '[data-active="true"]',
    );
    node?.scrollIntoView({ block: "nearest" });
  }, [active]);

  // Clicking away closes it, the same as every other popup on the canvas.
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close, true);
    return () => document.removeEventListener("pointerdown", close, true);
  }, [open]);

  const choose = (jobType: string) => {
    onPick(jobType);
    setOpen(false);
  };

  const activeRow = matches[active];

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      {/* The + stays put when the list opens. It becomes the close button, so
          the popup has a visible anchor instead of floating free, and it is
          never covered by the panel it just opened. */}
      <button
        type="button"
        data-add-job
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className={cn(
          "grid size-11 place-items-center rounded-full border shadow-lg transition-transform",
          open
            ? "rotate-45 border-frame-muted bg-frame-raise text-frame-ink"
            : "border-frame-rule bg-frame text-frame-ink hover:scale-105 hover:border-frame-muted",
        )}
        title={open ? "Close (Esc)" : "Add a job (N)"}
        aria-label={open ? "Close the job list" : "Add a job"}
      >
        <Plus className="size-5" />
      </button>

      {open && (
        <div
          className={cn(
            // Above the +: opening downward would push the tail of a 34-entry
            // list off the bottom of the window on a laptop screen.
            "absolute bottom-[calc(100%+0.5rem)] left-0 flex max-h-[min(74vh,35rem)] w-[23.75rem] flex-col overflow-hidden",
            "rounded-box border border-rule bg-base-100 shadow-[0_16px_48px_-12px_rgb(0_0_0/0.3)]",
          )}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              setOpen(false);
              return;
            }
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setActive((value) => Math.min(matches.length - 1, value + 1));
              return;
            }
            if (event.key === "ArrowUp") {
              event.preventDefault();
              setActive((value) => Math.max(0, value - 1));
              return;
            }
            if (event.key === "Enter" && activeRow) {
              event.preventDefault();
              choose(activeRow.jobType);
            }
          }}
        >
          <div className="flex items-center gap-2 border-b border-rule px-2.5 py-2">
            <Search className="size-3.5 shrink-0 text-ink-faint" />
            <input
              ref={inputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search jobs…"
              spellCheck={false}
              className="min-w-0 flex-1 bg-transparent t-ui outline-none placeholder:text-ink-faint"
            />
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="grid size-5 place-items-center rounded-selector text-ink-faint hover:bg-base-200 hover:text-base-content"
              title="Close (Esc)"
              aria-label="Close"
            >
              <X className="size-3" />
            </button>
          </div>

          <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-1">
            {grouped.length === 0 && (
              <p className="px-3 py-5 t-body text-ink-muted">
                No job matches “{query}”.
              </p>
            )}
            {grouped.map((group) => (
              <div key={group.title}>
                <div className="t-section px-3 pt-2 pb-1 text-ink-faint">
                  {group.title}
                </div>
                {group.jobTypes.map((jobType) => {
                  const index = matches.findIndex(
                    (row) => row.jobType === jobType,
                  );
                  const highlighted = index === active;
                  return (
                    <button
                      key={jobType}
                      type="button"
                      data-active={highlighted}
                      onMouseEnter={() => setActive(index)}
                      onClick={() => choose(jobType)}
                      className={cn(
                        "flex w-full items-center gap-2 py-1.5 pr-3 t-ui",
                        isVariant(jobType) ? "pl-7" : "pl-3",
                        highlighted
                          ? "bg-[color-mix(in_srgb,var(--signal)_12%,transparent)] text-base-content"
                          : "text-base-content hover:bg-base-200",
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate text-left">
                        {title(jobType)}
                      </span>
                      <span className="shrink-0 font-mono t-meta text-ink-faint">
                        {jobType}
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>

          <div className="flex shrink-0 items-center gap-2 border-t border-rule bg-base-200/60 px-3 py-1.5 t-meta text-ink-faint">
            <kbd className="rounded-[0.125rem] border border-rule bg-base-100 px-1 py-px">
              ↑↓
            </kbd>
            navigate
            <kbd className="ml-1.5 rounded-[0.125rem] border border-rule bg-base-100 px-1 py-px">
              ↵
            </kbd>
            add
            <span className="flex-1" />
            <span>{matches.length} job types</span>
          </div>
        </div>
      )}
    </div>
  );
}
