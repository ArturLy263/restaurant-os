import { useId, useState, useSyncExternalStore } from "react";

import type { CashierOperationType } from "../api/cashier";
import type { CurrentUser } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { formatClock } from "../kds/time";
import {
  executeOperation,
  mayCheckOthersOperations,
  operationsOf,
  type PendingOperation,
  type PendingOperationStore,
} from "./pendingOperations";

const TYPE_LABELS: Record<CashierOperationType, string> = {
  "business_day.open": "Open business day",
  "business_day.close": "Close business day",
  "cash_shift.open": "Open cash shift",
  "cash_shift.close": "Close cash shift",
  "cash_shift.handover": "Hand over cash shift",
  "cash_shift.opening_correction": "Correct opening cash",
  "cash_movement.create": "Cash in / out",
  "bill.create": "Create bill",
  "bill.cancel_item": "Cancel billed item",
  "order.void": "Void order",
  "payment.create": "Payment",
  "payment.refund": "Refund",
};

interface PendingOperationsBannerProps {
  store: PendingOperationStore;
  user: CurrentUser;
  token: string;
}

/**
 * Warns while cashier operations are unresolved on this device and lets
 * their owner retry them with the same key and body. Other employees'
 * operations show only what, when and who — never amounts or server
 * messages.
 */
