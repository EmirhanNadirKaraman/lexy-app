# TESTS.md

Test inventory and coverage status. **Update this file whenever you add, remove, or rename a test.**

---

## Backend test suite — `lexy-app/backend/tests/`

Test runner: pytest + pytest-asyncio. Fixtures in `conftest.py` provide `db_pool` (real Postgres) and `client` (httpx.AsyncClient against the app).

### Status legend
- ✅ passing
- ❌ failing (pre-existing, not introduced by current work)
- 🆕 added in this session

### Existing tests
| File | What it covers |
|---|---|
| `test_auth.py` | register, login, JWT token validation |
| `test_auth_throttle.py` | 🆕 S1 auth throttling: login per-(IP,email) + per-IP spray guard, register per-IP, generic 429 (no user enumeration), reset, validation-not-throttled |
| `test_security_headers.py` | 🆕 S5 security headers: baseline headers on 200/403/404/422, HSTS off-by-default + on-when-`ENABLE_HSTS`, CSP policy-builder unit |
| `test_books_upload.py` | upload size guards (413 via `file.size` + bounded read) + 🆕 S8 magic-byte validation: renamed non-PDF → 400, mismatched Content-Type still accepted; rejected uploads never call downstream |
| `test_chat.py` | session lifecycle (create, get, messages), free + guided |
| `test_free_chat_progression.py` | free-chat crediting: target word always scanned; de→both tracks, mixed/en-with-target→passive only, en-without-target→no progression |
| `test_grammar_rules_srs.py` | grammar rule via `/words/{type}/{id}/status`; status_marked_learning currently creates passive only (grammar_rule guard added in this session) |
| `test_llm_cache.py` | cache key generation, hit/miss, TTL |
| `test_matcher.py` | phrase matching via `/sentences/match` (🆕 auth-gated, S16): 403 unauth / 200 auth / 422 over-length cap; German + Spanish (es-model) extraction; unknown-language → []. |
| `test_client_errors.py` | W7 crash-sink coverage; 🆕 extended with S6 per-IP throttle tests (429 after limit, authed below-limit still 204) |
| `test_playlist.py` | playlist generation from target words |
| `test_prioritization.py` | get_prioritized_items signal weights |
| `test_progression.py` | rule table + apply_progression behaviours |
| `test_reading_progression.py` | reading_selections save/review → main progression via find_catalog_item |
| `test_reading_stats.py` | lemma coverage calculation |
| `test_recommendations.py` | score_sentence, rank_sentences, recommend_videos, channel/category multipliers |
| `test_settings.py` | preferences GET/PUT — ❌ 3 tests fail (pre-existing: expected key set out of sync with current default keys: `disliked_genres`, `auto_mark_known`, `dark_mode`, `liked_genres`) |
| `test_srs_review.py` | `/srs/due` + `/srs/review/{card_id}` end-to-end |
| `test_transcript_click.py` | `/words/word/{id}/transcript-click` → passive_level + create card |
| `test_usage_events.py` | record_event + aggregations |
| `test_words.py` | lookup, knowledge list, status PUT |

### Pre-existing failures (NOT introduced by current work)

These were failing before #0b and are tracked here so they don't get blamed on future changes.

| Test | Failure | Root cause |
|---|---|---|
| ~~`test_matcher.py` × 5~~ | ~~AttributeError~~ | **RESOLVED 2026-05-19** as a side effect of #5b's Path A fix. `matcher_service.match_sentence` now wraps the string with `_pf.nlp()` before calling `extract_german_logic`. All 6 matcher tests pass. |
| ~~`test_settings.py` × 3~~ | ~~stale ALL_PREFERENCE_KEYS~~ | **RESOLVED 2026-05-19**. `ALL_PREFERENCE_KEYS` now derives from `settings_service.DEFAULTS` plus the four derived keys (`liked_categories`, `disliked_categories`, `liked_genres`, `disliked_genres`) that `get_preferences` always appends — stays in sync automatically when DEFAULTS grows. `test_get_preferences_new_user_returns_defaults` updated to expect the four empty derived lists alongside DEFAULTS. |
| `test_account_deletion.py::test_client_error_log_user_id_set_null_on_delete` and `test_srs_backfill.py::test_audit_ignores_rows_that_already_have_active_srs_card` | Intermittent under `pytest -n auto` only | **Parallel-execution races, not logic bugs** (observed 2026-05-24, S1/S5 work). Both **pass serially** and in isolation; the `srs_backfill` one throws `UniqueViolationError` on `srs_cards (user_id,item_id,item_type,direction)` from cross-worker collisions on shared global tables. Unrelated to any specific feature diff. Re-confirm with `MOCK_LLM=true python -m pytest backend/tests/test_account_deletion.py backend/tests/test_srs_backfill.py -q` (serial → green). Fix would be worker-scoped isolation of the shared rows; deferred. |

---

## Frontend tests — `lexy-app/frontend/`

Test runner: Vitest + @testing-library/react + jsdom. Setup: `src/test/setup.ts`.

**Current count (W13 baseline, 2026-05-20):** ~179+ tests across ~31 files.
The full per-file inventory is below in the dated "Tests added in this session"
rows — each W# / T# row lists which test files it added or extended. Rather
than maintain a parallel index here, treat those rows as the live list and
update them when you add new tests.

