// ThemeProvider.tsx — runtime theme switching + persistence + font loading.
//
// Three modes:
//   "light"  — force light (default)
//   "dark"   — force dark
//   "system" — follow the OS colour scheme
//
// Persisted in AsyncStorage under THEME_KEY. Reads on mount; writes when
// the user picks a new mode. The first render uses "light" so we never
// paint a flash of dark before the persisted value loads — the persisted
// value applies on the next render, which on a real device is one frame
// later (~16ms) and not perceptible.
//
// Also owns Google font loading. Fonts arrive at runtime via
// expo-google-fonts (no .ttf bundling in this repo); useFonts returns
// `loaded=true` after they're cached locally. We render children
// immediately and let the text fall back to the platform font for that
// first paint — preferable to a flash of blank screen.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import { useColorScheme } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";

import {
  useFonts,
  Inter_400Regular,
  Inter_500Medium,
  Inter_600SemiBold,
  Inter_700Bold,
} from "@expo-google-fonts/inter";
import {
  SpaceGrotesk_500Medium,
  SpaceGrotesk_600SemiBold,
  SpaceGrotesk_700Bold,
} from "@expo-google-fonts/space-grotesk";

import { PALETTES, type Palette, type ThemeMode } from "./colors";
import { FONT_FAMILIES, FONT_SIZES, LINE_HEIGHTS, TABULAR_FONT_VARIANT } from "./typography";

const THEME_KEY = "sailline.theme";

export type ThemePref = "light" | "dark" | "system";

type ThemeCtx = {
  mode: ThemeMode;          // resolved (light or dark)
  pref: ThemePref;          // user's preference (incl. system)
  colors: Palette;
  font: typeof FONT_FAMILIES;
  size: typeof FONT_SIZES;
  lineHeight: typeof LINE_HEIGHTS;
  tabularVariant: typeof TABULAR_FONT_VARIANT;
  /** True once Google fonts have finished loading. UI works either way. */
  fontsReady: boolean;
  setPref: (p: ThemePref) => Promise<void>;
};

const Ctx = createContext<ThemeCtx | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const systemScheme = useColorScheme() ?? "light";
  const [pref, setPrefState] = useState<ThemePref>("light");

  const [fontsReady] = useFonts({
    Inter_400Regular,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_700Bold,
    SpaceGrotesk_500Medium,
    SpaceGrotesk_600SemiBold,
    SpaceGrotesk_700Bold,
  });

  // Hydrate persisted preference once on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const stored = await AsyncStorage.getItem(THEME_KEY);
        if (cancelled) return;
        if (stored === "light" || stored === "dark" || stored === "system") {
          setPrefState(stored);
        }
      } catch {
        /* AsyncStorage unavailable — keep light. */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const setPref = useCallback(async (p: ThemePref) => {
    setPrefState(p);
    try {
      await AsyncStorage.setItem(THEME_KEY, p);
    } catch {
      /* best effort */
    }
  }, []);

  const mode: ThemeMode = pref === "system" ? (systemScheme as ThemeMode) : pref;
  const colors = PALETTES[mode];

  const value = useMemo<ThemeCtx>(
    () => ({
      mode,
      pref,
      colors,
      font: FONT_FAMILIES,
      size: FONT_SIZES,
      lineHeight: LINE_HEIGHTS,
      tabularVariant: TABULAR_FONT_VARIANT,
      fontsReady,
      setPref,
    }),
    [mode, pref, colors, fontsReady, setPref],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTheme must be used inside <ThemeProvider>");
  return v;
}
