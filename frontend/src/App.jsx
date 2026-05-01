// App — listens to Firebase auth state and routes between
// AuthView (logged out) and AppView (logged in).
//
// AppView is lazy-loaded so the auth-gate path doesn't pull mapbox-gl
// or any of the post-login bundle. The Suspense fallback intentionally
// matches the pre-auth-resolution splash below — visually a single
// continuous loading state from page open through authenticated render.

import { lazy, Suspense, useEffect, useState } from "react";
import { onAuthStateChanged } from "firebase/auth";
import { auth } from "./firebase";
import AuthView from "./AuthView.jsx";

const AppView = lazy(() => import("./AppView.jsx"));

// Single splash element reused for both pre-auth-resolution and the
// AppView chunk download. Identical-by-design so the two states feel
// like one continuous "still loading" rather than two flashes.
const SPLASH = <div style={{ height: "100vh", background: "var(--night)" }} />;

export default function App() {
  const [user, setUser] = useState(null);
  const [authReady, setAuthReady] = useState(false);

  useEffect(() => {
    // onAuthStateChanged fires once on mount with the cached user
    // (or null), then again on every sign-in / sign-out.
    return onAuthStateChanged(auth, (u) => {
      setUser(u);
      setAuthReady(true);
    });
  }, []);

  if (!authReady) {
    // Pre-auth-resolution flash. Keep it dark so we don't flash
    // the light form while Firebase resolves the cached session.
    return SPLASH;
  }

  if (!user) {
    return <AuthView />;
  }

  return (
    <Suspense fallback={SPLASH}>
      <AppView user={user} />
    </Suspense>
  );
}
