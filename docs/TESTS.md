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

(Those counts are the suite sizes **as measured during that investigation** —
745 backend / 671 root. Both have since moved; see the current baselines at the
bottom of this file. They are kept as-recorded because the point of the table is
the with/without ratio, not the absolute numbers.)

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
| `test_migration_025_downgrade.py` | 🆕 db-02 downgrade guard for migration 025: `_tokenize` id instability, refuse-before-DDL when `reading_selections` anchors exist, proceed at zero dependents, the counting SQL against the live schema (token_id counts, legacy/null/empty don't), and a shape matrix proving the predicate never *errors* on non-array `anchors` (JSON null / object / scalar / SQL NULL). **Never runs `alembic downgrade`** — see the caveat below the table. |
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
| `test_content_requests.py` | `/content-requests` POST/GET: pending-row creation, per-user idempotency + failed→pending reset, per-user uniqueness (migration 029), notification routing, S7 `content_id` validation/normalization, auth. 🆕 arch-05: a `content_request_service` section drives the extracted SQL directly — `create_or_reset` (insert, idempotent, resets `failed`, leaves `done`), `list_for_user` (newest-first + user-scoped), `count_pending`. `user_id` is passed as a **str** to match `get_current_user`'s normalization; `count_pending` asserts a `>= 1` lower bound, not an exact count, because the query is global and the suite runs `-n auto`. New rows use `_channel_id()`/`_video_id()` so the per-worker cleanup fixture reaps them |
| `test_notifications.py` | SSE stream (#4a): per-row mark-after-yield ordering, disconnect leaves rows unseen, heartbeat on empty, string-JSONB payload parsing, `request_failed` write + delivery. 🆕 arch-05: a `notification_service` section covers `fetch_unseen` (oldest-first, excludes seen, user-scoped) and `mark_seen` (flips only its row). Ordering assertions stay on `_yield_unseen` — the sequencing is deliberately router-side |

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

**Current count (2026-07-27): 261 tests across 43 files.** Was 248/41 before the
playlist optimizer selector, 238/40 before the
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
- Vocabulary lists: `WordListsPage` + `tests/test_word_lists.py` (2026-07-27).
- LLM provider seam + OpenAI-compatible backend: `tests/test_llm_provider.py` (2026-07-27, 61 tests).

🆕 **2026-07-27 — German catalog backfill (+36 backend / +1 file)**

`scripts/backfill_word_catalog.py` + `services/word_seed_service.py` seed
missing German words from `data/final_result.txt` column 0, so vocabulary
lists resolve its headwords instead of reporting ~half unresolved. Thin CLI
over a testable service, matching the `cleanup_orphan_srs_cards.py` /
`srs_cleanup_service.py` split. No migration.

| File | Tests | Covers |
|---|---|---|
| `tests/test_word_seed.py` (NEW) | 36 | article stripping incl. casing and the *derartig*/*dasselbe* false-positive guard; clean-candidate accept/reject; **skip reasons split** into multi-word vs multi-entry-cell; case-insensitive dedup with first-spelling-wins; file order preserved; **column 1 blueprints never become candidates**; dry-run writes nothing; apply inserts with `pos=''`/`tag=''`/`lemma==word`/`frequency=0`; idempotence; **existing word not re-inserted case-insensitively**; article nouns seeded bare while the article form stays out of `word_table`; unclean entries never inserted; language scoping; and an end-to-end regression that a seeded word flips a list entry from `unresolved` to `unknown` |

**The load-bearing test is `test_existing_word_is_not_reinserted_case_insensitively`.**
`word_table` has `UNIQUE (word, language, pos)` with `pos` *in the key*, and
every existing German row carries `pos = ''`. An insert differing only in case
or POS does **not** conflict — it creates a second row, and two rows for one
surface is exactly what `word_list_service` reports as `ambiguous`. A careless
backfill would have turned thousands of currently-resolvable words ambiguous
while every existing test stayed green.

**A fixture-naming trap worth remembering.** The first run had all 7 DB tests
failing with `missing == 0`. Cause: fixtures used the repo's usual
`_testword_…` prefix, but `is_clean_candidate` rejects a leading underscore by
design — so no fixture ever became a candidate. Had the assertions been
weaker (`>= 0` rather than `== 1`), the tests would have *passed* while
testing nothing. Fixtures now use a letter-initial `Zzseed…` prefix.

**Validation:** backend `-n auto` → **894 passed, 2 skipped** (858 + 36);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.


🆕 **2026-07-27 — Unicode case-insensitive catalog resolution (+37 backend / +1 file)**

Fixes the C-collation bug: this database is `datcollate=C datctype=C`, so
Postgres's `lower()` and `ILIKE` fold **ASCII only**. The resolver compared
Python-lowered keys against SQL `lower(col)`, so the two sides computed
different keys for anything non-ASCII — 50 German `word_table` rows and 18
`phrase_table` canonicals were unreachable and reported `unresolved` even for
a byte-exact paste. Folding now
happens in Python (`services/text_norm.py`).

| File | Tests | Covers |
|---|---|---|
| `tests/test_text_norm.py` (NEW) | 15 | case variants share a key (Ö/Ü/Ä, single- and multi-token, ASCII, surrounding whitespace); NFC so decomposed `o`+U+0308 matches precomposed `ö`; **ß and ss are different keys** (parametrized over the three real pairs) while ß stays case-insensitive with itself; `index_by_key` groups case variants, sorts ids, and dedupes a repeated id |
| `tests/test_word_lists.py` | +15 | umlaut word resolves **with its stored spelling** (the case that used to fail even byte-exact) and from lower/upper pastes, parametrized over Ö/Ü/Ä; a single umlaut row is `unknown`, **not** `ambiguous`; genuine umlaut duplicates are still `ambiguous`; **multi-token umlaut phrase** resolves from lower/upper/title casing; umlaut surfaces dedupe case-insensitively; **ß and ss resolve to different `word_id`s**; the five backfill sample words (`heißen`, `gelten`, `beginnen`, `entsprechen`, `sitzen`) still resolve |
| `tests/test_word_seed.py` | +7 | an existing `Öl` is seen as present when the candidate is `öl`; dry-run `missing` excludes existing umlaut case-variants; a case variant is **not** inserted as a second row (the anti-fork guard, for the case it used to miss); a genuinely absent umlaut word is still seeded; ß/ss spellings seed as two separate words; **second and third `--apply` report `inserted 0`**; `inserted` counts only rows this call wrote (monkeypatched so `missing` names a row that already exists — the old count query returned 2) |

**Both new behaviours are mutation-checked.** Switching `normalize_key` to
`casefold()` fails the three ß tests; reducing it to an ASCII-only fold (what
the C-locale SQL side did) fails six umlaut tests. Neither mutation is caught
by any pre-existing test, which is why the umlaut fixtures carry a real umlaut
rather than reusing the ASCII `_testword_` helper default.

**Do not "simplify" the resolver back to a bounded query.** `_resolve_surfaces`
fetches the whole language-scoped catalog on purpose (worst case is Spanish
at 29,629 `word_table` rows, ~40 ms; German ~20 ms, flat in list size): no whole-string case transform turns a typed
`Die Änderung` into a stored `die Änderung`, so a variant-based lookup is
provably incomplete for multi-token surfaces. `lower(col COLLATE "und-x-icu")`
is the correct bounded alternative if this ever gets hot.

**Validation:** backend `-n auto` → **931 passed, 2 skipped** (894 + 37);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed. The backfill
dry-run now reports `missing: 0` (was 17, all umlaut-initial), confirming the
resolver and the seeder agree on what is already present.


🆕 **2026-07-28 — Unicode word lookup + learn-anyway de-duplication (+18 backend)**

Closes the **data-corrupting** half of the C-collation bug. `ILIKE` folds ASCII
only here (`'Öl' ILIKE 'öl'` → false), so `/words/by-text?word=öl` reported
`not_found` while `Öl` sat in `word_table`. The picker then offered "Learn
anyway", and its `pos='X'` INSERT with `ON CONFLICT (word, language, pos)`
cannot conflict with the same word stored as `pos=''` or `pos='NOUN'` — so
accepting **forked the surface into a second row**, which `word_list_service`
reports as `ambiguous` for every user, permanently. `word_service` now resolves
through `resolve_word_ids` (Python `normalize_key`) and reuses an existing row.

| File | Tests | Covers |
|---|---|---|
| `tests/test_words.py` | +18 | `/words/by-text` finds an umlaut word from lower- and upper-cased input, parametrized over Ö/Ü/Ä; a case-variant hit returns the **stored** spelling, not the typed one; `straße`/`strasse` and `schließen`/`schliessen` each resolve `single` to their own row (never merged); umlaut duplicates still report `ambiguous` with `item=None` and 2 candidates (W3 / Hole 2 intact); lookup stays language-scoped; **learn-anyway reuses an existing `pos=''` row**, an existing scraper `pos='NOUN'` row, and an existing row when the user submits a Unicode case variant (`Öl` stored, `öl` submitted) — asserting in each case that no second row appears; a genuinely new word still gets its sparse `pos='X'` row; learn-anyway on an already-ambiguous surface reuses the lowest `word_id` and adds **no third row**; learn-anyway stays language-scoped |

**Mutation-checked.** Reverting `services/word_service.py` to its pre-fix state
fails 9 of the 18 (the three lowercased-input params, stored-spelling-returned,
umlaut-duplicates-ambiguous, and all four learn-anyway reuse/no-fork cases).
The other 9 pass before and after by design — they are regression guards on
behaviour that must not change (byte-exact lookup, ß/ss separation, language
scoping, new-word creation).

**Note one test is a guard, not a reproduction.** Unlike vocabulary lists,
`/words/by-text` passed the raw input to `ILIKE`, so a byte-exact umlaut
spelling already worked. `test_by_text_finds_umlaut_word_with_stored_spelling`
pins that; the case-variant tests are the ones that were broken.

**Validation:** backend `-n auto` → **949 passed, 2 skipped** (931 + 18);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-28 — Unicode free-chat progression (+14 backend)**

`chat_service.match_learning_words` pushed case-folding into SQL —
`LOWER(wt.word) = ANY($2)` fed Python-lowered tokens. Postgres folds ASCII
only here, so producing an umlaut word in free chat matched nothing and earned
**no progression or SRS credit**, silently. The word half now inverts the join
(fetch the user's tracked non-known words, filter with `normalize_key`); the
phrase half was never affected, since it matches on spaCy-returned
`phrase_id`s.

| File | Tests | Covers |
|---|---|---|
| `tests/test_free_chat_progression.py` | +14 | tracked umlaut word matches a lowercase message, parametrized over Ö/Ü/Ä; also from the exact stored spelling and an uppercase message; **lemma** half folds too (lemma-only case variant matches); ASCII still matches; `straße`/`strasse` and `schließen`/`schliessen` do **not** cross-match, so no credit leaks to the wrong word; `known` items stay excluded; language scoping holds; untracked words are never returned (the inverted join must not widen scope); three different case *spellings* of one word collapse to a single match, so the fold cannot credit it twice (plain repeated-token dedup stays covered by the pre-existing ASCII test); and end-to-end, an umlaut word in a German message advances **both** passive and active levels |

**Mutation-checked.** Reverting `services/chat_service.py` fails 8 of the 14 —
all three lowercase params, exact-spelling, uppercase, lemma-variant,
case-variant-collapse, and the end-to-end progression test. Note the
case-variant-collapse test fails on old code because the word matched *zero*
times, not because dedup broke — dedup itself is unaffected by the fold. The other 6 pass either way by design:
they guard scope and status behaviour the fix must not widen (ASCII, ß/ss
separation, known-excluded, language scoping, untracked-excluded).

**Note the exact-spelling case was broken here too**, unlike `/words/by-text`.
Chat lowercases message tokens before querying, so even a byte-exact umlaut
surface missed — the failure was not limited to case variants.

**Fixture surfaces are letters-only** (`_letters()` maps uuid hex to a–p).
The tokenizer is `[^\W\d_]+`, so a digit or underscore makes a surface
untokenizable and every assertion would pass vacuously against a word that can
never match — the same trap already documented on `_get_word`.

**Validation:** backend `-n auto` → **963 passed, 2 skipped** (949 + 14);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-28 — Reading catalog binding + shared resolver (+21 backend / +1 file)**

Extracted `services/catalog_resolver.py` so vocabulary lists and interactive
reading resolve a surface identically. `find_catalog_item` had three bugs, all
closed: SQL-side folding (an umlaut selection never bound, so it never reached
the main SRS); matching `phrase_table.surface_form` where lists match
`canonical` (**773 German rows differ**); and a bare `fetchrow` that silently
first-matched an ambiguous surface. `get_word_statuses_for_page` is fixed in
the same pass — its keys were SQL-lowered, so an umlaut word was filed under
`'Öl'` while the frontend looks up `'öl'`.

| File | Tests | Covers |
|---|---|---|
| `tests/test_reading_progression.py` | +21 | `find_catalog_item` binds an umlaut word from a lowercased selection (Ö/Ü/Ä) and from the stored spelling; `straße`/`strasse` and `schließen`/`schliessen` bind to their own rows; **ambiguous surfaces bind to nothing** (two POS rows, and two Unicode case-variant rows); language scoping; phrases match **`canonical`** and a `surface_form` fragment no longer binds; umlaut phrase canonical binds; **reading and `catalog_resolver.resolve_one` return the same phrase row**; end-to-end, an umlaut selection creates the knowledge row and **both** SRS cards. Plus first-ever coverage of `get_word_statuses_for_page`: umlaut words appear (Ö/Ü/Ä), the key is the **JS-`toLowerCase()`-compatible** form and the stored spelling is *not* a key, ASCII still works, off-page words stay excluded (the Python filter must not widen scope), and language scoping holds. Also pins the sharpest edge of the fail-safe: **`mastered` on an ambiguous surface promotes neither candidate to `known`**, while the reading row still records the user's action |

**Mutation-checked.** Reverting `services/reading_service.py` fails 15 of the
21. The 5 that pass either way are guards: ß/ss separation (×2), language
scoping (×2), and the ASCII page-status path.

**Two existing tests were changed, both deliberately.**
`_get_word` now picks an **unambiguous** word — its old lowest-`word_id` pick
was `das`, which has two German rows, so 8 binding tests were passing only
because the resolver used to guess. Uniqueness is checked with `normalize_key`,
not SQL `lower()`, so it agrees with the resolver. And
`test_save_selection_with_phrase_match_marks_learning` now selects on
`canonical` with `ORDER BY phrase_id` (it used `surface_form` and a bare
`LIMIT 1` — the unordered-pick flake documented above).

**Behaviour change worth knowing:** ambiguity fails safe, and 410 German word
surfaces have duplicate rows. Reading selections of those no longer propagate
to the catalog — including `mastered`, where a user *explicitly declares*
mastery and now gets no catalog effect. That is the intended direction: the
old behaviour promoted a coin-flip row to `known`, and auto-promotion is
one-way, so a wrong `known` is unrecoverable. They are still saved and reviewable on the reading schedule —
the trade is losing a coin-flip binding rather than gaining a wrong one, and
`word_list_service` has reported those same surfaces as `ambiguous` since it
shipped.

**Validation:** backend `-n auto` → **984 passed, 2 skipped** (963 + 21);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-28 — Unicode corpus search (+20 backend / +1 file)**

`search_service.search` folded case in SQL on both halves — `w.word ILIKE $1 OR
w.lemma ILIKE $1` for words, and an `ILIKE`/word-boundary-regex pair against
`phrase_blueprint`. Postgres folds ASCII only here, so searching *öl* returned
nothing though `Öl` was indexed against real sentences. Both halves now resolve
to ids in Python via `normalize_key`; the SQL matches `= ANY(...)`.

| File | Tests | Covers |
|---|---|---|
| `tests/test_search_unicode.py` (NEW) | 20 | umlaut word found from lowercase (Ö/Ü/Ä), exact and uppercase queries; ASCII regression guard; **`match_type` still distinguishes a surface hit from a lemma-only hit**, and the lemma half folds too; `straße`/`strasse` and `schließen`/`schliessen` return their own sentence and not each other's; language scoping (on the *video's* language, as before) and the no-language case; several videos sharing one normalized surface all surface (resolving to ids must not collapse them); unknown word returns nothing; umlaut **blueprint** token matches; single-word blueprint search still requires a whole word (*ist* must not match inside *Tadschikistan*); multi-word search keeps substring behaviour; **regex metacharacters are escaped** (`.*`, `.+`, `(`, `[a-z]+`, `Ö.*geben` do not match, with a literal-token control); route response shape unchanged; and **two tests pinning that `_suggest_words` is still the unfixed version** |

**Mutation-checked.** Reverting `services/search_service.py` fails 10 of the
20. The 10 that pass either way are guards — notably
`test_search_finds_umlaut_word_from_exact_query`, because this endpoint passed
the raw query to `ILIKE`, so a byte-exact umlaut spelling already worked. Only
case variants were broken.

**The deferral is test-pinned, not just documented.**
`test_suggest_words_is_still_the_unfixed_sql_version` asserts autocomplete
returns nothing for a lowercase umlaut prefix. When autocomplete is fixed that
test **should** fail — invert the assertion and close the item in
`docs/TODO.md` rather than deleting it.

**A test assumption that was wrong and got corrected.** The escaping test first
asserted `search(db_pool, "(") == []`. It failed — the real corpus has
punctuation tokens in `word_table`, so `(` has legitimate word hits that say
nothing about regex handling. It now asserts against `_resolve_blueprint_ids`
directly.

**Fixture note:** `video.category` has an FK to `video_category`, so fixtures
insert `'other'` rather than `''`.

**Seven `print(f"[search] …")` debug statements were removed** from the
single-word branch in the same change. They fired on every production search
and buried this suite's output in per-row dumps. They were a pure side-effect
block — nothing downstream read them — so search behaviour is unchanged.

**Security:** this closed **S19** in `docs/SECURITY.md` — the old word-boundary
blueprint predicate concatenated the raw search term into a Postgres regex.

**Validation:** backend `-n auto` → **1004 passed, 2 skipped** (984 + 20);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-28 — Unicode autocomplete, migration 036 (+29 backend / +1 migration)**

The last C-collation Unicode site. `/api/suggest` could not use the
fold-in-Python approach the rest of the app uses — it fires per keystroke and
needs an **indexed prefix** — so migration 036 persists the normalization as
generated columns `word_table.word_norm` and `phrase_blueprint.lookup_key_norm`
(`lower(btrim(normalize(col, NFC)) COLLATE "und-x-icu")`), plus
`ix_word_table_lang_word_norm (language, word_norm text_pattern_ops)`.

| File | Tests | Covers |
|---|---|---|
| `tests/test_search_unicode.py` | +29 | `_suggest_words` finds an umlaut word from lower/exact/upper prefixes and from umlaut-*initial* prefixes (Ö/Ü/Ä); language scoping; `limit`; **frequency ordering**; **case variants collapse to one suggestion, highest-frequency spelling winning**; `straße`/`strasse` and `schließen`/`schliessen` stay separate; `%` and `_` treated literally *and* a word containing a literal `_` still found; blank query. The generated column itself: 036 populated **every** existing row; `word_norm` matches `normalize_key` on real non-ASCII rows; a new insert gets it **without any application code setting it**; writing it directly raises. `_suggest_phrases`: regex metacharacters (`.*`, `.+`, `(`, `[a-z]+`, `\`, `Ö.*geben`) treated literally with a literal-token control; umlaut blueprint found from a lowercase query; blank query |

**Mutation-checked.** Reverting `services/search_service.py` (leaving the
migration applied) fails **14 of the 29**.

**The deferral test was inverted, not deleted.**
`test_suggest_words_is_still_the_unfixed_sql_version` became
`test_suggest_words_finds_umlaut_word_from_lowercase_prefix`, as its own
docstring had instructed.

**EXPLAIN (after `ANALYZE word_table`):**
```
Index Scan using ix_word_table_lang_word_norm on word_table
  Index Cond: ((language = 'de') AND (word_norm ~>=~ 'öl') AND (word_norm ~<~ 'öm'))
  Filter: (word_norm ~~ 'öl%')
Execution Time: 0.072 ms   Buffers: shared hit=3 read=2
```

**Three bugs the tests found that reading alone did not.**
1. Postgres's `\m`/`\M` word boundaries are **ctype-dependent**: under the C
   locale `ö` is not a word character, so `'jdm öl geben' ~ ('\m' || 'öl' || '\M')`
   is **false** while the ASCII equivalent is true. Phrase autocomplete was
   broken for umlauts independently of the case fold. The boundary check moved
   to Python (`\w` is Unicode-aware there), which also removed the last
   user-controlled pattern from that SQL.
2. A patch script silently ate a backslash, shipping `ESCAPE ''` instead of
   `ESCAPE '\'`. Direct SQL worked while the service returned nothing —
   the test caught it. (Same class as the COMMON_ERRORS entry on scripted
   multi-step edits.)
3. **A test assumption was wrong, again, and got corrected rather than
   weakened.** `_suggest_words("%")` was asserted to return `[]`; the real
   corpus tokenizes punctuation, so `%` is itself a `word_table` row
   (frequency 5) and legitimately matches *itself*. It now asserts the typed
   character does not expand — an unrelated high-frequency word is absent and
   every hit literally starts with it. Same shape as the earlier `(` case; when
   a corpus-backed assertion fails, check the corpus before the code.

**Security:** closed **S20** and corrected **S19**'s scope in
`docs/SECURITY.md`. Both findings' verification greps were run and pass.

**Validation:** backend `-n auto` → **1033 passed, 2 skipped** (1004 + 29);
`ruff check .` → All checks passed; `alembic upgrade head` → **036**. Frontend
not run — no frontend files changed. Root pipeline suite not run — no root
files changed.

🆕 **2026-07-27 — word-list phrase support (+11 backend / +4 frontend)**

Vocabulary lists now resolve against **both** `word_table` and `phrase_table`,
so a pasted blueprint (*jdm. (Dat) etw. (Akk) sagen*) binds to the same phrase
row the chat matcher and SRS already use. No migration: `item_type` already
existed and `apply_progression` was already polymorphic.

| File | Tests | Covers |
|---|---|---|
| `tests/test_word_lists.py` | +11 | phrase surface resolves from `phrase_table`; blueprint resolves as `item_type="phrase"` while its bare verb stays a word; **a single-token surface never binds to a multi-token phrase** even when a phrase canonical ends with that word; `das Haus` (phrase) and `Haus` (word) stay two entries; ambiguous phrase reported, not first-match resolved; unmatched multi-token surface is `unresolved` *as a phrase*; `create_list` persists the resolved `item_type`; mark-unknown-learning progresses phrases through `progression_service` and writes `item_type='phrase'`; unresolved/ambiguous phrases skipped; export keeps phrase surfaces in insertion order; counts include phrases |
| `components/WordListsPage.test.tsx` | +4 | type badge renders `word` / `phrase` on resolved entries; badge omitted on unresolved/ambiguous (no catalog row to describe); phrases render alongside words; mark-as-learning works on a mixed list |

**Precedence is decided by surface shape, not by which table answers first:**
multi-token → phrase first, word as fallback; single-token → word first,
phrase as fallback. Within the preferred type, several matches mean
`ambiguous` and the fallback is *not* consulted — a surface that is genuinely
ambiguous as a word must not quietly become a phrase.

**Phrase ambiguity is currently unreachable in real data** —
`UNIQUE (canonical, language)` is exact and there are zero case-collisions in
the seeded German rows. The test reaches it synthetically by inserting two
canonicals differing only by case, so the branch is covered without claiming
it occurs today.

**One non-obvious fix:** the late-binding UPDATE in `mark_unknown_as_learning`
now writes `item_type` alongside `item_id`. A multi-token surface stored as
`phrase` can later resolve via the *fallback* to a word, and persisting the id
without the type would have left a row whose join points at the wrong catalog.

🆕 **2026-07-27 — OpenAI-compatible provider (+30 tests, same file)**

Second backend behind the seam: `OpenAICompatibleProvider` POSTs
`{LLM_BASE_URL}/chat/completions`, so the backend can target a self-hosted model
server. **Anthropic stays the default** — with no new env vars the new class is
never constructed. No Gemma/Ollama/llama.cpp-specific code, no new dependency
(`httpx` was already in `requirements.txt`; the installed-but-undeclared
`openai` package was deliberately not used).

| Area | Tests | Covers |
|---|---|---|
| Provider selection | 8 | unset env → Anthropic (the back-compat promise); `LLM_PROVIDER=anthropic` incl. case/whitespace variants; `openai_compatible` → the new class; missing `LLM_BASE_URL` → clear error; missing `LLM_MODEL` → clear error; unknown value names both valid options; non-numeric `LLM_TIMEOUT_SECONDS` → clear error |
| Request shape | 8 | URL is `{base}/chat/completions`; **four remote base-URL forms incl. a Tailscale name, a 100.x address and a trailing slash** — the no-localhost-assumption is parametrized, not asserted once; model + `max_tokens`; system message first, then caller messages in order; `response_format.json_schema` with `title`/`description` lifted out of `schema`; Bearer header; placeholder key when unset; timeout reaches the client |
| Response handling | 8 | success → parsed dict; malformed JSON retries once then raises; **retry falls back to `json_object` and restates the schema in the prompt**; missing required field; non-object JSON; empty `choices`; HTTP 5xx raises *without* retrying; connection failure → `LLMProviderError` |
| Credential hygiene | 3 | API key absent from errors on both malformed-JSON and HTTP-error paths while model + host remain present; `user:pass@` userinfo redacted from the base URL; credential-free URLs untouched |

**Two deliberate design choices the tests pin:**

1. **`LLM_MODEL` is required for `openai_compatible`.** No default is meaningful
   across runtimes, and an empty/wrong model name surfaces as an opaque 404
   from the server rather than a config error.
2. **A 5xx is not retried.** The retry exists for *format* failures — a server
   that doesn't implement `json_schema` may still honour `json_object`. Re-asking
   a broken host with a different `response_format` just doubles the latency.

Misconfiguration fails at **import**, since `get_provider()` runs at module
import in all three services. That is intended: a backend that cannot reach its
model server should fail at boot, not on the first learner's message.

**Validation:** backend `-n auto` → **847 passed, 2 skipped** (817 + 30);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-28 — `llm_cache` test isolation (+7 backend / +1 file)**

`llm_cache` is GLOBAL with no user FK, so the autouse user cleanup could not
reach it and **no fixture ever did**. A dev database had accumulated **2,762
rows of pure test residue**. Two distinct problems, not one:

- **Residue** — `test_llm_cache.py` wrote 26 times per run and deleted nothing.
- **Collision** — the provider fakes in `test_llm_cache_migration.py` and
  `test_srs_gloss.py` reported the **production** `model_id`, so their output
  landed on the exact cache keys production uses. All 335 `item_gloss` rows
  were fakes' `{"gloss": "stub"}` — a test run could hand `"stub gloss"` to a
  real SRS card, and a curated gloss seed (TODO #43 step 2) would have
  collided with them.

**The tag lives in `model`, not `prompt_key`.** `model` is the one field every
write path controls — direct `set_cached` calls *and* the fakes, whose
`model_id` feeds `make_cache_key`. Tagging prompt keys would miss the fakes,
which must keep real keys (`item_gloss`) for the code under test to behave
normally. **Cleanup must never key on `prompt_key`** — `item_gloss` will hold
real curated rows.

| File | Tests | Covers |
|---|---|---|
| `tests/_cache_helper.py` (NEW) | — | `test_model()` / `cleanup_pattern()`, mirroring `_email_helper`'s per-worker pattern |
| `tests/conftest.py` | — | autouse `cleanup` also reaps `llm_cache WHERE model LIKE <worker pattern>` |
| `tests/test_llm_cache.py` | +4 | rows are tagged with a non-production model (and never look like `claude*` or `curated*`); the pattern matches only this worker's rows; **a seeded `curated:*` row and a production row are both spared**; and an end-to-end proof the fixture reaps — a later test finds zero tagged rows from earlier ones |

**Parallel-safe.** The pattern embeds `PYTEST_XDIST_WORKER` (or `main`
serially), so a finishing worker cannot delete another worker's in-flight rows.
Verified on both paths: a full `-n auto` run and a serial run each left the
table at **exactly 2,762 rows**, with 0 `zztest%` rows remaining.

**One write path was missed on the first pass and caught by measurement, not
by reading.** `book_llm_service.repair_block` is stubbed at `_call_llm` — one
layer *above* the provider — so retagging the fake providers didn't reach it
and it kept writing `book_ocr_repair` rows under the production model id. The
before/after row count showed +1; the fix patches `_provider` with a
`_ModelIdOnly` stand-in used solely for the cache key. **Check the row delta,
not just that tests pass.**

**Validation:** backend `-n auto` → **1040 passed, 2 skipped** (1033 + 7);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-28 — Curated gloss-cache seeding (+43 backend / +2 files)**

`review_service.get_due_cards` asks `translate_item_gloss` for a gloss on every
non-grammar due card — an LLM call per never-glossed item, on the review hot
path. **3,622 curated glosses** are now pre-seeded from
`data/words_4000_old.txt` under the sentinel model `curated:words_4000_old`,
which `translate_item_gloss` checks *before* the model-specific key.

| File | Tests | Covers |
|---|---|---|
| `tests/test_gloss_seed.py` (NEW) | 43 | parser reads columns 1+3 and skips blank/malformed lines; article stripping incl. the *derartig* false positive; gloss-shape rules with the 4-word boundary asserted on both sides; multi-entry cells, multi-sense and long rows skipped; candidates keyed by `.lower()` with first-spelling-wins; **the seeder's key equals what `translate_item_gloss` computes** (parametrized over umlauts and `straße`) rather than being asserted in isolation; umlaut case variants share one key; the sentinel is neither a real model nor reapable by the test cleanup; dry-run writes nothing; apply inserts with `expires_at IS NULL`; apply is idempotent; **inserted count excludes pre-existing rows**; surfaces absent from the catalog are not seeded; **an ambiguous catalog surface IS seedable** (the key is text, not `item_id`); a curated gloss is served **without calling the provider** (an exploding fake proves it); **it survives a model switch**; an uncurated word still falls back to the model cache and computes exactly once; `MOCK_LLM` semantics unchanged; seeding never calls the provider |

**Two mistakes I made here, both caught by measurement rather than by a green
suite — worth reading before writing a fixture that touches a shared table.**

1. **A fixture wiped production data.** The first `curated_rows` fixture did a
   blanket `DELETE FROM llm_cache WHERE model = CURATED_MODEL`. Every test
   using it deleted **all 3,622 seeded rows**, not just its own writes. The
   suite passed; the row count afterwards was 0. It now registers keys
   explicitly (`curated_rows(word)`), the same shape as `tracked_words`, so a
   worker can only delete keys it computed itself.
2. **A test asserted a global count.** `count(*) WHERE model = CURATED_MODEL == 1`
   passed only while the table was empty and failed the moment real rows
   existed. Scoped to the test's own `cache_key`.

Both are the same lesson: **on a shared table, assert and delete by your own
keys, never by a class-wide predicate.**

**Verified end to end:** seed → full `-n auto` run → curated rows still 3,622,
total 3,622, zero `zztest-model-%` residue; a follow-up dry-run reports
`already cached 3622 / missing 0`. Live spot-check with an exploding provider:
`Haus`→`house`, `haus`→`house`, `Öl`→`oil`, `Übung`→`exercise, practice`,
`ich`→`I`, all without an LLM call.

**Validation:** backend `-n auto` → **1083 passed, 2 skipped** (1040 + 43);
`ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-28 — System word lists, phase 1 (+20 backend / migration 037)**

`word_lists.user_id` is nullable and `is_system` marks a shared built-in list.
The read filters widened to `(user_id = $2 OR is_system)` — which is exactly
where a private list could leak — so the pre-existing cross-user isolation
tests now run against a database that contains system rows.

| File | Tests | Covers |
|---|---|---|
| `tests/test_word_lists.py` | +20 | **Schema:** user lists default `is_system=false`; the CHECK rejects *both* hybrid states (system-with-owner, ownerless-private); system names are unique while duplicate **user** list names still work and a user may reuse a system list's name. **Reads:** owner reads their own; another user still gets 404 on detail *and* export; a private list never appears in another user's index; a system list is visible to every user including a brand-new one; system detail/export readable. **Writes:** a normal user cannot delete a system list (404, row survives); no public API can create one even when the client sends `is_system: true`; owners can still delete their own. **Mark-learning:** on a system list the shared `word_list_items` row is **not** mutated while progression still runs; only the acting user gains a knowledge row; two users marking the same list keep separate progress; the same list shows different counts per user; and user-list late binding still persists as before |

**Both guards are mutation-checked.** Reverting the late-binding suppression
(`if not row["is_system"]` → `if True`) fails
`test_mark_learning_on_system_list_does_not_mutate_shared_rows`; widening
`list_lists` to all rows fails both index-isolation tests.

**Two tests had to change, and the distinction matters.**
`test_list_index_returns_only_own_lists` asserted `resp.json() == []`; the index
now legitimately includes system lists, so it asserts the property it was
really about — user A's list id is absent, and every row B sees is a system
row. And `test_brand_new_user_sees_system_lists` first asserted exact equality
with one id, which **passed serially and failed under `-n auto`**: `word_lists`
is global, so another worker's system fixture can be present concurrently.
Same shared-table trap as the `_get_word` and `%`-token cases above — on a
global table, assert your own rows, not the whole result set.

**Validation:** backend `-n auto` → **1103 passed, 2 skipped** (1083 + 20);
`ruff check .` → All checks passed; `alembic current` → **037 (head)**.
Frontend not run — no frontend files changed. Root pipeline suite not run — no
root files changed.

🆕 **2026-07-27 — LLM provider seam, step 1 (+31 tests / +1 file)**

Removed the three ad-hoc `AsyncAnthropic` constructions (`llm_service`,
`book_llm_service`, `reading_llm_service`) and routed all 13 call sites through
`services/llm_provider.py`. Anthropic behaviour preserved exactly — no local /
OpenAI-compatible adapter yet.

| File | Tests | Covers |
|---|---|---|
| `tests/test_llm_provider.py` (NEW) | 31 | `split_schema` (extract name/description, **strip both from the body**, missing-title → `LLMProviderError`); request shape (one tool, forced `tool_choice`, model/system/messages/max_tokens pass-through); tool-input extraction incl. a leading text block; missing/empty `tool_use` → `LLMProviderError` naming schema + model; `model_id` default, `LLM_MODEL` override, explicit-arg precedence; all three services expose `_provider` and **no** `_client`/`_MODEL`; the two read-only cache lookups key on `provider.model_id`; **14 byte-identity checks** rebuilding every tool dict from its schema and diffing against the pre-seam definition read out of git |

**The load-bearing assertion is byte-identity.** `title` and `description` are
carried as JSON Schema keywords (so the interface stays four arguments wide and
a future OpenAI-compatible adapter maps the same dict onto
`response_format.json_schema`), and `split_schema` strips them before building
`input_schema`. Leaving them in would be *accepted by the API without error*
while changing the prompt the model sees — exactly the silent regression
"preserve behaviour exactly" is meant to rule out. 11 constants + 2 factories ×
3 languages all reconstruct identically.

**Retargeted, not deleted:**

| File | Change |
|---|---|
| `tests/test_llm_cache_migration.py` | `_StubBlock`/`_StubResp` (fake Anthropic content blocks) → one `_FakeProvider` returning the dict directly; 5 monkeypatches move from `_client.messages.create` to `_provider`. `model_id` returns the real default so cache keys hash as in production. |
| `tests/test_srs_gloss.py` | 2 client stubs → `_CountingProvider` / `_ExplodingProvider`; 2 `llm_service._MODEL` reads → `_provider.model_id`. |
| `tests/test_chat_language.py` | `_make_eval_tool` / `_make_guided_hints_tool` → `_make_eval_schema` / `_make_guided_hints_schema`; assertions read top-level `["properties"]` instead of `["input_schema"]["properties"]`. |

**MockProvider deferred — wrong layer, not too big.** `MOCK_LLM` fakes are
computed from *call arguments* the provider never receives: `guided_evaluate`
needs `target_word in user_content`, `evaluate_production` substring-matches
`target_text` against `user_answer`, `translate_item_gloss` returns
`[gloss:{text}]`, `evaluate_and_reply` echoes the requested `language`. The
provider sees those only as prose inside the prompt, so a MockProvider would
have to regex them back out — coupling the mock to prompt wording and making it
strictly worse. `test_srs_produce.py`, `test_free_chat_progression.py` and
`test_srs_gloss.py` depend on those exact semantics. The 16 `_MOCK` branches
stay where they are.

**Validation:** backend `-n auto` → **817 passed, 2 skipped** (786 baseline
+ 31); `ruff check .` → All checks passed. Frontend not run — no frontend files
changed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-28 — System list seeding, phase 2 (+26 backend / +2 files)**

Two built-in lists seeded from `data/final_result.txt` — **9,122 items** —
using `catalog_resolver`, the same rule user lists and reading use.

| File | Tests | Covers |
|---|---|---|
| `tests/test_system_list_seed.py` (NEW) | 26 | parser splits the two columns, counts a tab-less line as malformed while treating a whitespace-only line as blank, and keeps a row with one cell filled; dedupe is case-insensitive first-spelling-wins, **folds umlauts stricter than the DB index**, preserves file order, drops blanks; list names/descriptions are stable (the name *is* the idempotency key); a `seed_system_lists` dry-run plans exactly the two production lists; dry-run writes nothing; apply creates a row with `user_id NULL` / `is_system true` / `language de`; apply is idempotent; an existing list is reused and only new surfaces appended; inserted counts exclude pre-existing rows; duplicate system names are impossible; original surfaces stored; words *and* phrases bind with the right `item_type`; **ambiguous and unresolved surfaces are kept with `item_id=NULL`, never dropped or first-matched**; counts add up to the surface total; seeding makes no LLM calls. Through the public API: a seeded list is visible to multiple users, cannot be deleted, `mark-unknown-learning` leaves the shared rows untouched while skipping ambiguous entries, two users stay independent, and user-created lists are unaffected |

**Behaviour tests call `seed_one_list` with a unique per-test name**, never
`seed_system_lists`. The latter uses the two fixed production names and seeding
is append-only, so a test running it against a fixture source would silently
add its surfaces to the real *Top German Words* list. Same shared-state trap as
the `curated_rows` fixture that wiped the gloss seed — assert and write against
your own rows, not a class-wide name.

**One dead branch removed rather than tested.** The parser had a
"both cells empty" case that is unreachable: a line with any non-whitespace
content must put it in some cell, and a whitespace-only line is already caught
as blank. The first version of the test asserted `skipped == 2` and failed,
which is what surfaced it.

**Seeder verified end to end:** dry-run (no writes) → apply (9,122 items) →
dry-run again (`items inserted 0`, `already present 4087 / 5035`) → apply again
(`inserted 0`). A full `-n auto` run left the seeded lists at exactly 2 lists /
9,122 items.

**Validation:** backend `-n auto` → **1129 passed, 2 skipped** (1103 + 26);
`ruff check .` → All checks passed; alembic unchanged at **037**. Frontend not
run — no frontend files changed. Root pipeline suite not run — no root files
changed.

🆕 **2026-07-29 — Built-in lists in the UI, phase 3 (+11 frontend)**

`WordListsPage` now splits the index into *Built-in lists* and *Your lists*.
**Frontend-only** — no backend file was touched.

| File | Tests | Covers |
|---|---|---|
| `src/components/WordListsPage.test.tsx` | +11 | system lists render in their own section and the user's do not appear there; a “Built-in” badge shows on a system list and not on a user list; **Delete is hidden for a system list while Open remains**; Delete still renders for a user list; **a list with no `is_system` field is treated as user-owned** (a pre-037 backend must not silently make lists read-only); an empty-state line under *Your lists* when only built-ins exist, and no built-in section at all when there are none; the display-name override renders without the stored name changing; the detail view shows the badge and read-only note; **export and mark-unknown-learning stay available and enabled on a system list**; and a user-list detail shows neither badge nor note |

**Mutation-checked.** Rendering every list under *Your lists*
(`myLists = lists`) fails 4 tests, including the one asserting Delete is absent.

**Hiding Delete is a courtesy, not the guarantee.** The backend already answers
404 for a system-list delete, because a system list has no owner and the
ownership filter can never match it. The UI change only avoids offering a
control that would produce an error.

**The stored list name was not changed.** *Top German Words* is 46%
phrase-typed (articled nouns bind to `phrase_table` collocations), so the UI
shows “Top German Words & Phrases” via `SYSTEM_DISPLAY_NAME` — display only,
because the backend name is the seeding idempotency key
(`ON CONFLICT (name) WHERE is_system`) and renaming it would fork the list on
the next seed.

**Validation:** frontend `npx vitest run` → **289 passed across 44 files**
(278 + 11); `npx tsc --noEmit` clean; `npm run build` succeeded.
`ruff check .` → All checks passed. Backend pytest not run — **no backend files
changed**. Root pipeline suite not run — no root files changed.

🆕 **2026-07-29 — Mass-marking guard (+8 backend / +7 frontend)**

Seeding built-in lists made `mark-unknown-learning` dangerous: each entry costs
its own `apply_progression` transaction, so one click on a 5,035-item list was
thousands of sequential round-trips (measured floor ≥5.5 s) and up to **9,560
SRS cards for one user, with no undo** — auto-promotion is one-way. Marking is
now capped at `MAX_LIST_WORDS` (500) per call, and the response carries
`remaining` / `capped`.

Capping is safe *because* the call is idempotent: it degrades into chunking
rather than truncation, and targets are taken in stable `word_list_items.id`
order so repeated calls drain front-to-back instead of re-rolling a subset.

| File | Tests | Covers |
|---|---|---|
| `tests/test_word_lists.py` | +8 | an under-cap user list is unchanged and reports `remaining 0 / capped false`; an over-cap list marks **exactly** 500 and reports the true remainder; the marked ids are the **first** eligible entries in list order; repeated calls drain 500 → 3 → 0 and end `capped false`; ambiguous/unresolved are skipped and **do not consume cap budget**; already-learning entries are not counted as remaining (so the drain terminates); a capped call on a system list still leaves the shared rows untouched; two users capping the same list stay independent |
| `src/components/WordListsPage.test.tsx` | +7 | confirmation appears above the 200 threshold and names the count; no confirmation below it; **cancelling makes no API call**; accepting does call; a capped response renders `3331 remaining — click again to continue`; the uncapped message is unchanged; and a pre-cap backend omitting `remaining`/`capped` still renders correctly |

**Both guards mutation-checked.** Removing the backend slice
(`eligible[:MAX_LIST_WORDS]` → `eligible`) fails 4 backend tests; disabling the
frontend threshold fails 2, including the cancel-makes-no-call test.

**One test assumption corrected.** The ordering test first read `item_id`
straight from `word_list_items` — but on a system list those are NULL (entries
resolve late on read, and the shared-row binding write is suppressed), so it
compared against a list of `None`. It now joins surfaces to `word_table` in
`wli.id` order. Same shared-state shape as the earlier fixtures: on system
lists, stored bindings are not the source of truth.

**Threshold vs cap are deliberately different numbers.** The UI confirms at
200, well below the backend's 500, so the dialog always fires before the cap
does — a user who confirms gets their whole request rather than a surprise
chunk.

**Not addressed here:** `get_list` still returns 406 KB / 514 KB and renders
every entry in a flat `.map()`. Slow, but recoverable; a flooded review queue
is not. Tracked in `docs/TODO.md`.

**Validation:** backend `-n auto` → **1137 passed, 2 skipped** (1129 + 8);
frontend `npx vitest run` → **296 passed across 44 files** (289 + 7);
`npx tsc --noEmit` clean; `npm run build` succeeded; `ruff check .` → All
checks passed. Root pipeline suite not run — no root files changed.

🆕 **2026-07-29 — Large-list render guard (+10 frontend)**

`get_list` returns every entry — 4,087 and 5,035 for the seeded built-in lists,
406 KB / 514 KB — and the detail view mounted all of them in one flat `.map()`.
Entries now render `ENTRY_CHUNK` (200) at a time behind a “Showing N of M
entries / Show more” control. **Frontend-only**: the response, the API, and
every count are unchanged; only what is on screen is bounded.

| File | Tests | Covers |
|---|---|---|
| `src/components/WordListsPage.test.tsx` | +10 | a 5,035-entry list mounts exactly 200 rows and `w200` is absent; the footer reads `Showing 200 of 5,035 entries`; Show more reveals the next 200; a 450-entry list reaches its final partial chunk and the control then disappears; a small list renders in full with **no** extra chrome; **ambiguous and unresolved entries are not filtered out** of the slice (it is positional); the count resets when a different list is opened **and** after a mark-learning refresh; counts, export and mark-learning still read the **full** response (5,035 shown in the badge and button while 200 rows are mounted); the built-in badge and delete-hiding are untouched |

**Both halves mutation-checked.** Dropping the slice
(`entries.slice(0, visibleCount)` → `entries`) fails 5 tests; dropping the
reset inside `showDetail` fails exactly the 2 reset tests — so the guard and
its lifetime are pinned independently.

**The reset is structural, not by convention.** There were four `setDetail`
call sites; they now all go through `showDetail()`, which sets the detail and
resets the slice together. Resetting at each call site would work until the
next one forgets.

**No “Show all” button, deliberately.** It would put the freeze one click away,
which is the thing this change exists to prevent.

**Still open:** the 406 KB / 514 KB payload itself. Reducing it needs backend
pagination — query params, a stable cursor, and a story for keeping status
counts whole-list — which is a larger design question than the render freeze
was. Tracked in `docs/TODO.md`.

**Validation:** frontend `npx vitest run` → **306 passed across 44 files**
(296 + 10); `npx tsc --noEmit` clean; `npm run build` succeeded;
`ruff check .` → All checks passed. Backend pytest not run — **no backend files
changed**. Root pipeline suite not run — no root files changed.

🆕 **2026-07-29 — Optional detail pagination, phase 1 (+14 backend)**

`GET /word-lists/{id}` accepts optional `limit` (1–1000) and `offset` (≥0).
They window **`entries` only** — `total` and `counts` stay whole-list on every
page, because they drive the status badges and the mark-learning count, which
describe the list rather than the window. 514 KB → ~22 KB for a 200-entry
window.

| File | Tests | Covers |
|---|---|---|
| `tests/test_word_lists.py` | +14 | no params returns every entry in order (the back-compat regression); `limit` returns exactly that many; three offset pages reassemble into the whole list with no overlap or gaps, ending in a partial page; offset past the end is an **empty page, not an error**; **`total` and `counts` are identical on a page and on the full response**; ambiguous/unresolved paginate in place and are not filtered; pagination works on a system list; **cross-user private access is still 404 with the params present**; four invalid-param cases (`limit=0`, `limit=-1`, `limit=1001`, `offset=-1`) are 422; export is byte-identical with and without params; and mark-learning still marks all 12 after a paged read |

**The whole-list guarantee is mutation-checked twice.** Computing
`total`/`counts` from the page, and slicing before counting instead of after,
both fail `test_total_and_counts_stay_whole_list_on_every_page` — the two ways
that invariant could plausibly be broken by a later edit.

**Back-compat is the load-bearing property.** All 80 pre-existing word-list
tests passed unchanged before a single new test was written, which is the real
evidence that omitting the params reproduces the old response — `create_list`
returns `get_list(...)` and would have broken loudly otherwise.

**Deliberately documented in the code:** this shrinks the response, not the
query. `_load_entries` costs ~33–36 ms on the seeded lists against ~3–4 ms to
serialise, and `_counts` needs the fully resolved set, so every page still does
the whole load. Nobody should later expect a latency win that isn't there.

~~**Phase 2 (frontend) not started.**~~ **Shipped 2026-07-29** — see the entry
directly below.

**Validation:** backend `-n auto` → **1151 passed, 2 skipped** (1137 + 14);
`ruff check .` → All checks passed; alembic unchanged at **037**. Frontend not
run — no frontend files changed. Root pipeline suite not run — no root files
changed.

🆕 **2026-07-29 — Frontend paged fetching, phase 2 (+16 frontend)**

`WordListsPage` now consumes the phase-1 params instead of slicing a full
response client-side. Opening a list requests `limit=200&offset=0`; *Show more*
requests `offset=<entries already loaded>` and **appends**. `visibleCount` is
gone — there is one paging concept, not two.

| File | Tests | Covers |
|---|---|---|
| `src/components/WordListsPage.test.tsx` | 10 → **26** in the (renamed) paged-fetching block | opening sends `limit=200&offset=0`; only the first page renders; **the footer counts against `total`, not the loaded page**; an explicit "never says 200 of 200" regression guard; *Show more* requests `offset=200&limit=200`; the page is appended with nothing duplicated; the final partial page hides the control; a one-page list shows no chrome; ambiguous/unresolved are not filtered out of a page; *Show more* is **disabled while in flight and fires exactly one request**; a failed page keeps loaded entries and shows an inline error with the control still enabled; **retry re-requests the same offset**, leaving no gap; opening another list resets entries *and* clears a page error; mark-learning refresh returns to `offset=0`; whole-list counts survive an append; **a freshly created list shows no paging control** (POST is the one path that fills `detail` without the paged GET) and files under *Your lists*, not the built-ins; built-in badge / delete-hiding and the user-list path unchanged |

**The stub pages the way the backend does** — slice `entries`, leave
`total`/`counts` alone. That matters: a stub returning the whole list would
have let the old `entries.length` denominator keep passing.

**Mutation-checked, including one that failed and changed the code.**
Denominator → `entries.length`: **3 tests fail**. Append → overwrite: **7
fail**. `hasMore` keyed on the page size (`>= ENTRY_CHUNK`) rather than on
`total`: **2 fail**. Dropping `disabled={loadingMore}`: **1 fails**. But dropping the
handler's `loadingMore` early-return: **0 fail** — correctly, because a
disabled button never dispatches a click, *and* because two clicks in one frame
would both read the pre-click closure where that state is still `false`. It was
guarding a race it could not see. Replaced with the `inFlightPagesRef` key set
described below, which holds regardless of commit timing. Its *same-page*
behaviour is **not independently observable through the DOM** — `disabled` is
what locks the double-click; the ref is the backstop. Its *cross-list*
behaviour is fully observable and is tested.

**Three ordering hazards are handled, each pinned by a test that fails when its
guard is removed.** A page arriving after the user opened a different list is
dropped (`prev.list_id === listId` inside the functional `setDetail`); a late
failure cannot write its error over the new list (`openListIdRef`); and the
duplicate-request guard is a **set of `listId:offset:limit` keys**, not a
boolean. The key shape is load-bearing in both directions — dropping `listId`
from it fails *"does not let one list's pending page block another list's
paging"*, and clearing the set in `showDetail` fails *"still refuses a
duplicate after navigating away and back"*. Each request deletes only its own
key, so the set drains itself while every other pending page keeps its guard.
Without all three, "reset on list switch" holds only until a slow request
lands.

**Validation:** frontend `npx vitest run` → **322 passed across 44 files**
(306 + 16); `npx tsc --noEmit` clean; `npm run build` clean (474.86 kB JS /
127.80 kB gzip). Backend pytest **not run — no backend files changed**; `ruff`
**not run — no Python files changed**; root pipeline suite **not run — no root
files changed**.

🆕 **2026-07-27 — vocabulary list upload/download (+39 tests / +2 files)**

New feature: paste or upload a word list, see what you already know, mark the
unknown ones as learning, export the list. Backend `word_list_service` +
`routers/word_lists.py`; frontend `api/wordLists.ts` + `components/WordListsPage.tsx`
at route `/lists`. Migration **035** made the dormant 001 tables usable
(`word_lists.language`, `word_list_items.surface NOT NULL`, nullable `item_id`,
unique on `(list_id, lower(surface))`).

| File | Tests | Covers |
|---|---|---|
| `tests/test_word_lists.py` (NEW) | 26 | migration-035 schema shape (incl. **the 001 unique constraint is gone**, not merely shadowed); create + resolve; case-insensitive resolution; **language scoping** (same surface, `es` list → unresolved); unresolved stored not dropped, and still there on re-read; **ambiguous never first-match resolved** (asserts the stored `item_id` is NULL); dedupe by `lower(surface)` with first-spelling-wins; blank entries skipped; all five counts; 422 on empty/oversized, 201 at exactly 500; export round-trips **all** surfaces in insertion order incl. unresolved/ambiguous, `text/plain`; cross-user 404 on read/export/delete/**mark-learning** (parametrized) + a dedicated test that a cross-user mark creates **no** knowledge row; mark-learning creates knowledge + **both** SRS directions; skips unresolved/ambiguous; leaves `known` alone; idempotent; delete cascades entries |
| `components/WordListsPage.test.tsx` (NEW) | 13 | paste flow posts parsed words (blank lines dropped, order kept); comma/semicolon splitting + live count; `.txt` upload fills the textarea and defaults the list name; all five counts render; unresolved + ambiguous surfaces stay **visible**; the ambiguous explanation shows only when something is ambiguous; mark-learning hits the endpoint and reports skips; the button disables at 0 unknown; download hits `/export`; empty and oversized lists refused **client-side with no POST**; server 422 and index-load 500 both surface inline |

**Why `ambiguous` is a first-class state rather than a first-match pick.**
`word_service.lookup_word_by_text` deliberately refuses to choose between
several `word_table` rows for one surface (*die Bank* = bench vs. bank) — the
W3 / Hole 2 fix. A 500-word upload has no interactive picker, so rather than
reviving the silent-pick bug in bulk, an ambiguous surface is stored unbound
and reported as such. `mark-unknown-learning` skips it. Two tests pin this: one
asserts the persisted `item_id` is NULL, one asserts neither candidate sense
gains a `user_word_knowledge` row.

**Resolution rule under test:** `lower(w.word) = ANY($1::text[]) AND w.language = $2`,
one round-trip for the whole list. Not `ILIKE` — that would not use
`ix_word_table_lang_lower_word` (migration 032). Comparing Python's `lower()`
against Postgres's also fails safe: a disagreement on an edge glyph reports the
word as *unresolved* rather than binding it to the wrong row.

**Validation:** backend `-n auto` → **786 passed, 2 skipped**; frontend
**274 passed** (44 files); `tsc --noEmit` clean; `npm run build` clean;
`ruff check .` → All checks passed. Root pipeline suite not run — no root files
changed.

🆕 **2026-07-27 — playlist optimizer selector (frontend only, +13 tests / +2 files)**

Surfaces the backend's opt-in `algorithm` parameter in `PlaylistPanel` as a
**Planning** control: *Fast* (greedy, default) vs *Optimal (slower)* (ILP).

| File | Tests | Covers |
|---|---|---|
| `api/playlists.test.ts` | 7 | `algorithm` defaults to `"greedy"` and is **always sent explicitly** (never omitted, so the request states the choice rather than depending on the backend default); explicit `"ilp"`/`"greedy"`; 503 → `PlaylistSolverUnavailableError`; the operator-facing PuLP detail never reaches the message; non-503 stays an ordinary `Error`; success parses |
| `components/PlaylistPanel.test.tsx` | 6 | defaults to Fast; untouched → `"greedy"`; selecting Optimal → `"ilp"`; **503 shows an inline "switch to Fast" recovery message** with no PuLP text and the page intact; non-503 keeps the generic message; 16px font + 44px touch target |

**Why the 503 is handled in `api/playlists.ts` rather than `_http.ts`:** 503 has
no shared meaning across the API — this is the only endpoint whose backend is
optional (PuLP) and can be absent while the request is perfectly valid. A typed
`PlaylistSolverUnavailableError` lets the panel say *"Optimal planning is
unavailable right now. Switch to Fast and try again."* instead of `assertOk`'s
generic error or the backend's `pip install pulp` string.

**Gotcha worth remembering:** the first run reported `6 passed` but **exited 1**.
Vitest logged 2 *unhandled errors* — the stubbed playlist response carried a
partial `coverage` object, and on success the panel renders `ResultView`, which
reads `uncovered_item_ids.length`. The fixture, not the product, was wrong (the
backend always sends all five coverage fields — see
`test_endpoint_response_shape`). **A passing test count is not a passing run:
check the exit code and the Errors line.**

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

- 🆕 `tests/evaluation/` — **147 tests** in six files against the
  document-ingestion evaluation harness (`evaluation/`, roadmap **A1**). No LLM,
  no `nlp_histo` import, no Docling/torch. spaCy `de_core_news_md` is the only
  heavy dependency and `evaluation/segmentation.py` falls back to
  `spacy.blank("de")` + sentencizer when it is absent, so the suite runs in a
  bare environment. `tests/evaluation/conftest.py` supplies `benchmark_dir`
  (the committed corpus) and `tiny_corpus` (a two-sentence tmp_path fixture).

Root suite baseline: **368 passed** (97 runtime + 124 scraper + 147 evaluation).
Run `pytest tests/` from repo root. Historical: 221 before the A1 harness,
562 at W4, peaking at 744 before the `src/app/` deletion, 207 immediately after
it, 211 before the Spanish gerund slice.

🆕 **2026-07-29 — Document-package import, roadmap A2 (+89 backend / +1 file)**

`tests/test_document_package.py`. Mostly hermetic — every fixture is a small
synthetic package built in `tmp_path` rather than a committed blob, so a test
names the exact defect it introduces. Only the five `TestImportEndpoint` cases
need `client`/`db_pool`.

Three groups worth knowing before changing `services/document_package/`:

- **`TestBoundary`** asserts `nlp_histo`, `docling`, `torch` and `transformers`
  are absent from `sys.modules` after importing the backend. The package-on-disk
  boundary is the whole point of the design (§3); an accidental import would
  drag GPU dependencies into the API process and undo it silently.
- **`TestContainment`** is the security suite for the traversal chokepoint —
  parametrized over `../`, absolute paths, a real symlink escape, a hostile
  `page_images.path_template`, and a hostile package name. See the matching
  entry in `docs/SECURITY.md` under *Verified strengths*.
- **Severity pins.** Several tests assert a defect is a *warning* rather than
  fatal (unknown element type, reading-order gaps, inverted y, malformed
  confidence, missing provenance) and vice-versa (duplicate reading order,
  coordinate-space mismatch, bbox off-page, `bbox_page_mismatch`). That split is the
  information-preservation principle (§2b) expressed as tests: losing content is
  irreversible, so only misplacement/loss is allowed to block an import.
  Flipping one of these is a design change, not a test fix.

`test_real_persistence_refuses_until_a3` and
`test_real_import_refuses_and_reports_rather_than_raising` are the pins that
flip when **A3** lands.

🆕 **2026-08-04 — `bbox.page` / `page_index` consistency (+11 backend, same file)**

Eight test functions in `TestStructuralValidators`, 11 collected (one is
parametrized over four values), covering
`validators.validate_bbox_page_consistency`. The question the audit raised first
was whether `page_index` is zero-based while `bbox.page` is one-based — it is
not: §4 of `docs/INGESTION_PIPELINE.md` states one 1-based numbering, now pinned
as `contract.PAGE_INDEX_BASE` and asserted by
`test_page_numbering_is_one_based_throughout`. The 11/12 pair in that doc's
example was a typo, and is fixed.

Three of the eight are not about the happy path and should survive refactors:

- `test_bbox_page_mismatch_is_caught_by_full_validation` is a **registration
  pin**. Most tests here call a validator function directly, so one missing from
  `STRUCTURAL_VALIDATORS` would pass every unit test and run on no import.
- `test_non_integer_bbox_page_is_fatal` parametrizes `True`, which equals `1` in
  Python and would otherwise pass on page 1.
- `test_bbox_page_not_reported_when_page_index_is_unusable` pins the
  no-double-report rule: a bad `page_index` belongs to `validate_page_indices`
  alone.
🆕 **2026-08-04 — The checksum gate may not claim what it did not verify (+6 collected in `test_document_package.py`; 4 new test functions, the failure case parametrized ×3; no new file)**

Audit `ingestion_pipeline:ing-06` (rt-06; duplicate of `ing-05` /
`ingest-defect-01`). `verify_checksums` closed with an unconditional INFO
"verified N file(s) against CHECKSUMS.txt", so a rejected package's report
carried a success claim beside the `checksum_mismatch` that refuted it. Now
`checksums_verified` is emitted only when the **whole gate** found nothing
fatal; otherwise a neutral `checksums_checked` reports the size of the
inventory that was walked, with no derived failure count (the fatal delta also
covers malformed lines and coverage gaps, which are not failed digests).

Three new cases in `TestChecksumGate` plus one in `TestImportFlow`:

- `test_a_clean_gate_reports_what_it_verified` — the positive claim survives,
  message and all; the neutral code is absent.
- `test_a_failed_gate_never_claims_verification` — parametrized over
  `mismatch` / `uncovered_required_file` / `malformed_line`. **Two of those
  three fail outside the digest loop**, which is the pin that matters: every
  listed digest can match while the package is still unverifiable because the
  inventory is malformed or does not cover `manifest.json`. A fix that gates
  only on the loop passes the mismatch case and fails these two.
- `test_an_absent_inventory_claims_nothing_either_way` — nothing was hashed, so
  neither line is emitted.
- `TestImportFlow::test_a_rejected_result_carries_no_verification_claim` —
  end-to-end through `dry_run_package`, because `result.info` is the field a
  human triaging the rejection actually reads.

Counts in the summary table below were not re-measured for this change (no
execution in the worker role that made it); the delta is +5 backend cases.

🆕 **2026-08-05 — Migration 025 downgrade guard (+23 backend / +1 file)**

Audit `tests_ci:db-02` (rt-02). `025_block_token_ids.downgrade()` was a bare
`DROP COLUMN tokens`. `reading_selections.anchors` holds `{block_id, token_id,
surface}` pointing into that column with **no FK**, and `_tokenize` mints fresh
`uuid4()`s on every run — so downgrade + re-upgrade silently reassigns every id
and orphans every anchor. No error, no warning; the selections survive but stop
resolving to any text. `downgrade()` now counts dependent rows and raises
before touching the schema.

| File | Tests | Covers |
|---|---|---|
| `tests/test_migration_025_downgrade.py` (NEW) | 23 | `_tokenize` id instability across runs (the reason the loss is unrecoverable); refusal with the row count + the DELETE that constitutes explicit consent; **no DDL executed on the refusal path**; proceed at zero dependents and on a `None` scalar; no env-var force flag; the counting SQL run against the live schema — a token_id anchor counts, pre-025 legacy anchors / JSON-null token_id / empty anchors do not, and a 3-anchor selection counts once; an 8-case shape matrix over every JSON value `anchors` can hold; the unscoped constant run over a table that really contains non-array rows; SQL-NULL anchors via a synthetic row; `EXPLAIN` on the DELETE the refusal quotes; and the constant surviving `sa.text()` intact (the `'[]'::jsonb` cast vs. its bind-param regex) |

**No force flag, deliberately.** Nothing else in this repo gates destruction on
an env var (022 raises flatly, 035 deletes only rows it created), and the
escape hatch already exists and is better: the operator deletes the dependent
`reading_selections` rows, which makes the loss explicit and auditable. The
refusal message hands them that exact statement.

**The load-bearing test is `test_dependent_anchors_sql_counts_only_token_id_anchors`.**
Anchors written before 025 have no `token_id` key at all — that's why
`routers/reading.py:64` falls back to `"legacy"`. A guard that counted every
`reading_selections` row would block the downgrade on deployments that never
depended on the column, which is a bug in the opposite direction. The scoped
count pins both halves on one user's fixtures.

**Second round (same day): the predicate is shape-safe, not just correct on
well-formed rows.** `anchors` is `JSONB NOT NULL DEFAULT '[]'` (migration
009:32) — an array of objects is a convention of the write path, not a
constraint — and `jsonb_array_elements` *errors* on a JSON null, an object or a
scalar. One such row anywhere in the table turned the guard from a clean refusal
into `cannot extract elements from a scalar` **mid-downgrade**, which is worse
than having no guard at all. The predicate now expands only arrays
(`CASE WHEN jsonb_typeof(rs.anchors) = 'array' … ELSE '[]'::jsonb END`) and only
object elements. Non-array anchors count as zero dependents on purpose:
`routers/reading.py:59-60` already coerces any non-list to `[]`, so the row
resolves to no anchors in the app today and the downgrade destroys nothing it
had not already lost.

Coverage for that: `test_dependent_anchors_sql_classifies_every_stored_shape`
(8 params — JSON null, un-wrapped object, bare scalar, empty array, legacy
array, array with a JSON-null token_id, array of scalars, token_id array) plus
`test_dependent_anchors_sql_survives_non_array_rows_in_the_live_table`, which
inserts the non-array rows and then runs the constant **unscoped**, exactly as
`downgrade()` does — that is the one that fails pre-fix. SQL NULL is the only
shape the live schema cannot hold (NOT NULL), so
`test_predicate_treats_sql_null_anchors_as_no_dependency` drives it over a
synthetic `(VALUES ($1::jsonb)) AS rs(anchors)` row, with a positive control so
the harness can't pass vacuously.

**The refusal's DELETE is derived, not re-typed.** `_DELETE_DEPENDENTS_SQL` is
built from `_ANCHOR_DEPENDS_PREDICATE`, so the operator's escape hatch inherits
the array guard instead of crashing on exactly the rows it exists to clear.
`test_delete_dependents_sql_plans_against_the_live_schema` runs `EXPLAIN` on it
— plans against the real schema, executes nothing, deletes nothing.

**This suite never runs `alembic downgrade`** — the tests share the dev
database, and a real downgrade would drop `book_blocks.tokens` out from under
every other suite. The SQL constant is executed directly through asyncpg
instead, and `downgrade()` is driven with a fake `op` that only records what it
was handed. The two live tests scope their count by *appending* to
`mig._DEPENDENT_ANCHORS_SQL` rather than copying the predicate, so the string
under test is the one that ships and the count stays deterministic under xdist.

Note this edits a migration that has already been applied (CLAUDE.md §12,
append-only). Only `downgrade()`, the module-level SQL constants it reads, and
the prose changed — `upgrade()`, `_tokenize` and the revision ids are
byte-for-byte unchanged, so no deployment can diverge. The docstring says so, to
save the next reader the diff.

**Latent sibling, deliberately not touched:** `services/reading_service.py:155`
runs the same unguarded `jsonb_array_elements(anchors)` on the request path and
would raise on the same non-array rows. Out of scope for this task (a migration
guard), recorded here so it isn't mistaken for coverage.

Counts in the summary table below were not re-measured for this change (no
execution in the worker role that made it); the delta is +23 backend cases,
counted from the file rather than from a run.

🆕 **2026-07-29 — Document-ingestion evaluation harness (+147 root / +6 files)**

`tests/evaluation/{test_normalize,test_annotation,test_metrics,test_fidelity,test_report_and_compare,test_integration}.py`.

Three of these encode design invariants rather than mere behaviour, and are the
ones to read before changing `evaluation/metrics.py`:

- **`test_fidelity.py`** pins the information-preservation principle: deleting
  genuine content is *irreversible* (the multimodal reviewer never sees it),
  retaining an artifact is *recoverable*. `FALSE_DELETION_WEIGHT` is asserted to
  be ≥10× `RETAINED_ARTIFACT_WEIGHT`, deleting is asserted to score worse than
  retaining comparable text, and keeping-plus-flagging (`uncertain=True`) is
  asserted cheaper than leaving unflagged but **not free**. A pipeline that
  cannot report what it deleted scores `None`, never zero.
- **`test_report_and_compare.py`** pins severity ordering: a *critical*
  regression sorts above a numerically larger *normal* one, and descriptive
  counts (`predicted_boundaries`, `total_units`) carry no direction at all.
  Also the comparability guards — differing schema version, corpus hash or
  segmenter **raise** rather than diff, so an edited annotation can never read
  as a code regression.
- **`test_annotation.py::TestRoundTrip`** — `parse → serialize` byte-identity.
  This caught a real serializer bug: the page-break marker `<PB/>` does not
  advance the parser's page counter, so advancing it on serialize swallowed the
  following `==== PAGE n ====` header and silently reassigned later sentences.

Two known-limitation pins that should flip when later roadmap items land:
`test_page_spanning_sentence_is_not_recovered_by_naive_join` (0/1 today → 1/1
with **A4** reconstruction), and `03_zeichensetzung`'s OCR-missing-period case
(needs **A8** AI review). Both are asserted at their current value rather than
xfailed, so the improvement is visible as a test change.

Backend suite baseline (W13, 2026-05-20): **521 passed / 2 skipped** (xdist parallel run ~48s). Run: `pytest -n auto` from `lexy-app/backend/`.

---

## Autoloop tests — `autoloop/tests/`

Added 2026-07-29 with the Fable ↔ ChatGPT orchestration infrastructure
(`autoloop/`, see `docs/AUTOLOOP.md`); expanded the same day by Phase 2 (task
registry, contract v2 review-integrity stamps, template library,
`LLMConversation` seam, review context) and Phase 3 (single-instance locking,
task-owned change manifests / exact-path staging, `doctor` + `smoke-browser`,
the audit executor with read-only claude-CLI subagents, phase gating) and by
the 2026-07-30 browser-transport repair (submission confirmation vs optimistic
rendering, explicit reconciliation, no navigation while awaiting,
browser-realistic composer input, per-stage bounded timeouts), and the same
day's Autoloop M1 produce-then-review commit path — pass 1 (honest-sha commit
capture, F2/F5-hardened exact-refspec push, crash reconciliation via
`reconcile_after_crash`), pass 2a (per-task worktree lifecycle, the commit
path wired into `_dispatch_executor`, gated behind an optional
`worktrees`/`execution_store`/`intent_store` constructor triple so every
prior test is unaffected), and pass 2b, same day (the review packet —
`packet.py` — sent for review on a passing commit instead of parking with a
placeholder; the `PostcommitBinding` request/response binding that pins a
push to the exact candidate it reviewed; `push_exact`-only publishing —
`GitGateway.push()` removed entirely, `_dispatch_git`'s legacy push
re-implemented on `push_exact` and fail-closed against a live
produce-then-review candidate with no matching binding; the round>0
path-ownership union fix; the two-round cap with `_park_round_cap`;
`PendingRequest.prompt_sha256` verified before every resend), and by
Autoloop M2, also 2026-07-30 (`worker_env.py`, `publisher.py` — structural
worker/publisher separation: `worker_env()` env scrubbing, `WorkerRepoManager`
no-remote per-task repos, `verify_worker_isolation`, `Publisher`'s
hooks-controlled dedicated repo and `import_candidate`/`publish`,
`GitGateway.fetch_object` + the new `env=` gateway parameter,
`Orchestrator`'s optional `publisher=` constructor param wired into
`_dispatch_task_push` — see `docs/AUTOLOOP.md` §4c for the full design and
the explicit threat-model boundary).
**Autoloop v1 (2026-07-30): the legacy authorize-then-produce/change-manifest
commit path was RETIRED** (`docs/SECURITY.md` S21 — closed by retirement, not
a fix). `orchestrator.py`'s `_dispatch_git` and `GitGateway.commit()` are
removed; `_dispatch_executor` routes every directive — audit included, via
`_resolve_audit_task`'s synthetic per-run `Task` — through the SAME
produce-then-review path (`_dispatch_task_postcommit`). `cli.py`'s
`_build_orchestrator` now always constructs the full collaborator set
(`WorkerRepoManager`, `TaskExecutionStore`, `IntentStore`, a provisioned
`Publisher`), so the plain `run` command has exactly one dispatch path. New:
`run --continuous`, `next-task` (a git-tracked `seed_tasks.json` seeds
`rt-01`), `reprovision-publisher --confirm` (the only way the publisher's
url snapshot changes after first provisioning), and `doctor` checks for
worker isolation / controlled hooks dirs / publisher configuration /
publisher URL drift. Legacy-path-only tests were deleted (13 from
`test_manifest.py`, 11 from `test_git_gateway.py`, 19 from
`test_orchestrator.py` net two additions — a review-serialisation test
(item 5 of the v1 brief) and a two-audits-in-one-session test proving
`_resolve_audit_task` mints a distinct unit id per audit rather than
colliding on the literal `"audit"` pseudo-task id — final 39, 3 from
`test_postcommit_review.py` net one addition) and a new `test_v1_smoke.py`
(14 tests) proves the v1 wiring end to end. `commit_adopted`/
`ChangeManifest.adopt` are unmodified and still directly unit-tested
(`test_manifest.py`/`test_git_gateway.py`) but have no production caller
anymore (S22).

**Continuous-mode blockers (2026-07-31).** `run --continuous` was
single-track — ANY park stopped the whole loop, even one scoped to a single
task. `orchestrator.py`'s ~25 `_to_needs_user` call sites are now each
explicitly classified `kind="task_fatal"` (one task's own unit of work;
`cli.py` quarantines it via the new `TaskRegistry.block`/`unblock` and a
`TaskState.BLOCKED_BY_OPERATOR` state, then keeps working whatever else is
READY) or `kind="loop_fatal"` (the environment/operator; the loop stops,
same as every park before this). The default is `loop_fatal` — fail closed,
so an unclassified or future park site never silently churns. Every park,
either kind, persists a `blockers.Blocker` (new `blockers.py`,
`BlockerStore`) carrying the exact operator question; new CLI commands
`blockers`/`answer` list and resolve them. `_run_continuous`'s exhaustion
case (no ready task, unchanged fingerprint) now prints all open blockers and
exits 0 instead of sleeping forever, but only when at least one is open —
the ordinary idle steady state (zero blockers) is unchanged. **Quarantine is
enforced, not advisory:** `policy._check_task_reference` denies any
`implement`/`revise` naming a `BLOCKED_BY_OPERATOR` task id directly
(`task_blocked_by_operator`), and `TaskRegistry.mark_in_progress` refuses it
too (defense in depth for any dispatch path that bypasses policy) — without
this, `next_ready()` skipping the task would have been merely advisory: a
directive naming it directly would have sailed through. See
`docs/AUTOLOOP.md` §9c. New: `test_blockers.py` (15 tests). **600 tests.**

**Implement executor (2026-07-31).** `implement`/`revise` of a real registry
task had nowhere write-capable to go — `AuditExecutor`'s subagents are
read-only by construction and refuse anything but audit/revise-of-audit;
`NullExecutor` only records intent. New `autoloop/implement_executor.py`:
`ImplementExecutor` runs ONE `Edit`/`Write`-capable `claude -p` subagent (via
the audit's own `audit.agents.ClaudeCliRunner`, whose tool set is now a
constructor parameter — `allowed_tools`/`disallowed_tools` default to the
read-only set, so `AuditExecutor` and every existing `test_audit_agents.py`
test build the exact same argv as before this became configurable) against
the task's own isolated worker repo, then re-runs validation. `Bash`/
`Task`/`Agent` stay disallowed; no `--model` flag is ever passed (automatic
selection — deliberately no model table). `changed_paths` is derived from
the worker repo's OWN `git status --porcelain -z -uall`
(`GitGateway.dirty_entries_all`/`dirty_paths_all`, new; `policy.py`'s
`status` whitelist now additionally admits `-uall`) — NEVER the agent's own
claim about what it did — specifically because the plain `-z` form
collapses a new file inside a brand-new directory to just the directory
entry (`?? d/`), which would go on to fail the post-commit structural check
(it compares literal file paths). `cli._build_executor` now constructs BOTH
`AuditExecutor` and `ImplementExecutor` and wraps them in a small
`_DispatchingExecutor` (new, in `cli.py`) that routes each directive by the
same `is_audit` test the orchestrator's own `_dispatch_executor` uses;
`orchestrator.py` and the `TaskExecutor` protocol (`executor.py`) are
untouched. `policy.implement_enabled`'s default (`false`) is unchanged by
this work — flipping it is a separate operator decision. New
`test_implement_executor.py` (13 tests): write-capable argv (`Edit`/`Write`
allowed, `Bash`/`Task` disallowed), no `--model` flag, subagent `cwd` is the
task's worker repo not the main checkout, `changed_paths` ignores a false
agent claim, a filename with both a space AND a tab round-trips, agent
failure / no-files-changed / validation failure each report
`status="error"` without raising, success reports `status="ok"` with
`changed_paths`/`validation` populated, and nothing is written outside the
worker repo (main checkout and `.autoloop/` both provably untouched, plus
two bonus tests for the audit/`None`-task refusal branches and the
constructor's `worker_repo_root_for`/`policy` pairing contract). **668
tests.**

**Worker-isolation hardening (2026-07-31; `docs/AUTOLOOP.md` §4e,
`docs/SECURITY.md` S23-S26).** An adversarial review of the M2 worker
isolation (above) found four gaps: the worker repo lived at
`config.workers_dir` (`state_dir/"workers"`), INSIDE the checkout, invisible
to every git-based verification primitive scoped to it; the write-capable
agent's confinement was a sentence in the prompt with no filesystem-escape
detector; a task's post-commit path-ownership check was validated against
the executor's OWN report (`outcome.changed_paths`), never against anything
authorized independently, so an agent could never be "caught" widening its
own scope; a failed round's uncommitted files stayed in the worker repo for
the NEXT round to pick up, and the attempt-count ceiling only incremented
after a commit, so a validation failure never consumed an attempt. All four
closed (the third partially — see S24): `config.workers_root`, a required
absolute config value validated by the new `worker_env.validate_workers_root`
(refuses relative/nested-under-checkout/`.git`/state-dir/publisher-dir,
including a linked worktree's real gitdir behind its `.git` pointer file) —
replaces `config.workers_dir` everywhere a `WorkerRepoManager` is
constructed for real dispatch; new `escape_detector.py` — a deterministic
before/after filesystem snapshot (content sha256/symlink target/executable
bit over tracked+untracked+ignored paths) of the PRIMARY checkout bracketing
every write-capable executor call, non-audit only; new `Task.approved_paths`
— the ONLY thing a non-audit task's post-commit path-ownership check is
validated against now, fixed before the writer starts and never widened by
`outcome.changed_paths`/a recovered `CommitIntent.planned_paths` again (the
audit keeps its pre-existing accumulate behaviour — its scope is already
bounded by `MarkdownPolicy`, a different, non-agent-controlled mechanism); a
pre-commit gate refuses to commit at all if `outcome.changed_paths` leaves
`approved_paths`; `WorkerRepoManager.quarantine` moves (never deletes) a
dirty worker repo aside before a fresh attempt, so failed-round residue can
never ride into a later commit; `attempt_count` now increments and persists
BEFORE the executor runs, not in `_finish_postcommit`. Two dead/mismapped
`cli._RESOLUTION_PRECONDITIONS` keys fixed
(`git_failure_budget`→`git_failure_budget_exhausted`;
`worker_environment_drift` now a dedicated `verify_worker_isolation`-based
recheck instead of the unrelated browser/login probes) and one previously
dead code made real (`push_refused_protected`, now actually emitted by
`Orchestrator._dispatch_task_push` when a push is refused specifically for
targeting a protected branch). New `test_m1_hardening.py` (52 tests):
`workers_root` relative/missing/nested-under-checkout/`.git`/linked-worktree-
gitdir/state-dir/publisher-dir refusal and the positive external-path
acceptance case, `escape_detector` unit tests for each of
creation/deletion/tracked-content/ignored-content/symlink-target/
executable-bit changes, end-to-end orchestrator dispatch tests proving an
agent writing outside its worker repo (including tampering with
`.autoloop/state.json` and a blocker record specifically) is caught and the
audit path is NOT wrapped, `Task.approved_paths` structural-violation
parametrization (absolute/`..`/glob/empty/padded/backslash — 14 cases) plus
symlink-traversal, an agent-reported extra path refused before commit, an
unexpected commit path from a crash-recovered prior process refused after
adoption (hooks are proven categorically impossible for `worker_repos`, so
this is the realistic mechanism, not an evasion of an easier hook-based
test), a task with no `approved_paths` refused before a worker repo is even
created, failed-attempt residue proven absent from the next candidate (with
the quarantine directory and its preserved evidence asserted directly), the
attempt budget proven to survive a simulated process restart (a fresh
`Orchestrator` per attempt, nothing carried over but disk state), the
`worker_environment_drift` precondition proven to be the dedicated recheck
(not the browser one) by exercising both a still-broken and a genuinely-
fixed config, `answer` end-to-end refusing a worker-drift blocker on
arbitrary text, and an AST-based (not regex, not hand-maintained)
exhaustiveness check that every `_RESOLUTION_PRECONDITIONS` key matches a
real `code=` literal `orchestrator.py` actually emits (including one built
from a conditional expression). **723 tests initially; +6 across two
rounds of same-day follow-up below.**

**Same-day follow-up, round 1 (5 tests) — two real gaps an external review
found in the pass above, neither caught by its own suite passing:** (1)
three new `loop_fatal` codes the pass introduced
(`primary_checkout_dirty`/`checkout_escape_detected`/
`worker_isolation_violation`) had no `_RESOLUTION_PRECONDITIONS` entry at
all — the exact S26 failure mode, reopened by S26's own fix, since the
forward-only AST exhaustiveness test cannot detect a missing key, only a
stale one. Mapped `worker_isolation_violation` to the existing
`_precondition_worker_environment_drift`, `primary_checkout_dirty` to a
new `_precondition_checkout_clean` (re-runs `GitGateway.is_dirty()`), and
— at this point incorrectly, see round 2 below —
`checkout_escape_detected` to that SAME function; plus three dedicated
tests and one new CURATED (not AST-derived) reverse test,
`test_security_and_environment_codes_all_have_a_precondition`, pinning the
full set of codes that must never resolve on text alone. (2) The
vacuous-by-construction original `test_audit_dispatch_is_not_wrapped_by_
the_escape_detector` (its assertion was a disjunction satisfied by ANY
unrelated loop_fatal park, and it dispatched a task id the audit path
never actually uses) rewritten under the same name with a positive spy on
`_execute_with_escape_detection` itself. A separate, non-test bug found and
fixed in the same round (one new test, no new file):
`_prepare_write_capable_worker`'s quarantine-and-recreate branch always
fetched from the primary checkout even when resuming
`execution.candidate_sha`, which exists only inside the worker repo just
quarantined — see `test_quarantine_recreate_resumes_from_a_candidate_sha_
that_only_exists_in_the_quarantined_repo` and `docs/COMMON_ERRORS.md` §8.

**Same-day follow-up, round 2 (1 more test; 1 existing test rewritten, no
count change from it) — a second external review found the round-1 fix for
`checkout_escape_detected` was itself wrong.** Sharing
`_precondition_checkout_clean` with `primary_checkout_dirty` means the
recheck is `GitGateway.is_dirty()`, which only sees tracked + working-tree
changes — but `escape_detector`'s own snapshot deliberately covers a
SUPERSET (tracked + untracked + IGNORED paths; this file's own
`test_escape_detector_detects_ignored_content_change` proves
`git.dirty_files() == []` even while the detector correctly flags
ignored-path tampering). Autoloop's own state directory is gitignored in
production, so the canonical "agent forged its own records" escape is
EXACTLY the case an `is_dirty()` recheck cannot see — that blocker would
have cleared on a check that verified nothing. Fixed:
`checkout_escape_detected` now has its own
`_precondition_checkout_escape_detected`, which refuses UNCONDITIONALLY
(mirroring `_precondition_protected`'s shape) rather than attempting any
automated recheck.
`test_checkout_escape_detected_precondition_refuses_until_checkout_is_clean`
(round 1's test, which asserted the WRONG "clears once clean" behaviour)
is replaced by
`test_checkout_escape_detected_precondition_never_resolves_by_answer_text`
(dirty AND clean both refuse) plus one new end-to-end test,
`test_checkout_escape_detected_blocker_cannot_be_cleared_even_when_only_an_ignored_path_was_touched`,
which reproduces the exact ignored-path scenario through
`python -m autoloop answer`. The audit-dispatch negative-control test from
round 1 was also strengthened in this round (no count change): asserting
only that the escape-detector spy saw zero calls could pass vacuously if
the audit dispatch parked for an unrelated reason before ever reaching the
executor — `executor.calls` is now asserted directly too, proving the
audit executor actually ran. **729 tests.**

**Operator-changeset review (2026-07-31).** A `push` directive answering
anything other than a produce-then-review candidate hit
`legacy_git_path_retired` (§4c) — sound for reopening the retired
authorize-then-produce path, but it also meant an operator's own
infrastructure commits (authored directly on the branch autoloop runs from,
never produced by this loop's own executor) could only ever be published by
hand. New `changeset_review.py`: `ChangesetBinding` mirrors
`state.PostcommitBinding`'s shape (`base_sha`/`candidate_sha`/
`candidate_tree_sha`/`packet_sha256`, plus explicit `branch`/`dest_ref`) but
has no task and no worktree behind it — the candidate lives directly in the
checkout that reviewed it. `python -m autoloop review-changeset --base <sha>
--candidate <sha> [--packet FILE]` validates both shas (literal 40-hex,
resolve, descendant) and the destination branch (not protected) before
touching any session, renders the packet from immutable git objects
(reusing `packet.py`'s helpers), and queues it. `Orchestrator.
_dispatch_changeset_push` — new, modelled on `_dispatch_task_push` — is
Publisher-only (no direct-push fallback: that would be the retired legacy
shape this feature replaces) and re-verifies descendant-of-base, candidate
resolvability, and tree identity immediately before publishing.
`_step_executing` deliberately skips the generic "repository HEAD must
still equal the reviewed head_sha" staleness check for a changeset-bound
`push` response — that checkout IS the branch under review, and the whole
point is publishing the reviewed candidate even after a later, unreviewed
commit lands — and reads `resp.changeset.branch` (not a fresh
`current_branch()` lookup) for the protected-branch policy gate, for the
same "judge the actual destination, not incidental checkout state" reason
`_dispatch_task_push` already does. New `test_changeset_review.py` (4
tests; +1 pre-existing `test_prompts.py` parametrized case picked up the new
`changeset_review` template automatically).

**Same-day follow-up (review pass, no new test file, count unchanged).** Two
gaps a review found in the pass above: (1) `changeset_publisher_required`
(the new `loop_fatal` code for "no Publisher configured") had no
`cli._RESOLUTION_PRECONDITIONS` entry — exactly the S25/S26 failure shape
§4e warns about, invisible to the forward-only exhaustiveness test. Mapped
to the existing `_precondition_publisher_url` recheck and added to
`test_m1_hardening.py`'s curated `security_and_environment_codes` set (both
edited in this follow-up; `test_m1_hardening.py`'s own count is unchanged —
the set gained an entry, not a new test). (2)
`test_protected_destination_refuses_and_nothing_is_pushed` originally
couldn't discriminate `_step_executing`'s `destination_branch` ternary
(reading the pinned `resp.changeset.branch` rather than a fresh
`current_branch()`) from its absence, because the test repo's checkout
branch happened to equal the binding's branch either way. Fixed by
checking out an unprotected sibling branch AFTER `_step_ready()` but before
dispatch — reverting the ternary now makes the test's own `phase == READY`
assertion fail (verified). The same test also now exercises the CLI-time
guard (`build_changeset_binding` raising on a protected branch) via
`pytest.raises`, folded into the existing test rather than a 5th one. **736
tests.**

**Progress-based stall detection replaces the agent timeout (2026-08-14,
`stall-01`).** `audit.agent_timeout_seconds` bounded every subagent by ELAPSED
TIME. Measured over 2026-08-05/06 it never once caught a hung agent and killed
six agents mid-write (merge-01 twice at 1800s — 591 and 532 insertions across
16 and 15 files — plus scope-01/dash-04/exec-01/hlth-01 at 900s, 503/631/605/499
insertions). A hung agent leaves nothing behind; every one of those was writing
when it was cut off, and raising the ceiling 900 → 1800 changed only the size of
the loss. New `autoloop/stall.py` bounds the LACK OF PROGRESS instead: while the
worker repository keeps changing the agent runs, and it is killed only after
`agent_stall_seconds` (default 1800) with NO filesystem change at all, or at the
absolute backstop `agent_ceiling_seconds` (default 14400), which should
effectively never fire and reports itself as a finding when it does. The
observation is `git status --porcelain -z -uall` through the policy-validated
`GitGateway` plus per-path `st_mtime_ns`/`st_size` — never anything the agent
says about itself, and deliberately not a raw filesystem walk (the probe's own
`git status` refreshes `.git/index`, so a walk including `.git` would see churn
every tick and never fire). The old key is handled EXPLICITLY at load rather
than ignored: a config still naming it loads, its value migrates onto
`audit_agent_timeout_seconds` (the read-only audit path, which keeps the old
key's exact meaning — a read-only agent has no progress to observe, and a
timeout there costs a re-run rather than destroying work), an explicit
`audit_agent_timeout_seconds` takes precedence over it, and the retired name
never reaches `AuditConfig`. `load_config` stays PURE — it returns the notice
as `AutoloopConfig.migration_notices` and writes to no stream — while
`cli.emit_migration_notices` prints it on stderr once per process. That split
is deliberate: notice *content* is then asserted by ordinary tests in any
order, and only the once-per-process contract needs isolation. New
`test_stall_detector.py` (33 test functions, 34 collected — the first is
parametrized over both retired timeout values): an agent writing
steadily for 90 minutes is not killed (parametrized over BOTH retired timeout
values — this is the mutation guard, and reintroducing any elapsed bound fails
it); a 1400s pause inside a 1800s window is not a stall; silence past the window
IS killed and the report leads with `STALLED:` naming the silence, not the
elapsed time; the report carries the partial-work numbers and a stall that
produced nothing reads differently from one that produced 591 lines; SIGTERM
escalates to SIGKILL; a process that finishes between the decision and the
signal is `COMPLETED`, not stalled; the ceiling still terminates a run that
"progresses" forever and says loudly that it fired; a probe that cannot observe
the tree never triggers a stall kill (only the ceiling can end a run nobody can
see) and its report says the silence was unobserved; the probe sees one file
growing though the path set is unchanged, counts new files plus tracked
insertions, skips binaries, distinguishes a measured zero from an unreadable
repo, and carries its own shortfall when the tracked diff fails — "16 files
changed, ~0 lines written" for a run that wrote 591 would be a wrong number
presented as a measured one, which is worse than the timeout this replaced;
the supervised runner reports a stall instead of a timeout while keeping
the killed run's partial output, and a runner with no probe still passes the old
elapsed `timeout=` to `subprocess.run`; and the executor surfaces the stall,
`changed_paths` read from git, and — for ANY agent failure, not only stalls —
what was left behind. Config coverage: the retired key migrates onto
`audit_agent_timeout_seconds` while leaving the write path on the stall
defaults, with a notice naming all three replacements; an explicit
`audit_agent_timeout_seconds` wins when both are named; a config without the
retired key produces no notice; a junk value for the retired key is still
refused (it is migrated onto a live setting, so it is validated like one);
`load_config` writes to no stream and returns the same notices however many
times it runs; the CLI routes the notice to stderr, never stdout (`status` /
`tasks` / `next-task` have parseable stdout); a stall window at or above the
ceiling is refused (it would read as configured while being unreachable); and a
non-positive bound is refused.

`test_the_cli_prints_the_migration_notice_on_stderr_once` runs a real
SUBPROCESS (`sys.executable -c`, `PYTHONPATH` at the repo root) that loads the
same legacy config three times and asserts exactly one notice on stderr. It is
the one test here that cannot run in-process: the suppression ledger is
process-global by design, so any earlier test that loaded a legacy config
through the CLI would consume the single emission and leave this one asserting
against an empty stream — which is how it failed on the previous round. A
subprocess makes "this process has not printed it yet" true by construction
instead of by test ordering; the in-process sibling test resets only that
ledger, via `monkeypatch`, and never touches production semantics.

**584 tests (pre-blockers baseline), fully hermetic** — no network, no ChatGPT, no playwright import,
no live `claude` CLI (agent runner stubbed), no app DB. `test_git_gateway.py`,
`test_manifest.py`, `test_audit_executor.py`, `test_postcommit_primitives.py`,
`test_postcommit_flow.py`, `test_postcommit_review.py`,
`test_worker_publisher.py` and `test_v1_smoke.py` use real `git` against
throwaway `tmp_path` repos;
`test_lock.py` uses real separate processes. Two tests in
`test_worker_publisher.py` (the live `credential.helper` platform proofs)
`pytest.skip` if the machine running them has no ambient system/global
credential helper to demonstrate against — a precondition skip, not a
weakened assertion; both ran their full assertions (no skip) on the machine
this was verified on (Apple Git 2.39.5, macOS 26.2, ambient `osxkeychain`).

**The derived-bytecode exemption in the escape detector (2026-08-16,
`esc-01`; +13 in `test_m1_hardening.py`, new section 2b).** Three loop-fatal
`checkout_escape_detected` parks on 2026-08-15/16 were caused by CPython
bytecode caches inside the primary checkout, not by any agent (a dashboard
restart; two `health --json` polls after the loop merged a source change).
`escape_detector.is_derived_bytecode` now exempts a `.pyc` directly inside a
`__pycache__/` directory, **carrying a tag some interpreter really emits**,
whose sibling `.py` is present as a REGULAR FILE in every snapshot side the
cache entry exists on.
**`test_escape_detector_detects_ignored_content_change` is deliberately
unchanged** — the exemption's regression proof sits beside it, not on top of
it, and the sharpest of the new tests
(`test_a_real_ignored_path_escape_is_still_reported_when_bytecode_churns_beside_it`)
re-proves that same ignored-path escape in the SAME window as the bytecode
writes that used to be indistinguishable from it, which neither test proves
standing alone. The other twelve: the incident itself (a recompile with the
source untouched — also why "flag a `.pyc` only when its source did not
change in the window" would have flagged all three incidents); creation and
deletion across every name CPython emits (`.opt-N`, the `<name>.<pid>`
atomic-write temp file); seven that must STILL park (an orphan cache entry with
no sibling `.py`; a sourceless `pkg/evil.pyc` in the legacy layout outside
`__pycache__`; a symlink planted at a cache path; a `.pyo` inside
`__pycache__` beside a live source, asserted alongside a real `.pyc` rewrite
in the same window so the test proves discrimination rather than mere
noise; **a `.pyc` whose TAG is not one any interpreter emits
(`mod.attacker.pyc`), likewise asserted against a genuine `cpython-312`
rewrite in the same window — every other clause of the exemption is satisfied
there, so this is the case a dot-free-anything tag pattern would have made
silent for every sourced module in the tree**; a `.pyc` whose sibling `.py` is
a SYMLINK, which the snapshot watches as a target string and never hashes; and
a `.pyc` created in the same window that deletes its source, which the per-side
check catches and a union-of-keys check would not); the predicate's own
boundary table, including `.so` never being exempt, foreign/malformed tags
(`mod.attacker.pyc`, `mod.cpython312.pyc`, `mod.CPYTHON-312.pyc`, pytest's
assertion-rewriter name) against this runtime's own
`sys.implementation.cache_tag`, no-sides defaulting to False, and the
symlinked/one-sided source cases at unit level; the inherited effect on
`diff_worker_tree` (a
validation run's bytecode is not a mutation, a real write in the same run
still is — the first unit tests this guard has had, its other coverage being
end-to-end in `test_postcommit_flow.py`); and one end-to-end orchestrator
test at the level the incidents actually happened, where a `.pyc` written
into the primary checkout mid-round finishes the round instead of parking.
The last three negatives are asserted through `diff_snapshots` with a real
git repo, not against the predicate alone: the wiring (which `PathState`s the
diff hands the predicate, per side) is exactly what a predicate-level test
cannot see. The shared fixture `_build_worker_repos_orchestrator` gained
`__pycache__/` + `*.py[cod]` in its `.gitignore` so that scenario is IGNORED
rather than merely untracked, as in production. **71 tests in the file.**

Note: several per-file counts in the table below drifted stale before this
entry (documented counts trail actual collected counts for a handful of
files, e.g. `test_orchestrator.py`, `test_audit_executor.py`,
`test_smoke.py`) from earlier rounds that added tests without updating this
table. Not re-audited here — out of scope for this change — but flagged so
it isn't mistaken for a claim that every row below is current.

**Immediate priority edits and the fine-grained task-file mutex (2026-08-16,
`dash-04`; +11 functions in `test_tasks.py`, +6 in `test_dashboard.py` — plus
one REWRITTEN there (`test_the_post_endpoint_writes_only_to_the_inbox` →
`test_a_queued_priority_request_is_still_drained`, so it is in neither figure)
— and +4 in `test_m1_hardening.py`. Hand-counted, no shell in the worker; these
are what this change added, not a re-audit of any row's total.** Setting a priority from the dashboard queued an
inbox request, so the value became true only when the loop next drained while
the page kept re-rendering the old number — a save that worked and one that did
not were indistinguishable, and the operator resubmitted (two identical
requests sat in `~/.autoloop/inbox` on 2026-08-05). It now writes `tasks.json`
under a short-lived mutex and answers with a READ-BACK from that file. Four
properties carry it, each with tests that fail when it is removed:

* **The mutex is real and cross-process.** `test_two_concurrent_writers_do_not_lose_an_update`
  spawns a REAL `sys.executable` child (not a mock, not a monkeypatched sleep)
  that holds the mutex across load → pause → save, exactly the sequence
  `apply_priority` runs internally, while this process reads, marks a task
  completed and saves. With the mutex both survive; delete `with self.lock()`
  from `save`/`apply_priority` and the loop's save lands mid-sequence and is
  overwritten — the completion is lost, which is the failure the brief calls far
  worse than a late priority. The wall clock is asserted too (the save must
  actually WAIT), so a version that never contends cannot pass quietly.
* **It does not take `LoopLock`.** `test_a_priority_write_succeeds_while_the_loop_lock_is_held`
  (store level) and `test_a_priority_edit_lands_while_the_loop_lock_is_held`
  (over the real socket) hold the run-level lock in a separate live process,
  prove it is genuinely held (`LoopLock.acquire` raises `LockHeldError` for this
  process), and then bound the edit's wall clock — "does not block" is measured,
  not implied.
* **It changes one field and creates nothing.** Store- and endpoint-level tests
  compare every other key of every row before and after (`status`,
  `approved_paths`, `depends_on` explicitly), assert the only new file inside
  the checkout is the always-empty `tasks.json.lock`, and assert `state.json`,
  the execution records and the blockers are byte-identical. A priority edit
  against a checkout with no `tasks.json` REFUSES and leaves the file absent —
  materialising a registry from a form is a different write path.
* **Failures report.** An unknown id and a non-integer draw the registry's own
  refusal; a ledger that cannot be appended to leaves the task file untouched
  (the record is written BEFORE the write for exactly that reason). A row that
  silently snaps back is the defect being fixed, so "reports" is the assertion.

Plus the two halves that keep it out of the loop's way: `save` adopts the
on-disk priority into a registry held in memory since before the edit (with the
loop's own `set_priority` — a drained inbox request — taking precedence, and
reconciliation failing OPEN on an unreadable file, since `save` is the path
that records completions), and the escape-detector regressions in
`test_m1_hardening.py`. Those four are the pair the brief asked for and their
controls: an operator priority edit landing INSIDE the detection window
finishes the round (before this, it parked loop-fatal and cost the round); the
same field written directly into the checkout, unattested, still parks; a
widened `approved_paths` carrying a FORGED ledger record still parks, which is
what proves the exemption checks the bytes and not the record's claim; and the
mutex file is never reported as a created path.

**The attestation became a CHAIN rather than reachability (2026-08-16 revision
round, `dash-04`; +3 functions in `test_m1_hardening.py` (7 in that group), +9
in `test_tasks.py`, and one REWRITTEN — `test_a_forged_attestation_cannot_widen_
authorization` — so it is in neither figure. Hand-counted, no shell in the
worker.)** Review found the first cut's "is the after-digest REACHABLE from the
before-digest" question strictly weaker than the one the detector needs
answered, and two end-to-end regressions name the gap:

* `test_an_in_window_round_trip_does_not_authorize_a_direct_edit` — an operator
  moves a priority and moves it back, so the file ends at its baseline but the
  intermediate digest is reachable forever. The agent then writes that exact
  state. Under reachability the escape is silent; under a chain the window's
  completed mutations read A → B → A, whose TERMINAL state is A, so an observed
  after-state of B parks the round.
* `test_an_intent_from_a_failed_write_does_not_authorize_a_direct_edit` — a
  ledger record is written BEFORE the file write (that ordering is what makes an
  unwritable ledger leave the task file untouched, and the "Failures report"
  bullet above still depends on it). A write that then FAILS therefore leaves a
  record naming a state that never existed on disk. Only a `complete` record —
  appended after the bytes land — is an edge, so the agent reproducing that
  state parks the round.

Three more controls came with them, each failing if its binding is removed:
`test_records_from_an_earlier_window_do_not_break_an_in_window_edit` (the
watermark is a CORRECTNESS control, not only a tightening — without it, an
earlier round's record breaks the chain and an ordinary operator edit parks),
`test_the_same_task_file_spelled_differently_is_the_same_file` (path binding via
a symlinked spelling, so `canonical_task_path` is exercised rather than
assumed), and `test_a_state_later_than_the_observed_one_is_not_attested`, which
PINS the residual the terminal-state rule buys: a second legitimate edit landing
between the after-snapshot and the check now parks. The rewritten forged-record
test additionally asserts that its forged record really is a valid completed
chain ending at the bytes on disk — without that, a path-spelling or phase
mismatch would make it pass for a reason that says nothing about
`priority_only_change`, i.e. the byte-level half would go vacuous.

**Roadmap throughput on the dashboard (2026-08-16, `dash-05`; +14 functions in
`test_dashboard.py`. Hand-counted, no shell in the worker; that is what this
change added, not a re-audit of the file's total.)** The page listed tasks and
answered none of the three questions an operator arrives with — how much is
done, how much is moving, is the queue converging. Counting it took a script
(2026-08-06: 66 tasks, 17 completed, 23 in progress, 18 pending, 8 blocked).
`roadmap_stats` now derives the task-state counts from the `groups` payload
`collect()` already builds, i.e. from `TaskRegistry.state_of()` and nothing
else, and the summary renders above every list on the page. The in-progress
PUBLICATION subcategories beside them are a different question with a different
source — execution records plus one cached `ls-remote` — and are not claimed to
come from `state_of()`, which knows a task is in progress and cannot know where
its commit went. Four properties carry it:

* **The counts cannot disagree with what dispatches, and no word means two
  states.** One count per `TaskState`, keyed by `TaskState.value`, labelled in
  `TaskRegistry.summary()`'s own vocabulary (ready / blocked / quarantined /
  retired). `test_the_counts_are_what_state_of_reports_and_nothing_else` runs
  `state_of()` directly over the same rows, compares state by state, and pins
  the one-line summary string; `test_the_summary_is_wired_from_the_same_groups_the_roadmap_renders`
  asserts end-to-end (real checkout, real `origin`) that EVERY count equals the
  group count rendered below it, walking `STAT_BUCKETS` rather than spot-checking.
  `test_every_task_state_is_claimed_by_exactly_one_bucket` keeps the buckets a
  bijection onto the six `TaskState`s and pins each bucket's count key to its
  state's value. `test_no_word_in_the_summary_names_two_different_states` is the
  regression the first version needed: it rolled READY ∪ BLOCKED into `pending`
  and spent the freed name on BLOCKED_BY_OPERATOR, so `blocked` meant the
  quarantine at the top of the page and "waiting on a dependency" in the Roadmap
  panel below — opposite calls to action under one word. That test asserts the
  quarantine tile carries the Roadmap group's own label ("needs a human", never
  containing "blocked"), the dependency tile keeps `blocked` and names the
  dependency, each tile counts exactly its group, and `open` is the sum of the
  four non-terminal counts. `test_the_summary_renders_at_the_top_of_the_page`
  additionally asserts the template renders labels FROM the payload and spells
  no `TaskState` value itself — a hard-coded tile list is the shape that let the
  word drift in the first place.
* **The in-progress breakdown is the part that carries information.** A flat
  "23 in progress" hid twelve tasks holding unpublished candidates, each pinning
  a `task_base_sha` and so each a `task_base_behind_head` park waiting to happen
  — the failure that stopped the loop twice on 2026-08-04.
  `test_each_in_progress_sub_category_is_classified_including_a_published_one`
  drives all three (published to its side branch / holding an unpublished
  candidate / no candidate at all) and asserts the sub-counts SUM to the flat
  count, so the two cannot tell the operator different things.
* **An unreachable remote is unknown, never not-published.** The mutation test
  is `test_an_unreachable_remote_is_unknown_never_not_published`: a failed
  `ls-remote` and a remote with no such branch both produce an empty ref map, and
  reading them alike would manufacture the alarming state out of a network
  hiccup. `test_an_unreachable_origin_leaves_the_breakdown_unknown_end_to_end`
  repeats it over a real checkout whose `origin` was re-pointed at a path that is
  not a repository (fails instantly; an unroutable URL would sit on the 15s
  `ls-remote` timeout and stall the suite). The related trap has its own test —
  `test_a_record_naming_no_remote_is_read_against_the_one_that_was_polled`: most
  records carry no `intended_remote`, and reading that absence as "some other
  remote" would make the whole breakdown read `unknown` against live data while
  every other test still passed.
* **Retired tasks leave the percentage on BOTH sides.**
  `test_retired_tasks_leave_the_percentage_denominator_on_both_sides` pins
  `denominator == completed + open == total - retired` and asserts the figure is
  neither of the two wrong readings (retired-as-outstanding understates,
  retired-as-done overstates), and
  `test_a_roadmap_with_nothing_to_divide_by_reports_no_percentage` keeps 0% from
  being invented where nothing was measured.

Plus `test_the_open_work_is_broken_down_by_priority_and_by_area` (both splits
cover exactly the open tasks and exclude completed + retired; priority keeps its
own ascending order, areas lead with the biggest),
`test_the_branch_comes_from_the_record_before_the_naming_convention` (the
extracted `branch_for` helper the merge panel now shares, so the two panels
cannot name different branches for one task),
`test_an_unreadable_graph_reports_unknown_rather_than_a_row_of_zeros` (a summary
is the one panel where a fabricated zero would be believed), and
`test_the_summary_renders_at_the_top_of_the_page` — placement was the operator's
actual request, so it is asserted rather than described: the section indexes
before `#tiles`, `#progressbox`, `#merged` and `#roadmap`, every field reaches
the DOM, each in-progress state ships an icon + a word, and `renderStats` sits
INSIDE the re-render guard (unlike `renderProgress`, whose clock ticks every
poll). The served script is still parsed by the existing
`test_the_served_javascript_actually_parses`, which globs every `<script>` block
and runs `node --check` over it, so no second parse test was added.

**A fault must not spend a task's attempt budget (2026-08-17, `budget-01`; new
`test_attempt_budget.py`, 28 tests — hand-counted, no shell in the worker).** `attempt_count` was one counter paying
for two unrelated things. It is incremented before the executor runs — the M1
finding #3 property that bounds a task which dies every round without ever
reaching a reviewer — so it also charged rounds destroyed by a provider 429, a
stall-killed agent, or a process that did not survive. Six tasks reached the
ceiling on rounds nobody reviewed (brw-09 5/1, exec-01 5/1, port-01 5/3,
brw-11 4/0-1, dash-04, hlth-01), each repaired by an operator editing the
execution record by hand, and `~/.autoloop/afk-worker.sh` had to guess which
attempts were faults from `attempt_count - review_round >= 2`.

Now two budgets. `attempt_count` keeps the task's own work
(`MAX_TASK_ATTEMPTS`); `fault_attempt_count` takes rounds lost to faults, with
its own ceiling and park code `fault_attempt_ceiling`. `TaskExecution.
attempt_ledger` records `"<ordinal>|<budget>|<reason>"` per attempt — `budget`
one of `pending` / `pending_fault` (open) or `task` / `fault` (settled), and
`reason` either a bare outcome slug or `"<origin>><outcome>"` for a round a
fault forced the loop to redo — so the reason is READ rather than inferred.
Six properties carry it:

* **The split is decided from structured signals, never prose.**
  `ExecutionOutcome.fault_kind` is set only in `implement_executor`'s
  agent-did-not-complete branch, from `AgentResult.stall` and a narrow phrase
  list over `AgentResult.error` (`audit.agents.classify_agent_fault`). A bare
  HTTP status is deliberately not enough on its own —
  `test_classify_agent_fault_defaults_to_the_task_owning_its_own_failure`
  drives "AssertionError at line 429 of test_thing.py" and "502 tests
  collected" through it and pins the empty answer, because a wrongly-set fault
  would excuse a genuine failure forever while a wrongly-blank one costs a
  single attempt.
* **A structural refusal is CHARGED, and pinned to that rule.**
  `test_a_structural_refusal_spends_the_task_attempt_budget` is the decision
  this task had to make and defend: the reviewer never judged the round, which
  is the argument for exempting it, but it is a real defect the task's own work
  produced and repeating it is exactly the churn `MAX_TASK_ATTEMPTS` bounds.
  Exempting it would have removed the only ceiling on that case.
* **Reconciliation cannot relabel a finished round.** Every exit of a
  dispatched round stamps its ledger entry, so an entry still reading `pending`
  can only be a round that never reached one of its own exits — the process died
  mid-round, or a `GitError` escaped the dispatch to `_handle_git_failure`, which
  the loop already charges to `consecutive_failures` rather than to the task.
  Both are environmental, which is why either settles onto the fault budget.
  `test_reconciliation_never_reclassifies_a_finished_task_attempt` runs a
  failure, a fault and a review through, then re-runs the reconciliation and
  asserts both counters and all three classifications are untouched;
  `test_finalising_an_attempt_is_one_way_and_cannot_be_re_stamped` pins the
  same guard at the unit level. Without it, five validation failures could be
  refunded on the next restart and churn forever.
* **Consecutive faults never alternate back into the task budget.**
  `test_consecutive_session_ending_faults_never_fall_back_onto_the_task_budget`
  runs three dispatches with a session-ending fault between each and asserts
  `attempt_count == 1` throughout. It pins the defect this feature shipped with
  on its first cut: a redo was written into the ledger as an already-SETTLED
  `fault|<code>` entry, so when that redo reached the reviewer the round's own
  exit had nothing to stamp, the entry never recorded a review in flight, and
  the next fault — which matched only the literal pair `(task,
  "sent_for_review")` — declined to mark it. Its redo was then charged to
  `attempt_count`. A redo is now opened OPEN like any other round
  (`pending_fault`), `_settle_attempt` stamps it `fault|<origin>>sent_for_review`
  when it reaches review, and `_note_round_fault` keys on the OUTCOME
  (`attempt_outcome`) rather than the whole reason, on either budget. Its two
  partners keep the exemption narrow:
  `test_a_redo_that_fails_on_its_own_merits_goes_back_onto_the_task_budget`
  (a redo that ends in a structural refusal moves its charge BACK to the task —
  a redo must not launder a fresh defect into a fault) and
  `test_a_redo_the_process_does_not_survive_keeps_its_replacement_on_the_fault_budget`
  (reconciliation adds the stamp without moving a charge that was already
  correct, AND carries the recovery forward).
  `test_a_reason_carries_a_redos_origin_without_hiding_its_outcome` pins
  `compose_reason` / `attempt_outcome` at the unit level.
* **A recovery chain interrupted repeatedly stays on the fault budget, and
  still ends.** The second defect found in review: `pending_fault_code` was
  consumed by `_open_attempt` and re-armed only by `_note_round_fault`, which
  needs a SETTLED round that reached the reviewer — so a redo the environment
  took (process dead mid-round, or the agent's provider gone) left the marker
  cleared while the review was still lost, and the dispatch after it was billed
  to `attempt_count`. `_settle_attempt` rule 4 re-arms from the redo's own
  origin whenever a fault-opened round stays on the fault budget without
  reaching a review; it sits in `_settle_attempt` because all three arrival
  paths (`_reconcile_unfinished_attempts`' `interrupted_mid_round`,
  `_finalise_attempt`'s `fault_kind` and `worker_environment_drift`) share it.
  `test_a_recovery_chain_interrupted_twice_never_reaches_the_task_budget` runs
  the full shape — review → session fault → redo killed mid-round → restart →
  redo hit by a provider outage → redo reaches review — and asserts
  `attempt_count == 1` across all four dispatches, `fault_attempt_count == 3`,
  no surviving marker, and the on-disk ledger reading one origin with four
  outcomes. `test_a_recovery_chain_interrupted_forever_still_hits_the_fault_ceiling`
  is the bound: `MAX_TASK_FAULT_ATTEMPTS` interrupted redos in a row park on
  `fault_attempt_ceiling` with the executor not called, because every
  carried-forward dispatch is charged.
* **Both budgets terminate.**
  `test_the_fault_budget_terminates_a_task_that_faults_every_round` and
  `test_a_task_whose_process_dies_every_round_still_terminates` reach
  `fault_attempt_ceiling` with the executor NOT called on the parking dispatch;
  `test_the_task_budget_still_terminates_a_task_that_fails_its_own_work`
  re-checks the unchanged `attempt_count_ceiling` path against the ledger. An
  `assert_books_balance` helper pins `attempt_count + fault_attempt_count ==
  len(attempt_ledger)` at each step, which is what makes the combined bound
  (`MAX_TASK_ATTEMPTS + MAX_TASK_FAULT_ATTEMPTS - 1` dispatches) exact.

Plus: the three not-chargeable shapes end-to-end (a rate-limited round that
produced no work, a supervisor-killed agent, a round interrupted mid-flight),
the chargeable control (a completed validation failure), a round that reached
the reviewer and came back `revise`,
`test_a_session_ending_browser_fault_charges_the_redo_to_the_fault_budget` with
its narrowing partner `..._marks_nothing_when_no_review_was_in_flight`,
on-disk ledger round-trip + a pre-ledger record loading with its
`attempt_count` honoured as-is, `split_attempt` tolerance for a hand-edited
entry, and the two `answer` tests proving a `fault_attempt_ceiling` answer
resets ONLY the fault counter (preserving `candidate_sha`, `review_round`,
`last_revise_feedback` and every ledger entry) while an `attempt_count_ceiling`
answer refills nothing — the second of those runs against a task that was never
quarantined, because plain `run` parks task_fatal without
`cli._handle_parked_task`, so `registry.unblock` raises and a reset placed after
it would be skipped in exactly the case it exists for.

**A send that never appears is a wedged conversation, not a browser fault
(2026-08-18, brw-12; new `test_conversation_retirement.py`, 14 tests).**
Observed 2026-08-17: a submission the loop believed sent (`submitted=True`,
`send_attempted=True`) never appeared, the conversation sat at 33 messages for
ten minutes, and the symptom surfaced as a locator timeout on the loop's own
message — read as a lost session, answered with a Chrome restart every 45
seconds, which cannot help because the browser was fine (12 CDP targets, an
operator posting by hand in another chat). The classification fires from BOTH
surfaces the fault wears. `BrowserChatGPT._rule_out_missing_submission` runs
when the response-START bound expires with the request absent from the
mounted window, and it is ALSO reached when a mid-await DOM read dies with
the lost-session label itself — `_classify_awaiting_read_failure` catches the
`SessionLostError` inside `await_response`, re-probes through the same
session (a fresh read succeeding is the attachability proof), and only then
rules. Either way the tail is mounted with the SAME two proofs
`find_conversation_with` requires (list demonstrably at its end, window then
unchanged), and only settled absence on an attachable, un-throttled,
logged-in page raises
`ConversationUnusableError(code="submission_never_appeared")` — which the
existing `_handle_conversation_unusable` answers with rotation, no restart,
no `consecutive_failures` charge (the rotation `reason` carries the error's
`code`, so the record distinguishes a vanished submission from a chat that
would not load). Ten client-level tests: proven bounded absence raises with
the new code and a `submission-never-appeared` diagnostic, from the clean
timeout AND from the exact reported locator-timeout read failure; a read
failure whose probe SIGHTS the request re-raises the original
`SessionLostError` (transient fault, restart path intact); a browser whose
probe cannot read the page at all re-raises it too (brw-11's state 3); a
read failure whose absence cannot settle (no position signal) re-raises it as
well; a request hidden by the unmounted tail is SIGHTED by the mount and
falls back to the ordinary `stage="start"` timeout (no rotation); an adapter
that cannot report a scroll position can never establish absence and also
falls back; a request visible in the window spends no gestures at all (the
silent-conversation trigger's case, untouched); a throttle overlay arriving
mid-mount — or discovered by the read-failure probe — is still routed as
`RateLimitedError`. Four orchestrator-level tests: the fault rotates without
restarting (a restart command IS configured, so one would be visible) and
without touching `consecutive_failures`/`browser_restart_skips`; the exact
reported shape runs END-TO-END through the real `await_response` code over a
scripted page (`IncidentAwaitClient`) and rotates with no restart and no
budget increment; the rebinding survives a process restart with
`last_rotation.reason == "submission_never_appeared"`; and a
`SessionLostError` that ESCAPES the client still takes the restart-and-budget
path — which after this change can only mean the client's own probe could do
no better (brw-11's dead-browser boundary is not swallowed).
**Worker reuse at dispatch (wrk-01, 2026-08-18; new `test_worker_env.py`, 13
tests — hand-counted, no shell in the worker).** A resumed round must reuse
its worker repository, not recreate it. Seven unit tests pin the probe
(`worker_env.worker_repo_is_reusable`): True only for an existing directory
that is itself the top level of a git repository with exactly the recorded
branch checked out — a missing directory, a plain directory, a subdirectory
inside someone else's repo (`--show-toplevel` compared back against the
path), the wrong branch, a detached HEAD, and an empty recorded branch are
all False. Six dispatch tests pin the guard in
`_dispatch_task_postcommit`:
`test_a_second_dispatch_reuses_the_existing_worker_with_no_new_clone`
(one `WorkerRepoManager.create` across two dispatches — counted by
shadowing the method — same path, same branch, nothing quarantined, round
2's candidate contains both rounds' files);
`test_partial_uncommitted_work_in_the_reused_worker_survives_a_resumed_dispatch`
(THE incident the task exists for: a tracked partial edit and an untracked
partial file left in the valid recorded worker between dispatches survive
the resumed dispatch — still one `create` total, same path/branch, no
`worker_quarantined` or `worker_recreated` transcript entry, the resumed
executor sees both edits byte-for-byte on entry, and the round completes
with the partial work carried into the new candidate; the reuse decision
is passed to `_prepare_write_capable_worker` as
`reused_recorded_worker=True`, which skips ONLY the dirty-residue
quarantine and only for this gate);
`test_a_reusable_worker_with_a_stale_base_is_kept_not_rebuilt`
(the decision is made BEFORE `_rebase_execution_if_stale`: an interrupted
round — `review_round` 0, no candidate — whose recorded `task_base_sha`
falls behind mainline between dispatches keeps its valid worker, still one
`create` total, no `execution_rebased`/`worker_quarantined`/
`worker_recreated` entries, the record's base unrewritten, the skip logged
as `execution_rebase_skipped_worker_reused`, and the partial work carried
into the resumed round's candidate; `_rebase_execution_if_stale`'s
reviewed-record reconcile/park branches are untouched, and
`test_a_pending_retry_survives_the_reconciliation_of_its_own_record` plus
all of `test_rebase_stale_base.py` — which call the method directly, where
`worker_reusable` defaults to False — pass unchanged);
`test_a_missing_worker_directory_is_recreated_by_the_existing_creation_path`
(fallback is the SAME `create()` call, at the RECORDED base onto the
RECORDED branch, `worker_recreated` in the transcript, record not
rewritten); and the two bounds tests (wrong branch / non-git directory):
not reuse cases, the fallback `create()` refuses with its existing
"already exists" error, and the path is left byte-for-byte as found — no
repair, no deletion, no branch switch, no attempt charged. Two
`test_m1_hardening.py` tests were UPDATED for the reuse gate rather than
weakened:
`test_failed_attempt_residue_absent_from_the_next_candidate` now proves
the fail-closed remainder of M1 finding #3 — an executor that does NOT
adopt the reused worker's residue still cannot leak it into the candidate
(`commit_and_capture` stages exactly the reported paths) and the round is
refused at post-commit review ("worktree is not clean after commit") with
the residue preserved in place, not quarantined; and
`test_quarantine_recreate_resumes_from_a_candidate_sha_that_only_exists_in_the_quarantined_repo`
now calls `_prepare_write_capable_worker` directly with the flag left
False, because a dispatch whose recorded worker passes the reuse probe no
longer routes into the quarantine branch at all — the branch (and its
candidate-sha fetch-source fix) is retained unchanged for preparations
that did not pass the gate.

Run: `pytest autoloop/tests` from the repo root to run only this tree.

**Included in a bare `pytest` since 2026-08-04 (rt-05).** Root `testpaths` is
now `tests autoloop/tests`, so a bare root run collects both trees. It
previously pointed at `tests/` alone, which meant a bare `pytest` reported a
green root suite while this entire suite had silently not been collected — the
default surface said nothing about what it was skipping, so nobody had to
choose the exclusion for it to hold.

Two properties made merging safe, and both are worth re-checking before adding
any third tree: this suite imports only `pytest` and the stdlib (`playwright`
is imported lazily inside functions and faked in tests, so a machine without it
still collects), and it derives every state dir, worker root and lock path from
`tmp_path` — including `test_v1_smoke.py`'s deliberately RELATIVE `.autoloop`,
which runs under `monkeypatch.chdir`. Nothing here can reach the real
`~/.autoloop`, so a bare `pytest` cannot disturb a loop that is mid-round.
The cost is wall-clock: real `git` and real subprocesses make a bare root run
take minutes.

As of 2026-08-01 this suite is **790 passed, 1 skipped** (~2m26s serial). The
one skip is `test_real_db_validation_command_succeeds`, which needs an
operator-supplied dedicated test database — see its row below. Every other
test is hermetic (no network, no database, no real `claude` CLI).

That 790 is a MEASURED figure from 2026-08-01 and is now behind: the
2026-08-02 rows below (`test_playwright_driver.py`, `test_failure_digest.py`,
`test_pause_location.py`, `test_recovery_commands.py`, `test_start_command.py`,
`test_recovery_paths.py`, and `test_dashboard.py` 13 → 19) all landed after it.
Deliberately not replaced with an arithmetic guess — re-run the suite and
record what it actually reports rather than trusting a summed total.

**Every task is decomposed and the decomposition approved before any code is
written (2026-08-18, `plan-01`; +11 functions / 22 collected in
`test_contract.py`, +6 / 8 in `test_policy.py`, +7 in `test_tasks.py`, +4 in
`test_orchestrator.py`, +2 in `test_implement_executor.py`, +11 in
`test_context.py`. Hand-counted, no
shell in the worker; these are what this change added, not a re-audit of any
row's total.)** Operator decision
of 2026-08-17, unconditional: the tasks that failed were not obviously large
when filed (prov-01 arrived at 12,020 characters asking for five things;
brw-11 was amended from 2,900 to 6,505 mid-flight and then took five revise
verdicts), so a rule that depends on the author noticing size is the rule that
already failed.

**The approval rides on the `implement` directive the loop already exchanges,
and costs no extra round.** That is the design decision, and it is why there is
no new phase, no new request kind and no new state: the loop already asks what
to work on and already receives `implement` before any agent runs. A dedicated
plan round would add one round to EVERY task, against the one to three that
tasks currently take — a 30-100% tax on the common case, paid to catch the
occasional oversized one.

Six properties carry it, each with tests that fail when it is removed:

* **Shape is the parser's job, requirement is policy's.** `contract`
  parses `decomposition` (approach, files, steps) when it is present and
  forbids it on every decision that is not implement/revise; it never
  REQUIRES it. Same layering `TaskSpec.approved_paths` already uses, for two
  reasons the tests state: requiring it in the parser would be a breaking
  wire change (PROTOCOL_VERSION stays 3), and it would answer a missing plan
  out of the small parse-retry budget instead of the denial that explains
  the rule.
* **One step is an accepted outcome, pinned at all three layers** — parser,
  policy and the rendered text, which reads back "This is one step:" rather
  than a list of one. Over-splitting is what turned one capability into ten
  tasks with four already implemented, so "this is one step" being refusable
  anywhere would cost more than the rule buys.
* **Refusal happens before anything is spent.** `_step_executing` authorizes
  before `_dispatch`, so `test_implement_without_a_decomposition_never_starts_
  the_task` asserts the task is still READY, the executor was never called and
  no `TaskExecution` — hence no `attempt_count` — exists. A plan produces no
  commit, so it must not consume the budget that bounds commit attempts; the
  refusal spends `state.policy_denials` instead.
* **The approved plan is durable and readable by the round that implements
  it.** `TaskRegistry.set_decomposition` writes it in the same save as
  `mark_in_progress` (so a task is never in progress against an unrecorded
  plan), it survives a `TaskStore` save/load, and
  `implement_executor._agent_prompt` shows it to the agent labelled as
  approved. A `tasks.json` written before the field existed still loads.
* **`revise` reuses the stored plan, but is not exempt from the gate.** It
  names a task exactly as `implement` does, and `_check_task_reference` admits
  one on a task that was never implemented — after which `_dispatch_executor`
  marks it in progress and runs a write-capable agent, so exempting `revise`
  left a route to starting a task with no plan that a reviewer never had to
  notice. The rule is therefore "the directive carries one OR the task already
  holds one", for both decisions: the ordinary revise-after-implement passes
  carrying nothing, only revise-before-any-plan is refused, and demanding the
  plan again every round would tax the rounds that are already going well. A
  reshape replaces rather than merges and is allowed on an in-progress task,
  precisely because a task under review is in progress by definition; blank is
  refused rather than treated as "clear it", so a reshape cannot silently
  un-approve a task. A `decomposition` sent with `revise task_id="audit"` is
  REFUSED rather than accepted and dropped — the audit is not a roadmap task,
  so nothing would store or apply one.
* **Every actionable request carries what the decision needs** (added by the
  first revision round). The gate is only answerable if the request carrying it
  is self-contained, and it was not: `roadmap` offered the next READY task as an
  id and a title, and no review packet showed the stored plan — so a reviewer
  that is fresh, rotated onto a new conversation or switched to the fallback
  provider had to guess a task's files and steps before the first dispatch, and
  on a later `revise` could not tell whether its feedback fitted the approved
  plan. `context.py` now renders both briefs into the block every request
  already carries (`test_context.py`'s brief section, plus two end-to-end pins
  in `test_orchestrator.py`): `next_ready` with the task's FULL description and
  its effective scope including trackers, and `in_review` with the stored
  decomposition VERBATIM — a paraphrase would make "fits the plan" and "needs a
  reshape" indistinguishable, which is the decision the section exists to
  support. Rendered in the shared CONTEXT block rather than per payload
  template, so no template can forget it, and appended strictly after the stamp
  lines because a description is text this package did not author
  (`docs/SECURITY.md` S33). The description is carried on the READY side only:
  CONTEXT is not chunked, a review request already holds a diff, and restating
  it there would double the largest requests. **The schema is pinned to the key
  that actually parses** — the response format documented `{approach, files,
  ordered steps}` while the accepted key was literally `steps`, so a reviewer
  copying the documentation would have spent the small parse-retry budget on a
  correction the instructions caused, on a field that is now mandatory. The
  pin asserts the documented set equals `contract._DECOMPOSITION_KEYS` and that
  `ordered steps` draws `unknown_keys`, rather than a substring a reword could
  satisfy vacuously.

No second split mechanism was added: the steps are prose in the same category
as `Task.description`, nothing dispatches per step, and splitting a task
remains `split-01`'s atomic mechanism across the registry, the execution record
and the worker repo.

**A task's full description on the dashboard, expandable (2026-08-18,
`dash-10`; +8 functions in `test_dashboard.py`, plus two helpers
(`pure_roadmap_js`, `run_js`) that are in neither figure. Hand-counted, no
shell in the worker; that is what this change added, not a re-audit of the
file's total.)** The roadmap panel sent `id`, `title` and `priority` and
nothing else, so the one question an operator has about a queued task — what
does it actually say — could only be answered by opening `.autoloop/tasks.json`
by hand, and that is the file the whole roadmap is steered from. Each live row
is now a `<details>`: ordinal, id, title, priority chip, "waits on" chip, char
count, and the complete description in a `pre` with `white-space:pre-wrap`.

Four properties carry it, and each is asserted rather than described:

* **The description is carried WHOLE.** `test_a_tasks_full_description_reaches_
  the_page_untruncated` drives a >5,000-character description through
  `collect()` and asserts EQUALITY, not a prefix: a truncation is invisible on
  the page, because a cut description reads exactly like a task that really is
  that short — which is the failure being fixed, not a smaller version of it.
* **It is ESCAPED, and the test RUNS the escaping** rather than grepping the
  template for `esc(`. `tasks.json` is untrusted input to this page (anyone who
  can write that file can write `<script>`), and a template that escapes the
  title and forgets the description passes every string check. The page's pure
  row helpers sit between `PURE_ROADMAP_START`/`PURE_ROADMAP_END` markers,
  `pure_roadmap_js()` lifts them verbatim, and `run_js` executes them under node
  against a hostile description — skipping, never faking, when node is absent,
  for the same reason `test_the_served_javascript_actually_parses` does.
* **The ordinal is pinned against the real `next_ready()`**, not against a
  repeat of its sort key: `test_the_ordinal_is_the_position_next_ready_would_
  pick` runs the actual `next_ready()`/`mark_completed()` loop on a registry of
  its own and demands the same sequence, the same shape
  `test_the_ready_group_is_in_next_ready_order` already uses.
* **A task that cannot be picked has NO ordinal** and names what it waits on.
  The blocked fixture gives `b-1` the BEST priority in the roadmap, so a number
  beside it would claim the loop picks it first; `waits_on` lists only the
  INCOMPLETE dependencies and comes from the same `_waiting_on` the prose
  `detail` is formatted from, so the chip and the sentence cannot disagree. It
  is populated for the BLOCKED group only — `state_of` says in as many words
  that a retirement usually still declares the dependencies it was planned
  with, so filling it in unconditionally would hang a "waits on" chip on a task
  that waits on nobody, which is the misread `TaskState.RETIRED` exists to end.

Two more pin the parts a screenshot would otherwise be the only record of: the
`--rm-*` tokens are declared on bare `:root`, under the guarded dark media
query and under `:root[data-theme="dark"]` (so the toggle beats the OS setting
in both directions), the priority BAND is its own ramp and never a
`--good`/`--warning`/`--critical` role (a p0 is urgent, not broken), and the
page stays self-contained — no `<link`, `@import` or external `src`. The search
box is static markup re-rendering through the SAME function a poll uses, so a
filtered panel and a polled one cannot disagree, and open rows are restored
from `RMOPEN` because a successful priority save clears `LASTJSON` to force a
rebuild — without it, editing a priority would snap shut the row being read.
`/api/priority` and its handler are untouched; this change is display only.

The contract text grew by a measured 140 characters and was paid for with 150
freed by compressing prose that states the same rules in fewer words, so
`test_contract_stays_within_its_budget` keeps its 3,700 ceiling untouched (net
-10) and every content test above it still passes. The arithmetic is recorded
in that test's own docstring, including the one compression that was reverted
rather than kept for its 18 characters, because it would have changed a rule
instead of shortening it.

| File | Count | Covers |
|---|---|---|
| `test_contract.py` | 80 | Contract v3 parsing: valid form for every decision (plan batches, task-id work, `reviewed` stamps, **required non-empty `commit.paths`**); strict rejection codes; last-json-block-wins; `verify_review` accept + all three mismatch codes. **Strict envelope (2026-07-30):** the byte-exact live-captured `'JSON\\n{...}'` form parses (rendered fences carry no backticks in `innerText`), as do plain/lowercase/mixed-case labels, whitespace, canonical fences, prose *around a fence*, escaped quotes/backslashes and braces inside strings. **Rejected, never positionally resolved:** two fenced blocks, two bare objects, two contradictory directives, noise-then-approval and approval-then-noise, trailing instructions, prose around a bare object, arrays, schema-invalid objects, malformed JSON, prose-only. **+9 on 2026-08-14 for `NEXT_WORK_PREFERENCE`** — the finish-before-start scheduling preference, the first of the two advisory paragraphs in the instructions (the second landed 2026-08-15, below). Its content is pinned the way the other prompt-text tests pin theirs: it states the preference and which decisions it ranks; it keeps all three start-anyway conditions (nothing in flight / blocked on something external / the operator asks — parametrized, so losing any one fails on its own); it reads as advisory, not a refusal; it cites the `in_flight` counts (the mutation guard — drop the counts and the rule points at numbers that are not in the block); it does NOT restate the separate audit-vs-ready-work rule; and it is actually shipped inside `CONTRACT_INSTRUCTIONS`. **Budget re-measured rather than derived:** the clause is 400 chars (own ceiling 420) and the whole text 3,214 (ceiling 3,240, replacing 2,850) — hand-summed line lengths, the method first validated by reproducing the recorded 2,812 for the unchanged part exactly. **+11 collected on 2026-08-15 for `AUDIT_VS_READY_PREFERENCE`** (9 functions, one parametrized over the three conditions) — the second advisory clause: prefer ready roadmap work over a fresh audit. Same pinning shape as the clause above, and it exists because the reviewer kept choosing an audit while the queue had work in it (observed 2026-08-05: a synthetic audit unit running with 15 ready tasks, six at priority 1), which grows the backlog because an audit ADDS findings. Pinned: the rule is stated as a rule ("While any task is ready", `implement` "over `audit`") with its reason; all three audit-anyway conditions survive (no task ready / every ready task blocked on something outside the roadmap / the operator asks — parametrized, so losing one fails on its own, and each fragment sits inside one line of the shipped text so a re-flow cannot break the pin for a reason unrelated to the rule); it reads as advisory, not a refusal — **the preference is deliberately NOT encoded as a `policy.py` denial**, which would park the loop instead of redirecting it, so there is no policy test to pair with these; it cites the `roadmap` line and both counts (the mutation guard — drop the ready/priority-1 counts and the rule points at numbers that are not in the block); it does not restate the in-flight rule; and it ships inside `CONTRACT_INSTRUCTIONS`. **The two functions added by the same-day revision are lifecycle guards on the clause's own wording**, each pairing an absence check with a positive anchor so it cannot pass vacuously against an empty string: the clause recommends `implement` and never `revise` (the protocol's directive for a READY task is `implement`; `revise` targets already-started work and is phase-gated, so recommending it would name a directive invalid for exactly the tasks the READY count describes), and it never calls a READY task dependency-blocked (`TaskRegistry.state_of` returns BLOCKED until every `depends_on` is complete, so the audit-anyway escape hatch has to name the unmodelled/external blocker instead — "outside the roadmap"). **Budget re-measured the same way:** the clause is 438 chars (own ceiling 470), hand-summed again (no shell in the worker) — 449 as first written, 11 fewer after that revision. The total ceiling moves 3,240 → 3,700 and stays there, derived from the previous assertion's GUARANTEED bound plus the 451 the clause and its join added when first written (440 now) — deliberately not from the recorded 3,214, which is a hand count nothing re-verified: trusting it would land on 3,690 and leave the suite one character from failing if that record was off by its own margin. Count documented as 80 before this row was touched and known to trail the collected figure (see the staleness note above the table); the `+N` figures in this row are what each change added, not a re-audit of the total, and no total was guessed at here. **+1 on 2026-08-16 (auto-03):** every decision in `RETIRED_DECISIONS` still PARSES, parametrized over the set rather than naming `ask_user`. The existing pair of legacy-`ask_user` parse tests pin that one shape (with and without `question`); this pins the rule they are instances of, so a later retirement cannot be implemented as "delete the enum member" — which would answer an in-flight conversation still holding the old instructions with `unknown_decision`, spending the parse-retry budget on a correction that never says the decision was retired, instead of the policy denial that does. **+4 functions / +18 collected on 2026-08-16 (auto-06) — the `question` FIELD, which the decision-level retirement tests do not reach.** `question` survives in `_TOP_LEVEL_KEYS` for one reason only: a legacy `ask_user` carrying one must reach the policy denial rather than die at `unknown_keys`. Two new pins keep that tolerance from leaking. (1) The instructions must not document `question` — a re-added `question (optional) ...` line names no retired *decision*, so `test_contract_never_offers_a_retired_decision` passes straight through it, yet it would advertise a field no advertised decision accepts; paired with a positive anchor (`notes`, the optional field that IS documented) so it cannot pass vacuously against a truncated text. (2) `question` draws `unexpected_field` on every ACTIVE decision, generalizing the pre-existing `stop`-only case to the whole set. The payload table those two parametrize over is deliberately COMPLETE per decision — `question` is the LAST field `parse_response` checks, so an under-specified payload would fail earlier on `missing_field:task_id`/`:tasks`/`:reviewed` and pin the wrong rule — which is why the third and fourth functions exist: one asserts the table covers `ACTIVE_DECISIONS` (a new decision cannot be silently unexercised), the other is the positive control that each payload parses cleanly on its own, so the rejection above is attributable to the added `question` and nothing else. Counts are hand-counted (no shell in the worker) and are what this change added, not a re-audit of the row's total. |
| `test_policy.py` | 55 | Directive authorization (git gating, protected branches, task-reference checks, **phase gate**: implement/revise-of-task denied by default, revise-of-audit allowed); git whitelist (**`add -A` denied, `restore` only with `--staged`**, force-push escape-hatch absence); budgets. **+1 on 2026-08-14 (brw-03):** `check_browser_restart_skip_budget` is a SEPARATE budget from `check_failure_budget` — spending one leaves the other untouched, which is the property that stops a restart the cooldown refused from killing a session (see `test_rounds_and_restart.py`). **+1 on 2026-08-16 (brw-09):** the same property for `check_rate_limit_backoff_budget`, asserted against BOTH neighbours — a wait against ChatGPT's account throttle spends neither the failure budget nor the restart-skip one, because a server-side limit is evidence about the account and never about the transport. Count documented as 54 before this row was touched and known to trail the collected figure (see the staleness note above the table); not re-audited here. **+2 collected on 2026-08-16 (task-retire-01, one function parametrized over implement/revise):** a RETIRED task is denied `task_retired`, with the successor named in the reason. This is the gate every `TASK_DECISIONS` directive passes through, and the test exists because the six retirements were previously stored as `blocked` and denied as quarantined — giving retirement its own state without restating the denial here would have quietly made six superseded tasks dispatchable, i.e. a strictly worse roadmap than the confusing one it replaces. **+5 functions / +19 collected on 2026-08-16 (auto-03) — the `ask_user` retirement, re-pinned as a property of RETIRED DECISIONS rather than of one enum member.** The four pre-existing `ask_user` denial tests are now parametrized over `contract.RETIRED_DECISIONS`, so the next retirement inherits "no config, no branch and no task reference admits it" instead of shipping with none of it; what is genuinely decision-specific stays pinned by name — `legacy_ask_user_retired` is asserted as a literal in its own test, because that string reaches the loop log, the blocker record and `docs/AUTOLOOP.md`, and the set-membership assertions elsewhere would happily survive a rename. New: the verdict `authorize_directive` returns is EQUAL to `policy.retired_decision_verdict(decision)`, which is what pins the two catch sites together (that helper is also what `orchestrator._dispatch`'s retired branch emits — they each carried their own copy of the text before, differing by one semicolon); and `_RETIRED_DENIALS` covers every retired decision, so the fail-closed generated fallback inside that helper stays the safety net it is meant to be rather than the live path. **The other half is that the retirement is surgical**, which is the part no existing test covered: across a five-config matrix (default, phase-gate-lifted, fully permissive on `main`, commit+push disabled) and four task references, no ACTIVE decision ever draws a retired code, and no denial reason any of them can produce names a retired decision — generalizing `test_phase_gate_reason_does_not_offer_ask_user`, which pinned one reason under one config, to every reason `authorize_directive` emits. Counts are hand-counted (no shell in the worker) and are what this change added, not a re-audit of the row's total. |
| `test_tasks.py` | 35 | Task registry/graph: ids, duplicates, unknown deps, cycles, batch atomicity, derived ready/blocked, `next_ready`, lifecycle guards, persistence. **+11 functions on 2026-08-16 (dash-04) — the immediate priority write, the fine-grained mutex and stale-memory reconciliation** (hand-counted; `store_with_ledger`/`spawn`/`wait_for` are helpers and are in neither figure); two of the eleven spawn REAL child processes (a competing writer, a `LoopLock` holder). The full account is in the dash-04 entry above this table. **+3 on 2026-08-15 — `summary()` now breaks the READY count out by priority**, because `contract.AUDIT_VS_READY_PREFERENCE` asks the reviewer to weigh how much ready work is queued and how urgent it is, and a rule written against a number nobody renders is decoration. Pinned: `3 ready (2 at priority 1)` with a priority-1 task that is BLOCKED counting as neither ready nor urgent (it is not work the reviewer can pick); an untriaged roadmap reports `0 at priority 1` rather than treating the default 100 as urgent; and an empty ready queue states `0 ready (0 at priority 1)` explicitly. The existing count assertions are substring-based and unaffected; `test_context.py` owns the other half — that the numbers survive into the rendered CONTEXT block.  **+1 and two rewrites on 2026-08-04 — `TRACKER_PATHS` widened to six.** `CLAUDE.md` and `docs/SCHEMA.md` joined, each earned by a real refusal (rt-06's stale test count, rt-02's migration-table row) rather than guessed at. The exact-set test is the intended way to notice a widening, so it was updated deliberately, and its `startswith("docs/")` half retired — CLAUDE.md lives at the repo root, and the property that matters is "a document, not code", which the directory was only ever a proxy for. The new case pins the sharpest entry: CLAUDE.md is the INSTRUCTIONS future agents read, so it asserts the non-circularity that bounds it — an unscoped task still gains nothing, and a scoped task's own paths still come from the Task, so a CLAUDE.md edit can never widen the editing task's scope. `test_trackers_do_not_authorize_code_outside_the_task_scope` kept its assertion and swapped its example: CLAUDE.md was listed there as out-of-scope and is now a tracker.  **+22 functions / 30 collected on 2026-08-16 (task-retire-01) — `TaskState.RETIRED` and `Task.superseded_by`.** The property under test is that three states which all mean "not running" stay three states: BLOCKED resolves itself, BLOCKED_BY_OPERATOR resolves when a human answers, RETIRED resolves for nobody. Lifecycle: `retire` records the successor and leaves `blocked_reason` untouched (nothing is deleted — the chain is regression history), takes a new reason and several successors when given them, accepts an IN-PROGRESS task (dash-01's exact shape: in_progress at dispatch, no candidate, no record — a pending-only guard would have refused the one task that most needed this) and a QUARANTINED one without a trip through READY (which would expose it to the loop mid-retirement), and refuses only `completed`. Derivation: a retired task is RETIRED even with every dependency complete AND when its dependencies are incomplete — the branch sits before the dependency scan, like `blocked`, or a retirement would read as "waiting on brw-01" — and it never appears in `ready_tasks()`/`next_ready()`. The converse is pinned too and is a real consequence rather than an oversight: a task DEPENDING on a retired one stays BLOCKED, because retirement does not make a prerequisite happen. Refusals: `mark_in_progress` and `mark_completed` both raise `task_retired` (defense in depth behind `policy._check_task_reference`, whose own test is in `test_policy.py`) with the successor named in the message; `unblock` raises `task_retired` rather than the generic "not blocked", since `answer` calls it and an operator would read the generic message as a bug; `release` still refuses it (a retirement must not be launderable back into the queue by the recovery command). `summary()` counts retired separately from quarantined — folded together, the roadmap line tells the reviewer that superseded work is waiting on someone. Shape validation is parametrized over a bad id, a self-reference, a repeat, a bare string (which would otherwise iterate character by character), `None`, `""` and a non-sequence; a successor that does not exist is explicitly ALLOWED (brw-06 was split into brw-07 + brw-08 before either was planned — a supersession is a record, not a schedule, so it is neither a dependency nor part of the cycle check), and creation validates the same field through the same function. Persistence: the field round-trips as a TUPLE (JSON has no tuples; miss the conversion and it reloads as a list that compares unequal to everything else), and a `tasks.json` written before the field existed still loads. **The migration is the half with teeth** (`_RETIREMENTS`, a code-resident data migration because the live `tasks.json` is loop state outside this repository): all six pre-state retirements are re-filed on load with the successors read from their own reasons — brw-05 records brw-02/brw-04 rather than brw-06, because that is what ITS reason says, and dash-01 records NO successor because it went stale rather than being replaced — with every reason preserved verbatim; `audit-0003`, the one genuine failure among the seven, stays BLOCKED_BY_OPERATOR, and a migration that swept it up would delete the only row on that list anybody has to act on; a listed id whose reason no longer matches is left alone (the self-limiting guard — a revived brw-02 quarantined again for a real reason must not be silently re-retired); only a `blocked` row is touched, parametrized over pending/in_progress/completed (the idempotence guard); and the whole thing survives a save-and-reload unchanged.  **+11 functions / 20 collected on 2026-08-16 (task-retire-01, review round 2) — the record has to survive a SECOND `retire`, and a stored row has to be checked at all.** Both are ways the chain gets deleted by the code that exists to keep it. *Written once:* `retire` used to end in an unconditional `task.superseded_by = successors`, so a bare `python -m autoloop retire brw-02` assigned `()` over `('brw-06',)` — the reported call, pinned by name. A repeat that says nothing is a no-op (successors AND reason intact, and separately for a task retired with no successor, which must not be the moment an invented one gets written); a repeat that would add, replace or REORDER the successors is `task_already_retired` with the recorded chain named in the message, parametrized over all three; a repeat that would reword the reason is refused the same way. `block` stays idempotent in the other direction on purpose — a quarantine is a live question that can re-fire, a supersession cannot. *Persisted rows:* `from_dict` bypasses `add_many` by design, so it is the ONLY gate a stored or hand-edited row passes, and it ran a bare `tuple()` over this field — the bare string `"brw-06"` loaded as six single-character successors, silently, and flowed on into the dashboard and the next save. Malformed values now fail closed as `StateCorruptError` (the registry's normal answer to a file it cannot trust), parametrized over a bare string, `null`, a bad id, a self-reference, a duplicate, a non-sequence, `[None]` and a dict; the bare-string case is pinned separately because it is the silent one, `TaskStore.load` is shown reporting rather than dropping it, and a well-formed chain still loads. *And the last un-retire path:* `block` ends in a bare `status = "blocked"`, so it would have put a superseded row back under "needs a human" with the chain still attached — it now raises `task_retired`, with the state, successors and reason all re-read afterwards. Unreachable by design (a retired task cannot be dispatched, so it cannot park), which is why `_handle_parked_task` fail-closing to loop_fatal on the refusal is the right answer rather than something to smooth over.  **+20 functions / 36 collected on 2026-08-16 (inbox-02) — the operator mutators behind the inbox's new vocabulary.** (Counts hand-counted, no shell in the worker; they are what this change added, not a re-audit of the row's total.) *Parity with creation, the reason the validators were extracted:* bad scopes and bad dependency lists are parametrized and asserted to draw the same code AND the same MESSAGE from `add_many` and from the mutator, which is what makes the sharing load-bearing (a re-implemented check would word its refusal differently). The scope cases cover the duplicate rule specifically, because it lived inline in `add_many` and nowhere else — a mutation reusing only the singular `_validate_approved_path` could have written a scope creation refuses — and the bare-string case pins the per-character split both fields used to allow. *Semantics:* both setters REPLACE rather than merge (a merging mutator can only widen, making a mistaken scope uncorrectable), clearing `approved_paths` to `()` leaves `effective_approved_paths` empty so revoking parks the task instead of silently granting the trackers, and a dependency change re-drives the DERIVED ready/blocked state with no second write. *Atomicity* is asserted over the whole serialised graph, not the one field: the rejected duplicate is the SECOND entry (so a validate-as-you-assign ordering would already have written the first), and the rejected cycle is one only `_check_acyclic` can see (every id known, no self-edge), which kills the assign-then-check-then-revert implementation. *The strand guard* is parametrized over all three content mutators — refused on `in_progress`, refused on `completed`/`retired`, ALLOWED on `blocked` (correcting a quarantined task is exactly what usually has to happen before its blocker can be answered), with `set_priority` shown still working on a running task since it only orders `next_ready()`. **The sharpest one is `test_the_strand_guard_reads_stored_status_not_the_derived_state`:** `state_of` tests dependencies BEFORE the in_progress branch, so an in-progress task with an incomplete dependency reports BLOCKED and a `state_of`-based guard falls silent on precisely the stranded case — built through `from_dict`, which is where that shape really comes from, since it deliberately bypasses `add_many`. *Operator holds:* the pair round-trips and clears the reason; the reverse REFUSES a loop-raised quarantine (otherwise anything able to write to the inbox could return a quarantined task to `ready_tasks()` with its blocker still open) and the recorded reason is re-read to prove it survived; a hold cannot overwrite an existing quarantine's reason — which is the previous guard laundered away, since it would also stamp the row as inbox-releasable; a hold is refused on a running task and without a reason; the terminal refusals are shown DELEGATED to `block`/`unblock` rather than re-implemented; and the hold survives a save/reload, so a restart does not strand it.  **+4 functions / 7 collected on 2026-08-16 (inbox-02, review round 2) — provenance had to become a stored field, and the tests that existed could not see why.** (Hand-counted, no shell in the worker: three plain functions plus one parametrized over four stored shapes. `test_an_operator_hold_survives_persistence` was rewritten, not added, so it is in neither figure.) The reverse was gated on `blocked_reason.startswith(OPERATOR_HOLD_PREFIX)`, and `blocked_reason` is unconstrained free text ordinary quarantines write too, so `block("t", OPERATOR_HOLD_PREFIX + …)` produced a REAL quarantine the inbox would release with its `blockers.Blocker` record still open — the load-bearing claim of the row above, falsified by a park detail that merely begins with the right characters. `test_a_loop_quarantine_whose_reason_reads_like_a_hold_is_still_refused` drives exactly that reason, and its middle assertion is the one that pins the fix rather than re-testing the refusal: `hold_origin` is `""` after a `block()` whose reason IS the prefix, so the marker is written by one method and never inferred from text; the recorded reason is re-read intact, and a genuine hold is shown still round-tripping so the guard is not simply refusing everything. Two more cover the ways a marker leaks: it is cleared on release AND a subsequent loop `block()` cannot inherit one (otherwise the next real quarantine claims an operator held it), and a hold `block` REFUSES — completed, retired, in-progress — stamps nothing, which is why `operator_block` writes the field after the delegate returns rather than before. *Persistence* is parametrized over the four stored shapes (absent, `null`, the near-miss `"Operator "`, a foreign `"loop"`): all four load as NOT the marker and stay refused, so an old `tasks.json` reads as a loop quarantine (the safe direction — `answer` still works), a hand-edited `null` loads as `""` rather than `None`, and the exact-match compare is pinned against a well-meant `strip().lower()` that would widen what can be released. The existing round-trip test now asserts the FIELD survives, with the prose prefix alongside it.  **+1 on 2026-08-16 (inbox-11) — the strand guard's EMPTY case.** `test_an_in_progress_scope_cannot_be_emptied`: `CONTENT_MUTATIONS` parametrizes `set_approved_paths` with a non-empty scope, so the sharpest shape went unpinned — a mutation that UN-authorizes a running dispatch, which is stranding plus the empty scope `_dispatch_task_postcommit` refuses outright. Refused as `task_in_progress` (the guard runs before `_validate_approved_paths`, so it is not an empty-list complaint — clearing is legal off a running task, per `test_set_approved_paths_can_revoke_a_scope_entirely`), and the scope is re-read afterwards because "refused" and "refused without writing" are different claims. |
| `test_lock.py` | 8 | Single-instance lock: roundtrip; live-lock fail-closed against a REAL separate process; stale detection via a verifiably-dead pid + explicit `unlock` recovery; foreign-host = live; corrupt-lock diagnosis; run-id-guarded release (zombie can't remove a successor's lock). The `exec_handoff` adoption added 2026-08-18 (loop-02) — and the inherited-token check that authorizes it — is tested in `test_self_upgrade.py`, next to the replacement that needs it. Nothing here arms a handoff, so no test in this file touches `AUTOLOOP_EXEC_HANDOFF_TOKEN`; the autouse fixture that clears it lives in `test_self_upgrade.py` alone. |
| `test_self_upgrade.py` | 54 | **Added 2026-08-18 (loop-02).** (43 functions; 54 collected — three are parametrized. Hand-counted, no shell in the worker.) The loop runs the code it just merged, without an operator restarting it. Measured that day: the process started 04:07:03, plan-01 merged a hard decomposition gate at 06:23:59, and at 09:00 the registry held 0 decompositions across 102 tasks — dash-10 among them, a task that STARTED after the merge, because `policy.py` was loaded at 04:07 and merging into a checkout does not reload a live process (brw-11's browser fix, merged 00:58, was inert the same way all night). **The autouse fixture is the highest-value line in the file:** `os.execv` raises, so a test that reached a real exec would replace the pytest process with a loop run instead of failing — same shape as `test_restart_wiring.py`'s `no_machine_access`. The two tests that observe the exec install their own recorder, which raises a non-`OSError` (the production code catches `OSError` as "the exec was refused" and would swallow anything else). **The signal, through the real merge path** (real git, real worktree, real push, `AutoMerger` reached exactly as in production): a merge touching `autoloop/marker.py` writes one `PendingUpgrade` whose `base_sha` is the base head AFTER the merge, with the candidate, task id, paths and repo root; a docs-only merge writes nothing AND is asserted to have really merged (`auto_merge_pushed`), so the negative cannot pass on a merge that never happened; and `changed_paths` stubbed to raise leaves the merge pushed, the record absent and `self_upgrade_error` logged — a raise there would reach `_guarded_attempt` and report a verified, pushed merge as `failed`. `loop_code_paths` counts `autoloop/tests/…` (the claim is "any file under `autoloop/`") and not `autoloopish/`. **The boundary** (`run(max_steps=0)`, which returns before stepping, so no client, git or executor is reached): `ready` with no pending request returns `SELF_UPGRADE` and leaves the session byte-identical; a prepared `pending_request` does not, because a request outlives its own phase; the other five non-terminal phases do not, each being mid-round by construction; a terminal phase reports its park instead; a record in any settled status is never offered again; an unreadable record is no record; and an orchestrator built WITHOUT `self_upgrade_enabled` (every construction but `cli._build_orchestrator`'s — `smoke-browser`'s in particular, which sits in the boundary shape and PASSes only on a clean contract stop) is not offered the boundary at all. **The replacement:** the argv is rebuilt as `python -m autoloop <args>` (`argv[0]` under `-m` is `__main__.py`, and re-running that as a script breaks its relative imports), the preflight provably runs BEFORE the exec and against `_package_root()`, a failed preflight exec's nothing and settles the record so it is not retried every round, a merge in another checkout is refused without even preflighting, a missing/unarmable lock refuses the replacement outright (its successor would find a live lock — its own pid — and fail closed), and an `execv` that raises `OSError` disarms the handoff AND settles the record to `exec_failed` — the sha is still spent (a second call returns `none`), but `execed` is the status `_confirm_self_upgrade` retires one iteration later, and no replacement happened here. **The preflight really launches an interpreter** — `_preflight_import(REPO_ROOT)` succeeds, and a tmp tree whose `autoloop/__init__.py` raises fails with the message quoted — plus a test that the module list names `autoloop.policy` (the module the measurement names) and no optional-dependency module, which would fail every preflight on a machine without playwright and disable the feature for good. **One shot:** after an exec attempt the record is `execed`, a second call returns `none`, and the orchestrator boundary no longer offers it — a merge that imports and then fails at RUNTIME cannot loop. `_run_continuous` is driven for two real iterations (selection stubbed, `time.sleep` used as the probe): the marker is still armed at the end of iteration one and retired at the top of iteration two; a `pending` record is never confirmed by someone else's iteration. **The lock is continuously valid across the replacement:** the file exists and reads LIVE at every instant, the successor (same pid, new `LoopLock`, new run id) adopts it, `started_at` survives and the marker is cleared. Four mutation tests keep that from being a lock-stealing hole — a live lock with no marker is still refused, so is a marker naming another pid, so is a foreign host's, and a second adoption of the same marker is refused; plus `clear_exec_handoff` restoring the ordinary refusal, the superseded lock object being unable to delete its successor's file (run-id guard), `mark_exec_handoff` refusing a lock we do not own, `Path.unlink` made to raise for the whole handoff (both rewrites are temp + `os.replace`; an unlink would open the window the continuity claim denies), and a lock written before the field existed still reading. **+8 functions / 12 collected on 2026-08-18 (loop-02, review round) — the marker's other facts are all forgeable, so adoption now needs an INHERITED token.** The sharpest one is `test_a_valid_looking_marker_this_process_inherited_no_token_for_is_refused`: a hand-written lock naming this host, THIS pid twice and the lock's own run id — everything a dead run leaves behind plus a pid the kernel handed out again — is refused `LockHeldError` (the pid is alive, because it is ours, so the lock is live and a live lock is never stolen), and nothing is consumed. Its complements: a real armed handoff presented with some OTHER token is refused (guessing is the attack, so a near miss must be a miss); a marker naming another RUN is refused, with pid, host and token all correct, so the new identity check is attributable on its own; and the two pre-existing pid/host mutation tests were rewritten to carry a MATCHING token, or they would now pass for the wrong reason. The token itself is pinned as unguessable and environment-only — 64 hex characters, equal in the lock file and `AUTOLOOP_EXEC_HANDOFF_TOKEN` and nowhere else, not derived from the pid or run id, and freshly minted per arming (a reused one would make the previous handoff's environment usable against this lock). `test_a_malformed_token_is_a_refusal_not_a_crash` parametrizes the five shapes `secrets.compare_digest` cannot take (non-`str`, `None`, a dict, empty-both-sides, non-ASCII): a raise there would land in `acquire` — the successor's FIRST act after `execv`, with no `finally` behind it — so it must refuse instead. Continuity gained the other half of the claim: the token is asserted to be armed in the environment **at the instant `os.execv` is called** (observed inside the recorder, since "armed at some point" is a weaker claim), consumed by adoption, cleared when `execv` raises, and never left behind by an arming whose lock write failed (`_write` made to raise: no token, no marker, `False` returned). And the whole handoff is driven once through the production arming site — `cli._self_upgrade_at_boundary`, its only caller — with the successor adopting afterwards and a second acquire refused. **+1 on 2026-08-18 (loop-02, review round 2) — a refused `execv` must not be CONFIRMED as a replacement that happened.** `test_a_refused_exec_is_never_confirmed_as_a_replacement_that_happened` drives the refusal through the iteration that follows it, which is where the damage was: `_run_continuous` carries on after `os.execv` raises, and the top of its next iteration is the one place `_confirm_self_upgrade` fires — against a record that said `execed`, it cleared the marker and logged `self_upgrade_confirmed`, i.e. "one iteration completed under the merged code" for a replacement that never occurred and an iteration the OLD image ran. The orchestrator is stubbed to return `SELF_UPGRADE` on round one and to end the test on round two, so `rounds[1]` is read AFTER the confirmation check and is evidence about it: the record is `exec_failed` there, `self_upgrade_confirmed` is asserted ABSENT, and `self_upgrade_exec` proves the replacement was really attempted rather than skipped earlier. It fails against the previous behaviour on both the cleared record and the entry. Its unit-level companion (`test_an_exec_that_is_refused_disarms_the_lock_again`) now pins the settled status, the `OSError` text in `detail`, and the one shot as the property it actually is — a second `_self_upgrade_at_boundary` returns `none`. `test_a_settled_record_is_never_offered_again` gained `exec_failed` as a fourth case in the same edit (still three parametrized functions; collected 52 → 54 for the round), so the status a refused `execv` now settles to is pinned at the orchestrator boundary too and not only inside the function. The sibling branch it makes uniform was already there: an unarmable lock has always written `execed` and then settled `exec_failed` through `_settle_upgrade` (`test_the_replacement_is_refused_when_the_lock_cannot_be_armed`) — the raising `execv` was the one refusal that did not. |
| `test_manifest.py` | 43 | **RETIRED 2026-07-30 (S21): the executor-provenance tests (snapshot/classify + `verify_commit`'s executor gate — 13 tests) were DELETED**, since `ChangeManifest.begin`/`.finish` and `verify_commit`'s executor branch have no production caller left. Store roundtrip. **Adopted manifests (still live as a unit-tested primitive, no production caller — (content-bound commits):** explicit path lists only (never inferred from the dirty tree), one-byte post-approval change refused, missing file refused, unapproved path refused, a digest copied from another file refused, unpresented manifest refused, verification without repo access refused, multiple dirty files stay separable into explicit groups, invalid path sets (empty/duplicate/`..`/absolute/clean/deleted) refused, canonical adoption block is deterministic, round-trip preserves kind+hashes+binding, and pre-adoption manifests on disk still load as executor kind. **Staged-blob verification (review finding, 2026-07-30):** the swap-and-restore attack — stage altered bytes, restore the working tree — passes a worktree recheck and is caught by hashing the index; `staged_blob` returns index bytes not worktree bytes; symlinks and symlinked parent directories refused at adoption; a staged mode-120000 entry refused even if adopted. |
| `test_doctor.py` | 17 | Doctor with mocked boundaries: all-green (now includes a configured `origin` remote and asserts `worker_isolation`/`hooks_dirs`/`publisher`/`publisher_url_drift` all `ok`, 2026-07-30), git identity + protected-branch warning, CDP-unreachable → live check skipped, playwright missing, logged-out browser, stale lock reported — plus 11 conversation-URL shape cases (plain `/c/<id>`, **project-scoped `/g/g-p-…/c/<id>`**, custom-GPT scope, query string, trailing slash accepted; wrong host, bare host, project root, `http://`, empty id, unset placeholder rejected). Proves doctor never submits and never reconciles (a reload would be a side effect). The new checks' own deeper coverage (isolation violations, drift mismatches, hook detection) lives in `test_v1_smoke.py` and `test_worker_publisher.py`. |
| `test_smoke.py` | 5 | smoke-browser through the real CLI with a fake registered provider: full contract path (request id, stamped CONTEXT, parser, transcript), isolated smoke state, clean `stopped` terminal, FAIL path, executor provably never invoked, **a parked smoke session is archived not resumed**, and **no git write is reachable even from a commit-approval reply** (guard gateway). **Single-round-trip proofs:** the effective policy is one iteration / zero parse retries / zero denial retries / one failure, a malformed reply sends exactly one request and fails, exactly one submission and one response are transcribed, an audit executor or agent runner can never be constructed, and each run mints a fresh request id. |
| `test_audit_findings.py` | 14 | Strict agent-output contract: field-by-field validation, per-item rejection with reasons, duplicate ids, non-JSON output, wrong top-level shape. |
| `test_audit_reconcile.py` | 7 | Bucket classification, cross-agent dedupe, speculation + style hard-rejected from promotion. **Dedupe semantics changed 2026-08-01** — it merges rather than keeping the higher-quality instance; preservation is pinned in `test_audit_compaction.py`. |
| `test_audit_compaction.py` | 29 | **Report compaction (added 2026-08-01) — almost entirely preservation tests, not size tests.** Guards the three silent losses: truncated, dropped, or accepted-oversized. Bounds: an ordinary finding is untouched; a 21k-style essay in `evidence` is HELD (not rejected, not truncated, item byte-for-byte intact) while its domain still counts as covered; many individually-legal fields still trip the whole-finding budget; oversize is checked AFTER validation, so an invalid finding is still rejected. Merge (**semantics corrected same day** — an earlier version widened folding to file+symbol overlap and a test here pinned that bug): **distinct defects in the same symbol stay separate**, location overlap alone never folds, evidence alone cannot make two findings look the same (same `file:line` is location agreement in disguise), findings in different files never fold, different categories never merge — while two agents describing the SAME defect in near-identical terms do fold, so dedup still fires. When it fires, every unique evidence reference/acceptance criterion from both survives, attributed to its original id, and identical text is not duplicated; the stronger severity/confidence and the **cautious** parallelism answer win; merging is order-independent in what it preserves; three-way overlap collapses without losing any of the three. Reshape (end-to-end through the executor with a fake runner): a successful reshape keeps the finding and runs **exactly one** round; a still-oversized reshape parks with the ORIGINAL preserved on disk; a failed reshape agent parks; and an empty reshape parks rather than silently losing the finding. Report: the task graph renders once as JSON with the table gone, every proposed task survives into it, and a merged finding's content reaches the rendered bytes. A parametrized test asserts there is no truncation path at all — if someone 'fixes' an oversized finding by slicing it, it fails. |
| `test_audit_taskgen.py` | 8 | Priority ordering per the mandated ranking, `au-NNN` ids skipping registry collisions (never A1-style ids), finding→task dependency mapping (incl. deps on existing roadmap tasks; unresolved deps noted, not invented), human decisions skipped, full task structure. |
| `test_audit_executor.py` | 20 | End-to-end with fake agents + stubbed validation: 6-domain fan-out with scope/feedback threading, raw reports persisted separately, one dated Markdown report as the only repo write, proposal JSON, agent failure → honest "COVERAGE INCOMPLETE", non-audit decisions refused, unsafe validation binaries refused. **+4 for scope semantics (2026-08-06, rt-11 / `tests_ci:arch-01`):** a reviewer `scope` is threaded into every per-domain prompt verbatim, and used to land immediately after "Stay in your domain." with no statement of what it meant — so a scope describing the whole audit process ("parallel read-only domain reviews", "apply the task routing", "produce one dated Markdown report") read to a single-domain agent as a change of remit. The tests pin the framing rather than any filtering: the verbatim text still reaches the agent, but the sentence bounding it (narrows this domain only, no extra authority/tools/write access/delegation, not responsible for other domains or the report) appears BEFORE it — asserted by string index, since framing after untrusted data is framing the data can pre-empt. The end-to-end case drives the real orchestration-shaped scope through all six domains and checks each prompt still names its OWN domain. The count was documented as 6 while 16 were collected (see the staleness note above this table); 20 is a hand count of `def test_` in the file, not a collected count — no Bash in the worker. |
| `test_markdown_policy.py` | 7 | Markdown-only gate: canonical files ok, ONE dated report max, production code / non-canonical md / traversal / absolute paths refused. |
| `test_audit_agents.py` | 24 | ClaudeCliRunner with stubbed subprocess: read-only headless argv (allow Read/Grep/Glob, disallow Edit/Write/Bash/Task/…), result-JSON unwrap, timeout / missing binary / non-zero exit reported not raised. Tool set is now a constructor parameter (`allowed_tools`/`disallowed_tools`, defaulting to the read-only pair asserted here); `test_implement_executor.py` is the other construction site, via `implement_agent_runner`. Env is stripped of the validation credentials for BOTH tool sets. **+5 for failure summarisation (2026-08-01):** an advisory banner leading stderr must NOT become the reported cause (the real regression — the CLI's connectors notice prints first, and `stderr[:2000]` made it the whole answer, which reached ChatGPT as "unset ANTHROPIC_API_KEY" for a variable that was never set); banner-only stderr reports `NO diagnostic output` instead of blaming the notice; a 400-frame traceback keeps its TAIL, where the cause lives; plain failures and the stdout fallback are unchanged; and one end-to-end case through `run()` itself, verified to FAIL against the old head-only capture. **+10 for `run()` never raising (2026-08-04, rt-04):** `run` caught only `TimeoutExpired` and `FileNotFoundError`, and `_run_agents` consumes the fan-out via `list(pool.map(...))`, which re-raises the first exception — so one denied `cwd` or failed pipe read discarded every domain that had already finished. Four parametrized generic causes (plain `OSError`, `PermissionError`, `UnicodeDecodeError`, `RuntimeError`) now come back as an `AgentResult`, with the control assertion that neither dedicated message (`command not found` / `timed out`) is mis-attributed to them. **The construction trap has its own test:** `OSError(errno.ENOENT, …)` is built as a `FileNotFoundError` by `OSError.__new__`, so a "generic exception" case written that way hits the DEDICATED branch and passes against the unfixed code — `test_every_generic_case_really_misses_the_dedicated_branch` asserts that specialization explicitly and that no parametrized case is an instance of it (see `docs/COMMON_ERRORS.md`). Plus: the `FileNotFoundError` message survives the broad clause added below it (the reorder guard, since it is an `OSError` subclass); an exception whose `str()` is EMPTY still reads as a failure, because `AgentResult.ok` is `not error` and a blank one would count a blown-up domain as covered with zero findings; a `proc.stdout` read that fails AFTER the child exits is caught too (the whole body is guarded, not just the spawn); **argv CONSTRUCTION is inside the guard as well** (review follow-up — `build_argv` ran before the `try`, so a failure there still escaped and still cost the whole fan-out; driven through the real `build_argv` via a spec stand-in whose `model` raises, not an override, which would only prove the guard catches an override — and the result still names the command, since `argv` is bound to the base command *before* the `try` so reporting a failure cannot itself raise `NameError`, with the stub asserted never spawned); and the acceptance case driven through a real `ThreadPoolExecutor.map` — one domain raising leaves its two siblings' output intact. `autoloop/audit/executor.py` was deliberately NOT changed: it already turns `not result.ok` into an `agent_failures` entry. |
| `test_implement_executor.py` | 13 | `ImplementExecutor` with fake/stubbed agents: write-capable argv (Edit/Write allowed, Bash/Task disallowed), no `--model` flag (automatic selection), subagent `cwd` is the task's own worker repo not the main checkout, `changed_paths` derived from the worker repo's real `git status` and NOT from the agent's own claim (a fake agent claims a file it never touched — ignored), a filename with both a space AND a tab round-trips (`-uall`/`-z` NUL-safety), agent failure / no-files-changed / validation failure each `status="error"` without raising, success is `status="ok"` with `changed_paths`/`validation` populated, and nothing is written outside the worker repo (main checkout + `.autoloop/` marker both provably untouched); plus the audit/`None`-task defense-in-depth refusals and the `worker_repo_root_for`/`policy` constructor pairing contract. |
| `test_stall_detector.py` (new, 2026-08-14) | 27 | The progress-based stall detector that replaced `audit.agent_timeout_seconds` (see the narrative entry above for the six measured losses). `supervise()` is pure over an injected handle/probe/clock/sleep, so no test spawns a process or waits. The load-bearing one is `test_an_agent_writing_steadily_past_the_old_timeouts_is_not_killed`, parametrized over BOTH values the retired key ever held (900/1800): a steadily-writing agent runs 90 minutes and exits on its own, never signalled — reintroduce any elapsed bound and it fails. Also: a 1400s pause inside the 1800s window is not a stall; silence past the window IS killed, leads with `STALLED:` and names the silence rather than the elapsed time; the report carries the partial-work numbers, and a stall that produced NOTHING is worded differently from one that produced 591 lines across 16 files (the two mean opposite things to a reviewer); SIGTERM escalates to SIGKILL for a process that ignores it; a process that finishes between the stall decision and the signal is `COMPLETED`; the ceiling terminates a run that "progresses" forever and reports itself as a finding, not a routine timeout; **a blind probe never stall-kills** — "I cannot see the tree" is not "the tree is not changing", so only the ceiling ends an unobservable run and the report says the silence was unobserved. `WorkerTreeProbe` against a real `tmp_path` git repo: one file growing is progress even though `git status`'s path set is unchanged (why the per-path stat is load-bearing), an untouched tree samples identically, tracked insertions plus whole new files are counted (2 + 3 = 5), a binary new file counts as a file but not as lines, a measured zero is distinct from an unreadable repo, and a failed tracked diff makes the report say `INCOMPLETE` and name how many tracked files it excluded rather than quietly reporting `~0` lines (`HEAD` always resolves in a real worker repo — `WorkerRepoManager.create` ends with `git checkout -B <branch> FETCH_HEAD` — but a wrong number presented as measured would be worse than the timeout this replaced). Through `ClaudeCliRunner` with a fake spawn: a stall is reported instead of a timeout and the killed run's partial stdout survives; a long healthy run finishes with the retired 900s value passed in and provably unused; and a runner with NO probe still hands `timeout=900.0` to `subprocess.run` (the read-only audit path, unchanged). Through `ImplementExecutor`: the stall report and `changed_paths` (read from git, never the agent) reach `ExecutionOutcome`, exactly one set of numbers is printed, and an ORDINARY agent failure now also reports what it left behind. Config: the retired key is refused with a message naming all three replacements (not remapped — the old meaning survives in none of them), a stall window at or above the ceiling is refused, and a non-positive bound is refused. |
| `test_postcommit_flow.py` (+2, 2026-08-01) | — | **B4b regression.** `test_post_commit_reruns_the_tasks_own_validation_not_the_audit_set` drives a REAL `ImplementExecutor` and the orchestrator's post-commit re-run through ONE recording `command_runner`, so "the same commands ran before and after the commit" is *observed* rather than asserted twice against two separate doubles: the task's declared command must appear exactly twice and the audit default (`ruff check .`) never, and the persisted `TaskExecution.validation_commands` is checked as the thing that carried it across a resume. Verified to FAIL when the fix is reverted — it then records `[declared, ruff-check]`, which is the bug exactly. `test_post_commit_validation_honours_the_declared_cwd` pins that a declared `validation_cwd` applies post-commit too (right commands, wrong directory checks nothing). `build_postcommit` gained `task_validation` / `task_validation_cwd` / `executor_factory` — the last because a real `ImplementExecutor` must be rooted at a worktree that does not exist until the helper builds it. |
| `test_tasks.py` (+4, 2026-08-01) | — | **Always-approved trackers.** `TRACKER_PATHS` pinned as an exact set (anything added widens every task in the repo, so it must be a deliberate diff) and asserted to be markdown-under-`docs/` only; a scoped task gains the four and stays sorted; an UNSCOPED task stays unscoped — the property that must not regress, since returning just the trackers would turn "no scope authorized yet" into a dispatchable task that may write docs; and non-tracker paths (source files, `docs/AUTOLOOP.md`, `CLAUDE.md`) are still outside scope. |
| `test_postcommit_flow.py` (+2, 2026-08-01; second test rewritten 2026-08-05) | — | End-to-end: a `docs/SUMMARY.md` edit NOT named in `approved_paths` now commits instead of parking (rt-01's actual failure, twice). The first cut of the change patched three of four gates, and the PRE-commit gate then refused what the POST-commit check allowed — these two are what caught it. The companion used to pin "a non-tracker path outside scope still refuses and still does not commit"; since the scope check went ADVISORY it is `test_a_NON_tracker_path_outside_approved_paths_commits_and_is_recorded` — see the advisory row below. |
| `test_postcommit_flow.py` / `test_m1_hardening.py` (2026-08-05) | — | **The path-scope check became ADVISORY at BOTH gates** (`docs/SECURITY.md` S25 amendment). Six parks in three days were all legitimate work; a declared scope is a prediction, and a wrong one now informs the reviewer instead of discarding the round. **The mutation these tests exist to kill is "relax only the pre-commit gate"** — that moves the park downstream rather than removing it, so `candidate_sha != ""` is deliberately NOT the assertion anywhere here (the commit exists either way; only the park differs). Every one asserts the round reaches `POST-COMMIT REVIEW PACKET`. `test_a_NON_tracker_path_outside_approved_paths_commits_and_is_recorded` (site 1: the executor reports an out-of-scope path). `test_hook_adding_unexpected_path_is_recorded_not_refused` (**site 2 in isolation** — a commit hook adds the path strictly after the pre-commit check ran, so site 1 structurally cannot see it; this is the one that fails if site 2 is left blocking). `test_unexpected_commit_path_from_a_prior_process_is_recorded_on_adoption` (crash-recovery adoption, where no `ExecutionOutcome` exists and only the post-commit comparison can see the path — why the record, not the round, is where this lives). `test_agent_reported_extra_path_is_recorded_but_cannot_widen_authorization` (renamed from `..._cannot_widen_authorization`: the park is gone, the M1 finding #2/#3 non-circularity it was written for is still pinned — `allowed_paths == effective_approved_paths(task.approved_paths)`). Bounds, all still refusing: `test_task_with_no_approved_paths_cannot_be_dispatched` (empty scope is a DIFFERENT rule, deliberately not relaxed), `test_failing_post_commit_validation_is_refused` (+1 assertion: non-scope post-commit failures are untouched), and the escape-detection tests (unchanged — a write outside the worker repo is loop-fatal confinement, not scope). Plus a store round-trip test (JSON has no tuples; without the `load` coercion `out_of_scope_paths` comes back a list, and a record predating the field must load as `()` not raise) and a negative control (a clean round records nothing). |
| `test_postcommit_review.py` (+4, 2026-08-05) | — | **The out-of-scope section — the control that REPLACES the park.** The row above relaxed the enforcement; these pin the reporting, and without them the relaxation removes a control rather than relaxing one (`docs/SECURITY.md` S25 amendment declared the packet's rendering load-bearing while `packet.py` had no such section at all — the docs were ahead of the code for a day). `test_the_packet_names_every_out_of_scope_path` asserts the round reaches `POST-COMMIT REVIEW PACKET`, the count in the label, both offending paths, that an IN-scope path is not flagged, and that the declared scope is printed — a list of violations is unjudgeable without what was declared. **Every path assertion matches on the `! ` marker, not the bare path**, because every changed path also appears in the changed-paths list above: a bare substring check passes against a packet with no section at all, which is the exact mutation these tests exist to catch. `test_the_out_of_scope_paths_come_from_git_not_from_the_record` is the sharp one — by packet-build time `TaskExecution.out_of_scope_paths` already holds git's own answer, so a hook/adoption case does NOT discriminate and only FABRICATION does: the record is overwritten with a set DISJOINT from git's — two paths the commit does not contain (neither a `TRACKER_PATHS` entry, so neither can reach the packet via the declared-scope line either), and the real one dropped. Disjointness is what makes it non-vacuous rather than merely passing: read from the record the packet loses the real path and gains both fabrications, read from git exactly the reverse, so the `in` and `not in` assertions pin opposite halves of the same mutation and neither can hold while the other fails. An OVERLAPPING fabricated set — the first cut of this test — leaves both `not in` assertions passing against a section that renders nothing at all. Rendering from that field passes every other test here and fails this one. `test_a_round_inside_its_scope_carries_no_section_at_all` (renamed 2026-08-15 from `test_a_round_inside_its_scope_says_so_rather_than_going_silent`, and now asserting the OPPOSITE): a clean round carries no section, not even an empty one. The `(none)` line it used to pin was dropped because a section that is empty in nearly every packet trains a reviewer to skim past the one packet where it matters — so the control would read weakest exactly when it finally had something to say. That gives up "absence is indistinguishable from a section lost in a refactor", which is a real objection and is now answered by `test_the_packet_names_every_out_of_scope_path` failing the moment a REAL overrun renders nothing, plus S25's `rg -n 'OUT-OF-SCOPE PATHS' autoloop/packet.py`. The rewritten test asserts BOTH directions — `"OUT-OF-SCOPE PATHS" not in payload` alone also passes against a packet that never rendered at all, so `POST-COMMIT REVIEW PACKET` and the in-scope changed path are pinned too, plus no double blank line left between the changed-path list and `Diff stat:`. `test_the_out_of_scope_section_survives_an_omitted_diff` covers the residual risk SECURITY.md names: over `DIFF_INCLUDE_MAX_CHARS` the reviewer sees paths but not content, and paths-without-content is degraded while no-section-at-all is nothing. |
| `test_task_inbox.py` | 13 | **Added 2026-08-01.** The inbox path is asserted to be OUTSIDE the checkout — the property the whole design rests on, since the escape detector snapshots ignored paths and a state-dir write mid-execute parks the loop loop-fatal. Submit/drain round-trip in submission order (which caught a real bug: a `monotonic_ns() % N` filename tiebreaker WRAPS, so a later request sorted first); malformed requests refused at submit and never queued; an unparseable file is quarantined to `rejected/` and not replayed forever; submit is atomic with no temp file left. Priority: lower number wins, a task added later can overtake one already queued (the reason ordering changed from insertion order), ties break on id, the field survives persistence, and an old `tasks.json` without it defaults to last place. **+3 for the shared merge (2026-08-01):** `apply_requests` is asserted to be the implementation BOTH `Orchestrator._drain_task_inbox` and `cli._cmd_drain_inbox` call (two copies would drift, so the same request would behave differently depending on who applied it); one pass adds, reprioritises and refuses independently, leaving the good requests landed and the original task untouched; and a batch containing a malformed request never raises, so one typo cannot discard the fifteen queued behind it. **+10 on 2026-08-16 (inbox-02) — the mutation vocabulary.** `task` + `priority` became `task` + six kinds, and each of the three properties that makes that safe to expose has a test rather than a comment. *Wiring:* one round trip per kind through the real inbox (submit → drain → apply), because a kind that is in `MUTATION_PAYLOAD` but wired to no mutator looks fine at submit and silently does nothing on merge. *Shape vs content:* a request naming a field its kind ignores is refused at submit (parametrized across the four ways to get it wrong — a foreign field, a payload on `unblock`, a blank id, a string where a list belongs), while a blank description and a globbed path both QUEUE and are then refused on merge in the registry's own words — pinning "submission checks shape, the registry owns content", so a second rule set here cannot start refusing what `add_many` accepts. *The strand guard:* three mutations against an in-progress task are all refused while the `priority` request queued alongside them still lands, and the task's fields are re-read afterwards to prove nothing was half-applied. *The reverse:* a hold placed through the inbox is released through the inbox (without which the vocabulary would write a state with no way out — an inbox hold creates no `blockers.Blocker` record, and `answer` takes a blocker id), while a LOOP-raised quarantine is refused with the message pointing at `autoloop answer`, its recorded reason intact. *Exclusions and ordering:* `retire` is absent from `KINDS` and refused at submit; a hand-written file carrying an unknown kind is named as such rather than falling through to the creation branch and being refused for some unrelated missing field; and one batch pins single-pass submission order — a mutation queued before its target exists is refused rather than deferred, and two writes to one field resolve last-wins. (The `13` in the count column predates this row's edits and is known to trail the collected figure — see the staleness note above the table; `+10` is hand-counted and is what this change added, not a re-audit.) **The last one is a wiring guard, not a behaviour test:** `apply_requests` must keep returning THREE buckets, and each caller's `if added or <middle>:` save gate is read out of its own source, because both drain sites unpack positionally and a fourth bucket either forgot to persist would apply a mutation in memory that the next save silently overwrites — while every unit test here still passed. **+3 on 2026-08-16 (inbox-02, review round 2) — one per hole the first cut shipped, plus a drift guard.** *The reverse, end to end:* `test_the_inbox_reverse_refuses_a_quarantine_that_merely_reads_like_a_hold` is the twin of the registry regression in `test_tasks.py` — a LOOP quarantine whose `blocked_reason` literally begins with `OPERATOR_HOLD_PREFIX` is still refused, pointing at `autoloop answer`, with the reason intact. The pre-existing quarantine test could not catch this: it used an ordinary reason, so it passed against a provenance check that read the reason text, which is what made the "cannot launder a quarantine" claim false. *The request shape:* `test_a_creation_request_cannot_carry_a_mutation_field` — `reason` on a `kind: task` request (and on the no-kind legacy form, which is also a creation) is refused at SUBMIT, naming it as mutation-only. Checked against one global field set it submitted cleanly and was then silently dropped on merge, contradicting the per-kind contract the row above pins for every other field; the control asserts the same field on `block`, the kind that owns it, still queues. A third is a drift guard rather than a behaviour test: every `MUTATION_PAYLOAD` payload must land in `CREATION_FIELDS` or `MUTATION_ONLY_FIELDS`, since nothing validates against the union `ALLOWED_FIELDS` any more (`dashboard.TASK_REQUEST_FIELDS` only documents itself against it), so a new kind that forgets it would fail nothing else. **+3 on 2026-08-16 (inbox-02, review round 3) — the per-kind rule at the OTHER gate.** Round 2 put the shape contract in `TaskInbox.submit` only, and `submit` is not the route the task documents: hand-writing the JSON file is the only way to queue five of the six kinds, and such a file reaches `apply_requests` having passed through nothing. Two regressions call `apply_requests` DIRECTLY, the way a drained hand-written file arrives. *Creation:* `test_a_hand_written_creation_carrying_a_mutation_field_is_refused_on_merge` — `kind: task` plus `reason` is refused naming it mutation-only, the task is not created at all, and a plain creation queued behind it still lands. *Mutation:* `test_a_hand_written_mutation_carrying_a_foreign_field_is_refused_atomically` — `block` plus an unrelated `approved_paths` is refused "carries only", with BOTH fields re-read afterwards (status/`blocked_reason`/`hold_origin` untouched AND the original scope intact), because the defect it pins is a request that half-did what it said: the hold applied, the scope rewrite silently dropped. A `priority` request queued behind it still lands, so the never-raises promise is exercised on the new refusal path too. The third is the house-style drift guard: `test_one_shape_implementation_serves_both_gates` reads both `TaskInbox.submit` and `apply_requests` with `inspect.getsource` and asserts each calls `check_request_shape`, which is the assertion that answers "so the rules cannot drift" directly — the same technique as the shared-merge and three-bucket guards in this row. **+1 on 2026-08-16 (inbox-11) — the approved_paths clause end to end.** `test_an_inbox_request_cannot_empty_a_running_tasks_scope` drives the value `test_a_mutation_cannot_strand_a_task_the_loop_is_running` cannot: `[]` against an in-progress task is refused with the scope re-read intact, while a non-empty edit queued in the SAME batch against a pending task lands and lands in the `applied` bucket — the bucket both drain sites gate `task_store.save()` on. The control is what makes it non-vacuous: a guard written as "refuse `approved_paths` from the inbox at all" passes the refusal half and fails the other. |
| `test_orchestrator.py` (+3, 2026-08-01) | — | The integration point: a submitted request becomes a registry task written by the LOOP; a duplicate id is reported and dropped while the good request still lands and the original is untouched; and a priority-1 submission changes what `next_ready()` returns while a lower-priority task is already queued. |
| `test_scope_prefixes.py` | 16 | **Added 2026-08-02.** Directory prefixes in `approved_paths`. A prefix authorizes everything beneath it including nested paths; it stops at the SEGMENT boundary, so `routers/` never authorizes `routers_backup/` (a bare string-prefix check would — this is the test that makes the trailing slash load-bearing); an exact entry authorizes only that file, never its directory; unrelated paths are still refused. Prefixes are held to every rule paths are (`..`, globs, whitespace, leading `-`, absolute), and `"/"` is refused so it can never authorize the whole repository. Plus the leading-dot/underscore fix: `tests/_auth_helper.py` and `.gitignore` were previously unrepresentable while the error text claimed `_` was legal, and a leading `-` is still refused. |
| `test_crash_safety.py` | 15 (1 `isolated`) | **Added 2026-08-02.** What survives the machine going away mid-run. **Pid reuse across a reboot:** a lock written before the current boot is stale even when its recorded pid is unquestionably alive (the test uses `os.getpid()` as the reused-pid stand-in) — without the check, `unlock` refuses and sends the operator to stop an innocent process. The fallbacks are pinned in the safe direction: unreadable boot time, an unparseable stamp, and a timezone-naive stamp all defer to the pid probe, and a stamp within the clock slack of boot still counts as live. `boot_time_epoch()` is asserted against the real platform (in the past, this century) and skips where there is no source. **State durability:** `os.fsync`/`os.replace` are spied to prove the DATA fsync precedes the rename — the ordering that stops a power cut publishing a rename whose blocks never landed — with the directory fsync after it; a filesystem that refuses to fsync a directory must not fail a save that otherwise succeeded; no temp file is left behind. **Signals** (rewritten 2026-08-02 after `test_sigint_release_is_unchanged` was found to be THE flaky test refusing the loop's commits — it asserted `proc.wait(timeout=30)`, i.e. full interpreter teardown by a fixed deadline, which a loaded machine misses; it now polls for the lock file to disappear, since the release precedes exit, and holders go through a context manager that always reaps the child because a leaked sleeping process makes its neighbours flaky)**:** SIGTERM and SIGHUP (parametrized) each release the lock in a real subprocess, and the next `LoopLock` acquires without operator recovery; SIGINT is unchanged; SIGKILL still leaves a lock, asserted to be one `break_stale()` clears — the honest guarantee. The load-bearing one is `test_lock_is_released_before_unwinding_not_by_it`: the holder's cleanup blocks for 120s, so reaching the lock's context-manager exit is impossible and only an in-handler release can have run. **ISOLATED 2026-08-04:** `test_sigint_release_is_unchanged` is correct alone (release measured at 0.02–0.03s, 14/14 under CPU load) but flaky INSIDE a full-suite run — an interaction with its neighbours, not latency, and relaxing it twice would have papered over that. It now carries `@pytest.mark.isolated`; the default run excludes it via `-m "not isolated"` in pytest.ini and the loop's validation runs it in a dedicated process, so the coverage is still enforced rather than dropped. A guard test pins that the isolation cannot decay into deletion — and it asks configparser and `pytest --collect-only` rather than grepping, because the FIRST version grepped and two of its three assertions matched their own text (the explanatory comment contains `-m "not isolated"`, and the assertion line contains `@pytest.mark.isolated`); both mutations passed until it was rewritten. It was written because the first version of this test passed against a handler that did NOT release — `SystemExit` unwinds through the `with` block on its own — which would have shipped a guarantee that evaporates exactly when the loop is mid-fan-out and cleanup outlives the shutdown's grace period. |
| `test_orchestrator.py` (+2, 2026-08-02) | — | The audit-churn fix. A quarantined audit unit is DENIED rather than re-dispatched — driven at `_resolve_audit_task` directly, because the unit id derives from `state.iteration`, which advances during `run()`, so an end-to-end test cannot name the id it needs to quarantine without racing the loop (the first version of this test did exactly that and passed against the bug). Asserts the denial re-prompts rather than parks: `policy_denials == 1`, phase back to `ready`, the unit id present in the outbox so ChatGPT is actually told, and the executor never called. The companion pins the other direction — an UNKNOWN unit must be dispatchable, since audit units are synthetic and usually absent from the registry; treating unknown as quarantined would block every audit that ever runs, and that mutation fails here. |
| `test_playwright_driver.py` | 46 | **Added 2026-08-02.** One Playwright driver per process. `sync_playwright().start()` refuses when another driver is already RUNNING in the thread, so a leaked driver killed `run --continuous` on the second browser client it built — and leaking one was easy, since `close()` swallowed a failed `stop()` and `_drop_client` swallows a failed `close()` and drops the reference regardless. Against a fake `playwright.sync_api`: five sessions share one driver and start it once; `close()` closes the browser and never stops the driver, and the next connect still works; a teardown whose `close()` RAISES cannot poison the next connect (the production path); `close()` is idempotent; a failed connect leaves the driver usable rather than stopping it (the old code stopped it, and a refused connect is the case most likely to be retried); a missing playwright still explains how to install it. **Empirically grounded. **+5 (2026-08-03) for tab binding:** `connect` bound to the first tab merely CONTAINING `chatgpt.com`, so a stray in the profile could attach the loop to the wrong conversation — which then surfaced as `page left the configured conversation while awaiting <id>`, a page 'leaving' a chat it was never on. Now it binds to the configured conversation (host+path, so a query string or trailing slash does not defeat it), opens its own tab rather than adopting a stranger's when nothing matches, and closes ONLY tabs it opened — closing the operator's would be the mirror of the bug. The substring path is retained and pinned for callers with no conversation in hand (`doctor`, the smoke test). before the tests were written:** against a live Chrome, the old code crashes on connect #2 with the production error once a teardown fails, the new code survives repeated cycles, and `browser.close()` on a CDP connection leaves the human's Chrome running on the same pid. **+6 (2026-08-06) for reaping orphaned tabs:** tabs accumulated in the dedicated profile until Chrome was restarted (observed 2026-08-04: two tabs on the SAME conversation, one orphaned). Not a bug in the close path — `close()` already closes the tab it opened and leaves a borrowed one alone; the leak is that it has to RUN, and it does not on an abrupt exit (pause-and-exit, kill, crash, `doctor`, any ad-hoc probe), while `_drop_client` swallows a failed `close()` by design. `connect` now closes any OTHER tab on the conversation it just bound to. The tests pin the three bounds that keep that safe rather than merely that it closes something: a tab on a DIFFERENT chat survives (it may be an operator reading a past conversation — this is the assertion that fails the "reap every chatgpt tab" mutation), the bound page is never closed (identity, not URL — a duplicate has the same URL by definition, so a URL-based skip would close the tab we work in), a stray whose `close()` RAISES neither aborts the connect nor stops the remaining strays being reaped (per-candidate best-effort, like the existing teardown), and a connect with no conversation in hand reaps nothing at all (`doctor` picked its page by bare substring, so "duplicates of whatever we landed on" is the ambiguous case). **+18 (2026-08-15), 17 → 35, for the crash that bypassed every recovery:** on 2026-08-15 the loop did not park, it DIED — `connect_over_cdp` raised a PLAIN `Exception` ("Connection closed while reading from the driver"), because Playwright's `rewrite_error` gives driver-channel failures no type, so `except playwright.sync_api.Error` missed it and the process ended with `phase=submitting`, `stop_reason=None` and no blocker: indistinguishable from a clean exit. The guards are now positional, and these tests raise things that are deliberately NOT `module.Error` — `FakeError` IS one, so a test built on it proves nothing about this bug and would pass against the old code. Pinned: a plain `Exception` from `connect_over_cdp` becomes a `SessionLostError` (a `BrowserError`, the only thing `run()` routes to restart → budget → park) and still leaves the shared driver usable; a failure STARTING the driver is routed the same way (same subprocess channel, same death); the converted message names the ORIGINAL exception type, since `_handle_browser_failure` logs `kind=` and that now always reads `SessionLostError`; an `AutoloopError` raised inside the guard passes through unconverted, in both `connect` and `_call`, so a `LoginExpiredError` keeps its own routing; a dead channel MID-OPERATION (`goto`) converts too — guarding only the connect leaves the same crash reachable from every submit and poll; and a `page.on` that fails cannot end the process from a call site that exists only to make diagnostics nicer. **The park's evidence (11):** "browser alive, port dead" and "no browser at all" need opposite operator actions, so the message carries `endpoint=`, `port_open=`, `cdp_answering=` and the pids of non-helper Chromes on the profile and on the port. A running-but-unusable browser reports `Chrome IS running`, an empty machine reports `NO Chrome is running`, helper processes (`--type=`) count as neither (they inherit `--user-data-dir` and the port, so counting them reports a live browser from the children of a dead one — the same mistake that made the old restart script kill a renderer and report success), a process holding the port under ANOTHER profile is still reported, a non-loopback endpoint is `unknown` rather than measured against 127.0.0.1 — and since `ps` lists THIS machine too, the sharper companion gives the local machine a Chrome matching BOTH the dedicated profile and `--remote-debugging-port=9222` and still requires `http://gpu-box:9222` to come back unknown: no pid in the message, neither `Chrome IS running` nor `NO Chrome is running`, no `chrome_restart` advice, the host named instead, and (secondary, killing "scan then ignore") the `ps` not run at all; an endpoint with no usable port is withheld the same way and says to fix the url instead, since a local browser cannot settle a broken one — and its companion drives the same branch through an UNPARSEABLE url (`:not-a-port`), which raises out of `urlsplit` rather than merely lacking a port: whether the operator gets a diagnosis at all must not depend on which of the two it is, so `_endpoint_host_port` guards the whole parse and the case is asserted NOT to degrade to `diagnosis=unavailable` — the diagnosis reaches the raised error (it is only worth measuring if it survives into `stop_reason`), and a probe that RAISES yields `diagnosis=unavailable` with the original error intact — the thing added to prevent a crash must not become one. One more pins the ORDER: the action leads and the key=value evidence follows, asserted against `str(exc)[:160]` because `autoloop start` prints `blocker.question[:160]` — written evidence-first (as it first was), that view shows four fields and cuts off the instruction. An autouse fixture fakes the port/HTTP/`ps` probes for the whole file, so the suite never measures the operator's own Chrome. **+5 (2026-08-16), 35 → 40, for the ONE call here that swallows an exception on purpose:** `scroll_to_end` forgives a node that will not come into view, because the virtualizer detaches nodes under the gesture and restarting Chrome over a repaint would be worse than a gesture that painted nothing. That swallow was a bare `except Exception`, which also ate a dead driver channel — and its caller (`BrowserChatGPT._mount_message_tail`) reads ABSENCE out of a list that stops changing, so a lost browser reported as "the gesture did nothing" becomes "the request is not in this conversation", the exact confident-wrong answer the search exists to avoid, with no `BrowserError` for the orchestrator to restart or park on. Pinned: a plain `Exception` carrying the driver-channel message is re-raised and converted to `SessionLostError`, and the End press does NOT run (the gesture aborts rather than half-running on a dead channel); an `AutoloopError` under the gesture keeps its own type; a bounded `TimeoutError` (matched by NAME — `playwright.sync_api` is imported lazily so the suite runs without the package, and importing the real class to name it would defeat that) is forgiven and End still presses; the same fault arriving as a plain error whose message names a detached node is forgiven too, since the predicate is a list of what we choose to forgive rather than a claim about which shape the library raises; and an empty list (a freshly loaded chat, nothing mounted yet) still presses End rather than treating "no last node" as a failure. **+6 (2026-08-16), 40 → 46, for the POSITION the gesture now reports.** `scroll_to_end` returns whether the list reached its end, because its caller cannot tell "fully mounted" from "the gesture went to the wrong element" — both leave the window unchanged — and a `None` here costs the caller the ability to conclude absence at all. Measured by walking out from the last mounted node to the real scroll container, so no container selector can rot silently into "no evidence". Pinned: nothing below the viewport reports the end; pixels remaining below reports not-at-the-end (this is what stops a stuck gesture settling into a false absence); a hair short of the bottom still counts as the end, since sub-pixel layout leaves a fully scrolled container off its own maximum and an exact compare would turn every absence into a refusal; a measurement that fails element-locally reports NO position while the gesture itself still ran (reported as `True` it would license the exact confident absence the signal exists to prevent); a dead driver channel during the MEASUREMENT is re-raised as `SessionLostError`, the same contract as the scroll half — demoted to `None` it would look like an adapter without the capability and park instead of restarting; and a measurement that is not a number (the JS returns `null` when there is nothing to measure) reports no position rather than being read for truthiness. **+6 (2026-08-17, brw-11), 46 → 52, for the state `/json/version` cannot see.** On 2026-08-17 the operator closed the browser window: Chrome stayed alive, `/json/version` kept answering with a valid `webSocketDebuggerUrl` — so every probe in this file's existing evidence set reported a healthy browser — and `/json/list` returned ZERO targets, with Playwright unable to attach at all. `attachable_page_targets` is that missing question, and its tests pin the distinction the caller acts on rather than the parsing: an empty list reports **0** (the window-closed state), a payload of pages reports its count, and only `type == "page"` counts (a service worker is not somewhere a conversation can be driven). The load-bearing pair is that **zero and unmeasurable are different answers** — a refused connection, a non-200 and a body that is not a JSON array all report `None`, because 0 authorises `orchestrator._recover_unattachable_browser` to restart Chrome while an endpoint answering nothing is the ORDINARY `BrowserError` path already covered above, which diagnoses itself and restarts on its own budget. A `/json/version`-shaped dict (right endpoint, wrong path) is unmeasurable for the same reason. Last, the probe is built for the endpoint it is GIVEN, unlike the loopback-only port/`ps` probes: an HTTP GET to `/json/list` describes the browser at the other end, so `gpu-box:9222` is asked about itself (scheme filled in), and an empty endpoint asks nothing. The HTTP call goes through a `_urlopen` seam so the file's no-real-machine rule still holds. |
| `test_failure_digest.py` | 10 | **Added 2026-08-02.** A refused commit must be diagnosable from its own record. The summary kept only the LAST line of output, which for pytest is the count line — discarding the `FAILED <file>::<test>` lines printed immediately above it, so a refusal could only be diagnosed by re-running the tree by hand. Driven by the real output shape including ANSI codes (pytest emits them under `-q` when it thinks it has a tty, and `capture_output` preserves them into blocker text): the failing test is NAMED, the count still survives, escapes are stripped, every failure is listed up to a cap, and a 400-failure run stays under 700 chars while saying it truncated and keeping the total — silent truncation would read as 'only 12 failed'. Non-pytest output (ruff/tsc, empty, whitespace) still reports something, and a lone FAILED line is not duplicated. **The boundary it must not weaken:** the digest reaches `state.last_validation` and from there the transcript, blocker records and the review packet, so a test drives a real `load_validation_env` and asserts a password, JWT key and DB user are all redacted out of a failure message that contains them — while the test name still survives. |
| `test_path_suggest.py` | 15 | **Added 2026-08-02.** Detect-and-confirm for `approved_paths`. **The load-bearing test is `test_suggesting_queues_nothing`:** a suggestion is not an authorization, and the endpoint that proposes a scope must never be able to queue one — the mutation routing it to `_submit_task` fails there. The rest is false-positive control, because a detector offering a plausible WRONG file is worse than one offering nothing: prose colliding with a real function name is ignored (`report` is both an English word and a def — the first version matched it, and the shape rule of snake_case/CamelCase is what stops it rather than a blocklist needing an entry per collision), an ambiguous basename is dropped rather than guessed, and a non-existent path is never invented. Positive cases: explicit paths, a directory getting its trailing slash, a uniquely resolving basename, an identifier resolved to where it is DEFINED (not merely mentioned), and a file the task will create. Bounded to 12, and scanning is proven to leave the repo byte-identical — it runs against a checkout the loop may be mid-round in. Driven against a REAL git repo, since `suggest` shells out to git. |
| `test_merge_window.py` | 32 | **Added 2026-08-02.** Two dead ends from the operator and the loop sharing one branch. **`merge-window`:** an in-flight CANDIDATE closes the window — the case a phase check misses, and the one that stranded four tasks in a day, because "no agent is running right now" was mistaken for "safe to merge". A quiet loop with no candidate is open; an executing phase closes it; an execution record with no candidate does NOT (nothing reviewed to discard); a torn record is skipped rather than fatal; `--wait` gives up and reports instead of hanging. **+3 from dogfooding:** run against the real repo the moment it was written, it reported a COMPLETED task and a QUARANTINED one — records outlive the work they describe, and counting those would close the window permanently on work that can no longer be stranded, which is the cry-wolf failure it exists to prevent. Finished work is now skipped, while a LIVE task and an id the registry has never heard of both still close it (the mutation widening the guard fails against the first). **The stale completed-task park:** resolving a park by publishing its candidate and completing the task used to leave a session that could only be archived, since `block` refuses a completed task and the fail-closed branch escalated that to loop_fatal. Now it continues, clears the session, and leaves completion untouched. The fail-closed branch is pinned intact by two companions: an UNKNOWN task still escalates (the mutation making every refusal 'stale' fails there), and an ordinary task_fatal park still quarantines. **+7 on 2026-08-04, when the window turned out to be shut PERMANENTLY:** the only exemption was the registry's terminal states, and nothing at runtime ever marks a task completed (AUTOLOOP_TODO B10 — `mark_completed` has no runtime caller and `Decision` has no terminal member), so every task that published closed the window for good. A PUBLISHED candidate is now exempt — its reviewed object is durable on its own side branch — and the cases pin what "published" is allowed to mean: push INTENT alone is not publication (the orchestrator writes `intended_remote_ref` BEFORE the network call, so a refused push leaves an identical record on disk), a ref at a DIFFERENT sha is not publication, and an unverifiable remote keeps the window shut. A record with no push intent never reaches the network at all; an executing phase still closes it even when everything is published; and the exemption prints the residual (a published record is still re-dispatchable and would park on `task_base_behind_head`) rather than hiding it. The three mutations — trust the intent, fail OPEN when unverifiable, drop the phase blocker — fail against 4, 1 and 3 cases respectively. **+2 for `--wait`, which polls every 15s by default:** a confirmed publication is memoized for the life of one invocation (three published candidates would otherwise cost hundreds of round-trips an hour, and a throttled remote turns every fail-closed lookup into 'could not verify' — the wait talks itself into never opening), while a NEGATIVE is re-checked on every poll, since becoming published is the event `--wait` exists to notice. Mutating either direction fails one of the pair. **+1 fail-closed:** a `state_dir` that does not exist is not evidence of safety; it is relative in the shipped config, so running from a sibling worktree globbed an empty directory and printed OPEN — hit for real while dry-running this change. **+6 on 2026-08-15 (rel-01) for the THIRD exemption — a record that is a DEFECT, not a hazard.** `release` used to leave its execution record in place, and on 2026-08-14 fourteen such records (all bound to the pre-merge HEAD) held the window shut on work that existed only inside quarantined worker repos; it could not reopen by itself, since every one of those tasks would have had to be re-dispatched AND re-published first. The exemption needs all THREE conditions, and there is a test per condition so no two of them can carry it alone: the task is back in the queue AND the `worktree_path` it recorded is gone AND the checkout answers that it cannot resolve the candidate. `test_a_vanished_worker_is_not_enough_while_the_commit_is_REACHABLE` is the sharp one — a reachable commit can still be stranded by a moved base, so the worker being gone proves nothing on its own. `test_a_record_with_NO_recorded_worker_path_still_closes_the_window` pins the distinction the whole branch rests on: "we never recorded where it was" is not "we know it is gone", which is also why the existing `..._for_a_LIVE_task_...` and `..._with_no_push_intent_...` cases (both `worktree_path=""`) are untouched by it. `test_an_IN_PROGRESS_task_is_never_written_off` keeps the case the command exists for — a dispatched round's worker can be missing for reasons that are not retirement. `test_a_checkout_that_cannot_answer_keeps_the_window_shut` is the fail-closed half (git being unable to answer is not git answering "no such object"), and the acceptance case asserts the note SAYS it is ignoring a record that should have been retired — a defect must be visible, not silently swallowed. Deciding it never touches the network, asserted directly. **+2 in the same task's final round, closing a fail-open in the third condition:** the checkout's answer used to be "`read_commit` raised", which is not an answer at all — a `GitOperationDenied`, a corrupt object or an I/O error dies exactly like a missing one, so a repository merely having a bad day could open the window on real in-flight work. A failed read now asks `GitGateway.object_exists` (True/False from `cat-file -e`'s exit code, raising on anything else), and both new cases keep the window SHUT: `test_a_READ_that_fails_for_any_other_reason_keeps_the_window_shut` (both probes refused by policy — and it asserts `notes == []`, so the record is not written off *quietly* either) and `test_a_candidate_that_IS_there_but_unreadable_keeps_the_window_shut` (the object is in the database, the read fails anyway — the affirmative answer wins). The mutation that treats any `read_commit` failure as absence fails both. **+1 for the published note's RESIDUAL**, which used to be unconditional: since publication writes a confirmed `published_sha`, a record that knows what the remote said is reconciled on the next revise rather than parked, and `test_a_record_that_KNOWS_it_published_reports_no_park` pins that the note says so — while the pre-existing `..._reports_the_residual_rather_than_hiding_it` (a record with no `published_sha`) still reports the park, so the two halves of the branch are pinned against each other. |
| `test_rebase_stale_base.py` | 12 | **B9, plus the publication half of the record/lifecycle drift (+4 on 2026-08-15, rel-01) and the deferral pin (+3, same day).** The original four: a base the branch has moved past is re-pointed at HEAD with the old worker QUARANTINED and `attempt_count` preserved (a moving base must not refill the retry budget); a record a reviewer has already seen REFUSES instead, naming both shas; an up-to-date base and a base that is not an ancestor at all are both left alone. Real git throughout. **The new three cover the drift the other way — PUBLISHED while the record still describes work in flight**, which `merge-window` reported for `audit-0002` on 2026-08-15 ("safe to merge past, but its record still reads in_progress, so a later revise would park it"). Benign for merging, latent for revising. `test_a_published_candidate_is_reconciled_instead_of_parking` asserts the park does NOT happen, that the record is retired (archived with its worker under one label naming what shipped) and that the registry is reconciled to COMPLETED. `test_a_record_the_remote_does_not_confirm_still_parks` is the authority test: `published_sha` only says where to go and ask, so a ref sitting at someone else's commit parks exactly as before and leaves the record alone — the mutation that trusts the record's own claim fails here. `test_a_QUARANTINED_task_is_parked_not_reconciled` closes the sharp edge in the reconcile itself: `mark_completed` refuses a quarantined task and `_mark_task_completed` swallows that refusal to a log — right after a push, wrong here, because the record would already be retired and the task would end up with no record, no completion, no park and nothing behind the blocker the operator was about to answer. The guard therefore lives in the reconcile, not in `_mark_task_completed`, and this test is what pins it. `test_an_unpublished_reviewed_candidate_parks_exactly_as_before` is the regression guard for the case the park exists for; that park is what keeps thirteen reviewed candidates recoverable (see `test_auto_merge.py`). **The pin (+3):** retiring a record is right for one nothing reads anymore and wrong while an auto-merge DEFERRAL is still waiting to be drained from it — `AutoMerger.attempt` reads the candidate back off the live record, so archiving it makes the next drain skip the task, and skipping CLEARS the deferral. `test_a_record_an_UNDRAINED_merge_retry_still_needs_is_pinned` asserts the whole shape: no archive, no worker quarantine, the deferral intact, the task COMPLETED (the merger only touches completed tasks, so leaving it in progress would lose the retry just as surely) and no park — the park's own message tells the operator to archive that record. `test_a_DRAINED_retry_leaves_the_record_free_to_retire` is what stops "pinned" decaying into "never retired": clear the deferral, and the next dispatch retires exactly as before. `test_a_deferral_store_that_cannot_be_read_pins_the_record_too` is the fail-closed half — a corrupt deferral file reads as "assume one is outstanding", since a dropped retry is indistinguishable from work that was never merged. |
| `test_auto_merge.py` | 27 | **Added 2026-08-14.** Auto-merge — a completed task's published branch reaching the base. Real git throughout, base branch `work` so `protected_branches` does not swallow the integration path. **The happy path asserts the REMOTE**, not just the local head: HEAD moved, contains the candidate, still contains the old base, tree clean, `origin/work == HEAD` — a merge that never leaves the checkout is the same invisibility one level down, so a test satisfied by a local merge would ship the bug. **The gate, and the mutation that kills a passing-but-wrong version:** an unpublished in-flight record (`review_round=1`, no push intent) closes the window and the merge DEFERS; its companion then calls the real `_rebase_execution_if_stale` on that record and asserts it survives — merge anyway and the base moves past its pinned `task_base_sha`, which parks a reviewed record on `task_base_behind_head`. That is the 2026-08-06 thirteen-stranded case, expressed as an assertion rather than a comment. A deferral is durable on disk and DRAINS on the next completion (both tasks end up merged and pushed, deferral gone) — otherwise "defer" is a politer word for "never". A PUBLISHED candidate does not hold the window shut, or every task the loop ever finished would block every merge. **Fail-closed before mutation:** a remote base moved by a second clone is caught before anything merges (head unchanged, remote untouched, no `auto_merge_merged` entry, deferral names the moved sha); a dirty checkout defers, since an abort can only promise to restore a state that was clean. **Conflict:** a base commit touching the same lines → abort, head byte-identical, tree clean, nothing pushed, and `conflicted_files == ["shared.py"]` — the files are NAMED, read before the abort clears them. **Verification, the two mutations that matter:** `merge_commit` stubbed to a no-op is a FAILURE ("HEAD did not move") with nothing pushed — a merge command returning 0 is not evidence; and a head that gained the candidate but LOST a base-only commit is a rewrite, not a merge, and is refused too. Plus: idempotent re-entry (`already_integrated`), a task quarantined between publication and integration is skipped not merged, a protected base merges locally but is never pushed while the deferral is KEPT for retry, and the flag defaults off both as a unit (`PolicyConfig().auto_merge_enabled is False`) and end-to-end (nothing moves, nothing deferred, nothing logged). **Whitelist (7):** `merge` is the first mutating checkout subcommand on it, so a branch name, `HEAD` and two shas are each refused (`git_merge_commit`), `--abort` carrying anything else is refused (`git_merge_abort_shape`), an unknown merge flag is still refused, and the two legal shapes are admitted. **Store (4):** repeated deferrals bump `attempts` instead of duplicating, the drain is oldest-first, and a corrupt record raises rather than reading as absent — a dropped retry is indistinguishable from the unmerged-forever state the module exists to end. **+1 on 2026-08-15 (rel-01), where this module meets the execution record:** `test_a_pending_retry_survives_the_reconciliation_of_its_own_record` drives the full sequence end to end — t1 publishes and defers behind an in-flight t9, the base then moves under t1's record, and `_rebase_execution_if_stale` reconciles it. Retiring there would have archived the very record `attempt` reads the candidate back off, so the next drain would SKIP t1 and skipping clears the deferral: published work quietly stops being retried, which is the backlog this module exists to end, rebuilt one task at a time with auto-merge switched on and apparently working. The test asserts the record and the deferral both survive reconciliation, and then that the retry still lands — t1's candidate reaches the remote base on the next completion, no deferrals left, and `auto_merge_skipped` is empty, which is the entry a lost retry would have left behind. |
| `test_merge_sweep.py` | 62 | **Added 2026-08-15 (merge-03).** The other half of integration: branches nobody is going to report. `auto_merge.py` reacts to a completion it SEES; on 2026-08-06 seven completed tasks were published and unmerged at once with the base still at d2d4d6b, and a hand-written `git ls-remote` loop was what noticed. Real git throughout, base `work`, and the fixture builds the backlog DIRECTLY — a real commit on a side branch, a real push to a real bare origin, an execution record, a completed task — rather than driving the loop, because that is exactly the shape the sweep must cope with: work published by a process that is long gone. **The five the task specifies:** a published branch that is not an ancestor of HEAD is merged and the base PUSHED; one already an ancestor is skipped SILENTLY (`sweep_entries() == []` and the merger never called — a log line per already-merged task would bury the handful that matter, and a second merge of it is a head-move for nothing); branches are attempted oldest-publication-first; the sweep STOPS at the first conflict with the base byte-identical and the remainder untouched; and a shut gate defers the WHOLE sweep. **The order test drives both key paths at once** — two records with no `published_at` (the field only exists from 2026-08-15, so its absence dates the record older than any record that has one) ordered by their candidates' committer dates, then two with one, published in an order that is neither the expected one nor the registry's insertion order. Its real-git companion is the reason order matters at all: `second` edits the file `first` created, so publication order merges cleanly and `auto_merge_conflict` stays empty. **Two stop tests, not one:** the conflict-first case asserts the base never moved at all, and the conflict-halfway case asserts the opposite half — everything before it landed and was pushed, everything after it was never attempted (`never-tried` is not in HEAD). A merge that fails VERIFICATION stops the sweep too (`merge_commit` stubbed to a no-op → "HEAD did not move"), because `AutoMerger._merge` deliberately does not undo one and stacking the next branch onto that head is worse than a conflict. **The gate test asserts the absence that makes "whole, not part" checkable:** no `MergeDeferral` records at all — the sweep keeps no queue of its own, since the work-list is re-derived from ancestry every run. **Ancestry, never a name:** a branch published under `refs/heads/some/other/naming` is still merged, and a completed task whose branch was DELETED from origin is NAMED (`merge_sweep_unresolved`) rather than merged from the record's own claim. **And a branch it could not judge is never reported as a clear backlog** — `test_a_remote_that_cannot_be_REACHED_never_reports_a_clear_backlog` makes `remote_ref_sha` raise, which is what an offline run looks like, and asserts `is_clear is False`; the CLI companion exits 1 and prints `UNJUDGED`. Without that pair the tool answers "I looked, there is nothing there" for a run in which it could not look, which is the 2026-08-06 invisibility rebuilt one layer up, inside the thing written to end it. `_candidate_publication` deliberately cannot tell "the ref is gone" from "the remote did not answer", so the report must treat both as unresolved. Plus: an executing phase defers, a dirty checkout stops, a non-completed task is left alone, the flag defaults the whole feature off, the CLI exits 0 swept / 1 stopped / 1 flag-off and names the untouched remainder, `run` sweeps before the loop starts, and a sweep that blows up returns `failed` instead of stopping the run — it runs before the loop has done anything, so an integration problem that prevented startup would be strictly worse than the unmerged branch it was fixing. **+13 in the second round, closing two ways the sweep could still say "clear" without having looked.** *(a) Metadata, not just the remote (6+2).* The remote was the only unjudgeable thing named; an unreadable RECORD was logged and then `continue`d, so a sweep whose only completed task had a torn record returned `nothing_to_do` with nothing skipped — `is_clear` true, exit 0, nothing inspected. `test_an_execution_record_it_cannot_READ_is_never_a_clear_backlog` uses exactly that shape (one completed task, `{not json` on disk) and its CLI companion exits 1 while printing which thing would not answer; `test_startup_REPORTS_a_completed_task_it_could_not_judge` is the third caller, and the sharp one — the outcome is still `nothing_to_do`, so any check written as an outcome comparison rather than `is_clear` returns early and prints nothing. Three more states join it, each with a test: no record at all (completion implies a confirmed publication, so nothing names the branch), a record naming no candidate, and a RETIRED record. The retired TRIO is what keeps this from crying wolf, since `retire_execution` archives on publication being *confirmed*, not merged: `..._whose_work_IS_in_the_base_stays_silent` asserts `sweep_entries() == []` when the archived copy names a sha already ancestral to HEAD, and `..._is_NOT_in_the_base_is_unresolved` asserts the branch is named, the base untouched and `auto_merge_skipped` empty — it is read, never merged from. `test_a_LEGACY_archive_with_no_published_sha_is_judged_on_ancestry_alone` is the one that fixes the rule rather than testing it: requiring the record's own `published_sha` would be stricter than the LIVE path (which asks ancestry and nothing else) and stricter with a DATE on it, since the field only exists from 2026-08-15 — so every archive written before then would report unresolved forever with its candidate demonstrably merged and nothing an operator could do. Git is authoritative about what is in the base; the remote's agreement answers a different question, and is corroboration rather than a precondition. `test_startup_stays_quiet_when_the_backlog_really_is_clear` is the control that stops the reporting tests passing vacuously. *(b) Enumeration evidence is not mutation authority (3).* The `seen` cache of confirmed publications was filled by the enumeration and handed to every later `attempt`, so branch N was merged on an `ls-remote` taken before branches 1..N-1 ran — a delete or force-move during the minutes a sweep spends merging and pushing was HIDDEN by the positive cache rather than caught by it. `MeddlingMerger` wraps a REAL `AutoMerger` and moves the remote between branches, so these drive the actual `ls-remote` path instead of stubbing it: with the second ref DELETED after the first lands, and again with it FORCE-MOVED to another sha (the sneakier half — the ref still exists), the second candidate must never reach `AutoMerger.attempt` at all (`merger.attempted == ["first"]`), the sweep stops with `stopped_outcome == UNCONFIRMED`, and the remainder is reported. `test_the_re_confirmation_costs_one_lookup_per_branch_not_two` pins the other direction by counting real round-trips: exactly two for the side branch, since evicting only the candidate's own key lets `_candidate_publication` re-add it and `attempt`'s own check read what `_reconfirm` just obtained. A twelfth asserts `UNCONFIRMED` is minted here rather than borrowed from `auto_merge` (nothing reached the merger, so one of its slugs would report a decision it never made) and that it stays out of `_CONTINUE_ON`, which is what makes it stop the sweep. **+13 in the third round, closing two ancestry/identity holes that survived the second.** *(a) Naming an unjudgeable task and sweeping on is not safe (6).* The sweep exists BECAUSE a later branch may be cut from an earlier one, so excluding an unresolved task does not exclude its work: `test_a_branch_DESCENDED_from_an_unjudgeable_task_is_not_merged_either` publishes A, cuts B from A, deletes A's ref while B's is still confirmed, and asserts that neither reaches HEAD — merging B alone would make A an ancestor of HEAD anyway, the refusal to merge A undone transitively by the very next branch in the list. `contains(B, A)` is asserted in the setup so the test cannot pass by accident. The invariant is therefore per-INVOCATION: any enumeration-time unresolved ⇒ `HELD`, nothing merged, base and remote byte-identical, `auto_merge_merged` empty. `test_a_task_whose_RECORD_will_not_load_holds_the_sweep_the_same_way` covers the less provable half — a record nobody can read names no commit, so the relationship is not merely unknown but unaskable — and `test_the_hold_is_per_INVOCATION_not_per_lineage` pins the deliberate over-approximation with a branch that shares nothing but the base, since excluding only descendants needs the ancestry of a commit the sweep may be unable to resolve at all. `test_an_unjudgeable_task_with_NOTHING_to_sweep_is_not_reported_as_held` is the control that keeps `held` an honest claim about withheld branches rather than a synonym for unresolved (`pending == []` under "N branch(es) left untouched" would read as a lie), and the CLI companion asserts the operator sees both halves — the task that could not be judged AND the branch waiting on it. `test_a_HELD_sweep_REPORTS_and_still_lets_the_loop_start` is the second clause of the invariant and what stops the hold becoming a denial of service: it drives the real `_cmd_run`, asserts `_run_locked` was reached, that nothing merged on the way, and that both the unjudged task and the waiting branch are printed — a startup sweep that refused to let the loop start over one unjudgeable task would be strictly worse than the branch it is reporting. *(b) An older — or foreign — archive cannot answer for a newer publication (7).* `TaskExecutionStore.archive` deliberately keeps every generation (release → retry → the `published-<sha>` retirement that completed the task), and "the first integrated archived copy wins" therefore clears a task on the strength of a superseded attempt. `test_an_OLDER_archived_retirement_cannot_stand_in_for_the_newest` builds exactly that: attempt one lands and is released, the retry that actually completed the task sits unmerged on its branch, and the older copy is asserted ancestral so the test fails against the old rule rather than passing vacuously — the task must stay unresolved and the reason must name the NEWEST copy. `test_the_NEWEST_retirement_being_in_the_base_clears_a_task_with_older_ones` is the control in the other direction: a superseded generation is *not* required to have landed (a released attempt's candidate is usually abandoned), so demanding all of them would report every retried task unresolved forever. Generation is read off the archive FILENAME, and two tests pin that parse where it can actually break — `test_the_retirement_stamp_is_read_off_a_label_retire_execution_really_wrote` drives the REAL `retire_execution` with a `-`-containing reason so a change to the label format fails here instead of quietly making every multi-generation archive unorderable, and its unit companion covers the rejections. The two fail-closed branches get one each: an unstamped label among several is unresolved rather than guessed at (`cannot be put in order`, naming the file), and a newest copy that will not load is unresolved with no older readable one allowed to answer in its place. `test_another_TASKS_archive_cannot_answer_through_a_shared_id_PREFIX` closes the collision that judging-the-newest made sharper rather than safer: the archive is globbed by filename prefix and task ids may contain `-`, so `rt-1-*.json` matches `rt-1-b-published-<stamp>.json` — and with the sibling holding the newer stamp its copy BECOMES `rt-1`'s newest generation, answering with a commit that says nothing about `rt-1` while `rt-1`'s own branch sits outstanding. The sibling is genuinely integrated in the fixture, so the test fails against the unguarded version; each copy is now checked against the `task_id` it carries and dropped only when it PROVES it belongs elsewhere, since an unreadable or owner-less copy that cannot be told apart must be kept. **+10 in the fourth round, closing the post-mutation startup hole: a stop is not automatically a restoration.** The sweep stopped for the right reason and then the startup hook reported it and dispatched roadmap work onto the resulting checkout anyway — which defeats the reason for stopping, since `AutoMerger._merge` deliberately does NOT undo a merge that failed verification (`reset` is off the git whitelist by design) and there is no policy-legal automatic undo. `test_a_merge_that_moved_HEAD_and_failed_verification_stops_the_LOOP_starting` drives the real `_cmd_run` through a REAL merge — real commit, real head move, all three ancestry checks satisfied — that then leaves a file behind so verification fails on its last check, and asserts `_run_locked` was never reached, exit 1, the merge genuinely in the checkout and never on the remote. `test_a_conflict_that_restored_the_base_still_lets_the_loop_start` is the control that stops the guard degrading into "any stop blocks": a conflict aborts back to the exact pre-merge head with a clean tree, so the loop starts normally — blocking there would turn one unmergeable branch into a loop that will not start, the same denial of service the HELD outcome avoids. **The case that cannot be classified by outcome slug (2).** `_push` catches a refused push and calls `_defer`, so it returns `auto_merge.DEFERRED` — what a shut gate and a dirty checkout also return, both of which touch nothing — over a base that has already moved locally. `test_a_REFUSED_push_leaves_the_base_moved_under_the_deferred_slug` and its startup companion reproduce it with no mock at all, by putting the base branch in `protected_branches` (the pairing `PolicyConfig` documents: auto-merge on is not by itself permission to push `main`), and `test_a_dirty_checkout_defers_and_still_lets_the_loop_start` is the same slug on the harmless side — the tree was ALREADY dirty when the attempt found it and is dirty identically afterwards, which is why the comparison is against the pre-ATTEMPT observation rather than against "is the tree clean". So the answer is never read off the slug: `_probe` observes HEAD and `status --porcelain` immediately before each attempt and again the moment one does not land, and `is_reconciled` is that comparison. `test_a_conflict_whose_ABORT_did_not_restore_is_not_called_restored` (`merge_abort` stubbed to a no-op) makes the sweep reach the same conclusion `_abort`'s own `restored: false` does, independently, because the sweep is the layer deciding whether anything else may run there. `test_a_probe_that_cannot_read_the_checkout_is_not_read_as_unchanged` pins the fail-closed half at the helper — "could not look" is not "nothing moved", the same rule the enumeration applies to a branch. **The crash path (2).** An exception reaching `sweep_on_startup` came either from construction (before anything could move) or from the transcript write that follows a stop (after one), and `FAILED` cannot tell them apart — so it is not asked to, and both `test_a_sweep_that_blows_up_never_stops_the_run` (now asserting `is_reconciled is True`, nothing withheld) and `test_a_crash_that_left_the_base_MOVED_is_not_waved_through` drive the same comparison to opposite answers. **And the report is made truthful on the same boundary:** `_format_sweep` printed "the base is exactly as it was before this branch" for EVERY stopped outcome, which is the one line that would send the operator away; `test_the_command_does_not_claim_a_base_it_left_MOVED_is_where_it_was` asserts that line is absent, that both ends of the move are named on screen, and that the report says why nothing here will undo it. |
| `test_heartbeat.py` | 13 | **Added 2026-08-02.** The loop publishes liveness OUTSIDE the checkout so a monitor can judge without touching a protected path. Placement is pinned for both reasons independently: it is not inside the checkout, and writing it is proven invisible to the REAL escape detector (snapshot, publish, diff, nothing reported) — the bug the pause flag had. Contents: phase/session/pid/timestamp, and open blockers turn a `running` beat into `blocked` with `needs_attention`. Writing never raises (a monitor is an accessory and must not take down the run it watches) and leaves no `.tmp`. **The standalone checker** is asserted to import nothing from `autoloop` — it is copied outside the repo and run from there, so an `autoloop` import would break it exactly where it must work. Fresh beat = exit 0; stale = exit 1; a clean `stopped` and a `paused` beat are NOT alarms even though both are stale by definition (judged before staleness, or every deliberate stop would cry wolf); a missing or unparseable heartbeat still reports, because a monitor that goes quiet when it breaks is the worst kind. |
| `test_health.py` | 31 | **Added 2026-08-02.** `autoloop health` — the read-only verdict a scheduler acts on. The tests that matter most are the ones proving it stays QUIET, since a monitor is only as good as its false-alarm rate: a long audit fan-out (silent 90 min, agent alive) is NOT stuck — that is the commonest false alarm and the mutation removing agent suppression fails here; a deliberate pause is a decision, not a fault; and a `task_fatal` park on a live loop is not escalated because continuous mode quarantines that task itself. The alarms: open blockers, a `loop_fatal` park, a FAILED session, a stale lock (crash), a loop that is not running, and a live-but-silent loop with no agent. The threshold is honoured in both directions. Transcript reading takes the newest entry from the TAIL only (a 400-line file proves it) and survives a missing, empty or half-written final line — a torn line during a concurrent append must not crash a monitor. Finally `check()` is proven to write NOTHING: it runs on a schedule, possibly mid-round, and a write into the state dir is exactly what parks the loop for an escape. **+11 on 2026-08-18 (hlth-01) — machine sleep must not read as silence.** The incident shape verbatim: a 224-minute-old transcript with 220 of those minutes proven asleep is NOT stuck (the probe is also asserted to receive exactly the transcript-to-now window, so the wiring cannot silently pass a wrong interval); the SAME wall-clock age awake throughout IS stuck — the mutation guard that fails a version discounting all silence rather than proven sleep; partial sleep short of explaining the silence is still stuck, with the discount named in the detail; unavailable wake history fails toward quiet and says why in the detail (a false 'stuck' discredits the monitor, a missed detection is retried minutes later); and a live agent wins BEFORE wake history is consulted — the sleep probe raises if reached, pinning evaluation order, so the proof-of-work rule stays load-bearing. Existing check() tests keep deterministic verdicts via a default awake-throughout probe in the `_check` helper, since the real probe reads the test host's own sysctls. **+6 on 2026-08-18 (hlth-01 revision) — partial wake history must not authorize the alarm.** Verdict-level, against the real `machine_sleep_in_window` over a synthetic darwin history (`_darwin_history` monkeypatches boot time, platform and the sysctl reader): a 100-minute silence holding TWO 30-minute sleeps — the sysctls carry only the second — is NOT stuck, because the 40 pre-pair minutes are unprovable and must not be credited as awake (the round-1 code reported 70 awake minutes here => falsely stuck); a transcript predating the current boot is NOT 224 awake minutes even though 'no sleep this boot' is true — `lock.boot_time_epoch` is the shared boundary and pre-boot off time joins the discount; and the anti-blindness mutation guard: partial history with an hour of PROVEN awake tail since the last wake IS stuck, so a version that goes blanket-quiet whenever history is partial fails. The platform evidence itself: darwin windows starting at or after kern.sleeptime get the exact overlap (the pair characterises [sleeptime, now] completely — a window after the wake is a REAL zero), while a window predating the last sleep credits only the tail since the wake as awake and discounts the unprovable rest, with the note saying so; 0/0 sysctls are a REAL zero ('never slept this boot' — established history, so stuck stays reachable) while unreadable or inconsistent sysctls (wake stamped before its sleep) are unavailable; an unreadable boot time is unavailable wake history (evidence describes a boot the window can no longer be tied to), and pre-boot window time is added to the discount with a 'pre-boot' note; the timeval parse is pinned to `lock.boot_time_epoch`'s kern.boottime format so the two modules keep reading one evidence family; and linux CLOCK_BOOTTIME−CLOCK_MONOTONIC is clamped to the window in both directions with zero suspend a real zero. |
| `test_pause_location.py` | 7 | **Added 2026-08-02.** The pause flag must live OUTSIDE the snapshotted tree. It used to sit at `state_dir/PAUSE`, and `escape_detector` enumerates ignored paths on purpose (`.autoloop/` is gitignored, so an agent forging state there is exactly what it catches) — so running the documented `pause` while a task was dispatched created a file the detector reported as an escape and parked the loop `loop_fatal`. The regression test drives the **real detector**: snapshot, create the flag, diff, assert nothing reported — plus a control proving the OLD location still trips it, so the test cannot pass vacuously. Placement is pinned to `workers_root`'s parent, the same guarantee `inbox_dir_for` relies on. Legacy handling: a flag written by an older build still pauses (ignoring it would leave the operator with a loop that keeps running), `pause` writes only the new path, and clearing removes both. |
| `test_recovery_commands.py` | 13 | **Added 2026-08-02.** Two dead ends an operator could previously only escape by hand-editing state — both hit for real. **`release`:** returns a task stranded IN-PROGRESS by a loop-fatal park to pending (`next_ready` skips in-progress forever, and `unblock` refuses anything not `blocked`). Narrow by construction: pending, quarantined AND completed are all refused, so it cannot un-complete finished work or launder a quarantine — the quarantined case is then shown still needing `unblock`. The CLI clears BOTH halves, since a stale worker repo makes the next dispatch refuse; the worker is MOVED to quarantine and the test reads its file back out, because an interrupted round usually holds real work. **`archive-blocker`:** closes a blocker whose session is retired, writing `archived_reason` and asserting `answer is None` — the distinction the command rests on. It REFUSES a blocker belonging to the live session, which is the test that stops it becoming the 'clear the escape detection' button `_RESOLUTION_PRECONDITIONS` deliberately withholds; plus empty reason, unknown id, and already-closed. **+3 on 2026-08-15 (rel-01) — the THIRD half `release` silently left behind.** It cleared the status and the worker repo and left the `TaskExecution` record in place, `candidate_sha` and all, claiming live unpublished work for a task that would be redone from scratch; `_merge_window_blockers` reads those records, and 14 of them held the merge window shut on 2026-08-14 until an operator archived them by hand. `test_release_retires_the_execution_record_alongside_the_worker` asserts the live record is gone, that it is ARCHIVED rather than deleted (the candidate it names lives only in the worker being quarantined, so dropping the pointer is the one irreversible half of the command), and — the actual point — that `_merge_window_blockers` returns no reasons afterwards. `test_both_halves_are_filed_under_the_same_label` is the structural guarantee: one operation, so the quarantined worker and the archived record carry the SAME per-call label and name each other on disk; it also asserts the label is not a constant, which is what stops the equality being vacuous. `test_the_record_is_retired_before_the_worker` pins the ORDERING, which is a real decision rather than an accident — either half can fail, and a left-behind record is silent (it holds the window shut and nothing announces it) while a left-behind worker is loud (the next `create()` refuses the directory and says so), so the record goes first and whichever half fails, the survivor is the one that reports itself. The no-worker case gained an assertion that absence is a no-op for the record too. **+3 on 2026-08-16 (task-retire-01) — `retire`, the third dead end, and the only one that produced a wrong ANSWER rather than a stuck task.** Six of the seven `blocked` rows on 2026-08-14 were retirements saying so only in free text, so the dashboard's blocked count meant "needs you" and "needs nobody" at once; `tasks._RETIREMENTS` migrates those six on load, and this command is the route for every retirement after them plus the fallback when a reason no longer matches that table. The command records the successor, and the test then reads back everything it must NOT have touched — the reason, the approved paths, the task itself — because nothing here is ever deleted: the chain is the only record that brw-07/brw-08 continue brw-02/brw-04. It takes a task stranded IN-PROGRESS (dash-01's shape, and the reason this is not `release`'s pending-only guard), recording no successor for work that went stale rather than being replaced. And it refuses completed work and a malformed successor id, with the registry re-read afterwards to show a refused retirement wrote nothing — validation runs before the single assignment, so rejection is atomic. **+5 on 2026-08-16 (task-retire-01, review round 2) — the blocker record is the other half of a retirement.** A quarantine lives in its own file, read independently of the registry, so retiring a quarantined task used to move only `tasks.json`: the row said RETIRED / "waits on nobody" while `start`, `health` and the heartbeat stayed stopped on the question that task had parked with. `test_retiring_a_quarantined_task_closes_its_blocker` asserts the record is CLOSED, that its `archived_reason` names the retirement and its successor, that `answer` is still None (nobody answered it — writing text there would forge the operator confirmation `_RESOLUTION_PRECONDITIONS` demands) and that the original question survives verbatim. The sweep's boundaries are pinned next to it: a second quarantined task (`audit-0003`, the one genuine failure) and a `(loop)` blocker both stay open, since a login expiry is a loop-level condition no task retirement answers; and a retirement with no blocker at all is a no-op, which is the common case. Plus the two CLI-level halves of "written once": `retire` run twice with no `--superseded-by` leaves the chain intact and reports "already retired … nothing changed" instead of `retired -> retired`, and a second `retire` carrying different successors or a different reason exits 1 with both fields re-read from disk unchanged. |
| `test_start_command.py` | 10 | **Added 2026-08-02.** `start` — repair what is provably safe, report what is not. The safe half: a provably-dead lock is cleared, a silent CDP is restarted via the DECLARED command and re-probed, and the pause flag is cleared because `start` is an explicit request to run. The refusals are the point, and each is a separate test: an open blocker is reported with its exact `answer` command and is asserted STILL OPEN afterwards; a session parked at `needs_user` is reported with its question and left at that phase, never auto-answered; a `failed` session gets its recovery command; a silent CDP with no `restart_command` refuses rather than inferring which Chrome to kill. Plus the bug the first draft shipped: a LIVE lock is the healthy already-running case, so it exits 0 without 'needs a decision' — and it must not run the repairs at all, pinned by making the CDP probe raise if reached (restarting a working run's browser mid-request breaks a run that was fine). `--check-only` never launches. **+2:** a `task_fatal` park with no open blocker does NOT stop `start` — `_handle_parked_task` quarantines that task and continues, which is the whole task_fatal/loop_fatal split, so refusing on every `needs_user` would send the operator to resolve something the loop handles itself; a `loop_fatal` park still stops it. **+2:** a synthetic audit unit is registered before `block` so it can actually be quarantined — audit units are minted per run and never planned, so `block` refused them as `task_unknown` and the park escalated to loop_fatal, meaning EVERY audit that failed its own post-commit validation stopped the loop; registration is pinned narrow (`audit-<4+ digits>` only, not `audit`, `audit-x`, `auditing-0001`, or a real task) and idempotent. **+4 on 2026-08-16 (task-retire-01, review round 2) — a retired task must not keep the loop stopped.** `start` refuses while any blocker is open and reads that list from the blocker files, not the registry, so a retirement that moved only `tasks.json` left the loop halted on a question about superseded work. The end-to-end test runs the whole path with TWO quarantined tasks, which is what makes the claim "solely": both blockers open → `start` exits 2 and `health` reports `stuck_blocked`; `retire` one → `start` still exits 2 but names only the OTHER blocker and `health` counts 1; `answer` that one → `start` exits 0 and `health` is no longer `stuck_blocked`; and the retired task's record is read back closed, unanswered, with a machine reason. The pre-state path is covered separately, because it has no command to hook: a `blocked` row whose reason still matches `_RETIREMENTS` is re-filed on load, and `start`'s preflight is where its orphaned blocker gets reconciled. The two negatives: a genuinely quarantined `audit-0003` is untouched and still stops `start`, and an unreadable `tasks.json` is REPORTED (`start` exists to say what is wrong rather than die of it — the loading it now does for the sweep must not change that). **+2 on 2026-08-16 (task-retire-01, review round 3) — the sweep is scoped by KIND, not only by task id.** It archived every open blocker belonging to a retired task, and a `loop_fatal` park routinely names whichever task was in flight when it fired (`checkout_escape_detected`, `primary_checkout_dirty`, a worker/publisher environment failure), so retiring that task manufactured resolution of a loop-wide safety condition and let `start` proceed into a checkout that had been written from outside the worker. `test_retiring_a_task_never_closes_a_loop_fatal_blocker_naming_it` gives ONE task two blockers with distinct codes — `find_open` matches on `(task, code, phase)`, so two records sharing a code would silently upsert into one and the test would prove nothing, which is why the count is asserted as 2 before the retirement — then asserts the split: the quarantine closes with `answer is None` and a machine reason, while the loop-fatal record is still in `open_blockers()`, still makes `start` exit 2 and `health` report `stuck_blocked`, and is `asdict`-identical to its pre-sweep snapshot (not resolved, not archived, not re-worded, not even a bumped `recurrences`/`last_seen_at`). `test_the_historical_sweep_cannot_clear_a_loop_fatal_record_either` pins the same rule on the path with no command behind it: `brw-02` is re-filed by `tasks._RETIREMENTS` on LOAD, so `start`'s preflight is the sweep that sees it, and a migration silently clearing a dirty-checkout record would be the worst version of this — nobody typed anything. Its quarantine is closed and named on screen while the loop-fatal record survives byte-identical and `start` still exits 2. Both are allowlist tests: an unrecognised or empty `kind` takes the same untouched path, matching `_to_needs_user`'s fail-closed default. |
| `test_recovery_paths.py` | 8 | **Added 2026-08-02.** The two escape hatches an operator reaches for when a run is wedged — both of which used to look like they addressed the problem and quietly do something else. **Rotation budget:** a new run starts fresh (the incident — a dropped network spent the one rotation and every later `run --retry` re-read the same count and parked identically); the reset logs `rotation_budget_reset` with the forgiven count, so "every run rotates" stays visible rather than being silently absorbed; an unspent budget rewrites neither state nor transcript, which is the test that fails if the reset is made unconditional and therefore fires per iteration — the shape that would delete the cap; no session yet is not an error; and the cap still binds within one run. **`reset`:** the default archives the session and KEEPS the registry (asserting both that the tasks survive and that the output does not claim to have archived them), `--tasks` opts into archiving both and leaves a recoverable `.bak-` path, and the un-confirmed prompt names which of the two it would take — the old prompt said "archives the current session state" while also taking the roadmap. |
| `test_dashboard.py` | 51 | The live tracker. **Read-only is the load-bearing property** — `collect()` is proven to leave the observed checkout byte-identical (the reason every `git` call carries `--no-optional-locks`: plain `git status` rewrites `.git/index`, and a dirty checkout makes the escape detector refuse the next write-capable task). Plus: the roadmap falls back to the tracked seed file when no registry has been saved yet (reading only `tasks.json` showed an empty roadmap while `next-task` correctly reported rt-01), app tasks come from the newest audit report rather than the registry with markdown emphasis stripped, and a parked loop marks stages blocked with nothing running. **+5 for visual encoding (2026-08-01):** status colours are never used for a non-status state (`--good`/`--warning`/`--serious` must not appear in the mark map; `--critical` still must, for `blocked`) — verified to actually discriminate by doctoring the page string and confirming the assertion flips; every drawable state ships an icon + word so colour never carries meaning alone; `pipeline()` across four scenarios only emits states the page has a mark for (an unknown state renders as an undefined fill — invisible, and silently so); the validated mark hexes are the ones the CSS actually ships, in both modes, so editing the palette without re-running `validate_palette.js` fails here; and stat-tile values use proportional figures while `code` columns keep `tabular-nums`. **+4 (2026-08-03) — it had been lying:** the header read `stopped` while the loop was executing, because liveness matched `pgrep -f "autoloop run --continuous"` (never matches a loop started as `autoloop start`, which calls the run path in-process) and read the lock as `lock.json` when the file is named `LOCK`. Liveness now goes through `LoopLock` — the same authority `autoloop health` uses, and boot-aware, so a lock whose pid was reused after a power cut reads STALE rather than running, a distinction `ps -p` cannot make. Driven against reality in both directions: no lock → stopped, a lock held by the test process → running with that pid, a dead-pid lock → `stopped — stale lock`. Plus the audit parser: it expected a markdown TABLE row, a format the auditor stopped emitting, and matched ZERO rows in BOTH reports on disk — so that panel had been silently empty for longer than the newest report existed. It now reads the `#### <domain>:<id> — <title>` form (40 findings in the real report) and still honours the retired table form. **+3 for priority editing (2026-08-01):** the POST write path leaves the observed checkout byte-identical (a write into `.autoloop/` mid-run would trip the escape detector and park the loop loop-fatal — the reason the inbox lives outside the checkout); a priority request carrying `approved_paths` is REFUSED, so the form cannot change authorization; and the roadmap is sorted by priority so what the operator sees matches what `next_ready()` would pick. **Superseded in part on 2026-08-16 (dash-04):** the first of those tests now pins that a QUEUED priority request still touches nothing observed (the inbox kind survives for hand-written requests), because the dashboard itself no longer queues one — see the dash-04 entry above the table. **+6 there, plus that rewrite:** the edit is applied and read back immediately (with `collect()` re-reading the same file the endpoint wrote, so the page and the write can never point at different paths); nothing but `priority` moves, and no other file in the state dir is touched except the always-empty mutex file; a request carrying `approved_paths` is refused at the endpoint too, not merely at the inbox gate it no longer passes through; an unknown id and a non-integer are reported rather than silently reverted; the edit refuses to CREATE a registry that does not exist yet; and it lands while a REAL other process holds `LoopLock`, with the wall clock asserted. **+6 for task authoring (2026-08-02):** these drive the REAL handler over a real socket (`ThreadingHTTPServer` on port 0, `urllib` with a timeout on every call), because a test calling `TaskInbox.submit` directly would pass with the endpoint deleted — routing and field handling are the whole point. The queued request must carry the operator's paths **verbatim and in order** (`approved_paths` is authorization surface: nothing inferred from the title, nothing defaulted, no wildcard), and the checkout is proven byte-identical across the request. A task with no usable path is refused rather than queued, because the registry accepts an empty scope and the orchestrator then refuses to dispatch that task for ever — a queued trap. A field the form cannot produce (`validation`, `depends_on`) is refused, not dropped: it means the request came from somewhere else. The routing guards hold on the new endpoint too — missing `X-Autoloop` → 403, foreign `Origin` → 403, unknown path → 404, and *nothing* reaches the inbox in any of the three. Pending authorization is readable as text before the merge (`collect()["inbox"]` carries the paths; the page renders the field rather than a count) — the loop applies a creation request without asking again, so that window is the only chance to notice a scope nobody meant to grant. And the form is static markup with exactly one listener: `render()` rebuilds sections on a 2s poll, so a JS-built form would erase half-typed text and queue one task per accumulated listener. See `docs/SECURITY.md` S28. **+9 for merged vs unmerged (2026-08-06):** seven tasks read `completed` while none of their code was in HEAD, and nothing on the page said so. Five tests drive REAL git through `collect()` against a fixture built in the production shape — an observed checkout, an `origin`, one branch that is an ancestor of HEAD, and one pushed from a **separate clone** so its objects were never fetched here. That separate clone is the whole point: `merge-base --is-ancestor` then exits 128 rather than 1, which is the path all seven real tasks took, and a fixture that committed the branch locally would exercise the easy exit code and prove nothing. So: an ancestor renders merged; a branch on origin and not in HEAD renders unmerged; a completed task with no branch anywhere renders `not published` rather than being folded into either; an unreachable remote (origin re-pointed at a nonexistent path, which fails instantly instead of stalling on the ls-remote timeout) renders **unknown** — asserted as `!= "unmerged"` as well as `== "unknown"`, so rendering a network hiccup as not-merged fails here — while a task whose own commit is provably in HEAD still reads merged, because the record supplies a commit id and git decides ancestry; and the count is checked to equal the rows AND to tally nothing unknown. `is_ancestor` is exercised for all three verdicts directly (exit 0, exit 1 against a local non-ancestor, and absent-from-the-object-database). Two pure-logic tests pin the evidence rules without a repo: the execution record is positive evidence only (no-branch + commit-not-in-HEAD is `unpublished`, unreadable remote is `unknown`, commit-in-HEAD is `merged` — it can never produce a negative), and the naming convention may FIND a branch but decides nothing about it. Last, the count is pinned as a stat tile rendered before every section, with icon + word beside the number. An autouse fixture clears the module caches: ancestry keyed on the sha alone would leak between repos, since `make_repo` commits fixed content with a fixed author and no `GIT_AUTHOR_DATE` and so produces identical shas in different directories within one wall-clock second. **+8 for live progress on the running task (2026-08-14):** the figures must come from GIT IN THE WORKER, so every fixture's `task_execution` carries an absurd `report_summary`/`report_details` claim (424242 insertions across 31337 files) and the tests assert those numbers appear nowhere in the payload — a worker with a tracked edit and an untracked file renders 7/1/2 read from `git diff --numstat <task_base_sha>` plus the untracked scan, because counting only tracked changes shows 0 lines for a round that wrote hundreds. The mutation with teeth is the UNREADABLE worker (`shutil.rmtree` before the read): insertions/deletions/files must be `None`, never zero and never the agent's claim — a fallback to the executor's own report is the one edit that fails it. Unknown is all-or-nothing: a failing `ls-files` (monkeypatched `_run_checked`) makes the whole figure unknown rather than reporting the tracked diff alone, which would understate exactly the agent whose output is new files. An idle loop (`task_execution` cleared at publish, `current_task` still naming the finished round) yields `progress is None` and the page ships the section hidden, so the last round's numbers can never sit beside a stopped loop. Elapsed comes from the dispatch stamp (`state.current_task.started_at`, a fixed 2026-08-06 timestamp hours from any fixture mtime, `now` injected → exactly 1500.0s); a `current_task` naming a different task makes elapsed unknown while the git-derived lines still render. And the worker repo is proven byte-identical across `collect()` — the same read-only property the observed checkout has, now extended to the directory the loop's agent is actively writing (this is what `--no-optional-locks` buys, and why the progress reads go through `_run_checked` rather than a bare `subprocess.run`). Two page-side pins finish it: `renderProgress` must interpolate all four fields (`p.insertions`/`p.deletions`/`p.files`/`p.elapsed_seconds`), because a payload carrying the numbers is not a page showing them — deleting one interpolation leaves every `worker_progress` test green while the operator sees nothing; and `progress` stays out of the re-render signature and is rendered before the unchanged-payload guard, since its clock ticks every poll and leaving it in would rebuild the whole DOM every 2s. **+10 for the roadmap grouped by state (2026-08-15, dash-03):** one flat list of 20+ tasks shows an operator nothing, so the roadmap is split into five counted groups — and the load-bearing property is WHERE the state comes from. `test_the_groups_come_from_state_of_and_never_from_the_status_string` is the one to keep: two tasks with the identical stored `pending` land in Ready and Blocked (the difference is derived from dependencies and never stored), while the task whose stored status IS `blocked` lands in Needs a human and neither of the others — a page reading the status string is wrong in both directions at once, and the same test pins that every `TaskState` member is claimed by exactly one group, so a new state cannot render nowhere. One test per group places a task in it and asserts what that group exists to say: In progress carries `candidate_sha` + `review_round` from the execution record (driven through `collect()`, since reading those records is half the feature) and says "no candidate committed yet" for a task dispatched moments ago rather than rendering a blank cell; Needs a human carries the blocker reason and is proven NOT to be the Blocked group; Ready holds only the dependency-satisfied pending task and marks the head "next to be dispatched"; Blocked NAMES the incomplete dependency and omits the completed one; Done is counted, `collapsed`, never `hidden`, ships as a `<details>` with no `open` attribute and states on the page that done means published to the task's own side branch, not merged. `test_the_ready_group_is_in_next_ready_order` is the anti-drift device for the one duplicated line in the module — `_ready_order` repeats `next_ready()`'s `(priority, id)` key because simulating that method means COMPLETING tasks and this page may call no mutating method — so the test runs the real `next_ready()`/`mark_completed()` loop on its own registry and demands full equality, with a fixture whose insertion order differs from priority order, a priority tie to exercise the id tiebreak, and a blocked task waiting on the IN-PROGRESS one so completing the ready set can never add a pick mid-loop. `test_needs_a_human_renders_explicitly_when_empty_and_other_groups_do_not` pins the asymmetry: that group is `hidden: False` at count 0 while an empty In progress and Blocked are `hidden: True`, because silence and "nothing needs you" must not look identical. `test_an_unreadable_task_graph_says_so_rather_than_rendering_empty_groups` (renamed 2026-08-16 when the sixth group landed — it said "five") covers the distinction an empty list has to carry — a roadmap with no tasks still returns every group zeroed, so `[]` can only mean the graph did not load (a dangling dependency, which `from_dict` accepts and `state_of` then raises on, must not take the page down). And `test_the_page_renders_every_group_with_its_heading_and_count` is the display pin, because a payload carrying the groups is not a page showing them: every interpolation reaches the DOM, the Save binding stays scoped to `#roadmap`, and the Done rows carry no priority input (one under `#done` would render with no listener). No new `node --check` test — the grouped rendering lives in the same `<script>` block `test_the_served_javascript_actually_parses` already extracts and parses, so a broken escape in it fails there. **+5 for the Retired group (2026-08-16, task-retire-01):** a sixth group, and the one whose absence made the panel still readable-wrong — six of the seven `blocked` rows were superseded work. The keeper is `test_a_retired_task_is_grouped_apart_from_both_kinds_of_blocked`: three tasks, three "not running" states, one per group, so folding a retirement into either kind of blocked fails here. The successor is the row's content (`superseded by brw-07, brw-08`) and `superseded_by` reaches the payload as a list, so the chain is followable from /data.json rather than only from prose; a retirement with no successor falls back to its reason (dash-01 went stale rather than being replaced) and, with neither, says "retired; no successor recorded" rather than rendering a blank cell that reads as a broken panel. The collapse test pins the trap that comes with a SECOND collapsed group: `groups.find(g => g.collapsed)` returns the first one, so it is asserted absent from the script and both disclosures are looked up by key — left alone, the Done box would have quietly filled with retirements, silently. The end-to-end test drives `collect()` against a `tasks.json` in the shape actually on disk (still `blocked`, successor in free text): the migration runs when the registry LOADS, so the page shows the retirement before the loop has written anything, `audit-0003` still lands in Needs a human, and the flat `roadmap` row agrees with the group — without that overlay the app-task panel marks brw-02 "blocked" two panels below the Retired group listing it. `test_the_groups_come_from_state_of_and_never_from_the_status_string` needed no edit and covers the rest: its every-`TaskState`-is-claimed-by-exactly-one-group assertion is what would have failed had the new state been added without a group. |
| `test_validation_env.py` | 43 (1 skipped) | **Added 2026-07-31 — the validation-environment boundary (`docs/SECURITY.md` S27).** These tests ARE the deliverable: the module's value is a set of negative guarantees. The writer cannot observe the six validation variables (a real `implement_agent_runner` argv+env capture, asserting neither the values nor the file path appear in either, and that `PATH` survives the strip), and neither can a read-only audit subagent or a worker git subprocess. The validator receives exactly the allowlist, with an ambient same-named value OVERRIDDEN rather than inherited; with no file configured it receives none rather than ambient ones (the fail-closed half). Provider keys (`ANTHROPIC_API_KEY`, `LLM_API_KEY`, `OPENAI_API_KEY`, `AWS_SECRET_ACCESS_KEY`, parametrized) are refused as unknown keys, and the refusal message never quotes the value. Unknown/duplicate/malformed/`export `-prefixed lines, empty values, each missing key (parametrized over all six), and a sub-8-character secret all fail closed. Location and permission refusals: 0o644/0o640/0o604/0o660 (parametrized), a symlink refused BEFORE resolution, relative path, missing file, inside the checkout / state dir / `workers_root`. The repo-declared `DB_NAME` is refused while `DB_HOST=localhost` is explicitly allowed (pinned so nobody adds a host refusal later). A failing validation summary redacts every value including the password, JWT key, user and database name; `repr`/`str`/`describe()` never show values. A validation failure still returns `status="error"` with `changed_paths` populated — the shape that drives quarantine-and-consume-an-attempt. **Credential delivery is proven end-to-end without a database:** a REAL `python3` subprocess launched through `run_validation_commands` uses asyncpg to dial a loopback listener the test owns, and the Postgres startup packet on the wire carries exactly the file's `DB_USER`/`DB_NAME` (the listener must answer asyncpg's SSLRequest with `N` first — see `docs/COMMON_ERRORS.md`). The one SKIPPED test, `test_real_db_validation_command_succeeds`, runs a live `SELECT 1` and is skipped unless `AUTOLOOP_TEST_VALIDATION_ENV_FILE` points at a real dedicated test database; its docstring carries the exact command. |
| `test_config_repo_section.py` | 46 (29 functions; three parametrized, ×6, ×12 and ×2) | **Added 2026-08-16 (port-02) — the `[repo]` section, i.e. the constants that used to name THIS repository.** The load-bearing test is the first one, `test_defaults_are_exactly_the_previously_hardcoded_constants`: "behaviour here is unchanged" is the entire premise under which the hardcoding was allowed to become configuration, so a drift in any default silently changes a repository that never opted in. `test_the_duplicated_default_spellings_agree` pins the two constants `validation_env` and `dashboard` deliberately repeat rather than import (the dashboard must render against a checkout `load_config` would refuse) — duplication is only safe while something asserts it. **Trackers — the section is about what the config may NOT do.** `[repo].tracker_paths` was written and withdrawn in review the same day (`docs/SECURITY.md` S31): the config is gitignored, and the suffix blocklist offered as its bound accepts `.env`, `Makefile`, `Dockerfile`, `Gemfile`, `.gitignore` and extensionless scripts. Those six are `BEHAVIOUR_CHANGING_FILES`, and `test_a_config_edit_cannot_newly_authorize_a_behaviour_changing_file` drives each one through a real `load_config` and the orchestrator's own accessor: the config loads (refusing would break an unmigrated deployment), the value is discarded, and the granted set is exactly the task's paths plus `TRACKER_PATHS`. Two structural pins do what a behavioural test cannot — `test_tracker_paths_is_not_a_repo_config_field` fails the moment the key is reintroduced, and `test_the_suffix_blocklist_and_its_validator_are_gone` fails if `validate_tracker_paths`/`_NON_TRACKER_SUFFIXES` come back as dead code for a future caller to trust. `test_the_dropped_key_is_reported_rather_than_silently_ignored` reads the operator notice (value quoted back, `autoloop/tasks.py` named), since a setting that loads and does nothing reads as configured while behaving otherwise. Plus: an unscoped task gains nothing for ANY list, including a long one (finding #2's circular-ownership bound restated as a property of the task), an empty list grants exactly what the task declared, and the default argument is the constant. **The DB marker:** read from the configured file+key (`env.sample` / `POSTGRES_DB`), with the defaults asserted NOT to see it — the marker is genuinely configured, not sniffed. `test_the_marker_says_where_to_look_never_what_to_refuse` is the one that keeps it a marker rather than a denylist: the value is still read out of the repository, so a config cannot forbid a name the repo does not declare. End to end through `load_validation_env`, with the SAME file accepted under the default marker in the same test, so the refusal is proven to come from the configured lookup. **The dashboard glob:** `app_tasks` reads the configured location and `collect` is driven separately, because a parameter nothing passes is inert; an absolute, traversing or empty glob yields no rows instead of raising (`Path.glob` raises outright on an absolute pattern, and a page whose whole contract is to keep rendering must not 500); and an unconfigured or unparseable checkout falls back to the default, which is why the module reads raw TOML at all. **The orchestrator:** `test_every_orchestrator_call_site_reads_the_one_accessor` SCANS `orchestrator.py` and demands exactly three `effective_approved_paths(` calls, each carrying `self._tracker_paths()` — a behavioural test cannot make this claim, since changing one site leaves every other test green while that site authorizes something different, and a site disagreeing with its neighbour is worse still (the dispatch seed and the every-dispatch re-sync compare and assign the same value, so two lists rewrite the execution record forever). The count is asserted as well as the content, so a wrapped or added call cannot slip past. Same idiom as `test_dashboard.py`'s `PAGE` interpolation checks. `test_the_accessor_returns_the_reviewed_constant_and_reads_no_config` answers "where does the active per-repository declaration come from" in both directions: the accessor returns `TRACKER_PATHS` off a real `AutoloopConfig`, AND the last statement of its body is scanned to be `return TRACKER_PATHS` — a value assertion alone would still pass for an accessor reading a config field that merely defaults to the constant, which is precisely the withdrawn design. Plus: an unknown `[repo]` key is refused, not ignored (a near miss like `tracker_path` must not read as the consumed key). **+16 collected / +4 functions on 2026-08-16 (port-02, review round 2) — the blank-looking value that was a second, undeclared opt-out.** `_load_repo_section` validated a path only `if value.strip()`, so `env_example_file = "   "` skipped `_repo_relative` entirely and reached `repo_declared_db_name`, which reads any blank marker as "this repository declares no application database" — turning the validation-database refusal OFF while reading as configured. Empty is the DOCUMENTED opt-out; padding must not be a second one. The rule is now exact: `""` is allowed, every other value goes through `_repo_relative` (which already refuses padding), and both keys get it, so `audit_report_glob` cannot drift into the looser rule later. `test_a_whitespace_env_example_file_cannot_disable_the_database_guard` is the one to keep — it proves the consequence rather than the syntax, in three steps: the guard really refuses the application database, a blank marker really would switch it off (`repo_declared_db_name(repo, "   ") == ""`, and the same env file then loads through `load_validation_env`), and no config can reach that state because the loader refuses `"   "`. The padding cases are parametrized over both keys × six spellings (whitespace-only, tab, leading, trailing), and the opt-out itself is pinned per key — an unpinned `""` could otherwise be hardened away later, silently removing a documented setting. `test_a_non_table_repo_value_is_refused_by_the_loader_itself` covers the other half of strictness: `repo = "x"` used to raise `dict()`'s raw conversion error instead of a `ConfigError` naming the section. Its scalar is written BEFORE any table header on purpose — TOML binds a bare key to the section above it, so appending it after `[paths]` yields `paths.repo`, refused by that section's key check, and the guard under test is never reached. Counts hand-counted (no shell in the worker) and are what this change added, not a re-audit of the total. |
| `test_prompts.py` | 24 | Template library incl. `audit_kickoff`, `smoke_test`, `postcommit_review`, `changeset_review`; strict rendering (the per-template parametrized case, `sorted(TEMPLATES)`-driven, picked up `changeset_review` automatically — +1 with no test file edited); payload helpers. **+1 on 2026-08-16 (auto-03):** no template BODY offers a decision in `contract.RETIRED_DECISIONS`. `CONTRACT_INSTRUCTIONS` is not the only reviewer-visible text — every payload template ships in the same prompt and several name decisions outright ("reply `commit`…", "reply `revise` with feedback"), so a retired decision surviving in one of them re-offers the directive policy refuses unconditionally, and the contract-side test would not see it. Bodies only, deliberately: `policy_denied_payload` renders a verdict reason, and the retirement's own denial names the retired decision on purpose. |
| `test_context.py` | 16 | Review context: stamp values, porcelain parsing, previous decision/task, validation + roadmap summaries, truncation. **+8 on 2026-08-14 for the `in_flight` line** the finish-before-start preference reads: in-progress count and how many hold an unpublished candidate; a completed task's `candidate_sha` is not counted as awaiting approval (the count is scoped to in-progress tasks); an in-progress task with no commit yet counts as in-flight only; a roadmap with nothing in flight and an empty roadmap both render zeroes with the roadmap line unaffected; **no execution store, or an unreadable record, renders "unknown" rather than 0** — 0 is precisely the value that says "start something new" — while `TaskExecutionStore.load` still raises on the corrupt record; and the label pinned across files (`context.IN_FLIGHT_LABEL` appears in both the rendered block and `NEXT_WORK_PREFERENCE`). **+4 on 2026-08-15 for the `roadmap` line's ready counts**, which `AUDIT_VS_READY_PREFERENCE` reads the same way: a queued roadmap renders `3 ready (2 at priority 1)`; a roadmap whose queue is empty renders `0 ready (0 at priority 1)` and no `next ready:` pointer (the case where an audit IS the right call — 0 has to be stated, not inferred); ready tasks at the default priority 100 count as ready but not urgent; and the second cross-file label pin (`context.ROADMAP_LABEL` appears in both the rendered block and the clause). The mutation these guard: drop either count from `TaskRegistry.summary()` and the rendered block no longer carries the numbers the rule is written against. |
| `test_conversation.py` | 5 | Provider registry + interface conformance without playwright. |
| `test_codex_provider.py` | 33 | **The Codex CLI reviewer + recorded quota failover (added 2026-08-01).** No codex binary anywhere — `CodexRunner` is a protocol and every test injects a fake, exactly as `audit/agents.py` does for `claude`. The quota classifier is pure, so it is fully testable without the CLI: a zero exit is **never** exhaustion whatever the text says (soft stop — that turn produced a review); five real-shaped exhaustion wordings are recognised; an unrecognised failure degrades to an ordinary failure rather than guessing; patterns are overridable without touching code; the failure digest is bounded and carries no prompt. Adapter: a successful invocation confirms and returns the reply; `submit` **never** returns UNCONFIRMED (no state in which "we might have sent something" is true); a clean exit with empty stdout is REJECTED, not CONFIRMED handing the parser an empty string; `reconcile` is authoritative in-process; a captured request re-submits nothing; `idempotent_submit` is declared and `retarget`/`current_url` are **absent**, so rotation is unreachable; every failed invocation is logged including unrecognised ones (the thing that makes the first real exhaustion a config edit); a missing binary names `codex login`; the argv preview carries no prompt. Failover: an exhausted primary hands over and re-issues on the browser; the handover clears only per-transport marks and leaves the request id and bytes intact; the response records which reviewer produced it; no fallback / fallback-equals-primary / no request in flight / **a captured reply pending** / budget spent each park `loop_fatal` with their own codes; state beats config after a switch so a resumed run does not re-exhaust the same allowance; an idempotent provider never parks on ambiguity while a non-idempotent one still does (the regression guard on the capability probe not leaking). |
| `test_transport_recovery.py` | 57 | **Transport recovery (added 2026-07-31): submission classification, the one same-chat resend, bounded conversation rotation.** `RotatingFakeClient` keeps server truth PER conversation and only mints a `/c/<id>` when a project chat takes its first turn, so rotation's submit-then-capture ordering is modelled rather than assumed. Covers: a persisted send never resends or rotates; a REJECTED send reconciles absent and earns exactly ONE same-chat resend (and a successful resend never rotates); a second confirmed rejection rotates exactly once; **UNKNOWN acceptance parks and never resends or rotates**; a `stage="complete"` response-timeout, login expiry and generic BrowserError (rate limit / capacity) never rotate; a `ConversationUnusableError` does rotate, and charges the fault to the rotation budget only (`consecutive_failures` stays 0); rotation is refused when `project_url` is unset, when the cap is reached, and — the interesting one — when the new chat does not hold the request after reconciliation (a URL alone never binds); per-request conversation binding, so a historical request reconciles against ITS url and an unbound request after a rotation raises rather than guessing; the config heal is surgical (comments and a same-named key in `[policy]` survive), atomic (no `.tmp` left), refuses a git-tracked file byte-for-byte, and refuses a URL that would break the TOML; the drift guard accepts only a recorded rotation; `SendObservation` has no field that could hold a header/cookie/body and query strings are stripped from logged paths; a restart preserves the request URL, epoch, rotation count and the `rejected` verdict; the replacement chat's prompt is the original plus exactly one continuation line, same request id, re-stamped. **+8, same day — the "silent conversation" entry condition (confirmed, persisted send, model never starts):** two `stage="start"` timeouts are an ordinary retry, never a rotation; the THIRD, plus a final `reconcile_no_response` confirming continued silence, rotates (and resets `start_timeouts`/`start_timeout_wait_seconds` to 0 on the fresh conversation); a reply appearing during that final reconciliation cancels the attempt and resets the streak; no duplicate send occurs across the three timeouts — the only send is the rotation's own, into the replacement; every read after the rotation follows the request's new binding, so a delayed reply in the retired conversation is structurally never read; the replacement reuses the SAME request id; a second silent-conversation rotation attempt (budget already spent) is refused and parks `loop_fatal` exactly like the other two triggers' cap refusals; and a restart mid-streak (two timeouts persisted, zero rotations) resumes the count from disk under a FRESH client and completes the same rotation a live run would have.  **+2 (2026-08-03) for content-based rotation:** when the address bar never leaves the project page, the chat is found by the request id it CONTAINS — the failure that stopped the loop three times while the chat existed and held the request. Its companion pins the guard: when the search finds nothing either, the rotation still refuses rather than adopting an unrelated chat. That second test initially passed for the wrong reason — the fake persists a CONFIRMED submit into the chat it rotated to, so "seed nothing" does not model an empty project, and the rotation was legitimately succeeding; the empty search is now forced explicitly. **+9 (2026-08-03) for the project-membership check:** a chat URL carrying the project SLUG (`/g/g-p-<id>-<name>/c/<id>`) is inside the project, which a plain prefix compare against `/g/g-p-<id>/project` rejects — the bug that stranded a rotation after it had already created the chat and posted the request into it. The companion pins why the old check was never discriminating: it also rejected the conversation the loop had been using all day. Plus a no-slug chat, the segment-boundary trap (`g-p-abc` must not match `g-p-abcdef`), a different project, a chat outside any project, the landing page itself, a foreign host, and an empty/missing `/c/` segment. **+9 (2026-08-16) for resolving a FALSE `submission_ambiguous`.** `reconcile()` reads a VIRTUALIZED window, so on 2026-08-05 `alr-af11e1b3-0006` parked a human while the conversation held the request *and* its answer (`decision push`); the by-content search from the same day mounts the tail and sees what the reload could not. The asymmetry is the design and each half has its own test: a request the search proves present in this request's OWN conversation resolves to `awaiting` with **`client.submitted == []`** — the assertion that makes it safe rather than merely convenient — and no `submission_ambiguous` entry at all; a genuinely absent one still parks; a search that raises `ConversationSearchInconclusive` still parks (said-nothing must not read as absent, and `presence_search_inconclusive` records that it was not a clean miss); a hit in a DIFFERENT chat parks too, naming that chat in the question, because rotation reuses the request id and rebinding on a duplicate is a rotation performed on the wrong evidence. Two preconditions fall through to the unchanged park with their own reason codes — a provider without the capability (probed via `getattr`, which is what keeps `test_codex_provider.py`'s fake parking exactly as before) and no `browser.project_url` to search; both also assert on the park TEXT, because the first draft claimed "a by-content search did not find it either" in a case where no search had run, which is manufactured evidence pointing at `--resubmit`. A `LoginExpiredError` raised by the search parks `login_expired` with `submission_unconfirmed` as the resume phase, never `submission_ambiguous`: it is a `BrowserError`, so a search-site clause catching that base would report every logged-out profile as an ambiguous submission — the exact misclassification this change removes — and nothing else pins that. The last one is the guard on the whole change: after the park, re-running the loop sends nothing no matter how often, and only `cli._authorize_resubmit` — driven for real, not simulated — produces the single send. Making this testable needed `RotatingFakeClient.unmounted`: the server holds a request that `has_request`/`reconcile` (window reads) go blind to while `find_conversation_with` still sees it — without those two truths the fake resolves on `reconcile` and every test passes with the fix deleted. One test is pure URL logic and exists because the trap is production-only: `find_conversation_with` builds candidates with `urljoin`, so the same chat comes back as `/c/<id>` without the project prefix the request is bound to, and `Orchestrator._same_conversation` compares the conversation id rather than the string — a string compare would park every rescue in production while passing every test that types URLs by hand. **+3 (2026-08-16) pinning WHICH search failures may become that park.** Only the search's own refusal may: a `SessionLostError` and a bare `BrowserError` (parametrized, so nobody can read the fix as "only the subclass is routed") take the ordinary browser-failure route instead — `browser_error` logged with the right `kind` and `phase`, `consecutive_failures == 1`, the phase left intact for the retry, and **no** `submission_ambiguous` / `presence_search_inconclusive` entry, because a dropped CDP connection is not evidence about what is in the conversation and collapsing it would also throw away the restart path `_handle_browser_failure` exists to run. Asserted at `max_steps=1` deliberately: a sticky error retried past the failure budget lands in `failed` and hides the phase under test. The third is the deliberate exception — a `ConversationUnusableError` from the search IS collapsed into the safe park, and the test asserts `submitted == []`, `retargets == []` and `rotations == 0`, because that error's normal route ACTS (`_attempt_rotation` posts the request id into a replacement chat) and the search reads the project page and up to `limit` OTHER chats, so propagating it would let a stranger's wedged chat license a repost of a request the backend may already hold. **+ the park TEXT is now pinned as evidence-only (2026-08-16).** The question used to open "the request is not in persisted history after reconciliation" — a claim `reconcile()` cannot support, since it reads a mounted window — so the no-project, inconclusive, wedged-page and cannot-search parks asserted absence and then said, one sentence later, that absence was never established; that contradiction is what steers an operator to `--resubmit`. The base sentence now reports only what the readback did (it did not SEE the request), and the shared `assert_claims_no_absence` helper checks the four parks that established nothing carry none of the absence phrases (`not in persisted history`, `read its recent chats to the end`, `did not find the request`), each paired with a positive anchor (`could not settle it`, `project_url is not configured`, `cannot search a project`) so a note that vanished entirely could not pass. Keyed on phrases rather than the word "absent" on purpose: the one park entitled to the claim — the search that walked the chats to their end — says "evidence of absence", and its test keeps asserting that wording so weakening every park uniformly fails too. **+4 (2026-08-16) for PRIMING the replacement chat before judging its project membership.** A chat opened from a project page has no durable URL until its first message lands; until then the address is `https://chatgpt.com/c/WEB:<uuid>`, under no project at all. The old wait stopped at the first address that DIFFERED from the project page, handed that placeholder to the membership check, and parked the loop `loop_fatal` on a rotation that had actually worked — an ordering bug that would recur on every rotation. `RotatingFakeClient.placeholder_until(n)` models the real sequence (placeholder for the first `n` address reads, then the real one). The happy-path test asserts **`client.find_calls == []`** — that is what proves it primed rather than being rescued by the by-content search, which is the assertion the old code would also pass without. Its three companions are the rules that must survive: a replacement that opens OUTSIDE the project is still refused with the observed address in the park text, by the address-bar check on the URL that never resolves (the by-content search is forced empty there, because it reads the PROJECT'S list and a chat outside the project is genuinely not in it — the rule is deliberately NOT re-applied to what that search returns, whose candidates arrive prefix-less from `urljoin` and would then all be refused, undoing the 2026-08-03 rescue); an address that NEVER resolves parks on the bounded wait rather than hanging, quoting the placeholder and asserting exactly one submit went to the project page, since a retried send there opens a second chat and orphans the first; and the rotated URL survives a restart — reloaded from the state file, a fresh orchestrator built on a config that still names the retired conversation awaits in the replacement. All four patch `ROTATION_URL_TIMEOUT_SECONDS`/`ROTATION_URL_POLL_SECONDS` (module globals, read at call time) so the bounded wait costs milliseconds instead of a real 30s window. |
| `test_state.py` | 10 | Round-trip (incl. stamps + manifest pointer), atomic save, corruption handling, archive. |
| `test_chatgpt_client.py` | 88 | **Browser transport (rewritten 2026-07-30 for the submission-confirmation repair; +11 for send observation 2026-07-31).** The 2026-07-31 additions use an `ObservingSession` subclass, leaving every earlier test on the no-capability path so the pre-observation behaviour stays pinned: a failed send request is REJECTED rather than UNCONFIRMED; a request that never completed is REJECTED; a mixed failure-then-success window stays UNCONFIRMED; **a 2xx alone never confirms** (the history check is load-bearing) while persisted history outranks a rejecting status; a session without the capability behaves exactly as before; rejection diagnostics carry the observations and no secrets. Plus the narrow rotation trigger: a loaded, logged-in conversation with no composer raises `ConversationUnusableError`, a page that never reached the conversation raises only a plain `BrowserError`, an explicit unavailable marker is unusable, and `retarget` moves every page-identity check. The fake session keeps `persisted` (server truth) separate from `dom` (rendered), so an optimistic-only send vanishes on reload exactly as in production. Covers: attach navigates only when off-conversation (query/trailing-slash tolerant) and never reloads a live page; **optimistic bubble alone never confirms**; bubble disappears on reload; assistant-start and generation-indicator each confirm; composer clearing is not evidence; already-persisted skips sending; focus+select-all+delete+bulk-insert input with content verification; Send never clicked when the editor ignores input or a generation is running; reconciliation finds / doesn't find / waits for late persistence; awaiting never navigates; streaming survives repeated polls; an **older assistant message cannot satisfy the request**; separate start vs completion bounds; page-drift and logout detection; structured secret-free diagnostics + a guard that the session protocol exposes no cookie/storage accessor. **+3 for attachment delivery (2026-08-15):** the composer cannot be proven to hold a large patch — `_enter_prompt` reads the editor back and a 30,000-character part never returns its own tail, so the client refuses to send, correctly and permanently. Uploading the diff sidesteps the editor entirely (measured: a 336 KB `.md` was read in full, quoting canaries from its last line). The three cases pin what must not be lost in the move: the file is attached BEFORE any text is typed, since attaching after would leave the send control live with the prompt but no diff; a submit without an attachment still takes the old typed path untouched; and an upload that silently does nothing — `set_input_files` returns, no chip ever appears — RAISES rather than sending, because a review request for a diff the reviewer never received would be approved unseen, which is strictly worse than a send that fails loudly. `FakeSession` models the silent no-op via `accepts_uploads=False`; without that the guard would be untestable and the failure invisible. **Corrected against the live DOM 2026-08-15 (47 -> 51):** the first cut guessed the attachment selector and was wrong — ChatGPT renders an upload as a file tile carrying `role="group" aria-label="<filename>"`, with NO `data-testid` on it, so `_attach_file` timed out on every real send while the file was visibly attached. Matching the FILENAME is what makes the check proof rather than a guess: a previous attempt's file still on the composer would otherwise read as success and ship a review request whose diff belongs to another change. Two more behaviours the browser taught us, both now pinned: a file already on the composer is NOT uploaded again (a retry finds its own file there, and re-uploading only raises the modal), and a repeat upload produces `modal-duplicate-file` — "You've already uploaded this file" — which is DISMISSED rather than merely detected, because it covers the composer and would block every later attempt too. Dismissing can reveal the file was attached all along, which is a success; if it is not there, the send is refused. **+2 for upload COMPLETION (2026-08-15, 51 -> 53):** the attachment tile is a promise, not a completion. Measured live on a 105 KB file: the tile rendered after 0.5s while the Send control stayed disabled until 6.3s. Clicking in that window was accepted, consumed the attachment, and persisted no turn — request alr-75bdba23-0002 simply did not exist in the conversation afterwards, surfacing as `submission_ambiguous`. The signal is Send becoming enabled, and it only means what it says while the composer is EMPTY — typed text enables it independently, which is why `_attach_file` clears the composer before uploading. Two cases pin it: a slow upload still sends once complete, and an upload that never completes refuses rather than sending. `FakeSession` models the gap via `upload_completes_after`. **+16 for the by-content conversation search (2026-08-16, 53 -> 69):** `find_conversation_with` identifies a rotated chat by the request it contains, and had to be trustworthy before anything could decide on it. Three defects, all pinned. (1) It read a VIRTUALIZED message list without mounting the tail: on 2026-08-05 `alr-af11e1b3-0006` parked as `submission_ambiguous` while the chat held the request *and* its answer — seeing them by hand took End plus six scrolls. Tests: a request in the already-mounted window is found with ZERO scrolls; a request only in the unmounted tail is ALSO found (this is the mutation test — skip the mount and it fails, and the test first asserts the fresh-load window does not show it); a genuinely absent request is still reported absent, but only after the mount has demonstrably read the list to its end (the proof this row's later `+3` and `+5` entries sharpen twice); a list still painting when the mount bound runs out refuses instead of reporting absent, because unseen and absent differ in a virtualized list. (2) It concluded from whatever page it was on, so a rotation mid-flight produced a confident answer about a different chat — tested in BOTH directions (drift onto a chat that HOLDS the request must not return the candidate; drift onto one that does not must not report absent), plus a project page that lands elsewhere refused before any chat is opened. Slug rewrites and `?model=…` are explicitly NOT drift — pinned on both the project landing page (`/g/g-p-<id>-<name>/project`) and a conversation (`/g/g-p-<id>-<name>/c/<chat>`, where a conversation is identified by its `/c/<id>` and not by the prefix ChatGPT rewrites in front of it), because an exact URL compare would refuse the page ChatGPT just loaded and make every search inconclusive in production while passing every test that types its URLs by hand. Tolerating the prefix does not tolerate the id: a drift to a different `/c/<id>` is still refused. A logged-out profile still raises `LoginExpiredError` rather than being demoted to inconclusive. (3) A plain unpacking bug: `elements()` yields (attribute, inner text), so `for _text, href` read each chat's TITLE as its href — no title contains `/c/`, so every candidate was skipped and the search could only ever return None. Refusals raise `ConversationSearchInconclusive` (a `BrowserError`, so the existing call site's behaviour is unchanged) and leave a diagnostics snapshot. Fakes: `FakeProjectSession` (a chat list + per-conversation history behind a virtualized window, keyboard-only) and `FakeScrollingSession` (adds the optional `scroll_to_end` capability); purpose-built because `FakeSession` has one flat `persisted` list and an `elements` that ignores its selector. **+3 (2026-08-16, 69 → 72) for the convergence proof itself, after review found the first cut settling on the node COUNT.** A virtualizer may slide a constant-size window — mounting newer nodes as it drops older ones — so a count that reads 6 before and after a gesture is consistent with six different messages having gone past; the count-based mount stops after two gestures, reads an intermediate window and reports a present request absent, i.e. reproduces the 2026-08-05 park inside the fix for it. The first cut's fakes only ever grew a prefix, so every test passed without touching the hazard. A third fake, `FakeSlidingWindowSession`, mounts `window` nodes from a moving `offset` and drops everything else, and the three cases are: a request reachable only after four gestures is found while the window size never once moves (asserted as `{6}` — the number a count-based proof would have called settled after two), which is the mutation test for judging convergence on CONTENT; a request the window slid PAST (the answered case — on 2026-08-05 the chat held the request *and* its reply, so it is not the last message) is still found, and the test first shows that mounting to the end and asking `has_request` afterwards sees nothing, which is why the verdict comes from what the mount SAW; and a sliding window that comes to rest with the request in none of the chats still reports absent, so the fix does not trade every false absence for a refusal. **+5 (2026-08-16, 72 → 77) for the END-OF-LIST proof, after review found that content alone cannot establish absence either.** An unchanged window says the GESTURE stopped mounting — the tail when the gesture works, the OPENING window when it silently missed (End goes to whatever holds focus) — so a short or initially stable list plus a no-op gesture still produced a confident false absence after two identical reads. Absence now requires the session to report the list is AT ITS END *and* the window to stop changing there; `scroll_to_end` returns that position (True/False/None) and the End-key fallback returns None. Cases: a gesture that mounts nothing while honestly reporting "not at the end" refuses instead of returning None, with the request eleven messages below the stuck window (the mutation test — drop the end-of-list requirement and it reports absent); the same stuck gesture reporting NO position refuses too, so `None` is never read as a quiet yes; that stuck gesture still returns a request already visible in its window, because refusing absence must not cost a sighting; a chat still streaming at the bottom (view genuinely at the end, content changing every read) refuses, which is why the end signal alone is not the proof either; and a keyboard-only session — no `scroll_to_end` at all — refuses on a genuinely absent request, the deliberate price of the fallback having no trustworthy signal. Its positive twin (the fallback still FINDS a tailed request by pressing End) is unchanged. Fakes added: `FakeStuckGestureSession` (a gesture that paints nothing, reporting `False` or `None`) and `FakeStreamingTailSession`. **+11 (2026-08-16, brw-09, 77 → 88) for ChatGPT's ACCOUNT rate limit — the fault whose every passive symptom reads healthy.** Throttled, ChatGPT covers the page with an `absolute inset-0` overlay that intercepts pointer events and removes nothing: the page loads, the messages list, the composer reports present AND enabled, and the page text does not contain "Too many requests". All four were checked live on 2026-08-15 and all four said fine, which is why `FakeSession.throttle()` keeps every one of them healthy — a test passing against a fake that ALSO hides the composer would prove nothing about this fault. The first test is the trap itself, asserted as a pair: `exists(composer)` is True and `attach()` raises anyway. Detection is by `data-testid` and NOT by prose (pinned with the page text shown lacking the phrase), because the wording and locale move while the testid does not, and the prose is not even a fallback — it is a false negative. The error is asserted to be an `AutoloopError` and NOT a `BrowserError`: that is what routes it away from the drop-restart-retry recovery which, on an account-level limit, is the mechanism of the failure rather than the cure. The overnight path is reproduced end to end: `FakeSession.focus` raises the verbatim `Locator.click: Timeout 30000ms exceeded. waiting for locator("#prompt-textarea")` while throttled, and `submit()` re-reads the modal and reports a throttle with `send_attempted` still False — with a paired negative (an ordinary editor that never enables Send still raises `SubmissionError`) so the re-read cannot launder every send fault into a throttle. Same for `await_response`, which would otherwise sit out its whole start timeout and report silence, reading as a slow model rather than a limited account. Dismissal is pinned as three separate rules: it clears the modal and the page works again (a stale modal hides the composer even after the server-side limit expires, so it must not read as a continuing throttle); when the candidate selectors match nothing it reports NOT cleared, because the element carrying the testid is the overlay and the button may be a sibling, so a click that hit nothing must never read as success; and dismissing with nothing up clicks nothing at all. Last, ordering: a logged-out profile still raises `LoginExpiredError` even with the modal present — an auth prompt answered with a back-off would wait out a limit that does not exist. The diagnostics snapshot gains `rate_limit_modal_present`, asserted alongside `composer_present: true` in the same dump, so the misleading half is recorded next to what explains it. **+3 (2026-08-17, brw-11, 88 → 91) for `composer_interactive()`, the positive half of that trap.** The orchestrator's back-off now has to tell a throttled ACCOUNT from a browser it cannot attach to, and "the composer is there" cannot decide it — that is exactly the false all-clear above. So the probe attempts a real click (`focus()`, which is a genuine click in the Playwright transport) and re-tests the modal afterwards, since an overlay can appear during the interaction. Pinned: a throttled page reports NOT interactive with the composer asserted present in the same test (the mutation test — a presence-based implementation passes everything else and fails here); an unthrottled page reports interactive and the click is shown to have actually happened (`session.focused`), so the answer is never a passive read; and a page whose probes RAISE reports not-interactive rather than propagating, because the caller is choosing between three states while a fault is already in flight and an exception there would be a fourth answer. |
| `test_orchestrator.py` | 39 | **RETIRED 2026-07-30 (S21): 19 legacy-path tests deleted** — the 7 change-manifest commit-gate tests (no-manifest/pre-existing-dirty/untouched-path/task-modified refused-or-allowed, commit_and_push, task-completion-on-commit), 2 more testing the retired legacy commit→push_approval cycle and GitError-from-`.commit()` propagation, and 9 adopted-manifest end-to-end tests whose ONLY path (`_dispatch_git`'s adopted branch) is gone — their unit-level equivalents live on in `test_manifest.py`/`test_git_gateway.py` (see the deletion's coverage map in that commit). Two `build()` helpers now exist: `build()` (in-memory `FakeGit`, for anything that never reaches `_dispatch_executor`) and `build_postcommit()` (a REAL throwaway repo + real `WorkerRepoManager`/`TaskExecutionStore`/`IntentStore`, since `FakeGit` cannot back a real `git fetch`) — used by the audit-flow tests, now exercising the produce-then-review path end to end (`_resolve_audit_task`'s synthetic per-run unit id, a `revise` round resuming the SAME id, `postcommit_review` in the outbox). **+2 new:** review requests are serialised through one memoized conversation client across a multi-round session (item 5 of the v1 brief); and two AUDIT decisions dispatched within one session (audit → push → audit) get distinct `audit-NNNN` unit ids, worker repos, and `TaskExecution` records — the collision `_resolve_audit_task`'s synthetic per-run id exists to avoid, and the one case the audit/revise loop test doesn't cover. Remaining coverage: phase gate reprompts; review integrity (stale stamp, HEAD-moved — both now proven via `build_postcommit`, upstream of `_dispatch()` so unaffected by the retirement); context propagation; parse/policy/git/browser failure routing; duplicate-submission prevention + byte-identical resend; budgets; pause; persistence. **Transport repair (2026-07-30):** submitting reconciles before sending, awaiting never reconciles, an UNCONFIRMED send parks in `submission_unconfirmed` and is **never auto-resent** (request id + prompt preserved), late persistence resolves to awaiting without a second send, a resumed `submission_unconfirmed` reconciles before acting, and a prior send attempt blocks an automatic resend even on re-entering `submitting`. **Exactly-once (durable send marker):** the marker is on disk *before* the transport is called (asserted by reading state from inside the fake's `submit`), an exception *after* the click never resends across a restart, a provable no-send clears the marker so a retry may submit, a landed message is adopted on restart without resending, and an await timeout exits with a resumable phase and the request preserved. |
| `test_git_gateway.py` | 27 | **+3 on 2026-08-15 (rel-01) for `object_exists`** — the existence probe `cli._candidate_is_retired` needs before it may write an execution record off. `read_commit` cannot answer that question: `cat-file commit` dies identically for a missing object, a corrupt one, an I/O error and a policy refusal, so its failure proves only that the read failed. Against a real repo: a present commit is True, a well-formed 40-hex that is simply not in the object database is False (with the companion assertion that `read_commit` on that same sha merely raises — the distinction, shown rather than described), and a name git REFUSES (rc 128) raises "not an answer" instead of being reported as absent, which is the fail-open the merge-window gate must not have. The third pins the whitelist: `cat-file -e` and `cat-file commit` are admitted, `-p` and `--batch` are not — the flag is an exit status, never content. **RETIRED 2026-07-30 (S21): the legacy `commit()` tests (exact-path staging, idempotent recovery, post-stage-check hook — 11 tests) were DELETED** along with `GitGateway.commit()` itself (plain `git commit` gated only on manifest provenance — a pre-commit hook could rewrite approved bytes after the check ran). Real git: reads; detached-HEAD; force-push + `add -A` denied; denied commands never reach subprocess; **`push()` no longer exists on `GitGateway`** (removed pass 2b — publishing goes only through `push_exact`, covered in `test_postcommit_primitives.py`). **Immutable-tree adopted commits (`commit_adopted` — the primitive that closed S21 correctly; still live as a tested primitive, no production caller):** a pre-commit hook that rewrites an approved file or stages an extra file cannot affect the commit (each of the four commit hooks fails closed with the hook named); non-executable and `*.sample` hooks do not block; executable symlink hooks do; `core.hooksPath` is resolved; a branch moved after tree verification makes `update-ref` CAS fail without overwriting; an index mutation after `write-tree` cannot alter the commit (and the late edit is reported, not reset); trees with unapproved paths or changed approved content are rejected; the final commit carries the verified tree, single parent and exact message bytes; detached HEAD refused. |
| `test_postcommit_primitives.py` | 40 | Autoloop M1 pass 1, real git: `commit_and_capture` reads the candidate sha honestly from `rev-parse HEAD` (never predicted); a pre-commit hook's rewrite/added-path shows up in `range_diff`/`commit_range_paths` rather than being blocked (produce-then-review, not authorize-then-produce); `range_diff` is scoped to the recorded candidate, immune to later local commits; `commit_list` ordering/parents; `push_exact` (publishes only the pinned sha, idempotent, rejects non-fast-forward, refuses protected refs / non-sha sources / `+`-prefixed refspecs / a configured `pushurl` or `insteadOf` rule / an active pre-push hook, confirms via a fresh `ls-remote`); `range_diff` refuses above a byte cap and never invokes an external diff driver or textconv filter; NUL-delimited paths round-trip spaces and tabs; `reconcile_after_crash` (`NO_COMMIT`/`RECOVERABLE`/an AMBIGUOUS human-commit-same-parent-and-message case/a merge commit/an unrelated expected-parent); `environment.snapshot`/`verify_unchanged` catches a new hook, a `core.hooksPath` change, a new `insteadOf` rule; corrupt `IntentStore`/`TaskExecutionStore` records raise rather than reading as absent; store round-trips; `commit_and_capture` is WIRED to refuse on HEAD drift and on environment drift (not just capable of it). |
| `test_postcommit_flow.py` | 29 | Autoloop M1 pass 2a (updated pass 2b for the new success behaviour): `WorktreeManager` create/remove round-trip (branch survives removal), `list_worktrees` porcelain parsing, refusals (duplicate task id, existing branch, unsafe task ids incl. `..`/`/`/leading `-`/empty — proven via an unchanged `list_worktrees()` snapshot, not a tautology), and the underlying git behaviour it relies on (two worktrees can't check out the same branch). Policy: `git worktree lock`/`unlock`/`move`/`repair` and a bare `git worktree` all denied, `add`/`remove`/`list`/`prune` allowed. **The commit path through `Orchestrator._dispatch_executor`** (gated behind the optional worktree collaborators, every prior orchestrator test unaffected): full happy path (commit → candidate_sha persisted → descendant of base → paths within ownership → clean tree → post-commit validation passes → **pass 2b: the review packet is built and sent, re-entering `ready` with `POST-COMMIT REVIEW PACKET` in the outbox, not a placeholder park**); the environment snapshot is proven to predate the EXECUTOR (not just the commit) via a hook installed from inside a fake executor's `execute()`; a pre-commit hook ADDING an unplanned path is refused (commit stays on the branch, not rolled back); a hook MODIFYING an approved file is allowed and visible via `range_diff` (now asserted inside the packet's own outbox, not just `wt_git.range_diff`); a residual untracked file after commit is refused; a failing re-run of `validation_commands` is refused; a non-`ok` executor outcome never reaches the commit step at all. **Crash reconciliation through the orchestrator:** a commit made but not yet persisted (`candidate_sha` blank, intent still on disk) is adopted as `RECOVERABLE` with no second commit and the executor never re-invoked, then the packet is sent exactly as the happy path; the same crash with a hook-added path outside the intent's plan is `AMBIGUOUS` and parks with the intent preserved for inspection. State schema v2 refuses to load (`SCHEMA_VERSION` bumped to 3). |
| `test_postcommit_review.py` | 29 | Autoloop M1 pass 2b, real git: `task_id`/`task_branch`/`base_sha`/`candidate_sha` are literal substrings of `PendingRequest.payload`, and `sha256(payload) == report_sha256` recomputed independently (not just asserted against `build_review_packet`'s return value); the packet is byte-identical after an unrelated later local commit; **approving candidate A cannot push candidate B** (the response's frozen `PostcommitBinding` disagrees with a fresh `TaskExecutionStore` lookup — refused); `revise` keeps `task_base_sha` and advances `candidate_sha`; **the round>0 path-ownership union fix**, regression-tested (round 2 touching a NEW path is not flagged as "outside" the plan); a third review round never reaches the executor or ChatGPT, parking with both the full diff and the latest round's own diff plus the feedback; `push_exact` through the orchestrator excludes a later local commit, is idempotent on a same-sha retry, refuses a non-fast-forward remote (a sibling commit pushed first), and is refused twice over for a protected destination (the policy layer, proving `authorize_directive` is judged against `resp.postcommit.task_branch` not the main checkout's branch, AND `push_exact`'s own independent check); `allow_push=false` refuses without pushing; a same-candidate retry after a simulated crash never calls `push_exact` again (`remote_ref_sha` pre-check); a tampered `PendingRequest.prompt` with a stale `prompt_sha256` is refused before `client.submit` is ever called; no `.push(` caller of the removed method remains anywhere in `autoloop/` (grep-style, source-scanned); an oversized packet (`RANGE_DIFF_MAX_BYTES` forced tiny) parks with a clear message instead of propagating as a budget-cycled git error. **RETIRED 2026-07-30 (S21): 3 legacy-push-guard tests deleted, replaced by 1 stronger test.** The three (`test_parse_error_reprompt_does_not_leak_postcommit_binding_to_legacy_push`, `test_stale_audit_manifest_does_not_leak_postcommit_push_to_legacy_path`, `test_successful_postcommit_push_does_not_block_a_later_unrelated_audit_push`) each pinned a conditional fail-closed guard INSIDE the now-removed `_dispatch_git`. The replacement, `test_legacy_commit_and_push_is_always_refused_even_with_a_live_candidate`, proves the STRICTLY STRONGER successor: `commit`/`commit_and_push`/an unbound `push` are refused UNCONDITIONALLY now (`_dispatch`'s `legacy_git_path_retired` policy denial), whether or not a live candidate is on record — there is no more leak-shaped scenario to test.  **+6 on 2026-08-04 — report-first packets.** The packet carried the whole patch and nothing the executor said; on rt-09 that was a 40,056-char message ChatGPT could not process at all (composer accepted it, generation failed server-side, the turn was never persisted — the loop then waited 486s for a reply to a message that did not exist, and rotation to a fresh chat failed the same way). The cases pin both halves: the executor's summary/details reach the reviewer and are persisted on the execution record (so crash-recovery adoption still has them); the section is labelled **CLAIMED by the executor, not read from git** — dropping that label fails a test, because it is the entire safety margin for reintroducing the voice finding #2 removed from authorization; an oversized diff is OMITTED with its real size, never silently truncated (the truncate mutation fails); a small diff is still sent in full; a record with no report says so instead of going blank; and a report naming a path it never touched does NOT appear among the git-read changed paths, which is what makes including it safe. Removing the report section fails 3 cases.  **+5 on 2026-08-04 — B10, a published task is now COMPLETED.** Nothing at runtime ever wrote `status="completed"` (`mark_completed` had no runtime caller, `Decision` has no terminal member), so every task the loop published stayed `in_progress` forever — six of them on 2026-08-04, candidates sitting on origin — and the merge window stayed shut until it was re-gated on publication. The cases pin: a pushed task is completed; the completion is PERSISTED (the loop reloads `tasks.json` every outer iteration, so an unsaved one silently vanishes); a re-entered push on an already-completed task is a no-op, not a park (crash recovery reaches this path); an operator quarantine outranks it and the candidate still publishes; and a registry write failure never undoes a successful push. The third case FOUND a real gap — `mark_completed` had no BLOCKED_BY_OPERATOR guard, unlike `mark_in_progress`, so a quarantined task was silently completed. Mutations (never mark / never persist / drop the guard) fail 3, 1 and 1 cases.  **+1 on 2026-08-05 — the diff cap is pinned to its evidence.** `DIFF_INCLUDE_MAX_CHARS` was 8,000, guessed the same day as the 40,056-char failure it reacted to, with nothing between the two tested. It then blocked rt-02 at 8,971 — a 12-insertion/87-deletion change, trivial to review and merely long to print — and the reviewer escalated to the operator rather than approve a diff it could not see. Raised to 30,000. The test asserts both bounds against measured numbers (above rt-02's real patch, at most 80% of the observed failure), so moving the cap past its evidence fails: the 8,000 and 50,000 mutations each fail it.  **Amended 2026-08-14 (pkt-01):** `test_an_oversized_diff_is_OMITTED_not_truncated` now drives the FALLBACK path (a provider that cannot chunk), because an oversized diff is normally delivered as numbered parts instead — see `test_chunked_packet_delivery.py`. The property it pins is unchanged and is the reason the notice exists: omission is loud, complete, and never a partial view. |
| `test_worker_publisher.py` | 32 | Autoloop M2, real git throughout: worker repo has no remotes (config-gated AND raw `.git/config` read); `worker_env()` neutralizes a poisoned global `HOME` (`insteadOf` + credential helper both invisible under it); an active hook in the worker's controlled hooks dir is reported by `verify_worker_isolation`, `WorkerRepoManager.create` refuses a pre-populated hooks dir outright, and a real commit proves a hook in the DEFAULT (non-redirected) `.git/hooks` never fires; **the live platform-trap negative control** — `GIT_CONFIG_SYSTEM=/dev/null` genuinely leaks the ambient system `credential.helper` via a real subprocess, `worker_env()` (via `GIT_CONFIG_NOSYSTEM=1`) does not; `SSH_AUTH_SOCK`/`GIT_SSH*`/`GIT_ASKPASS`/any other `GIT_CONFIG*` stripped, forced vars asserted by name; `describe_policy` carries no secret substring; a worker-planted ref/object in the publisher does not change what `publish()` sends (candidate_sha stays authoritative) and, with a DECOY remote planted first so the check has non-empty text to search (an empty config would make path-absence vacuous), the worker's own config carries no path reference to the publisher; `Publisher.import_candidate` imports exactly the candidate object (idempotent), refuses an absent object, a non-commit object (a real blob sha — via `read_commit`'s `cat-file commit` failing, not a separate `cat-file -t`), and a malformed sha; a later worker HEAD moving on does not affect a candidate already imported (the publisher never even fetches the later commit); `Publisher` refuses construction on multiple configured urls / `pushurl` / `mirror` / `push.followTags` / an `insteadOf` rewrite; a hook in the publisher's DEFAULT (non-effective) `.git/hooks` never fires a real push (canary proven absent) while a hook in the EFFECTIVE (redirected) dir refuses construction outright; `publish` refuses a protected branch and a force-shaped refspec (`+`-prefix, `..`); repeated identical `publish` is idempotent; a divergent remote refuses non-fast-forward without ever forcing; crash recovery reads `remote_ref_sha` and never re-pushes (`push_exact` monkeypatched to fail if called); `Publisher.describe()` redacts embedded url userinfo. **Orchestrator wiring:** `_dispatch_task_push` with a `publisher=` supplied imports the candidate into the SEPARATE publisher repo before publishing (asserted via direct object-presence in the publisher's own repo — the discriminating fact between this path and the no-publisher one, verified to actually fail if the routing is removed); with `publisher=None` (a supported non-CLI mode; production `cli._build_orchestrator` always sets one now — see `test_v1_smoke.py`) `worktree_git.push_exact` runs directly from the task's own worktree and the publisher repo never receives the object. |
| `test_changeset_review.py` | 4 | **New 2026-07-31 — operator-changeset review (`changeset_review.py`, `Orchestrator._dispatch_changeset_push`), real git throughout, throwaway repos with a bare remote (matches `test_worker_publisher.py`'s style).** A stamped approval echoing the queued request's stamp publishes exactly the bound `candidate_sha` — the remote ref equals it; a LATER commit landing on the branch after the packet was sent is NOT published, the recorded candidate is (proves the binding, not a fresh `HEAD` read, decides — the generic HEAD-moved staleness check is skipped for a changeset-bound response); a protected destination (`ChangesetBinding` built directly for `main`, bypassing `build_changeset_binding`'s own CLI-time guard, to prove the independent dispatch-time defense-in-depth) refuses via `authorize_directive` before ever reaching `_dispatch_changeset_push`, and nothing lands on the remote; a `push` response carrying no changeset (and no postcommit) binding still hits `legacy_git_path_retired` — the retired direct-push route is not reopened. |
| `test_v1_smoke.py` | 14 | **New 2026-07-30 — Autoloop v1 wiring, end to end through `cli.py`'s real entry points, real git throughout.** `_build_orchestrator` constructs the full collaborator set and `audit`/`implement` dispatch actually reaches `_dispatch_task_postcommit`; the legacy `_dispatch_git`/`GitGateway.commit()` are structurally gone (`hasattr` + source-scan); `next-task` prints `rt-01`'s exact line from the git-tracked `seed_tasks.json`; a CLI-built worker repo is isolated (`verify_worker_isolation`); the persisted candidate sha matches an independent, freshly-read `git rev-parse HEAD` and the review packet embeds it literally; `revise` retains `task_base_sha` while advancing `candidate_sha`; an approved push calls `push_exact` ONLY on the `Publisher`'s own repo, never the worker's; a drifted publisher url snapshot (main checkout's `origin` changed since provisioning) refuses the push and names `reprovision-publisher --confirm`; `reprovision_publisher` refuses without `confirm=True`, updates the snapshot only with it, and no directive-reachable module calls it (source-scanned); a protected task branch refuses the push; an idle, unchanged repository fingerprint makes the selection policy return `False` having made ZERO Claude/ChatGPT calls (counting fakes that raise if invoked); a session parked mid-flight (`EXECUTING`) is resumed by `run --continuous` to completion via the ordinary `Orchestrator.run()`, never re-derived; `doctor` reports `worker_isolation`/`hooks_dirs`/`publisher`/`publisher_url_drift` all `ok` against a fully-configured repo. |
| `test_m1_hardening.py` | 72 | **New 2026-07-31 — worker-isolation hardening (`docs/AUTOLOOP.md` §4e, `docs/SECURITY.md` S23-S26); +6 across two rounds of same-day follow-up (S25/S26 addenda); +13 on 2026-08-16 (esc-01, §2b — the derived-bytecode exemption); +1 on 2026-08-16 (auto-04); +4 on 2026-08-16 (dash-04 — an operator's immediate `tasks.json` priority edit inside the detection window is not an escape, while an unattested direct write and a forged-attestation scope widening both still park, and the mutex file is never reported).** The auto-04 one guards the blocker-code AST walk itself: `_emitted_blocker_codes` now covers `_to_fault_stop` as well as `_to_needs_user`, because a fault stop records the same `loop_fatal` blocker, and a walk that knew only about parks would report a legitimate precondition key for a fault-stop code as "matching no emitted code" — pushing whoever hit it into deleting the key. The walk feeds two exhaustiveness tests, so a silently narrowed one weakens both while every assertion still passes; the new test pins the emitter list and the concrete code that moved (`policy_denial_budget_exhausted`), plus a park-emitted control so it cannot pass on an empty walk. **Two tests updated in place on 2026-08-18 (wrk-01, no count change):** `test_failed_attempt_residue_absent_from_the_next_candidate` and `test_quarantine_recreate_resumes_from_a_candidate_sha_that_only_exists_in_the_quarantined_repo` — see the wrk-01 narrative entry above this table for what each now pins (the resumed-worker reuse gate exempts a valid recorded worker from the dirty-residue quarantine, so the first pins the fail-closed no-silent-ride-along remainder and the second exercises the retained quarantine branch directly). See the narrative entries above this table for the full description; self-contained, real git throughout, no shared fixtures imported from other test files. |
| `test_chunked_packet_delivery.py` | 45 | **New 2026-08-14 — pkt-01, delivering an oversized review packet in numbered parts (`docs/AUTOLOOP.md` §5d-bis).** Real git, self-contained helpers. The common case first, because it must not change: a small diff is still one message with the patch inline, no `delivering` phase, no delivery state left behind. Then the mechanism: an oversized patch is planned as ordered numbered parts, each under the 40,056-character measured failure and each carrying at most `DIFF_INCLUDE_MAX_CHARS` of patch; concatenating the parts reproduces the diff byte-for-byte and that concatenation appears verbatim inside the hashed payload; the parts are sent in order and the verdict message — which is itself small enough to send — names every part id. **Rule 1 (all or nothing)** is pinned by the mutation test: with one part landed and the rest outstanding, `_step_submitting` raises and sends NOTHING, so reordering the transition cannot quietly approve on half a patch; `delivering` with no plan raises rather than guessing; the per-part cursor is persisted, and a resumed delivery re-confirms by readback instead of re-posting. **Rule 2 (fall back to omission)**: a part the composer accepts but never persists — the exact 2026-08-04 failure — reverts to the omission notice, which additionally DISOWNS the parts that already landed ("If any `autoloop review diff part` messages appear above, IGNORE them … a FRAGMENT"); a provider that does not declare `supports_chunked_delivery` (codex's shape) is never sent one part; a patch over `DIFF_MAX_PARTS`, and a stored patch that is not inside its payload, both refuse to chunk; and a rotation — whose replacement chat cannot contain the parts — gives them up for the omission notice, while a rotation refused BEFORE it sends anything keeps the delivery intact (the old conversation still holds it). **Rule 3 (the integrity binding)**: `sha256(payload) == report_sha256` while the patch's last line is in the payload and NOT in the message that asks for the verdict — the discriminating pair proving the hash covers the whole logical packet; the fallback re-stamps all three holders of the digest (request, `postcommit.packet_sha256`, `TaskExecution.presented_report_sha256`), keeps `prompt_sha256` truthful, and does not spend a second review round. **The part-id collision**, tested as behaviour not format: with every part landed, `reconcile(request_id)` must still be False — a part carrying the request id verbatim would make `submitting` conclude the verdict was already sent and wait forever for an answer to an unasked question. **The virtualized tail**: a part that reads absent until `mount_message_tail()` scrolls it in completes the delivery instead of triggering the fallback; a provider without the capability still delivers; an ordinary mount failure cannot fail a confirmation, while a `LoginExpiredError` from the mount PROPAGATES rather than being demoted to "the part is absent" (answering a login prompt by omitting a diff is the failure that guards against). Plus: a `GitError` raised while delivering parks retryably with the request and its cursor intact, instead of taking the generic route that writes a git-error payload into `outbox` and lets `ready` overwrite the in-flight request — which would abandon a part-delivered patch with nothing left to disown it. Plus the splitting primitives (lossless, line-preferring, an overlong single line cut rather than allowed to overflow) and a second pin that `DIFF_INCLUDE_MAX_CHARS` is NOT raised by this work. **+5 for attachment delivery (2026-08-15, pkt-03):** chunking fails permanently on exactly the changes most worth reviewing — the composer cannot be PROVEN to hold a large patch, because `_enter_prompt` reads the editor back and a 30,000-character part never returns its own tail. Uploading the diff sidesteps the editor. The switch is pinned BOTH ways, because a feature that silently changed what the reviewer receives would be worse than the bug it fixes: with the flag off (the default) an oversized diff is still chunked and no attachment is set, and with it on the plan is `None` — an attached diff needs no parts and never enters `delivering`. Three properties carry the safety argument: the written file equals `range_diff(base, candidate)` byte for byte (a truncated or re-encoded attachment would be a partial review presented as a whole one), it lives OUTSIDE the checkout (a file written under the repository mid-run is what `escape_detector` reports, and would park the loop loop-fatal), and the payload says `Full diff: ATTACHED as` while dropping the inline copy, so the verdict message is itself small enough to send. The attachment travels on the REQUEST, not on loop state — a path left on shared state would outlive its packet and attach one change's diff to another's review. |
| `test_blockers.py` | 15 | **New 2026-07-31 — continuous-mode blockers (`docs/AUTOLOOP.md` §9c).** A `task_fatal` park (review-round cap, driven directly through `_dispatch_executor` — real git, `WorktreeManager`-backed) quarantines only that task (`TaskRegistry.block`, `TaskState.BLOCKED_BY_OPERATOR`) and the NEXT ready task (`next_ready()`, asserted by id) is what `_select_and_kickoff` picks up; the block survives a fresh `TaskStore` reload (proving `_handle_parked_task`'s `task_store.save` isn't decorative). A `loop_fatal` park (pre-seeded `needs_user` state) stops `_run_continuous` (`rc == 2`) and leaves every task's status untouched. `_to_needs_user`'s default (`kind` unset) persists as `loop_fatal`/`unclassified`; a `None`/unrecognised `park_kind` on a pre-existing state file is treated as `loop_fatal` by `_handle_parked_task`, never task_fatal. `Blocker` round-trips through `BlockerStore` with the full question text; a corrupt record raises (`load`/`open_blockers`/`all_blockers` all propagate, never silently skip) EXCEPT inside `_summary`, which degrades to a `?` display rather than taking `status`/every park message down with it. `blockers`/`answer` CLI commands: listing shows id/task/question, `answer` resolves + unblocks a `task_fatal` blocker's task, refuses an unknown id and an already-resolved one (second answer never overwrites the first). Exhaustion (no ready task, unchanged fingerprint, >=1 open blocker) prints every open blocker and exits 0; with zero open blockers and everything blocked, `_run_continuous` still makes zero Claude/zero ChatGPT calls (counting fakes). `next_ready()`/`ready_tasks()` skip every blocked task with no changes to either method. A pre-existing `tasks.json` with no `blocked_reason` key loads, defaults to `""`, and supports `block()` immediately. Round-trip: block → `answer` → task READY → selected by `_select_and_kickoff` on the next pass. **Enforcement:** a quarantined task's id cannot be dispatched around — `policy.authorize_directive` denies `implement`/`revise` naming it directly (`task_blocked_by_operator`), and `TaskRegistry.mark_in_progress` refuses it too, defense in depth for any dispatch path that bypasses policy. |

| `test_validation_parallelism.py` | 15 | **New 2026-08-06 (val-01) — validation runs parallel, cache-free and honest about `isolated`, in the RUNTIME path.** `validation.effective_validation_commands` adds `-n auto` + `-p no:cacheprovider` to configured pytest commands at run time; the config template is not the mechanism, because nothing re-reads it after an operator copies it, so a session started before the change would keep running serially forever. The primary cases therefore feed `run_validation_commands` a LEGACY SERIAL list (what an already-copied `.autoloop/config.toml` still holds) and assert on the argv the runner is really handed: both shared suites come out `-n auto`; a command that never had `-p no:cacheprovider` gains it; the `-m isolated` command runs exactly once and comes out with no `-n`/`--numprocesses`; marker selection is byte-identical before and after, so parallelism can neither include nor drop the marker; an explicit `-n 4`/`-n 0`/`--numprocesses=2` is never overridden or duplicated; `ruff`/`npx vitest`/`npx tsc`/a bare `python3 -c` probe come back untouched (detection is structural — `pytest` as argv[0] or `python -m pytest` — not "is 'pytest' in the argv", so the real Postgres-dialling `-c` probe in `test_validation_env.py` cannot be handed pytest flags); normalization is idempotent, so a persisted or hand-updated command never collects a second `-n auto`; flags are INSERTED after the `pytest` token, so a command ending in `-- <path>` still collects its path. End to end: a real `config.toml` on disk carrying the old serial list loads unchanged (the operator's file is never rewritten) yet reaches the runner parallel; and a task-declared `validation` run through a real `ImplementExecutor` is parallelised too, which is the path a declared-validation task actually takes. Plus: the summary is still one `PASS`/`FAIL` line per command, naming the command that really ran and the failing test; and `pytest.ini` `addopts` still deselects `isolated` and still carries no parallelism (`-n` there would reach the dedicated isolated run and `test_crash_safety.py`'s `--collect-only` subprocess). A second layer keeps the template from rotting behind the runtime: every shipped binary is in `SAFE_VALIDATION_BINARIES`, exactly one shipped command selects `-m isolated` (normalization changes how a command runs, never which commands exist), and the shipped list needs no repair at run time. Companion updates: `test_postcommit_flow.py`'s B4b case and `test_implement_executor.py`'s declared-validation case now compare against `effective_validation_command(...)` instead of the declared literal — both ends still agree by construction, since both normalize at the same single point. |

| `test_test_selection.py` | 32 | **New 2026-08-20 (val-02) — which tests a per-commit validation run executes, and why that is enough.** The acceptance case runs against the REAL checkout, not a fixture, because a fixture would only prove the fixture: `build_import_graph(REPO_ROOT)` plus a changed `autoloop/publisher.py` must select `autoloop/tests/test_v1_smoke.py` — the file auto-01's f06454b5 broke on 2026-08-06 while updating only the four test files it touched, so a filename-matching rule would have run green there. Two assertions make that mean something rather than pass trivially: `test_v1_smoke.py` must NOT be in `graph.opaque` (it is selected by an import edge, not because the model gave up), and the selection must be strictly smaller than the repository's test-file set. The rest drives a miniature repo (`project/`, named so it cannot collide with the real git checkout `build_postcommit` creates at `tmp_path/repo`) whose `suite/test_smoke.py` shares no part of its name with the module it imports. Reachability: a direct importer and a TRANSITIVE one are both selected while an unrelated test is not; a `conftest.py` change selects every test under its directory (pytest applies one to a tree that never imports it); a package `__init__.py` change selects everything importing the package; a changed test file selects itself. Determinism is asserted over the commands, the selected list AND the evidence string, with the changed paths supplied in two different orders, since git hands back a set. **The opacity rules are pinned in both directions, which is the pair that matters:** a test using `importlib.import_module`, one spawning an interpreter, and one that does not parse are each selected for a change they have no import path to — while a PRODUCTION module naming `python3` is asserted NOT opaque, because half this package names an interpreter for a living and marking those opaque would put them on every change's frontier and drag in everything that imports them. Widening: a non-Python changed path, a path absent from the tree, an empty changed set, an explicit `full` mode, a caller-supplied reason, and a reachability result of zero test files all run the configured list unmodified — the last one deliberately treated as a gap in the model rather than as proof no test exercises the change. Command rewriting: `ruff` is never touched; flags survive and the directory is replaced by files; `-m isolated` keeps its marker and `isolated` is never read as a path; a command whose paths hold no selected test is SKIPPED and says so in the evidence; an unrecognised flag, a node-id target and a command declaring no paths at all are each left exactly as configured rather than guessed at. The evidence is asserted to name the changed inputs, the selected files, why an untouched file is still covered, what the model cannot see, and how to widen — and to stay bounded (40 generated test files render as 12 plus a count, under 3,000 characters), because it becomes `state.last_validation`. Four integration cases drive the real `Orchestrator._run_post_commit_validation` with a recording runner: the subset reaches the summary AND the argv; `test_selection = "full"` runs the configured commands untouched; and a task declaring its own `validation`, or its own `validation_cwd`, is never narrowed. (30 is a hand count of `def test_` — no Bash in the worker — not a collected figure.) |
| `test_restart_wiring.py` | 15 | **New 2026-08-16 (brw-08) — what `browser.restart_command` points AT, and what an unmigrated config still gets.** `test_chrome_restart.py` proves the module restarts the right browser; nothing there says the loop ever calls it, and the value that decides is TOML — the shipped template plus each operator's own `.autoloop/config.toml`, which is not in this repository. So one end: the template loads through the real `load_config` (not bare `tomllib`, so every other loader rule applies to it as something copyable) and its `restart_command` is `["python3", "-m", "autoloop.browser.chrome_restart"]`; that dotted path is `find_spec`-able, because a string compare alone passes just as well for a typo that exits 1 with `No module named` at the one moment the loop is already recovering; the module carries a `__main__` guard exiting on `main()`'s code, since `python3 -m` on a module without one runs the body, prints nothing and exits **0** — a restart command reporting success while restarting nothing, the exact bug the shell version shipped; and the template never names the retired script, comments included. The other end is the **transition rule**, which is also the mutation test against re-adding a load-time refusal: a config still naming the script LOADS, and loads exactly as written, in five invocation forms (`bash …`, bare relative, `./`-relative, absolute, `bash -x …`) — not refused (that would fail `status`/`doctor`/`run`/the recovery commands on every deployment that had not yet hand-edited its config, over a setting only a restart reads) and not rewritten to the module (the loader inventing a command that starts a browser is not a repair either). The module invocation round-trips too, and an operator's unrelated wrapper is left alone. The tombstone at `scripts/restart_autoloop_chrome.sh` is now asserted to EXIST — with no refusal in the loader it is the live compatibility path, and deleting it would swap an instruction for bash's exit 127 mid-fault — to say RETIRED, to carry `exit 1` and no `exit 0` as a STATEMENT (prose about exit codes must not decide a test) and no `kill`/`open`/`pkill` statement, and — asserted on the heredoc it prints, not on the file, since the comments above it discuss exit 127 by name — to name the replacement and carry the literal `restart_command = [...]` paste line, built in the test from `RESTART_COMMAND_REPLACEMENT` so the constant and the tombstone cannot drift. That last case is where the "must not surface as a bare file-not-found" requirement lives now. An autouse fixture refuses `subprocess.run`, `subprocess.Popen` and `os.kill` for the whole file, and one test asserts that refusal is real, so a file about commands that signal and launch browsers cannot grow one that ends the developer's Chrome. (15 is a hand count of collected cases — 10 plain plus one parametrized def contributing 5 — no Bash in the worker.) |
| `test_rounds_and_restart.py` (+5, 2026-08-14, brw-03; +12 then +4/1 rewritten, 2026-08-16, brw-09) | 32 | **The restart cooldown vs the failure budget — the interaction, not either guard.** Observed 2026-08-04: four consecutive browser failures each logged `browser_restart_skipped {reason: within cooldown}`, so Chrome was never restarted, while those same failures spent `max_consecutive_failures`; the session ended `failed` with no blocker. The two load-bearing cases are a pair, and the mutation each kills is the other's behaviour: a failure whose restart was SKIPPED for the cooldown leaves `consecutive_failures` at 0 (counting it anyway fails here) while `browser_restart_skips` counts it and the transcript still carries a `browser_error` tagged `recovered=restart_skipped_cooldown` — not charged is not invisible; and a restart that genuinely RAN and failed still advances the budget to `failed` with a resumable phase, so real breakage still stops. A third pins the boundary the exemption must NOT cross: with no `restart_command` configured (the default deployment) the budget is still reachable — exempting "no restart happened" rather than "the cooldown refused it" would make it unreachable everywhere. Then the visible ending: exhausting `policy.max_browser_restart_skips` parks `needs_user`/`loop_fatal` with exactly one open `blockers.Blocker`, `code="browser_restart_cooldown_blocked"`, the question naming `restart_cooldown_seconds` AND its value, `park_blocker_id` matching, and `resume_phase` set — never `failed` with an empty blocker list. Last, the counter is scoped to one cooldown window: a restart that actually runs clears it, so skips cannot accumulate across healthy periods into a park. Driven through the real `Orchestrator` (`build()` from `test_orchestrator.py`, real `StateStore`/`PolicyEngine`/`TranscriptLogger`, a real `BlockerStore`) with `subprocess.run` monkeypatched; the 11 pre-existing round/restart tests are unchanged. (16 is a hand count of `def test_` in the file — no Bash in the worker — not a collected count.) **+12 on 2026-08-16 (brw-09) — ChatGPT's ACCOUNT rate limit, the second interaction with that budget and a sharper one: here the browser recovery is not merely useless, it is the mechanism of the failure.** Observed overnight 2026-08-14/15: a throttled account put up a full-screen modal, the loop read the resulting click timeouts as a lost session, and restarting-and-retrying is exactly what generates requests too quickly — so it deepened the condition it was failing on and burned pkt-03's attempt ceiling. Three tests pin what `_handle_rate_limited` must NOT do, each killing a different reflex the other handlers all have: the restart command is never executed (`subprocess.run` recorded, list empty), `consecutive_failures` stays 0 across four occurrences under `max_consecutive_failures=1` with the phase re-entered rather than failed, and the client is neither dropped nor closed (re-attaching navigates, and a navigation is another request). Then the wait itself: the back-off escalates and CAPS (10/20/40/40/40 against a 40s ceiling), the recorded total is the sum of what was actually slept rather than the configured schedule, and both are persisted BEFORE the sleep — asserted by reading the state file back from inside the injected `sleep`, since a crash mid-wait that resumed at zero would come back ready to hammer. **The clearing rule carries the sharpest test in the set, because the obvious implementation of it is a bug.** Dismissing the overlay always succeeds — it is a click on a page the loop already holds — and proves nothing: the limit is server-side and answers to a timer. Reset the streak on that and it resets on EVERY occurrence, so the delay never doubles, `max_rate_limit_backoffs` never accumulates and the park is unreachable — a fixed 60-second retry loop wearing the shape of a back-off, which is a slower version of the incident being fixed. `_ThrottleAwareClient` is built to catch exactly that: its `dismiss_rate_limit_modal()` succeeds while `is_rate_limited()` stays True (the modal gone, the limit in force), and the test asserts all three dismissals happened, the count still reached 3, and the delays were `[10, 20, 40]`. A fake whose probe went permanently False after a dismissal would model the limit LIFTING and would let the bug pass. The reset therefore lives on `run()`'s success path — a step that COMPLETES is the only honest signal and costs no request — pinned in both directions: a completed step clears the count durably, and a step that raises the throttle again does not (or the reset would undo the count it was about to be charged). One more: `_dismiss_rate_limit_modal` reads `self._client` directly and never `_get_client()`, asserted by a factory that records construction and must stay unused — building the Playwright client binds to the conversation and can navigate, which is the extra request this whole path exists to avoid. The ending is visible: exhausting `policy.max_rate_limit_backoffs` parks `needs_user`/`loop_fatal` with one open blocker, `code="rate_limited"`, the question naming the throttle, the MEASURED total wait, and that a restart is not the remedy. Last, the transcript says `rate_limited` (with the `stage` the transport saw it at) and NOT `browser_error` — the overnight run's transcript contained neither the word rate, limit nor throttle, which is why the operator had to find it by opening the browser. **+4 and 1 rewritten on 2026-08-16 (brw-09, review round 2) — the WAIT is durable, not just its counter.** A persisted streak records that a back-off was *entered*, not that it was *served*: a process killed just after that save resumes unable to tell the two apart, re-enters the browser step at once, and a supervisor restarting the loop skips every back-off in turn — the same restart storm rebuilt out of process restarts. So `state.rate_limit_retry_not_before` (an ISO deadline) is saved beside the counter before the sleep, and `run()` serves whatever remains of it before EVERY step. The regression drives that literally: process one's injected sleep raises `KeyboardInterrupt` mid-wait, the state file is read back to show the debt outlived it with none of the wait paid, and a SECOND orchestrator built over the same state directory must sleep the remainder first — asserted as an ordering over one list recording sleeps, steps AND client construction, since constructing the Playwright client binds to the conversation and can navigate, so "no browser operation" has to mean more than "no step" (same shape as `test_no_client_is_constructed_just_to_dismiss`). It also asserts the resumed wait is not a fresh occurrence (`rate_limited` appears exactly once across both processes) and that the stale overlay is dismissed before the re-probe — the resumed process is where a stale one is most likely, the whole wait having elapsed with nobody holding the page, and the modal hides the composer even after the server-side limit expires. `test_the_count_is_durable_before_the_wait_starts` is **rewritten** as `..._the_wait_is_durable_before_it_starts_and_credited_only_once_served` because the behaviour it pinned was the defect — it asserted `(1, 60.0)` mid-sleep, i.e. 60 seconds credited for a wait that had not happened, which is exactly what lets a killed process come back believing it already waited; it now asserts the counter and the deadline are on disk while the credited total is still `0.0`, and that the credit and the cleared deadline both land after the sleep returns. Two guards on the resumed path: the remainder is CLAMPED to the delay the schedule prescribes (a deadline three days out sleeps 10s, not three days — a backward clock jump or a hand-edited stamp must not become a sleep that outlives the heartbeat monitor's 45-minute staleness threshold, which is the same reason `rate_limit_backoff_max_seconds` exists), and an unreadable stamp fails open, is discarded rather than left to re-log on every step, and lets the step run. The fourth pins the transcript: a resumed wait logs `rate_limit_wait_resumed` with its remaining seconds and streak, because a process that silently sleeps ten minutes before its first step is a smaller copy of the incident this task exists to fix. (Counts are hand counts of `def test_` — no Bash in the worker — not collected figures.) **+9 on 2026-08-17 (brw-11), 32 → 41 — a throttled ACCOUNT vs a browser nothing can attach to.** Every rule above assumes the browser is usable and merely being refused. On 2026-08-17 it was not: the operator closed the browser window, Chrome stayed alive, `/json/version` kept answering with a valid `webSocketDebuggerUrl` and `/json/list` returned ZERO targets, so there was no page to read a modal on — and the loop spent its whole back-off budget and parked saying `rate_limited` while a probe agreed with it for four hours. `_handle_rate_limited` now classifies first, and the tests pin the boundary rather than the new branch alone. **The state-1 test is the one that keeps the no-restart rule intact, and it is asserted against a target count of ZERO** — the reading that authorises a restart everywhere else — because the modal is asked FIRST: a page that can show it is a page the loop can drive, so `subprocess.run` is still never called, the client is still held, the streak still advances and the wait is still taken. State 3 (`_UnattachableClient`, whose every probe raises as Playwright does on a closed target, plus a probe answering 0 then 11 — the numbers the incident produced) restarts EXACTLY once, re-probes (the second probe is asserted consumed, so a restart cannot be assumed to have worked), drops and closes the client (the restart ends the browser process it was bound to), takes NO wait, leaves `rate_limit_backoffs`, `rate_limit_wait_seconds`, `consecutive_failures` and `browser_restart_skips` all at zero — a local recovery must not spend the budget that bounds waiting on the SERVER — and logs `browser_unattachable` then `browser_reattached` with no `rate_limited` entry at all. When the restart does NOT help, one restart is followed by a verdict, never a loop: `needs_user`/`loop_fatal`, one open blocker, `code="browser_unattachable"` (explicitly asserted ≠ `rate_limited`), the question naming the BROWSER and the command to run, `resume_phase` preserved, no budget spent. State 2 resumes without waiting — and still counts the occurrence, which is the anti-hammering guard: skipping both the sleep and the count would answer a page that keeps clearing with an immediate retry every step. Its paired negative is the 2026-08-15 trap in classifier form: a composer that will NOT take a click is not a clear, so modal-absence alone still waits. The restart bound is per EPISODE and not only per cooldown window, pinned with `restart_cooldown_seconds = 0` (a deployment that has disabled the time bound): two consecutive state-3 handlings execute ONE restart between them, the second logging `restart_already_spent`, because every attempt here "succeeds" while the re-probe still finds no page — a restart loop by another door. Last, an endpoint that cannot be MEASURED never restarts — unmeasurable is not evidence, and a browser genuinely unreachable already raises `BrowserError` from the next step onto its own budget — plus a case pinning that the `rate_limited` park quotes the evidence it classified from, since "this is NOT a browser fault" is a strong claim and the four-hour failure was an operator unable to check it. `autoloop/tests/conftest.py` gained an autouse fixture stubbing `Orchestrator._attachable_page_targets` to "unmeasurable" for the whole suite: it is the one probe here that opens a socket, and left live these tests would classify against whatever Chrome happens to be running on the developer's machine. **The ninth is the other half of that per-episode bound, added in review round 2, and the shape it kills is the reason the bound was half-implemented.** The guard was cleared only inside `run()`'s `if rate_limit_backoffs:` branch — but state 3 deliberately never increments that counter, so the ordinary recovery (zero targets → restart → a normal step COMPLETES with the counter still 0) skipped the reset entirely and left the guard true for the rest of the process; the next unattachable browser, hours later and unrelated, was refused its own restart and parked `skipped_already_spent`. The regression drives exactly that sequence — one state-3 fault whose restart restores pages (`[0, 11]`, both probes asserted consumed), then `run(max_steps=1)` with `_step` stubbed so the property under test is the reset SITE rather than whatever a phase needs, with `rate_limit_backoffs == 0` asserted at that moment because that is the condition under which the old code was correct and the new code differs — then a second, independent zero-target fault, which must execute a SECOND restart (`subprocess.run` recorded twice, both `browser_unattachable` entries logging `restart_already_spent: False`). Asserted through the transcript and the recorded restarts rather than the private flag, so it cannot pass against a restart refused somewhere else. Its paired negative is unchanged: two state-3 handlings with no completed step between them still get ONE restart, cooldown disabled. **The reset is deliberately NOT also placed on the successful re-probe** — targets that exist at probe time and are gone at attach time would then produce restart → probe OK → clear → restart, unbounded and never parking; only a step that COMPLETED is evidence the browser works. |

| `test_blockers.py` / `test_smoke.py` / `test_orchestrator.py` (2026-08-16, auto-04) | +6 / +2 / 1 rewritten | **The fault stop: `stopped` now means two things, and everything downstream has to tell them apart.** An exhausted POLICY-denial budget ends the run instead of parking (`orchestrator._to_fault_stop`) — the last park a retired `ask_user` could still reach, by a reviewer answering it until the budget ran out. `test_repeated_ask_user_exhausts_the_denial_budget` is rewritten as `..._stops_the_run_and_never_parks` with its invariants intact and strengthened: it still asserts the run ended on the BUDGET rather than the reviewer's question and that the executor was never called, and now also that no question was posed, `park_kind` is unset and nothing is resumable — the properties that make "never parks" checkable rather than a claim about the phase name. In `test_blockers.py`: the fault stop still writes the same `loop_fatal` `Blocker` (same code, same text, the ORIGINATING phase, id round-tripping through `stop_blocker_id`), a reviewer's own `stop` is classified `"contract"` — the negative control, without which `stop_kind` could default its way to passing the fault test while every real stop stayed unclassified — and `run --continuous` HALTS on a fault stop (exit 2, task still READY, session not replaced) instead of treating it as the clean boundary that would kick a fresh session into the same wall. Two bounds keep that from over-reaching: a `contract` stop is still a clean boundary that starts the next round, and a legacy `""` reads as one too. A sixth pins the OPERATOR-facing half: plain `run` on a fault-stopped session must not print the parked branch's `--answer` / `--retry` advice, because both raise for this terminal (`--answer` requires `needs_user`, `--retry` requires the `resume_phase` a fault stop clears) — a terminal that tells someone to run two commands that error is worse than one that says nothing. **In `test_smoke.py` the three existing refusal tests are UNCHANGED and are the real regression test** — they assert exit 2 for `ask_user`, `implement` and a commit approval, and the smoke policy allows zero denials, so all three now travel the fault-stop path; had the PASS gate stayed on the phase alone, `smoke-browser` would have reported PASS for every one of them. The two added tests pin the mechanism they depend on at state level (`phase == "stopped"` with `stop_kind == "fault"`, no question, nothing resumable) and the positive half (a real `stop` is `"contract"`, or the gate would fail closed on healthy runs). |
| `test_postcommit_primitives.py` / `test_postcommit_review.py` / `test_implement_executor.py` (2026-08-16, auto-04) | +4 / +5 / +8 | **Persisted assumptions — where a task's ambiguities go now that nothing can stop mid-run to ask about one.** Store: `assumptions` round-trips as a TUPLE and is NOT sorted (unlike `allowed_paths`/`out_of_scope_paths` — order is which round chose what, so sorting prose would destroy the only information the order carries); a record written before the field existed still loads, as `()`, and stays loadable after the new build writes it back; and accumulation is a union in first-seen order that drops blanks and restatements, with the load-bearing case being that a round assuming NOTHING does not erase what an earlier round assumed and shipped into the range under review. **The record is COMPLETE and the PACKET is what is bounded, on BOTH axes** (revised in review, 2026-08-16 — the first cut capped the executor at 20 lines of 500 characters *before* the record was written, which meant assumption 20+ of an early round was unrecoverable the moment `report_details` was replaced by the next one; those caps are gone, and `test_every_assumption_reaches_the_record_however_many_the_round_wrote` / `test_a_long_assumption_reaches_the_record_in_full` are the inverted regressions, asserting 40 lines survive as 40 with no overflow notice and a 2,000-character line arrives whole). Both halves have their own test because nothing upstream bounds a message any more: `max_review_rounds` defaults to UNLIMITED. Thirty rounds through the accumulator keeps all 600 entries including the oldest (truncating the record would delete evidence out of the file crash-recovery adoption reads, to solve a problem that only exists at render time), while the packet-level test drives 400 long assumptions (~415 characters each — deliberately just inside `ASSUMPTION_MAX_CHARS_EACH`, so this test measures the SECTION budget and not the per-line one) into a real `build_review_packet` and asserts the rendered section stays inside `packet.ASSUMPTIONS_MAX_CHARS` — measured with the diff cut off, or it would pass for a reason unrelated to the bound — showing the NEWEST (they describe the code under review; the oldest were shown in the packet for the round that made them), stating how many it withheld, and leaving the stored record untouched. The per-LINE render bound has its own test for the failure the section budget alone has: entries render newest-first and the loop stops at the first that does not fit, so ONE ~8,500-character line would render nothing but a withheld count and hide every real disclosure behind it — `test_one_enormous_assumption_is_shortened_here_and_kept_whole_on_the_record` asserts every rendered bullet is inside `packet.ASSUMPTION_MAX_CHARS_EACH`, that the shortening SAYS where the whole line is (a bare ellipsis reads as the executor trailing off, a different claim), that the ordinary entry beside it still renders, and that both come back from the store byte-identical. Its negative control: an ordinary two-line list renders with no withheld note, or every packet would report a drop that never happened. Packet: a recorded assumption reaches the reviewer verbatim, inside the executor-report section and labelled a CLAIM — asserted by SPLITTING on the report heading, so a line rendered among the git-read facts would fail rather than pass on a bare substring match — and an empty list renders no section at all (deliberately unlike the empty report, which announces itself: "no assumption lines" is not a claim the executor made). Executor: the prompt actually carries the smallest-reversible-reading rule, the `ASSUMPTION:` form and the fact that the lines are shown to a reviewer (an instruction to take a reading without one to disclose it is worse than the question it replaced); the lines are collected from the agent's own output, case- and indent-tolerantly; prose ABOUT the convention is NOT harvested (anchored at line start — otherwise an agent explaining its instructions would have them read back as disclosures nobody made); and a FAILED round reports none, since nothing it produced is committed and an assumption about discarded code would describe a candidate the reviewer never sees. **The anchor is not enough on its own**, which is the second thing review caught: `^[ \t>*-]*assumption:` admitted `> ASSUMPTION: <what you assumed...>` — the prompt's own example, quoted back — and the bulleted restatement `- ASSUMPTION: ...`, so an agent echoing its instructions could manufacture a deliberate choice it never made. `test_quoted_or_bulleted_echoes_of_the_instruction_are_not_disclosures` covers `>`, `-`, `*` and `> - ` and is deliberately NOT an `== ()` assertion — that passes equally against extraction broken outright — so a REAL declaration sits in the same raw text and the assertion is exact-tuple equality, pinning both directions at once. The paired `test_the_prompt_tells_the_agent_that_a_prefixed_line_is_not_collected` is what keeps the trade honest: refusing the bulleted form silently would swap a fabricated disclosure for a missed one, so the instruction states the accepted shape and says what a prefix does. |
| `test_docs_merge.py` (2026-08-19, docs-01) | 30 | **Two branches recording a change note must merge — and nothing else may.** Real git throughout, and almost every case runs through the PRODUCTION merge path (`merge_sweep.sweep_backlog` → `AutoMerger.attempt` → `_merge`) against a real checkout with a real origin, because that is the invocation that was halting. Resolves: two branches cut from one base each appending a note to BOTH trackers integrate with every note present exactly once and the pre-existing notes and prose untouched; three in sequence likewise; a branch that also adds a table row above merges too (git handles the one-sided row, the resolver only rebuilds the tail); the auto-resolution gets its own transcript entry naming the paths. Refuses, each one stopping the sweep with the base left where the first merge put it and the tree clean: concurrent edits to tracker PROSE (with and without a clean pair of appends alongside — the second is what proves section-awareness rather than file-awareness), a concurrent edit to an existing NOTE line, a deletion and a reorder of existing notes racing an append, a source-file conflict, and a doc outside the two trackers (`docs/TODO.md`). One test pins the all-or-nothing rule: `SUMMARY.md` resolves and `TESTS.md` (reached second) refuses, and the resolved text must never reach disk. `merge=union` is rejected with evidence, written INLINE since the repo no longer ships it — it duplicates a grown 19,000-character row instead of merging the two additions, and it silently concatenates a genuine prose conflict that must stop the sweep. A bare `git merge` of two note appends still conflicts, so the resolver is doing this and git was not weakened. Ten unit tests on `note_merge.resolve_note_append` itself (arrival order, git's head kept verbatim, markers above the section, an edited note line, a grown last line, an appended heading, a missing/duplicated marker, neither side appending, the two-path scope). Plus the doc-shape pins: both trackers end on their append-only section, no note line exceeds 700 chars, `CLAUDE.md` still carries the instruction, and the four split `state.py`/`transcript.py` notes are all still there. (30 is a hand count of `def test_` in the file — no Bash in the worker — not a collected count.) |

| `test_audit_charters.py` (2026-08-19, port-03) | 54 | **The audit's domain charters, shipped by the repository under audit.** Real git repos, a recording fake agent runner, no CLI. The fallback claim is pinned as an IDENTITY BETWEEN PROMPTS, not a domain count: a checkout with no `docs/audit_charters.toml` produces exactly `_agent_prompt(...)` of each `DEFAULT_DOMAINS` entry, so a fallback that quietly reworded a charter fails here. The portability claim is executed rather than asserted — `render_charter_file(DEFAULT_DOMAINS)` parsed back equals `DEFAULT_DOMAINS`, the two-backend sentence crosses the boundary inside it, and a repository shipping that rendered file gets prompts identical below the framing line to the built-in run. **THIS repository's own shipped file is a regression, not a capability demo**: `docs/audit_charters.toml` must exist, must parse to exactly `DEFAULT_DOMAINS` (parse equality, not renderer byte equality — the load-bearing claim is what the agents are briefed with), and must be what `RepoConfig()`'s default resolves to; its bytes are then copied into a tmp repo and driven through a real `execute()` so the two-backend wording is observed reaching agent #3 out of the file. Run against a copy, never `REPO_ROOT`, because an audit writes `docs/AUDIT_<date>.md` and residue in the working checkout reads as an escape. **Framing follows the source**: an unrelated target's prompts start with `REPO_SUPPLIED_FRAMING` and carry none of `German language-learning` / `language-learning app` / `lexy-app` while carrying their own `openapi.yaml` and `cmd/server` wording, and the built-in path still starts with the word-for-word historical sentence. Override: a two-domain file with different names, order, models and prose replaces the built-ins wholesale (no `lexy-app/backend` text survives into any prompt), its own two-backend wording reaches its agent, and the ground rules, the reviewer-scope framing and the findings schema still wrap it — a file cannot drop them by omission. **The report must describe the domains that actually ran**: `self._domains` is read at three sites and feeding only the spec fan-out would leave the coverage table and the domain COUNT describing the built-ins, so `across 2 domains`, `2/2 domains reported usable output`, the file's slugs in the table and `ingestion_pipeline` absent are all asserted, plus the raw-output filenames. Fails closed: eleven malformed bodies (not TOML, no `[[domain]]` array, a `[domain]` table, an empty array, a missing field, an unknown field, a non-string value, a blank charter, a duplicate slug, a slug with a path separator, an unusable model) each refuse by naming the file AND the fault; end to end a malformed file returns `status="error"` with `validation="not run"`, zero agents launched, no run directory and no report — and never the built-ins, which would file a report reading as complete about the wrong codebase. **Absence must mean absence**: a directory at the charter path and a path whose parent is a file are each refused by name rather than read as "ships no charters", since `is_file()` answered False for both exactly as it does for nothing at all; the directory case is also driven end to end to show existence faults reach the reviewer by the same error-outcome route parse faults do. Scope: a symlink is refused rather than followed, absolute/traversing/padded paths are refused by the loader as well as by `load_config`, an audit re-rooted onto a worker repo reads THAT checkout's charters (not the main one's), one executor auditing two repositories gets each one's and re-auditing the first still gets the first's, and a repository shipping nothing still falls back while its sibling ships a file. Config: `[repo].audit_charters_file` follows the port-02 rule (default constant, relative-only, globs refused since it names one file, exact `""` opt-out honoured end to end with a file present), and a source scan pins that `cli._build_executor` actually passes it — an unpassed setting reads as configured and does nothing. (54 is a hand count: 37 `def test_`, three of them parametrized into 11 + 4 + 5 cases — no Bash in the worker — not a collected count.) |

Fakes live in the test files themselves. `conftest.py` inserts the repo root on
`sys.path` and, since 2026-08-17 (brw-11), stubs the loop's single socket-opening
probe (`Orchestrator._attachable_page_targets` → "unmeasurable") so the suite
stays hermetic; a test about that classification sets its own value on the
instance.

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
- `notifications.py` SSE — covered by `test_notifications.py` (#4a); its extracted `notification_service` is covered directly in the same file (arch-05).
- `content_requests.py` — covered by `test_content_requests.py`; its extracted `content_request_service` is covered directly in the same file (arch-05).
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
| Backend | `cd lexy-app/backend && python3 -m pytest -n auto` | 1258 passed, 2 skipped |
| Root (pipeline + autoloop) | `pytest` *(repo root)* | re-measure — see below |
| ↳ pipeline only | `pytest tests/` *(repo root)* | 368 passed |
| ↳ autoloop only | `pytest autoloop/tests` *(repo root)* | 869 passed, 1 skipped **as last measured; +13 since (esc-01, 2026-08-16) — re-measure rather than trusting either number** |

> **The root command is now a bare `pytest`, not `pytest tests/` (rt-05,
> 2026-08-04).** `testpaths = tests autoloop/tests`, so one command covers both
> trees and the autoloop suite is no longer conditional on "only when touching
> `autoloop/`" — that qualifier is what let a loop-harness regression ship
> unnoticed by anyone who did not think to run it. The two split rows are kept
> for narrowing a failure, not for validating a change.
>
> The combined expected count is deliberately left unmeasured rather than
> guessed. The three figures on record disagree — the audit that filed this task
> said 415, this file said 790 at 2026-08-01 and 869 in the row above — because
> each was measured on a different date, and the change that merged the trees
> was made without a shell to run them in. Record what a real run reports; do
> not sum the rows above (the same idiom as the autoloop section's own note).

> **The loop runs the root trees in parallel; you can too (val-01, 2026-08-06).**
> Post-commit validation re-runs the configured suites against a task's
> committed worker repo on every round, revises included. Every pytest command
> it runs gets `-n auto -p no:cacheprovider` — applied at run time by
> `autoloop/validation.py`'s `effective_validation_commands`, not only written
> into `autoloop/config.example.toml`, because nothing re-reads that template
> after an operator copies it to `.autoloop/config.toml`. The `-m isolated`
> command is exempted from `-n` and stays serial. The two flags are not
> interchangeable with the plain command above: `-p no:cacheprovider` exists
> because a failing test writes `.pytest_cache/` into the tree it is grading and
> the gate after validation refuses a worktree validation dirtied. Adding `-n
> auto` to your own run is safe; adding it to `pytest.ini`'s `addopts` is not —
> it would reach the isolated run as well. Pinned by
> `autoloop/tests/test_validation_parallelism.py`.
>
> **Backend command reconciled 2026-07-29:** it must be `python3 -m pytest`,
> NOT the bare `pytest` entrypoint. `python -m` puts the cwd on `sys.path`,
> which `tests/test_document_package.py` (A2, 107 tests) requires for its bare
> `from services import …` imports; the console script omits cwd and that one
> file fails collection, reporting `1151 passed + 1 error`. 1151 + 107 = 1258.
> Full mechanism in `docs/COMMON_ERRORS.md` §2. (The 894 previously listed
> here predated the A2/backfill work; the 221 root figure predated the
> evaluation-harness tests — both were stale doc claims, now measured.)

### CI — `.github/workflows/tests.yml` (rt-10, 2026-08-06)

Until this landed, the table above was a convention enforced by whoever
remembered it. The workflow runs it on every push and pull request to `main`,
one job per command so a failure names its own suite:

| Job | Command(s) | Notes |
|---|---|---|
| `lint` | `ruff check .` *(repo root)* | ruff installed at the pin read out of `lexy-app/backend/requirements.txt`, so a bump there moves CI with it instead of silently diverging |
| `pipeline` | `python3 -m pytest tests/ -q` | installs the full backend requirement set, then downloads three spaCy models |
| `autoloop` | `python3 -m pytest autoloop/tests -q`, then `python3 -m pytest autoloop/tests -q -m isolated -p no:cacheprovider` | both steps blocking |

Deliberate choices, so they don't get "fixed" back:

- **CI runs the two split rows, not the bare root `pytest`.** Same files and
  the same `addopts` either way — `testpaths` is exactly those two trees — but
  in separate processes, so the duplicate-module-basename collision
  `pytest.ini` warns about cannot occur and a red job names the tree that
  broke. Coverage identical, attribution better. The bare command remains the
  right one to type locally.
- **The `isolated` step is blocking, not decoration.** Root `pytest.ini`
  justifies excluding that test from the shared run by saying the coverage "is
  still enforced" because it runs separately. If CI is the gate and CI skips
  it, that sentence stops being true.
- **spaCy models are downloaded, not skipped.** `de_core_news_sm` is a hard
  requirement — `subtitle-scraper/phrase_finder.py:15` loads it at module
  scope, so its absence is a collection error, not a skip. `de_core_news_md`
  and `es_core_news_sm` back fixtures that *do* skip cleanly, which is why they
  are installed too: a green job that quietly skipped a third of its assertions
  is the drift this workflow exists to catch. `en_core_web_sm` is only asserted
  as a config string (`tests/test_language_config.py:47`) and is not installed.
- **`python3 -m pytest` everywhere**, matching the backend note above and
  `pytest.ini`'s own isolated-test invocation.
- **No expected pass counts in the workflow or in this section.** Green/red is
  the signal; the counts live in the table above and are only ever written from
  a measured run.

**The backend suite is not gated, and that is hb-01, not an oversight.** Every
backend test pulls the autouse `cleanup` fixture (`tests/conftest.py:67`),
which takes `db_pool`, so there is no DB-free subset to run — and the database
itself cannot be built from this repo: `video`, `word_table` and eight more
tables are `ALTER`ed by migrations but never `CREATE`d, so `alembic upgrade
head` fails at revision 006 on an empty database (`006_video_channel_genre.py:16`
against no `CREATE TABLE video` anywhere in the 37 revisions). A job that
cannot pass would make the gate red on its first run and train everyone to
ignore it; a job gated to *skip* is worse, because branch protection scores a
skip as a success. The workflow header records what to add once hb-01 is
resolved (a `postgres:16` service, `alembic upgrade head`, then
`python3 -m pytest -n auto`). Until then, **CI green does not mean the backend
suite passed** — run it locally.

**Frontend vitest is also not in this workflow.** It is not one of the Python
validation commands rt-10 was scoped to, and `npm audit` already runs in
`dependency-audit.yml`. `npm run build` + `npx vitest run` remain ungated — a
separate gap, deliberately left named rather than half-closed.

How the backend baseline got to 847, newest last:

| Count | What landed |
|---|---|
| 745 | after #39 slice 3C |
| 760 | ILP playlist optimizer (+15: 11 unit tests for `ilp_cover`, 4 endpoint tests for the `algorithm` parameter — accepts `"ilp"`, 422s an unknown value, 503s when the solver is unavailable, and defaults to greedy without invoking it) |
| 786 | vocabulary lists (+26, migration 035) |
| 817 | LLM provider seam (+31) |
| 847 | OpenAI-compatible provider (+30) |
| 858 | word-list phrase support (+11), plus 14 provider byte-identity tests restored from skips — see below |
| **894** | German catalog backfill (+36) |

Frontend baseline: **322 passed across 44 files** (`npx vitest run` in
`lexy-app/frontend`), as of 2026-07-29.

> **A sliding `HEAD~n` window silently disarmed 14 tests — fixed 2026-07-27.**
> The `test_llm_provider.py` byte-identity checks read the pre-seam service
> modules out of git, searching `HEAD`, `HEAD~1`, `HEAD~2` for a revision still
> constructing `AsyncAnthropic`. As unrelated commits landed that revision
> drifted to HEAD~8, and all 14 degraded to skips — the suite stayed green
> while the assertions stopped running. The lookup now also tries a pinned
> `PRE_SEAM_REV = "9977a4e"`, which cannot drift; the graceful skip survives
> for shallow clones and rewritten history, but is no longer the normal
> outcome. **Lesson: a test that skips on a moving reference goes quiet
> instead of failing — pin the reference.**

The root suite is **221 = 97 + 124**: `tests/runtime/` (97) covers the root
pipeline modules, `tests/*.py` (124) covers `subtitle-scraper/`. It was 744
until 2026-07-27, when the `src/app/` refactor and the 537 tests that only
targeted it were deleted — see the retirement note below.

🆕 **2026-07-27 — Spanish gerund enclitics (#36 slice A, +10 root tests)**

`tests/test_spanish_phrase_extractor.py` 52 → 62. **Note this is the ROOT
suite, not the backend** — `phrase_finder.py` lives in `subtitle-scraper/`, and
no backend test imports `extract_spanish_logic` directly (the backend touches
it only indirectly, through `matcher_service` in `test_matcher.py`).

Now covered: `Está lavándose…` → `lavarse`, `duchándome` → `ducharse`,
`levantándose` → `levantarse`, emitted with a distinct
`match_type="es_reflexive_gerund"` (a gerund is not an infinitive, and
conflating the two would misreport provenance downstream).

**The load-bearing test is the garbage-lemma rejection.** For `preguntándome`
the model returns the lemma `preguntándomar`, which *ends in `-ar`* and so
passes a naive infinitive check while being nonsense — accepting it would put
`preguntándomarse` into `phrase_table` and teach a word that does not exist.
One test pins the rejection through the real model; a second exercises
`_es_gerund_base` directly with fabricated lemmas, so the guard stays proven
even if the model's output changes.

**Still unsupported, deliberately pinned (slice B):** positive fused
imperatives. `Lávate las manos.` tags VERB/`Fin` with the clitic fused and a
garbage lemma (`lávatir`); `Levántate ahora.` tags **PROPN** at sentence start
(the same word tags VERB mid-sentence — the mistagging is positional). Both
assert *no* extraction, so a model upgrade surfaces as a visible test change
rather than silent drift.

Also corrected while pinning: **negative imperatives always worked.**
`No te levantes.` → `levantarse` via block 1, because the clitic is a separate
token. `docs/TODO.md` had claimed imperatives extract nothing at all.

**When one of these fails, check [`docs/COMMON_ERRORS.md`](./COMMON_ERRORS.md)
before debugging.** It carries the failures that have actually happened here —
including two that mislead rather than simply fail: `pytest-randomly` erroring
every test at setup so no assertion runs, and a `cd` persisting between shell
calls so `pytest tests/` silently runs the *backend* suite from
`lexy-app/backend` and reports 745 instead of 207.

Frontend changes additionally want
`cd lexy-app/frontend && npx tsc --noEmit && npx vitest run && npm run build`
(261 tests across 43 files).

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

---

## Change notes — append ONE new line at the END of this file

Where a task records a test change when there is no natural new row for it
above. **This section stays last in the file, and a note is appended at the
very end of it.**

Five rules. They are not style — they are the precondition the loop's own merge
path checks before it will combine two branches' notes
(`auto_merge.AutoMerger._merge` → `autoloop/note_merge.py`; the shape is pinned
by `autoloop/tests/test_docs_merge.py`, the reasoning is in `CLAUDE.md` §12):

1. **Add a line. Never grow a line.** Do not append your note into an existing
   table row or paragraph. The resolver only combines whole lines added AFTER
   everything that was already here; a grown line is an edit, and the merge
   stops. That habit is how one row in this file reached 15,729 characters.
2. **One note, one line.** Keep it to roughly a sentence (~400 characters). If
   it needs more, add a second line, or put the detail in the section above
   that owns the suite and leave a pointer here.
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
   makes the resolver decline every parallel merge, silently. That is the
   original failure with an extra step, and it is how docs-01 itself first
   shipped.

Adding a genuinely new table row anywhere above is fine and always was — that
is also "a new line". This section exists for the notes that have no row.

Nothing may follow the last note line: this section is the end of the file, so
that the next task's append lands at the end of the ledger rather than inside
whatever came after it.

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. Never edit a line above. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-19 | docs-01 | `autoloop/tests/test_docs_merge.py` added (30 tests, real git, through `merge_sweep.sweep_backlog`): parallel branches recording change notes integrate cleanly, while a prose edit, an edited/deleted/reordered note line, a source conflict and a doc outside the two trackers all still stop the sweep. |
| 2026-08-19 | docs-01 | The two `merge=union` failures are demonstrated rather than asserted — a grown 19,000-character row duplicates, and a genuine prose conflict is silently concatenated — which is why the attribute was removed the day it shipped. |
| 2026-08-19 | docs-01 | `notes_section`'s one-marker assertion now explains what a duplicate marker costs (it makes `resolve_note_append` refuse every parallel merge of that file), because it caught exactly that in the shipped `docs/SUMMARY.md` and read as a formatting nit. |
| 2026-08-19 | port-03 | `test_audit_charters.py` pins the audit-charter fallback as an identity between whole PROMPTS rather than a domain count, because a count still passes against a fallback that quietly reworded a charter — and asserts the coverage table and domain count alongside the fan-out, since feeding only the fan-out would make the report describe domains that never ran. |
| 2026-08-19 | port-03 | The shipped `docs/audit_charters.toml` is pinned by PARSE equality to `DEFAULT_DOMAINS`, not byte equality with `render_charter_file`: the load-bearing claim is what the agents are briefed with, and pinning the renderer's blank lines and trailing newline would fail on formatting that changes nothing. A companion test drives the shipped bytes through a real `execute()` — against a tmp copy, never the working checkout, since an audit writes a report and the residue reads as an escape. |
| 2026-08-19 | port-03 | The prompt-identity test now compares everything BELOW the opening framing paragraph rather than the whole prompt, because the framing is deliberately different on the two paths; the framing itself is asserted separately on both, so the test is narrowed by one line rather than weakened until it passes. |
| 2026-08-20 | base-02 | `autoloop/tests/test_rebase_stale_base.py` gained eight real-git regressions for carrying a reviewed candidate past a moved head: it survives with its candidate_sha/review_round/attempt_count intact, a hand-written old-shape JSON record is rescued identically, the next round's diff contains only the task's own paths, a genuine conflict parks and leaves the worker byte-identical, a dirty worker and a tip that lost the candidate both refuse, and a carried-forward candidate is still pushable. |
| 2026-08-20 | base-02 | Every one of those eight builds a real `WorkerRepoManager` repo — the two pre-existing reviewed-park tests point `worktree_path` at a directory that is never created, so they stay green against any probe-gated design without exercising the merge at all, and green tests there are not evidence the fix works. |
| 2026-08-19 | scope-04 | `autoloop/tests/test_scope_cleanup.py` added (18 test functions, 25 collected with the two parametrized sets, real git): it drives a REAL `ImplementExecutor` through the orchestrator rather than the usual stub executor, because the claim spans the prompt the agent reads, the unlink the executor performs and the record the orchestrator writes — a stubbed executor would prove none of the interesting half. |
| 2026-08-19 | scope-04 | The end-to-end regression reproduces the roadmap-01 deadlock exactly: round 1 creates an out-of-scope file and the loop records it, a later revise round removes that exact file and REACHES REVIEW, and the file is asserted absent from the candidate via `git ls-tree` rather than from the worktree — "not committed as a zero-byte addition" is a claim about the commit. |
| 2026-08-20 | val-02 | `autoloop/tests/test_test_selection.py` added (32 test functions). The acceptance case is the only one that runs against the REAL checkout — `autoloop/publisher.py` must select `autoloop/tests/test_v1_smoke.py` — because the claim being made is about THIS repository's import graph, and a fixture proving a fixture is exactly how the 2026-08-06 filename-similarity rule looked sound. |
| 2026-08-20 | val-02 | Every existing validation test is unchanged, and one of them explains why: `test_orchestrator.py`'s `assert orch.state.last_validation == "ruff check .: PASS"` is a byte-exact equality on a post-commit summary, so the selection evidence is appended only when the configured list actually contains a pytest command — no test command, no decision, nothing to report. |
| 2026-08-19 | scope-04 | The refusal tests aim at `lexy-app/backend/main.py`, tracked from the base commit and written by no round, deliberately NOT at a path under `autoloop/tests/` or a `TRACKER_PATHS` entry: a "still refused" assertion pointed at a path the task was approved to touch would pin the wrong rule and read as entrenching the deadlock. |
| 2026-08-19 | scope-04 | Two ordering constraints are pinned by tests rather than by comments: a cleanup-only round still reaches review (the unlink must precede the `git status` read, or it dies on "changed no files" with the deletion uncommitted), and the validation command runner records the file present in round 1 and absent in round 2 (the unlink must precede validation, or the suite grades a tree that is not the one committed). |
| 2026-08-19 | scope-04 | The stubbed agent runner deliberately CANNOT delete a file — it only writes — because that is the real production constraint (`WRITE_ALLOWED_TOOLS`, no `Bash`) and a double with a shortcut around it would pass against an implementation that never needed to exist. |
| 2026-08-19 | dash-13 | +6 test functions in `test_dashboard.py` for the merge panel's state grouping, plus three helpers (`four_state_rows`, `merge_groups_by_key`, `merge_panel_js`) that are in neither figure: one group per state with its count and collapsed flag, `merged` collapsed and the other three not, a zero-count group still present, and grouping proven display-only by comparing the grouped rows back against the flat `rows` list end-to-end through `collect()`. Hand-counted, no shell in the worker. |
| 2026-08-19 | dash-13 | "An opened disclosure survives the refresh" is asserted as a NEGATIVE as well as a positive: the `<details>` is in static markup AND `renderMerge`'s body contains no write of a disclosure state, because a render that reopened the box on every tick would pass every structural check in the file and snap the panel shut every two seconds. |
| 2026-08-19 | dash-13 | The panel's own render is lifted out of `PAGE` by the `MERGE_PANEL_START`/`END` markers (the `PURE_ROADMAP_*` pattern, extended to a region that touches the DOM) and RUN under node against a stub `document`, twice with the disclosure opened in between — so "every actionable group renders with its count, `Unknown (0)` included" and "the open state survives a tick" are measured on the real template rather than grepped for. |
| 2026-08-20 | dash-14 | +12 test functions in `test_dashboard.py` for the dependency graph, plus four helpers (`dep_graph`, `dep_nodes`, `dep_pairs`, `deps_panel_js`) that are in neither figure. Hand-counted, no shell in the worker. |
| 2026-08-20 | dash-14 | What those 12 cover: one edge per `depends_on` pair (a dependency declared twice is one edge), dependency-before-dependent layering, isolated tasks omitted with the count reported, a cyclic registry and a self-dependency rendered and reported, node state equal to the registry's, a dangling dependency drawn rather than dropped, and display-only proven by comparing the drawn pairs back against the stored rows. |
| 2026-08-20 | dash-14 | Every layout fixture is built so ALPHABETICAL order contradicts dependency order (`a-01` depends on `z-01`), because `layer[dep] < layer[dependent]` is otherwise satisfied by a layout that merely sorts by id — the assertion says nothing until the two orders disagree. |
| 2026-08-20 | dash-14 | The cyclic and dangling cases are driven through `collect()` end to end and assert BOTH halves — that the registry refused the file (`payload["groups"] == []`) and that the panel still drew it — because a graph derived from a `TaskRegistry` would pass a payload-only test on acyclic fixtures and show nothing on the only input that matters. Termination is asserted by the test returning at all; there is no timeout to hide behind. |
| 2026-08-20 | dash-14 | `renderDeps` is lifted out of `PAGE` by `DEPGRAPH_START`/`END` (the `MERGE_PANEL_*` pattern) and RUN under node against a stub `document`, twice with the edge table opened and a node selected in between, so "one `<path>` per edge, the cycle's own included", "the omitted count is on the page" and "neither an opened disclosure nor a selected node is replaced by a tick" are measured on the real template. |
| 2026-08-20 | dash-14 | The dependency panel's listeners live OUTSIDE that lifted region — they reach `showTip`/`render`/`LAST`, which the harness has not — and a test asserts the region stays self-contained. |
| 2026-08-20 | dash-14 | +4 test functions for the refresh gate (`updateDeps`/`depsBusy`), plus two helpers (`DEPS_STUB_JS`, `deps_gate_js`) that are in neither figure: an unrelated payload change redraws nothing, a focused node survives two ticks, a hovered node and a selection anchored in the panel each hold the redraw, and a forced selection click redraws the graph on screen rather than the held one. |
| 2026-08-20 | dash-14 | Non-replacement is proven by STAMPING a sentinel over the panel's DOM after a draw and asserting it survives: comparing one render's HTML with another's cannot say anything, since a render that rebuilt everything from an identical payload produces an identical string — which is what the bug looked like. |
| 2026-08-20 | dash-14 | Every refresh-gate negative is paired with its positive (the gesture is released and the held payload must then land), because a `depsBusy` stuck at true passes all four negatives on its own. |
| 2026-08-20 | dash-14 | +1 refresh-gate test, so five now rather than the four the earlier dash-14 refresh-gate line counts: a graph held mid-gesture goes obsolete, and any later non-forced payload must supersede it — the poll that returns the graph ALREADY ON SCREEN (asserted as `DEPHELD === null`, then as a `settle` that renders nothing when the gesture ends), and the poll that draws a FURTHER change while a stale one is held, reached by a selection collapsed outside the panel where no listener fires. |
| 2026-08-20 | dash-14 | Both halves of that test carry the paired positive (the newest graph still lands, and the retired task is absent from a DOM that was really rendered rather than from a sentinel-stamped one), because a gate that discarded every held payload passes every negative in it on its own. |
| 2026-08-20 | dash-14 | That test spells `settle` out inline (`if (DEPHELD) updateDeps(DEPHELD);`) the way the picker test spells out `bindDeps`: the listener is bound outside `DEPGRAPH_START`/`END`, so the lifted region does not carry it and the delivery it drives can only be reproduced by hand. |
| 2026-08-20 | val-02 | `test_test_selection.py` grew to 35 test functions in review round 2. `test_an_unreachable_test_is_omitted` was failing because its target module was reached by no test at all, so the zero-reachable widening fired and the omission was never exercised: the fixture now gives `pkg/lonely.py` exactly one importer (`suite/test_lonely.py`), and the assertion is an equality on the whole selected tuple rather than one `not in`. |
| 2026-08-20 | val-02 | `test_the_pre_commit_run_is_not_narrowed_today` drives a REAL `ImplementExecutor` over a real git worker repo and asserts both halves of the scope blocker in one place — the executor still runs the whole configured tree, and `select_validation_commands` handed that round's own `changed_paths` and worker root narrows it — so the outstanding work is authorization rather than design, and the packet sentence claiming a full pre-commit run stays true or the test fails. |
| 2026-08-20 | val-02 | `test_test_selection.py` grew to 37 test functions in review round 3. The three "left exactly as configured" tests (unrecognised flag, no declared paths, node-id target) asserted only that the argv came back unchanged, which passed happily against the bug review round 3 found — the run was reported as a SUBSET anyway. They now go through the shared `assert_full_suite_because` helper, which asserts `widened is True`, the unchanged command list, `FULL SUITE` present and `SUBSET` absent from the evidence, and a reason naming the offending command and what could not be read in it. |
| 2026-08-20 | val-02 | Two tests added for the widening's blast radius and its bound: one blocked command puts every OTHER command back to configured (a subset summary beside a command that ran the whole tree is the mismatch being removed) and `selected` comes back empty, and two blocked commands are BOTH named in the reason rather than only the first. The skip test is strengthened in the same place to assert `SUBSET` is still reported, since skip-versus-blocked is the distinction the widening rule turns on. |
| 2026-08-20 | val-02 | `test_test_selection.py` grew to 41 test functions in review round 4, for the bare-sibling-import resolution. `test_a_bare_sibling_import_is_a_real_edge_in_the_real_repository` asserts on `graph.importers`, NOT on `chosen.selected`, and that choice is the whole test: this module contains the literal `"python3"` (its `SUITE` constant), so it is in `graph.opaque` and on the frontier for every change — a selection assertion would have come back green with the edge still missing, which is the same vacuity the 2026-08-06 filename rule had. |
| 2026-08-20 | val-02 | The fixture half (`sibling_tree`) is written free of every literal `_file_is_opaque` watches for — no `"python"`, no `importlib` — and asserts `graph.opaque` is EMPTY before asserting anything else, so each selection below it is carried by an import edge rather than by the frontier. Its chain is `pkg/core.py` → `suite/helper.py` → `suite/test_middle.py` → `suite/test_outer.py` with only the first hop dotted, which is what makes the transitivity claim about bare names specifically; `suite/test_apart.py` joins nothing and is asserted absent, so the fixture can tell reachability from directory fan-out. |
| 2026-08-20 | val-02 | Two more resolution cases: a changed sibling helper selects both modules that import it by bare name, and one bare name matching two files (repo root and beside the importer) gets an edge to BOTH, with changing either selecting the test. Determinism is re-asserted on the sibling path (same diff twice → same `selected`, same `commands`, same `evidence()`), since the new per-root index is dict-ordered internally. |
| 2026-08-20 | val-02 | Nothing in `test_test_selection.py` pins the ABSENCE of an import edge, and a comment where such a test would have gone says why: `_import_roots` skipping package directories is a precision choice, so asserting it would turn an under-selection into a contract — and under-selection is the one direction this model is not allowed to be wrong in. A drafted `test_a_bare_name_is_not_resolved_against_a_package_directory` was removed for that reason before the round was reported. |
| 2026-08-21 | blk-01 | `test_blockers.py` gained a section 15 (+9 test functions, plus three helpers `_open_blocker` / `_transcript_entries` / `_split_brain` that are in no figure) for the invariant "a task is `blocked` only while at least one OPEN blocker names it": the last blocker's answer requeues the task, one of two leaves it blocked, an open `loop_fatal` record still counts, a registry already in the split state is repaired by `start`'s preflight and by the top of a real `_run_continuous` iteration, an operator hold survives every one of those, and the transition is asserted in the TRANSCRIPT because `unblock()` clears the reason it carried. |
| 2026-08-21 | blk-01 | Two of those nine are the negative half and are the reason the rest mean anything: every blocker record is compared byte-for-byte (`asdict`, open and closed alike) across a sweep, since "do not resolve blockers as a side effect" is otherwise a claim nothing checks; and the `archive-blocker` case asserts the DEFERRAL under a held `LoopLock` as well as the release without one, so "a live loop owns the registry" is not satisfied by a sweep that simply never runs. |
| 2026-08-21 | blk-01 | The continuous-mode test makes the released task depend on an in-progress one, so the reconciliation produces no READY task and the iteration reaches the exhaustion check instead of kicking off a session that would need a browser — the `(loop)` blocker is what makes that check exit 0, and the Claude/ChatGPT doubles still raise on any call. |
