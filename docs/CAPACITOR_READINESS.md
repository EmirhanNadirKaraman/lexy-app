# Capacitor / iOS Readiness Checklist

Pre-wrap audit before starting **#34 / T3.1**. Read this end-to-end before
running any of the commands in §5. Don't run commands during this audit —
this doc is decision-only.

Companion to `docs/ROADMAP.md` (priority view) and `docs/TODO.md` #34
(path-A through path-D analysis).

Last updated: 2026-05-20. Reviewed 2026-07-27 — the audit below still holds,
with these deltas from the 2026-05-24 security pass that a wrap must account
for (each already tracked in `docs/SECURITY.md`; noted here because they touch
the mobile client or the hosted deployment):

- **S3** — account deletion now needs password re-auth and sends a body on
  DELETE. See §2.7.
- **S1** — auth endpoints are throttled per IP and per (IP, email). A native
  client behind carrier-grade NAT shares an IP with other users; if login 429s
  in the field, that is the first thing to check.
- **S4** — `DB_SSL_MODE` gates DB TLS and applies to the app, Alembic, and the
  scraper. Set it for the hosted deploy (§ deploy gate in `docs/ROADMAP.md`).
- **S5** — security headers ship by default; HSTS is opt-in via `ENABLE_HSTS`.
- **S11** — API docs are off unless `ENABLE_DOCS` is set, so don't expect
  `/docs` to answer on the hosted backend while testing the wrap.
- **S2** — signup can be gated behind `REGISTRATION_CODE`. If that is set in
  production, the native onboarding flow needs an invite-code field.

---

## 1. Current readiness status

### Already ready

- [x] **Mobile responsive web** — #27a–g landed. 44px touch targets, fluid
      typography, `clamp()` font sizes, iOS focus-zoom guards (16px on every
      input/select/textarea).
- [x] **PWA shell** — `frontend/public/sw.js` (v1), `manifest.webmanifest`,
      `offline.html`. Service worker is production-only; dev unaffected.
- [x] **PNG icon set** — `icons/icon-192.png`, `icons/icon-512.png`,
      `apple-touch-icon.png` (180×180 with ~10% safe-zone padding).
- [x] **Viewport-fit=cover** — `index.html` line 8 already includes it, so
      a Capacitor wrap can paint behind the iPhone notch.
- [x] **Apple meta tags** — `apple-mobile-web-app-capable`,
      `apple-mobile-web-app-title="Lexy"`, status-bar style.
- [x] **Dark-mode tristate** — T1.3 (2026-05-20). `theme_mode = system | light
      | dark`; `useResolvedTheme` follows `prefers-color-scheme: dark` live.
- [x] **Theme-color** — `#1a237e`, present in both `index.html` and
      `manifest.webmanifest`. iOS uses it for the status bar in standalone.
- [x] **CORS env-driven** — `CORS_ORIGINS` comma-separated, parsed in
      `backend/main.py:_parse_cors_origins`. Documented in `.env.example`.
- [x] **JWT expiry handling** — `core/deps.py` differentiates
      `token_expired` vs generic invalid; frontend `_http.ts`
      `signalAuthExpired` dispatches `auth:expired` with reason; Layout
      catches it and bounces to home.
