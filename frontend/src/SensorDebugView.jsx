// SensorDebugView — hidden diagnostic page for verifying IMU sensor
// reads on a real device. Reachable only via the URL parameter
// `?debug=sensors`. No UI entry point by design — useful in production
// for support ("open this URL and read me the values").
//
// Step 2 exit criteria this view validates:
//   - Platform detection picks the right API path on iPhone vs Android.
//   - iOS permission prompt works when bound to a user gesture.
//   - 10 Hz samples flow.
//   - Screen sleep stops the stream cleanly; wake resumes cleanly.

import { useEffect, useRef, useState } from "react";
import {
    detectPlatform,
    needsPermissionPrompt,
    requestPermission,
    start,
    createComplementaryFilter,
} from "./sensors/imu";

const RATE_HZ = 10;

export default function SensorDebugView() {
    const [platform] = useState(() => detectPlatform());
    const [permissionState, setPermissionState] = useState(
        needsPermissionPrompt() ? "prompt-required" : "not-needed",
    );
    const [running, setRunning] = useState(false);
    const [error, setError] = useState(null);
    const [sample, setSample] = useState(null);
    const [filtered, setFiltered] = useState(null);
    const [sampleCount, setSampleCount] = useState(0);
    const [observedHz, setObservedHz] = useState(0);

    // Refs for things the sampler callback closes over but shouldn't
    // trigger re-renders on every tick.
    const handleRef = useRef(null);
    const filterRef = useRef(null);
    const countRef = useRef(0);
    const lastRateCheckRef = useRef(null);

    const onStart = async () => {
        setError(null);

        // iOS: prompt for permission inside the user-gesture handler.
        let result = "not-needed";
        if (needsPermissionPrompt()) {
            result = await requestPermission();
            setPermissionState(result);
            if (result !== "granted") return;
        }

        filterRef.current = createComplementaryFilter();
        countRef.current = 0;
        lastRateCheckRef.current = performance.now();

        handleRef.current = start({
            rateHz: RATE_HZ,
            onSample: (s) => {
                setSample(s);
                setFiltered(filterRef.current.step(s));
                countRef.current += 1;

                // Update observed rate ~once per second so the display number
                // is stable rather than oscillating sample-to-sample.
                const now = performance.now();
                if (now - lastRateCheckRef.current >= 1000) {
                    setObservedHz(
                        countRef.current / ((now - lastRateCheckRef.current) / 1000),
                    );
                    countRef.current = 0;
                    lastRateCheckRef.current = now;
                    setSampleCount((c) => c + Math.round(RATE_HZ)); // approx, for the badge
                }
            },
            onError: (err) => setError(err.message || String(err)),
        });
        setRunning(true);
    };

    const onStop = () => {
        handleRef.current?.stop();
        handleRef.current = null;
        setRunning(false);
        setObservedHz(0);
    };

    // Stop the sampler on unmount — leaving sensors running after the
    // user backs out would be both wasteful and a privacy concern.
    useEffect(() => () => handleRef.current?.stop(), []);

    // Surface tab-visibility changes so we can verify "sleep stops, wake
    // resumes" behavior on a real device. We don't auto-restart — the
    // user gets to see exactly what happened.
    const [tabVisible, setTabVisible] = useState(
        typeof document !== "undefined" ? !document.hidden : true,
    );
    useEffect(() => {
        const onVis = () => setTabVisible(!document.hidden);
        document.addEventListener("visibilitychange", onVis);
        return () => document.removeEventListener("visibilitychange", onVis);
    }, []);

    const onBack = () => {
        onStop();
        // Strip the debug param without a full reload so the app state
        // we're returning to is fresh.
        const url = new URL(window.location.href);
        url.searchParams.delete("debug");
        window.history.replaceState(null, "", url.toString());
        window.location.reload();
    };

    return (
        <div style={styles.shell}>
            <header style={styles.header}>
                <button onClick={onBack} style={styles.backBtn}>← Exit</button>
                <h1 style={styles.title}>Sensor debug</h1>
                <span style={styles.platformChip}>{platform || "unsupported"}</span>
            </header>

            <main style={styles.body}>
                <section style={styles.section}>
                    <h2 style={styles.h2}>Status</h2>
                    <Row label="Platform" value={platform || "unsupported"} />
                    <Row label="Permission" value={permissionState} />
                    <Row label="Tab visible" value={tabVisible ? "yes" : "no"} />
                    <Row label="Sampler" value={running ? "running" : "stopped"} />
                    <Row
                        label="Observed rate"
                        value={running ? `${observedHz.toFixed(1)} Hz` : "—"}
                    />
                    <Row label="Samples seen" value={String(sampleCount)} />
                </section>

                {error && (
                    <section style={styles.errorBox}>
                        <strong>Error:</strong> {error}
                    </section>
                )}

                <section style={styles.section}>
                    <h2 style={styles.h2}>Accel (m/s²)</h2>
                    <Reading label="ax" value={sample?.ax} />
                    <Reading label="ay" value={sample?.ay} />
                    <Reading label="az" value={sample?.az} />
                </section>

                <section style={styles.section}>
                    <h2 style={styles.h2}>Gyro (rad/s)</h2>
                    <Reading label="gx" value={sample?.gx} />
                    <Reading label="gy" value={sample?.gy} />
                    <Reading label="gz" value={sample?.gz} />
                </section>

                <section style={styles.section}>
                    <h2 style={styles.h2}>Complementary filter (phone frame)</h2>
                    <Reading label="heel" value={filtered?.heelDeg} unit="°" />
                    <Reading label="pitch" value={filtered?.pitchDeg} unit="°" />
                </section>

                <section style={styles.controls}>
                    {!running ? (
                        <button
                            onClick={onStart}
                            disabled={!platform}
                            style={styles.primaryBtn}
                        >
                            {needsPermissionPrompt() && permissionState !== "granted"
                                ? "Grant permission & start"
                                : "Start sensors"}
                        </button>
                    ) : (
                        <button onClick={onStop} style={styles.secondaryBtn}>
                            Stop sensors
                        </button>
                    )}
                </section>

                <section style={styles.footnote}>
                    Phone-frame values — no boat-frame remapping or calibration applied.
                    To verify sleep/wake behavior: start the sampler, then lock the
                    screen for 5–10 s and unlock. Observed rate should drop to 0 while
                    locked and resume on unlock.
                </section>
            </main>
        </div>
    );
}

