/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */
import { useEffect, useSyncExternalStore } from 'react';

import { api } from './api';
import type { DeploymentCredential } from './deployment';

export type ModelProtocol = 'responses' | 'chat-completions' | 'anthropic';
export interface ModelParameters {
  temperature?: number | null;
  topP?: number | null;
  maxOutputTokens?: number | null;
  reasoningEffort?: string | null;
  outputSchema?: unknown;
}
export interface ModelCapabilities {
  streaming?: boolean | null;
  tools?: boolean | null;
  structuredOutput?: boolean | null;
  temperature?: boolean | null;
  reasoningEfforts: string[];
}
export interface ModelConnection {
  id: string;
  name: string;
  provider: string;
  protocol: ModelProtocol;
  baseUrl: string;
  credentialId: string | null;
  enabled: boolean;
  timeoutMs: number;
  revision: number;
  updatedAt: string;
}
export interface ModelEntry {
  id: string;
  connectionId: string;
  name: string;
  modelId: string;
  enabled: boolean;
  capabilities: ModelCapabilities;
  contextWindow: number | null;
  parameters: ModelParameters;
  revision: number;
  updatedAt: string;
}
export interface ModelSelection { modelId?: string | null; parameters?: ModelParameters }
export interface ModelTrace {
  modelId: string;
  modelName: string;
  upstreamModel: string;
  connectionName: string;
  parameters: ModelParameters;
}
export interface ModelVerification {
  streaming: boolean | null;
  tools: boolean | null;
  success: boolean;
  checkedAt: string;
  message: string;
  elapsedMs: number;
}
export interface ModelInventory {
  connections: ModelConnection[];
  models: ModelEntry[];
  credentials: DeploymentCredential[];
  defaults: { chat: string | null; workflow: string | null; revision: number };
  verifications: Record<string, ModelVerification>;
}
const empty: ModelInventory = { connections: [], models: [], credentials: [], defaults: { chat: null, workflow: null, revision: 0 }, verifications: {} };
let snapshot = empty;
let loaded = false;
let pending: Promise<ModelInventory> | null = null;
const listeners = new Set<() => void>();
export function refreshModels(fresh = false): Promise<ModelInventory> {
  if (pending) return fresh ? pending.catch(() => undefined).then(() => refreshModels()) : pending;
  pending = api<ModelInventory>('/api/settings/models').then((next) => {
    snapshot = next;
    loaded = true;
    listeners.forEach((listener) => listener());
    return next;
  }).finally(() => { pending = null; });
  return pending;
}
export function useModels() {
  const value = useSyncExternalStore((listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => snapshot);
  useEffect(() => { if (!loaded) void refreshModels().catch(() => undefined); }, []);
  return value;
}
export function modelUnavailable(catalog: ModelInventory, model: ModelEntry, project?: string, tools = false, streaming = false) {
  const connection = catalog.connections.find((c) => c.id === model.connectionId);
  if (!model.enabled || !connection?.enabled) return '已停用';
  const credential = connection.credentialId && catalog.credentials.find((c) => c.id === connection.credentialId);
  if (connection.credentialId && !credential) return '缺少凭证';
  if (project && credential && credential.projectIds !== null && !credential.projectIds.includes(project)) return '凭证未授权此项目';
  if (tools && model.capabilities.tools !== true) return '需要工具调用能力';
  if (streaming && model.capabilities.streaming !== true) return '需要流式输出能力';
  return '';
}
export function openModelSettings() {
  sessionStorage.setItem('flowgram.settings.intent', 'model');
  window.dispatchEvent(new Event('flowgram:open-model-settings'));
}
export const protocols = [
  { value: 'responses', label: 'Responses API' },
  { value: 'chat-completions', label: 'Chat Completions API' },
  { value: 'anthropic', label: 'Anthropic Messages API' },
] as const;
