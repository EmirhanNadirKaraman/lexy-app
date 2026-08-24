# SECURITY.md

Living security tracker for this repo. Not a vulnerability-disclosure policy — this is the working list of what's wrong, what's right, and how to re-check both.

**This file is paired with `CLAUDE.md` §14.** That section says *when* to read and update this file. This file holds the *findings*.

---

## How to use this file

- **Every open finding carries four things:** `file:line`, a severity, a one-line **verification check** (how to confirm it's still true / still fixed), and a suggested fix. A finding without a verification check is a dead finding — it rots into a claim nobody re-tests.
- **When a finding is fixed:** move it to *Resolved* with the date and the commit/PR. **Do not delete it** — the history is how we avoid regressions and how we remember why the code looks the way it does.
- **When you find something new:** append to *Open findings* with the same four fields. Add a row to the summary table.
- **The "Verified strengths" section is load-bearing.** Those are controls we depend on. If a PR weakens one (e.g. widens the settings whitelist, drops an ownership filter), that's a regression, not a refactor.
- **Findings, resolutions and `Changelog` bullets stay exactly where they are — but a task's own one-line change note goes at the very END of this file**, below the CHANGE-NOTES marker that opens the *Change notes* section (added 2026-08-23, notes-03). That section is the only region the loop's merge sweep may combine without a human; everything above it still conflicts and still needs one. Nothing may follow the last note line, so do not append a `Changelog` bullet or a new `##` section after it — `test_docs_merge.py::test_every_shipped_tracker_ends_with_an_append_only_section` fails if you do.

Severity legend: **HIGH** (exploitable now, real impact) · **MEDIUM** (exploitable under conditions, or a strong amplifier) · **LOW** (hardening / defense-in-depth) · **INFO** (note / good-practice gap, not a vuln today).

Last full sweep: **2026-05-24** (manual read of backend auth, routers, services, scraper, frontend token/XSS surface).

---

## Open findings — summary

> **S1, S4, S5, S6, S7, S11, S15, S16, S17 are RESOLVED (2026-05-24)** — see the *Resolved findings* section. They are kept out of the open table below.
>
> **S2, S3, S9, S12 are architecture/infra decisions** — implementation paused 2026-05-24. The scoped options, the `token_version`/Redis/email tradeoffs, the **pre-launch config checklist**, and the recommended order live in [`docs/SECURITY_ARCHITECTURE_DECISIONS.md`](./SECURITY_ARCHITECTURE_DECISIONS.md). Read that before starting any of them.

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
| S18 | LOW | Self-hosted model server has no auth; `LLM_BASE_URL` egress is operator-controlled (opt-in, unset by default) | `services/llm_provider.py` |
| S22 | INFO | `commit_adopted` is sound but has no production call site (tracked, not a vulnerability) | `autoloop/git_gateway.py`, `autoloop/manifest.py` |
| S24 | HIGH | Write-capable agent isolation is DETECTED (checkout snapshot diff), not PREVENTED (no OS-level sandbox); `.git/` internals not covered. Amended 2026-08-16: derived bytecode (`__pycache__/*.pyc` whose sibling `.py` is a regular file in the same snapshot side(s) — never `.pyo`, never a symlinked source) is exempt from the snapshot diff after three false loop-fatal parks — a forged cache entry whose header matches its source is the new, named residual | `autoloop/escape_detector.py`, `autoloop/orchestrator.py` |
| S28 | MEDIUM | The dashboard's unauthenticated localhost POST can now queue a task CREATION request, which carries `approved_paths` — so its blast radius is a future agent's write scope, not just a priority number | `autoloop/dashboard.py` |
| S30 | MEDIUM | The inbox vocabulary grew from create+priority to six mutation kinds, so a queued request can now rewrite an EXISTING task's `approved_paths` and `description` — falsifying S28's "it creates, never widens" bound. Guarded: nothing `in_progress`/`completed`/`retired` is editable, mutations share creation's validators, `block`/`unblock` cannot launder a loop-raised quarantine, `retire` is excluded, and no dashboard endpoint reaches any of it | `autoloop/inbox.py`, `autoloop/tasks.py` |
| S32 | MEDIUM | The dashboard's localhost POST now writes `.autoloop/tasks.json` IMMEDIATELY (one field: `priority`), so the loop is no longer the registry's only writer, and `escape_detector.diff_snapshots` gained an `exempt` predicate for that file. Bounded: the exemption needs BOTH an attestation chain in a ledger outside the checkout AND a byte-level check that nothing but `priority` moved, so an agent editing that file still parks the loop loop-fatal | `autoloop/dashboard.py`, `autoloop/tasks.py`, `autoloop/escape_detector.py`, `autoloop/orchestrator.py` |
| S33 | LOW | Every request's CONTEXT block now renders two task briefs — the READY task's full description and effective scope, and the under-review task's stored decomposition — so text this package did not author sits in the block that carries the review-integrity stamp. Bounded: briefs are appended strictly after every stamp line (pinned by a test whose description contains a forged one), and `verify_review` compares an approval's echo against what was recorded, so a planted stamp yields a refused approval | `autoloop/context.py`, `autoloop/contract.py` |
| S35 | INFO | The merge sweep now auto-resolves ONE conflict shape without a human (2026-08-19, docs-01): two branches each appending change-note lines to the terminal append-only section of a tracker. Scope widened 2026-08-23 (notes-03) from `docs/SUMMARY.md` / `docs/TESTS.md` to those plus `docs/SECURITY.md` / `docs/COMMON_ERRORS.md`, each only after it was given such a section. Bounded: four literal paths, each side's section must extend the merge base byte-for-byte, a conflict anywhere else in the file or the merge refuses the whole merge, and every decision is in the transcript. Replaces S34 (`merge=union`), which disabled conflict detection for the whole file | `autoloop/note_merge.py`, `autoloop/auto_merge.py`, `autoloop/git_gateway.py` |
| S36 | INFO | `profile` (prof-01, 2026-08-20) is the first command whose whole job is to read `transcript.jsonl`, which holds complete review packets (`request_submitted.data.prompt`) and complete reviewer replies (`response_received.data.raw`). Bounded structurally by a read/render split: `build_profile` reduces the read to counts plus per-stage floats and static stage labels, and `render_profile` receives only that, so the layer that writes to stdout holds no record at all — do not add a flag that prints one. `--transcript FILE` changes only WHICH file is read, and the bound is input-independent | `autoloop/cli.py`, `autoloop/transcript.py` |
| S37 | INFO | The reviewer transport gained a LIVE agent session (`codex_app_server`, 2026-08-22, codex-01): one long-lived `codex app-server` child instead of a process per turn, and it can ask this client to approve a command or a patch. Bounded by this client's REPLIES — every approval is answered `{"decision": "abort"}`, every other server→client request gets a JSON-RPC error (never silence, which would wedge the turn), argv is a list with no shell and no model text on it, `cwd` stays outside the checkout and stderr is `DEVNULL`. Explicitly NOT a sandbox claim: no preset is selected or enforced | `autoloop/codex/app_server.py`, `autoloop/codex/app_server_conversation.py`, `autoloop/conversation.py` |
| S38 | MEDIUM | The inbox gained `urgent` (preempt-01, 2026-08-22) — the first request kind whose effect is on the LOOP: it ends the round in flight at the next safe boundary and MOVES that task's worker repo and execution record to quarantine, so S30's "nothing in flight can be edited" no longer covers the whole vocabulary. Bounded: the target must be a dispatchable READY task with a non-empty scope, a non-blank reason is required, ONE live pin at a time, it acts only at `_at_round_boundary` (the same predicate the self-upgrade restart uses) so no review packet is stranded, the displaced task moves through the one release path, nothing is deleted (a retirement that fails names what survived, and pairs it back with its record when that worker is resumable), and no packet, stamp, approval or push is touched — the one added gate only DENIES an `implement`/`revise` of another task or a fresh `audit` while the pin is live | `autoloop/inbox.py`, `autoloop/tasks.py`, `autoloop/orchestrator.py`, `autoloop/cli.py` |
| S39 | INFO | The Codex CLI reviewer's failure classifier no longer treats the loop's OWN prompt as evidence (quota-01, 2026-08-23). `codex exec` echoes the whole prompt onto stderr, so a packet quoting a line number made any non-zero exit read as a spent allowance and parked the loop loop-fatal, twice, on 2026-08-22 — content the loop SENT could stop the loop. A marker now counts only when it does not occur in the prompt, which is a required argument. The same change adds a bounded, echo-stripped `stdout_tail` to `codex_invocation_failed`, narrowly amending `failure_digest`'s "never stdout" promise; argv and the environment are still never recorded, and the reviewer's reply already reaches that file in full (see S36) | `autoloop/codex/quota.py`, `autoloop/codex/conversation.py`, `autoloop/conversation.py` |
| S40 | INFO | The dashboard now replaces its own process image (loop-03, 2026-08-23) when `.autoloop/pending_upgrade.json` — a file inside the observed checkout — says the checkout moved under it. Bounded: that record decides only WHETHER, never WHAT. The argv is `sys.executable` + a module name derived from `__main__.__spec__` + `sys.argv[1:]`, with no record field interpolated and an underivable launch shape refused outright; `repo_root` is compared, never acted on; a preflight subprocess proves the tree imports first; the record is never written back (it is the loop's one-shot, and writing into `.autoloop/` parks the loop on its escape detector); and the exec happens only between connections, with the armed flag cleared in a `finally` so a refused replacement cannot silence the port | `autoloop/dashboard.py` |
| S29 | LOW | `merge` joined the git whitelist (first subcommand that moves the checkout's own head) and `push_exact` now publishes the BASE branch — deliberate, shape-checked to a literal 40-hex, default off. Amended 2026-08-15: the same head may now move at STARTUP and from `merge-backlog`, via the same gate, flag and primitives — and, since that head can be left moved-but-unpushed by a failed verification or a refused push with no undo primitive available, startup now probes the checkout and refuses to run the loop on one it did not finish integrating | `autoloop/policy.py`, `autoloop/git_gateway.py`, `autoloop/auto_merge.py`, `autoloop/merge_sweep.py`, `autoloop/cli.py` |

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

---

### S18 — Self-hosted model server: no auth, operator-controlled egress — LOW — OPEN (opt-in)

**file:line** — `services/llm_provider.py` (`OpenAICompatibleProvider.__init__`, `.structured`)

**severity** — LOW. Inert by default: the provider is only constructed when an
operator sets `LLM_PROVIDER=openai_compatible`. No user input selects it and no
route exposes it.

**What it is.** When enabled, the backend POSTs prompt content — which includes
learner-authored chat messages and book text — to whatever host `LLM_BASE_URL`
names. Two properties worth stating plainly rather than discovering later:

1. **The target server typically has no authentication.** Ollama and
   llama.cpp's `llama-server` bind without auth by default. Anyone who can
   reach the port can use the model and read whatever is sent to it.
2. **`LLM_BASE_URL` is an operator-set egress destination.** It is read from
   env at import, never from a request, so it is not user-controllable — but it
   does mean a typo'd or hostile value silently redirects prompt content.
   Treat it like a database URL.

**Controls already in place.** `LLM_API_KEY` is never included in an error
message; `_redact()` additionally strips `user:pass@` userinfo from the base URL
before it reaches an `LLMProviderError`, because those strings land in logs and
client-error reports. Both are regression-tested
(`test_errors_never_contain_the_api_key`,
`test_errors_redact_credentials_embedded_in_the_base_url`).

**verification check**
```bash
# Provider is opt-in and defaults to Anthropic:
rg -n 'LLM_PROVIDER' lexy-app/backend/services/llm_provider.py
# API key never interpolated into an error:
rg -n '_api_key' lexy-app/backend/services/llm_provider.py   # expect: header build only
```

**suggested fix** — Deployment guidance rather than code: keep the model server
on Tailscale or another authenticated tunnel, never on a public interface, and
prefer `https://` when the tunnel supports it. If a shared/multi-tenant model
server is ever used, revisit — prompt content would then leave the trust
boundary.

### S22 — `commit_adopted` is sound but has no production call site — INFO — OPEN (tracked, not a vulnerability)

**What:** `GitGateway.commit_adopted` (the immutable-tree / `commit-tree` +
`update-ref` CAS sequence that closed S21 — see *Resolved findings*) is
correct and still directly exercised by `test_git_gateway.py` /
`test_manifest.py`, but as of the 2026-07-30 S21 retirement it has **no
production caller anywhere**: `orchestrator.py`'s `_dispatch_git` (its only
caller) was removed along with the legacy authorize-then-produce commit path,
and `ChangeManifest.adopt` (the only thing that would produce an adopted
manifest to commit) already had no production caller before that. Tracked
here so "why does this exist" has an answer, and so a future PR does not
silently wire it back into `orchestrator.py` without a fresh design review —
produce-then-review commits automatically now; there is no remaining
authorize-then-produce step for `commit_adopted` to gate.
**file:line** — `autoloop/git_gateway.py` `commit_adopted()`; `autoloop/manifest.py` (module-level, `ChangeManifest.adopt`).
**Verification check:**
```bash
# Expect: no match — the only historical caller is gone
rg -n '\.commit_adopted\(' autoloop --glob '!**/tests/**'
rg -n 'ChangeManifest\.adopt\(' autoloop --glob '!**/tests/**'
# Expect: both modules' own docstrings say so
rg -n 'NON-PRODUCTION' autoloop/git_gateway.py autoloop/manifest.py
```
**Suggested fix:** none needed — this is a tracking note, not a defect. If a
future produce-then-review variant needs a pre-verified-content commit path
again, reuse `commit_adopted` rather than reintroducing a second
implementation.

### S24 — Write-capable agent isolation is DETECTED, not PREVENTED — HIGH — OPEN (mitigated 2026-07-31, not closed)

**What:** M2's worker isolation (`worker_env.py`, resolved-but-undocumented
2026-07-30 — see the note above the summary table) is entirely git
CONFIGURATION isolation (no remote, no hooks, scrubbed credential env). It
says nothing about the OS-level filesystem confinement of the agent
subprocess itself. `ClaudeCliRunner`'s argv (`audit/agents.py`) carries only
`--output-format`/`--permission-mode`/`--allowedTools`/`--disallowedTools`
— no `--add-dir`, no path jail, no container. "Stay inside your worker
repo" is a sentence in the agent's prompt
(`implement_executor._agent_prompt`), not something enforced structurally.
A write-capable agent that ignored it, or that has a bug, could write
anywhere the orchestrator process itself can write — including the primary
checkout.

**Mitigated 2026-07-31, not closed:** `escape_detector.py` now takes a
deterministic filesystem snapshot (content sha256 / symlink target /
executable bit, over tracked + untracked + ignored paths) of the PRIMARY
checkout immediately before and immediately after every write-capable
`TaskExecutor.execute()` call, and any creation/deletion/content/symlink/
mode change outside the worker repo is a LOOP-FATAL park
(`checkout_escape_detected`) — see `docs/AUTOLOOP.md` §4e for the design.
This is DETECTION, checked before the resulting work is committed or
reviewed — not prevention. Residual, explicitly still open:

  * the agent subprocess is not sandboxed at the OS level; nothing stops the
    write from happening, only from being silently trusted afterward. A
    write that is made and reverted before the "after" snapshot runs is not
    caught by this or by anything else in this codebase.
  * the validation-environment boundary (S27, resolved 2026-07-31,
    `docs/AUTOLOOP.md` §4g) does NOT close this. It removes the six
    validation credentials from the agent's own environment, which is how
    every library and config loader in this repository finds credentials —
    but an agent that can already run arbitrary code can still read the
    credential file off disk. Process separation, not confinement.
  * the detector does not inspect the primary checkout's `.git/` internals
    (hooks, config, refs) — scoped to the three `git status` categories
    (tracked/untracked/ignored) over the WORKING TREE only. A write to
    `.git/hooks/pre-commit` or `.git/config` in the PRIMARY checkout (as
    opposed to the task's own worker repo, which `verify_worker_isolation`
    already refuses any hook in) would compromise every future commit in the
    checkout and is not covered.
  * the diff has TWO silences now, and both are argued where they live rather
    than here: derived bytecode (the 2026-08-16 amendment above,
    `is_derived_bytecode`) and the caller-injected `exempt` predicate the
    orchestrator supplies for an operator's immediate `tasks.json` priority
    edit (**S32**). Neither is a path exclusion — the first requires the `.py`
    source to be watched byte for byte in the same snapshot, the second
    requires an attested chain of COMPLETED mutations outside the checkout,
    ending at exactly the observed after-state, AND a byte-level check that
    nothing but `priority` moved.

**file:line** — `autoloop/escape_detector.py` (new); wired in
`autoloop/orchestrator.py`'s `_execute_with_escape_detection` /
`_prepare_write_capable_worker`; agent confinement gap itself in
`autoloop/audit/agents.py` (`ClaudeCliRunner.build_argv`) and
`autoloop/implement_executor.py` (`WRITE_ALLOWED_TOOLS`,
`_agent_prompt`).

**Verification check:**
```bash
# Detector exists and is wired only into the non-audit dispatch path:
rg -n 'checkout_escape_detected' autoloop/orchestrator.py
rg -n 'is_audit' autoloop/orchestrator.py | rg -n 'escape_detection|_prepare_write_capable_worker'
# Confinement is still prompt-only, not argv-enforced (expected — this is
# the residual, not a regression):
rg -n '\-\-add-dir|sandbox|chroot|container' autoloop/audit/agents.py autoloop/implement_executor.py   # expect: no match
pytest autoloop/tests/test_m1_hardening.py -k escape_detector -q
```

**Suggested fix:** real OS-level sandboxing for the write-capable agent
subprocess (a container, a chroot, a filesystem namespace with only the
worker repo bind-mounted writable) is the only way to close this fully;
until then, treat the escape detector as the last line of defense, not the
primary one, and keep `docs/AUTOLOOP.md` honest about the distinction.

**Addendum, same day (two rounds):** the `checkout_escape_detected` park
this section's detector emits was, for a window within this same pass,
itself clearable by arbitrary operator text — it had no entry in
`cli._RESOLUTION_PRECONDITIONS` at all (see S26's addendum, round 1). A
first fix gave it a precondition that re-checked `GitGateway.is_dirty()` —
still wrong, and caught in a SECOND round of review: the escape detector's
snapshot deliberately covers untracked AND ignored paths, a strict superset
of what `is_dirty()` can see, so an escape touching only an ignored path
(the canonical case this detector exists to catch — Autoloop's own state
directory is gitignored, so an agent tampering with `.autoloop/state.json`
to cover its tracks leaves the working tree clean throughout) would have
cleared on a recheck that verified nothing. Fixed for real: this code now
refuses UNCONDITIONALLY, regardless of checkout state — the reported paths
need a human to read them, not an automated recheck to wave through. See
S26's addendum for detail. **Operability, checked (not just asserted):**
refusing unconditionally means this specific blocker record can never be
marked resolved via `answer` — but the operator is not stuck. `reset`
(`cli._cmd_reset`) archives `state.json` via `os.replace` (moves it aside
entirely, does not touch `blockers_dir`), so the NEXT `run` sees no
persisted state at all and starts a genuinely fresh session — the loop
resumes ordinary work immediately, unblocked. The stale
`checkout_escape_detected` `Blocker` record is not deleted or auto-resolved
by `reset` — it stays open by design, on disk, and reappears (a) in
`blockers`/`blockers --all` listings and (b) in `_run_continuous`'s
exhaustion path (prints every open blocker and exits 0) whenever the loop
next has nothing ready and nothing new to audit. That is the intended
behaviour for a security-shaped detection, not a wedge: a standing
reminder that something was never actually resolved, surfaced only when
there is otherwise nothing productive to report, rather than a hard block
on all future work. Does not change anything above: the OS-sandbox
gap is unaffected either way.

**Addendum, 2026-08-16 (esc-01) — one class narrowed out of the snapshot,
and the residual that creates.** The unconditional refusal above is what
made three FALSE parks expensive: on 2026-08-15/16, `checkout_escape_
detected` fired three times on CPython bytecode caches inside the primary
checkout, none of them caused by an agent (a dashboard restart importing
`autoloop.dashboard`; two `python3 -m autoloop health --json` polls after the
loop merged a source change). `__pycache__/` is gitignored and the detector
covers ignored paths on purpose, so an out-of-band import is
indistinguishable from tampering. Each cost a `reset --yes` and the in-flight
round; one stranded five tasks `in_progress`.

`escape_detector.is_derived_bytecode` now exempts exactly one class from
`diff_snapshots`: a `.pyc` directly inside a `__pycache__/` directory whose
sibling `.py` source is present **as a regular file** in every snapshot side
on which the cache entry itself exists. The distinction being drawn is
AUTHORED vs DERIVED — `state.json` and a blocker record hold the only copy of
a claim, while a cache entry is written from a source this same snapshot
still tracks byte for byte. That last clause is enforced, not assumed, which
is why every clause of the rule is narrow:

* **`.pyc` only, never `.pyo`.** No supported CPython writes a `.pyo` (PEP 488
  folded the optimisation level into a `.pyc`'s `.opt-N` infix), so
  `__pycache__/mod.cpython-312.pyo` is not a compile product — it is an
  authored file borrowing a derived-looking extension. Accepting the whole
  `py[co]` family would have granted a silent write beside every sourced
  module in the tree.
* **The tag must be one an interpreter really emits.** Accepted:
  `cpython-<digits>[t]` (the CPython family shape; `t` is the free-threaded ABI
  flag) and this runtime's own `sys.implementation.cache_tag` as a literal.
  Everything else — `mod.attacker.pyc`, `mod.cpython312.pyc`, pytest's
  assertion-rewriter name — is reported. The first version of the rule accepted
  any dot-free tag, which satisfied every other clause beside a live source and
  so amounted to one silent write per sourced module in the tree; that is the
  `.pyo` hole again, one level down the name. The family shape carries the
  weight, not the runtime tag: the three incidents were written by a DIFFERENT
  process than the loop (a dashboard restart, `health --json` polls), which
  need not run the loop's interpreter or version, so keying on
  `sys.implementation.cache_tag` alone would recreate the parks; the runtime
  tag is only a backstop for a build (debug, non-CPython) the family shape does
  not anticipate. A cache file from a genuine but foreign interpreter is
  reported — a readable park, not a silent write.
* **The source must be `kind="file"`, per side.** `snapshot_checkout` records
  a symlink as a target STRING and never hashes it, so a `.py` symlink's bytes
  could change with the snapshot unmoved — it vouches for nothing, and a cache
  entry beside it is not exempt. Checking each side separately (rather than
  the union of both key sets) also refuses a cache entry created in the same
  window that deletes its source.

Nothing else was excluded: a `.pyc` outside `__pycache__` (the pre-PEP-3147
layout, which imports with no source beside it), a `.pyc` wearing a tag no
interpreter emits, an orphan cache entry with no sibling `.py`, and any
symlink or directory appearing at a cache path all still park. `.so`/`.pyd` are NOT covered and must never be added — those are
authored build outputs.

**Two things this changes elsewhere, stated rather than left to be
discovered.** (1) The same rule reaches `diff_worker_tree`, the validation
mutation guard (S27), because it delegates to `diff_snapshots` — a
validation `pytest` run's own bytecode no longer reads as "validation
MUTATED the worker tree"; that message now says "beyond its own bytecode
cache". (2) A NEW residual: "the next import regenerates it" holds only
while the cached header is stale. An agent that writes a `.pyc` can also
write the `(mtime, size)` / PEP 552 source-hash header that makes the
interpreter accept it, so a forged cache entry whose header matches its
source would be loaded and is NOT reported. Header verification was
considered and rejected: it costs an attacker one `os.stat` to defeat, while
failing closed on an unreadable or foreign-magic header would manufacture
exactly the spurious loop-fatal parks this exists to stop. The residual
needs the OS-level sandbox this finding already tracks, not a wider
snapshot, and it is strictly smaller than the `.git/hooks/` gap above.
`PYTHONDONTWRITEBYTECODE`/`-B` is REPORTED (esc-01 brief) as already applied
to the loop, supervisor, deadman and dashboard wrappers, which live outside
this checkout — nothing in this repository sets it, so do not record it here
as a verified control. It would in any case not be the fix: it depends on
every future caller remembering, which is the failure that produced all
three incidents.

**Verification check:**
```bash
# The exemption exists, is keyed on the cache DIRECTORY, and is not widened:
rg -n 'is_derived_bytecode|BYTECODE_CACHE_DIR' autoloop/escape_detector.py
rg -n '\.so|\.pyd' autoloop/escape_detector.py      # expect: only the "never add these" comments
# `.pyc` only — the regex must NOT match a `.pyo` (expect no `py[co]` hit;
# every `.pyo` mention should be prose saying it is deliberately in scope):
rg -n 'py\[co\]|pyo' autoloop/escape_detector.py
# The TAG is constrained to shapes an interpreter emits — expect the family
# pattern plus the runtime literal, and NO bare `[^.]+`/`.+` in the tag slot:
rg -n '_CPYTHON_CACHE_TAG|_CACHE_TAG_ALTERNATIVES|cache_tag' autoloop/escape_detector.py
# The source must be verified as a regular file, per side — not merely present
# in a key set (expect the `state.kind == "file"` check and the `sides` fan-in):
rg -n 'state\.kind != "file"|\*sides' autoloop/escape_detector.py
# The `-B` stopgap is operator-side, not in this checkout — expect NO match in
# CODE (the docs discuss it in prose, which is why this is scoped to `autoloop/`
# and `*.py`), and do not "fix" the docs by claiming the control lives here:
rg -n 'PYTHONDONTWRITEBYTECODE' autoloop -g '*.py'
# A genuine ignored-path escape is still detected, including alongside
# bytecode churn in the same window:
pytest autoloop/tests/test_m1_hardening.py -k 'escape_detector or bytecode' -q
```

### S28 — The dashboard's localhost POST can now queue authorization, not just a priority — MEDIUM — OPEN (bounded, accepted)

**What:** `autoloop/dashboard.py` serves an unauthenticated page on
`127.0.0.1` and, since 2026-08-01, accepts `POST /api/priority`. That endpoint
takes a task id and an integer, and nothing else — the finding it could not
raise was authorization, because a priority request carries no
`approved_paths` (refused at `inbox.TaskInbox.submit`). Since 2026-08-16 it
APPLIES that integer instead of queueing it, and still carries nothing else;
what the timing change costs is tracked separately as **S32**.

`POST /api/task` (2026-08-02) changes that. A creation request carries
`approved_paths`, which is the scope a write-capable agent is later authorized
against, and the loop drains the inbox between steps without asking the
operator again. So **any local process that can reach the port can queue a
task naming paths the operator never typed**, and the next drain merges it.
This is not a browser-origin bug that a header fixes: the header + Origin
checks stop a *cross-origin page* in the operator's browser, not a local
process that speaks HTTP.

What bounds it, stated rather than assumed:
- **Well-formedness, not intent.** `TaskRegistry.add_many` →
  `_validate_approved_path` refuses globs, `..`, absolute and `~` paths, and
  backslashes; `escape_detector.find_symlink_traversal` re-checks symlink
  traversal at dispatch. None of that asks whether the path was *wanted*.
- ~~**It creates, never widens.**~~ **No longer true — see S30.** This bullet
  said `inbox.KINDS` was `("task", "priority")` and that no request kind could
  edit an existing task's `approved_paths`. Since 2026-08-16 one can. Kept
  struck through rather than deleted: it is the assumption the rest of this
  finding's blast-radius argument was sized against.
- **Visible before it runs.** `_pending_inbox` carries the paths and the page
  prints them per queued request, and the loop's drain reports each merge.
- **Same trust boundary as the rest.** Anything that can post here can also
  run `python -m autoloop add-task`, or edit the inbox directory directly.
  The endpoint adds convenience to an existing local-user capability; it does
  not cross a boundary that was previously closed.

**file:line** — `autoloop/dashboard.py` `Handler.do_POST` / `Handler._submit_task`
(routing + creation), `Handler._queue` (the only write), `_pending_inbox`
(the visibility mitigation).
**Severity:** MEDIUM — requires local code execution as the operator, which
already implies broader access; the concrete gain is a *plausible-looking*
scope that a hurried operator may not re-read before the loop drains it.
**Verification check:**
```bash
# Expect: the only writes are inbox submits plus the ONE immediate priority
# write S32 documents (`_task_store(...).apply_priority`) — no other repo or
# state-dir path is written from this file
rg -n 'write_text|mkdir|open\(|subprocess\.run|apply_priority' autoloop/dashboard.py
# Expect: no second path validator here — add_many stays the single authority
rg -n '_validate_approved_path|APPROVED_PATH|glob|fnmatch' autoloop/dashboard.py
# Expect: creation cannot smuggle validation commands or dependencies
rg -n 'TASK_REQUEST_FIELDS' autoloop/dashboard.py
# Expect: the page still posts ONLY /api/priority and /api/task — no endpoint
# reaches the mutation kinds S30 added (no _submit_mutation, no submit_mutation)
rg -n 'submit_mutation|/api/(description|approved|depends|block|unblock)' autoloop/dashboard.py
```
**Suggested fix (if the page ever leaves a single-operator machine):** require
a per-process token printed by `main()` and sent as a header — cheap, and it
distinguishes "the operator's tab" from "a local process". Do NOT fix it by
validating paths inside `dashboard.py`: a second rule set would drift from
`add_many`, which is the failure S25 closed. Binding anything other than
`127.0.0.1` must stay out of the question.

### S30 — An inbox request can now rewrite an existing task's `approved_paths` — MEDIUM — OPEN (bounded, accepted)

**What:** the task inbox vocabulary was `task` (create) + `priority` (a task id
and an integer). Since 2026-08-16 it also carries `description`,
`approved_paths`, `depends_on`, `block` and `unblock`, each mutating an
EXISTING task (`autoloop/inbox.py` `MUTATION_PAYLOAD`, applied by
`apply_requests` through the matching `TaskRegistry` mutator).

That directly falsifies S28's "it creates, never widens" bullet, which is why
this is a new finding rather than an edit to that one. **Anything that can
write a file into the inbox directory can now widen the scope a write-capable
agent is authorized against**, where before it could only propose a new task
carrying a new scope. The inbox is a plain directory beside `workers_root`,
readable and writable by the operator's own user — so the actor is the same
local user S28 already assumes, and what changed is what that actor can
express, not who they are.

Two capabilities are worth naming separately, because neither existed before:

- **Widening in place.** A queued `approved_paths` mutation against a task the
  reviewer has already looked at replaces its scope without the task appearing
  as new anywhere. A creation request at least shows up as a new row.
- **Rewriting instructions.** A `description` mutation rewrites what the
  write-capable agent is told to do, on a task that has already been planned.

What bounds it, stated rather than assumed:
- **Nothing in flight can be touched.** `TaskRegistry._refuse_immutable`
  refuses `description`, `approved_paths` and `depends_on` on an `in_progress`
  task — so a scope cannot be swapped underneath a dispatch that is already
  being judged against it — and on `completed`/`retired` tasks, so a finished
  commit's authorization record cannot be rewritten after the fact.
- **Same validator as creation.** `_validate_approved_paths` /
  `_validate_depends_on` / `_validate_description` are called by BOTH
  `add_many` and the mutators, so a mutation cannot express a scope creation
  would refuse; `escape_detector.find_symlink_traversal` still re-checks
  traversal at dispatch. As with S28 this is well-formedness, not intent.
- **The per-kind shape rule is enforced on the route this finding documents.**
  Hand-writing the JSON file is the only way to queue the five new kinds, and
  `inbox.check_request_shape` is called by BOTH `TaskInbox.submit` and
  `apply_requests`, so that route is gated by the same rule the API is.
  The first cut checked shape at submit only, and `apply_requests` consumed the
  keys it recognised and ignored the rest — so a hand-written `block` carrying
  a stray `approved_paths` applied the hold and dropped the scope rewrite. That
  direction of the failure was silent-ignore rather than over-grant, but it
  made a request do something other than what its author wrote on the one route
  an operator actually has. It is now refused whole: neither field lands, and
  the refusal is that request's line in the drain output rather than an
  aborted batch.
- **Blocking is reversible and cannot launder a quarantine.**
  `operator_block` refuses a task that is already `blocked` and records the
  hold's provenance in `Task.hold_origin` (`tasks.HOLD_ORIGIN_OPERATOR`);
  `operator_unblock` releases ONLY a task carrying that origin. So an inbox
  request can neither overwrite the recorded reason of a real `task_fatal`
  quarantine nor return a quarantined task to `ready_tasks()` with its blocker
  still open and unanswered — those still go through
  `python -m autoloop answer`, which resolves both halves.

  **Provenance is a stored value, not the reason text** — and the first cut of
  this finding shipped the wrong one. It tested
  `blocked_reason.startswith(OPERATOR_HOLD_PREFIX)`, but `blocked_reason` is
  unconstrained free text that ordinary loop-raised quarantines write too
  (`cli._handle_parked_task` passes the park detail straight through), so a
  genuine quarantine whose reason merely BEGAN with those characters was
  releasable from the inbox with its blocker record open — the exact bypass
  this bullet claims is impossible. The field closes it: `block()` clears
  `hold_origin` unconditionally regardless of reason text (and so an idempotent
  re-block cannot inherit one), `unblock()` clears it on release,
  `operator_block` is the only writer and writes it only after the delegate
  returns, and `from_dict` loads a missing or `null` value as `""` — an
  unmarked row reads as a loop quarantine, which is the safe direction for a
  `tasks.json` written before the field existed. `OPERATOR_HOLD_PREFIX` remains
  on the reason as prose for a human reading the row and decides nothing;
  re-introducing a check against it would re-open this hole.
- **`retire` is not in the vocabulary** and must not be added: it is
  written-once with no reverse, so reaching it from here would create a state
  the inbox cannot undo.
- **Visible before it runs.** Every applied mutation is reported in the drain
  output (`task_inbox_drained` in the loop's log, printed by `drain-inbox`),
  and the queued request is a readable JSON file until then.
- **No new route to it.** Nothing in `dashboard.py` or the `add-task` CLI
  submits a mutation kind; today the only way to queue one is to write the
  file, which is the same capability as editing the inbox directory directly.
  **Wiring a dashboard endpoint to these kinds is the change that would make
  this finding materially worse** — it would put scope rewriting behind an
  unauthenticated localhost POST, which is exactly the step S28 flags.

**Known gap in the visibility bound, stated rather than left to be found.** The
dashboard's queued-request line branches on `kind === "priority"` versus
everything else, so a queued mutation of any other kind renders as
`new task <id> (priority 100) may write: nothing — undispatchable`. That is
reachable by the exact route this finding documents as the intended one
(hand-writing the file), not a theoretical case — and it misreads in the
unhelpful direction: a queued `approved_paths` rewrite displays as a harmless
undispatchable new task. `_pending_inbox` already carries the fields, so this
is a renderer fix, not a data one; it is deliberately out of scope for
inbox-02 (registry + vocabulary only) and is the first thing the follow-up
task that adds routes should close. Until then the drain output — which does
report every applied mutation correctly — is the reliable record, and the page
is not.

**file:line** — `autoloop/inbox.py` `MUTATION_PAYLOAD` / `CREATION_FIELDS` /
`check_request_shape` (called by `TaskInbox.submit` AND `apply_requests`) /
`_check_creation` / `_check_mutation` / `_apply_mutation`;
`autoloop/tasks.py` `_refuse_immutable`, `set_approved_paths`,
`set_depends_on`, `operator_block`, `operator_unblock`, `Task.hold_origin`,
`HOLD_ORIGIN_OPERATOR`.
**Severity:** MEDIUM — same actor and same trust boundary as S28 (local write
access as the operator), and the concrete gain is that a widened scope can be
made to look like an untouched, already-reviewed task.
**Verification check:**
```bash
# Expect: exactly the six mutation kinds, and NO 'retire' among them
rg -n 'MUTATION_PAYLOAD|KIND_' autoloop/inbox.py
# Expect: per-kind allowed fields — a creation request is bounded by
# CREATION_FIELDS, which must NOT contain 'reason'
rg -n 'CREATION_FIELDS|MUTATION_ONLY_FIELDS|_check_creation' autoloop/inbox.py
# Expect: the definition plus exactly TWO call sites — one in TaskInbox.submit,
# one in apply_requests — so the hand-written-file route (the only route to the
# five new kinds) is gated by the same rule the API route is
rg -n 'check_request_shape\(' autoloop/inbox.py
# Expect: the strand/terminal guard is on all three content mutators
rg -n '_refuse_immutable' autoloop/tasks.py
# Expect: the inbox reverse gates on hold_origin — `operator_block` the only
# assignment of HOLD_ORIGIN_OPERATOR, `block`/`unblock` both clearing it
rg -n 'hold_origin' autoloop/tasks.py
# Expect: EMPTY — no runtime branch on the prose prefix (written, never read;
# the tests do assert it survives persistence, hence the exclusion)
rg -n 'startswith\(OPERATOR_HOLD_PREFIX\)' autoloop --glob '!tests/**'
# Expect: no second validator — the mutators call what add_many calls
rg -n '_validate_approved_paths|_validate_depends_on|_validate_description' autoloop/tasks.py
# Expect: EMPTY — no unauthenticated endpoint reaches a mutation kind
rg -n 'submit_mutation' autoloop/dashboard.py
```
**Suggested fix (if a route to these is ever added to the dashboard):** gate
the mutation kinds behind the per-process token S28 already proposes, and do
NOT let `approved_paths` be one of the fields the page can post — the priority
form's original argument ("priority decides what runs next, `approved_paths`
decides what an agent may touch, and only the first belongs on a form") is
still the right line for an unauthenticated page, even though it is no longer
the right line for the inbox file format.

### S32 — The dashboard writes `tasks.json` directly, and the escape detector has an exemption for it — MEDIUM — OPEN (bounded, accepted)

**What:** since 2026-08-16 `POST /api/priority` no longer queues a request. It
writes `.autoloop/tasks.json` immediately (`tasks.TaskStore.apply_priority`)
and answers with the value it read back from the file. Two properties this
tracker previously relied on therefore changed, and both are named here rather
than left implied:

- **The loop is no longer the only writer of the registry.** It is the only
  writer of everything except one integer field on one existing task.
- **`escape_detector.diff_snapshots` now accepts an `exempt` predicate**, and
  `orchestrator._operator_priority_exemption` supplies one for exactly that
  file. Without it a routine priority edit landing inside a detection window
  would park the loop LOOP-FATAL — i.e. the feature for steering the loop would
  be a way to stop it (the failure mode S24's bytecode amendment already cost
  three parks for).

**Why it was not done the safe-looking way.** Queuing was the safe-looking way
and it is what shipped first; it failed the operator, because a queued priority
becomes true only when the loop next drains between steps while the page keeps
re-rendering the old value — a save that worked and one that did not looked
identical. Excluding `tasks.json` from the snapshot was the other
safe-looking way, and it is the one this file has refused twice (S25's circular
ownership, the `pause_file` placement note in `config.py`): that file holds
`approved_paths`, so an agent able to edit it undetected could widen its own
authorization.

What bounds it, stated rather than assumed:
- **The exemption proves WHO, from outside the checkout.** Each immediate write
  appends `before -> after` content-digest records to
  `~/.autoloop/task-mutations.jsonl` (`tasks.MutationLedger`), beside
  `workers_root` — the placement the inbox, PAUSE flag and heartbeat already
  use because it is required to be outside the checkout. A change to
  `tasks.json` is silenced only if the COMPLETED records for THAT task file,
  written since the watermark captured before the agent started, form one
  unbroken chain whose first hop leaves the digest the snapshot saw before and
  whose TERMINAL state is the digest it saw after. An agent writing that file
  from inside the checkout produces no record and still parks the loop.
- **A chain, deliberately not a reachability set** (tightened 2026-08-16, after
  review). "Is the after-digest reachable from the before-digest" is a weaker
  claim than "this is what happened", and the gap was exploitable twice over:
  a legitimate round trip (3 -> 2 -> 3) left its intermediate digest reachable
  forever, so an agent writing that state was exempted for a change no operator
  made; and a record written before the file write meant an ANNOUNCED-then-
  FAILED write left an edge to a state that never existed on disk. Both are
  closed by the same three bindings — `phase == "complete"` (written only after
  the bytes land, so an intent is never an edge), the canonical task-file path
  on the record, and the caller's watermark — plus the terminal-state rule.
  Pinned by `test_an_in_window_round_trip_does_not_authorize_a_direct_edit` and
  `test_an_intent_from_a_failed_write_does_not_authorize_a_direct_edit`.
- **The exemption independently proves WHAT, from the bytes.** Even given a
  matching chain, the two file states must differ in nothing but `priority`
  values (`tasks.priority_only_change`, which re-reads the file and ignores the
  ledger's own claim about itself). A widened `approved_paths`, a flipped
  `status`, a rewritten `depends_on` or `description`, an added or removed task
  — none of them is a priority edit whatever any record says.
- **The endpoint cannot express anything else.** `PRIORITY_REQUEST_FIELDS` is
  `{id, priority}` and anything else is refused (not dropped);
  `apply_priority` refuses to CREATE the registry, so it cannot materialise a
  task graph from the seed; and it reaches no other file in the state dir.
- **Lost updates are answered by a lock, not by hope.** A short-lived mutex
  (`tasks.task_file_mutex` — a `flock` on `tasks.json.lock`, held for the
  milliseconds of load/mutate/save) serialises BOTH writers, and
  `TaskStore.save` reconciles the on-disk priority into the loop's in-memory
  registry so a round that began before the edit cannot write the stale value
  back. The lock file is created before the detector's "before" snapshot and
  never written to, so it is byte-identical on both sides and needs no
  exemption.
- **Same actor as S28/S30.** Anything that can post here can already edit the
  inbox directory or run the CLI. What changed is the timing of one field, not
  who may set it.

**The residual, named.** The ledger is an ATTESTATION, not an authenticator:
nothing signs a record, so an agent that writes outside its worker repo AND
knows the format can append one. What that buys is bounded by the second half
of the check — the bytes must still differ in nothing but `priority`, which the
task graph already treats as safe to change mid-flight because nothing already
dispatched depends on it. A second, smaller residual, WIDENED DELIBERATELY on
2026-08-16 and named here because it is a behaviour reversal: the predicate now
requires the file on disk to hash to exactly the digest the "after" snapshot
observed, and to be the terminal state of the window's chain. The previous
version explicitly allowed the file to have moved on to a LATER attested state,
and that allowance is what made the check "reachable from" rather than "is the
outcome of" — the round-trip hole above lived in it. The cost is that a SECOND
legitimate operator edit landing between the after-snapshot and the comparison
now parks the round, and the gap is the remainder of `snapshot_checkout`'s walk
over the checkout rather than microseconds. A spurious park an operator can read
and recover from was preferred to a laundering path that is silent by
construction. A third residual of the same shape: `TaskStore._write_bytes` writes
`.autoloop/tasks.json.tmp` and `os.replace`s it away, so an "after" enumeration
AND its hashing pass both landing inside that microsecond window would report
`created outside the worker repo: .autoloop/tasks.json.tmp`. (The mutex file
itself is not in this class — it is pre-created before the "before" snapshot
and never written, so it is byte-identical on both sides.) Both of the last two
are spurious parks in the safe direction, and both are strictly smaller than
the previous behaviour, where EVERY priority edit during a window parked the
loop. All three are the same shape as S24's honest residual and want the same
fix (an OS-level sandbox), not a wider exemption.

**file:line** — `autoloop/dashboard.py` `Handler._submit_priority` /
`PRIORITY_REQUEST_FIELDS` / `_task_store`; `autoloop/tasks.py`
`TaskStore.apply_priority` / `attested_priority_edit` / `save` /
`task_file_mutex` / `MutationLedger` / `priority_only_change`;
`autoloop/escape_detector.py` `diff_snapshots(exempt=…)`;
`autoloop/orchestrator.py` `_operator_priority_exemption` /
`_execute_with_escape_detection`.
**Severity:** MEDIUM — it opens a checked hole in a security-shaped check, and
the concrete gain for an attacker who already has local write access is limited
to one scheduling integer.
**Verification check:**
```bash
# Expect: the ONE exemption call site, in the orchestrator, and nothing in the
# detector that names a path of its own
rg -n 'exempt' autoloop/escape_detector.py autoloop/orchestrator.py
# Expect: both halves of the check in one place — the completed CHAIN (never a
# reachability set) AND priority_only_change — never one without the other
rg -n 'attested_priority_edit|priority_only_change|completed_chain' autoloop/tasks.py
# Expect: NO hits — reachability was the weaker claim the round-trip hole lived
# in, and it must not come back
rg -n 'def reachable' autoloop/tasks.py
# Expect: only a COMPLETE record is an edge, and it is written after the bytes
rg -n 'LEDGER_PHASE_COMPLETE|record_complete|record_intent' autoloop/tasks.py
# Expect: the ledger resolves BESIDE workers_root, never inside the state dir
# for a configured run
rg -n 'def mutation_ledger_for' -A 12 autoloop/tasks.py
# Expect: every writer of the task file takes the mutex
rg -n 'with self.lock\(\)|task_file_mutex' autoloop/tasks.py
# Expect: the endpoint's field set is {id, priority} and unknown fields refuse
rg -n 'PRIORITY_REQUEST_FIELDS' autoloop/dashboard.py
# Expect: EMPTY — no endpoint reaches any other registry mutator
rg -n 'set_approved_paths|set_depends_on|set_description|operator_block' autoloop/dashboard.py
```
**Suggested fix (if this ever needs to be stronger):** sign the ledger records
with a per-run secret the orchestrator writes outside the checkout at startup
and the dashboard reads — that turns the attestation into an authentication and
closes the forged-record residual. Do NOT close it by widening the exemption to
the whole state dir, and do NOT let a second field join `priority`: the "what"
half of the check is the only thing keeping a forged record cheap.

### S33 — Task descriptions and stored plans are rendered into the CONTEXT block that carries the review-integrity stamp — LOW — OPEN (bounded, accepted)

**What:** since 2026-08-18 (`plan-01`) `autoloop/context.py` renders two task
briefs into every request: `next_ready` (the READY task's id, title, FULL
description and effective `approved_paths`) and `in_review` (the task under
review, with its stored `decomposition`). Both are needed because `implement`
now REQUIRES a decomposition and `revise` may reuse the stored one — decisions
nobody can make from an id and a title — but they put text this package did not
author (an operator's task description, a reviewer's own earlier plan) into the
same block that carries `request_id` / `head_sha` / `report_sha256`, the values
a commit/push approval must copy.

**The exposure, stated plainly:** a description containing a line like
`report_sha256: 0000…` renders a second stamp-shaped line inside the CONTEXT
block. A reviewer (or any reader) that takes "the value after the label" could
read the planted one.

**Why it is bounded rather than open-ended — two independent reasons:**
1. **Ordering.** Both briefs are appended STRICTLY after every stamp line, so a
   first-match read still lands on the real value. Pinned by
   `test_context.test_briefs_are_rendered_after_every_stamp_line` (whose
   description contains a forged stamp line) and by
   `test_orchestrator.test_the_request_that_offers_ready_work_carries_what_to_
   plan_it_from`, not left to where someone happened to append.
2. **Verification, which is the actual control.** `contract.verify_review`
   compares all three echoed values against what was recorded for that request
   (`PendingRequest`), so a copied forgery draws `review_mismatch:*` and the
   approval is REFUSED. The failure mode is a denied push, never a push bound to
   a review that did not happen.

**Not mitigated by editing the text, deliberately.** The description is rendered
verbatim and uncapped: the reviewer cannot open the repository, so a truncated
or escaped description is one it would silently plan around — a worse failure
than a long request. This is the same trust boundary `S14` records for
user-content prompts, at the loop layer.

**Who can write the input:** whoever can plan a task (the reviewer itself, via
`plan`), the operator (`seed_tasks.json`, `python -m autoloop`, the inbox's
`description` kind — see S30), and nobody else. This is not learner-supplied
content.

**file:line** — `autoloop/context.py` (`TaskBrief`, `_render_brief`,
`render_context`'s ordering comment); `autoloop/contract.py` (`verify_review`).
**Severity:** LOW — it inserts unauthored text into a security-relevant block,
but the value that block exists to carry is verified against a recorded copy,
and the writers are already-privileged.
**Verification check:**
```bash
# Expect: the briefs are appended AFTER the stamp lines — the list is built,
# then extended; nothing may insert a brief above `report_sha256`
rg -n 'lines \+= _render_brief|report_sha256: ' autoloop/context.py
# Expect: the regression that plants a stamp-shaped line in a description
rg -n 'briefs_are_rendered_after_every_stamp_line' autoloop/tests/test_context.py
# Expect: all three values compared against what was recorded, not what was echoed
rg -n 'review_mismatch' autoloop/contract.py
```
**Suggested fix (only if this ever needs to be stronger):** move the briefs into
the PAYLOAD rather than the CONTEXT block, so the stamp block contains only
values this package wrote. That costs the per-template duplication this design
avoided (every payload template would have to carry them, and a new template
could forget), so it is worth doing only if a second unauthored-text section is
ever added here.

### S38 — An inbox request can now END the round in flight and quarantine its work — MEDIUM — OPEN (bounded, accepted)

**What:** `inbox.KIND_URGENT` (preempt-01, 2026-08-22) is the first request kind
whose effect is on the LOOP rather than on the task graph. Applying it makes
`Orchestrator._preempt_for_urgent` end the round currently in flight at its next
safe boundary, return that task to pending, and MOVE its worker repo and
execution record to quarantine. Every other mutation kind edits a row that
nothing is currently reading (`TaskRegistry._refuse_immutable` is built on
exactly that); this one deliberately reaches work that is under way, so S30's
"nothing in flight can be edited" bound no longer describes the whole
vocabulary and is recorded here rather than left implied.

The reachable damage from a hostile or mistaken request is **delay plus a
quarantined directory**: a round's committed candidate is moved, not deleted,
under `quarantine/<task>-displaced-by-urgent-<stamp>` with its execution record
archived beside it under the same label, and the displaced task is returned to
the queue where it will be dispatched again. Repeated requests could in
principle keep displacing rounds — bounded by one live pin at a time, and by
the fact that the pin is consumed by the very next dispatch, so a denial of
service needs a fresh request per round rather than a single write.

A retirement that FAILS (an unwritable quarantine root, a colliding label) is
the same statement with one directory not moved: nothing is deleted there
either, and what survived is named in `LoopState.preemption` and in the
operator report. When the worker is one the next dispatch would resume, the
record is put back beside it rather than left orphaned
(`orchestrator._repair_orphaned_record`); when it is not, the split is left and
the `mv` is named. The residual exposure either way is a worker repo that stays
under `workers_root` instead of `quarantine/` — no wider than where it already
was, and reachable by nothing that was not already reading it.

**Why it is not a review, approval or publication bypass.** Nothing on this path
builds a packet, verifies a stamp, marks a task completed or pushes: the round
simply ends between rounds, exactly as a reviewer's own `stop` does. The
`report_sha256` binding — which covers the packet bytes INCLUDING the candidate
sha, and is what stops an approval computed over candidate A from publishing
candidate B — is untouched, and the urgent task is implemented, reviewed,
approved and published through the identical path as any other. The one
enforcement ADDED is a refusal: `Orchestrator._refused_ahead_of_urgent` denies
an `implement`/`revise` of a different task, and (since the second round of
preempt-01, 2026-08-22) a FRESH `audit`, while a pin is live — through the
existing budget-capped policy-denial re-prompt. A `revise` continuing an audit
arc already in flight is the one exemption. That refusal only ever DENIES work;
it authorizes nothing, and `push` — the sole publishing decision — never reaches
it.

**Who can write one.** The same writers as any other inbox request — anything
able to write the inbox directory beside `workers_root`, which is outside the
checkout and therefore outside what a write-capable agent's own worker repo can
reach. No dashboard endpoint reaches this kind (`dashboard.TASK_REQUEST_FIELDS`
carries no `kind`, and `/api/priority` is the one immediate write, still
`priority`-only), so the surface is unchanged from S28/S30: an operator, or
anything with write access to that directory.

**What bounds it:**
- a target must be READY, dependency-satisfied, not quarantined, not retired,
  not completed, not already in flight, and must have a non-empty
  `approved_paths` (`TaskRegistry._refuse_unurgentable`) — so it cannot be used
  to point the loop at a task no dispatch could start;
- a non-blank `reason` is REQUIRED, so a preemption is never unaccounted for;
- ONE live pin at a time; a second request is refused naming the incumbent
  rather than queued or applied over it;
- it acts only at `_at_round_boundary` (`ready`, no pending request), the same
  predicate the self-upgrade restart uses, so a request arriving while a review
  packet is outstanding cannot strand it;
- the displaced task moves through `orchestrator.release_task_to_pending`, so
  the status, worker repo and execution record always move together;
- it takes no lock and writes no PAUSE flag, so it cannot wedge the loop for
  another operator (the failure mode the manual sequence had).

**`file:line`:** `autoloop/inbox.py` (`KIND_URGENT`, `_apply_mutation`),
`autoloop/tasks.py` (`TaskRegistry.request_urgent`, `_refuse_unurgentable`),
`autoloop/orchestrator.py` (`_preempt_for_urgent`, `release_task_to_pending`,
`_repair_orphaned_record`, `_refused_ahead_of_urgent`'s `urgent_target_pending`
denials) and `autoloop/cli.py` (`_start_new_session`, `URGENT_KICKOFF`,
`urgent_kickoff_payload`) — the pinned session's kickoff, which is reviewer-
visible text only and authorizes nothing. `autoloop/prompts.py` is NOT touched:
the kickoff template lives beside its caller in `cli.py`, as a `PromptTemplate`,
so the shared library's strictness applies to it without the library changing.

**Severity:** MEDIUM — availability/latency and operator-visible disruption, no
path to unreviewed publication or to widened write scope.

**Verification check:**
```bash
# Expect: the pin is refused for anything that is not a dispatchable READY task
rg -n '_refuse_unurgentable|urgent_already_pending' autoloop/tasks.py
# Expect: the preemption acts ONLY at the shared safe boundary, and reuses the release path
rg -n '_at_round_boundary|release_task_to_pending' autoloop/orchestrator.py
# Expect: the added gate only ever DENIES, and names no publishing decision
rg -n -A3 'def _refused_ahead_of_urgent' autoloop/orchestrator.py
rg -n 'push_exact|reviewed_commit|mark_completed' autoloop/orchestrator.py | rg 'urgent'
# Expect: EMPTY — nothing on this path reviews, approves or publishes
rg -n 'verify_review|report_sha256|push_exact' autoloop/orchestrator.py | rg 'preempt'
# Expect: the gate itself still refuses a mismatched approval, unchanged
rg -n 'review_mismatch' autoloop/contract.py
```
**Suggested fix (only if this ever needs to be stronger):** require the request
to carry a shared secret written beside `workers_root` at provision time, so a
preemption needs something more than write access to the inbox directory. Not
done now because it would be the FIRST authenticated inbox kind while the other
seven stay unauthenticated — a half-authenticated queue reads as protected
without being so, and the real confinement for that whole surface is the
OS-level sandbox S24 tracks.

### S29 — `merge` is on the git whitelist, and the loop now pushes the BASE branch — LOW — OPEN (deliberate, gated, accepted)

**What:** auto-merge (`autoloop/auto_merge.py`, 2026-08-14) needed two things
that did not exist before, and both widen surface that S21/S22/F2/F5 spent
effort narrowing. Recorded here rather than left to a reviewer to find in the
diff.

1. **`merge` joined `_ALLOWED_GIT`.** It is the first subcommand there that
   moves the CHECKOUT's own branch head. Everything else either reads, writes
   into a worker's separate worktree, or publishes an already-resolved sha by
   refspec. The whitelist's stated hard rule — reset/clean/rebase/checkout are
   simply absent — is unchanged; `merge` is not a history rewrite (it only
   adds a commit whose parents include the previous head), and it is verified
   to be one afterwards.
2. **`push_exact` now publishes the base branch**, not only a task side
   branch. That is a new destination class for the loop.

What bounds it, stated rather than assumed:
- **Shape-checked like `push` and `fetch` (F2).** `validate_git_command`
  admits exactly two forms: `merge --abort` with **no other token**, and
  `merge [--no-ff] [--no-edit] [-m <msg>] <40-hex>`. A branch name, `HEAD`, a
  tag or a second commit is refused before any subprocess runs — a branch can
  move between the merge-window check and the merge.
- **Off by default.** `policy.auto_merge_enabled = False`, and it is the only
  setting that moves the shared branch head with no operator in the loop.
- **Only reviewed, already-published objects.** The merged sha comes from a
  `TaskExecution.candidate_sha` whose task is COMPLETED, re-confirmed against
  the remote by `ls-remote` (`cli._candidate_publication`) at merge time, not
  merely at completion time.
- **The base push obeys the existing gate.** `gateway_protected` is computed
  exactly as `_dispatch_task_push` computes it, so a base in
  `protected_branches` is refused unless `allow_protected_push` is set.
  Enabling auto-merge is not permission to push `main`.
- **No new undo primitive.** `reset` was NOT added. A merge that fails
  verification stops and reports; it is never unwound by the loop.
- **Verified, not assumed.** After the merge: head moved, head contains the
  candidate, head still contains the previous head, tree clean — then, and
  only then, `push_exact`, whose own confirmation is a fresh `ls-remote`.

**file:line** — `autoloop/policy.py` (`_ALLOWED_GIT["merge"]` and the
`sub == "merge"` shape check in `validate_git_command`);
`autoloop/git_gateway.py` `merge_commit` / `merge_abort` / `conflicted_paths`;
`autoloop/auto_merge.py` `AutoMerger._merge` / `_verify_merge` / `_push`.
**Severity:** LOW — the merge target is a literal, already-reviewed,
already-published commit id; the feature is off by default; and no
history-rewriting subcommand became reachable.
**Verification check:**
```bash
# Expect: only the two legal merge shapes; a branch name is refused
rg -n 'git_merge_commit|git_merge_abort_shape' autoloop/policy.py
# Expect: still absent from the whitelist — no undo primitive was added
rg -n '"(reset|clean|rebase|checkout|filter-branch)":' autoloop/policy.py
# Expect: false — the flag must stay opt-in
rg -n 'auto_merge_enabled: bool' autoloop/policy.py
# Expect: the protected-ref computation is the same one _dispatch_task_push uses
rg -n 'allow_protected_push' autoloop/auto_merge.py autoloop/orchestrator.py
```
**Suggested fix:** none — the exposure is the feature. If it ever needs
narrowing, the cheapest lever is refusing the merge outright when any commit
hook is active (`active_commit_hooks`), mirroring `push_exact`'s push-hook
refusal; today a `post-merge` hook is caught after the fact by the dirty-tree
check rather than refused before it.

**Amendment 2026-08-15 (rel-01) — a third merge-window exemption.** The gate
this feature is bound to (`cli._merge_window_blockers`) now writes off one more
class of record, so the set of states in which auto-merge may move the shared
branch head is strictly larger. Recorded here because widening that gate
weakens the control this finding rests on, and doing so silently is the
regression CLAUDE.md §14 asks about.

What was added: a record is ignored when its task is pending or
dependency-blocked **and** the `worktree_path` it recorded is gone from disk
**and** the checkout affirmatively answers that it cannot resolve the
candidate. What bounds it:

- **All three conditions, never a subset.** Each is pinned by its own test
  (`test_merge_window.py`), including the negative ones — a reachable commit
  still closes the window however dead its worker looks, and an IN-PROGRESS
  task is never written off.
- **Absence of evidence is not evidence.** An empty `worktree_path` fails the
  second condition. "We never recorded where it was" is not "we know it is
  gone", and every pre-existing record shape in the test suite has an empty
  one, which is why none of them changed verdict.
- **Fail-closed on the git probe.** The repository must first prove it can
  answer (`head_sha`) before its "no such object" counts; anything else —
  no repository, git unavailable, any other raise — keeps the window shut,
  the same rule `_candidate_publication` already states. Strengthened
  2026-08-15 (same task, review round 3): a failing `read_commit` was being
  read as "the object is absent", which it is not — `cat-file commit` dies
  identically for a corrupt object, an I/O error and a `GitOperationDenied`
  policy refusal, so operational trouble could open the window. A failed read
  now asks `GitGateway.object_exists`, which answers True/False from
  `cat-file -e`'s exit code and RAISES on anything else; only an explicit
  False writes the record off.
- **One new whitelist flag, read-only.** `_ALLOWED_GIT["cat-file"]` gained
  `-e` for that probe. It prints nothing and returns only a status — strictly
  less than the `cat-file commit`/`blob` content reads already admitted there
  — and no other `cat-file` flag was added (`-p`, `--batch` and friends stay
  refused).
- **Never silent.** It is reported as a `note:` by `merge-window` and logged as
  `auto_merge_window_note` by `AutoMerger`, which previously discarded the
  gate's notes entirely. A record being written off is visible on both paths.
- **The reason it is narrow enough to be honest:** `release` now retires its
  own execution record (`worktask.retire_execution`), so this exemption exists
  for records that predate that fix or drifted some other way, not as the
  primary mechanism.

**Verification check:**
```bash
# Expect: all three conditions present, and the fail-closed head_sha probe
rg -n 'def _candidate_is_retired' -A 40 autoloop/cli.py
# Expect: the write-off happens only after object_exists answers False; every
# other outcome of either probe returns "" (window stays shut)
rg -n 'object_exists' autoloop/cli.py autoloop/git_gateway.py
# Expect: -e only; no other cat-file flag on the whitelist
rg -n '"cat-file": frozenset' autoloop/policy.py
# Expect: the gate's notes are logged, not discarded, on the auto-merge path
rg -n 'auto_merge_window_note' autoloop/auto_merge.py
# Expect: release retires BOTH halves through one call
rg -n 'retire_execution' autoloop/cli.py autoloop/worktask.py autoloop/orchestrator.py
# Expect: reconciliation refuses to retire a record an undrained merge
# deferral still reads from
rg -n 'execution_retire_pinned_by_deferral|_outstanding_merge_deferral' autoloop/orchestrator.py
```

**Amendment 2026-08-15 (merge-03) — the base head can now move at STARTUP, and
from a new command.** `autoloop/merge_sweep.py` integrates branches that were
published before auto-merge existed. It adds no git primitive and no policy
flag: it calls `AutoMerger.attempt` per branch, so every bound listed above —
the two legal merge shapes, the publication re-confirmation, the protected-base
refusal, the four-part verification, no undo primitive — applies unchanged.
What genuinely widens is WHEN the shared head may move: previously only in the
last statement of `_dispatch_task_push`, now also once per `run`/`start`/
`resume` process before the loop begins, and whenever an operator runs
`merge-backlog`. Recorded because "the head only moves right after a
completion" was a real property of the previous design, and losing it silently
is the regression CLAUDE.md §14 asks about.

What bounds the wider window:

- **The same flag.** `policy.auto_merge_enabled`, still default False. With it
  off, both entry points return `disabled` before reading anything.
- **The same gate, checked before anything mutates.**
  `cli._merge_window_blockers` — called once for the whole sweep, so a shut
  window means zero merges rather than a partial one. A crash-left
  `phase=executing` in `state.json` therefore defers the startup sweep, which
  is correct: an agent may have been mid-write.
- **The same objects, on evidence that is FRESH per branch.** A branch is only
  attempted when its task is COMPLETED and an `ls-remote` reports the branch
  carrying exactly that `candidate_sha` — the record's own
  `intended_remote_ref` is never taken as evidence, which is what stops a
  deleted or force-moved ref from being merged from a stale claim. Since the
  merge-03 review round the confirmation is re-taken immediately before each
  branch's own merge (`BacklogSweeper._reconfirm` evicts that candidate's key
  from the memo set first): a sweep spends minutes merging and pushing, and
  reusing the enumeration's positive cache would have let a ref deleted or
  force-moved during that window be merged from an answer obtained before the
  earlier merges ran. A candidate the remote no longer confirms stops the sweep
  there.
- **Reading the execution ARCHIVE is read-only and never a merge source.** A
  completed task with no live record is checked against
  `executions/archive/<task_id>-*.json` for a sha already ancestral to HEAD —
  a local `merge-base` question, answered from the object database, with no
  authority granted to anything the archived record asserts about itself. That
  answer can only make the sweep report MORE (an unresolved branch); it can
  never make a branch mergeable, because the merge path loads the LIVE record
  and skips a task without one. Since the third merge-03 review round only the
  NEWEST archived generation answers, and an archive whose generations cannot be
  ordered answers "unresolved": `archive` keeps one file per retirement, so an
  older released/salvaged copy could otherwise clear a task whose completing
  publication is still outstanding — a report-clear on evidence about a
  different commit. The archive is globbed by filename PREFIX and task ids may
  contain `-`, so `rt-1-*.json` also matches `rt-1-b-published-<stamp>.json`;
  each copy is now checked against the `task_id` it carries and dropped only
  when it proves it belongs to another task (an unreadable or owner-less copy
  is kept, since "cannot tell" must not read as "not mine"). All three changes
  move the answer toward "unresolved", i.e. toward reporting more and merging
  less.
- **An unjudgeable task withholds the whole invocation.** Since the third
  merge-03 review round, any completed task the enumeration cannot judge makes
  the sweep non-mutating for that run (`merge_sweep_held`). The reason is an
  ancestry one rather than a reporting one: a candidate the remote DOES confirm
  may be descended from the one it does not, so merging the judgeable branch
  carries the unconfirmed publication into the base transitively — granting
  exactly what refusing to merge it directly withheld. Fail-closed before
  mutation; it can only reduce what the sweep merges, never widen it.
- **Enumeration cannot invent a target.** The set comes from the registry and
  the execution records, never from the remote's ref namespace, so no branch a
  third party pushes to origin becomes mergeable by appearing there.
- **It stops rather than continuing.** The first branch that does not land
  (conflict, unverified merge, deferral) halts the sweep; nothing is stacked
  onto a head that failed verification, and no second merge follows a
  `merge --abort`.
- **A stop that LEFT the head moved fails closed at startup.** Since the fourth
  merge-03 review round. Stopping bounds the sweep, but two of its outcomes
  leave the base moved and unpublished — a merge that failed verification (not
  undone, deliberately: there is no undo primitive) and one whose push was
  refused, which reports as `deferred`, the same slug a shut gate produces. The
  checkout is therefore PROBED (HEAD + `status --porcelain`) immediately before
  each attempt and again the moment one does not land, and only a match is
  reported as "the base is exactly as it was"; an unreadable probe is not a
  match. When it does not match, `cli._sweep_backlog_on_startup` returns False
  and `_cmd_run` returns 1 without entering `_run_locked` — otherwise the loop
  would dispatch a task cut from a head nobody verified, and its own
  `_dispatch_task_push` would carry the unverified or unpushed merge along with
  it, which is the very stacking the stop exists to prevent. This narrows what
  runs after a sweep; it grants nothing. The refusal publishes a `parked`
  heartbeat (an ATTENTION status) rather than `stopped` or nothing at all, so a
  monitor cannot go on reading a dead run's `running` beat while the only notice
  sits on a terminal.
- **Under the loop lock, both ways.** The startup sweep runs inside `_cmd_run`'s
  `LoopLock`, and `merge-backlog` takes the same lock (unlike `merge-window`,
  which only reports), so it cannot race a live loop.
- **It cannot stop a run, except over its own residue.** Every failure swallows
  to a transcript entry — a sweep that refused to let the loop start over an
  unmergeable branch would be a strictly worse failure than the branch itself,
  so held, deferred, dirty-refused and cleanly-aborted-conflict outcomes all
  report and continue. The single exception is the bullet above: a checkout this
  module moved and could not finish integrating, which no later round can be
  trusted to build on.

**Verification check:**
```bash
# Expect: the sweep CALLS the gate and the merger; it defines no merge/push of
# its own (no merge_commit / push_exact / update-ref in this module)
rg -n '_merge_window_blockers|\.attempt\(|merge_commit|push_exact' autoloop/merge_sweep.py
# Expect: both entry points gate on the flag before doing anything
rg -n 'auto_merge_enabled' autoloop/merge_sweep.py
# Expect: the startup sweep is inside the lock, once per process
rg -n '_sweep_backlog_on_startup' -B 4 -A 2 autoloop/cli.py
# Expect: merge-backlog takes LoopLock (merge-window deliberately does not)
rg -n 'def _cmd_merge_backlog' -A 12 autoloop/cli.py
# Expect: publication is re-confirmed per branch, remote-first — TWO call sites
# (enumeration, then `_reconfirm` immediately before that branch's own merge),
# and the memo key is evicted before the second so it cannot answer from cache
rg -n '_candidate_publication|seen.discard' autoloop/merge_sweep.py
# Expect: the hold is checked BEFORE the gate and BEFORE any `_attempt` call —
# i.e. an unjudgeable task makes the invocation non-mutating, not merely noisy
rg -n 'if result.unresolved|_merge_window_blockers|self._attempt' autoloop/merge_sweep.py
# Expect: only the newest archived generation is judged (no loop over every
# archived copy returning True on the first ancestral one)
rg -n '_newest_generation|_retirement_stamp' autoloop/merge_sweep.py
# Expect: "the base is where it was" is a PROBE of the checkout, not a reading
# of the outcome slug — one observation before each attempt, one after a stop
rg -n '_probe\(|_unreconciled\(' autoloop/merge_sweep.py
# Expect: startup FAILS CLOSED on an unreconciled checkout — `_cmd_run` returns
# without reaching `_run_locked`, and no `stopped` heartbeat is published
rg -n 'if not _sweep_backlog_on_startup' -A 8 autoloop/cli.py
```

### S25 — Circular task-scope authorization, failed-round residue, and unbounded pre-commit retries — HIGH — RESOLVED 2026-07-31

**What it was:** three compounding gaps in the produce-then-review commit
path (§4b/§4c), reported together because the fixes share one root cause
(the agent's own report was trusted as authorization) and one mechanism
(a task's worker repo).

1. *Circular ownership.* `TaskExecution.allowed_paths` — what the
   post-commit review gate (`_verify_committed`) checks a commit's changed
   paths against — was the UNION of every round's `outcome.changed_paths`,
   i.e. the agent's OWN report of what it touched
   (`orchestrator.py:1641-1643` and `:1517-1519`, pre-fix). "Did it stay in
   scope?" was checked against "whatever it did" — there was no
   machine-checkable authorization independent of the executor.
2. *Failed-round residue.* `ImplementExecutor._run_implementation` reads
   `git.dirty_paths_all()` only AFTER the agent runs; a round that returned
   `status="error"` (a validation failure, before any commit) left its
   files sitting in the worker repo, and the NEXT round reused the same
   dirty worktree — so content that failed its own validation could ride
   into a later, passing round's commit.
3. *Unbounded pre-commit retries.* `TaskExecution.attempt_count` only
   incremented in `_finish_postcommit`, reached only AFTER a commit — a
   validation failure or a pre-commit crash never consumed an attempt, so
   `MAX_TASK_ATTEMPTS` did not bound the one failure mode it exists for.

**Fix:** `Task.approved_paths` — exact, repo-relative, non-glob, non-`..`
paths, validated on the way into `TaskRegistry.add_many`
(`tasks._validate_approved_path`) and re-checked for symlink traversal at
dispatch time (`escape_detector.find_symlink_traversal`) — is now the
single, machine-checkable, pre-authorized scope for a task, set before the
writer ever starts and never widened by anything the executor reports.
`_dispatch_task_postcommit` refuses to dispatch a task with none
(`approved_paths_missing`), checks `outcome.changed_paths` against it
BEFORE `commit_and_capture` runs, and `execution.allowed_paths` is seeded
once from it and never unioned again (the post-commit check sees what the
pre-commit one cannot — a hook adding a path after it runs).

**Amended 2026-08-05 — the two scope CHECKS are now ADVISORY (operator
decision).** Both comparisons still run, unchanged: same
`tasks.unauthorized_paths` matcher, same inputs, at both ends
(`orchestrator.py` pre-commit, `_verify_committed` post-commit). Only the
consequence changed — where they parked the task
(`changed_paths_outside_approved` / `post_commit_verification_failed`, both
now gone for this reason) they record onto
`TaskExecution.out_of_scope_paths` and the round proceeds to review. Six
parks in three days were all legitimate work, at least three caused by a
task scope guessed wrong when the task was written; a declared scope is a
prediction, and a wrong prediction belongs in front of the reviewer rather
than discarding the round. Both sites had to change together — relaxing
only the pre-commit one moves the park downstream to the post-commit one
for the same paths.

**What this does NOT weaken.** `approved_paths` is still the authorization
and is still never widened by anything an executor reports:
`out_of_scope_paths` is written ONLY from what the comparisons produced,
never from an agent claim, and records that scope was exceeded without
granting it — `allowed_paths` stays `effective_approved_paths(task.
approved_paths)` (pinned by
`test_agent_reported_extra_path_is_recorded_but_cannot_widen_authorization`).
An empty `approved_paths` still refuses dispatch outright — a different
rule, deliberately not relaxed, since "no scope declared" means there is no
prediction to be wrong about. Escape detection (a write outside the worker
repo into the primary checkout) is a different mechanism and stays
loop-fatal. Every other post-commit refusal — ancestry, empty range, dirty
worktree, failing validation, validation mutating the tree — is untouched.
The residual exposure is real and accepted: an out-of-scope path now
reaches a human reviewer instead of a hard stop, so **the review packet's
out-of-scope section is the control** that replaces the park.

**The control, built 2026-08-05** (`packet._format_out_of_scope`). Every
review packet carries an `OUT-OF-SCOPE PATHS (n)` section naming each path,
with three properties the control depends on:

* **Computed from git at render time** — `commit_range_paths` vs
  `execution.allowed_paths`, the same inputs `_verify_committed` uses — and
  deliberately NOT read from `TaskExecution.out_of_scope_paths`. That record
  holds the same answer by then, but it is the union of both gates and the
  pre-commit gate's input is `outcome.changed_paths`, the executor's own
  report; sourcing the section from it would let an agent's report state a
  path as a fact about the commit. Pinned by
  `test_the_out_of_scope_paths_come_from_git_not_from_the_record`.
* **Rendered only when there IS an overrun** (changed 2026-08-15; the first
  cut rendered unconditionally with an explicit `(none)` line). A section
  present in every packet and empty in nearly all of them is one a reviewer
  learns to skip — and it would be trained to skip it by the clean packets,
  so the control would read weakest on the rare packet that finally had
  something to say. The objection the `(none)` line answered stands and is
  answered elsewhere: absence is indistinguishable from a section dropped in
  a refactor, so what guards against that is now
  `test_the_packet_names_every_out_of_scope_path` — which fails the moment a
  REAL overrun renders nothing — plus the literal-string grep below. A
  regression must be caught by a failing test, not by a human noticing a
  missing line.
* **Labelled as advisory in its own text.** The section states that the
  scope check did NOT stop the commit and asks for a `revise` verdict if the
  edits do not belong to the task, so a reviewer reads it as a judgement
  call on a live candidate rather than as a failure already handled.
* **Placed above the diff**, so it survives the omission below.

**Residual risk, stated plainly.** Detection now depends on a human reading
that list; an agent that touches unexpected files produces a reviewable
candidate rather than a refusal. And on a diff over
`packet.DIFF_INCLUDE_MAX_CHARS` (30,000 chars) the patch is OMITTED, so the
reviewer sees the out-of-scope PATHS but not the CONTENT of those edits —
the same degradation `tasks.py`'s `TRACKER_PATHS` comment names for tracker
files, and an argument for reviewing large commits harder, not for trusting
the list less.

**Verification check:** `rg -n 'out_of_scope_paths' autoloop/orchestrator.py`
— must show it assigned at BOTH sites (the pre-commit check and
`_verify_committed`) and appended to `failures` at neither. And
`rg -n 'OUT-OF-SCOPE PATHS' autoloop/packet.py` must hit: that string IS the
replacement control, and a grep for the field name alone would never notice
it had gone (the section is computed from git and does not mention the
field). That grep is load-bearing now that a clean packet omits the section
entirely — reading one packet with no overrun can no longer tell you the
control exists.
`Orchestrator._prepare_write_capable_worker` requires the worker repo clean
before every write-capable dispatch; residue is QUARANTINED (moved, never
deleted — `WorkerRepoManager.quarantine`) rather than reused, and a fresh
repo is created from the last committed round (or the task base if none).
**Scoped 2026-08-18 (wrk-01):** a resumed dispatch whose recorded worker
passes the reuse gate (`worker_repo_is_reusable` — exists, is a git repo,
on the recorded branch) is EXEMPT from that residue quarantine
(`reused_recorded_worker=True`): its uncommitted residue is the
interrupted round's own partial work, which the resumed executor picks
back up. This is a deliberate narrowing, not a silent regression of this
finding: what the quarantine protected against — content that failed its
own validation slipping unreviewed into a later candidate — is still held
by two unchanged controls, `commit_and_capture` staging exactly the
reported `changed_paths` and `_verify_committed` refusing any candidate
whose worktree is not clean after commit (everything committed is in the
reviewed diff; anything uncommitted refuses the round). The quarantine
itself is unchanged for every preparation that did not pass the gate.
`attempt_count` now increments and persists BEFORE the executor is ever
called, so it is durable across a crash, a restart, or a validation
failure. See `docs/AUTOLOOP.md` §4e for the full design and
`autoloop/tests/test_m1_hardening.py` for the adversarial tests (agent
self-report cannot widen scope; a failed attempt's UNREPORTED files never
reach a later commit — since wrk-01 proven via exact staging plus the
post-commit residual-dirty refusal rather than via quarantine; the attempt
budget survives a simulated restart).

**file:line** — `autoloop/tasks.py` (`Task.approved_paths`,
`_validate_approved_path`); `autoloop/orchestrator.py`
(`_dispatch_task_postcommit`, `_prepare_write_capable_worker`); `autoloop/
worker_env.py` (`WorkerRepoManager.quarantine`); `autoloop/contract.py`
(`TaskSpec.approved_paths`).

**Verification check:**
```bash
# The self-widening union sites are gone for a real (non-audit) task:
rg -n 'execution.allowed_paths = tuple' autoloop/orchestrator.py   # both remaining sites are `if is_audit:`
# attempt_count is incremented before dispatch, not in _finish_postcommit:
rg -n 'attempt_count \+= 1' autoloop/orchestrator.py               # one hit, before `self._executor.execute`
pytest autoloop/tests/test_m1_hardening.py -k "approved_path or attempt or residue" -q
```

**Suggested fix:** none outstanding — closed. If a future change re-adds a
path to `execution.allowed_paths` from executor/agent output for a
non-audit task, that is a regression of this finding, not a refactor.

**Addendum, same day (quarantine-recreate fetch bug):** the quarantine-and-
recreate branch of `_prepare_write_capable_worker` (point 2 above) initially
recreated the worker repo by fetching from `self._git.repo_root` (the
PRIMARY checkout) unconditionally — correct when resuming from
`execution.task_base_sha` (always present in the primary checkout), but
wrong when resuming from `execution.candidate_sha`: that commit exists ONLY
inside the worker repo just quarantined, its own separate git object
database, never pushed or fetched anywhere. Reachable whenever a round
already committed successfully, a LATER round then fails validation and
leaves residue in the same worker repo, and a THIRD round's dispatch
quarantines that residue — the recreate would raise `GitCommandError`
("does not match any") instead of resuming, which is a robustness bug, not
an authorization bypass (nothing insecure lands; the task simply cannot
proceed), but it defeats the whole point of quarantine-and-recreate for the
exact multi-round-revise case it exists to handle. Fixed: the fetch source
now depends on which sha is resumed — `candidate_sha` fetches from the
quarantined directory itself (moved, never deleted, so the object is still
there); `task_base_sha` still fetches from the primary checkout. See
`docs/COMMON_ERRORS.md` §8 for the reproduction and
`test_quarantine_recreate_resumes_from_a_candidate_sha_that_only_exists_in_
the_quarantined_repo` for the regression test.

**Amended 2026-08-19 (scope-04) — a task may DELETE what the loop recorded it
created out of scope, and only that.** The 2026-08-05 amendment above made
advisory scope asymmetric without anyone intending it: creating a file outside
`approved_paths` was permitted and recorded, but removing that same file again
was not, so "delete the residue you added" — a correct review — was
unperformable. Observed on roadmap-01 (2026-08-18): the reviewer asked twice,
verbatim, that `autoloop/obsolete.py` be absent from the candidate rather than
committed as a zero-byte addition; the identical feedback tripped
`review_feedback_unchanged` and the task parked after 8 rounds with its
implementation already accepted. Every component behaved correctly and the task
still deadlocked.

**The rule.** A later round of the SAME execution may delete a path that is
already in that execution's `TaskExecution.out_of_scope_paths`. Nothing else
changes: the path is not added to `approved_paths` or to
`allowed_paths`, and creating, editing, recreating or renaming into it stays
exactly as unauthorized as before (it lands, it is recorded, the reviewer
judges it — the 2026-08-05 behaviour, untouched).

**Why this does not widen scope.** The authorizing set is the loop's own record,
written by the two path comparisons from `outcome.changed_paths` and git's
`commit_range_paths` and never from an agent claim — so a task can only clean up
what the loop itself DEMONSTRATED it wrote out of scope. Three properties bound
it:

* **Exact match only** (`tasks.authorized_cleanup_paths`), deliberately unlike
  `unauthorized_paths`: no directory-prefix rule, so a recorded file never
  authorizes a sibling, a near-miss spelling, or its directory.
* **Deletion only.** The capability is one `unlink` in
  `implement_executor._remove_recorded_file`, which refuses an absolute path, any
  `..` segment, a parent that does not resolve inside the worker repo, and
  anything that is not a regular file or symlink — so no directory and no
  recursive delete is reachable, and a symlink is removed as the link, never
  followed.
* **The agent selects, it never authorizes.** The write-capable agent has no
  delete tool at all (`WRITE_ALLOWED_TOOLS`, `Bash` disallowed), so it asks with
  a `REMOVE-OUT-OF-SCOPE: <path>` line and the executor performs the unlink only
  after `authorized_cleanup_paths` matches it against the persisted record. A
  request for anything else deletes nothing and is reported as ignored (counted,
  never quoted — the round summary becomes the commit message). An agent echoing
  its own instruction emits the placeholder `<repository-relative path>`, which
  no record can contain.

**What is unchanged.** The empty-`approved_paths` dispatch refusal, escape
detection, and every post-commit check (ancestry, empty range, dirty worktree,
failing validation, validation mutating the tree) are all untouched. The
`cleanup_paths_for` reader is injected in `cli._build_executor`; absent it —
any embedder that does not wire it — there is no cleanup authority at all.

**Residual exposure, stated plainly.** A round can now delete an out-of-scope
file the reviewer wanted KEPT. Bounded by the same control as the rest of this
amendment: the deletion is staged and committed like any other change, so it is
in the diff the reviewer reads, and `TaskExecution.removed_out_of_scope_paths`
records it durably — needed because `commit_range_paths` is a tree-to-tree diff,
so a file created in round 1 and deleted in round 2 is absent from the reviewed
range entirely. `out_of_scope_paths` is NEVER pruned when a path is cleaned up:
the record that authorization was exceeded is regression history.

**Not addressed, and worth naming.** This closes out-of-scope cleanup only. A
task still cannot delete a file INSIDE its `approved_paths` — the missing
capability is the agent's, not the authorization's — so a review asking for the
removal of an in-scope file remains unperformable. roadmap-01's second file,
`autoloop/tests/test_obsolete.py`, was exactly that case.

**Amended 2026-08-24 (scope-05) — the same authority now also RESTORES a
recorded path to its base content, and still grants nothing else.** The
amendment above covers a CREATED file. port-01's contamination on 2026-08-20 was
ten EDITED files and zero creations, so the exception reached none of it and the
same edits were carried onto every following round of the same branch (8 commits
over 11 attempts, discarded by hand). `REVERT-OUT-OF-SCOPE: <path>` is a second
request form whose authorizing set is EXACTLY the one above — the loop's own
`out_of_scope_paths`, through the same `tasks.authorized_cleanup_paths`, exact
match only, agent selects and never adds.

Four properties bound the new capability, and each is the deletion's own
property restated for a write:

* **The content is git's, at a LOOP-written sha.** `_revert_recorded_file` writes
  the blob `TaskExecution.task_base_sha` holds for that path — never bytes the
  agent supplied, and never a path the agent named that is not already recorded.
  That field is not immutable (a stale-base refresh or a recut moves it) but it
  is unreachable from any agent, and it is the commit the reviewed range is
  already measured from, which is the property that matters here.
  A revert is therefore verifiable after the fact (the path leaves
  `commit_range_paths(task_base_sha, candidate_sha)`), which an agent-authored
  "put it back" would not be.
* **The write is bounded like the unlink.** Same refusals — absolute path, any
  `..` segment, a parent that does not resolve inside the worker repo — plus:
  only a `100644`/`100755` BLOB entry is restorable (a `120000` symlink or
  `160000` submodule at the base is refused, never written out as file bytes), a
  directory at the target is refused (no recursive delete is reachable), and a
  symlink at the target is unlinked as the LINK and replaced, never written
  through. Only git's one permission bit is set; nothing else is chmod'ed.
* **No new agent capability.** `WRITE_ALLOWED_TOOLS` and
  `IMPLEMENT_DISALLOWED_TOOLS` are byte-identical; the executor performs the
  restore. The second anchor refuses `-`/`*`/`>` prefixes like the first, and
  the prompt's placeholder is not a path any record can hold.
* **Fail-closed at every absent input.** No injected `revert_authority`, no base
  sha, a base sha that is not a plain hex object name, or a base tree git cannot
  read: nothing is reverted, the capability is not offered, and every request is
  reported refused. An unreadable base explicitly does NOT fall through to the
  created-path branch — the one fail-open available here would have been an
  unreadable base silently converting a revert into a deletion.

**Residual exposure, stated plainly.** A round can now overwrite an out-of-scope
file the reviewer wanted left as the round had edited it. Bounded by the same
controls: the write is staged and committed like any other change and is in the
diff the reviewer reads; the content can only ever be the base commit's, so the
worst case is a file returned to where the task found it;
`TaskExecution.reverted_out_of_scope_paths` records it durably; and
`out_of_scope_paths` is never pruned.

**Wired in production since 2026-08-24 (same task, revision round).**
`cli._build_orchestrator` passes
`revert_authority=RecordedRevertAuthority(execution_store)` into
`cli._build_executor`, over the SAME store `cleanup_paths_for` reads — one
authorizing list, not two. Deleting that argument re-arms the fail-closed
default for every live round, which is a capability change and not a cleanup.
Two consequences of being live that were theoretical before it: a corrupt
execution record now really can raise `StateCorruptError` inside
`RecordedRevertAuthority.base_sha` (caught by `_revert_base_sha`'s `except
Exception`, answered as "", pinned by
`test_a_corrupt_execution_record_offers_no_revert_and_never_raises`), and a
task's very first dispatch has no record at all (also "").

**file:line** — `autoloop/tasks.py` (`authorized_cleanup_paths`);
`autoloop/implement_executor.py` (`_CLEANUP_RE`, `_cleanup_instruction`,
`_apply_recorded_cleanup`, `_remove_recorded_file`, and since scope-05
`_REVERT_RE`, `_apply_recorded_reverts`, `_revert_recorded_file`,
`_revert_base_sha`); `autoloop/orchestrator.py` (`_dispatch_task_postcommit`,
the `removed_out_of_scope_paths` union); `autoloop/worktask.py`
(`TaskExecution.removed_out_of_scope_paths`,
`TaskExecution.reverted_out_of_scope_paths`, `RecordedRevertAuthority`);
`autoloop/cli.py` (`_recorded_out_of_scope_paths`, and the
`revert_authority=RecordedRevertAuthority(execution_store)` argument
`_build_orchestrator` hands `_build_executor`).

**Verification check:**
```bash
# The gate: the ONLY callers of the matcher are the executor's two repair
# passes, and the matcher is exact-match — no `is_directory_prefix`, no
# `startswith`:
rg -n 'authorized_cleanup_paths' autoloop/                  # tasks.py def + 2 implement_executor.py calls
rg -n 'REMOVE-OUT-OF-SCOPE|REVERT-OUT-OF-SCOPE' autoloop/implement_executor.py
# The wiring, which is what makes the capability exist at all — expect BOTH the
# `_build_executor` parameter and the `_build_orchestrator` argument:
rg -n 'revert_authority' autoloop/cli.py
# Repair must never widen either authorization field — expect NO hit:
rg -n 'allowed_paths.*cleanup|approved_paths.*cleanup' autoloop/
rg -n 'allowed_paths|approved_paths' autoloop/implement_executor.py  # read-only rendering only
pytest autoloop/tests/test_scope_cleanup.py autoloop/tests/test_scope_revert.py -q
```

**Suggested fix:** none outstanding. If a future change ever populates the
repair set from an agent's report, admits a prefix match, lets a repair path
reach `allowed_paths`/`approved_paths`, restores content from anywhere but
`task_base_sha`, or makes an unreadable base fall through to a deletion, that is
a regression of this finding, not a refactor.

### S26 — Two `answer`-precondition keys were dead or mismapped, letting environmental blockers clear on text alone — MEDIUM — RESOLVED 2026-07-31

**What it was:** `cli._RESOLUTION_PRECONDITIONS` maps a blocker `code` to a
function re-checked at `python -m autoloop answer` time, specifically so an
operator's promise cannot clear a condition that is still environmentally
true. Two of the seven keys did not do what their own comment claimed:
`git_failure_budget` never matched anything (the code `orchestrator.py`
actually emits is `git_failure_budget_exhausted`), and
`push_refused_protected` was never emitted at all (every push refusal,
protected-branch or otherwise, surfaced as the generic `push_refused`) — so
its correctly-implemented precondition (`_precondition_protected`, "a
protected destination cannot be cleared by an answer") was unreachable.
Separately, `worker_environment_drift` was mapped to `_precondition_
browser`, whose checks (cdp/playwright/provider/conversation_url/
browser_live) never inspect git hooks or worker isolation — so ANY answer
text cleared a blocker about the worker environment regardless of whether
that environment was actually still broken.

**Fix:** renamed `git_failure_budget` → `git_failure_budget_exhausted`;
`Orchestrator._dispatch_task_push` now distinguishes a protected-branch
push refusal from every other kind by RE-COMPUTING the same
`gateway_protected` membership check `push_exact` itself uses (never by
string-matching the exception), emitting `push_refused_protected`
specifically for it; `worker_environment_drift` now maps to a dedicated
`_precondition_worker_environment_drift`, which reuses `validate_workers_
root` + `WorkerRepoManager` + `verify_worker_isolation` against a real
throwaway probe repo — the SAME primitives `doctor`'s `worker_isolation`
check is built on. Verified exhaustively rather than by a hand-maintained
list of expected codes (the failure mode that let the first two go
unnoticed): `test_every_precondition_key_matches_a_real_emitted_code`
AST-walks `orchestrator.py` for every string literal that can appear as a
`_to_needs_user(code=...)` argument — including from inside a conditional
expression — and asserts every precondition key matches one.

**file:line** — `autoloop/cli.py` (`_RESOLUTION_PRECONDITIONS`,
`_precondition_worker_environment_drift`); `autoloop/orchestrator.py`
(`_dispatch_task_push`'s `is_protected_refusal` computation).

**Verification check:**
```bash
rg -n '"git_failure_budget"' autoloop/cli.py                 # expect: no match (only the _exhausted form)
rg -n 'push_refused_protected' autoloop/orchestrator.py       # expect: emitted, not just mapped
pytest autoloop/tests/test_m1_hardening.py -k "precondition or push_refused_protected" -q
```

**Suggested fix:** none outstanding — closed. Any future blocker `code=`
addition must add or deliberately omit a `_RESOLUTION_PRECONDITIONS` entry;
the exhaustiveness test only catches a key with no matching code, not a
code that should have gained one and didn't (that half is a design review
question, not something the AST walk can decide for you).

**Addendum, same day — the predicted gap happened within hours, in this same
pass, in two rounds.** The paragraph above names the exact failure mode
`test_every_precondition_key_matches_a_real_emitted_code` cannot catch: a
new environmental/security `code=` gets emitted but never gains a
precondition entry. S24/S25's own fixes did exactly that — three new
`loop_fatal` parks (`primary_checkout_dirty`, `checkout_escape_detected`,
`worker_isolation_violation`) were added to `orchestrator.py` with no
corresponding `_RESOLUTION_PRECONDITIONS` key, so for a window within this
same changeset an operator's answer text alone would have cleared a
still-dirty primary checkout, a live escape-detector finding, or a still-
broken worker isolation — precisely the class of bug S26 was opened to
close. Caught in a first round of review, not by the exhaustiveness test
(as predicted), and fixed: `worker_isolation_violation` reuses the existing
`_precondition_worker_environment_drift`; `primary_checkout_dirty` got
`_precondition_checkout_clean` (re-runs `GitGateway.is_dirty()`);
`checkout_escape_detected` was ALSO mapped to `_precondition_checkout_clean`
at this point — which was itself wrong, caught in a SECOND round of review:
`escape_detector`'s snapshot deliberately covers untracked and ignored
paths, a strict superset of `is_dirty()`'s visibility, so an escape that
touches only an ignored path — Autoloop's own state directory is gitignored
in production, so an agent tampering with `.autoloop/state.json` to cover
its tracks is exactly this case — would have cleared the blocker on a
recheck that verified nothing. Fixed for real: `checkout_escape_detected`
now has its own `_precondition_checkout_escape_detected`, which refuses
UNCONDITIONALLY (mirroring `_precondition_protected`'s shape) rather than
attempting any automated recheck — this detection needs a human to
actually read the reported paths. A new, deliberately CURATED (not
AST-derived) reverse-direction test,
`test_security_and_environment_codes_all_have_a_precondition`, pins the
full set of codes that must never resolve on text alone — added specifically
because the forward-only exhaustiveness test cannot express this
constraint (and would not have caught either round of this gap). **Lesson
recorded, not just fixed:** any new `loop_fatal` / security-shaped blocker
code added anywhere in this codebase needs its precondition DESIGNED, not
just present, in the SAME change that adds it — "is this recheck actually
re-verifying the condition that fired the park, or just checking something
correlated with it" is exactly what the second round caught, and it is a
design question no automated test in this codebase can fully answer.

### S35 — The merge sweep auto-resolves one conflict shape in four documentation trackers — INFO — OPEN (deliberate, narrow, accepted)

**Location:** `autoloop/note_merge.py` (`resolve_note_append`, the whole
decision, plus `combine_conflicted_notes`, the shared read/resolve/write/commit
plumbing), `autoloop/auto_merge.py` (`AutoMerger._resolve_note_conflicts`,
reached only from `_merge`'s conflict branch),
`autoloop/orchestrator.py` (`_note_conflict_resolver`, reached only from
`_carry_reviewed_candidate_past`'s merge — the SECOND call site since notes-04,
2026-08-23), `autoloop/git_gateway.py` (`merge_stage_blob` / `add_paths` /
`commit_staged`, the three primitives it uses, and `merge_foreign_commit`'s
`resolve_conflicts` hook plus `_finish_resolved_merge`, which verify the second
site's result).

**Severity:** INFO. No runtime behaviour and no control is reached: all four
files are documentation, and nothing reads them at run time. It is recorded
because the loop now writes a merge commit that no human approved the CONTENT
of, which is a class of action worth a tracker entry even when the content is
prose.

**What it is.** Since 2026-08-19 (docs-01), a `git merge` that conflicts ONLY
in the append-only change-note section of a tracker in
`note_merge.NOTE_TRACKERS` is resolved by the loop instead of aborted: the two
branches' appended lines are concatenated and the merge is committed. This
replaced `merge=union` on the same paths (S34, resolved below), which was
strictly worse — it disabled conflict detection for the WHOLE file.

**Scope widened from two paths to four on 2026-08-23 (notes-03).** It covered
`docs/SUMMARY.md` and `docs/TESTS.md`; it now also covers `docs/SECURITY.md`
(this file) and `docs/COMMON_ERRORS.md`. This is a deliberate widening of the
blast radius, recorded rather than quietly taken, and it was made in the only
order that keeps the bound below true: **each of those two files was first
given exactly one `NOTES_MARKER`-delimited append-only section at its end, and
only then added to the list.** A path in `NOTE_TRACKERS` whose file has no such
region would have this resolver reasoning about ordinary prose — which is why
`CLAUDE.md` and `docs/SCHEMA.md`, trackers a task may equally write
(`tasks.TRACKER_PATHS`), are still NOT in it and still conflict normally. What
prompted it: a conflict in ONE uncovered path refuses the WHOLE merge, so on
2026-08-22 bind-01, split-01 and dash-17 were each refused over documentation
conflicts that were all the same append-at-the-end shape, and the two covered
files bought nothing.

**Why it is accepted, and what bounds it.**
- **Four literal paths**, `note_merge.NOTE_TRACKERS` — no glob, no prefix match,
  no source file. A conflict on any other path in the same merge refuses the
  whole merge rather than resolving the trackers partially.
- **Only the region below the marker is ever combined.** Everything above it —
  in this file, every finding, every verification check and the whole change
  log — is git's own 3-way merge, and a conflict there refuses the merge.
- **The base must survive byte-for-byte.** Each side's change-note section must
  hold the merge base's section text as a literal PREFIX, so an edited,
  deleted, rewritten or reordered pre-existing line disqualifies that side.
  Nothing already in the ledger can be changed by an auto-resolution.
- **Everything outside the section is git's own merge, untouched.** The
  resolver keeps git's half-merged text for the part of the file above the
  marker and refuses outright if git left a conflict marker there — so a
  concurrent edit to tracker PROSE still stops the sweep.
- **Nothing is written until every conflicted path has resolved**, and the
  result is then verified before it counts — `auto_merge._verify_merge` (head
  moved, contains both parents, tree clean) before anything is pushed on the
  task-into-base side, and `git_gateway._finish_resolved_merge` (head moved,
  contains the branch tip AND the merged commit, tree clean) on the base-refresh
  side. Neither direction treats "the resolver returned True" as evidence; a
  resolution that does not verify is a FAILED merge, and the base refresh then
  parks rather than re-pointing the record.
- **The base refresh resolves nothing a reviewed candidate has not already
  survived.** Its five preconditions run FIRST and are untouched: a worker with
  uncommitted changes, or a branch tip that no longer contains the reviewed
  candidate, is refused before any merge is attempted. Nothing is ever re-based:
  every reviewed commit keeps its exact sha.
- **Every decision is in the transcript** — `auto_merge_notes_resolved` /
  `auto_merge_notes_refused` for the merge sweep, `execution_base_notes_resolved`
  / `execution_base_notes_refused` for the base refresh, each naming the paths or
  the reason — and both merge commits' own messages say the notes were combined
  automatically.

**The residual, stated rather than hidden.** A defect in `resolve_note_append`
could combine tracker content in a case a human should have seen, without
stopping the sweep. That is bounded to the four paths above, to the region
below each file's marker, and to the strict prefix precondition; every tracker
edit remains visible in `commit_range_paths` and in the reviewed diff — this
changes what git resolves automatically, not what a reviewer can see. Since
2026-08-23 the residual reaches this file's own change-note section: a note
appended below its marker by two branches can be combined without a human. No
finding, control or verification check lives there — they are all prose above
the marker, where a conflict still stops the sweep. `CLAUDE.md` and
`docs/SCHEMA.md` are deliberately NOT in scope and still conflict normally.

Since notes-04 (2026-08-23) that residual has a second reachable site — the
base refresh — and the honest statement of what changed is *where* the resolver
runs, not *what* it may combine: the rule, the four paths and the prefix
precondition are one implementation shared by both, so a widening is still a
single diff in a single frozenset. The one thing the second site adds is a
merge commit written into a WORKER repository without a human, which is a
private branch that no reviewer has approved the content of and that nothing
publishes on its own — the ordinary review and push path still runs afterwards
and still shows the tracker edit.

**Verification check:**
```bash
# The scope, and that it is still a literal list of four documentation paths:
rg -n 'NOTE_TRACKERS' autoloop/note_merge.py autoloop/auto_merge.py
# BOTH call sites, and that neither grew a second resolver of its own. Expect
# exactly 5 lines: 3 in `note_merge.py` (the two definitions and the one
# internal call), plus ONE call each in `auto_merge.py` (`_resolve_note_
# conflicts`) and `orchestrator.py` (`_note_conflict_resolver`). A sixth line,
# or any `resolve_note_append(` call outside `note_merge.py`, is the drift this
# finding's bound depends on not happening — two implementations of this rule
# would mean a merge resolved in one direction and refused in the other:
rg -n 'combine_conflicted_notes\(|resolve_note_append\(' autoloop --glob '!tests/*'
# Each of those files must carry the CHANGE-NOTES marker EXACTLY once (expect
# `1` per file) — a file with none is a path the resolver was granted without an
# append-only region, a file with two has auto-resolution silently off:
rg -c '<!--[ ]CHANGE-NOTES:' docs/SUMMARY.md docs/TESTS.md docs/SECURITY.md docs/COMMON_ERRORS.md
# No merge attribute may come back alongside it (must print nothing):
grep -v '^\s*#' .gitattributes | grep -v '^\s*$'
```
Pinned by `autoloop/tests/test_docs_merge.py` — in particular
`test_the_resolver_is_scoped_to_exactly_the_declared_trackers`,
`test_every_shipped_tracker_ends_with_an_append_only_section`,
`test_a_tracker_without_the_marker_is_refused_rather_than_combined`,
`test_a_concurrent_edit_to_tracker_prose_still_conflicts`,
`test_a_concurrent_edit_to_an_existing_note_line_still_conflicts`,
`test_one_refusing_tracker_stops_the_whole_merge_even_if_the_others_resolved`
and `test_a_real_conflict_in_a_source_file_still_stops_the_sweep`. The second
call site is pinned by `autoloop/tests/test_base_refresh_notes.py` — in
particular `test_a_conflict_in_tracker_prose_still_parks`,
`test_a_conflict_in_a_source_file_still_parks_with_the_existing_message`,
`test_a_source_conflict_alongside_resolvable_trackers_resolves_nothing`,
`test_a_dirty_worker_is_not_merged_over_even_when_only_notes_conflict`,
`test_a_branch_tip_that_lost_the_candidate_is_not_merged_into` and
`test_a_resolver_that_claims_success_without_committing_is_not_believed`.

**Suggested fix if it ever needs one:** move the append-only note ledger into
its own per-task file and drop the resolver — the trackers then conflict
normally again in every case. Not done now because a task's write scope is a
list of exact paths (`tasks.unauthorized_paths`), so a new ledger file cannot
be written by the tasks that would need to append to it, and the trackers stop
being readable as one document. Adding a path to `NOTE_TRACKERS` is not a fix
for anything and is never the first step: every entry there is a file the loop
may merge without a human, and a path may only be added AFTER that file carries
exactly one marker-delimited append-only section — section first, list second,
in one reviewed commit that shows both.

---

### S36 — `profile` reads a transcript that holds whole packets and whole reviewer replies — INFO — OPEN (bounded by design, accepted)

**Location:** `autoloop/cli.py` (`_cmd_profile`), `autoloop/transcript.py`
(`read_records`, `profile_stages`, `build_profile`, `render_profile`).

**Severity:** INFO. No new data is written and no new reader is granted: the
transcript already sits in `state_dir` at the operator's own permissions, and
this command reads a file the operator can already `cat`. It is recorded
because prof-01 (2026-08-20) added the FIRST command whose whole job is to read
that file and print from it, which makes what it prints a control rather than
an accident.

**What is in the file.** `transcript.jsonl` carries `request_submitted.data.
prompt` (the complete review packet, including the diff) and
`response_received.data.raw` (the complete reviewer reply). Both are logged
verbatim, deliberately — the transcript is the audit log. Anything that reads
it and emits somewhere else is a disclosure surface.

**What bounds it today.** `profile` prints AGGREGATES only: per-stage count,
median, mean, p90 and total, plus stage names and fixed explanatory prose. It
never renders a record body, a `data` value, a `request_id`, a sha or a task
id. The bound is the READ/RENDER SPLIT, not a habit of not printing things:
`read_records` and `profile_stages` see whole records — they must, the
durations and the timestamps live in them — and `build_profile` reduces the
read to a `TranscriptProfile` (two counts, one flag, per-stage `Stats` of
floats beside static `Stage` labels) BEFORE anything renders.
`render_profile(path, profile)` takes that and nothing else, so the layer that
writes to stdout holds no record dict at all. (Corrected 2026-08-20, same
task: the first version of this finding claimed the structural property while
`render_profile` still received the whole `TranscriptRead`, whose records carry
the packet and the reply. The claim was true of what the function *used*; it
was not true of what it *held*, and a disclosure bound has to be the second
one.)

**`--transcript FILE` does not widen this.** The flag chooses WHICH file the
reader opens — an archived or rotated transcript instead of the configured one
— and the structural bound above is input-independent: whatever the file
contains, the renderer still receives only that aggregate. So pointing it at a
file that is not a transcript prints "no usable records", not the file. It grants no read the operator does not already have (the process
runs as them, and `open()` is subject to the same permissions as `cat`), and it
is deliberately a PATH argument, not a glob or a directory walk — one open, one
report. Pinned by
`test_transcript_override_of_a_non_transcript_prints_no_content`.

**The residual:** timing itself is information. A reader of the output learns
how many review rounds ran and how long each stage took. That is the point of
the command and is not treated as sensitive here.

**Do not** add a `--raw`, `--events`, `--tail` or `--request <id>` flag that
prints record bodies. If per-request detail is ever genuinely needed, print
identifiers and durations only, and re-review this entry first.

**Verification check:**
```bash
# Read the render layer's WHOLE input, since that is the bound. Expect exactly
#   def render_profile(path: Path, profile: TranscriptProfile) -> str:
# — no `TranscriptRead`, no `records`, no `Sequence[dict]` parameter:
rg -n 'def render_profile' -A 1 autoloop/transcript.py
# And the command must reduce before it renders. EXPECT EXACTLY ONE LINE:
#   print(render_profile(path, build_profile(read)))
rg -n 'render_profile\(' autoloop/cli.py
# And the command must not have grown a body-printing flag. Read the flag list
# ITSELF rather than grepping a fixed window for forbidden words: a window
# sized to today's block stops covering the flags a later one adds, and the
# words themselves appear innocently in `_cmd_profile`'s own docstring (it
# names `response_received.data.raw` to explain what it must never print).
# EXPECT EXACTLY ONE LINE: `"--transcript",`
rg -n 'if name == "profile"' -A 8 autoloop/cli.py | rg '"--'
```
Pinned by
`autoloop/tests/test_profile.py::test_profile_prints_only_aggregates_never_a_record_body`,
which writes a transcript containing a marked packet body and a marked reviewer
reply and asserts neither reaches stdout, and — for the structural half, which
is the part that was wrong before —
`test_the_render_layer_receives_no_record_and_so_cannot_print_one`, which walks
every value reachable from the `TranscriptProfile` and requires each to be a
count, a flag, a float or a static stage label: no `dict`, no `TranscriptRead`.

**Suggested fix if the bound ever breaks:** keep the read/render split — the
tolerant reader may see everything, the renderer may only receive numbers — and
extend the test above with the new marker rather than reviewing the output by
eye.

---

### S37 — The reviewer transport now holds a LIVE agent session instead of a one-shot process — INFO — OPEN (bounded by this client's replies, accepted)

**Location:** `autoloop/codex/app_server.py` (`SubprocessAppServer`,
`AppServerClient._answer_server_request`, `_read_message`),
`autoloop/codex/app_server_conversation.py`, `autoloop/conversation.py`
(`_codex_app_server_factory`).

**Severity:** INFO. No new authority is granted to anything: the reviewer's role
is unchanged (it answers a self-contained prompt with a directive that the
existing contract and policy layers still gate), the process runs at the
operator's own permissions exactly as `codex exec` did, and no new file, socket
or credential is touched. It is recorded because codex-01 (2026-08-22) changed
the SHAPE of the reviewer from a process that exits after each turn into a live
agent session that stays open, asks this client questions, and could ask to run
a command.

**What actually changed, in security terms.**

1. *A long-lived child process.* `codex app-server` is spawned once by `attach()`
   and lives until `close()`, where `codex exec` was one process per turn. It is
   started from an argv LIST — never a shell — and nothing model-authored can
   reach that list at all: prompts travel as JSON on stdin, which also removes
   the 700 KB argv ceiling `codex/conversation.py` had to defend with a size
   refusal.
2. *The server can ask this client to do things.* `ServerRequest` in the
   committed protocol includes `applyPatchApproval`, `execCommandApproval`,
   `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`,
   `item/tool/call` and others. This is the real new surface, and it is answered
   rather than ignored: the two whose response type the reference settles are
   answered `{"decision": "abort"}`, and every other server→client request gets a
   JSON-RPC error naming the refusal. **Silence is the one forbidden answer** —
   the server blocks on its own request, the turn dies at the timeout, and
   nothing in the transcript says why.
3. *`cwd` outside the checkout, unchanged.* Same containment `codex/conversation.
   py` states and for the same reason: the prompt is self-contained, so the
   reviewer needs no filesystem, and a containment that does not name a sandbox
   flag still holds when the flag is renamed. **This is NOT a sandbox claim.** No
   preset is selected, named or enforced; `codex.sandbox_args` is a `codex_cli`
   setting and this transport does not read it. Enforcing one is codex-03.
4. *stderr is `DEVNULL`.* Two reasons, and both are controls: an undrained
   `stderr=PIPE` deadlocks a chatty child, and this transport classifies failures
   from protocol fields, so there is no free-form text blob to scan even by
   accident.

**What reaches the transcript.** Bounded and secret-free by construction, the
same rule `codex/quota.py`'s `failure_digest` follows: a protocol failure logs
`{code, error_type, status, message}` with the message truncated to 400
characters, and a non-JSON line on stdout logs at most 200 characters. Never the
params — those are the review packet — and never the environment, which carries
the child's auth.

**The residual.** A live session accumulates history for the life of the process,
so one thread holds every review packet of that run rather than each turn
starting clean. That is the FEATURE (`supports_chunked_delivery` depends on it)
and the exposure is unchanged in kind — the same reviewer account already
received every one of those packets, one process at a time — but it is a longer
retention window inside the agent's own context, and codex-03's `thread/resume`
would extend it across restarts. Re-review this entry when it does.

**Verification check:**
```bash
# No shell anywhere in the transport, and stderr must stay DEVNULL.
# EXPECT: a `list(self._command)` argv, `stderr=subprocess.DEVNULL`, no shell=True:
rg -n 'Popen|shell=True|stderr=' autoloop/codex/app_server.py
# Every server->client request must be ANSWERED. Expect the abort branch and the
# error branch, and no `return` that leaves one unanswered:
rg -n '_answer_server_request' -A 30 autoloop/codex/app_server.py
# And the reviewer must still run outside the checkout — expect `Path.home()`
# as the default cwd:
rg -n 'Path.home\(\)' autoloop/codex/app_server.py
```
Pinned by
`autoloop/tests/test_codex_app_server.py::test_every_approval_the_server_asks_for_is_answered_abort`
and `::test_an_unserviceable_server_request_is_refused_never_ignored`, which
drive a fake server that asks mid-turn and assert on the JSON this client wrote
back, plus
`::test_the_real_transport_never_uses_a_shell_and_confines_the_working_dir`.

**Suggested fix if the bound ever breaks:** if a future task needs the reviewer
to run a command, do not widen `_answer_server_request` — add a named capability
with its own config gate and its own finding here, so "the reviewer may execute"
is a decision somebody made rather than a default that arrived with a protocol
upgrade.

---

### S39 — A failed `codex exec` now records BOTH its streams to the transcript, and the loop's own prompt is no longer evidence — INFO — OPEN (bounded, accepted)

**Location:** `autoloop/codex/quota.py` (`classify`, `strip_echoed_prompt`,
`failure_digest`), `autoloop/codex/conversation.py` (`CodexConversation.submit`),
`autoloop/conversation.py` (`_transcript_log`).

**Severity:** INFO. Two changes are recorded here, one that REMOVES an
availability hazard and one that narrowly widens what is written to a file the
operator already owns. Neither grants a new reader, opens a socket or touches a
credential.

**1. The availability half — a self-inflicted denial of service, now closed.**
`is_quota_exhausted` searched `f"{stdout}\n{stderr}".lower()`, and `codex exec`
ECHOES THE WHOLE PROMPT BACK ON STDERR (measured: a 180,024-byte packet came
back verbatim). The pattern list held the bare substrings `"429"`, `"quota"` and
`"rate limit"`, so every word of the review packet was inside the string being
matched. On 2026-08-22 a packet quoting `docs/autoloop.md:4295` — a LINE NUMBER
— turned an unrelated non-zero exit into `QuotaExhaustedError`, which is
loop_fatal with no retry path; the loop parked twice against an account 4%
through its weekly window (~25 minutes down, an hour of investigation). Content
the loop SENT could stop the loop. It is worth naming as a security property
rather than a bug: the review packet is assembled from repository text and agent
output, so the input that could trigger it is not fully under an operator's
control, and several task descriptions in this repository discuss quotas and
rate limits by name. Closed by making the prompt a REQUIRED argument to
`classify`, dropping every output LINE the prompt accounts for
(`codex_owned_text`) before anything is matched, matching only WITHIN a line so
a wording cannot be assembled across a join that was never printed
(`_folded_lines`), and counting a surviving marker only when the prompt does not
account for it either. Which one carries the property is worth stating exactly:
SUPPRESSION does, alone — an echoed line's squeeze is contained in the prompt's,
so any marker folding into it squeezes into the prompt and is refused. The other
two are not redundant (the line bound stops the comparison manufacturing a
wording contiguous in neither side; the haystack bound makes `matched_pattern`
readable as codex's own output) but neither holds it alone. The comparisons ignore
whitespace and punctuation: a literal substring test was tried first and refused
on review, because a REFLOWED echo can carry a marker the prompt does not
contain as an exact string — prompt text `quota` + newline + `exceeded`, printed
back as `quota exceeded`, is the worked example. Matching folds (whitespace and
punctuation to single spaces) and suppression squeezes (folds, then removes the
spaces); squeezing is monotone over substring containment, so anything that can
be MATCHED in a stream is necessarily SUPPRESSED when applied to the prompt.

**2. The disclosure half — `stdout_tail` is new in the transcript.**
`failure_digest`'s docstring promised the record was "the return code plus a
bounded stderr TAIL … never the prompt, stdout, argv or environment". It now
also carries a bounded excerpt of STDOUT, because a `codex exec` that dies
before writing to stderr puts its complaint there and round 1 of quota-01 was
refused for classifying such a failure while recording nothing about it. What
bounds the amendment: both excerpts are echo-stripped (exact occurrences of the
prompt are excised first) and each is capped at `STDERR_TAIL_CHARS` (400); argv
and the environment are still never included; and the reviewer's reply already
reaches this same file in FULL under `response_received.data.raw`, so a bounded
excerpt of the same stream is not a new class of content in the transcript. See
S36 for what reads that file and the read/render split that bounds it.

**The residual.** Echo stripping is best-effort and is deliberately NOT the
classification bound: a codex build that reflows or re-wraps its echo defeats
the strip, and up to 400 characters of text the loop itself wrote could then
appear in a `codex_invocation_failed` record. That is bounded, is already
operator-owned content, and cannot affect routing — routing is decided by the
guard in `classify`, which does not depend on recognising the echo at all.

**Verification check:**
```bash
# The guard must be a REQUIRED argument. Expect `prompt: str,` with NO default
# on all three, and the prompt consulted before any marker counts — expect the
# `sent_squeezed` test inside `first_own_match` and the line filter above it:
rg -n 'def classify|def is_quota_exhausted|def codex_owned_text' -A 6 autoloop/codex/quota.py
rg -n 'sent_squeezed|codex_owned_text\(' autoloop/codex/quota.py
# The adapter must guard with the prompt it SENT (post-attachment), and the
# factory must pass a real logger. Expect one `classify(` with `prompt` and one
# `log=_transcript_log(config)` per codex factory:
rg -n 'classify\(' -A 8 autoloop/codex/conversation.py
rg -n '_transcript_log\(config\)' autoloop/conversation.py
# The digest must still carry no argv and no environment. Read its KEYS rather
# than grepping the function for forbidden words — its own docstring names argv
# and the environment in order to say it excludes them, so a word grep hits.
# `failure_digest` builds the only string-keyed literal in this module; EXPECT
# exactly request_id, returncode, classification, matched_pattern, stderr_tail,
# stdout_tail, stderr_chars, stdout_chars, prompt_echo_chars, prompt_guard — and
# nothing that says argv, command, env or the prompt ITSELF (`prompt_guard` is
# the word "active" or "inert", never any of the text). Three further keys are
# added conditionally just below it (`suppressed_patterns`, `echo_lines_dropped`,
# `note`) and are all derived here:
rg -n '^\s+"[a-z_]+":|digest\["' autoloop/codex/quota.py
```
Pinned by
`autoloop/tests/test_codex_provider.py::test_an_echoed_prompt_cannot_declare_the_allowance_spent`,
`::test_the_guard_survives_framing_the_echo_is_wrapped_in`,
`::test_classification_cannot_be_called_without_the_prompt_it_sent` (the
signature check that stops a future call site disabling the guard by omission)
and `::test_an_echoed_review_packet_never_parks_the_loop`, which drives a real
`CodexConversation` through the orchestrator. The reflow bound is pinned by
`::test_no_reshaping_of_the_echo_can_declare_the_allowance_spent` and
`::test_no_reshaped_echo_parks_the_loop_end_to_end`, each parametrized over 15
ways a CLI can print text back without changing a word of it, plus
`::test_anything_that_can_match_is_first_suppressible` for the fold/squeeze
implication the guard rests on. The other direction — codex's OWN exhaustion
message still parking the loop while the packet in flight quotes exhaustion
wordings — is `::test_a_genuine_exhaustion_parks_even_after_a_marker_laden_echo`.
An absent prompt leaves the guard inert; that is recorded as
`prompt_guard: inert` rather than inferred, and pinned by
`::test_an_absent_prompt_leaves_the_guard_inert_and_records_that`.

**Suggested fix if the bound ever breaks:** if the guard ever has to be relaxed,
do not widen the haystack — narrow the transport instead (a mode that does not
echo, or a structured error like the app-server's `protocol_errors.py`, which
classifies from named fields and never scans text at all). And do not add a
field to the digest that is not bounded and echo-stripped.

---

### S40 — A file inside the observed checkout can now make the dashboard `os.execv` itself — INFO — OPEN (bounded, accepted)

**Location:** `autoloop/dashboard.py` (`upgrade_decision`, `relaunch_argv`,
`_upgrade_at_boundary`, `Handler.handle`).

**Severity:** INFO. The dashboard was a read-only tracker that also serves two
narrow POST paths (S28, S32); since loop-03 it also **replaces its own process
image** when `.autoloop/pending_upgrade.json` says the checkout moved under it.
That file is inside the tree a write-capable agent runs in, so it is worth
recording plainly: **a file an agent could write decides whether this process
execs.** It does not decide WHAT it execs, and that is the whole bound.

**What is actually reachable.** The record can make the dashboard (a) do
nothing, (b) print a sentence and render a banner, or (c) restart itself into
the code already on disk. It cannot make it run anything else:

- **The argv is built from `sys.executable` and `sys.argv[1:]` only**, with the
  module name derived from `__main__.__spec__`. No field of the record —
  `repo_root`, `paths`, `task_id`, `candidate_sha`, `base_sha` — is
  interpolated, appended or consulted while building it. A launch shape that
  cannot be derived (a script run by path) REFUSES rather than guessing, which
  is also why the loop's hard-coded `-m autoloop` rebuild was not copied: under
  `python -m autoloop.dashboard --repo X` it would have started a LOOP RUN — a
  write-capable, git-pushing process — from a read-only tracker.
- **`repo_root` is compared, never used as a path to act on.** A record naming
  another checkout is refused (`not_this_tree`); it is not resolved, entered, or
  passed anywhere.
- **The replacement runs the tree this process already imports from**, proven by
  a preflight subprocess before anything is replaced. Writing the record cannot
  substitute a different tree — only moving `autoloop/` on disk can, which is
  the same authority as editing the running program.
- **`sys.argv[1:]` is the operator's own command line**, inherited from the
  process being replaced. The record cannot add to it.

**What the dashboard does NOT do to the record.** It never calls `save`,
`clear` or a settle. Two reasons, both load-bearing: the record is the LOOP's
one-shot, so consuming it would silently stop the loop re-execing (loop-02's
tests never run a dashboard, so nothing there would catch it); and any write
into `.autoloop/` mid-round is a diff `escape_detector` cannot tell from an
agent writing where it may not, which parks the loop loop-fatal. Attempt
outcomes are held in memory and printed to the dashboard's own terminal.

**Availability, both directions.** A tree that does not import leaves the old
image serving and says so on the page — a dashboard that exec'd into a broken
tree would be gone, with nothing left to report it. An `execv` that raises, and
anything else on its way up, clears the armed flag in a `finally`, because a
flag left set makes the port refuse every connection for good. One attempt per
sha, so a failure is not retried on each 2s poll.

**Verification check:**
```bash
# The argv must be built from the interpreter and this process's own command
# line, and from nothing in the record. EXPECT one construction, naming only
# sys.executable / "-m" / a derived module name / sys.argv[1:]:
rg -n 'def relaunch_argv' -A 14 autoloop/dashboard.py
# And EXPECT no record field on any line that builds argv. Prose lines may
# match (the docstring names the fields in order to say it excludes them), so
# read the hits — any line that is CODE is the bug:
rg -n 'argv.*(record|repo_root|task_id|candidate_sha|decision\[)' autoloop/dashboard.py
# The record must never be written by this module. EXPECT only `.load()`:
rg -n 'UpgradeStore\(' -A 2 autoloop/dashboard.py
rg -n '\.save\(|\.clear\(' autoloop/dashboard.py
```
Pinned by
`autoloop/tests/test_dashboard.py::test_nothing_from_the_record_reaches_the_relaunch_command`
(a record carrying `--evil-task`, `--evil-path` and a shell metacharacter, driven
through a real attempt),
`::test_a_launch_shape_that_cannot_be_derived_refuses_rather_than_guesses`,
`::test_an_undeterminable_launch_shape_is_never_exec_ed`,
`::test_the_dashboard_never_writes_the_signal_it_reads` (byte-identical record
plus an unchanged checkout snapshot, driven through a FAILED attempt — settling
is exactly what the loop does at that point),
`::test_a_merge_in_another_checkout_is_not_a_reason_to_restart`,
`::test_a_tree_that_does_not_import_leaves_the_process_serving`,
`::test_an_exec_that_is_refused_leaves_the_process_serving` and
`::test_an_exec_that_raises_something_else_still_unlocks_the_port`. The
whole file's `no_process_replacement` fixture makes "not exec'ed" a real
assertion rather than an absence.

**Suggested fix if the bound ever breaks:** do not add a record field to the
argv, and do not add a fallback launch shape — refusing is the correct answer to
an underivable one. If the marker ever needs to carry more authority than "the
checkout moved", move it out of the observed checkout (as `port-01` moved
`state_dir`) rather than trusting it further where it is.

---

## Verified strengths (do not regress)

These were checked in the 2026-05-24 sweep and are working controls. A PR that weakens one is a security regression.

- **SQL is fully parameterized** via asyncpg `$1`/`$2`. The only f-string queries in application code interpolate a loop integer and a module constant, not user data (`services/search_service.py:62,103,216`). User search terms go through bound params.
- **Authorization / ownership is consistently enforced** on user-owned resources:
  - Books: `book_service.get_document(pool, doc_id, user_id)` filters by `user_id` (`book_service.py:532`), called by every `/books/{doc_id}/...` handler.
  - Chat: `_require_session` rejects sessions not owned by the caller (`routers/chat.py:366`).
  - Reading: `delete_selection(pool, selection_id, user_id)` is user-scoped (`routers/reading.py:356`).
  - SRS: card lookup filters `card_id AND user_id` (`services/review_service.py:174`) — **verified, no IDOR**.
  - Word lists: every `/word-lists/{id}` handler resolves the row through `word_list_service._load_list_row`. Since migration 037 that filter is `list_id = $1 AND (user_id = $2::uuid OR is_system)` — **deliberately widened** so built-in system lists are readable by everyone. Private lists are unaffected: the migration's CHECK (`(is_system AND user_id IS NULL) OR (NOT is_system AND user_id IS NOT NULL)`) makes a system list with an owner unrepresentable, so the `OR` cannot return someone else's row. **Writes were not widened** — `delete_list` still filters on `user_id` alone and therefore refuses a system list, and `mark_unknown_as_learning` suppresses its late-binding `UPDATE word_list_items` on shared rows. Misses return **404, not 403**, so list ids can't be enumerated. Covered by the original parametrized cross-user test over read/export/delete/mark-learning and the cross-user no-knowledge-row test, **re-pinned against a database containing system rows**, plus new tests that the CHECK rejects both hybrid states, that a private list never appears in another user's index, that a system list cannot be deleted or created through any public API, and that two users marking the same system list keep separate progress (`tests/test_word_lists.py`). Both guards are mutation-checked: reverting the late-binding suppression or widening the index filter to all rows fails the suite.
- **Privilege escalation is blocked at the settings write.** `update_preferences` only merges keys present in `DEFAULTS` (`services/settings_service.py:223`); `is_admin` is not in `DEFAULTS` (`settings_service.py:15`), so a user cannot grant themselves admin via `PUT /settings/preferences`. Enforced by `tests/test_settings.py`. **Do not widen this to a blind `{**current, **updates}` merge.**
- **Passwords** use bcrypt with per-password salt (`core/security.py:19`). **Login does not leak account existence** — generic "Invalid email or password" (`auth_service.py:37`).
- **CORS** is an explicit allow-list, not `*`, and credentials mode is not enabled; auth is Bearer-header (not cookies), so **CSRF does not apply** to the current design. (Revisit if S3's cookie option is taken.)
- **Secrets come from env**, never hardcoded; `SECRET_KEY` is required at boot (`core/security.py:15`). Only `.env.example` is tracked in git — the real `.env` is not committed.
- **No frontend XSS sinks.** No `dangerouslySetInnerHTML`, `innerHTML`, `document.write`, or `eval` in `frontend/src`; React auto-escaping covers user-rendered fields (filenames, notes). Keep it that way (ties to S8).
- **Notifications SSE** requires `get_current_user` (`routers/notifications.py:57`) and scopes rows to the user. The `WHERE user_id = $1::uuid` moved verbatim into `services/notification_service.py:30` (`fetch_unseen`) in the arch-05 SQL extraction — the router passes only `current_user["user_id"]`, so there is still no caller-supplied user id on this path. **Verification check:** `rg -n 'user_id = \$1::uuid' lexy-app/backend/services/notification_service.py` and `rg -n 'get_current_user' lexy-app/backend/routers/notifications.py`.
- **Content-request subprocess** uses `create_subprocess_exec` with fixed args (`routers/content_requests.py:29`) — no shell, no command injection. Unchanged by the arch-05 extraction: the subprocess spawn deliberately stayed in the router, so the service layer never spawns a process.
- **Autoloop git/subprocess + browser surface (added 2026-07-29; hardened same day by Phase 2).** The Fable↔ChatGPT loop (`autoloop/`, `docs/AUTOLOOP.md`) runs git only via `subprocess.run(["git", ...])` — argv list, no `shell=True` — and only after `PolicyEngine.validate_git_command` passes a **whitelist** (subcommand + per-subcommand flags, `autoloop/policy.py`). Force pushes and destructive subcommands (`reset`, `clean`, `rebase`, `checkout`, …) are denied *before* any subprocess spawns, and no config knob can enable them. LLM-controlled strings (commit message, staged paths — they originate from ChatGPT's directive) are passed only as individual argv elements, never interpolated. **Phase 2 adds review-integrity enforcement:** every request is stamped (request_id, head_sha, base_sha, SHA-256 of the report); a `commit`/`push` directive must echo the stamp of the request it answers (`contract.verify_review`) AND the repository HEAD must still equal the approved head at execution time — so a git approval can never be applied to a state ChatGPT did not actually review (replayed, reordered, or post-drift approvals are rejected deterministically). The browser side stores **no credentials**: it connects over CDP to a human-launched, pre-logged-in dedicated Chrome profile and never automates login. **Verification:** `pytest autoloop/tests/test_policy.py autoloop/tests/test_git_gateway.py autoloop/tests/test_contract.py` — includes `test_force_push_has_no_config_escape_hatch`, `test_denied_command_never_reaches_subprocess`, `test_verify_review_rejects_mismatch`, and the orchestrator-level `test_stale_stamp_rejected_and_nothing_committed` / `test_head_moved_since_review_rejected`. Keep the whitelist additive-only; never add `shell=True`, a force-flag knob, or a bypass around `verify_review`. **2026-07-31 (worker-isolation pass):** the `ls-files` entry was widened from `{"-s", "-z", "--"}` to also allow `--others --ignored --exclude-standard` (`autoloop/policy.py`) — every added flag is read-only enumeration (needed by `escape_detector.enumerate_checkout_paths` / `git_gateway.list_untracked_paths` / `list_ignored_paths` to see untracked and ignored paths `git status` alone would hide), consistent with "additive-only" — no new subcommand, no write-shaped flag.
- **Autoloop network observation is observation only, and cannot carry a secret (added 2026-07-31).** The transport now reads the browser's own send traffic to tell "the send failed" from "we didn't see the send" (`autoloop/browser/observation.py`). Three properties keep that from becoming an exposure. (1) **It issues nothing.** The listener is a passive `page.on("response")`/`on("requestfailed")` handler; there is no request-issuing method on the session protocol, so it cannot become a second transport that bypasses the DOM path. (2) **The vocabulary cannot express a secret.** `SendObservation` has exactly three fields — `path`, `status`, `failure`. There is nowhere to put a header, a cookie, an `Authorization` value, a request body or a response body, and `scrub_path` drops the query string before a path is ever recorded, so a diagnostics dump or transcript line cannot leak credentials even by accident. Response bodies are never read; the classifier deliberately works from status codes alone. (3) **It composes with, never replaces, the existing no-credential guarantees** — the session protocol still exposes no cookie/storage accessor, and the profile is still human-logged-in over CDP. **Verification:** `pytest autoloop/tests/test_transport_recovery.py -k "observation or secret or query"` — includes `test_observation_vocabulary_cannot_express_a_secret` (asserts the field set exactly), `test_observed_paths_drop_query_strings`, and `test_rejected_submission_logs_only_path_status_and_failure`, which greps the written transcript for `cookie`/`authorization`/`bearer`. **Do not** add a body/header field to `SendObservation`, and do not "enrich" observations by reading `response.body()` — the message id it would give you is not worth putting message content and auth material into a log.
- **Autoloop conversation rotation cannot edit tracked files or escape its budget (added 2026-07-31).** A rotation is the loop reacting to a fault by writing to the filesystem, so it is gated three ways. `config_writer.assert_untracked` **refuses** to rewrite a git-tracked config and fails closed if git cannot be consulted at all — the heal only ever touches the gitignored `.autoloop/` config, so a browser fault can never produce a repository change. The rewrite is line-surgical and atomic (temp + `os.replace`), and refuses a URL containing a quote, so it cannot corrupt a config that `load_config` would then refuse to parse. `PolicyEngine.check_rotation_budget` caps rotations per run (default 1), and no project URL configured means no rotation at all — the target is configured explicitly and never derived from the conversation URL, so the loop cannot open a chat somewhere the operator did not choose. **Verification:** `pytest autoloop/tests/test_transport_recovery.py -k "config or rotation_cap or project_url"`. **Do not** make `assert_untracked` advisory, and do not add a fallback that derives `project_url` from `conversation_url`.
- **Autoloop worker repos are provably OUTSIDE the checkout, and a task's write scope is authorized before the writer ever starts (added 2026-07-31, S23/S25).** `config.workers_root` is a required, absolute config value; `worker_env.validate_workers_root` refuses one nested beneath the checkout, its `.git` (including a linked worktree's real gitdir), the state dir, or the publisher paths, checked both at real-dispatch construction time (`cli._build_orchestrator`, raises) and by `doctor` (a `fail` check). `Task.approved_paths` is the ONLY thing a write-capable dispatch's post-commit path-ownership check is validated against — never anything the executor/agent itself reports — and a task with none can never be dispatched. **Verification:** `pytest autoloop/tests/test_m1_hardening.py` (52 tests: workers_root refusal/acceptance, escape-detector snapshot diffing over tracked/untracked/ignored/symlink/exec-bit changes, agent-report-cannot-widen-scope, failed-round quarantine, attempt-budget-survives-restart, blocker-precondition exhaustiveness). **Do not** reintroduce a fallback from `config.workers_root` to the old `config.workers_dir` default, and do not union `execution.allowed_paths` with `outcome.changed_paths` for a non-audit task again (see S25) — see S24 for what remains open (detection, not an OS sandbox).
- **The Codex reviewer runs outside the repository and cannot edit it (added 2026-08-01).** Replacing a chat window with a local CLI agent puts a second process next to the checkout, so the containment is explicit. `SubprocessCodexRunner` runs with `cwd` **outside** the repository (`codex.working_dir`, defaulting to `$HOME`) — the reviewer's prompt is self-contained, so it needs no filesystem access at all, and containment that does not depend on a sandbox flag's *name* still holds when the flag is renamed. `doctor` **fails** (not warns) when `working_dir` resolves inside the repo, symlinks included (`_is_within` resolves both sides). `codex.sandbox_args` is deliberately **empty** by default rather than carrying a guessed read-only flag: an unverifiable flag would look like a control without being one, so `doctor` warns while it is unset instead. The prompt reaches the CLI through **argv, never a shell** — same rule as `git_gateway` and `audit/agents.py`; it is model-authored text and `shell=True` near it would be command injection with extra steps. Diagnostics carry `argv_preview` (command + sandbox flags), never the prompt, which is the whole review packet. **Verification:** `pytest autoloop/tests/test_codex_provider.py -k "shell or workdir or binary"` plus `python -m autoloop doctor` (`codex_workdir` must be `ok`). **Do not** point `codex.working_dir` at the checkout, and do not add repository tools to `sandbox_args` — the reviewer reviews the packet it is given, not the tree.
- **A reviewer handover stays attributable, bounded and gated (added 2026-08-01).** The reviewer grants authority: its approval carries the `reviewed{request_id, head_sha, report_sha256}` stamp that authorizes a commit or push. Automatic failover to `conversation.fallback_provider` on an exhausted allowance is therefore recorded, not silent — `provider` on both `PendingRequest` and `LastResponse`, plus a `ProviderSwitch` record and a `provider_switched` transcript entry, so "which reviewer authorized this" is answerable after the fact. It is bounded by `policy.max_provider_switches` (default 1) and gated on `state.last_response is None`: a handover straddling an answered turn is the one shape that could place two reviewers inside a single review round, and the guard is asserted rather than inferred from the phase machine. `active_provider` on state beats config afterwards, so a resumed run cannot quietly return to the exhausted provider. **Verification:** `pytest autoloop/tests/test_codex_provider.py -k "handover or captured or budget or beats_config"`. **Do not** make the switch silent, unbounded, or reachable mid-round.
- **The supervised agent spawn keeps every control the timed one had, and its kill cannot reach anything it did not start (added 2026-08-14, `stall-01`).** Replacing `audit.agent_timeout_seconds` with progress-based stall detection (`autoloop/stall.py`) added a SECOND way a write-capable `claude` subagent is launched — `stall.spawn_supervised` (`subprocess.Popen`) alongside the existing `subprocess.run`. Four properties keep that from being a widening. (1) **Argv, never a shell** — same rule as `git_gateway.py` and `audit/agents.py`; the prompt is model-adjacent text and `shell=True` near it would be command injection with extra steps. (2) **The validation-credential strip applies to both paths.** `ClaudeCliRunner._run_supervised` passes `env=strip_validation_vars()` exactly as the timed path does, so S27's boundary does not depend on which bound is in force. (3) **The kill's blast radius is a group the loop itself created.** `spawn_supervised` passes `start_new_session=True` and `ProcessGroupHandle` signals `os.killpg(os.getpgid(pid), …)`, so the SIGTERM/SIGKILL can only reach descendants of the agent we spawned — never the loop's own process tree — and it falls back to signalling the single process when `getpgid` fails. Signalling the parent alone was the alternative and is worse: orphaned children keep writing into the worker repo after the kill, which corrupts the very partial-work numbers the stall report exists to give a reviewer. (4) **The progress probe reads, never writes**, and reads only through the policy-validated `GitGateway` (`git status --porcelain -z -uall`, `git diff HEAD --stat` — both already-whitelisted flags; **no whitelist change was made for this work**, which is why `--numstat` is parsed out of `--stat` instead of admitted). Agent stdout/stderr go to `tempfile.TemporaryFile` handles, deliberately outside the worker repository, so agent-controlled output can never appear to the probe as filesystem progress. **Verification:** `pytest autoloop/tests/test_stall_detector.py -k "strips or probe or ceiling"` — includes `test_the_supervised_spawn_still_strips_the_validation_credentials`. **Do not** add `shell=True` here, do not drop the strip on the spawn path because the timed path already has it, and do not widen the git whitelist to make the partial-work count exact.
- **Document-package path containment (roadmap A2, added 2026-07-29).** A document package is untrusted input: it is produced by an offline worker and may arrive from another machine, and every path inside it (`CHECKSUMS.txt` entries, `page_images.path_template`, per-element `image_path`/`asset_path`) is attacker-influenced if the package is. `services/document_package/loader.py:resolve_within` is the **single chokepoint** — no file in a package is opened, hashed or recorded unless it resolves inside the package root. It refuses `../` traversal, absolute paths, and symlinked escapes (`Path.resolve()` follows links *before* the containment test, so a symlink pointing outside is caught). The API surface never accepts a path: `POST /api/v1/books/import` takes a package **name**, which is itself passed through `resolve_within` before any I/O (`routers/books.py`), mirroring the S7 pattern of validating at the boundary. **Verification:** `pytest tests/test_document_package.py -k "Containment or traversal"` — parametrized over `../`, `../../etc/passwd`, `pages/../../../etc/passwd`, absolute paths, a real symlink escape, a hostile `path_template`, and a hostile package name. **Do not** replace `resolve_within` with `os.path.join` + a string `startswith` check; that misses symlinks.
- **The decomposition gate adds a denial and no new trust surface (added 2026-08-18, `plan-01`).** an `implement` or `revise` must now carry the plan it authorizes (`contract.Decomposition`) or the task must already hold one, or `policy._check_decomposition` denies it `decomposition_missing`. Three properties keep this from widening anything. (1) **It only ever refuses.** It is a new denial in `authorize_directive`; it admits no directive that was previously refused, and it runs AFTER `implement_enabled` and after `_check_task_reference`, so no phase, quarantine or retirement denial can be answered by supplying a plan instead. (2) **The text is instructions, never authority.** `Task.decomposition` reaches only `implement_executor._agent_prompt` — the same channel `Task.description` (also reviewer-authored, via `plan`) has always used. It is NOT consulted by `effective_approved_paths`, the pre-commit gate or the post-commit ownership check, so no plan can widen what a task may write; `Task.approved_paths` remains the only thing that decides that (S25). (3) **The write path is narrow.** `TaskRegistry.set_decomposition` is called from exactly one place, `orchestrator._dispatch_executor`, from a parsed directive; it refuses blank (so a reshape cannot silently un-approve a task), refuses `completed`/`retired`, and there is no inbox kind or dashboard form that reaches it. **Verification:** `rg -n 'set_decomposition' autoloop/` should show the definition, the single orchestrator call site and tests, and nothing under `inbox.py`/`dashboard.py`; `pytest autoloop/tests/test_policy.py -k decomposition`. **Do not** make the field an input to any authorization decision, and do not add a second writer for it.
- **The self-upgrade replaces a process; it neither widens the git surface nor weakens the lock (added 2026-08-18, `loop-02`).** The loop now runs code it just merged by replacing its own interpreter (`cli._self_upgrade_at_boundary`, `docs/AUTOLOOP.md` §3f-quater), which introduces the only `os.execv` in the package and a new `subprocess` call. Five properties bound it. (1) **Argv, never a shell** — `os.execv(sys.executable, [sys.executable, "-m", "autoloop", *sys.argv[1:]])` and `subprocess.run([sys.executable, "-c", <a constant script>], …)`; every element is either the interpreter, a package-authored constant, or this process's OWN argv, so nothing model-authored or task-authored reaches either call. The preflight script is built from `PREFLIGHT_MODULES`, a module-level tuple of literals — it is not derived from the merge, the record, or any agent output. (2) **The trigger is a git-observed fact, not a claim.** The record is written only from `AutoMerger._merge`'s verified merge, from `git diff-tree --name-only` between the pre- and post-merge heads, and the replacement additionally requires the merged `repo_root` to equal the package root this process imported from. No executor report, directive or config value can cause a restart. (3) **No new git capability.** `changed_paths` is an existing, already-whitelisted read; the whitelist is untouched. (4) **The lock is not weakened** — see the entry below, and `autoloop/lock.py`'s handoff section: a live lock is still refused unless it carries a one-shot marker THIS pid wrote for itself immediately before the exec. (5) **Fail-closed both ways.** An unreadable record, a tree that does not import, a lock that cannot be armed and a `repo_root` that does not match all mean "do not replace the process"; a sha already exec'd for is never exec'd for again, so a merge that imports and then fails at runtime cannot loop. An `os.execv` that RAISES is settled `exec_failed` rather than left `execed` (2026-08-18, review round 2), which keeps the audit trail true in the other direction too: `execed` is what `_confirm_self_upgrade` retires with a `self_upgrade_confirmed` entry, and "this process was replaced" is exactly the claim a transcript must not make on a replacement that did not happen. **Verification:** `rg -n 'os\.execv' autoloop/` should show exactly one call site (plus tests), `rg -n 'shell=True' autoloop/` stays empty, and `pytest autoloop/tests/test_self_upgrade.py`. **Do not** derive the exec argv or the preflight script from anything outside this package, and do not exec before the record is durably marked.
- **Lock adoption is a handoff, never a steal, and the authorization is a secret the successor INHERITS (added 2026-08-18, `loop-02`; token hardening the same day).** `LoopLock.acquire` now has one path past a LIVE lock, and it is deliberately unreachable by anything but the image `os.execv` produced. Five conditions, all required: an `exec_handoff` marker; this hostname; THIS pid named twice (as the lock's owner and inside the marker); the run id the lock itself records; and a token equal to the one this process inherited in `AUTOLOOP_EXEC_HANDOFF_TOKEN`. Adoption clears the marker AND consumes the environment token, so it works once and nothing spawned later inherits a spent authorization. **The token is what makes the other four load-bearing.** They are all forgeable or reproducible from outside — the hostname is public, the run id sits in the lock file beside the marker, and pids are small integers the kernel reuses within a boot — so a marker left behind by a dead run, or written by anything that can write the state dir, plus that pid coming round again would have been a complete handoff. `mark_exec_handoff` mints 32 random bytes (`secrets.token_hex`) per arming, puts them in the environment before writing the lock file (a marker on disk without an inheritable token could never be adopted, and by then the upgrade's one shot is already spent), and `clear_exec_handoff` drops both when `execv` is refused. A malformed token — non-`str`, empty, or non-ASCII, all of which make `secrets.compare_digest` raise — is a **refusal, not an exception**: that comparison runs in the successor's first act after the exec, where a raise is a crash with no `finally` behind it rather than a fail-closed answer. The weaker rule — "the lock's pid is my pid, so it is mine" — was considered and rejected for the pid-reuse reason above. Every pre-existing refusal is intact: a live lock with no marker, a foreign host's lock, a corrupt one, and `break_stale`'s refusal to remove anything live. Both rewrites use temp-file + `os.replace` and never unlink, so the lock file exists at every instant of the handoff. **Verification:** `pytest autoloop/tests/test_self_upgrade.py -k "lock or token or handoff"` — includes `test_a_valid_looking_marker_this_process_inherited_no_token_for_is_refused` (a hand-written marker with correct host, pid and run id, refused `LockHeldError`), the wrong-token and wrong-run mutations, the five malformed-token shapes, and the four original mutation tests (no marker, another pid, another host, second adoption) that fail against a same-pid-wins implementation; `rg -n 'EXEC_HANDOFF_TOKEN_ENV' autoloop/` shows the definition, the arm/adopt/clear sites in `lock.py` and tests, and no other writer. **Do not** relax any of the five marker conditions, do not persist the token anywhere but the lock file, do not spawn a subprocess between arming and the exec (it would inherit the token), do not let adoption leave either half in place, and do not implement either rewrite as unlink-then-create.
- **Returning a task to the queue is derived from the blocker records, and cannot reach an operator hold or rewrite one (added 2026-08-21, `blk-01`).** `cli._reconcile_unblocked_tasks` is a new automatic path OUT of `blocked` — a state that keeps a task away from a write-capable dispatch — so it is bounded rather than trusted. (1) **It never writes a `blockers.Blocker`.** It reads `BlockerStore.open_task_ids()` and nothing else; resolution stays an operator act (`answer`) or an explicit machine archival (`archive_stale`), so it cannot become a way to launder the confirmation `_RESOLUTION_PRECONDITIONS` demands, and it cannot silence `start`, `health` or the heartbeat, all of which read the records directly. (2) **It cannot release an operator hold.** `TaskRegistry.blocker_derived_blocked` excludes `hold_origin == HOLD_ORIGIN_OPERATOR` — the same provenance field `operator_unblock` is narrowed by, written only by `operator_block` and cleared unconditionally by `block`/`unblock`, never inferred from `blocked_reason`. An inbox hold creates no blocker record by design, so a record-counting rule alone would have released every hold on its first pass. (3) **Any kind of open blocker keeps the task out** — deliberately wider than `_reconcile_retired_blockers`' `task_fatal` allowlist, because this sweep only decides whether to keep a task quarantined, where the conservative direction is the opposite of the one that closes records. (4) **It grants nothing else.** A released task returns to `pending`; `approved_paths`, `decomposition`, `depends_on` and every execution counter are untouched, so it is dispatchable on exactly the authorization it already had, and `policy._check_task_reference` / `mark_in_progress` are unchanged. (5) **It never writes `tasks.json` underneath a running loop.** All five sweep sites either hold the loop lock (`answer`, `archive-blocker`) or ARE the loop (`run`, `run --continuous`, and `start`'s preflight, which refuses outright when a live lock is held). `archive-blocker` was the exception for one review round and is no longer: it archived with no lock, then READ the lock and skipped the requeue when a live one was found, which is both a half-transition (a closed record whose task stays `blocked` — the split brain this entry is about, manufactured by the fix for it) and a check-then-act race (a loop acquiring the lock inside the window got `tasks.json` written underneath it anyway). It now takes the lock around the WHOLE command and, when the lock cannot be taken, changes nothing — the blocker is not archived either, and the refusal says so. That is also the stronger form of the escape-detector argument the old shape rested on: `.autoloop/` is inside the tree `enumerate_checkout_paths` snapshots (ignored paths included), and holding the lock is what proves no round is in flight, where a read only proves none was a moment ago. A STALE lock refuses identically and names `unlock`; nothing here calls `break_stale`. **Verification:** `pytest autoloop/tests/test_blockers.py` (the whole file deliberately — a `-k` filter here is a claim that has to be re-checked every time a test is renamed, and the one this replaced selected none of the tests it named) — section 15 holds the byte-for-byte `asdict` comparison of every blocker record across a sweep, and the two lock refusals (`test_a_live_lock_refuses_the_whole_archival_rather_than_half_of_it`, `test_a_stale_lock_refuses_it_too_and_names_the_recovery`), which assert `resolved_at is None` on the BLOCKER rather than only that the task stayed blocked; `rg -n 'blocker_derived_blocked' autoloop/` should show the definition, the single `cli` call site and tests, and nothing that drops the `hold_origin` filter. **Do not** let this sweep resolve, archive or bump a blocker, do not widen it to `state_of()` (it would stop distinguishing a hold), and do not let `archive-blocker` — or any future sweep site — write `tasks.json` without holding the loop lock.
- **A close that cannot requeue is undone, and the undo is not a `reopen` verb (added 2026-08-21, `blk-01`, review round 3).** `answer` and `archive-blocker` now fail closed: when the task half of a close cannot be completed (`cli._requeue_after_close` catches a handled task-load / reconciliation / task-save failure) the blocker record is written straight back as it was read and the command exits non-zero without reporting a close. That adds the first write that moves a blocker record from CLOSED back to OPEN, so it is bounded rather than trusted. (1) **It is an undo of THIS command's own write, not a capability.** `_reopen_blocker` takes the snapshot the command loaded *before* it closed the record, inside the same `LoopLock`, and calls the ordinary `BlockerStore.save`; there is no `BlockerStore.reopen()`, no CLI verb, and no way to name an arbitrary record — so it cannot be used to un-answer an operator's answer or to reopen someone else's archival. (2) **It cannot manufacture a close either.** The bullet above still holds: `_reconcile_unblocked_tasks` reads blocker records and never writes one, and `resolve` / `archive_stale` are still the only ways a record closes, both still refusing an already-closed record. (3) **It fails loud, never silently.** A restore that cannot itself be written prints that the record is CLOSED and its task was not requeued, rather than raising or reporting success — the operator is told the one state the loop cannot repair by itself. (4) **The startup sweeps are deliberately NOT changed.** `start`, `run` and `run --continuous` still report an unreadable task graph and carry on: they hold no blocker to put back, and a sweep that refused to start would turn a repairable state into an unstartable loop. **Verification:** `pytest autoloop/tests/test_blockers.py` (the whole file, per the bullet above) — the six round-3 tests inject a reconciliation failure and a real `TaskStore.save` failure into both commands and assert non-zero exit, the record restored byte-for-byte (`asdict`), the task still blocked, and no `task_auto_unblocked` transcript entry; `rg -n '_reopen_blocker' autoloop/` should show the definition, the single `_requeue_after_close` call site and tests, and nothing else. **Do not** promote this into a `BlockerStore.reopen()` or a CLI command, do not let it run outside the lock, and do not make the closing commands tolerant again.
- **All routers were swept for auth (2026-05-24).** Every handler is covered by `get_current_user` (router-level or per-handler) **except** the documented public ones — see the unauthenticated-surface list below. `search.py` is auth-gated at the router level; `playlists/generate` is auth-gated and is DB-only (no LLM, so it correctly does not need `rate_limit_llm`); `phrases/seed` is auth-gated **and** admin-gated via `require_admin` (S17 resolved). `POST /sentences/match` is now auth-gated too (S16 final fix), so **every** API handler requires a bearer token.
- **Admin-gated routes (`require_admin`, 403 `admin_required` for non-admins):** `POST /phrases/seed` (S17), `GET /admin/lemma-corrections` (#39 3A), `POST /admin/lemma-corrections/{id}/{accept,reject}` (#39 3B), `…/{id}/adjudicate` (#39 3C, read-only dry-run), and the document-package ingestion pair `GET /books/packages` + `POST /books/import` (S28, 2026-08-01). All rely on `is_admin` being un-self-grantable (settings writes are `DEFAULTS`-filtered). **The rule this list encodes:** a route whose subject is *server-side operator inventory* rather than the caller's own data belongs here, even when it only reads.
- **User-signal ≠ authority (#39 3A/3B).** `POST /api/v1/lemma-corrections` (auth + per-user throttle) writes only the `lemma_correction_candidate` *signal* table — it can NEVER mutate `lemma_override` (the table the extractor/matcher trust); regression-guarded by `test_post_never_mutates_lemma_override`. The **only** path from a user signal to `lemma_override` is an **admin** `POST /admin/lemma-corrections/{id}/accept` (3B) — human-gated, transactional, never automatic; no raw-vote auto-promotion.

> **Doc-drift note:** `CLAUDE.md` §7 calls `/api/search`, `/api/suggest`, `/api/video-sentences`, `/api/word-forms`, `/api/languages`, `/api/categories` "public legacy endpoints." They are actually auth-gated at the router level (`routers/search.py:20`, `APIRouter(dependencies=[Depends(get_current_user)])`). No data leak — but the §7 label is stale and should not be trusted when reasoning about the public attack surface.
>
> **The genuinely unauthenticated surface is:** the static file mount (`main.py:142`) and `POST /api/v1/errors/client` (per-IP throttled — S6 resolved). The FastAPI docs `/docs` + `/openapi.json` are off by default (require an explicit `ENABLE_DOCS=true` — S11 resolved). `POST /sentences/match` is no longer public (auth-gated — S16 final fix). Everything else requires a valid bearer token.

---

## Resolved findings

### S34 — `merge=union` disabled conflict detection on two documentation trackers — INFO — RESOLVED 2026-08-19 (docs-01)

**Kept rather than deleted, per §14's regression-history rule.** This shipped
and was removed the same day, in the same task, after review; the argument
below is why the one-line attribute must not come back.

**What it was.** `/.gitattributes` gave `docs/SUMMARY.md` and `docs/TESTS.md`
`merge=union`, so two branches that each recorded a change note both landed
instead of stopping the merge sweep. It was accepted at first as a narrow,
documentation-only trade.

**Why it was not acceptable.** Git has no way to scope a merge attribute to a
REGION of a file, and union NEVER reports a conflict. So the attribute did not
mean "combine the appended notes" — it meant those two files could no longer
conflict at all. Two branches rewriting the same sentence of tracker PROSE, or
the same existing note line, produced two contradictory copies and no warning,
including for a line that WEAKENS a claim. Reproduced, and kept as a test:
`test_docs_merge.py::test_union_would_have_swallowed_a_genuine_prose_conflict`.
Union was also insufficient for the notes themselves — it resolves per LINE, so
two branches that grew the same 19,410-character row duplicated the whole row
(`..._duplicates_a_grown_row_instead_of_merging_the_two_additions`).

**Fix shipped.** The attribute was removed; `.gitattributes` is kept rule-free
and carries the argument above so it is not reintroduced. Combining the two
branches' appended notes moved into `autoloop/note_merge.py`, wired into
`auto_merge.AutoMerger._merge`, which resolves only the append-only section and
only when each side left every pre-existing line untouched. Its own residual is
tracked as S35 (open, bounded).

**Verification check:**
```bash
# Must print NOTHING — no merge attribute is shipped:
grep -v '^\s*#' .gitattributes | grep -v '^\s*$'
```
Pinned by `autoloop/tests/test_docs_merge.py::test_the_repo_ships_no_merge_attribute_at_all`
(exact emptiness, so any new rule is a decision that needs its own review) and
by `..._a_bare_git_merge_of_two_note_appends_still_conflicts`, which proves
git's own behaviour on these files was not weakened.

### S31 — Making the always-approved tracker list a config value — LOW — WITHDRAWN 2026-08-16, never shipped

**Filed here rather than under *Open findings*, and kept rather than deleted.**
Nothing described below is live: the design was written, reviewed, rejected and
reverted inside one task (port-02), so this entry records a boundary that was
tested, not a weakness that exists. It stays for the same reason every resolved
finding does — the next person who wants per-repository trackers needs the
argument that killed this version.

**What was proposed:** `tasks.TRACKER_PATHS` is the set of documentation files
EVERY scoped task may write without naming them in its `approved_paths`
(`tasks.effective_approved_paths`, `docs/AUTOLOOP.md` §4f-bis). It encoded THIS
repository's documentation obligations *by filename*, which blocks reuse
against any other repository — the obligations are real everywhere,
`docs/SUMMARY.md` is not. The proposal moved the active list to
`[repo].tracker_paths` in `.autoloop/config.toml`, defaulting to the constant.

**Why it was rejected.** `.autoloop/config.toml` lives under the gitignored
state directory, so an edit to it is not a reviewed diff — anything that can
write that file could add a path to every scoped task's authorization at once.
The offered bound was a load-time refusal (`validate_tracker_paths`: the same
validator a task's own scope gets, plus no directory prefixes, plus a blocklist
of code/config extensions), advertised as "the widest thing a config edit buys
is another document". **That claim was false, and not repairable by extending
the blocklist:** `.env`, `.gitignore`, `Makefile`, `Dockerfile`, `Gemfile` and
any extensionless script carry no refused suffix and change behaviour. The set
of behaviour-changing filenames is open-ended, so a suffix heuristic cannot
enforce "documentation only". A hard control (a reviewed diff) was being traded
for an unenforceable one.

**What shipped instead.** `TRACKER_PATHS` stays a fixed constant and `[repo]`
carries only non-authority settings — `env_example_file` /
`env_example_db_key` (where the repo declares its application database),
`audit_report_glob` (where the dashboard reads the backlog) and, since port-03
(2026-08-19), `audit_charters_file` (where the repository ships the per-domain
briefs its read-only audit agents get). Each says WHERE to read something the
repository states; none decides what an agent may write. The charters are the
case worth stating explicitly, because they are prose that reaches an agent:
they say what to LOOK AT and grant nothing. Read-only confinement stays
argv-level (`audit/agents.py`'s `--allowedTools`/`--disallowedTools`), and
`_agent_prompt` wraps whatever the file says in the standing ground rules and
the findings schema, so a charter cannot drop them by omission — the same
containment the reviewer-scope text gets, and subject to the same limit (S24:
prompt text is guidance, not a control). Loading is read-only, repository-scoped
and fails closed; a file that exists but does not parse, or is not a readable
regular file, aborts the audit rather than silently briefing the agents on
another repository's architecture.
Portability for the tracker list comes from the constant itself: `autoloop/` is
vendored into the repository it operates on, so editing `TRACKER_PATHS` in a
target repo is a commit in that repo's reviewed history — the property a
gitignored config edit lacks. `validate_tracker_paths` and
`_NON_TRACKER_SUFFIXES` were deleted rather than left caller-less, so the
disproven claim is not sitting in the tree for a future caller to trust.

A config that still names `repo.tracker_paths` (the unshipped
`config.example.toml` advertised it) LOADS and is handled explicitly by
`config._migrate_retired_tracker_paths`: the key is consumed, the value is
DROPPED, and an operator notice says so and names `autoloop/tasks.py`.
Discarding grants fewer paths than the operator may believe, so the failure
mode is a task refused for an unauthorized path — never an over-authorized one.

**file:line** — `autoloop/tasks.py` (`TRACKER_PATHS`,
`effective_approved_paths`); `autoloop/config.py` (`RepoConfig`,
`RETIRED_TRACKER_PATHS_KEY`, `_migrate_retired_tracker_paths`);
`autoloop/orchestrator.py` (`_tracker_paths`).

**Verification check:**
```bash
# Expect: EMPTY — the suffix heuristic and its validator are gone, not unused
rg -n 'validate_tracker_paths|_NON_TRACKER_SUFFIXES' autoloop --glob '!**/tests/**'
# Expect: EMPTY — no config read supplies the tracker list. `config.py` (the
# consume-and-drop handler) and the tests that pin it are the only mentions
rg -n 'repo\.tracker_paths|config\.repo\.tracker' autoloop --glob '!**/config.py' --glob '!**/tests/**'
# Expect: the accessor returns the reviewed constant, and all three call sites
# read that one accessor (so the seed and the re-sync cannot diverge)
rg -n 'return TRACKER_PATHS|effective_approved_paths\(' autoloop/orchestrator.py
# Expect: hits — the regressions that pin it, including .env / Makefile
rg -n 'BEHAVIOUR_CHANGING_FILES|cannot_newly_authorize' autoloop/tests
```
**Suggested fix (only if per-repository trackers are wanted again):** source
the declaration from git-TRACKED repository metadata — a committed
`.autoloop.toml` at the repo root — so that declaring an implicit grant is once
again a change that appears in the repository's reviewed history. The
requirement is that property, not per-repository-ness on its own; a runtime
config file cannot satisfy it however it is validated.

### S28 — Document-package ingestion endpoints were auth-gated but not admin-gated — MEDIUM — RESOLVED 2026-08-01

**Was:** `GET /api/v1/books/packages` and `POST /api/v1/books/import` (`routers/books.py:182-212`) depended only on `Depends(get_current_user)`. Neither the endpoints nor the service functions behind them (`book_import_service.list_packages` / `import_package`) take any ownership parameter — `list_packages` enumerates the whole of `PACKAGE_ROOT` — so *every* registered learner could read the operator's curated package inventory and make the server load, checksum-verify and structurally validate any named package on demand. Two exposures: **information disclosure** (package names are operator inventory, not user content) and **compute cost** (checksum verification hashes every file in a package, and the route is not in the LLM rate limiter's scope because it makes no LLM call). A third was latent: `dry_run=False` reaches only `PendingSchemaPersistence`, which refuses to write until roadmap **A3** lands migration 038 — the day a real backend replaces it, the same unscoped route becomes an unauthenticated-in-practice write path into `book_blocks`. Raised by the 2026-07-30 audit as `tests_ci:ing-02` + `security_paths:sec-02`.

**Fix shipped (admin gate, the S17 pattern):** both handlers now take `user=Depends(require_admin)` — 403 `admin_required` for a non-admin, unchanged behaviour for an admin. `require_admin` returns the user dict, so `import_package`'s existing `user["user_id"]` is untouched. No change to the service layer: this is an authorization boundary at the route, and the containment chokepoint (`resolve_within`, the *Verified strength* above) still does its own job for the callers who get through — admin-gating narrows who may ask, it is not the traversal defence, and the traversal tests still assert it directly.

**Why admin rather than a per-user scope:** there is nothing to scope to. A package is written to server-side disk by the offline worker (`docs/INGESTION_PIPELINE.md` §5, "run by whoever launches the worker CLI, not an end-user feature"); it has no owner column and no user association until an import creates one.

**Behaviour change (intentional):** a non-admin who could previously list packages or run a dry-run import now gets 403 — that is the finding, not a regression. No frontend caller exists (`rg -n 'books/import|books/packages' lexy-app/frontend/src` → no matches), so no UI breaks.

**Verification check:** `rg -n 'require_admin' lexy-app/backend/routers/books.py` (expect both handlers); `cd lexy-app/backend && MOCK_LLM=true python3 -m pytest tests/test_books_import_admin.py tests/test_document_package.py -q` passes (non-admin → 403 `admin_required` on both routes; admin → 200 listing / 200-with-rejected-result import; no token → 401/403).

**Tests:** `tests/test_books_import_admin.py` +9 (new) — both routes' non-admin 403, admin path unchanged, no package name in a refused listing's body, and `test_non_admin_import_never_reaches_the_service`, which patches `import_package` to raise and asserts the refusal costs the server no package I/O. `tests/test_document_package.py` — the four authenticated HTTP cases now register an admin via a local `_admin_headers` helper (same direct-SQL planting as `test_phrases_seed_admin.py`); the two no-token cases are unchanged. Backend baseline 1258 → **1267** (`docs/TESTS.md` still records 1258 — updating it, `docs/SUMMARY.md` and `docs/INGESTION_PIPELINE.md` was outside rt-01's approved paths; see the changelog entry).

### S27 — Database credentials for validation had no delivery path that excluded the writer — MEDIUM — RESOLVED 2026-07-31

**What it was:** a task whose declared validation needs a database (`rt-01`
runs the backend suite) could not validate honestly in a worker repo, because
a worker is a fresh clone and `.env` is gitignored. Every workaround available
before this changeset was a security regression: copying `.env` into worker
repos puts the production DB password, the JWT signing key and the Anthropic
API key on disk in every worker; exporting the variables into the loop's shell
hands them to the write-capable `claude` subprocess through ordinary
inheritance (`ClaudeCliRunner.run` passed no `env=`, so the agent inherited
everything the loop had); narrowing the declared validation reintroduces the
vacuous-validation weakness closed by `7616b18`.

**Fix (`autoloop/validation_env.py`, new):** an explicit
`[paths].validation_env_file` — absolute, outside the checkout / state dir /
`workers_root` / both publisher paths, `chmod 600`, owned by the running user,
never a symlink — parsed under a six-name allowlist (`DB_HOST`, `DB_PORT`,
`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `SECRET_KEY`) that rejects unknown keys,
duplicates, malformed lines, empty values, missing keys, and secrets under 8
characters. Delivery is explicit at both ends: `run_validation_commands`
always passes `strip_validation_vars(os.environ)` and overlays the file's
values only for post-writer validation, while `ClaudeCliRunner.run` and
`worker_env()` explicitly REMOVE the same six names. Values are redacted from
every validation summary — that string becomes `state.last_validation`, which
reaches `state.json`, the transcript, blocker records and the review packet
sent to the reviewer. Design and rationale: `docs/AUTOLOOP.md` §4g.

**Deviation from the brief, recorded deliberately:** the brief named
`JWT_SECRET_KEY`; this repository reads `SECRET_KEY` (`core/security.py:11`).
Verified in a clean clone: six test modules fail to import until `SECRET_KEY`
is set, after which all 1260 tests collect with nothing else supplied.
`JWT_SECRET_KEY` is not accepted as an alias.

**Scope — what this is NOT, stated plainly (revised 2026-08-01).**

**Candidate validation code CAN observe the test credentials.** That is the
accepted v1 posture, not an oversight: the validation subprocess is handed
`DB_*` and `SECRET_KEY` so the backend suite can run, and any test, conftest,
plugin or imported module in that process can read `os.environ`, print it,
encode it, or write it to a file. Output redaction removes the values from
summaries the loop produces; it does not and cannot stop candidate code from
exfiltrating what it was deliberately given. **This is not secrecy from
candidate code, and must not be described as if it were.**

What the protection actually is, in order of how much it carries:

1. **Least privilege** — a dedicated role on a dedicated throwaway database,
   with no access to application data.
2. **Local-only scope** — the validation server is a separate local cluster;
   the credentials authenticate to nothing reachable off this machine.
3. **Separation from production** — production credentials are FORBIDDEN here,
   enforced by refusing the `DB_NAME` this repo declares in `.env.example`.
4. **Publication gating** — a candidate that mutates the tree during validation
   is refused and never published (see the mutation guard, §4g).
5. **Non-inheritance** — the writer subprocess has these variables explicitly
   removed, so an agent that never runs validation never sees them.

**It does NOT close S24.** It separates credentials from the writer PROCESS.
It is not an OS sandbox, does not stop a process that can already run
arbitrary code from reading the credential file off disk, and the
write-capable agent still has no path jail. Per-run ephemeral databases and a
real sandbox are the next steps and are deliberately NOT in this changeset.

- `file:line` — `autoloop/validation_env.py:1`, `autoloop/validation.py:60`,
  `autoloop/audit/agents.py:128`, `autoloop/worker_env.py:110`
- severity — MEDIUM (credential exposure to a write-capable subprocess)
- verification check —
  `rg -n 'env=strip_validation_vars' autoloop/audit/agents.py` (writer strips),
  `rg -n 'validation_env.apply|strip_validation_vars()' autoloop/validation.py`
  (validator's env is always explicit), and
  `python3 -m pytest autoloop/tests/test_validation_env.py -q` (42 pass, 1
  skipped pending operator test credentials)
- fix — shipped; see above

### S21 — Commit hooks can rewrite an executor-manifest commit after verification — HIGH — CLOSED BY RETIREMENT 2026-07-30

**What it was:** `autoloop`'s executor-manifest commit path used plain `git
commit` (`GitGateway.commit()`). `git commit` runs `pre-commit` **after** any
check the caller performed, and a pre-commit hook could `git add` arbitrary
content — so a hook could change the bytes of an approved path and stage
additional unapproved paths, and both would land in the commit. Reproduced
2026-07-30 (see the original finding text preserved below) while hardening
the sibling *adopted*-manifest path, which closed the same hole a different
way (immutable tree → `commit-tree` → `update-ref` CAS, refusing outright
when any commit hook is active).

**Closed by retirement, not by a fix.** The vulnerable path itself —
`GitGateway.commit()`, and its only caller, `orchestrator.py`'s
`_dispatch_git` (the authorize-then-produce/`ChangeManifest` commit gate) —
was **removed** as part of replacing the whole authorize-then-produce commit
model with produce-then-review end to end (`worktask.py` / `packet.py` /
`Orchestrator._dispatch_task_postcommit`, now used for audit **and**
implement/revise alike — see `_resolve_audit_task`). There is no more
"approve, then commit" step for a hook to interpose itself in: the
orchestrator commits automatically after implementation/audit work
completes, with hooks enabled and unrestricted, and REVIEW happens
afterward, reading the immutable committed objects — a hook cannot rewrite
content between "was reviewed" and "was committed" because nothing is
reviewed before it is committed anymore. `commit_adopted` (the fix that
*did* patch the hole directly) is unaffected and still sound, but likewise
lost its only caller in the same change — tracked separately as **S22**
(non-production, kept for its own unit tests).

**Verification check:**
```bash
# Expect: neither symbol exists anymore
rg -n 'def commit\(' autoloop/git_gateway.py        # only commit_adopted/commit_and_capture/commit_tree remain
rg -n '_dispatch_git' autoloop/orchestrator.py       # no match
```

**Original finding (preserved for history):**

> **What:** `autoloop`'s executor-manifest commit path uses `git commit`
> (`git_gateway.commit`). `git commit` runs `pre-commit` **after** any check the
> caller performed, and a pre-commit hook can `git add` arbitrary content. So a
> hook can change the bytes of an approved path and stage additional unapproved
> paths, and both land in the commit.
>
> **Reproduced 2026-07-30** while hardening the *adopted*-manifest path: an index
> check passed, a pre-commit hook rewrote the approved file to `HOOK-INJECTED
> PAYLOAD` and staged an extra `sneaked.txt`, and the created commit contained
> both. Verbatim:
>
> ```
> index check at post_stage_check: PASS
> files in the commit : ['f.txt', 'sneaked.txt']
> committed f.txt     : 'HOOK-INJECTED PAYLOAD\n'
> ```
>
> **Current exposure (at the time):** low in practice — this repository had no
> active hooks (`.git/hooks` held only `*.sample`, `core.hooksPath` unset), and
> a hook is operator-installed, not attacker-installed, so this needed local
> write access. Exposure would have risen the moment anyone added a formatter
> hook — which is exactly why the path was retired rather than left in place.

### S19 — Raw user input concatenated into a Postgres regex in corpus **search** — LOW — RESOLVED 2026-07-28
**Was:** `search_service.PHRASE_WORD_QUERY` matched blueprints with a
case-insensitive regex built by concatenating the user's search term between
word-boundary escapes. The term was parameterised (so **not** SQL injection)
but was still evaluated as a **regular expression**: `.*` matched every one of
the 43,549 `phrase_blueprint` rows, and a catastrophic-backtracking pattern
(`(a+)+$`-style) would be run against all of them inside a request.
`/api/search` is auth-gated (`routers/search.py:20` —
`APIRouter(dependencies=[Depends(get_current_user)])`), so this needed a
logged-in account; that is what keeps it LOW rather than MEDIUM.
**file:line:** `lexy-app/backend/services/search_service.py:52` (pre-fix).
**Fix shipped:** blueprint matching for `search()` moved to Python in
`_resolve_blueprint_ids`, which `re.escape`s the term before compiling it, so
the query is matched as a literal. That SQL now takes pre-resolved
`blueprint_id`s (`= ANY($1::int[])`).

> **Scope correction (2026-07-28).** As first written this finding claimed the
> file "contains no user-controlled pattern at all". **That was wrong**, and
> its verification check failed the moment anyone ran it: `_suggest_phrases`
> still built the same kind of pattern. S19 covers the `search()` path only;
> the autocomplete occurrence is tracked as **S20** below and is now also
> resolved. The claim is corrected rather than deleted because a resolved
> finding with a failing check is worse than an open one — the next reader
> greps it, sees a hit, and cannot tell whether the fix regressed or the doc
> was always wrong.

**Verification check:** `rg -n 're.escape' lexy-app/backend/services/search_service.py`
shows the escape in `_resolve_blueprint_ids`. The file-wide grep is under S20.
**Tests:** `tests/test_search_unicode.py::test_blueprint_search_escapes_regex_metacharacters`
asserts `.*`, `.+`, `(`, `[a-z]+` and `Ö.*geben` do **not** match a fixture
blueprint, with a control asserting the literal token still does.

### S20 — Same regex sink in `/api/suggest` autocomplete — LOW — RESOLVED 2026-07-28
**Was:** `search_service._suggest_phrases` matched `phrase_blueprint.lookup_key`
with a case-insensitive regex whose pattern was the raw search term
concatenated between word-boundary escapes — the same shape as S19, in the
function S19's fix deliberately left alone. Found while investigating the
autocomplete Unicode bug, *after* S19 was committed claiming the file was
clean. Autocomplete fires on **every keystroke**, so the per-request cost of a
hostile pattern is paid more often here than in search; `/api/suggest` is
auth-gated on the same router.
**file:line:** `lexy-app/backend/services/search_service.py:436` (pre-fix).
**Fix shipped:** `_suggest_phrases` now folds the query with
`text_norm.normalize_key` and passes it through `_escape_regex`, which escapes
every Postgres ARE metacharacter (`\^$.[]|()*+?{}`) so the term matches
itself literally. The operator is also now the case-**sensitive** `~` against
the generated `lookup_key_norm` column (migration 036), so no case folding
happens in SQL either. `_escape_like_prefix` does the equivalent for
`_suggest_words`' `LIKE` prefix — previously a typed `%` matched the entire
catalog and `_` matched any character.
**Verification check:** `rg -n '~\*' lexy-app/backend/services/search_service.py`
returns **nothing** (no case-insensitive regex operator anywhere in the file,
including in comments, so the grep stays meaningful), and
`rg -n '_escape_regex|_escape_like_prefix' lexy-app/backend/services/search_service.py`
shows both escapes wired into the two suggest functions.
**Tests:** `tests/test_search_unicode.py` — `_suggest_phrases` treats `.*`,
`.+`, `(`, `[a-z]+`, `\` and `Ö.*geben` literally (with a literal-token
control), and `_suggest_words` treats `%` and `_` literally.

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

### S15 — No dependency vulnerability scanning — INFO — RESOLVED 2026-05-24
**Was:** no automated audit of `lexy-app/backend/requirements.txt` or `lexy-app/frontend/package.json`; a known-vuln dependency could land unnoticed. The repo had no CI at all.
**Fix shipped:** new GitHub Actions workflow `.github/workflows/dependency-audit.yml` (the project's first CI). Two blocking jobs — `pip-audit -r lexy-app/backend/requirements.txt` (backend; tooling installed inline, not a project dep) and `npm audit --omit=dev --audit-level=high` (frontend, prod deps only) — on every push/PR to `main` plus a weekly cron (06:00 UTC Mondays) so newly disclosed CVEs against already-pinned deps surface without a code change. Baseline was clean when this landed (`pip-audit` + `npm audit` both reported 0 locally), so the gate is green from day one.
**Verification check:** `cat .github/workflows/dependency-audit.yml`; locally `python -m pip_audit -r lexy-app/backend/requirements.txt` → "No known vulnerabilities found" and `cd lexy-app/frontend && npm audit --omit=dev --audit-level=high` → "found 0 vulnerabilities".
**Known limitations of the gate (not finding residuals — S15 is fully closed):** scoped to high+ for npm (moderate/low dev-chain advisories don't block); `pip-audit` resolves transitive deps live, so a flaky PyPI/OSV connection can fail a run (the weekly cron re-checks).
**Re-check:** `rg -n 'model_validator|_normalize_video_id|_normalize_channel_id' lexy-app/backend/routers/content_requests.py`; `cd lexy-app && MOCK_LLM=true python -m pytest backend/tests/test_content_requests.py -q` passes (a bare 11-char id / `UC…` id → 201; `https://evil.com/...` or `"; rm -rf /"` → 422).

---

## Changelog

- **2026-08-22** — **The implementation agent's prompt now renders the task's effective approved paths (no new findings; no authorization moved)** (`impl-01`, `autoloop/implement_executor.py:_scope_instruction`, INFO). Recorded because it is an LLM prompt composed from task-record data, which §14 of `CLAUDE.md` names as a review trigger. What it is: the `APPROVED SCOPE` section lists `tasks.effective_approved_paths(task.approved_paths)` so the write-capable agent can tell a fix it is authorized to make from a finding it must only report. What it is NOT: a grant. Authorization is still computed from the `Task` and enforced by `tasks.unauthorized_paths` against `TaskExecution.allowed_paths`; this module reads neither and writes neither, `WRITE_ALLOWED_TOOLS`/`IMPLEMENT_DISALLOWED_TOOLS` are unchanged, and the section is the same set the pre-commit gate compares against (one computation, not a paraphrase). Three controls carry the injection surface: entries were already allowlisted to `[A-Za-z0-9._-]` segments by `tasks._validate_approved_path` (no second validator was added here — that drift is what `unauthorized_paths`' docstring argues against); `_scope_entry` escapes every non-printable character so one entry occupies exactly one line, which stops a newline in a hand-built record from forging a ground rule on a line of its own; and each entry is rendered behind a `- ` bullet, because `_ASSUMPTION_RE` and `_CLEANUP_RE` accept leading WHITESPACE — an entry that simply BEGINS with `ASSUMPTION:` or `REMOVE-OUT-OF-SCOPE:` would otherwise sit at a matching position for an agent echoing its prompt, feeding a fabricated disclosure into the durable assumptions record or a deletion request into the cleanup channel (bounded by `out_of_scope_paths`, but asked for by nobody). Both regexes refuse a `-`/`*`/`>` prefix by design, so the bullet closes that with a property they already have. The list is deliberately never truncated: eliding an entry would tell the agent an authorized file is out of scope. **Verification check:** `rg -n 'effective_approved_paths' autoloop/implement_executor.py` shows one call, with default trackers, and no read of `allowed_paths`; `pytest autoloop/tests/test_implement_executor.py -k "scope or hostile or grants"`. **Fix if it regresses:** do not render `Task.approved_paths` raw (it is narrower than what is enforced), and do not add a path validator to this module.

- **2026-08-22** — **Validation stops at the first failing command; no new findings, and no security boundary moved** (`val-03`, `autoloop/validation.py:353`, INFO, `docs/AUTOLOOP.md` §4h). `run_validation_commands` short-circuits by default and reports every command after the failure as `NOT RUN`. Recorded because it touches the CHECK a gate reads, so the boundary is stated rather than assumed: (1) the short circuit is reached only AFTER a command has failed, so `all_passed` is False in exactly the cases it was before — a refused binary, a missing binary and a timeout are still failures rather than exceptions, and an empty list still reports passed; (2) nothing is skipped while everything passes, so a candidate cannot avoid a check by making an earlier one pass; (3) the summary still names every configured command, so what the reviewer authorizes from cannot silently shrink; (4) `redact_with(validation_env, …)` still runs LAST over the assembled summary, covering the new lines (S27); (5) `SAFE_VALIDATION_BINARIES` is untouched and a refused command still spawns nothing — the refusal now also stops the run, which can only launch fewer processes. **Verification check:** `pytest autoloop/tests/test_validation_failfast.py` (`test_a_refused_binary_is_still_a_failure_and_launches_nothing`, `test_the_summary_is_still_redacted_end_to_end`); `rg -n 'redact_with' autoloop/validation.py` must still show it wrapping the returned summary. **Fix if it regresses:** do not move the short circuit before the failure is recorded, and do not drop a `NOT RUN` line to shorten the summary. No Verified Strengths bullet was added: that list sits mid-file among 2 KB lines, and the diff CONTEXT alone would have cost more than the reviewer's whole inline patch budget (see `docs/SUMMARY.md`'s val-03 note).

- **2026-08-22 (third round)** — **S38 narrowed to its approved paths, and one established recovery behaviour restored.** No change to what the finding permits. (1) `autoloop/prompts.py` is no longer touched at all: the pinned session's kickoff template moved to `cli.URGENT_KICKOFF`, still a `PromptTemplate` (strict rendering), with the two checks `test_prompts.py` runs over `TEMPLATES` — renders with its own fields, offers no retired decision — re-run against it in `test_urgent_preemption.py`. Reviewer-visible text either way; it authorizes nothing. (2) `release_task_to_pending` re-raises a failed retirement by DEFAULT, so `python -m autoloop release` keeps the ending it has always had (record archived first, worker repo left as the loud residue, error propagated for `main` to report and exit 1 on — `test_recovery_commands.py::test_the_record_is_retired_before_the_worker`); only `_preempt_for_urgent` passes `tolerate_retirement_failure=True` and takes the failure back inside the `Release`, because nobody is watching a preemption. `_repair_orphaned_record` therefore runs on the preemption path only, so no operator command can shut the repository-wide merge window behind a traceback. Verification check: `rg -n 'tolerate_retirement_failure' autoloop/orchestrator.py autoloop/cli.py` (expect the definition, one `True` at the preemption call site, and no CLI caller) and `pytest autoloop/tests/test_urgent_preemption.py autoloop/tests/test_recovery_commands.py`.

- **2026-08-22 (second round)** — **S38 widened in one direction and tightened in another; still MEDIUM, still bounded.** Two changes to the finding above, neither of which touches review, approval or publication. (1) The added dispatch gate moved to `orchestrator._refused_ahead_of_urgent` and now also refuses a FRESH `audit` while a pin is live, because an audit is a full executor round (measured 1282s) even though it takes no task out of the queue — which is what made "the urgent task is dispatched next" true rather than merely claimed. `cli._start_new_session` opens a pinned session on the urgent task (`cli.urgent_kickoff_payload`) instead of the audit kickoff, so the refusal is a correction and not a wall. The gate only ever DENIES; it authorizes nothing, `push` never reaches it, and a `revise` continuing an audit arc already in flight is exempt. (2) `release_task_to_pending` returns an `orchestrator.Release` and now catches `GitError` — what a colliding quarantine label actually raises, and not an `OSError`, so it previously escaped the preemption's own handler — retries once under a distinct label, and when even that fails puts the execution record back beside the worker it names (`_repair_orphaned_record`) — but only when `worker_repo_is_reusable` accepts that worker, since a live record shuts the repository-wide merge window and paying that for a worker the next dispatch would refuse anyway buys nothing. Nothing is deleted on any of those paths and the residue, plus which of the two remedies applies (`residue_resumable`), is named in `LoopState.preemption`, the transcript and the operator report. Verification check: `rg -n -A3 'def _refused_ahead_of_urgent' autoloop/orchestrator.py` and `pytest autoloop/tests/test_urgent_preemption.py`.

- **2026-08-22** — **An inbox request can now end the round in flight (new finding S38, MEDIUM, bounded).** `inbox.KIND_URGENT` (preempt-01) is the first request kind whose effect is on the LOOP rather than on the task graph: applying it makes `orchestrator._preempt_for_urgent` end the round currently in flight, return that task to pending, and MOVE its worker repo and execution record to quarantine. That falsifies S30's "nothing in flight can be edited" as a description of the whole vocabulary, so it is recorded rather than absorbed. It is NOT a review or merge bypass and must not become one: nothing on this path builds a packet, verifies a stamp, marks a task completed or pushes, so the `report_sha256` binding that stops an approval computed over candidate A from publishing candidate B is untouched, and the urgent task goes through the identical implement/review/approve/publish path as any other. Skipping review would have bought about 2% of a round anyway (packet build 0.98s, submit 12–15s, verdict ~0s, executor round 1282s). The one enforcement ADDED is a refusal — `_dispatch_executor` denies an `implement`/`revise` of a different task while a pin is live, through the existing budget-capped policy-denial re-prompt — because `next_ready()` decides what the CONTEXT block offers and not what policy authorizes. Bounded on six axes: the target must be a dispatchable READY task with a non-empty `approved_paths` (a blocked, quarantined, retired, completed, in-flight or unscoped target is refused by name and code, at the CLI as well as in the drain); a non-blank reason is required; exactly ONE live pin at a time, a second request refused naming the incumbent rather than queued or applied over it; it acts only at `_at_round_boundary` (`ready`, no pending request — the SAME predicate the self-upgrade restart uses, in one function), so a request arriving while a review packet is outstanding waits instead of stranding it; the displaced task moves through `release_task_to_pending`, shared with `python -m autoloop release`, so status, worker repo and execution record always move together and nothing is deleted; and no PAUSE flag or lock is taken anywhere, which is what makes two overlapping requests structurally unable to deadlock. Reachable damage is delay plus a quarantined directory. Verification check: `rg -n '_refuse_unurgentable|urgent_already_pending' autoloop/tasks.py`, `rg -n '_at_round_boundary|release_task_to_pending' autoloop/orchestrator.py`, and `pytest autoloop/tests/test_urgent_preemption.py`.

- **2026-08-22** — **The reviewer seat gained a LIVE agent session as a selectable transport (new finding S37, INFO).** `codex_app_server` drives `codex app-server` over stdio JSON-RPC and holds ONE THREAD, which is what lets a codex seat declare `supports_chunked_delivery` truthfully: an oversized diff reaches the reviewer as numbered parts and the verdict question reaches it afterwards in the same context, where `codex_cli`'s process-per-turn had to omit the diff (port-01 lost an attempt to a 414,596-byte diff against the 400,000-byte packet cap; codex-01's own 228 KB candidate needed an operator patch on 2026-08-21). Security-relevant because the reviewer stops being a process that exits and becomes a session that can ask this client to approve a command or a patch — answered `{"decision": "abort"}` for the two approval requests whose response type the committed protocol settles, and a JSON-RPC error for every other server→client request, never silence. Argv is a list with no shell and carries no model-authored text at all (prompts go as JSON on stdin), `cwd` stays outside the checkout, stderr is `DEVNULL`, and transcript records are bounded and secret-free. **Two things are explicitly NOT claimed**, and no test asserts either: nothing survives a process restart (`thread/resume` exists in the protocol and is never sent) and no sandbox preset is selected or enforced — both are codex-03. Quota detection moved from substring-matching stderr to exact matches on named error fields plus a numeric 429, so an error whose prose merely mentions a usage limit routes as an ordinary failure. `codex_cli` is unchanged and still selectable; `fallback_provider` can still name any of the three. Left open and named: `doctor` does not yet recognise the new provider, and `config.example.toml` documents none of its four keys — both files were outside the task's approved paths. Verification check: `rg -n 'Popen|shell=True|stderr=' autoloop/codex/app_server.py` and `pytest autoloop/tests/test_codex_app_server.py`.

- **2026-08-21** — **A task can now leave `blocked` with nobody typing anything (no new findings; one Verified Strength added).** `cli._reconcile_unblocked_tasks` returns a `blocked` task to the queue as soon as no OPEN blocker names it — the reverse of `_reconcile_retired_blockers`, and the fix for `port-01` sitting quarantined for hours with every one of its blockers already resolved, out of `next_ready()` with nothing justifying it and no supported command able to return it. Recorded as a strength rather than a finding because the sweep only ever removes a state the records no longer support, and it is bounded on four axes that matter: it never writes a blocker record (so it cannot manufacture the operator confirmation `_RESOLUTION_PRECONDITIONS` demands, nor silence `start`/`health`/the heartbeat, all of which read those records directly); it cannot reach an operator hold, gated on `Task.hold_origin` rather than on the absence of a record, since an inbox hold has no record by design and a record-counting rule alone would have released every hold on its first pass; ANY open blocker kind keeps a task out, deliberately wider than the `task_fatal` allowlist the record-closing sweep uses; and a released task returns to `pending` with `approved_paths`, `decomposition`, `depends_on` and every execution counter untouched, so it is dispatchable on exactly the authorization it already had. `answer`'s unblock was tightened in the same change — previously unconditional, so the first of two answers requeued a task the second question was still about. `archive-blocker` — the one closing path an operator reaches for a blocker that refuses every answer — takes the loop lock around the whole command for the same reason: closing the last record naming a quarantined task has to requeue that task in the same operation, which writes `.autoloop/tasks.json`, inside the tree `escape_detector.enumerate_checkout_paths` snapshots (ignored paths included). A live or stale lock refuses the ARCHIVAL too, rather than closing a record and leaving its task blocked with nothing open naming it. Verification check: `rg -n 'blocker_derived_blocked' autoloop/` shows the definition, one `cli` call site and tests, with the `hold_origin` filter intact, and `pytest autoloop/tests/test_blockers.py` — whose section 15 includes a byte-for-byte `asdict` comparison of every blocker record across a sweep and the two lock refusals, each asserting the record is still open.

- **2026-08-18** — **The loop can now replace its own process, and a live lock has one adoption path (no new findings; two Verified Strengths added).** Security-relevant for two reasons, both recorded above rather than absorbed. (1) `cli._self_upgrade_at_boundary` introduces the package's only `os.execv` plus one `subprocess.run`, so that the loop actually runs code it merged into its own checkout (measured 2026-08-18: a hard decomposition gate merged at 06:23:59 into a process started at 04:07:03 never applied to a task that started after it). Neither call takes a shell, and neither takes anything model- or task-authored: the exec argv is the interpreter plus this process's own `sys.argv[1:]`, and the preflight runs a script built from a module-level tuple of literals. The trigger is a git-observed fact — `diff-tree --name-only` between the pre- and post-merge heads, on a merge this package verified — never an executor report or a directive, and the git whitelist is UNTOUCHED (`changed_paths` was already allowed). (2) `LoopLock.acquire` gains its first path past a live lock, and it is narrow by construction: an `exec_handoff` marker written by that same pid immediately before the exec, matching hostname, the pid named twice, the lock's own run id, **a 32-random-byte token that reaches the successor only by being inherited across `os.execv`**, and all of it cleared on adoption so it works once. The rejected alternative — treating any same-pid lock as ours — would be a lock-stealing hole, since pids are reused within a boot; the four mutation tests that fail against it are in `test_self_upgrade.py`. The token was added the same day, in review, for the residue that argument leaves: host, pid and run id are readable or reproducible from outside, so the marker's own contents are not evidence that a handoff happened — a stale file plus a reused pid would satisfy every one of them. Only the image `execv` produced can present the token, it is consumed on adoption and dropped when `execv` is refused (so nothing spawned afterwards inherits it), and a malformed or non-ASCII value refuses rather than raising `TypeError` inside the successor's first act. Verification check: `rg -n 'EXEC_HANDOFF_TOKEN_ENV' autoloop/` and `pytest autoloop/tests/test_self_upgrade.py -k "token or handoff"`. Every existing refusal (no marker, foreign host, corrupt file, `break_stale` on a live lock) stands, and both lock rewrites are temp-file + `os.replace`, so the file exists at every instant of the handoff. Fail-closed throughout: an unreadable record, a tree that does not import, a `repo_root` that is not this process's package root, and a lock that cannot be armed all mean "keep running the old code", and a sha already exec'd for is never exec'd for again. Verification check: `rg -n 'os\.execv' autoloop/` shows one production call site, `rg -n 'shell=True' autoloop/` stays empty, and `pytest autoloop/tests/test_self_upgrade.py`.

- **2026-08-16** — **A denied directive can now END the run, and the executor's claimed text gained a second labelled section (both deliberate; finding #2 unchanged).** Two halves of completing `ask_user`'s retirement. (1) An exhausted POLICY-denial budget calls `orchestrator._to_fault_stop` instead of `_to_needs_user`: the run ends in `stopped` with `stop_kind="fault"`. This does NOT drop a control — the same `loop_fatal` `blockers.Blocker` is still recorded under the same code, so `blockers`/`answer` and `health.check`'s blocker-first classification are unchanged — it changes which terminal carries it, because a park asked a human a question that only the reviewer could answer. The classification is read POSITIVELY everywhere (`_cmd_smoke_browser` PASSes only on `"contract"`, `cli._is_fault_stop` halts only on `"fault"`), so an unclassified stop fails closed in both directions; without that gate `smoke-browser` would have reported PASS for a reply it exists to catch. The two sibling sites spending the same counter (plan rejection, review mismatch) deliberately still park. (2) `TaskExecution.assumptions` renders inside the existing **"CLAIMED by the executor, not read from git"** section of the review packet. This is the same channel the 2026-08-04 entry below opened, not a new one: `report_details` already carries the agent's whole transcript, so the added text is a labelled, bounded subset of what the packet already showed — what it adds is the label and the cross-round accumulation (`report_details` is replaced each round). Bounded at RENDER time only, on two axes: `packet.ASSUMPTIONS_MAX_CHARS` (4,000) caps the section and `packet.ASSUMPTION_MAX_CHARS_EACH` (500) shortens one over-long line, since review rounds are unlimited by default and the oversized-packet fallback drops the DIFF rather than this section. The record itself is deliberately NOT truncated — that would delete evidence out of the file crash-recovery adoption reads, and since `report_details` is replaced every round it would be the only copy. (Reviewed the same day: the first cut ALSO capped what one round could contribute, 20 lines of 500 characters, applied before the record was written; those caps are removed. The extraction regex was tightened in the same round — `^[ \t>*-]*assumption:` accepted `> ASSUMPTION: <what you assumed...>`, i.e. the prompt's own example quoted back, so an agent echoing its instructions could produce a disclosure it never made in the section a reviewer is most likely to read alone. Only the declaration form, optionally indented, is collected now.) Nothing downstream consumes it — scope is still `commit_range_paths` vs `allowed_paths`, validation is still re-run on the committed tree — so a false or adversarial assumption line can mislead the reviewer's JUDGEMENT and cannot widen scope or authorize a push. Verification check: `rg -n '_to_fault_stop' autoloop/orchestrator.py autoloop/cli.py` (emitted and read), `python3 -c "from autoloop.packet import ASSUMPTIONS_MAX_CHARS, ASSUMPTION_MAX_CHARS_EACH; print(ASSUMPTIONS_MAX_CHARS, ASSUMPTION_MAX_CHARS_EACH)"` prints `4000 500` while `python3 -c "import autoloop.worktask as w; print(hasattr(w, 'MAX_ASSUMPTIONS_PER_ROUND'), hasattr(w, 'MAX_ASSUMPTION_CHARS'))"` prints `False False` (the bounds live at render time, not on the record — a grep would still hit the comment in `worktask.py` explaining why they are gone), `rg -n '_ASSUMPTION_RE' autoloop/implement_executor.py` shows a `^[ \t]*` anchor with no `>`/`-`/`*` in the prefix class, and `rg -n 'stop_kind == "contract"' autoloop/cli.py` (the PASS gate is positive, not `!= "fault"`).

- **2026-08-14** — **`merge` joined the git whitelist and the loop can now push the BASE branch (new finding S29, LOW, deliberate).** Auto-merge (`autoloop/auto_merge.py`) closes the gap between publication and integration: B10 retires a task once its candidate is confirmed on its own side branch, and on 2026-08-06 seven completed tasks sat unmerged, including fixes for failures the loop was still hitting. Two widenings, recorded rather than absorbed. (1) `merge` is the FIRST subcommand on `_ALLOWED_GIT` that moves the checkout's own branch head — it is not a history rewrite, and it carries an F2-style shape check admitting exactly two forms: `merge --abort` with no other token, and a merge of a literal 40-hex commit id (a branch name can move between the merge-window check and the merge). (2) `push_exact` now publishes the base branch, a new destination class, gated by the same `protected_branches` / `allow_protected_push` computation `_dispatch_task_push` uses — so a `main` base merges locally and refuses to publish unless that flag is set. What did NOT change: `reset`/`clean`/`rebase`/`checkout` are still absent, so a merge that fails verification is reported and never unwound; the merged object is a COMPLETED task's candidate re-confirmed on the remote by `ls-remote` at merge time, not merely at completion time; the merge-window predicate is `cli._merge_window_blockers` **called**, not a second copy (a drifted copy is how thirteen tasks get parked at once); and the whole feature is off by default (`policy.auto_merge_enabled = False`). A merge exit of 0 is not treated as evidence — head moved, head contains the candidate, head still contains the old head, tree clean, then push. Verification check: `rg -n 'git_merge_commit|git_merge_abort_shape' autoloop/policy.py` and `python3 -c "from autoloop.policy import PolicyConfig; print(PolicyConfig().auto_merge_enabled)"` prints `False`.

- **2026-08-05** — **Review-packet diff cap raised 8,000 → 30,000 characters (widens what a reviewer sees; the 2026-08-04 residual risk narrows).** The report-first change capped included patch text at 8,000 to avoid the 40,056-character message ChatGPT could not process. That number was a same-day guess with nothing between it and the failure tested, and it blocked rt-02 at 8,971 characters — a 12-insertion/87-deletion change — causing the reviewer to escalate to the operator rather than approve an omitted diff. The cap counts PATCH BYTES, not reviewability, which is what made 8,000 feel defensible while being ~5× tighter than the evidence warranted. 30,000 keeps a ~25% margin below the only measured failure. **This REDUCES the residual risk recorded on 2026-08-04** (a reviewer approving a large change without the patch in front of them): the omission threshold now bites far less often. Everything else is unchanged — an oversized diff is still OMITTED rather than truncated, the changed-path list and diff stat are still complete and git-read, and the executor's report is still labelled as claimed. Verification check: `python3 -c "from autoloop.packet import DIFF_INCLUDE_MAX_CHARS as c; print(c)"` prints 30000, and `test_the_cap_is_sized_from_evidence_not_instinct` fails if the value moves outside its measured bounds.

- **2026-08-04** — **`TRACKER_PATHS` widened from four to six: `CLAUDE.md` and `docs/SCHEMA.md` are now implicitly approved for every scoped task (authorization widening, deliberate; finding #2 unchanged).** Three tasks in two days were refused for the same shape of escape, each writing a doc that described its OWN change: rt-06 updated the backend test count in `CLAUDE.md` §11 (1258 → 1266) that its own 8 new tests made stale, and rt-02 added one migration-table row to `docs/SCHEMA.md` recording that 025's downgrade is now guarded — which is precisely the change rt-02 exists to make. Answering these per task does not converge, for the same reason rt-01 did not: the obligation is repo-wide, so a hand-enumerated scope keeps missing a different entry each time.
  **`CLAUDE.md` is the sharpest entry and is not lumped in with the docs.** Unlike a record, it is the INSTRUCTIONS future agents read, so an executor may now edit the rules it will later operate under without that being named in its task. What bounds this is not trust: the file changes no runtime behaviour, `approved_paths` is still enforced from the Task and never from anything an agent writes (so a CLAUDE.md edit cannot widen the editing task's own scope — pinned by `test_CLAUDE_md_is_implicitly_approved_with_its_risk_understood`), an unscoped task still gains nothing and stays undispatchable, and every edit remains visible in `commit_range_paths`.
  **Residual risk, and it is NEW rather than inherited:** the previous justification for this list leaned on "every tracker edit is visible in the review packet". Since report-first packets (same date) a diff over `packet.DIFF_INCLUDE_MAX_CHARS` is OMITTED, so on a large commit the reviewer sees the tracker PATH in the changed-path list — always rendered, always git-read — but not the edited TEXT. Visibility of the fact survives; visibility of the content does not. That is an argument for keeping this list short, not for trusting it less. Verification check: `python3 -c "from autoloop.tasks import TRACKER_PATHS; print(TRACKER_PATHS)"` must print exactly six markdown paths, and `python3 -c "from autoloop.tasks import effective_approved_paths; print(effective_approved_paths(()))"` must print `()` — an unscoped task gaining trackers would make an unscoped task dispatchable, which finding #2 forbids.

- **2026-08-04** — **Review packets are report-first: the executor's own account is now IN the packet, and an oversized diff is omitted rather than sent (deliberate change to what the reviewer reads; finding #2 unchanged).** The packet carried the entire patch and nothing else the executor said. On rt-09 that was 38 KB of new test code inside a 40,056-character message, and ChatGPT could not process it: the composer accepted it and rendered optimistically (so the loop logged `request_submitted: confirmed`), generation failed server-side, and the turn was never persisted — reloading the conversation showed no user message at all. The loop waited 486s for a reply that could not exist, restarted Chrome between attempts, then tried to rotate to a fresh chat, which failed identically because the same payload could not land there either. Three blockers (`blk-(loop)-013`, `-014`, and the silent-response park before them), one cause.
  Two changes. (1) `TaskExecution.report_summary`/`report_details` persist `ExecutionOutcome.summary`/`.details` at commit time, and `packet._format_executor_report` renders them under an explicit **"CLAIMED by the executor, not read from git"** heading. (2) `packet._format_diff_section` sends the patch only under `DIFF_INCLUDE_MAX_CHARS` (8,000) and otherwise states the real size, says plainly that nothing was truncated, names what IS still complete and git-read (changed paths + diff stat), and tells the reviewer to reply `revise` rather than approve unseen. rt-09's real packet goes 40,056 → 1,898 characters.
  **This reintroduces the executor's voice into the packet, which finding #2 removed from AUTHORIZATION — the distinction is the whole safety argument, so state it plainly.** Nothing downstream consumes the report: path ownership is still checked by comparing git's own `commit_range_paths` against `allowed_paths` (never against the report), ancestry against the real commit graph, and validation by re-running it on the committed tree. A false report can mislead the reviewer's JUDGEMENT; it cannot widen scope, fake a passing suite, or authorize a push. **Residual risk, recorded not hidden:** a reviewer approving a large change now does so without the patch in front of them, so ChatGPT can no longer catch "the report claims X but the diff does Y" on any commit over 8 KB of diff. The mechanical checks above are unaffected, and the omission is loud by design — silent truncation would have been strictly worse, since a partial diff reads as a whole one. Verification check: `rg -n "CLAIMED by the executor" autoloop/packet.py` (label present) and `rg -n "DIFF_INCLUDE_MAX_CHARS" autoloop/packet.py` (cap present, below `GitGateway.RANGE_DIFF_MAX_BYTES`).

- **2026-08-02** — **The dashboard can author tasks, so its POST endpoint now touches authorization (new finding S28).** The read-only tracker's one write path used to express a priority number and nothing else; it now also queues task CREATION requests, which carry `approved_paths`. Recorded as S28 (MEDIUM, open, bounded) rather than folded in silently, because the mitigations are honest but partial: the custom header + Origin check stop a cross-origin page, not a local process; `TaskRegistry.add_many` proves paths are well-formed, not that anyone wanted them. What did NOT change: the endpoint still writes only to the inbox OUTSIDE the checkout (a write into `.autoloop/` mid-run trips the escape detector and parks the loop loop-fatal), there is still no request kind that edits an EXISTING task's scope, no path is inferred or defaulted, no wildcard is representable, and `dashboard.py` deliberately contains no path validator of its own — a second rule set would drift from the one S25 made authoritative. New: queued requests print their paths on the page, so pending authorization is readable before the loop merges it. 19 dashboard tests (was 13), including the endpoint driven over a real socket.

- **2026-08-01** — **Four repository trackers are now implicitly approved for every scoped task (authorization widening, deliberate).** `CLAUDE.md` makes updating `docs/SUMMARY.md`, `docs/TESTS.md`, `docs/SECURITY.md` and `docs/COMMON_ERRORS.md` a CONDITION of doing the work, so a task that adds a file but does not name `SUMMARY.md` cannot be completed while obeying the repo's own rules — the agent must violate one or the other. rt-01 was refused twice for exactly this, the second time after its scope had already been widened by four paths. `tasks.TRACKER_PATHS` + `effective_approved_paths` make the four implicit, combined in ONE place so the dispatch seed, the re-sync, the pre-commit gate and the post-commit path-ownership check cannot disagree (the first cut of the change patched three of the four, and the pre-commit gate then refused what the post-commit check allowed). Bounded: fixed constant rather than configuration, markdown trackers only, an unscoped task stays unscoped and still refuses dispatch (finding #2 unchanged), and tracker edits remain visible in `commit_range_paths` and the review packet — this removes a refusal, not visibility. **Residual risk, recorded not hidden:** `docs/SECURITY.md` is this tracker, so an agent may now edit a finding's record without that being named in its task. It is documentation and not a control, but a weakened finding misleads a later reader — which is why the list is four entries and fixed. See `docs/AUTOLOOP.md` §4f-bis.

- **2026-08-01** — **Codex CLI reviewer, browser demoted to fallback (no new findings; two Verified Strengths added).** The reviewer seat's default moves from a browser conversation to `codex exec`, one process per turn. Security-relevant because it trades a transport that was *structurally incapable* of touching the repository for a local agent that is not: the containment is now explicit (runs outside the checkout, argv never a shell, `doctor` fails on a workdir inside the repo, sandbox flags left empty rather than guessed) and recorded above. Automatic failover to the browser on an exhausted allowance is bounded, gated on no captured reply, and attributed on both request and response, so an approval never loses track of which reviewer produced it. Quota classification is a pure, overridable function and every non-zero exit logs a bounded stderr tail — a missed pattern degrades to an ordinary failure, which authorizes nothing.

- **2026-08-01** — **Document-package ingestion admin-gated (new resolved finding S28).** `GET /books/packages` and `POST /books/import` were auth-gated but not admin-gated, so any registered learner could enumerate operator package inventory and spend server compute on checksum verification — and would inherit a `book_blocks` write path the moment roadmap A3 activates a real persistence backend. Both now take `Depends(require_admin)`, the same gate `POST /phrases/seed` has carried since S17. No service-layer change and no widening anywhere: path containment (`resolve_within`) is unchanged and still asserted directly, because a gate that narrows *who* asks is not a substitute for validating *what* they ask for. No frontend caller existed, so nothing in the UI regresses. `tests/test_books_import_admin.py` +9; the four authenticated HTTP cases in `test_document_package.py` now use an admin token. **Deliberately deferred, not forgotten:** rt-01's approved write scope is `routers/books.py` + the new test file + this file, so the doc updates this change would otherwise carry — the `test_books_import_admin.py` row and the 1258 → 1267 baseline in `docs/TESTS.md`, the `books.py` endpoint list in `docs/SUMMARY.md`, and the admin note on the §5 operator walkthrough in `docs/INGESTION_PIPELINE.md` (whose curl still shows the wrong field name, `package_path` for `package_name`) — were left undone rather than written outside that scope. They need a follow-up with those paths approved.
- **2026-07-31** — **Validation-environment boundary (new resolved finding S27; S24 unchanged).** A task whose declared validation needs a database could not validate honestly in a worker repo, and every available workaround leaked credentials to the write-capable agent. `autoloop/validation_env.py` gives the six DB/JWT variables a one-directional delivery path: an allowlisted, permission-checked, location-checked operator file → the post-writer validation subprocess only, with the same names explicitly REMOVED from the agent subprocess and from every worker git subprocess. `run_validation_commands` no longer lets any validation subprocess inherit `os.environ`, so "no file configured" means "no credentials" rather than "whatever the operator exported" — a shell that sourced `.env` cannot silently change what validation connects to. Values are redacted from validation summaries because that string reaches `state.json`, the transcript, blocker records and the review packet. One exact production marker is refused (the `DB_NAME` this repo declares in `.env.example`); no name heuristics and deliberately no host refusal, since `localhost` is where a legitimate test database lives. **S24 stays OPEN** — this is process separation, not an OS sandbox. 783 hermetic autoloop tests (was 741), plus 1 skipped pending operator-supplied test credentials.
- **2026-07-31** — **Autonomous publication of operator-authored changesets, and one review-integrity check deliberately relaxed.** A changeset written directly on the branch could be reviewed by the loop but never published by it: `_dispatch_git` was retired with S21, and the review stamp's `head_sha` binds the RUNNING checkout rather than the reviewed candidate, so the two coincided only when the branch had already been fast-forwarded. Every infrastructure change therefore needed a manual Publisher run — which is not autonomy, it is a human standing in for a missing component. `changeset_review.py` records `base_sha`/`candidate_sha`/`branch`/`dest_ref`/packet digest in the request binding, the same shape the produce-then-review path already used, and a stamped approval publishes that exact SHA through the Publisher. **The relaxation:** for a changeset-bound `push`, the `head_sha` staleness check is skipped, because the reviewed branch is expected to keep advancing while the packet is out. This is not a weakening: identity moves to the pinned `candidate_sha`, which is more specific than HEAD, and the dispatch independently re-verifies that the candidate still resolves, is still a descendant of the reviewed base, and still carries the reviewed tree — plus `verify_review` against stored request values, the publisher URL snapshot, protected-ref refusal, and post-push `ls-remote` confirmation. A Publisher is REQUIRED on this path with no direct-push fallback, so the retired legacy shape is not reopened. Proven by test: a later, unreviewed commit landing on the branch after the packet is queued does not publish — the recorded candidate does.


- **2026-07-31** — **Two rounds of follow-up review of the same-day worker-isolation pass found three real gaps in it (no new findings opened; S25/S26 addenda).** Round 1: (1) three new `loop_fatal` codes that pass introduced (`primary_checkout_dirty`, `checkout_escape_detected`, `worker_isolation_violation`) had no entry in `cli._RESOLUTION_PRECONDITIONS` — exactly the S26 failure mode, reopened by the fix that closed S26. Mapped `worker_isolation_violation` to the existing `_precondition_worker_environment_drift`, `primary_checkout_dirty` to a new `_precondition_checkout_clean` (re-runs `GitGateway.is_dirty()`), and — at this point incorrectly — `checkout_escape_detected` to the SAME `_precondition_checkout_clean`. (2) `_prepare_write_capable_worker`'s quarantine-and-recreate branch always fetched from the primary checkout, which does not have `execution.candidate_sha` when that commit was made inside the very worker repo being quarantined — a robustness bug (task gets stuck, no authorization bypass), reachable on any third round after an already-committed task's next round fails validation. Fixed by fetching from the quarantined directory itself when resuming a `candidate_sha`. Round 2, on the round-1 fix itself: (3) `checkout_escape_detected` sharing `is_dirty()` with `primary_checkout_dirty` was itself wrong — the escape detector's snapshot covers untracked AND ignored paths, which `is_dirty()` cannot see, so an escape touching only an ignored path (the canonical case: Autoloop's own gitignored state directory) would have cleared the blocker on a recheck that verified nothing. Fixed for real with a dedicated `_precondition_checkout_escape_detected` that refuses UNCONDITIONALLY, never on any recheck. All three bugs were found by an external advisor review, not by the pass's own test suite passing; all three are now covered by dedicated regression tests, including an end-to-end `answer`-through-CLI test using an ignored-path-only escape specifically. 729 hermetic autoloop tests (was 723); root pipeline suite unchanged at 368; `ruff check .` clean; `git diff --check` clean.
- **2026-07-31** — **Autoloop worker isolation hardened: external worker location, escape detection, non-circular task ownership, bounded attempts (three findings closed, one opened — S23 folded into S25/verified-strengths, S24 new, S25/S26 resolved).** An adversarial review of M2's worker isolation (§ the git/subprocess Verified Strength above, and `docs/AUTOLOOP.md` §4c) found the isolation was git-CONFIGURATION isolation only — filesystem location and agent confinement were unaddressed. Closed: (1) `config.workers_root`, a required absolute config value outside the checkout/`.git`/state dir/publisher dirs, replaces the old `state_dir/"workers"` default that put a task's own working repository INSIDE the tree every verification primitive was scoped to; (2) `Task.approved_paths` replaces "the executor's own report" as the post-commit path-ownership check's authorization source — an agent can never widen its own scope by reporting more than it was approved for, and a task with no approved paths can never be dispatched; (3) failed-round residue is quarantined (moved, never reused) rather than silently riding into a later round's commit, and the attempt-count ceiling now persists BEFORE the executor runs, closing the "unbounded pre-commit retries" gap. Opened, honestly: `escape_detector.py` gives the write-capable agent's confinement a DETECTOR (before/after filesystem snapshot of the primary checkout, tracked+untracked+ignored, diffed for creation/deletion/content/symlink/exec-bit changes) — this is new coverage, not a regression, but it is detection after the fact, not an OS-level sandbox, and does not cover the primary checkout's own `.git/` internals; recorded as **S24**, open, on purpose. 723 hermetic autoloop tests (was 671); root pipeline suite unchanged at 368.
- **2026-07-31** — **Autoloop transport recovery (no new findings; two Verified Strengths added).** The loop can now tell a send the browser *disproved* from a send it merely failed to observe, and act on the difference: confirmed absence licenses exactly one same-chat resend, a second confirmed rejection or a wedged conversation licenses at most one rotation to a fresh chat in an explicitly configured project. Two controls are recorded above: (1) network observation is passive and its data model — `path`/`status`/`failure`, no headers, bodies or query strings — makes a credential leak into diagnostics structurally impossible; (2) the post-rotation config heal refuses git-tracked files fail-closed, is atomic and line-surgical, and the rotation itself is budget-capped with no derived project URL. Unknown acceptance still parks for a human and still never resends — the ambiguity rule from the 2026-07-29/30 transport repair is unchanged, only ambiguity's *scope* shrank. 629 hermetic tests (was 584).
- **2026-07-31** — **Blocker records persist operator-facing text, so error messages became durable.** Continuous mode gained task-scoped quarantine: a `task_fatal` park sets one task aside and the loop continues, a `loop_fatal` park stops everything, and both write a `Blocker` record carrying the question text (`autoloop/blockers.py`). Classification is fail-closed — `_to_needs_user`'s `kind` defaults to `loop_fatal` with code `unclassified`, so a park site added later halts the loop rather than silently churning the backlog. Enforcement is not advisory: `policy._check_task_reference` denies a directive naming a quarantined task id, because keeping it out of `next_ready()` alone would not stop a direct reference. Consequence for this file: any value interpolated into a refusal message now also lands in a persisted, printable record. `git_gateway.push_exact` therefore no longer prints the configured `remote.<n>.pushurl` value — a pushurl can carry embedded credentials (`https://user:token@host`), and the refusal only needs to say that one is configured, not what it is. Publisher URL drift messages already went through `publisher.redact_url`.


- **2026-07-30** — **Adopted-manifest content binding (new finding S21).** Autoloop gains a second manifest kind that authorizes commits by *content* (SHA-256 per explicitly named path) rather than executor provenance, so work the loop did not create can be reviewed and committed under an integrity-bound approval. Three review rounds each found the verification reading something mutable — working tree, then working tree after staging (swap-and-restore), then the index (rewritten by `pre-commit`) — so the final design commits an **immutable verified tree** via `commit-tree` + `update-ref` compare-and-swap and **refuses** when any commit hook is active (never bypassed, never emulated). Symlinks are refused at adoption and again on the tree. The same hook weakness in the *executor* commit path is recorded as **S21** and deliberately not fixed in that changeset.

- **2026-07-29** — **Autoloop Phase 3 (no new findings; Verified Strength hardened again).** (1) **`git add -A` eliminated**: the policy whitelist now rejects `-A` and requires explicit `--` paths; commits demand a non-empty path list at the contract level AND must match the task-owned change manifest (content-hash snapshots before/after each executor run) — pre-existing human changes are unstageable by construction, with no config escape hatch. (2) **Single-instance locking** on the state dir (fail-closed, explicit `unlock` that refuses live locks). (3) **Audit subagents are read-only**: headless `claude -p` with `--allowedTools Read Grep Glob` and every editing/executing/delegating tool disallowed; the audit executor itself may write Markdown only (allowlist + at most one dated report). (4) **Phase gate**: `implement` is policy-denied until `policy.implement_enabled=true`. Verification: `pytest autoloop/tests` (329) — esp. `test_add_all_denied_at_the_gateway`, `test_commit_of_preexisting_dirty_file_refused`, `test_live_lock_from_separate_process_fails_closed`, `test_argv_is_read_only_headless`, `test_production_code_refused`.

- **2026-07-29** — **Autoloop Infrastructure Phase 2 (no new findings; Verified Strength hardened).** Contract v2 replaces free-form implementation instructions with task-id authorization against a validated task graph, and adds **review-integrity stamps**: git approvals must echo `{request_id, head_sha, report_sha256}` from the exact request reviewed, and HEAD must not have moved since — replay/reorder/drift approvals are rejected before any git subprocess. New `LLMConversation` seam changes no security posture (same no-credential browser model). 229 hermetic tests (was 127).

- **2026-07-29** — **Autoloop orchestration infrastructure (no new findings; one Verified Strength added).** New `autoloop/` package drives ChatGPT through a browser and executes its directives under a deterministic policy layer. Security posture recorded as a verified strength: whitelisted argv-only git subprocess (force push / destructive ops structurally impossible, denied pre-spawn), commit/push only on explicit contract decisions, no credential storage or login automation (human-managed dedicated Chrome profile over CDP), strict response parser that rejects rather than guesses. Runtime state dir `.autoloop/` gitignored (may hold a private conversation URL). 127 hermetic tests.

- **2026-07-28** — **System word lists, phase 1 (no new findings; one Verified Strength deliberately widened).** Migration 037 makes `word_lists.user_id` nullable and adds `is_system` plus a CHECK pairing the two. Read filters widen from `user_id = $2` to `(user_id = $2 OR is_system)`; write paths are unchanged and still user-only. The CHECK is the control that makes the widening safe — it renders "system list with an owner" and "ownerless private list" unstorable, so the `OR` cannot leak a private row. No system list is created by this migration; seeding is a separate reviewed script. Private-list isolation is re-pinned by the existing cross-user tests running against a database that now contains system rows, and by mutation checks on both the widened filter and the shared-row write guard.

- **2026-07-28** — **Autocomplete Unicode fix; second regex sink closed (S20), S19 scope corrected.** Migration 036 adds generated `word_table.word_norm` and `phrase_blueprint.lookup_key_norm` columns (`lower(btrim(normalize(col, NFC)) COLLATE "und-x-icu")`) plus a prefix index, so `/api/suggest` matches a `normalize_key`-folded prefix on an indexed column instead of folding in SQL. While implementing it, `_suggest_phrases` was found to still carry the raw-regex pattern S19 claimed the file no longer had — filed and fixed as **S20**, and S19 narrowed to the `search()` path with a corrected verification check. Both suggest functions now escape user input (`_escape_regex` for the ARE, `_escape_like_prefix` for the `LIKE` prefix). No new route, no auth change; both endpoints stay auth-gated.

- **2026-07-28** — **Corpus search rewritten for Unicode; regex sink removed (S19 RESOLVED).** `search_service.search` folded case in SQL (`ILIKE` on `word`/`lemma`, `ILIKE`/`~*` on `phrase_blueprint`), which under this database's C collation folds ASCII only — a Unicode bug whose fix also removed a security sink. The single-word blueprint predicate concatenated the raw search term into a Postgres regex (`~* ('\m' || $1 || '\M')`); it is now a Python `re.escape`d literal in `_resolve_blueprint_ids`, and the SQL takes pre-resolved integer ids. No new user-controlled SQL or pattern anywhere in the path; all remaining predicates are `= ANY($1)` over ids the server computed. `/api/search` was and remains auth-gated. `_suggest_words` (autocomplete) is deliberately untouched — it uses `LIKE` with a parameterised prefix, no regex, and its Unicode fix needs a migration.

- **2026-07-27** — **OpenAI-compatible provider added (opt-in; new finding S18).** `OpenAICompatibleProvider` lets the backend target a self-hosted `/chat/completions` server. Default behaviour is unchanged — with no new env vars the provider is Anthropic, and the new class is never constructed. New finding **S18** (LOW) records what enabling it means: the target server usually has no auth, and `LLM_BASE_URL` is an operator-set egress destination for prompt content (read from env at import, never from a request, so not user-controllable). Credential hygiene is test-pinned: `LLM_API_KEY` never appears in an error, and `_redact()` strips `user:pass@` userinfo from the base URL in error strings. No new dependency — `httpx` was already in requirements.txt.

- **2026-07-27** — **LLM provider seam (no new findings, no regression).** `services/llm_provider.py` becomes the only `AsyncAnthropic` construction; `llm_service`, `book_llm_service` and `reading_llm_service` now depend on it. No new route, no path param, no change to how user content reaches a prompt (same system/messages, byte-identical tool definitions — test-pinned). `ANTHROPIC_API_KEY` is still read from env at import time and never logged; the new optional `LLM_MODEL` holds a model name, not a secret. S14 (LLM prompt injection from user content) is unchanged in scope — the seam moves where the client is built, not what goes into the prompt.

- **2026-07-27** — **Vocabulary lists added (no new findings).** New `routers/word_lists.py` + `services/word_list_service.py` (migration 035). Security-relevant properties, all test-pinned: every route is ownership-filtered and answers 404 on another user's id (no enumeration); the only state-changing route (`mark-unknown-learning`) goes through `progression_service` and writes neither `user_word_knowledge` nor `srs_cards` directly; list size is capped at 500 by Pydantic before any DB work; all SQL is parameterized (bulk resolution passes a `text[]` via `$1`, no interpolation). **The `.txt` upload is read client-side only** (`File.text()` in `WordListsPage`) and posted as a JSON string array — there is no server-side file-upload path here, so S8's upload surface is unchanged. Export sets `Content-Disposition: attachment` with a server-generated filename (`word-list-{id}.txt`), not user input.

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
- **2026-05-24** — **S15 resolved:** added the repo's first CI — `.github/workflows/dependency-audit.yml` runs `pip-audit` (backend) + `npm audit --omit=dev --audit-level=high` (frontend) on push/PR to main + a weekly cron. Both blocking; baseline clean (0 vulns each) so green from day one. Audit tooling installed inline (not added to `requirements.txt`/`package.json`). Moved to *Resolved findings*. Open: S2, S9, S12–S14 (+ S3 token-revocation, S8 & S10 long-password residuals). Remaining open items are architecture/infra: S2/S9 (email verification/CAPTCHA), S3 (JWT/session revocation), S12 (Redis-backed limiter); S13/S14 are INFO (f-string-SQL-in-migration caution, LLM prompt-injection).

---

## Change notes — append ONE new line at the END of this file

Where a task records what it changed in THIS tracker when there is no natural
home for it above. **This section stays last in the file, and a note is
appended at the very end of it.**

**It is not where findings go.** A new finding is an *Open findings* entry with
the four required fields, a fix moves that finding to *Resolved findings*, and
a security-relevant change still gets its `Changelog` bullet. All of those live
above, all of them stay ordinary prose that two branches conflict on and a
human resolves — nothing about that changed. This section is only for the
one-line "what my task did here" note, and it exists because that note is what
every parallel branch writes and therefore what every parallel branch collided
on (measured 2026-08-22: bind-01, split-01 and dash-17 each refused a merge
over exactly this file).

Five rules. They are not style — they are the precondition the loop's own merge
path checks before it will combine two branches' notes
(`auto_merge.AutoMerger._merge` → `autoloop/note_merge.py`; the shape is pinned
by `autoloop/tests/test_docs_merge.py`, the reasoning is in `CLAUDE.md` §12):

1. **Add a line. Never grow a line.** Do not append your note into an existing
   finding, bullet or paragraph. The resolver only combines whole lines added
   AFTER everything that was already here; a grown line is an edit, and the
   merge stops.
2. **One note, one line.** Keep it to roughly a sentence. If it needs more, add
   a second line, or put the detail in the finding above that owns it and leave
   a pointer here.
3. **Never edit, delete or reorder a line someone else wrote.** The resolver
   refuses the whole merge if you do, and that refusal is the point: a
   rewritten claim needs a human to look at it.
4. **Order is arrival order, not chronology.** A merge concatenates one
   branch's appended lines then the other's. Read the `Date` and `Task`
   columns, never the position.
5. **Call the marker `CHANGE-NOTES`; never write the comment out again.** The
   section below opens with an HTML comment the resolver finds by that text,
   and it requires the marker to appear EXACTLY ONCE in this file. A second
   copy — the easy way to quote it while documenting how any of this works —
   makes the resolver decline every parallel merge, silently.

Nothing may follow the last note line: this section is the end of the file, so
that the next task's append lands at the end of the ledger rather than inside
whatever came after it. Something appended below it in the older shape — a
`Changelog` bullet, a new `##` section — fails
`test_docs_merge.py::test_every_shipped_tracker_ends_with_an_append_only_section`
rather than landing outside the ledger unnoticed.

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. Never edit a line above. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-23 | notes-03 | This file joined `note_merge.NOTE_TRACKERS` (S35, widened to four paths): a merge conflicting only in the section below is now combined by the loop, while a conflict in any finding, control or `Changelog` bullet above the marker still refuses the whole merge. |
| 2026-08-23 | notes-03 | The section was added BEFORE the path was added to the list, and that order is the rule for any future tracker — a path granted to the resolver without a marker-delimited region is a file whose ordinary prose it would start reasoning about. |
| 2026-08-23 | port-01 | An unconfigured `[paths].state_dir` now resolves beside `workers_root`, outside the tree `escape_detector` snapshots. This REMOVES an exposure rather than adding one: loop state written inside that tree mid-round was a diff indistinguishable from an agent writing where it may not, which is why the inbox, PAUSE, heartbeat and mutation ledger moved first. Nothing about the detector, its ignored-path coverage, or `TRACKER_PATHS` changed — the snapshot still reaches `.autoloop/`. |
| 2026-08-23 | port-01 | Two things NOT weakened, deliberately. No path the loop writes gained a fallback, so nothing can silently resolve back into the checkout; the single legacy read (`workers_dir`) is consumed only by `doctor`'s read-only report. And `config.example.toml` still ships an explicit `state_dir`, so a template-derived deployment keeps today's behaviour exactly — the residual exposure is unchanged for it, not newly created, until that line is removed in the follow-up. |
| 2026-08-23 | loop-03 | New S40: the dashboard now `os.execv`s itself on the strength of a record inside the observed checkout. The bound is that the record decides only WHETHER, never WHAT — the argv is `sys.executable` plus a module name derived from `__main__.__spec__` plus `sys.argv[1:]`, no record field is interpolated, and an underivable launch shape refuses rather than guessing. Pinned by a test driving `--evil-task`, `--evil-path` and a shell metacharacter through a real attempt. |
| 2026-08-23 | loop-03 | Nothing was weakened. The dashboard never writes `pending_upgrade.json`, so S32's escape-detector exemption is untouched and still covers only `tasks.json`; S28's and S32's POST paths are unchanged; and the loop still neither finds nor signals the dashboard, asserted as an absence of `os.kill` / `killpg` / `send_signal` / `pkill` in `cli.py`, `auto_merge.py` and `orchestrator.py`. |
| 2026-08-23 | port-01 | Transition hazard worth stating: `LoopLock` is scoped to `state_dir`, so a loop started under the old default and one started after it hold DIFFERENT lock files and neither refuses the other. Single-instance enforcement is per state dir and always was (`docs/AUTOLOOP.md` §11); moving the default is the first time that can happen without an operator editing a config. Stop the loop before taking the change, or set `state_dir` explicitly. |
| 2026-08-23 | quota-01 | New finding S39. The availability half is the interesting one: the reviewer's own INPUT could declare the account exhausted, because `codex exec` echoes the prompt onto stderr and the classifier searched both streams for `"429"`, `"quota"` and `"rate limit"`. A packet quoting a line number parked the loop loop-fatal twice on 2026-08-22. Closed by a guard, not by narrowing the list. |
| 2026-08-23 | quota-01 | The disclosure half is a narrow amendment: `failure_digest` promised "never stdout" and now carries a bounded, echo-stripped `stdout_tail`, because a codex failure that never writes to stderr was being classified with nothing recorded about it. Argv and the environment are still never recorded, and the reviewer's reply already reaches that file in full (S36). |
| 2026-08-23 | quota-01 | Residual named rather than claimed away: echo stripping is best-effort and a reflowed echo defeats it, so up to 400 characters the loop wrote itself can appear in a record. It cannot affect routing — routing is the guard in `classify`, which never depends on recognising the echo's shape. |
| 2026-08-23 | quota-01 | S39's availability half was reopened on review and re-closed. A LITERAL substring test against the prompt is not enough: a reflowed echo can carry a marker the prompt does not hold verbatim (`quota` + newline + `exceeded` printed back as `quota exceeded`). Classification now drops output lines the prompt accounts for, matches only within a line, and compares both sides ignoring whitespace and punctuation. The finding text, its verification greps and its pinned-by list were updated in place; the residual above is unchanged and still stands. |
| 2026-08-23 | quota-01 | Two digest keys added, neither carrying prompt text: `prompt_guard` is the word `active` or `inert`, and `echo_lines_dropped` is a count. `inert` is the disclosure that matters — the guard has one input, and with no prompt sent it suppresses nothing. That behaviour is correct and unchanged; recording it is what stops "the check quietly switched itself off" from being invisible in the transcript. |
| 2026-08-23 | quota-01 | Amends S39's closing advice, which points at `protocol_errors.py` as the safer alternative: that module had the SAME availability defect. `rate_limit_exceeded`, `rate_limited`, `too_many_requests` and a numeric 429 all raised the loop_fatal `QuotaExhaustedError`, so a short-window throttle parked the loop from a structured field instead of a substring. Split into two vocabularies; only a spent allowance parks. Nothing was widened — this is a denial removed, not a permission added. |
| 2026-08-23 | quota-01 | Correction to the 2026-08-22 changelog bullet for S37, which says quota detection there is "exact matches on named error fields plus a numeric 429": read the 429 clause as superseded. It is a THROTTLE now and routes retryably; everything else in that bullet stands. The bullet is unedited — S37's own summary row and finding text never made the 429 claim, so nothing above the marker asserts it any more. |
| 2026-08-23 | quota-01 | Residual on that side, named: there is no prompt guard on the app-server transport and none is needed (the comparison is exact against a named field, never against printed text), but that also means `codex.quota_error_codes` is obeyed literally — an operator who files a throttle code there still parks the loop. Pinned by a test so it is a documented consequence, not a surprise. The `codex_app_server_failed` digest gained `classification` (one of three words) and carries no new content. |
| 2026-08-23 | notes-04 | S35 gained a SECOND call site: `orchestrator._carry_reviewed_candidate_past` reaches the same resolver through `git_gateway.merge_foreign_commit`'s new `resolve_conflicts` hook, so refreshing a reviewed candidate's stale base combines the append-only sections instead of parking. WHAT may be combined is unchanged — same four paths, same prefix precondition, one shared implementation — and the result is verified by `_finish_resolved_merge` before it counts as merged. |
| 2026-08-23 | retire-01 | No new finding, and none of §14's triggers is touched: no auth, no upload, no raw SQL, no subprocess, no LLM prompt, no path param. `TaskRegistry.retire` gained a precondition and an opt-in dependency rewrite. Recorded because it WIDENS what one operator command may write — `retire` can now edit other tasks' `depends_on` — and that is bounded three ways: only tasks that name the retired id, only ids the operator supplied via `--superseded-by` or an explicit `--rewrite-dependents`, and only through `_validate_depends_on` plus `_check_acyclic` on the whole candidate before any write. |
| 2026-08-23 | retire-01 | Nothing was weakened. Retirement is still written-once with no reverse (a repeat cannot rewrite dependents and refuses the flag rather than ignoring it), `completed` work is still refused, and no dependency of an IN-PROGRESS task can be rewritten by either route. The strand check itself fails CLOSED: it reads stored statuses, never `state_of`, so a graph with a dangling `depends_on` refuses instead of raising `KeyError` into an `except` that would pass it. |
| 2026-08-23 | retire-01 | Revision, and the one place the third bound above is now narrower: `_check_acyclic` no longer sees a SELF-EDGE on the task being retired. Scoped to entries equal to that one id, on that one row, in the throwaway candidate copy — not a skipped check. Dropping the row from the candidate instead WOULD have been a fail-open (`_check_acyclic` uses `color.get`, so a missing key stops every edge into it being walked); a self-edge elsewhere, or any cycle a substitution builds, still refuses with nothing written. |
| 2026-08-23 | retire-01 | The exposure that carve-out could have opened is nil, and the reason is worth recording: no route into a registry can produce the shape it exempts. `from_dict` cycle-checks the whole stored graph on load, `add_many` and `set_depends_on` refuse a self-edge outright, so it is defence in depth against an in-memory corruption. Tested by corrupting a loaded registry rather than by loading a corrupt file — a guard whose test cannot reach it is not a guard, and the round before this had exactly that. |
| 2026-08-23 | ship-01 | S30's mutation vocabulary gained an EIGHTH kind, `shipped_elsewhere`, and it is the widest of them: it moves a task to a terminal status that SATISFIES its dependents, so a request naming the wrong id can unblock work whose prerequisite never landed. Three bounds, none of them trust. The registry refuses an empty commit list and a blank note, so an evidence-free claim cannot be written at all. Every reader re-checks the recorded shas against the current base head. And nothing here is auto-converted. |
| 2026-08-23 | ship-01 | It is also the first kind whose payload is an OBJECT. `inbox._check_shipped_elsewhere` applies the same per-kind rule one level down — exactly `{commits, note}`, both required, typed — at BOTH gates, so a hand-written file (the documented route for most kinds) cannot land half a record with the other half silently dropped. Shape stays the inbox's answer and content the registry's; nothing on the drain path talks to git. |
| 2026-08-23 | ship-01 | The ancestry gate lives in `cli._cmd_record_shipped` and fails CLOSED in both of its bad cases: a commit git says is not an ancestor is refused, and so is one git could not decide about (shallow clone, unfetched object, unreadable repo). "Could not look" is not "verified" — a verification step that queued on an indeterminate answer would switch itself off precisely when it cannot see. It is a GATE, not the guarantee: the record rests on the continuous re-check, not on that one call. |
| 2026-08-23 | ship-01 | Nothing was weakened. No `retire` reverse was added and no route back to `pending` exists; `_refuse_immutable` gained an arm so the new terminal status cannot have its scope, description or dependencies rewritten; the merge sweep's unresolved rule for genuinely completed tasks is untouched; and `cli._merge_window_blockers` gained the state only to PRESERVE an exemption the same tasks already held while parked, never to grant a new one. |
| 2026-08-23 | ship-01 | One refusal is about a DIFFERENT file and was added on adversarial review: a LOOP-raised quarantine cannot be recorded, because its open `blockers.Blocker` record is read independently of the registry by `start`, `health.check` and the heartbeat, and going terminal without closing it orphans an unanswered question. Refused rather than reconciled — `tasks.py` has no blocker store and must not grow one, and closing a question from a drain would forge the operator confirmation `_RESOLUTION_PRECONDITIONS` demands. |
| 2026-08-23 | ship-01 | Residual named rather than claimed away: `policy._check_task_reference` has no arm for the new state and falls through to `Verdict.ok()`, so the primary dispatch gate would admit a directive naming a shipped-elsewhere id. `policy.py` is outside this task's approved scope. `TaskRegistry.mark_in_progress` refuses it as the backstop — the same pairing RETIRED has — but the policy arm is owed, and until it lands the registry guard is the only refusal. |
| 2026-08-23 | stop-01 | No new finding, and none of §14's triggers is touched: no auth, no upload, no raw SQL, no subprocess, no LLM prompt built from user content, no path param. Recorded because a park's `question` now embeds REVIEWER-authored text verbatim for the first time (`code=stop_livelock`). Not a new exposure: `LoopState.stop_reason` has always carried that same unbounded string, S36 already records that the reply reaches the transcript in full, and the question is only ever printed and stored. |
| 2026-08-23 | stop-01 | Three bounds on that text. Nothing PARSES a blocker question, so it cannot reach a decision; no agent brief renders one, so a reviewer cannot forge an `ASSUMPTION:` or `REMOVE-OUT-OF-SCOPE:` line through it (the impl-01 echo channels are untouched); and `health.check` already truncates the question to 200 characters for its detail line. The reviewer also gains no availability capability — one `stop` already ends a run, and the denial budget already ends one. |
| 2026-08-23 | stop-01 | The new state file `.autoloop/stop_repetition.json` holds a digest, a count, timestamps, a session id and the last reason. It lives under `state_dir`, which since port-01 is outside the tree `escape_detector` snapshots, so writing it mid-round is not a diff the detector must reason about. It is written only by the loop, read only by the loop and `status`, and carries nothing an agent supplied. |
| 2026-08-23 | stop-01 | Nothing was weakened, and the one direction that could have been is the reason the ledger fails CLOSED: a counter that cannot be read counts nothing, so reading corruption as "no stops recorded" would have disabled the park permanently with every signal still green — the incident's own shape. An unreadable or unwritable ledger parks `loop_fatal` instead (`stop_repetition_ledger_unusable`), naming the file. |
| 2026-08-23 | impl-02 | No new finding. `WRITE_ALLOWED_TOOLS` and `IMPLEMENT_DISALLOWED_TOOLS` are byte-identical — `Bash` stays disallowed and no tool moved. `implement_executor.AdvisoryValidation` prepares ONE fixed call, not a shell: the commands come from `Task.validation` or the operator's config, the binary allowlist is still `validation.SAFE_VALIDATION_BINARIES`, and `run()` has no parameter, so there is no argument to sanitize. No transport publishes it yet, so no agent can reach it in this build. |
| 2026-08-23 | impl-02 | The credential boundary is unmoved because the run stays IN-PROCESS: the advisory run is `run_validation_commands` in the loop's own process, under the same `ValidationEnv`, with the same redaction applied before any text is returned. The constraint a future CROSS-PROCESS transport must keep is written at the binding site — an MCP server spawned by the CLI must carry neither the values nor the file path, or the agent's own command line would undo `validation_env.py`'s "the writer never learns the file's path either". |
| 2026-08-23 | impl-02 | Two fail-open directions were closed deliberately. An advisory run with nothing to run (no commands configured, missing working directory, launcher raising) answers `NOT RUN`/FAILURE and never a pass, so a channel that cannot check cannot report green. And the round's count and last-run verdict come from this module's counters, never from `result.raw_text`, so an agent claiming green runs it never made moves no number a reviewer reads. |
| 2026-08-23 | impl-02 | Revision round, and it changes the reachability sentence above: a transport now DOES publish the call, so an agent can reach it in this build. Still no new finding. The transport is `AdvisoryRendezvous` — two fixed dotfile paths in the worker repo, reached with the Read/Write the agent already had. No tool was added, `IMPLEMENT_DISALLOWED_TOOLS` is byte-identical, no server or subprocess is spawned for the agent, and no flag reaches the `claude` argv. |
| 2026-08-23 | impl-02 | What the agent can now cause is bounded to: up to `ADVISORY_VALIDATION_MAX_CALLS` runs of the ALREADY-CONFIGURED validation commands, in the executor's own directory, under the executor's own environment. The one byte-stream it controls is the request file's content, which is read only to be handed to `serve_advisory_tool_call` and discarded — no field of it is parsed, so there is no injection surface, and the credential boundary is unmoved (the run is still in-process). |
| 2026-08-23 | impl-02 | The new deletion capability is narrow and is NOT the scope-04 cleanup path. `_remove_entry` unlinks only three FIXED names joined onto the round's own root, contributes no agent-supplied path component, unlinks a symlink as the LINK (never following it), and recursively removes only a directory sitting at one of those three names — which `Write` can create, since it makes parent directories. Nothing else in the tree is reachable from it. |
| 2026-08-24 | scope-05 | S25 amended: the recorded-cleanup authority now also RESTORES a recorded path to its `task_base_sha` content (`REVERT-OUT-OF-SCOPE:`). Authorizing set unchanged — same `out_of_scope_paths`, same exact-match matcher, agent selects and never adds. New exposure is a WRITE rather than a delete, bounded by the content being git's at a loop-written sha no agent can reach: the worst case is a file returned to where the task found it, staged and committed like any other change and visible in the reviewed diff. |
| 2026-08-24 | scope-05 | The write is bounded like the unlink and then some: absolute path, `..`, and a parent outside the worker repo all refused, plus only a `100644`/`100755` BLOB base entry is restorable — a `120000` symlink or `160000` submodule is refused rather than written out as file bytes — a directory at the target is refused (no recursive delete reachable), and a symlink at the target is unlinked as the LINK and replaced, never written through. Only git's one permission bit is set. |
| 2026-08-24 | scope-05 | Fail-closed, with the single fail-open closed by name: an unreadable base tree does NOT fall through to the created-path branch, so it can never silently convert a revert into a deletion. No authority, no base sha, a non-hex base sha and an unreadable base tree all revert nothing and report refused. No agent capability was added — `WRITE_ALLOWED_TOOLS` and `IMPLEMENT_DISALLOWED_TOOLS` are byte-identical and the executor performs the restore. |
| 2026-08-24 | scope-05 | Not wired in production: `cli._build_executor` passes no `revert_authority`, so a live run has no revert authority and the new anchor does nothing. Enabling it is `revert_authority=RecordedRevertAuthority(execution_store)` beside the existing `cleanup_paths_for=`; whoever adds that line owns re-reading S25's 2026-08-24 amendment first. `autoloop/cli.py` was outside this task's approved paths. |
| 2026-08-24 | scope-05 | Revision round, superseding the note directly above: `cli._build_orchestrator` now passes `revert_authority=RecordedRevertAuthority(execution_store)` over the SAME store `cleanup_paths_for` reads, so the capability is live and deleting that argument is a capability change rather than a cleanup. Two paths that were theoretical while nothing in production called `base_sha` are now reachable, and both answer "" without raising: a corrupt execution record (`StateCorruptError`) and a first dispatch with no record at all. |
| 2026-08-24 | recov-01 | Exposure NARROWED, not widened: `browser.restart_command` — the one operator-declared argv the orchestrator spawns outside git — is now unreachable on a run whose `conversation.provider` is not browser-backed. A `codex_cli` run measured 2026-08-22 really launched Chrome (pid 29055) on a subprocess fault. No new subprocess, no new argv, no new config value reaches one; the command, its `list(command)` call and its 120s bound are byte-identical. |
| 2026-08-24 | recov-01 | One new AUTOMATIC action: `_replay_unrecoverable_await` re-invokes a reviewer with the same request id and the same bytes. Its only licence is the transport's own `idempotent_submit` — never inferred, never defaulted-on — and it additionally requires phase `awaiting`, a `reconcile` that CONFIRMS absence (a raising one declines), and `replays_used < MAX_AWAIT_REPLAYS`. No prompt is rebuilt, so `prompt_sha256` still gates the send; a silent transport cannot make it unbounded. |
| 2026-08-24 | recov-01 | Both parks a non-browser run can reach were audited for advice about a subsystem it does not run, not just the restart: `transport_failure_budget_exhausted` is new and names the transport, and `_handle_rate_limited`'s existing `rate_limited` park gained a third `verdict_text` branch, because its unsighted-modal wording tells the operator to `curl 127.0.0.1:9222/json/list` and that "a restart IS the remedy". Unreachable today; the browser's two branches are byte-identical. |
| 2026-08-24 | strand-01 | No new finding, and none of §14's triggers is touched: no auth, no upload, no raw SQL, no subprocess, no LLM prompt, no path param. Recorded because the loop gained a new AUTOMATIC registry mutation — `_reconcile_stranded_tasks` returns an `in_progress` task to `pending` with no operator and no reviewer involved. Its inputs are the loop's own records only (registry status, execution ledger, `LoopState.task_execution`); nothing an agent or a reviewer writes reaches the decision. |
| 2026-08-24 | strand-01 | Four bounds on that mutation. It moves the STATUS and nothing else — no path, no scope, no counter, no ledger entry, and the execution record is neither archived nor rewritten. It fires only for `candidate_sha == ""` and `review_round == 0`, so no reviewed or candidate-bearing work can be discarded by it. It refuses when either attempt ceiling is reached, so it cannot buy a dispatch the budget has already denied. And every fire is written to the transcript with the task id and the fault code. |
| 2026-08-24 | strand-01 | Availability was the direction to get wrong, and the reason the execution record is KEPT rather than retired: retiring it would reset both attempt counters, so an outage could requeue one task forever through a dead API. Keeping it leaves `fault_attempt_count` climbing to `MAX_TASK_FAULT_ATTEMPTS`, which parks the task exactly as it does today. The blocker arm changes no status at all — `blocked` is a merge-window exemption, and granting one to a candidate-bearing record would open the window on live reviewed work. |
| 2026-08-24 | strand-01 | Nothing was weakened. `TaskRegistry.release`'s own precondition is unchanged and a refusal is REPORTED as a blocker rather than swallowed; `mark_in_progress`'s quarantine, retirement and shipped-elsewhere refusals still gate the dispatch that follows a requeue; and the new blocker code is minted outside `_to_needs_user`/`_to_fault_stop` deliberately (it must not park a working loop), which is why it adds no `_RESOLUTION_PRECONDITIONS` key and can be answered without a recheck. |
| 2026-08-24 | strand-01 | Amending the three notes above: that automatic mutation can now also fire on the task `LoopState.task_execution` names, once its dispatch stamp is older than `health.round_ceiling_for` (the agent's own kill ceiling + 1h grace, so the round provably is not executing). TRIGGER widened, BOUNDS unchanged — same safe shape, same two attempt ceilings, same transcript entry, still decided only from loop-written records. The unbounded exemption it replaces was itself the availability failure it was meant to prevent. |
| 2026-08-24 | strand-01 | One new input, and it is loop-written like the rest: `state.current_task["started_at"]`, read only when it names the same task `task_execution` does. Missing, unparseable, another dispatch's, or further in the future than a 2-minute skew grace all read as NO age — which grants no exemption and never silences the sweep, so a corrupted stamp cannot hide a task. It also cannot extend one: only a stamp that parses to an age inside the ceiling exempts anything. |
| 2026-08-24 | bind-02 | S21's retirement is UNCHANGED and its verification check still comes back clean: `GitGateway.commit` and `_dispatch_git` do not exist, `commit`/`commit_and_push` are still refused unconditionally, and an approval that resolves no binding is still refused. What changed is only where a `push`'s binding may be FOUND — inherited across a corrective re-prompt, or resolved from the loop's own record of the packet the approval names — never what a binding permits. |
| 2026-08-24 | bind-02 | The identity checks are unmoved and are what bound both routes. `verify_review` still demands `request_id`, `head_sha` and `report_sha256` exactly, asked of the packet the reviewer names rather than of the most recent send; `_dispatch_task_push` still refuses unless the execution record shows the same candidate, it still resolves, it is on the task's own line and its tree still matches `candidate_tree_sha`. The binding still names ONE sha; nothing new can be substituted into it. |
| 2026-08-24 | bind-02 | Every unreadable input fails CLOSED and says so: `postcommit_binding_from_record` requires all six fields as non-empty strings and otherwise answers None, which REFUSES the approval, and it returns rather than raises because nothing catches a `TypeError` out of `_step_ready`/`_dispatch` — a traceback there ends the process with no park and no blocker. A carry is additionally void unless `state.task_execution` still names the same task and candidate. |
| 2026-08-24 | bind-02 | Two replay-shaped routes were closed rather than left to argument. A published task's ledger entries are dropped, so a repeat approval of an already-answered packet refuses instead of re-running the completion path; and the ledger is bounded at `MAX_SENT_POSTCOMMIT_RECORDS` with eviction that resolves nothing rather than resolving something else. The reviewer's own reach is unchanged — it still cannot name a candidate, only a request id the loop itself minted. |
| 2026-08-24 | bind-02 | Revision round widens WHERE a corrective re-prompt may inherit a binding from — a ledger-resolved one, not only `last_response.postcommit` — and nothing else. It decides what the NEXT request is bound to, never whether a push may publish: the corrected approval is authorized from scratch (`authorize_directive` against the carried binding's own `task_branch`, so a protected branch still refuses), `verify_review` against the correction's own three stamps, and every `_dispatch_task_push` check. The carry's `task_execution` staleness guard is unchanged and still applies. |
| 2026-08-24 | dash-21 | No new finding, and none of §14's triggers is touched: no auth, no upload, no raw SQL, no new subprocess or argv, no LLM prompt, no path param, no settings/CORS/header change. The dashboard's read-only posture is unchanged — `collect_shared` adds no read, writes nothing, and every git call still goes through `_run_status` with `--no-optional-locks`. Recorded because it changes how concurrent unauthenticated localhost requests consume this process's resources. |
| 2026-08-24 | dash-21 | That direction NARROWS. Before, N concurrent `/api/state` requests ran N independent `collect()` sweeps on N threads — an unauthenticated localhost flood scaled git subprocesses linearly and the browser's own 2s poll was already producing about seventeen of them. Now N concurrent callers run ONE sweep. Nothing new is reachable: the route, its `startswith` match, its payload and `Handler.handle`'s upgrade boundary are all unchanged. |
| 2026-08-24 | dash-21 | The availability shape that had to fail safe is the wait. A joiner blocks on `_Sweep.done`, released from `collect_shared`'s `finally` — so on the failure path as well as the success path — and the `except` is `BaseException`, because a `KeyboardInterrupt` or `SystemExit` in the leader must not leave every joined connection parked forever. Nothing is retained between sweeps, so no state a caller induces survives its own request; the page's own guard carries a 120s deadline for the same reason. |
