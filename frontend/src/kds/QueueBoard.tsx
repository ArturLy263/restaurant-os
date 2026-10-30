import type { PreparationQueueItem } from "../api/types";
import { QUEUE_COLUMNS, groupQueue, nextAction, shortOrderId, type OrderTicket } from "./queue";
import { formatClock, formatElapsed, minutesSince } from "./time";

// Ticket ages (minutes) from which a ticket is highlighted.
export const WARNING_AFTER_MINUTES = 10;
export const LATE_AFTER_MINUTES = 20;

interface QueueBoardProps {
  items: PreparationQueueItem[];
  now: Date;
  permissions: ReadonlySet<string>;
  pendingIds: ReadonlySet<string>;
  onAction(item: PreparationQueueItem): void;
}

export function QueueBoard({ items, now, permissions, pendingIds, onAction }: QueueBoardProps) {
  const columns = groupQueue(items);

  return (
    <div className="board">
      {QUEUE_COLUMNS.map((column) => {
        const tickets = columns[column.status];
        const count = tickets.reduce((sum, ticket) => sum + ticket.items.length, 0);
        return (
          <section
            key={column.status}
            className={`column column--${column.status.toLowerCase()}`}
            aria-labelledby={`column-${column.status}`}
          >
            <header className="column__header">
              <h2 id={`column-${column.status}`} className="column__title">
                {column.title}
              </h2>
              <span className="column__count" aria-label={`${count} items`}>
                {count}
              </span>
            </header>
            <div className="column__body">
              {tickets.length === 0 ? (
                <p className="column__empty">Nothing here</p>
              ) : (
                tickets.map((ticket) => (
                  <Ticket
                    key={ticket.orderId}
                    ticket={ticket}
                    now={now}
                    permissions={permissions}
                    pendingIds={pendingIds}
                    onAction={onAction}
                  />
                ))
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
}

interface TicketProps {
  ticket: OrderTicket;
  now: Date;
  permissions: ReadonlySet<string>;
  pendingIds: ReadonlySet<string>;
  onAction(item: PreparationQueueItem): void;
}

function Ticket({ ticket, now, permissions, pendingIds, onAction }: TicketProps) {
  const age = minutesSince(ticket.createdAt, now);
  const urgency =
    age >= LATE_AFTER_MINUTES ? "late" : age >= WARNING_AFTER_MINUTES ? "warning" : "normal";

  return (
    <article
      className={`ticket ticket--${urgency}`}
      aria-label={`Table ${ticket.tableNumber}, order ${shortOrderId(ticket.orderId)}`}
    >
      <header className="ticket__header">
        <div>
          <h3 className="ticket__table">
            Table {ticket.tableNumber}
            {ticket.paid && (
              <span className="tag tag--paid" title="Fully paid; not yet served">
                Paid
              </span>
            )}
          </h3>
          {ticket.tableName && <p className="ticket__table-name">{ticket.tableName}</p>}
        </div>
        <div className="ticket__meta">
          <span className="ticket__age" title={`Received ${formatClock(ticket.createdAt)}`}>
            {formatElapsed(age)}
          </span>
          <span className="ticket__order">#{shortOrderId(ticket.orderId)}</span>
        </div>
      </header>

      <ul className="ticket__items">
        {ticket.items.map((item) => (
          <TicketItem
            key={item.id}
            item={item}
            canAct={permissions.has(nextAction(item.status).permission)}
            pending={pendingIds.has(item.id)}
            onAction={onAction}
          />
        ))}
      </ul>
    </article>
  );
}

interface TicketItemProps {
  item: PreparationQueueItem;
  canAct: boolean;
  pending: boolean;
  onAction(item: PreparationQueueItem): void;
}

function TicketItem({ item, canAct, pending, onAction }: TicketItemProps) {
  const action = nextAction(item.status);

  return (
    <li className="item">
      <div className="item__body">
        <p className="item__line">
          <span className="item__quantity">{item.quantity}×</span>
          <span className="item__name">{item.product_name}</span>
        </p>
        {item.modifiers.length > 0 && (
          <ul className="item__modifiers" aria-label="Modifiers">
            {item.modifiers.map((modifier) => (
              <li key={modifier.modifier_id}>{modifier.name}</li>
            ))}
          </ul>
        )}
        {item.notes && (
          <p className="item__notes">
            <span className="visually-hidden">Note: </span>
            {item.notes}
          </p>
        )}
        <p className="item__times">
          <span>Received {formatClock(item.created_at)}</span>
          {item.started_at && <span>Started {formatClock(item.started_at)}</span>}
          {item.ready_at && <span>Ready {formatClock(item.ready_at)}</span>}
        </p>
      </div>
      {canAct && (
        <button
          type="button"
          className={`button button--action button--${action.action}`}
          disabled={pending}
          aria-label={`${action.label}: ${item.quantity}× ${item.product_name}`}
          onClick={() => onAction(item)}
        >
          {pending ? "…" : action.label}
        </button>
      )}
    </li>
  );
}
