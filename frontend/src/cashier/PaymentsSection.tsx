import { useEffect, useState, type FormEvent } from "react";

import { cashierMutations, getBill, splitBillEqually } from "../api/cashier";
import type { BillDetail, EqualSplit, PaymentMethod } from "../api/cashierTypes";
import { isApiError } from "../api/client";
import { Permission, type CurrentUser } from "../api/types";
import { hasPermission } from "../auth/permissions";
import { formatClock } from "../kds/time";
import { centsToDecimal, formatAmount, formatCents, parseAmountInput, toCents } from "../lib/money";
import { Stepper } from "../pos/controls";
import { ErrorState, LoadingState } from "../ui/States";
import { ForbiddenState } from "./CashierStates";
import { tableLabel } from "./OrdersSection";
import type { PendingOperationStore } from "./pendingOperations";
import { AmountField, amountProblem, Card, Chip, errorText, Facts, Notice, SectionHeader, TextField, type NoticeMessage } from "./ui";
import { useCashierData } from "./useCashierData";
import { failureText, useCashierOperation } from "./useCashierOperation";

const MAX_SPLIT_PARTS = 50; // backend app/services/bill_service.py

interface PaymentsSectionProps {
  token: string;
  user: CurrentUser;
  store: PendingOperationStore;
  billId: string | null;
  onChooseOrder(): void;
  onCurrency?(currency: string): void;
}

/**
 * One bill: its lines, what was paid and what is still due (always the
 * server's numbers), and taking a cash or card payment for all of it or
 * for one equal share. Change is worked out here for the cashier only; it
 * is never sent.
 */
export function PaymentsSection({ token, user, store, billId, onChooseOrder, onCurrency }: PaymentsSectionProps) {
  const { state, reload } = useCashierData<BillDetail>(billId ? `bill:${billId}:${token}` : null, (signal) =>
    getBill(token, billId!, signal),
  );
  const [notice, setNotice] = useState<NoticeMessage | null>(null);
  const bill = state.status === "ready" ? state.data : null;

  useEffect(() => {
    if (bill) onCurrency?.(bill.currency);
  }, [bill, onCurrency]);

  // A new bill starts without the previous bill's message.
  useEffect(() => setNotice(null), [billId]);

  let body;
  if (!billId) {
    body = (
      <section className="state">
        <h3 className="state__title">No bill selected</h3>
        <p className="state__text">Choose an order in Orders to create its bill or take a payment.</p>
        <div className="state__actions">
          <button type="button" className="button button--primary" onClick={onChooseOrder}>
            Go to Orders
          </button>
        </div>
      </section>
    );
  } else if (state.status === "loading") body = <LoadingState text="Loading the bill…" />;
  else if (state.status === "forbidden") body = <ForbiddenState what="this bill" onRetry={reload} />;
  else if (state.status === "error") body = <ErrorState title="Could not load the bill" error={state.error} onRetry={reload} />;
  else {
    const detail = state.data;
    body = (
      <>
        {state.refreshError && (
          <div className="banner banner--error" role="status">
            <span>Could not refresh the bill ({state.refreshError.message}). The amounts may be out of date.</span>
            <button type="button" className="button button--ghost button--small" onClick={reload}>
              Refresh
            </button>
          </div>
        )}
        <div className="cx-columns">
          <BillCard bill={detail} />
          <div className="cx-stack">
            <DueCard bill={detail} />
            {detail.status === "OPEN" && hasPermission(user, Permission.PaymentsCreate) ? (
              <PaymentForm
                key={detail.id}
                bill={detail}
                token={token}
                user={user}
                store={store}
                onNotice={setNotice}
                onDone={reload}
              />
            ) : detail.status === "OPEN" ? (
              <Card title="Take a payment">
                <p className="cx-card__text">Your role cannot take payments.</p>
              </Card>
            ) : null}
          </div>
        </div>
        <PaymentsList bill={detail} />
      </>
    );
  }

  return (
    <section className="cx-section" aria-labelledby="cx-payments-title">
      <SectionHeader
        id="cx-payments-title"
        title="Payments"
        meta={bill ? `${tableLabel(bill.order)} · ${bill.order.guest_count} guests` : null}
      />
      {billId && (
        <div className="cx-toolbar">
          <button type="button" className="button button--ghost button--small" onClick={onChooseOrder}>
            ← All orders
          </button>
        </div>
      )}
      <Notice notice={notice} onClose={() => setNotice(null)} />
      {body}
    </section>
  );
}

