import type { ReactNode } from "react";

/** A 403 from the server: the role cannot use this (any more). */
export function ForbiddenState({ what, onRetry }: { what: string; onRetry?: () => void }) {
  return (
    <section className="state" role="alert">
      <h2 className="state__title">No access</h2>
      <p className="state__text">
        Your role does not have access to {what}. If your permissions were changed recently, sign out and sign in
        again, or ask a manager.
      </p>
      {onRetry && (
        <div className="state__actions">
          <button type="button" className="button button--secondary" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
    </section>
  );
}

/** A section that exists in the navigation but is built in a later stage. */
export function PlaceholderSection({ title, children }: { title: string; children: ReactNode }) {
  const id = `cx-${title.toLowerCase().replace(/\W+/g, "-")}-title`;
  return (
    <section className="cx-section" aria-labelledby={id}>
      <header className="cx-section__header">
        <h2 id={id} className="cx-section__title">
          {title}
        </h2>
      </header>
      <div className="cx-placeholder">
        <p className="cx-placeholder__badge">Coming soon</p>
        <p className="cx-placeholder__text">{children}</p>
      </div>
    </section>
  );
}
