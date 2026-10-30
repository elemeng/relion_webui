// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useId, useMemo, useState } from "react";
import { ChevronDown, Plus, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { CliOption } from "@/lib/types";

/** A row the user added: a flag and, unless it is a boolean, its value. */
export interface CliExtra {
  flag: string;
  value: string;
}

interface AdvancedCliPanelProps {
  /** Every flag the program accepts, with those the form sets marked. */
  options: CliOption[];
  /** Why the program's flags could not be read, when they could not. */
  unavailable: string | null;
  /** The job's output flag style, which the flag picker filters by. */
  flagStyle: "underscore" | "hyphen" | null;
  value: CliExtra[];
  onChange: (rows: CliExtra[]) => void;
}

/**
 * The escape hatch for flags the GUI never shows.
 *
 * RELION's job window exposes a renamed, merged subset of each program's flags,
 * so a value the program accepts can be unreachable from the form. This panel
 * lists the program's real flags - extracted from its own argument parser - and
 * lets any of them be set. They travel in ``other_args``, which every builder
 * already appends at the position its program expects, so nothing about the
 * command's shape changes.
 *
 * A flag the form already sets can still be added: ``IOParser::getOption``
 * keeps the last occurrence, so this is how a value is overridden.
 */
export function AdvancedCliPanel({
  options,
  unavailable,
  flagStyle,
  value,
  onChange,
}: AdvancedCliPanelProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [showUsed, setShowUsed] = useState(false);
  const listId = useId();

  const freeCount = useMemo(
    () => options.filter((option) => !option.used_by_form).length,
    [options],
  );

  // The picker offers what the job's own programs read. Matching flags come
  // first, but flags of the other style are kept: a job can run more than one
  // program (tomo_import runs a Typer app and a C++ utility), so hiding a valid
  // override would be worse than showing an extra entry.
  const candidates = useMemo(() => {
    const matchesStyle = (flag: string) =>
      flagStyle === "hyphen"
        ? !flag.includes("_")
        : !(flag.includes("-") && !flag.includes("_"));
    const text = filter.trim().toLowerCase();
    const visible = options
      .filter((option) => showUsed || !option.used_by_form)
      .filter(
        (option) =>
          !text ||
          option.flag.toLowerCase().includes(text) ||
          (option.usage ?? "").toLowerCase().includes(text),
      );
    return [...visible].sort((left, right) => {
      const a = matchesStyle(left.flag) ? 0 : 1;
      const b = matchesStyle(right.flag) ? 0 : 1;
      return a - b;
    });
  }, [options, filter, flagStyle, showUsed]);

  function update(index: number, patch: Partial<CliExtra>) {
    onChange(
      value.map((row, position) =>
        position === index ? { ...row, ...patch } : row,
      ),
    );
  }

  function remove(index: number) {
    onChange(value.filter((_, position) => position !== index));
  }

  function add(flag = "") {
    onChange([...value, { flag, value: "" }]);
    setOpen(true);
  }

  return (
    <div className="overflow-hidden rounded-box border border-rule">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="flex w-full items-center gap-2 bg-base-200/70 px-2.5 py-1.5 text-left hover:bg-base-200"
        aria-expanded={open}
        aria-controls={listId}
      >
        <ChevronDown
          className={cn(
            "size-3.5 shrink-0 text-ink-faint transition-transform",
            open && "rotate-180",
          )}
        />
        <span className="t-label font-medium">Advanced arguments</span>
        {value.length > 0 ? (
          <Badge tone="primary">{value.length}</Badge>
        ) : (
          <span className="t-meta text-ink-faint">
            {unavailable
              ? unavailable
              : `${freeCount} flag${freeCount === 1 ? "" : "s"} not in this form`}
          </span>
        )}
        <span className="ml-auto shrink-0 font-mono t-meta text-ink-faint">
          {value.length === 0 ? "" : "other_args"}
        </span>
      </button>

      {open && (
        <div
          id={listId}
          className="flex flex-col gap-2 border-t border-rule-soft p-2.5"
        >
          {unavailable ? (
            <p className="t-meta leading-relaxed text-ink-muted">
              {unavailable}. Anything typed here is passed through unchanged, so
              check the program&apos;s own documentation for the accepted flags.
            </p>
          ) : (
            <p className="t-meta leading-relaxed text-ink-muted">
              Passed through as <span className="font-mono">other_args</span>,
              which the program reads last, so a value here overrides the form.
              Flags marked <span className="font-medium">form</span> already
              have a control above.
            </p>
          )}

          {value.length > 0 && (
            <ul className="flex flex-col gap-1.5">
              {value.map((row, index) => {
                const known = options.find(
                  (option) => option.flag === row.flag,
                );
                const boolean = known?.kind === "boolean";
                return (
                  <li
                    key={`${row.flag}-${index}`}
                    className="flex items-start gap-1.5"
                  >
                    <div className="min-w-0 flex-1">
                      <Input
                        list={listId + "-flags"}
                        value={row.flag}
                        placeholder="--flag"
                        spellCheck={false}
                        className="h-8 font-mono t-ui"
                        onChange={(event) =>
                          update(index, { flag: event.target.value })
                        }
                      />
                      {known && (
                        <p className="mt-0.5 flex items-center gap-1 t-meta leading-snug text-ink-faint">
                          {known.used_by_form && (
                            <Badge tone="muted">form</Badge>
                          )}
                          <span className="truncate">
                            {known.usage || known.flag}
                          </span>
                        </p>
                      )}
                      {!known && row.flag.startsWith("-") && (
                        <p className="mt-0.5 t-meta leading-snug text-[var(--st-aborted-text)]">
                          not a flag this program declares; it will be ignored
                        </p>
                      )}
                    </div>
                    <Input
                      value={row.value}
                      placeholder={boolean ? "flag" : "value"}
                      disabled={boolean}
                      spellCheck={false}
                      className="h-8 w-28 shrink-0 font-mono t-ui"
                      onChange={(event) =>
                        update(index, { value: event.target.value })
                      }
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-8 shrink-0 text-ink-faint hover:text-st-failed"
                      title="Remove"
                      onClick={() => remove(index)}
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="flex flex-wrap items-center gap-1.5">
            <Button variant="outline" size="sm" onClick={() => add()}>
              <Plus className="size-3.5" />
              Add flag
            </Button>
            <Input
              value={filter}
              placeholder="filter flags…"
              className="h-8 min-w-32 flex-1 t-ui"
              onChange={(event) => setFilter(event.target.value)}
            />
            {!unavailable && (
              <Button
                variant="ghost"
                size="sm"
                className="t-label"
                onClick={() => setShowUsed((current) => !current)}
                title="Show flags that already have a form control, to override one"
              >
                {showUsed ? "hide form flags" : "show form flags"}
              </Button>
            )}
          </div>

          {!unavailable && filter.trim() !== "" && candidates.length > 0 && (
            <ul className="max-h-48 overflow-y-auto rounded-field border border-rule">
              {candidates.slice(0, 40).map((option) => (
                <li key={option.flag}>
                  <button
                    type="button"
                    className="hover:bg-base-200 flex w-full flex-col items-start gap-0.5 px-2 py-1 text-left"
                    onClick={() => add(option.flag)}
                  >
                    <span className="flex w-full items-center gap-1.5 font-mono t-label">
                      {option.flag}
                      {option.kind === "boolean" ? (
                        <Badge tone="muted">flag</Badge>
                      ) : (
                        <Badge tone="muted">value</Badge>
                      )}
                      {option.used_by_form && <Badge tone="muted">form</Badge>}
                    </span>
                    {option.usage && (
                      <span className="line-clamp-1 t-meta text-ink-faint">
                        {option.usage}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <datalist id={listId + "-flags"}>
            {candidates.slice(0, 200).map((option) => (
              <option key={option.flag} value={option.flag}>
                {option.usage ?? ""}
              </option>
            ))}
          </datalist>
        </div>
      )}
    </div>
  );
}
