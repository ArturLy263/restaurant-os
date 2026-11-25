import { useEffect, useState } from "react";

import { cashierMutations, getBusinessDayReport, listBusinessDays } from "../api/cashier";
import type {
  BlockingOrder,
  BusinessDay,
  BusinessDayReport,
  BusinessDayReportResponse,
  MethodTotals,
} from "../api/cashierTypes";
import { isApiError } from "../api/client";
import { Permission, type CurrentUser } from "../api/types";
import { hasPermission } from "../auth/permissions";
import { formatClock } from "../kds/time";
import { formatAmount } from "../lib/money";
import { ErrorState, LoadingState } from "../ui/States";
import { ForbiddenState } from "./CashierStates";
import type { PendingOperationStore } from "./pendingOperations";
import { ExplainCountForm } from "./RegisterSection";
import { canSeeExpectedCash } from "./sections";
import { Card, Chip, Facts, formatBusinessDate, Notice, SectionHeader, type NoticeMessage } from "./ui";
import { useCashierData } from "./useCashierData";
import { failureText, refusalCode, useCashierOperation } from "./useCashierOperation";

const BUSINESS_DAYS_LIMIT = 30;

interface ReportsSectionProps {
  token: string;
  user: CurrentUser;
  store: PendingOperationStore;
}

/**
 * Business days and their reports: an open day's figures so far, a closed
 * day's report as it was frozen at closing. Closing the day is offered to
 * those allowed to; the server decides whether it may close.
 */
