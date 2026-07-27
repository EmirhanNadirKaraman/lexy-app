# TESTS.md

Test inventory and coverage status. **Update this file whenever you add, remove, or rename a test.**

---

## Backend test suite — `lexy-app/backend/tests/`

Test runner: pytest + pytest-asyncio. Fixtures in `conftest.py` provide `db_pool` (real Postgres) and `client` (httpx.AsyncClient against the app).

### ⚠️ `pytest-randomly` is blocked repo-side — do not remove the block

Both ini files (`lexy-app/pytest.ini` for the backend suite, `pytest.ini` for the
root pipeline suite) carry `addopts = -p no:randomly`. **This is load-bearing
whenever `pytest-randomly` is present in the environment** — it does not have to
be a declared dependency to break the suite, and it is not in any
`requirements.txt` (it arrived transitively; observed as 4.1.0 on 2026-07-27).

The conflict, briefly: pytest-randomly reseeds before and after every test with
`seed = session_seed + offset`, where `offset` is a crc32 of the test nodeid
spread across the full 32-bit range. It masks that value for **its own** numpy
call (`np_random.seed(seed % 2**32)`) but forwards the **unmasked** sum to every
`pytest_randomly.random_seeder` entry point. spaCy's thinc registers
`thinc.api:fix_random_seed` there, and that function calls `numpy.random.seed()`
directly with no mask. numpy rejects any seed ≥ 2**32, so the sum overflows and
raises `ValueError: Seed must be between 0 and 2**32 - 1` at both setup and
teardown of nearly every test.

Observed impact with the plugin active and the block removed:

| Suite | Without the block | With the block |
|---|---|---|
| backend `pytest -n auto` | 211 passed, **1036 errors** | 745 passed, 2 skipped |
| root `pytest tests/` | 1 passed, **1339 errors** | 671 passed |

