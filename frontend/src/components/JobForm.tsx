// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useMemo, useState } from "react";
import { AlertCircle, ChevronRight, Info, ListTree } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PathInput } from "@/components/PathInput";
import { Select, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { AdvancedCliPanel, type CliExtra } from "@/components/AdvancedCliPanel";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type {
  JobField,
  JobFieldValues,
  JobFormSchema,
  JobGate,
} from "@/lib/types";

interface JobFormProps {
  schema: JobFormSchema;
  values: JobFieldValues;
  onChange: (variable: string, value: string | number | boolean) => void;
  /** Options that are required but still empty. */
  missing: string[];
  /** Flags added through the advanced panel. */
  extraCli: CliExtra[];
  onExtraCliChange: (rows: CliExtra[]) => void;
  /** Open the list of files that may fill a node-typed option. */
  onBrowseNodes?: (variable: string, label: string) => void;
  /** A job card was dropped on a node-typed option. */
  onJobDropped?: (variable: string, label: string, producer: string) => void;
}

/**
 * The option form for one job, laid out as the desktop job window lays it out.
 *
 * The tabs and the field order come from `gui_jobwindow.cpp`, so a RELION user
 * finds the same options in the same place. Widget kinds come from each
 * `JobOption`'s C++ constructor: a slider for a bounded number, a switch for a
 * boolean, a select for a radio, and a path field (with the remote picker) for
 * anything that names a file.
 */
export function JobForm({
  schema,
  values,
  onChange,
  missing,
  extraCli,
  onExtraCliChange,
  onBrowseNodes,
  onJobDropped,
}: JobFormProps) {
  const fieldByName = useMemo(() => {
    const map = new Map<string, JobField>();
    for (const field of schema.fields) map.set(field.variable, field);
    return map;
  }, [schema]);

  // Only the fields the window actually places, in the order it places them.
  // Options the window does not show (scheduler settings, "other arguments")
  // stay out of the form but keep their defaults.
  const tabs = useMemo(
    () =>
      schema.tabs
        .map((tab) => ({
          ...tab,
          fields: tab.fields
            .map((name) => fieldByName.get(name))
            .filter((field): field is JobField =>
              Boolean(field && !field.internal),
            ),
        }))
        .filter((tab) => tab.fields.length > 0),
    [schema, fieldByName],
  );

  const unplaced = useMemo(() => {
    const placed = new Set(schema.tabs.flatMap((tab) => tab.fields));
    return schema.fields.filter(
      (field) => !placed.has(field.variable) && !field.internal,
    );
  }, [schema]);

  if (tabs.length === 0) {
    return <p className="t-body text-ink-muted">This job has no options.</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      {missing.length > 0 && (
        <div className="flex items-start gap-2 rounded-field border border-[color-mix(in_srgb,var(--st-failed-text)_35%,var(--rule))] bg-[var(--st-failed-fill)] px-2.5 py-2 t-label text-[var(--st-failed-text)]">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
          <span>
            Required before submitting:{" "}
            {missing
              .map((variable) =>
                (fieldByName.get(variable)?.label ?? variable).replace(
                  /:\s*$/,
                  "",
                ),
              )
              .join(", ")}
          </span>
        </div>
      )}

      {/* Vertical sections, not horizontal tabs. A job with eight tabs hides
            six of them; a stack of collapsible sections shows where everything
            is at once, and a long form scrolls in one direction. */}
      <div className="flex flex-col gap-2">
        {tabs.map((tab) => (
          <Section
            key={tab.index}
            title={tab.title}
            count={tab.fields.length}
            defaultOpen={tab.index === tabs[0].index}
          >
            <div className="flex flex-col gap-3 pt-2">
              {tab.fields.map((field) => {
                const gate = disabledBy(tab.gates, field.variable, values);
                return (
                  <FieldControl
                    key={field.variable}
                    field={field}
                    value={values[field.variable]}
                    required={schema.required.includes(field.variable)}
                    disabledBy={gate}
                    onChange={(value) => onChange(field.variable, value)}
                    onBrowseNodes={
                      onBrowseNodes
                        ? () => onBrowseNodes(field.variable, field.label)
                        : undefined
                    }
                    onJobDropped={
                      onJobDropped
                        ? (producer) =>
                            onJobDropped(field.variable, field.label, producer)
                        : undefined
                    }
                  />
                );
              })}
            </div>
          </Section>
        ))}
      </div>

      {unplaced.length > 0 && (
        <details className="t-label text-ink-muted">
          <summary className="cursor-pointer select-none">
            {unplaced.length} further option{unplaced.length === 1 ? "" : "s"}
          </summary>
          <div className="mt-2 flex flex-col gap-3">
            {unplaced.map((field) => (
              <FieldControl
                key={field.variable}
                field={field}
                value={values[field.variable]}
                required={schema.required.includes(field.variable)}
                onChange={(value) => onChange(field.variable, value)}
                onBrowseNodes={
                  onBrowseNodes
                    ? () => onBrowseNodes(field.variable, field.label)
                    : undefined
                }
                onJobDropped={
                  onJobDropped
                    ? (producer) =>
                        onJobDropped(field.variable, field.label, producer)
                    : undefined
                }
              />
            ))}
          </div>
        </details>
      )}

      <AdvancedCliPanel
        options={schema.cli_options ?? []}
        unavailable={schema.cli_options_unavailable ?? null}
        flagStyle={schema.flag_style}
        value={extraCli}
        onChange={onExtraCliChange}
      />
    </div>
  );
}

/**
 * One collapsible group of options.
 *
 * Open state is remembered per job type and section, so reopening a form you
 * were just editing puts the sections back the way you left them. The first
 * section starts open, which is where a form begins.
 */
function Section({
  title,
  count,
  defaultOpen,
  children,
}: {
  title: string;
  count: number;
  defaultOpen: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="overflow-hidden rounded-box border border-rule">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 bg-base-200/70 px-2.5 py-1.5 text-left hover:bg-base-200"
      >
        <ChevronRight
          className={cn(
            "size-3 shrink-0 text-ink-faint transition-transform",
            open && "rotate-90",
          )}
        />
        <span className="min-w-0 flex-1 truncate t-label font-medium">
          {title}
        </span>
        <span className="shrink-0 rounded-[0.125rem] bg-base-100 px-1.5 font-mono t-meta text-ink-faint">
          {count}
        </span>
      </button>
      {open && (
        <div className="border-t border-rule-soft px-2.5 py-2.5">
          {children}
        </div>
      )}
    </section>
  );
}

interface FieldControlProps {
  field: JobField;
  value: string | number | boolean | undefined;
  required: boolean;
  /** The controller option that currently makes this field inactive. */
  disabledBy?: string | null;
  onChange: (value: string | number | boolean) => void;
  /**
   * Opens the list of files that may fill this slot. Present only for
   * node-typed options, which are the ones with candidates to choose from.
   */
  onBrowseNodes?: () => void;
  /** A job was dropped on this field; its name is offered first. */
  onJobDropped?: (producer: string) => void;
}

function FieldControl({
  field,
  value,
  required,
  disabledBy,
  onChange,
  onBrowseNodes,
  onJobDropped,
}: FieldControlProps) {
  const label = field.label.replace(/:\s*$/, "");
  const help = field.help?.trim();
  const inactive = Boolean(disabledBy);
  const [dropping, setDropping] = useState(false);
  const acceptsDrop = Boolean(onJobDropped) && field.kind === "input_node";

  return (
    <div
      className={cn(
        "flex flex-col gap-1",
        inactive && "opacity-50",
        // The target has to be visible while dragging, or the gesture is a
        // guess: a slot you cannot see you are over is worse than no drag.
        acceptsDrop &&
          dropping &&
          "rounded-field outline-2 outline-offset-2 outline-signal",
      )}
      title={inactive ? `only used when ${disabledBy} is on` : undefined}
      onDragOver={
        acceptsDrop
          ? (event) => {
              if (
                event.dataTransfer.types.includes("application/x-relion-job")
              ) {
                event.preventDefault();
                event.dataTransfer.dropEffect = "link";
                setDropping(true);
              }
            }
          : undefined
      }
      onDragLeave={acceptsDrop ? () => setDropping(false) : undefined}
      onDrop={
        acceptsDrop
          ? (event) => {
              const producer = event.dataTransfer.getData(
                "application/x-relion-job",
              );
              event.preventDefault();
              setDropping(false);
              if (producer) onJobDropped?.(producer);
            }
          : undefined
      }
    >
      <div className="flex items-baseline justify-between gap-2">
        <Label
          htmlFor={field.variable}
          className="flex items-center gap-1.5 t-label leading-tight"
        >
          <span>{label}</span>
          {required && (
            <span className="text-st-failed" title="required">
              *
            </span>
          )}
          {/* The help is a paragraph in RELION's own option table. Printing it
              inline turns a 22-option form into a wall of grey; the marker is
              what tells you the text is there. */}
          {help && (
            <Tip text={help}>
              <button
                type="button"
                aria-label={`about ${label}`}
                className="text-ink-faint hover:text-base-content inline-flex shrink-0 transition-colors"
              >
                <Info className="size-3" />
              </button>
            </Tip>
          )}
        </Label>
        {field.kind === "boolean" ? (
          <Switch
            id={field.variable}
            checked={asBoolean(value)}
            disabled={inactive}
            onCheckedChange={onChange}
          />
        ) : null}
      </div>

      {field.kind === "boolean" ? null : field.kind === "radio" ? (
        <Select
          id={field.variable}
          value={String(value ?? field.default ?? "")}
          disabled={inactive}
          onValueChange={onChange}
        >
          {(field.choices ?? []).map((choice) => (
            <SelectItem key={choice} value={choice}>
              {choice}
            </SelectItem>
          ))}
        </Select>
      ) : field.kind === "slider" ? (
        <div className="flex items-center gap-2">
          <Input
            id={field.variable}
            type="number"
            className="h-8 flex-1 font-mono t-ui"
            value={String(value ?? field.default ?? "")}
            min={field.min}
            max={field.max}
            step={field.step}
            disabled={inactive}
            onChange={(event) => onChange(toNumber(event.target.value))}
          />
          {field.min !== undefined && field.max !== undefined && (
            <span className="shrink-0 font-mono t-meta text-ink-faint">
              {field.min}–{field.max}
            </span>
          )}
        </div>
      ) : field.kind === "input_node" || field.kind === "path" ? (
        // Three ways to fill a node/path option, all kept:
        //   * type or paste a path in the field;
        //   * the folder button, which browses the agent's filesystem
        //     (directories are always listed, so a directory is reachable too);
        //   * the list button, which offers the files earlier jobs produced.
        // The list button is for `input_node` only: a plain `path` option (an
        // executable, a queue template) has no candidate nodes.
        <div className="flex min-w-0 items-start gap-1">
          <div className="min-w-0 flex-1">
            <PathInput
              id={field.variable}
              value={String(value ?? "")}
              onChange={(next) => onChange(next)}
              mode={field.kind === "input_node" ? "star" : "all"}
              placeholder={field.pattern || "path"}
              disabled={inactive}
            />
          </div>
          {onBrowseNodes && field.kind === "input_node" && (
            <Tip text="Pick from the files earlier jobs produced">
              <button
                type="button"
                onClick={onBrowseNodes}
                disabled={inactive}
                aria-label={`choose ${label} from earlier jobs`}
                className="grid size-9 shrink-0 place-items-center rounded-field border border-rule text-ink-muted hover:border-rule-strong hover:bg-base-200 hover:text-base-content disabled:opacity-40"
              >
                <ListTree className="size-3.5" />
              </button>
            </Tip>
          )}
        </div>
      ) : isLongTextField(field) ? (
        <Textarea
          id={field.variable}
          className="min-h-16 font-mono t-ui"
          value={String(value ?? "")}
          disabled={inactive}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <Input
          id={field.variable}
          className="h-8 font-mono t-ui"
          value={String(value ?? "")}
          disabled={inactive}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </div>
  );
}

function asBoolean(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  const text = String(value ?? "")
    .trim()
    .toLowerCase();
  return text === "yes" || text === "true" || text === "1";
}

function toNumber(text: string): string | number {
  if (text.trim() === "") return "";
  const number = Number(text);
  return Number.isFinite(number) ? number : text;
}

/** Long free-text options render as a textarea rather than a single line. */
function isLongTextField(field: JobField): boolean {
  // Named "other arguments" options hold free-form CLI text, so they need room.
  // The old rule also promoted any field whose *help* was long, which decided
  // the widget from prose the user never saw inline; the help is a tooltip now,
  // and a long explanation does not mean a long value.
  return (
    field.variable === "other_args" ||
    field.variable === "other_motioncor2_args" ||
    field.variable === "other_aretomo_args"
  );
}

/** A short note explaining what a job writes, shown under the form. */
export function JobOutputNote({ outputs }: { outputs: string[] }) {
  if (outputs.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 t-label text-ink-muted">
      <Info className="size-3" />
      <span>writes</span>
      {outputs.map((output) => (
        <Badge key={output} tone="muted" className="font-mono t-meta">
          {output}
        </Badge>
      ))}
    </div>
  );
}

/**
 * Which controller option, if any, makes this field inactive.
 *
 * The desktop job window greys out a group of options when its controlling
 * boolean is off (`place(key, TOGGLE_DEACTIVATE, group)` followed by
 * `guientries[key].cb_menu_i()`), and the generated schema records the same
 * groups. Showing them greyed rather than hiding them keeps the form stable as
 * the user toggles the controller, and matches what a RELIM user sees.
 */
function disabledBy(
  gates: JobGate[] | undefined,
  variable: string,
  values: JobFieldValues,
): string | null {
  if (!gates) return null;
  for (const gate of gates) {
    if (!gate.fields.includes(variable)) continue;
    const live = asBoolean(values[gate.controller]);
    // active_when 'false' means the controlled group is live when the
    // controller is *off* (the C++ `actually_activate` case).
    const expected = gate.active_when !== "false";
    if (live !== expected) return gate.controller;
  }
  return null;
}
