import type {
  CurrentUser,
  DiningTable,
  Menu,
  MenuProduct,
  OrderDetail,
  OrderItem,
} from "../api/types";
import { BAR, STATION } from "./fixtures";

export const WAITER: CurrentUser = {
  id: "9d6f0d8e-0000-4000-8000-0000000000aa",
  name: "Wendy Waiter",
  email: "wendy@example.com",
  restaurant_id: "1f2e3d4c-0000-4000-8000-000000000001",
  role: { id: "aa000000-0000-4000-8000-0000000000aa", name: "WAITER" },
  permissions: [
    "orders:add_items",
    "orders:cancel_items",
    "orders:create",
    "orders:send",
    "orders:view",
    "orders:void",
    "preparations:all_stations",
    "preparations:serve",
  ],
  stations: null,
};

export const ORDER_ID = "order000-0000-4000-8000-0000000000aa";

export const TABLES: DiningTable[] = [
  { id: "table000-0000-4000-8000-000000000001", number: 1, name: null, capacity: 4, status: "AVAILABLE", active_order: null },
  {
    id: "table000-0000-4000-8000-000000000002",
    number: 2,
    name: "Window",
    capacity: 2,
    status: "OCCUPIED",
    active_order: {
      id: ORDER_ID,
      status: "SENT",
      user_id: WAITER.id,
      guest_count: 2,
      created_at: "2026-10-30T18:00:00",
      unsent_items: 1,
      sent_items: 2,
    },
  },
  { id: "table000-0000-4000-8000-000000000003", number: 3, name: null, capacity: 6, status: "RESERVED", active_order: null },
];

const KITCHEN = { id: STATION.id, name: "Kitchen", type: "KITCHEN", is_active: true };
const BAR_STATION = { id: BAR.id, name: "Bar", type: "BAR", is_active: true };

export const BURGER: MenuProduct = {
  id: "prod0000-0000-4000-8000-000000000001",
  name: "Burger",
  description: "Beef, brioche",
  price: "12.50",
  station: KITCHEN,
  is_available: true,
  unavailable_reason: null,
  modifier_groups: [
    {
      id: "group000-0000-4000-8000-000000000001",
      name: "Doneness",
      min_selections: 1,
      max_selections: 1,
      is_required: true,
      modifiers: [
        { id: "mod00000-0000-4000-8000-000000000001", name: "Medium", price_adjustment: "0.00" },
        { id: "mod00000-0000-4000-8000-000000000002", name: "Well done", price_adjustment: "0.00" },
      ],
    },
    {
      id: "group000-0000-4000-8000-000000000002",
      name: "Extras",
      min_selections: 0,
      max_selections: 2,
      is_required: false,
      modifiers: [
        { id: "mod00000-0000-4000-8000-000000000003", name: "Cheese", price_adjustment: "1.00" },
        { id: "mod00000-0000-4000-8000-000000000004", name: "Bacon", price_adjustment: "2.00" },
      ],
    },
  ],
};

export const BEER: MenuProduct = {
  id: "prod0000-0000-4000-8000-000000000002",
  name: "Beer",
  description: null,
  price: "4.00",
  station: BAR_STATION,
  is_available: true,
  unavailable_reason: null,
  modifier_groups: [],
};

export const CIDER: MenuProduct = {
  ...BEER,
  id: "prod0000-0000-4000-8000-000000000003",
  name: "Cider",
  price: "3.50",
  is_available: false,
  unavailable_reason: "PRODUCT_INACTIVE",
};

export const MENU: Menu = {
  currency: "EUR",
  categories: [
    { id: "cat00000-0000-4000-8000-000000000001", name: "Food", display_order: 1, products: [BURGER] },
    { id: "cat00000-0000-4000-8000-000000000002", name: "Drinks", display_order: 2, products: [BEER, CIDER] },
  ],
};

let sequence = 0;

export function orderItem(overrides: Partial<OrderItem> = {}): OrderItem {
  sequence += 1;
  return {
    id: `item0000-0000-4000-8000-${String(sequence).padStart(12, "0")}`,
    order_id: ORDER_ID,
    product_id: BEER.id,
    product_name: "Beer",
    quantity: 1,
    unit_price: 4,
    total_price: 4,
    status: "ADDED",
    created_at: "2026-10-30T18:01:00",
    cancelled_at: null,
    modifiers: [],
    notes: null,
    preparation: null,
    ...overrides,
  };
}

export function order(items: OrderItem[], overrides: Partial<OrderDetail> = {}): OrderDetail {
  return {
    id: ORDER_ID,
    restaurant_id: WAITER.restaurant_id,
    table_id: TABLES[1].id,
    user_id: WAITER.id,
    guest_count: 2,
    status: items.some((item) => item.status === "SENT") ? "SENT" : "OPEN",
    created_at: "2026-10-30T18:00:00",
    items,
    ...overrides,
  };
}

export function sentItem(overrides: Partial<OrderItem> = {}): OrderItem {
  const item = orderItem({ status: "SENT", ...overrides });
  return {
    ...item,
    preparation: item.preparation ?? {
      id: `prep-${item.id}`,
      station_id: BAR.id,
      station_name: "Bar",
      status: "PENDING",
    },
  };
}
