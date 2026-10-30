import { useEffect, type ReactNode } from "react";

interface DialogProps {
  title: string;
  onClose?: () => void;
  children: ReactNode;
}

export function Dialog({ title, onClose, children }: DialogProps) {
  useEffect(() => {
    if (!onClose) return;
    const close = onClose;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") close();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="dialog-backdrop" onClick={(event) => event.target === event.currentTarget && onClose?.()}>
      <section className="dialog" role="dialog" aria-modal="true" aria-label={title}>
        <header className="dialog__header">
          <h2 className="dialog__title">{title}</h2>
          {onClose && (
            <button type="button" className="banner__close" aria-label="Close" onClick={onClose}>
              ×
            </button>
          )}
        </header>
        <div className="dialog__body">{children}</div>
      </section>
    </div>
  );
}

interface StepperProps {
  label: string;
  value: number;
  min: number;
  max: number;
  disabled?: boolean;
  onChange(value: number): void;
}

export function Stepper({ label, value, min, max, disabled, onChange }: StepperProps) {
  return (
    <div className="stepper" role="group" aria-label={label}>
      <button
        type="button"
        className="stepper__button"
        aria-label={`Fewer ${label.toLowerCase()}`}
        disabled={disabled || value <= min}
        onClick={() => onChange(value - 1)}
      >
        −
      </button>
      <span className="stepper__value">{value}</span>
      <button
        type="button"
        className="stepper__button"
        aria-label={`More ${label.toLowerCase()}`}
        disabled={disabled || value >= max}
        onClick={() => onChange(value + 1)}
      >
        +
      </button>
    </div>
  );
}
