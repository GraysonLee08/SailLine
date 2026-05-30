// app/(app)/race-edit.tsx — race editor route.
//
// `?id=<uuid>` → edit existing race.
// No query param      → new race.
//
// All logic lives in src/screens/RaceEditScreen.tsx. This file is just
// the Expo Router shim that reads the search param and forwards it.

import { useLocalSearchParams } from "expo-router";

import { RaceEditScreen } from "../../src/screens/RaceEditScreen";

export default function RaceEditRoute() {
  const params = useLocalSearchParams<{ id?: string }>();
  const id = typeof params.id === "string" && params.id ? params.id : undefined;
  return <RaceEditScreen raceId={id} />;
}
