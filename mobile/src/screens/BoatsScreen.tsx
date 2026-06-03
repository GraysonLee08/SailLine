// BoatsScreen.tsx — list of the user's boats.
//
// 2026-06-03 — minimum-viable read-only list. The mobile boats API
// (`src/api/boats.ts`) only exposes a list endpoint today; create and
// edit live in the webapp. This screen shows what the user has and
// points them at the web for edits. Backend has the full CRUD; mobile
// just hasn't wrapped it yet.
//
// Flagged in the session summary as a follow-up: wrap POST/PATCH/DELETE
// on /api/boats and add a real BoatEditScreen next session. For
// race-day usefulness today, just showing the boat list and letting
// the user confirm "yes my Tartan 4100 is here" closes the immediate
// gap.

import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { router } from "expo-router";

import { listBoats, type BoatOption } from "../api/boats";
import { useTheme } from "../theme/ThemeProvider";

export function BoatsScreen() {
  const { colors, font, size } = useTheme();
  const insets = useSafeAreaInsets();

  const [boats, setBoats] = useState<BoatOption[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    const data = await listBoats();
    setBoats(data);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  }, [load]);

  return (
    <View style={[styles.root, { backgroundColor: colors.surface.page }]}>
      <View
        style={[
          styles.header,
          { paddingTop: insets.top + 8, borderColor: colors.border.hairline },
        ]}
      >
        <Pressable
          onPress={() => router.back()}
          accessibilityRole="button"
          accessibilityLabel="Back"
          hitSlop={12}
        >
          <Ionicons name="chevron-back" size={26} color={colors.text.primary} />
        </Pressable>
        <Text
          style={{
            color: colors.text.primary,
            fontFamily: font.displaySemibold,
            fontSize: size.title,
          }}
        >
          Boats
        </Text>
        <View style={{ width: 26 }} />
      </View>

      {boats === null ? (
        <View style={styles.center}>
          <ActivityIndicator color={colors.accent.primary} />
        </View>
      ) : boats.length === 0 ? (
        <View style={styles.center}>
          <Ionicons name="boat-outline" size={48} color={colors.text.muted} />
          <Text
            style={{
              color: colors.text.primary,
              fontFamily: font.bodySemibold,
              fontSize: size.bodyLg,
              marginTop: 16,
              textAlign: "center",
            }}
          >
            No boats yet
          </Text>
          <Text
            style={{
              color: colors.text.muted,
              fontFamily: font.body,
              fontSize: size.body,
              marginTop: 4,
              textAlign: "center",
              paddingHorizontal: 24,
            }}
          >
            Add a boat in the web app — mobile editing arrives in a
            follow-up.
          </Text>
        </View>
      ) : (
        <FlatList
          data={boats}
          keyExtractor={(b) => b.id}
          contentContainerStyle={[
            styles.listBody,
            { paddingBottom: insets.bottom + 24 },
          ]}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor={colors.accent.primary}
            />
          }
          ListHeaderComponent={
            <Text
              style={{
                color: colors.text.muted,
                fontFamily: font.body,
                fontSize: size.small,
                marginBottom: 12,
              }}
            >
              Mobile boat editing arrives in a follow-up. For now this is
              a read-only roster — tap "Add / edit" on the web to make
              changes.
            </Text>
          }
          renderItem={({ item }) => (
            <View
              style={[
                styles.row,
                {
                  backgroundColor: colors.surface.elevated,
                  borderColor: colors.border.hairline,
                },
              ]}
            >
              <Ionicons
                name="boat"
                size={22}
                color={colors.accent.primary}
                style={{ width: 28 }}
              />
              <View style={{ flex: 1 }}>
                <Text
                  style={{
                    color: colors.text.primary,
                    fontFamily: font.bodyMedium,
                    fontSize: size.body,
                  }}
                  numberOfLines={1}
                >
                  {item.name}
                </Text>
                {item.sail_number ? (
                  <Text
                    style={{
                      color: colors.text.muted,
                      fontFamily: font.body,
                      fontSize: size.small,
                      marginTop: 2,
                    }}
                  >
                    Sail # {item.sail_number}
                  </Text>
                ) : null}
              </View>
            </View>
          )}
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 14,
    paddingBottom: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  listBody: {
    paddingHorizontal: 18,
    paddingTop: 16,
    gap: 10,
  },
  center: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 14,
    paddingVertical: 14,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
  },
});
