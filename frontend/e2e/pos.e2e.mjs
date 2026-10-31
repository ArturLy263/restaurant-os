// Waiter POS + Kitchen/Bar display acceptance on the real stack:
// headless Chrome (waiter, kitchen, bar) -> Vite dev server -> FastAPI with
// in-process outbox delivery over WebSockets -> PostgreSQL.
//
// Everything runs against a disposable database created for this run by
// scripts/pos_e2e/database.py (random name, verified before it is dropped
// at the end); no other database is read or written. Needs Docker's
// PostgreSQL (the .env DATABASE_URL server), the backend virtualenv
// (../.venv or PYTHON) and Chrome. Run: npm run e2e:pos
//
// Scenarios: A first order, B second round, C preparation progress,
// D reliability (rapid taps, lost responses, a failing round, API restart,
// redelivered events), E access rules, F viewports. ARTIFACTS=<dir> keeps
// screenshots.

import { spawn } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { randomBytes } from "node:crypto";

import { createServer } from "vite";

import { findChrome, launchChrome } from "./chrome.mjs";

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = resolve(FRONTEND, "..");
const PYTHON = process.env.PYTHON ?? join(ROOT, ".venv/bin/python");
const PASSWORD = randomBytes(12).toString("hex");
const ARTIFACTS = process.env.ARTIFACTS ?? (await mkdtemp(join(tmpdir(), "pos-e2e-")));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let scenario = "";
let step = 0;
const results = [];
const log = (message) => {
  results.push(`${scenario} ${++step}. ${message}`);
  console.log(`  ${String(step).padStart(2)}. ${message}`);
};
const section = (name) => {
  scenario = name.split(" — ")[0];
  step = 0;
  console.log(`\n${name}`);
};
function assert(condition, message) {
  if (!condition) throw new Error(`Assertion failed: ${message}`);
}
function assertEqual(actual, expected, message) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`Assertion failed: ${message}\n    expected ${e}\n    actual   ${a}`);
}

