import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { queueItem } from "../test/fixtures";
import { QueueBoard } from "./QueueBoard";

const ALL = new Set(["preparations:view", "preparations:update", "preparations:serve"]);
const NOW = new Date("2026-10-22T18:25:00Z");

function renderBoard(props: Partial<Parameters<typeof QueueBoard>[0]> = {}) {
  const onAction = vi.fn();
  render(
    <QueueBoard
      items={[]}
      now={NOW}
      permissions={ALL}
      pendingIds={new Set()}
      onAction={onAction}
      {...props}
    />,
  );
  return { onAction };
}

function column(name: string) {
  return screen.getByRole("region", { name });
}

describe("QueueBoard", () => {
  it("renders tickets per order with table, items, modifiers and times", () => {
    const burger = queueItem({
      order_id: "order-a",
      table_number: 7,
      table_name: "Window",
      product_name: "Burger",
      quantity: 2,
      created_at: "2026-10-22T18:20:00",
      modifiers: [
        { modifier_id: "m1", name: "Extra cheese" },
        { modifier_id: "m2", name: "No onions" },
      ],
    });
    const fries = queueItem({ order_id: "order-a", table_number: 7, table_name: "Window", product_name: "Fries" });
    const soup = queueItem({
      order_id: "order-b",
      table_number: 3,
      table_name: null,
      product_name: "Soup",
      status: "IN_PROGRESS",
      created_at: "2026-10-22T18:00:00",
      started_at: "2026-10-22T18:05:00",
    });

    renderBoard({ items: [burger, fries, soup] });

    const pending = column("New");
    const ticket = within(pending).getByRole("article", { name: /Table 7/ });
    expect(within(ticket).getByText("Window")).toBeTruthy();
    expect(within(ticket).getByText("2×")).toBeTruthy();
    expect(within(ticket).getByText("Burger")).toBeTruthy();
    expect(within(ticket).getByText("Fries")).toBeTruthy();
    expect(within(ticket).getByText("Extra cheese")).toBeTruthy();
    expect(within(ticket).getByText("No onions")).toBeTruthy();
    expect(within(ticket).getByText("5 min")).toBeTruthy();
    expect(within(pending).getByLabelText("2 items")).toBeTruthy();

    const preparing = column("Preparing");
    const soupTicket = within(preparing).getByRole("article", { name: /Table 3/ });
    expect(within(soupTicket).getByText("25 min")).toBeTruthy();
    expect(soupTicket.className).toContain("ticket--late");
    expect(within(soupTicket).getByText(/^Started /)).toBeTruthy();

    expect(within(column("Ready")).getByText("Nothing here")).toBeTruthy();
  });

  it("marks tickets of fully paid orders and keeps their actions", () => {
    const paid = queueItem({
      order_id: "order-paid",
      table_number: 3,
      status: "READY",
      product_name: "Burger",
      order_status: "CLOSED",
      bill_status: "PAID",
    });
    const unpaid = queueItem({ order_id: "order-open", table_number: 4, bill_status: "OPEN" });
    renderBoard({ items: [paid, unpaid] });

    const paidTicket = screen.getByRole("article", { name: /Table 3/ });
    expect(within(paidTicket).getByText("Paid")).toBeTruthy();
    expect(within(paidTicket).getByRole("button", { name: "Serve: 1× Burger" })).toBeTruthy();
    expect(within(screen.getByRole("article", { name: /Table 4/ })).queryByText("Paid")).toBeNull();
  });

  it("shows the age of a days-old paid ticket in days and hours", () => {
    const stale = queueItem({
      order_id: "order-stale",
      table_number: 5,
      status: "READY",
      order_status: "CLOSED",
      bill_status: "PAID",
      // 848 h 34 min before NOW.
      created_at: "2026-09-17T09:51:00",
    });
    renderBoard({ items: [stale] });

    const ticket = screen.getByRole("article", { name: /Table 5/ });
    expect(within(ticket).getByText("Paid")).toBeTruthy();
    expect(within(ticket).getByText("35 d 8 h")).toBeTruthy();
    expect(ticket.className).toContain("ticket--late");
  });

  it("offers the next action for each status", () => {
    const pending = queueItem({ status: "PENDING", product_name: "Tea" });
    const cooking = queueItem({ status: "IN_PROGRESS", product_name: "Pasta" });
    const ready = queueItem({ status: "READY", product_name: "Salad", ready_at: "2026-10-22T18:10:00" });
    const { onAction } = renderBoard({ items: [pending, cooking, ready] });

    fireEvent.click(screen.getByRole("button", { name: "Start: 1× Tea" }));
    fireEvent.click(screen.getByRole("button", { name: "Ready: 1× Pasta" }));
    fireEvent.click(screen.getByRole("button", { name: "Serve: 1× Salad" }));

    expect(onAction.mock.calls.map(([item]) => item.id)).toEqual([pending.id, cooking.id, ready.id]);
  });

  it("hides actions the user lacks permission for", () => {
    renderBoard({
      items: [queueItem({ status: "PENDING", product_name: "Tea" }), queueItem({ status: "READY", product_name: "Salad" })],
      permissions: new Set(["preparations:view", "preparations:serve"]),
    });

    expect(screen.queryByRole("button", { name: /Start/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Serve: 1× Salad" })).toBeTruthy();
  });

  it("disables the button of an item whose action is in flight", () => {
    const item = queueItem({ product_name: "Tea" });
    renderBoard({ items: [item], pendingIds: new Set([item.id]) });

    expect((screen.getByRole("button", { name: "Start: 1× Tea" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the waiter's note on the item", () => {
    renderBoard({ items: [queueItem({ product_name: "Burger", notes: "allergy: nuts" })] });

    expect(screen.getByText("allergy: nuts")).toBeTruthy();
    expect(screen.getByText("allergy: nuts").textContent).toBe("Note: allergy: nuts");
  });
});
