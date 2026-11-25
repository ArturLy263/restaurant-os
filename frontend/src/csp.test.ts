import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { contentSecurityPolicy, inlineScripts, scriptHash, securityHeaders } from "../csp";

const indexHtml = readFileSync(resolve(process.cwd(), "index.html"), "utf8");

describe("frontend Content-Security-Policy", () => {
  it("allows exactly the inline theme script of index.html, by hash", () => {
    const scripts = inlineScripts(indexHtml);
    expect(scripts).toHaveLength(1);
    expect(scripts[0]).toContain("restaurantos.theme");
    const policy = contentSecurityPolicy(indexHtml);
    expect(policy).toContain(`script-src 'self' ${scriptHash(scripts[0])}`);
  });

  it("follows index.html: a changed inline script changes the hash", () => {
    const changed = indexHtml.replace('var theme = "dark";', 'var theme = "light";');
    expect(contentSecurityPolicy(changed)).not.toBe(contentSecurityPolicy(indexHtml));
  });

  it("never allows inline code, eval, other origins or framing", () => {
    const policy = contentSecurityPolicy(indexHtml);
    expect(policy).not.toMatch(/unsafe-inline|unsafe-eval|\*|https?:/);
    for (const directive of [
      "default-src 'self'",
      "style-src 'self'",
      "connect-src 'self'",
      "object-src 'none'",
      "base-uri 'none'",
      "frame-ancestors 'none'",
    ]) {
      expect(policy).toContain(directive);
    }
  });

  it("is sent with the other headers by vite preview", () => {
    const headers = securityHeaders(indexHtml);
    expect(headers["Content-Security-Policy"]).toBe(contentSecurityPolicy(indexHtml));
    expect(headers["X-Content-Type-Options"]).toBe("nosniff");
    expect(headers["X-Frame-Options"]).toBe("DENY");
    expect(headers["Referrer-Policy"]).toBe("no-referrer");
  });

  it("matches the inline script the production build keeps", () => {
    let built: string;
    try {
      built = readFileSync(resolve(process.cwd(), "dist/index.html"), "utf8");
    } catch {
      return; // no build yet (npm test runs before npm run build in CI)
    }
    expect(inlineScripts(built).map(scriptHash)).toEqual(inlineScripts(indexHtml).map(scriptHash));
  });
});
