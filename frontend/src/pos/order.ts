import type {
  Menu,
  MenuModifierGroup,
  MenuProduct,
  OrderDetail,
  OrderItem,
  PreparationStatus,
  UnavailableReason,
} from "../api/types";
import { toCents } from "./money";

export interface OrderSections {
  // Added but not sent yet: the next round.
  pending: OrderItem[];
  // With the kitchen or bar (including items cancelled after sending).
  sent: OrderItem[];
}

/** Splits an order into the next round and what was already sent.
 * Items removed before sending are left out. */
export function orderSections(order: OrderDetail): OrderSections {
  const pending: OrderItem[] = [];
  const sent: OrderItem[] = [];
  for (const item of order.items) {
    if (item.status === "ADDED") pending.push(item);
    else if (item.status === "SENT" || item.preparation) sent.push(item);
  }
  return { pending, sent };
}

/** The total of every item not cancelled, in cents. */
export function orderSubtotalCents(order: OrderDetail): number {
  return order.items
    .filter((item) => item.status !== "CANCELLED")
    .reduce((sum, item) => sum + toCents(item.total_price), 0);
}

export function productIndex(menu: Menu): Map<string, MenuProduct> {
  const index = new Map<string, MenuProduct>();
  for (const category of menu.categories) {
    for (const product of category.products) index.set(product.id, product);
  }
  return index;
}

export function itemName(item: OrderItem, products: Map<string, MenuProduct>): string {
  return item.product_name ?? products.get(item.product_id)?.name ?? "Unknown product";
}

export interface Destination {
  name: string;
  items: number;
}

/** Where each pending item will go, per station (by the menu's current
 * routing, as the send uses), in the order first seen. Items without a
 * station are grouped under null. */
export function roundDestinations(
  pending: OrderItem[],
  products: Map<string, MenuProduct>,
): { stations: Destination[]; unrouted: OrderItem[] } {
  const stations = new Map<string, Destination>();
  const unrouted: OrderItem[] = [];
  for (const item of pending) {
    const station = products.get(item.product_id)?.station;
    if (!station) {
      unrouted.push(item);
      continue;
    }
    const entry = stations.get(station.id) ?? { name: station.name, items: 0 };
    entry.items += 1;
    stations.set(station.id, entry);
  }
  return { stations: [...stations.values()], unrouted };
}

export function describeDestinations(destinations: Destination[]): string {
  return destinations
    .map((destination) => `${destination.items} to ${destination.name}`)
    .join(", ");
}

/** Problems with a modifier selection, per group id; empty when valid.
 * Mirrors the backend rules (required groups need at least one). */
export function selectionProblems(
  groups: MenuModifierGroup[],
  selected: ReadonlySet<string>,
): Map<string, string> {
  const problems = new Map<string, string>();
  for (const group of groups) {
    const count = group.modifiers.filter((modifier) => selected.has(modifier.id)).length;
    const min = group.is_required ? Math.max(group.min_selections, 1) : group.min_selections;
    if (count < min) {
      problems.set(group.id, min === 1 ? "Choose one" : `Choose at least ${min}`);
    } else if (group.max_selections !== null && count > group.max_selections) {
      problems.set(group.id, `Choose at most ${group.max_selections}`);
    }
  }
  return problems;
}

/** (product price + selected modifier prices) × quantity, in cents. */
export function lineTotalCents(
  product: MenuProduct,
  selected: ReadonlySet<string>,
  quantity: number,
): number {
  let unit = toCents(product.price);
  for (const group of product.modifier_groups) {
    for (const modifier of group.modifiers) {
      if (selected.has(modifier.id)) unit += toCents(modifier.price_adjustment);
    }
  }
  return unit * quantity;
}

export const UNAVAILABLE_LABELS: Record<UnavailableReason, string> = {
  PRODUCT_INACTIVE: "Unavailable",
  PRODUCT_HAS_NO_STATION: "No station",
  STATION_INACTIVE: "Station closed",
};

export function unavailableLabel(product: MenuProduct): string {
  if (product.unavailable_reason === "STATION_INACTIVE" && product.station) {
    return `${product.station.name} closed`;
  }
  return product.unavailable_reason ? UNAVAILABLE_LABELS[product.unavailable_reason] : "Unavailable";
}

export const PREPARATION_LABELS: Record<PreparationStatus, string> = {
  PENDING: "Waiting",
  IN_PROGRESS: "Preparing",
  READY: "Ready",
  SERVED: "Served",
  CANCELLED: "Cancelled",
};
