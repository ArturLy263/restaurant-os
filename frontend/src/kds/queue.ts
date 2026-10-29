import type { PreparationAction } from "../api/endpoints";
import { Permission, type PreparationQueueItem, type QueueStatus } from "../api/types";

export interface QueueColumn {
  status: QueueStatus;
  title: string;
}

export const QUEUE_COLUMNS: readonly QueueColumn[] = [
  { status: "PENDING", title: "New" },
  { status: "IN_PROGRESS", title: "Preparing" },
  { status: "READY", title: "Ready" },
];

/** The preparations of one order that share a status. */
export interface OrderTicket {
  orderId: string;
  tableNumber: number;
  tableName: string | null;
  // The oldest item's created_at; the ticket's age.
  createdAt: string;
  // The order is CLOSED: fully paid, but these items are not served yet.
  paid: boolean;
  items: PreparationQueueItem[];
}

/**
 * Splits the queue into columns by status and, within a column, into one
 * ticket per order. The backend returns preparations oldest first, so
 * tickets keep the order in which their first item arrived.
 */
export function groupQueue(
  items: readonly PreparationQueueItem[],
): Record<QueueStatus, OrderTicket[]> {
  const columns: Record<QueueStatus, OrderTicket[]> = {
    PENDING: [],
    IN_PROGRESS: [],
    READY: [],
  };
  const tickets = new Map<string, OrderTicket>();

  for (const item of items) {
    const columnTickets = columns[item.status];
    // Unknown statuses are not part of the queue contract; skip them.
    if (!columnTickets) continue;

    const key = `${item.status}:${item.order_id}`;
    let ticket = tickets.get(key);
    if (!ticket) {
      ticket = {
        orderId: item.order_id,
        tableNumber: item.table_number,
        tableName: item.table_name,
        createdAt: item.created_at,
        paid: item.order_status === "CLOSED",
        items: [],
      };
      tickets.set(key, ticket);
      columnTickets.push(ticket);
    }
    ticket.items.push(item);
  }

  return columns;
}

export interface ItemAction {
  action: PreparationAction;
  label: string;
  permission: string;
}

/** The single forward transition the backend accepts from a status. */
export function nextAction(status: QueueStatus): ItemAction {
  switch (status) {
    case "PENDING":
      return { action: "start", label: "Start", permission: Permission.PreparationsUpdate };
    case "IN_PROGRESS":
      return { action: "ready", label: "Ready", permission: Permission.PreparationsUpdate };
    case "READY":
      return { action: "serve", label: "Serve", permission: Permission.PreparationsServe };
  }
}

export function shortOrderId(orderId: string): string {
  return orderId.slice(0, 8).toUpperCase();
}
