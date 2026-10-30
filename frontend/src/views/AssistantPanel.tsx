// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import {
  AlertCircle,
  ArrowUp,
  Check,
  ChevronDown,
  Plug,
  Settings2,
  Sparkles,
  Square,
  Trash2,
  Wrench,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import {
  type AssistantConfig,
  type PendingApproval,
  type ToolActivity,
  type TranscriptEntry,
  streamChat,
} from "@/lib/assistant";
import { cn } from "@/lib/utils";

interface AssistantPanelProps {
  open: boolean;
  onClose: () => void;
  /** The open project, so the assistant starts from it rather than asking. */
  projectDirectory: string | null;
  /** Called after a turn that changed something, so the views refresh. */
  onProjectChanged?: () => void;
}

/**
 * The assistant panel.
 *
 * A right-hand sheet, like the job form: the pipeline stays visible, because the
 * conversation is *about* the pipeline and hiding it makes the answers harder
 * to check. It streams, so a long answer appears as it is written rather than
 * after a pause that looks like a failure.
 *
 * Writes are the interesting part. A tool that changes the project does not
 * run: it parks, and this panel renders the exact call with Approve and Deny.
 * That is the whole reason the tool registry marks write tools.
 */
export function AssistantPanel({
  open,
  onClose,
  projectDirectory,
  onProjectChanged,
}: AssistantPanelProps) {
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [pending, setPending] = useState<PendingApproval[]>([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const counter = useRef(0);
  const nextId = () => {
    counter.current += 1;
    return counter.current;
  };

  const loadConfig = useCallback(async () => {
    try {
      const data = await api.get<AssistantConfig>("/api/assistant/config");
      setConfig(data);
      setError(null);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "could not reach the assistant",
      );
    }
  }, []);

  useEffect(() => {
    if (open) loadConfig();
  }, [open, loadConfig]);

  // Keep the newest message in view as it streams.
  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [entries, pending]);

  const update = useCallback(
    (id: number, change: (entry: TranscriptEntry) => TranscriptEntry) => {
      setEntries((current) =>
        current.map((entry) => (entry.id === id ? change(entry) : entry)),
      );
    },
    [],
  );

  const send = useCallback(
    async (text: string, approved: PendingApproval[] = []) => {
      if (!text.trim() || running) return;
      setRunning(true);
      setError(null);
      setPending([]);

      const userId = nextId();
      const replyId = nextId();
      setEntries((current) => [
        ...current,
        { id: userId, role: "user", text: text.trim(), calls: [] },
        {
          id: replyId,
          role: "assistant",
          text: "",
          calls: [],
          streaming: true,
        },
      ]);

      const controller = new AbortController();
      abortRef.current = controller;
      let wroteSomething = false;

      try {
        await streamChat(
          { message: text, approved, project: projectDirectory },
          (event, data) => {
            if (event === "text") {
              const piece = String(data.text ?? "");
              update(replyId, (entry) => ({
                ...entry,
                text: entry.text + piece,
              }));
              return;
            }
            if (event === "tool_start") {
              const call: ToolActivity = {
                id: nextId(),
                name: String(data.tool ?? ""),
                arguments:
                  (data.arguments as Record<string, unknown> | undefined) ??
                  null,
                result: null,
                writes: Boolean(data.writes),
              };
              update(replyId, (entry) => ({
                ...entry,
                calls: [...entry.calls, call],
              }));
              return;
            }
            if (event === "tool_result") {
              const name = String(data.tool ?? "");
              const result = data.result;
              const parked =
                result &&
                typeof result === "object" &&
                (result as Record<string, unknown>).status === "needs_approval";
              if (!parked) wroteSomething = true;
              update(replyId, (entry) => {
                // Attach the result to the newest call of that name that has
                // none yet: one turn can call the same tool twice.
                const calls = [...entry.calls];
                for (let index = calls.length - 1; index >= 0; index -= 1) {
                  if (
                    calls[index].name === name &&
                    calls[index].result === null
                  ) {
                    calls[index] = {
                      ...calls[index],
                      result,
                      pendingKey: parked
                        ? String((result as Record<string, unknown>).key ?? "")
                        : undefined,
                    };
                    break;
                  }
                }
                return { ...entry, calls };
              });
              return;
            }
            if (event === "needs_approval") {
              setPending((current) => [
                ...current,
                {
                  key: String(data.key ?? ""),
                  tool: String(data.tool ?? ""),
                  arguments: (data.arguments as Record<string, unknown>) ?? {},
                },
              ]);
              return;
            }
            if (event === "error") {
              update(replyId, (entry) => ({
                ...entry,
                error: String(data.text ?? "the assistant failed"),
              }));
              return;
            }
            if (event === "done") {
              update(replyId, (entry) => ({ ...entry, streaming: false }));
            }
          },
          controller.signal,
        );
      } catch (caught) {
        if ((caught as Error).name !== "AbortError") {
          setError(
            caught instanceof Error ? caught.message : "the request failed",
          );
        }
      } finally {
        setRunning(false);
        abortRef.current = null;
        update(replyId, (entry) => ({ ...entry, streaming: false }));
        if (wroteSomething) onProjectChanged?.();
      }
    },
    [onProjectChanged, projectDirectory, running, update],
  );

  const decide = useCallback(
    async (approval: PendingApproval, approve: boolean) => {
      setPending((current) =>
        current.filter((item) => item.key !== approval.key),
      );
      try {
        const outcome = await api.post<{
          approved: boolean;
          tool?: string;
          result?: Record<string, unknown>;
        }>("/api/assistant/approve", {
          key: approval.key,
          approve,
        });

        if (!approve) {
          setEntries((current) => [
            ...current,
            {
              id: nextId(),
              role: "assistant",
              text: `Cancelled ${approval.tool}.`,
              calls: [],
            },
          ]);
          return;
        }

        // The server ran the call and returned its result, so the transcript
        // shows what actually happened rather than asking the model to narrate
        // a call it never made.
        const result = outcome.result ?? {};
        const message =
          typeof result.message === "string"
            ? result.message
            : typeof result.error === "string"
              ? result.error
              : JSON.stringify(result);
        const failed = typeof result.error === "string";
        setEntries((current) => [
          ...current,
          {
            id: nextId(),
            role: "assistant",
            text: "",
            calls: [
              {
                id: nextId(),
                name: approval.tool,
                arguments: approval.arguments,
                result,
                writes: true,
                decision: "approved",
              },
            ],
            error: failed ? message : undefined,
          },
        ]);
        if (!failed) onProjectChanged?.();
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "could not record that",
        );
      }
    },
    [onProjectChanged],
  );

  const reset = useCallback(async () => {
    abortRef.current?.abort();
    await api.post("/api/assistant/reset").catch(() => undefined);
    setEntries([]);
    setPending([]);
    setError(null);
  }, []);

  if (!open) return null;

  const ready = Boolean(config?.configured);
  const noProject = !projectDirectory;

  return (
    <aside
      className="absolute inset-y-0 right-0 z-30 flex w-[min(28.75rem,100%)] flex-col border-l border-rule-strong bg-base-100 shadow-[-8px_0_24px_-16px_rgb(0_0_0/0.4)]"
      aria-label="Assistant"
    >
      <header className="flex shrink-0 items-center gap-2 border-b border-rule px-3 py-2">
        <Sparkles className="size-3.5 shrink-0 text-signal" />
        <span className="t-ui font-semibold">Assistant</span>
        {config && (
          <span className="truncate font-mono t-meta text-ink-faint">
            {config.provider}/{config.model}
          </span>
        )}
        {config?.mcp.enabled && (
          <span
            className={cn(
              "flex shrink-0 items-center gap-1 rounded-selector px-1.5 py-px t-meta",
              config.mcp.allow_writes
                ? "bg-base-200 text-ink-muted"
                : "bg-[var(--st-succeeded-fill)] text-[var(--st-succeeded-text)]",
            )}
            title={
              config.mcp.allow_writes
                ? `MCP server at ${config.mcp.path}, ${config.mcp.tools} tools, read and write`
                : `MCP server at ${config.mcp.path}, ${config.mcp.tools} read-only tools`
            }
          >
            <Plug className="size-2.5" />
            MCP
            {!config.mcp.allow_writes && (
              <span className="font-medium">read-only</span>
            )}
          </span>
        )}
        <span className="flex-1" />
        <button
          type="button"
          onClick={() => setShowSettings((value) => !value)}
          className="grid size-6 place-items-center rounded-selector text-ink-faint hover:bg-base-200 hover:text-base-content"
          title="Provider settings"
          aria-label="Provider settings"
          aria-expanded={showSettings}
        >
          <Settings2 className="size-3.5" />
        </button>
        <button
          type="button"
          onClick={reset}
          className="grid size-6 place-items-center rounded-selector text-ink-faint hover:bg-base-200 hover:text-base-content"
          title="New conversation"
          aria-label="New conversation"
        >
          <Trash2 className="size-3.5" />
        </button>
        <button
          type="button"
          onClick={onClose}
          className="grid size-6 place-items-center rounded-selector text-ink-faint hover:bg-base-200 hover:text-base-content"
          title="Close (Esc)"
          aria-label="Close assistant"
        >
          <X className="size-4" />
        </button>
      </header>

      {showSettings && config && (
        <Settings
          config={config}
          onSaved={(next) => {
            setConfig(next);
            setShowSettings(false);
          }}
          onClose={() => setShowSettings(false)}
        />
      )}

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {entries.length === 0 && (
          <Empty config={config} noProject={noProject} onPick={send} />
        )}
        <div className="flex flex-col gap-3">
          {entries.map((entry) => (
            <Turn key={entry.id} entry={entry} />
          ))}
        </div>

        {pending.map((approval) => (
          <ApprovalCard
            key={approval.key}
            approval={approval}
            disabled={running}
            onDecide={(approve) => decide(approval, approve)}
          />
        ))}
      </div>

      {error && (
        <div className="shrink-0 border-t border-rule px-3 py-2" role="alert">
          <p className="flex items-start gap-1.5 t-label text-st-failed">
            <AlertCircle className="mt-px size-3.5 shrink-0" />
            <span>{error}</span>
          </p>
        </div>
      )}

      <div className="shrink-0 border-t border-rule p-2">
        {!ready && config && (
          <p className="mb-1.5 px-1 t-meta leading-relaxed text-ink-muted">
            No API key for {config.provider}. Open settings to add one, or set{" "}
            <code className="font-mono">RELION_WEB_LLM_API_KEY</code> on the
            server.
          </p>
        )}
        <div className="flex items-end gap-1.5">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                send(input);
                setInput("");
              }
            }}
            rows={2}
            placeholder={
              ready
                ? `Ask about ${projectDirectory ? "this project" : "your projects"}…`
                : "Configure a provider first"
            }
            disabled={!ready || running}
            className={cn(
              "textarea textarea-sm min-h-0 flex-1 resize-none t-ui",
              "focus:outline-none",
            )}
            aria-label="Message the assistant"
          />
          {running ? (
            <Button
              size="sm"
              variant="outline"
              className="h-8 shrink-0"
              onClick={() => abortRef.current?.abort()}
              title="Stop generating"
            >
              <Square className="size-3" />
            </Button>
          ) : (
            <Button
              size="sm"
              className="h-8 shrink-0"
              disabled={!ready || !input.trim()}
              onClick={() => {
                send(input);
                setInput("");
              }}
              title="Send (Enter)"
            >
              <ArrowUp className="size-3.5" />
            </Button>
          )}
        </div>
      </div>
    </aside>
  );
}

