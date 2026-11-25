import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

// jsdom computes no layout, so the card layout of the cashier Overview is
// pinned here as a CSS contract: the declarations that keep label/value
// pairs readable in narrow cards (verified in headless Chrome at 375, 1280,
// 1440 and 1920 px). Removing one brings back the squeezed "Closed / by"
// columns and chips running into the card's padding.

const css = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

/** The declarations of the first top-level rule whose selector is exactly `selector`. */
function rule(selector: string): Record<string, string> {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  if (!match) throw new Error(`no rule for ${selector}`);
  const declarations: Record<string, string> = {};
  for (const part of match[2].replace(/\/\*[\s\S]*?\*\//g, "").split(";")) {
    const index = part.indexOf(":");
    if (index > 0) declarations[part.slice(0, index).trim()] = part.slice(index + 1).trim();
  }
  return declarations;
}

describe("cashier card layout", () => {
  it("gives cards a shrinkable column, so content never spills into the padding", () => {
    expect(rule(".cx-card")["grid-template-columns"]).toBe("minmax(0, 1fr)");
    expect(rule(".cx-card")["min-width"]).toBe("0");
  });

  it("does not offer cards too narrow for a label and a name, nor wider than a phone", () => {
    expect(rule(".cx-cards")["grid-template-columns"]).toBe("repeat(auto-fill, minmax(min(100%, 300px), 1fr))");
  });

  it("lets a value move to its own line instead of squeezing label and value into columns", () => {
    expect(rule(".cx-facts__row")["flex-wrap"]).toBe("wrap");
    const value = rule(".cx-facts dd");
    expect(value["min-width"]).toBe("0");
    expect(value["overflow-wrap"]).toBe("anywhere");
    expect(value["text-align"]).toBe("right");
  });

  it("wraps the card header and long explanatory text", () => {
    expect(rule(".cx-card__header")["flex-wrap"]).toBe("wrap");
    expect(rule(".cx-card__hint")["overflow-wrap"]).toBe("anywhere");
    expect(rule(".cx-card__text")["overflow-wrap"]).toBe("anywhere");
  });
});
