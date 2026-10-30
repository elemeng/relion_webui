// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

/** Types and transport for the LLM assistant. */

export interface AssistantProvider {
  id: string;
  label: string;
  models: string[];
  default_model: string;
  base_url: string | null;
}

export interface AssistantConfig {
  enabled: boolean;
  provider: string;
  model: string;
  base_url: string | null;
  configured: boolean;
  writes_allowed: boolean;
  providers: AssistantProvider[];
  mcp: {
    enabled: boolean;
    path: string;
    error: string | null;
    /** How many tools are published; fewer when the server is read-only. */
    tools: number;
    /** False when the operator started the agent with --mcp-read-only. */
    allow_writes: boolean;
  };
}

export interface AssistantTool {
  name: string;
  description: string;
  writes: boolean;
  needs_project: boolean;
  schema: Record<string, unknown>;
}

/** One turn of the transcript, as the panel renders it. */
export interface TranscriptEntry {
  id: number;
  role: "user" | "assistant";
  text: string;
  /** Tool activity attached to an assistant turn. */
  calls: ToolActivity[];
  error?: string;
  streaming?: boolean;
}

export interface ToolActivity {
  id: number;
  name: string;
  arguments: Record<string, unknown> | null;
  result: unknown;
  writes: boolean;
  /** Set when the call parked for approval. */
  pendingKey?: string;
  decision?: "approved" | "denied";
}

export interface PendingApproval {
  key: string;
  tool: string;
  arguments: Record<string, unknown>;
}

type Handler = (event: string, data: Record<string, unknown>) => void;

/**
 * Streams one assistant turn.
 *
 * EventSource cannot POST, so this reads the fetch body directly and splits it
 * on the SSE record separator. The server sends `event:` names, which become
 * the handler's first argument, so the caller switches on the same vocabulary
 * the backend produces rather than re-deriving intent from the payload.
 */
export async function streamChat(
  body: {
    message: string;
    approved: PendingApproval[];
    project: string | null;
  },
  onEvent: Handler,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch("/api/assistant/chat", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload && typeof payload.error === "string") message = payload.error;
    } catch {
      // Not JSON; keep the status line.
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error("the server sent no stream");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Records are separated by a blank line; a partial record stays buffered
    // so a chunk boundary in the middle of `data:` cannot split an event.
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const record = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");

      let event = "message";
      const dataLines: string[] = [];
      for (const line of record.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length === 0) continue;
      try {
        onEvent(event, JSON.parse(dataLines.join("\n")));
      } catch {
        // A record that is not JSON is a protocol problem, not a turn failure;
        // dropping it keeps the transcript readable.
      }
    }
  }
}
