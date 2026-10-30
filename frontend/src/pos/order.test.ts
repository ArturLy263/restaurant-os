import { describe, expect, it } from "vitest";

import { BEER, BURGER, CIDER, MENU, order, orderItem, sentItem } from "../test/posFixtures";
import {
  describeDestinations,
  lineTotalCents,
  orderSections,
  orderSubtotalCents,
  productIndex,
  roundDestinations,
  selectionProblems,
  unavailableLabel,
} from "./order";

const [DONENESS, EXTRAS] = BURGER.modifier_groups;
const [MEDIUM, WELL_DONE] = DONENESS.modifiers;
const [CHEESE, BACON] = EXTRAS.modifiers;

describe("orderSections", () => {
  it("separates the next round from what was sent, without removed items", () => {
    const pending = orderItem();
    const sent = sentItem();
    const removed = orderItem({ status: "CANCELLED" });
    const cancelledAfterSending = sentItem({ status: "CANCELLED" });

    const sections = orderSections(order([pending, sent, removed, cancelledAfterSending]));

    expect(sections.pending).toEqual([pending]);
    expect(sections.sent).toEqual([sent, cancelledAfterSending]);
  });

  it("totals items not cancelled, in cents", () => {
    const items = [
      orderItem({ total_price: 13.5 }),
      sentItem({ total_price: 0.1 }),
      sentItem({ total_price: 0.2 }),
      orderItem({ total_price: 99, status: "CANCELLED" }),
    ];

    expect(orderSubtotalCents(order(items))).toBe(1380);
  });
});

describe("roundDestinations", () => {
  it("groups pending items by station and lists items with none", () => {
    const products = productIndex({
      ...MENU,
      categories: [...MENU.categories, { id: "x", name: "X", display_order: 3, products: [{ ...CIDER, id: "lemonade", station: null }] }],
    });
    const items = [
      orderItem({ product_id: BURGER.id }),
      orderItem({ product_id: BEER.id }),
      orderItem({ product_id: BEER.id }),
      orderItem({ product_id: "lemonade" }),
    ];

    const { stations, unrouted } = roundDestinations(items, products);

    expect(stations).toEqual([
      { name: "Kitchen", items: 1 },
      { name: "Bar", items: 2 },
    ]);
    expect(unrouted).toEqual([items[3]]);
    expect(describeDestinations(stations)).toBe("1 to Kitchen, 2 to Bar");
  });
});

describe("modifier selection", () => {
  it("requires required groups and respects maximums", () => {
    const groups = BURGER.modifier_groups;

    expect(selectionProblems(groups, new Set()).get(DONENESS.id)).toBe("Choose one");
    expect(selectionProblems(groups, new Set([MEDIUM.id])).size).toBe(0);
    expect(selectionProblems(groups, new Set([MEDIUM.id, WELL_DONE.id])).get(DONENESS.id)).toBe("Choose at most 1");
    expect(selectionProblems(groups, new Set([MEDIUM.id, CHEESE.id, BACON.id])).size).toBe(0);
  });

  it("prices the line with its modifiers", () => {
    expect(lineTotalCents(BURGER, new Set([MEDIUM.id, CHEESE.id, BACON.id]), 2)).toBe(3100);
    expect(lineTotalCents(BEER, new Set(), 3)).toBe(1200);
  });
});

describe("unavailableLabel", () => {
  it("names the reason", () => {
    expect(unavailableLabel(CIDER)).toBe("Unavailable");
    expect(unavailableLabel({ ...BEER, is_available: false, unavailable_reason: "STATION_INACTIVE" })).toBe("Bar closed");
    expect(unavailableLabel({ ...BEER, station: null, unavailable_reason: "PRODUCT_HAS_NO_STATION" })).toBe("No station");
  });
});
