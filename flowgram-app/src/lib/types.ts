/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

export interface Project {
  id: string;
  name: string;
  createdAt: string;
  directory: string;
}
export interface WorkflowNode {
  id: string;
  type: string;
  meta?: { position?: { x: number; y: number } };
  data: Record<string, unknown>;
  blocks?: WorkflowNode[];
  edges?: WorkflowSchema['edges'];
}
export interface WorkflowSchema {
  nodes: WorkflowNode[];
  edges: Array<{
    sourceNodeID: string;
    targetNodeID: string;
    sourcePortID?: string;
    targetPortID?: string;
  }>;
}
export interface Workflow {
  id: string;
  projectId: string;
  name: string;
  revision: number;
  schema: WorkflowSchema;
  updatedAt: string;
}
export interface Run {
  id: string;
  workflowId: string;
  sessionId: string | null;
  status: string;
  inputs: Record<string, unknown>;
  outputs: unknown;
  error: string | null;
  revision: number;
  createdAt: string;
  testNodeId?: string | null;
}
export interface RuntimeEvent {
  sequence: number;
  kind: string;
  objectId: string;
  data: string;
  createdAt: string;
}
export interface Snapshot {
  projects: Project[];
  workflows: Workflow[];
  runs: Run[];
}
export interface ModelSettings {
  baseUrl: string;
  displayName: string;
  configured: boolean;
  hasApiKey: boolean;
}
