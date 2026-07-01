// Firebase init — modular SDK (v9+).
// Config keys are project identifiers, not secrets — safe to commit.
// Real auth security comes from Firebase Auth rules and the backend
// verifying ID tokens with the Admin SDK.
//
// Env wiring (2026-06-30): values are read from import.meta.env so the
// Secret Manager pipeline in cloudbuild.frontend.yaml controls what
// ships. Falls back to the hardcoded values for local dev (when .env
// is absent) so `npm run dev` works out of the box.

import { initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY || "AIzaSyDhK3xgF9-vWp5rQ2sZ6tN8mHbVcLnY1o",
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || "sailline.firebaseapp.com",
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID || "sailline",
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET || "sailline.firebasestorage.app",
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID || "105706282249",
  appId: import.meta.env.VITE_FIREBASE_APP_ID || "1:105706282249:web:a807ee7f63f041705e87d8",
  measurementId: "G-RC0G8DL7BX",
};

export const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
