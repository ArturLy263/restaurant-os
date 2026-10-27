import type { Station } from "../api/types";

interface StationPickerProps {
  stations: Station[];
  onSelect(station: Station): void;
}

export function StationPicker({ stations, onSelect }: StationPickerProps) {
  if (stations.length === 0) {
    return (
      <section className="state" aria-live="polite">
        <h2 className="state__title">No stations available</h2>
        <p className="state__text">
          There are no stations you can view. Kitchen and bar staff see only
          the stations a manager has assigned to them.
        </p>
      </section>
    );
  }

  return (
    <section className="picker">
      <h2 className="picker__title">Choose a station</h2>
      <ul className="picker__list">
        {stations.map((station) => (
          <li key={station.id}>
            <button
              type="button"
              className="picker__station"
              onClick={() => onSelect(station)}
            >
              <span className="picker__name">{station.name}</span>
              <span className="picker__meta">
                {station.type}
                {!station.is_active && (
                  <span className="tag tag--muted">Inactive</span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