export function ReportsSection({ token, user, store }: ReportsSectionProps) {
  const days = useCashierData<BusinessDay[]>(`days:${token}`, (signal) => listBusinessDays(token, BUSINESS_DAYS_LIMIT, signal));
  const [selected, setSelected] = useState<string | null>(null);
  const dayList = days.state.status === "ready" ? days.state.data : null;
  const dayId = selected ?? dayList?.[0]?.id ?? null;
  const report = useCashierData<BusinessDayReportResponse>(dayId ? `report:${dayId}:${token}` : null, (signal) =>
    getBusinessDayReport(token, dayId!, signal),
  );
  const [notice, setNotice] = useState<NoticeMessage | null>(null);
  const [blocking, setBlocking] = useState<BlockingOrder[] | null>(null);
  const [unexplained, setUnexplained] = useState<UnexplainedCount[] | null>(null);

  useEffect(() => {
    setBlocking(null);
    setUnexplained(null);
  }, [dayId]);

  function refresh() {
    days.reload();
    report.reload();
  }

  let body;
  if (days.state.status === "loading") body = <LoadingState text="Loading business days…" />;
  else if (days.state.status === "forbidden") body = <ForbiddenState what="the reports" onRetry={days.reload} />;
  else if (days.state.status === "error") {
    body = <ErrorState title="Could not load the business days" error={days.state.error} onRetry={days.reload} />;
  } else if (days.state.data.length === 0) {
    body = (
      <section className="state">
        <h3 className="state__title">No business days yet</h3>
        <p className="state__text">A cashier opens the first business day in Cash Register.</p>
      </section>
    );
  } else {
    body = (
      <div className="cx-report-layout">
        <nav className="cx-days" aria-label="Business days">
          <ul className="cx-days__list">
            {days.state.data.map((day) => (
              <li key={day.id}>
                <button
                  type="button"
                  className={`cx-days__button${day.id === dayId ? " cx-days__button--current" : ""}`}
                  aria-current={day.id === dayId ? "true" : undefined}
                  onClick={() => setSelected(day.id)}
                >
                  <span>{formatBusinessDate(day.business_date)}</span>
                  <Chip tone={day.status === "OPEN" ? "ok" : "off"}>{day.status === "OPEN" ? "Open" : "Closed"}</Chip>
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="cx-stack">
          {report.state.status === "loading" && <LoadingState text="Loading the report…" />}
          {report.state.status === "forbidden" && <ForbiddenState what="this report" onRetry={report.reload} />}
          {report.state.status === "error" && (
            <ErrorState title="Could not load the report" error={report.state.error} onRetry={report.reload} />
          )}
          {report.state.status === "ready" && (
            <>
              <DayHeader
                day={report.state.data.business_day}
                user={user}
                token={token}
                store={store}
                onNotice={setNotice}
                onBlocking={setBlocking}
                onUnexplained={setUnexplained}
                onDone={refresh}
              />
              {blocking && blocking.length > 0 && <BlockingOrders orders={blocking} currency={report.state.data.report.currency} />}
              {unexplained && unexplained.length > 0 && (
                <UnexplainedCounts
                  counts={unexplained}
                  user={user}
                  token={token}
                  store={store}
                  onNotice={setNotice}
                  onExplained={(count) => {
                    setUnexplained((current) => (current ?? []).filter((c) => c !== count));
                    refresh();
                  }}
                />
              )}
              <DayReport report={report.state.data.report} user={user} />
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <section className="cx-section" aria-labelledby="cx-reports-title">
      <SectionHeader id="cx-reports-title" title="Reports" />
      <Notice notice={notice} onClose={() => setNotice(null)} />
      {body}
    </section>
  );
}

function DayHeader({
  day,
  user,
  token,
  store,
  onNotice,
  onBlocking,
  onUnexplained,
  onDone,
}: {
  day: BusinessDay;
  user: CurrentUser;
  token: string;
  store: PendingOperationStore;
  onNotice(notice: NoticeMessage | null): void;
  onBlocking(orders: BlockingOrder[] | null): void;
  onUnexplained(counts: UnexplainedCount[] | null): void;
  onDone(): void;
}) {
  const runner = useCashierOperation(store, user, token);
  const [confirming, setConfirming] = useState(false);
  const open = day.status === "OPEN";
  const canClose = open && hasPermission(user, Permission.BusinessDaysClose);

  useEffect(() => setConfirming(false), [day.id]);

  async function close() {
    onNotice(null);
    onBlocking(null);
    onUnexplained(null);
    const outcome = await runner.run(cashierMutations.closeBusinessDay(day.id));
    setConfirming(false);
    if (!outcome) return;
    if (outcome.ok) {
      onNotice({ kind: "success", text: `Business day ${formatBusinessDate(day.business_date)} closed. Its report is now frozen.` });
    } else {
      const code = refusalCode(outcome);
      if (code === "BUSINESS_DAY_HAS_OPEN_ORDERS" && !outcome.ok && isApiError(outcome.error)) {
        onBlocking(blockingOrdersOf(outcome.error.details));
      }
      if (code === "CASH_DIFFERENCE_UNEXPLAINED" && !outcome.ok && isApiError(outcome.error)) {
        onUnexplained(unexplainedCountsOf(outcome.error.details));
      }
      const text = failureText(outcome);
      if (text) {
        const hint =
          code === "CASH_SHIFT_STILL_OPEN"
            ? " Count and close it in Cash Register first."
            : code === "CASH_DIFFERENCE_UNEXPLAINED"
              ? " Record an explanation for each one below."
              : "";
        onNotice({ kind: "error", text: `The business day was not closed: ${text}${hint}` });
      }
    }
    onDone();
  }

  return (
    <Card
      title={formatBusinessDate(day.business_date)}
      className="cx-card--wide"
      status={open ? <Chip tone="ok">Open · figures so far</Chip> : <Chip tone="off">Closed · frozen report</Chip>}
    >
      <Facts
        rows={[
          ["Opened at", formatClock(day.opened_at)],
          day.closed_at ? ["Closed at", formatClock(day.closed_at)] : null,
        ]}
      />
      {canClose &&
        (confirming ? (
          <div className="cx-confirm" role="group" aria-label="Confirm closing the day">
            <p>
              Close the business day of <strong>{formatBusinessDate(day.business_date)}</strong>? Its report is frozen and
              no more payments can be taken in it. This cannot be undone.
            </p>
            <div className="cx-form__actions">
              <button type="button" className="button button--secondary" disabled={runner.busy} onClick={() => setConfirming(false)}>
                Back
              </button>
              <button type="button" className="button button--danger" disabled={runner.busy} onClick={() => void close()}>
                {runner.busy ? "Closing…" : "Confirm close day"}
              </button>
            </div>
          </div>
        ) : (
          <div className="cx-form__actions cx-form__actions--start">
            <button type="button" className="button button--primary" onClick={() => setConfirming(true)}>
              Close business day
            </button>
          </div>
        ))}
    </Card>
  );
}

interface UnexplainedCount {
  cash_shift_id: string;
  kind: "CLOSING" | "HANDOVER";
}

function unexplainedCountsOf(details: unknown): UnexplainedCount[] {
  if (typeof details !== "object" || details === null) return [];
  const counts = (details as { counts?: unknown }).counts;
  return Array.isArray(counts)
    ? (counts as UnexplainedCount[]).filter((c) => c && (c.kind === "CLOSING" || c.kind === "HANDOVER"))
    : [];
}

/** The day's counts whose difference still needs an explanation (no amounts). */
function UnexplainedCounts({
  counts,
  user,
  token,
  store,
  onNotice,
  onExplained,
}: {
  counts: UnexplainedCount[];
  user: CurrentUser;
  token: string;
  store: PendingOperationStore;
  onNotice(notice: NoticeMessage | null): void;
  onExplained(count: UnexplainedCount): void;
}) {
  const runner = useCashierOperation(store, user, token);
  return (
    <Card
      title="Explain these counted differences first"
      className="cx-card--alert"
      status={<Chip tone="warn">{counts.length}</Chip>}
    >
      {counts.map((count) => (
        <ExplainCountForm
          key={`${count.kind}-${count.cash_shift_id}`}
          kind={count.kind === "CLOSING" ? "closing" : "handover"}
          shiftId={count.cash_shift_id}
          user={user}
          runner={runner}
          onNotice={onNotice}
          onDone={() => undefined}
          onRecorded={() => onExplained(count)}
        />
      ))}
    </Card>
  );
}

function blockingOrdersOf(details: unknown): BlockingOrder[] {
  if (typeof details !== "object" || details === null) return [];
  const orders = (details as { orders?: unknown }).orders;
  return Array.isArray(orders) ? (orders as BlockingOrder[]) : [];
}

function BlockingOrders({ orders, currency }: { orders: BlockingOrder[]; currency: string }) {
  return (
    <Card title="Still open — settle, close or void these first" className="cx-card--alert" status={<Chip tone="warn">{orders.length}</Chip>}>
      <ul className="cx-blocking">
        {orders.map((order) => (
          <li key={order.order_id}>
            <span>
              Table {order.table_number}
              {order.table_name ? ` · ${order.table_name}` : ""}
            </span>
            <span className="cx-note">
              {order.bill_status ? `Bill ${order.bill_status.toLowerCase()}` : `Order ${order.order_status.toLowerCase()}, no bill`}
            </span>
            <span className="cx-money">{formatAmount(order.outstanding_amount, currency)} due</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function DayReport({ report, user }: { report: BusinessDayReport; user: CurrentUser }) {
  const currency = report.currency;
  const showExpected = canSeeExpectedCash(user);
  return (
    <>
      <div className="cx-cards">
        <TotalsCard title="Revenue" totals={report.revenue} currency={currency} hint="Payments minus refunds, without tips." />
        <TotalsCard title="Tips" totals={report.net_tips} currency={currency} hint="Net of refunded tips." />
        <TotalsCard title="Refunds" totals={report.refunds} currency={currency} />
        <Card title="Activity">
          <Facts
            rows={[
              ["Payments", String(report.payments_count)],
              ["Refunds", String(report.refunds_count)],
              ["Shifts", String(report.shifts.length)],
              ["Handovers", String(report.handovers.length)],
              showExpected && ["Closing differences", formatAmount(report.closing_difference_total, currency)],
              ["Handover differences", formatAmount(report.handover_difference_total, currency)],
            ]}
          />
        </Card>
      </div>

      <Card title="Cash shifts" status={<Chip tone="off">{report.shifts.length}</Chip>}>
        {report.shifts.length === 0 ? (
          <p className="cx-card__hint">No shift was opened on this day.</p>
        ) : (
          <div className="cx-table-wrap">
            <table className="cx-table">
              <thead>
                <tr>
                  <th scope="col">Cashier</th>
                  <th scope="col" className="cx-table__num">
                    Opening
                  </th>
                  <th scope="col" className="cx-table__num">
                    Cash sales
                  </th>
                  <th scope="col" className="cx-table__num">
                    In / out
                  </th>
                  {showExpected && (
                    <th scope="col" className="cx-table__num">
                      Expected
                    </th>
                  )}
                  <th scope="col" className="cx-table__num">
                    Counted
                  </th>
                  {showExpected && (
                    <th scope="col" className="cx-table__num">
                      Difference
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {report.shifts.map((shift) => (
                  <tr key={shift.cash_shift_id}>
                    <th scope="row">
                      {shift.opened_by ?? "—"}
                      <span className="cx-table__sub">
                        {formatClock(shift.opened_at)} – {shift.closed_at ? formatClock(shift.closed_at) : "open"}
                      </span>
                      {shift.closing?.explanation && <span className="cx-table__sub">“{shift.closing.explanation}”</span>}
                    </th>
                    <td className="cx-table__num">{formatAmount(shift.drawer.effective_opening_amount, currency)}</td>
                    <td className="cx-table__num">{formatAmount(shift.drawer.cash_sales, currency)}</td>
                    <td className="cx-table__num">
                      +{formatAmount(shift.drawer.cash_in, currency)} / −{formatAmount(shift.drawer.cash_out, currency)}
                    </td>
                    {showExpected && (
                      <td className="cx-table__num">{formatAmount(shift.closing?.expected_amount ?? shift.drawer.expected, currency)}</td>
                    )}
                    <td className="cx-table__num">{shift.closing ? formatAmount(shift.closing.counted_amount, currency) : "—"}</td>
                    {showExpected && (
                      <td className="cx-table__num">{shift.closing ? formatAmount(shift.closing.difference, currency) : "—"}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Handovers" status={<Chip tone="off">{report.handovers.length}</Chip>}>
        {report.handovers.length === 0 ? (
          <p className="cx-card__hint">No handover on this day.</p>
        ) : (
          <div className="cx-table-wrap">
            <table className="cx-table">
              <thead>
                <tr>
                  <th scope="col">At</th>
                  <th scope="col">From → to</th>
                  <th scope="col" className="cx-table__num">
                    Handed over
                  </th>
                  <th scope="col" className="cx-table__num">
                    Received
                  </th>
                  <th scope="col" className="cx-table__num">
                    Difference
                  </th>
                  <th scope="col">Reason</th>
                </tr>
              </thead>
              <tbody>
                {report.handovers.map((handover) => (
                  <tr key={handover.id}>
                    <td>{formatClock(handover.created_at)}</td>
                    <td>
                      {handover.outgoing_user ?? "—"} → {handover.incoming_user ?? "—"}
                      {handover.self_confirmed && <span className="cx-table__sub">Self-confirmed</span>}
                    </td>
                    <td className="cx-table__num">{formatAmount(handover.handed_over_amount, currency)}</td>
                    <td className="cx-table__num">{formatAmount(handover.received_amount, currency)}</td>
                    <td className="cx-table__num">{formatAmount(handover.difference, currency)}</td>
                    <td>{handover.reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

function TotalsCard({ title, totals, currency, hint }: { title: string; totals: MethodTotals; currency: string; hint?: string }) {
  return (
    <Card title={title}>
      <p className="cx-total">{formatAmount(totals.total, currency)}</p>
      <Facts
        rows={[
          ["Cash", formatAmount(totals.CASH, currency)],
          ["Card", formatAmount(totals.CARD, currency)],
        ]}
      />
      {hint && <p className="cx-card__hint">{hint}</p>}
    </Card>
  );
}
