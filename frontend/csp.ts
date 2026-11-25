// The frontend's Content-Security-Policy, derived from index.html.
//
// The page runs only its own bundle plus one inline script (the theme bootstrap
// in index.html), allowed by its sha256 hash; it talks only to its own origin
// (/api over HTTP and the station WebSockets), loads nothing else and cannot be
// framed. `vite preview` serves the production build with these headers; a
// production reverse proxy must send the same (docs/security.md, which also
// shows how to print the current policy).

import { createHash } from "node:crypto";

/** The inline <script> blocks of an HTML document (not src= scripts). */
export function inlineScripts(html: string): string[] {
  return [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
}

export function scriptHash(source: string): string {
  return `'sha256-${createHash("sha256").update(source, "utf8").digest("base64")}'`;
}

export function contentSecurityPolicy(indexHtml: string): string {
  const hashes = inlineScripts(indexHtml).map(scriptHash).join(" ");
  return [
    "default-src 'self'",
    `script-src 'self' ${hashes}`.trim(),
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
  ].join("; ");
}

/** Every security header the frontend's host must send. */
export function securityHeaders(indexHtml: string): Record<string, string> {
  return {
    "Content-Security-Policy": contentSecurityPolicy(indexHtml),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
  };
}
