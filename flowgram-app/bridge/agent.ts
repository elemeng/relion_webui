/**
 * The one place that talks to the RELION agent.
 *
 * Both the projection (`runs.ts`) and the routes (`middleware.ts`) need it, and
 * keeping the base URL in one module means a deployment changes one env var.
 */

export const AGENT = process.env.RELION_WEB_API ?? 'http://127.0.0.1:8765';

export async function agentJson<T = unknown>(
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<T> {
  const response = await fetch(`${AGENT}${path}`, {
    method: init?.method ?? 'GET',
    ...(init?.body === undefined
      ? {}
      : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(init.body),
        }),
  });
  if (!response.ok) {
    // Carry the agent's own message through. A 400 from /api/queue names the
    // missing option, and that is the only diagnosis the user gets.
    const detail = await response.text().catch(() => '');
    let message = detail;
    try {
      const parsed = JSON.parse(detail);
      message = parsed.detail ?? parsed.error ?? detail;
    } catch {
      /* not JSON; use the raw body */
    }
    throw new Error(`${path} -> ${response.status}: ${String(message).slice(0, 200)}`);
  }
  return (await response.json()) as T;
}
