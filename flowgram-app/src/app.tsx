/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

import {
  FolderOpen,
  Loader2,
  PanelLeft,
  Plus,
  Workflow as WorkflowIcon,
  X,
} from 'lucide-react';

import {
  emptyWorkbench,
  workbenchAction,
  type Workbench,
  type WorkbenchAction,
  type WorkspaceDocument,
} from './lib/workbench';
import type { ModelSettings, Project, Snapshot, Workflow } from './lib/types';
import { ProjectScope } from './lib/project-context';
import { errorText, getSystemLocale, t, tx, useLocale } from './lib/i18n';
import { api, connectDesktop, subscribeEvents } from './lib/api';
import { registerRelionJobs } from './lib/register-relion';
import { WorkspaceHeader } from './components/workspace-header';
import { WorkflowPanel, type WorkflowPanelHandle } from './components/workflow-panel';
import { Presence, useContentTransition } from './components/ui/presence';
import { Input } from './components/ui/input';
import { Dialog } from './components/ui/dialog';
import { PathBrowser } from './components/form/path-browser';
import { Button } from './components/ui/button';
import { SettingsPage } from './components/settings-page';
import { RunPage } from './components/run-page';
import { RunList } from './components/run-inspector/run-list';
import { ResizablePanel } from './components/resizable-panel';
import { ProjectMenu, ProjectSidebar, type ProjectAction } from './components/project-navigation';
import { ProjectManagement, type ProjectManagementAction } from './components/project-management';

