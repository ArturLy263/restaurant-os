import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PreparationQueueItem } from "../api/types";
import { orderSent, resyncRequired, statusChanged } from "../test/events";
import { FakeWebSocket } from "../test/fakeSocket";
import { STATION, queueItem } from "../test/fixtures";
import {
  RECONNECT_MAX_MS,
  SUBSCRIBE_TIMEOUT_MS,
  StationLiveQueue,
  reconnectDelay,
  type LiveSnapshot,
} from "./stationLive";

const TOKEN = "header.payload.signature";
const URL = `ws://localhost/api/v1/stations/${STATION.id}/events`;

interface Load {
  signal: AbortSignal;
  resolve(items: PreparationQueueItem[]): void;
  reject(error: unknown): void;
}

function setup() {
  const loads: Load[] = [];
  const fetchQueue = vi.fn(
    (signal: AbortSignal) =>
      new Promise<PreparationQueueItem[]>((resolve, reject) => loads.push({ signal, resolve, reject })),
  );
  const onLoadError = vi.fn();
  const onAuthExpired = vi.fn();
  const live = new StationLiveQueue({
    stationId: STATION.id,
    token: TOKEN,
    url: URL,
    fetchQueue,
    createSocket: (url) => new FakeWebSocket(url),
    onLoadError,
    onAuthExpired,
    // Longest delay of each backoff step, so tests can step exactly.
    random: () => 1,
  });
  return { live, loads, fetchQueue, onLoadError, onAuthExpired };
}

async function settle() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

function items(snapshot: LiveSnapshot): PreparationQueueItem[] {
  if (snapshot.queue.status !== "loaded") throw new Error(`queue is ${snapshot.queue.status}`);
  return snapshot.queue.data;
}

