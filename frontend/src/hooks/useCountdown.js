// useCountdown — live countdown to a target ISO timestamp.
//
// Re-renders once per second while the target is in the future. Returns
// formatted strings the UI drops in directly — the consumer doesn't have
// to know about Date arithmetic.
//
// Three states the UI cares about:
//   - isUnset:  no target supplied → "No start time set"
//   - isPast:   target time has passed → "Race in progress" (within the
//               6h ongoing window) or "Race ended" (after that)
//   - default:  formatted countdown like "00:23:45" or "1d 04:12:00"

import { useEffect, useState } from "react";

const ONGOING_GRACE_MS = 6 * 60 * 60 * 1000;

export function useCountdown(targetIso) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!targetIso) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [targetIso]);

  if (!targetIso) {
    return { label: "No start time set", isUnset: true, isPast: false, msUntil: null };
  }

  const target = new Date(targetIso).getTime();
  if (Number.isNaN(target)) {
    return { label: "Invalid start time", isUnset: false, isPast: false, msUntil: null };
  }

  const ms = target - now;
  if (ms <= 0) {
    const sincePast = -ms;
    if (sincePast < ONGOING_GRACE_MS) {
      return { label: "Race in progress", isPast: true, isUnset: false, msUntil: ms };
    }
    return { label: "Race ended", isPast: true, isUnset: false, msUntil: ms };
  }

  return { label: formatHMS(ms), isPast: false, isUnset: false, msUntil: ms };
}

function formatHMS(ms) {
  const totalSec = Math.floor(ms / 1000);
  const days = Math.floor(totalSec / 86400);
  const h = Math.floor((totalSec % 86400) / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  if (days > 0) return `${days}d ${pad(h)}:${pad(m)}:${pad(s)}`;
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}
