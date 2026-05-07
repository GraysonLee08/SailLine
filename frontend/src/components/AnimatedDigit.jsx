// frontend/src/components/AnimatedDigit.jsx
//
// Slides its `value` up + fades in whenever the value changes. Used by
// the countdown to draw the eye to the ticking seconds.
//
// `display: inline-block` is required so the transform applies — inline
// elements ignore translate.

import { useEffect, useRef } from "react";
import { safeAnimate, EASE_OUT_SOFT } from "../lib/motion";

export function AnimatedDigit({ value, className, style }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return;
    safeAnimate(ref.current, {
      translateY: ["-1em", "0em"],
      opacity: [0, 1],
      duration: 200,
      easing: EASE_OUT_SOFT,
    });
  }, [value]);

  return (
    <span
      ref={ref}
      className={className}
      style={{ display: "inline-block", ...style }}
    >
      {value}
    </span>
  );
}

// Splits an HH:MM:SS or "Nd HH:MM:SS" countdown label into the static
// prefix ("HH:MM:" or "Nd HH:MM:") and the trailing two-digit seconds.
// Returns { prefix: null, seconds: null } for non-tick labels like
// "Race ended" or "No start time set" — caller should fall back to
// rendering the raw label.
export function splitSecondsFromCountdown(label) {
  if (!label) return { prefix: null, seconds: null };
  const match = label.match(/^(.*:)(\d{2})$/);
  if (!match) return { prefix: null, seconds: null };
  return { prefix: match[1], seconds: match[2] };
}