const empty: Snapshot = { projects: [], workflows: [], runs: [] };
function readWorkbenches(): Record<string, Workbench> {
  try {
    const workbenches = JSON.parse(localStorage.getItem('flowgram.workbench.v3') || '{}') as Record<
      string,
      Workbench
    >;
    for (const key of Object.keys(workbenches)) {
      if ((workbenches[key].view.kind as string) === 'services')
        workbenches[key].view = { kind: 'workflows' };
    }
    return workbenches;
  } catch {
    return {};
  }
}
export function App() {
  useLocale();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(
    () => localStorage.getItem('flowgram.project.v2') || ''
  );
  const [snapshots, setSnapshots] = useState<Record<string, Snapshot>>({});
  const [workbenches, setWorkbenches] = useState(readWorkbenches);
  const [ready, setReady] = useState(false);
  const [online, setOnline] = useState(false);
  const [model, setModel] = useState<ModelSettings | null>(null);
  const [settings, setSettings] = useState(false);
  useEffect(() => {
    const open = () => setSettings(true);
    window.addEventListener('flowgram:open-model-settings', open);
    return () => window.removeEventListener('flowgram:open-model-settings', open);
  }, []);
  const [sidebar, setSidebar] = useState(
    () => localStorage.getItem('flowgram.layout.sidebar.visible') !== 'false'
  );
  const [notice, setNotice] = useState<{ message: string; projectId: string | null } | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [projectManagement, setProjectManagement] = useState<{
    action: ProjectManagementAction;
    project: Project;
  } | null>(null);
  const editors = useRef(new Map<string, WorkflowPanelHandle>());
  const archiving = useRef(new Set<string>());
  const [createKind, setCreateKind] = useState<'project' | 'workflow' | 'open' | null>(null);
  const [name, setName] = useState('');
  const [directory, setDirectory] = useState('');
  // Where a project with no named directory goes; the agent knows its own home.
  const [defaultProjectRoot, setDefaultProjectRoot] = useState('');
  const [createError, setCreateError] = useState('');
  const [creating, setCreating] = useState(false);
  const project = projects.find((item) => item.id === projectId);
  const data = snapshots[projectId] || empty;
  const bench = workbenches[projectId] || emptyWorkbench();
  const view = bench.view;
  const destinationSurface = useRef<HTMLDivElement>(null);
  useContentTransition(destinationSurface, view.kind);
  // No in-app conversation surface: the assistant lives in an external harness
  // that connects over MCP (see components/handoff-dialog.tsx), so every
  // document opens in the main workbench area.
  const document: WorkspaceDocument | null = view.kind === 'workflow' || view.kind === 'run'
    ? view
    : null;
  const report = (message: string, scope: string | null = projectId || null) =>
    setNotice({ message, projectId: scope });
  function dispatch(action: WorkbenchAction, scope = projectId) {
    setWorkbenches((current) => ({
      ...current,
      [scope]: workbenchAction(current[scope] || emptyWorkbench(), action),
    }));
  }
  const refresh = useCallback(async (scope: string) => {
    const next = await api<Snapshot>(
      '/api/bootstrap',
      'GET',
      undefined,
      undefined,
      scope || undefined
    );
    setProjects(next.projects);
    if (scope) setSnapshots((current) => ({ ...current, [scope]: next }));
    if (scope)
      setWorkbenches((current) => {
        const state = current[scope];
        if (!state || state.view.kind !== 'workflow') return current;
        if (next.workflows.some((item) => item.id === (state.view as { id: string }).id))
          return current;
        return { ...current, [scope]: workbenchAction(state, { type: 'view', view: { kind: 'workflows' } }) };
      });
    return next;
  }, []);
  useEffect(() => {
    let disposed = false;
    void connectDesktop()
      .then(async () => {
        // Register RELION job types as node definitions before a pipeline can
        // render. The inspector is the stock component; it only needs to know
        // which fields each node type has.
        await registerRelionJobs();
        await api('/api/projects/initialize', 'POST', { locale: getSystemLocale() });
        const next = await refresh('');
        if (disposed) return;
        setProjectId((id) =>
          next.projects.some((item) => item.id === id) ? id : next.projects[0]?.id || ''
        );
        const configured = await api<ModelSettings>('/api/settings/model');
        const config = await api<{ default_project_root?: string }>('/api/config');
        if (!disposed) {
          setModel(configured);
          setDefaultProjectRoot(config.default_project_root ?? '');
          setReady(true);
          setOnline(true);
        }
      })
      .catch((cause) => {
        if (!disposed) setNotice({ message: cause.message, projectId: null });
      });
    return () => {
      disposed = true;
    };
  }, [refresh]);
  useEffect(() => {
    localStorage.setItem('flowgram.project.v2', projectId);
    if (!ready || !projectId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    void refresh(projectId).catch((cause) => {
      if (!controller.signal.aborted) setNotice({ message: cause.message, projectId });
    });
    void subscribeEvents(
      controller.signal,
      (event) => {
        if (event.kind === 'project_deleted') {
          forgetProjectDrafts(projectId);
          setSnapshots((current) => {
            const next = { ...current };
            delete next[projectId];
            return next;
          });
          setWorkbenches((current) => {
            const next = { ...current };
            delete next[projectId];
            return next;
          });
          void refresh('')
            .then((next) =>
              setProjectId((current) =>
                next.projects.some((item) => item.id === current)
                  ? current
                  : next.projects[0]?.id || ''
              )
            )
            .catch((cause) => report(cause.message, null));
          return;
        }
        if (
          [
            'object_archived',
            'object_restored',
            'object_deleted',
            'project_changed',
            'workflow_changed',
            'session_status',
            'run_started',
            'run_finished',
            'conversation_turn',
          ].includes(event.kind)
        ) {
          if (event.kind === 'object_archived' || event.kind === 'object_deleted') {
            const object = JSON.parse(event.data) as { kind: 'workflow'; id: string };
            if (event.kind === 'object_deleted')
              forgetDeletedDraft(projectId, object.kind, object.id);
            dispatch({ type: 'remove-object', kind: 'workflow', id: object.id }, projectId);
          }
          clearTimeout(timer);
          timer = setTimeout(() => {
            void refresh(projectId).catch((cause) => {
              if (!controller.signal.aborted) setNotice({ message: cause.message, projectId });
            });
          }, 120);
        }
      },
      (connected) => {
        if (!controller.signal.aborted) setOnline(connected);
      },
      undefined,
      0,
      projectId
    );
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [projectId, ready, refresh]);
  useEffect(() => {
    localStorage.setItem('flowgram.layout.sidebar.visible', String(sidebar));
  }, [sidebar]);
  useEffect(() => {
    localStorage.setItem(
      'flowgram.workbench.v3',
      JSON.stringify(
        Object.fromEntries(
          Object.entries(workbenches).map(([id, value]) => [
            id,
            { ...value, referenceIntent: undefined },
          ])
        )
      )
    );
  }, [workbenches]);
  const commands = useRef({ newSession: () => {}, settings: () => {}, search: () => {} });
  commands.current = {
    newSession: () => {
      // No chat any more: Cmd/Ctrl+N returns to the project's workflows.
      if (projectId) dispatch({ type: 'view', view: { kind: 'workflows' } });
    },
    settings: () => setSettings(true),
    search: () => {
      if (projectId) setSearchOpen(true);
    },
  };
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.defaultPrevented || documentIsModal()) return;
      const action =
        event.key === 'n'
          ? commands.current.newSession
          : event.key === 'k'
          ? commands.current.search
          : event.key === ','
          ? commands.current.settings
          : null;
      if (action) {
        event.preventDefault();
        action();
      }
      if (event.key === 'b') {
        event.preventDefault();
        setSidebar((value) => !value);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);
  async function selectProject(id: string) {
    if (id === projectId) return;
    try {
      await refresh(id);
      setProjectId(id);
      setNotice(null);
    } catch (cause) {
      setName('');
      setDirectory(projects.find((item) => item.id === id)?.directory || '');
      setCreateError((cause as Error).message);
      setCreateKind('open');
    }
  }
  function openCreate(kind: NonNullable<typeof createKind>) {
    setName('');
    setDirectory('');
    setCreateError('');
    setCreateKind(kind);
  }
  function projectAction(action: ProjectAction, targetId = projectId) {
    if (action === 'details' || action === 'rename' || action === 'delete') {
      const target = projects.find((item) => item.id === targetId);
      if (target) setProjectManagement({ action, project: target });
    } else openCreate(action);
  }
  // The agent's filesystem, not the browser's: the directory a project lives
  // on is the machine RELION runs on (see form/path-browser.tsx).
  const [browsingDirectory, setBrowsingDirectory] = useState(false);
  async function create() {
    const scope = projectId;
    if (creating) return;
    setCreating(true);
    setCreateError('');
    try {
      if (createKind === 'workflow') {
        const created = await api<Workflow>(
          '/api/workflows',
          'POST',
          { projectId: scope, name },
          undefined,
          scope
        );
        await refresh(scope);
        dispatch({ type: 'workflow', id: created.id }, scope);
      } else if (createKind === 'open') {
        // Registering an existing directory: the agent writes
        // default_pipeline.star if the folder is empty and unclaimed.
        const created = await api<{ path: string }>('/api/projects', 'POST', {
          path: directory.trim(),
        });
        await refresh(created.path);
        setProjectId(created.path);
        setNotice(null);
      } else {
        // A brand-new project. An empty directory means "put it where this
        // installation keeps its projects", which the agent told us at boot;
        // the name becomes the directory name there.
        const target = directory.trim()
          ? directory.trim()
          : `${defaultProjectRoot.replace(/\/$/, '')}/${name.trim()}`;
        // /api/projects/create writes the empty pipeline, then registration adds
        // it to the list - two calls because creating and listing are separate
        // steps in the agent.
        await api<{ path: string }>('/api/projects/create', 'POST', {
          path: target,
          name: name.trim() || undefined,
        });
        const created = await api<{ path: string }>('/api/projects', 'POST', {
          path: target,
        });
        await refresh(created.path);
        setProjectId(created.path);
        setNotice(null);
      }
      setCreateKind(null);
    } catch (cause) {
      setCreateError((cause as Error).message);
    } finally {
      setCreating(false);
    }
  }
  function saved(workflow: Workflow) {
    setSnapshots((current) => {
      const snapshot = current[workflow.projectId];
      if (!snapshot) return current;
      return {
        ...current,
        [workflow.projectId]: {
          ...snapshot,
          workflows: snapshot.workflows.map((item) => (item.id === workflow.id ? workflow : item)),
        },
      };
    });
  }
  async function archiveObject(kind: 'workflow', id: string, scope = projectId) {
    const key = `${scope}:${kind}:${id}`;
    if (archiving.current.has(key)) return;
    archiving.current.add(key);
    try {
      if (kind === 'workflow') await editors.current.get(`${scope}:${id}`)?.flush();
      await api(`/api/workflows/${id}/archive`, 'POST', undefined, undefined, scope);
      dispatch({ type: 'remove-object', kind, id }, scope);
      await refresh(scope);
      report(tx('已归档，可在设置中恢复。'), scope);
    } catch (cause) {
      report((cause as Error).message, scope);
    } finally {
      archiving.current.delete(key);
    }
  }
  function headerLeading(): ReactNode {
    return (
      !sidebar && (
        <>
          <Button
            variant="ghost"
            size="icon"
            aria-label={tx('打开侧栏')}
            title={tx('打开侧栏')}
            onClick={() => setSidebar(true)}
          >
            <PanelLeft />
          </Button>
          <ProjectMenu
            projects={projects}
            projectId={projectId}
            onSelect={(id) => void selectProject(id)}
            onAction={projectAction}
            compact
          />
          <span className="mx-1 h-4 w-px shrink-0 bg-border" />
        </>
      )
    );
  }
  return (
    <>
      <div
        className="flex h-dvh min-h-0 overflow-hidden bg-background text-foreground"
        inert={settings || undefined}
      >
        <ResizablePanel
          storageKey="flowgram.layout.sidebar"
          label={tx('侧栏宽度')}
          initial={248}
          min={200}
          max={340}
          reserve={640}
          hidden={!sidebar}
          className="border-r border-border"
        >
          <ProjectSidebar
            projects={projects}
            projectId={project?.id || ''}
            snapshot={data}
            bench={bench}
            onSelectProject={(id) => void selectProject(id)}
            onProjectAction={projectAction}
            onCollapse={() => setSidebar(false)}
            onSearch={() => setSearchOpen(true)}
            onNewWorkflow={() => openCreate('workflow')}
            onWorkflow={(id) => dispatch({ type: 'workflow', id })}
            onRuns={() => dispatch({ type: 'view', view: { kind: 'runs' } })}
            onSettings={() => setSettings(true)}
            onArchive={(_, id) => void archiveObject('workflow', id)}
          />
        </ResizablePanel>
        <main className="relative flex min-h-0 min-w-0 flex-1">
          {!project ? (
            <div className="flex min-w-0 flex-1 flex-col">
              <WorkspaceHeader title="FlowGram" leading={headerLeading()} />
              <div className="flex flex-1 items-center justify-center p-8">
                <div className="max-w-md">
                  <h1 className="text-2xl font-medium">{tx('从一个工作流项目开始')}</h1>
                  <p className="my-4 text-sm leading-6 text-muted-foreground">
                    {tx('把工作流、资源、会话和运行记录放在同一个工程中。')}
                  </p>
                  <div className="flex gap-3">
                    <Button onClick={() => openCreate('project')} disabled={!ready}>
                      <Plus />
                      {t('newProject')}
                    </Button>
                    <Button variant="outline" onClick={() => openCreate('open')} disabled={!ready}>
                      <FolderOpen />
                      {tx('打开项目')}
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <>
              <div
                ref={destinationSurface}
                className="workspace-workbench relative min-h-0 min-w-0 flex-1"
                data-active
                inert={undefined}
              >
                {document?.kind === 'workflow' &&
                  (() => {
                    const workflow = data.workflows.find((item) => item.id === document.id);
                    if (!workflow) return null;
                    return (
                      <div
                        key={`${projectId}:${document.id}`}
                        className="workspace-document absolute inset-0 flex min-h-0"
                        data-active
                      >
                        <ProjectScope projectId={projectId}>
                          <WorkflowPanel
                            ref={(handle) => {
                              const key = `${projectId}:${document.id}`;
                              if (handle) editors.current.set(key, handle);
                              else editors.current.delete(key);
                            }}
                            onArchive={() =>
                              void archiveObject('workflow', document.id, projectId)
                            }
                            workflow={workflow}
                            model={model}
                            onOpenSettings={() => setSettings(true)}
                            workflows={data.workflows}
                            runs={data.runs}
                            selectedRunId={null}
                            onSaved={saved}
                            onClose={() => dispatch({ type: 'close-document', document }, projectId)}
                            headerLeading={headerLeading()}
                            onInspectRun={(runId) =>
                              dispatch({ type: 'view', view: { kind: 'run', id: runId } }, projectId)
                            }
                            onRun={() => {
                              void refresh(projectId).catch((cause) =>
                                report(cause.message, projectId)
                              );
                            }}
                            error={(message) => report(message, projectId)}
                          />
                        </ProjectScope>
                      </div>
                    );
                  })()}
                {document?.kind === 'workflow' &&
                  !data.workflows.some((item) => item.id === document.id) && (
                    <div className="flex h-full flex-col">
                      <WorkspaceHeader
                        title={tx('工作流')}
                        leading={headerLeading()}
                      />
                      <p className="p-6 text-sm text-muted-foreground">
                        <Loader2 className="mr-2 inline animate-spin" size={14} />
                        {t('loading')}
                      </p>
                    </div>
                  )}
                {view.kind === 'runs' && (
                  <div className="flex h-full min-h-0 flex-col">
                    <WorkspaceHeader title={tx('运行记录')} leading={headerLeading()} />
                    <RunList
                      onOpen={(runId) =>
                        dispatch({ type: 'view', view: { kind: 'run', id: runId } }, projectId)
                      }
                    />
                  </div>
                )}
                {document?.kind === 'run' && (
                  <ProjectScope projectId={projectId}>
                    <RunPage
                      key={`${projectId}:${document.id}`}
                      id={document.id}
                      workflows={data.workflows}
                      headerLeading={headerLeading()}
                      
                      onClose={() => dispatch({ type: 'close-document', document })}
                      
                      onWorkflow={(id) => {
                        if (!data.workflows.some((item) => item.id === id))
                          report(tx('此工作流已归档或删除，请先在设置中恢复。'));
                        else dispatch({ type: 'workflow', id });
                      }}
                    />
                  </ProjectScope>
                )}
              </div>
            </>
          )}
        </main>
      </div>
      {ready && (
        <Presence open={settings} keepMounted className="settings-presence fixed inset-0 z-40">
          <SettingsPage
            visible={settings}
            close={() => setSettings(false)}
            model={model}
            saved={setModel}
            online={online}
          />
        </Presence>
      )}
      {notice && (notice.projectId === null || notice.projectId === projectId) && !settings && (
        <div role="alert" className="app-notice">
          <span className="min-w-0 text-sm">{errorText(notice.message)}</span>
          <Button
            size="icon"
            variant="ghost"
            aria-label={tx('关闭提示')}
            onClick={() => setNotice(null)}
          >
            <X />
          </Button>
        </div>
      )}
      {projectManagement && (
        <ProjectManagement
          key={`${projectManagement.action}:${projectManagement.project.id}`}
          action={projectManagement.action}
          project={projectManagement.project}
          close={() => setProjectManagement(null)}
          renamed={(updated) => {
            setProjects((current) =>
              current.map((item) => (item.id === updated.id ? updated : item))
            );
            setProjectManagement(null);
          }}
          deleted={async (id) => {
            await api(`/api/projects?path=${encodeURIComponent(id)}`, 'DELETE');
            setProjects((current) => current.filter((item) => item.id !== id));
            if (projectId === id) setProjectId('');
            setProjectManagement(null);
            await refresh('').catch((cause) => report(cause.message, null));
          }}
        />
      )}
      <Dialog
        open={!!createKind}
        onOpenChange={(value) => {
          if (!value && !creating) setCreateKind(null);
        }}
        title={
          createKind === 'workflow'
            ? t('newWorkflow')
            : createKind === 'open'
            ? tx('打开项目')
            : t('newProject')
        }
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
          className="space-y-4"
        >
          {createKind !== 'open' && (
            <label className="grid gap-2 text-xs">
              {createKind === 'workflow' ? t('workflowName') : t('projectName')}
              <Input
                autoFocus
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
          )}
          {createKind !== 'workflow' && (
            <label className="grid gap-2 text-xs">
              {tx('工程目录')}
              <div className="flex gap-2">
                <Input
                  autoFocus={createKind === 'open'}
                  required={createKind === 'open'}
                  value={directory}
                  onChange={(event) => setDirectory(event.target.value)}
                  placeholder={
                    createKind === 'open'
                      ? tx('选择包含工程清单的目录')
                      : tx('留空使用应用管理的位置')
                  }
                />
                <Button
                  type="button"
                  variant="outline"
                  aria-label={tx('选择目录')}
                  onClick={() => setBrowsingDirectory(true)}
                >
                  <FolderOpen />
                </Button>
              </div>
            </label>
          )}
          {createError && (
            <p role="alert" className="text-sm leading-6 text-destructive">
              {errorText(createError)}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={creating}
              onClick={() => setCreateKind(null)}
            >
              {t('cancel')}
            </Button>
            <Button
              disabled={
                creating ||
                (createKind === 'open'
                  ? !directory.trim()
                  : !name.trim() || (!directory.trim() && !defaultProjectRoot))
              }
            >
              {creating
                ? t('saving')
                : createKind === 'open'
                ? createError
                  ? tx('重试打开')
                  : t('open')
                : t('create')}
            </Button>
          </div>
        </form>
        <PathBrowser
          open={browsingDirectory}
          onOpenChange={setBrowsingDirectory}
          onPick={(picked) => setDirectory(picked)}
          value={directory}
          filter="directory"
          title={tx('选择项目目录')}
        />
      </Dialog>
      <Dialog open={searchOpen} onOpenChange={setSearchOpen} title={tx('搜索项目内容')}>
        <Input
          autoFocus
          aria-label={tx('搜索项目内容')}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <div className="mt-4 space-y-1">
          {data.workflows
            .filter((item) => item.name.toLowerCase().includes(search.toLowerCase()))
            .map((item) => (
              <button
                key={item.id}
                className="sidebar-row"
                onClick={() => {
                  dispatch({ type: 'workflow', id: item.id });
                  setSearchOpen(false);
                }}
              >
                <WorkflowIcon size={15} />
                {item.name}
              </button>
            ))}
        </div>
      </Dialog>
    </>
  );
}
function documentIsModal() {
  return !!window.document.querySelector('[role="dialog"],[role="menu"],[role="listbox"]');
}
function forgetDeletedDraft(projectId: string, kind: 'session' | 'workflow', id: string) {
  if (kind === 'workflow') {
    sessionStorage.removeItem(`flowgram.draft.${projectId}.${id}`);
    sessionStorage.removeItem(`flowgram.node-editors.${projectId}.${id}`);
  } else {
    const key = `flowgram.compose.v2.${projectId}`;
    let drafts: Record<string, unknown> = {};
    try {
      drafts = JSON.parse(sessionStorage.getItem(key) || '{}');
    } catch {
      /* Discard an invalid local cache. */
    }
    delete drafts[id];
    sessionStorage.setItem(key, JSON.stringify(drafts));
    sessionStorage.removeItem(`flowgram.pending.v2.${projectId}.${id}`);
    window.dispatchEvent(
      new CustomEvent('flowgram:session-deleted', { detail: { projectId, id } })
    );
  }
}

function forgetProjectDrafts(projectId: string) {
  for (const key of Object.keys(sessionStorage)) {
    if (
      key === `flowgram.compose.v2.${projectId}` ||
      [
        `flowgram.pending.v2.${projectId}.`,
        `flowgram.draft.${projectId}.`,
        `flowgram.node-editors.${projectId}.`,
      ].some((prefix) => key.startsWith(prefix))
    )
      sessionStorage.removeItem(key);
  }
}