async function until(label, check, timeout = 10_000) {
  const deadline = Date.now() + timeout;
  let last;
  for (;;) {
    last = await check();
    if (last) return last;
    if (Date.now() > deadline) throw new Error(`Timed out after ${timeout} ms waiting for: ${label}`);
    await sleep(50);
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

// --- Disposable database ---------------------------------------------------

async function startDatabase() {
  const child = spawn(PYTHON, [join(ROOT, "scripts/pos_e2e/database.py")], {
    env: { ...process.env, E2E_PASSWORD: PASSWORD },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const lines = createInterface({ input: child.stdout });
  const waiting = [];
  let ready;
  const readyPromise = new Promise((res, rej) => {
    ready = { res, rej };
    child.once("exit", (code) => rej(new Error(`database.py exited (${code}):\n${stderr.slice(-2000)}`)));
  });
  lines.on("line", (line) => {
    const message = JSON.parse(line);
    if (message.ready) ready.res(message.ready);
    else waiting.shift()(message);
  });
  const info = await readyPromise;
  let chain = Promise.resolve();
  return {
    info,
    /** Runs one statement in its own transaction; returns the rows. */
    sql(query, params = {}) {
      const result = chain.then(
        () =>
          new Promise((res, rej) => {
            waiting.push((reply) => (reply.error ? rej(new Error(`${reply.error}\n    in: ${query}`)) : res(reply.rows)));
            child.stdin.write(`${JSON.stringify({ sql: query, params })}\n`);
          }),
      );
      chain = result.catch(() => {});
      return result;
    },
    async value(query, params) {
      const rows = await this.sql(query, params);
      return rows[0]?.[0];
    },
    async close() {
      child.stdin.end();
      await new Promise((res) => (child.exitCode !== null ? res() : child.once("exit", res)));
    },
  };
}

// --- API process ---------------------------------------------------------

function apiProcess(databaseUrl, port) {
  let child = null;
  let output = "";
  return {
    get output() {
      return output;
    },
    async start() {
      child = spawn(PYTHON, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port), "--no-access-log"], {
        cwd: join(ROOT, "backend"),
        env: {
          ...process.env,
          DATABASE_URL: databaseUrl,
          OUTBOX_WEBSOCKET_DELIVERY: "true",
          OUTBOX_WORKER_POLL_INTERVAL_SECONDS: "0.2",
        },
        stdio: ["ignore", "pipe", "pipe"],
      });
      child.stdout.on("data", (chunk) => (output += chunk));
      child.stderr.on("data", (chunk) => (output += chunk));
      await until(
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
      );
    },
    async stop() {
      if (!child || child.exitCode !== null) return;
      const exited = new Promise((res) => child.once("exit", res));
      child.kill("SIGTERM");
      const timer = setTimeout(() => child.kill("SIGKILL"), 10_000);
      await exited;
      clearTimeout(timer);
    },
  };
}

// --- Browser pages -------------------------------------------------------

function pageOf(browser, origin, name) {
  const ev = (js) => browser.evaluate(js);
  const p = {
    browser,
    name,
    ev,
    text: () => ev("document.body.innerText"),
    // innerText follows CSS text-transform; compare case-insensitively.
    has: async (t) => (await p.text()).toLowerCase().includes(t.toLowerCase()),
    waitText: (t, timeout) => until(`${name}: text "${t}"`, () => p.has(t), timeout),
    waitGone: (t, timeout) => until(`${name}: text "${t}" gone`, async () => !(await p.has(t)), timeout),
    /** The enabled button whose aria-label or text matches, clicked once. */
    click: (pattern, timeout) =>
      until(
        `${name}: enabled button /${pattern}/`,
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
    /** Is there a button matching `pattern`, and is it disabled? null if none. */
    buttonDisabled: (pattern) =>
      ev(`(() => {
        const re = new RegExp(${JSON.stringify(pattern)});
        const b = [...document.querySelectorAll("button")].find((b) => re.test(b.getAttribute("aria-label") ?? "") || re.test(b.textContent.trim()));
        return b ? b.disabled : null;
      })()`),
    fill: (selector, value) =>
      ev(`(() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, "value").set.call(el, ${JSON.stringify(value)});
        el.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
      })()`),
    /** Text of the order panel's lines: [{section, text, price, prep}]. */
    lines: () =>
      ev(`[...document.querySelectorAll(".line")].map((l) => ({
        sent: l.classList.contains("line--sent"),
        title: l.querySelector(".line__title")?.textContent.trim(),
        modifiers: l.querySelector(".line__modifiers")?.textContent.trim() ?? null,
        notes: l.querySelector(".line__notes")?.textContent.trim() ?? null,
        price: l.querySelector(".line__price")?.textContent.trim(),
        prep: l.querySelector(".prep")?.textContent.trim() ?? null,
      }))`),
    subtotal: () => ev(`document.querySelector(".order__total-value")?.textContent.trim()`),
    /** KDS: "status|quantity× name|modifiers|notes" for every queue item. */
    tickets: () =>
      ev(`[...document.querySelectorAll(".item")].map((i) => [
        i.closest("[data-status]")?.dataset.status ?? i.closest("section")?.getAttribute("aria-label") ?? "",
        (i.querySelector(".item__quantity")?.textContent ?? "") + " " + (i.querySelector(".item__name")?.textContent ?? ""),
        [...i.querySelectorAll(".item__modifiers li")].map((m) => m.textContent).join(","),
        i.querySelector(".item__notes")?.textContent.replace(/^Note: /, "") ?? "",
      ].join("|"))`),
    liveStatus: () => ev(`document.querySelector(".live")?.className.replace(/.*live--/, "") ?? ""`),
    /** fetch() as the signed-in user; returns {status, code, body}. */
    api: (path, { method = "GET", body, key } = {}) =>
      ev(`(async () => {
        const token = sessionStorage.getItem("restaurantos.accessToken");
        const headers = { Authorization: "Bearer " + token, Accept: "application/json" };
        if (${JSON.stringify(body !== undefined)}) headers["Content-Type"] = "application/json";
        if (${JSON.stringify(key ?? null)}) headers["Idempotency-Key"] = ${JSON.stringify(key ?? null)};
        const r = await fetch(${JSON.stringify(path)}, { method: ${JSON.stringify(method)}, headers, body: ${JSON.stringify(body === undefined ? null : JSON.stringify(body))} });
        const text = await r.text();
        let json = null;
        try { json = JSON.parse(text); } catch {}
        return { status: r.status, code: json?.error?.code ?? null, body: json };
      })()`),
    async login(email) {
      await browser.send("Page.navigate", { url: origin });
      await until(`${name}: login form`, () => ev(`document.querySelectorAll("label.field input").length === 2`), 30_000);
      await ev(`(() => {
        const set = (el, v) => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, v); el.dispatchEvent(new Event("input", { bubbles: true })); };
        const field = (label) => [...document.querySelectorAll("label.field")].find((l) => l.textContent.includes(label)).querySelector("input");
        set(field("Email"), ${JSON.stringify(email)});
        set(field("Password"), ${JSON.stringify(PASSWORD)});
        return true;
      })()`);
      await p.click("^Sign in$");
    },
    viewport: (width, height) =>
      browser.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 }),
    /** Screenshot plus layout problems at the current viewport. */
    async audit(label) {
      await sleep(300);
      const report = await ev(`(() => {
        const vw = document.documentElement.clientWidth;
        const scrollX = document.documentElement.scrollWidth - vw;
        // Elements sticking out of the viewport that are not inside a
        // horizontally scrolling or clipping container.
        const clipped = (el) => {
          for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
            const o = getComputedStyle(a).overflowX;
            if (o === "auto" || o === "scroll" || o === "hidden" || o === "clip") return true;
          }
          return false;
        };
        const offenders = [...document.body.querySelectorAll("*")]
          .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && (r.right > vw + 1 || r.left < -1) && !clipped(el); })
          .slice(0, 5)
          .map((el) => el.tagName.toLowerCase() + (el.className ? "." + String(el.className).trim().split(/\\s+/).join(".") : "") + " right=" + Math.round(el.getBoundingClientRect().right));
        // Visible enabled buttons smaller than 32 px in either direction.
        const small = [...document.querySelectorAll("button")]
          .filter((b) => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (r.height < 32 || r.width < 32); })
          .map((b) => (b.getAttribute("aria-label") || b.textContent.trim()).slice(0, 30) + " " + Math.round(b.getBoundingClientRect().width) + "x" + Math.round(b.getBoundingClientRect().height));
        return { vw, scrollX, offenders, small: [...new Set(small)] };
      })()`);
      const { data } = await browser.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(join(ARTIFACTS, `${label}.png`), Buffer.from(data, "base64"));
      return report;
    },
  };
  return p;
}

// --- Run -----------------------------------------------------------------

const chromePath = findChrome();
assert(chromePath, "Chrome not found (set CHROME_PATH)");
await mkdir(ARTIFACTS, { recursive: true });

console.log("Creating the disposable database…");
const db = await startDatabase();
const seed = db.info;
const main = seed.main;
const other = seed.other;
console.log(`  ${seed.database}`);

const apiPort = await freePort();
const api = apiProcess(seed.url, apiPort);
const browsers = [];
let server;
let failed = null;

try {
  await api.start();
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

  const [waiterB, cookB, barB, waiter2B] = await Promise.all([1, 2, 3, 4].map(() => launchChrome(chromePath)));
  browsers.push(waiterB, cookB, barB, waiter2B);
  const waiter = pageOf(waiterB, origin, "waiter");
  const cook = pageOf(cookB, origin, "kitchen");
  const barman = pageOf(barB, origin, "bar");
  const waiter2 = pageOf(waiter2B, origin, "waiter2");
  // A waiter's tablet and the stations' tablets.
  await waiter.viewport(1024, 768);
  await cook.viewport(1024, 768);
  await barman.viewport(1024, 768);
  await waiter2.viewport(1024, 768);

  const count = (query, params) => db.value(query, params).then(Number);
  const prepsOf = (orderId) =>
    db.sql(
      `select i.product_name_snapshot, s.name, p.status from preparations p
       join order_items i on i.id = p.order_item_id join stations s on s.id = p.station_id
       where i.order_id = :o order by i.product_name_snapshot, p.created_at`,
      { o: orderId },
    );
  const outboxSent = () => count(`select count(*) from outbox_events where event_type = 'order.sent'`);
  const undelivered = () => count(`select count(*) from outbox_events where published_at is null and dead_lettered_at is null`);

  section("Setup");
  await cook.login(main.users.cook);
  await cook.waitText("All caught up");
  await until("kitchen stream subscribed", async () => ["connected", "live"].includes(await cook.liveStatus()));
  await barman.login(main.users.barman);
  await barman.waitText("All caught up");
  await until("bar stream subscribed", async () => ["connected", "live"].includes(await barman.liveStatus()));
  log("Kitchen and Bar displays signed in (station staff), queues empty, live streams subscribed");
  await waiter.login(main.users.waiter);
  await waiter.waitText("Tables");
  await waiter.waitText("0 of 4 with an open order");
  assert(!(await waiter.has("Kitchen display")), "a waiter gets no KDS mode switch");
  assert(!(await waiter.has("Other E2E Tenant")), "no other tenant data");
  log("waiter signed in: 4 own tables listed, 0 occupied; the other tenant's occupied table is not listed");

  // ---------------------------------------------------------------------
  section("Scenario A — first order");
  await waiter.click("^Table 1, Terrace: Free");
  await waiter.waitText("How many guests?");
  await waiter.click("^More guests$");
  await waiter.click("^Open order$");
  await waiter.waitText("Next round");
  const orderId = await db.value(`select id from orders where table_id = :t and status = 'OPEN'`, { t: main.tables[1] });
  assert(orderId, "order created");
  assertEqual(await db.sql(`select guest_count, user_id from orders where id = :o`, { o: orderId }), [[3, main.user_ids.waiter]], "order row");
  assertEqual(await db.value(`select status from tables where id = :t`, { t: main.tables[1] }), "OCCUPIED", "table occupied");
  log(`table 1 opened for 3 guests: order ${orderId.slice(0, 8)} OPEN, table OCCUPIED`);

  await waiter.click("^Burger,");
  await waiter.waitText("Doneness");
  assert(await waiter.has("Add · €12.50"), "dialog starts at the base price");
  await waiter.click("^Add ·");
  await waiter.waitText("Doneness: Choose one");
  assertEqual(await count(`select count(*) from order_items where order_id = :o`, { o: orderId }), 0, "nothing added without the required modifier");
  await waiter.click("^Medium$");
  await waiter.click("^Cheese");
  await waiter.click("^Bacon");
  await waiter.waitText("Add · €15.65");
  assert(await waiter.buttonDisabled("^Well done$") === false, "single-choice group stays switchable");
  await waiter.click("^More quantity$");
  await waiter.waitText("Add · €31.30");
  await waiter.fill(".dialog textarea", "  no onions please  ");
  await waiter.click("^Add ·");
  await waiter.waitText("Added 2× Burger");
  log("required Doneness enforced in the dialog; Medium + Cheese (+€1.10) + Bacon (+€2.05) × 2 previewed as €31.30");

  await waiter.click("^Drinks$");
  assert(await waiter.buttonDisabled("^Mojito,") === true, "inactive product shown but not addable");
  await waiter.click("^Beer,");
  await waiter.waitText("Added 1× Beer");
  await waiter.click("^Cola,");
  await waiter.waitText("Added 1× Cola");
  await waiter.click("^More quantity of beer$");
  await until("beer ×2", async () => (await waiter.lines()).some((l) => l.title === "2× Beer" && l.price === "€8.00"));
  await until("subtotal with 2 beers", async () => (await waiter.subtotal()) === "€42.10");
  await waiter.click("^Fewer quantity of beer$");
  await until("beer ×1", async () => (await waiter.lines()).some((l) => l.title === "1× Beer" && l.price === "€4.00"));
  await waiter.click("^Remove Cola$");
  await until("cola removed", async () => !(await waiter.lines()).some((l) => l.title?.includes("Cola")));
  await until("subtotal", async () => (await waiter.subtotal()) === "€35.30");
  assertEqual(
    (await waiter.lines()).map(({ modifiers, ...line }) => line),
    [
      { sent: false, title: "2× Burger", notes: "Note: no onions please", price: "€31.30", prep: null },
      { sent: false, title: "1× Beer", notes: null, price: "€4.00", prep: null },
    ],
    "pending lines",
  );
  const burgerMods = (await waiter.lines())[0].modifiers.split(", ").sort();
  assertEqual(burgerMods, ["Bacon", "Cheese", "Medium"], "burger modifiers shown");
  assertEqual(
    await db.sql(
      `select product_name_snapshot, quantity, unit_price, total_price, status, notes from order_items where order_id = :o order by created_at`,
      { o: orderId },
    ),
    [
      ["Burger", 2, "12.50", "31.30", "ADDED", "no onions please"],
      ["Beer", 1, "4.00", "4.00", "ADDED", null],
      ["Cola", 1, "2.80", "2.80", "CANCELLED", null],
    ],
    "item rows (unit price = base, total = (base + modifiers) × qty, note trimmed)",
  );
  assertEqual(
    await db.sql(
      `select m.name_snapshot, m.price_snapshot from order_item_modifiers m join order_items i on i.id = m.order_item_id where i.order_id = :o order by m.name_snapshot`,
      { o: orderId },
    ),
    [["Bacon", "2.05"], ["Cheese", "1.10"], ["Medium", "0.00"]],
    "modifier snapshots",
  );
  await waiter.waitText("To 1 to Kitchen, 1 to Bar");
  log("Beer and Cola added; Beer 1→2→1 (€8.00, subtotal €42.10, back to €4.00); Cola removed; subtotal €35.30 — matches PostgreSQL");

  // Menu prices changing later must not change the order (snapshots).
  await db.sql(`update products set price = 99.00 where id = :p`, { p: main.products.Burger });
  await db.sql(`update modifiers set price_adjustment = 9.00, name = 'Cheddar' where id = :m`, { m: main.modifiers.Cheese });

  await waiter.click("^Send round · 2 items$");
  await waiter.waitText("Round sent: 1 to Kitchen, 1 to Bar.");
  assertEqual(await prepsOf(orderId), [["Beer", "Bar", "PENDING"], ["Burger", "Kitchen", "PENDING"]], "routing");
  assertEqual(await db.value(`select status from orders where id = :o`, { o: orderId }), "SENT", "order SENT");
  log("round 1 sent: Burger → Kitchen, Beer → Bar (PENDING); order SENT");

  await cook.waitText("no onions please");
  const kitchenA = await cook.tickets();
  assertEqual(kitchenA.length, 1, "kitchen has one item");
  assert(/2× Burger\|Bacon,Cheese,Medium\|no onions please$/.test(kitchenA[0]), `kitchen ticket: ${kitchenA[0]}`);
  await barman.waitText("Beer");
  const barA = await barman.tickets();
  assertEqual(barA.length, 1, "bar has one item");
  assert(/1× Beer\|\|$/.test(barA[0]), `bar ticket: ${barA[0]}`);
  assert(!(await barman.has("Burger")) && !(await cook.has("Beer")), "no cross-station items");
  await until("kitchen live", async () => (await cook.liveStatus()) === "live");
  await until("outbox delivered", async () => (await undelivered()) === 0);
  log("Kitchen shows only 2× Burger with Bacon, Cheese, Medium and the note; Bar shows only 1× Beer — no reload, stream Live");

  await until("waiter sent section", async () => (await waiter.lines()).filter((l) => l.sent).length === 2);
  const sentA = (await waiter.lines()).filter((l) => l.sent);
  assertEqual(sentA.map((l) => [l.title, l.price, l.prep]), [["2× Burger", "€31.30", "Waiting"], ["1× Beer", "€4.00", "Waiting"]], "waiter sent lines (old prices kept)");
  assert(await waiter.has("Nothing waiting") || await waiter.has("Tap products in the menu"), "next round empty");
  assertEqual(await waiter.subtotal(), "€35.30", "subtotal unchanged after menu price change");
  assert(!(await waiter.has("Cheddar")), "modifier name snapshot kept");
  log("waiter sees both items under Sent (Waiting), €35.30 — the later menu price/name change did not alter the order");

  // ---------------------------------------------------------------------
  section("Scenario B — second round");
  await waiter.click("^Food$");
  await waiter.click("^Fries,");
  await waiter.waitText("Added 1× Fries");
  await waiter.waitText("To 1 to Kitchen");
  await waiter.click("^Send round · 1 item$");
  await waiter.waitText("Round sent: 1 to Kitchen.");
  await until("kitchen fries", async () => (await cook.tickets()).length === 2);
  await sleep(1000);
  const kitchenB = (await cook.tickets()).map((t) => t.split("|")[1]).sort();
  assertEqual(kitchenB, ["1× Fries", "2× Burger"], "kitchen: each item once");
  assertEqual((await barman.tickets()).map((t) => t.split("|")[1]), ["1× Beer"], "bar unchanged");
  assertEqual(
    await prepsOf(orderId),
    [["Beer", "Bar", "PENDING"], ["Burger", "Kitchen", "PENDING"], ["Fries", "Kitchen", "PENDING"]],
    "three preparations, one per item",
  );
  assertEqual(
    await db.sql(`select payload->>'initial', jsonb_array_length(payload->'items') from outbox_events where event_type = 'order.sent' order by created_at`),
    [["true", 2], ["false", 1]],
    "second order.sent carries only the new item",
  );
  assertEqual(await count(`select count(*) from operational_events where event_type = 'order.sent'`), 2, "two order.sent audit events");
  await until("waiter 3 sent", async () => (await waiter.lines()).filter((l) => l.sent).length === 3);
  await waiter.waitText("€39.80");
  log("round 2: only 1× Fries sent; Kitchen shows Burger and Fries once each, Bar unchanged; 3 preparations; outbox items 2 then 1");

  // ---------------------------------------------------------------------
  section("Scenario C — preparation progress");
  await cook.click("^Start: 2× Burger$");
  await until("burger IN_PROGRESS", async () =>
    JSON.stringify(await prepsOf(orderId)) === JSON.stringify([["Beer", "Bar", "PENDING"], ["Burger", "Kitchen", "IN_PROGRESS"], ["Fries", "Kitchen", "PENDING"]]),
  );
  const t0 = Date.now();
  await until("waiter sees Preparing", async () => (await waiter.lines()).some((l) => l.title === "2× Burger" && l.prep === "Preparing"), 20_000);
  const waiterLag = Date.now() - t0;
  const progress = (await waiter.lines()).filter((l) => l.sent).map((l) => [l.title, l.prep]);
  assertEqual(progress, [["2× Burger", "Preparing"], ["1× Beer", "Waiting"], ["1× Fries", "Waiting"]], "only the burger changed");
  log(`kitchen Start → only the Burger preparation IN_PROGRESS; waiter shows Burger "Preparing" after ${waiterLag} ms, Beer/Fries still Waiting`);

  await cook.click("^Ready: 2× Burger$");
  await barman.click("^Start: 1× Beer$");
  await until("waiter sees Ready", async () => {
    const l = await waiter.lines();
    return l.some((x) => x.title === "2× Burger" && x.prep === "Ready") && l.some((x) => x.title === "1× Beer" && x.prep === "Preparing");
  }, 20_000);
  await until("kitchen board", async () => (await cook.tickets()).some((t) => /2× Burger/.test(t)) && (await cook.has("Ready")));
  log("Burger READY (kitchen) and Beer IN_PROGRESS (bar) reach the waiter as Ready / Preparing on the right lines");

  // ---------------------------------------------------------------------
  section("Scenario D — reliability");
  // Count the waiter's POST /send requests from inside the page.
  await waiter.ev(`(() => {
    if (window.__sendCount !== undefined) return true;
    window.__sendCount = 0;
    const original = window.fetch;
    window.fetch = (input, init) => {
      if (/\\/send$/.test(String(input)) && init?.method === "POST") window.__sendCount += 1;
      return original(input, init);
    };
    return true;
  })()`);
  await waiter.click("^Drinks$");
  // Five taps on a product within one task.
  await waiter.ev(`(() => {
    const b = [...document.querySelectorAll("button")].find((b) => /^Cola,/.test(b.getAttribute("aria-label") ?? ""));
    for (let i = 0; i < 5; i++) b.click();
    return true;
  })()`);
  await waiter.waitText("Added 1× Cola");
  await sleep(500);
  assertEqual(
    await count(`select count(*) from order_items where order_id = :o and product_name_snapshot = 'Cola' and status = 'ADDED'`, { o: orderId }),
    1,
    "rapid taps on a product add it once",
  );
  log("5 rapid taps on Cola → 1 Cola item in PostgreSQL");
  const sentBefore = await outboxSent();
  // Five taps within one task: React has not re-rendered in between.
  await waiter.ev(`(() => {
    const b = [...document.querySelectorAll("button")].find((b) => /^Send round/.test(b.textContent));
    for (let i = 0; i < 5; i++) b.click();
    return true;
  })()`);
  await waiter.waitText("Round sent: 1 to Bar.");
  await sleep(500);
  const sendRequests = await waiter.ev("window.__sendCount");
  assertEqual(await outboxSent(), sentBefore + 1, "one order.sent event");
  assertEqual(await count(`select count(*) from preparations p join order_items i on i.id = p.order_item_id where i.order_id = :o and i.product_name_snapshot = 'Cola' and i.status <> 'CANCELLED'`, { o: orderId }), 1, "one Cola preparation");
  assertEqual(sendRequests, 1, "rapid taps on Send round make one request");
  await until("bar cola", async () => (await barman.tickets()).filter((t) => /Cola/.test(t)).length === 1);
  log(`5 rapid taps on Send round → ${sendRequests} request, 1 order.sent event, 1 Cola preparation, 1 Bar ticket`);

  // Lost answers: the server sends the round, the browser never hears.
  // `failGetOrder` also fails the order reload that follows.
  async function sendWithLostAnswer(failGetOrder) {
    const patterns = [{ urlPattern: "*/send", requestStage: "Response" }];
    if (failGetOrder) patterns.push({ urlPattern: `*/api/v1/orders/${orderId}`, requestStage: "Request" });
    await waiterB.send("Fetch.enable", { patterns });
    let sendStatus = null;
    const off = waiterB.on("Fetch.requestPaused", (params) => {
      if (params.responseStatusCode !== undefined) sendStatus = params.responseStatusCode;
      void waiterB.send("Fetch.failRequest", { requestId: params.requestId, errorReason: "ConnectionReset" });
    });
    await waiter.click("^Send round · 1 item$");
    await until("send answered", async () => sendStatus !== null);
    return async () => {
      off();
      await waiterB.send("Fetch.disable");
      return sendStatus;
    };
  }
  const prepCount = (itemId) => count(`select count(*) from preparations where order_item_id = :i`, { i: itemId });

  await waiter.click("^Food$");
  await waiter.click("^Fries,");
  await waiter.waitText("Added 1× Fries");
  const lostOne = await db.value(`select id from order_items where order_id = :o and status = 'ADDED'`, { o: orderId });
  let restore = await sendWithLostAnswer(false);
  await waiter.waitText("Round sent: 1 to Kitchen.");
  assertEqual(await restore(), 200, "the server had sent the round");
  assert(!(await waiter.has("unknown whether")), "no 'unknown outcome' message once the order shows the round");
  assertEqual(await prepCount(lostOne), 1, "one preparation");
  log("answer of a send lost (connection reset after the server committed): the reloaded order shows the round, so it is reported as sent");

  await waiter.click("^Fries,");
  await waiter.waitText("Added 1× Fries");
  const lostTwo = await db.value(`select id from order_items where order_id = :o and status = 'ADDED'`, { o: orderId });
  const keysBefore = await count(`select count(*) from idempotency_keys where operation like 'POST %/send'`);
  restore = await sendWithLostAnswer(true);
  await waiter.waitText("unknown whether the round went out");
  assertEqual(await restore(), 200, "the server had sent the round");
  assertEqual(await db.value(`select status from order_items where id = :i`, { i: lostTwo }), "SENT", "sent server-side");
  const eventsAfterLoss = await outboxSent();
  await waiter.click("^Send round · 1 item$");
  await waiter.waitText("Round sent: 1 to Kitchen.");
  assertEqual(await outboxSent(), eventsAfterLoss, "the retry was replayed, not sent again");
  assertEqual(await prepCount(lostTwo), 1, "one preparation");
  assertEqual(await count(`select count(*) from idempotency_keys where operation like 'POST %/send'`), keysBefore + 1, "one key for both attempts");
  log("answer and reload both lost: the waiter is told the outcome is unknown; tapping Send again reuses the key and is replayed — no second round");

  // A round that fails validation commits nothing.
  await waiter.click("^Food$");
  await waiter.click("^Burger,");
  await waiter.click("^Well done$");
  await waiter.click("^Add ·");
  await waiter.waitText("Added 1× Burger");
  await waiter.click("^Drinks$");
  await waiter.click("^Beer,");
  await waiter.waitText("Added 1× Beer");
  await db.sql(`update stations set is_active = false where id = :s`, { s: main.bar_id });
  const snapshot = async () => [
    await count(`select count(*) from preparations`),
    await count(`select count(*) from outbox_events`),
    await count(`select count(*) from operational_events where event_type = 'order.sent'`),
    await db.sql(`select product_name_snapshot, status from order_items where order_id = :o and status = 'ADDED' order by created_at`, { o: orderId }),
  ];
  const beforeFail = await snapshot();
  await waiter.click("^Send round · 2 items$");
  await waiter.waitText("The round was not sent");
  await waiter.waitText("inactive");
  assertEqual(await snapshot(), beforeFail, "nothing changed");
  assertEqual(beforeFail[3], [["Burger", "ADDED"], ["Beer", "ADDED"]], "both items still unsent");
  assertEqual((await waiter.lines()).filter((l) => !l.sent).length, 2, "UI still shows 2 unsent");
  await db.sql(`update stations set is_active = true where id = :s`, { s: main.bar_id });
  await waiter.click("^Send round · 2 items$");
  await waiter.waitText("Round sent: 1 to Kitchen, 1 to Bar.");
  log("round with an item for a closed Bar is refused (409 STATION_INACTIVE): no preparation, outbox or audit row, Burger stays unsent too; after reopening it sends");

  // API down: errors are visible, nothing half-done; KDS reconnects.
  await waiter.click("^Food$");
  await api.stop();
  await until("kitchen notices", async () => ["reconnecting", "offline"].includes(await cook.liveStatus()), 15_000);
  await waiter.click("^Fries,");
  await waiter.waitText("Fries was not added");
  const downMessage = await waiter.ev(`document.querySelector(".banner--error")?.textContent ?? ""`);
  await waiter.waitText("may be out of date");
  const unsentWhileDown = await count(`select count(*) from order_items where order_id = :o and status = 'ADDED'`, { o: orderId });
  assertEqual(unsentWhileDown, 0, "no item added while the API is down");
  log(`API stopped: kitchen display shows "${await cook.liveStatus()}"; adding Fries shows "${downMessage.replace("×", "").trim()}" and stores nothing; the order is marked as possibly out of date`);
  await api.start();
  await until("kitchen reconnected", async () => ["connected", "live"].includes(await cook.liveStatus()), 40_000);
  await until("bar reconnected", async () => ["connected", "live"].includes(await barman.liveStatus()), 40_000);
  const kitchenBeforeRound = (await cook.tickets()).length;
  await waiter.click("^Fries,");
  await waiter.waitText("Added 1× Fries");
  await waiter.click("^Send round · 1 item$");
  await waiter.waitText("Round sent: 1 to Kitchen.");
  await waiter.waitGone("may be out of date");
  await until("kitchen got the round after reconnecting", async () => (await cook.tickets()).length === kitchenBeforeRound + 1);
  await until("kitchen live again", async () => (await cook.liveStatus()) === "live");
  log("API restarted: both displays reconnect by themselves; the out-of-date notice clears; the next round reaches the Kitchen live");

  // Redelivered events (at-least-once delivery) do not duplicate tickets.
  const kitchenTickets = (await cook.tickets()).sort();
  const barTickets = (await barman.tickets()).sort();
  const redelivered = await count(
    `with r as (update outbox_events set published_at = null, attempts = 0, next_attempt_at = now()
       where restaurant_id = :r returning 1) select count(*) from r`,
    { r: main.restaurant_id },
  );
  await until("redelivered", async () => (await undelivered()) === 0, 20_000);
  await sleep(1500);
  assertEqual((await cook.tickets()).sort(), kitchenTickets, "kitchen unchanged after redelivery");
  assertEqual((await barman.tickets()).sort(), barTickets, "bar unchanged after redelivery");
  log(`all ${redelivered} outbox events of the restaurant delivered again: Kitchen (${kitchenTickets.length}) and Bar (${barTickets.length}) tickets unchanged`);

  // ---------------------------------------------------------------------
  section("Scenario E — access rules");
  const sentItem = await db.value(`select id from order_items where order_id = :o and status = 'SENT' limit 1`, { o: orderId });
  const cancelled = await db.value(`select id from order_items where order_id = :o and status = 'CANCELLED' limit 1`, { o: orderId });
  const r1 = await waiter.api(`/api/v1/orders/${orderId}/items/${sentItem}`, { method: "PATCH", body: { quantity: 5 } });
  const r2 = await waiter.api(`/api/v1/orders/${orderId}/items/${cancelled}`, { method: "PATCH", body: { notes: "x" } });
  assertEqual([r1.status, r1.code, r2.status, r2.code], [409, "ORDER_ITEM_ALREADY_SENT", 409, "ORDER_ITEM_ALREADY_CANCELLED"], "sent/cancelled items cannot be edited");
  const otherCalls = [
    await waiter.api(`/api/v1/orders/${other.order_id}`),
    await waiter.api(`/api/v1/orders/${other.order_id}/items`, { method: "POST", body: { product_id: main.products.Fries, quantity: 1 } }),
    await waiter.api(`/api/v1/orders/${other.order_id}/items/${other.item_id}`, { method: "PATCH", body: { quantity: 3 } }),
    await waiter.api(`/api/v1/orders/${other.order_id}/items/${other.item_id}/cancel`, { method: "POST", body: { reason: "x" } }),
    await waiter.api(`/api/v1/orders/${other.order_id}/send`, { method: "POST" }),
    await waiter.api(`/api/v1/orders`, { method: "POST", body: { table_id: other.tables[2], guest_count: 1 } }),
  ];
  assertEqual(otherCalls.map((r) => r.status), [404, 404, 404, 404, 404, 404], "another tenant's data looks missing");
  // Station queues: refused by permission or station scope before any
  // lookup, so another tenant's station answers like a made-up id.
  const randomId = "00000000-0000-4000-8000-000000000000";
  const queueAnswers = [];
  for (const [page, station] of [[waiter, other.kitchen_id], [waiter, randomId], [cook, other.kitchen_id], [cook, randomId]]) {
    const r = await page.api(`/api/v1/stations/${station}/preparations`);
    queueAnswers.push(`${r.status} ${r.code}`);
  }
  assertEqual(queueAnswers[0], queueAnswers[1], "waiter: foreign station answers like a missing one");
  assertEqual(queueAnswers[2], queueAnswers[3], "cook: foreign station answers like a missing one");
  assertEqual(
    await db.sql(`select quantity, status from order_items where id = :i`, { i: other.item_id }),
    [[1, "ADDED"]],
    "other tenant's item untouched",
  );
  const tables = await waiter.api(`/api/v1/tables`);
  assert(!tables.body.some((t) => Object.values(other.tables).includes(t.id)), "tables list is own restaurant only");
  const startAsWaiter = await waiter.api(`/api/v1/preparations/${await db.value(`select id from preparations where status = 'PENDING' limit 1`)}/start`, { method: "POST" });
  const tablesAsCook = await cook.api(`/api/v1/tables`);
  const barAsCook = await cook.api(`/api/v1/stations/${main.bar_id}/preparations`);
  assertEqual([startAsWaiter.status, tablesAsCook.status, barAsCook.status, barAsCook.code], [403, 403, 403, "STATION_ACCESS_DENIED"], "role and station scope");
  log(`sent/cancelled items: PATCH → 409; other tenant's order/item/table → 404 for get, add, edit, cancel, send, open; its station queue answers like a made-up id (${queueAnswers[0]} / ${queueAnswers[2]}); waiter cannot Start (403); cook cannot list tables or read the Bar queue (403)`);

  await waiter.click("^Food$");
  await waiter.click("^Fries,");
  await waiter.waitText("Added 1× Fries");
  const pendingId = await db.value(`select id from order_items where order_id = :o and status = 'ADDED'`, { o: orderId });
  await waiter2.login(main.users.waiter2);
  await waiter2.click("^Table 1, Terrace:");
  await waiter2.waitText("Another waiter opened this order");
  assert((await waiter2.buttonDisabled("^Burger,")) === true, "menu disabled for another waiter");
  assert((await waiter2.buttonDisabled("^Send round")) === null, "no send button for another waiter");
  const r3 = await waiter2.api(`/api/v1/orders/${orderId}/items/${pendingId}`, { method: "PATCH", body: { quantity: 9 } });
  const r4 = await waiter2.api(`/api/v1/orders/${orderId}/send`, { method: "POST" });
  assertEqual([r3.status, r3.code, r4.status, r4.code], [403, "ORDER_ACCESS_DENIED", 403, "ORDER_ACCESS_DENIED"], "creator-only rule");
  assertEqual(await db.value(`select quantity from order_items where id = :i`, { i: pendingId }), 1, "unchanged");
  log("a second waiter sees table 1's order read-only; their PATCH/send through the API → 403 ORDER_ACCESS_DENIED; nothing changed");

  const audit = await db.sql(
    `select event_type, count(*) from operational_events where restaurant_id = :r group by event_type order by event_type`,
    { r: main.restaurant_id },
  );
  const auditMap = Object.fromEntries(audit.map(([t, c]) => [t, Number(c)]));
  for (const type of ["order.created", "item.added", "item.updated", "item.cancelled", "order.sent", "preparation.started", "preparation.ready"]) {
    assert(auditMap[type] > 0, `audit events of type ${type}`);
  }
  assertEqual(await count(`select count(*) from operational_events where event_type = 'order.sent' and restaurant_id = :r`, { r: main.restaurant_id }), await outboxSent(), "one audit event per order.sent outbox event");
  assertEqual(await count(`select count(*) from outbox_events where dead_lettered_at is not null`), 0, "no dead letters");
  log(`audit trail: ${audit.map(([t, c]) => `${t} ${c}`).join(", ")}; one audit event per order.sent outbox event; no dead letters`);

  // ---------------------------------------------------------------------
  section("Scenario F — viewports");
  const viewports = [[375, 812], [768, 1024], [1024, 768], [1440, 900], [1920, 1080]];
  const layout = [];
  const record = (page, label, report) => {
    layout.push({ label, ...report });
    const ok = report.scrollX <= 0 && report.offenders.length === 0;
    console.log(`     ${ok ? "ok  " : "FAIL"} ${label}: overflow ${report.scrollX}px${report.offenders.length ? ` (${report.offenders.join(", ")})` : ""}${report.small.length ? `; small buttons: ${report.small.join("; ")}` : ""}`);
  };
  for (const [w, h] of viewports) {
    await waiter.viewport(w, h);
    record(waiter, `pos-order-${w}x${h}`, await waiter.audit(`pos-order-${w}x${h}`));
    await waiter.click("^Burger,");
    await waiter.waitText("Doneness");
    record(waiter, `pos-dialog-${w}x${h}`, await waiter.audit(`pos-dialog-${w}x${h}`));
    await waiter.click("^Cancel$");
    await waiter.click("^← Tables$");
    await waiter.waitText("with an open order");
    record(waiter, `pos-tables-${w}x${h}`, await waiter.audit(`pos-tables-${w}x${h}`));
    await waiter.click("^Table 1, Terrace:");
    await waiter.waitText("Next round");
    await cook.viewport(w, h);
    record(cook, `kds-kitchen-${w}x${h}`, await cook.audit(`kds-kitchen-${w}x${h}`));
  }
  const overflowing = layout.filter((r) => r.scrollX > 0 || r.offenders.length > 0);
  await writeFile(join(ARTIFACTS, "layout.json"), JSON.stringify(layout, null, 2));
  assertEqual(overflowing.map((r) => r.label), [], "no horizontal overflow");
  log(`no horizontal overflow on POS tables/order/dialog and Kitchen display at ${viewports.map(([w]) => w).join(", ")} px`);

  const problems = browsers.flatMap((b, i) => b.problems.map((p) => `${["waiter", "kitchen", "bar", "waiter2"][i]}: ${p}`))
    // Expected while the API was deliberately stopped or a response dropped.
    .filter((p) => !/Failed to load resource|ERR_CONNECTION|50[0-4]|WebSocket connection/.test(p));
  assertEqual(problems, [], "no unexpected console errors or exceptions");
  log("no unexpected console errors or exceptions in the four browsers");
  console.log(`\nPASS — screenshots and layout.json in ${ARTIFACTS}`);
} catch (error) {
  failed = error;
  console.error(`\nFAIL in ${scenario} step ${step + 1}: ${error.stack ?? error}`);
  console.error(`(screenshots in ${ARTIFACTS})`);
  if (process.env.E2E_DEBUG) console.error(`--- API output ---\n${api.output.slice(-4000)}`);
} finally {
  await Promise.all(browsers.map((b) => b.close()));
  await server?.close();
  await api.stop();
  await db.close();
}
process.exit(failed ? 1 : 0);
