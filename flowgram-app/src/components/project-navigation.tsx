/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

import {
  Boxes,
  Loader2,
  MessageCircle,
  MoreHorizontal,
  PanelLeftClose,
  Plus,
  Search,
  Settings2,
  Workflow as WorkflowIcon,
} from 'lucide-react';

import type { Workbench } from '../lib/workbench';
import { cn } from '../lib/utils';
import type { Project, Snapshot } from '../lib/types';
import { t, tx } from '../lib/i18n';
import logo from '../assets/flowgram-logo.svg';
import { EntitySelect } from './ui/select';
import { MenuButton } from './ui/menu-button';
import { Button } from './ui/button';

export type ProjectAction =
  | 'project'
  | 'open'
  | 'details'
  | 'rename'
  | 'delete';

export function ProjectMenu({
  projects,
  projectId,
  onSelect,
  onAction,
  compact = false,
}: {
  projects: Project[];
  projectId: string;
  onSelect: (id: string) => void;
  onAction: (action: ProjectAction, projectId?: string) => void;
  compact?: boolean;
}) {
  return (
    <EntitySelect
      label={tx('项目菜单')}
      value={projectId}
      placeholder={tx('选择项目')}
      className={cn('project-menu', compact && 'max-w-44')}
      onValueChange={onSelect}
      items={projects.map((project) => ({ value: project.id, label: project.name }))}
      actions={[
        { id: 'rename', label: tx('重命名项目') },
        { id: 'details', label: tx('查看详情') },
        { id: 'delete', label: tx('删除项目'), danger: true },
      ]}
      onAction={(action, id) => onAction(action as ProjectAction, id)}
      actionLabel={(item) => tx('项目操作：{0}', [item.label])}
      onCommand={(action) => onAction(action as ProjectAction)}
      commands={[
        { id: 'project', label: t('newProject') },
        { id: 'open', label: tx('打开项目') },
      ]}
    />
  );
}
export function ProjectSidebar({
  projectId,
  projects,
  snapshot,
  bench,
  onSelectProject,
  onProjectAction,
  onCollapse,
  onSearch,
  onNewWorkflow,
  onWorkflow,
  onRuns,
  onResources,
  onSettings,
  onArchive,
}: {
  projectId: string;
  projects: Project[];
  snapshot: Snapshot;
  bench: Workbench;
  onSelectProject: (id: string) => void;
  onProjectAction: (action: ProjectAction, projectId?: string) => void;
  onCollapse: () => void;
  onSearch: () => void;
  onNewWorkflow: () => void;
  onWorkflow: (id: string) => void;
  onRuns: () => void;
  onResources: () => void;
  onSettings: () => void;
  onArchive: (kind: 'session' | 'workflow', id: string) => void;
}) {
  return (
    <aside className="flex min-h-0 flex-1 flex-col bg-sidebar" aria-label={tx('项目导航')}>
      <header className="flex h-12 shrink-0 items-center gap-1 px-2" data-tauri-drag-region>
        <img
          src={logo}
          alt="FlowGram"
          className="h-8 w-8 shrink-0 object-contain"
          draggable={false}
        />
        <ProjectMenu
          projects={projects}
          projectId={projectId}
          onSelect={onSelectProject}
          onAction={onProjectAction}
        />
        <Button
          variant="ghost"
          size="icon"
          aria-label={tx('搜索项目内容')}
          title={tx('搜索项目内容')}
          disabled={!projectId}
          onClick={onSearch}
        >
          <Search size={15} />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={tx('收起侧栏')}
          title={tx('收起侧栏')}
          onClick={onCollapse}
        >
          <PanelLeftClose size={15} />
        </Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
        <nav className="mb-5 mt-3" aria-label={tx('项目操作')}>
          {[
            { label: tx('搜索'), icon: Search, action: onSearch },
            {
              label: tx('项目资源'),
              icon: Boxes,
              action: onResources,
              selected: bench.view.kind === 'resources',
            },
          ].map(({ label, icon: Icon, action, selected }) => (
            <button
              key={label}
              className={cn('sidebar-row', selected && projectId && 'sidebar-selected')}
              disabled={!projectId}
              aria-current={selected && projectId ? 'page' : undefined}
              onClick={action}
            >
              <Icon size={16} strokeWidth={1.6} />
              <span className="truncate">{label}</span>
            </button>
          ))}
        </nav>
        {projectId ? (
          <>
            <section aria-label={tx('工作流')} className="mb-5">
              <div className="mb-1 flex h-8 items-center justify-between pl-2.5">
                <h2 className="flex items-center gap-2.5 text-xs font-medium text-muted-foreground">
                  <WorkflowIcon size={15} strokeWidth={1.6} aria-hidden="true" />
                  {tx('工作流')}
                </h2>
                <div className="flex items-center gap-0.5">
                  <MenuButton
                    label={tx('工作流列表更多操作')}
                    commands={[{ id: 'runs', label: tx('运行记录') }]}
                    onCommand={onRuns}
                  >
                    <MoreHorizontal size={14} />
                  </MenuButton>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={t('newWorkflow')}
                    title={t('newWorkflow')}
                    onClick={onNewWorkflow}
                  >
                    <Plus size={14} />
                  </Button>
                </div>
              </div>
              {snapshot.workflows.map((workflow) => {
                const selected = bench.view.kind === 'workflow' && bench.view.id === workflow.id;
                return (
                  <div className="sidebar-object group relative" key={workflow.id}>
                    <button
                      title={workflow.name}
                      className={cn(
                        'sidebar-row sidebar-object-row pr-9',
                        selected && 'sidebar-selected'
                      )}
                      aria-current={selected ? 'page' : undefined}
                      onClick={() => onWorkflow(workflow.id)}
                    >
                      <span className="truncate">{workflow.name}</span>
                    </button>
                    <div className="sidebar-object-menu absolute right-0 top-0">
                      <MenuButton
                        label={tx('工作流操作：{0}', [workflow.name])}
                        commands={[{ id: 'archive', label: tx('归档工作流') }]}
                        onCommand={() => onArchive('workflow', workflow.id)}
                      >
                        <MoreHorizontal size={14} />
                      </MenuButton>
                    </div>
                  </div>
                );
              })}
              {!snapshot.workflows.length && (
                <p className="py-2 pl-[35px] pr-2 text-xs text-muted-foreground">
                  {t('emptyWorkflows')}
                </p>
              )}
            </section>
          </>
        ) : (
          <p className="px-2 py-3 text-xs leading-6 text-muted-foreground">
            {tx('从项目菜单创建或打开工程。')}
          </p>
        )}
      </div>
      <footer className="shrink-0 px-2 py-3">
        <button className="sidebar-row" onClick={onSettings}>
          <Settings2 size={16} strokeWidth={1.6} />
          {t('settings')}
        </button>
      </footer>
    </aside>
  );
}
