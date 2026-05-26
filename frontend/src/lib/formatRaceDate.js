// formatRaceDate.js — render a race's start_at as a short, friendly string.
//
// Output examples:
//   "Sat 30 May · 14:00"        when the race is this year
//   "Sat 30 May 2027 · 14:00"   when the race is in a different year
//   "No start time"             when start_at is null/undefined
//   "Invalid date"              when the ISO can't be parsed
//
// Day names + month names come from the device locale (Intl). Time is
// 24-hour because sailing instructions are universally 24-hour and it
// avoids am/pm ambiguity at gun time.
//
// Duplicate-but-not-shared with mobile/src/lib/formatRaceDate.ts.
// Both copies will collapse into packages/shared in a later session;
// keeping them separate tonight avoids touching the shared package
// (which would require a synchronised mobile + web verification on
// Windows that I can't do from this sandbox).

/**
 * @param {string|null|undefined} iso ISO 8601 string or empty.
 * @param {Date} [now=new Date()] reference "now" (injectable for tests).
 * @returns {string} A human-readable date string.
 */
export function formatRaceDate(iso, now = new Date()) {
  if (!iso) return "No start time";

  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Invalid date";

  const weekday = d.toLocaleDateString(undefined, { weekday: "short" });
  const day = d.getDate();
  const month = d.toLocaleDateString(undefined, { month: "short" });

  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const time = `${hh}:${mm}`;

  const yearSuffix =
    d.getFullYear() !== now.getFullYear() ? ` ${d.getFullYear()}` : "";

  return `${weekday} ${day} ${month}${yearSuffix} · ${time}`;
}
