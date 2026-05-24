# ROADMAP.md

ROI-ranked plan for remaining work. Companion to `docs/TODO.md` (which keeps the
full item history, including resolved items) and `docs/WORKFLOW_AUDIT.md` (which
numbers the workflow holes referenced below).

Last re-ranked: 2026-05-20 (later same day — direction changed: finish
polishing the web/desktop app to "bug free" first, then resume iOS
migration. T1.1–T1.4, the four Capacitor-prereq Tier-1 items, are all
done. W1–W8 + W10 + W11 + W12 + W13 also done; W1 verified clean
(`npm audit` → 0 vulns); W8 #25 sub-task reported as overscoped and
dropped; W10 BookReaderPage memoization deferred as
architecture-not-memo; W11 shipped account-deletion endpoint +
`/privacy` page + `docs/PRIVACY.md`; W12 closed the pre-launch
placeholder, localStorage disclosure, T3.2 audit (0 rows), Hole 10
orphan-SRS cleanup (0 rows), and #30 schema doc. W9 (free-chat
multi-lang) stays deferred — bundle with #19 when a 2nd language
ships. Web-polish + privacy + Tier-3/4 maintenance are
**complete**. Capacitor itself partially installed (`npx cap add ios`
complete, `xcode-select` needs full Xcode before next sync) —
DEFERRED.

---

## Current state, in one paragraph

Progression rules, SRS production review (Hole 12 / passive front-flip),
reading review UI, mastered→known, mobile responsive #27a–g, PWA shell +
icons, dark-mode tristate (T1.3), LLM rate limit, cache thundering-herd +
call-site migration, print→logging, channel flat-files→DB, channel prefs
relational (T1.4 / migration 027), transcript-click dedup (T1.1), `os.chdir`
import hacks — all landed. Capacitor packages installed, `ios/` project
scaffolded, `VITE_API_BASE_URL` helper in place, readiness checklist
documented in `docs/CAPACITOR_READINESS.md`. iOS migration is DEFERRED on
the user's call — finish web/desktop polish first.

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

### W9 — Hole 19 + Hole 20: free-chat multi-language + per-message detection
- **Effort:** M. Today `chat.py:175` hardcodes `language='de'` and
  `evaluate_and_reply` returns one `language_detected` per turn — a
  mixed sentence ("Yesterday I bought Brot…") may misclassify.
- **Fix:** thread `current_user_language` through; if LLM returns
  `mixed`, fall back to spaCy + scoring matched tokens per-language.
- **Impact:** Currently zero practical impact (only German exists);
  blocks any second language. Bundle with #19's multi-language phrase
  extractor when adding a second language; otherwise defer.
