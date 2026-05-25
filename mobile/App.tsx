import { StatusBar } from "expo-status-bar";
import { ScrollView, StyleSheet, Text, View } from "react-native";

// Proof-of-wiring: pull real logic/data from the shared workspace package.
// If this screen renders these values, the monorepo + @sailline/shared +
// Metro resolution are all working end to end.
import {
  MORF_MARK_LIST,
  BOAT_CLASSES,
  baseRegionForPoint,
  parseCoord,
} from "@sailline/shared";

export default function App() {
  // Chicago lakefront — should resolve to the CONUS base region.
  const region = baseRegionForPoint(41.935, -87.677);
  const parsed = parseCoord("41 56.10N");
  const markCount = Array.isArray(MORF_MARK_LIST) ? MORF_MARK_LIST.length : 0;
  const boatCount = Array.isArray(BOAT_CLASSES) ? BOAT_CLASSES.length : 0;

  return (
    <ScrollView contentContainerStyle={styles.container}>
      <Text style={styles.title}>SailLine mobile</Text>
      <Text style={styles.subtitle}>@sailline/shared wiring check</Text>

      <View style={styles.row}>
        <Text style={styles.label}>Base region (Chicago)</Text>
        <Text style={styles.value}>{String(region)}</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>parseCoord("41 56.10N")</Text>
        <Text style={styles.value}>{JSON.stringify(parsed)}</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>MORF marks loaded</Text>
        <Text style={styles.value}>{markCount}</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>Boat classes loaded</Text>
        <Text style={styles.value}>{boatCount}</Text>
      </View>

      <Text style={styles.note}>
        Scaffold only — no capture, routing, or auth yet. See the mobile
        development plan.
      </Text>
      <StatusBar style="auto" />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: {
    flexGrow: 1,
    backgroundColor: "#0b1f2a",
    paddingTop: 72,
    paddingHorizontal: 24,
    gap: 14,
  },
  title: { color: "#f5f7fa", fontSize: 28, fontWeight: "700" },
  subtitle: { color: "#8fb4c7", fontSize: 14, marginBottom: 12 },
  row: {
    backgroundColor: "#13303f",
    borderRadius: 10,
    padding: 14,
  },
  label: { color: "#8fb4c7", fontSize: 12, marginBottom: 4 },
  value: { color: "#f5f7fa", fontSize: 16, fontWeight: "600" },
  note: { color: "#5e7d8c", fontSize: 12, marginTop: 20 },
});
