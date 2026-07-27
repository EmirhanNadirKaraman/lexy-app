# ROADMAP.md

ROI-ranked plan for remaining work. Companion to `docs/TODO.md` (which keeps the
full item history, including resolved items) and `docs/WORKFLOW_AUDIT.md` (which
numbers the workflow holes referenced below).

Last re-ranked: **2026-07-27**. The previous ranking (2026-05-20 PM) went
stale: roughly 35 commits landed between 2026-05-21 and 2026-05-25 —
Spanish as a second language (stages 0–4), the S1–S17 security pass,
the #39 lemma-override layer, #18 scraper language config, and four
rounds of xdist test-isolation work — none of which was reflected here.
Its "recommended next prompt" still pointed at T1.1, which had already
shipped that same day.

What that work changes about priorities: **a second language is now in
production**, so every item deferred with the rationale "only German
exists" has had its deferral condition expire. See §Current priorities.

Historical (unchanged): T1.1–T1.4 done; W1–W8 + W10–W13 done; W1 verified
clean (`npm audit` → 0 vulns); W8's #25 sub-task dropped as overscoped;
W10's BookReaderPage memoization deferred as architecture-not-memo; W11
shipped account-deletion + `/privacy` + `docs/PRIVACY.md`; W12 closed the
pre-launch placeholder, localStorage disclosure, the T3.2 audit (0 rows),
Hole 10 orphan-SRS cleanup (0 rows), and #30. Capacitor partially
installed (`npx cap add ios` complete, `xcode-select` needs full Xcode)
— still DEFERRED.

---

## Current state, in one paragraph

The German loop is complete and green. Progression rules, SRS production
review (Hole 12 / passive front-flip), reading review UI, mastered→known,
mobile responsive #27a–g, PWA shell + icons, dark-mode tristate (T1.3), LLM
rate limit, cache thundering-herd + call-site migration, print→logging,
channel flat-files→DB, channel prefs relational (T1.4 / migration 027),
transcript-click dedup (T1.1), `os.chdir` import hacks — all landed. Since
then: **Spanish shipped as a real second language** (scraper ingest,
language-aware chat + LLM prompts, frontend generalisation, phrase
extractor slices 1–4, `language_config.py` centralisation), a **security
pass closed S1/S4/S5/S6/S7/S11/S15/S16/S17** (8 residuals open, none
HIGH), and the **#39 lemma-override layer** shipped backend-complete
through slice 3C (dry-run adjudication). Capacitor packages installed,
`ios/` scaffolded, `VITE_API_BASE_URL` helper in place, checklist in
`docs/CAPACITOR_READINESS.md`; iOS migration still DEFERRED per the
user's call.

Since that paragraph was written, four more features shipped the same day:
the **ILP playlist optimizer** (backend `greedy`/`ilp` + a Fast/Optimal
selector), **vocabulary lists** (migration 035 — paste or upload a word
list, unresolved and ambiguous surfaces preserved rather than dropped,
mark-unknown-as-learning through `progression_service`, export
round-trips), the **LLM provider seam** (the three ad-hoc
`AsyncAnthropic` constructions centralised into `services/llm_provider.py`),
and an **OpenAI-compatible provider** so the backend can target a
self-hosted model server. Anthropic remains the default throughout.

Verified on 2026-07-27 (current): backend **847 passed / 2 skipped**, root
pipeline **221 passed**, frontend **274 passed across 44 files**,
`ruff check .` clean.

> Historical baselines quoted elsewhere in this file (745 backend, 671
> root, 238 frontend) are as-measured at the time of the entry that quotes
> them. The root suite moved 744 → 221 when `src/app/` and the 537 tests
> targeting it were deleted; it is not a regression.

---

## Remaining work — re-ranked 2026-07-27 (evening)

**This section supersedes §Current priorities below**, which is kept for its
per-item detail and its P-numbers (other docs and the "Direct answers"
section reference `P2`–`P5` by number, so they are not renumbered). Where an
old P-item is still live it is cross-referenced here; where it shipped or was
deferred, its status is updated in place.

Items are labelled **N1–N6** — a fresh namespace, deliberately not reusing
P-numbers.

### How to read the categories

| Category | Meaning |
|---|---|
| **code** | Work in this repo. Normal ruff + pytest loop. |
| **infra/machine** | Happens on a physical machine (desktop, laptop, network). Not a repo change, and mostly not testable from here. |
| **admin/account** | Needs money, identity, or a third-party account. No engineering. |
| **research/deferred** | Investigated, parked with a stated reason. Not actionable until the reason changes. |
| **optional/fun** | Explicitly not core. Never blocks anything. |

---

### N1 — Desktop model server + Tailscale reachability — **infra/machine**
- **Where it happens:** the RTX 4070 desktop and the local network. **Not a repo task** — no code change is expected, and nothing here is verifiable by the backend test suite.
- **The repo side is already done.** `LLM_PROVIDER=openai_compatible` + `LLM_BASE_URL` + `LLM_MODEL` is all the backend needs; see `.env.example` and `CLAUDE.md` §11. `LLM_BASE_URL` accepts any host and is regression-tested against a Tailscale machine name, a `100.x` address and a non-default port.
- **Done when:** a model server is running on the desktop, reachable from the backend host over Tailscale, and one minimal structured request round-trips. The cheapest smoke test is a single `structured()` call against a small schema — if it returns a dict, the seam works end to end.
- **Constraint:** keep the server on Tailscale or another authenticated tunnel. Ollama and llama.cpp ship without meaningful auth; anyone who can reach the port can use the model and read what is sent to it. Recorded as **S18** in `docs/SECURITY.md`.

### N2 — Local-model quality evaluation — **code + measurement**
- **Depends on N1.** Nothing to measure until a server answers.
- **The point:** decide *which call sites* may move off Anthropic, not whether the provider works. The seam makes swapping trivial, which is exactly why this needs a gate.
- **Do not move the grading paths without numbers.** `guided_evaluate` and `evaluate_production` write real SRS state through `progression_service`. A weaker model there corrupts scheduling silently — the user sees wrong intervals weeks later, not a bad answer today. Translations, glosses and OCR repair are the safe places to start: wrong output is visible immediately and cached rather than persisted as progression state.
- **Also worth measuring:** JSON-schema conformance rate. Anthropic's forced tool use is a hard guarantee; local constrained decoding is good but not equivalent. The provider retries once and then raises — a high retry rate is a quality signal, not just latency.
- **Cache note:** switching models re-namespaces every `llm_cache` key (the model is part of the hash). Correct, but expect a cold start and plan for the permanently-cached translations/glosses being rebuilt.

### N3 — Deployment / domain path — **infra/machine + admin/account**
- **Two halves.** *Admin:* buy a domain, pick a host. *Infra:* decide how the backend reaches the model server from wherever it is deployed.
- **The unobvious constraint:** if the backend is deployed off-desktop, it still needs a private path to the model server. Do not open the model server to the internet to solve this — put the deployment host on the same tailnet, or keep the LLM on the deploy host.
- **Already decided, don't relitigate:** single-worker MVP (see P2 below and `docs/SECURITY_ARCHITECTURE_DECISIONS.md` §3). Horizontal scale is gated on a Redis-backed rate limiter, because the in-process limiter also backs the login brute-force guard.

### N4 — Capacitor / native installability — **two independent blockers**
- **N4a — full Xcode — machine.** `xcode-select` points at CommandLineTools; `cap sync` and signing need the full install. Costs disk space and time, nothing else.
- **N4b — real privacy contact email — admin/account.** `PrivacyPage.tsx` and `docs/PRIVACY.md` still ship the literal `<YOUR_REAL_PRIVACY_EMAIL_BEFORE_LAUNCH>` placeholder. Needs an address that will still exist in a year, not a decision.
- These unblock **independently** — doing one does not advance the other. Everything else on the checklist in `docs/CAPACITOR_READINESS.md` is done: packages installed, `ios/` scaffolded, `VITE_API_BASE_URL` helper in place, responsive + theme + PWA shipped.

