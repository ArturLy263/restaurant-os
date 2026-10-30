import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AuthProvider } from "../auth/AuthContext";
import { errorResponse, jsonResponse, mockApi } from "../test/fixtures";
import {
  BEER,
  BURGER,
  MENU,
  ORDER_ID,
  TABLES,
  WAITER,
  order,
  orderItem,
  sentItem,
} from "../test/posFixtures";
import { PosScreen } from "./PosScreen";

const ORDER = `GET /api/v1/orders/${ORDER_ID}`;
const ITEMS = `POST /api/v1/orders/${ORDER_ID}/items`;
const SEND = `POST /api/v1/orders/${ORDER_ID}/send`;

function renderPos(user = WAITER) {
  return render(
    <AuthProvider>
      <PosScreen token="token" user={user} />
    </AuthProvider>,
  );
}

function body(init: RequestInit | undefined): unknown {
  return JSON.parse(String(init?.body));
}

function header(init: RequestInit | undefined, name: string): string | undefined {
  return (init?.headers as Record<string, string> | undefined)?.[name];
}

async function openTableTwo() {
  fireEvent.click(await screen.findByRole("button", { name: /Table 2, Window/ }));
  return screen.findByRole("region", { name: "Order" });
}

describe("Tables", () => {
  it("shows tables with their status and open orders", async () => {
    mockApi({ "GET /api/v1/tables": [jsonResponse(TABLES)] });

    renderPos();

    const free = await screen.findByRole("button", { name: /Table 1: Free — tap to open an order/ });
    const busy = screen.getByRole("button", { name: /Table 2, Window: Order sent · 1 not sent · 2 sent/ });
    const reserved = screen.getByRole("button", { name: /Table 3: Reserved — no open order/ });
    expect(free.hasAttribute("disabled")).toBe(false);
    expect(busy.hasAttribute("disabled")).toBe(false);
    expect(reserved.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("1 of 3 with an open order")).toBeTruthy();
  });

  it("opens an order for a free table with the number of guests", async () => {
    const { calls } = mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      "POST /api/v1/orders": [
        jsonResponse({ ...order([]), table_id: TABLES[0].id, guest_count: 3 }, 201),
      ],
      [ORDER]: [jsonResponse(order([]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
    });

    renderPos();
    fireEvent.click(await screen.findByRole("button", { name: /Table 1:/ }));
    const dialog = screen.getByRole("dialog", { name: "Table 1" });
    fireEvent.click(within(dialog).getByRole("button", { name: "More guests" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Open order" }));

    expect(await screen.findByText(/Tap products in the menu/)).toBeTruthy();
    const create = calls.find((call) => call.key === "POST /api/v1/orders")!;
    expect(body(create.init)).toEqual({ table_id: TABLES[0].id, guest_count: 3 });
    expect(header(create.init, "Idempotency-Key")).toBeTruthy();
    expect(screen.getByText("Table 1")).toBeTruthy();
  });

  it("refreshes the tables when someone else opened the table meanwhile", async () => {
    const taken = { ...TABLES[0], status: "OCCUPIED", active_order: { ...TABLES[1].active_order!, user_id: "someone" } };
    mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES), jsonResponse([taken, ...TABLES.slice(1)])],
      "POST /api/v1/orders": [errorResponse(409, "TABLE_NOT_AVAILABLE", "Table is not available")],
    });

    renderPos();
    fireEvent.click(await screen.findByRole("button", { name: /Table 1:/ }));
    fireEvent.click(screen.getByRole("button", { name: "Open order" }));

    expect(await screen.findByRole("button", { name: /Table 1: Order sent/ })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getAllByText("Other waiter")).toHaveLength(1);
  });

  it("reopens a table's active order with what was sent and its progress", async () => {
    const burger = sentItem({
      product_id: BURGER.id,
      product_name: "Burger",
      quantity: 2,
      total_price: 27,
      modifiers: [{ id: "x", modifier_id: BURGER.modifier_groups[0].modifiers[0].id, name: "Medium", price: 0 }],
      notes: "one without onions",
      preparation: { id: "p1", station_id: "k", station_name: "Kitchen", status: "IN_PROGRESS" },
    });
    const beer = sentItem({ preparation: { id: "p2", station_id: "b", station_name: "Bar", status: "READY" } });
    const unsent = orderItem({ notes: "no ice" });
    mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([burger, beer, unsent]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
    });

    renderPos();
    const panel = await openTableTwo();

    const sent = within(panel).getByRole("region", { name: /Sent/ });
    expect(within(sent).getByText("Preparing")).toBeTruthy();
    expect(within(sent).getByText("Ready")).toBeTruthy();
    expect(within(sent).getByText("Note: one without onions")).toBeTruthy();
    expect(within(sent).getByText("→ Kitchen")).toBeTruthy();
    expect(within(sent).getByText("Medium")).toBeTruthy();
    const round = within(panel).getByRole("region", { name: /Next round/ });
    expect(within(round).getByRole("listitem", { name: "1× Beer, not sent" })).toBeTruthy();
    expect(within(round).getByText("→ Bar")).toBeTruthy();
    // 27.00 + 4.00 + 4.00
    expect(within(panel).getByText(/35[.,]00/)).toBeTruthy();
  });
});

