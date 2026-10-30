import { useState, type ReactNode } from "react";

import type { CurrentUser, DiningTable } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { OrderScreen } from "./OrderScreen";
import { TablesView } from "./TablesView";

const ORDER_KEY = "restaurantos.pos.order";

// The open order screen, remembered across reloads of the tab.
export interface OpenOrder {
  orderId: string;
  tableNumber: number;
  tableName: string | null;
}

interface PosScreenProps {
  token: string;
  user: CurrentUser;
  nav?: ReactNode;
}

export function PosScreen({ token, user, nav }: PosScreenProps) {
  const { logout } = useAuth();
  const [open, setOpen] = useState<OpenOrder | null>(readStoredOrder);

  function openOrder(orderId: string, table: DiningTable) {
    const next = { orderId, tableNumber: table.number, tableName: table.name };
    storeOrder(next);
    setOpen(next);
  }

  function backToTables() {
    storeOrder(null);
    setOpen(null);
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <span className="topbar__logo">RestaurantOS</span>
          {nav}
          {open && (
            <span className="topbar__station">
              Table {open.tableNumber}
              {open.tableName ? ` · ${open.tableName}` : ""}
            </span>
          )}
        </div>
        <div className="topbar__actions">
          {open && (
            <button type="button" className="button button--ghost" onClick={backToTables}>
              ← Tables
            </button>
          )}
          <span className="topbar__user">
            {user.name}
            {user.role && <span className="tag">{user.role.name}</span>}
          </span>
          <button type="button" className="button button--ghost" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      </header>

      {open ? (
        <OrderScreen key={open.orderId} token={token} user={user} open={open} onLeave={backToTables} />
      ) : (
        <TablesView token={token} user={user} onOpenOrder={openOrder} />
      )}
    </div>
  );
}

function readStoredOrder(): OpenOrder | null {
  try {
    const value = window.sessionStorage.getItem(ORDER_KEY);
    if (!value) return null;
    const parsed = JSON.parse(value) as Partial<OpenOrder>;
    return typeof parsed.orderId === "string" && typeof parsed.tableNumber === "number"
      ? { orderId: parsed.orderId, tableNumber: parsed.tableNumber, tableName: parsed.tableName ?? null }
      : null;
  } catch {
    return null;
  }
}

function storeOrder(open: OpenOrder | null): void {
  try {
    if (open) window.sessionStorage.setItem(ORDER_KEY, JSON.stringify(open));
    else window.sessionStorage.removeItem(ORDER_KEY);
  } catch {
    // Not remembered across reloads.
  }
}