### N5 — Hole 19: per-message language detection — **measurement-first, deferred**
*Investigated 2026-07-27 (no code written). Real, but far narrower than it
had been described — and the direction of the risk argues against fixing it
now.*

- **Scope is free chat only.** Guided chat persists `language_detected` but
  never gates on it; it uses `target_counted` from the richer evaluation.
- **One decision site in the whole codebase:** `routers/chat.py:204`, choosing
  between `free_chat_used_correctly` (passive+1, **active+1**, active SRS
  card) and `free_chat_mixed_lang` (passive only). Everywhere else the label
  is persisted to `chat_messages` or declared in `types/index.ts:78` and
  **never rendered by any component**.
- **Per-token evidence already exists.** `chat_service.match_learning_words`
  matches against `word_table` with `wt.language = <session language>` and
  `uwk.status != 'known'`. A match already proves that token is a
  target-language item the learner is tracking. The whole-message label
  *overrides* evidence the code has already computed.

**Why not to fix it now — the failure directions are asymmetric.**

| | Today (under-grant) | Naive fix (over-grant) |
|---|---|---|
| What happens | Learner produces the word once more in a cleaner sentence | A homograph in an English sentence grants active credit |
| Recovery | Self-correcting, costs one turn | **None automatic** |

`active_level` drives auto-promotion to `known`, and per `CLAUDE.md` §8b
**auto-promotion is one-way — no event auto-demotes `known`.** So
over-granting silently retires a word the learner cannot produce, and only
manual demotion recovers it. The learner never sees the mistake. German
homographs that read as ordinary English make this concrete: *war*, *die*,
*bald*, *Gift*, *Hut*, *Rat*.

**Concrete cases:**
- `"Ich möchte ein bread kaufen und Brot essen."` — **may under-grant
  today.** *Brot* sat in correct German, but the turn is labelled `mixed`,
  so it earns passive credit only. This is the genuine miss.
- `"I war there yesterday."` — **must never earn active German credit.**
  Any fix that trusts a match alone fires here too.

**Recommended next step is a query, not a feature.**
`chat_messages.language_detected` is already persisted for every turn, so
the evidence is sitting in the database and nobody has looked. Read-only:
count `mixed` free-chat turns, and how many of those also had matched
learning words. If that number is ~0, the hole is theoretical and should be
closed rather than built. **No implementation before that measurement.**

**Reopen when** either holds:
1. Real chat logs show frequent mixed-language turns *with* target-language
   context (the measurement above), or
2. spaced forgetting / auto-demotion ships — that removes the one-way
   promotion asymmetry and makes over-granting recoverable, which is what
   currently makes the cheap fix unsafe.

**If built anyway,** the shape is **per-match, not per-message**: keep the
label as a gate and additionally require the matched token to sit in
target-language context. Never widen on a match alone. Files:
`services/chat_service.py` (return per-match context), `routers/chat.py`
(per-match event choice), `services/matcher_service.py` (expose the spaCy
Doc it already builds). No frontend change, no schema change, no new
dependency. The existing `mixed → passive only` test in
`tests/test_free_chat_progression.py` encodes today's behaviour and would
need rewriting; a homograph regression test (`"I war there"` must not grant
active credit) is the one that protects the asymmetry above.

### N6 — Idle mini-game — **optional/fun**
- Explicitly not core, blocks nothing, and has no design yet. Listed so it is not lost, not because it is queued.

---

### Still open, unranked here (see §Current priorities for detail)

| Item | Status |
|---|---|
| **P5** security residuals (8, none HIGH) + new **S18** | small, deploy-adjacent; `docs/SECURITY.md` is the tracker |
| **#39** live-LLM adjudicator | product/cost decision, not engineering |
| **#37** multi-track subtitle capture | blocked on a product decision |
| **T2.2** LISTEN/NOTIFY | cost-only; not a multi-worker blocker |

### Deferred — do not start as code work

- **#36 Spanish positive fused imperatives** (*"lávate"*, *"levántate"*). Slice A (gerund reflexives) shipped 2026-07-27; positive fused imperatives are **parked with a measured reason, not merely unscheduled**:
  1. **The model swap was measured and rejected.** `es_core_news_md` / `_lg` were compared against `_sm` and do not fix the tagging — this is an upstream tagging issue, not a model-size one.
  2. **The local corpus cannot validate the feature.** It is mostly UNED lecture register, where the target imperatives are essentially absent — so even a correct extractor would have nothing to extract, and no way to show it works.
  Reopening needs one of: an upstream spaCy fix, a normalisation pass that does not depend on the tagger, or a corpus in a register that actually uses imperatives. See `docs/TODO.md` #36.

- **Built-in "B1" and "top 4000" vocabulary lists** — blocked on a provenance
  review, not on engineering. **The source files exist and are git-tracked**
  (`data/b1_parsed.txt`, `data/b1_unparsed.txt`, `data/words_4000.txt`); what
  they are *not* is wired into the backend or exposed as built-in/system word
  lists. They are read only by one-off scripts and the unwired `ilp/` CLI.
  The `b1_*` pair matches the shape of the Goethe-Institut B1 Wortliste, and
  `words_4000.txt` has no recorded source, so **they are not safe to expose
  under CEFR or "top 4000" labels until provenance and licence are verified.**
  Two separate labelling hazards apply even if the licence clears:
  `onboarding.py`'s A1/A2/B1 tiers are self-described as *informal*, not CEFR;
  and `word_table.frequency` is app-corpus (scraped-subtitle) frequency, not
  general German frequency — a German "top 4000" built from it is mostly
  proper nouns and one-offs. **Safer first step:** built-in *Starter German*
  lists from the project-authored `onboarding.py` tiers (150/310/471), named
  for what they are. Full record and measurements in `docs/TODO.md` #43.

---

## Current priorities (2026-07-27, morning) — SUPERSEDED

> Kept for detail and stable P-numbers. See §Remaining work above for the
> live ordering. Statuses below are current.


Ranked. Everything above the line in §Web-polish path is history; this is
the live list.

### P1 — #39 frontend: flag button + admin review queue — ✅ SHIPPED 2026-07-27
- **Frontend only. No backend behaviour changed** — zero backend Python
  files were touched, and `test_lemma_corrections.py` (41 tests) still
  passes unmodified against slices 3A/3B as they already were.
- **Learner flag:** `components/LemmaFlagButton.tsx`, mounted in the SRS
  review reveal (`SRSReviewPage`) directly under the revealed canonical —
  the moment a learner is actually looking at a lemma and can judge it.
  Rendered only for `item_type` `word` / `phrase`: grammar rules carry no
  lemma and the backend's `item_type` is `Literal["word","phrase"]`, so
  offering it there would guarantee invalid submissions. Blank optional
  fields are omitted from the body rather than sent as `''`.
- **Admin queue:** `components/AdminLemmaQueuePage.tsx` at
  `/admin/lemma-corrections`, over `GET /api/v1/admin/lemma-corrections`
  with per-row accept/reject. Accept may carry an overriding
  `corrected_lemma`; blank falls back to the candidate's
  `suggested_lemma` (400 `nothing_to_promote` if both are empty, surfaced
  inline). Rows update **in place** rather than refetching.
- **Admin gating is discoverability, NOT security.** `is_admin` lives in
  `users.settings` JSONB and is exposed in no response model — not the
  token, not `/settings/preferences` — so there is no flag for the client
  to read. `hooks/useIsLemmaAdmin.ts` probes the admin endpoint and hides
  the nav link on a non-2xx. `require_admin` on the routes is the real
  gate; a non-admin who types the URL still gets a server 403, rendered
  as the queue's error state. Do not "harden" this by adding a
  client-side forbidden screen — that would imply the client enforces
  access.
- **`/adjudicate` deliberately not wired.** It returns 503 in production
  (slice 3C ships no adjudicator), so no UI is built against it.
- **Tests:** +19 Vitest (238 across 40 files, was 219/37). See the
  2026-07-27 row in `docs/TESTS.md`.