- [x] **Rate limiting** — in-process limiter (#12). Per-user 30/min, 400/hr.
- [x] **LLM cache + thundering-herd guard** — won't change shape on mobile.
- [x] **Backend deploy gate sweep prep** — `.env.example` documents
      `DB_*`, `SECRET_KEY`, `ANTHROPIC_API_KEY`, `MOCK_LLM`, `CORS_ORIGINS`.
- [x] **Procfile present** — `web: cd lexy-app && uvicorn backend.main:app
      --host 0.0.0.0 --port $PORT`. Heroku/Render-compatible.

### Now ready (since the original audit)

- [x] **`capacitor.config.ts` written** — placeholder
      `appId='com.lexy.learning'` (ROADMAP T3.1).
- [x] **`@capacitor/*` dependencies installed** —
      `@capacitor/core@8.3.4`, `@capacitor/cli@8.3.4`,
      `@capacitor/ios@8.3.4` in `frontend/package.json`.
- [x] **`ios/` directory scaffolded** — `npx cap add ios` complete; web
      assets copied to `ios/App/App/public/`; Podfile platform bumped to
      iOS 15 (Capacitor-8 requirement).
- [x] **API base URL is configurable** — `frontend/src/api/_baseUrl.ts`
      exports `apiUrl(path)` + `API_BASE_URL`. All 14 api files and the
      few inline fetches (`BookReaderPage`, `useNotifications`,
      `useReadingStats`, `useWordColors`, `getPageImageUrl`) route through
      it. Empty `VITE_API_BASE_URL` preserves the path-relative web
      behaviour (§8.1, done 2026-05-20).
- [x] **Account-deletion endpoint + privacy page** — `DELETE
      /api/v1/account` (W11) cascades private FKs and SET-NULLs
      audit-signal tables. `/privacy` page accessible logged-out;
      footer link in the global Layout.
- [x] **`useNotifications` lifecycle hardened** — per-connection
      `AbortController` + reconnect timer (W13 audit fix C). Foregrounding
      after a backgrounded reload reconnects on the next effect fire.

### Still missing for Capacitor

- [ ] **Vite dev proxy is the only thing that makes `/api` work** today —
      `vite.config.ts:11-13` proxies to `localhost:8000`. Production builds
      have no proxy, so a hosted frontend would already need a same-origin
      backend or a CDN/edge rewrite. Capacitor compounds this — the
      hosted backend at the `VITE_API_BASE_URL` value must be reachable.
- [ ] **Token storage is `localStorage`** — `auth.ts` calls
      `localStorage.getItem/setItem('auth_token')`. Works in Capacitor's
      WKWebView, but not secure (any local script can read it) and not
      ideal for App Store review. Should move to `@capacitor/preferences`
      (or Keychain via `@capacitor-community/secure-storage`).
- [ ] **SSE polling at 3s** — `useNotifications.ts` opens a long-lived
      `fetch('/api/v1/notifications/stream', { signal })`. iOS background
      kills the connection. **#4b LISTEN/NOTIFY refactor is the upstream
      fix** (not blocking Capacitor but degrades UX).
- [ ] **Real privacy / support email** — `PrivacyPage.tsx` ships a
      `<YOUR_REAL_PRIVACY_EMAIL_BEFORE_LAUNCH>` placeholder; cannot ship
      to App Store with this.
- [ ] **App Store Connect declarations** — Privacy Nutrient Label / Apple
      App Privacy form, age rating, support URL, screenshots.
- [ ] **Production env values** — `VITE_API_BASE_URL` for the hosted
      backend; backend `CORS_ORIGINS` including
      `capacitor://localhost,https://localhost` plus the web origin.
- [ ] **Full Xcode + signing** — `xcode-select` currently points at
      CommandLineTools; need full Xcode to finish `cap sync` and set the
      development team + bundle ID before TestFlight.
- [ ] **No splash screen assets** — only icons present. iOS uses the
      LaunchScreen storyboard by default; Capacitor generates one but
      branded splash needs design.
- [ ] **No icon-only "maskable" PWA icon** — current icons are
      `purpose: "any"` only. Capacitor doesn't strictly require maskable,
      but adaptive Android (if path expands) would.

---

## 2. Must do before Capacitor wrap

### 2.1 API base URL strategy
**Decision required.** Pick one:

- **(A) Hosted backend at a real domain** — e.g.
  `https://api.lexy.app`. Most flexible. Requires DNS, TLS cert, and
  `CORS_ORIGINS=capacitor://localhost,https://localhost` (the two iOS
  WebView origins) in addition to your web origin.
- **(B) Embedded backend** — ship the FastAPI server inside the iOS bundle.
  Not viable (requires Python on iOS; large bundle; battery cost).
- **(C) Reverse-proxy from inside the WebView** — Capacitor `server.url`
  pointing at a hosted backend or local tunnel. Convenient for dev, but
  bypasses your built JS bundle; not the production shape.

**Recommended:** A. Add a `VITE_API_BASE_URL` env var (empty in web dev → falls back
to relative `/api`, populated for the Capacitor build → absolute URL).

Files that need to read it:
- All `frontend/src/api/*.ts` (~14 files, ~50 fetch sites).
- `useNotifications.ts:19` (the SSE call).
- Concrete shape: `const API = import.meta.env.VITE_API_BASE_URL ?? '';` then
  `fetch(\`${API}/api/v1/...\`)`. Backwards-compatible — empty string keeps
  the path-relative behaviour for web.

### 2.2 CORS_ORIGINS for native builds
- [ ] Add `capacitor://localhost` and `https://localhost` to the hosted
      backend's `CORS_ORIGINS`. (iOS WKWebView uses `capacitor://localhost`
      by default; some plugins use the https variant.)