// --------------------------------------------------------------------- pieces

function Turn({ entry }: { entry: TranscriptEntry }) {
  if (entry.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-box rounded-br-selector bg-secondary px-2.5 py-1.5 t-ui whitespace-pre-wrap">
          {entry.text}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      {entry.calls.map((call) => (
        <ToolRow key={call.id} call={call} />
      ))}
      {entry.text && (
        <div className="t-ui leading-relaxed whitespace-pre-wrap">
          {entry.text}
        </div>
      )}
      {entry.streaming && !entry.text && entry.calls.length === 0 && (
        <span className="flex items-center gap-1.5 t-label text-ink-faint">
          <Spinner size="xs" />
          thinking…
        </span>
      )}
      {entry.error && (
        <p className="flex items-start gap-1.5 rounded-field border border-[color-mix(in_srgb,var(--st-failed-text)_35%,var(--rule))] bg-[var(--st-failed-fill)] px-2 py-1.5 t-label text-[var(--st-failed-text)]">
          <AlertCircle className="mt-px size-3 shrink-0" />
          {entry.error}
        </p>
      )}
    </div>
  );
}

function ToolRow({ call }: { call: ToolActivity }) {
  const [open, setOpen] = useState(false);
  const parked = Boolean(call.pendingKey);
  const summary = useMemo(() => {
    if (!call.result || typeof call.result !== "object") return null;
    const result = call.result as Record<string, unknown>;
    if (typeof result.error === "string") return result.error;
    if (typeof result.message === "string") return result.message;
    if (result.status === "needs_approval") return "waiting for approval";
    if (Array.isArray(result.projects))
      return `${result.projects.length} projects`;
    if (Array.isArray(result.processes))
      return `${result.processes.length} jobs`;
    if (typeof result.n_tilt_series === "number") {
      return `${result.n_tilt_series} tilt-series`;
    }
    return null;
  }, [call.result]);

  return (
    <div
      className={cn(
        "overflow-hidden rounded-field border",
        parked
          ? "border-[color-mix(in_srgb,var(--st-aborted-text)_40%,var(--rule))] bg-[var(--st-aborted-fill)]"
          : "border-rule bg-base-200/60",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left"
        aria-expanded={open}
      >
        <ChevronDown
          className={cn(
            "size-3 shrink-0 text-ink-faint transition-transform",
            open && "rotate-180",
          )}
        />
        {call.writes ? (
          <Wrench className="size-3 shrink-0 text-st-aborted" />
        ) : (
          <Check className="size-3 shrink-0 text-st-succeeded" />
        )}
        <span className="shrink-0 font-mono t-meta">{call.name}</span>
        {summary && (
          <span className="min-w-0 flex-1 truncate t-meta text-ink-faint">
            {summary}
          </span>
        )}
        <span className="flex-1" />
        {call.writes && (
          <span className="shrink-0 t-meta text-st-aborted">write</span>
        )}
      </button>
      {open && (
        <div className="border-t border-rule-soft px-2 py-1.5">
          <div className="mb-1 t-meta text-ink-faint">arguments</div>
          <pre className="max-h-32 overflow-auto font-mono t-meta whitespace-pre-wrap">
            {JSON.stringify(call.arguments ?? {}, null, 2)}
          </pre>
          {call.result !== null && (
            <>
              <div className="mt-1.5 mb-1 t-meta text-ink-faint">result</div>
              <pre className="max-h-40 overflow-auto font-mono t-meta whitespace-pre-wrap">
                {JSON.stringify(call.result, null, 2)}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ApprovalCard({
  approval,
  disabled,
  onDecide,
}: {
  approval: PendingApproval;
  disabled: boolean;
  onDecide: (approve: boolean) => void;
}) {
  return (
    <div className="mt-2 overflow-hidden rounded-box border border-[color-mix(in_srgb,var(--st-aborted-text)_45%,var(--rule))]">
      <div className="border-b border-rule-soft bg-[var(--st-aborted-fill)] px-2.5 py-1.5">
        <span className="t-label font-medium text-[var(--st-aborted-text)]">
          Confirm: {approval.tool}
        </span>
      </div>
      <pre className="max-h-32 overflow-auto bg-base-100 px-2.5 py-1.5 font-mono t-meta whitespace-pre-wrap">
        {JSON.stringify(approval.arguments, null, 2)}
      </pre>
      <div className="flex items-center gap-1.5 border-t border-rule-soft px-2.5 py-1.5">
        <Button
          size="sm"
          className="h-6"
          disabled={disabled}
          onClick={() => onDecide(true)}
        >
          Approve
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-6"
          disabled={disabled}
          onClick={() => onDecide(false)}
        >
          Deny
        </Button>
        <span className="flex-1" />
        <span className="t-meta text-ink-faint">changes the project</span>
      </div>
    </div>
  );
}

function Settings({
  config,
  onSaved,
  onClose,
}: {
  config: AssistantConfig;
  onSaved: (config: AssistantConfig) => void;
  onClose: () => void;
}) {
  const [provider, setProvider] = useState(config.provider);
  const [model, setModel] = useState(config.model);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(config.base_url ?? "");
  const [allowWrites, setAllowWrites] = useState(config.writes_allowed);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const current = config.providers.find((entry) => entry.id === provider);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const next = await api.post<AssistantConfig>("/api/assistant/settings", {
        provider,
        model,
        // An empty string means "keep what is stored", which is what the field
        // being blank communicates; the server treats undefined the same way.
        api_key: apiKey || undefined,
        base_url: baseUrl || undefined,
        allow_writes: allowWrites,
      });
      onSaved(next);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not save");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="shrink-0 border-b border-rule bg-base-200/50 px-3 py-2.5">
      <div className="mb-2 flex items-center gap-2">
        <span className="t-section text-ink-faint">Provider</span>
        <span className="flex-1" />
        <button
          type="button"
          onClick={onClose}
          className="grid size-5 place-items-center rounded-selector text-ink-faint hover:bg-base-200"
          aria-label="Close settings"
        >
          <X className="size-3" />
        </button>
      </div>

      <div className="flex flex-col gap-2">
        <div className="grid grid-cols-2 gap-2">
          <div className="flex flex-col gap-1">
            <Label>Provider</Label>
            <Select
              value={provider}
              onValueChange={(value) => {
                setProvider(value);
                const next = config.providers.find(
                  (entry) => entry.id === value,
                );
                setModel(next?.default_model ?? "");
                setBaseUrl(next?.base_url ?? "");
              }}
            >
              {config.providers.map((entry) => (
                <SelectItem key={entry.id} value={entry.id}>
                  {entry.label}
                </SelectItem>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <Label>Model</Label>
            {current && current.models.length > 0 ? (
              <Select value={model} onValueChange={setModel}>
                {current.models.map((name) => (
                  <SelectItem key={name} value={name}>
                    {name}
                  </SelectItem>
                ))}
              </Select>
            ) : (
              <Input
                value={model}
                onChange={(event) => setModel(event.target.value)}
                placeholder="model name"
                className="font-mono"
              />
            )}
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <Label>API key</Label>
          <Input
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={
              config.configured
                ? "a key is configured; leave blank to keep it"
                : "paste the key"
            }
            autoComplete="off"
            className="font-mono"
          />
          <p className="t-meta leading-relaxed text-ink-faint">
            Held in the server's memory for this session only; never written to
            disk and never sent back to the browser.
          </p>
        </div>

        {current?.base_url !== null && (
          <div className="flex flex-col gap-1">
            <Label>Base URL</Label>
            <Input
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="http://127.0.0.1:8000/v1"
              className="font-mono"
            />
          </div>
        )}

        <label className="flex items-center gap-2 py-1">
          <Switch checked={allowWrites} onCheckedChange={setAllowWrites} />
          <span className="t-label">
            Allow changes to the project (each one still asks)
          </span>
        </label>

        {error && <p className="t-label text-st-failed">{error}</p>}

        <div className="flex items-center gap-1.5">
          <Button size="sm" className="h-7" disabled={busy} onClick={save}>
            {busy && <Spinner size="xs" />}
            Save
          </Button>
          <span className="flex-1" />
          <span className="t-meta text-ink-faint">
            {config.mcp.enabled
              ? `MCP on ${config.mcp.path}, ${
                  config.mcp.allow_writes ? "read and write" : "read-only"
                }`
              : "MCP disabled"}
          </span>
        </div>
      </div>
    </div>
  );
}

function Empty({
  config,
  noProject,
  onPick,
}: {
  config: AssistantConfig | null;
  noProject: boolean;
  onPick: (text: string) => void;
}) {
  const suggestions = [
    noProject
      ? "Which projects are available?"
      : "What is the state of this project?",
    "Which jobs failed, and why?",
    "What should I run next?",
  ];
  return (
    <div className="mb-3 flex flex-col gap-2">
      <p className="t-label leading-relaxed text-ink-muted">
        Ask about the pipeline, a failing job, or what to run next. It reads the
        project through the same tools an MCP client would.
      </p>
      <div className="flex flex-wrap gap-1.5">
        {suggestions.map((suggestion) => (
          <button
            key={suggestion}
            type="button"
            disabled={!config?.configured}
            onClick={() => onPick(suggestion)}
            className={cn(
              "rounded-full border border-rule px-2 py-1 t-meta text-left",
              "hover:border-rule-strong hover:bg-base-200 disabled:opacity-50",
            )}
          >
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  );
}
