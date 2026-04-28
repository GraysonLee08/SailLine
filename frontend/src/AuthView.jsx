// AuthView — split-screen login.
//
// Design notes:
// - LEFT (60%): cinematic dark panel. Massive Inter display headline,
//   subtle animated wind-streamline SVG, wordmark + system status row
//   at the bottom (the "intelligent" data signal — like an Apple
//   keynote shows live data, never plain marketing copy).
// - RIGHT (40%): paper-light form. Single column. Email + password.
//   Sign in is the default mode (most users are returning); a tiny
//   text link toggles to sign-up. Google sign-in is a secondary
//   action below a hairline divider.
//
// What was iterated from the wireframe (OnbA in design_reference):
//   - Dropped the wireframe's progress dots — login isn't onboarding.
//     Onboarding (boat, home waters, plan) comes after first sign-up
//     and gets its own multi-step flow later.
//   - Replaced paper-2 left panel with deep night background. More
//     cinematic, more "this software is for serious racers."
//   - Replaced static SVG with an animated wind-streamline motif —
//     conveys the product (wind routing) without a screenshot.
//   - Added a live system-status row at the bottom of the hero —
//     subtle data accent that signals "real product, real numbers."

import { useState } from "react";
import {
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  signInWithPopup,
  GoogleAuthProvider,
} from "firebase/auth";
import { auth } from "./firebase";
import HeroPanel from "./HeroPanel.jsx";