/** Started, subscribed, and both loads (initial and after subscribed) done. */
async function liveWith(queue: PreparationQueueItem[]) {
  const context = setup();
  context.live.start();
  const socket = FakeWebSocket.latest();
  socket.accept();
  socket.subscribe(STATION.id);
  context.loads[0].resolve(queue);
  await settle();
  context.loads[1].resolve(queue);
  await settle();
  expect(context.fetchQueue).toHaveBeenCalledTimes(2);
  return { ...context, socket };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("StationLiveQueue protocol", () => {
  it("authenticates with the first message, never in the URL, and goes live on subscribed", async () => {
    const { live } = setup();
    live.start();
    const socket = FakeWebSocket.latest();

    expect(socket.url).toBe(URL);
    expect(socket.url).not.toContain(TOKEN);
    expect(live.getSnapshot().live.status).toBe("connecting");

    socket.accept();
    expect(socket.sent).toEqual([JSON.stringify({ type: "authenticate", token: TOKEN })]);
    expect(live.getSnapshot().live.status).toBe("connecting");

    socket.subscribe(STATION.id);
    expect(live.getSnapshot().live).toEqual({ status: "connected", reason: null });
  });

  it("reports live only once the server delivered something on this connection", async () => {
    const item = queueItem();
    const { live, socket } = await liveWith([item]);
    expect(live.getSnapshot().live.status).toBe("connected");

    // Even an event that changes nothing proves delivery.
    socket.receive(statusChanged("e1", queueItem(), "PENDING", "IN_PROGRESS"));
    expect(live.getSnapshot().live.status).toBe("live");

    // A new connection has to prove it again.
    socket.serverClose(1012);
    vi.advanceTimersByTime(1_000);
    const next = FakeWebSocket.latest();
    next.accept();
    next.subscribe(STATION.id);
    expect(live.getSnapshot().live.status).toBe("connected");
    next.receive(resyncRequired());
    expect(live.getSnapshot().live.status).toBe("live");
  });

  it("reloads the queue after subscribed, even when the first load is still running", async () => {
    const { live, loads, fetchQueue } = setup();
    const item = queueItem();
    live.start();
    expect(fetchQueue).toHaveBeenCalledTimes(1);

    const socket = FakeWebSocket.latest();
    socket.accept();
    socket.subscribe(STATION.id);
    // The running load may predate the subscription: one more follows it.
    expect(fetchQueue).toHaveBeenCalledTimes(1);
    loads[0].resolve([]);
    await settle();
    expect(fetchQueue).toHaveBeenCalledTimes(2);
    expect(live.getSnapshot().refreshing).toBe(true);

    loads[1].resolve([item]);
    await settle();
    expect(items(live.getSnapshot())).toEqual([item]);
    expect(live.getSnapshot().refreshing).toBe(false);
  });

  it("ignores events sent before subscribed and a subscription to another station", async () => {
    const { live } = setup();
    const item = queueItem();
    live.start();
    const socket = FakeWebSocket.latest();
    socket.accept();
    socket.receive(statusChanged("e1", item, "PENDING", "IN_PROGRESS"));
    expect(live.getSnapshot().live.status).toBe("connecting");

    socket.subscribe("another-station");
    expect(live.getSnapshot().live.status).toBe("offline");
    expect(socket.closed).toBe(true);
    vi.advanceTimersByTime(RECONNECT_MAX_MS * 2);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe("StationLiveQueue events", () => {
  it("applies live events and ignores stale ones without reloading", async () => {
    const pending = queueItem({ status: "PENDING" });
    const ready = queueItem({ status: "READY" });
    const { live, socket, fetchQueue } = await liveWith([pending, ready]);

    socket.receive(statusChanged("e1", pending, "PENDING", "IN_PROGRESS"));
    socket.receive(statusChanged("e2", ready, "READY", "SERVED"));
    // Already reflected: previous_status no longer matches.
    socket.receive(statusChanged("e3", pending, "PENDING", "IN_PROGRESS"));
    // Not in this queue.
    socket.receive(statusChanged("e4", queueItem(), "PENDING", "IN_PROGRESS"));

    expect(items(live.getSnapshot()).map((item) => [item.id, item.status])).toEqual([[pending.id, "IN_PROGRESS"]]);
    expect(fetchQueue).toHaveBeenCalledTimes(2);
  });

  it("buffers events that arrive during a load and reconciles them with its result in order", async () => {
    const item = queueItem({ status: "PENDING" });
    const { live, socket, loads } = await liveWith([item]);

    socket.receive(resyncRequired());
    // Committed before the load (already in its result), then after it.
    socket.receive(statusChanged("e1", item, "PENDING", "IN_PROGRESS"));
    socket.receive(statusChanged("e2", item, "IN_PROGRESS", "READY"));
    expect(items(live.getSnapshot())[0].status).toBe("PENDING");

    loads[2].resolve([{ ...item, status: "IN_PROGRESS", started_at: "2026-10-22T18:01:00" }]);
    await settle();
    expect(items(live.getSnapshot())[0]).toMatchObject({ status: "READY", started_at: "2026-10-22T18:01:00" });
  });

  it("deduplicates events on their id, also across reconnects", async () => {
    const known = queueItem();
    const added = queueItem();
    const { live, socket, loads, fetchQueue } = await liveWith([known]);

    // A new preparation: reload for its product, table and modifiers.
    socket.receive(orderSent("sent-1", [added]));
    socket.receive(orderSent("sent-1", [added]));
    expect(fetchQueue).toHaveBeenCalledTimes(3);
    loads[2].resolve([known, added]);
    await settle();

    socket.serverClose(1006);
    vi.advanceTimersByTime(reconnectDelay(0, () => 1));
    const next = FakeWebSocket.latest();
    expect(next).not.toBe(socket);
    next.accept();
    next.subscribe(STATION.id);
    expect(fetchQueue).toHaveBeenCalledTimes(4);
    loads[3].resolve([known, added]);
    await settle();

    // Delivered again to the new connection (at least once).
    next.receive(orderSent("sent-1", [queueItem()]));
    expect(fetchQueue).toHaveBeenCalledTimes(4);
    expect(items(live.getSnapshot())).toEqual([known, added]);
  });

  it("reloads on resync_required and on events it cannot read, without failing", async () => {
    const item = queueItem();
    const { socket, loads, fetchQueue } = await liveWith([item]);

    socket.receive(resyncRequired());
    expect(fetchQueue).toHaveBeenCalledTimes(3);
    loads[2].resolve([item]);
    await settle();

    socket.receive({ ...statusChanged("e1", item, "PENDING", "IN_PROGRESS"), schema_version: 7 });
    expect(fetchQueue).toHaveBeenCalledTimes(4);
    // While that load runs, more resyncs coalesce into a single extra load.
    socket.receive("{broken json");
    socket.receive({ type: "something_new" });
    loads[3].resolve([item]);
    await settle();
    expect(fetchQueue).toHaveBeenCalledTimes(5);
    loads[4].resolve([item]);
    await settle();
    expect(fetchQueue).toHaveBeenCalledTimes(5);
  });

  it("keeps the last queue when a reload fails and reports the error", async () => {
    const item = queueItem();
    const { live, socket, loads, onLoadError } = await liveWith([item]);

    socket.receive(resyncRequired());
    const failure = new Error("offline");
    loads[2].reject(failure);
    await settle();

    expect(onLoadError).toHaveBeenCalledWith(failure);
    expect(items(live.getSnapshot())).toEqual([item]);
    expect(live.getSnapshot().refreshing).toBe(false);
  });

  it("shows an error when the first load fails and recovers on the next one", async () => {
    const { live, loads, onLoadError } = setup();
    live.start();
    loads[0].reject(new Error("down"));
    await settle();
    expect(live.getSnapshot().queue.status).toBe("error");
    expect(onLoadError).toHaveBeenCalledTimes(1);

    const socket = FakeWebSocket.latest();
    socket.accept();
    socket.subscribe(STATION.id);
    loads[1].resolve([]);
    await settle();
    expect(items(live.getSnapshot())).toEqual([]);
  });
});

describe("StationLiveQueue reconnection", () => {
  it("backs off exponentially with jitter up to a bound", () => {
    expect(reconnectDelay(0, () => 0)).toBe(500);
    expect(reconnectDelay(0, () => 1)).toBe(1_000);
    expect(reconnectDelay(3, () => 1)).toBe(8_000);
    expect(reconnectDelay(5, () => 1)).toBe(RECONNECT_MAX_MS);
    expect(reconnectDelay(50, () => 1)).toBe(RECONNECT_MAX_MS);
    expect(reconnectDelay(50, () => 0)).toBe(RECONNECT_MAX_MS / 2);
  });

  it("reconnects after drops, slow-consumer and busy closes, and resets the backoff once live", async () => {
    const { live, socket, fetchQueue, loads } = await liveWith([]);

    socket.serverClose(4429, "SLOW_CONSUMER");
    expect(live.getSnapshot().live.status).toBe("reconnecting");
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // Failing attempts wait longer each time: 2 s, then 4 s.
    FakeWebSocket.latest().serverClose(1013, "TOO_MANY_CONNECTIONS");
    vi.advanceTimersByTime(1_999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);
    FakeWebSocket.latest().serverClose(1006);
    vi.advanceTimersByTime(4_000);
    expect(FakeWebSocket.instances).toHaveLength(4);

    const current = FakeWebSocket.latest();
    current.accept();
    current.subscribe(STATION.id);
    expect(live.getSnapshot().live.status).toBe("connected");
    expect(fetchQueue).toHaveBeenCalledTimes(3);
    loads[2].resolve([]);
    await settle();

    // Live again: the next drop starts from the shortest delay.
    current.serverClose(1012);
    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances).toHaveLength(5);
  });

  it("gives up on a connection that never subscribes and tries again", () => {
    const { live } = setup();
    live.start();
    const socket = FakeWebSocket.latest();
    socket.accept();

    vi.advanceTimersByTime(SUBSCRIBE_TIMEOUT_MS);
    expect(socket.closed).toBe(true);
    expect(live.getSnapshot().live.status).toBe("reconnecting");
    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("ends the session on 4401 without retrying", async () => {
    const { live, socket, onAuthExpired } = await liveWith([]);
    socket.serverClose(4401, "NOT_AUTHENTICATED");

    expect(onAuthExpired).toHaveBeenCalledTimes(1);
    expect(live.getSnapshot().live.status).toBe("offline");
    vi.advanceTimersByTime(RECONNECT_MAX_MS * 4);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it.each([
    [4403, "STATION_ACCESS_DENIED", "No access to this station's live updates."],
    [4404, "STATION_NOT_FOUND", "This station no longer exists."],
  ])("stops on %i without retrying and keeps the queue", async (code, reason, text) => {
    const item = queueItem();
    const { live, socket, onAuthExpired } = await liveWith([item]);
    socket.serverClose(code, reason);

    expect(live.getSnapshot().live).toEqual({ status: "offline", reason: text });
    expect(items(live.getSnapshot())).toEqual([item]);
    expect(onAuthExpired).not.toHaveBeenCalled();
    vi.advanceTimersByTime(RECONNECT_MAX_MS * 4);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe("StationLiveQueue wake", () => {
  it("reconnects at once when woken during a backoff, without a second socket", async () => {
    const { live, socket } = await liveWith([]);
    socket.serverClose(1006);
    // Three failed attempts: the next one would wait 8 s.
    for (const delay of [1_000, 2_000, 4_000]) {
      vi.advanceTimersByTime(delay);
      FakeWebSocket.latest().serverClose(1006);
    }
    expect(FakeWebSocket.instances).toHaveLength(4);

    vi.advanceTimersByTime(1_000);
    live.wake();
    live.wake();
    expect(FakeWebSocket.instances).toHaveLength(5);
    // The pending backoff timer is gone: no extra socket later.
    vi.advanceTimersByTime(8_000);
    expect(FakeWebSocket.instances).toHaveLength(5);

    // A failure after a wake keeps backing off from where it was.
    FakeWebSocket.latest().serverClose(1006);
    vi.advanceTimersByTime(15_999);
    expect(FakeWebSocket.instances).toHaveLength(5);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(6);
  });

  it("reconnects no sooner than the base delay after the last attempt when woken early", async () => {
    const { live, socket } = await liveWith([]);
    vi.advanceTimersByTime(5_000);
    socket.serverClose(1006);
    live.wake();
    expect(FakeWebSocket.instances).toHaveLength(2);

    // Fails at once; the backoff would now wait 2 s.
    FakeWebSocket.latest().serverClose(1006);
    vi.advanceTimersByTime(500);
    live.wake();
    live.wake();
    expect(FakeWebSocket.instances).toHaveLength(2);
    // Brought forward to 1 s after the last attempt, not 2 s.
    vi.advanceTimersByTime(499);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);
    vi.advanceTimersByTime(10_000);
    expect(FakeWebSocket.instances).toHaveLength(3);
  });

  it("keeps a pending reconnect that is due sooner than a wake could bring it", async () => {
    const { live, socket } = await liveWith([]);
    socket.serverClose(1006);
    // Due 1 s after the close; a wake now cannot make it earlier.
    vi.advanceTimersByTime(200);
    live.wake();
    vi.advanceTimersByTime(799);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("reloads a subscribed queue once, and not while a load runs", async () => {
    const { live, loads, fetchQueue } = await liveWith([]);
    live.wake();
    live.wake();
    expect(fetchQueue).toHaveBeenCalledTimes(3);
    loads[2].resolve([]);
    await settle();
    expect(fetchQueue).toHaveBeenCalledTimes(3);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("leaves refused, connecting, unstarted and stopped streams alone", async () => {
    const refused = await liveWith([]);
    refused.socket.serverClose(4403, "STATION_ACCESS_DENIED");
    refused.live.wake();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(refused.fetchQueue).toHaveBeenCalledTimes(2);

    FakeWebSocket.reset();
    const connecting = setup();
    connecting.live.wake();
    expect(FakeWebSocket.instances).toHaveLength(0);
    connecting.live.start();
    vi.advanceTimersByTime(5_000);
    connecting.live.wake();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(connecting.fetchQueue).toHaveBeenCalledTimes(1);

    connecting.live.stop();
    FakeWebSocket.latest().serverClose(1006);
    connecting.live.wake();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe("StationLiveQueue stop", () => {
  it("closes the stream, aborts the load and ignores everything after", async () => {
    const { live, loads } = setup();
    const listener = vi.fn();
    live.subscribe(listener);
    live.start();
    const socket = FakeWebSocket.latest();
    socket.accept();

    live.stop();
    expect(socket.closedBy).toBe(1000);
    expect(loads[0].signal.aborted).toBe(true);
    listener.mockClear();

    loads[0].resolve([queueItem()]);
    await settle();
    socket.serverClose(1006);
    vi.advanceTimersByTime(RECONNECT_MAX_MS * 4);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(listener).not.toHaveBeenCalled();
    expect(live.getSnapshot().queue.status).toBe("loading");
    await expect(live.reload()).resolves.toBeUndefined();
  });

  it("cancels a scheduled reconnect", async () => {
    const { live, socket } = await liveWith([]);
    socket.serverClose(1006);
    live.stop();
    vi.advanceTimersByTime(RECONNECT_MAX_MS * 4);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
