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

The app no longer runs inside a Tauri shell. Four modules were imported in
branches that only execute in the shell; they are now local no-op stubs so the
bundler can resolve them and the browser never executes them.

| File | Change |
|---|---|
| `src/app.tsx` | `chooseDirectory()` imports `./tauri-stub/dialog` |
| `src/components/project-management.tsx` | "reveal in file manager" imports `../tauri-stub/opener` |
| `src/components/project-transfer.tsx` | import/export dialogs import `../tauri-stub/dialog` |
| `src/lib/window-theme.ts` | native window theme imports `../tauri-stub/window` |
| `src/tauri-stub/*.ts` | **new** — no-op stand-ins |
| `src/tauri-shims.d.ts` | **new** — module declarations so `tsc` resolves the specifiers |

Why stubs and not `catch()`: Vite resolves `import('literal')` statically and
fails the build when the specifier does not exist. A `.catch()` handles a
runtime failure, not a missing module. Aliasing `@tauri-apps/*` to stubs in
`vite.config.mts` was tried first and does **not** apply to dynamic-import
specifiers here; rewriting the import paths in source does.

The browser consequence: there is no native directory/file picker. The project
creation dialog keeps its editable path field, which is the intended fallback.

### 2. Backend connection

| File | Change |
|---|---|
| `src/lib/api.ts` | `connectDesktop()` is now a no-op |

Upstream asks the Tauri shell for the spawned backend's endpoint and bearer
token. In the browser there is no shell and no token: requests go to the same
origin, and the dev server proxies `/api` and `/ws` to the RELION agent.

### 3. Node catalogue paths

| File | Change |
|---|---|
| `src/lib/atomic-nodes.ts` | `../../../desktop-backend/catalog/…` → `../../catalog/…` |
| `src/lib/control-nodes.ts` | same |
| `src/lib/node-layout.ts` | same |
| `src/lib/i18n.test.ts` | same |

The JSON files live in `flowgram-app/catalog/`, copied from
`apps/desktop-backend/catalog/`.

### 4. Configuration

| File | Note |
|---|---|
| `package.json` | **new** — derived from upstream; `@tauri-apps/*` dropped, `workspace:*` versioned to `1.0.16`, build scripts reduced to `dev`/`build`/`ts-check` |
| `vite.config.mts` | **rewritten** — no `connection.json`, proxies to `RELION_WEB_API` (default `127.0.0.1:8765`) |
| `tsconfig.json`, `index.html` | copied unchanged |

## What still does not work

The app loads and renders, but most of it calls FlowGram's own backend API,
which does not exist here:

- `GET /api/bootstrap` 404s — the app cannot list projects
- no `/api/workflows`, `/api/sessions`, `/api/runs`, `/api/settings/model`

Bridging those to the RELION agent is the next piece of work, not a defect in
this vendoring. See `CAPABILITY-MAP.md` for which FlowGram APIs map to which
RELION endpoints.

## Updating from upstream

```bash
cd flowgram.ai && git fetch && git checkout <newer commit>
diff -rq ../flowgram-app/src apps/desktop/src     # see what upstream changed
```

Re-copy files that changed, then re-apply the four changes above. They are
small and localised by design: one no-op function, four import paths, four
catalogue paths. Nothing in the canvas, node system or form renderer was
touched.
