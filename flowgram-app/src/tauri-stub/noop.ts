/**
 * Stand-in for the Tauri modules in the browser build.
 *
 * Every call site guards on `__TAURI_INTERNALS__` or checks the resolved value,
 * so these are never executed here. They exist so Vite's static import analysis
 * can resolve the specifier - a `.catch()` on a dynamic import does not satisfy
 * the resolver, it only handles a runtime failure.
 */
export function invoke<T>(): Promise<T> {
  return Promise.reject(new Error('Tauri is unavailable in the browser build'));
}

export function getCurrentWindow() {
  return {
    setTheme: async () => {},
    setBackgroundColor: async () => {},
  };
}

export const open = async () => null;
export const save = async () => null;
export const revealItemInDir = async () => {};
