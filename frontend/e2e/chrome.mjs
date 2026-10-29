// Headless Chrome over the DevTools protocol, using Node's built-in fetch
// and WebSocket: no browser automation dependency.

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CANDIDATES = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
];

export function findChrome() {
  return CANDIDATES.find((path) => path && existsSync(path)) ?? null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function launchChrome(executable) {
  const profile = await mkdtemp(join(tmpdir(), "kds-e2e-chrome-"));
  const chrome = spawn(
    executable,
    [
      "--headless=new",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--disable-background-networking",
      "--disable-sync",
      "--use-mock-keychain",
      "--password-store=basic",
      `--user-data-dir=${profile}`,
      "--remote-debugging-port=0",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  let port = null;
  for (let i = 0; i < 150 && port === null; i++) {
    await sleep(100);
    try {
      port = (await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0];
    } catch {
      // Not written yet.
    }
  }
  if (port === null) {
    chrome.kill();
    throw new Error("Chrome did not open its DevTools port");
  }

  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = targets.find((target) => target.type === "page");
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });

  let nextId = 0;
  const pending = new Map();
  const problems = [];
  const frames = [];
  socket.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.id !== undefined) {
      const call = pending.get(data.id);
      pending.delete(data.id);
      if (data.error) call.reject(new Error(`${call.method}: ${data.error.message}`));
      else call.resolve(data.result);
    } else if (data.method?.startsWith("Network.webSocket")) {
      frames.push({ method: data.method, opcode: data.params.response?.opcode, payload: data.params.response?.payloadData, url: data.params.url, id: data.params.requestId, error: data.params.errorMessage });
    } else if (data.method === "Runtime.exceptionThrown") {
      problems.push(`exception: ${data.params.exceptionDetails.exception?.description ?? data.params.exceptionDetails.text}`);
    } else if (data.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(data.params.type)) {
      problems.push(`console.${data.params.type}: ${data.params.args.map((arg) => arg.value ?? arg.description).join(" ")}`);
    }
  };

  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++nextId;
      pending.set(id, { method, resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });

  await send("Runtime.enable");
  await send("Page.enable");

  async function evaluate(expression) {
    const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) {
      throw new Error(`evaluate failed: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`);
    }
    return result.result.value;
  }

  return {
    send,
    evaluate,
    problems,
    frames,
    async close() {
      socket.close();
      chrome.kill();
      await new Promise((resolve) => (chrome.exitCode !== null ? resolve() : chrome.once("exit", resolve)));
      // Chrome's helpers may still be writing the profile as they exit.
      await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }).catch((error) => {
        console.warn(`Could not remove the temporary Chrome profile ${profile}: ${error.message}`);
      });
    },
  };
}
