// The backend stores naive UTC timestamps (datetime.utcnow) and serializes
// them without an offset, e.g. "2026-10-22T18:04:05.123456". Treat a
// timestamp without an offset as UTC rather than browser-local time.
const HAS_OFFSET = /(Z|[+-]\d{2}:?\d{2})$/i;

export function parseApiTimestamp(value: string): Date {
  return new Date(HAS_OFFSET.test(value) ? value : `${value}Z`);
}

/** Minutes elapsed, never negative (clocks may differ slightly). */
export function minutesSince(value: string, now: Date): number {
  const elapsed = now.getTime() - parseApiTimestamp(value).getTime();
  return Math.max(0, Math.floor(elapsed / 60_000));
}

const MINUTES_PER_DAY = 24 * 60;

/**
 * Minutes as "now", "7 min", "1 h 35 min" or, from a day on, "35 d 8 h"
 * (minutes dropped). An unparseable timestamp yields NaN: shown as "—".
 */
export function formatElapsed(minutes: number): string {
  if (!Number.isFinite(minutes)) return "—";
  const whole = Math.floor(minutes);
  if (whole < 1) return "now";
  if (whole < 60) return `${whole} min`;
  if (whole < MINUTES_PER_DAY) {
    const hours = Math.floor(whole / 60);
    const rest = whole % 60;
    return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
  }
  const days = Math.floor(whole / MINUTES_PER_DAY);
  const hours = Math.floor((whole % MINUTES_PER_DAY) / 60);
  return hours === 0 ? `${days} d` : `${days} d ${hours} h`;
}

export function formatClock(value: string): string {
  const date = parseApiTimestamp(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}
