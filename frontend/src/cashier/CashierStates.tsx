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
