// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

/**
 * Thin fetch wrapper for the agent's JSON API.
 *
 * A 401 means the session expired or was revoked. Rather than letting every
 * caller render an error, it reloads the page, which lands on the login page.
 */

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

let onUnauthorized: (() => void) | null = null;

/**
 * Register what happens when a session is missing or expired.
 *
 * The app shows its login view rather than reloading, so the bundle is fetched
 * once and the page transitions in place.
 */
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...init,
  });

  if (response.status === 401) {
    onUnauthorized?.();
    throw new ApiError("login required", 401);
  }

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && typeof body.error === "string") message = body.error;
    } catch {
      // Not JSON; keep the status line.
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return null as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "PUT",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "PATCH",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