### P1-followups — #39 remains OPEN overall
Shipping the frontend closes the *reachability* gap, not #39. Two pieces
are still open and both are **product decisions, not blocked engineering**
(see also the "Blocked on a product decision" list below):
- **Live LLM adjudicator** behind `get_lemma_adjudicator` — currently
  returns `None`, so `/adjudicate` 503s by design. Wiring a real
  Haiku-backed adjudicator is a **cost** decision, plus a call on whether
  proposals get persisted as history. Until then the accept/reject path
  is human-only, which is the intended slice-3C posture.
- **Community voting on corrections** — a **stretch idea, not a plan**.
  `docs/TODO.md` #39 records the case against it as the primary
  mechanism: learners are the least-qualified group to adjudicate
  lemmatisation, lemma correctness is objective rather than a matter of
  opinion, and it would need sybil/abuse handling for worse accuracy than
  the curated table. The shipped flag → admin-accept chain is the
  "community surfaces, an authority decides" shape that idea concluded
  was better.

### P2 — Deploy shape — ✅ DECIDED 2026-07-27: single-worker MVP
- **Decision:** the backend is **officially single-worker** for MVP. Both
  deploy paths already were (`Procfile` and `entrypoint.sh` run one
  uvicorn process, no `--workers`, no gunicorn in `requirements.txt`);
  this ratifies it as the chosen posture and gates horizontal scale on a
  shared rate-limit backend. Authoritative write-up:
  [`docs/SECURITY_ARCHITECTURE_DECISIONS.md`](./SECURITY_ARCHITECTURE_DECISIONS.md) §3.
  Warnings placed in `Procfile`, `lexy-app/backend/entrypoint.sh`, and the
  "Deploy posture" block in `.env.example`.
