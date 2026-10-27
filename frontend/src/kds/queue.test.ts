import { describe, expect, it } from "vitest";

import { queueItem } from "../test/fixtures";
import { groupQueue, nextAction } from "./queue";

describe("groupQueue", () => {
  it("groups items by status, then by order, keeping arrival order", () => {
    const a1 = queueItem({ order_id: "order-a", table_number: 1, created_at: "2026-10-22T18:00:00" });
    const b1 = queueItem({ order_id: "order-b", table_number: 2, created_at: "2026-10-22T18:01:00" });
    const a2 = queueItem({ order_id: "order-a", table_number: 1, created_at: "2026-10-22T18:02:00" });
    const a3 = queueItem({ order_id: "order-a", status: "IN_PROGRESS", created_at: "2026-10-22T18:03:00" });
    const b2 = queueItem({ order_id: "order-b", status: "READY" });

    const columns = groupQueue([a1, b1, a2, a3, b2]);

    expect(columns.PENDING.map((t) => t.orderId)).toEqual(["order-a", "order-b"]);
    expect(columns.PENDING[0].items).toEqual([a1, a2]);
    expect(columns.PENDING[0].createdAt).toBe(a1.created_at);
    expect(columns.PENDING[0].tableNumber).toBe(1);
    expect(columns.IN_PROGRESS[0].items).toEqual([a3]);
    expect(columns.READY[0].items).toEqual([b2]);
    expect(columns.PENDING[0].paid).toBe(false);
  });

  it("marks tickets of closed (fully paid) orders", () => {
    const paid = queueItem({ order_id: "order-p", order_status: "CLOSED", bill_status: "PAID" });
    const billed = queueItem({ order_id: "order-b", order_status: "SENT", bill_status: "OPEN" });

    const columns = groupQueue([paid, billed]);

    expect(columns.PENDING.map((t) => [t.orderId, t.paid])).toEqual([
      ["order-p", true],
      ["order-b", false],
    ]);
  });

  it("returns empty columns for an empty queue", () => {
    expect(groupQueue([])).toEqual({ PENDING: [], IN_PROGRESS: [], READY: [] });
  });
});

describe("nextAction", () => {
  it("maps each status to the one transition the backend allows", () => {
    expect(nextAction("PENDING")).toMatchObject({ action: "start", permission: "preparations:update" });
    expect(nextAction("IN_PROGRESS")).toMatchObject({ action: "ready", permission: "preparations:update" });
    expect(nextAction("READY")).toMatchObject({ action: "serve", permission: "preparations:serve" });
  });
});
