import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { writeToken } from "../auth/tokenStorage";
import { statusChanged } from "../test/events";
import { FakeWebSocket } from "../test/fakeSocket";
import { BAR, KITCHEN_USER, STATION, fakeToken, jsonResponse, mockApi, queueItem } from "../test/fixtures";

const TOKEN = fakeToken({ sub: KITCHEN_USER.id, exp: Math.floor(Date.now() / 1000) + 3600 });
const QUEUE = `GET /api/v1/stations/${STATION.id}/preparations`;
const BAR_QUEUE = `GET /api/v1/stations/${BAR.id}/preparations`;

function liveStatus(): string {
  return screen.getByRole("status").textContent ?? "";
}

/** Opens the Kitchen queue with `items`, signed in with a stored token. */
async function openKitchen(items = [queueItem({ product_name: "Pasta" })]) {
  writeToken(TOKEN);
  const api = mockApi({
    "GET /api/v1/me": [jsonResponse(KITCHEN_USER)],
    "GET /api/v1/stations": [jsonResponse([STATION])],
    [QUEUE]: [jsonResponse(items)],
  });
  render(<App />);
  expect(await screen.findByText(items[0]?.product_name ?? "All caught up")).toBeTruthy();
  return { ...api, socket: FakeWebSocket.latest() };
}

async function goLive(socket: FakeWebSocket, stationId = STATION.id) {
  await act(async () => {
    socket.accept();
    socket.subscribe(stationId);
  });
}

describe("KDS live updates", () => {
  it("shows connecting, connected, then live once events arrive, without any action request", async () => {
    const pasta = queueItem({ product_name: "Pasta" });
    const { socket, calls } = await openKitchen([pasta]);

    expect(socket.url).toBe(`ws://${window.location.host}/api/v1/stations/${STATION.id}/events`);
    expect(liveStatus()).toContain("Connecting…");

    await goLive(socket);
    expect(liveStatus()).toContain("Connected");
    expect(liveStatus()).toContain("delivery is not confirmed");
    expect(JSON.parse(socket.sent[0])).toEqual({ type: "authenticate", token: TOKEN });
    // The queue is reloaded once subscribed.
    expect(await screen.findByRole("button", { name: "Refresh" })).toBeTruthy();
    expect(calls.filter((call) => call.key === QUEUE)).toHaveLength(2);

    act(() => socket.receive(statusChanged("e1", pasta, "PENDING", "IN_PROGRESS")));
    expect(liveStatus()).toMatch(/^Live\./);
    const preparing = screen.getByRole("region", { name: "Preparing" });
    expect(within(preparing).getByRole("button", { name: "Ready: 1× Pasta" })).toBeTruthy();

    act(() => socket.receive(statusChanged("e2", pasta, "IN_PROGRESS", "READY")));
    act(() => socket.receive(statusChanged("e3", pasta, "READY", "SERVED")));
    expect(screen.getByText("All caught up")).toBeTruthy();

    expect(calls.some((call) => call.init?.method === "POST")).toBe(false);
  });

  it("does not crash on an unknown event version and reloads the queue instead", async () => {
    const pasta = queueItem({ product_name: "Pasta" });
    const { socket, calls } = await openKitchen([pasta]);
    await goLive(socket);
    expect(await screen.findByRole("button", { name: "Refresh" })).toBeTruthy();

    await act(async () => {
      socket.receive({ ...statusChanged("e9", pasta, "PENDING", "IN_PROGRESS"), schema_version: 42 });
    });
    expect(await screen.findByRole("button", { name: "Refresh" })).toBeTruthy();
    expect(calls.filter((call) => call.key === QUEUE)).toHaveLength(3);
    expect(screen.getByText("Pasta")).toBeTruthy();
  });

  it("shows reconnecting after a drop and keeps the queue on screen", async () => {
    const { socket } = await openKitchen();
    await goLive(socket);

    act(() => socket.serverClose(1006));
    expect(liveStatus()).toContain("Reconnecting…");
    expect(screen.getByText("Pasta")).toBeTruthy();
  });

  it("reconnects at once when the network returns or the display becomes visible", async () => {
    const { socket } = await openKitchen();
    await goLive(socket);
    // Past the minimum gap between connection attempts.
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 10_000);

    act(() => socket.serverClose(1006));
    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    expect(FakeWebSocket.instances).toHaveLength(2);

    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 20_000);
    act(() => FakeWebSocket.latest().serverClose(1006));
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(FakeWebSocket.instances).toHaveLength(2);
    visibility.mockReturnValue("visible");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(FakeWebSocket.instances).toHaveLength(3);
    expect(liveStatus()).toContain("Reconnecting…");
  });

  it("stops listening for network and visibility changes when the queue closes", async () => {
    const { socket } = await openKitchen();
    await goLive(socket);
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 10_000);
    act(() => socket.serverClose(1006));

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    act(() => {
      window.dispatchEvent(new Event("online"));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("stops live updates for a forbidden station but keeps manual refresh", async () => {
    const { socket } = await openKitchen();
    await goLive(socket);

    act(() => socket.serverClose(4403, "STATION_ACCESS_DENIED"));
    expect(liveStatus()).toContain("Live updates off");
    expect(liveStatus()).toContain("No access to this station's live updates.");
    expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("returns to sign-in when the stream rejects the token", async () => {
    const { socket } = await openKitchen();
    await goLive(socket);

    act(() => socket.serverClose(4401, "NOT_AUTHENTICATED"));
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.getByText("Your session has expired. Please sign in again.")).toBeTruthy();
    expect(window.sessionStorage.getItem("restaurantos.accessToken")).toBeNull();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("closes the stream on sign out", async () => {
    const { socket } = await openKitchen();
    await goLive(socket);

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(socket.closedBy).toBe(1000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("closes the old station's stream and opens one for the new station", async () => {
    writeToken(TOKEN);
    mockApi({
      "GET /api/v1/me": [jsonResponse({ ...KITCHEN_USER, stations: null })],
      "GET /api/v1/stations": [jsonResponse([STATION, BAR])],
      [QUEUE]: [jsonResponse([queueItem({ product_name: "Pasta" })])],
      [BAR_QUEUE]: [jsonResponse([queueItem({ product_name: "Lemonade", station_id: BAR.id })])],
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /Kitchen/ }));
    expect(await screen.findByText("Pasta")).toBeTruthy();
    const kitchen = FakeWebSocket.latest();
    await goLive(kitchen);

    fireEvent.click(screen.getByRole("button", { name: "Change station" }));
    expect(kitchen.closedBy).toBe(1000);

    fireEvent.click(await screen.findByRole("button", { name: /Bar/ }));
    expect(await screen.findByText("Lemonade")).toBeTruthy();
    const bar = FakeWebSocket.latest();
    expect(bar).not.toBe(kitchen);
    expect(bar.url).toContain(`/stations/${BAR.id}/events`);

    // A late message on the closed stream changes nothing.
    act(() => kitchen.receive({ type: "resync_required", reason: "UNSUPPORTED_EVENT" }));
    expect(screen.getByText("Lemonade")).toBeTruthy();
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});
