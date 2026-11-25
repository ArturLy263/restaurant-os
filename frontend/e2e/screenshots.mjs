// README screenshots of the real application, taken while it runs the
// whole demo workflow (npm run screenshots).
//
// Starts, like `npm run e2e:pos`: a disposable PostgreSQL database
// (scripts/demo_screenshots/database.py: random *_test name, migrated, the
// demo restaurant seeded with backend/seed_demo.py, dropped at the end),
// the FastAPI app with OUTBOX_WEBSOCKET_DELIVERY=true, the Vite dev server
// and headless Chrome — one browser per person. Then it signs in with the
// demo accounts and drives the UI exactly as a person would: cashier1
// opens the day and a shift, the waiter sends table 3, Kitchen and Bar
// prepare, the waiter serves, cashier1 bills, splits and takes cash and
// card, records a cash out and hands over, cashier2 receives the cash and
// closes short with an explanation, the manager closes the day.
//
// Every step waits for what the screen must show and the run fails on any
// console error, so the pictures can only show what the app really does.
// Writes PNGs to docs/screenshots/ (or SCREENSHOTS_DIR). Needs Docker's
// PostgreSQL, the backend virtualenv (../.venv or PYTHON) and Chrome.

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { createServer as createNetServer } from "node:net";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { createServer } from "vite";

import { findChrome, launchChrome } from "./chrome.mjs";

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = resolve(FRONTEND, "..");
const PYTHON = process.env.PYTHON ?? join(ROOT, ".venv/bin/python");
const OUT = process.env.SCREENSHOTS_DIR ?? join(ROOT, "docs/screenshots");
const PASSWORD = randomBytes(12).toString("hex");
const DOMAIN = "demo.restaurantos.example";
const WIDTH = 1280;
const HEIGHT = 800;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function until(label, check, timeout = 15_000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out after ${timeout} ms waiting for: ${label}`);
    await sleep(80);
  }
}

function freePort() {
  return new Promise((resolvePort, reject) => {
    const server = createNetServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolvePort(port));
    });
  });
}

function step(message) {
  console.log(`  - ${message}`);
}

// --- Disposable demo database -------------------------------------------------

async function startDatabase() {
  const child = spawn(PYTHON, [join(ROOT, "scripts/demo_screenshots/database.py")], {
    env: { ...process.env, SCREENSHOTS_PASSWORD: PASSWORD },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const lines = createInterface({ input: child.stdout });
  const info = await new Promise((res, rej) => {
    child.once("exit", (code) => rej(new Error(`database.py exited (${code}):\n${stderr.slice(-2000)}`)));
    lines.on("line", (line) => {
      const message = JSON.parse(line);
      if (message.ready) res(message.ready);
    });
  });
  return {
    info,
    async close() {
      child.stdin.end();
      await new Promise((res) => (child.exitCode !== null ? res() : child.once("exit", res)));
    },
  };
}

function startApi(databaseUrl, port) {
  let output = "";
  const child = spawn(PYTHON, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port), "--no-access-log"], {
    cwd: join(ROOT, "backend"),
    env: { ...process.env, DATABASE_URL: databaseUrl, OUTBOX_WEBSOCKET_DELIVERY: "true", OUTBOX_WORKER_POLL_INTERVAL_SECONDS: "0.2" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (chunk) => (output += chunk));
  child.stderr.on("data", (chunk) => (output += chunk));
  return {
    ready: until(
      "API health",
      async () => {
        if (child.exitCode !== null) throw new Error(`API exited:\n${output.slice(-3000)}`);
        try {
          return (await fetch(`http://127.0.0.1:${port}/health`)).ok;
        } catch {
          return false;
        }
      },
      30_000,
    ),
    async stop() {
      if (child.exitCode !== null) return;
      const exited = new Promise((res) => child.once("exit", res));
      child.kill("SIGTERM");
      const timer = setTimeout(() => child.kill("SIGKILL"), 10_000);
      await exited;
      clearTimeout(timer);
    },
  };
}

