import { useEffect, useState } from "react";

/**
 * One-shot browser geolocation. For continuous tracking during a race,
 * a separate `useGPS` hook with `watchPosition` will land in Week 6.
 *
 * @returns {{ position: {lat:number, lon:number, accuracy:number}|null,
 *             error: Error|null,
 *             loading: boolean }}
 */
export function useGeolocation() {
  const [position, setPosition] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!navigator.geolocation) {
      setError(new Error("Geolocation not supported"));
      setLoading(false);
      return;
    }

    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setPosition({
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
        });
        setLoading(false);
      },
      (err) => {
        setError(err);
        setLoading(false);
      },
      { timeout: 8000, maximumAge: 60_000 }
    );
  }, []);

  return { position, error, loading };
}