function BillCard({ bill }: { bill: BillDetail }) {
  return (
    <Card title="Bill" status={<BillStatus bill={bill} />}>
      <ul className="cx-lines">
        {bill.lines.map((line) => (
          <li key={line.order_item_id} className={`cx-lines__item${line.status === "CANCELLED" ? " cx-lines__item--cancelled" : ""}`}>
            <span className="cx-lines__qty">{line.quantity}×</span>
            <span className="cx-lines__name">
              {line.product_name ?? "Item"}
              {line.modifiers.length > 0 && (
                <span className="cx-lines__sub">{line.modifiers.map((modifier) => modifier.name).join(", ")}</span>
              )}
              {line.notes && <span className="cx-lines__sub">“{line.notes}”</span>}
              {line.status === "CANCELLED" && <span className="cx-lines__sub">Cancelled</span>}
            </span>
            <span className="cx-lines__price">{formatAmount(line.total_price, bill.currency)}</span>
          </li>
        ))}
      </ul>
      <Facts
        rows={[
          ["Total", formatAmount(bill.total, bill.currency)],
          toCents(bill.adjustments_total) !== 0 && ["Adjustments", formatAmount(bill.adjustments_total, bill.currency)],
          toCents(bill.adjustments_total) !== 0 && ["Adjusted total", formatAmount(bill.adjusted_total, bill.currency)],
          ["Paid", formatAmount(bill.paid_total, bill.currency)],
          toCents(bill.tips_total) !== 0 && ["Tips", formatAmount(bill.tips_total, bill.currency)],
          toCents(bill.refunded_total) !== 0 && ["Refunded", formatAmount(bill.refunded_total, bill.currency)],
        ]}
      />
    </Card>
  );
}

function BillStatus({ bill }: { bill: BillDetail }) {
  if (bill.status === "PAID") return <Chip tone="ok">Paid</Chip>;
  if (bill.status === "OPEN") return <Chip tone="warn">Open</Chip>;
  return <Chip tone="off">{bill.status}</Chip>;
}

function DueCard({ bill }: { bill: BillDetail }) {
  const paid = bill.status === "PAID";
  return (
    <div className={`cx-due${paid ? " cx-due--paid" : ""}`} role="status" aria-label="Amount due">
      <span className="cx-due__label">{paid ? "Paid in full" : "Amount due"}</span>
      <span className="cx-due__value">{formatAmount(bill.amount_due, bill.currency)}</span>
      {paid && <span className="cx-due__hint">The order is closed and the table is free again.</span>}
    </div>
  );
}

interface PaymentFormProps {
  bill: BillDetail;
  token: string;
  user: CurrentUser;
  store: PendingOperationStore;
  onNotice(notice: NoticeMessage | null): void;
  onDone(): void;
}

/** An equal split as the server computed it, with the shares paid here. */
interface SplitState {
  split: EqualSplit;
  paid: number[];
  // Cents of the share paid last: until the bill is reloaded, the server's
  // amount due still includes it.
  lastPaid: number;
}

