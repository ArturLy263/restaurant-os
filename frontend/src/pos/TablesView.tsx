import { useCallback, useEffect, useRef, useState } from "react";

import { isApiError, newIdempotencyKey } from "../api/client";
import { createOrder, getTables } from "../api/endpoints";
import { Permission, type CurrentUser, type DiningTable } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import type { Load } from "../kds/stationLive";
import { ErrorState, LoadingState } from "../ui/States";
import { Dialog, Stepper } from "./controls";

// Other waiters open and close tables; the list refreshes itself.
const REFRESH_INTERVAL_MS = 20_000;

interface TablesViewProps {
  token: string;
  user: CurrentUser;
  onOpenOrder(orderId: string, table: DiningTable): void;
}

export function TablesView({ token, user, onOpenOrder }: TablesViewProps) {
  const { handleAuthError } = useAuth();
  const [tables, setTables] = useState<Load<DiningTable[]>>({ status: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const [choosing, setChoosing] = useState<DiningTable | null>(null);
  const canOpen = user.permissions.includes(Permission.OrdersCreate);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setRefreshing(true);
      try {
        const data = await getTables(token, signal);
        setTables({ status: "loaded", data });
      } catch (error) {
        if (signal?.aborted || handleAuthError(error)) return;
        // Keep a list already shown; only a first load fails the view.
        setTables((current) => (current.status === "loaded" ? current : { status: "error", error: error as Error }));
      } finally {
        if (!signal?.aborted) setRefreshing(false);
      }
    },
    [token, handleAuthError],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    const timer = window.setInterval(() => void load(controller.signal), REFRESH_INTERVAL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [load]);

  if (tables.status === "loading") return <LoadingState text="Loading tables…" />;
  if (tables.status === "error") {
    const forbidden = isApiError(tables.error) && tables.error.isForbidden;
    return (
      <ErrorState
        title={forbidden ? "No access to tables" : "Tables could not be loaded"}
        error={tables.error}
        onRetry={forbidden ? undefined : () => void load()}
      />
    );
  }

  return (
    <main className="pos">
      <div className="queue__toolbar">
        <h1 className="queue__title">Tables</h1>
        <span className="queue__summary">
          {tables.data.filter((table) => table.active_order).length} of {tables.data.length} with an open order
        </span>
        <button type="button" className="button button--secondary" onClick={() => void load()} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {tables.data.length === 0 ? (
        <section className="state">
          <h2 className="state__title">No tables</h2>
          <p className="state__text">This restaurant has no active tables yet.</p>
        </section>
      ) : (
        <ul className="tables" aria-label="Tables">
          {tables.data.map((table) => (
            <li key={table.id}>
              <TableCard
                table={table}
                userId={user.id}
                canOpen={canOpen}
                onSelect={() => {
                  if (table.active_order) onOpenOrder(table.active_order.id, table);
                  else setChoosing(table);
                }}
              />
            </li>
          ))}
        </ul>
      )}

      {choosing && (
        <OpenOrderDialog
          token={token}
          table={choosing}
          onCancel={() => setChoosing(null)}
          onOpened={(orderId) => {
            setChoosing(null);
            onOpenOrder(orderId, choosing);
          }}
          onTableChanged={() => {
            setChoosing(null);
            void load();
          }}
        />
      )}
    </main>
  );
}

interface TableCardProps {
  table: DiningTable;
  userId: string;
  canOpen: boolean;
  onSelect(): void;
}

function TableCard({ table, userId, canOpen, onSelect }: TableCardProps) {
  const order = table.active_order;
  // A table can be OCCUPIED (or RESERVED, ...) without an order this list
  // can open; only free tables take a new one.
  const available = table.status === "AVAILABLE";
  const disabled = !order && (!available || !canOpen);

  let detail: string;
  if (order) {
    const parts = [order.status === "OPEN" ? "Order open" : "Order sent"];
    if (order.unsent_items > 0) parts.push(`${order.unsent_items} not sent`);
    if (order.sent_items > 0) parts.push(`${order.sent_items} sent`);
    detail = parts.join(" · ");
  } else if (available) {
    detail = canOpen ? "Free — tap to open an order" : "Free";
  } else {
    detail = `${statusLabel(table.status)} — no open order`;
  }

  return (
    <button
      type="button"
      className={`table-card table-card--${order ? "busy" : available ? "free" : "blocked"}`}
      disabled={disabled}
      onClick={onSelect}
      aria-label={`Table ${table.number}${table.name ? `, ${table.name}` : ""}: ${detail}`}
    >
      <span className="table-card__number">{table.number}</span>
      {table.name && <span className="table-card__name">{table.name}</span>}
      <span className="table-card__seats">{table.capacity} seats</span>
      <span className="table-card__status">{statusLabel(table.status)}</span>
      <span className="table-card__detail">{detail}</span>
      {order && order.user_id !== userId && <span className="tag tag--muted">Other waiter</span>}
    </button>
  );
}

function statusLabel(status: string): string {
  return status.charAt(0) + status.slice(1).toLowerCase();
}

interface OpenOrderDialogProps {
  token: string;
  table: DiningTable;
  onCancel(): void;
  onOpened(orderId: string): void;
  onTableChanged(): void;
}

function OpenOrderDialog({ token, table, onCancel, onOpened, onTableChanged }: OpenOrderDialogProps) {
  const { handleAuthError } = useAuth();
  const [guests, setGuests] = useState(Math.min(2, Math.max(1, table.capacity)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Kept across retries after a lost connection, so a retry cannot open a
  // second order.
  const key = useRef<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    key.current ??= newIdempotencyKey();
    try {
      const order = await createOrder(token, table.id, guests, key.current);
      onOpened(order.id);
    } catch (failure) {
      if (handleAuthError(failure)) return;
      if (isApiError(failure) && failure.isConflict) {
        // Someone else opened it meanwhile; the list shows their order.
        onTableChanged();
        return;
      }
      if (!(isApiError(failure) && failure.isNetworkError)) key.current = null;
      setError(failure instanceof Error ? failure.message : "The order could not be opened.");
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Table ${table.number}${table.name ? ` · ${table.name}` : ""}`} onClose={busy ? undefined : onCancel}>
      <p className="dialog__text">How many guests?</p>
      <Stepper
        label="Guests"
        value={guests}
        min={1}
        max={99}
        onChange={(value) => {
          setGuests(value);
          key.current = null;
        }}
        disabled={busy}
      />
      {error && (
        <p className="dialog__error" role="alert">
          {error}
        </p>
      )}
      <div className="dialog__actions">
        <button type="button" className="button button--secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="button button--primary" onClick={() => void submit()} disabled={busy}>
          {busy ? "Opening…" : "Open order"}
        </button>
      </div>
    </Dialog>
  );
}
