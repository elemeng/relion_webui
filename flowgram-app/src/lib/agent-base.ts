/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

/**
 * Where the RELION agent listens.
 *
 * The bridge answers every `/api` call on the same origin, so the browser has no
 * notion of an agent host; an external harness needs a concrete URL. `VITE_AGENT_BASE`
 * wins when set (a split deployment), otherwise the page's own origin is the
 * agent - which is true in development, where Vite proxies `/api` and `/mcp`.
 */
export function getAgentBase(): string {
  const configured = import.meta.env.VITE_AGENT_BASE as string | undefined;
  if (configured) return configured.replace(/\/$/, '');
  if (typeof window !== 'undefined') return window.location.origin;
  return 'http://127.0.0.1:8765';
}