function Row({ label, value }) {
    return (
        <div style={styles.row}>
            <span style={styles.rowLabel}>{label}</span>
            <span style={styles.rowValue}>{value}</span>
        </div>
    );
}

function Reading({ label, value, unit = "" }) {
    const formatted =
        typeof value === "number" ? value.toFixed(3) + unit : "—";
    return (
        <div style={styles.row}>
            <span style={styles.rowLabel}>{label}</span>
            <span style={styles.reading}>{formatted}</span>
        </div>
    );
}

const styles = {
    shell: {
        position: "absolute",
        inset: 0,
        background: "var(--paper)",
        overflow: "auto",
        fontFamily: "inherit",
    },
    header: {
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "16px 20px",
        borderBottom: "1px solid var(--rule)",
        position: "sticky",
        top: 0,
        background: "var(--paper)",
        zIndex: 1,
    },
    backBtn: {
        background: "none",
        border: "1px solid var(--rule)",
        borderRadius: "var(--r-sm)",
        padding: "6px 12px",
        fontSize: 14,
        cursor: "pointer",
        color: "var(--ink)",
        fontFamily: "inherit",
    },
    title: {
        flex: 1,
        margin: 0,
        fontSize: 16,
        fontWeight: 500,
        color: "var(--ink)",
    },
    platformChip: {
        padding: "2px 10px",
        fontSize: 11,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        background: "rgba(22,22,26,0.05)",
        color: "var(--ink)",
        borderRadius: 999,
    },
    body: {
        padding: "20px",
        maxWidth: 600,
        margin: "0 auto",
    },
    section: {
        marginBottom: 24,
        padding: "16px",
        border: "1px solid var(--rule)",
        borderRadius: "var(--r-sm)",
    },
    h2: {
        margin: "0 0 12px 0",
        fontSize: 12,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: "var(--ink-3)",
        fontWeight: 500,
    },
    row: {
        display: "flex",
        justifyContent: "space-between",
        alignItems: "baseline",
        padding: "6px 0",
        borderBottom: "1px solid var(--rule)",
    },
    rowLabel: {
        fontSize: 13,
        color: "var(--ink-3)",
    },
    rowValue: {
        fontSize: 14,
        color: "var(--ink)",
    },
    reading: {
        fontSize: 14,
        color: "var(--ink)",
        fontFamily: "var(--mono, monospace)",
        fontVariantNumeric: "tabular-nums",
    },
    errorBox: {
        padding: "12px 16px",
        marginBottom: 24,
        background: "rgba(220, 50, 50, 0.08)",
        border: "1px solid rgba(220, 50, 50, 0.3)",
        borderRadius: "var(--r-sm)",
        color: "#a02020",
        fontSize: 14,
    },
    controls: {
        display: "flex",
        justifyContent: "center",
        marginBottom: 16,
    },
    primaryBtn: {
        background: "var(--ink)",
        color: "var(--paper)",
        border: "none",
        borderRadius: "var(--r-sm)",
        padding: "12px 20px",
        fontSize: 14,
        cursor: "pointer",
        fontFamily: "inherit",
    },
    secondaryBtn: {
        background: "var(--paper)",
        color: "var(--ink)",
        border: "1px solid var(--rule)",
        borderRadius: "var(--r-sm)",
        padding: "12px 20px",
        fontSize: 14,
        cursor: "pointer",
        fontFamily: "inherit",
    },
    footnote: {
        fontSize: 12,
        color: "var(--ink-4)",
        lineHeight: 1.6,
        padding: "0 4px",
    },
};