- [ ] Verify backend accepts the `Authorization: Bearer ...` header from
      those origins — `allow_headers=["*"]` already does.
- [ ] Test preflight for non-GET routes (login, settings PUT, etc.).

### 2.3 Environment variables
- [ ] Add `VITE_API_BASE_URL` to `.env.example` with a comment.
- [ ] Decide on `.env.production` vs `.env.capacitor` for the wrap build.
      Vite reads `.env.production` for `vite build`; Capacitor uses the
      result of that build. Simplest: one `.env.production` consumed by
      both web and native; Capacitor `server.url` (if used) overrides.

### 2.4 Auth / token storage decision
**Current:** `localStorage.getItem('auth_token')` via `frontend/src/auth.ts`.

**Inside Capacitor:** WKWebView's localStorage IS persistent and
sandboxed per app, but it's not encrypted at rest and clears on app
reinstall. Two upgrade paths:

- **Minimal (works day 1):** keep `localStorage`. Documented limitation.
- **Recommended for App Store:** swap to `@capacitor/preferences` (JSON
  store, async API, persists across reinstalls if iCloud restore is on)
  OR `@capacitor-community/secure-storage` (Keychain-backed).

If we go async-storage, `auth.ts` needs an async API and every caller
(`getToken()`) becomes `await getToken()`. Cascades through ~12 files.
Consider doing this AFTER the first wrap is green so the change is
isolated.

### 2.5 Service worker behaviour inside Capacitor
**WKWebView quirks:**
- Service workers ARE supported on iOS WKWebView 14+. The `sw.js` we
  shipped registers and intercepts.
- BUT Capacitor's `server.androidScheme`/iOS scheme is `capacitor://` by
  default, NOT `https://`. Some SW features (push, periodic sync) require
  a secure context — `capacitor://` is considered secure by WebKit, so
  basic SW works.
- **Risk:** stale assets in the SW shell cache survive an app update if
  the bundle ID doesn't change `CACHE_VERSION`. We already bump
  `CACHE_VERSION` per shape change (sw.js:21).

**Decision:** keep the SW enabled in the Capacitor wrap. It does no harm,
provides offline navigation, and matches PWA behaviour. If issues arise,
gate registration in `main.tsx:19` with a Capacitor detection:
```ts
if (import.meta.env.PROD
    && typeof navigator !== 'undefined'
    && 'serviceWorker' in navigator
    && !window.Capacitor) {  // ← would gate it out
  ...
}
```

### 2.6 Icons / splash assets
- [x] **App icon source:** `frontend/public/favicon.svg` exists, sized at 48×46.
- [x] **Generated PNGs:** `icons/icon-192.png`, `icons/icon-512.png`,
      `apple-touch-icon.png` (180×180).
- [ ] **Need a 1024×1024 master PNG** for App Store listing (Apple requires
      this exact size for App Store Connect). Can derive from the SVG with
      `rsvg-convert -w 1024 favicon.svg -o icon-1024.png` then `sips -p 1024
      1024 --padColor FFFFFF` for safe-zone padding.
- [ ] **iOS LaunchScreen** — Capacitor scaffolds a default. Branded splash
      requires an Xcode tweak (Assets.xcassets / LaunchScreen.storyboard).
      Acceptable to ship v1 with the default white background + logo.

### 2.7 Privacy / account deletion / App Store requirements
- [x] **Privacy policy URL** — `/privacy` (`PrivacyPage.tsx`) shipped W11.
      Linked from the global Layout footer. Placeholder contact email
      still needs replacing before App Store submission.
