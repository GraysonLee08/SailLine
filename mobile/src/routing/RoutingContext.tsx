// RoutingContext.tsx — hoists routing + better-route SSE to a layout-level
// lifetime so the computed plan AND live re-route updates survive navigation
// between the map home and the recording screen.
//
// Why this lives above the screens (mirrors RecorderContext):
//
//   useRouting holds the computed route in component-local state and
//   useRouteNotifications holds an open SSE connection. Both were previously
//   instantiated per-screen, which meant:
//     * the orange plan the user computed on home vanished the moment they
//       navigated to /recording (a fresh useRouting started at route=null),
//       and
//     * no "faster route" SSE alerts reached the recording screen at all
//       (its hook instance never opened the stream).
//
//   Hoisting both here gives a single instance keyed on the selected race id
//   that every screen under the provider reads. The SSE connection now also
//   persists across Home -> Recording -> Home navigation instead of being
//   torn down and reopened each time a screen unmounts.
//
// accept() is wired internally: the SSE accept handler feeds the alternative
// Feature straight into useRouting.applyAlternative, so consumers just call
// acceptAlternative() and read the updated `route`.
//
// Auto-compute on race select (step "2.5", 2026-06-11): when the selected
// race changes to a pre-race candidate (>= 2 marks, not finished), the
// route computes itself in quiet mode — failures log but never surface
// error UI; the 425 forecast-pending state still shows (the sheet renders
// it as a friendly message, and the manual Compute button stays available).

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
} from "react";
import type { ReactNode } from "react";

import { useRecorder } from "../recorder/RecorderContext";
import { useRouting } from "../hooks/useRouting";
import { useRouteNotifications } from "../hooks/useRouteNotifications";
import type {
  AlternativePayload,
  TacticsPayload,
} from "../hooks/useRouteNotifications";
import type { ComputeOptions } from "../hooks/useRouting";
import type { RouteFeature, RouteMeta } from "../api/routing";

type Pending = {
  detail: string;
  availableAt: string;
  hoursUntilAvailable: number;
};

type RoutingCtx = {
  // ── computed route (POST /api/routing/compute) ──
  route: RouteFeature | null;
  meta: RouteMeta | null;
  pending: Pending | null;
  loading: boolean;
  error: string | null;
  compute: (opts?: ComputeOptions) => Promise<void>;
  clear: () => void;
  // ── better-route SSE (/api/routing/notifications/{raceId}) ──
  alternative: AlternativePayload | null;
  /** Adopt the current alternative as the active route, then clear it. */
  acceptAlternative: () => void;
  /** Discard the current alternative without applying it. */
  dismissAlternative: () => void;
  // ── AI tactician calls (same SSE stream, 2026-06-11) ──
  tactics: TacticsPayload | null;
  dismissTactics: () => void;
  notificationsError: string | null;
};

const Ctx = createContext<RoutingCtx | null>(null);

export function RoutingProvider({ children }: { children: ReactNode }) {
  const { selectedRace } = useRecorder();
  const raceId = selectedRace?.id ?? null;

  const routing = useRouting(raceId);
  const notifications = useRouteNotifications(raceId);

  // The auto-compute effect below must key on raceId, not the race object:
  // a list refresh can replace the object with an identical race, and
  // refiring on identity change would clear (and needlessly recompute) a
  // plan the user is looking at. The ref gives the effect the current
  // race fields without adding the object to its dependency array.
  const selectedRaceRef = useRef(selectedRace);
  selectedRaceRef.current = selectedRace;

  // Clear any stale route when the selected race changes so a newly picked
  // race never briefly shows the previous race's polyline, then quietly
  // auto-compute for the new race. Navigation between Home and Recording
  // does NOT change raceId (both read the same selectedRace from
  // RecorderContext), so the computed plan is preserved across that
  // transition — only an actual race switch clears and recomputes.
  //
  // Skips:
  //   * no race selected (clear only),
  //   * finished races (ended_at set — those open the Review screen),
  //   * races with < 2 marks (the backend would 400 with "at least
  //     2 marks"; skipping avoids a guaranteed-failure request).
  const { clear, compute } = routing;
  useEffect(() => {
    clear();
    if (!raceId) return;
    const race = selectedRaceRef.current;
    if (!race || race.ended_at) return;
    if (!Array.isArray(race.marks) || race.marks.length < 2) return;
    void compute({ quiet: true });
  }, [raceId, clear, compute]);

  // Bridge SSE -> routing state: applying an alternative rehydrates meta from
  // the Feature's properties (see useRouting.applyAlternative).
  const { accept } = notifications;
  const { applyAlternative } = routing;
  const acceptAlternative = useCallback(() => {
    accept((feature) => applyAlternative(feature));
  }, [accept, applyAlternative]);

  const value = useMemo<RoutingCtx>(
    () => ({
      route: routing.route,
      meta: routing.meta,
      pending: routing.pending,
      loading: routing.loading,
      error: routing.error,
      compute: routing.compute,
      clear: routing.clear,
      alternative: notifications.alternative,
      acceptAlternative,
      dismissAlternative: notifications.dismiss,
      tactics: notifications.tactics,
      dismissTactics: notifications.dismissTactics,
      notificationsError: notifications.error,
    }),
    [
      routing.route,
      routing.meta,
      routing.pending,
      routing.loading,
      routing.error,
      routing.compute,
      routing.clear,
      notifications.alternative,
      notifications.dismiss,
      notifications.tactics,
      notifications.dismissTactics,
      notifications.error,
      acceptAlternative,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRoute(): RoutingCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useRoute must be used inside <RoutingProvider>");
  return v;
}
