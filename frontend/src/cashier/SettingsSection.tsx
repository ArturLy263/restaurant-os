import { useState } from "react";

import { useTheme, type Theme } from "../theme/theme";

const OPTIONS: Array<{ value: Theme; label: string; hint: string }> = [
  { value: "dark", label: "Premium Dark", hint: "Default; easy on the eyes in dim rooms." },
  { value: "light", label: "Light", hint: "For bright counters and daylight." },
];

export function SettingsSection() {
  const [theme, setTheme] = useTheme();
  const [notStored, setNotStored] = useState(false);

  return (
    <section className="cx-section" aria-labelledby="cx-settings-title">
      <header className="cx-section__header">
        <h2 id="cx-settings-title" className="cx-section__title">
          Settings
        </h2>
      </header>
      <fieldset className="cx-fieldset">
        <legend className="cx-fieldset__legend">Theme</legend>
        <p className="cx-card__hint">Saved on this device. The waiter POS and kitchen display keep their own look.</p>
        <div className="cx-options">
          {OPTIONS.map((option) => (
            <label key={option.value} className={`cx-option${theme === option.value ? " cx-option--current" : ""}`}>
              <input
                type="radio"
                name="cx-theme"
                value={option.value}
                checked={theme === option.value}
                onChange={() => setNotStored(!setTheme(option.value))}
              />
              <span className="cx-option__label">{option.label}</span>
              <span className="cx-option__hint">{option.hint}</span>
            </label>
          ))}
        </div>
        {notStored && (
          <p className="cx-card__hint" role="status">
            This browser does not allow saving the choice; it applies until the page is reloaded.
          </p>
        )}
      </fieldset>
    </section>
  );
}
