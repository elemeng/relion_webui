/**
 * Vite middleware that answers FlowGram's API from the RELION agent.
 *
 * Vite dev server only. A production build would need the same logic behind
 * nginx or in the agent; keeping it here for now means the demo has one moving
 * part instead of two.
 */
import type { Connect } from 'vite';

import { projectFromRelion, translate, workflowFromPipeline } from './translate';
import { layoutPipeline } from '../../frontend/src/lib/layout';

const AGENT = process.env.RELION_WEB_API ?? 'http://127.0.0.1:8765';

async function agentJson(path: string): Promise<unknown> {
  const response = await fetch(`${AGENT}${path}`);
  if (!response.ok) {
    throw new Error(`${path} -> ${response.status} ${response.statusText}`);
  }
  return response.json();
}

/** Read the pipeline and lay it out. Used by both bootstrap and a single read. */
async function pipelineWorkflow(projectPath: string) {
  const query = `?project_directory=${encodeURIComponent(projectPath)}`;
  const graph = (await agentJson(`/api/project/graph${query}`)) as never;
  const layout = layoutPipeline(graph);
  const project = { path: projectPath, name: projectPath.split('/').pop() ?? projectPath };
  return workflowFromPipeline(project, graph, layout);
}

export function bridge(): Connect.NextHandleFunction {
  return async (request, response, next) => {
    const url = request.url ?? '';
    const path = url.split('?')[0];
    const method = request.method ?? 'GET';
    const route = translate(method, path);
    if (!route) {
      next();
      return;
    }

    const send = (status: number, body: unknown) => {
      response.statusCode = status;
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify(body));
    };

    try {
      if (route.kind === 'bootstrap' || route.kind === 'projects') {
        const listing = (await agentJson('/api/projects')) as {
          projects: { path: string; name: string; added_at: number }[];
        };
        const projects = listing.projects
          .filter((entry) => entry.readable !== false)
          .map(projectFromRelion);

        // A workflow per open project, so the sidebar has something to show.
        const workflows = [];
        for (const project of projects) {
          try {
            workflows.push(await pipelineWorkflow(project.directory));
          } catch {
            // An unreadable project simply contributes no workflow; the
            // project row still appears so the user can see it failed.
          }
        }

        if (route.kind === 'projects') {
          send(200, projects);
        } else {
          send(200, { projects, sessions: [], workflows, runs: [] });
        }
        return;
      }

      if (route.kind === 'empty-list') {
        send(200, []);
        return;
      }

      if (route.kind === 'noop') {
        send(200, { initialized: false });
        return;
      }

      if (route.kind === 'model-settings') {
        // No model connection is configured; the UI reads this to decide
        // whether the assistant can run. An empty shape is the honest answer.
        send(200, { connections: [], entries: [], defaults: {} });
        return;
      }

      if (route.kind === 'tools') {
        // The MCP/tool inventory the settings page lists. Our agent has one, in
        // a different shape; reporting it is a later step.
        send(200, { tools: [] });
        return;
      }

      if (route.kind === 'events') {
        // A long-lived SSE stream with no events. Kept open (not ended) so the
        // client's reconnect loop stops, and so the shape matches what it
        // expects if we later forward RELION job changes through it.
        response.statusCode = 200;
        response.setHeader('Content-Type', 'text/event-stream');
        response.setHeader('Cache-Control', 'no-cache');
        response.setHeader('Connection', 'keep-alive');
        response.write(': connected\n\n');
        const keepAlive = setInterval(() => response.write(': keep-alive\n\n'), 30_000);
        request.on('close', () => clearInterval(keepAlive));
        return;
      }

      if (route.kind === 'workflow-list') {
        const listing = (await agentJson('/api/projects')) as {
          projects: { path: string; readable?: boolean }[];
        };
        const workflows = [];
        for (const entry of listing.projects) {
          if (entry.readable === false) continue;
          try {
            workflows.push(await pipelineWorkflow(entry.path));
          } catch {
            /* skip */
          }
        }
        send(200, workflows);
        return;
      }

      if (route.kind === 'workflow') {
        // `id` is `pipeline:<project path>`, assigned in workflowFromPipeline.
        const projectPath = route.id.replace(/^pipeline:/, '');
        send(200, await pipelineWorkflow(projectPath));
        return;
      }

      next();
    } catch (cause) {
      send(502, {
        error: {
          code: 'agent_unreachable',
          message: cause instanceof Error ? cause.message : String(cause),
        },
      });
    }
  };
}
