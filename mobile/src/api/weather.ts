// api/weather.ts — typed wrapper for GET /api/weather?region=...
//
// Returns the regridded wind grid the backend serves from Redis (HRRR or
// GFS depending on region). Shape:
//   {
//     lats: number[],       // monotonic
//     lons: number[],       // monotonic
//     u:    number[][],     // [latIdx][lonIdx], m/s
//     v:    number[][],     // [latIdx][lonIdx], m/s
//     valid_time?: string,  // ISO
//     source?: string,      // "hrrr" | "gfs"
//   }
//
// The mobile client uses this to feed `computeBarbFeatures` for the
// wind overlay. Errors surface as throws — caller decides whether to
// render an empty overlay or a banner.

import { apiFetch } from "../api";

export type WindGrid = {
  lats: number[];
  lons: number[];
  u: number[][];
  v: number[][];
  valid_time?: string;
  source?: string;
};

export type WeatherOptions = {
  /** Optional override; backend picks default per region otherwise. */
  source?: "hrrr" | "gfs";
};

export async function getWeather(
  region: string,
  options: WeatherOptions = {},
): Promise<WindGrid> {
  const q = new URLSearchParams({ region });
  if (options.source) q.set("source", options.source);
  const data = await apiFetch<WindGrid>(`/api/weather?${q.toString()}`);
  if (!data) throw new Error("weather returned empty body");
  return data;
}
