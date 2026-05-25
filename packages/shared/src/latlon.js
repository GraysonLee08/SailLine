// Lat/lon helpers.
//
// Inputs accept either decimal degrees ("41.85283", "-87.55683") or the
// degrees + decimal-minutes format sailors actually use ("41 51.17",
// "41°51.17'", "41 51.17 N"). Storage and the API are always decimal
// degrees. The display format for inputs is user-toggleable; the popup
// always uses deg-min so it matches the race book.

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

// ── Display formatters ──────────────────────────────────────────────

// Pretty deg-min for popups: "41°51.17' N" / "87°33.41' W".
// Uses unicode °/' symbols; not meant to be re-typed by users.
function formatDMPretty(value, posHemi, negHemi) {
  if (!Number.isFinite(value)) return "—";
  const sign = value < 0 ? -1 : 1;
  const abs = Math.abs(value);
  const deg = Math.floor(abs);
  const min = (abs - deg) * 60;
  return `${deg}°${min.toFixed(2)}' ${sign >= 0 ? posHemi : negHemi}`;
}
export const formatLat = (v) => formatDMPretty(v, "N", "S");
export const formatLon = (v) => formatDMPretty(v, "E", "W");

// Plain deg-min for editable inputs: "41 51.170 N" / "87 33.410 W".
// Space-separated and ASCII so users can edit without fighting symbols.
// 3 decimals on minutes preserves ~2m precision when round-tripping
// from decimal degrees.
function formatDMInput(value, posHemi, negHemi) {
  if (!Number.isFinite(value)) return "";
  const sign = value < 0 ? -1 : 1;
  const abs = Math.abs(value);
  const deg = Math.floor(abs);
  const min = (abs - deg) * 60;
  return `${deg} ${min.toFixed(3)} ${sign >= 0 ? posHemi : negHemi}`;
}
export const formatLatInput = (v) => formatDMInput(v, "N", "S");
export const formatLonInput = (v) => formatDMInput(v, "E", "W");

// Decimal degrees with up to 5 places, trailing zeros trimmed.
export function formatDecimal(v) {
  if (!Number.isFinite(v)) return "";
  return parseFloat(v.toFixed(5)).toString();
}
