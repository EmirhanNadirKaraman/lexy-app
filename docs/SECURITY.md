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

> **S1, S5, S6, S16 are RESOLVED (2026-05-24)** — see the *Resolved findings* section. They are kept out of the open table below.

| ID | Severity | Title | Primary location |
|----|----------|-------|------------------|
| S2 | MEDIUM | Open registration multiplies the per-user LLM budget | `routers/auth.py` + `services/rate_limiter.py` |
| S3 | MEDIUM | Account deletion + long-lived non-revocable token + localStorage (chain) | `routers/account.py`, `core/security.py`, `frontend/src/auth.ts` |
| S4 | MEDIUM | DB connection pool created without TLS | `database.py` |
| S7 | MED-LOW | Unvalidated `content_id` fed to yt-dlp | `routers/content_requests.py` → `subtitle-scraper/pipeline.py` |
| S8 | LOW-MED | Upload: extension-only check, unsanitized filename, pre-handler disk spool | `routers/books.py` |
| S9 | LOW | User enumeration on registration | `services/auth_service.py` |
| S10 | LOW | bcrypt 72-byte truncation; no password max length | `core/security.py`, `models/schemas.py` |
| S11 | LOW/INFO | FastAPI `/docs` + `/openapi.json` exposed | `main.py` |
| S12 | LOW | In-memory rate limiter bypassable across workers (known) | `services/rate_limiter.py` |
| S13 | INFO | f-string SQL in a migration (pattern caution) | `migrations/versions/013_*.py` |
| S14 | INFO | LLM prompt injection from user content | `services/llm_service.py` |
| S15 | INFO | No dependency vulnerability scanning | `requirements.txt`, `package.json` |
| S17 | LOW/INFO | `POST /phrases/seed` triggerable by any authenticated user (not admin-gated) | `routers/phrases.py` |

---

## Open findings — detail

> S1 (auth throttling), S5 (security headers), S6 (crash-report throttle), and S16 (sentence-match auth-gate) were resolved on 2026-05-24 — see *Resolved findings*.

### S2 — Open registration multiplies the per-user LLM budget — MEDIUM
**Where:** `routers/auth.py` register (no throttle, no email verification, no captcha) combined with `services/rate_limiter.py`, whose budget is keyed *per user*.
**Impact:** The LLM rate limit is per-user, so an attacker who can mint accounts in a loop multiplies the total LLM spend an attacker can drive — turning the per-user cap into no cap. This is a consequence of S1 but worth tracking on its own because the fix is different (registration friction, not login throttle).
**Verification check:** Confirm `/auth/register` has no captcha, no email-verification gate, and no per-IP cap (`rg -n 'captcha|verify_email|email_verified' lexy-app/backend` → nothing).
**Fix:** Require email verification before an account can call paid routes, add a captcha or per-IP registration cap.

### S3 — Account deletion + long-lived non-revocable token + localStorage (chain) — MEDIUM (HIGH if any XSS lands)
**Where:** `routers/account.py:26` (`DELETE /api/v1/account` — bearer token is the *only* gate; no password re-auth); `core/security.py:13` (`ACCESS_TOKEN_EXPIRE_MINUTES = 60*24*7`, 7 days) with no revocation/blacklist in `decode_token` (`core/security.py:32`); token stored in `localStorage` (`frontend/src/auth.ts:12`).
**Impact:** This is a *chain*. A leaked token is valid for 7 days, cannot be revoked, and is readable by any JavaScript on the page. With only that token an attacker can irreversibly delete the account (and all cascading data). Any future XSS — there is none today (see strengths) — converts directly into account takeover + destruction for a week.
**Verification check:** `rg -n 'password|verify' lexy-app/backend/routers/account.py` → no re-auth; confirm no token-revocation table/column exists; confirm `setToken` still writes `localStorage`.
**Fix:** (a) Require password re-authentication (or a "recent login" check) for destructive account actions. (b) Add token revocation — a `token_version` column on `users` checked in `decode_token`, or a short access token + rotating refresh token. (c) Consider moving the token to an httpOnly cookie (changes CSRF posture — see strengths) or accept localStorage only while XSS surface stays zero.

