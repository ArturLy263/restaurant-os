import { useEffect, useState, type FormEvent } from "react";

import { cashierMutations, listHandovers } from "../api/cashier";
import type { CashMovement, CashRegister, CashShift, Handover } from "../api/cashierTypes";
import { Permission, type CurrentUser } from "../api/types";
import { hasPermission } from "../auth/permissions";
import { formatClock } from "../kds/time";
import { centsToDecimal, formatAmount, parseAmountInput } from "../lib/money";
import { ErrorState, LoadingState } from "../ui/States";
import { ForbiddenState } from "./CashierStates";
import type { PendingOperationStore } from "./pendingOperations";
import { canSeeExpectedCash } from "./sections";
import {
  AmountField,
  amountProblem,
  Card,
  Chip,
  Facts,
  formatBusinessDate,
  Notice,
  SectionHeader,
  TextField,
  type NoticeMessage,
} from "./ui";
import { useCashierData } from "./useCashierData";
import { failureText, useCashierOperation, type CashierOperationRunner } from "./useCashierOperation";
import { useRegister } from "./useRegister";

interface RegisterSectionProps {
  token: string;
  user: CurrentUser;
  store: PendingOperationStore;
  onCurrency?(currency: string): void;
}

/**
 * The business day, the register's shift and its cash: open the day, open
 * a shift (receiving the previous shift's cash), count blind to close or
 * hand over, and record cash in / out. Every amount shown comes from the
 * server; expected cash only for those allowed to see it.
 */
