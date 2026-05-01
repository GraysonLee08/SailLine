// Lat/lon helpers.
//
// Inputs accept either decimal degrees ("41.85283", "-87.55683") or the
// degrees + decimal-minutes format sailors actually use ("41 51.17",
// "41°51.17'", "87 33.41 W"). Storage and the API are always decimal degrees.
// Hover popups display deg-min so they match the race book.

// Parse a lat or lon string. Returns NaN if unparseable.
//
// Accepted forms (whitespace, °, and ' are interchangeable separators):
//   "41.85283"           → 41.85283
//   "-87.55683"          → -87.55683
//   "41 51.17"           → 41.85283
//   "41°51.17'"          → 41.85283
//   "41° 51.17' N"       → 41.85283
//   "87 33.41 W"         → -87.55683
//   "S 41 51.17"         → -41.85283
export function parseCoord(input) {
  if (input == null) return NaN;
  const s = String(input).trim();
  if (s === "") return NaN;

  // Pure decimal — fast path.
  if (/^-?\d+(\.\d+)?$/.test(s)) return parseFloat(s);

  // Optional leading hemisphere letter / sign, then degrees, then optional
  // minutes, then optional trailing hemisphere letter.
  const re = /^([+-NSEW])?\s*(\d+(?:\.\d+)?)(?:\s*[°\s]\s*(\d+(?:\.\d+)?))?\s*'?\s*([NSEW])?$/i;
  const m = s.match(re);
  if (!m) return NaN;
  const [, lead, degStr, minStr, trail] = m;

  let value = parseFloat(degStr);
  if (minStr != null) value += parseFloat(minStr) / 60;

  const hemi = (trail || lead || "").toUpperCase();
  if (hemi === "-" || hemi === "S" || hemi === "W") value = -value;
  return value;
}

// Format a signed decimal degree as "41°51.17' N" / "87°33.41' W".
function formatDM(value, posHemi, negHemi) {
  if (!Number.isFinite(value)) return "—";
  const sign = value < 0 ? -1 : 1;
  const abs = Math.abs(value);
  const deg = Math.floor(abs);
  const min = (abs - deg) * 60;
  return `${deg}°${min.toFixed(2)}' ${sign >= 0 ? posHemi : negHemi}`;
}

export const formatLat = (v) => formatDM(v, "N", "S");
export const formatLon = (v) => formatDM(v, "E", "W");

// "41.85283" with up to 5 decimals, trailing zeros trimmed. Used for the
// editable input value so users see a clean number.
export function formatDecimal(v) {
  if (!Number.isFinite(v)) return "";
  return parseFloat(v.toFixed(5)).toString();
}
