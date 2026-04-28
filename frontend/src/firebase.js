// Firebase init — modular SDK (v9+).
// Config keys are project identifiers, not secrets — safe to commit.
// Real auth security comes from Firebase Auth rules and the backend
// verifying ID tokens with the Admin SDK.

import { initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";

const firebaseConfig = {
  apiKey: "AIzaSyDQCeMfB1EOMwRNCExXO52g11J2ynYdtRM",
  authDomain: "sailline.firebaseapp.com",
  projectId: "sailline",
  storageBucket: "sailline.firebasestorage.app",
  messagingSenderId: "105706282249",
  appId: "1:105706282249:web:a807ee7f63f041705e87d8",
  measurementId: "G-RC0G8DL7BX",
};

export const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