export function PendingOperationsBanner({ store, user, token }: PendingOperationsBannerProps) {
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const { own, others, history } = operationsOf(snapshot, user);
  // Managers may record a check of other employees' unknown operations
  // (the store verifies the permission again).
  const mayCheckOthers = mayCheckOthersOperations(user);

  const warnings: string[] = [];
  if (!snapshot.storageAvailable) {
    warnings.push(
      "This browser cannot store pending operations. If the page is closed or reloaded while an operation runs, its outcome cannot be recovered here.",
    );
  } else if (snapshot.unsaved) {
    warnings.push(
      "Some operations could not be saved on this device (storage is full or blocked) and exist only in this tab. Do not close or reload it until they are resolved.",
    );
  }
  if (snapshot.recoveredCorruptData) {
    warnings.push("Unreadable pending-operation data was found on this device and kept unchanged. Tell a manager.");
  }

  const ownHistory = history.filter((op) => op.userId === user.id);
  if (own.length === 0 && others.length === 0 && ownHistory.length === 0 && warnings.length === 0) return null;

  const inDoubt = own.filter((op) => store.effectiveStatus(op) === "unknown").length;

  return (
    <section className="cx-pending" aria-label="Unresolved operations">
      {warnings.map((warning) => (
        <p key={warning} className="cx-pending__warning" role="alert">
          {warning}
        </p>
      ))}
      {(own.length > 0 || others.length > 0) && (
        <details className="cx-pending__details" open={inDoubt > 0}>
          <summary className="cx-pending__summary">{summaryText(own.length, inDoubt, others.length)}</summary>
          <p className="cx-card__hint">
            Do not repeat these by hand: retrying here sends the same request again, and the server will not apply it
            twice.
          </p>
          <ul className="cx-pending__list">
            {own.map((op) => (
              <OwnRow key={op.id} op={op} store={store} user={user} token={token} />
            ))}
            {others.map((op) => (
              <OtherRow key={op.id} op={op} store={store} user={user} mayCheck={mayCheckOthers} />
            ))}
          </ul>
        </details>
      )}
      {ownHistory.length > 0 && (
        <details className="cx-pending__details">
          <summary className="cx-pending__summary">Recent on this device ({ownHistory.length})</summary>
          <ul className="cx-pending__list">
            {ownHistory.map((op) => (
              <HistoryRow key={op.id} op={op} />
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function summaryText(own: number, inDoubt: number, others: number): string {
  const parts: string[] = [];
  if (own > 0) {
    parts.push(
      inDoubt > 0
        ? `${inDoubt} of your operations ${inDoubt === 1 ? "has" : "have"} an unknown outcome`
        : `${own} of your operations ${own === 1 ? "is" : "are"} unresolved`,
    );
  }
  if (others > 0) parts.push(`${others} started by other employees on this device`);
  return parts.join(" · ");
}

/** What the owner sees about their own operation's state. */
export function ownStatusLabel(op: PendingOperation, store: PendingOperationStore): string {
  if (store.isRecovering(op)) return "Outcome being recovered — the request may have completed";
  const status = store.effectiveStatus(op);
  switch (status) {
    case "pending":
      return op.attempts === 0 ? "Not sent yet" : "Not processed: sign in again to retry";
    case "sending":
      return "Sending…";
    case "unknown":
      return "Outcome unknown — not checked";
    case "failed":
      return "Refused by the server — nothing was changed";
    case "checked":
      return "Checked — outcome not confirmed by the server";
    case "succeeded":
      return "Confirmed by the server";
  }
}

function OwnRow({
  op,
  store,
  user,
  token,
}: {
  op: PendingOperation;
  store: PendingOperationStore;
  user: CurrentUser;
  token: string;
}) {
  const { handleAuthError } = useAuth();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const status = store.effectiveStatus(op);
  const block = store.retryBlock(op, user);
  // "no_server_time" here is only the page's estimate: Retry asks the server
  // for its time first and sends nothing if it cannot get it.
  const canRetry = (block === null || block === "no_server_time") && (status === "pending" || status === "unknown");
  const canDismiss = store.canDismiss(op, user);

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setMessage(null);
    try {
      await action();
    } catch (error) {
      if (!handleAuthError(error)) setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className={`cx-pending__item cx-pending__item--${status}`}>
      <div className="cx-pending__info">
        <span className="cx-pending__type">{TYPE_LABELS[op.type] ?? op.type}</span>
        <span className="cx-pending__meta">
          {ownStatusLabel(op, store)} · {formatClock(op.createdAt)}
        </span>
        {op.lastError && <span className="cx-pending__meta">Last answer: {op.lastError.message}</span>}
        {(block === "expired" || block === "unverifiable") && status === "unknown" && (
          <span className="cx-pending__meta">
            {block === "expired"
              ? "Too old to retry safely. Do not repeat it by hand: a manager must check the register and record the check."
              : "The server's time was unknown when this was created, so a retry cannot be proven safe. Do not repeat it by hand: a manager must check the register and record the check."}
          </span>
        )}
        {block === "no_server_time" && status === "unknown" && (
          <span className="cx-pending__meta" role="status">
            The server's time could not be confirmed yet, so this operation must not be sent again until it can be.
            Retry checks the server's time first and sends nothing if it cannot. Do not repeat it by hand.
          </span>
        )}
        {status === "unknown" && !store.isRecovering(op) && (
          <span className="cx-pending__meta">Only a manager can record a check of this operation.</span>
        )}
        {message && (
          <span className="cx-pending__meta" role="status">
            {message}
          </span>
        )}
      </div>
      <div className="cx-pending__actions">
        {canRetry && (
          <button
            type="button"
            className="button button--primary button--small"
            disabled={busy}
            onClick={() => act(() => executeOperation(store, op.id, { user, token }))}
          >
            {busy ? "Retrying…" : "Retry"}
          </button>
        )}
        {canDismiss && (
          <button
            type="button"
            className="button button--ghost button--small"
            disabled={busy}
            onClick={() => act(() => store.dismiss(op.id, user))}
          >
            Dismiss
          </button>
        )}
      </div>
    </li>
  );
}

/** Another employee's operation: only what, when and who. */
function OtherRow({
  op,
  store,
  user,
  mayCheck,
}: {
  op: PendingOperation;
  store: PendingOperationStore;
  user: CurrentUser;
  mayCheck: boolean;
}) {
  const status = store.effectiveStatus(op);
  return (
    <li className="cx-pending__item cx-pending__item--other">
      <div className="cx-pending__info">
        <span className="cx-pending__type">{TYPE_LABELS[op.type] ?? op.type}</span>
        <span className="cx-pending__meta">
          {status === "sending" ? "Being sent" : store.isRecovering(op) ? "Being recovered" : "Unresolved"} ·{" "}
          {formatClock(op.createdAt)} · started by{" "}
          {op.userName || "another employee"}
        </span>
      </div>
      {mayCheck && status === "unknown" && !store.isRecovering(op) && (
        <div className="cx-pending__actions">
          <CheckControl op={op} store={store} user={user} />
        </div>
      )}
    </li>
  );
}

function HistoryRow({ op }: { op: PendingOperation }) {
  return (
    <li className={`cx-pending__item cx-pending__item--${op.status}`}>
      <div className="cx-pending__info">
        <span className="cx-pending__type">{TYPE_LABELS[op.type] ?? op.type}</span>
        {op.status === "succeeded" ? (
          <span className="cx-pending__meta">
            Confirmed by the server · {op.resolvedAt ? formatClock(op.resolvedAt) : "—"}
          </span>
        ) : (
          <span className="cx-pending__meta">
            Checked by {op.check?.checkedByName || "an employee"}
            {op.check ? ` at ${formatClock(op.check.checkedAt)}` : ""} — outcome not confirmed by the server
            {op.check?.note ? ` · “${op.check.note}”` : ""}
          </span>
        )}
      </div>
    </li>
  );
}

/** Two steps: explain what a check means, then record it with an optional note. */
function CheckControl({
  op,
  store,
  user,
}: {
  op: PendingOperation;
  store: PendingOperationStore;
  user: CurrentUser;
}) {
  const noteId = useId();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return (
      <button type="button" className="button button--ghost button--small" onClick={() => setOpen(true)}>
        Record a check…
      </button>
    );
  }

  async function record() {
    setError(null);
    try {
      await store.markChecked(op.id, user, { note });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  return (
    <div className="cx-check">
      <p className="cx-card__hint">
        This only records that you checked the register. It does not confirm, reverse or repeat the operation, and it
        cannot be retried afterwards.
      </p>
      <label className="field" htmlFor={noteId}>
        <span className="field__label">What did you check? (optional)</span>
        <input
          id={noteId}
          className="field__input"
          value={note}
          maxLength={500}
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      {error && (
        <p className="cx-pending__meta" role="status">
          {error}
        </p>
      )}
      <div className="cx-pending__actions">
        <button type="button" className="button button--danger button--small" onClick={record}>
          Record check
        </button>
        <button type="button" className="button button--ghost button--small" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}