// --- One person's browser ---------------------------------------------------------

function person(browser, origin, name) {
  const ev = (js) => browser.evaluate(js);
  const p = {
    browser,
    name,
    text: () => ev("document.body.innerText"),
    has: async (t) => (await p.text()).toLowerCase().includes(t.toLowerCase()),
    waitText: (t, timeout) => until(`${name}: "${t}"`, () => p.has(t), timeout),
    /** Clicks the enabled button whose aria-label or text matches `pattern`. */
    click: (pattern, timeout) =>
      until(
        `${name}: button /${pattern}/`,
        () =>
          ev(`(() => {
            const re = new RegExp(${JSON.stringify(pattern)});
            const b = [...document.querySelectorAll("button")].find((b) => !b.disabled && (re.test(b.getAttribute("aria-label") ?? "") || re.test(b.textContent.trim())));
            if (!b) return false;
            b.click();
            return true;
          })()`),
        timeout,
      ),
    /** Types into the input or textarea whose label is exactly `label`. */
    fill: (label, value) =>
      until(`${name}: field "${label}"`, () =>
        ev(`(() => {
          const label = [...document.querySelectorAll("label")].find((l) => ((l.querySelector(".field__label") ?? l).textContent ?? "").trim() === ${JSON.stringify(label)});
          if (!label) return false;
          const el = label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector("input, textarea");
          if (!el || el.disabled) return false;
          const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          Object.getOwnPropertyDescriptor(proto, "value").set.call(el, ${JSON.stringify(value)});
          el.dispatchEvent(new Event("input", { bubbles: true }));
          return true;
        })()`),
      ),
    /** Clicks the input matching `selector` (radio buttons, checkboxes). */
    pick: (selector) =>
      until(`${name}: ${selector}`, () =>
        ev(`(() => {
          const el = document.querySelector(${JSON.stringify(selector)});
          if (!el || el.disabled) return false;
          el.click();
          return true;
        })()`),
      ),
    async login(account) {
      await browser.send("Emulation.setDeviceMetricsOverride", { width: WIDTH, height: HEIGHT, deviceScaleFactor: 2, mobile: false });
      await browser.send("Page.navigate", { url: origin });
      await p.fill("Email", `${account}@${DOMAIN}`);
      await p.fill("Password", PASSWORD);
    },
    async signIn(account) {
      await p.login(account);
      await p.click("^Sign in$");
      await until(`${name}: signed in`, async () => !(await p.has("Sign in")) || (await p.has("Sign out")), 20_000);
    },
    /** A screenshot of the viewport, or of the whole page with `full`. */
    async shot(file, { full = false } = {}) {
      await sleep(400);
      let height = HEIGHT;
      if (full) {
        height = Math.max(HEIGHT, await ev("document.documentElement.scrollHeight"));
        await browser.send("Emulation.setDeviceMetricsOverride", { width: WIDTH, height, deviceScaleFactor: 2, mobile: false });
        await sleep(300);
      }
      const { data } = await browser.send("Page.captureScreenshot", { format: "png" });
      await writeFile(join(OUT, file), Buffer.from(data, "base64"));
      if (full) await browser.send("Emulation.setDeviceMetricsOverride", { width: WIDTH, height: HEIGHT, deviceScaleFactor: 2, mobile: false });
      step(`screenshot ${file}`);
    },
  };
  return p;
}

// --- Run ----------------------------------------------------------------------

const chromePath = findChrome();
if (!chromePath) {
  console.error("Chrome not found (set CHROME_PATH)");
  process.exit(2);
}
await mkdir(OUT, { recursive: true });

console.log("Creating the disposable demo database…");
const db = await startDatabase();
console.log(`  ${db.info.database}`);
const apiPort = await freePort();
const api = startApi(db.info.url, apiPort);
const browsers = [];
let server;
let failed = null;