export default function AuthView() {
  const [mode, setMode] = useState("signin"); // "signin" | "signup"
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const isSignup = mode === "signup";

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (isSignup) {
        await createUserWithEmailAndPassword(auth, email.trim(), password);
      } else {
        await signInWithEmailAndPassword(auth, email.trim(), password);
      }
    } catch (err) {
      setError(prettyAuthError(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleGoogle() {
    setError("");
    setBusy(true);
    try {
      await signInWithPopup(auth, new GoogleAuthProvider());
    } catch (err) {
      // signInWithPopup throws if the user closes the popup —
      // that's not really an error worth showing.
      if (err.code !== "auth/popup-closed-by-user") {
        setError(prettyAuthError(err));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={styles.shell}>
      <HeroPanel />

      <section style={styles.formPanel}>
        <div style={styles.formInner}>
          {/* Mobile-only wordmark (hidden on desktop where the hero shows it) */}
          <div className="wordmark" style={styles.mobileWordmark}>
            SailLine<span className="dot">.</span>
          </div>

          <header style={styles.formHeader}>
            <p className="t-label" style={{ marginBottom: 12 }}>
              {isSignup ? "Create account" : "Sign in"}
            </p>
            <h1 className="t-display" style={styles.formTitle}>
              {isSignup ? "Set sail." : "Welcome back."}
            </h1>
            <p style={styles.formSubtitle}>
              {isSignup
                ? "Free is plenty for pre-race. Upgrade when you cross the start line."
                : "Pick up where you left off."}
            </p>
          </header>

          <form onSubmit={handleSubmit} style={styles.form} noValidate>
            <Field
              label="Email"
              type="email"
              value={email}
              onChange={setEmail}
              autoComplete="email"
              autoFocus
              disabled={busy}
            />
            <Field
              label="Password"
              type="password"
              value={password}
              onChange={setPassword}
              autoComplete={isSignup ? "new-password" : "current-password"}
              disabled={busy}
              hint={isSignup ? "At least 6 characters." : null}
            />

            {error && (
              <div role="alert" style={styles.error}>
                {error}
              </div>
            )}

            <button type="submit" disabled={busy || !email || !password} style={styles.primaryBtn}>
              {busy ? "…" : isSignup ? "Create account" : "Sign in"}
              <span style={styles.primaryBtnArrow}>→</span>
            </button>
          </form>

          <div style={styles.divider}>
            <span style={styles.dividerRule} />
            <span className="t-label" style={styles.dividerLabel}>or</span>
            <span style={styles.dividerRule} />
          </div>

          <button onClick={handleGoogle} disabled={busy} style={styles.secondaryBtn}>
            <GoogleMark />
            <span>Continue with Google</span>
          </button>

          <p style={styles.toggle}>
            {isSignup ? "Already have an account?" : "New to SailLine?"}{" "}
            <button
              type="button"
              onClick={() => {
                setMode(isSignup ? "signin" : "signup");
                setError("");
              }}
              style={styles.toggleBtn}
            >
              {isSignup ? "Sign in" : "Create one"}
            </button>
          </p>
        </div>

        <footer style={styles.footer}>
          <span className="t-label">v1.0 · Beta · Great Lakes</span>
        </footer>
      </section>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Field — labeled input with floating focus ring + optional hint     */
/* ─────────────────────────────────────────────────────────────────── */

function Field({ label, type, value, onChange, hint, ...rest }) {
  const [focused, setFocused] = useState(false);

  return (
    <label style={styles.field}>
      <span className="t-label" style={styles.fieldLabel}>
        {label}
      </span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        style={{
          ...styles.input,
          borderColor: focused ? "var(--ink)" : "var(--rule)",
          boxShadow: focused ? "0 0 0 4px rgba(22, 22, 26, 0.06)" : "none",
        }}
        {...rest}
      />
      {hint && <span style={styles.fieldHint}>{hint}</span>}
    </label>
  );
}

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.49h4.84a4.14 4.14 0 0 1-1.79 2.71v2.26h2.9c1.7-1.56 2.69-3.87 2.69-6.62z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.81 5.96-2.18l-2.9-2.26c-.8.54-1.83.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.96v2.33A8.997 8.997 0 0 0 9 18z"
      />
      <path
        fill="#FBBC05"
        d="M3.95 10.7A5.41 5.41 0 0 1 3.66 9c0-.59.1-1.16.29-1.7V4.96H.96A8.997 8.997 0 0 0 0 9c0 1.45.35 2.82.96 4.04l2.99-2.34z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.51.45 3.44 1.35l2.58-2.58A8.97 8.97 0 0 0 9 0 8.997 8.997 0 0 0 .96 4.96L3.95 7.3C4.66 5.17 6.65 3.58 9 3.58z"
      />
    </svg>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Helpers                                                             */
/* ─────────────────────────────────────────────────────────────────── */

function prettyAuthError(err) {
  // Firebase error codes are noisy. Translate the common ones.
  const code = err?.code || "";
  switch (code) {
    case "auth/invalid-credential":
    case "auth/wrong-password":
    case "auth/user-not-found":
      return "That email and password don't match.";
    case "auth/invalid-email":
      return "That email address looks off.";
    case "auth/email-already-in-use":
      return "An account with that email already exists.";
    case "auth/weak-password":
      return "Password needs to be at least 6 characters.";
    case "auth/too-many-requests":
      return "Too many attempts. Try again in a minute.";
    case "auth/network-request-failed":
      return "Network hiccup. Check your connection and retry.";
    default:
      return err?.message || "Something went wrong.";
  }
}

/* ─────────────────────────────────────────────────────────────────── */
/* Styles — colocated for a single-file component                      */
/* ─────────────────────────────────────────────────────────────────── */

const styles = {
  shell: {
    display: "grid",
    gridTemplateColumns: "minmax(0, 1.3fr) minmax(0, 1fr)",
    minHeight: "100vh",
  },
  formPanel: {
    background: "var(--paper)",
    display: "flex",
    flexDirection: "column",
    justifyContent: "space-between",
    padding: "48px 56px",
    minHeight: "100vh",
  },
  formInner: {
    width: "100%",
    maxWidth: 420,
    margin: "0 auto",
    flex: 1,
    display: "flex",
    flexDirection: "column",
    justifyContent: "center",
  },
  mobileWordmark: {
    display: "none", // toggled on at narrow widths via @media in App-level CSS
    marginBottom: 32,
  },
  formHeader: {
    marginBottom: 36,
  },
  formTitle: {
    fontSize: 44,
    margin: "0 0 12px",
  },
  formSubtitle: {
    margin: 0,
    color: "var(--ink-3)",
    fontSize: 15,
    lineHeight: 1.5,
    maxWidth: 360,
  },
  form: {
    display: "flex",
    flexDirection: "column",
    gap: 20,
  },
  field: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
  },
  fieldLabel: {
    color: "var(--ink-3)",
  },
  fieldHint: {
    fontSize: 12,
    color: "var(--ink-4)",
    marginTop: 2,
  },
  input: {
    height: 48,
    padding: "0 16px",
    border: "1.5px solid var(--rule)",
    borderRadius: "var(--r-md)",
    fontSize: 15,
    color: "var(--ink)",
    background: "var(--paper)",
    outline: "none",
    transition: "border-color 0.15s, box-shadow 0.15s",
    fontFamily: "var(--display)",
  },
  primaryBtn: {
    height: 52,
    marginTop: 8,
    border: "none",
    borderRadius: "var(--r-md)",
    background: "var(--ink)",
    color: "var(--paper)",
    fontSize: 15,
    fontWeight: 500,
    letterSpacing: "-0.005em",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    transition: "transform 0.1s, opacity 0.15s",
  },
  primaryBtnArrow: {
    fontFamily: "var(--mono)",
    opacity: 0.6,
  },
  error: {
    padding: "10px 14px",
    borderRadius: "var(--r-sm)",
    background: "rgba(214, 59, 31, 0.08)",
    color: "var(--error)",
    fontSize: 13,
    lineHeight: 1.4,
    border: "1px solid rgba(214, 59, 31, 0.2)",
  },
  divider: {
    display: "flex",
    alignItems: "center",
    gap: 16,
    margin: "32px 0 20px",
  },
  dividerLabel: {
    color: "var(--ink-4)",
  },
  dividerRule: {
    flex: 1,
    height: 1,
    background: "var(--rule)",
  },
  secondaryBtn: {
    height: 48,
    width: "100%",
    border: "1.5px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-md)",
    fontSize: 14,
    fontWeight: 500,
    color: "var(--ink)",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    transition: "background 0.15s, border-color 0.15s",
  },
  toggle: {
    marginTop: 32,
    textAlign: "center",
    color: "var(--ink-3)",
    fontSize: 14,
  },
  toggleBtn: {
    background: "none",
    border: "none",
    padding: 0,
    color: "var(--ink)",
    fontWeight: 500,
    textDecoration: "underline",
    textDecorationColor: "var(--rule)",
    textUnderlineOffset: "3px",
  },
  footer: {
    display: "flex",
    justifyContent: "center",
    paddingTop: 24,
  },
};