Highlights of what's covered now (non-exhaustive — see the dated rows for the
full picture):
- Hooks: `useWordStatus` (W13 + T1.1), `useNotifications` (W13 audit fix C),
  `usePreferences` (#9 follow-up), `useViewport` (#27a), `_http.ts` 401 flow
  (#11/#13/#14 bundle).
- Components: `SRSReviewPage` (mobile + behaviour), `ReadingReviewPage` (#5),
  `WordStatusPicker`, `NotificationToast` (W13 fix A), `ErrorBoundary` (#22),
  `PlayerView` / `PlayerControls` / `SubtitleDisplay` / `TranscriptPanel`
  (mobile + theme), `LoginForm`, `BookLibraryPage`, `BookReaderPage`,
  `ContentRequestPage`, `SettingsPanel`, `PlaylistPanel`,
  `RecommendationsPanel`, `MessageInput`, `GuidedChatPage`, `PrepView`,
  `SessionSummaryCard`, `SelectionPanel`, `FreeChatPage`, `ReminderBanner`,
  `PrivacyPage`.
- Theme tokens: `src/test/theme.test.tsx` (across #20a/b).

---

## Pipeline tests — `tests/`

Hermetic pytest tests. Two sub-trees:
- `tests/{subtitles,learning,exposure,pipeline}/` — 537 tests against the `src/app/` refactor (TODO #17 inventory).
- `tests/runtime/` — 20 tests salvaged onto the runtime root modules (W4 batch 1, 2026-05-20: subtitle cleaner / merger / ingestion / multi-speaker guard / word_knowledge / onboarding).

Root suite baseline (W4): **562 passed.** Run: `pytest tests/` from repo root.

Backend suite baseline (W13, 2026-05-20): **521 passed / 2 skipped** (xdist parallel run ~48s). Run: `pytest -n auto` from `lexy-app/backend/`.

---

## Coverage gaps (backend)

Re-derived 2026-05-20 after the W1–W13 + T1.1–T1.4 rollouts. Many earlier
gaps closed.

### No direct test file
| Service / router | Status |
|---|---|
| `analytics.py` router (4 endpoints) | covered indirectly by `test_usage_events.py` aggregations, but not end-to-end via HTTP |
| `books.py` router (upload, list, page, block, llm-repair) | **none** — major gap |
| `book_service`, `book_llm_service` | none |
| `reading.py` translate / explain endpoints | covered indirectly via cache-migration tests (`test_llm_cache_migration.py`); no dedicated router test |
| `reminders.py` | covered by `test_reminders.py` |
| `videos.py` | none |
| `search.py` legacy public endpoints | none |
| `phrases.py` router + `phrase_service` | indirect via `test_matcher.py` (now green post-#5b) and `test_guided_chat_targets.py` |

### Closed gaps (formerly listed here)
- `insights.py` / `insights_service` — covered by `test_insights.py` (#5a + #5c).
- `notifications.py` SSE — covered by `test_notifications.py` (#4a).
- `content_requests.py` — covered by `test_content_requests.py`.
- `recommendation_service.enrich_by_type` — covered by `test_recommendations.py` (#5c).

### Indirect coverage that should be made direct
- `progression_service` — `test_progression.py` covers the rule table + demotion + auto-promotion. End-to-end integrations live in `test_e2e_learning_loop.py` (#29) — `test_free_chat_progression.py`, `test_reading_progression.py`, and `test_srs_review.py` each pin one path.
- `prioritization_service` — has its own test but downstream consumers (insights, recommendations) aren't asserted.

### Pipeline (root) gaps
- No integration test between `pipeline.GermanSubtitlePipeline` and the FastAPI backend (the two never run together in tests).
- No test that `subtitle-scraper/pipeline.py --requests-only` consumes a `content_request` row, populates `word_table`, and writes a notification.

### Frontend gaps
- `App.tsx` routing + auth gates.
- `useChat`, `useGuidedChat` — still uncovered.
- (Note: `useWordStatus`, `useNotifications`, `WordStatusPicker`, `SRSReviewPage`, `_http.ts` 401 flow are now covered — see the dated 🆕 rows below.)

---

## Tests added in this session

🆕 **2026-05-24 (latest) — Spanish phrase extractor slice 2 (#36)**

Added a third pattern family (clitic-attached reflexive infinitives) and broadened the verb+prep allowlist. Scraper-only (`phrase_finder.py`); German untouched.

| File | Change |
|---|---|
| `subtitle-scraper/phrase_finder.py` | New block 3 in `extract_spanish_logic`: clitic infinitives (`quiero lavarme`→`lavarse`) via VerbForm=Inf + longest-first clitic strip + ends-in-`r` check + override on the recovered base. `_es_prep_candidates` widened to `mark` deps. `_ES_VERB_PREP` +6 pairs (`confiar en`, `consistir en`, `creer en`, `jugar a`, `salir de`, `llegar a`). |
| `tests/test_spanish_phrase_extractor.py` | +13: 4 clitic infinitives (lavarse/levantarse/ducharse/acostarse), non-reflexive-infinitive negative, 6 verb+prep, 2 deferred-pattern guards (imperative→[], reflexive+prep emits only `acordarse` not `acordar de`). |

**Spanish spaCy (`es_core_news_sm`) limits hit (deferred, documented):** imperatives (`Lávate.`) and some 1sg forms (`llamarme`, `Salgo`, `Llego`) are mis-tagged as non-verbs, so those specific cases don't extract — verb+prep pairs use sentences the model tags correctly. Reflexive+prep combos (`acordarse de`) deferred to a separate set.

**Validation:** root Spanish/dispatcher/es-path → 50 passed · backend `test_matcher.py` → 16 passed · full root suite **630 passed** · full backend suite green (regression).

🆕 **2026-05-24 — matcher per-language model + override wiring, #39 slice 2**

Fixed a correctness bug: `matcher_service._extract` parsed **every** language with the German model (`_pf.nlp`), so backend chat matched Spanish through `de_core_news_sm`. Now `_model_for(language)` selects per-language (German resident model; others via `nlp_service`'s cache, lock-guarded; no extractor → skip), and `match_sentence_with_ids` loads `lemma_override` (it has the pool) and threads it into the extractor so chat canonicals come out corrected (`ducharse`).

| File | Change |
|---|---|
| `services/matcher_service.py` | `_model_for` (per-language model, fail-open); `_extract`/`match_sentence` gained `overrides`; `_load_overrides_for(pool, lang)` (fail-open on missing table / DB error); `match_sentence_with_ids` loads + threads overrides; `threading.Lock` around first model load. |
| `lexy-app/backend/tests/test_matcher.py` | +5: Spanish parses via Spanish model (spy on `nlp_service.get_model("es")` + behavioural reflexive/verb-prep), German unchanged, unknown→[]. Refreshed 2 stale "Spanish has no extractor" docstrings. (es tests skip if `es_core_news_sm` absent.) |
| `lexy-app/backend/tests/test_chat_language.py` | +1: `match_sentence_with_ids` applies the migration-033-seeded override → `ducharse`, never `duchaberse`. |

**Validation:** backend `test_matcher.py` + `test_chat_language.py` → 33 passed · root suite **617 passed** · full backend suite green (slice-1 baseline 591/2-skipped + the new tests).

🆕 **2026-05-24 — lemma-override layer, slice 1 (#39)**

Fixes spaCy Spanish lemmatizer errors (`ducha`→`duchaber`) deterministically, without switching models (verified `_sm`/`_md`/`_lg` share the bug). Migration 033 adds `lemma_override` (keyed `(language, observed_lemma)`, seeded `duchaber`/`duchir`→`duchar`); `extract_phrases(doc, lang, overrides)` applies the map before building canonicals; the scraper loads + threads it via `populate`→`insert_phrases`.

| File | Change |
|---|---|
| `lexy-app/backend/migrations/versions/033_lemma_override.py` (NEW) | Table + partial-unique index + seed. Applied to dev DB; verified via real psycopg2 `load_lemma_overrides`. |
| `subtitle-scraper/phrase_finder.py` | `extract_phrases` / `extract_spanish_logic` / `extract_german_logic` gained `overrides=None`; Spanish applies it at the verb-lemma read. German byte-identical (param accepted, not applied). |
| `subtitle-scraper/pipeline.py` | `load_lemma_overrides(cursor, language)`; `insert_phrases(..., overrides=None)`; `populate` loads once per video and threads it through. |
| `tests/test_spanish_phrase_extractor.py` | +5 #39 tests: override fixes `ducharse`, leaves unrelated lemmas alone, `None`==pre-#39, loader contract, end-to-end through `insert_phrases`. |
| `tests/test_scraper_es_path.py` | Spy signatures updated for the new `extractor(doc, overrides)` arity. |

**Validation:** targeted 37 passed · root suite **617 passed** · backend **591 passed, 2 skipped** (migration 033 applies clean, no regression).

**Deferred to slice 2:** user flagging endpoint + candidate/promotion system; LLM seeding pass; backend matcher override consultation (blocked on a separate bug — `matcher_service` parses all languages with the German model).

🆕 **2026-05-24 — Spanish phrase extractor, first slice (#36)**

`subtitle-scraper/phrase_finder.py` gained `extract_spanish_logic` (reflexive verbs + an allowlisted verb+preposition set), registered under `'es'` in `_LANGUAGE_EXTRACTORS`. Same output dict shape as the German extractor, so `pipeline.insert_phrases` consumes it unchanged. German path untouched.

| File | Change |
|---|---|
| `tests/test_spanish_phrase_extractor.py` (NEW) | 18 tests: dispatcher registration, reflexives (me/te/se/nos/os with person-number agreement; non-reflexive object clitic excluded), verb+prep allowlist (in/out), dedup, output-shape contract, fr-still-empty, de-unchanged, and a real-DB-free `insert_phrases` pipeline-compat test (fake cursor → asserts phrase_blueprint + sentence_to_phrase INSERTs fire). Requires `es_core_news_sm`; skips if absent. |
| `tests/test_phrase_dispatcher.py` | `test_es_dispatch_returns_empty` → `test_es_dispatch_routes_to_spanish_extractor` (es now routes to the Spanish extractor; a German doc still yields []). Updated the es insert-noop test's docstring. |
| `tests/test_scraper_es_path.py` | Softened the "Spanish never extracts phrases" invariant to fixture-dependent (the smoke fixture has no first-slice patterns); module + test docstrings updated. Assertions unchanged. |

**Known limitation (model-bound):** `es_core_news_sm` mis-lemmatizes some verbs (`ducha`→`duchaber`; `lavo`→`lavar`/`lavo` by context), so canonicals are occasionally imperfect. Detection is reliable; one test pins that the reflexive is still *detected* despite the noisy lemma. Future: `es_core_news_md` or a lemma-normalization pass.

**Validation:** targeted (`test_spanish_phrase_extractor` + `test_phrase_dispatcher` + `test_scraper_es_path`) → 32 passed. Full root suite → **612 passed**. Backend suite (German regression check via `test_matcher`) → **591 passed, 2 skipped**.

🆕 **2026-05-23 — word_table test-pollution cleanup**

`tests/test_words.py` created synthetic `word_table` rows (`ζtest_<hex>` via `/learn-anyway`, `Bnk_<hex>` via `_create_ambiguous_word`) that the autouse user-cleanup couldn't reap — `word_table` is global, not user-scoped — so they accumulated (332 found) and broke every `LIMIT 1`-based picker in other suites.

| File | Change |
|---|---|
| `tests/conftest.py` | New `tracked_words` fixture + `_reap_word_ids(pool, ids)` helper. Tests append created word_ids; teardown deletes them. Safe + order-independent: `user_word_knowledge`/`srs_cards` have NO FK to `word_table` (polymorphic key) and are user-CASCADE-cleaned; `word_table`'s own FK children (`word_to_sentence`, `word_strength`, `most_frequent_words`) are ON DELETE CASCADE. |
| `tests/test_words.py` | `_create_ambiguous_word` now appends its 2 ids to `tracked_words`; the 4 disambiguation tests + 5 `/learn-anyway` tests register their rows. Hardened `_get_word_id` / `_get_word_text_and_language` / `_get_word_for_lookup` with `word !~ '[0-9_]'` so a LIMIT-1 grab never returns a synthetic surface. +`test_reap_word_ids_removes_synthetic_rows` (proof of the reap, incl. idempotency). |

**One-off dev-DB cleanup:** deleted **332** orphaned `ζtest_*` / `Bnk_*` rows (0 had `word_to_sentence` links → none were real corpus). Snippet: `DELETE FROM word_table WHERE word LIKE 'ζtest\_%' ESCAPE '\' OR word LIKE 'Bnk\_%' ESCAPE '\'`.

**Validation:** `test_words.py` run twice → 29 passed each, 0 leftover synthetic rows. Full backend suite: **591 passed, 2 skipped, 0 failed.**

**Not addressed (bounded, benign):** `tests/test_chat_language.py`'s Spanish helper inserts fixed `hola`/`gato` (`pos='X'`) without cleanup — idempotent (`ON CONFLICT`), capped at 2 rows, no digit/underscore so they don't trip the hardened pickers. `phrase_table` also carries ~2 testish rows from another suite (out of scope here).

🆕 **2026-05-23 — free-chat mixed-language crediting fix**

A target-language learning word in a free-chat message the LLM labelled `"en"` was getting **zero** credit ("Yesterday I bought Brot" → Brot ignored). `routers/chat.py` now ALWAYS runs `match_learning_words` for the session language; `language_detected` only gates ACTIVE credit. Rule: `== session_language` → `free_chat_used_correctly` (passive+active); else if items matched → `free_chat_mixed_lang` (passive only); else no event. No progression-rule changes.

| File | Change |
|---|---|
| `routers/chat.py` | Removed the `language_detected in (session_language, "mixed")` gate around matching. Match is now unconditional; the event is chosen from `language_detected == session_language`. |
| `tests/test_free_chat_progression.py` | Split `test_english_message_triggers_no_progression` → `test_english_message_without_target_word_triggers_no_progression` (gibberish content). +`test_english_label_with_target_word_advances_passive_only` (en+target word → passive only; asserts no active SRS card fabricated). Hardened `_get_word` + the lemma-test query to skip `word_table` test-fixture pollution. |
| `tests/test_chat_language.py` | +`test_spanish_english_label_with_target_word_advances_passive_only`, +`test_spanish_es_label_advances_both_tracks` (real-DB Spanish: en→passive-only, es→both tracks). |

**Net delta:** +4 tests, 1 split/renamed. Targeted files: 34 passed, 1 skipped. Full backend suite: **590 passed, 2 skipped, 0 failed.**

**Known pre-existing issue (NOT fixed here):** `test_words.py:381` generates `ζtest_<hex>` / `Bnk_<hex>` word surfaces inserted into the un-user-scoped `word_table`; the autouse user-cleanup can't reap them, so ~177 orphan rows have accumulated. Their digit/underscore surfaces can't round-trip the message tokenizer, so a `LIMIT 1` grab silently broke 6 match-based tests. `test_free_chat_progression.py`'s helpers are now hardened against them; the root fix (purge rows + add a `word_table` cleanup to `test_words.py`) is **RESOLVED** — see the word_table test-pollution cleanup entry above.

🆕 **2026-05-19 — Dark-mode coverage (#20b)**

Follow-up pass to #20a converting the 22 components that were still rendering hardcoded light colors. 23 components edited in total. Semantic palettes (status pills, mistake/freq badges, word-status colors, PrefButton accents, LLM tint colors) intentionally kept fixed.

| Check | Outcome |
|---|---|
| `npx tsc --noEmit` clean | ✅ |
| `npx vitest run` → 94/94 (was 90; +4 new theme tests) | ✅ |
| `npm run build` → 421.30 kB JS / 116.03 kB gz | ✅ |

### Tests touched

- `src/test/theme.test.tsx` — added 4 tests for `PlayerControls`, `LoginForm`, `MessageInput`, `ReminderBanner` confirming they use `var(--color-*)` tokens.
- `src/components/TranscriptPanel.test.tsx` — `applies active highlight style to the active sentence` updated. `toHaveStyle` shorthand can't compute `var(--color-primary)` in jsdom, so the assertion now reads `style.borderLeft` directly via `toContain`.

### Files converted

`PlayerView`, `PlayerControls`, `SubtitleDisplay`, `TranscriptPanel`, `WordStatusPicker`, `SearchBar`, `LoginForm`, `ReminderBanner`, `FreeChatPage`, `ChatWindow`, `TargetCard`, `MessageInput`, `RecommendationsPanel`, `RecommendationCards`, `FollowedChannelsSection`, `ReadingStatsPanel`, `InsightsSection`, `PrepView`, `GuidedChatPage`, `SessionSummaryCard`, `ErrorBoundary`, `PlaylistPanel`, `SRSReviewPage`.

### Files NOT converted (semantic / intentional)

`ResultCard` (dead code, no importers), `STATUS_STYLES` / status badges in `ContentRequestPage`/`SessionSummaryCard`/`SelectionReviewPanel`/`WordStatusPicker`, `PrefButton` activeColor (passed in by parent encoding action meaning), YouTube thumbnail backdrops (`#000` is intentional for poster fade-in), highlight `<mark>` background `#fff176`, mistake-vs-freq border + accent in `InsightsSection`.

🆕 **2026-05-19 — Dark-mode theme system (#20)**

CSS variables in `src/index.css` (`:root` for light, `[data-theme="dark"]` for dark). App.tsx Layout sets `document.documentElement.dataset.theme` from `prefs.dark_mode`. SettingsPanel dark-mode toggle writes the attribute immediately so the UI flips without the 600ms save round-trip.

| Check | Outcome |
|---|---|
| `npx tsc --noEmit` clean | ✅ |
| `npx vitest run` → 90/90 (was 86; +4 new from `src/test/theme.test.tsx`) | ✅ |
| `npm run build` → 414.80 kB JS / 115.97 kB gz / 3.96 kB CSS (was 2.07 kB; +2 kB from token declarations) | ✅ |

### New test file (#20)

| File | What it asserts |
|---|---|
| `src/test/theme.test.tsx` (NEW) | 4 tests: NotificationToast dismiss uses `var(--color-text-muted)`; ContentRequestPage container uses `var(--color-surface)` + `var(--color-text)`; ContentRequestPage input uses `var(--color-input-bg)` + `var(--color-input-border)`; SettingsPanel dark-mode toggle flips `document.documentElement.dataset.theme` immediately. |

### Component refactors (#20)

`darkMode` / `dk` prop signatures dropped from: `NotificationToast.tsx`, `ContentRequestPage.tsx`, `BookLibraryPage.tsx`, `BookReaderPage.tsx` (kept local `dk` state for the in-reader Dark/Light toggle which now wraps content in `<div data-theme="dark|light">` for a scoped override), `SelectionPanel.tsx`, `SelectionReviewPanel.tsx`. `SettingsPanel.tsx` kept `darkMode` LOCAL STATE (drives the checkbox + auto-save) but styling now uses CSS vars. `App.tsx` Layout switched from `document.body.style.background` to `document.documentElement.dataset.theme`.

Call-site updates: `BookLibraryPage.mobile.test.tsx` and `BookReaderPage.mobile.test.tsx` dropped the `darkMode={false}` prop in their `render(...)` calls.

Net ternary count: **104 → 5** (the remaining 5 are all driving the `data-theme` attribute itself, not color decisions).

🆕 **2026-05-19 — PWA icon set (#27i)**

No unit tests — verified via build output:

| Check | Outcome |
|---|---|
| `dist/icons/icon-192.png` exists, 192×192 (sips) | ✅ |
| `dist/icons/icon-512.png` exists, 512×512 (sips) | ✅ |
| `dist/apple-touch-icon.png` exists, 180×180 (sips) | ✅ |
| `dist/manifest.webmanifest` references both PNGs (`192x192` + `512x512`, type `image/png`) | ✅ |
| `dist/index.html` contains `<link rel="apple-touch-icon" href="/apple-touch-icon.png">` | ✅ |
| `npx tsc --noEmit` clean | ✅ |
| `npx vitest run` → 86/86 (unchanged) | ✅ |
| `npm run build` → no warnings about manifest | ✅ |

Files added: `frontend/public/icons/icon-192.png`, `frontend/public/icons/icon-512.png`, `frontend/public/apple-touch-icon.png`.

🆕 **2026-05-19 — PWA shell (#27h)**

No new unit tests — the shell is verified via build-output inspection:

| Check | Outcome |
|---|---|
| `dist/manifest.webmanifest` exists + has theme/colours/icons | ✅ |
| `dist/sw.js` exists | ✅ |
| `dist/offline.html` exists | ✅ |
| `dist/index.html` contains `<link rel="manifest">`, `theme-color`, `mobile-web-app-capable`, `apple-mobile-web-app-capable`, `apple-mobile-web-app-title`, `apple-mobile-web-app-status-bar-style` (7 matches) | ✅ |
| No references to missing icon files (no `192`/`512`/`apple-touch-icon`) in built output | ✅ |
| `npx tsc --noEmit` clean | ✅ |
| `npx vitest run` → 86/86 (unchanged) | ✅ |
| `npm run build` → 414 kB / 116 kB gz (+1 kB from larger HTML head) | ✅ |

Files added: `frontend/public/manifest.webmanifest`, `frontend/public/sw.js`, `frontend/public/offline.html`. Files modified: `frontend/index.html`, `frontend/src/main.tsx`.

Future: an integration test would need Puppeteer/Playwright to verify "Add to Home Screen" install + offline navigation fallback. Not in scope for v1.

🆕 **2026-05-19 — Global mobile touch-target audit (#27g)**

Closing pass: every component #27a–f didn't touch was audited for iOS-zoom-vulnerable inputs (<16px font) and undersized touch targets (<36px secondary, <44px primary). Eleven components edited, one (`ResultCard.tsx`) flagged as dead code (no importers, left as-is).

| File | Change |
|---|---|
| `App.tsx` | NavLink chips: `padding 6px 14px` + `fontSize 13px` (~28px tall) → `padding 8px 14px`, `display: inline-flex`, `minHeight: 36px`, `touchAction: manipulation`. HomePage Free Chat / Guided Practice buttons: padding bumped to `10px 20px`, `minHeight: 44px`, `touchAction: manipulation`, test IDs added. |
| `components/LoginForm.tsx` | Email + password `inputStyle` rewritten: `fontSize 13px → 16px`, `minHeight: 44px`, padding `4px 8px → 8px 10px`, width `140px → 160px`. New `closeBtn` style: 44×44 flex-centered. `primaryBtn`: `minHeight: 44px`. `outlineBtn`: `minHeight: 36px`. `ghostBtn` (header signed-in "Sign out"): now padded for 32px tap target. New `modeToggleBtn` style for Login/Register inline toggles. Test IDs added: `login-signin-toggle`, `login-email`, `login-password`, `login-submit`, `login-cancel`, `login-signout`. Header signed-in row now `flexWrap: wrap`. |
| `components/ReminderBanner.tsx` | "For You →" button: `padding 4px 12px / 12px` → `8px 14px / 13px`, `minHeight: 36px`. Dismiss `×`: 44×44 flex-centered. Test IDs added. |
| `components/NotificationToast.tsx` | Dismiss `×`: 44×44 flex-centered, `aria-label="Dismiss notification"`. Test ID added. |
| `components/PlaylistPanel.tsx` | Container padding `16px 20px → clamp(14px, 4vw, 20px)`. Back button (Result view): `padding 0` → `8px 4px`, `minHeight: 36px`. Close `×`: 44×44 flex. BuildView shared `inputStyle`: `fontSize 13px → 16px` + `minHeight: 44px` — covers language `<select>`, word search `<input>`, and the max-videos `<input type="number">`. Word search "Add" button: `minHeight: 44px`, `minWidth: 64px`, padding `10px 18px`, font 13 → 14px. "Add recommended words" button: `minHeight: 36px`. Generate playlist CTA: padding `9px 24px → 12px 24px`, `minHeight: 44px`. Per-video Watch button: `minHeight: 36px`. Test IDs: `playlist-close`, `playlist-add`, `playlist-generate`. |
| `components/BookLibraryPage.tsx` | Title input: `fontSize 13px → 16px`, `minHeight: 44px`, padding `6px 8px → 8px 10px`. Language `<select>`: same. Flex basis bumped `0 0 90px → 0 0 110px` so the 16px text fits. Upload submit: `padding 7px 18px → 10px 20px`, `minHeight: 44px`. Close `×`: 44×44 flex. Per-book actions row: `flexWrap: wrap`. Read / Confirm / Cancel / Delete buttons: `minHeight: 36px`. Test IDs: `book-upload-title`, `book-upload-language`, `book-upload-submit`, `book-library-close`. |
| `components/RecommendationsPanel.tsx` | Container padding `16px 20px → clamp(14px, 4vw, 20px)`. Close `×`: 44×44 flex. Language `<select>`: `fontSize 13px → 16px`, `minHeight: 44px`. Refresh button: `minHeight: 36px`. Reading-units-due "Open Books" button: `padding 3px 8px → 6px 12px`, `minHeight: 36px`. Test IDs: `recs-close`, `recs-language`. |
| `components/RecommendationCards.tsx` | Shared `ActionButton` (used by Watch / Practice / Search / Dismiss / Mark Learning across item / video / sentence cards): `padding 5px 12px → 8px 14px`, `minHeight: 36px`, `touchAction: manipulation`, font 12 → 13px. `PrefButton` (Follow/Like/Dislike) intentionally kept compact — documented in TODO. |
| `components/PrepView.tsx` | Back button: `padding 0 → 8px 4px`, `minHeight: 36px`. Start Guided Practice CTA: `padding 10px 20px → 12px 22px`, `minHeight: 44px`. Generate examples: `padding 7px 16px → 10px 18px`, `minHeight: 36px`. Linked grammar rule chips: `padding 5px 12px → 8px 14px`, `minHeight: 36px`. Test IDs: `prep-back`, `prep-start-practice`. |
| `components/SessionSummaryCard.tsx` | "See next recommended item" CTA: `padding 10px 20px → 12px 20px`, `minHeight: 44px`. Secondary row (`flexWrap: wrap` added). Practice again / Back to home: padding `7px 14px → 8px 14px`, `minHeight: 36px`. Test ID: `summary-next-item`. |
| `components/SelectionPanel.tsx` | Note `<textarea>`: `fontSize 13px → 16px`, padding `6px 8px → 8px 10px`. Clear `×` (sidebar): 36×36 flex-centered (documented sidebar carve-out — 44 would crowd the count badge). `llmBtnStyle` (Translate sentence / Explain in context): `padding 7px 12px → 10px 12px`, `minHeight: 40px`. Save / Clear / Remove / Clear selection buttons: padding `8px → 10px`, `minHeight: 40px`. Test IDs: `selection-note`, `selection-save`. |
| `components/SelectionReviewPanel.tsx` | Close `×` (sidebar): 36×36 flex-centered. `reviewBtnStyle` (Got it / Not quite / ★): `padding 6px 4px → 8px 4px`, `minHeight: 36px`, font 12 → 13px. |
| `components/FreeChatPage.tsx` | Header `×`: 44×44 flex-centered, `aria-label="Close free chat"`. Test ID added. |
| `components/GrammarRulePanel.tsx` | Close `×` (inline panel): 36×36 flex-centered. Learn more / Add to study buttons: `padding 5px 12px → 8px 14px`, `minHeight: 36px`, font 12 → 13px. |
| `components/InsightsSection.tsx` | Primary insight item button: padding `9px 11px → 10px 12px`, `minHeight: 44px`. Secondary chip buttons: `padding 4px 10px → 6px 12px`, `minHeight: 32px`. |

### New test files (#27g)

| File | What it asserts |
|---|---|
| `components/LoginForm.mobile.test.tsx` (NEW) | 5 tests: sign-in toggle opens form, email + password inputs 16px + 44px, submit 44px, cancel 44×44, toggle/cancel cycle. |
| `components/ReminderBanner.mobile.test.tsx` (NEW) | 3 tests: open button 36px, dismiss 44×44, both fire their handlers. |
| `components/NotificationToast.mobile.test.tsx` (NEW) | 1 test: dismiss 44×44 + fires `onDismiss(id)`. |
| `components/PlaylistPanel.mobile.test.tsx` (NEW) | 4 tests: close 44×44, Add 44px, Generate 44px, BuildView inputs (`<select>`, `<input>`, `<input type="number">`) all 16px. Mocks `suggestApi.fetchSuggestions`. |
| `components/BookLibraryPage.mobile.test.tsx` (NEW) | 4 tests: title 16px + 44px, language `<select>` 16px + 44px, upload submit 44px, close 44×44. Mocks `booksApi.listBooks`. |
| `components/RecommendationsPanel.mobile.test.tsx` (NEW) | 2 tests: close 44×44, language `<select>` 16px + 44px. Mocks `fetchItemRecommendations`, `fetchVideoRecommendations`, `fetchSentenceRecommendations`, `fetchInsightCards` (`{ cards: [] }` shape), `getDueSelections`. |
| `components/FreeChatPage.mobile.test.tsx` (NEW) | 1 test: close 44×44 + fires `onClose`. Mocks `chatApi.createSession` with never-resolving promise so close renders. |
| `components/SessionSummaryCard.mobile.test.tsx` (NEW) | 1 test: next-item CTA 44px + fires `onNextItem`. |
| `components/SelectionPanel.mobile.test.tsx` (NEW) | 2 tests: note textarea 16px, Save button 40px (sidebar context). |
| `components/PrepView.mobile.test.tsx` (NEW) | 2 tests: Start Guided Practice CTA 44px (waits on `fetchPrepData`), Back 36px. |

**Net delta:** +25 Vitest tests across 10 new test files. Total **86/86 frontend**. `npm run build` clean (413 kB / 116 kB gz). Backend untouched, still 392 passed.

🆕 **2026-05-19 — ContentRequestPage + SettingsPanel mobile (#27f)**

| File | Change |
|---|---|
| `components/ContentRequestPage.tsx` | Container padding fluid `clamp(16px, 4vw, 24px)`. Content-ID input: `fontSize: 16px` + `minHeight: 44px`. Channel/Video toggle row: `flexWrap: wrap`, buttons `minHeight: 36px`. Submit: `minHeight: 44px`. Close `×`: 44×44 flex. Test IDs added. |
| `components/SettingsPanel.tsx` | Container padding `clamp(14px, 4vw, 24px)`. Shared `input` helper now `fontSize: 16px` + `minHeight: 44px` (covers both reps inputs). TagInput text field: `fontSize: 16px`. TagInput preset chips: `minHeight: 32px`. Color picker: 44×44. Close `×`: 44×44 flex. Test IDs added. |
| `components/ContentRequestPage.mobile.test.tsx` (NEW) | 5 tests: input 16px + 44px, submit 44px, toggle row wraps, close 44×44 + dismisses, submit-still-fires-with-typed-ID. |
| `components/SettingsPanel.mobile.test.tsx` (NEW) | 4 tests: both reps inputs 16px + 44px, TagInput field 16px, close 44×44 + dismisses, debounced auto-save still fires preference update. |

**Net delta:** +9 Vitest tests across 2 new files. Total **61/61 frontend**. `npm run build` clean (409 kB / 116 kB gz).

🆕 **2026-05-19 — BookReaderPage mobile (#27d)**

| File | Change |
|---|---|
| `components/BookReaderPage.tsx` | Imported `useViewport`. Content area now flips `flexDirection` row↔column via `isMobile`. Both edit-mode children (annotation list + scan image) and the normal-mode side panels (scan / SelectionPanel / SelectionReviewPanel) get `flex: '0 0 auto'` + `maxHeight: 50vh` + stacking borders on mobile. Page input: `fontSize: 16px`, `minHeight: 44px`. `navBtnStyle`: 44×44, font 14→16px, `touchAction: 'manipulation'`. `topBtnStyle`: `minHeight: 36px`, font 12→13px. Back button: `minHeight: 44px`. Mode toggle: `minHeight: 36px`, font 12→13px. Reader padding: fluid `clamp(12px, 4vw, 24px)`. Test IDs added: `book-content-area`, `book-page-input`, `book-back`. |
| `components/BookReaderPage.mobile.test.tsx` (NEW) | 6 tests: page input 16px font + 44px minHeight, ◀/▶ buttons 44×44, Back ≥44px, content area row on desktop, column on mobile (matchMedia mock), Mode toggle ≥36px. Mocks `booksApi.getPage` / `listPages`, `readingApi.getPageWordStatuses` / `getPageSelections` / `listAllSelections` to avoid network. |

**Net delta:** +6 Vitest tests. Total **52/52 frontend**. `npm run build` clean (408 kB / 116 kB gz).

🆕 **2026-05-19 — bare-except cleanup bundle (#8 + #16 + #23)**

Backend hygiene pass. No new tests; existing 392-test suite still green.

| File | Change |
|---|---|
| `routers/books.py` | Imported `anthropic` + `asyncpg`. Batch repair loop now catches `(anthropic.APIError, asyncpg.PostgresError)` as known modes (warning), with a defensive `except Exception:` fallback logged via `logger.exception()` so a bug in `repair_block_by_id` doesn't lose batch progress. |
| `main.py` | Module-level `logger = logging.getLogger(__name__)`; removed per-site `import logging`. Three lifespan seeds (phrase / grammar / content-request resume) now narrow to known types first (`asyncpg.PostgresError`, `FileNotFoundError`, `ImportError`, `OSError` as appropriate), then a documented broad fallback logged via `logger.exception()`. Startup-must-never-crash invariant preserved. |
| `services/settings_service.py` | `_coerce_settings`'s `dict(value)` fallback now catches only `(TypeError, ValueError)` — actual exceptions that `dict()` raises for non-iterable / malformed inputs. Real errors bubble up. |

**Net delta:** 0 new tests. Backend remains **392 passed, 2 skipped, 0 failed**. Untouched broad catches (`book_service.py:182/476/483` book ingestion, `chat_service.py:140` phrase matcher defensive catch) are intentionally out of scope — each guards a different resilience invariant.

🆕 **2026-05-19 — SRSReviewPage + GuidedChat mobile (#27e)**

| File | Change |
|---|---|
| `components/SRSReviewPage.tsx` | Active input: `fontSize: 16px` (iOS guard), `minHeight: 44px`. All review/grade buttons: `minHeight: 44px`, fluid sizing, `flexWrap: wrap` + `flex: 1 1 140px` on rows. Card padding: `clamp(20px, 5vw, 28px) clamp(16px, 5vw, 24px)`. Prompt font: `clamp(24px, 7vw, 32px)`. Feedback / prompt containers: `overflowWrap: anywhere`. Close `×`: 44×44 flex. Language select: 16px font, 44px min. Reload + Continue buttons: 44px min. |
| `components/GuidedChatPage.tsx` | Header `×`: 44×44 flex. End Session: 36px min, font 11px → 13px. HintButton ("Need a hint?" / "See more"): 36px min, 13px. HintPanel inline link button: 32px min, 13px. Test IDs added. |
| `components/MessageInput.tsx` | textarea: `fontSize: 16px` (iOS guard), `minWidth: 0`. Send button: `minHeight: 44px`, `minWidth: 64px`, `touchAction: manipulation`. Test IDs added. |
| `components/SRSReviewPage.mobile.test.tsx` (NEW) | 6 tests: active input 16px font, 44px minHeight, active buttons 44px, button row flexWrap, passive buttons 44px after reveal, close 44×44. |
| `components/GuidedChatPage.mobile.test.tsx` (NEW) | 3 tests: chat textarea 16px, send button 44px minHeight, send minWidth 64px. |

**Net delta:** +9 Vitest tests across 2 new test files. Total **46/46 frontend**. `npm run build` clean (407 kB / 115 kB gz). The 16px input guard means iOS Safari no longer zooms when a user focuses the active-production input or the chat box — the single biggest blocker for daily mobile use.

🆕 **2026-05-19 — TranscriptPanel + WordStatusPicker mobile (#27c)**

| File | Change |
|---|---|
| `components/TranscriptPanel.tsx` | Sentence rows: `padding: 10px clamp(10px, 3vw, 16px)`, `minHeight: 44px`, `fontSize: clamp(14px, 3.5vw, 16px)`, `overflowWrap: anywhere` + `wordBreak: break-word`. |
| `components/WordStatusPicker.tsx` | Status buttons gain `minWidth/minHeight: 44px`; button row gains `flexWrap: wrap`. Close button widened to 44×44 with flex centering. Side padding `clamp(10px, 3vw, 16px)`. Test IDs added: `word-status-button`, `word-status-close`. |
| `components/TranscriptPanel.test.tsx` (extended) | +2 tests: sentence row `minHeight: 44px`, fluid font (soft check). |
| `components/WordStatusPicker.test.tsx` (NEW) | 5 tests: 3 buttons all 44×44, row wraps, close 44×44 + dismisses, status update still fires, "Not in vocabulary" rendering. |

**Net delta:** +7 Vitest tests across 1 edited + 1 new test file. Total **37/37 frontend**. `npm run build` clean (405 kB / 115 kB gz). Suite remains fully green.

🆕 **2026-05-19 — PlayerView mobile layout (#27b)**

| File | Change |
|---|---|
| `components/YoutubeEmbed.tsx` | Container now uses `width: 100%; aspectRatio: '16 / 9'` (was `paddingTop: 56.25%` trick). Inner div uses `inset: 0`. |
| `components/SubtitleDisplay.tsx` | `height: 200px` → `minHeight: 200px` (no more clipping). Font: `38px` → `clamp(20px, 5vw, 38px)`. Padding: fluid `clamp()`. |
| `components/PlayerControls.tsx` | Buttons gained explicit `minWidth/minHeight: 44px` alongside existing `width/height`. Outer container now `flexWrap: wrap` + `gap: 8px`. |
| `components/PlayerView.responsive.test.tsx` (NEW) | 5 tests: YT wrapper has `aspectRatio: '16 / 9'`, no fixed `paddingTop`; SubtitleDisplay uses `minHeight` not `height`; PlayerControls all buttons ≥44×44; outer container has `flexWrap: wrap`. |

**Net delta:** +5 Vitest tests. Total **30/30 frontend**. `npm run build` clean (405 kB / 115 kB gz). Note: the `clamp()` fluid font size couldn't be asserted in jsdom (it silently drops complex CSS values from inline styles) — verified visually instead, with a comment in the test file explaining the gap.

🆕 **2026-05-19 — mobile responsive infrastructure (#27a)**

| File | Change |
|---|---|
| `index.html` | Viewport meta: `width=device-width, initial-scale=1, viewport-fit=cover`. |
| `src/index.css` | Added `--bp-sm`, `--bp-md`, `--safe-top`, `--safe-bottom` to the live `:root` block. Added `html { -webkit-text-size-adjust: 100% }` and `body { font-size: 16px }` to block iOS input-focus zoom. |
| `src/hooks/useViewport.ts` (NEW) | `useViewport(): { isMobile }`. Tracks `(max-width: 768px)` via `matchMedia`. SSR-safe; defaults to non-mobile if `matchMedia` is missing. Modern `addEventListener('change',...)` with legacy `addListener` fallback. Cleans up on unmount. |
| `src/App.tsx` | Layout container consumes `useViewport`. Padding switches to `12px 8px` on mobile (`24px 16px` desktop). `paddingTop` adds `var(--safe-top)` so a future iOS Capacitor wrap respects the notch. |
| `src/hooks/useViewport.test.ts` (NEW) | 5 tests: default non-match, mount-time match, change event fires update, listener removed on unmount, missing matchMedia falls back to non-mobile. |

**Net delta:** +5 Vitest tests, +1 new hook + 1 new test file. **Vitest 25/25**, `npm run build` clean (404→405 kB, 115 kB gz).

🆕 **2026-05-19 — frontend hardening bundle (#9 + #22 + #32)**

| File | Change |
|---|---|
| `api/settings.ts` | Migrated all 4 fetches to `assertOkJson` from `_http.ts`. 401s now flow into the shared auth:expired handler automatically. |
| `hooks/usePreferences.ts` | Removed `.catch(() => {})`. Added `error: string \| null` to hook return. On failure, keeps the in-flight `prefs` state intact (doesn't snap back to defaults). |
| `components/ErrorBoundary.tsx` (NEW) | Class component catching render errors. Logs stack to `console.error`. Renders fallback with Reload button. |
| `App.tsx` | Wraps `<Outlet />` (not the whole shell) in `<ErrorBoundary>`. Navbar / Layout / auth state survive a deep route crash. |
| `auth.ts` | Tightened `getStoredEmail` docstring: "Callers MUST handle null". The one caller (`LoginForm.tsx:30`) already does. |
| `tests/ErrorBoundary.test.tsx` (NEW) | 3 tests: passthrough, fallback shown on render-throw, Reload button calls `window.location.reload`. |
| `tests/usePreferences.test.ts` (NEW) | 4 tests: success path, error path surfaces message, transient failure preserves prefs, token=null resets to defaults. |

**Net delta:** +7 Vitest tests across 2 new test files. **Vitest 20/20.** Backend untouched. `npm run build` clean (404 kB / 114 kB gz, exits 0).

🆕 **2026-05-19 — LLM rate limiting (#12)**

| File | Change |
|---|---|
| `services/rate_limiter.py` (NEW) | In-memory sliding-window limiter. Per-user `deque[float]` of timestamps; `asyncio.Lock` serialises check-and-record. Constants `PER_MINUTE_DEFAULT=30`, `PER_HOUR_DEFAULT=400` resolved at call time so tests can monkeypatch. Module docstring notes the in-process limitation. |
| `core/deps.py` | Added `rate_limit_llm` FastAPI dependency. Runs *after* `get_current_user` so anonymous callers get 401/403 first. |
| 5 routers (`srs`, `chat`, `reading`, `insights`, `books`) | Added `dependencies=[Depends(rate_limit_llm)]` to 10 LLM-backed routes. `GET /srs/due` intentionally exempt (cached glosses). |
| `api/_http.ts` (frontend) | Added friendly 429 handling: `rate_limit_minute` → "Too many AI requests…", `rate_limit_hour` → "AI request limit reached…". |
| `tests/conftest.py` | Autouse cleanup now calls `rate_limiter.reset_for_tests()` between tests. |
| `tests/test_rate_limit.py` (NEW) | 10 tests: limiter unit behaviour (4), window pruning, concurrent race, protected endpoint 429 burst, isolated per user, auth-fires-first, and exempt `/srs/due` confirmation. |

**Net delta:** +10 backend tests, +1 new module + 1 new test file. Full backend: **392 passed, 2 skipped, 0 xfailed, 0 failed.** Frontend Vitest 13/13, build passes.

🆕 **2026-05-19 — SRS active review is real production (#0a-2). Hole 0a fully closed.**

Backend:
| File | Change |
|---|---|
| `services/llm_service.py` | New `evaluate_production(target_text, target_lemma, user_answer, language)` — tool_use structured eval, not cached (high-cardinality input). MOCK mode does substring match for hermetic tests. |
| `services/review_service.py` | New `submit_production_answer` orchestrator. Validates ownership + active direction + non-grammar item_type, resolves target metadata from word/phrase tables, fast-path exact-match before LLM, routes through `apply_progression` so SM-2 advances identically to self-grade. |
| `routers/srs.py` | New `POST /api/v1/srs/review/{card_id}/produce` endpoint. ValueError → HTTP mapping: `card_not_found`/`target_missing` → 404, `passive_card`/`grammar_rule` → 400. |
| `models/schemas.py` | New `SRSProductionRequest`, `SRSProductionResponse`. |
| `tests/test_srs_produce.py` (NEW) | 12 tests: passive 400, grammar 400, other-user 404, auth 401/403, exact-match fast path (asserts LLM never called), LLM-judged correct → active SM-2 advance + level bump, LLM-judged incorrect → SM-2 reset, response shape, "I don't know" via existing `/review/{id}`, plus 3 mock-mode unit tests for `evaluate_production`. |

Frontend:
| File | Change |
|---|---|
| `types/index.ts` | `SRSReviewCard` extended with `prompt_text` + `answer_text`. New `SRSProductionResult`. |
| `api/srs.ts` | New `submitProductionAnswer(token, cardId, answer)` wrapper. |
| `components/SRSReviewPage.tsx` | Rewrote card rendering. Uses `prompt_text` on the front, `answer_text` on reveal/feedback. Passive: same self-grade flow (now reveals the real gloss instead of the same German word). Active: text input + "Submit" + "I don't know"; **no self-grade buttons exposed.** Feedback panel shows target, what the user typed, and the LLM verdict. |

**Net delta:** +12 backend tests, +0 frontend tests (Vitest still 13/13 — UI rewrite is integration-tested by the backend production endpoint tests). Frontend build passes (`npm run build` → `dist/` clean). Full backend: **382 passed, 2 skipped, 0 xfailed, 0 failed.**

🆕 **2026-05-19 — SRS gloss payload (#0a-1)**

| File | Change |
|---|---|
| `services/llm_service.py` | New `translate_item_gloss(text, item_type, language, *, pool)` — short English gloss for word/phrase, permanently cached. tool_use schema constrains output to 1-4 word glosses. Rejects `item_type='grammar_rule'` (callers use `short_explanation` directly). MOCK_LLM-aware. |
| `services/review_service.py` | `get_due_cards` now SELECTs `gr.short_explanation`, gathers glosses for word/phrase via `asyncio.gather`, and populates `prompt_text` / `answer_text` per direction. Defensive: gloss exception → fall back to `display_text`. |
| `models/schemas.py` | `SRSReviewCard` gains `prompt_text: str` and `answer_text: str`. Backward-compatible (existing fields kept). |
| `tests/test_srs_gloss.py` (NEW) | 10 tests: gloss for word, gloss for phrase, grammar_rule rejection, lowercase cache key normalization, passive shape, active shape, cache-hit invariant (second call doesn't re-invoke LLM), grammar_rule path makes no LLM call, existing-fields-preserved, phrase shape. |
| `tests/test_audit_holes.py` | `test_active_srs_card_has_english_prompt` xfail removed → regression guard. Hole 0a's backend half is closed. |

**Net delta:** +10 passing tests, +1 new file. xfail count drops 1 → **0** — first time since session start. Full suite: **370 passed, 2 skipped, 0 xfailed, 0 failed.**

🆕 **2026-05-19 — frontend build unblocked (TS cleanup)**

Fixed 9 pre-existing TS errors so `npm run build` passes for the first time (the 7 originally reported + 2 more that were hidden behind earlier compilation failures).

| File | Fix |
|---|---|
| `components/BookReaderPage.tsx:145` | `SentenceCard.onTokenClick` prop changed from `tokenIndex: number` to `tokenId: string` to match the real handler signature (token IDs became strings in migration 025). Call site stringifies the local index. |
| `components/BookReaderPage.tsx:464` | Removed unused `selectWord` from `useWordStatus` destructure. |
| `components/BookReaderPage.tsx:797` | Removed unused `showRightPanel` local. |
| `components/GuidedChatPage.tsx:140` | Cast `hintLevel` to `1 \| 2 \| 3` at the `HintPanel` boundary (the `hintLevel > 0` guard above guarantees it; TS doesn't narrow numeric literal unions through `>`). HintLevel state stays `0 \| 1 \| 2 \| 3` — `0` means "no hint shown yet". |
| `components/GuidedChatPage.tsx:311` | Removed unused `max` prop from `ProgressPill` (component never referenced it; both callers also dropped). |
| `components/PlaylistPanel.tsx:197` | Widened `inputRef` prop type to `RefObject<HTMLInputElement \| null>` for React 19's new useRef return type. |
| `components/TranscriptPanel.test.tsx:56` | Removed unused `allBlocks`. |
| `utils/sentenceUtils.tsx:3` | Added `import type { WordColorScheme } from '../config/wordColors';`. |
| `vite.config.ts:1` | Switched from `vite`'s `defineConfig` to `vitest/config`'s — same Vite API plus the `test` block now type-checks. |

**Net delta:** zero new tests (these are type-level cleanups), zero behavior changes. `npm run build` now produces a `dist/` bundle. Vitest still 13/13. Pre-existing test_settings warnings removed (mentioned earlier — those were already resolved).

🆕 **2026-05-19 — deploy-readiness bundle (#11 + #13 + #14 + #10)**

| File | Change |
|---|---|
| `backend/main.py` | `_parse_cors_origins(raw)` + `os.getenv("CORS_ORIGINS")` drive `allow_origins`. Whitespace-tolerant, drops empties, falls back to localhost when unset. |
| `backend/core/deps.py` | `jwt.ExpiredSignatureError` caught first → 401 with `detail="token_expired"` and `WWW-Authenticate: Bearer error="invalid_token", error_description="token expired"` header. Other `InvalidTokenError` cases keep `detail="Invalid token"`. |
| `.env.example` | Documents `CORS_ORIGINS` with a production example. |
| `frontend/src/api/_http.ts` (NEW) | Shared `assertOk`, `assertOkJson`, `signalAuthExpired`. 401 → clear `auth_token` + `auth_email` from localStorage, dispatch `CustomEvent('auth:expired', { detail: { reason } })`. |
| `frontend/src/api/reading.ts` | First consumer of the shared helper — its local `assertOk` deleted. |
| `frontend/src/App.tsx` | Layout listens for `AUTH_EXPIRED_EVENT`, calls `setToken(null)` + `navigate('/')`. HomePage now passes `recLanguage \|\| 'de'` into `useSearch`. |
| `frontend/src/hooks/useSearch.ts` | Drops hardcoded `'de'`; takes `language: string = 'de'` parameter. |
| `tests/test_deploy_readiness.py` (NEW backend) | 13 tests: CORS parser (7), CORS middleware ACAO header (2), JWT expired detail (1), generic invalid token (2), valid-token sanity (1). |
| `frontend/src/api/_http.test.ts` (NEW frontend) | 5 Vitest unit tests: 2xx passes through, non-401 surfaces detail, 401 token_expired clears auth + dispatches event with reason=expired, generic 401 clears auth + reason=unauthorized, signalAuthExpired direct test. |

**Net delta:** +13 backend tests, +5 frontend tests. Backend: **359 passed, 2 skipped, 1 xfailed, 0 failed.** Frontend Vitest: **13/13 passed** (including pre-existing TranscriptPanel tests).

🆕 **2026-05-19 — notification correctness (#4a)**

| File | Change |
|---|---|
| `routers/notifications.py` | Extracted `_yield_unseen(pool, user_id)` as a module-level async generator. Per-row mark-after-yield ordering. Route's `event_generator` now wraps it in the polling loop. Polling interval moved to a `_POLL_INTERVAL_SECONDS` constant. Also handles `payload` returned as a string by some asyncpg codec configs. |
| `subtitle-scraper/pipeline.py` | `_mark_request` now calls `_notify_user(..., "request_failed", {"reason": error})` whenever `status=="failed"`. Diff is one in-function `if` block; the six failure call sites get the notification automatically. |
| `tests/test_notifications.py` (NEW) | 7 tests: row not marked before yield, marked after yield resumes, disconnect leaves remaining rows unseen, empty user yields heartbeat, payload parses from string-JSONB, failed-mark writes `request_failed` notification, request_failed delivers through the helper. |

**Net delta:** +7 passing tests, +1 new test file. Full suite: **346 passed, 2 skipped, 1 xfailed, 0 failed.** Hole 28 + Hole 30 closed.

🆕 **2026-05-19 — unified enrichment dispatcher (#5c)**

| File | Change |
|---|---|
| `services/grammar_service.py` | New `enrich_grammar_rules` mirrors `enrich_phrases`: joins `grammar_rule_table` + `user_word_knowledge` + `srs_cards`. Display=`title`, secondary=`rule_type`. |
| `services/recommendation_service.py` | New `enrich_by_type(items: list[(item_type, item_id)])` — buckets by type, `asyncio.gather`s the three per-type enrichers, returns dict keyed by `(item_type, item_id)`. `recommend_items` refactored to use it (one call replaces three branches + the inline phrase import). `enrich_items` kept as the word-only backend; docstring updated to direct mixed-type callers at the dispatcher. |
| `services/insights_service.py` | `_build_card` now uses `enrich_by_type`. Removes the per-type bucket + gather + merge that duplicated the dispatch and silently dropped grammar rules. |
| `tests/test_recommendations.py` | +5 tests: dispatcher buckets words+phrases, omits unknown ids, no-collision-on-shared-int-id (conditional skip), recommend_items returns enriched phrase rows, recommend_items returns enriched grammar_rule rows (previously empty). |
| `tests/test_grammar_rules_srs.py` | +3 unit tests for `enrich_grammar_rules`: happy path with progress fields, unknown rule_ids omitted, empty input. |
| `tests/test_insights.py` | +1 HTTP test confirming the `frequent_unknowns` card now includes a prioritized grammar_rule. |

**Net delta:** +8 passing tests (1 conditional skip — collision test needs a word_id == phrase_id by chance). Full suite: **339 passed, 2 skipped, 1 xfailed, 0 failed.**

🆕 **2026-05-19 — settings test constant in sync (suite fully green)**

| File | Change |
|---|---|
| `tests/test_settings.py` | `ALL_PREFERENCE_KEYS` now derived: `set(DEFAULTS) \| {"liked_categories", "disliked_categories", "liked_genres", "disliked_genres"}`. `test_get_preferences_new_user_returns_defaults` updated to expect `{**DEFAULTS, ...empty derived lists}`. Both changes mean adding a key to `settings_service.DEFAULTS` no longer breaks the suite. |

**Net delta:** 3 pre-existing failures resolved. Full backend: **331 passed, 1 skipped, 1 xfailed, 0 failures.** First fully green run.

🆕 **2026-05-19 — phrases first-class in chat + matcher integration fix (#5b)**

| File | Change |
|---|---|
| `services/matcher_service.py` | `_extract` helper now does `_pf.nlp(sentence)` before `extract_german_logic`. Five `test_matcher` failures turn green; pre-existing failure count drops from 8 → 3. |
| `services/chat_service.py` | `match_learning_words` augmented with a second branch that calls `matcher_service.match_sentence_with_ids`, intersects matched phrase_ids with `user_word_knowledge` (item_type='phrase', status != 'known'), and appends polymorphic results. Word path unchanged. |
| `services/guided_chat_service.py` | `get_next_target` rewritten so each of 3 priority tiers considers words + phrases. LEFT JOINs + item_type-aware CASE expressions for tiers 1 + 2; UNION ALL for tier 3 random fallback. Return shape unchanged. |
| `tests/test_audit_holes.py` | `test_match_learning_words_matches_phrases` xfail flipped → regression guard. Xfail count drops 2 → 1 (only Hole 0a remains). |
| `tests/test_free_chat_progression.py` | +4 phrase-matching tests: surface-form match, inflected production (skipped if `sich freuen auf` isn't seeded), known-status exclusion, word + phrase in same message. |
| `tests/test_guided_chat_targets.py` (NEW) | 7 tests covering all 3 priority tiers for phrases, word-vs-phrase tiebreak by due_date, priority 2 skip-when-card-exists, priority 3 reachability, return-shape contract. |

**Net delta this turn:** +16 passing tests, +1 new test file, 1 xfail flipped, 5 pre-existing failures resolved as side effect. Full suite: **328 passed**, 1 skipped (conditional inflection test), 1 xfailed, 3 pre-existing settings failures.

🆕 **2026-05-19 — atomic status updates via `status_override` (#2)**

| File | Change |
|---|---|
| `services/progression_service.py` | `apply_progression` gained `status_override: str \| None = None` kwarg. When set, writes status inside the existing transaction (same INSERT…ON CONFLICT). Re-fetches the final row at end of txn and returns it. |
| `routers/words.py` | `update_status` no longer calls `word_service.upsert_word_status` — single call to `apply_progression(..., status_override=body.status)`. |
| `services/word_service.py` | `upsert_word_status`, `VALID_STATUSES`, `VALID_ITEM_TYPES` removed (no other callers). |
| `models/schemas.py` | `WordStatusUpdate.status` tightened to `Literal["unknown","learning","known"]` so Pydantic returns 422 directly. |
| `tests/test_progression.py` | +7 atomicity tests covering: learning writes status + creates both cards; known persists status without active fabrication; unknown persists status + resets existing cards; row created on fresh user via status_override only; status overwrite path; status-touching events without override don't change status; None returned when no write happens. |
| `tests/test_words.py` | Updated `test_first_status_update_creates_row` — response now reflects post-progression state (passive_level=1 instead of the old stale 0). Comments touched up. |

**Net delta:** +7 passing tests this turn. Full suite: 312 passed, 2 xfailed (Hole 0a, Hole 18 — both have scoped fixes pending), 8 pre-existing failures untouched.

🆕 **2026-05-19 — delete dead `srs_service` (#1)**

| File | Change |
|---|---|
| `services/srs_service.py` | Deleted entirely. |
| `routers/srs.py` | Rewritten — kept only `/due` + `/review/{card_id}`; dropped `/check-answer`, `/magic-sentences`, `/cloze-questions`. |
| `models/schemas.py` | Removed `CheckAnswerRequest`, `MagicSentencesRequest`, `SentenceResult`, `MagicSentencesResponse`, `ClozeQuestionsRequest`, `ClozeQuestionResult`. |
| `main.py` | Updated the inline `/api/v1` route comment for the SRS router. |
| `services/review_service.py` | Removed the now-stale "NOTE: legacy srs_service" docstring lines. |
| **Tests** | None added/removed. No test previously asserted the dead endpoints' behaviour. Full suite still: 305 passed, 2 xfailed, 8 pre-existing failures. |

🆕 **2026-05-18 (latest) — insights transcript filter (#5a)**

| File | Tests | Covers |
|---|---|---|
| `usage_events_service.py:59` | (service edit) | Added `'transcript'` to the context IN clause in `most_frequent_unknown_items`. |
| `test_audit_holes.py` (edited) | 1 xfail flipped | `test_transcript_context_in_frequent_unknowns_aggregation` is now a regular passing regression guard. |
| `test_insights.py` (edited) | 1 xfail flipped | `test_frequent_unknowns_includes_transcript_clicks` is now a regular passing regression guard. |

**Xfail count drops from 4 → 2.** Remaining pinned holes: English prompt on active SRS payload (Hole 0a), phrases in `match_learning_words` (Hole 18).

🆕 **2026-05-18 — finish rule-table cleanup (#5d remaining)**

| File | Tests | Covers |
|---|---|---|
| `test_progression.py` (edited + extended) | 1 unit test updated + 7 new integration tests | `test_compute_delta_status_marked_unknown` asserts new SRS-reset shape; new integration tests: existing passive card reset, existing active card reset, no active card created when missing, no passive card created when missing, levels unchanged. For passive_review_correct: passive_level increments, card still SM-2 advances. |
| `test_srs_review.py` (edited) | 1 unit test updated | `test_passive_review_correct_delta` asserts `passive_delta == 1`. |
| `test_audit_holes.py` (edited) | 2 xfails flipped to regular passing tests | `test_status_marked_unknown_should_reset_srs` and `test_passive_review_correct_bumps_passive_level` now serve as regression guards. |

**Xfail count drops from 6 → 4.** Remaining pinned holes: transcript context in frequent_unknowns aggregation (Hole 5, ×2 — one in test_insights.py + one in test_audit_holes.py), English prompt on active SRS payload (Hole 0a), phrases in `match_learning_words` (Hole 18).

🆕 **2026-05-18 (later) — status_marked_known semantics fix**

| File | Tests | Covers |
|---|---|---|
| `test_progression.py` (edited + extended) | 1 updated unit test + 4 new integration tests | `test_compute_delta_status_marked_known` asserts the new minimal rule (passive_srs="correct" only); new tests cover: passive card created but no active card, levels unchanged, existing active card not advanced, times_used_correctly not bumped, active_review_correct and free_chat_used_correctly still increment active_level. |
| `test_words.py` (extended) | 4 new HTTP-level tests | `PUT /words/word/{id}/status` with status=known: writes status, no active SRS card created, active_level stays 0, an existing active card (from prior status=learning) is not advanced. |
| `test_audit_holes.py` (edited) | -1 (removed) | The `test_status_marked_known_active_delta_reaches_threshold` xfail was based on the wrong assumption that the rule should hit the mastery threshold; the new design makes manual known not bump active_level at all. Hole closed → test removed. |

**Net delta (cumulative since session start):** 4 new files + 2 edited test files; 32 new test functions across 4 files + 9 added/edited within existing files; 6 audit-hole xfail pins remain (was 7 — Hole 8 closed).

🆕 **2026-05-18 — coverage expansion + audit-hole pinning**

| File | Tests | Covers | Related |
|---|---|---|---|
| `test_progression.py` (edited) | 2 updated | `status_marked_learning` now creates both passive AND active SRS cards (#0b) | TODO #0b |
| `test_srs_review.py` (edited) | 1 updated | `/srs/due` returns 2 cards (passive + active) after marking learning | TODO #0b |
| `test_grammar_rules_srs.py` (existing — passes via guard) | 0 added | Grammar rules remain passive-only after the rule change, via `_update_srs` guard at progression_service.py:268 | TODO #0b follow-on |
| **🆕 `test_insights.py`** | 7 | `/insights/cards` shape, frequent_unknowns from chat events, recent_mistakes from incorrect events, auth gate; one xfail pins Hole 5 (transcript context exclusion) | Audit Hole 5 |
| **🆕 `test_content_requests.py`** | 8 | POST creates pending row, duplicate returns existing, failed→pending on resubmit, GET lists newest-first, user isolation, auth gate. Subprocess spawn patched out via `monkeypatch`. | Audit Hole 30 / TODO #4 |
| **🆕 `test_audit_holes.py`** | 7 (6 xfail + 1 sentinel) | One xfail per known unfixed hole: status_marked_unknown reset, status_marked_known reaches threshold, passive_review_correct bumps level, transcript in frequent-unknowns aggregation, active SRS payload includes English prompt, phrase matching in free chat. Plus a sanity test that the rule table contains all expected events. | Audit Holes 5, 7, 8, 15, 18, 0a — flip strict=True as each fix lands |
| **🆕 `test_reminders.py`** | 5 | `/reminders/summary` shape, zero-state, counts after marking learning, excludes known status, auth gate | gap fill |

**Net delta:** 4 new files, 27 new test functions (20 passing + 7 xfailed pinning audit holes), 0 net failures.

🆕 **2026-05-19 — #0b regression guards + Hole 9 (passive learning → known)**

| File | Tests | Covers | Related |
|---|---|---|---|
| `test_progression.py` (extended) | +3 | `status_marked_learning` regression guards: active_level stays 0, times_used_correctly stays 0, re-marking does not duplicate or clobber existing cards | TODO #0b |
| `test_progression.py` (extended) | +5 | Hole 9 promotion path: `learning + passive_level ≥ threshold → known`; below-threshold stays `learning`; promotion leaves `active_level` + `times_used_correctly` at 0; promotion does NOT advance the active SRS card; once `known`, further passive evidence keeps it `known` | Audit Hole 9 / TODO Hole 9 |

**Net delta:** 8 new test functions in 1 edited file. Full suite: 400 passed, 2 skipped.

🆕 **2026-05-19 — Hole 26 (manual demotion resets levels + reschedules cards)**

| File | Tests | Covers | Related |
|---|---|---|---|
| `test_progression.py` (extended) | +8 unit | `_is_demotion` rank logic: known→learning/unknown True; learning→unknown True; all upgrades False; same-status False; None prior False | Audit Hole 26 |
| `test_progression.py` (extended) | +16 integration | `known → learning`: passive_level=1, active_level=0, both cards reset (ease preserved); missing active card created; times_used_correctly preserved. `known → unknown`: both levels=0; existing cards penalised (ease −0.15); missing active card NOT created; times counters preserved. `learning → unknown`: levels=0; existing cards reset. Regression: `unknown → learning` still additive; `learning → known` still preserves levels. Post-demotion: production events still climb back to known. Grammar-rule guard: demotion does not create active grammar SRS card. | Audit Hole 26 |

**Net delta:** 24 new test functions in 1 edited file. Full suite: 424 passed, 2 skipped.

🆕 **2026-05-19 — #29 backend end-to-end progression loop**

| File | Tests | Covers | Related |
|---|---|---|---|
| **🆕 `test_e2e_learning_loop.py`** | 2 | Golden path through real HTTP routes: register → PUT status=learning → GET /srs/due → POST /srs/review/{passive} correct → POST /srs/review/{active}/produce (exact-match fast path × ACTIVE_MASTERY_THRESHOLD) → status=known → /srs/due filters known → PUT status=learning (Hole 26 'reset') → PUT status=unknown (Hole 26 'incorrect'). Plus a companion test that 'known via confidence click → unknown' does not fabricate a missing active card. LLM mocked via `llm_service._MOCK = True`; fast path keeps the active-produce flow LLM-free. | TODO #29 (#0a-2 + #0b + Hole 9 + Hole 26 regression lock) |

**Net delta:** 1 new file, 2 new test functions. Full suite: 426 passed, 2 skipped.

🆕 **2026-05-20 — #5 ReadingReviewPage + mastered → known wiring**

| File | Tests | Covers | Related |
|---|---|---|---|
| `test_reading_progression.py` (edited + extended) | 1 updated + 3 new = +3 net | Replaced `test_review_mastered_does_not_change_srs_card` with `test_review_mastered_marks_catalog_item_known` (uwk.status becomes 'known' on catalog match) + `test_review_mastered_does_not_inflate_active` (active_level stays 0, no active card fabricated, existing active card unchanged) + `test_review_mastered_advances_passive_card_via_status_marked_known` (status_marked_known passive_srs='correct' so passive card SM-2 advances) + `test_review_mastered_unmatched_succeeds` (no catalog match → review still succeeds) | TODO #5 / Audit Hole 24 |
| **🆕 `ReadingReviewPage.test.tsx`** | 9 | Empty queue state; renders canonical + sentence + doc title; each outcome button posts the correct body to `/reading/selections/{id}/review`; session-complete panel after final review; advances to next selection after submit; all three action buttons + close button meet the 44px touch target; error message surfaces on review POST failure. Fetch stubbed via `vi.stubGlobal('fetch', …)`. | TODO #5 |

**Net delta:** 1 new frontend test file (9 tests) + 3 net new backend tests. Backend: 429 passed, 2 skipped. Frontend: 103 passed (24 files).

🆕 **2026-05-20 — #5 follow-up: save_selection marks linked catalog item 'learning'**

| File | Tests | Covers | Related |
|---|---|---|---|
| `test_reading_progression.py` (extended) | +4 | `save_selection` on matched word → `uwk.status='learning'`, `passive_level=1`, `active_level=0`, `times_used_correctly=0`. Save creates BOTH passive and active SRS cards (#0b shape). Same contract for matched phrases (phrase_id catalog match → status='learning'). Unmatched canonical → save returns 201, no user_word_knowledge row, no SRS cards. | TODO #5 follow-up |

**Net delta:** 4 new backend tests. Backend: 433 passed, 2 skipped.

🆕 **2026-05-20 — #24 LLM cache thundering-herd guard**

| File | Tests | Covers | Related |
|---|---|---|---|
| `test_llm_cache.py` (extended) | +6 | New `get_or_compute` helper: cache hit skips compute; 10 concurrent same-key callers fire exactly 1 compute and all receive the same result; concurrent different-key callers run in parallel (no cross-key block); compute exception releases the lock and allows retry; double-check inside the lock short-circuits later waiters' compute. Driven with `asyncio.Event` for deterministic concurrency, no real LLM calls. | TODO #24 |

**Net delta:** 6 new backend tests. Backend: 439 passed, 2 skipped.

🆕 **2026-05-20 — #24-followup: migrate all cached LLM call sites to `get_or_compute`**

| File | Tests | Covers | Related |
|---|---|---|---|
| **🆕 `test_llm_cache_migration.py`** | 6 | Concurrent same-key proof for the 4 distinct provider surfaces touched by the migration: `translate_item_gloss`, `reading_translate_sentence`, `reading_explain_in_context`, `book_repair_block`. Plus: `evaluate_production` remains uncached (2 identical calls → 2 provider invocations), and `translate_item_gloss` second call skips the provider (regression guard against the migration accidentally bypassing the cache check). Each test stubs the underlying provider with an `asyncio.Event`-gated counter; no real LLM calls. | TODO #24-followup |

**Net delta:** 1 new file, 6 new backend tests. Backend: 445 passed, 2 skipped. All 9 cached LLM call sites now route through the lock; 4 uncached and 2 read-only-lookup paths intentionally left as-is and documented in the new tests.

🆕 **2026-05-20 — pytest-xdist parallel runs**

`pytest -n auto` now finishes in ~48s (vs ~165s serial — 3.4× speedup on this machine, 8 workers). Required surgical changes for safe parallel cleanup:

| File | Change |
|---|---|
| **🆕 `tests/_email_helper.py`** | `worker_id()`, `make_test_email()`, `cleanup_pattern()`. Embeds `PYTEST_XDIST_WORKER` in email format; serial runs collapse to `'main'`. |
| `tests/conftest.py` | Cleanup fixture now uses `LIKE cleanup_pattern()` instead of bare `'test+%@example.com'` — each worker only deletes its own rows. |
| `tests/test_*.py` (24 files) | Replaced every `f"test+{uuid.uuid4().hex[:N]}@example.com"` inline literal with `make_test_email()`. Test files import via `from ._email_helper import make_test_email`. |
| `tests/test_words.py` + `tests/test_recommendations.py` | Replaced 5 instances of `SELECT user_id FROM users WHERE email LIKE 'test+%@example.com' ORDER BY user_id DESC LIMIT 1` with the parameterised `LIKE $1, cleanup_pattern()` form — without this, those tests grabbed another worker's user and asserted against rows that worker was about to delete. |
| `backend/requirements.txt` | `pytest-xdist` added. |

Stability: ran `pytest -n auto` twice back-to-back, both green at 433 passed / 2 skipped, no flakes. Net delta: 1 new helper module, +44 changed lines across test files. Zero production-code changes.

### Audit holes pinned as xfail (flip on fix)

When closing each TODO/audit hole, remove the `@pytest.mark.xfail` (or set `strict=True`):

| File | Test | Hole | TODO |
|---|---|---|---|
| `test_insights.py` | `test_frequent_unknowns_includes_transcript_clicks` | 5 | #5a |
| `test_audit_holes.py` | `test_status_marked_unknown_should_reset_srs` | 7 | #5d |
| `test_audit_holes.py` | `test_passive_review_correct_bumps_passive_level` | 15 | #5d |
| `test_audit_holes.py` | `test_transcript_context_in_frequent_unknowns_aggregation` | 5 | #5a |
| `test_audit_holes.py` | `test_active_srs_card_has_english_prompt` | 0a | #0a |
| `test_audit_holes.py` | `test_match_learning_words_matches_phrases` | 18 | #5b |

---

## Conventions when adding tests

1. **One test file per router or per service** as a rule of thumb. Cross-cutting flows can sit in a dedicated file (e.g. `test_free_chat_progression.py`).
2. **DB-backed tests use the `db_pool` fixture.** Hermetic logic tests should not touch the DB.
3. **HTTP-level tests use the `client` fixture.** Use the bearer token from `_register_and_login` helpers.
4. **Lock in audit holes with xfail or assertion-of-current-behaviour** so that when the fix lands, the test naturally flips to passing without rewriting. Mark with `@pytest.mark.xfail(reason="TODO #X: …", strict=False)`.
5. **After adding tests, update this file** (TESTS.md). The CLAUDE.md convention says so.
