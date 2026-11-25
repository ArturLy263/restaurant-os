import { useId, type ReactNode } from "react";

import { isApiError } from "../api/client";
import { parseAmountInput } from "../lib/money";

// Building blocks shared by the cashier sections, in the workspace's
// visual language (cx-* classes in styles.css).

export function Card({
  title,
  status,
  children,
  className,
}: {
  title: string;
  status?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const id = `cx-card-${title.toLowerCase().replace(/\W+/g, "-")}`;
  return (
    <article className={`cx-card${className ? ` ${className}` : ""}`} aria-labelledby={id}>
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

export function Chip({ tone, children }: { tone: "ok" | "off" | "warn"; children: ReactNode }) {
  return <span className={`cx-chip cx-chip--${tone}`}>{children}</span>;
}

export function Facts({ rows }: { rows: Array<[string, ReactNode] | null | false> }) {
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

/** The title row every cashier section starts with. */
export function SectionHeader({ id, title, meta }: { id: string; title: string; meta?: ReactNode }) {
  return (
    <header className="cx-section__header">
      <h2 id={id} className="cx-section__title">
        {title}
      </h2>
      {meta && (
        <div className="cx-section__meta" aria-live="polite">
          {meta}
        </div>
      )}
    </header>
  );
}

type NoticeKind = "success" | "error" | "info";

export interface NoticeMessage {
  kind: NoticeKind;
  text: string;
}

/** A one-line outcome of the last action; errors are announced at once. */
export function Notice({ notice, onClose }: { notice: NoticeMessage | null; onClose?: () => void }) {
  if (!notice) return null;
  return (
    <div className={`banner banner--${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}>
      <span>{notice.text}</span>
      {onClose && (
        <button type="button" className="banner__close" aria-label="Dismiss message" onClick={onClose}>
          ×
        </button>
      )}
    </div>
  );
}

/**
 * A money input. Keeps the typed text; `cents` is null while it is not a
 * valid amount. The error is shown only once the field was submitted.
 */
export function AmountField({
  label,
  value,
  onChange,
  invalid,
  hint,
  disabled,
  autoFocus,
}: {
  label: string;
  value: string;
  onChange(value: string): void;
  invalid?: string | null;
  hint?: ReactNode;
  disabled?: boolean;
  autoFocus?: boolean;
}) {
  const id = useId();
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="field__input cx-amount"
        inputMode="decimal"
        autoComplete="off"
        placeholder="0.00"
        value={value}
        disabled={disabled}
        autoFocus={autoFocus}
        aria-invalid={invalid ? true : undefined}
        aria-describedby={invalid || hint ? `${id}-help` : undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {(invalid || hint) && (
        <p id={`${id}-help`} className={invalid ? "cx-field-error" : "cx-field-hint"}>
          {invalid ?? hint}
        </p>
      )}
    </div>
  );
}

export function TextField({
  label,
  value,
  onChange,
  invalid,
  hint,
  disabled,
  multiline,
  maxLength = 500,
}: {
  label: string;
  value: string;
  onChange(value: string): void;
  invalid?: string | null;
  hint?: ReactNode;
  disabled?: boolean;
  multiline?: boolean;
  maxLength?: number;
}) {
  const id = useId();
  const common = {
    id,
    value,
    disabled,
    maxLength,
    "aria-invalid": invalid ? true : undefined,
    "aria-describedby": invalid || hint ? `${id}-help` : undefined,
  } as const;
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {multiline ? (
        <textarea
          {...common}
          className="field__input field__textarea"
          rows={2}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input {...common} className="field__input" onChange={(event) => onChange(event.target.value)} />
      )}
      {(invalid || hint) && (
        <p id={`${id}-help`} className={invalid ? "cx-field-error" : "cx-field-hint"}>
          {invalid ?? hint}
        </p>
      )}
    </div>
  );
}

/** The validation message of an amount field, or null when valid. */
export function amountProblem(text: string, { allowZero = true }: { allowZero?: boolean } = {}): string | null {
  const cents = parseAmountInput(text);
  if (text.trim() === "") return "Enter an amount.";
  if (cents === null) return "Enter an amount such as 12.50 (at most two decimals).";
  if (!allowZero && cents === 0) return "The amount must be greater than zero.";
  return null;
}

/** The message to show for a failed request, with its request id when known. */
export function errorText(error: unknown): string {
  if (isApiError(error)) return error.requestId ? `${error.message} (request ${error.requestId})` : error.message;
  return error instanceof Error ? error.message : String(error);
}

/** "2026-11-08" as a local calendar date (no time-zone shift). */
export function formatBusinessDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}
