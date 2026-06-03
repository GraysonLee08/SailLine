// app/(app)/settings.tsx — route shim for SettingsScreen.
//
// Kept as a thin re-export so the actual UI lives under src/screens/
// (testable + reusable) and the app/ tree is pure routing.

import { SettingsScreen } from "../../src/screens/SettingsScreen";

export default SettingsScreen;
