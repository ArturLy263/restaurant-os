// The production build under its Content-Security-Policy (npm run e2e:csp).
//
// Builds the app, serves dist/ with `vite preview` (which sends the headers
// from csp.ts) against the mock API of e2e/mockApi.mjs, and drives headless
// Chrome through sign-in, the Kitchen queue and a live WebSocket event. Fails
// on any console error or exception, so a CSP violation (Chrome reports
// "Refused to ...") fails the run; also checks that an injected inline script
// is really blocked. No backend or database is involved.

import { createServer as createNetServer } from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { build, preview } from "vite";

import { findChrome, launchChrome } from "./chrome.mjs";
import { KITCHEN, startMockApi } from "./mockApi.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function freePort() {
  return new Promise((done, fail) => {
    const server = createNetServer();
    server.once("error", fail);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => done(port));
    });
  });
}

async function until(label, check, timeout = 10_000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for: ${label}`);
    await sleep(25);
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(`Assertion failed: ${message}`);
}

let step = 0;
const log = (message) => console.log(`  ${String(++step).padStart(2)}. ${message}`);

const executable = findChrome();
if (!executable) {
  console.error("No Chrome or Chromium found; set CHROME_PATH to run the browser test.");
  process.exit(2);
}

await build({ root, configFile: join(root, "vite.config.ts"), logLevel: "error" });
const mock = await startMockApi();
process.env.API_PROXY_TARGET = `http://127.0.0.1:${mock.port}`;
const port = await freePort();
const server = await preview({
  root,
  configFile: join(root, "vite.config.ts"),
  logLevel: "error",
  preview: { host: "127.0.0.1", port, strictPort: true },
});
const origin = `http://127.0.0.1:${port}`;
const browser = await launchChrome(executable);
let failed = null;

const text = () => browser.evaluate("document.body.innerText");
const click = (name) =>
  browser.evaluate(`(() => {
    const b = [...document.querySelectorAll("button")].find((b) => b.textContent.trim().startsWith(${JSON.stringify(name)}));
    if (!b) throw new Error("no button " + ${JSON.stringify(name)});
    b.click();
  })()`);
const fill = (label, value) =>
  browser.evaluate(`(() => {
    const field = [...document.querySelectorAll("label")].find((l) => l.textContent.includes(${JSON.stringify(label)})).querySelector("input");
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(field, ${JSON.stringify(value)});
    field.dispatchEvent(new Event("input", { bubbles: true }));
  })()`);

try {
  const response = await fetch(origin);
  const policy = response.headers.get("content-security-policy") ?? "";
  assert(policy.includes("script-src 'self' 'sha256-"), "the build is served with the CSP");
  assert(response.headers.get("x-frame-options") === "DENY", "frame protection");
  log(`vite preview serves the build with the policy: ${policy.split("; ").slice(0, 3).join("; ")}; …`);

  // Record every CSP violation in the page itself, from before any page
  // script runs (Chrome does not report them through the console API).
  await browser.send("Page.addScriptToEvaluateOnNewDocument", {
    source: `window.__cspViolations = [];
      document.addEventListener("securitypolicyviolation", (e) =>
        window.__cspViolations.push(e.violatedDirective + " " + (e.blockedURI || "inline")));`,
  });
  const violations = () => browser.evaluate("window.__cspViolations");
  await browser.send("Page.navigate", { url: origin });
  await until("the sign-in form", async () => (await text()).includes("Sign in"), 20_000);
  assert(await browser.evaluate(`document.documentElement.getAttribute("data-theme") === "dark"`), "the hashed inline theme script ran");
  log("the app boots; the inline theme script (allowed by hash) ran");

  await fill("Email", "e2e@example.com");
  await fill("Password", "e2e-password");
  await click("Sign in");
  await until("the station picker", async () => (await text()).includes("Choose a station"));
  await click("Kitchen");
  await until("the Kitchen queue", async () => (await text()).includes("Pasta"));
  const stream = await until("an authenticated stream", () =>
    mock.state.connections.find((c) => c.authMessage && !c.ended && c.closeCode === null),
  );
  stream.send({ type: "subscribed", station_id: KITCHEN.id });
  await until("Connected", async () => (await text()).includes("Connected"));
  log("sign-in, the API over /api and the station WebSocket all work under connect-src 'self'");

  await sleep(300);
  assert(browser.problems.length === 0, `no console errors:\n    ${browser.problems.join("\n    ")}`);
  const seen = await violations();
  assert(Array.isArray(seen) && seen.length === 0, `no CSP violation while using the app: ${JSON.stringify(seen)}`);
  const injected = await browser.evaluate(`(() => {
    window.__cspProbe = false;
    const s = document.createElement("script");
    s.textContent = "window.__cspProbe = true";
    document.head.appendChild(s);
    return window.__cspProbe;
  })()`);
  assert(injected === false, "an injected inline script is blocked");
  // Exactly one violation: the refusal of that probe.
  await sleep(300);
  const after = await violations();
  assert(after.length === 1 && after[0].startsWith("script-src"), `only the probe was refused: ${JSON.stringify(after)}`);
  log("an injected inline script is refused by the policy; no other violation or console error");
} catch (error) {
  failed = error;
} finally {
  await browser.close().catch(() => undefined);
  await new Promise((done) => server.httpServer.close(done));
  await mock.close?.();
}

if (failed) {
  console.error(`\nCSP browser test FAILED: ${failed.stack ?? failed}`);
  process.exit(1);
}
console.log("CSP browser test passed.");
process.exit(0);
