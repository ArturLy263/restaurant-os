import type { SocketLike } from "../kds/stationLive";

/**
 * A WebSocket stand-in driven by the test: `open()`, `receive()` and
 * `serverClose()` play the server; `sent` and `closedBy` record the client.
 * Installed as the global WebSocket for every test (src/test/setup.ts).
 */
export class FakeWebSocket implements SocketLike {
  static instances: FakeWebSocket[] = [];

  static latest(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (!socket) throw new Error("No WebSocket was opened");
    return socket;
  }

  static reset(): void {
    FakeWebSocket.instances = [];
  }

  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  readonly sent: string[] = [];
  open = false;
  // The code the client closed with, if it did.
  closedBy: number | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    if (!this.open) throw new Error("send() before open");
    this.sent.push(data);
  }

  close(code = 1000): void {
    this.closedBy = code;
    this.open = false;
  }

  get closed(): boolean {
    return this.closedBy !== null;
  }

  // Server side.

  accept(): void {
    this.open = true;
    this.onopen?.(new Event("open"));
  }

  receive(message: unknown): void {
    const data = typeof message === "string" ? message : JSON.stringify(message);
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  subscribe(stationId: string): void {
    this.receive({ type: "subscribed", station_id: stationId });
  }

  serverClose(code: number, reason = ""): void {
    this.open = false;
    this.onclose?.(new CloseEvent("close", { code, reason }));
  }
}