- [x] **Account deletion flow** — `DELETE /api/v1/account` shipped W11
      (router: `routers/account.py`). Single statement `DELETE FROM users
      WHERE user_id = $1::uuid`; FK declarations carry the cascade. See
      `docs/PRIVACY.md` for the per-table cascade / SET-NULL audit and
      `tests/test_account_deletion.py` for the regression guards.
      Frontend: destructive Account section in `SettingsPanel`, two-step
      confirm, `signalAuthExpired` on success.
      **Changed since this doc was written (S3, 2026-05-24): the endpoint
      now requires the current password in the DELETE body** in addition
      to the bearer token; missing/wrong → 403. Two consequences for the
      wrap: (a) the confirm step collects a password, so any native
      re-implementation of that screen must too; (b) it **sends a body on
      DELETE**, which a strict CDN/proxy in front of the API may strip —
      verify against the hosted deployment before TestFlight.
- [ ] **Age rating** — language-learning content with YouTube embeds.
      German **and Spanish** as of 2026-05-21 (Spanish seeded in
      `language_table` via migration 030), so the rating question spans
      both corpora, not just the German channel set. Likely 12+ for
      "Infrequent/Mild Profanity or Crude Humor" (depends on which
      channels users follow). Disclose in App Store Connect.
- [ ] **Support URL** — public-facing help page. Can be a simple GitHub
      pages site for v1.
- [ ] **Data collection disclosure** — App Privacy section in App Store
      Connect. We collect: email, password (hashed), learning state,
      content requests. Not linked to identity beyond email. No third-party
      analytics SDKs. Document accordingly.

---

## 3. Can do after first native build

Deferring these to "shipped + opening on a phone" reduces wrap risk.

- [ ] **Push notifications** — replaces the SSE-only flow.
      `@capacitor/push-notifications` + APNs cert + a `device_token` table.
      Pairs with **#4b LISTEN/NOTIFY** so the backend can fan out
      efficiently.
- [ ] **Secure token storage** — swap `localStorage` →
      `@capacitor/preferences` or Keychain.
- [ ] **Deep links** — `lexy://video/abc123`, `lexy://book/42`.
      Useful for sharing, not load-bearing.
- [ ] **Native file picker** — book PDF upload currently uses
      `<input type="file">`. Works in WKWebView but limited. Replace with
      `@capacitor/filesystem` + `@capacitor/file-picker` for a nicer UX.
- [ ] **Offline SRS review** — cache `GET /srs/due` payload locally; queue
      review submissions; sync on reconnect. Big feature, scope separately.
- [ ] **Background fetch / app refresh** — quietly pre-fetch tomorrow's
      due cards overnight.
- [ ] **Biometric re-auth** — `@capacitor-community/biometric-auth` for
      Face ID / Touch ID re-unlock on app open instead of password
      re-entry after JWT expiry.

---

## 4. Technical decisions needed

### 4.1 Hosted backend vs local backend
- **Hosted (recommended).** App Store apps cannot ship a local Python
  server. The backend must be deployed and reachable over HTTPS before
  TestFlight. Procfile is Heroku/Render-compatible.
- **Where to host?** Deferred decision. Likely targets: Render, Fly.io,
  Railway, AWS Lightsail. All support uvicorn + Postgres + env-driven config.

### 4.2 Normal `fetch` vs Capacitor HTTP plugin
- **Normal fetch (recommended).** Works in WKWebView, hits the same code
  paths as web. No third-party plugin churn.
- **`@capacitor/http` plugin.** Bypasses CORS by issuing native HTTP
  requests outside the WebView. Useful when CORS is hard to configure;
  unnecessary here because we control the backend. Stick with `fetch`.

### 4.3 `localStorage` vs `@capacitor/preferences` vs Keychain
| Storage | Encrypted | Async API | Survives reinstall | Best for |
|---|---|---|---|---|
| localStorage | No | No | iCloud-restore only | v0 (current) |
| @capacitor/preferences | No | Yes | iCloud-restore only | Settings, prefs |
| Keychain (secure-storage) | Yes | Yes | iCloud Keychain only | Tokens, passwords |

**Recommendation:** v1 ships with localStorage; immediate post-wrap PR
moves auth token to Keychain via `@capacitor-community/secure-storage`,
keeps `auth_email` in localStorage (low sensitivity).