function PaymentForm({ bill, token, user, store, onNotice, onDone }: PaymentFormProps) {
  const runner = useCashierOperation(store, user, token);
  const [method, setMethod] = useState<PaymentMethod>("CASH");
  const [amount, setAmount] = useState(() => centsToDecimal(toCents(bill.amount_due)));
  const [tip, setTip] = useState("");
  const [tendered, setTendered] = useState("");
  const [cardConfirmed, setCardConfirmed] = useState(false);
  const [reference, setReference] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [parts, setParts] = useState(2);
  const [split, setSplit] = useState<SplitState | null>(null);
  const [share, setShare] = useState<number | null>(null);
  const [splitting, setSplitting] = useState(false);
  const [splitError, setSplitError] = useState<string | null>(null);

  const dueCents = toCents(bill.amount_due);
  const amountCents = parseAmountInput(amount);
  const tipCents = tip.trim() === "" ? 0 : parseAmountInput(tip);
  const tenderedCents = tendered.trim() === "" ? null : parseAmountInput(tendered);

  // A split stays valid only while its unpaid shares add up to what the
  // server says is due (another payment or a bill change voids it).
  const unpaidShares = split ? split.split.shares.map((value, index) => ({ value, index })).filter((s) => !split.paid.includes(s.index)) : [];
  const unpaidCents = unpaidShares.reduce((sum, s) => sum + toCents(s.value), 0);
  const splitValid =
    split !== null && (unpaidCents === dueCents || (split.lastPaid > 0 && unpaidCents + split.lastPaid === dueCents));

  useEffect(() => {
    // The bill changed under the split: forget it rather than suggest wrong shares.
    if (split && !splitValid) {
      setSplit(null);
      setShare(null);
    }
  }, [split, splitValid]);

  let amountError = amountProblem(amount, { allowZero: false });
  if (!amountError && amountCents !== null && amountCents > dueCents) {
    amountError = `At most the amount due (${formatCents(dueCents, bill.currency)}).`;
  }
  const tipError = tipCents === null ? "Enter a tip such as 2.00, or leave it empty." : null;
  const toCollect = (amountCents ?? 0) + (tipCents ?? 0);
  let tenderedError: string | null = null;
  if (method === "CASH" && tendered.trim() !== "") {
    if (tenderedCents === null) tenderedError = "Enter the cash received, such as 50.00.";
    else if (tenderedCents < toCollect) tenderedError = `Not enough: ${formatCents(toCollect - tenderedCents, bill.currency)} short.`;
  }
  const cardError = method === "CARD" && !cardConfirmed ? "Confirm that the card terminal approved the payment." : null;
  const change = method === "CASH" && tenderedCents !== null && !tenderedError && amountCents !== null ? tenderedCents - toCollect : null;

  async function loadSplit() {
    setSplitError(null);
    setSplitting(true);
    try {
      const result = await splitBillEqually(token, bill.id, parts);
      setSplit({ split: result, paid: [], lastPaid: 0 });
      choose(result.shares[0], 0);
    } catch (error) {
      setSplitError(isApiError(error) ? error.message : errorText(error));
    } finally {
      setSplitting(false);
    }
  }

  function choose(value: string, index: number) {
    setShare(index);
    setAmount(centsToDecimal(toCents(value)));
    setTendered("");
  }

  function payAll() {
    setShare(null);
    setAmount(centsToDecimal(dueCents));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    if (amountError || tipError || tenderedError || cardError || amountCents === null) return;
    onNotice(null);
    const outcome = await runner.run(
      cashierMutations.createPayment(bill.id, {
        amount: centsToDecimal(amountCents),
        method,
        ...(tipCents ? { tip_amount: centsToDecimal(tipCents) } : {}),
        ...(method === "CARD"
          ? { card_confirmed: true, ...(reference.trim() ? { external_reference: reference.trim() } : {}) }
          : {}),
      }),
    );
    if (!outcome) return;
    if (outcome.ok) {
      const paid = outcome.result;
      const remaining = dueCents - toCents(paid.amount);
      const message = [
        `${paid.method === "CARD" ? "Card" : "Cash"} payment of ${formatAmount(paid.amount, bill.currency)} recorded`,
        toCents(paid.tip_amount) > 0 ? ` (tip ${formatAmount(paid.tip_amount, bill.currency)})` : "",
        ".",
        change !== null && change > 0 ? ` Give ${formatCents(change, bill.currency)} change.` : "",
        remaining > 0 ? ` Still due: ${formatCents(remaining, bill.currency)}.` : " The bill is paid in full.",
      ];
      onNotice({ kind: "success", text: message.join("") });
      if (split && share !== null) {
        const next = { ...split, paid: [...split.paid, share], lastPaid: toCents(paid.amount) };
        setSplit(next);
        const nextShare = next.split.shares.findIndex((_, index) => !next.paid.includes(index));
        if (nextShare >= 0) choose(next.split.shares[nextShare], nextShare);
        else setShare(null);
      } else {
        setAmount(centsToDecimal(Math.max(remaining, 0)));
      }
      setTip("");
      setTendered("");
      setCardConfirmed(false);
      setReference("");
      setSubmitted(false);
    } else {
      const text = failureText(outcome);
      if (text) onNotice({ kind: "error", text: `The payment was not recorded: ${text}` });
    }
    onDone();
  }

  const maxParts = Math.max(2, Math.min(MAX_SPLIT_PARTS, dueCents));

  return (
    <form className="cx-fieldset cx-form" onSubmit={(event) => void submit(event)} noValidate aria-label="Take a payment">
      <h3 className="cx-form__title">Take a payment</h3>

      <div className="cx-split" role="group" aria-label="Split the bill equally">
        <div className="cx-split__row">
          <span className="field__label">Split equally</span>
          <Stepper label="Guests" value={parts} min={2} max={maxParts} disabled={splitting || runner.busy} onChange={setParts} />
          <button type="button" className="button button--secondary button--small" disabled={splitting || runner.busy || dueCents < 2} onClick={() => void loadSplit()}>
            {splitting ? "Splitting…" : `Split in ${parts}`}
          </button>
          {split && (
            <button type="button" className="button button--ghost button--small" disabled={runner.busy} onClick={() => { setSplit(null); payAll(); }}>
              No split
            </button>
          )}
        </div>
        {splitError && <p className="cx-field-error" role="alert">{splitError}</p>}
        {split && splitValid && (
          <ul className="cx-shares" aria-label="Shares">
            {split.split.shares.map((value, index) => {
              const done = split.paid.includes(index);
              return (
                <li key={index}>
                  <button
                    type="button"
                    className={`cx-share${share === index ? " cx-share--current" : ""}${done ? " cx-share--paid" : ""}`}
                    disabled={done || runner.busy}
                    aria-pressed={share === index}
                    aria-label={`Share ${index + 1} of ${split.split.shares.length}: ${formatAmount(value, bill.currency)}${done ? ", paid" : ""}`}
                    onClick={() => choose(value, index)}
                  >
                    <span className="cx-share__label">Share {index + 1}</span>
                    <span className="cx-share__value">{formatAmount(value, bill.currency)}</span>
                    {done && <span className="cx-share__paid">Paid</span>}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="cx-segmented" role="radiogroup" aria-label="Payment method">
        {(
          [
            ["CASH", "Cash"],
            ["CARD", "Card"],
          ] as const
        ).map(([value, label]) => (
          <label key={value} className={`cx-option${method === value ? " cx-option--current" : ""}`}>
            <input type="radio" name="payment-method" value={value} checked={method === value} disabled={runner.busy} onChange={() => setMethod(value)} />
            <span className="cx-option__label">{label}</span>
          </label>
        ))}
      </div>

      <div className="cx-form__grid">
        <AmountField
          label="Amount"
          value={amount}
          onChange={(value) => {
            setAmount(value);
            setShare(null);
          }}
          invalid={submitted ? amountError : null}
          hint={share !== null && split ? `Share ${share + 1} of ${split.split.shares.length}` : undefined}
          disabled={runner.busy}
        />
        <AmountField label="Tip (optional)" value={tip} onChange={setTip} invalid={tipError} disabled={runner.busy} />
      </div>
      {amountCents !== null && amountCents < dueCents && !amountError && (
        <p className="cx-field-hint">
          A partial payment: {formatCents(dueCents - amountCents, bill.currency)} stays due.{" "}
          <button type="button" className="cx-link" onClick={payAll}>
            Pay all
          </button>
        </p>
      )}

      {method === "CASH" ? (
        <>
          <AmountField
            label="Cash received (optional, for change)"
            value={tendered}
            onChange={setTendered}
            invalid={tenderedError}
            disabled={runner.busy}
          />
          {change !== null && (
            <p className="cx-change" role="status">
              Change to give: <strong>{formatCents(change, bill.currency)}</strong>
            </p>
          )}
        </>
      ) : (
        <>
          <label className="cx-checkbox">
            <input
              type="checkbox"
              checked={cardConfirmed}
              disabled={runner.busy}
              onChange={(event) => setCardConfirmed(event.target.checked)}
            />
            <span>The card terminal approved {formatCents(toCollect, bill.currency)}</span>
          </label>
          {submitted && cardError && <p className="cx-field-error">{cardError}</p>}
          <TextField
            label="Terminal receipt number (optional)"
            value={reference}
            onChange={setReference}
            maxLength={100}
            hint="The terminal's receipt or transaction number, never card data."
            disabled={runner.busy}
          />
        </>
      )}

      <div className="cx-form__actions">
        <button type="submit" className="button button--primary button--block" disabled={runner.busy}>
          {runner.busy
            ? "Recording…"
            : `Record ${method === "CARD" ? "card" : "cash"} payment${amountCents !== null && !amountError ? ` · ${formatCents(toCollect, bill.currency)}` : ""}`}
        </button>
      </div>
    </form>
  );
}

function PaymentsList({ bill }: { bill: BillDetail }) {
  if (bill.payments.length === 0) return null;
  return (
    <Card title="Payments on this bill" status={<Chip tone="off">{bill.payments.length}</Chip>}>
      <div className="cx-table-wrap">
        <table className="cx-table">
          <thead>
            <tr>
              <th scope="col">Time</th>
              <th scope="col">Method</th>
              <th scope="col" className="cx-table__num">
                Amount
              </th>
              <th scope="col" className="cx-table__num">
                Tip
              </th>
              <th scope="col">By</th>
            </tr>
          </thead>
          <tbody>
            {bill.payments.map((payment) => (
              <tr key={payment.id}>
                <td>{formatClock(payment.created_at)}</td>
                <td>
                  {payment.method === "CARD" ? "Card" : payment.method === "CASH" ? "Cash" : payment.method}
                  {payment.external_reference && <span className="cx-table__sub">Ref. {payment.external_reference}</span>}
                  {toCents(payment.refunded_amount) > 0 && (
                    <span className="cx-table__sub">Refunded {formatAmount(payment.refunded_amount, bill.currency)}</span>
                  )}
                </td>
                <td className="cx-table__num">{formatAmount(payment.amount, bill.currency)}</td>
                <td className="cx-table__num">{formatAmount(payment.tip_amount, bill.currency)}</td>
                <td>{payment.user_name ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
