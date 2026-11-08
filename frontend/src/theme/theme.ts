import { useCallback, useSyncExternalStore } from "react";

// The cashier workspace's colour theme. Premium Dark is the default; Light
// is the alternative. The preference is per device (localStorage) and is
// applied as <html data-theme> by an inline script in index.html before
// React renders, so the page never flashes the wrong theme. The theme only
// restyles the cashier workspace (<html data-workspace="cashier">): the
// waiter POS and the kitchen display keep their own look.
//
// index.html repeats THEME_KEY and WORKSPACE_MODE_KEY; keep them in sync
// (theme.test.ts checks).

export type Theme = "dark" | "light";

export const THEME_KEY = "restaurantos.theme";
export const DEFAULT_THEME: Theme = "dark";
// The screen remembered by App (sessionStorage); "cashier" marks the
// workspace before the first render.
export const WORKSPACE_MODE_KEY = "restaurantos.mode";

export function readTheme(): Theme {
  try {
    return window.localStorage.getItem(THEME_KEY) === "light" ? "light" : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** Applies the theme to the document; returns whether it was remembered. */
export function setTheme(theme: Theme): boolean {
  applyTheme(theme);
  let stored = true;
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch {
    stored = false;
  }
  for (const listener of listeners) listener();
  return stored;
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

/** Marks (or unmarks) the document as showing the cashier workspace. */
export function setCashierWorkspace(active: boolean): void {
  if (active) document.documentElement.dataset.workspace = "cashier";
  else delete document.documentElement.dataset.workspace;
}

const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTheme(): [Theme, (theme: Theme) => boolean] {
  const theme = useSyncExternalStore(subscribe, currentTheme, () => DEFAULT_THEME);
  const update = useCallback((next: Theme) => setTheme(next), []);
  return [theme, update];
}
