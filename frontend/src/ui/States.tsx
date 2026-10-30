import { isApiError } from "../api/client";

export function LoadingState({ text }: { text: string }) {
  return (
    <section className="state" aria-busy="true">
      <div className="spinner" aria-hidden="true" />
      <p className="state__text">{text}</p>
    </section>
  );
}

interface ErrorStateProps {
  title: string;
  error: Error;
  onRetry?: () => void;
  secondary?: { label: string; onClick(): void };
}

export function ErrorState({ title, error, onRetry, secondary }: ErrorStateProps) {
  const requestId = isApiError(error) ? error.requestId : null;
  return (
    <section className="state state--error" role="alert">
      <h2 className="state__title">{title}</h2>
      <p className="state__text">{error.message}</p>
      {requestId && <p className="state__hint">Request ID: {requestId}</p>}
      <div className="state__actions">
        {onRetry && (
          <button type="button" className="button button--primary" onClick={onRetry}>
            Try again
          </button>
        )}
        {secondary && (
          <button type="button" className="button button--secondary" onClick={secondary.onClick}>
            {secondary.label}
          </button>
        )}
      </div>
    </section>
  );
}
