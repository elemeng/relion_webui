# Vendored from FlowGram desktop

`flowgram-app/` is a copy of FlowGram's desktop front end, made standalone so it
can be edited and built independently of the upstream monorepo.

## Source

| | |
|---|---|
| Repository | https://github.com/bytedance/flowgram.ai |
| Path | `apps/desktop` |
| Commit | `02a01680b53facd3287ba693b588a41527c520a3` (2026-10-22) |
| Commit subject | `feat(desktop): improve authoring, trial runs and preview downloads (#1230)` |
| License | MIT (see `../LICENSE` for this repository's own terms) |

Copied: `apps/desktop/src/` (128 files, ~1.5 MB) and
`apps/desktop-backend/catalog/*.json` (node catalogue + layout geometry).

Not copied, deliberately: `src-tauri/`, `apps/desktop-backend/` (the Rust
server), `scripts/`, tests that need the monorepo.

## Why a copy rather than a dependency

`@flowgram.ai/desktop` has `"private": true` and is not published to npm. Only
the editor *library* is published:

| Package | On npm | Used by |
|---|---|---|
| `@flowgram.ai/free-layout-editor` | ✅ 1.0.16, MIT | dependency in `package.json` |
| `@flowgram.ai/desktop` | ❌ private | vendored here |
| `@flowgram.ai/desktop-backend` | ❌ Rust crate | not used at all |

So the canvas engine comes from npm with a real version and real updates; the
UI layer around it has to be vendored.

## What was changed

Everything below is the complete diff against the upstream commit above. Run
`diff -rq flowgram-app/src <flowgram.ai>/apps/desktop/src` to verify.

### 1. Tauri removed

The app no longer runs inside a Tauri shell. The four modules imported in
shell-only branches (`@tauri-apps/plugin-dialog`, `plugin-opener`, `api/core`,
`api/window`) were first replaced by local stubs; every call site has since been
replaced with a real browser-side capability, so the stubs and the module
declarations are gone too:

| Call site | Replacement |
|---|---|
| `app.tsx` "choose directory" | `components/form/path-browser.tsx` over the agent's `/api/browse` |
| `project-management.tsx` "reveal in file manager" | removed (the directory path is already copyable) |
| `project-transfer.tsx` | file removed; projects are created/opened through the agent's own endpoints |
| `lib/window-theme.ts` | file removed; the palette is applied in CSS only |

Nothing under `src/` imports a `@tauri-apps/*` specifier any more.

### 2. Backend connection

| File | Change |
|---|---|
| `src/lib/api.ts` | `connectDesktop()` is now a no-op |

Upstream asks the Tauri shell for the spawned backend's endpoint and bearer
token. In the browser there is no shell and no token: requests go to the same
origin, and the dev server proxies `/api` and `/ws` to the RELION agent.

### 3. Three small UI-adjacent changes for RELION

These touch vendored components. They are all additive, reuse FlowGram's own
widgets and class names, and introduce no new visual language.

| File | Change | Why |
|---|---|---|
| `src/components/node-inspector/definitions.ts` | `registerNodeDefinitions()` + lookup in `definitionFor` | RELION's 33 job types are generated at runtime from `/api/job-schema`; the inspector needs to resolve their field tables. A data registry, no component. |
| `src/components/form/run-input-form.tsx` | honour `title` and `enum`/`enumNames` from the start node's schema | The stock form rendered every non-boolean field as a text box labelled with the raw key, so an execution intent could not be expressed as a dropdown. Uses the existing `Select` and `settings-select` class. |
| `src/components/workflow-panel.tsx` | include node `inputsValues` in the run request body | The run dialog collects only the intent; each job's parameters live on its node. Serialising them at submit is one loop. |

### 4. Node catalogue paths

| File | Change |
|---|---|
| `src/lib/atomic-nodes.ts` | `../../../desktop-backend/catalog/…` → `../../catalog/…` |
| `src/lib/control-nodes.ts` | same |
| `src/lib/node-layout.ts` | same |
| `src/lib/i18n.test.ts` | same |

The JSON files live in `flowgram-app/catalog/`, copied from
`apps/desktop-backend/catalog/`.

### 5. Configuration

| File | Note |
|---|---|
| `package.json` | **new** — derived from upstream; `@tauri-apps/*` dropped, `workspace:*` versioned to `1.0.16`, build scripts reduced to `dev`/`build`/`ts-check` |
| `vite.config.mts` | **rewritten** — no `connection.json`, proxies to `RELION_WEB_API` (default `127.0.0.1:8765`) |
| `tsconfig.json`, `index.html` | copied unchanged |

## What was removed from upstream

The vendored copy is no longer a faithful port of the desktop app: the
assistant stack was removed on purpose (see `../flowgram/HARNESS.md`). Gone:

- conversation UI and sessions (`conversation-*`, `session-panel`,
  `mention-input`, `mentions`)
- model providers and publishing (`model-settings`, `model-picker`, `models`,
  `deployment-*`, `publication-*`, `services`)
- archive settings and project import/export (`archive-settings`,
  `project-transfer`)
- the run-inspector views the bridge does not serve (comparison, debug)
- FlowGram's multi-document session workbench (`workbench.ts` is now a
  single-document state)
- every Tauri entry point and its stubs

What remains is the canvas, the node form, the palette, run inspection and the
cluster page - driven by the RELION agent through `../bridge/`. The assistant is
an external harness over MCP; the canvas offers its configuration from the
"外部 agent" button.

## Updating from upstream

```bash
cd flowgram.ai && git fetch && git checkout <newer commit>
diff -rq ../flowgram-app/src apps/desktop/src     # see what upstream changed
```

Re-copy files that changed, then re-apply the changes above. Section 1 is now
a deletion rather than a stub: do not re-introduce a `@tauri-apps/*` import, and
do not restore the assistant surface listed under "What was removed from
upstream" - both are deliberate.
