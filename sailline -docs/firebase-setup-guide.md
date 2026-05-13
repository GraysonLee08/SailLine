# Firebase Project Setup

Sets up Firebase Hosting (for the React app) and Firebase Authentication (user signup/login + JWT issuance) on top of the existing GCP project.

> Firebase is layered onto an existing GCP project. There is no separate "Firebase project" — it's the same project with Firebase services activated.

---

## Prerequisites

- The `sailline` GCP project exists with billing linked (see `01-gcp-bootstrap.md`)
- You're a Project Owner on the GCP project
- The `gcloud` CLI is authenticated as the same user you'll use for Firebase

---

## Step 1 — Add Firebase to the GCP project

Firebase setup is done through the Firebase Console (CLI doesn't fully support it for first-time setup).

1. Go to [console.firebase.google.com](https://console.firebase.google.com)
2. Click **Add project**
3. Click the **dropdown** in the project name field — you'll see your existing GCP projects listed
4. Select **`sailline`**
5. Click **Continue**
6. **Google Analytics:** disable for now. You can add it later if needed; it adds complexity to the setup and isn't useful pre-launch.
7. Click **Add Firebase**

This activates Firebase services on the `sailline` GCP project. It takes 30–60 seconds.

---

## Step 2 — Enable Authentication

1. In the Firebase Console, select the `sailline` project
2. Left sidebar → **Build** → **Authentication**
3. Click **Get started**
4. Under the **Sign-in method** tab, enable:
   - **Email/Password** — click it, toggle Enable, click Save
   - **Google** — click it, toggle Enable, set your support email, click Save

> Email link (passwordless), GitHub, Apple, etc. can be added later. Email + Google covers the vast majority of users for v1.

### Authorized domains

Still under Authentication → Settings tab:

1. Add your custom domain when you have one (e.g., `sailline.app`)
2. `localhost` is added by default for local dev
3. The `*.web.app` and `*.firebaseapp.com` domains are added automatically

---

## Step 3 — Enable Hosting

1. Left sidebar → **Build** → **Hosting**
2. Click **Get started**
3. The setup wizard shows you commands to run locally — you can skip through it; we'll do the local setup in Step 5.

---

## Step 4 — Register the web app

Firebase needs to know about your specific app to issue config keys.

1. Project Overview (home icon, top-left) → click the **`</>`** (web) icon to add a web app
2. App nickname: `SailLine Web`
3. Check the box for **Also set up Firebase Hosting**, select the default site
4. Click **Register app**
5. **Copy the `firebaseConfig` object** that appears — you'll need this for `frontend/.env.local`. It looks like:
   ```javascript
   const firebaseConfig = {
     apiKey: "AIza...",
     authDomain: "sailline.firebaseapp.com",
     projectId: "sailline",
     storageBucket: "sailline.appspot.com",
     messagingSenderId: "123456789",
     appId: "1:123:web:abc..."
   };
   ```
6. Click **Continue to console** (skip the SDK install — we'll handle it via npm)

> The `apiKey` here is **not** secret — it identifies your project to Firebase services but doesn't grant access. It's safe to commit. The actual security is enforced by Firebase Auth rules and your backend's JWT verification.

---

## Step 5 — Local Firebase CLI setup

Install the Firebase CLI globally:

```bash
npm install -g firebase-tools
```

Log in:

```bash
firebase login
```

A browser window opens for OAuth. Use the same Google account that owns the GCP project.

Verify you can see the project:

```bash
firebase projects:list
# Should show: sailline
```

---

## Step 6 — Initialize Firebase in the frontend directory

```bash
cd frontend
firebase init
```

Interactive prompts:

- **Which Firebase features?** → Select **Hosting** (spacebar to select, enter to confirm)
- **Project setup** → Select **Use an existing project** → choose `sailline`
- **Public directory?** → `dist` (this is where Vite outputs the production build)
- **Configure as a single-page app (rewrite all URLs to /index.html)?** → **Yes**
- **Set up automatic builds and deploys with GitHub?** → **No** (we'll use Cloud Build instead)
- **File `dist/index.html` already exists. Overwrite?** → **No** (Vite will generate it)

This creates two files in `frontend/`:

- `firebase.json` — hosting config
- `.firebaserc` — links the local directory to the `sailline` Firebase project

Both are safe to commit.

---

## Step 7 — Configure environment variables

In `frontend/.env.example`:

```bash
# Firebase web config (from Step 4)
VITE_FIREBASE_API_KEY=
VITE_FIREBASE_AUTH_DOMAIN=
VITE_FIREBASE_PROJECT_ID=
VITE_FIREBASE_STORAGE_BUCKET=
VITE_FIREBASE_MESSAGING_SENDER_ID=
VITE_FIREBASE_APP_ID=

# Backend API
VITE_API_URL=http://localhost:8080

# MapboxGL token (from Step 9 below)
VITE_MAPBOX_TOKEN=

# Stripe (added later in Week 7)
VITE_STRIPE_PUBLIC_KEY=
```

Copy to `.env.local` (gitignored) and fill in the actual values from Step 4:

```bash
cd frontend
cp .env.example .env.local
# Edit .env.local with your real values
```

---

## Step 8 — Wire up Firebase in the React app

In `frontend/src/lib/firebase.js`:

```javascript
import { initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
};

export const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
```

Install the Firebase SDK:

```bash
cd frontend
npm install firebase
```

---

## Step 9 — Get a Mapbox access token (separate from Firebase, but Week 1)

1. Sign up at [mapbox.com](https://www.mapbox.com) (free tier is generous: 50K map loads/month)
2. Account → Tokens → **Create a token**
3. Name it `sailline-dev`
4. Default scopes (public scopes only) are fine
5. Copy the token starting with `pk.eyJ...`
6. Paste into `VITE_MAPBOX_TOKEN` in `.env.local`

> Create a separate `sailline-prod` token later for production with URL restrictions to your domain.

---

## Step 10 — Test the deploy

Even before there's a real React app, test the deploy pipeline:

```bash
cd frontend
mkdir -p dist
echo "<h1>SailLine</h1>" > dist/index.html
firebase deploy --only hosting
```

Output ends with a hosting URL like:

```
Hosting URL: https://sailline.web.app
```

Visit it in a browser. You should see the placeholder page.

If this works, your Firebase Hosting pipeline is wired up. From here on, `npm run build && firebase deploy --only hosting` ships the frontend.

---

## Step 11 — Configure backend to verify Firebase tokens

The FastAPI backend will verify Firebase-issued JWTs server-side. This requires a service account credential.

In **GCP Console** (not Firebase):

1. Go to IAM & Admin → Service Accounts
2. Click **Create service account**
3. Name: `firebase-admin`
4. Grant role: **Firebase Authentication Admin**
5. Done

Cloud Run will use this service account automatically when you deploy with `--service-account=firebase-admin@sailline.iam.gserviceaccount.com`. **No JSON key file needs to be downloaded** for production.

For local development, you can:

- Use `gcloud auth application-default login` (uses your user credentials), or
- Generate a JSON key for the `firebase-admin` service account and set `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json` (but never commit the key file)

---

## What you've built after this

- Firebase Auth issues JWTs to the React frontend on login
- React stores the JWT and includes it in the `Authorization: Bearer ...` header on API calls to the FastAPI backend
- FastAPI verifies the JWT using the Firebase Admin SDK before processing requests
- Firebase Hosting serves the React app at `https://sailline.web.app` (until you add a custom domain)

---

## Custom domain (optional, can defer until launch)

To use `sailline.app` (or whatever you register):

1. Firebase Console → Hosting → **Add custom domain**
2. Enter your domain
3. Firebase walks you through DNS verification (TXT record at your registrar)
4. Once verified, Firebase issues an SSL cert automatically (24–48 hours typical)
5. Add the domain to **Authentication → Settings → Authorized domains**

---

## Troubleshooting

**`firebase init` fails with "no projects available"**
Run `firebase login` again. The CLI sometimes has stale credentials.

**`firebase deploy` returns 403**
Your Google account doesn't have hosting permissions. In Firebase Console → Project Settings → Users and permissions, confirm your account is listed as an Owner.

**Auth login succeeds but token verification fails on backend**
Most common cause: the backend's service account doesn't have `Firebase Authentication Admin` role. Re-check Step 11.

**`apiKey` showing in client code feels wrong**
It's normal. Firebase web API keys identify the project but don't grant elevated access. Real security is enforced by Auth rules and backend JWT verification. Don't worry about committing `.env.local` only because it has other things in it (Mapbox tokens, Stripe keys) that DO need to stay private.

---

## What's next

After Firebase is set up, the next infrastructure steps are:

1. **VPC + Serverless VPC Connector** (`02-vpc-and-cloud-sql.md`) — networking for private DB access
2. **Cloud SQL provisioning** (same doc) — Postgres + PostGIS extension
3. **Memorystore Redis** (`03-memorystore-and-storage.md`)
4. **Artifact Registry repo** (`04-secrets-and-iam.md`)
5. **Cloud Build pipeline** (`05-cloudbuild-cicd.md`)

Then you're ready to start writing application code.
