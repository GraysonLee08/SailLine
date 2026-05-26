// formatRaceDate.ts — small pure helper to render a race's start_at.
//
// Format target (from the wireframe Grayson approved):
//   "Sat 30 May · 14:00"        when the race is this year
//   "Sat 30 May 2027 · 14:00"   when the race is in a different year
//   "No start time"             when start_at is null/undefined
//   "Invalid date"              when the ISO can't be parsed
//
// Uses the device locale's day-of-week + month names via Intl.
// Time is 24-hour because sailing instructions are universally 24-hour.
//
// Duplicate-but-not-shared with the web's frontend/src/lib/formatRaceDate.ts.
// Kept in mobile/src/lib/ to avoid touching the @sailline/shared package
// tonight (which would require a web rebuild + vitest verification).
// Both copies will collapse to packages/shared in a later session.

/**
 * Render a race start_at ISO string for display.
 * @param iso ISO 8601 string or null/undefined.
 * @param now reference "now" — defaults to Date.now(); injectable for tests.
 */
export function formatRaceDate(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return "No start time";

  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Invalid date";

  const weekday = d.toLocaleDateString(undefined, { weekday: "short" });
  const day = d.getDate();
  const month = d.toLocaleDateString(undefined, { month: "short" });

  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const time = `${hh}:${mm}`;

  const yearSuffix = d.getFullYear() !== now.getFullYear() ? ` ${d.getFullYear()}` : "";

  return `${weekday} ${day} ${month}${yearSuffix} · ${time}`;
}