### 4.4 Keep or disable service worker in Capacitor shell
**Keep.** It already bypasses `/api/*` and SSE, caches only hashed
immutable Vite assets + the shell. Worst case: a stale cached `index.html`
after a TestFlight update — mitigated by bumping `CACHE_VERSION` in sw.js
on each release, or gating the SW out of Capacitor via
`window.Capacitor` detection.

### 4.5 SSE notifications on mobile / native
Two-layer answer:

**Web (today):** `fetch('/api/v1/notifications/stream')` long-lived,
3s polling on the backend. Backgrounding the tab keeps the connection
alive but iOS Safari may pause JS execution.

**Native Capacitor (without changes):** SSE works while app is in
foreground. Backgrounding kills the WebView's JS loop. When the user
returns, the connection auto-reconnects on the next `useEffect` fire
because the hook is gated on `token` — but in-flight notifications are
lost (mitigated by per-row mark-after-yield since #4a).

**Native + APNs (after first wrap):** push notifications fire even when
app is backgrounded. Pairs with **#4b LISTEN/NOTIFY** on the backend so
we're not polling per-user.

**Decision for v1:** ship the wrap with SSE as-is. Document the
foreground-only constraint. Schedule APNs as a fast-follow.

---

## 5. Proposed Capacitor setup commands

**Do NOT run these during the audit.** Run only after §2 decisions are
locked.

```bash
# From frontend/ — adds Capacitor core + iOS platform.
cd lexy-app/frontend

# 1. Install Capacitor packages.
npm install @capacitor/core @capacitor/cli
npm install @capacitor/ios

# 2. Initialise (interactive — name=Lexy, appId=app.lexy.ios or similar).
npx cap init

# 3. Build the web bundle that Capacitor will package.
#    Requires VITE_API_BASE_URL to be set in .env.production so the bundle
#    points at the hosted backend, not /api.
npm run build

# 4. Add the iOS platform (scaffolds ios/App/App/Info.plist etc.).
npx cap add ios

# 5. Sync the latest web build into the iOS Xcode project.
#    Re-run this every time you change frontend code.
npx cap sync ios

# 6. Open Xcode to configure signing, capabilities, run on simulator.
npx cap open ios
```

After §5 you'll need (in Xcode):
- Set the development team + bundle identifier.
- Add capabilities: Push Notifications (deferred), Sign in with Apple
  (if we add it later), Background Modes (only if APNs).
- Configure `LaunchScreen.storyboard` if branded splash desired.
- For TestFlight: archive via Product → Archive, upload via
  Organizer → Distribute App.

---

## 6. Expected files to add / change

### New files
- [ ] `frontend/capacitor.config.ts` — generated by `npx cap init`.
      Holds `appId`, `appName`, `webDir: 'dist'`, optional `server.url`
      for dev mode pointing at a tunnel.
- [ ] `frontend/ios/` — entire directory generated by `npx cap add ios`.
      Should be checked in (per Capacitor convention) so signing/cert
      changes are versioned.
- [ ] `frontend/ios/App/App/Info.plist` — needs:
      - `NSAppTransportSecurity` (allow https://your-backend.example)
      - `LSApplicationCategoryType` (Education)
      - Future: `NSCameraUsageDescription`, `NSMicrophoneUsageDescription`
        if we ever record audio/video.

### Modified files
- [ ] `frontend/package.json` — Capacitor deps added by npm install.
      Add a script: `"cap:sync": "npm run build && npx cap sync ios"`.
- [ ] `frontend/.env.example` — document `VITE_API_BASE_URL`.
- [ ] `frontend/.env.production` (new) — actual hosted backend URL.
- [ ] `frontend/src/api/_http.ts` (or a new `_baseUrl.ts`) — single source
      of the API base. All `fetch('/api/...')` rewritten as
      `fetch(\`${API_BASE}/api/...\`)`.
- [ ] ~14 `frontend/src/api/*.ts` files — adopt the base-URL helper.
- [ ] `frontend/src/hooks/useNotifications.ts:19` — same.
- [ ] `frontend/index.html` — possibly tighten the viewport meta; possibly
      remove the `apple-mobile-web-app-*` tags inside the native shell
      (they're no-ops there, harmless either way).
- [ ] `frontend/public/sw.js` — optionally bump `CACHE_VERSION` to `v2`
      when shipping the Capacitor-aware build.
- [ ] Backend env (`CORS_ORIGINS`) on the hosted environment.
- [ ] `docs/ROADMAP.md` — flip T3.1 status as work progresses.

### Generated, NOT manually edited
- [ ] `frontend/ios/App/App/public/` — Capacitor copies the Vite `dist/`
      here on every `cap sync`. **Never edit by hand.**

---

## 7. Risks

### 7.1 Auth / session expiry
JWT defaults to 7 days. Mid-session expiry triggers `signalAuthExpired`
which clears local storage and bounces to home. **On native, this looks
like a logout — disruptive.** Mitigations:
- Long-lived JWT (already 7 days; could bump to 30 with refresh).
- Refresh-token flow (new endpoint + storage).
- Biometric re-auth on expiry (post-wrap).

### 7.2 CORS / API URL mismatch
Easy footgun: build the iOS bundle with `VITE_API_BASE_URL` empty →
all requests 404 because `fetch('/api/v1/...')` resolves against
`capacitor://localhost`. **Hard-fail mitigation:** add a startup assertion
in `main.tsx`:
```ts
if (window.Capacitor && !import.meta.env.VITE_API_BASE_URL) {
  document.body.innerHTML = '<h1>Build error: VITE_API_BASE_URL not set</h1>';
  throw new Error('Capacitor build requires VITE_API_BASE_URL');
}
```

### 7.3 Service worker cache issues
A stale cached `index.html` after a TestFlight update could pin the user
to old JS. Mitigations:
- Bump `CACHE_VERSION` on every release.
- Network-first navigation (already shipped — sw.js:77).
- Optional: gate SW out of Capacitor entirely.

### 7.4 iOS file upload / PDF upload
Book upload uses `<input type="file" accept=".pdf">`. WKWebView supports
this, but:
- iOS may surface only the Files app picker (not iCloud Drive). Usually fine.
- Large PDFs (>100MB) — current backend accepts them but may hit
  WKWebView memory limits. Test with 50MB+ files before TestFlight.
- Drag-and-drop is keyboard-driven on macOS but absent on iPhone.

### 7.5 SSE / background behaviour
See §4.5. Foreground-only delivery is the v1 constraint. Document in app
("notifications only arrive while app is open"). APNs is the upgrade.

### 7.6 App Store privacy / compliance
- **Account deletion** — Guideline 5.1.1(v). Must be in-app, not "email
  us to delete." Backend endpoint + UI required before submission.
- **Privacy policy URL** — required at submission time. Even a single
  HTML page on GitHub Pages works.
- **Data collection disclosure** — fill out App Privacy section in App
  Store Connect. We don't use third-party analytics, so the form is short.
- **YouTube embed compliance** — we use YouTube IFrame Player API. Their
  TOS prohibits commercial use without authorisation; we're an
  educational consumer app, generally allowed under the API ToS — but
  worth reviewing before App Store launch.

---

## 8. Recommended next implementation order

Ranked from smallest safe step → device testing → store prep. Each step
is independently shippable; don't bundle.

### 8.1 — Smallest safe first step (do BEFORE `npx cap init`)
1. [x] **Introduce `VITE_API_BASE_URL`** — ✅ DONE 2026-05-20.
       `frontend/src/api/_baseUrl.ts` exports `apiUrl(path)` +
       `API_BASE_URL`. All 14 api files + 4 component/hook inline fetches
       (`BookReaderPage`, `useNotifications`, `useReadingStats`,
       `useWordColors`) + `getPageImageUrl` (img src) route through it.
       Default empty string preserves path-relative web behaviour.
       Documented in `frontend/.env.example`. +13 unit + integration
       tests in `_baseUrl.test.ts`. Verified: tsc clean, vitest 127
       passed (was 114), vite build clean.
2. [ ] **Add `CORS_ORIGINS=capacitor://localhost,https://localhost`** to
       the deployed backend's env (alongside the web origin). Verify with
       `curl -H "Origin: capacitor://localhost" -i https://api.../...`.
       Full config + verification checklist in §9 below.

### 8.2 — Native wrapper setup
3. [x] `npm install @capacitor/core @capacitor/cli @capacitor/ios`
       — done (T3.1). `@capacitor/*@8.3.4` in lockfile.
4. [x] `npx cap init` — `capacitor.config.ts` written with placeholder
       `appId='com.lexy.learning'`, `appName='Lexy'`,
       `webDir='dist'`.
5. [ ] First `vite build` with `VITE_API_BASE_URL=https://your-backend`.
6. [x] `npx cap add ios` — `ios/` scaffolded; web assets copied to
       `ios/App/App/public/`; Podfile bumped to iOS 15.
7. [ ] `npx cap open ios` → set team + bundle ID in Xcode (blocked on
       full Xcode install).
8. [ ] Build to simulator. Verify: login, word click, status update, SRS
       review, dark mode follows simulator setting.

### 8.3 — Device testing
9. [ ] Install on a real device via Xcode → Run on iPhone. Verify the
       same flows under flaky network and after backgrounding.
10. [ ] Test SSE behaviour during backgrounding (expect lost stream;
        document the constraint).
11. [ ] Test PDF upload with a 5MB and 50MB file.
12. [ ] Test theme tristate (System should follow Settings → Display
        toggling Light/Dark live).
13. [ ] Verify offline.html appears with airplane mode on.

### 8.4 — App Store prep (in parallel with device testing)
14. [x] Privacy policy page — `/privacy` (W11). Linked from the global
        Layout footer. Replace the placeholder contact email before
        submission.
15. [x] Account-deletion endpoint + UI in SettingsPanel — `DELETE
        /api/v1/account` + destructive Account section with two-step
        confirm (W11).
16. [ ] App Store Connect: app record, screenshots (6.7", 5.5"),
        description, keywords, category=Education, age=12+.
17. [ ] App Privacy disclosures filled in.
18. [ ] 1024×1024 marketing icon uploaded.
19. [ ] TestFlight build uploaded; invite a small group.

### 8.5 — Post-launch (fast-follow)
20. [ ] Swap `localStorage` auth token → Keychain (post-wrap PR).
21. [ ] Add APNs push (`@capacitor/push-notifications` + backend
        `device_token` table) — pair with **#4b LISTEN/NOTIFY** so
        the backend isn't polling per-user.
22. [ ] Biometric re-auth on JWT expiry.

---

## 9. Concrete deployment configuration for Capacitor

The single combination of env vars that makes the Capacitor wrap reach the
backend without CORS errors. Copy-paste-ready.

### Frontend (Capacitor build only)

`lexy-app/frontend/.env.production` (or whichever env file Vite reads
for `npm run build`):

```env
# Absolute backend URL — required because the WebView origin is
# capacitor://localhost, so same-origin paths can't resolve.
VITE_API_BASE_URL=https://api.example.com
```

For the normal web/PWA build, leave `VITE_API_BASE_URL` empty (or omit
the env entirely). Same code, same bundle layout — `apiUrl()` returns
the path unchanged when the base is empty (see
`frontend/src/api/_baseUrl.ts`).

### Backend (hosted deployment)

`.env` on the backend host:

```env
CORS_ORIGINS=https://app.example.com,capacitor://localhost,https://localhost
```

Three origins required:

| Origin | Used by |
|---|---|
| `https://app.example.com` | The deployed web/PWA front-end. |
| `capacitor://localhost` | iOS WKWebView default scheme. |
| `https://localhost` | iOS WKWebView falls back here for some plugin paths. |

Order doesn't matter. Whitespace around commas is fine (the parser at
`backend/main.py:_parse_cors_origins` strips it). When `CORS_ORIGINS` is
empty/unset, the backend falls back to `http://localhost:5173` (dev only).

### Why this exact shape

- **Empty `VITE_API_BASE_URL` → unchanged web/PWA behaviour.** The bundle
  emits same-origin paths like `/api/v1/auth/login`; Vite dev proxy or
  the production reverse proxy routes them to FastAPI. No code branch on
  build target needed.
- **Non-empty `VITE_API_BASE_URL` → Capacitor works.** WKWebView serves
  the bundled `index.html` from `capacitor://localhost`. Same-origin
  fetches resolve against that URL, so they'd hit the iOS sandbox, not
  the backend. Absolute URLs bypass this.
- **`CORS_ORIGINS` must list both Capacitor schemes.** WKWebView's
  `Origin` header is `capacitor://localhost` for normal requests; some
  plugins (notably file/image uploads via `cap.convertFileSrc`) issue
  requests with `Origin: https://localhost`. Listing both is harmless
  and avoids one-off failures.
- **Service worker still bypasses API calls** when the bundle is loaded
  inside Capacitor. `sw.js:71` returns early for any cross-origin
  request (`url.origin !== self.location.origin`), so calls to
  `https://api.example.com/...` are never intercepted, never cached, and
  SSE/POST behave normally. The same SW also bypasses `/api/*` on
  same-origin web builds (line 74). Net result: zero SW interaction with
  the backend in either mode.

### Verification checklist (run after deploying both)

1. [ ] **Build the frontend with the env set.** From
       `lexy-app/frontend/`:
       ```bash
       VITE_API_BASE_URL=https://api.example.com npm run build
       ```
       Confirm `dist/assets/index-*.js` contains the absolute URL with
       `grep -o 'https://api.example.com[^"]*' dist/assets/index-*.js | head`.

2. [ ] **Open the app in the Capacitor shell** (TestFlight build, sim,
       or `npx cap run ios --livereload` once §8.2 has installed
       Capacitor).

3. [ ] **Login request reaches the hosted backend.** Watch backend logs
       for `POST /api/v1/auth/login` from the device's IP. In Safari
       Web Inspector → Network tab on the connected device, the request
       URL should be `https://api.example.com/api/v1/auth/login` and the
       `Origin` request header should read `capacitor://localhost`.

4. [ ] **Authenticated API request succeeds.** After login, navigate to
       the SRS page or Books library. `GET /api/v1/srs/due` or
       `GET /api/v1/books` should return 200 with the `Authorization:
       Bearer …` header intact.

5. [ ] **Notifications stream connects (or fails visibly).** Open Safari
       Web Inspector → Network and check that
       `https://api.example.com/api/v1/notifications/stream` shows as a
       long-lived `text/event-stream` response. Backgrounding the app
       will pause the stream (documented v1 limitation — APNs is the
       upgrade path); foregrounding should auto-reconnect on the next
       `useNotifications` effect.

6. [ ] **No CORS errors in Safari Web Inspector → Console.** Specifically
       check for:
       - "Origin capacitor://localhost is not allowed by Access-Control-
         Allow-Origin." → backend missing `capacitor://localhost` in
         `CORS_ORIGINS`.
       - "Origin https://localhost is not allowed…" → same fix, add
         the second scheme.
       - Preflight failures for `OPTIONS /api/v1/...` → check
         `allow_methods` includes the verb (currently allows GET/POST/
         PUT/PATCH/DELETE/OPTIONS, so all current routes are covered).

If any step fails, the most common causes:
- `VITE_API_BASE_URL` not set at build time → calls hit
  `capacitor://localhost/api/v1/…` and 404. Fix: rebuild with the env
  set, then `npx cap sync ios`.
- `CORS_ORIGINS` doesn't include the WebView origin → preflight or
  actual response missing `Access-Control-Allow-Origin: capacitor://
  localhost`. Fix: update backend env, restart the process.
- Bundle cached from a previous build → bump `sw.js:CACHE_VERSION` or
  uninstall+reinstall the app.

---

## Quick reference — key file locations

| Concern | File |
|---|---|
| API fetch sites | `frontend/src/api/*.ts` (14 files) |
| SSE consumer | `frontend/src/hooks/useNotifications.ts:19` |
| Auth storage | `frontend/src/auth.ts` |
| 401 handler | `frontend/src/api/_http.ts:signalAuthExpired` |
| Service worker | `frontend/public/sw.js` |
| PWA manifest | `frontend/public/manifest.webmanifest` |
| HTML shell | `frontend/index.html` |
| Vite config | `frontend/vite.config.ts` (dev proxy `/api`→:8000) |
| SW registration | `frontend/src/main.tsx:19` |
| Theme resolver | `frontend/src/hooks/useResolvedTheme.ts` |
| CORS config | `backend/main.py:_parse_cors_origins` |
| JWT expiry detail | `backend/core/deps.py` |
| Deploy entry | `Procfile` (root) |
| Env shape | `.env.example` (root) |
