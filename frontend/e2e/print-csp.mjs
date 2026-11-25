// Prints the frontend's security headers (from csp.ts and index.html), for a
// production reverse proxy to send with the built files. Node 22.6+.
//   node --experimental-strip-types e2e/print-csp.mjs
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const { securityHeaders } = await import(resolve(root, "csp.ts"));
for (const [name, value] of Object.entries(securityHeaders(readFileSync(resolve(root, "index.html"), "utf8")))) {
  console.log(`${name}: ${value}`);
}