### S4 — DB connection pool created without TLS — MEDIUM (deploy-dependent)
**Where:** `database.py:11–19` — `asyncpg.create_pool(...)` is called with discrete host/port/user/password kwargs and **no `ssl=` argument**. With kwargs and no `sslmode`, asyncpg does not negotiate TLS by default.
**Impact:** If `DB_HOST` is a remote/managed Postgres, credentials and all query traffic may cross the network in plaintext. Non-issue when `DB_HOST` is localhost.
**Verification check:** `rg -n 'create_pool|ssl' lexy-app/backend/database.py` → no `ssl=`; then check the deployed `DB_HOST` is local. If remote and no `ssl`, open.
**Fix:** Pass `ssl="require"` (or, better, verify-full with the provider CA) whenever `DB_HOST` is not localhost. Same applies to `migrations/env.py` and `subtitle-scraper` DB connections.

### S7 — Unvalidated `content_id` fed to yt-dlp — MED-LOW
**Where:** `routers/content_requests.py:31–33` (`ContentRequestCreate.content_id: str` — free-form, no format validation). The scraper reads it (`subtitle-scraper/pipeline.py:785`) and interpolates it into YouTube URLs handed to `yt_dlp` (`pipeline.py:141`, `:211`, `:232`, `:529`).
**Impact:** The URL host is pinned to `youtube.com`, so this is not arbitrary SSRF, but (a) any string reaches the large yt-dlp attack/extractor surface, and (b) a request for a huge channel is unbounded work — resource exhaustion. The subprocess itself uses `create_subprocess_exec` with fixed args, so there is **no shell/command injection** (good).
**Verification check:** Confirm `ContentRequestCreate.content_id` has no regex/length validator and the pipeline still interpolates it into the URL string.
**Fix:** Validate at the API boundary — video `^[A-Za-z0-9_-]{11}$`, channel `^UC[A-Za-z0-9_-]{22}$`. Cap per-channel video count and wall-clock for the subprocess.

### S8 — Upload: extension-only check, unsanitized filename, pre-handler disk spool — LOW-MED
**Where:** `routers/books.py:115` validates only `file.filename.lower().endswith(".pdf")` (no content-type / magic-byte check). `books.py:147` stores the raw `file.filename` in the DB. The handler's own comment (`books.py:118–127`) notes Starlette has already spooled the full request body to disk before the size guard runs.
**Impact:** (1) A non-PDF can be uploaded with a `.pdf` name (file-type confusion; docling will likely fail, low impact). (2) The on-disk file is named `{doc_id}.pdf` with a server UUID, so there is **no path traversal** — good — but the user's `filename` is persisted and shown in the UI. React escapes it today (no stored XSS), but it becomes a stored-XSS payload the moment anyone renders it via `dangerouslySetInnerHTML`. (3) Large/lying uploads spool to disk before rejection → disk-exhaustion DoS.
**Verification check:** Confirm the only type check is the extension; confirm `filename` is still persisted; confirm there's no magic-byte check.
**Fix:** Verify the `%PDF-` magic bytes; sanitize + length-cap `filename` before storage; enforce the size limit at the reverse proxy / ASGI middleware so the body is never fully spooled.

### S9 — User enumeration on registration — LOW
**Where:** `services/auth_service.py:14–15` raises "Email already registered" → `routers/auth.py:15` returns it as a 400.
**Impact:** An attacker can probe which emails have accounts. (Login is *not* enumerable — it returns a generic "Invalid email or password" — see strengths.)
**Verification check:** Register with a known-existing email → distinct 400 message.
**Fix:** Return a generic success/"check your email" response and move existence handling into a verification email, or accept the tradeoff explicitly.

### S10 — bcrypt 72-byte truncation; no password max length — LOW
**Where:** `core/security.py:19–24` (`bcrypt.hashpw`/`checkpw`) and `models/schemas.py` (`RegisterRequest.password: str = Field(min_length=8)` — no `max_length`).
**Impact:** bcrypt silently ignores bytes past 72, so very long passwords have a smaller effective space than the user believes. Also, no max length lets a client send a multi-MB string into the request body.
**Verification check:** Schema has `min_length` but no `max_length`; bcrypt truncation is inherent.
**Fix:** Add `max_length` (e.g. 128) on the password field. If you want to support the full password length, pre-hash as `base64(sha256(password))` before bcrypt — use base64, **not** raw SHA-256 digest bytes, because raw digest bytes can contain a null which bcrypt truncates on (reintroducing the bug). Simplest safe option is just the `max_length` cap.