Note this is an *environment* failure, not a test-quality signal — every error is
a setup/teardown crash, and no assertion ever runs. If you genuinely want
randomised ordering later, the fix is upstream-shaped (mask the seed before
handing it to entry points, or drop thinc's seeder), not deleting the `addopts`
line.

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
| `test_database_ssl.py` | 🆕 S4 DB TLS. asyncpg `_resolve_ssl` (`DB_SSL_MODE` unset/`disable`→`False`, `require`/`verify-ca`/`verify-full`→passthrough, case-insensitive, `prefer`/`allow`/garbage→`ValueError`); mocked `create_pool` forwards `ssl=` + raises before opening a pool on a bad mode. libpq `resolve_sslmode` (for alembic/scraper): unset/`disable`→`None` (omit param), `require`/`verify-*`→string, `prefer`/`allow`/invalid→`ValueError` |
| `test_password_limits.py` | 🆕 S10 bcrypt 72-byte cap: register rejects >72-byte passwords byte-accurately (`"a"*73`→422, multibyte `"€"*37`/111B→422, `"€"*24`/72B→201), `min_length=8` preserved, 72-byte register→login round-trip; login/delete body-capped at 1024 (over-72-byte login→401 not 422, >1024→422) |
| `test_docs_gating.py` | 🆕 S11 docs gating: `_docs_enabled` parse matrix (`ENABLE_DOCS` unset/falsy/garbage→off, `1`/`true`/`yes`→on); live app `docs_url`/`redoc_url`/`openapi_url` agree with the flag; `/docs`,`/redoc`,`/openapi.json`→404 when off |
| `test_phrases_seed_admin.py` | 🆕 S17 admin gate: `POST /phrases/seed` → 403 `admin_required` for a normal user, 201 for an admin (`is_admin` planted via direct SQL, not the settings API), 401/403 unauthenticated |
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
| `test_settings.py` | preferences GET/PUT — ✅ passing (the 3 stale-`ALL_PREFERENCE_KEYS` failures were resolved 2026-05-19; see the resolved-failures table below) |
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
| ~~`test_account_deletion.py::test_client_error_log_user_id_set_null_on_delete` and `test_srs_backfill.py::test_audit_ignores…`~~ (the `srs_cards` family) | ~~Intermittent under `pytest -n auto`~~ | **RESOLVED 2026-05-24 (test isolation round 3)** — see the round-3 section below. The actual errors were a `client_error_log` global-`DELETE`/unscoped-pick race **and** a `srs_cards` **ForeignKeyViolation** (not Unique) from the global backfill `INSERT…SELECT` racing a concurrent worker's user deletion. Fixed by per-worker message tagging (client errors) + an additive `user_id` scope on the four `srs_*` maintenance functions. Full suite `-n auto` now green twice. |

---

## Frontend tests — `lexy-app/frontend/`

Test runner: Vitest + @testing-library/react + jsdom. Setup: `src/test/setup.ts`.

**Current count (2026-07-27): 248 tests across 41 files.** Was 238/40 before the
`auth.ts` error-parser dedup, and 219 across 37
files before the #39 lemma-correction UI landed (+19 tests, +3 files).
Historical baseline: ~179 across ~31 files at W13 (2026-05-20).
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
- Lemma corrections (#39 frontend, 2026-07-27) — see the dated row below.

🆕 **2026-07-27 — #39 lemma-correction UI (frontend only, +19 tests / +3 files)**

Frontend for the already-shipped slice 3A/3B backend. **No backend behaviour
changed** — no backend Python was touched; `test_lemma_corrections.py` (41 tests)
still passes unmodified.

| File | Tests | Covers |
|---|---|---|
| `components/LemmaFlagButton.test.tsx` | 6 | POST body shape; **blank optional fields omitted, not sent as `''`**; quiet success collapses the form; a failed submit keeps the form open AND preserves typed input; the 429 throttle message surfaces from `_http.ts`; 44px target + 16px input guards |
| `components/AdminLemmaQueuePage.test.tsx` | 8 | renders `surface_form` / `observed_lemma` / `suggested_lemma` / `context_text` / `report_count`; accept and reject each hit the right endpoint and update the row **in place** (asserted by counting list fetches — no refetch); `corrected_lemma` omitted when blank so the backend falls back to `suggested_lemma`, sent when typed; a `nothing_to_promote` 400 surfaces inline and leaves the row actionable; a 403 renders the error state, not a misleading empty queue |
| `hooks/useIsLemmaAdmin.test.tsx` | 5 | 200 → true, 403 → false, starts false (no link flash), no probe without a token, network failure resolves false |

**Two things these tests deliberately pin down:**

1. **Admin gating is discoverability, not security.** `is_admin` lives in
   `users.settings` JSONB and is exposed in *no* response model — not the token,
   not `/settings/preferences` — so the client has no flag to read and instead
   probes the admin endpoint. `require_admin` on the backend routes is the real
   gate. The 403-error-state test in `AdminLemmaQueuePage.test.tsx` exists
   precisely so nobody later mistakes the hidden nav link for enforcement.
2. **`/adjudicate` is intentionally unused.** It returns 503 in production
   (slice 3C ships no adjudicator), so no UI is built against it and no test
   references it.

**Gotcha found while adding these:** `npx tsc --noEmit` passed while
`npm run build` failed. `build` runs `tsc -b`, a different project scope, which
caught two uses of `global` (absent from the browser lib). **`tsc --noEmit`
alone does not prove the build is clean** — run `npm run build` too.

---

## Pipeline tests — `tests/`

Hermetic pytest tests. Two sub-trees:
- `tests/runtime/` — **97 tests** against the runtime root pipeline modules, in
  ten files: subtitle cleaning / ingestion / merging / segmentation, quality
  filter, exposure service, eligibility + unit extraction, onboarding tiers,
  learning invariants, and a pipeline composition smoke test.
- ~~`tests/{subtitles,learning,exposure,pipeline}/` — 537 tests against the
  `src/app/` refactor.~~ **Deleted 2026-07-27** with the refactor itself; the
  valuable behaviour was ported into `tests/runtime/` first (TODO #17).
- Scraper-facing: `tests/test_scraper_channels.py`, `tests/test_scraper_es_path.py`, 🆕 `tests/test_scraper_db_ssl.py` (S4 residual — `subtitle-scraper/db_ssl.py` resolver matrix + `seed_channels.connect()` forwards `sslmode`; scraper modules imported via add-if-absent / `sys.modules.pop` fixtures so the file never pollutes `sys.path` for `test_scraper_channels`), and 🆕 `tests/test_language_config.py` (#18 — `language_config.py` values/order/helpers/unknown-lang/malformed-config + pipeline-no-longer-hardcodes; loaded by `spec_from_file_location` with **no `sys.path` mutation**, the round-3 lesson — a module-level insert here broke `test_scraper_channels` under churn).

Root suite baseline: **211 passed** (97 runtime + 114 scraper). Run
`pytest tests/` from repo root. Historical: 562 at W4, peaking at 744 before
the `src/app/` deletion, 207 immediately after it.

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

🆕 **2026-05-24 (latest) — S9 + S2 registration hardening (no email infra)**

Registration now returns a single generic failure for duplicate-email and bad/missing invite-code alike (S9 enumeration-leak removed), and an optional `REGISTRATION_CODE` env gates signup invite-only (S2), on top of the S1 per-IP throttle.
- Backend `test_auth.py` **+5**: duplicate uses `GENERIC_REGISTER_ERROR`; code ignored when env unset; code required (missing/wrong → generic 400, correct → 201) when set; wrong-code and duplicate-email responses are byte-identical.
- Frontend `LoginForm.register.test.tsx` **+4** (new): optional invite-code field shows only in register mode (accessible label); `register()` gets the code when entered / `undefined` when blank; backend generic failure surfaces in the form.

🆕 **2026-05-24 — S3 account-deletion password re-auth**

`DELETE /api/v1/account` now requires the current password in addition to the bearer token.
- Backend `test_account_deletion.py` **+3**: bare DELETE (token only, no body) / empty password / wrong password → **403** with account + cascade data intact; the existing delete tests now send the password via `client.request("DELETE", …, json={"password": …})` (httpx's `client.delete` takes no body).
- Frontend `AccountDeletion.test.tsx`: confirm step shows a labelled password field, confirm is disabled until filled, sends `(token, password)`; a failure keeps the confirm UI open. New `api/account.test.ts` **+3**: password in the DELETE body, clears auth on 204, **preserves** auth on a 403 (wrong password — user stays logged in).

🆕 **2026-05-24 — Test isolation: own the test word (global-table race)**

Root cause of the growing flake cluster (`test_srs_review.py` 1→2→4→6 failures across full-suite runs, plus intermittent `test_suggest` / `test_srs_backfill` / `test_account_deletion`): tests grabbed a **shared** `word_table` row via an unfiltered `SELECT ... LIMIT N` (no `ORDER BY`). Under `pytest -n auto`, another worker's `_reap_word_ids` (conftest teardown) could delete that exact row mid-test; `review_service.get_due_cards`' filter `WHERE wt.word_id IS NOT NULL` then silently dropped the card and the assert failed. Order-independent (test order was not randomised — see the pytest-randomly note dated 2026-07-27 below), concurrency-triggered (passed serially / in isolation).

Fix (tests only — **no production change**). Two shared, race-safe patterns now exist; both prevent selecting a row another worker can reap:
| Pattern | Where it lives | When to use |
|---|---|---|
| `make_word` / `srs_word` fixtures + `_word_helper.insert_owned_word` + autouse `_reap_owned_words` | `conftest.py`, `_word_helper.py` | **Default.** Inserts a uniquely-named (`_testword_<uuid>` / `_srstest_<uuid>`) row per call; reaped at teardown. Unique surface ⇒ no unfiltered pick returns it and no worker can reap it. For tests needing an id (or a self-matching surface, e.g. the `/produce` answer). |
| `word !~ '[0-9_]' ORDER BY word_id LIMIT 1` (real-word filter) | `test_words.py`, `test_free_chat_progression.py` (pre-existing); `test_reading_progression.py` (this pass) | Deterministically picks a REAL corpus word, excluding synthetic surfaces (digits/underscores). Use where the surface must round-trip the matcher/lemmatizer (`find_catalog_item`) — a synthetic token wouldn't resolve. Also race-safe (real words are never reaped). |

**Migrated to owned words:** `test_srs_review`, `test_reminders`, `test_audit_holes`, `test_rate_limit`, `test_guided_chat_targets`, `test_recommendations`, `test_usage_events`, `test_e2e_learning_loop`, `test_progression`, `test_transcript_click`, `test_insights`, `test_srs_produce`, `test_srs_backfill`, `test_srs_gloss`, `test_srs_cleanup`, `test_prioritization`. **Real-word filter:** `test_reading_progression`. **Already safe, left as-is:** `test_words`, `test_free_chat_progression`. **Out of scope** (different tables, not part of this race): `phrase_table` / `sentence` LIMIT picks (`_get_phrase`, `_get_two_sentences`).

**Regression guards:** `test_word_fixtures.py` — `make_word` + `insert_owned_word` produce unique, present rows; custom surface + language honoured.

**Validation:** newly-migrated subset serial → 188 passed; SRS + churners `-n auto` 3× → 125 each; **full suite `-n auto` run 2× → 623 passed, 2 skipped, 0 failed each** (previously 1–6 intermittent failures per run).

🆕 **2026-05-24 — Test isolation round 2: shared-catalog reads/counts**

Round 1 owned the `word_table` rows tests *pick*; round 2 fixes tests that depend on the *shared global state* of catalog tables a different way. Two flakes were reproduced under `-n auto` (both green serially / in isolation):

| Flake | Root cause | Fix |
|---|---|---|
| `test_suggest.py::test_suggest_respects_limit` (+ 8 siblings via the same fixtures) | `seeded_words` / `seeded_de` inserted a **shared** `zzqx%` namespace into `word_table` and tore down with `DELETE … WHERE word LIKE 'zzqx%'`. A concurrent worker's teardown deleted this test's lowercase rows mid-run while leaving the capital casing variant → observed `['zzqxbajo','Zzqxalto']`. | Fixtures now build rows via `make_word` under a per-test-unique `zzqx<uuid>` prefix (reaped by id, no shared namespace, no prefix `DELETE`) and **yield the surfaces**; assertions reference them instead of hardcoded literals. |
| `test_account_deletion.py::test_shared_catalog_data_preserved` and `test_srs_cleanup.py::test_apply_does_not_touch_word_or_phrase_tables` | Asserted `SELECT COUNT(*) FROM word_table/phrase_table/grammar_rule_table/channel/video` was unchanged across an operation — but other workers insert/reap owned catalog rows concurrently, so the global counts drift. | Insert **owned rows** (unique business keys) per catalog table, run the operation, assert each *specific* row still exists by key (and clean up by exact key). Stronger than the count check: account-deletion now also links the user → the owned word first. |

**Acceptable shared-row reads left as-is (verified, not churned):** `phrase_table` `LIMIT 1` picks (`_get_phrase` in `test_srs_gloss`/`test_srs_backfill`/`test_reading_progression`/`test_audit_holes`/`test_recommendations`) read a row that `phrase_service` seeds at startup and **no test reaps**; `video`/`sentence` `LIMIT 1` reads (`test_recommendations`, `test_transcript_click`) hit scraper-populated rows that tests only ever read. None are mutated by the suite, so the pick is stable — unlike `word_table`, which has active per-test reaping. (If a future commit adds a phrase/video-reaping fixture, revisit these.)

**Validation:** 3 fixed files serial → 30 passed; fixed + churners (`test_words`/`test_recommendations`/`test_e2e_learning_loop`) `-n auto` → 113 passed / 1 skipped; **full suite `-n auto` run 2× → 687 passed, 2 skipped, 0 failed each.** No production code changed.

🆕 **2026-05-24 — Test isolation round 3: `client_error_log` race + `srs_cards` FK race**

The last documented flake family. Two distinct root causes (the "UniqueViolation" label in the prior tracker was imprecise — the srs one is a **ForeignKeyViolation**):

| Cause | Detail | Fix |
|---|---|---|
| `client_error_log` shared-table race (`test_client_errors`, and `test_account_deletion::test_client_error_log_user_id_set_null_on_delete` as collateral) | Every `test_client_errors` test picked `ORDER BY error_id DESC LIMIT 1` (another worker's row) and the autouse cleanup did a **global** `DELETE FROM client_error_log` (deleting other workers' in-flight rows → `row=None` → `TypeError`). | `test_client_errors.py` rewritten: per-worker `_tag()` prefix on every message (rides at the front so it survives `MAX_MESSAGE` truncation), query the row back by tag, cleanup `DELETE … WHERE message LIKE 'cetest-<worker>-%'`. The collateral account-deletion test needed no change once the global DELETE was gone. |
| `srs_cards` FK race (`test_srs_backfill`, `test_srs_cleanup`, and the varying victims `test_grammar_rules_srs`/`test_audit_holes`) | The maintenance ops `backfill_missing_active_cards` / `cleanup_orphan_srs_cards` scan **all** users. The backfill `INSERT…SELECT` FK-violated when a concurrent worker's user was deleted mid-statement (the FK check reads committed state, not the snapshot — a `JOIN users` would NOT close it); the cleanup's global `DELETE` + count assertions also collided. | **Additive `user_id: str \| None = None`** on all four `srs_backfill_service`/`srs_cleanup_service` functions (default `None` = the global behaviour the maintenance scripts use, byte-identical). Tests pass their own `user_id` so a global scan can't touch other workers' rows. One global dry-run smoke test per service keeps the unscoped path covered. |

**Why a service signature change is in-scope:** the `user_id` param is strictly **additive** — every existing caller (`scripts/backfill_missing_active_srs.py`, `scripts/cleanup_orphan_srs_cards.py`) is unchanged, and "scope a backfill/cleanup to one user" is a genuinely useful maintenance capability. No behaviour change for the global path.

**Validation:** churn group (`test_client_errors` + `test_srs_backfill` + `test_grammar_rules_srs` + `test_srs_cleanup` + `test_audit_holes`) `-n auto` × 3 → 46 passed each (was 3–6 failing); **full suite `-n auto` × 2 → 704 passed, 2 skipped, 0 failed each.**

🆕 **2026-05-24 — Test isolation round 4: grammar_rule_table unscoped pick**

A residual flake round 3 left behind: `test_grammar_rules_srs.py::test_grammar_rule_appears_in_srs_due_with_title_as_display_text` still failed intermittently under `-n auto`. Root cause: `_get_german_rule` did `SELECT … WHERE language='de' LIMIT 1` with **no ORDER BY** (the round-1 shared-row-pick pattern, on `grammar_rule_table`). It was surfaced by the round-2/3 catalog-survival inserts — `grammar_rule_table.language` DEFAULTs to `'de'`, so those transient `_testrule_` rows land in the `language='de'` pool; an unscoped pick could grab one that the inserting test then deletes, and `/srs/due`'s JOIN drops the card. Fix: `ORDER BY rule_id LIMIT 1` (lowest id = a seeded rule, never a transient high-id one). One-line, in `test_grammar_rules_srs.py`. Validation: grammar + catalog-inserting tests `-n auto` × 3 → 42 passed each. (This corrects the round-3 "no round 4 known" note.)

**Audit hint for a future round 5:** the failure mode is the *pattern*, not the table — `… FROM <shared catalog> LIMIT 1` with no `ORDER BY`. Remaining such reads (`_get_phrase` / `_get_grammar_rule_id` on `phrase_table`/`grammar_rule_table`; `video`/`sentence` `LIMIT 1` in `test_recommendations`/`test_transcript_click`) are stable **today** because no test inserts+deletes a row matching their WHERE — but flip any of them to a deterministic pick the moment a test starts mutating those tables.

🆕 **2026-05-24 — #39 slice 3A: lemma correction candidates**

Backend signal inbox for bad lemmas (no frontend / LLM / promotion). See `docs/LEMMA_OVERRIDE_WORKFLOW.md`.

| File | Change |
|---|---|
| migration `034_lemma_correction_candidate.py` | Signal table; `''`-absent optionals; partial-unique pending dedup on `(language, surface_form, observed_lemma, suggested_lemma, context_text)` (cross-user); `user_id` `ON DELETE SET NULL`. |
| `services/lemma_correction_service.py`, `routers/lemma_corrections.py` | `POST /lemma-corrections` (auth + 30/hr per-user throttle, create-or-bump) + `GET /admin/lemma-corrections` (`require_admin`, read-only). Never writes `lemma_override`. |
| `models/schemas.py` | `LemmaCorrectionCreate` (caps, trims, item_type allowlist; `model_validator` normalizes missing/blank optionals → '' — a field_validator skips defaults). `LemmaCorrectionRead`. |
| `tests/test_lemma_corrections.py` | +17: auth gate, create + defaults, validation matrix, cross-user dedup (`report_count`), separate-on-different-suggestion, **never-mutates-`lemma_override`** guard, admin-only list, per-user throttle. Per-worker-unique `surface_form` (dedup key is global). |

**Validation:** `test_lemma_corrections.py` 17 passed; full backend suite `-n auto` → 721 passed, 2 skipped (after the round-4 grammar fix).

🆕 **2026-05-24 — #39 slice 3B: admin accept/reject (promote to `lemma_override`)**

The human-gated signal→authority path. Backend only; see `docs/LEMMA_OVERRIDE_WORKFLOW.md`.

| File | Change |
|---|---|
| `services/lemma_correction_service.py` | `accept_candidate` (transactional: `FOR UPDATE` → pending check → choose corrected lemma → upsert `lemma_override` via `ON CONFLICT … DO UPDATE` → flip `accepted`) + `reject_candidate`; domain exceptions `CandidateNotFound`/`CandidateNotPending`/`NothingToPromote`; `REVIEWED_OVERRIDE_SOURCE` constant. |
| `routers/lemma_corrections.py`, `models/schemas.py` | `POST /admin/lemma-corrections/{id}/{accept,reject}` (`require_admin`), exceptions mapped → 404/409/400. `LemmaCorrectionAccept`/`Reject`/`LemmaOverrideRead`/`ReviewResult`. |
| `tests/test_lemma_corrections.py` | +12: accept-creates-override, corrected-overrides-suggestion, **existing-override-updates-not-duplicates** (the upsert proof), review metadata, atomicity (both applied), reject-no-override, no-reopen (409 ×2), non-admin/unauth 403, nothing-to-promote 400, not-found 404. **Updated** 3A's `test_post_never_mutates_lemma_override` → scoped to a unique `observed_lemma` (3B writes `lemma_override` concurrently under -n auto). Per-worker-unique `observed_lemma` + override-row cleanup. |

**Validation:** `test_lemma_corrections.py` 29 passed (17 + 12); **full backend `-n auto` × 2 → 733 passed, 2 skipped, 0 failed each.**

🆕 **2026-05-24 — #39 slice 3C: dry-run LLM adjudication (advisory)**

Proposal-only adjudication; no live LLM calls, no persistence, no mutation. Backend only.

| File | Change |
|---|---|
| `services/lemma_correction_service.py` | `adjudicate_candidate_dry_run` (read-only: load pending candidate → `await adjudicator(input)` → validate via schema → return; raises `CandidateNotFound`/`CandidateNotPending`/`AdjudicatorError`); `build_adjudication_input`, `ADJUDICATION_INSTRUCTION`, `format_adjudication_prompt`. |
| `routers/lemma_corrections.py`, `models/schemas.py` | `POST /admin/lemma-corrections/{id}/adjudicate` (`require_admin` + injected `get_lemma_adjudicator` dep → `None`/503 in 3C); `LemmaCorrectionAdjudication` schema; errors → 404/409/502/503. |
| `tests/test_lemma_corrections.py` | +12: service-level with a fake adjudicator (proposal returned, **no override mutation**, **no candidate change**, non-pending, not-found, invalid-output → `AdjudicatorError`), prompt builder (fields + the don't-trust-blindly warning), endpoint auth (non-admin 403 / unauth) + 503-no-adjudicator + a happy-path via `dependency_overrides[get_lemma_adjudicator]`. |

**Validation:** `test_lemma_corrections.py` 41 passed (29 + 12); **full backend `-n auto` → 745 passed, 2 skipped, 0 failed.**

🆕 **2026-05-25 — #18: scraper language config centralised**

`subtitle-scraper/language_config.py` is the single source for the per-language maps that were hardcoded in `pipeline.py`. Scraper-only; no behaviour change.

| File | Change |
|---|---|
| `subtitle-scraper/language_config.py` (new) | `LANGUAGES` dict + helpers + validation + derived `MODEL_MAP`/`TRANSCRIPT_CODES`/`NO_MORPH_LANGS`. Plain Python (no YAML dep). |
| `subtitle-scraper/pipeline.py` | 3 dict literals replaced by `from language_config import … as LANG_*` (re-export; 11 call sites unchanged). `POS_LIST` stays. |
| `subtitle-scraper/profile_pipeline.py` | local `NO_MORPH_LANGS` duplicate now imported from `pipeline` (← config). |
| `tests/test_language_config.py` (new) | +9: values + `de_core_news_md` asymmetry + **order invariant** + helper semantics + unknown-lang-matches-old + malformed-config `ValueError` + pipeline-no-longer-hardcodes (source check). |

**Validation:** `test_language_config.py` 9 passed; scraper suites (`test_scraper_channels`/`test_scraper_es_path`/`test_phrase_dispatcher`) 28 passed; full root suite **671 passed**; backend unaffected (imports none of the changed files).

🆕 **2026-05-24 — Spanish phrase extractor slice 2 (#36)**

Added a third pattern family (clitic-attached reflexive infinitives) and broadened the verb+prep allowlist. Scraper-only (`phrase_finder.py`); German untouched.

| File | Change |
|---|---|
| `subtitle-scraper/phrase_finder.py` | New block 3 in `extract_spanish_logic`: clitic infinitives (`quiero lavarme`→`lavarse`) via VerbForm=Inf + longest-first clitic strip + ends-in-`r` check + override on the recovered base. `_es_prep_candidates` widened to `mark` deps. `_ES_VERB_PREP` +6 pairs (`confiar en`, `consistir en`, `creer en`, `jugar a`, `salir de`, `llegar a`). |
| `tests/test_spanish_phrase_extractor.py` | +13: 4 clitic infinitives (lavarse/levantarse/ducharse/acostarse), non-reflexive-infinitive negative, 6 verb+prep, 2 deferred-pattern guards (imperative→[], reflexive+prep emits only `acordarse` not `acordar de`). |

**Spanish spaCy (`es_core_news_sm`) limits hit (deferred, documented):** imperatives (`Lávate.`) and some 1sg forms (`llamarme`, `Salgo`, `Llego`) are mis-tagged as non-verbs, so those specific cases don't extract — verb+prep pairs use sentences the model tags correctly. Reflexive+prep combos (`acordarse de`) deferred to a separate set (shipped in slice 3 below).

🆕 **2026-05-24 — Spanish phrase extractor slice 3 (#36)**

Reflexive+preposition combos for finite forms. Scraper-only (`phrase_finder.py`); German untouched.

| File | Change |
|---|---|
| `subtitle-scraper/phrase_finder.py` | New `_ES_REFLEXIVE_PREP` set (separate from `_ES_VERB_PREP`); block 1 restructured so a finite verb with an agreeing reflexive clitic + a matching prep candidate emits `f"{lemma}se {prep}"` (`es_reflexive_prep`) and suppresses the bare reflexive for that token. |
| `tests/test_spanish_phrase_extractor.py` | +7 net: 5-verb combo matrix (`acordarse/enamorarse/quejarse de`, `preocuparse por`, `olvidarse de`), per-token-suppression guard (`Se queja constantemente.`→`quejarse`, not `quejarse de`), `es_reflexive_prep` match_type lock; the pre-existing `test_reflexive_prep_combo_emits_reflexive_only` renamed/rewritten to `..._emits_combo` (now asserts `acordarse de` present, bare `acordarse` suppressed, `acordar de` never emitted). |

All 5 verb lemmas verified correct against `es_core_news_sm` (no #39 override needed). Per-token suppression only — the bare reflexive still surfaces from prep-less occurrences elsewhere.

**Validation:** root Spanish/dispatcher/es-path → 50 passed · backend `test_matcher.py` → 16 passed · full root suite **630 passed** · full backend suite green (regression).

🆕 **2026-05-24 — Spanish phrase extractor slice 4 (#36)**

Reflexive+preposition combos on a clitic-attached infinitive ("quiero acordarme de…"). Scraper-only (`phrase_finder.py`); German untouched.

| File | Change |
|---|---|
| `subtitle-scraper/phrase_finder.py` | Block 3 (clitic-infinitive) now also checks `_es_prep_candidates(token)` against `_ES_REFLEXIVE_PREP`: emits `f"{base}se {prep}"` (`es_reflexive_prep`) and suppresses the bare reflexive when a combo matches (same rule as the finite block 1a). The infinitival "a" (`voy a enamorarme…`) is filtered for free — only `(base, prep)` pairs in the set emit. |
| `tests/test_spanish_phrase_extractor.py` | +9: 5-verb infinitive-combo matrix (`Quiero acordarme de ti.`→`acordarse de`, etc.), bare-fallback when no prep (`Quiero acordarme.`→`acordarse`), non-allowlisted-prep negative (`…acordarme con ella.`→ no combo, bare only), no-duplicate-bare guard. |

Probe-confirmed against `es_core_news_sm`: the preposition attaches to the fused infinitive token as a grand-ADP (`case`), so `_es_prep_candidates` finds it — no model switch or override needed.

**Validation:** root Spanish + dispatcher → 61 passed; backend `test_matcher.py` → 19 passed (German regression); full root suite **662 passed**.

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
| **`test_content_requests.py`** | 8 + 🆕 12 | POST creates pending row, duplicate returns existing, failed→pending on resubmit, GET lists newest-first, user isolation, auth gate. 🆕 **S7** content_id validation: video/channel bare-id + `youtube.com`/`youtu.be`/`shorts`/`channel` URL normalization to canonical id; rejects non-YouTube host, look-alike host, shell strings, path-traversal, overlong, empty, wrong-length, wrong-type (422); an invalid request creates no row and spawns nothing. Subprocess spawn patched out via `monkeypatch`. | Audit Hole 30 / TODO #4 / S7 |
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
6. **Run `ruff check .` from the repo root** before calling a Python change done —
   it is part of backend validation, not a separate cleanup task. See the
   Validation commands section below.

---

## Validation commands — the full set for a Python change

Run all three. The lint step is not optional.

| Step | Command | Expected |
|---|---|---|
| Lint | `ruff check .` *(repo root)* | `All checks passed!` |
| Backend | `cd lexy-app/backend && pytest -n auto` | 760 passed, 2 skipped |
| Root pipeline | `pytest tests/` *(repo root)* | 211 passed |

Backend was 745 until the ILP playlist optimizer landed (+15: 11 unit tests for
`ilp_cover`, 4 endpoint tests for the `algorithm` parameter — accepts `"ilp"`,
422s an unknown value, 503s when the solver is unavailable, and defaults to
greedy without invoking the solver).

The root suite is **211 = 97 + 114**: `tests/runtime/` (97) covers the root
pipeline modules, `tests/*.py` (114) covers `subtitle-scraper/`. It was 744
until 2026-07-27, when the `src/app/` refactor and the 537 tests that only
targeted it were deleted — see the retirement note below.

**When one of these fails, check [`docs/COMMON_ERRORS.md`](./COMMON_ERRORS.md)
before debugging.** It carries the failures that have actually happened here —
including two that mislead rather than simply fail: `pytest-randomly` erroring
every test at setup so no assertion runs, and a `cd` persisting between shell
calls so `pytest tests/` silently runs the *backend* suite from
`lexy-app/backend` and reports 745 instead of 207.

Frontend changes additionally want
`cd lexy-app/frontend && npx tsc --noEmit && npx vitest run && npm run build`
(248 tests across 41 files).

🆕 **2026-07-27 — `src/app/` retired; root suite 744 → 207**

Phase 1 ported the valuable behaviour off the orphan refactor onto the runtime
modules (`tests/runtime/`: **20 → 93 tests**, nine files), then Phase 2 deleted
`src/` (3,462 lines), the four test directories that only targeted it
(`tests/{subtitles,learning,exposure,pipeline}/`, 537 tests) and the root
`conftest.py` sys.path injection.

Coverage went *up* where it matters: before this, the shipping pipeline modules
had 20 tests while 537 tests exercised code with zero runtime callers.
`eligibility.py` — the i+1 rule the whole learning model rests on — had never
been tested in its shipping form until batch 4.

**Two accepted coverage losses, recorded rather than glossed:**
  - `pipeline_diagnostics.py` (290 lines) lost its only 39 tests and has **no
    runtime equivalent**. It emits profiling/timing tables with no product
    behaviour, so this was judged acceptable — but it remains the one runtime
    pipeline module with zero coverage. Still open.
  - ~~The 11 end-to-end `GermanSubtitlePipeline` smoke tests are gone, so a
    regression that only appears in stage *composition* would not be caught.~~
    **Closed 2026-07-27** by `tests/runtime/test_pipeline_smoke.py` — see below.
    The 11 refactor-shape tests were not restored; four composition tests
    replace them.

🆕 **2026-07-27 — runtime pipeline composition smoke test (+4, root 207 → 211)**

`tests/runtime/test_pipeline_smoke.py` covers the shipping
`pipeline.GermanSubtitlePipeline` wiring, which the per-stage files
structurally cannot:

    fragments → merge → segment → quality filter → extract → i+1 filter → I1Match

Four tests: construction wires all six stages (and `exposure_service.store is
pipeline.store` — if those diverged, recorded exposures would never affect i+1
decisions); the happy path yields one `I1Match` with the expected target; the
**negative** path yields none when three units are unknown (if that ever returns
a match, the i+1 filter has been bypassed in composition — the failure a
stage-level test can never see); and the write path pipeline → exposure service
→ store keyed by `utterance_id`, including dedup.

Uses `run_fragments`, not `run`, so there is **no file I/O, no network and no
external service** — fragments are built in memory. Only dependency is the
spaCy model, skipped-if-missing like the sibling runtime tests.

The i+1 scenario was probed against the real pipeline before being asserted:
`"Ich gehe heute ins Kino."` extracts `{gehen, heute, kino}`, so seeding
`gehen` + `heute` leaves `kino` as the sole unknown. The seed is derived from
observed behaviour, not guessed — and unseeded the same sentence really does
yield zero matches, which is what makes the negative test meaningful.

Deliberately small: stage internals stay in the per-stage files. No production
code was changed; the smoke test exposed no runtime bug.

`conftest.py` was deleted outright, not rewritten. Runtime tests import root
modules directly (`from subtitle_cleaner import …`) and the repo root reaches
`sys.path` via the `tests/__init__.py` + `tests/runtime/__init__.py` package
chain, which makes pytest's basedir the repo root. That was an inference about
pytest's import mode, so it was verified by removing the file and re-running
(207 either way) rather than by reasoning.

**Ruff baseline (2026-07-27): zero findings**, down from 153.

The check is reproducible: the rule set lives in `/ruff.toml`
(`select = ["E", "F"]`, `ignore = ["E501"]`, `line-length = 100`,
`target-version = "py311"`) and ruff is pinned to `0.14.1` in
`lexy-app/backend/requirements.txt` — the one pinned dependency in that file,
because ruff's default selections change between releases and an unpinned ruff
reports differently machine-to-machine.

The selected set is *broader* than ruff's built-in default (E4/E7/E9 + F): full
`E` adds E1/E2/E3, all already clean. Two rule families are off on purpose:
  - **E501 line-too-long** — enabling it reports 405 violations across existing
    files. That's a repo-wide reflow, not a lint fix. `line-length = 100`
    therefore doesn't affect `ruff check` today; it's there so a future
    `ruff format` has an agreed width.
  - **`I` import sorting** — would reorder imports repo-wide, and several
    `subtitle-scraper/` modules depend on import *order* (sibling imports after
    a `sys.path.insert`, TODO #3). Needs those sites checked by hand, not a
    blanket `--fix`.

What the 153 were, and the two traps in re-fixing them:

| Rule | Count | Notes |
|---|---|---|
| F401 unused-import | 72 | 71 auto-fixed. The 72nd is `masking/latest_ingest.py`'s `visualize_docling_full` import — **the import IS an availability probe**, so it keeps a `noqa`. `importlib.util.find_spec` is not a substitute: it proves the module resolves, not that the symbol exists. |
| E402 import-not-at-top | 41 | 27 were a genuine bug — a stray `logger = logging.getLogger(__name__)` sat above the import block in `main.py` (21), `subtitle-scraper/pipeline.py` and `transcript_fetcher.py`. The other 14 are the **deliberate `sys.path.insert`-then-import pattern** (TODO #3) and carry `noqa` + a "do not hoist" comment. Hoisting them breaks the scraper. |
| F541 f-string w/o placeholder | 27 | auto-fixed |
| F841 unused local | 7 | `_`-prefixed where the value documents a tuple shape or the expression still asserts something; deleted where dead |
| E702 semicolon statements | 3 | split onto separate lines |
| E741 ambiguous name `l` | 3 | renamed to `line` / `level` |

Note `compileall` is **not** sufficient to validate an F401 sweep — it checks
syntax, not import resolution. The scraper modules that ruff edited
(`pipeline.py`, `phrase_finder.py`, `transcript_fetcher.py`) are imported by no
test, so they were smoke-tested directly with `python -c "import <module>"`.
