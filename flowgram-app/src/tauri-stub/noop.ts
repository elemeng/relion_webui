/**
 * Stand-in for the Tauri modules in the browser build.
 *
 * Every call site guards on `__TAURI_INTERNALS__` or checks the resolved value,
 * so these never execute here. They exist so Vite's static import analysis can
 * resolve the specifier - a `.catch()` on a dynamic import does not satisfy the
 * resolver, it only handles a runtime failure.
 *
 * Signatures are deliberately permissive: the point is to make the module
 * resolvable, not to model Tauri's API.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

export function invoke<T>(..._args: any[]): Promise<T> {
  return Promise.reject(new Error('Tauri is unavailable in the browser build'));
}

export function getCurrentWindow() {
  return {
    setTheme: async (..._args: any[]) => {},
    setBackgroundColor: async (..._args: any[]) => {},
  };
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
export async function open(..._args: any[]): Promise<string | null> {
  return null;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
export async function save(..._args: any[]): Promise<string | null> {
  return null;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
export async function revealItemInDir(..._args: any[]): Promise<void> {}
