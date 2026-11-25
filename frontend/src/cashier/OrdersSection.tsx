import { useEffect, useState } from "react";

import { cashierMutations, listCashierOrders } from "../api/cashier";
import type { CashierOrder, CashierOrdersView } from "../api/cashierTypes";
import { Permission, type CurrentUser } from "../api/types";
import { hasPermission } from "../auth/permissions";
import { formatClock } from "../kds/time";
import { formatAmount } from "../lib/money";
import { usePageVisible } from "../lib/usePageVisible";
import { ErrorState, LoadingState } from "../ui/States";
import { ForbiddenState } from "./CashierStates";
import type { PendingOperationStore } from "./pendingOperations";
import { Chip, Notice, SectionHeader, type NoticeMessage } from "./ui";
import { useCashierData } from "./useCashierData";
import { failureText, useCashierOperation } from "./useCashierOperation";

const ORDERS_POLL_MS = 20_000;

interface OrdersSectionProps {
  token: string;
  user: CurrentUser;
  store: PendingOperationStore;
  currency: string | null;
  /** Opens a bill in the Payments section. */
  onOpenBill(billId: string): void;
}

/**
 * Orders the cashier settles: every active order with what is still owed
 * (the server's amount due), whether its bill exists, and the action that
 * comes next. "Settled" lists recently paid bills.
 */