describe("Order builder", () => {
  it("adds a product without options straight away", async () => {
    const { calls } = mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([])), jsonResponse(order([orderItem()]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
      [ITEMS]: [jsonResponse(orderItem(), 201)],
    });

    renderPos();
    await openTableTwo();
    fireEvent.click(screen.getByRole("tab", { name: "Drinks" }));
    fireEvent.click(screen.getByRole("button", { name: /^Beer,/ }));

    expect(await screen.findByRole("listitem", { name: "1× Beer, not sent" })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("Added 1× Beer");
    const add = calls.find((call) => call.key === ITEMS)!;
    expect(body(add.init)).toEqual({ product_id: BEER.id, quantity: 1, modifier_ids: [], notes: null });
    expect(header(add.init, "Idempotency-Key")).toBeTruthy();
  });

  it("shows unavailable products and does not add them", async () => {
    mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
    });

    renderPos();
    await openTableTwo();
    fireEvent.click(screen.getByRole("tab", { name: "Drinks" }));

    const cider = screen.getByRole("button", { name: /Cider, .*Unavailable/ });
    expect(cider.hasAttribute("disabled")).toBe(true);
  });

  it("asks for required options, then adds with modifiers, quantity and a note", async () => {
    const { calls } = mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
      [ITEMS]: [jsonResponse(orderItem({ product_id: BURGER.id }), 201)],
    });

    renderPos();
    await openTableTwo();
    fireEvent.click(screen.getByRole("button", { name: /^Burger,/ }));
    const dialog = screen.getByRole("dialog", { name: "Burger" });

    fireEvent.click(within(dialog).getByRole("button", { name: /^Add/ }));
    expect(within(dialog).getByRole("alert").textContent).toBe("Doneness: Choose one");
    expect(calls.some((call) => call.key === ITEMS)).toBe(false);

    fireEvent.click(within(dialog).getByRole("radio", { name: "Medium" }));
    fireEvent.click(within(dialog).getByRole("radio", { name: "Well done" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Cheese/ }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Bacon/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "More quantity" }));
    fireEvent.change(within(dialog).getByLabelText("Note for the kitchen/bar"), {
      target: { value: "  sauce on the side " },
    });
    // (12.50 + 1.00 + 2.00) × 2
    const add = within(dialog).getByRole("button", { name: /^Add · .*31[.,]00/ });
    fireEvent.click(add);

    await waitFor(() => expect(calls.some((call) => call.key === ITEMS)).toBe(true));
    expect(body(calls.find((call) => call.key === ITEMS)!.init)).toEqual({
      product_id: BURGER.id,
      quantity: 2,
      // A single-choice group keeps the last choice.
      modifier_ids: [
        BURGER.modifier_groups[0].modifiers[1].id,
        BURGER.modifier_groups[1].modifiers[0].id,
        BURGER.modifier_groups[1].modifiers[1].id,
      ],
      notes: "sauce on the side",
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("changes quantities, edits notes and removes unsent items", async () => {
    const beer = orderItem({ quantity: 2, total_price: 8 });
    const { calls } = mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([beer]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
      [`PATCH /api/v1/orders/${ORDER_ID}/items/${beer.id}`]: [jsonResponse(beer)],
      [`POST /api/v1/orders/${ORDER_ID}/items/${beer.id}/cancel`]: [jsonResponse({ ...beer, status: "CANCELLED" })],
    });

    renderPos();
    const panel = await openTableTwo();
    const line = within(panel).getByRole("listitem", { name: "2× Beer, not sent" });

    fireEvent.click(within(line).getByRole("button", { name: "More quantity of beer" }));
    await waitFor(() => expect(calls.filter((call) => call.key.startsWith("PATCH"))).toHaveLength(1));
    expect(body(calls.find((call) => call.key.startsWith("PATCH"))!.init)).toEqual({ quantity: 3 });

    fireEvent.click(await within(line).findByRole("button", { name: "Add note" }));
    fireEvent.change(within(line).getByLabelText("Note for Beer"), { target: { value: "no glass" } });
    fireEvent.click(within(line).getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(calls.filter((call) => call.key.startsWith("PATCH"))).toHaveLength(2));
    expect(body(calls.filter((call) => call.key.startsWith("PATCH"))[1].init)).toEqual({ notes: "no glass" });

    fireEvent.click(await within(line).findByRole("button", { name: "Remove Beer" }));
    await waitFor(() => expect(calls.some((call) => call.key.endsWith("/cancel"))).toBe(true));
    const cancel = calls.find((call) => call.key.endsWith("/cancel"))!;
    expect(body(cancel.init)).toEqual({ reason: "Removed before sending" });
    expect(header(cancel.init, "Idempotency-Key")).toBeTruthy();
  });

  it("keeps an order opened by another waiter read-only", async () => {
    mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([orderItem()], { user_id: "someone-else" }))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
    });

    renderPos();
    const panel = await openTableTwo();

    expect(within(panel).getByText(/Another waiter opened this order/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Drinks" }));
    expect(within(panel).queryByRole("button", { name: /Send round/ })).toBeNull();
    expect(within(panel).queryByRole("button", { name: /Remove/ })).toBeNull();
    expect(screen.getByRole("button", { name: /^Beer,/ }).hasAttribute("disabled")).toBe(true);
  });
});

