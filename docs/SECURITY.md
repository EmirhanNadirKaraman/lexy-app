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
| S24 | HIGH | Write-capable agent isolation is DETECTED (checkout snapshot diff), not PREVENTED (no OS-level sandbox); `.git/` internals not covered | `autoloop/escape_detector.py`, `autoloop/orchestrator.py` |

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
BEFORE `commit_and_capture` runs (`changed_paths_outside_approved` —
nothing can roll a commit back once it exists), and `execution.
allowed_paths` is seeded once from it and never unioned again (the
post-commit check remains as defense in depth for what the pre-commit gate
cannot see — a hook adding a path after it runs).
`Orchestrator._prepare_write_capable_worker` requires the worker repo clean
before every write-capable dispatch; residue is QUARANTINED (moved, never
deleted — `WorkerRepoManager.quarantine`) rather than reused, and a fresh
repo is created from the last committed round (or the task base if none).
`attempt_count` now increments and persists BEFORE the executor is ever
called, so it is durable across a crash, a restart, or a validation
failure. See `docs/AUTOLOOP.md` §4e for the full design and
`autoloop/tests/test_m1_hardening.py` for the adversarial tests (agent
self-report cannot widen scope; a failed attempt's files never reach a
later commit; the attempt budget survives a simulated restart).

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
- **Notifications SSE** requires `get_current_user` and scopes rows to the user (`routers/notifications.py:65`).
- **Content-request subprocess** uses `create_subprocess_exec` with fixed args (`routers/content_requests.py:26`) — no shell, no command injection.
- **Autoloop git/subprocess + browser surface (added 2026-07-29; hardened same day by Phase 2).** The Fable↔ChatGPT loop (`autoloop/`, `docs/AUTOLOOP.md`) runs git only via `subprocess.run(["git", ...])` — argv list, no `shell=True` — and only after `PolicyEngine.validate_git_command` passes a **whitelist** (subcommand + per-subcommand flags, `autoloop/policy.py`). Force pushes and destructive subcommands (`reset`, `clean`, `rebase`, `checkout`, …) are denied *before* any subprocess spawns, and no config knob can enable them. LLM-controlled strings (commit message, staged paths — they originate from ChatGPT's directive) are passed only as individual argv elements, never interpolated. **Phase 2 adds review-integrity enforcement:** every request is stamped (request_id, head_sha, base_sha, SHA-256 of the report); a `commit`/`push` directive must echo the stamp of the request it answers (`contract.verify_review`) AND the repository HEAD must still equal the approved head at execution time — so a git approval can never be applied to a state ChatGPT did not actually review (replayed, reordered, or post-drift approvals are rejected deterministically). The browser side stores **no credentials**: it connects over CDP to a human-launched, pre-logged-in dedicated Chrome profile and never automates login. **Verification:** `pytest autoloop/tests/test_policy.py autoloop/tests/test_git_gateway.py autoloop/tests/test_contract.py` — includes `test_force_push_has_no_config_escape_hatch`, `test_denied_command_never_reaches_subprocess`, `test_verify_review_rejects_mismatch`, and the orchestrator-level `test_stale_stamp_rejected_and_nothing_committed` / `test_head_moved_since_review_rejected`. Keep the whitelist additive-only; never add `shell=True`, a force-flag knob, or a bypass around `verify_review`. **2026-07-31 (worker-isolation pass):** the `ls-files` entry was widened from `{"-s", "-z", "--"}` to also allow `--others --ignored --exclude-standard` (`autoloop/policy.py`) — every added flag is read-only enumeration (needed by `escape_detector.enumerate_checkout_paths` / `git_gateway.list_untracked_paths` / `list_ignored_paths` to see untracked and ignored paths `git status` alone would hide), consistent with "additive-only" — no new subcommand, no write-shaped flag.
- **Autoloop network observation is observation only, and cannot carry a secret (added 2026-07-31).** The transport now reads the browser's own send traffic to tell "the send failed" from "we didn't see the send" (`autoloop/browser/observation.py`). Three properties keep that from becoming an exposure. (1) **It issues nothing.** The listener is a passive `page.on("response")`/`on("requestfailed")` handler; there is no request-issuing method on the session protocol, so it cannot become a second transport that bypasses the DOM path. (2) **The vocabulary cannot express a secret.** `SendObservation` has exactly three fields — `path`, `status`, `failure`. There is nowhere to put a header, a cookie, an `Authorization` value, a request body or a response body, and `scrub_path` drops the query string before a path is ever recorded, so a diagnostics dump or transcript line cannot leak credentials even by accident. Response bodies are never read; the classifier deliberately works from status codes alone. (3) **It composes with, never replaces, the existing no-credential guarantees** — the session protocol still exposes no cookie/storage accessor, and the profile is still human-logged-in over CDP. **Verification:** `pytest autoloop/tests/test_transport_recovery.py -k "observation or secret or query"` — includes `test_observation_vocabulary_cannot_express_a_secret` (asserts the field set exactly), `test_observed_paths_drop_query_strings`, and `test_rejected_submission_logs_only_path_status_and_failure`, which greps the written transcript for `cookie`/`authorization`/`bearer`. **Do not** add a body/header field to `SendObservation`, and do not "enrich" observations by reading `response.body()` — the message id it would give you is not worth putting message content and auth material into a log.
- **Autoloop conversation rotation cannot edit tracked files or escape its budget (added 2026-07-31).** A rotation is the loop reacting to a fault by writing to the filesystem, so it is gated three ways. `config_writer.assert_untracked` **refuses** to rewrite a git-tracked config and fails closed if git cannot be consulted at all — the heal only ever touches the gitignored `.autoloop/` config, so a browser fault can never produce a repository change. The rewrite is line-surgical and atomic (temp + `os.replace`), and refuses a URL containing a quote, so it cannot corrupt a config that `load_config` would then refuse to parse. `PolicyEngine.check_rotation_budget` caps rotations per run (default 1), and no project URL configured means no rotation at all — the target is configured explicitly and never derived from the conversation URL, so the loop cannot open a chat somewhere the operator did not choose. **Verification:** `pytest autoloop/tests/test_transport_recovery.py -k "config or rotation_cap or project_url"`. **Do not** make `assert_untracked` advisory, and do not add a fallback that derives `project_url` from `conversation_url`.
- **Autoloop worker repos are provably OUTSIDE the checkout, and a task's write scope is authorized before the writer ever starts (added 2026-07-31, S23/S25).** `config.workers_root` is a required, absolute config value; `worker_env.validate_workers_root` refuses one nested beneath the checkout, its `.git` (including a linked worktree's real gitdir), the state dir, or the publisher paths, checked both at real-dispatch construction time (`cli._build_orchestrator`, raises) and by `doctor` (a `fail` check). `Task.approved_paths` is the ONLY thing a write-capable dispatch's post-commit path-ownership check is validated against — never anything the executor/agent itself reports — and a task with none can never be dispatched. **Verification:** `pytest autoloop/tests/test_m1_hardening.py` (52 tests: workers_root refusal/acceptance, escape-detector snapshot diffing over tracked/untracked/ignored/symlink/exec-bit changes, agent-report-cannot-widen-scope, failed-round quarantine, attempt-budget-survives-restart, blocker-precondition exhaustiveness). **Do not** reintroduce a fallback from `config.workers_root` to the old `config.workers_dir` default, and do not union `execution.allowed_paths` with `outcome.changed_paths` for a non-audit task again (see S25) — see S24 for what remains open (detection, not an OS sandbox).
- **Document-package path containment (roadmap A2, added 2026-07-29).** A document package is untrusted input: it is produced by an offline worker and may arrive from another machine, and every path inside it (`CHECKSUMS.txt` entries, `page_images.path_template`, per-element `image_path`/`asset_path`) is attacker-influenced if the package is. `services/document_package/loader.py:resolve_within` is the **single chokepoint** — no file in a package is opened, hashed or recorded unless it resolves inside the package root. It refuses `../` traversal, absolute paths, and symlinked escapes (`Path.resolve()` follows links *before* the containment test, so a symlink pointing outside is caught). The API surface never accepts a path: `POST /api/v1/books/import` takes a package **name**, which is itself passed through `resolve_within` before any I/O (`routers/books.py`), mirroring the S7 pattern of validating at the boundary. **Verification:** `pytest tests/test_document_package.py -k "Containment or traversal"` — parametrized over `../`, `../../etc/passwd`, `pages/../../../etc/passwd`, absolute paths, a real symlink escape, a hostile `path_template`, and a hostile package name. **Do not** replace `resolve_within` with `os.path.join` + a string `startswith` check; that misses symlinks.
- **All routers were swept for auth (2026-05-24).** Every handler is covered by `get_current_user` (router-level or per-handler) **except** the documented public ones — see the unauthenticated-surface list below. `search.py` is auth-gated at the router level; `playlists/generate` is auth-gated and is DB-only (no LLM, so it correctly does not need `rate_limit_llm`); `phrases/seed` is auth-gated **and** admin-gated via `require_admin` (S17 resolved). `POST /sentences/match` is now auth-gated too (S16 final fix), so **every** API handler requires a bearer token.
- **Admin-gated routes (`require_admin`, 403 `admin_required` for non-admins):** `POST /phrases/seed` (S17), `GET /admin/lemma-corrections` (#39 3A), `POST /admin/lemma-corrections/{id}/{accept,reject}` (#39 3B), and `…/{id}/adjudicate` (#39 3C, read-only dry-run). All rely on `is_admin` being un-self-grantable (settings writes are `DEFAULTS`-filtered).
- **User-signal ≠ authority (#39 3A/3B).** `POST /api/v1/lemma-corrections` (auth + per-user throttle) writes only the `lemma_correction_candidate` *signal* table — it can NEVER mutate `lemma_override` (the table the extractor/matcher trust); regression-guarded by `test_post_never_mutates_lemma_override`. The **only** path from a user signal to `lemma_override` is an **admin** `POST /admin/lemma-corrections/{id}/accept` (3B) — human-gated, transactional, never automatic; no raw-vote auto-promotion.

> **Doc-drift note:** `CLAUDE.md` §7 calls `/api/search`, `/api/suggest`, `/api/video-sentences`, `/api/word-forms`, `/api/languages`, `/api/categories` "public legacy endpoints." They are actually auth-gated at the router level (`routers/search.py:20`, `APIRouter(dependencies=[Depends(get_current_user)])`). No data leak — but the §7 label is stale and should not be trusted when reasoning about the public attack surface.
>
> **The genuinely unauthenticated surface is:** the static file mount (`main.py:142`) and `POST /api/v1/errors/client` (per-IP throttled — S6 resolved). The FastAPI docs `/docs` + `/openapi.json` are off by default (require an explicit `ENABLE_DOCS=true` — S11 resolved). `POST /sentences/match` is no longer public (auth-gated — S16 final fix). Everything else requires a valid bearer token.

---

## Resolved findings

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

**Scope — this does NOT close S24.** It separates credentials from the writer
PROCESS. It is not an OS sandbox and does not stop a process that can already
run arbitrary code from reading the file. The write-capable agent still has no
path jail.

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
