// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "relion-web-theme";

/**
 * Light is the default; dark is remembered per browser once chosen.
 *
 * daisyUI selects a theme through `data-theme` on <html>, so that is what this
 * sets - not a `.dark` class, which nothing reads now that the palette lives in
 * daisyUI's variable names. `color-scheme` follows the attribute in index.css,
 * so native widgets (scrollbars, form controls) match too.
 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "dark" || stored === "light") return stored;
    } catch {
      // Storage can be unavailable in private modes; fall through.
    }
    return "light";
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Ignore: the choice simply will not persist.
    }
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => (current === "dark" ? "light" : "dark"));
  }, []);

  return { theme, setTheme, toggle };
}
