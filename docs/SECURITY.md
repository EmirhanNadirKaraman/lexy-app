# SECURITY.md

Living security tracker for this repo. Not a vulnerability-disclosure policy — this is the working list of what's wrong, what's right, and how to re-check both.

**This file is paired with `CLAUDE.md` §14.** That section says *when* to read and update this file. This file holds the *findings*.

---

## How to use this file

- **Every open finding carries four things:** `file:line`, a severity, a one-line **verification check** (how to confirm it's still true / still fixed), and a suggested fix. A finding without a verification check is a dead finding — it rots into a claim nobody re-tests.
- **When a finding is fixed:** move it to *Resolved* with the date and the commit/PR. **Do not delete it** — the history is how we avoid regressions and how we remember why the code looks the way it does.
- **When you find something new:** append to *Open findings* with the same four fields. Add a row to the summary table.
- **The "Verified strengths" section is load-bearing.** Those are controls we depend on. If a PR weakens one (e.g. widens the settings whitelist, drops an ownership filter), that's a regression, not a refactor.

Severity legend: **HIGH** (exploitable now, real impact) · **MEDIUM** (exploitable under conditions, or a strong amplifier) · **LOW** (hardening / defense-in-depth) · **INFO** (note / good-practice gap, not a vuln today).

Last full sweep: **2026-05-24** (manual read of backend auth, routers, services, scraper, frontend token/XSS surface).

---

## Open findings — summary

> **S1, S4, S5, S6, S7, S11, S16, S17 are RESOLVED (2026-05-24)** — see the *Resolved findings* section. They are kept out of the open table below.

| ID | Severity | Title | Primary location |
|----|----------|-------|------------------|
| S2 | MEDIUM | Open-signup LLM-budget abuse — per-IP throttle (S1) + optional `REGISTRATION_CODE` invite gate DONE (2026-05-24); residual: open signup when no code set (needs email/CAPTCHA) | `routers/auth.py`, `services/auth_service.py` |
| S3 | MEDIUM | Account-deletion **re-auth DONE** (2026-05-24, password required); residual: no token revocation (leaked token → non-destructive access until 7-day expiry) | `routers/account.py`, `core/security.py`, `frontend/src/auth.ts` |
| S8 | LOW | Upload validation hardened — magic-byte + size (2026-05-24). Residuals (filename sanitization, proxy-level body spool) **deferred as accepted low risk** — no current exploit path | `routers/books.py` |
| S9 | LOW | Registration enumeration — generic failure message DONE (2026-05-24); residual: 201-vs-400 status still inferable without email verification | `services/auth_service.py` |
| S10 | LOW | bcrypt 72-byte truncation — register now rejects > 72-byte passwords (byte-accurate) + login/delete body-capped (DONE 2026-05-24); residual: pre-cap accounts may hold truncated hashes (not retroactively detectable) | `core/security.py`, `models/schemas.py`, `routers/account.py` |
| S12 | LOW | In-memory rate limiter bypassable across workers (known) | `services/rate_limiter.py` |
| S13 | INFO | f-string SQL in a migration (pattern caution) | `migrations/versions/013_*.py` |
| S14 | INFO | LLM prompt injection from user content | `services/llm_service.py` |
| S15 | INFO | No dependency vulnerability scanning | `requirements.txt`, `package.json` |

---

## Open findings — detail

> S1 (auth throttling), S5 (security headers), S6 (crash-report throttle), and S16 (sentence-match auth-gate) were resolved on 2026-05-24 — see *Resolved findings*.

### S2 — Open registration multiplies the per-user LLM budget — MEDIUM — PARTIALLY RESOLVED 2026-05-24
**Was:** open, unthrottled signup let an attacker mint accounts in a loop and multiply the per-user LLM budget into effectively no cap.
**Fix shipped (defence-in-depth, no email infra):**
- **Per-IP register throttle** (S1, already shipped): 5 registrations / hour / IP.
- **Optional invite code** (`REGISTRATION_CODE` env): when set, `/auth/register` requires a matching `registration_code` (constant-time compare via `hmac.compare_digest`; wrong/missing → the same generic 400 as a duplicate, so the cause isn't revealed). When unset, registration stays open (throttle only). Frontend `LoginForm` shows an optional invite-code field in register mode; `.env.example` documents the var.
**Residual (deferred):** when `REGISTRATION_CODE` is *unset* (the default), signup is still open — the per-IP throttle slows but doesn't stop a distributed account-minting campaign. Full closure of open-signup abuse wants email verification, CAPTCHA, payment, or invite-only mode (set `REGISTRATION_CODE`). Accepted until one is adopted.
**Re-check:** with `REGISTRATION_CODE=secret`, `POST /auth/register` without / with wrong code → 400; correct code → 201; unset → open. `rg -n 'REGISTRATION_CODE' lexy-app/backend/services/auth_service.py`; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_auth.py -q` passes.
**Tests:** `test_auth.py` +4 (code required when set, ignored when unset, wrong-code == duplicate-email response).

### S3 — Account deletion + long-lived non-revocable token + localStorage (chain) — MEDIUM — PARTIALLY RESOLVED 2026-05-24
**Was:** `DELETE /api/v1/account` required only a valid bearer token. Combined with 7-day non-revocable JWTs (`core/security.py`) stored in `localStorage`, a leaked token alone could irreversibly delete the account — the worst link in the chain.
**Fix shipped (password re-auth):** `DELETE /api/v1/account` now requires the current password in the request body, verified via `verify_password` (`routers/account.py`), in addition to the bearer token. Missing/empty/wrong password → **403** (`"Incorrect password. Please try again."`), no deletion; only a correct password runs the cascade delete. A stolen token can no longer delete the account. Frontend (`SettingsPanel`) gained a labelled password field in the confirm step (confirm disabled until filled); `api/account.deleteAccount(token, password)` sends it in the DELETE body and clears local auth only on the 204 (a 403 throws *without* logging the user out — `assertOk` clears only on 401).
**Design notes:** the 403 detail is shown to the user and intentionally says "incorrect password" — the caller is already authenticated (valid token), so this reveals no account-existence secret; "generic" here means *not distinguishing missing-vs-wrong* (it doesn't). Sends a body on DELETE — fine for the same-origin SPA → API call; a strict CDN/proxy in front would need to allow DELETE request bodies.
**Residual (deferred — token revocation):** the chain is only *partly* closed. There is still **no token revocation** — a leaked 7-day token can perform non-destructive actions until expiry (it just can't delete the account without the password). Full closure wants a `token_version` column checked in `decode_token` (or short access + rotating refresh token), and/or moving the token off `localStorage` to an httpOnly cookie (changes CSRF posture). Deferred per scope (touches the JWT/session model).
**Re-check:** bare `DELETE /api/v1/account` (token only, no body) → **403**, account intact; wrong password → 403; correct password → 204 + cascade. `rg -n 'verify_password' lexy-app/backend/routers/account.py`; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_account_deletion.py -q` passes.
**Tests:** `tests/test_account_deletion.py` +3 (no-body / empty-password / wrong-password → 403 with account + cascade intact; existing delete tests updated to send the password). Frontend: `AccountDeletion.test.tsx` (password field, disabled-until-filled, sends token+password) + `api/account.test.ts` (body, clears-on-204, preserves-auth-on-403).

### S8 — Upload hardening — LOW — PARTIALLY RESOLVED 2026-05-24
**Was:** `routers/books.py` validated only the `.pdf` extension (no content-type/magic check); large/lying uploads could do work before rejection; the raw `filename` is persisted.
**Resolved (2026-05-24):**
- **Content validation by magic bytes.** `upload_book` now rejects any body that doesn't start with `_PDF_MAGIC = b"%PDF-"` with **400** before `book_service.create_document` / `process_document` run. A renamed non-PDF (e.g. `.exe`→`.pdf`) is caught up front. The extension check is kept as a cheap first filter; `Content-Type` is intentionally **not** a gate (spoofable/varies by client) — a valid-magic file with an odd Content-Type is accepted.
- **Size guards (already in place, re-verified).** Pre-read `file.size` check → **413** before reading any body; bounded `await file.read(_MAX_UPLOAD_BYTES + 1)` → **413** even when `Content-Length` is missing/lying. No unbounded read before rejection.
**Residuals — deferred as accepted low risk (not queued work; no current exploit path):**
1. **`filename` is stored unsanitized** — latent stored-XSS only, and *not currently exploitable*: React escapes it everywhere it's rendered (a verified strength). On-disk path is `{doc_id}.pdf` (server UUID) so there's **no path traversal**. Becomes real only if someone later renders the filename via `dangerouslySetInnerHTML`; revisit then (length-cap + strip control/path chars at the upload boundary). Re-flag if the no-XSS-sinks strength is ever broken.
2. **Full request body is spooled to disk by Starlette before the handler runs** — the route-level bounded read caps *in-memory* bytes, not the disk spool. This is an infra concern, not app code: the real fix is `client_max_body_size` at the reverse proxy. Deferred to deployment config.
**Verification check (resolved parts):** unauthenticated upload still requires auth; a `.pdf` whose bytes are `b"not a pdf"` → **400** "not a valid PDF"; a `%PDF-` file with `Content-Type: application/octet-stream` → **200**; oversize → **413** with no downstream call. `rg -n '_PDF_MAGIC' lexy-app/backend/routers/books.py` shows the gate; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_books_upload.py -q` passes.

### S9 — User enumeration on registration — LOW — PARTIALLY RESOLVED 2026-05-24
**Was:** `/auth/register` raised "Email already registered" → a 400 that let an attacker probe which emails have accounts.
**Fix shipped:** the obvious leak is gone. `auth_service.register_user` raises a single `GENERIC_REGISTER_ERROR` ("Registration could not be completed. Please check your details, or sign in if you already have an account.") for *every* failure — duplicate email, and (when configured) wrong/missing invite code — so the response body no longer distinguishes causes. The frontend shows this message as-is.
**Residual (deferred — needs email verification):** this only *reduces* enumeration. A successful register still returns a user object (the SPA then logs in), so a **201-vs-400 status** difference remains: with `REGISTRATION_CODE` unset, an attacker can still infer "email exists" from the 400 status alone (just not from the message). True non-enumeration needs an email-verification flow (always 202 "check your email", reveal nothing) — out of scope (no SMTP/email infra). With `REGISTRATION_CODE` set, even that status difference is gated behind knowing the code.
**Re-check:** register a known email → 400 whose detail == `GENERIC_REGISTER_ERROR` (no "already registered"). `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_auth.py -q` passes.
**Tests:** `test_auth.py` — duplicate now asserts the generic message; +4 invite-code / indistinguishability tests.

### S10 — bcrypt 72-byte truncation; no password max length — LOW — PARTIALLY RESOLVED 2026-05-24
**Was:** `core/security.py` (`bcrypt.hashpw`/`checkpw`) silently ignores bytes past 72, and `RegisterRequest.password` had `min_length=8` but **no upper bound** — so a long password's effective space was smaller than the user believed, and no max length let a client POST a multi-MB string.
**Resolved (2026-05-24):**
- **Register rejects > 72-byte passwords up front**, byte-accurately. `BCRYPT_MAX_PASSWORD_BYTES = 72` lives in `core/security.py`; a `field_validator("password")` on `RegisterRequest` raises (→ 422 with a clear "must not exceed 72 bytes" message) when `len(v.encode("utf-8")) > 72`. **Bytes, not characters** — a char-based `max_length(72)` would wrongly accept a 37-char / 111-byte multibyte password (`"€" * 37`); the validator catches it. `min_length=8` is preserved. We deliberately do **not** pre-hash (that's a broader password-hashing migration), so nothing is silently truncated and the scheme is unchanged.
- **Login + account-delete carry a generous `max_length=1024` body guard** — explicitly NOT the 72-byte rule. Capping those verify-paths at 72 bytes would lock out any account whose password predates this cap (its stored hash already used the truncated 72 bytes, and `verify_password` truncates identically). 1024 is a multi-KB ceiling: well above any realistic password, well below a DoS body.
**Verification check:** `rg -n 'BCRYPT_MAX_PASSWORD_BYTES|field_validator' lexy-app/backend/models/schemas.py lexy-app/backend/core/security.py`; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_password_limits.py -q` passes (`"a"*73` → 422, `"€"*37` → 422, `"€"*24` (72 bytes) → 201, login `"a"*200` → 401 not 422, login `"a"*2000` → 422).
**Residual (accepted):** pre-cap accounts may have been registered with a password whose effective entropy was already bcrypt-truncated; this is **not retroactively detectable** without the plaintext, and forcing a reset would be hostile for a LOW finding. Full-length-password support would need the base64(sha256(pw))-before-bcrypt migration (use base64, **not** raw digest bytes — a null byte in the raw digest reintroduces the truncation bug); deferred as out of scope for S10.

### S12 — In-memory rate limiter bypassable across workers — LOW (known)
**Where:** `services/rate_limiter.py` — sliding window stored in process memory. Already documented in `CLAUDE.md` §10.
**Impact:** With multiple uvicorn/gunicorn workers, each worker has its own counters → effective limit is N×. Also resets on restart.
**Verification check:** Limiter state is a module-level dict; no Redis/shared store.
**Fix:** Back the limiter with Redis (or the DB) before scaling past one worker.

### S13 — f-string SQL in a migration (pattern caution) — INFO
**Where:** `lexy-app/backend/migrations/versions/013_video_category_cleanup.py:48` — `f"INSERT INTO video_category (category) VALUES ('{cat}')"`.
**Impact:** Not user-facing — migrations are append-only and admin-run, and the values are internal. Listed only so the pattern doesn't get copied into a request path. **Application queries are fully parameterized** (see strengths).
**Verification check:** `rg -n 'f"(SELECT|INSERT|UPDATE|DELETE)' lexy-app/backend` — any hit *outside* migrations that interpolates non-constant data is a real bug.
**Fix:** Use bound parameters even in migrations/data jobs.

### S14 — LLM prompt injection from user content — INFO
**Where:** `services/llm_service.py` and callers — book text, transcripts, and chat messages (all user-influenced) flow into Claude prompts.
**Impact:** A user can try to steer model output (e.g. via text embedded in an uploaded PDF). Impact is bounded because LLM output is not wired into shell/SQL/eval and responses are constrained by `tool_use` structured output. Treat as model-output manipulation, not RCE.
**Verification check:** Confirm no code path passes LLM output into a shell, SQL string, `eval`, or filesystem path.
**Fix:** Keep treating LLM output as untrusted; keep structured tool_use; don't interpolate model output into privileged sinks.

### S15 — No dependency vulnerability scanning — INFO
**Where:** `lexy-app/backend/requirements.txt`, `lexy-app/frontend/package.json` — no automated audit in CI.
**Impact:** Known-vuln versions of yt-dlp, anthropic SDK, FastAPI, React toolchain, etc. can land unnoticed.
**Verification check:** No `pip-audit` / `npm audit` step in CI config.
**Fix:** Add `pip-audit` (backend) and `npm audit --omit=dev` (frontend) to CI; review on a cadence.

---

## Verified strengths (do not regress)

These were checked in the 2026-05-24 sweep and are working controls. A PR that weakens one is a security regression.

- **SQL is fully parameterized** via asyncpg `$1`/`$2`. The only f-string queries in application code interpolate a loop integer and a module constant, not user data (`services/search_service.py:62,103,216`). User search terms go through bound params.
- **Authorization / ownership is consistently enforced** on user-owned resources:
  - Books: `book_service.get_document(pool, doc_id, user_id)` filters by `user_id` (`book_service.py:532`), called by every `/books/{doc_id}/...` handler.
  - Chat: `_require_session` rejects sessions not owned by the caller (`routers/chat.py:366`).
  - Reading: `delete_selection(pool, selection_id, user_id)` is user-scoped (`routers/reading.py:356`).
  - SRS: card lookup filters `card_id AND user_id` (`services/review_service.py:174`) — **verified, no IDOR**.
- **Privilege escalation is blocked at the settings write.** `update_preferences` only merges keys present in `DEFAULTS` (`services/settings_service.py:223`); `is_admin` is not in `DEFAULTS` (`settings_service.py:15`), so a user cannot grant themselves admin via `PUT /settings/preferences`. Enforced by `tests/test_settings.py`. **Do not widen this to a blind `{**current, **updates}` merge.**
- **Passwords** use bcrypt with per-password salt (`core/security.py:19`). **Login does not leak account existence** — generic "Invalid email or password" (`auth_service.py:37`).
- **CORS** is an explicit allow-list, not `*`, and credentials mode is not enabled; auth is Bearer-header (not cookies), so **CSRF does not apply** to the current design. (Revisit if S3's cookie option is taken.)
- **Secrets come from env**, never hardcoded; `SECRET_KEY` is required at boot (`core/security.py:15`). Only `.env.example` is tracked in git — the real `.env` is not committed.
- **No frontend XSS sinks.** No `dangerouslySetInnerHTML`, `innerHTML`, `document.write`, or `eval` in `frontend/src`; React auto-escaping covers user-rendered fields (filenames, notes). Keep it that way (ties to S8).
- **Notifications SSE** requires `get_current_user` and scopes rows to the user (`routers/notifications.py:65`).
- **Content-request subprocess** uses `create_subprocess_exec` with fixed args (`routers/content_requests.py:26`) — no shell, no command injection.
- **All 21 routers were swept for auth (2026-05-24).** Every handler is covered by `get_current_user` (router-level or per-handler) **except** the documented public ones — see the unauthenticated-surface list below. `search.py` is auth-gated at the router level; `playlists/generate` is auth-gated and is DB-only (no LLM, so it correctly does not need `rate_limit_llm`); `phrases/seed` is auth-gated **and** admin-gated via `require_admin` (S17 resolved). `POST /sentences/match` is now auth-gated too (S16 final fix), so **every** API handler requires a bearer token.

> **Doc-drift note:** `CLAUDE.md` §7 calls `/api/search`, `/api/suggest`, `/api/video-sentences`, `/api/word-forms`, `/api/languages`, `/api/categories` "public legacy endpoints." They are actually auth-gated at the router level (`routers/search.py:20`, `APIRouter(dependencies=[Depends(get_current_user)])`). No data leak — but the §7 label is stale and should not be trusted when reasoning about the public attack surface.
>
> **The genuinely unauthenticated surface is:** the static file mount (`main.py:142`) and `POST /api/v1/errors/client` (per-IP throttled — S6 resolved). The FastAPI docs `/docs` + `/openapi.json` are off by default (require an explicit `ENABLE_DOCS=true` — S11 resolved). `POST /sentences/match` is no longer public (auth-gated — S16 final fix). Everything else requires a valid bearer token.

---

## Resolved findings

### S1 — No brute-force / rate limit on auth endpoints — HIGH — RESOLVED 2026-05-24
**Was:** `/api/v1/auth/login` and `/register` accepted unlimited attempts (the rate limiter only guarded LLM routes) → password brute-force, credential-stuffing, signup floods.
**Fix shipped:** Added a generic single-window limiter `rate_limiter.check_window(key, limit, window_seconds, …)` (reuses the existing in-process store + lock) and two web-layer helpers in `core/deps.py` called at the top of each handler *before* any DB/bcrypt work:
- `rate_limit_login(request, email)` — **two-tier** (deliberate; the original finding said "or", we did both): per `(ip, email)` **10 / 5 min** (targeted brute-force on one account) **and** per `ip` **30 / 5 min** (password-spraying across accounts).
- `rate_limit_register(request)` — per `ip` **5 / hour**.
Throttled requests get **429** with a generic body `"Too many attempts. Try again later."` — identical for login and register and independent of whether the account exists, so the throttle never leaks account existence. Client IP comes from `request.client.host`; `X-Forwarded-For` is honoured only when `TRUST_PROXY_HEADERS=true` (spoofable otherwise). Limits are module constants in `rate_limiter.py` (monkeypatchable in tests).
**Still in-process (caveat):** like the LLM limiter, this is per-worker — a multi-worker deploy needs a shared backend (Redis). Tracked under S12.
**Tests:** `tests/test_auth_throttle.py` (9 tests — below-limit success, per-email 429, per-IP spray-guard 429, register 429, no-enumeration, reset, validation-not-throttled). Existing `tests/test_auth.py` still green.
**Re-check:** `rg -n 'rate_limit_login|rate_limit_register' lexy-app/backend/routers/auth.py` shows both wired; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_auth_throttle.py backend/tests/test_auth.py -q` passes.

### S5 — No HTTP security headers — MEDIUM — RESOLVED 2026-05-24
**Was:** Only `CORSMiddleware` was installed; no CSP/HSTS/X-Frame-Options/X-Content-Type-Options/Referrer-Policy/Permissions-Policy.
**Fix shipped:** `core/security_headers.py` adds `SecurityHeadersMiddleware`, wired in `main.py` **after** CORS so it is the outermost middleware and stamps headers on every response (success, 4xx, 404, validation errors). Baseline always-on: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`, `Cross-Origin-Opener-Policy: same-origin`, and a CSP. **HSTS is opt-in** via `ENABLE_HSTS=true` (off by default so local HTTP dev isn't pinned). The CSP was tuned against the actual built SPA (`frontend/dist/index.html` loads only external same-origin JS/CSS; React uses inline `style=` attributes; PlayerView embeds YouTube; SSE/fetch are same-origin):
```
default-src 'self';
script-src 'self' https://www.youtube.com https://s.ytimg.com;
style-src 'self' 'unsafe-inline'; img-src 'self' data: https:;
font-src 'self' data:; connect-src 'self';
frame-src https://www.youtube.com https://www.youtube-nocookie.com;
object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
```
The YouTube origins are in **`script-src`** (not just `frame-src`) because `YoutubeEmbed.tsx` injects `https://www.youtube.com/iframe_api` at runtime — a bare `script-src 'self'` would silently break video playback in production (tests stub the YT API, so they wouldn't catch it; this was caught by reading the embed code). `worker-src` (for `dist/sw.js`) intentionally falls back to `script-src`. SSE (`/notifications/stream`, a `StreamingResponse`) verified still streaming through the middleware via `tests/test_notifications.py`.
**New env vars:** `ENABLE_HSTS`, `TRUST_PROXY_HEADERS` (documented in `.env.example`).
**Tests:** `tests/test_security_headers.py` (8 tests — headers on 200/403/404/422, HSTS off-by-default + on-when-enabled, policy-builder unit, CSP allows app needs).
**Re-check:** `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_security_headers.py backend/tests/test_notifications.py -q` passes; `rg -n 'SecurityHeadersMiddleware' lexy-app/backend/main.py` shows it wired.
**Follow-up (not blocking):** CSP `script-src 'self'` has no `nonce`/hash; if a future build inlines a script, either externalize it or add a nonce — don't add `'unsafe-inline'` to `script-src`.

### S6 — Unauthenticated, unthrottled crash-report insert — MED-LOW — RESOLVED 2026-05-24
**Was:** `POST /api/v1/errors/client` was public (by design — crashes happen pre-login) but had no rate limit → anyone could flood `client_error_log` (storage DoS + log-spam hiding real crashes).
**Fix shipped:** `core/deps.rate_limit_client_errors(request)` (per-IP, **30 / 10 min** via `rate_limiter.check_window`) called at the **top** of `report_client_error` — before the user-id lookup and the insert. The endpoint stays public and the frontend contract is unchanged: the ErrorBoundary reporter is fire-and-forget and ignores the 429. Payload caps were already enforced (`MAX_*` + `_truncate`, `message` `min_length=1`) — left as-is.
**Not done (deferred):** a retention/pruning job (or row cap) for `client_error_log`; the throttle bounds inflow but not lifetime accumulation.
**Tests:** `tests/test_client_errors.py` +2 (per-IP 429 after limit; authed report below limit still 204). Existing 9 tests still green.
**Re-check:** `rg -n 'rate_limit_client_errors' lexy-app/backend/routers/errors.py` shows it wired; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_client_errors.py -q` passes.

### S16 — `POST /sentences/match` unauthenticated + uncapped input — MED-LOW — RESOLVED 2026-05-24 (auth-gated)
**Was:** Public route with a bare `str` body running spaCy → unauthenticated CPU-exhaustion DoS (large input and/or high volume).
**First fix (superseded):** shipped public + per-IP throttle + input cap (commit `a94b751`). Closed the DoS but kept the route public.
**Final fix (auth-gated):** `POST /api/v1/sentences/match` now requires `Depends(get_current_user)` (`routers/matcher.py`). The frontend never calls it (`rg sentences/match lexy-app/frontend/src` is empty) — it's a logged-in utility/debug route — so authentication is the right gate and **removes the unauthenticated CPU-DoS surface entirely**. With the route no longer public, the per-IP throttle on it was dropped (an authenticated abuser is identifiable and out of scope for S16; an authenticated rate limit can be re-added later if wanted). The **input cap is kept** (`MatchRequest.sentence = Field(..., max_length=1000)`) as defence-in-depth — an over-length body is still 422'd before the parser runs.
**Cleanup (done):** the now-dead `core/deps.rate_limit_sentence_match` helper + `SENTENCE_MATCH_*` constants (`services/rate_limiter.py`) were removed in a follow-up commit. If authenticated throttling on this route is ever wanted, re-add it via `rate_limiter.check_window`.
**Tests:** `tests/test_matcher.py` — `test_unauthenticated_request_rejected` (403), `test_authenticated_request_succeeds` (200), `test_oversize_sentence_returns_422`; existing HTTP tests now run through an authenticated client fixture. The old public-throttle file `tests/test_matcher_limits.py` was deleted (its tests assumed public access).
**Re-check:** unauthenticated `POST /api/v1/sentences/match` → **403**; authenticated → **200**. `rg -n 'get_current_user' lexy-app/backend/routers/matcher.py` shows the gate; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_matcher.py -q` passes.

### S7 — Unvalidated `content_id` fed to yt-dlp — MED-LOW — RESOLVED 2026-05-24
**Was:** `ContentRequestCreate.content_id` was a free-form `str`. The scraper interpolates it into `youtube.com` URLs handed to `yt_dlp` (`pipeline.py:141/211/232/551`). Host was pinned to youtube.com (not arbitrary SSRF), but any string reached yt-dlp's large extractor surface, and a huge channel is unbounded work. No shell injection (subprocess uses `create_subprocess_exec` with fixed args).
**Fix shipped (API-boundary allowlist):** a Pydantic `model_validator` on `ContentRequestCreate` (`routers/content_requests.py`) now normalizes + validates `content_id` per `request_type` *before* the handler runs (a `ValueError` → **422**, so nothing reaches the DB or the subprocess):
- **video** → `^[A-Za-z0-9_-]{11}$`; also accepts and normalizes `youtube.com/watch?v=`, `youtu.be/<id>`, `youtube.com/shorts/<id>` to the bare 11-char id.
- **channel** → `^UC[A-Za-z0-9_-]{22}$`; also accepts `youtube.com/channel/UC…` (handle/@/c/user URLs rejected — they don't yield a canonical UC id).
- Non-YouTube hosts, shell-metachar strings, path-traversal-ish input, empty/whitespace, and anything > 256 chars are rejected. The stored value is always the canonical bare id.
**Accepted formats:** bare video id (11 chars) · bare channel id (`UC`+22) · `https://www.youtube.com/watch?v=<id>` · `https://youtu.be/<id>` · `https://www.youtube.com/shorts/<id>` · `https://www.youtube.com/channel/<UC…>` (host ∈ youtube.com/www/m/youtu.be/youtube-nocookie).
**Auth note:** the route still requires a bearer token (`get_current_user`); this validation is a *second* gate after auth. **Scraper untouched** (the API is the only writer of `content_request`, so it inherits validated ids). **Residual (deferred):** legacy rows created before this commit aren't re-validated — defense-in-depth re-checking at the scraper before `yt_dlp` would catch them, deferred per scope.
**Tests:** `tests/test_content_requests.py` +12 (URL normalization for watch/youtu.be/channel; rejects non-YouTube host, look-alike host, shell strings, path-traversal, overlong, empty, wrong-length, wrong-type; invalid request neither inserts a row nor spawns the scraper).

### S11 — FastAPI interactive docs + OpenAPI schema exposed — LOW/INFO — RESOLVED 2026-05-24
**Was:** `main.py` built `FastAPI(...)` with the default `docs_url`/`redoc_url`/`openapi_url`, so `/docs`, `/redoc`, and `/openapi.json` (the full route + model schema) were publicly reachable in every environment.
**Fix shipped (env-gated, secure default):** `main.py:_docs_enabled()` reads `ENABLE_DOCS` (truthy ∈ `{1,true,yes}`, matching the `ENABLE_HSTS` idiom) and the app passes `docs_url`/`redoc_url`/`openapi_url = None` unless it's set. **Default OFF** — a production deploy that doesn't opt in never publishes the schema; local dev sets `ENABLE_DOCS=true`. `.env.example` ships `ENABLE_DOCS=false` (off everywhere by default, so a prod that copied the template isn't exposed). No residual: the only way to expose docs now is an explicit opt-in.
**Verification check:** `rg -n 'docs_url|_docs_enabled' lexy-app/backend/main.py`; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_docs_gating.py -q` passes (env-unset → `app.openapi_url is None` + `/docs`,`/redoc`,`/openapi.json` → 404; truthy values enable). With `ENABLE_DOCS` unset, `GET /openapi.json` → 404.
**Tests:** `tests/test_docs_gating.py` +15 (parse matrix for `_docs_enabled`; live app docs URLs agree with the flag; endpoints 404 when off).

### S17 — `POST /phrases/seed` triggerable by any authenticated user — LOW/INFO — RESOLVED 2026-05-24
**Was:** `/phrases/seed` was gated by `get_current_user` only — any logged-in user could trigger a global `phrase_table` re-seed (shared-catalog write + dict/DB work). Idempotent, so impact was low, but a shared-resource/admin operation shouldn't be user-triggerable.
**Fix shipped (admin gate):** new `core/deps.require_admin` dependency (depends on `get_current_user`; **403 `admin_required`** when `current_user["is_admin"]` is false, returns the user otherwise). `routers/phrases.py:seed_phrases` now `Depends(require_admin)`. `is_admin` is planted out-of-band in `users.settings` and **cannot be self-granted** — `settings_service` filters writes to `DEFAULTS` keys (`settings_service.py:223`, `k in DEFAULTS`) and `is_admin` isn't one, so a `PUT /settings` can't set it. `/phrases/match` and `GET /phrases` are unchanged (not admin operations).
**Behaviour change (intentional):** a non-admin authenticated user who could previously trigger a reseed now gets 403 — that's the finding, not a regression. Startup still seeds in the lifespan; this endpoint remains the manual recovery path for admins.
**Verification check:** `rg -n 'require_admin' lexy-app/backend/routers/phrases.py lexy-app/backend/core/deps.py`; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_phrases_seed_admin.py -q` passes (non-admin → 403 `admin_required`; admin via out-of-band SQL → 201; no token → 401/403).
**Tests:** `tests/test_phrases_seed_admin.py` +3.

### S4 — DB connections created without TLS — MEDIUM (deploy-dependent) — RESOLVED 2026-05-24
**Was:** every DB connection site opened without an `ssl`/`sslmode` argument, so credentials + query traffic could cross the network in plaintext against a remote/managed Postgres.
**Fix shipped — `DB_SSL_MODE` applied to all four connection families:**
- **App pool** (`backend/database.py`, first pass): `asyncpg.create_pool(ssl=_resolve_ssl(...))`. `disable`/unset → `ssl=False`; `require`/`verify-ca`/`verify-full` → enforced; `prefer`/`allow`/invalid → `ValueError`. Test pool (`tests/conftest.py`) threads the same helper.
- **Alembic** (`backend/migrations/env.py`, this pass): `get_url()` appends `?sslmode=<mode>` via the new `database.resolve_sslmode()`.
- **Scraper** (`subtitle-scraper/`, this pass): new `db_ssl.py` (`sslmode_from_env()` / `connect_kwargs()`); all five `psycopg2.connect()` sites (`pipeline.py`, `seed_channels.py`, `backfill_video_channels.py`, `backfill_channel_names.py`, `backfill_categories.py`) pass `**connect_kwargs()`. A deliberate small duplicate since the scraper can't import the backend.
- **Policy is uniform** (same accepted values, `prefer`/`allow` rejected everywhere). The libpq sites (`resolve_sslmode` / `sslmode_from_env`) return `None` for `unset`/`disable` so the caller **omits** the param and libpq's default is preserved — same "preserve the driver default when unset" principle as the asyncpg path (which returns `ssl=False` because asyncpg's no-arg default is already no-TLS), applied to a driver whose default (`prefer`) differs. Net: **zero behaviour change when unset**, no forced-plaintext that could break a prod scraper.
**Deployment note (runbook, not a code residual):** a remote/managed prod DB must set `DB_SSL_MODE=require` (or `verify-full`); local dev stays `disable`. Same shape as S11's `ENABLE_DOCS`.
**Verification check:** `rg -n 'resolve_sslmode|connect_kwargs|sslmode' lexy-app/backend/database.py lexy-app/backend/migrations/env.py subtitle-scraper/db_ssl.py`; `cd lexy-app && python -m pytest backend/tests/test_database_ssl.py -q` (asyncpg `_resolve_ssl` + libpq `resolve_sslmode`) and `python -m pytest tests/test_scraper_db_ssl.py -q` (scraper resolver + `seed_channels.connect()` forwards `sslmode`) pass; `alembic upgrade head` loads `env.py` cleanly.
**Tests:** `tests/test_database_ssl.py` (+8 `resolve_sslmode`), `tests/test_scraper_db_ssl.py` (+16, new).
**Re-check:** `rg -n 'model_validator|_normalize_video_id|_normalize_channel_id' lexy-app/backend/routers/content_requests.py`; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_content_requests.py -q` passes (a bare 11-char id / `UC…` id → 201; `https://evil.com/...` or `"; rm -rf /"` → 422).

---

## Changelog

- **2026-05-24** — Initial audit. 17 open findings (S1–S17), verified-strengths baseline recorded. Swept all 21 routers for auth; only `POST /sentences/match` (S16) is unauthenticated.
- **2026-05-24** — **S1 (auth throttling)** and **S5 (HTTP security headers)** resolved. 17 new tests (`test_auth_throttle.py`, `test_security_headers.py`); added env vars `ENABLE_HSTS`, `TRUST_PROXY_HEADERS`. 15 findings remain open (S2–S4, S6–S17).
- **2026-05-24** — **S6 (crash-report throttle)** and **S16 (sentence-match throttle + input cap)** resolved via the same per-IP `check_window`. 6 new tests (`test_client_errors.py` +2, `test_matcher_limits.py` +4). 13 findings remain open (S2–S4, S7–S15, S17). S16 ships public (throttled+capped); auth-gating is a now-unblocked follow-up (#39 has landed).
- **2026-05-24** — **S16 final fix:** `POST /sentences/match` auth-gated (`Depends(get_current_user)`); public per-IP throttle on it dropped (input cap kept as defence-in-depth). `test_matcher.py` gains auth tests (403 unauth / 200 auth / 422 over-length); obsolete `test_matcher_limits.py` deleted. The now-unused `rate_limit_sentence_match` helper + `SENTENCE_MATCH_*` constants were removed in a follow-up cleanup commit. Every API handler now requires a bearer token.
- **2026-05-24** — **S8 partially resolved:** PDF upload now validates the `%PDF-` magic header (400 on a renamed non-PDF) before any processing; size guards re-verified (413 via `file.size` + bounded read); `Content-Type` deliberately not gated. `test_books_upload.py` +2 (wrong-magic rejected, mismatched-Content-Type accepted). Residuals deferred: filename sanitization (latent stored-XSS) + full-body disk spool (reverse-proxy fix). Open: S2–S4, S7, S9–S15, S17 (+ S8 residuals).
- **2026-05-24** — **S7 resolved:** `content_id` validated + normalized to a canonical YouTube id at the API boundary (`model_validator` on `ContentRequestCreate`) before any DB write or scraper spawn — bare ids or youtube.com/youtu.be URLs accepted, everything else 422'd. `test_content_requests.py` +12. Scraper untouched (inherits validated ids); legacy rows not re-validated (deferred). Open: S2–S4, S9–S15, S17 (+ S8 residuals).
- **2026-05-24** — **S9 + S2 partially resolved (no email infra):** registration failures now return one generic message (`GENERIC_REGISTER_ERROR`) for duplicate-email and bad-invite-code alike (S9 enumeration leak removed); optional `REGISTRATION_CODE` env gates signup invite-only when set, on top of the S1 per-IP throttle (S2). Frontend `LoginForm` gained an optional invite-code field. `test_auth.py` +5; `LoginForm.register.test.tsx` +4. Residuals: true non-enumeration + open-signup closure need email verification / CAPTCHA (deferred). Open: S4, S10–S15, S17 (+ S2/S3/S8/S9 residuals).
- **2026-05-24** — **S3 partially resolved:** `DELETE /account` now requires password re-auth (verified via `verify_password`; missing/wrong → 403), closing the stolen-token → instant-delete hole. Frontend gained a labelled password field (confirm disabled until filled). Backend `test_account_deletion.py` +3; frontend `AccountDeletion.test.tsx` updated + `api/account.test.ts` +3. **Residual deferred:** token revocation (a leaked token still has non-destructive access until 7-day expiry) — needs the JWT/session model. Open: S2, S4, S9–S15, S17 (+ S3 token-revocation & S8 residuals).
- **2026-05-24** — **S4 partially resolved:** the asyncpg pool now passes `ssl=_resolve_ssl(os.getenv("DB_SSL_MODE"))` instead of omitting `ssl=`. `DB_SSL_MODE` ∈ {`disable` (default → plaintext), `require`, `verify-ca`, `verify-full`}; `prefer`/`allow` rejected (silent-plaintext-fallback footgun), unknown values raise at startup. `tests/conftest.py` pool threads the same helper; `.env.example` documents the knob. New `test_database_ssl.py` +13 (mapping + mocked `create_pool` forwarding + startup-raise). **Residual deferred:** prod must SET `DB_SSL_MODE=require` for a remote DB (default stays plaintext for local dev), and alembic `env.py` + `subtitle-scraper` connections still lack TLS. Open: S2, S9–S15, S17 (+ S3 token-revocation, S4 operational/other-sites & S8 residuals).
- **2026-05-24** — **S10 partially resolved:** `RegisterRequest.password` gained a byte-accurate upper bound (`BCRYPT_MAX_PASSWORD_BYTES = 72` in `core/security.py`; `field_validator` → 422 when the UTF-8 byte length exceeds 72, so multibyte passwords under 72 *chars* but over 72 *bytes* are caught); `min_length=8` preserved. `LoginRequest` + `AccountDeleteRequest` got a generous `max_length=1024` body guard (NOT the 72-byte rule — avoids locking out pre-cap accounts). No pre-hashing (scheme unchanged, nothing truncated). New `test_password_limits.py` +8. **Residual accepted:** pre-cap accounts may hold already-truncated hashes (not retroactively detectable); full-length support needs the base64(sha256(pw))-before-bcrypt migration (deferred). Open: S2, S9, S11–S15, S17 (+ S3 token-revocation, S4 operational/other-sites, S8 & S10 long-password residuals).
- **2026-05-24** — **S11 resolved:** interactive docs + OpenAPI schema are now env-gated. `main.py:_docs_enabled()` (reads `ENABLE_DOCS`, `ENABLE_HSTS` idiom) makes `docs_url`/`redoc_url`/`openapi_url` `None` by default; only an explicit `ENABLE_DOCS=true` exposes `/docs`,`/redoc`,`/openapi.json`. `.env.example` ships it `false` (off everywhere by default). New `test_docs_gating.py` +15. Moved to *Resolved findings* (no residual — secure by default, opt-in for dev). Open: S2, S9, S12–S15, S17 (+ S3 token-revocation, S4 operational/other-sites, S8 & S10 long-password residuals).
- **2026-05-24** — **S17 resolved:** `POST /phrases/seed` is now admin-gated. New `core/deps.require_admin` (403 `admin_required` for non-admins); `routers/phrases.py:seed_phrases` depends on it. `is_admin` lives in `users.settings`, planted out-of-band and not self-grantable (settings writes are filtered to `DEFAULTS`). Non-admins who could previously reseed now get 403 (intentional — the finding). New `test_phrases_seed_admin.py` +3. Moved to *Resolved findings* (no residual). Open: S2, S9, S12–S15 (+ S3 token-revocation, S4 operational/other-sites, S8 & S10 long-password residuals).
- **2026-05-24** — **S4 fully resolved:** `DB_SSL_MODE` now covers the two remaining connection families. Alembic `migrations/env.py` appends `?sslmode=<mode>` via new `database.resolve_sslmode()`; the scraper gets `subtitle-scraper/db_ssl.py` and all five `psycopg2.connect()` sites pass `**connect_kwargs()`. Uniform policy (reject `prefer`/`allow`); libpq sites omit the param on unset/disable (preserve driver default → zero behaviour change). `test_database_ssl.py` +8, new `test_scraper_db_ssl.py` +16. The "prod must set `DB_SSL_MODE=require` for a remote DB" item is now a deployment runbook note, not a code residual. Moved to *Resolved findings*. Open: S2, S9, S12–S15 (+ S3 token-revocation, S8 & S10 long-password residuals).
