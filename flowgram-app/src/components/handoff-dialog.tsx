/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */
import { useState } from 'react';

import { Check, Copy, Terminal } from 'lucide-react';

import { getAgentBase } from '../lib/agent-base';
import { tx } from '../lib/i18n';
import { Button } from './ui/button';
import { Dialog } from './ui/dialog';

interface HandoffRow {
  label: string;
  value: string;
  note?: string;
}

/**
 * Hand this workspace to an external agent harness.
 *
 * The assistant is not part of this app any more: RELION's capabilities live in
 * the agent and are exposed over MCP at `/mcp`, so a harness (zcode, opencode,
 * Claude Code) drives them directly - with its own approvals, terminal and file
 * access. What the browser owes the harness is the *context*: which project,
 * which workspace, which nodes are drafts, and how to reach the MCP endpoint.
 *
 * Everything is shown as copyable text rather than launched, because the
 * harness runs in a terminal the browser cannot start, and the user may want to
 * read the configuration before pasting it.
 */
export function HandoffDialog({
  open,
  onOpenChange,
  projectId,
  workspaceId,
  workspaceName,
  selectedNode,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  workspaceId: string;
  workspaceName: string;
  selectedNode?: string;
}) {
  const [copied, setCopied] = useState<string | null>(null);
  const base = getAgentBase();

  const prompt = [
    `${tx('你在 RELION 项目的 agent 上工作。')}`,
    '',
    `project: ${projectId}`,
    `workspace: ${workspaceId} (${workspaceName})`,
    ...(selectedNode ? [`selected node: ${selectedNode}`] : []),
    '',
    tx('可用工具来自 RELION agent 的 MCP server：list_projects, get_pipeline, get_job, queue_job, set_job_alias, delete_job 等。写操作需要你的审批，请在执行前说明将改动什么。'),
    tx('节点的参数以 job.star 为准；草稿节点只在 workspace 中，尚未进入 pipeline。'),
  ].join('\n');

  const mcpConfig = JSON.stringify(
    {
      mcpServers: {
        relion: {
          type: 'http',
          url: `${base}/mcp`,
        },
      },
    },
    null,
    2,
  );

  const rows: HandoffRow[] = [
    {
      label: tx('MCP 端点'),
      value: `${base}/mcp`,
      note: tx('agent 需以 --enable-mcp 启动（dev 脚本已默认开启）'),
    },
    { label: tx('MCP 配置（Claude Code / Cursor 等）'), value: mcpConfig },
    {
      label: tx('opencode'),
      value: `opencode mcp add relion --url ${base}/mcp\nopencode`,
      note: tx('或写进 ~/.config/opencode/opencode.json 的 mcp 字段'),
    },
    { label: tx('上下文（粘贴为首条消息）'), value: prompt },
  ];

  const copy = async (key: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(key);
      window.setTimeout(() => setCopied(null), 1600);
    } catch {
      setCopied(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={tx('用外部 agent 接手')}>
      <p className="mb-4 text-xs leading-relaxed text-muted-foreground">
        {tx('本应用不再内置助手。RELION 的全部能力通过 MCP 暴露，zcode / opencode / Claude Code 可直接调用；审批、终端和文件访问由它们负责。')}
      </p>
      <div className="space-y-4">
        {rows.map((row) => (
          <section key={row.label}>
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="text-xs font-medium">{row.label}</span>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => void copy(row.label, row.value)}
                aria-label={tx('复制')}
              >
                {copied === row.label ? <Check /> : <Copy />}
                {copied === row.label ? tx('已复制') : tx('复制')}
              </Button>
            </div>
            <pre className="select-all whitespace-pre-wrap break-words rounded-lg border border-border bg-secondary/40 p-3 text-2xs leading-relaxed">
              {row.value}
            </pre>
            {row.note && (
              <p className="mt-1 text-3xs text-muted-foreground">{row.note}</p>
            )}
          </section>
        ))}
      </div>
      <div className="mt-5 flex items-center gap-2 text-2xs text-muted-foreground">
        <Terminal size={13} />
        {tx('在终端里运行 harness 时，选择本项目目录作为工作目录即可。')}
      </div>
    </Dialog>
  );
}
