import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { applyTheme, currentTheme, DEFAULT_THEME, readTheme, setCashierWorkspace, setTheme, THEME_KEY, WORKSPACE_MODE_KEY } from "./theme";

const root = document.documentElement;

afterEach(() => {
  window.localStorage.clear();
  delete root.dataset.theme;
  delete root.dataset.workspace;
});

/** Runs the inline script of index.html, as the browser does before React. */
function runIndexScript() {
  const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
  const match = /<script>([\s\S]*?)<\/script>/.exec(html);
  if (!match) throw new Error("index.html has no inline theme script");
  new Function(match[1])();
  return match[1];
}

describe("theme preference", () => {
  it("defaults to Premium Dark", () => {
    expect(DEFAULT_THEME).toBe("dark");
    expect(readTheme()).toBe("dark");
    window.localStorage.setItem(THEME_KEY, "neon");
    expect(readTheme()).toBe("dark");
  });

  it("remembers and applies the choice", () => {
    expect(setTheme("light")).toBe(true);
    expect(window.localStorage.getItem(THEME_KEY)).toBe("light");
    expect(root.dataset.theme).toBe("light");
    expect(currentTheme()).toBe("light");
    expect(readTheme()).toBe("light");
    setTheme("dark");
    expect(currentTheme()).toBe("dark");
  });

  it("still applies the theme when storage is blocked", () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage")!;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("blocked", "SecurityError");
      },
    });
    try {
      expect(readTheme()).toBe("dark");
      expect(setTheme("light")).toBe(false);
      expect(currentTheme()).toBe("light");
    } finally {
      Object.defineProperty(window, "localStorage", original);
    }
  });

  it("marks the cashier workspace on the document", () => {
    setCashierWorkspace(true);
    expect(root.dataset.workspace).toBe("cashier");
    setCashierWorkspace(false);
    expect(root.dataset.workspace).toBeUndefined();
  });

  it("applyTheme only touches the attribute", () => {
    applyTheme("light");
    expect(root.getAttribute("data-theme")).toBe("light");
  });
});

describe("index.html pre-render script", () => {
  it("uses the keys theme.ts owns", () => {
    const script = runIndexScript();
    expect(script).toContain(`"${THEME_KEY}"`);
    expect(script).toContain(`"${WORKSPACE_MODE_KEY}"`);
  });

  it("applies the stored theme before React renders", () => {
    runIndexScript();
    expect(root.dataset.theme).toBe("dark");
    window.localStorage.setItem(THEME_KEY, "light");
    runIndexScript();
    expect(root.dataset.theme).toBe("light");
  });

  it("marks the cashier workspace only when it is the remembered screen", () => {
    window.sessionStorage.setItem(WORKSPACE_MODE_KEY, "pos");
    runIndexScript();
    expect(root.dataset.workspace).toBeUndefined();
    window.sessionStorage.setItem(WORKSPACE_MODE_KEY, "cashier");
    runIndexScript();
    expect(root.dataset.workspace).toBe("cashier");
  });
});