try {
  await api.ready;
  process.env.API_PROXY_TARGET = `http://127.0.0.1:${apiPort}`;
  const vitePort = await freePort();
  server = await createServer({
    root: FRONTEND,
    configFile: join(FRONTEND, "vite.config.ts"),
    logLevel: "silent",
    server: { host: "127.0.0.1", port: vitePort, strictPort: true },
  });
  await server.listen();
  const origin = `http://127.0.0.1:${vitePort}`;
  const open = async (name) => {
    const browser = await launchChrome(chromePath);
    browsers.push(browser);
    return person(browser, origin, name);
  };

  console.log("\nCashier opens the day and a shift");
  const cashier1 = await open("cashier1");
  await cashier1.login("cashier1");
  await cashier1.shot("01-login.png");
  await cashier1.click("^Sign in$");
  await cashier1.click("^Cash Register$");
  await cashier1.click("^Open business day$");
  await cashier1.waitText("opened.");
  await cashier1.fill("Opening cash", "150");
  await cashier1.click("^Open shift$");
  await cashier1.waitText("Shift opened.");
  await cashier1.shot("06-cashier-register.png");

  console.log("\nKitchen and bar displays");
  const kitchen = await open("kitchen");
  await kitchen.signIn("kitchen");
  await kitchen.waitText("All caught up");
  const bar = await open("bar");
  await bar.signIn("bar");
  await bar.waitText("All caught up");

  console.log("\nWaiter takes table 3");
  const waiter = await open("waiter");
  await waiter.signIn("waiter");
  await waiter.waitText("with an open order");
  await waiter.shot("02-waiter-tables.png");
  await waiter.click("^Table 3:");
  await waiter.waitText("How many guests?");
  await waiter.click("^More guests$");
  await waiter.click("^Open order$");
  await waiter.waitText("Next round");
  await waiter.click("^Mains$");
  await waiter.click("^Classic burger,");
  await waiter.waitText("Doneness");
  await waiter.click("^Medium$");
  await waiter.click("^Cheese");
  await waiter.shot("03-order-modifiers.png");
  await waiter.click("^Add ·");
  await waiter.waitText("Added 1× Classic burger");
  await waiter.click("^Pizza Margherita,");
  await waiter.waitText("Pizza extras");
  await waiter.click("^Add ·");
  await waiter.waitText("Added 1× Pizza Margherita");
  await waiter.click("^Drinks$");
  await waiter.click("^Coca-Cola,");
  await waiter.waitText("Added 1× Coca-Cola");
  await waiter.click("^More quantity of coca-cola$");
  await until("Coca-Cola quantity 2", () =>
    waiter.browser.evaluate(`[...document.querySelectorAll(".line--pending .line__title")].some((t) => /2×\\s*Coca-Cola/.test(t.textContent))`),
  );
  await waiter.shot("04-waiter-order.png");
  await waiter.click("^Send round");
  await waiter.waitText("Round sent");

  console.log("\nKitchen and bar prepare (live)");
  await kitchen.waitText("Classic burger");
  await kitchen.waitText("Pizza Margherita");
  await kitchen.shot("05-kitchen-kds.png");
  await bar.waitText("Coca-Cola");
  await bar.shot("05-bar-kds.png");
  for (const [display, items] of [
    [kitchen, ["1× Classic burger", "1× Pizza Margherita"]],
    [bar, ["2× Coca-Cola"]],
  ]) {
    for (const item of items) {
      await display.click(`^Start: ${item}$`);
      await display.click(`^Ready: ${item}$`);
      await display.waitText("Serve");
    }
  }

  console.log("\nWaiter serves the ready dishes");
  // The open order refreshes every 15 s; the Served buttons appear then.
  await waiter.click("^Mark served: 1× Classic burger", 30_000);
  await waiter.waitText("served");
  await waiter.shot("07-waiter-serve.png");
  await waiter.click("^Mark served: 1× Pizza Margherita", 20_000);
  await waiter.click("^Mark served: 2× Coca-Cola", 20_000);
  await until(
    "nothing left to serve",
    () =>
      waiter.browser.evaluate(
        `!document.querySelector('[aria-label^="Mark served"]') && document.querySelectorAll(".line--sent .prep--served").length === 3`,
      ),
    20_000,
  );

  console.log("\nCashier bills, splits and takes cash and card");
  await cashier1.click("^Orders$");
  await cashier1.waitText("Table 3");
  await cashier1.shot("08-cashier-orders.png");
  await cashier1.click("^Create bill: Table 3$");
  await cashier1.waitText("Amount due");
  await cashier1.click("^Split in 2$");
  await cashier1.waitText("Share 1 of 2");
  await cashier1.fill("Cash received (optional, for change)", "20");
  await cashier1.waitText("Change to give");
  await cashier1.shot("09-payment-split-cash.png");
  await cashier1.click("^Record cash payment");
  await cashier1.waitText("Still due");
  await cashier1.pick('input[name="payment-method"][value="CARD"]');
  await cashier1.pick('.cx-checkbox input[type="checkbox"]');
  await cashier1.fill("Terminal receipt number (optional)", "A-1029");
  await cashier1.click("^Record card payment");
  await cashier1.waitText("Paid in full");
  await cashier1.shot("10-payment-paid.png");

  console.log("\nCash out and blind handover");
  await cashier1.click("^Cash Register$");
  await cashier1.pick('input[name="movement-type"][value="CASH_OUT"]');
  await cashier1.fill("Amount", "20");
  await cashier1.fill("Reason", "Supplier paid in cash");
  await cashier1.click("^Record cash out$");
  await cashier1.waitText("Cash out of");
  await cashier1.fill("Cash counted in the drawer", "145.55");
  await cashier1.click("^Hand over shift$");
  await cashier1.waitText("This cannot be undone");
  await cashier1.browser.evaluate(`document.querySelector(".cx-confirm").scrollIntoView({ block: "center" })`);
  await cashier1.shot("11-shift-handover.png");
  await cashier1.click("^Confirm handover$");
  await cashier1.waitText("Shift handed over.");

  console.log("\nSecond cashier receives the cash and closes short");
  const cashier2 = await open("cashier2");
  await cashier2.signIn("cashier2");
  await cashier2.click("^Cash Register$");
  await cashier2.waitText("Receive the cash and open a shift");
  await cashier2.fill("Cash counted in the drawer", "145.55");
  await cashier2.click("^Open shift$");
  await cashier2.waitText("Handover from Demo Cashier One recorded");
  await cashier2.pick('input[name="close-mode"][value="close"]');
  await cashier2.fill("Cash counted in the drawer", "140");
  await cashier2.click("^Close shift$");
  await cashier2.click("^Confirm close$");
  // The count is recorded as entered (blind: nothing tells whether it
  // matched); a difference is then explained, without any amount shown.
  await cashier2.waitText("Shift closed.");
  await cashier2.waitText("explain it below");
  await cashier2.fill("Explanation", "Coins miscounted");
  await cashier2.shot("12-shift-closed.png");
  await cashier2.click("^Record explanation$");
  await cashier2.waitText("Explanation recorded.");

  console.log("\nManager closes the day");
  const manager = await open("manager");
  await manager.signIn("manager");
  await manager.click("^Cashier$");
  await manager.click("^Reports$");
  await manager.click("^Close business day$");
  await manager.click("^Confirm close day$");
  await manager.waitText("Its report is now frozen");
  await manager.waitText("frozen report");
  await manager.shot("13-manager-report.png", { full: true });

  const problems = browsers.flatMap((browser) => browser.problems);
  if (problems.length) throw new Error(`Browser problems:\n  ${problems.join("\n  ")}`);
  console.log(`\nDone: screenshots in ${OUT}`);
} catch (error) {
  failed = error;
} finally {
  await Promise.all(browsers.map((browser) => browser.close().catch(() => undefined)));
  await server?.close();
  await api.stop();
  await db.close();
}

if (failed) {
  console.error(`\nFAILED: ${failed.stack ?? failed}`);
  process.exit(1);
}
