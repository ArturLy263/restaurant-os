// Real-browser check of the kitchen display: headless Chrome against the
// Vite dev server (and its /api proxy), with mockApi.mjs as the backend.
// Covers sign-in, station choice, the live stream lifecycle (subscribed,
// events, reconnects, network return, refused streams, 4401) and the layout
// at common display widths. Never touches a real backend or database.
//
//   npm run e2e                 # CHROME_PATH=... to use another Chrome
//   E2E_ARTIFACTS=dir npm run e2e   # screenshots (default e2e/artifacts)

import { mkdir, writeFile } from "node:fs/promises";
import { createServer as createNetServer } from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { createServer } from "vite";

import { findChrome, launchChrome } from "./chrome.mjs";
import { BAR, KITCHEN, TOKEN, startMockApi } from "./mockApi.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const artifacts = resolve(process.env.E2E_ARTIFACTS ?? join(root, "e2e", "artifacts"));
const VIEWPORTS = [
  [375, 812],
  [768, 1024],
  [1024, 768],
  [1280, 800],
  [1920, 1080],
];

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

async function until(label, check, timeout = 5_000) {
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
function log(message) {
  step += 1;
  console.log(`  ${String(step).padStart(2)}. ${message}`);
}

async function main() {
  const executable = findChrome();
  if (!executable) {
    console.error("No Chrome or Chromium found; set CHROME_PATH to run the browser test.");
    process.exit(2);
  }

  const mock = await startMockApi();
  process.env.API_PROXY_TARGET = `http://127.0.0.1:${mock.port}`;
  const port = await freePort();
  const vite = await createServer({
    root,
    configFile: join(root, "vite.config.ts"),
    logLevel: "error",
    clearScreen: false,
    server: { host: "127.0.0.1", port, strictPort: true },
  });
  await vite.listen();
  const browser = await launchChrome(executable);
  const origin = `http://127.0.0.1:${port}`;

  const page = {
    text: () => browser.evaluate("document.body.innerText"),
    liveStatus: () => browser.evaluate(`document.querySelector(".live")?.textContent ?? ""`),
    click: (name) =>
      browser.evaluate(`(() => {
        const button = [...document.querySelectorAll("button")].find(
          (b) => (b.getAttribute("aria-label") ?? b.textContent.trim()) === ${JSON.stringify(name)}
            || b.textContent.trim().startsWith(${JSON.stringify(name)}));
        if (!button) throw new Error("no button " + ${JSON.stringify(name)});
        button.click();
      })()`),
    fill: (label, value) =>
      browser.evaluate(`(() => {
        const field = [...document.querySelectorAll("label")].find((l) => l.textContent.includes(${JSON.stringify(label)}))
          .querySelector("input");
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(field, ${JSON.stringify(value)});
        field.dispatchEvent(new Event("input", { bubbles: true }));
      })()`),
    hasText: async (text) => (await browser.evaluate("document.body.innerText")).includes(text),
  };

  const connections = mock.state.connections;
  // The newest connection that authenticated and is still open.
  const authenticated = (after) =>
    until(`an authenticated stream after #${after}`, () =>
      connections.slice(after).find((c) => c.authMessage && !c.ended && c.closeCode === null),
    );

  try {
    await browser.send("Network.enable");
    await browser.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
    await browser.send("Page.navigate", { url: origin });
    await until("the sign-in form", () => page.hasText("Sign in"), 15_000);
    log("sign-in screen loads through the Vite dev server");

    await page.fill("Email", "e2e@example.com");
    await page.fill("Password", "e2e-password");
    await page.click("Sign in");
    await until("the station picker", () => page.hasText("Choose a station"));
    assert(mock.state.logins.length === 1, "one login request");
    log("signs in and lists the stations");

    await page.click("Kitchen");
    await until("the Kitchen queue", () => page.hasText("Pasta"));
    const first = await authenticated(0);
    assert(first.stationId === KITCHEN.id, "stream for the Kitchen");
    assert(JSON.stringify(first.authMessage) === JSON.stringify({ type: "authenticate", token: TOKEN }), "authenticate first");
    assert(!first.url.includes(TOKEN), "token not in the URL");
    assert((await page.liveStatus()).startsWith("Connecting…"), "connecting before subscribed");
    log("opens the queue; the stream authenticates with its first message");

    const loadsBefore = mock.state.queueRequests[KITCHEN.id];
    first.send({ type: "subscribed", station_id: KITCHEN.id });
    await until("Connected", async () => (await page.liveStatus()).startsWith("Connected"));
    await until("a reload after subscribed", () => mock.state.queueRequests[KITCHEN.id] > loadsBefore);
    log("shows Connected (delivery not yet confirmed) and reloads after subscribed");

    const pasta = mock.state.queues[KITCHEN.id][0];
    const event = {
      type: "event",
      id: "e2e-event-1",
      event_type: "preparation.status_changed",
      schema_version: 1,
      aggregate_type: "order",
      aggregate_id: pasta.order_id,
      created_at: new Date().toISOString(),
      payload: {
        preparation_id: pasta.id,
        order_id: pasta.order_id,
        order_item_id: pasta.order_item_id,
        station_id: KITCHEN.id,
        previous_status: "PENDING",
        status: "IN_PROGRESS",
      },
    };
    mock.state.queues[KITCHEN.id][0] = { ...pasta, status: "IN_PROGRESS", started_at: pasta.created_at };
    const loadsBeforeEvent = mock.state.queueRequests[KITCHEN.id];
    first.send(event);
    first.send(event);
    await until("Live", async () => (await page.liveStatus()).startsWith("Live"));
    await until("Pasta in Preparing", () =>
      browser.evaluate(`!!document.querySelector('[aria-label="Ready: 1× Pasta"]')`),
    );
    await sleep(200);
    assert(mock.state.queueRequests[KITCHEN.id] === loadsBeforeEvent, "a status event needs no reload");
    log("applies a delivered event once (duplicate ignored) and shows Live");

    await mkdir(artifacts, { recursive: true });
    for (const [width, height] of VIEWPORTS) {
      await browser.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 });
      await sleep(150);
      const layout = await browser.evaluate(`(() => {
        const issues = [];
        if (document.documentElement.scrollWidth > window.innerWidth) {
          issues.push("horizontal scroll: " + document.documentElement.scrollWidth + " > " + window.innerWidth);
        }
        const oneLine = (el) => el.getClientRects().length === 1 && el.offsetHeight < parseFloat(getComputedStyle(el).lineHeight) * 1.6;
        for (const ticket of document.querySelectorAll(".ticket")) {
          const name = ticket.getAttribute("aria-label");
          const box = ticket.getBoundingClientRect();
          const age = ticket.querySelector(".ticket__age");
          if (!oneLine(age)) issues.push(name + ": timer wraps");
          if (age.getBoundingClientRect().right > box.right + 0.5) issues.push(name + ": timer overflows the ticket");
          const [left, meta] = ticket.querySelector(".ticket__header").children;
          if (left.getBoundingClientRect().right > meta.getBoundingClientRect().left + 0.5) issues.push(name + ": header overlaps");
        }
        const live = document.querySelector(".live");
        if (!oneLine(live)) issues.push("live status wraps");
        for (const button of document.querySelectorAll(".button--action")) {
          if (button.getBoundingClientRect().height < 48) issues.push("action button below 48 px");
        }
        return issues;
      })()`);
      const metrics = await browser.send("Page.getLayoutMetrics");
      const shot = await browser.send("Page.captureScreenshot", {
        format: "png",
        captureBeyondViewport: true,
        clip: { x: 0, y: 0, width, height: Math.ceil(metrics.cssContentSize.height), scale: 1 },
      });
      await writeFile(join(artifacts, `kds-${width}x${height}.png`), Buffer.from(shot.data, "base64"));
      assert(layout.length === 0, `layout at ${width}px: ${layout.join("; ")}`);
    }
    await browser.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
    log(`layout holds at ${VIEWPORTS.map(([w]) => w).join(", ")} px (screenshots in ${artifacts})`);

    const beforeRestart = connections.length;
    first.close(1012, "SERVICE_RESTART");
    await until("Reconnecting", async () => (await page.liveStatus()).startsWith("Reconnecting"));
    const second = await authenticated(beforeRestart);
    const loadsBeforeSecond = mock.state.queueRequests[KITCHEN.id];
    second.send({ type: "subscribed", station_id: KITCHEN.id });
    await until("Connected again", async () => (await page.liveStatus()).startsWith("Connected"));
    await until("a reload after resubscribing", () => mock.state.queueRequests[KITCHEN.id] > loadsBeforeSecond);
    assert(first.closeCode === 1012, "the browser saw 1012, not a dropped connection");
    log("reconnects after a server restart (1012), back to Connected, and reloads");

    // Offline: the stream drops and reconnect attempts fail and back off.
    await sleep(1_100);
    await browser.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    const beforeOffline = connections.length;
    second.drop();
    await until("Reconnecting while offline", async () => (await page.liveStatus()).startsWith("Reconnecting"));
    await sleep(3_500);
    await browser.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    const online = Date.now();
    const third = await authenticated(beforeOffline);
    const waited = Date.now() - online;
    // The wake brings the pending backoff (here 2-4 s) forward to at most
    // 1 s after the last attempt.
    assert(waited < 2_000, `reconnected ${waited} ms after the network returned`);
    third.send({ type: "subscribed", station_id: KITCHEN.id });
    await until("Connected after the network returns", async () => (await page.liveStatus()).startsWith("Connected"));
    log(`retries while offline, then reconnects ${waited} ms after the network returns`);

    const beforeSwitch = connections.length;
    await page.click("Change station");
    await until("the Kitchen stream closed by the client", () => third.closeCode === 1000);
    await page.click("Bar");
    await until("the Bar queue", () => page.hasText("All caught up"));
    const bar = await authenticated(beforeSwitch);
    assert(bar.stationId === BAR.id, "stream for the Bar");
    bar.send({ type: "subscribed", station_id: BAR.id });
    await until("Bar connected", async () => (await page.liveStatus()).startsWith("Connected"));
    log("changing station closes the old stream (1000) and opens the new one");

    const beforeForbidden = connections.length;
    bar.close(4403, "STATION_ACCESS_DENIED");
    await until("Live updates off", async () => (await page.liveStatus()).startsWith("Live updates off"));
    await browser.evaluate(`window.dispatchEvent(new Event("online"))`);
    await sleep(2_500);
    assert(bar.closeCode === 4403, "the browser saw 4403");
    assert(connections.length === beforeForbidden, "no retry after 4403, even when woken");
    log("4403 turns live updates off without retrying");

    await page.click("Sign out");
    await until("the sign-in form again", () => page.hasText("Sign in"));
    await page.fill("Email", "e2e@example.com");
    await page.fill("Password", "e2e-password");
    await page.click("Sign in");
    // The Bar is remembered for this tab.
    await until("the Bar queue after signing in", () => page.hasText("All caught up"));
    const again = await authenticated(beforeForbidden);
    again.send({ type: "subscribed", station_id: BAR.id });
    await until("connected after signing in", async () => (await page.liveStatus()).startsWith("Connected"));
    again.close(4401, "NOT_AUTHENTICATED");
    await until("the session-expired notice", () => page.hasText("Your session has expired"));
    assert(again.closeCode === 4401, "the browser saw 4401");
    assert(
      !(await browser.evaluate(`sessionStorage.getItem("restaurantos.accessToken")`)),
      "token cleared after 4401",
    );
    log("4401 ends the session and returns to sign-in");

    assert(mock.state.actionRequests.length === 0, "no Start/Ready/Serve request was made");
    // Expected: sockets the app closed while connecting (StrictMode's
    // double mount in development) and attempts made while offline.
    const EXPECTED_FRAME_ERRORS = /closed before the connection is established|ERR_INTERNET_DISCONNECTED/;
    const frameErrors = browser.frames.filter(
      (frame) => frame.method === "Network.webSocketFrameError" && !EXPECTED_FRAME_ERRORS.test(frame.error ?? ""),
    );
    assert(frameErrors.length === 0, `WebSocket frame errors: ${frameErrors.map((frame) => frame.error).join("; ")}`);
    const offlineAttempts = browser.frames.filter((frame) => /ERR_INTERNET_DISCONNECTED/.test(frame.error ?? "")).length;
    assert(offlineAttempts > 0, "reconnect attempts were made while offline");
    assert(browser.problems.length === 0, `browser errors:\n${browser.problems.join("\n")}`);
    log("no action requests, frame errors, page errors or console errors/warnings");
    console.log("KDS browser test passed.");
  } finally {
    await browser.close();
    await vite.close();
    await mock.close();
  }
}

main().catch((error) => {
  console.error(error.stack ?? error);
  process.exit(1);
});
