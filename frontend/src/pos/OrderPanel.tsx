import { useState } from "react";

import type { MenuProduct, OrderDetail, OrderItem } from "../api/types";
import { shortOrderId } from "../kds/queue";
import { Stepper } from "./controls";
import { formatCents, formatMoney } from "./money";
import {
  PREPARATION_LABELS,
  describeDestinations,
  itemName,
  orderSections,
  orderSubtotalCents,
  roundDestinations,
} from "./order";

interface OrderPanelProps {
  order: OrderDetail;
  products: Map<string, MenuProduct>;
  currency: string;
  // Why the order cannot be changed here; null when it can.
  readOnlyReason: string | null;
  busyItemIds: ReadonlySet<string>;
  sending: boolean;
  onQuantity(item: OrderItem, quantity: number): void;
  onNotes(item: OrderItem, notes: string | null): void;
  onRemove(item: OrderItem): void;
  onSend(): void;
}

export function OrderPanel(props: OrderPanelProps) {
  const { order, products, currency, readOnlyReason, sending, onSend } = props;
  const { pending, sent } = orderSections(order);
  const { stations, unrouted } = roundDestinations(pending, products);
  const canSend = !readOnlyReason && pending.length > 0 && unrouted.length === 0 && !sending;

  return (
    <section className="order" aria-label="Order">
      <header className="order__header">
        <div>
          <h2 className="order__title">Order #{shortOrderId(order.id)}</h2>
          <p className="order__meta">
            {order.guest_count} {order.guest_count === 1 ? "guest" : "guests"} · {statusText(order.status)}
          </p>
        </div>
        <div className="order__total">
          <span className="order__total-label">Subtotal</span>
          <span className="order__total-value">{formatCents(orderSubtotalCents(order), currency)}</span>
        </div>
      </header>

      {readOnlyReason && (
        <div className="banner banner--info" role="status">
          <span>{readOnlyReason}</span>
        </div>
      )}

      <section className="order__section" aria-labelledby="round-title">
        <h3 id="round-title" className="order__section-title">
          Next round <span className="order__count">{pending.length} not sent</span>
        </h3>
        {pending.length === 0 ? (
          <p className="order__empty">
            {readOnlyReason ? "Nothing waiting to be sent." : "Tap products in the menu to add them to the next round."}
          </p>
        ) : (
          <ul className="lines">
            {pending.map((item) => (
              <PendingLine key={item.id} item={item} {...props} />
            ))}
          </ul>
        )}

        {pending.length > 0 && !readOnlyReason && (
          <div className="send">
            {unrouted.length > 0 && (
              <p className="send__warning" role="alert">
                {unrouted.map((item) => itemName(item, products)).join(", ")} cannot be sent: no station. Remove{" "}
                {unrouted.length === 1 ? "it" : "them"} to send the round.
              </p>
            )}
            <button
              type="button"
              className="button button--primary button--block button--send"
              disabled={!canSend}
              onClick={onSend}
            >
              {sending
                ? "Sending…"
                : `Send round · ${pending.length} ${pending.length === 1 ? "item" : "items"}`}
            </button>
            {stations.length > 0 && <p className="send__to">To {describeDestinations(stations)}</p>}
          </div>
        )}
      </section>

      <section className="order__section" aria-labelledby="sent-title">
        <h3 id="sent-title" className="order__section-title">
          Sent <span className="order__count">{sent.length}</span>
        </h3>
        {sent.length === 0 ? (
          <p className="order__empty">Nothing sent to the kitchen or bar yet.</p>
        ) : (
          <ul className="lines">
            {sent.map((item) => (
              <SentLine key={item.id} item={item} products={products} currency={currency} />
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}

function PendingLine({
  item,
  products,
  currency,
  readOnlyReason,
  busyItemIds,
  sending,
  onQuantity,
  onNotes,
  onRemove,
}: OrderPanelProps & { item: OrderItem }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.notes ?? "");
  const busy = busyItemIds.has(item.id) || sending;
  const locked = Boolean(readOnlyReason);
  const name = itemName(item, products);
  const station = products.get(item.product_id)?.station?.name;

  return (
    <li className="line line--pending" aria-label={`${item.quantity}× ${name}, not sent`}>
      <div className="line__main">
        <p className="line__title">
          <span className="line__quantity">{item.quantity}×</span> {name}
        </p>
        <LineDetails item={item} />
        <p className="line__station">{station ? `→ ${station}` : "No station"}</p>
      </div>
      <span className="line__price">{formatMoney(item.total_price, currency)}</span>

      {!locked && (
        <div className="line__controls">
          <Stepper
            label={`Quantity of ${name}`}
            value={item.quantity}
            min={1}
            max={99}
            disabled={busy}
            onChange={(quantity) => onQuantity(item, quantity)}
          />
          <button
            type="button"
            className="button button--secondary button--small"
            disabled={busy}
            onClick={() => {
              setDraft(item.notes ?? "");
              setEditing((current) => !current);
            }}
          >
            {item.notes ? "Edit note" : "Add note"}
          </button>
          <button
            type="button"
            className="button button--danger button--small"
            disabled={busy}
            onClick={() => onRemove(item)}
            aria-label={`Remove ${name}`}
          >
            Remove
          </button>
        </div>
      )}

      {editing && !locked && (
        <form
          className="line__note-editor"
          onSubmit={(event) => {
            event.preventDefault();
            onNotes(item, draft.trim() || null);
            setEditing(false);
          }}
        >
          <label className="field">
            <span className="field__label">Note for {name}</span>
            <textarea
              className="field__input field__textarea"
              value={draft}
              maxLength={500}
              rows={2}
              autoFocus
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <div className="dialog__actions">
            <button type="button" className="button button--secondary button--small" onClick={() => setEditing(false)}>
              Cancel
            </button>
            <button type="submit" className="button button--primary button--small" disabled={busy}>
              Save note
            </button>
          </div>
        </form>
      )}
    </li>
  );
}

function SentLine({
  item,
  products,
  currency,
}: {
  item: OrderItem;
  products: Map<string, MenuProduct>;
  currency: string;
}) {
  const name = itemName(item, products);
  const status = item.status === "CANCELLED" ? "CANCELLED" : (item.preparation?.status ?? "PENDING");

  return (
    <li className={`line line--sent${item.status === "CANCELLED" ? " line--cancelled" : ""}`}>
      <div className="line__main">
        <p className="line__title">
          <span className="line__quantity">{item.quantity}×</span> {name}
        </p>
        <LineDetails item={item} />
        {item.preparation && <p className="line__station">→ {item.preparation.station_name}</p>}
      </div>
      <span className="line__price">{formatMoney(item.total_price, currency)}</span>
      <span className={`prep prep--${status.toLowerCase()}`}>{PREPARATION_LABELS[status]}</span>
    </li>
  );
}

function LineDetails({ item }: { item: OrderItem }) {
  return (
    <>
      {item.modifiers.length > 0 && (
        <p className="line__modifiers">{item.modifiers.map((modifier) => modifier.name).join(", ")}</p>
      )}
      {item.notes && <p className="line__notes">Note: {item.notes}</p>}
    </>
  );
}

function statusText(status: OrderDetail["status"]): string {
  switch (status) {
    case "OPEN":
      return "not sent yet";
    case "SENT":
      return "sent";
    case "CLOSED":
      return "paid and closed";
    case "CANCELLED":
      return "cancelled";
  }
}
