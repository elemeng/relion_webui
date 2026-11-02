# 外部 harness 接入

本分支**不内置 LLM 助手**。RELION 的全部能力仍在 Python agent 里，通过 MCP
暴露；画布是操作与观测界面，对话/规划/工具调用交给外部 harness
（zcode、opencode、Claude Code 等）。

## 为什么

FlowGram desktop 自带一整套会话、模型 provider、发布与部署表面（约 5300 行，
分布在 `conversation-*`、`session-panel`、`model-settings`、`deployment-*`、
`publication-*`、`archive-settings`、`project-transfer`）。它和本仓库已有的
`assistant.py` + `tools.py` 构成**两套 agent 语义**，而且有一处硬冲突：FlowGram
的工具立即执行，我们的写工具需要人工审批（`tools.py` 的 `writes=True`）。

接外部 harness 之后：

- 只有一套 agent 语义（我们自己的工具表）；
- 审批、终端、文件访问由 harness 负责，那是它擅长的；
- 画布侧少 5300 行需要跟随上游更新的代码。

## 怎么用

```bash
./scripts/dev_flowgram.sh -p /path/to/project   # agent :8765（含 /mcp）+ UI :1420
```

画布工具栏 **外部 agent** 按钮给出三样东西（都可一键复制）：

1. MCP 端点：`http://127.0.0.1:8765/mcp`；
2. 各 harness 的配置片段（`mcpServers` JSON / `opencode mcp add`）；
3. 一段上下文：项目路径、workspace id、当前选中节点、可用工具清单。

## 安全边界

- `--mcp` 与 `--no-auth` 同时出现在**非 loopback** 地址会被拒绝：MCP 客户端无人
  值守，登录是唯一屏障。`dev_flowgram.sh` 固定绑定 `127.0.0.1`。
- 只读场景用 `--mcp-read-only`：只发布读工具，任何客户端都无法改动项目。
- 写工具在 harness 侧审批；agent 侧的 `allow_writes` 仍然是最后一道闸。

## 已验证

- `opencode mcp list` 显示 `relion ✓ connected`；
- MCP 握手（`initialize`）直连 8765 与经 Vite 代理的 1420 均成功；
- 浏览器侧栏不再有会话入口，handoff 对话框正确带出项目与 workspace 上下文。

## 本分支删掉的界面

会话、模型设置、部署、发布、归档、项目导入导出。RELION 侧的等价能力仍然存在
（`tools.py` 的 24 个工具、`/mcp`、项目的增删与 pipeline 操作），只是入口从
浏览器移到了 harness。
