import { describe, expect, it } from "vitest";

import { parseServerMessage, type StationEvent } from "../api/stationEvents";
import { orderSent, statusChanged } from "../test/events";
import { BAR, STATION, queueItem } from "../test/fixtures";
import { applyStationEvent } from "./liveQueue";

function event(message: unknown): StationEvent {
  const parsed = parseServerMessage(JSON.stringify(message));
  if (parsed.type !== "event") throw new Error("not an event");
  return parsed.event;
}

describe("applyStationEvent", () => {
  it("moves a preparation on, stamping the transition with the event time", () => {
    const item = queueItem({ status: "PENDING" });
    const started = applyStationEvent(
      [item],
      event(statusChanged("e1", item, "PENDING", "IN_PROGRESS", { createdAt: "2026-10-22T18:05:00+00:00" })),
      STATION.id,
    );
    expect(started.reload).toBe(false);
    expect(started.items[0]).toMatchObject({ status: "IN_PROGRESS", started_at: "2026-10-22T18:05:00+00:00", ready_at: null });

    const ready = applyStationEvent(started.items, event(statusChanged("e2", item, "IN_PROGRESS", "READY")), STATION.id);
    expect(ready.items[0]).toMatchObject({ status: "READY", started_at: "2026-10-22T18:05:00+00:00" });
    expect(ready.items[0].ready_at).not.toBeNull();
  });

  it("removes served and cancelled preparations (v2: discarded ready dishes)", () => {
    const served = queueItem({ status: "READY" });
    const discarded = queueItem({ status: "READY" });
    const other = queueItem();
    let items = [served, discarded, other];
    items = applyStationEvent(items, event(statusChanged("e1", served, "READY", "SERVED")), STATION.id).items;
    items = applyStationEvent(items, event(statusChanged("e2", discarded, "READY", "CANCELLED", { version: 2 })), STATION.id).items;
    expect(items).toEqual([other]);
  });

  it("ignores events already reflected in the queue or about other preparations", () => {
    const item = queueItem({ status: "IN_PROGRESS" });
    const items = [item];
    // Loaded after the change: previous_status no longer matches.
    expect(applyStationEvent(items, event(statusChanged("e1", item, "PENDING", "IN_PROGRESS")), STATION.id)).toEqual({ items, reload: false });
    // Not in the queue (served, or never this station's).
    const gone = queueItem();
    expect(applyStationEvent(items, event(statusChanged("e2", gone, "PENDING", "IN_PROGRESS")), STATION.id).items).toBe(items);
    // Another station's preparation.
    expect(
      applyStationEvent(items, event(statusChanged("e3", item, "IN_PROGRESS", "READY", { stationId: BAR.id })), STATION.id).items,
    ).toBe(items);
  });

  it("reloads for order.sent only when one of its preparations is missing", () => {
    const present = queueItem();
    const missing = queueItem();
    expect(applyStationEvent([present], event(orderSent("e1", [present])), STATION.id)).toEqual({ items: [present], reload: false });
    expect(applyStationEvent([present], event(orderSent("e2", [present, missing])), STATION.id).reload).toBe(true);
  });
});
