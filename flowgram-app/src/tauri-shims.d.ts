/**
 * Minimal declarations for the Tauri modules the vendored UI references.
 *
 * The web build has no Tauri shell, so these modules are never present at
 * runtime. The call sites all guard on `__TAURI_INTERNALS__` or `.catch()` a
 * failed import, and the branches are dead in a browser - but TypeScript still
 * has to resolve the specifiers.
 *
 * Declaring them here rather than installing `@tauri-apps/*` keeps the web
 * bundle free of a dependency that would only ever be tree-shaken away.
 */

declare module '@tauri-apps/api/core' {
  export function invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>;
}

declare module '@tauri-apps/api/window' {
  export function getCurrentWindow(): {
    setTheme(theme: string | null): Promise<void>;
    setBackgroundColor(color: string): Promise<void>;
  };
}

declare module '@tauri-apps/plugin-dialog' {
  export function open(options: {
    directory?: boolean;
    multiple?: boolean;
  }): Promise<string | string[] | null>;
  export function save(options?: {
    defaultPath?: string;
    title?: string;
    filters?: { name: string; extensions: string[] }[];
  }): Promise<string | null>;
}

declare module '@tauri-apps/plugin-opener' {
  export function revealItemInDir(path: string): Promise<void>;
}