export function OrdersSection({ token, user, store, currency, onOpenBill }: OrdersSectionProps) {
  const [view, setView] = useState<CashierOrdersView>("payable");
  const { state, reload } = useCashierData<CashierOrder[]>(`orders:${view}:${token}`, (signal) =>
    listCashierOrders(token, view, signal),
  );
  const runner = useCashierOperation(store, user, token);
  const [notice, setNotice] = useState<NoticeMessage | null>(null);
  const [creatingFor, setCreatingFor] = useState<string | null>(null);
  const visible = usePageVisible();
  const canCreateBill = hasPermission(user, Permission.BillsCreate);
  const canOpenPayments =
    hasPermission(user, Permission.PaymentsCreate) || hasPermission(user, Permission.PaymentsRefund);

  // Waiters send new orders all the time: refresh while the list is shown.
  useEffect(() => {
    if (!visible) return;
    const timer = window.setInterval(reload, ORDERS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [visible, reload]);

  async function createBill(order: CashierOrder) {
    setNotice(null);
    setCreatingFor(order.order_id);
    const outcome = await runner.run(cashierMutations.createBill(order.order_id));
    setCreatingFor(null);
    if (!outcome) return;
    if (outcome.ok) {
      setNotice({ kind: "success", text: `Bill created for ${tableLabel(order)}.` });
      reload();
      if (canOpenPayments) onOpenBill(outcome.result.id);
      return;
    }
    const text = failureText(outcome);
    if (text) setNotice({ kind: "error", text: `Could not create the bill for ${tableLabel(order)}: ${text}` });
    reload();
  }

  let body;
  if (state.status === "loading") body = <LoadingState text="Loading orders…" />;
  else if (state.status === "forbidden") body = <ForbiddenState what="the orders" onRetry={reload} />;
  else if (state.status === "error") body = <ErrorState title="Could not load the orders" error={state.error} onRetry={reload} />;
  else if (state.data.length === 0) {
    body = (
      <section className="state">
        <h3 className="state__title">{view === "payable" ? "No open orders" : "No settled bills yet"}</h3>
        <p className="state__text">
          {view === "payable"
            ? "Orders appear here as soon as a waiter opens a table."
            : "Bills appear here once they are fully paid."}
        </p>
      </section>
    );
  } else {
    body = (
      <>
        {state.refreshError && (
          <div className="banner banner--error" role="status">
            <span>Could not refresh ({state.refreshError.message}). Showing the last known list.</span>
          </div>
        )}
        <div className="cx-table-wrap">
          <table className="cx-table cx-table--rows">
            <thead>
              <tr>
                <th scope="col">Table</th>
                <th scope="col">Status</th>
                <th scope="col">Waiter</th>
                <th scope="col" className="cx-table__num">
                  Items
                </th>
                <th scope="col" className="cx-table__num">
                  Paid
                </th>
                <th scope="col" className="cx-table__num">
                  Due
                </th>
                <th scope="col">
                  <span className="visually-hidden">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {state.data.map((order) => (
                <tr key={order.order_id}>
                  <th scope="row">
                    {tableLabel(order)}
                    <span className="cx-table__sub">
                      {order.guest_count} {order.guest_count === 1 ? "guest" : "guests"} · since {formatClock(order.created_at)}
                    </span>
                  </th>
                  <td>
                    <OrderStatus order={order} />
                  </td>
                  <td>{order.waiter_name ?? "—"}</td>
                  <td className="cx-table__num">{order.items_count}</td>
                  <td className="cx-table__num">{formatAmount(order.paid_total, currency)}</td>
                  <td className="cx-table__num cx-table__strong">{formatAmount(order.amount_due, currency)}</td>
                  <td className="cx-table__action">
                    <OrderAction
                      order={order}
                      canCreateBill={canCreateBill}
                      canOpenPayments={canOpenPayments}
                      creating={creatingFor === order.order_id}
                      busy={runner.busy}
                      onCreateBill={() => void createBill(order)}
                      onOpenBill={onOpenBill}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  }

  return (
    <section className="cx-section" aria-labelledby="cx-orders-title">
      <SectionHeader
        id="cx-orders-title"
        title="Orders"
        meta={state.status === "ready" && state.refreshing ? "Refreshing…" : null}
      />
      <div className="cx-toolbar">
        <div className="cx-segmented cx-segmented--compact" role="radiogroup" aria-label="Orders to show">
          {(
            [
              ["payable", "To pay"],
              ["settled", "Settled"],
            ] as const
          ).map(([value, label]) => (
            <label key={value} className={`cx-option${view === value ? " cx-option--current" : ""}`}>
              <input type="radio" name="orders-view" value={value} checked={view === value} onChange={() => setView(value)} />
              <span className="cx-option__label">{label}</span>
            </label>
          ))}
        </div>
        <button type="button" className="button button--secondary button--small" onClick={reload}>
          Refresh
        </button>
      </div>
      <Notice notice={notice} onClose={() => setNotice(null)} />
      {body}
    </section>
  );
}

export function tableLabel(order: Pick<CashierOrder, "table_number" | "table_name">): string {
  return order.table_name ? `Table ${order.table_number} · ${order.table_name}` : `Table ${order.table_number}`;
}

function OrderStatus({ order }: { order: CashierOrder }) {
  if (order.bill_status === "PAID") return <Chip tone="ok">Paid</Chip>;
  if (order.bill_id) return <Chip tone="warn">Bill open</Chip>;
  if (order.order_status === "SENT") return <Chip tone="off">No bill yet</Chip>;
  return <Chip tone="off">Not sent</Chip>;
}

function OrderAction({
  order,
  canCreateBill,
  canOpenPayments,
  creating,
  busy,
  onCreateBill,
  onOpenBill,
}: {
  order: CashierOrder;
  canCreateBill: boolean;
  canOpenPayments: boolean;
  creating: boolean;
  busy: boolean;
  onCreateBill(): void;
  onOpenBill(billId: string): void;
}) {
  const label = tableLabel(order);
  if (order.bill_id) {
    if (!canOpenPayments) return null;
    const paid = order.bill_status === "PAID";
    return (
      <button
        type="button"
        className={`button button--small ${paid ? "button--secondary" : "button--primary"}`}
        aria-label={`${paid ? "View bill" : "Take payment"}: ${label}`}
        onClick={() => onOpenBill(order.bill_id!)}
      >
        {paid ? "View bill" : "Take payment"}
      </button>
    );
  }
  if (order.order_status !== "SENT") {
    return <span className="cx-note">Waiting for the waiter to send it</span>;
  }
  if (!canCreateBill) return null;
  return (
    <button
      type="button"
      className="button button--primary button--small"
      aria-label={`Create bill: ${label}`}
      disabled={busy}
      onClick={onCreateBill}
    >
      {creating ? "Creating…" : "Create bill"}
    </button>
  );
}
