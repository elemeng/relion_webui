/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

/**
 * Where the RELION agent listens.
 *
 * The page can call `/fg/api` relatively, so the browser has no notion of an
 * agent host; an external harness needs a concrete URL for MCP. In production
 * the agent serves this build, so its own origin is the answer; in development
 * Vite proxies `/mcp`, which amounts to the same thing. `VITE_AGENT_BASE` wins
 * when set, for a split deployment.
 */
export function getAgentBase(): string {
  const configured = import.meta.env.VITE_AGENT_BASE as string | undefined;
  if (configured) return configured.replace(/\/$/, '');
  if (typeof window !== 'undefined') return window.location.origin;
  return 'http://127.0.0.1:8765';
}
