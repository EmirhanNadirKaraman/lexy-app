# Security architecture decisions

Companion to [`docs/SECURITY.md`](./SECURITY.md). That file tracks findings;
this one records the **decisions** for the remaining open findings that are
architecture/product choices rather than hardening tweaks — **S2, S9** (signup
hardening), **S3** (token revocation), **S12** (shared rate limiter).

**Status (2026-05-24): implementation paused on purpose.** Every small/concrete
security item is shipped (S1, S4, S5, S6, S7, S10, S11, S15, S16, S17). The three
left each force a product or infra decision and an external dependency (an email
provider, Redis) — starting code now risks building the wrong system. This doc
picks the path and the order so the next security PR is pick-up-and-go.

> **Do not implement from this doc without re-confirming the choice.** It reflects
> the deploy topology and threat model as of 2026-05-24 (notably: single-process
> deploy — see below). Re-read before acting.

---

## 0. Pre-launch checklist — do this FIRST (config, ~an hour, no architecture)

Most of the pre-launch security posture is **already built** and just needs to be
*turned on in the production environment*. None of this is the architecture work
below; it's the deploy-gate sweep (ROADMAP T2.3) plus the interim S2 guard.

| Item | Action | Why |
|---|---|---|
| **Signup** | Set `REGISTRATION_CODE=<secret>` (invite-only) **or** ship §1 email verification | Open signup → unmetered LLM cost (S2). Invite-only is the zero-code interim. |
| **DB TLS** | Set `DB_SSL_MODE=require` (or `verify-full`) for a remote/managed DB | S4 — control exists; prod must opt in. |
| **API docs** | Leave `ENABLE_DOCS` unset/false in prod | S11 — don't publish the schema. |
| **HSTS** | `ENABLE_HSTS=true` once served over TLS | S5. |
| **CORS** | `CORS_ORIGINS=https://<your-app>` (+ Capacitor origins if iOS) | S5/#11 — don't ship the localhost default. |
| **JWT secret** | Generate a fresh `SECRET_KEY` (`python -c "import secrets;print(secrets.token_hex(32))"`); never reuse the `.env.example` value | App refuses to boot without one (good); a shared/example secret forges tokens. |
| **Proxy** | `TRUST_PROXY_HEADERS=true` only behind a trusted proxy that sets `X-Forwarded-For` | S1 — otherwise the per-IP throttle is spoofable. |

This checklist closes the *operational* exposure for a small launch. The items
below are the *structural* fixes for an open, at-scale product.

---

## Threat-model context: the deploy is single-process today

`Procfile` runs **one** uvicorn process (`uvicorn backend.main:app`, no
`--workers N`, single pod). Consequences:

- **S12 is latent, not exploitable today.** The in-memory rate limiter
  (`services/rate_limiter.py`) and the LLM cache locks (`llm_cache_service`) are
  *correct* on a single process — there are no other workers to bypass them.
  **S12 becomes real the moment the deploy scales horizontally** (`--workers N`,
  or multiple pods/dynos). It's a precondition tied to a future scaling decision,
  not a calendar date.
- JWT is stateless (HS256, **7-day** expiry, `SECRET_KEY` required at boot). A
  leaked token is valid for up to 7 days with no way to revoke it (S3).

---

## 1. Signup hardening — S2 (open-signup LLM abuse) + S9 (registration enumeration)

**Shipped already:** generic registration-failure message (S9 enumeration leak
closed at the message level); optional `REGISTRATION_CODE` invite gate (S2 interim).
**Residual:** with open signup (no code set), anyone can create accounts and spend
the per-user LLM budget; the 201-vs-400 status is still weakly inferable without
email verification.

**Options**
| | Path | Effort | UX | Notes |
|---|---|---|---|---|
| a | Email verification **required before login** | HIGH | Hard "check your email" cliff at signup | Strongest; worst conversion. |
| **b** | **Login allowed, but LLM/chat endpoints 403 until verified** | HIGH | Soft — user gets in, only the abuse vector is gated | **Recommended.** Gates the exact thing S2 is about (LLM cost) without a signup cliff. |
| c | Invite-only via `REGISTRATION_CODE` only | **DONE** | Fine for closed beta | Already shipped; the zero-code interim. Doesn't scale to open signup. |

**Provider:** Resend (simplest API, generous free tier) or Postmark (best
deliverability) for a small app; AWS SES if already on AWS (cheapest at volume,
more setup). Decision can wait until §1 is actually scheduled.

**Recommended MVP:** if launching **closed** → (c), already done, do nothing. If
launching **open** → (b): add a `users.email_verified` flag + a verification-token
table + send-on-register, and gate the LLM-backed routes (the 10 already behind
`rate_limit_llm`) on `email_verified`. Reuses the existing dependency-injection
seam; no auth-model change.

---

## 2. Token revocation / session model — S3

**Residual:** a stolen/leaked JWT works until its 7-day expiry; logout, password
change, and account deletion don't invalidate outstanding tokens.