- **Correction to the previous entry.** This was ranked as "one decision,
  three tickets (S12 + #24 + T2.2)". That grouping was wrong — verified
  against the code 2026-07-27:
  - **S12 / `rate_limiter.py` is the only real blocker.** `_windows` is a
    single in-process store, and it backs the **S1 login brute-force and
    password-spray guards**, the S6 client-error DoS guard, and #39's
    flood guard — not just the LLM budget. N workers ⇒ N× the brute-force
    allowance. That makes horizontal scale a **security** change.
  - **#24 is NOT a blocker.** `set_cached` writes with
    `ON CONFLICT (cache_key) DO NOTHING`, so cross-process writers are
    safe; the in-process lock is a thundering-herd optimisation. Worst
    case with N workers: N provider calls on a cold miss. Cost only.
  - **T2.2 is NOT a blocker.** `routers/notifications.py` holds no
    module-level mutable state and is entirely DB-backed. Multi-worker
    safe today; the 3s poll is pure cost. Its duplicate-delivery race
    already exists with two browser tabs on one worker.
- **Also gated, and it fails first:** `entrypoint.sh` runs
  `alembic upgrade head` before starting, so multiple *containers* race on
  migrations regardless of `--workers`. Multi-pod needs a release-phase or
  init-container migration step as well.
- **When this reopens:** the trigger is a real capacity need, not a date.
  Ship the Redis-backed limiter (`SECURITY_ARCHITECTURE_DECISIONS.md` §3)
  before the first `--workers N` or second pod. #24 and T2.2 can then ride
  along on the same Redis, but neither justifies standing it up alone.

### P3 — #36 Spanish phrase extractor, remaining patterns — ⏸ PARTLY SHIPPED, REST DEFERRED 2026-07-27
- **Effort:** M. **Category:** product (second-language parity).
- **Slice A shipped 2026-07-27** — gerund reflexives (`24c49f9`).
- **Positive fused imperatives are now DEFERRED with a measured reason**,
  not merely unscheduled. Two findings, both recorded in `docs/TODO.md` #36:
  (1) the `es_core_news_md` / `_lg` swap was measured and **rejected** — the
  mis-tagging is upstream, not a model-size problem; (2) the local corpus is
  mostly **UNED lecture register**, where the target imperatives are
  essentially absent, so a correct extractor would have nothing to extract
  and no way to demonstrate itself. Reopen on an upstream spaCy fix, a
  tagger-independent normalisation pass, or a corpus in a register that uses
  imperatives.
- Still open and *not* blocked: the verb+preposition allowlist is hardcoded
  rather than data/config; idioms, MWEs and subjunctive are untouched.
- **Why it matters now:** Spanish learners get materially thinner phrase
  coverage than German ones. That was acceptable while Spanish was a
  smoke test; it isn't once Spanish is a shipped language.

### P4 — Hole 19: per-message language detection in free chat
- **Effort:** S–M. **Category:** correctness (second-language).
- **Deferral expired, and it is now the *only* open half of the old W9.**
  Hole 20 (the hardcoded `language='de'`) is **CLOSED** — see W9 below.
  What remains: `evaluate_and_reply` returns one `language_detected` per
  turn, so a mixed sentence ("Yesterday I bought Brot…") gets a single
  label for the whole message.
- **Partial mitigation already shipped** (2026-05-23, "Credit
  target-language words in English-classified free-chat messages"): the
  target word is scanned regardless of the turn's classification, so the
  worst case (losing credit entirely) is already handled. This is now a
  precision improvement, not a correctness hole — ranked accordingly.
- ~~**Fix shape:** when the LLM returns `mixed`, fall back to spaCy and
  score matched tokens per-language.~~ **Corrected 2026-07-27:** that
  describes machinery the code already has —
  `chat_service.match_learning_words` is already spaCy-backed and scoped to
  `wt.language = <session language>`, so matched tokens *are* per-language
  evidence. The gap is that the whole-message label overrides it, not that
  the scoring is missing. Superseded by **N5** in §Remaining work, which
  reclassifies this as measurement-first and deferred.

### P5 — Security residuals (8 open, none HIGH)
- **Effort:** S each. **Category:** deploy-readiness.
- S2 (open signup when no `REGISTRATION_CODE` is set — needs email
  verification or CAPTCHA), S3 (no token revocation; a leaked JWT is good
  for up to 7 days), S9/S10 (minor, residual-only), S12 (see P2), S13/S14
  (INFO). Full detail and per-finding verification checks live in
  `docs/SECURITY.md` — that file, not this one, is the tracker.

### Blocked on a product decision, not on engineering
Do not start these as code work:
- **#37 multi-track subtitle capture** — `docs/TODO.md` says explicitly
  "Decision needed first… don't build the schema change speculatively."
  Is bilingual capture actually wanted?
- **#39 live-LLM adjudicator + community voting stretch** — cost and
  moderation-policy call.
- **T3.1 Capacitor wrap** — needs a full Xcode install on the dev machine
  and a real privacy contact email (`PrivacyPage.tsx` still ships
  `<YOUR_REAL_PRIVACY_EMAIL_BEFORE_LAUNCH>`).

---

## Web-polish path (re-ranked 2026-05-20 PM)

Direction change: finish polishing the web/desktop app to "bug free"
before resuming iOS migration. Capacitor is partially installed (T3.1
through `npx cap add ios`); paused there. The ranking below replaces the
old Tier 2 ordering for the moment.

### W1 — `npm audit` toolchain vulnerabilities — ✅ RESOLVED 2026-05-20
- **Verified:** `npm audit` in `lexy-app/frontend/` reports
  `found 0 vulnerabilities`. The vite/postcss/picomatch/brace-expansion
  CVEs flagged in the prior re-rank are already patched in the current
  lockfile.

### W2 — Hole 1: UX dead-end on never-scraped words — ✅ RESOLVED 2026-05-20
- **Shipped:** new `POST /api/v1/words/learn-anyway` (body
  `{text, language}` with Pydantic `min_length=1, max_length=80`);
  `word_service.learn_word_anyway` does an idempotent
  `INSERT ... ON CONFLICT (word, language, pos) DO NOTHING` with
  `pos='X'`, `lemma=text`, then `apply_progression(...,
  "status_marked_learning", status_override="learning")` inside the same
  pool session. Frontend: `useWordStatus` gains a `learnAnyway` action +
  `learnAnywayError` state field; `WordStatusPicker` shows "Not in
  vocabulary yet" + a "Learn this anyway" button when `onLearnAnyway`
  prop is supplied (back-compat: omitting the prop keeps the
  pre-existing "Not in vocabulary" dead-end label).
- **Schema:** no new column — `pos='X'` (spaCy universal "other") marks
  the sparse row; a future scraper enrichment pass can overwrite the
  fields in place if it sees the same surface in context.
- **Idempotent:** re-calling with the same text returns the same
  `word_id`. `active_level` and `times_used_correctly` stay at 0
  (exposure-only rule). Both passive and active SRS cards are created
  (via #0b's `status_marked_learning` rule).
- **Verified:** backend 479 passed / 2 skipped (was 471; +8 new W2
  tests), frontend 135 passed (was 127; +8 new — 5 picker + 3 hook),
  `tsc --noEmit` clean, `vite build` clean.

### W3 — Hole 2: ILIKE-LIMIT-1 silently picks wrong meaning — ✅ RESOLVED 2026-05-20
- **Shipped:** `GET /api/v1/words/by-text` now returns a discriminated
  `{status: 'not_found'|'single'|'ambiguous', item, candidates[]}` shape.
  Backend query drops `LIMIT 1`, fetches up to 10 candidates, sorts
  deterministically (exact case-insensitive surface match first, then
  lemma asc, then word_id asc) and joins `user_word_knowledge` +
  `srs_cards` per candidate so the UI can label each with current
  status/level. `WordLookupResult` gains `pos` for disambiguation.
- **Frontend:** `useWordStatus` state gains `candidates[]`; on ambiguous
  lookup, transcript-click is **deferred** until the user picks a
  candidate via the new `selectCandidate(c)` action — never recorded
  against an arbitrary `word_id`. `WordStatusPicker` renders a chooser
  with lemma + POS + per-user status chip; selecting one promotes it
  to `lookup` and fires the deferred click with the originally-stashed
  sentence_id. Non-interactive callers (`PlaylistPanel`,
  `GuidedChatPage`) use a new `pickSingleOrFirst()` helper to flatten
  the response for their existing single-pick semantics.
- **Conflict policy with W2:** when ambiguous, the "Learn this anyway"
  affordance is hidden (it only renders on not_found); the user picks
  an existing meaning instead of fabricating a new sparse row.
- **Verified:** backend 485 passed / 2 skipped (was 479; +6 net — 7
  new W3 tests, 4 existing assertions adapted to new shape), frontend
  142 passed (was 135; +7 — 4 picker + 3 hook), `tsc --noEmit` clean,
  `vite build` clean.

### W4 — T2.1: #17 src/app salvage batch 1 (20 tests onto runtime) — ✅ RESOLVED 2026-05-20
- **Shipped:** new `tests/runtime/` directory with 4 test files
  importing the actual runtime modules (`subtitle_cleaner`,
  `pipeline.parse_srt`, `subtitle_merger`, `utterance_unit_extractor`,
  `word_knowledge`, `onboarding`). 20 behavioural tests ported — all
  green on first run. No `app.*` imports in the new files.
  Per-file breakdown: `test_subtitle_cleaning.py` (4 cleaner + 2
  garbage = 6), `test_subtitle_ingestion.py` (4 HTML-passthrough),
  `test_subtitle_merging.py` (4 window + 4 multi-speaker = 8),
  `test_learning_invariants.py` (1 KnowledgeStore dedup + 1 onboarding
  tier monotonicity).
- **Runtime bugs found:** none. Runtime APIs matched the src/app
  refactor exactly for these 20 surfaces (class names, method
  signatures, dataclass field names all align). No production code
  changed.
- **Old artefacts left in place:** `src/app/` directory untouched.
  `tests/{subtitles,learning,exposure,pipeline}/` untouched.
  `conftest.py` sys.path injection untouched. Per spec — this is batch
  1 of N; deletion happens only after all worth-keeping tests are
  salvaged.
- **Verified:** `pytest tests/runtime -v --tb=short` → **20 passed,
  1 warning** in 2.5s. Full root suite `pytest --tb=no -q` →
  **562 passed** (was 542; +20 — exactly the batch).

### W5 — Hole 14: Skip ≠ defer in SRS review — ✅ RESOLVED 2026-05-20
- **Shipped:** new `POST /api/v1/srs/review/{card_id}/skip` →
  `review_service.skip_card` does a single
  `UPDATE srs_cards SET due_date = NOW() + INTERVAL '1 day'`. Touches
  ONLY `due_date` — no progression, no usage event, no level/interval/
  ease/repetitions change. Defer constant
  `review_service.SKIP_DEFER_DAYS = 1` ("not now, ask me tomorrow" —
  matches the SM-2 incorrect-branch interval but skips the penalty).
  Ownership/404 contract identical to `submit_answer`. Frontend
  `SRSReviewPage` Skip button now calls `skipCard()`, shows "Skipping…"
  while pending, advances on success, surfaces "Failed to skip. Try
  again." on failure. Also lifted `getDueCards` onto `apiUrl()` in
  passing (oversight from the W3.0 step-1 sweep).
- **Verified:** backend 494 passed / 2 skipped (was 485; +9 new W5
  tests — defer-by-1-day, no-repetitions-change, no-interval-change,
  no-ease-change, no-levels-change, hidden-from-/due, foreign-user 404,
  missing 404, no-usage-event). Frontend 145 passed (was 142; +3 new —
  success path, failure path, touch-target). `tsc --noEmit` clean,
  `vite build` clean.

### W6 — Hole 11/16 verification: any `learning` items still without active cards? — ✅ RESOLVED 2026-05-20
- **Dev DB audit count: 0.** Dry-run against the current dev database
  reports zero affected rows, confirming #0b's
  `status_marked_learning` fix has fully replaced the pre-#0b shape on
  this dataset. The W2 `learn-anyway` path and the reading-flow
  `save_selection` path both route through `apply_progression(
  status_marked_learning)` so they emit both cards by construction —
  no new code path silently skips the active card.
- **Shipped (safety net):** new
  `backend/services/srs_backfill_service.py` with
  `find_missing_active_cards()` + `backfill_missing_active_cards(
  apply=False)`. Pure SQL — no progression event, no usage event,
  no level/status/times-correct mutation. Idempotent via
  `srs_cards`'s `(user_id, item_id, item_type, direction)` unique key
  + `ON CONFLICT DO NOTHING`. Inserted cards use the
  `_update_srs("create")` defaults verbatim:
  `due_date=NOW(), interval_days=1.0, ease_factor=2.5, repetitions=0`.
  Grammar rules excluded (passive-only by design).
- **CLI:** `scripts/backfill_missing_active_srs.py` —
  dry-run default, `--apply` to commit, `--sample N` to preview rows.
  Re-readable from any cron/maintenance window without breaking state.
- **Verified:** backend 504 passed / 2 skipped (was 494; +10 new W6
  tests — finds-word, finds-phrase, ignores-grammar, ignores-known/
  unknown, ignores-already-fixed, inserts-with-defaults,
  no-uwk-mutation, idempotent, dry-run-doesn't-insert,
  passive-card-untouched). Production: no manual backfill needed
  (count = 0); script is available for future-proofing if a new edge
  case ever surfaces.

### W7 — ErrorBoundary → backend client error reporting — ✅ RESOLVED 2026-05-20
- **Shipped:**
  - `lexy-app/backend/routers/errors.py` — `POST /api/v1/errors/client`,
    auth-optional (`_try_resolve_user_id` never raises: missing/invalid/
    expired tokens collapse to `user_id=NULL`), server-side field caps via
    `_truncate` (message ≤ 2 KB, stack/component_stack ≤ 16 KB, url ≤ 2 KB,
    user_agent ≤ 1 KB, release ≤ 200), 204 on success, blank `message`
    rejected with 422.
  - Migration `028_client_error_log.py` — `client_error_log (error_id,
    user_id NULL, message, stack, component_stack, url, user_agent, release,
    created_at)`.
  - `main.py` wires `errors_router` under `/api/v1`.
  - `lexy-app/backend/tests/test_client_errors.py` — endpoint coverage.
  - `lexy-app/frontend/src/api/clientErrors.ts` — never-throws contract,
    no `assertOk` (a 401 here must not trigger global sign-out),
    `keepalive: true`.
  - `ErrorBoundary.tsx` — fires `reportClientError` from
    `componentDidCatch`; release tag from `import.meta.env.VITE_APP_VERSION`
    (NULL when unset); defensive `.catch(() => {})` so a regression in the
    reporter can't surface as an unhandled rejection on top of the crash.

### W8 — cleanup bundle — ✅ RESOLVED 2026-05-20
- **#28 CSS leftovers:** deleted `src/App.css` (184 lines, not imported
  anywhere). Pruned `src/index.css` from ~424 lines to ~150: removed two
  large commented-out Vite-template blocks, the orphan legacy CSS vars
  (`--text/--text-h/--bg/--border/--code-bg/--accent/--accent-bg/
  --accent-border/--social-bg/--shadow/--sans/--heading/--mono`) and
  their consumers (h1/h2/code/.counter default rules — all overridden
  by inline styles), a stray duplicate `font/letter-spacing/...` block
  that had leaked inside `[data-theme="dark"]`, the dead
  `@media (prefers-color-scheme: dark)` block referencing nonexistent
  `#social`, and the duplicate `body { margin: 0 }`. Built CSS bundle
  dropped 4 kB → 2.34 kB (0.76 kB gz). Theme tokens (#20a/#20b)
  intact.
- **#25 get_knowledge accessor:** SKIPPED per the prompt's overscope
  escape valve. Audit found zero duplication of the single-row
  `(user, item, item_type)` read pattern outside the
  `progression_service` write path; every other `user_word_knowledge`
  site is a JOIN / COUNT / DISTINCT / list-by-user with a different
  access shape. Accessor would have zero adopters. Reclassify or drop
  #25.
- **ResultCard.tsx:** deleted (144 lines). Re-verified zero importers
  before removal.
- **`tests/legacy/`:** deleted (9 ad-hoc phrase_finder debug scripts +
  `__init__.py` + `__pycache__/`). Glob-ignored by root `conftest.py`,
  never collected; zero overlap with `tests/runtime/`. The
  `collect_ignore_glob = ["tests/legacy/*"]` line was removed from
  `conftest.py` in the same commit.
- **Validation:** backend `pytest --tb=no -q` → 512 passed / 2 skipped;
  root `pytest` → 562 passed (matches W4 baseline). Frontend
  `tsc --noEmit` clean, vitest 151 passed, `vite build` clean.

### W9 — Hole 19 + Hole 20: free-chat multi-language + per-message detection — ✅ Hole 20 RESOLVED 2026-05-21 · Hole 19 still open (now tracked as P4)
- **Hole 20 (hardcoded `language='de'`) — CLOSED.** Shipped in "Land Stage
  3 of second-language plan: language-aware chat + LLM prompts"
  (2026-05-21). Migration 031 added a nullable `language` column to
  `chat_sessions`; `chat_service.create_session` takes and stores the
  target language, and `routers/chat.py` reads
  `session_language = session.get("language") or "de"` — the `or "de"` is
  a back-compat fallback for pre-Stage-3 rows, **not** a hardcode. Verified
  2026-07-27: the only `"de"` literals left in `routers/chat.py` are that
  legacy fallback and the comments documenting it.
- **Hole 19 (per-message detection) — still open**, and partially
  mitigated by the 2026-05-23 commit that credits target-language words
  inside English-classified messages. Re-ranked as **P4** in §Current
  priorities; it is no longer bundled with #19, which has shipped.
  **Investigated 2026-07-27** and reclassified as measurement-first /
  deferred — free chat only, one decision site, and the cheap fix risks
  unrecoverable over-granting. See **N5** in §Remaining work.
- **Stale rationale removed:** this item used to read "zero practical
  impact (only German exists)". Spanish shipped 2026-05-21.

### W10 — #21 memoization hotspots — ✅ RESOLVED 2026-05-20
- **usePlayerSentences:** `baseTerms` → `useMemo([surface_form, query])`;
  `hasPrevMatch` / `hasNextMatch` → `useMemo([sentences, sentenceIdx,
  highlightTerms])`. Parsing itself was already inside the fetch effect
  (no per-render re-parse); these two scans were the actual hotspots.
- **RecommendationCards:** all three exports wrapped in `React.memo`
  (`ItemRecommendationCard`, `VideoRecommendationCard`,
  `SentenceRecommendationCard`). `RecommendationsPanel` lifts the
  per-card `onChannelAction` / `onGenreAction` wrappers to stable
  `useCallback`s (was: inline `async (cid, cname, action) => { … }`
  closures created on every render, which would have defeated the
  memo).
- **SearchBar:** debounce bumped 200 → 250ms (spec range 250–300).
  Already had AbortController for stale-request cancellation and
  synchronous local-input echo; both preserved.
- **BookReaderPage:** SKIPPED with reason. `InteractiveBlock`'s
  `selectedKeys` / `savedAnchorKeys` are page-wide sets — every token
  click replaces the Set, so a naive `React.memo` would still re-render
  every block. Partitioning selection state per-block is an
  architecture change, not memoization. Cheap wins (`allSentences`,
  `savedAnchorKeys`, `SentenceCard.tokens`) were already memoized.
- **Verified:** `tsc --noEmit` clean, vitest 151 passed, `vite build`
  clean (+0.16 kB gz from memo wrappers; CSS 2.34 kB unchanged).

### W11 — Account deletion + privacy policy — ✅ RESOLVED 2026-05-20
- **Backend:** new `DELETE /api/v1/account` (`routers/account.py`).
  Single statement `DELETE FROM users WHERE user_id = $1::uuid` — every
  FK to `users` already declares `ON DELETE CASCADE` (private learning
  data: `user_word_knowledge`, `srs_cards`, `chat_*`, `word_lists`,
  `word_usage_events`, `book_*`, `reading_selections`, `notification`,
  `user_channel_preference`) or `ON DELETE SET NULL` (audit signal:
  `content_request`, `client_error_log`). Shared catalog
  (`word_table`, `phrase_table`, `grammar_rule_table`, `channel`,
  `video`, `sentence`, `llm_cache`) has no user FK and is left intact.
- **Frontend:** `api/account.ts` (never-throws-on-401-loop wrapper,
  signals `auth:expired` on success); SettingsPanel gained a
  destructive Account section with two-step confirm (start → "Yes,
  permanently delete" / "Cancel"); buttons 44px tall; failure surfaces
  inline. New `/privacy` page (logged-out accessible); footer link
  added to the global Layout.
- **Docs:** `docs/PRIVACY.md` engineering-side companion enumerating
  cascade / SET-NULL / untouched tables + the App-Store compliance
  gap checklist. Privacy text + page point at `privacy@example.com`
  placeholder — flagged for replacement before launch.
- **Verified:** backend 521 passed / 2 skipped (was 512; +9 new
  tests); frontend 158 passed across 29 files (was 151; +7 new tests
  — 6 settings-panel deletion flow + 1 privacy page); `tsc` clean;
  `vite build` clean (+7 kB JS for PrivacyPage + new test deps).

### W12 — Pre-launch cleanup bundle — ✅ RESOLVED 2026-05-20
- **Privacy placeholder hardened.** `CONTACT_EMAIL` in
  `PrivacyPage.tsx` now `<YOUR_REAL_PRIVACY_EMAIL_BEFORE_LAUNCH>` with a
  TODO comment; rendered as `<code>` not `mailto:` so the placeholder
  can't ship clickable. Mirrored in `docs/PRIVACY.md` checklist.
- **localStorage disclosure shipped.** New "Browser storage" section
  in `/privacy` names `auth_token` + `auth_email`, when they clear,
  and confirms they are not sent in client error reports. No cookie
  banner needed (we don't use cookies for auth).
- **T3.2 active-card audit:** ran the W6 script on dev DB →
  **0 learning items missing active cards.** No backfill needed.
- **Hole 10 orphan-SRS cleanup:** new
  `services/srs_cleanup_service.py` (`find_orphan_srs_cards` +
  `cleanup_orphan_srs_cards(apply=False)`, dry-run default,
  idempotent) + `scripts/cleanup_orphan_srs_cards.py`. Pure SQL —
  never touches `user_word_knowledge`, catalog tables, or valid SRS
  rows. 7 new tests. Audit on dev DB → **0 orphans.**
- **#30 docs/SCHEMA.md:** written as a text companion (table groups,
  polymorphic-key explainer, deletion cascade behaviour, recent
  migration highlights, conventions). `eralchemy` skipped (not
  installed, per spec "don't fight it"); the file documents the
  exact command to produce an SVG later.
- **Verified:** backend 528 passed / 2 skipped (was 521; +7);
  frontend 158 passed; `tsc` clean; `vite build` clean.

### W13 — Audit fixes A + B + C — ✅ RESOLVED 2026-05-20
- **A (notification toast):** `request_failed` was emitted by the
  scraper but rendered as a green success toast with `undefined`
  fields. Frontend `AppNotification.type` union extended to include
  `'request_failed'`; `NotificationToast` now branches explicitly per
  type with danger tokens + `!` icon for failures; success branches
  gracefully degrade when payload fields are missing. 4 new tests in
  `NotificationToast.test.tsx`.
- **B (useWordStatus error surfacing):** `State` gained
  `lookupError` + `statusSaveError`. `selectWord` try/catches the
  `lookupWord` call so a failed lookup no longer leaves the picker
  stuck on a spinner. `updateStatus` surfaces save failures inline
  and keeps the picker open with the prior lookup intact for retry.
  `toggleWordStatus` (right-click path in BookReaderPage) wrapped in
  try/catch — no more unhandled promise rejections. `WordStatusPicker`
  renders inline danger chips for both error fields; `PlayerView` and
  `BookReaderPage` thread the new props through. 8 new tests in
  `useWordStatus.test.tsx`.
- **C (useNotifications lifecycle):** replaced the `cancelRef` boolean
  with `AbortController` per connection + a ref-held reconnect timer.
  Cleanup, token change, and reconnect all abort the prior controller
  before starting a new one. One-hook-one-live-signal invariant proven
  by a StrictMode-style multi-rerender test. 9 new tests in new
  `useNotifications.test.tsx` (fake timers + mocked fetch).
- **Verified:** `tsc` clean; vitest **179 passed across 31 files**
  (was 158; +21); `vite build` clean (+3 kB).

### Deferred until web polish ships

- **T3.1 Capacitor wrap (#34)** — packages installed, `ios/` scaffolded,
  `VITE_API_BASE_URL` helper in place. Paused per direction change.
  Account-deletion + privacy now done (W11). Remaining prerequisites
  before resuming: real privacy contact email, localStorage
  disclosure for EU, App Store Privacy Nutrient Label declarations,
  full Xcode install on the dev machine.
- **T2.2 LISTEN/NOTIFY (#4b)** — cost not correctness. Now folded into
  the single-worker decision (P2); don't cost it separately.
- ~~**T3.3 multi-language pipeline (#18, #19)**~~ — **SHIPPED.** #19's
  `extract_phrases(doc, language)` dispatcher landed 2026-05-21, #18's
  `subtitle-scraper/language_config.py` landed 2026-05-25.
- **Hole 27 spaced forgetting** — DEFERRED by product decision
  (2026-05-21). Re-classified from "open hole" to chosen behaviour;
  re-open when a maintenance-review UX is designed. Manual demotion
  (Hole 26) remains the in-place mitigation.
- **Hole 23 dual-schedule** — accepted, not closed; revisit on
  complaints.
- **#26 a11y, #30 ERD, #31 extractor harness, Hole 10 orphan SRS** —
  Tier 4 polish, do anytime.

---

## Tier 1 — Resolved this session (historical audit trail)

**Order rationale:** T1.1 (passive front-flip) reweights how mastery is
measured — but if T1.3 isn't done first, every passive-level signal feeding
that measurement is still inflatable by repeated transcript clicks. Protect
the input signal before changing the readout.

### T1.1 (was T1.3) — Transcript-click error surfacing + per-sentence dedup (Hole 3 + Hole 4) — ✅ RESOLVED 2026-05-20
- **Shipped:** migration 026 (sentence_id column + stored UTC `event_day` +
  unique partial index `uq_word_usage_events_transcript_dedup`),
  `usage_events_service.record_transcript_click_event` (atomic
  INSERT ON CONFLICT DO NOTHING RETURNING), router accepts optional
  `{sentence_id}` body, frontend `recordTranscriptClick` sends sentence_id +
  throws on non-2xx, `useWordStatus.selectWord` surfaces failures via
  `console.warn`, `PlayerView` + `TranscriptPanel` pass the owning sentence
  through. Tests: +7 backend dedup tests in `test_transcript_click.py`,
  +4 frontend tests in `useWordStatus.test.tsx`, 1 existing TranscriptPanel
  assertion updated to assert the new 2-arg call shape.
- **Verified:** backend 452 passed / 2 skipped (parallel xdist), frontend
  107 tests pass, `tsc --noEmit` clean, `vite build` clean.

### T1.2 (was T1.1) — Flip passive SRS front to English gloss (Hole 12) — ✅ RESOLVED 2026-05-20
- **Shipped:** `review_service.get_due_cards` now assigns
  `prompt_text = gloss, answer_text = display_text` uniformly across both
  directions (was: passive used the inverse). Word/phrase: prompt = English
  LLM gloss, answer = German surface. Grammar rule: prompt = English
  `short_explanation`, answer = German `title` (uniform consequence of the
  flip — no LLM call). `SRSReviewPage.tsx` passive instruction reworded to
  "Recall the German. Reveal, then self-grade." Feedback panel label
  unified to "Target:" (both directions now reveal German). 2 backend
  assertions updated (`test_passive_due_card_has_english_prompt`,
  grammar card mapping). 1 audit-hole regression guard already present.
- **Verified:** backend 452 passed / 2 skipped, frontend 107 passed,
  `tsc --noEmit` clean, `vite build` clean.

### T1.3 (was T1.4) — Dark-mode tristate (`system | light | dark`) — ✅ RESOLVED 2026-05-20
- **Shipped:** `settings_service.DEFAULTS` gains `theme_mode: "system"`;
  `_normalize_theme_compat` derives theme_mode from legacy `dark_mode` (true
  → "dark", false → "light", preserved as explicit choice, not silently
  upgraded to "system"); `dark_mode` is mirrored from theme_mode on writes
  so legacy boolean readers still work. `UserPreferences` /
  `UserPreferencesUpdate` schemas accept `Literal["system","light","dark"]`.
  New `useResolvedTheme(mode)` hook listens to `prefers-color-scheme: dark`
  when mode === "system" (subscribes via `addEventListener` with legacy
  `addListener` fallback, cleans up on unmount); App.tsx Layout writes
  `document.documentElement.dataset.theme = resolvedTheme`. `SettingsPanel`
  swaps the boolean checkbox for a `<select>` with System/Light/Dark
  (16px font, iOS focus-zoom guard; same 600ms debounced save path; optimistic
  data-theme flip on change).
- **Compat policy:** stored `dark_mode=true` → `theme_mode="dark"`;
  stored `dark_mode=false` → `theme_mode="light"`; brand-new users with
  neither set → `theme_mode="system"`. Existing explicit users never get
  silently flipped to system.
- **Verified:** backend 461 passed / 2 skipped (was 452; +9 new tests),
  frontend 114 passed (was 107; +7 new tests), `tsc --noEmit` clean,
  `vite build` clean.

### T1.4 (was T1.2) — #6 channel prefs JSONB → relational — ✅ RESOLVED 2026-05-20
- **Shipped:** migration 027 creates a single `user_channel_preference`
  table `(user_id, youtube_channel_id, preference_kind ∈
  {'followed','liked','disliked'}, created_at)` with PK on the first three
  cols + `(user_id, preference_kind)` index. Backfill expands existing
  JSONB arrays via `jsonb_array_elements_text` inside the same migration.
  `settings_service.DEFAULTS` drops the three channel-array keys (they now
  live relationally); `get_preferences` + `update_preferences` +
  `channel_preference_action` route channel reads/writes through
  `_fetch_channel_prefs` and `_replace_channel_prefs`. `channel_names`
  (display-name cache) STAYS in JSONB by design. Frontend untouched —
  the response shape (`followed_channels: string[]`) is identical, the
  storage just changed underneath.
- **Source of truth:** `user_channel_preference` is authoritative; legacy
  JSONB arrays are ignored on read even if planted manually (regression-
  guarded). Old JSONB keys intentionally NOT deleted from existing rows —
  they're harmless dead weight; a follow-up migration can drop them once
  we're confident no consumer reads them.
- **Conflict policy preserved:** followed coexists with liked; only liked
  vs disliked are mutually exclusive; dislike clears followed AND liked;
  clear removes all three (and evicts the display-name cache entry only
  when no remaining presence).
- **Verified:** `alembic upgrade head` clean (migration 026 → 027),
  backend 471 passed / 2 skipped (was 461; +10 new T1.4 tests),
  recommendation tests all green (no service changes needed —
  recommendation_service reads `prefs.get("followed_channels")` etc.
  which still resolves through the dict shape), frontend 114 passed
  unchanged, `tsc --noEmit` clean, `vite build` clean.

---

## Tier 2 — Good next after Tier 1

### T2.1 — #17 src/app salvage — ✅ RESOLVED 2026-07-27 (salvage complete, tree deleted)
The plan below is preserved as written; what actually happened went further
than batch 1. Phase 1 ran four more salvage batches (runtime coverage
20 → **93** tests across nine files in `tests/runtime/`, including the first
ever coverage of `eligibility.py`, the i+1 core). Phase 2 then deleted `src/`
(3,462 lines), the 537 tests that only targeted it, and the root
`conftest.py` sys.path hack. Root suite 744 → **207** (93 runtime + 114
scraper), all against shipping code. Two accepted losses:
`pipeline_diagnostics.py` (39 profiling tests, no runtime equivalent) and the
11 end-to-end smoke tests (runtime is covered stage-by-stage instead).

Original plan follows.


- **Effort:** M (½ day per batch; plan already in TODO.md).
- **Impact:** HIGH structural. Runtime root pipeline modules ship with zero
  modern coverage; `src/app/` has 537 tests but no callers. Port the top-20
  onto runtime imports, then `git rm src/app/`. Plan B pre-approved.
- **Risk:** Low. Tests are pure transforms (cleaner, merger, SRT parse,
  multi-speaker guard).
- **Why before #34:** Surface hidden runtime bugs before they reach a phone.
- **Deps:** none.
- **Category:** structural debt.

### T2.2 — #4b LISTEN/NOTIFY + 30-day notification retention (Hole 29, 31)
- **Effort:** M. Writer `_notify_user` calls
  `NOTIFY user_notifications, payload`; handler does
  `await conn.add_listener(...)`. Add a periodic job to drop
  `seen=true AND created_at < NOW() - 30d`.
- **Impact:** Cost/perf, not correctness (correctness was #4a). Cuts idle DB
  load ~99%. Important before scaling user count + before Capacitor (background
  SSE on iOS is fragile; reducing client-side polling state to recover helps).
- **Risk:** Low–medium (LISTEN/NOTIFY requires a dedicated connection — handle
  pool exhaustion).
- **Why now:** Multi-worker rollout depends on this too (in-memory rate limiter
  + cache locks both flag the same shape problem; LISTEN/NOTIFY is the cleanest
  one of the three).
- **Category:** cost/perf, deploy-readiness.

### T2.3 — Deploy gate sweep (no single TODO #)
- **Effort:** S. Confirm `CORS_ORIGINS` set in prod, JWT secret rotated,
  `MOCK_LLM=false`, `ANTHROPIC_API_KEY` set, `DB_SSL_MODE=require` (or
  `verify-full`) when the prod DB is remote (S4), `alembic upgrade head` clean
  on the prod DB. Sanity check `Procfile` + that the frontend `vite build`
  references the right backend URL via env (or proxy).
- **Impact:** Required before Capacitor since iOS hits prod.
- **Risk:** Low.
- **Category:** deploy-readiness.

---

## Tier 3 — Bigger strategic work

### T3.0 — Capacitor readiness checklist
See `docs/CAPACITOR_READINESS.md` (2026-05-20). Pre-wrap audit listing
the API base URL strategy, CORS-for-native, auth/token storage, SW
behaviour, App Store requirements, and the recommended implementation
order. Do not start T3.1 until §8.1 (introduce `VITE_API_BASE_URL`) and
§2.7 (privacy policy + account-deletion endpoint) are decided.

### T3.1 — #34 Capacitor wrap (Path B) — ⏸ PAUSED 2026-05-20 PM
- **Status:** packages installed (`@capacitor/core@8.3.4`,
  `@capacitor/cli@8.3.4`, `@capacitor/ios@8.3.4`),
  `capacitor.config.ts` written with placeholder
  `appId='com.lexy.learning'`, `ios/` Xcode project scaffolded, web
  assets copied to `ios/App/App/public/`, Podfile platform bumped to
  iOS 15.0 (Capacitor-8 requirement; template default of 14.0 is a
  known upstream bug), pods installed. Cannot finish `cap sync` until
  full Xcode is installed on this dev machine (`xcode-select` currently
  points at CommandLineTools).
- **Direction change:** user paused iOS wrap to finish web/desktop
  polish first. Resume after W1–W7 in the web-polish path land + the
  Xcode env is sorted + privacy policy + account-deletion endpoint.
- **What's left when resumed:** Xcode signing config, `.env.production`
  with hosted `VITE_API_BASE_URL`, backend CORS update for
  `capacitor://localhost`/`https://localhost`, account-deletion endpoint,
  privacy policy page, TestFlight upload. See
  `docs/CAPACITOR_READINESS.md` §8 for the step-by-step.

### T3.2 — Hole 11/16 — guaranteed active-practice scheduling
- **Effort:** M. Today the only way a `learning` word gets an active SRS card
  is via guided chat or `status_marked_known`. With #0b shipped,
  `status_marked_learning` does create an active card — so this might already
  be partially mitigated. Verify the funnel: any `learning` words still without
  an active card after N days?
- **Impact:** Closes one of the two "almost works" gaps in the workflow audit
  summary.
- **Category:** product / correctness.

### T3.3 — #18 + #19 multi-language scraper + phrase extractor — ✅ RESOLVED
- **#19 (2026-05-21):** `extract_phrases(doc, language)` dispatcher in
  `subtitle-scraper/phrase_finder.py`; `de` → `extract_german_logic`,
  other languages → `[]` (later `es` → `extract_spanish_logic`).
- **#18 (2026-05-25):** `subtitle-scraper/language_config.py` is the
  single source for spaCy model / transcript codes / morphology flag /
  phrase extractor. `pipeline.py` re-exports the historical `LANG_*`
  names, so all 11 call sites were unchanged. Adding a language = edit
  one file. Chose a plain-Python module over YAML (PyYAML is only
  transitively available). See `docs/MAINTENANCE.md`.
- **Follow-on work is now product depth, not plumbing** — see P3
  (Spanish extractor patterns).

---

## Tier 4 — Long-term debt / polish

| # | Title | Effort | Impact | Notes |
|---|---|---|---|---|
| #21 | Memoization (`useMemo`, `React.memo`, search debounce) | S–M | Perf at scale | Do once Capacitor exposes real device perf gaps. |
| #25 | Single `word_service.get_knowledge(user, item, type)` accessor | S | Cleanup | Painless refactor; do alongside any service touching `user_word_knowledge`. |
| #26 | Accessibility (ARIA, focus management, alt text) | M | Required for some App Store regions | Pair with Capacitor review prep. |
| #28 | Clean `index.css` + `App.css` of Panda/Vite leftovers | XS | Polish | 10 min. |
| #30 | ERD / `docs/SCHEMA.md` | S | Onboarding | `eralchemy` one-shot. |
| #31 | Extractor thresholds validation harness | M | Robustness | Only if PDF imports start failing. |
| Hole 27 | Spaced forgetting (auto-demote `known` after long silence) | M–L | Real product change | **Deferred by product decision (2026-05-21)** — manual demotion via Hole 26 already covers "I forgot this". Re-open behind a maintenance-review UX, not as a silent behaviour change. |
| Hole 10 | Orphan SRS cards cleanup | XS | Operational hygiene | Script + scheduling docs landed 2026-05-21. See [docs/MAINTENANCE.md](./MAINTENANCE.md) — recommended weekly `--apply` at a quiet hour, wire into Render cron or platform scheduler. |
| Hole 14 | Skip ≠ defer in SRS review | S | UX nit | Tell backend to bury for today. |
| Hole 33/34 | Per-direction `last_seen`, `(item_id, item_type)` rec keys | S–M | Future-proofing | Do before adding phrase coverage to ranking. |

---

## Tier 5 — Probably defer / reclassify

| Item | Reclassify to | Why |
|---|---|---|
| **#5 Hole 23 dual schedule reconciliation** | DEFER — revisit on user complaints | TODO.md already says "accepted, not closed". No signal it confuses users. PK bridge (UUID vs SERIAL) is bigger than the win. |
| **#5e backfill of pre-2026-05-18 inflated active progress** | DROP (forward-only) | TODO.md recommends not running it. `status='known'` is the load-bearing field; inflation is invisible downstream. |
| **#33 `tests/legacy/`** | Just delete it | Glob-ignored, no one looks. Decide once. |
| **#27 remaining "media queries / 900px container"** | Mostly absorbed by #27a–g | Audit shows it's mostly done. Spot-check on a real phone instead of opening a new ticket. |
| ~~**Hole 19/20 — free chat language hardcoded `'de'`**~~ | **Resolved / re-ranked** | Hole 20 (the hardcode) shipped 2026-05-21 (Stage 3, migration 031). Hole 19 (per-message detection) is now **P4** in §Current priorities — the "no second language exists yet" rationale expired when Spanish shipped. |

---

## Direct answers to common planning questions

**1. Start #34 (Capacitor) now?**
No — and the blocker is no longer engineering. T1.1–T1.4 and the whole
web-polish path landed. What's left is a full Xcode install on the dev
machine and a real privacy contact email. Until those two exist, T3.1
cannot proceed regardless of code readiness.

**2. Next single best task.**
~~P1 — the #39 frontend.~~ **Shipped 2026-07-27** (see §Current
priorities). The signal→authority chain is now reachable end to end:
learners can flag, admins can accept/reject, accepting writes the
override.

~~Next up is **P3** — the remaining Spanish extractor patterns.~~
**Superseded 2026-07-27 (evening).** Slice A shipped; positive fused
imperatives are deferred on measured grounds (model swap rejected, local
corpus in the wrong register — see P3 above).

The next step is **N1 — stand up the model server on the desktop and reach
it over Tailscale.** Note the shape change: that is an
**infrastructure/machine task, not a repo change**. There is no code to
write, no test to add, and the backend side already ships. See §Remaining
work for the full N1–N6 ordering.

**3. Avoid right now.**
- #5 reconciliation (Hole 23) — no user signal yet.
- #5e backfill — forward-only by decision; don't touch prod data.
- Hole 27 spaced forgetting — DEFERRED by product decision (2026-05-21);
  not a code task until a maintenance-review UX is designed.
- #37 multi-track subtitles — blocked on a product decision, and the TODO
  explicitly warns against building the schema change speculatively.
- New top-level Python files, or recreating a parallel package tree like the
  old `src/app/` — that refactor was deleted 2026-07-27, not paused.

**4. Reclassify as no longer worth doing.**
- #5e (drop entirely).
- #5 / Hole 23 (defer indefinitely; reopen on signal).
- #27 remaining stages (mostly absorbed by 27a–g; spot-check, don't ticket).
- ~~Holes 19/20 bundled into #19~~ — obsolete: #19 shipped, Hole 20 is
  closed, Hole 19 is ranked on its own as P4.

**5. What about the rest of #17 (`src/app/` salvage)?**
~~Batch 1 shipped; batches 2+ never started.~~ **Done 2026-07-27.** Four more
salvage batches took runtime coverage 20 → 93 tests, then `src/` (3,462
lines), the 537 tests targeting it, and the `conftest.py` sys.path hack were
all deleted. Root suite 744 → 207, every test now against shipping code.
See T2.1 above.

---

## Recommended next prompt (paste back to continue)

> Replaced **2026-07-27 (evening)**. The previous contents asked for the
> remaining **Spanish extractor patterns (#36 / P3)** — that work is now
> partly shipped (slice A) and partly **deferred on measured grounds**, so
> pasting it would start work that was deliberately parked. Before that it
> asked for the #39 frontend, and before that T1.1 — both already shipped
> when they were read. **Check the item is still open before pasting.**

**The next step (N1) is not a repo task.** It happens on the RTX 4070
desktop: install a model server, expose it over Tailscale, confirm the
backend can reach it. There is no code to write — `LLM_PROVIDER`,
`LLM_BASE_URL` and `LLM_MODEL` already ship, and `LLM_BASE_URL` is
regression-tested against Tailscale-style hosts. So there is no prompt to
paste for N1; the checklist is:

1. Run an OpenAI-compatible server on the desktop (Ollama's `/v1`,
   llama.cpp's `llama-server`, vLLM, LM Studio — the adapter covers all of
   them).
2. Put both machines on the tailnet. **Do not port-forward or bind the
   model server to a public interface** — it has no meaningful auth (S18).
3. On the backend host, set:
   ```
   LLM_PROVIDER=openai_compatible
   LLM_BASE_URL=http://<desktop-tailscale-name>:11434/v1
   LLM_MODEL=<the model the server serves>
   ```
4. Smoke-test one structured call. A misconfigured pair fails at **import**
   by design, so a backend that starts at all has already proved it can
   construct the provider.

Once a server answers, **N2** is the first real repo task, and it is
measurement rather than construction:

```
Evaluate local-model quality against the current Anthropic baseline, and
recommend which LLM call sites (if any) can move off Anthropic.

Context:
- services/llm_provider.py has two backends behind one interface. Anthropic
  is the default; OpenAICompatibleProvider points at a self-hosted server.
- Switching is an env-var change, which is exactly why this needs a gate.
- Read docs/TODO.md #42 and CLAUDE.md section 11 before starting.

The gate that matters:
guided_evaluate and evaluate_production write real SRS state through
progression_service. A weaker model there corrupts scheduling silently -
the user sees wrong review intervals weeks later, not a bad answer today.
Translations, glosses and OCR repair are the safe places to start: wrong
output is visible immediately and cached rather than persisted as
progression state.

Measure, per call site, on a fixed input set run through both providers:
1. JSON-schema conformance rate. Anthropic forced tool use is a hard
   guarantee; local constrained decoding is not equivalent. The provider
   retries once then raises, so a high retry rate is a quality signal.
2. Output quality against the Anthropic response as reference.
3. Latency, including the retry path.

Constraints:
- Do not change provider selection defaults. Anthropic stays default until
  the numbers say otherwise.
- Do not move guided_evaluate or evaluate_production in the same change as
  the measurement.
- Note that changing the model re-namespaces every llm_cache key (model is
  part of the hash), so a switch means a cold cache, including the
  permanently-cached translations and glosses.

Report the per-call-site numbers and an explicit recommendation of which
sites may move, which may not, and why.
```
