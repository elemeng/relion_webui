/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

/**
 * The per-project workbench: which document is open.
 *
 * This used to be FlowGram's session model - conversation workspaces, turns,
 * activity routing, several documents per session. With the assistant moved out
 * of the app (see flowgram/HARNESS.md) there is no session to key any of that
 * on: a project has one open document, and that is the whole state.
 */

export type WorkspaceDocument = { kind: 'workflow' | 'run'; id: string };

export type DocumentView =
  | { kind: 'workflows' | 'runs' }
  | WorkspaceDocument;

export interface Workbench {
  view: DocumentView;
}

export const emptyWorkbench = (): Workbench => ({ view: { kind: 'workflows' } });

export type WorkbenchAction =
  | { type: 'workflow'; id: string }
  | { type: 'view'; view: DocumentView }
  | { type: 'close-document'; document: WorkspaceDocument }
  | { type: 'remove-object'; kind: 'workflow'; id: string };

export function sameDocument(a: WorkspaceDocument, b: WorkspaceDocument) {
  return a.kind === b.kind && a.id === b.id;
}

export function workbenchAction(state: Workbench, action: WorkbenchAction): Workbench {
  switch (action.type) {
    case 'workflow':
      return { ...state, view: { kind: 'workflow', id: action.id } };
    case 'view':
      return { ...state, view: action.view };
    case 'close-document':
      return sameDocument(state.view as WorkspaceDocument, action.document)
        ? { ...state, view: { kind: 'workflows' } }
        : state;
    case 'remove-object':
      return state.view.kind === 'workflow' && state.view.id === action.id
        ? { ...state, view: { kind: 'workflows' } }
        : state;
    default:
      return state;
  }
}