**Options**
| | Path | Per-request cost | Blast radius | Revocation granularity |
|---|---|---|---|---|
| **a** | **`users.token_version` column in the JWT claim** | **Zero extra** (see below) | Low | All of a user's tokens (logout-all) |
| b | Access + refresh tokens (short access, rotating refresh) | One refresh round-trip periodically | High (frontend rotation + storage) | Per-session |
| c | Redis/DB denylist keyed by token `jti` | One lookup per request | Medium | Per-token |

**Recommended MVP: (a) `token_version`.** The decisive cost argument:
`core/deps.get_current_user` **already** does `SELECT ... FROM users WHERE
user_id = $1` on every authenticated request (for `is_admin`). Adding
`token_version` to that SELECT and comparing it to a `ver` claim in the JWT costs
**zero additional queries**. Bump `token_version` on logout-all / password change
/ account delete → every prior token for that user fails the next request.
- **Implementation cost is genuinely small:** one migration
  (`ALTER TABLE users ADD COLUMN token_version INT NOT NULL DEFAULT 0;`), put
  `ver` in `create_access_token`, compare in `get_current_user`, bump where
  needed. Migrations are append-only here, so this is one new file.
- **What it does NOT do (be honest):** it revokes *all* of a user's tokens, not a
  single specific stolen one. "I think exactly one device is compromised, kill
  just that token" needs per-token `jti` + a denylist (option c). **Most apps
  don't need per-token revocation** — logout-all + password-change-invalidates is
  the 90% case. Revisit (c) only if per-device session management becomes a
  product feature.
- Options (b)/(c) are **considered and deferred**: (b) is a frontend-heavy rewrite
  for granularity we don't need yet; (c) adds a per-request lookup (and wants
  Redis — see §3) for the same reason.

**Blast radius caution:** even (a) touches the auth hot path. Land it behind
strong tests (the existing `test_e2e_learning_loop` + `test_account_deletion` +
new token_version tests); a bug here logs everyone out.

---

## 3. Redis-backed shared rate limiter — S12

**Residual:** `rate_limiter` + cache locks are per-process; multiple workers each
keep their own counters, so the effective limit multiplies by worker count.

- **Not needed for the current single-process deploy** (see threat-model note) —
  ship it **before scaling to `--workers N` or multiple pods**, not before launch.
- The call sites are already shaped for a backend swap (`check_and_record` /
  `get_or_compute` docstrings note the Redis path), so the lift is **infra +
  wiring + tests**, not a redesign.
- **Bonus:** a Redis instance also backs the S3 denylist (option 2c) and the LLM
  cache locks cleanly — so if Redis is stood up for scale, it unblocks three
  things at once.
- **Local-dev fallback:** keep the in-memory backend as default; select Redis via
  an env var (e.g. `REDIS_URL`) so local dev and the single-process deploy need no
  Redis. Same opt-in pattern as `DB_SSL_MODE` / `ENABLE_DOCS`.

---

## 4. Recommendation

**Before public launch** — config + (if open signup) one feature:
1. The §0 checklist (invite-code or email, `DB_SSL_MODE=require`, `ENABLE_DOCS`
   off, fresh `SECRET_KEY`, CORS, HSTS, proxy flag).
2. If signup is **open** (not invite-only): ship §1 option (b) — verify-to-unlock
   the LLM routes.

**Before scaling horizontally** (the first `--workers N` / multi-pod deploy):
3. §3 Redis-backed limiter (S12) — and stand up the Redis the denylist can reuse.

**When a real session-management need appears** (or as a deliberate security bump):
4. §2 option (a) `token_version` revocation (S3) — cheapest revocation that
   actually revokes; pairs naturally with the Redis from step 3 if you later want
   per-token granularity.

**Do not implement until the infra exists:**
- Redis limiter / denylist — no Redis provisioned today.
- Email verification flow — no email provider chosen today.

**Order if continuing security work now:** §1 (b) email verify *(only if launching
open)* → §3 Redis *(only when scaling)* → §2 token_version. If none of those
triggers (closed beta, single process) apply yet, **the right move is to stay
paused** and return to product work.

---

## 5. Remaining open risks while paused

- **S2/S9:** open signup (when `REGISTRATION_CODE` unset) is abusable for LLM cost;
  enumeration weakly inferable. *Mitigated* by keeping invite-only until §1 ships.
- **S3:** leaked token valid up to 7 days, no revocation. *Mitigated* by the
  password-re-auth gate already on account deletion (S3 partial) — the one
  destructive action a stolen token could do is already re-gated.
- **S12:** harmless on single-process; **must not** scale horizontally without
  shipping §3 first, or the rate limits silently weaken.
- **S13/S14 (INFO):** f-string SQL in one migration (constant, not user input);
  LLM prompt-injection from user content (inherent; guardrails only). Neither is a
  vuln today.
