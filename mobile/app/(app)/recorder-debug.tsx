// app/(app)/recorder-debug.tsx — hidden diagnostic screen.
//
// Not linked from any visible menu. Navigate via `router.push("/recorder-debug")`
// from a debug action you add to the home screen (a hidden gesture, a
// dev-only menu item, or a long-press on the LIVE pill once Phase 3 ships).
//
// All logic lives in RecorderDebugScreen.tsx — this file is just the
// Expo Router shim.

import { RecorderDebugScreen } from "../../src/screens/RecorderDebugScreen";

export default function RecorderDebugRoute() {
  return <RecorderDebugScreen />;
}