describe("Sending a round", () => {
  it("sends the pending items once and shows where they went", async () => {
    const burger = orderItem({ product_id: BURGER.id, product_name: "Burger" });
    const beer = orderItem();
    const sent = order([
      sentItem({ ...burger, status: "SENT", preparation: { id: "p1", station_id: "k", station_name: "Kitchen", status: "PENDING" } }),
      sentItem({ ...beer, status: "SENT" }),
    ]);
    const { calls } = mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([burger, beer]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
      [SEND]: [jsonResponse(sent)],
    });

    renderPos();
    const panel = await openTableTwo();
    expect(within(panel).getByText("To 1 to Kitchen, 1 to Bar")).toBeTruthy();
    const button = within(panel).getByRole("button", { name: "Send round · 2 items" });

    fireEvent.click(button);
    fireEvent.click(button);

    expect((await screen.findByRole("status")).textContent).toContain("Round sent: 1 to Kitchen, 1 to Bar.");
    expect(calls.filter((call) => call.key === SEND)).toHaveLength(1);
    expect(header(calls.find((call) => call.key === SEND)!.init, "Idempotency-Key")).toBeTruthy();
    expect(within(panel).getByText("Tap products in the menu to add them to the next round.")).toBeTruthy();
    expect(within(panel).getAllByText("Waiting")).toHaveLength(2);
  });

  it("retries a send with an unknown outcome under the same key", async () => {
    const beer = orderItem();
    const { calls } = mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([beer]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
      [SEND]: [
        () => {
          throw new TypeError("Failed to fetch");
        },
        jsonResponse(order([sentItem({ ...beer, status: "SENT" })])),
      ],
    });

    renderPos();
    const panel = await openTableTwo();
    fireEvent.click(within(panel).getByRole("button", { name: /Send round/ }));

    expect((await screen.findByRole("alert")).textContent).toContain("it will not be sent twice");
    fireEvent.click(await within(panel).findByRole("button", { name: /Send round/ }));
    expect((await screen.findByRole("status")).textContent).toContain("Round sent");

    const keys = calls.filter((call) => call.key === SEND).map((call) => header(call.init, "Idempotency-Key"));
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("shows why a round was refused and keeps the items", async () => {
    const beer = orderItem();
    mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([beer]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
      [SEND]: [errorResponse(409, "STATION_INACTIVE", "Station Bar is inactive")],
    });

    renderPos();
    const panel = await openTableTwo();
    fireEvent.click(within(panel).getByRole("button", { name: /Send round/ }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "The round was not sent: Station Bar is inactive×",
    );
    expect(within(panel).getByRole("listitem", { name: "1× Beer, not sent" })).toBeTruthy();
  });

  it("does not offer to send items without a station", async () => {
    const lemonade = orderItem({ product_id: "unknown-product", product_name: "Lemonade" });
    mockApi({
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      [ORDER]: [jsonResponse(order([lemonade]))],
      "GET /api/v1/menu": [jsonResponse(MENU)],
    });

    renderPos();
    const panel = await openTableTwo();

    expect(within(panel).getByText(/Lemonade cannot be sent: no station/)).toBeTruthy();
    expect(within(panel).getByRole("button", { name: /Send round/ }).hasAttribute("disabled")).toBe(true);
  });
});