export function RegisterSection({ token, user, store, onCurrency }: RegisterSectionProps) {
  const { state, retry } = useRegister(token);
  const shiftCurrency = state.status === "ready" ? (state.register.shift?.currency ?? null) : null;
  useEffect(() => {
    if (shiftCurrency) onCurrency?.(shiftCurrency);
  }, [shiftCurrency, onCurrency]);
  const runner = useCashierOperation(store, user, token);
  const [notice, setNotice] = useState<NoticeMessage | null>(null);
  const handovers = useCashierData<Handover[]>(`handovers:${token}`, (signal) => listHandovers(token, 5, signal));

  function refresh() {
    retry();
    handovers.reload();
  }

  const header = (
    <SectionHeader
      id="cx-register-title"
      title="Cash Register"
      meta={
        state.status === "ready" &&
        `Updated ${state.updatedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
      }
    />
  );

  let body;
  if (state.status === "loading") body = <LoadingState text="Loading the register…" />;
  else if (state.status === "forbidden") body = <ForbiddenState what="the register" onRetry={retry} />;
  else if (state.status === "error") {
    body = <ErrorState title="Could not load the register" error={state.error} onRetry={retry} />;
  } else {
    const { register, refreshError } = state;
    const shift = register.shift?.status === "OPEN" ? register.shift : null;
    body = (
      <>
        {refreshError && (
          <div className="banner banner--error" role="status">
            <span>Could not refresh ({refreshError.message}). Showing the last known state.</span>
            <button type="button" className="button button--ghost button--small" onClick={retry}>
              Refresh
            </button>
          </div>
        )}
        <div className="cx-cards">
          <DayCard register={register} user={user} runner={runner} onDone={refresh} onNotice={setNotice} />
          <ShiftSummary register={register} user={user} />
        </div>

        {register.previous_closing?.explanation_required &&
          (register.previous_closing.closed_by.id === user.id || canSeeExpectedCash(user)) && (
            <ExplainCountForm
              key={`closing-${register.previous_closing.cash_shift_id}`}
              kind="closing"
              shiftId={register.previous_closing.cash_shift_id}
              user={user}
              runner={runner}
              onDone={refresh}
              onNotice={setNotice}
            />
          )}
        {shift?.handover?.explanation_required &&
          (shift.handover.incoming_user.id === user.id || canSeeExpectedCash(user)) && (
            <ExplainCountForm
              key={`handover-${shift.id}`}
              kind="handover"
              shiftId={shift.id}
              user={user}
              runner={runner}
              onDone={refresh}
              onNotice={setNotice}
            />
          )}

        {register.business_day?.status === "OPEN" && !shift && (
          <OpenShiftForm register={register} user={user} runner={runner} onDone={refresh} onNotice={setNotice} />
        )}

        {shift && (
          <div className="cx-columns">
            <CloseShiftForm shift={shift} user={user} runner={runner} onDone={refresh} onNotice={setNotice} />
            <MovementForm shift={shift} user={user} runner={runner} onDone={refresh} onNotice={setNotice} />
          </div>
        )}

        {shift && <MovementList shift={shift} />}
        {shift?.handover && <HandoverCard handover={shift.handover} currency={shift.currency} title="Handover into this shift" />}
        <RecentHandovers state={handovers.state} currency={shift?.currency ?? null} />
      </>
    );
  }

  return (
    <section className="cx-section" aria-labelledby="cx-register-title">
      {header}
      <Notice notice={notice} onClose={() => setNotice(null)} />
      {body}
    </section>
  );
}

interface ActionProps {
  user: CurrentUser;
  runner: CashierOperationRunner;
  onDone(): void;
  onNotice(notice: NoticeMessage | null): void;
}

// --- Business day ---

function DayCard({ register, user, runner, onDone, onNotice }: ActionProps & { register: CashRegister }) {
  const day = register.business_day;
  const canOpen = hasPermission(user, Permission.BusinessDaysOpen);

  async function openDay() {
    onNotice(null);
    const outcome = await runner.run(cashierMutations.openBusinessDay());
    if (!outcome) return;
    if (outcome.ok) {
      onNotice({ kind: "success", text: `Business day ${formatBusinessDate(outcome.result.business_date)} opened.` });
    } else {
      const text = failureText(outcome);
      if (text) onNotice({ kind: "error", text: `Could not open the business day: ${text}` });
    }
    onDone();
  }

  if (!day || day.status !== "OPEN") {
    return (
      <Card title="Business day" status={<Chip tone="off">Not open</Chip>}>
        <p className="cx-card__text">No business day is open. Payments and shifts need an open day.</p>
        {canOpen ? (
          <button type="button" className="button button--primary" disabled={runner.busy} onClick={() => void openDay()}>
            {runner.busy ? "Opening…" : "Open business day"}
          </button>
        ) : (
          <p className="cx-card__hint">Ask a cashier or manager to open the day.</p>
        )}
      </Card>
    );
  }
  return (
    <Card title="Business day" status={<Chip tone="ok">Open</Chip>}>
      <Facts rows={[["Date", formatBusinessDate(day.business_date)], ["Opened at", formatClock(day.opened_at)]]} />
      <p className="cx-card__hint">A manager closes the day in Reports.</p>
    </Card>
  );
}

// --- Shift ---

function ShiftSummary({ register, user }: { register: CashRegister; user: CurrentUser }) {
  const shift = register.shift?.status === "OPEN" ? register.shift : null;
  if (!shift) {
    return (
      <Card title="Current shift" status={<Chip tone="off">None</Chip>}>
        <p className="cx-card__text">No cash shift is open on the register.</p>
        {register.previous_closing && (
          <Facts
            rows={[
              ["Last closed by", register.previous_closing.closed_by.name ?? "—"],
              ["Closed at", formatClock(register.previous_closing.closed_at)],
            ]}
          />
        )}
      </Card>
    );
  }
  const expectedVisible = canSeeExpectedCash(user) && shift.expected_visible && shift.expected_cash !== null;
  return (
    <Card title="Current shift" status={<Chip tone="ok">Open</Chip>}>
      <Facts
        rows={[
          ["Opened by", shift.opened_by.id === user.id ? "You" : (shift.opened_by.name ?? "—")],
          ["Opened at", formatClock(shift.opened_at)],
          ["Opening cash", formatAmount(shift.effective_opening_amount, shift.currency)],
          expectedVisible && ["Expected cash", formatAmount(shift.expected_cash, shift.currency)],
        ]}
      />
      {!expectedVisible && <p className="cx-card__hint">Blind count: the expected cash is not shown.</p>}
    </Card>
  );
}

function OpenShiftForm({ register, user, runner, onDone, onNotice }: ActionProps & { register: CashRegister }) {
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const previous = register.previous_closing;
  const canOpen = hasPermission(user, Permission.CashShiftsOpen);
  const closedItMyself = previous !== null && previous.closed_by.id === user.id;
  const maySelfConfirm = hasPermission(user, Permission.CashHandoversSelfConfirm);
  const problem = amountProblem(amount);

  if (!canOpen) {
    return (
      <Card title="Open a shift">
        <p className="cx-card__text">Your role cannot open cash shifts.</p>
      </Card>
    );
  }
  if (closedItMyself && !maySelfConfirm) {
    return (
      <Card title="Receive the cash" status={<Chip tone="warn">Handover pending</Chip>}>
        <p className="cx-card__text">
          You closed the previous shift. Another employee must count the cash and open the next shift to confirm
          the handover.
        </p>
      </Card>
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    if (problem) return;
    onNotice(null);
    const outcome = await runner.run(
      cashierMutations.openShift({
        opening_amount: centsToDecimal(parseAmountInput(amount)!),
        handover_reason: reason.trim() || null,
      }),
    );
    if (!outcome) return;
    if (outcome.ok) {
      const handover = outcome.result.handover;
      const recorded = handover
        ? `Shift opened. Handover from ${handover.outgoing_user.name ?? "the previous cashier"} recorded.`
        : "Shift opened.";
      onNotice({
        kind: "success",
        text: handover?.explanation_required
          ? `${recorded} Your count differs from what was handed over: explain it below.`
          : recorded,
      });
      setAmount("");
      setReason("");
      setSubmitted(false);
    } else {
      const text = failureText(outcome);
      if (text) onNotice({ kind: "error", text: `Could not open the shift: ${text}` });
    }
    onDone();
  }

  return (
    <form className="cx-fieldset cx-form" onSubmit={(event) => void submit(event)} noValidate aria-label="Open a shift">
      <h3 className="cx-form__title">{previous ? "Receive the cash and open a shift" : "Open a shift"}</h3>
      {previous ? (
        <p className="cx-card__hint">
          The last shift was closed by {previous.closed_by.id === user.id ? "you" : (previous.closed_by.name ?? "another employee")}{" "}
          at {formatClock(previous.closed_at)}. Count the cash in the drawer without looking at any total: your count
          confirms the handover.
          {closedItMyself && " You closed it yourself; the handover will show you on both sides."}
        </p>
      ) : (
        <p className="cx-card__hint">Count the cash you put in the drawer.</p>
      )}
      <AmountField
        label={previous ? "Cash counted in the drawer" : "Opening cash"}
        value={amount}
        onChange={setAmount}
        invalid={submitted ? problem : null}
        disabled={runner.busy}
      />
      {previous && (
        <TextField
          label="Reason, if you know the cash differs (optional)"
          value={reason}
          onChange={setReason}
          multiline
          hint="Your count is recorded as entered. If it differs from what was handed over, you explain it afterwards."
          disabled={runner.busy}
        />
      )}
      <div className="cx-form__actions">
        <button type="submit" className="button button--primary" disabled={runner.busy}>
          {runner.busy ? "Opening…" : "Open shift"}
        </button>
      </div>
    </form>
  );
}

type CloseMode = "close" | "handover";

function CloseShiftForm({ shift, user, runner, onDone, onNotice }: ActionProps & { shift: CashShift }) {
  const [mode, setMode] = useState<CloseMode>("handover");
  const [amount, setAmount] = useState("");
  const [explanation, setExplanation] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const problem = amountProblem(amount);

  if (!hasPermission(user, Permission.CashShiftsClose)) return null;

  function review(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    if (problem) return;
    setConfirming(true);
  }

  async function confirm() {
    onNotice(null);
    const counted = centsToDecimal(parseAmountInput(amount)!);
    const note = explanation.trim() || null;
    const outcome = await runner.run(
      mode === "handover"
        ? cashierMutations.handOverShift(shift.id, { counted_amount: counted, closing_explanation: note })
        : cashierMutations.closeShift(shift.id, { counted_amount: counted, explanation: note }),
    );
    setConfirming(false);
    if (!outcome) return;
    if (outcome.ok) {
      onNotice({ kind: "success", text: closingText(outcome.result, mode, user) });
      setAmount("");
      setExplanation("");
      setSubmitted(false);
    } else {
      const text = failureText(outcome);
      if (text) onNotice({ kind: "error", text: `Could not close the shift: ${text}` });
    }
    onDone();
  }

  const counted = parseAmountInput(amount);
  return (
    <form className="cx-fieldset cx-form" onSubmit={review} noValidate aria-label="Count and close the shift">
      <h3 className="cx-form__title">Count and close the shift</h3>
      <div className="cx-segmented" role="radiogroup" aria-label="How the shift ends">
        {(
          [
            ["handover", "Hand over", "The next cashier counts and receives the cash."],
            ["close", "Close the register", "End of service; the next shift starts later."],
          ] as const
        ).map(([value, label, hint]) => (
          <label key={value} className={`cx-option${mode === value ? " cx-option--current" : ""}`}>
            <input
              type="radio"
              name="close-mode"
              value={value}
              checked={mode === value}
              disabled={runner.busy || confirming}
              onChange={() => setMode(value)}
            />
            <span className="cx-option__label">{label}</span>
            <span className="cx-option__hint">{hint}</span>
          </label>
        ))}
      </div>
      <AmountField
        label="Cash counted in the drawer"
        value={amount}
        onChange={(value) => {
          setAmount(value);
          setConfirming(false);
        }}
        invalid={submitted ? problem : null}
        hint="Count blind: the expected amount is not shown while counting."
        disabled={runner.busy}
      />
      <TextField
        label="Explanation, if you know the count differs (optional)"
        value={explanation}
        onChange={setExplanation}
        multiline
        hint="The count is recorded as entered and cannot be changed. If it differs, you explain it afterwards."
        disabled={runner.busy}
      />
      {confirming && counted !== null ? (
        <div className="cx-confirm" role="group" aria-label="Confirm closing">
          <p>
            {mode === "handover" ? "Hand over" : "Close"} the shift with{" "}
            <strong>{formatAmount(centsToDecimal(counted), shift.currency)}</strong> counted? This cannot be undone.
          </p>
          <div className="cx-form__actions">
            <button type="button" className="button button--secondary" disabled={runner.busy} onClick={() => setConfirming(false)}>
              Back
            </button>
            <button type="button" className="button button--danger" disabled={runner.busy} onClick={() => void confirm()}>
              {runner.busy ? "Closing…" : mode === "handover" ? "Confirm handover" : "Confirm close"}
            </button>
          </div>
        </div>
      ) : (
        <div className="cx-form__actions">
          <button type="submit" className="button button--primary" disabled={runner.busy}>
            {mode === "handover" ? "Hand over shift" : "Close shift"}
          </button>
        </div>
      )}
    </form>
  );
}

function closingText(result: CashShift, mode: CloseMode, user: CurrentUser): string {
  const closing = result.closing;
  const parts = [mode === "handover" ? "Shift handed over." : "Shift closed."];
  if (closing?.counted_amount) parts.push(`Counted ${formatAmount(closing.counted_amount, result.currency)}.`);
  // Expected cash and the difference only for those allowed to see them,
  // and only when the server sent them.
  if (canSeeExpectedCash(user) && closing && closing.expected_amount !== null && closing.difference !== null) {
    parts.push(
      `Expected ${formatAmount(closing.expected_amount, result.currency)}, difference ${formatAmount(closing.difference, result.currency)}.`,
    );
  }
  if (closing?.explanation_required) {
    parts.push("The count differs from what the register expected: explain it below.");
  }
  if (mode === "handover") parts.push("The next cashier confirms the cash by opening their shift.");
  return parts.join(" ");
}

// --- Explaining a recorded count ---

/**
 * The explanation of a count that differs. The count itself is already
 * recorded and final, so asking for it here reveals nothing a cashier could
 * use to adjust a count; no amount is shown.
 */
export function ExplainCountForm({
  kind,
  shiftId,
  runner,
  onDone,
  onNotice,
  onRecorded,
}: ActionProps & { kind: "closing" | "handover"; shiftId: string; onRecorded?(): void }) {
  const [text, setText] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const problem = text.trim() ? null : "Explain the difference.";

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    if (problem) return;
    onNotice(null);
    const request = { explanation: text.trim() };
    const outcome = await runner.run(
      kind === "closing"
        ? cashierMutations.explainClosing(shiftId, request)
        : cashierMutations.explainHandover(shiftId, request),
    );
    if (!outcome) return;
    if (outcome.ok) {
      onNotice({ kind: "success", text: "Explanation recorded." });
      setText("");
      setSubmitted(false);
      onRecorded?.();
    } else {
      const message = failureText(outcome);
      if (message) onNotice({ kind: "error", text: `The explanation was not recorded: ${message}` });
    }
    onDone();
  }

  const title = kind === "closing" ? "Explain the closing count" : "Explain the handover count";
  return (
    <form className="cx-fieldset cx-form cx-form--attention" onSubmit={(event) => void submit(event)} noValidate aria-label={title}>
      <h3 className="cx-form__title">{title}</h3>
      <p className="cx-card__hint">
        {kind === "closing"
          ? "The counted cash differs from what the register expected. The count is recorded; say what happened. The business day cannot close until it is explained."
          : "The cash received differs from what the previous shift handed over. The count is recorded; say what happened. The business day cannot close until it is explained."}
      </p>
      <TextField
        label="Explanation"
        value={text}
        onChange={setText}
        multiline
        invalid={submitted ? problem : null}
        disabled={runner.busy}
      />
      <div className="cx-form__actions">
        <button type="submit" className="button button--primary" disabled={runner.busy}>
          {runner.busy ? "Recording…" : "Record explanation"}
        </button>
      </div>
    </form>
  );
}

// --- Cash in / out ---

function MovementForm({ shift, user, runner, onDone, onNotice }: ActionProps & { shift: CashShift }) {
  const [type, setType] = useState<"CASH_IN" | "CASH_OUT">("CASH_IN");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const problem = amountProblem(amount, { allowZero: false });
  const reasonProblem = reason.trim() ? null : "Give a reason.";

  if (!hasPermission(user, Permission.CashMovementsCreate)) return null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    if (problem || reasonProblem) return;
    onNotice(null);
    const value = centsToDecimal(parseAmountInput(amount)!);
    const outcome = await runner.run(cashierMutations.recordCashMovement(shift.id, { type, amount: value, reason: reason.trim() }));
    if (!outcome) return;
    if (outcome.ok) {
      onNotice({
        kind: "success",
        text: `${type === "CASH_IN" ? "Cash in" : "Cash out"} of ${formatAmount(outcome.result.amount, shift.currency)} recorded.`,
      });
      setAmount("");
      setReason("");
      setSubmitted(false);
    } else {
      const text = failureText(outcome);
      if (text) onNotice({ kind: "error", text: `Could not record the cash movement: ${text}` });
    }
    onDone();
  }

  return (
    <form className="cx-fieldset cx-form" onSubmit={(event) => void submit(event)} noValidate aria-label="Cash in or out">
      <h3 className="cx-form__title">Cash in / out</h3>
      <div className="cx-segmented" role="radiogroup" aria-label="Movement type">
        {(
          [
            ["CASH_IN", "Cash in"],
            ["CASH_OUT", "Cash out"],
          ] as const
        ).map(([value, label]) => (
          <label key={value} className={`cx-option${type === value ? " cx-option--current" : ""}`}>
            <input
              type="radio"
              name="movement-type"
              value={value}
              checked={type === value}
              disabled={runner.busy}
              onChange={() => setType(value)}
            />
            <span className="cx-option__label">{label}</span>
          </label>
        ))}
      </div>
      <AmountField label="Amount" value={amount} onChange={setAmount} invalid={submitted ? problem : null} disabled={runner.busy} />
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        invalid={submitted ? reasonProblem : null}
        hint="E.g. change float, supplier paid in cash."
        disabled={runner.busy}
      />
      <div className="cx-form__actions">
        <button type="submit" className="button button--primary" disabled={runner.busy}>
          {runner.busy ? "Recording…" : type === "CASH_IN" ? "Record cash in" : "Record cash out"}
        </button>
      </div>
    </form>
  );
}

const MOVEMENT_LABELS: Record<string, string> = {
  CASH_IN: "Cash in",
  CASH_OUT: "Cash out",
  CASH_REFUND: "Cash refund",
  CASH_TIP: "Cash tip",
};

function MovementList({ shift }: { shift: CashShift }) {
  const movements: CashMovement[] = [...shift.movements].sort((a, b) => b.created_at.localeCompare(a.created_at));
  return (
    <Card title="Cash movements this shift" status={<Chip tone="off">{movements.length}</Chip>}>
      {movements.length === 0 ? (
        <p className="cx-card__hint">No cash put in or taken out yet.</p>
      ) : (
        <div className="cx-table-wrap">
          <table className="cx-table">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Type</th>
                <th scope="col" className="cx-table__num">
                  Amount
                </th>
                <th scope="col">Reason</th>
                <th scope="col">By</th>
              </tr>
            </thead>
            <tbody>
              {movements.map((movement) => (
                <tr key={movement.id}>
                  <td>{formatClock(movement.created_at)}</td>
                  <td>{MOVEMENT_LABELS[movement.type] ?? movement.type}</td>
                  <td className="cx-table__num">
                    {movement.type === "CASH_OUT" || movement.type === "CASH_REFUND" ? "−" : "+"}
                    {formatAmount(movement.amount, shift.currency)}
                  </td>
                  <td>{movement.reason ?? "—"}</td>
                  <td>{movement.user.name ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// --- Handovers ---

function HandoverCard({ handover, currency, title }: { handover: Handover; currency: string | null; title: string }) {
  return (
    <Card title={title} status={handover.self_confirmed ? <Chip tone="warn">Self-confirmed</Chip> : <Chip tone="ok">Two people</Chip>}>
      <Facts
        rows={[
          ["From", handover.outgoing_user.name ?? "—"],
          ["To", handover.incoming_user.name ?? "—"],
          ["Handed over", formatAmount(handover.handed_over_amount, currency)],
          ["Received", formatAmount(handover.received_amount, currency)],
          ["Difference", formatAmount(handover.difference, currency)],
          handover.previous_expected_amount !== null && ["Expected before", formatAmount(handover.previous_expected_amount, currency)],
          handover.reason ? ["Reason", handover.reason] : null,
          ["At", formatClock(handover.created_at)],
        ]}
      />
    </Card>
  );
}

function RecentHandovers({
  state,
  currency,
}: {
  state: ReturnType<typeof useCashierData<Handover[]>>["state"];
  currency: string | null;
}) {
  if (state.status !== "ready") return null;
  if (state.data.length === 0) return null;
  return (
    <Card title="Recent handovers">
      <div className="cx-table-wrap">
        <table className="cx-table">
          <thead>
            <tr>
              <th scope="col">At</th>
              <th scope="col">From</th>
              <th scope="col">To</th>
              <th scope="col" className="cx-table__num">
                Received
              </th>
              <th scope="col" className="cx-table__num">
                Difference
              </th>
            </tr>
          </thead>
          <tbody>
            {state.data.map((handover) => (
              <tr key={handover.id}>
                <td>{formatClock(handover.created_at)}</td>
                <td>{handover.outgoing_user.name ?? "—"}</td>
                <td>
                  {handover.incoming_user.name ?? "—"}
                  {handover.self_confirmed && <span className="cx-note"> (self-confirmed)</span>}
                </td>
                <td className="cx-table__num">{formatAmount(handover.received_amount, currency)}</td>
                <td className="cx-table__num">{formatAmount(handover.difference, currency)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
