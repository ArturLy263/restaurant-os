import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { getStationQueue } from "../api/endpoints";
import {
  CloseCode,
  parseServerMessage,
  stationEventsUrl,
  type ServerMessage,
  type StationEvent,
} from "../api/stationEvents";
import type { PreparationQueueItem } from "../api/types";
import { applyStationEvent } from "./liveQueue";

export type Load<T> =
  | { status: "loading" }
  | { status: "loaded"; data: T }
  | { status: "error"; error: Error };

// connected: subscribed, but nothing delivered on this connection yet. The
// protocol cannot tell an idle station from an API that delivers no events
// (OUTBOX_WEBSOCKET_DELIVERY off), so only a delivered message makes it live.
export type LiveStatus = "connecting" | "connected" | "live" | "reconnecting" | "offline";

export interface LiveSnapshot {
  queue: Load<PreparationQueueItem[]>;
  // A queue load is in flight.
  refreshing: boolean;
  live: { status: LiveStatus; reason: string | null };
}

/** The part of WebSocket the controller uses (a fake in tests). */
export interface SocketLike {
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface StationLiveOptions {
  stationId: string;
  token: string;
  url: string;
  fetchQueue(signal: AbortSignal): Promise<PreparationQueueItem[]>;
  createSocket(url: string): SocketLike;
  onLoadError(error: unknown): void;
  // The stream refused the token (4401): the session is over.
  onAuthExpired(): void;
  random?: () => number;
}

export const RECONNECT_BASE_MS = 1_000;
export const RECONNECT_MAX_MS = 30_000;
// The server closes unauthenticated connections after 10 s by default.
export const SUBSCRIBE_TIMEOUT_MS = 15_000;
// Recently received event ids, kept across reconnects for deduplication.
export const SEEN_EVENT_LIMIT = 500;

const OFFLINE_REASONS: Record<number, string> = {
  [CloseCode.NotAuthenticated]: "Your session has expired.",
  [CloseCode.Forbidden]: "No access to this station's live updates.",
  [CloseCode.StationNotFound]: "This station no longer exists.",
};

/** Exponential backoff with jitter: 50-100 % of min(max, base * 2^attempt). */
export function reconnectDelay(attempt: number, random: () => number = Math.random): number {
  const ceiling = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
  return Math.round(ceiling * (0.5 + random() * 0.5));
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * Keeps one station's queue current: loads it over HTTP and applies the
 * station event stream, as the backend README ("Client synchronization")
 * prescribes. After every `subscribed` the queue is reloaded; events that
 * arrive during a load are buffered and reconciled with its result, in
 * arrival order; events are deduplicated on their id. Any close other than
 * an authentication or access refusal reconnects with bounded backoff.
 */
export class StationLiveQueue {
  private snapshot: LiveSnapshot = {
    queue: { status: "loading" },
    refreshing: false,
    live: { status: "connecting", reason: null },
  };
  private readonly listeners = new Set<() => void>();
  private readonly random: () => number;

  private started = false;
  private stopped = false;

  private socket: SocketLike | null = null;
  private subscribed = false;
  private wasLive = false;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDueAt = 0;
  private lastConnectAt = -Infinity;
  private subscribeTimer: ReturnType<typeof setTimeout> | null = null;

  private loading: Promise<void> | null = null;
  private loadAgain = false;
  private abort: AbortController | null = null;
  private buffer: StationEvent[] = [];
  private readonly seen = new Set<string>();

  constructor(private readonly options: StationLiveOptions) {
    this.random = options.random ?? Math.random;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): LiveSnapshot => this.snapshot;

  /** Loads the queue at once (for display) and opens the stream. */
  start(): void {
    if (this.started || this.stopped) return;
    this.started = true;
    void this.reload();
    this.connect();
  }

  /** Closes the stream and ignores every later result; final. */
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.clearReconnect();
    this.dropSocket();
    this.abort?.abort();
    this.buffer = [];
  }

  /**
   * The network came back or the display became visible again. A pending
   * reconnect runs now instead of waiting out its backoff, but never sooner
   * than RECONNECT_BASE_MS after the last attempt (a failure still backs off
   * further); a subscribed stream reloads the queue, since a sleeping device
   * may have missed events on a connection that only looks open. Nothing
   * else changes: refused streams stay off and an attempt in flight is kept.
   */
  wake(): void {
    if (!this.started || this.stopped) return;
    if (this.reconnectTimer !== null) {
      const now = Date.now();
      const earliest = Math.max(now, this.lastConnectAt + RECONNECT_BASE_MS);
      if (earliest >= this.reconnectDueAt) return;
      this.clearReconnect();
      if (earliest === now) this.connect();
      else this.scheduleConnect(earliest - now);
      return;
    }
    if (this.subscribed && !this.loading) void this.reload();
  }

  /**
   * Reloads the queue. A reload requested while one is in flight runs once
   * more after it (the running one may predate the request); the promise
   * settles when no load is left.
   */
  reload(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.loading) {
      this.loadAgain = true;
      return this.loading;
    }
    this.update({ refreshing: true });
    this.loading = this.runLoads();
    return this.loading;
  }