### S11 — FastAPI interactive docs + OpenAPI schema exposed — LOW/INFO
**Where:** `main.py:92` — `FastAPI(...)` with no `docs_url=None` / `redoc_url=None` / `openapi_url=None`.
**Impact:** `/docs`, `/redoc`, `/openapi.json` are publicly reachable, disclosing the full API surface. Usually acceptable; some prefer it off in production.
**Verification check:** `GET /docs` and `GET /openapi.json` return 200 in the deployed environment.
**Fix:** If undesired, gate behind an env flag and disable in production.

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

### S17 — `POST /phrases/seed` triggerable by any authenticated user — LOW/INFO
**Where:** `routers/phrases.py:32` — `/phrases/seed` is gated by `get_current_user` (good, not public) but **not** by an admin check (`is_admin`).
**Impact:** Any logged-in user can trigger a global phrase-table re-seed (shared catalog write + file/DB work). Impact is low: the seed is idempotent (`ON CONFLICT DO NOTHING`) and it's the same work startup already does. Listed for completeness — administrative/shared-resource operations should generally be admin-gated.
**Verification check:** `rg -n 'is_admin' lexy-app/backend/routers/phrases.py` → empty.
**Fix:** Gate `/phrases/seed` behind an `is_admin` check (the flag already exists on `current_user`), or remove the endpoint and rely on startup seeding.

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
- **All 21 routers were swept for auth (2026-05-24).** Every handler is covered by `get_current_user` (router-level or per-handler) **except** the documented public ones — see the unauthenticated-surface list below. `search.py` is auth-gated at the router level; `playlists/generate` is auth-gated and is DB-only (no LLM, so it correctly does not need `rate_limit_llm`); `phrases/seed` is auth-gated (see S17 for the admin-gate gap). `POST /sentences/match` is now auth-gated too (S16 final fix), so **every** API handler requires a bearer token.

> **Doc-drift note:** `CLAUDE.md` §7 calls `/api/search`, `/api/suggest`, `/api/video-sentences`, `/api/word-forms`, `/api/languages`, `/api/categories` "public legacy endpoints." They are actually auth-gated at the router level (`routers/search.py:20`, `APIRouter(dependencies=[Depends(get_current_user)])`). No data leak — but the §7 label is stale and should not be trusted when reasoning about the public attack surface.
>
> **The genuinely unauthenticated surface is:** the static file mount (`main.py:142`), `POST /api/v1/errors/client` (per-IP throttled — S6 resolved), and the FastAPI docs `/docs` + `/openapi.json` (S11, still open). `POST /sentences/match` is no longer public (auth-gated — S16 final fix). Everything else requires a valid bearer token.

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

---

## Changelog

- **2026-05-24** — Initial audit. 17 open findings (S1–S17), verified-strengths baseline recorded. Swept all 21 routers for auth; only `POST /sentences/match` (S16) is unauthenticated.
- **2026-05-24** — **S1 (auth throttling)** and **S5 (HTTP security headers)** resolved. 17 new tests (`test_auth_throttle.py`, `test_security_headers.py`); added env vars `ENABLE_HSTS`, `TRUST_PROXY_HEADERS`. 15 findings remain open (S2–S4, S6–S17).
- **2026-05-24** — **S6 (crash-report throttle)** and **S16 (sentence-match throttle + input cap)** resolved via the same per-IP `check_window`. 6 new tests (`test_client_errors.py` +2, `test_matcher_limits.py` +4). 13 findings remain open (S2–S4, S7–S15, S17). S16 ships public (throttled+capped); auth-gating is a now-unblocked follow-up (#39 has landed).
- **2026-05-24** — **S16 final fix:** `POST /sentences/match` auth-gated (`Depends(get_current_user)`); public per-IP throttle on it dropped (input cap kept as defence-in-depth). `test_matcher.py` gains auth tests (403 unauth / 200 auth / 422 over-length); obsolete `test_matcher_limits.py` deleted. The now-unused `rate_limit_sentence_match` helper + `SENTENCE_MATCH_*` constants were removed in a follow-up cleanup commit. Every API handler now requires a bearer token.
