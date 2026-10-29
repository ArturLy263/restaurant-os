import { describe, expect, it } from "vitest";

import { orderSent, resyncRequired, statusChanged } from "../test/events";
import { STATION, queueItem } from "../test/fixtures";
import { parseServerMessage, stationEventsUrl } from "./stationEvents";

const json = (value: unknown) => JSON.stringify(value);

describe("station event messages", () => {
  it("reads subscribed", () => {
    expect(parseServerMessage(json({ type: "subscribed", station_id: STATION.id }))).toEqual({
      type: "subscribed",
      stationId: STATION.id,
    });
  });

  it("reads preparation.status_changed v1 and v2 and order.sent v1", () => {
    const item = queueItem();
    for (const version of [1, 2]) {
      const message = parseServerMessage(json(statusChanged("e1", item, "READY", "CANCELLED", { version })));
      expect(message.type).toBe("event");
      if (message.type !== "event") continue;
      expect(message.event).toMatchObject({
        id: "e1",
        event_type: "preparation.status_changed",
        schema_version: version,
        payload: { preparation_id: item.id, previous_status: "READY", status: "CANCELLED" },
      });
    }

    const sent = parseServerMessage(json(orderSent("e2", [item])));
    expect(sent).toMatchObject({ type: "event", event: { event_type: "order.sent", payload: { items: [{ preparation_id: item.id }] } } });
  });

  it("asks for a resync on resync_required, naming its event", () => {
    expect(parseServerMessage(json(resyncRequired("evt-9")))).toEqual({ type: "resync", eventId: "evt-9" });
  });

  it("treats unknown or untrustworthy messages as a resync instead of guessing", () => {
    const item = queueItem();
    const unknownVersion = { ...statusChanged("e3", item, "PENDING", "IN_PROGRESS"), schema_version: 3 };
    const unknownType = { ...orderSent("e4", [item]), event_type: "order.renamed" };
    const badStatus = statusChanged("e5", item, "PENDING", "IN_PROGRESS");
    (badStatus.payload as Record<string, unknown>).status = "BURNT";
    const stringVersion = { ...orderSent("e6", [item]), schema_version: "1" };

    for (const raw of [json(unknownVersion), json(unknownType), json(badStatus), json(stringVersion)]) {
      expect(parseServerMessage(raw).type).toBe("resync");
    }
    for (const raw of ["not json", json([1, 2]), json(null), json({ type: "pong" }), undefined, new ArrayBuffer(2)]) {
      expect(parseServerMessage(raw)).toEqual({ type: "resync", eventId: null });
    }
  });

  it("builds a same-origin URL without the token", () => {
    expect(stationEventsUrl("abc", { protocol: "http:", host: "localhost:5173" } as Location)).toBe(
      "ws://localhost:5173/api/v1/stations/abc/events",
    );
    expect(stationEventsUrl("a/b", { protocol: "https:", host: "kds.example" } as Location)).toBe(
      "wss://kds.example/api/v1/stations/a%2Fb/events",
    );
  });
});
