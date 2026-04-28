// App — listens to Firebase auth state and routes between
// AuthView (logged out) and AppView (logged in).
//
// We intentionally keep this thin. Future routing (pre-race, in-race,
// settings) will live inside AppView once those screens exist.

import { useEffect, useState } from "react";
import { onAuthStateChanged } from "firebase/auth";
import { auth } from "./firebase";
import AuthView from "./AuthView.jsx";
import AppView from "./AppView.jsx";

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
    return <div style={{ height: "100vh", background: "var(--night)" }} />;
  }

  return user ? <AppView user={user} /> : <AuthView />;
}