- **Category:** product (multi-language readiness).

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
- **T2.2 LISTEN/NOTIFY (#4b)** — cost not correctness. Defer until
  user count + cost signal warrants it.
- **T3.3 multi-language pipeline (#18, #19)** — feature, not a bug.
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

### T2.1 — #17 src/app salvage batch 1 (20 tests onto runtime)
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

### T3.3 — #18 + #19 multi-language scraper + phrase extractor
- **Effort:** L. Move `LANG_MODEL_MAP` / `LANG_TRANSCRIPT_CODES` /
  `NO_MORPH_LANGS` to a `language_config` table; add
  `extract_phrases(doc, language)` dispatcher with no-op stubs.
- **Impact:** Unblocks a second language.
- **Risk:** Low–medium. Big surface but mechanical.
- **Category:** structural (product expansion).

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
| **Hole 19/20 — free chat language hardcoded `'de'`** | Bundle into #19 | No second language exists yet. Don't fix in isolation. |

---

## Direct answers to common planning questions

**1. Start #34 now, or one/two small items first?**
Small items first. Specifically: T1.1 (transcript dedup), T1.2 (passive
front-flip), T1.3 (theme tristate), T2.2 (LISTEN/NOTIFY + APNs prep), and
ideally T1.4 (#6 channel prefs). Capacitor on top of inflatable passive
signal + a half-fake passive review + boolean dark mode + polling SSE wastes
the wrap's first impression.

**2. Next single best task.**
T1.1 — transcript-click dedup + error surfacing. Protects the `passive_level`
signal that every downstream metric (auto-promotion, insights, ranking)
already reads from. Must precede T1.2's passive-front flip — no point making
review a real recall test against a counter that's still inflatable by
dragging the seek bar. ~½ day end-to-end. See the recommended next prompt
at the bottom of this file.

**3. Avoid right now.**
- #5 reconciliation (Hole 23) — no user signal yet.
- #5e backfill — recommended forward-only, don't touch prod data.
- Hole 27 spaced forgetting — DEFERRED by product decision (2026-05-21); not a code task at all until a maintenance-review UX is designed.
- Starting #34 before T1+T2 land.
- New top-level Python files / new `src/app/` modules — refactor is being
  deleted, not extended.

**4. Reclassify as no longer worth doing.**
- #5e (drop entirely).
- #5 / Hole 23 (defer indefinitely; reopen on signal).
- #33 (just `git rm tests/legacy/`; don't restore).
- #27 remaining stages (mostly absorbed by 27a–g; spot-check, don't ticket).
- Holes 19/20 isolated fix (bundle into #19's eventual multi-language work).

---

## Recommended next prompt (paste back to continue)

```
Implement T1.1: transcript-click error surfacing + per-sentence dedup.

Goal:
Make transcript clicks useful as exposure events without allowing repeated
clicks on the same word/sentence to inflate passive_level.

Current problem:
Frontend useWordStatus.recordTranscriptClick is fire-and-forget. If the
backend fails, the user never knows.
Also, repeated clicks on the same word in the same sentence can repeatedly
trigger transcript_clicked progression, inflating times_seen/passive_level
and causing noisy auto-promotion.

Desired behavior:
1. Transcript click should be idempotent per:
   - user_id
   - item_id
   - item_type
   - sentence_id or stable transcript/sentence identifier
   - calendar day or review/session window, whichever fits current schema best

2. First click in that scope:
   - records usage event
   - applies progression("transcript_clicked") as today

3. Duplicate click in that scope:
   - should not apply progression again
   - should not increment passive_level again
   - should not increment times_seen again
   - should return a harmless success/no-op response

4. Frontend should not silently swallow errors:
   - recordTranscriptClick should surface/log failure in a controlled way
   - do not block word lookup/status modal on click failure
   - but make debugging possible, e.g. console.warn or hook error state

Backend tasks:
- Inspect existing transcript-click endpoint:
    POST /words/word/{id}/transcript-click
- Inspect word_usage_events schema and whether it has sentence_id/context
  metadata.
- If a unique constraint/index is appropriate, add one.
- If schema does not currently store sentence_id, use the best existing stable
  identifier or extend minimally.
- Implement dedup atomically, preferably with INSERT ... ON CONFLICT DO NOTHING.
- Only call apply_progression when the insert is new.

Frontend tasks:
- Update useWordStatus.recordTranscriptClick or equivalent caller.
- Keep it non-blocking for UX.
- Replace silent failure with controlled warning/error state.
- Preserve current word-click behavior.

Tests:
Backend:
1. First transcript click applies progression.
2. Duplicate transcript click for same user/item/sentence/day does not apply
   progression.
3. Different sentence still counts.
4. Different user still counts.
5. Different item still counts.
6. Endpoint returns success for duplicate no-op.
7. Passive_level/times_seen do not inflate on duplicate.

Frontend:
1. transcript click still fires when word is clicked.
2. failed transcript click does not break word lookup/modal.
3. failure is surfaced via console.warn or hook error state.

Run:
python -m pytest tests/test_transcript_click.py tests/test_progression.py --tb=short
python -m pytest --tb=no -q

Frontend if touched:
npx tsc --noEmit
npx vitest run
npm run build

Report:
- existing transcript-click flow before change
- dedup key chosen
- schema/index changes, if any
- exact duplicate behavior
- frontend error-surfacing behavior
- tests added/updated
- backend/frontend results
```

After T1.1 lands, the follow-up prompt is T1.2 (flip passive SRS card front
to the English gloss using the existing `prompt_text` / `answer_text`
infrastructure from #0a-1).
