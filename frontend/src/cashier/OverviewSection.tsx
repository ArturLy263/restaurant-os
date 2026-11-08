import type { ReactNode } from "react";

import type { BusinessDay, CashRegister, CashShift, PreviousClosing } from "../api/cashierTypes";
import type { CurrentUser } from "../api/types";
import { formatClock } from "../kds/time";
import { formatAmount } from "../lib/money";
import { ErrorState, LoadingState } from "../ui/States";
import { ForbiddenState } from "./CashierStates";
import { canSeeExpectedCash } from "./sections";
import { useRegister } from "./useRegister";

interface OverviewSectionProps {
  token: string;
  user: CurrentUser;
}

/** Read-only state of the register, the business day and the shift. */
export function OverviewSection({ token, user }: OverviewSectionProps) {
  const { state, retry } = useRegister(token);

  if (state.status === "loading") return <LoadingState text="Loading the register…" />;
  if (state.status === "forbidden") return <ForbiddenState what="the register" onRetry={retry} />;
  if (state.status === "error") {
    return <ErrorState title="Could not load the register" error={state.error} onRetry={retry} />;
  }

  const { register, updatedAt, refreshError } = state;
  // The backend already leaves expected cash out for cashiers; the screen
  // checks the permission as well, so a cached or unexpected response
  // never shows it either.
  const showExpected = canSeeExpectedCash(user);
  const currency = register.shift?.currency ?? null;

  return (
    <section className="cx-section" aria-labelledby="cx-overview-title">
      <header className="cx-section__header">
        <h2 id="cx-overview-title" className="cx-section__title">
          Overview
        </h2>
        <p className="cx-section__meta" aria-live="polite">
          Updated {updatedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
        </p>
      </header>

      {refreshError && (
        <div className="banner banner--error" role="status">
          <span>Could not refresh ({refreshError.message}). Showing the last known state.</span>
          <button type="button" className="button button--ghost button--small" onClick={retry}>
            Refresh
          </button>
        </div>
      )}

      <div className="cx-cards">
        <RegisterCard register={register} />
        <BusinessDayCard day={register.business_day} />
        <ShiftCard shift={register.shift} user={user} showExpected={showExpected} />
        <EmployeeCard user={user} shift={register.shift} />
        {register.previous_closing && (
          <PreviousClosingCard closing={register.previous_closing} currency={currency} showExpected={showExpected} />
        )}
      </div>
    </section>
  );
}

function Card({ title, status, children }: { title: string; status?: ReactNode; children: ReactNode }) {
  const id = `cx-card-${title.toLowerCase().replace(/\W+/g, "-")}`;
  return (
    <article className="cx-card" aria-labelledby={id}>
      <header className="cx-card__header">
        <h3 id={id} className="cx-card__title">
          {title}
        </h3>
        {status}
      </header>
      {children}
    </article>
  );
}

function Chip({ tone, children }: { tone: "ok" | "off" | "warn"; children: ReactNode }) {
  return <span className={`cx-chip cx-chip--${tone}`}>{children}</span>;
}

function Facts({ rows }: { rows: Array<[string, ReactNode] | null | false> }) {
  return (
    <dl className="cx-facts">
      {rows.filter(Boolean).map((row) => {
        const [label, value] = row as [string, ReactNode];
        return (
          <div key={label} className="cx-facts__row">
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        );
      })}
    </dl>
  );
}

function RegisterCard({ register }: { register: CashRegister }) {
  const open = register.shift?.status === "OPEN";
  return (
    <Card
      title="Register"
      status={
        register.register_id === null ? (
          <Chip tone="off">Not set up</Chip>
        ) : open ? (
          <Chip tone="ok">In use</Chip>
        ) : (
          <Chip tone="off">No open shift</Chip>
        )
      }
    >
      <p className="cx-card__text">
        {register.register_id === null
          ? "The register is created when the first shift is opened."
          : open
            ? "A cash shift is open on this register."
            : "No cash shift is open on this register."}
      </p>
    </Card>
  );
}

function BusinessDayCard({ day }: { day: BusinessDay | null }) {
  if (!day) {
    return (
      <Card title="Business day" status={<Chip tone="off">Not open</Chip>}>
        <p className="cx-card__text">No business day is open.</p>
      </Card>
    );
  }
  return (
    <Card
      title="Business day"
      status={<Chip tone={day.status === "OPEN" ? "ok" : "off"}>{day.status === "OPEN" ? "Open" : day.status}</Chip>}
    >
      <Facts rows={[["Date", formatBusinessDate(day.business_date)], ["Opened at", formatClock(day.opened_at)]]} />
    </Card>
  );
}

function ShiftCard({ shift, user, showExpected }: { shift: CashShift | null; user: CurrentUser; showExpected: boolean }) {
  if (!shift) {
    return (
      <Card title="Current shift" status={<Chip tone="off">None</Chip>}>
        <p className="cx-card__text">No cash shift is open.</p>
      </Card>
    );
  }
  const expectedVisible = showExpected && shift.expected_visible && shift.expected_cash !== null;
  const corrected = shift.corrections.length > 0;
  return (
    <Card
      title="Current shift"
      status={<Chip tone={shift.status === "OPEN" ? "ok" : "off"}>{shift.status === "OPEN" ? "Open" : shift.status}</Chip>}
    >
      <Facts
        rows={[
          ["Opened by", shift.opened_by.id === user.id ? "You" : (shift.opened_by.name ?? "—")],
          ["Opened at", formatClock(shift.opened_at)],
          [
            "Opening cash",
            <>
              {formatAmount(shift.effective_opening_amount, shift.currency)}
              {corrected && <span className="cx-note"> (corrected)</span>}
            </>,
          ],
          ["Cash movements", String(shift.movements.filter((m) => m.type === "CASH_IN" || m.type === "CASH_OUT").length)],
          expectedVisible && ["Expected cash", formatAmount(shift.expected_cash, shift.currency)],
        ]}
      />
      {!expectedVisible && <p className="cx-card__hint">Blind count: expected cash is shown to managers only.</p>}
    </Card>
  );
}

function EmployeeCard({ user, shift }: { user: CurrentUser; shift: CashShift | null }) {
  return (
    <Card title="Signed in">
      <Facts
        rows={[
          ["Employee", user.name],
          ["Role", user.role?.name ?? "—"],
          shift && ["Shift owner", shift.opened_by.id === user.id ? "Yes" : "No"],
        ]}
      />
    </Card>
  );
}

function PreviousClosingCard({
  closing,
  currency,
  showExpected,
}: {
  closing: PreviousClosing;
  currency: string | null;
  showExpected: boolean;
}) {
  return (
    <Card
      title="Previous closing"
      status={closing.handover_pending ? <Chip tone="warn">Handover pending</Chip> : <Chip tone="off">Received</Chip>}
    >
      <Facts
        rows={[
          ["Closed by", closing.closed_by.name ?? "—"],
          ["Closed at", formatClock(closing.closed_at)],
          closing.counted_amount !== null && ["Counted", formatAmount(closing.counted_amount, currency)],
          showExpected && closing.expected_amount !== null && ["Expected", formatAmount(closing.expected_amount, currency)],
        ]}
      />
      {closing.handover_pending && (
        <p className="cx-card__hint">The next cashier counts this cash when opening their shift.</p>
      )}
    </Card>
  );
}

/** "2026-11-08" as a local calendar date (no time-zone shift). */
function formatBusinessDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}
