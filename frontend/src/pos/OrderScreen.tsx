import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { isApiError, newIdempotencyKey } from "../api/client";
import {
  addOrderItem,
  cancelOrderItem,
  getMenu,
  getOrder,
  sendOrder,
  updateOrderItem,
} from "../api/endpoints";
import { Permission, type CurrentUser, type Menu, type MenuProduct, type OrderDetail, type OrderItem } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import type { Load } from "../kds/stationLive";
import { ErrorState, LoadingState } from "../ui/States";
import { MenuPanel, type ProductChoice } from "./MenuPanel";
import { OrderPanel } from "./OrderPanel";
import { describeDestinations, itemName, orderSections, productIndex, roundDestinations } from "./order";
import type { OpenOrder } from "./PosScreen";

// Kitchen and bar progress shows up without a manual refresh.
const REFRESH_INTERVAL_MS = 15_000;

export const REMOVE_REASON = "Removed before sending";

interface Notice {
  kind: "info" | "error" | "success";
  text: string;
}

interface OrderScreenProps {
  token: string;
  user: CurrentUser;
  open: OpenOrder;
  onLeave(): void;
}

export function OrderScreen({ token, user, open, onLeave }: OrderScreenProps) {
  const { handleAuthError } = useAuth();
  const [menu, setMenu] = useState<Load<Menu>>({ status: "loading" });
  const [order, setOrder] = useState<Load<OrderDetail>>({ status: "loading" });
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busyProducts, setBusyProducts] = useState<ReadonlySet<string>>(new Set());
  const [busyItems, setBusyItems] = useState<ReadonlySet<string>>(new Set());
  const [sending, setSending] = useState(false);
  const [attempt, setAttempt] = useState(0);
  // The key of a send whose outcome is unknown (the connection dropped):
  // tapping Send again reuses it, so the round cannot go out twice.
  const sendKey = useRef<string | null>(null);
  const busy = useRef(0);

  const reloadOrder = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const data = await getOrder(token, open.orderId, signal);
        setOrder({ status: "loaded", data });
      } catch (error) {
        if (signal?.aborted || handleAuthError(error)) return;
        setOrder((current) =>
          current.status === "loaded" && !(isApiError(error) && error.status === 404)
            ? current
            : { status: "error", error: error as Error },
        );
      }
    },
    [token, open.orderId, handleAuthError],
  );

  useEffect(() => {
    const controller = new AbortController();
    setMenu({ status: "loading" });
    getMenu(token, controller.signal).then(
      (data) => setMenu({ status: "loaded", data }),
      (error: Error) => {
        if (controller.signal.aborted || handleAuthError(error)) return;
        setMenu({ status: "error", error });
      },
    );
    void reloadOrder(controller.signal);
    return () => controller.abort();
  }, [token, reloadOrder, handleAuthError, attempt]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      // Never overwrite the order while a change of it is in flight.
      if (busy.current === 0) void reloadOrder(controller.signal);
    }, REFRESH_INTERVAL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [reloadOrder]);

  const products = useMemo(
    () => (menu.status === "loaded" ? productIndex(menu.data) : new Map<string, MenuProduct>()),
    [menu],
  );

  /** Runs one change of the order, then shows the order as it now is. */
  async function change(run: () => Promise<unknown>, success: string | null, failure: string) {
    busy.current += 1;
    try {
      await run();
      setNotice(success ? { kind: "success", text: success } : null);
    } catch (error) {
      if (handleAuthError(error)) return;
      setNotice({ kind: "error", text: `${failure}: ${messageOf(error)}` });
    } finally {
      busy.current -= 1;
    }
    await reloadOrder();
  }

  function track(setter: typeof setBusyItems, id: string, on: boolean) {
    setter((current) => {
      const next = new Set(current);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function addProduct(product: MenuProduct, choice: ProductChoice) {
    track(setBusyProducts, product.id, true);
    await change(
      () =>
        addOrderItem(
          token,
          open.orderId,
          {
            product_id: product.id,
            quantity: choice.quantity,
            modifier_ids: choice.modifierIds,
            notes: choice.notes,
          },
          newIdempotencyKey(),
        ),
      `Added ${choice.quantity}× ${product.name}`,
      `${product.name} was not added`,
    );
    track(setBusyProducts, product.id, false);
  }

  async function changeItem(item: OrderItem, run: () => Promise<unknown>, failure: string) {
    track(setBusyItems, item.id, true);
    await change(run, null, failure);
    track(setBusyItems, item.id, false);
  }

  async function send(current: OrderDetail) {
    const { pending } = orderSections(current);
    const { stations } = roundDestinations(pending, products);
    sendKey.current ??= newIdempotencyKey();
    setSending(true);
    busy.current += 1;
    setNotice(null);
    try {
      const sent = await sendOrder(token, current.id, sendKey.current);
      sendKey.current = null;
      setOrder({ status: "loaded", data: sent });
      setNotice({
        kind: "success",
        text: `Round sent: ${describeDestinations(stations)}.`,
      });
    } catch (error) {
      if (handleAuthError(error)) return;
      if (isApiError(error) && error.isNetworkError) {
        setNotice({
          kind: "error",
          text: "The connection dropped while sending, so it is unknown whether the round went out. Tap Send again: it will not be sent twice.",
        });
      } else {
        sendKey.current = null;
        setNotice({ kind: "error", text: `The round was not sent: ${messageOf(error)}` });
      }
      await reloadOrder();
    } finally {
      busy.current -= 1;
      setSending(false);
    }
  }

  if (menu.status === "loading" || order.status === "loading") {
    return <LoadingState text={`Loading table ${open.tableNumber}…`} />;
  }
  if (order.status === "error") {
    const missing = isApiError(order.error) && order.error.status === 404;
    return (
      <ErrorState
        title={missing ? "This order no longer exists" : "The order could not be loaded"}
        error={order.error}
        onRetry={missing ? undefined : () => setAttempt((n) => n + 1)}
        secondary={{ label: "Back to tables", onClick: onLeave }}
      />
    );
  }
  if (menu.status === "error") {
    return (
      <ErrorState
        title="The menu could not be loaded"
        error={menu.error}
        onRetry={() => setAttempt((n) => n + 1)}
        secondary={{ label: "Back to tables", onClick: onLeave }}
      />
    );
  }

  const current = order.data;
  const readOnlyReason = readOnly(current, user);

  return (
    <main className="pos pos--order">
      {notice && (
        <div
          className={`banner banner--${notice.kind}`}
          role={notice.kind === "error" ? "alert" : "status"}
        >
          <span>{notice.text}</span>
          <button type="button" className="banner__close" aria-label="Dismiss" onClick={() => setNotice(null)}>
            ×
          </button>
        </div>
      )}
      <div className="pos-order">
        <MenuPanel
          menu={menu.data}
          busyProductIds={busyProducts}
          disabled={Boolean(readOnlyReason) || sending}
          onAdd={(product, choice) => void addProduct(product, choice)}
        />
        <OrderPanel
          order={current}
          products={products}
          currency={menu.data.currency}
          readOnlyReason={readOnlyReason}
          busyItemIds={busyItems}
          sending={sending}
          onQuantity={(item, quantity) =>
            void changeItem(
              item,
              () => updateOrderItem(token, current.id, item.id, { quantity }, newIdempotencyKey()),
              `${itemName(item, products)} was not changed`,
            )
          }
          onNotes={(item, notes) =>
            void changeItem(
              item,
              () => updateOrderItem(token, current.id, item.id, { notes }, newIdempotencyKey()),
              `The note for ${itemName(item, products)} was not saved`,
            )
          }
          onRemove={(item) =>
            void changeItem(
              item,
              () => cancelOrderItem(token, current.id, item.id, REMOVE_REASON, newIdempotencyKey()),
              `${itemName(item, products)} was not removed`,
            )
          }
          onSend={() => void send(current)}
        />
      </div>
    </main>
  );
}

/** Why this user cannot change the order here, or null. */
export function readOnly(order: OrderDetail, user: CurrentUser): string | null {
  const permissions = new Set(user.permissions);
  if (order.status === "CLOSED") return "This order is paid and closed.";
  if (order.status === "CANCELLED") return "This order was cancelled.";
  if (!permissions.has(Permission.OrdersAddItems) || !permissions.has(Permission.OrdersSend)) {
    return "You can view this order but not add to it.";
  }
  if (order.user_id !== user.id && !permissions.has(Permission.OrdersManageAll)) {
    return "Another waiter opened this order; only they or a manager can change it.";
  }
  return null;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}