  private async runLoads(): Promise<void> {
    try {
      do {
        this.loadAgain = false;
        await this.loadOnce();
      } while (this.loadAgain && !this.stopped);
    } finally {
      // Synchronously after the last load applied its buffer, so no event
      // can be buffered without being applied.
      this.loading = null;
      if (!this.stopped) this.update({ refreshing: false });
    }
  }

  private async loadOnce(): Promise<void> {
    const controller = new AbortController();
    this.abort = controller;
    let data: PreparationQueueItem[] | null = null;
    let failure: unknown = null;
    try {
      data = await this.options.fetchQueue(controller.signal);
    } catch (error) {
      failure = error;
    }
    if (this.stopped) return;

    const current = this.snapshot.queue;
    let items = data ?? (current.status === "loaded" ? current.data : null);
    if (data === null && !isAbort(failure)) {
      this.options.onLoadError(failure);
    }

    const buffered = this.buffer;
    this.buffer = [];
    if (items === null) {
      // Nothing to reconcile with; the next load reflects these events.
      this.update({
        queue: { status: "error", error: failure instanceof Error ? failure : new Error("Queue could not be loaded") },
      });
      return;
    }
    for (const event of buffered) {
      const result = applyStationEvent(items, event, this.options.stationId);
      items = result.items;
      if (result.reload) this.loadAgain = true;
    }
    this.update({ queue: { status: "loaded", data: items } });
  }

  private handleEvent(event: StationEvent): void {
    if (this.seen.has(event.id)) return;
    this.seen.add(event.id);
    if (this.seen.size > SEEN_EVENT_LIMIT) {
      this.seen.delete(this.seen.values().next().value!);
    }

    if (this.loading) {
      this.buffer.push(event);
      return;
    }
    const queue = this.snapshot.queue;
    if (queue.status !== "loaded") return;
    const result = applyStationEvent(queue.data, event, this.options.stationId);
    if (result.items !== queue.data) {
      this.update({ queue: { status: "loaded", data: result.items } });
    }
    if (result.reload) void this.reload();
  }

  private connect(): void {
    this.reconnectTimer = null;
    if (this.stopped) return;

    let socket: SocketLike;
    try {
      socket = this.options.createSocket(this.options.url);
    } catch {
      this.setLive("offline", "Live updates are not available in this browser.");
      return;
    }
    this.socket = socket;
    this.subscribed = false;
    this.lastConnectAt = Date.now();
    this.setLive(this.wasLive || this.attempt > 0 ? "reconnecting" : "connecting");

    socket.onopen = () => {
      if (socket !== this.socket) return;
      // The token goes in the first message, never in the URL.
      socket.send(JSON.stringify({ type: "authenticate", token: this.options.token }));
    };
    socket.onmessage = (event) => {
      if (socket !== this.socket) return;
      this.handleMessage(parseServerMessage(event.data));
    };
    socket.onclose = (event) => {
      if (socket !== this.socket) return;
      this.handleClose(event.code);
    };
    // An error is always followed by a close.
    socket.onerror = () => {};

    this.subscribeTimer = setTimeout(() => {
      if (socket !== this.socket || this.subscribed) return;
      this.dropSocket();
      this.scheduleReconnect();
    }, SUBSCRIBE_TIMEOUT_MS);
  }

  private handleMessage(message: ServerMessage): void {
    if (!this.subscribed) {
      // The server sends nothing else before `subscribed`.
      if (message.type !== "subscribed") return;
      if (message.stationId.toLowerCase() !== this.options.stationId.toLowerCase()) {
        this.dropSocket();
        this.setLive("offline", "The server subscribed to a different station.");
        return;
      }
      this.subscribed = true;
      this.clearSubscribeTimer();
      this.attempt = 0;
      this.wasLive = true;
      this.setLive("connected");
      // Events delivered before the subscription are in this load; every
      // later one reaches this connection.
      void this.reload();
      return;
    }

    // Only the delivery worker sends after `subscribed`: delivery works.
    this.setLive("live");
    if (message.type === "event") this.handleEvent(message.event);
    else if (message.type === "resync") void this.reload();
  }

  private handleClose(code: number): void {
    this.socket = null;
    this.subscribed = false;
    this.clearSubscribeTimer();

    if (code === CloseCode.NotAuthenticated) {
      this.setLive("offline", OFFLINE_REASONS[code]);
      this.options.onAuthExpired();
      return;
    }
    if (code === CloseCode.Forbidden || code === CloseCode.StationNotFound) {
      // Retrying does not help: the user's access or the station changed.
      this.setLive("offline", OFFLINE_REASONS[code]);
      return;
    }
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    this.clearReconnect();
    const delay = reconnectDelay(this.attempt, this.random);
    this.attempt += 1;
    this.setLive("reconnecting");
    this.scheduleConnect(delay);
  }

  private scheduleConnect(delay: number): void {
    this.reconnectDueAt = Date.now() + delay;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private dropSocket(): void {
    const socket = this.socket;
    this.socket = null;
    this.subscribed = false;
    this.clearSubscribeTimer();
    if (!socket) return;
    socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
    try {
      socket.close(1000);
    } catch {
      // Already closed.
    }
  }

  private clearSubscribeTimer(): void {
    if (this.subscribeTimer !== null) clearTimeout(this.subscribeTimer);
    this.subscribeTimer = null;
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private setLive(status: LiveStatus, reason: string | null = null): void {
    const live = this.snapshot.live;
    if (live.status === status && live.reason === reason) return;
    this.update({ live: { status, reason } });
  }

  private update(changes: Partial<LiveSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...changes };
    for (const listener of this.listeners) listener();
  }
}

const INITIAL_SNAPSHOT: LiveSnapshot = {
  queue: { status: "loading" },
  refreshing: false,
  live: { status: "connecting", reason: null },
};

function subscribeNothing(): () => void {
  return () => {};
}

interface UseStationLiveQueueOptions {
  stationId: string;
  token: string;
  onLoadError(error: unknown): void;
  onAuthExpired(): void;
}

/** One StationLiveQueue per station and token, stopped on unmount. */
export function useStationLiveQueue(options: UseStationLiveQueueOptions) {
  const { stationId, token } = options;
  const callbacks = useRef(options);
  useEffect(() => {
    callbacks.current = options;
  });

  const [controller, setController] = useState<StationLiveQueue | null>(null);

  useEffect(() => {
    const live = new StationLiveQueue({
      stationId,
      token,
      url: stationEventsUrl(stationId),
      fetchQueue: (signal) => getStationQueue(token, stationId, signal),
      createSocket: (url) => new WebSocket(url),
      onLoadError: (error) => callbacks.current.onLoadError(error),
      onAuthExpired: () => callbacks.current.onAuthExpired(),
    });
    setController(live);
    live.start();
    return () => live.stop();
  }, [stationId, token]);

  useEffect(() => {
    if (!controller) return;
    const wake = () => controller.wake();
    const wakeIfVisible = () => {
      if (document.visibilityState === "visible") wake();
    };
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wakeIfVisible);
    return () => {
      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wakeIfVisible);
    };
  }, [controller]);

  const snapshot = useSyncExternalStore(
    controller?.subscribe ?? subscribeNothing,
    controller?.getSnapshot ?? (() => INITIAL_SNAPSHOT),
  );
  const reload = useCallback(() => controller?.reload() ?? Promise.resolve(), [controller]);

  return { snapshot, reload };
}
