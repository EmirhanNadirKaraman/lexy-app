# TODO.md

> **For the current ROI-ranked plan, see [`docs/ROADMAP.md`](./ROADMAP.md).**
> This file remains the per-item history (resolved + open), indexed by number;
> ROADMAP.md is the live priority view re-ranked on 2026-05-20.

Bugs and pending work, ordered so that earlier items unblock later items. "Blocks: …" lists downstream work that depends on the fix.

Status legend: 🔴 will fail / data risk · 🟠 correctness / reliability · 🟡 tech debt · 🟢 polish · ✅ resolved · 🚫 won't do / dropped (product decision)

---

## P0 — Foundational. Do before anything else.

### 0a-1. ✅ SRS payload now carries prompt_text + answer_text — RESOLVED 2026-05-19
Backend half of the SRS-UI rewrite. `review_service.get_due_cards` returns `prompt_text` and `answer_text` for every card. For passive cards, prompt = German display, answer = English gloss. For active cards, swapped. Grammar rules use `title` / `short_explanation` (no LLM). Word + phrase glosses come from `llm_service.translate_item_gloss`, cached permanently. Hole 0a's audit test now passes (renamed to a regression guard).

### 0a-2. ✅ Frontend SRS review UI rewrite — RESOLVED 2026-05-19
Hole 0a closed entirely. Changes:
  - **New endpoint** `POST /api/v1/srs/review/{card_id}/produce` with body `{answer: str}` → response `{card_id, correct, expected, submitted, feedback}`. Validates ownership + that the card is active; rejects passive (400 passive_card) and grammar_rule (400 grammar_rule). Routes through normal `apply_progression` so SM-2 advances the same way as self-graded answers.
  - **New `llm_service.evaluate_production`** with tool_use schema returning `{correct, feedback, corrected_form}`. Not cached (high-cardinality user input). MOCK_LLM-aware.
  - **Fast path** in `review_service.submit_production_answer`: normalized exact match skips the LLM entirely.
  - **Frontend `SRSReviewPage`** now uses `prompt_text` as the front and `answer_text` on the back. Passive direction unchanged (self-graded). Active direction shows a text input + "Submit" button + "I don't know" button. **Self-grade is no longer exposed for active cards.** Feedback panel shows the canonical target, what the user typed, and the LLM's verdict.
  - **Frontend types**: `SRSReviewCard` extended with `prompt_text` + `answer_text`; new `SRSProductionResult`. `api/srs.ts` gained `submitProductionAnswer`.

Known limitations: production correctness check evaluates only "did the user produce the target item correctly" — not full sentence grammar correction. Inflection tolerance comes from the LLM's judgement (mock mode does substring match).

### ~0a. 🔴 Active SRS review doesn't actually test production~ — superseded
The original #0a is now split into the two items above. The first half (backend payload) ships. The UI rewrite (#0a-2) is the next leg.

Original problem statement preserved below for context:
**File:** `lexy-app/frontend/src/components/SRSReviewPage.tsx:272–367`
**Problem:** Both passive and active cards display the German word as the prompt. The "reveal" for an active card shows the same German word again. The user is asked "Can you use this naturally in a sentence?" and self-grades. There is no production test (no input field, no LLM evaluation, no translation cue) and no recognition test (no hidden gloss). The entire SRS loop is currently a self-report dialog.
**Fix (active):** show the English translation (or example sentence with the target redacted) as the prompt; expose a text input; route the answer through `llm_service.guided_evaluate` (or a dedicated `srs_evaluate_production`); use the eval result to drive correct/incorrect.
**Fix (passive):** show the English gloss as the prompt, German as the reveal — closer to a traditional Anki front/back card.
**Fix (data):** `srs_cards` rows and the `/srs/due` payload need a `prompt_text` (English gloss) field — currently the schema only carries `display_text` (German). Add via `word_table.gloss_en` or a per-item LLM-cache translation lookup.
**Blocks:** literally everything downstream of "user learns a word." Today the entire SRS UI is a self-grading checkbox.

### 0b. ✅ Active SRS card creation on status_marked_learning — RESOLVED 2026-05-19
`_RULES["status_marked_learning"]` ships with both `passive_srs="create"` and `active_srs="create"`. Marking a word "learning" now schedules BOTH directions. The active card is created at `interval_days=1.0, reps=0` (same defaults as the passive card — the design note about "a few days out" was dropped; `_update_srs` schedules at NOW() and the SRS due-feed handles ordering by `due_date` not by passive-vs-active class). Active production credit is gated: `active_delta=0`, `times_used_correctly_delta=0` — creating a card is exposure, not evidence. Re-marking is idempotent (`INSERT … ON CONFLICT (user_id, item_id, item_type, direction) DO NOTHING`). Regression-guarded in `test_progression.py`:
  - `test_status_marked_learning_creates_active_card`
  - `test_status_marked_learning_does_not_increment_active_level`
  - `test_status_marked_learning_does_not_increment_times_used_correctly`
  - `test_status_marked_learning_does_not_duplicate_cards`

### 1. ✅ Dead `srs_service.py` endpoints — RESOLVED 2026-05-19
File `services/srs_service.py` deleted along with the three legacy router endpoints (`POST /check-answer`, `/magic-sentences`, `/cloze-questions`) and the six Pydantic models that only those endpoints used (`CheckAnswerRequest`, `MagicSentencesRequest`, `SentenceResult`, `MagicSentencesResponse`, `ClozeQuestionsRequest`, `ClozeQuestionResult`). Pre-deletion grep across backend + frontend confirmed zero call sites. The `/api/v1/srs/*` namespace now only exposes the real endpoints (`/due`, `/review/{card_id}`).

### 2. ✅ Atomicity gap in `routers/words.py` status update — RESOLVED 2026-05-19
Implemented Approach A: `progression_service.apply_progression` gained a keyword-only `status_override: str | None = None` parameter. When provided, the status flip happens inside the existing transaction alongside level deltas, SRS card writes, and auto-promotion. The router now makes a single `apply_progression(..., status_override=body.status)` call. `word_service.upsert_word_status` and its private validation constants were deleted (no other callers). `WordStatusUpdate.status` was tightened to `Literal["unknown","learning","known"]` so Pydantic returns 422 for bad values.

### 3. ✅ `os.chdir()` import hacks removed — RESOLVED 2026-05-20
Root cause was `phrase_finder.py:42` loading `"data/final_result.txt"` via a cwd-relative path. Fixed by resolving the path from `__file__` (`Path(__file__).resolve().parent.parent / "data" / "final_result.txt"`). With that one-line change, every chdir site became unnecessary.

**Sites cleaned (5):**
- `lexy-app/backend/services/matcher_service.py` — chdir block replaced with `sys.path.insert(0, scraper_dir)` + import
- `subtitle-scraper/pipeline.py` — same
- `subtitle-scraper/profile_pipeline.py` — same
- `subtitle-scraper/profile_full_pipeline.py` — same
- `tests/test_scraper_channels.py` — fixture simplified

Grep verification: `grep -R "os\.chdir" --include="*.py" .` → 0 hits in production or test code (excluding caches).

Tests: `tests/test_matcher.py` 6/6, `tests/test_scraper_channels.py` 5/5, full root suite 542 passed, backend suite 445 passed / 2 skipped (pre-existing, unrelated).

---

## P1 — Correctness gaps that bite as soon as anyone uses the affected feature.

### 4a. ✅ Notification correctness — RESOLVED 2026-05-19
**Files:** `routers/notifications.py` (rewritten), `subtitle-scraper/pipeline.py:_mark_request`

Two correctness bugs closed:
  - **Lost on disconnect** — fixed. The SSE handler now yields each row first and only runs `UPDATE … SET seen = TRUE` AFTER the yield resumes. Disconnect mid-stream → un-yielded rows stay unseen for re-delivery. The fetch+yield loop is extracted into a module-level `_yield_unseen(pool, user_id)` async generator so it's directly testable.
  - **No failure notifications** — fixed. `_mark_request` now emits a `request_failed` notification (`payload={"reason": error}`) whenever it sets `status='failed'`. All six failure call sites get the notification automatically — they already pass an `error` string. No changes needed at call sites.

### 4b. 🟡 Notification transport refactor — NOT YET DONE
Remaining items intentionally deferred:
  - **3s polling regardless of load.** Switch to Postgres `LISTEN/NOTIFY` — writer (`_notify_user`) issues a `NOTIFY user_notifications, payload` after the INSERT; handler does `await conn.add_listener(...)` and blocks until either disconnect or event. Cuts idle DB load by ~99%.
  - **Unbounded growth.** Add periodic job to drop `seen=true AND created_at < NOW() - INTERVAL '30 days'`.
**Blocks nothing user-visible** (correctness is fixed in 4a). Purely a cost/scale concern.

### 5. ✅ Reading review frontend + mastered → known wired — RESOLVED 2026-05-20
Two of the three sub-issues closed; the dual-schedule reconciliation intentionally deferred.

**Frontend** — new `ReadingReviewPage.tsx` consumes `GET /api/v1/reading/selections/due`, renders the canonical text + sentence context + doc title + optional note, and posts each outcome to `POST /api/v1/reading/selections/{id}/review`. Action buttons: Still learning / Got it / Mastered. Mobile-safe (44px buttons, fluid padding, action row wraps). Empty state, session-complete state, error surfacing. Entry point: a "Reading Review" button in the `BookLibraryPage` header (only shows when `onOpenReadingReview` prop is provided). Route: `/reading-review`. 9 vitest tests in `ReadingReviewPage.test.tsx`.

**Backend** — `routers/reading.py:review_selection` now fires the catalog progression for `mastered` (in addition to the existing `got_it` → `passive_review_correct` and `still_learning` → `passive_review_incorrect`):
```python
elif catalog and body.outcome == "mastered":
    await progression_service.apply_progression(
        pool, user_id, item_id, item_type,
        "status_marked_known",
        status_override="known",
    )
```
Per the project policy "reading Mastered = manual known confidence, NOT active production": `status_marked_known` has `active_delta=0`, `times_used_correctly_delta=0`, `active_srs=None`. So `active_level` doesn't grow, `times_used_correctly` doesn't bump, and no active SRS card is fabricated. The passive SRS card advances via the rule's `passive_srs="correct"` (consistent with the manual Known click in the vocab UI). 4 new tests in `test_reading_progression.py` lock the contract (plus the pre-existing `test_review_mastered_does_not_change_srs_card` was updated to match the new behaviour).

**Dual schedule** — intentionally NOT reconciled. Reading mode keeps its own `reading_selections.next_review_at` schedule alongside `srs_cards`. They continue to diverge after the first review. The policy decision: reading review queue is its own UX surface (book-context-rich), main SRS queue is vocabulary-context-only. Users see the same word due in two places, but each lives in the queue that triggered it. Reconciling would require either a PK bridge (UUID vs SERIAL) or dropping one side; both are bigger refactors than this card. Re-open as a separate issue if the duplication starts confusing users.

### 5a. ✅ Insights filter — RESOLVED 2026-05-18
**File:** `lexy-app/backend/services/usage_events_service.py:59`
Added `'transcript'` to the context IN clause. Subtitle-clicked unknown words now surface in the "Keeps coming up" insight card. Regression-guarded by tests in both `test_audit_holes.py` and `test_insights.py`.

### 5b. ✅ Free-chat matching + guided target selection now handle phrases — RESOLVED 2026-05-19
**Files:** `chat_service.match_learning_words`, `guided_chat_service.get_next_target`, `matcher_service.match_sentence` (fix)

Implemented Path A (reuse existing phrase_finder). Three pieces:
  1. **Matcher fix** — `matcher_service.match_sentence` now does `nlp(sentence)` before passing to `extract_german_logic`. Lands 5 long-broken `test_matcher` tests green as a bonus. Pre-existing failure count drops from 8 → 3.
  2. **Free chat** — `match_learning_words` calls `matcher_service.match_sentence_with_ids` and intersects matched `phrase_id`s with `user_word_knowledge` rows where `item_type='phrase'` and `status != 'known'`. Returned alongside word matches in the same polymorphic shape; `routers/chat.py` iterates unchanged.
  3. **Guided target** — `get_next_target` rewritten so each of the three priority tiers considers both words and phrases (LEFT JOIN to word_table + phrase_table with item_type-aware CASE; Priority 3 UNION ALL over both catalogs).

`get_target_by_id` already supported phrases — untouched.

### 5c. ✅ Unified enrichment dispatcher — RESOLVED 2026-05-19
**Files:** `services/grammar_service.py`, `services/recommendation_service.py`, `services/insights_service.py`

Three pieces:
  1. **New** `grammar_service.enrich_grammar_rules(pool, user_id, rule_ids, language)` — joins `grammar_rule_table` + `user_word_knowledge` + `srs_cards` (passive direction). Display = `title`; secondary = `rule_type`.
  2. **New** `recommendation_service.enrich_by_type(pool, user_id, items, language)` — accepts list of `(item_type, item_id)` pairs, buckets by type, fans out via `asyncio.gather` to the three per-type enrichers, returns a dict keyed by `(item_type, item_id)` to prevent collisions across SERIAL primary keys.
  3. **Refactored** `recommend_items` and `insights_service._build_card` to use the dispatcher. The duplicated per-type dispatch is gone; grammar rules now flow through both paths.

`enrich_items` kept its signature (it's the word-only backend of the dispatcher) — docstring updated to point at `enrich_by_type` for mixed-type callers.

### 5d. ✅ Status change rules — RESOLVED 2026-05-18
**File:** `progression_service.py:_RULES`

All three sub-issues closed in this session:
  - **`status_marked_known`**: stopped fabricating active progress. `active_delta=0`, `active_srs=None`, `passive_srs="correct"` only. Active mastery only grows from real production events (`guided_counted`, `free_chat_used_correctly`, `active_review_correct`).
  - **`status_marked_unknown`**: now `passive_srs="incorrect"`, `active_srs="incorrect"`. Resets existing cards via SM-2 incorrect branch (interval=1 day, ease-0.15, reps=0). Never *creates* a missing active card — `_update_srs` line ~333 documents that `action="incorrect"` is a no-op when the card doesn't exist. Levels intentionally untouched.
  - **`passive_review_correct`**: `passive_delta=1` added. A controlled review is at least as strong evidence as a subtitle click. The "Understood" progress dots now grow from successful reviews.

See `progression_service.py:_RULES` docstrings and §5e below for the manual-known backfill question.

### 5e. 🚫 Backfill: pre-2026-05-18 manual-known users have inflated active progress — WON'T DO (forward-only) 2026-05-23
**Tables:** `user_word_knowledge.active_level`, `user_word_knowledge.times_used_correctly`, `srs_cards` (direction='active')
**Problem:** Before #5d's status_marked_known fix, every manual "Known" click bumped `active_level +1`, `times_used_correctly +1`, and either created or advanced the active SRS card via the SM-2 correct branch. Users who frontloaded their vocab by marking things known therefore have phantom active mastery that doesn't reflect actual production.
**Identification:** rows in `word_usage_events` with `context='status_change' AND outcome='correct'` are manual-known events (per `routers/words.py:85` outcome mapping). Counting them per (user_id, item_id) gives the inflation count.
**Possible backfill (manual, do not auto-run):**
```sql
-- Compute inflation
WITH inflation AS (
  SELECT user_id, item_id, item_type, COUNT(*) AS n
    FROM word_usage_events
   WHERE context = 'status_change' AND outcome = 'correct'
   GROUP BY user_id, item_id, item_type
)
-- Apply (review carefully before running)
UPDATE user_word_knowledge uwk SET
    active_level         = GREATEST(uwk.active_level         - i.n, 0),
    times_used_correctly = GREATEST(uwk.times_used_correctly - i.n, 0)
  FROM inflation i
 WHERE uwk.user_id   = i.user_id
   AND uwk.item_id   = i.item_id
   AND uwk.item_type = i.item_type;
-- Active SRS cards: hard to compute precise reset (a card may also have had real
-- production advances mixed in). Safest is to leave existing cards alone; the
-- status='known' filter excludes them from /srs/due anyway.
```
**Recommendation:** ship forward-only. Inflation is small per user (1 per known click) and isn't load-bearing — `status='known'` is the field that drives downstream filtering. Backfill only if analytics depending on `active_level` shows skew.
**Decision (2026-05-23):** 🚫 won't do. Forward-only accepted. The SQL above is preserved as a runbook should `active_level` analytics ever show meaningful skew — reopen then.

### 6. ✅ `users.settings` JSONB hides channel/genre preferences — RESOLVED 2026-05-23
**File:** `lexy-app/backend/services/settings_service.py`
**Problem:** Memory entry `project_db_migration_todo.md` flags two remaining JSON-in-DB problems — channel preferences are one. Filtering recommendations by followed channels means JSON queries on every call.
**Resolution:** Both preference families are relational now.
  - **Channels** → `user_channel_preference (user_id, youtube_channel_id, preference_kind ∈ {followed,liked,disliked})`, migration 027 (T1.4, 2026-05-20). Backfilled from the JSONB arrays; source of truth flipped to the table.
  - **Genres / categories** → `user_video_category (uid, video_category, preference)` since #5d. `settings_service` reads `liked_categories`/`disliked_categories` from there and aliases them to `liked_genres`/`disliked_genres` on the wire (settings_service.py:150–168).
  - `settings` JSONB now holds only genuinely freeform / lookup data — the `channel_names` display-name cache and freeform prefs — exactly as the original "keep settings JSONB for freeform prefs" plan prescribed.
**Residual (not blocking):** migration 027 intentionally left the legacy channel JSONB keys in place for one release as a read-ignored fallback; a future migration can `DELETE` them once we've confirmed no consumer reads them.

### 7. ✅ Channel flat files vs. DB — RESOLVED 2026-05-20
- Runtime was already DB-only (`pipeline.py:load_channels(cursor)` queries `channel` table; no file fallback existed).
- Seed data consolidated to `subtitle-scraper/seed_data/channels.json` (183 entries, ⊇ old `subscribed_channels.txt` set — verified zero IDs missing). `seed_channels.py` rewritten to read only this file, drop the legacy double-pass (merged + subscribed).
- `merge_channels.py` deleted (inputs + output all gone).
- `channel_finder.py` no longer writes `subscribed_channels.txt`; discovered IDs print to stdout for piping into the content-requests endpoint or seed file.
- Old flat files removed: `channels.json`, `merged_channels.json`, `subscribed_channels.txt`.
- Pipeline docstring scrubbed of flat-file references.
- Tests: `tests/pipeline/test_channel_loading.py` covers (a) `load_channels` returns DB rows, (b) no `open()` of legacy filenames anywhere in `subtitle-scraper/`.
- Deploy note: on a fresh DB, run `python subtitle-scraper/seed_channels.py` after migrations to populate `channel` (idempotent — safe to re-run after schema changes too).

### 8. ✅ books.py LLM repair exception specificity — RESOLVED 2026-05-19
`routers/books.py:359` (batch repair loop) now catches `(anthropic.APIError, asyncpg.PostgresError)` as the expected failure mode (logged at WARNING). A second narrower `except Exception:` block remains as a defensive top-level guard so a bug in `repair_block_by_id` doesn't lose all already-repaired blocks in the batch — that one is logged via `logger.exception()` to capture the full trace.

### 8b. (was original problem statement)
**File:** `lexy-app/backend/routers/books.py:352–354`
**Problem:** `except Exception` catches logic bugs alongside transient LLM errors. Counts them all as "errors" but doesn't surface anything actionable.
**Fix:** Catch specific exceptions (`anthropic.APIError`, `anthropic.RateLimitError`, `asyncpg.PostgresError`). Re-raise on logic errors. Keep retry/skip for the LLM/network class.
**Blocks:** debugging book-import failures, useful telemetry on LLM error rates.

### 9. ✅ Preference-load error surfacing — RESOLVED 2026-05-19
`api/settings.ts` migrated to use `assertOkJson` from `_http.ts`, so 401s flow into the shared auth:expired handler. `usePreferences` gained an `error: string \| null` field; on failure it keeps the in-flight `prefs` instead of resetting to defaults. 4 new Vitest tests lock the contract.

### 9b. (was original #9 problem statement, kept for context)
**File:** `lexy-app/frontend/src/hooks/usePreferences.ts:24` and similar `.catch(() => {})` patterns across hooks
**Problem:** If `getPreferences` fails (auth expired, server down), the UI sees empty defaults silently. Dark mode resets, channel filters disappear. User assumes their settings were lost.
**Fix:** Distinguish "not authenticated" from real errors. Bubble real errors to a top-level error toast. Don't reset preferences state on failure — keep last-known.
**Blocks:** "user trust" — even a single mysterious settings reset trains users to mistrust the app.

### 10. ✅ `useSearch` language hardcode — RESOLVED 2026-05-19
`useSearch` now accepts `language: string = 'de'` as a parameter. `HomePage` reads `recLanguage` from the outlet context and passes `recLanguage || 'de'` in. When the user changes their recommendation language in Settings, search results follow.

---

## P2 — Production-readiness. Block deploys, not local dev.

### 11. ✅ CORS env-driven — RESOLVED 2026-05-19
`backend/main.py` now reads `CORS_ORIGINS` env var (comma-separated, whitespace-tolerant, empty entries dropped, falls back to `http://localhost:5173` when unset). `.env.example` documents the variable. Verified by `tests/test_deploy_readiness.py` (parser unit tests + middleware integration tests).

### 12. ✅ LLM rate limiting — RESOLVED 2026-05-19
In-memory sliding-window limiter in `services/rate_limiter.py`. 30 req/min, 400 req/hour per user. Wired via `core/deps.rate_limit_llm` into 10 LLM-backed routes (SRS production, chat sessions/messages/complete/guided, reading translate/explain, insights prep/examples/grammar, book llm-repair single + batch). `GET /api/v1/srs/due` intentionally exempt (cached glosses). Frontend `_http.ts` maps 429 detail to friendly messages.

Known limit: in-memory, per-process. Multi-worker deployments need Redis. Documented in the module docstring; `check_and_record` signature shaped to allow a Redis backend swap behind the same call site.

### 13. ✅ JWT expired vs malformed — RESOLVED 2026-05-19
`core/deps.py` now catches `jwt.ExpiredSignatureError` first → 401 with `detail="token_expired"` and `WWW-Authenticate: Bearer error="invalid_token", error_description="token expired"` header. Other `jwt.InvalidTokenError` cases keep the generic `detail="Invalid token"` response. Frontend `_http.ts` reacts to the distinguished detail by dispatching `auth:expired` with `reason="expired"` vs `"unauthorized"`.

### 14. ✅ Global frontend 401 handler — RESOLVED 2026-05-19
New `frontend/src/api/_http.ts` exports a shared `assertOk(res)` that on 401 clears `auth_token` + `auth_email` from localStorage and dispatches a `CustomEvent('auth:expired', { detail: { reason } })` where reason is `'expired'` (when backend sends `detail='token_expired'`) or `'unauthorized'` (generic 401). Layout in `App.tsx` listens for the event and resets React token state + navigates to `/`. `reading.ts` migrated to the shared helper as the first consumer. Other api files can adopt incrementally — even before they do, ANY route that goes through `_http.ts` will trigger the handler.

### 15. ✅ Print → logging in scraper/scripts — RESOLVED 2026-05-20
Triaged 288 `print()` calls across 17 files. **62 prints converted** to `logger` calls across 7 files; **226 prints intentionally retained** across 10 files where stdout output IS the product (each gained a one-line docstring/comment explaining why).

**Converted to logging:**

| File | Prints converted | Notes |
|---|---|---|
| `subtitle-scraper/pipeline.py` | 24 | Main cron + content-request subprocess. `basicConfig(INFO)` guarded by `if __name__ == "__main__":`. Exception-handler prints became `logger.exception(...)`. yt-dlp errors became `logger.warning(... exc_info=True)`. |
| `subtitle-scraper/seed_channels.py` | 8 | Deploy migration script. |
| `subtitle-scraper/backfill_channel_names.py` | 10 | Per-row progress combined into one `logger.info("[%d/%d] %s -> %s", ...)` line each. |
| `subtitle-scraper/backfill_video_channels.py` | 9 | Same shape. |
| `subtitle-scraper/backfill_categories.py` | 8 | Same shape. |
| `subtitle-scraper/transcript_fetcher.py` | 1 | Library module — no `basicConfig`, caller wires the root logger. |
| `subtitle-scraper/channel_finder.py` | 2 of 6 | The 2 library-side prints (channels-not-found warning) converted; the 4 inside `if __name__ == "__main__":` block were left as CLI summary output (stdout is the product for the standalone run). |

**Intentionally retained — stdout IS the product:**

| File | Prints kept | Reason (documented in-file) |
|---|---|---|
| `subtitle-scraper/phrase_finder.py` | 13 | Library functions emit nothing; all 13 prints live in `main()` — a dev demo run via `python phrase_finder.py`. |
| `subtitle-scraper/debug_transcript.py` | 31 | Interactive debug tool — diagnostic data to stdout is the product. |
| `subtitle-scraper/profile_pipeline.py` | 27 | Profiler emits timing/coverage tables. |
| `subtitle-scraper/profile_full_pipeline.py` | 23 | Same. |
| `subtitle-scraper/merge_channels.py` | 1 | Result of the merge to stdout. (File has since been deleted — see #7; the triage above is preserved as written.) |
| `subtitle-scraper/channel_finder.py` | 4 (CLI block) | CLI summary on `python channel_finder.py`. |
| `scripts/b1_word_finder.py` | 3 | Dev throwaway — `print(d)` for inspection. |
| `scripts/known_words_fixer.py` | 1 | Same. |
| `scripts/percentage_finder.py` | 46 | Vocab-coverage report tables to stdout. |
| `scripts/validate_tier_lemmas.py` | 20 | Diagnostic mismatch report. (File has since been deleted — refactor Phase 0; it was a broken duplicate of the root `validate_tier_lemmas.py`, importing `app.learning.onboarding`, which only resolves under pytest. The triage above is preserved as written.) |
| `ilp/optimal_set_finder.py` | 57 | ILP solver progress + final coverage tables. |

**Conventions used for converted sites:**
  - `logger.info(...)` — normal progress (loaded N rows, processed X, done).
  - `logger.warning(... exc_info=True)` — recoverable failures (yt-dlp metadata error, missing spaCy model).
  - `logger.exception(...)` — inside `except:` blocks where the traceback matters.
  - All entry-point scripts call `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")` inside their `if __name__ == "__main__":` guard. Library modules (pipeline.py library functions, transcript_fetcher.py, phrase_finder.py, channel_finder.py library half) export `logger = logging.getLogger(__name__)` and let the caller wire the root logger.
  - Message text preserved as closely as practical — `[request] processed video: %s (%s)` reads identically to the previous f-string. No behaviour changes; no new dependencies; no schema/config touched.

**Validation:** `python -m compileall -q subtitle-scraper scripts ilp` clean. Backend `pytest -n auto --tb=no -q` → 445 passed, 2 skipped (matcher_service imports `subtitle-scraper/phrase_finder.py` via `os.chdir` hack; no regression).

### 16. ✅ Lifespan broad excepts — RESOLVED 2026-05-19
`main.py` lifespan's three seed paths (phrase, grammar, content-request resume) now narrow to known types first (`asyncpg.PostgresError`, `FileNotFoundError`, `ImportError`, `OSError` as appropriate per site) and log them as a WARNING with `exc_info=True`. A defensive `except Exception:` remains as a final guard per site — intentionally broad because **startup must NEVER crash on a seed failure** — but now logged via `logger.exception()` so the full trace lands in production logs. Module-level `logger = logging.getLogger(__name__)` added; the per-site `import logging` repeats are gone.

### 16b. (was original problem statement)
**File:** `lexy-app/backend/main.py:39, 50, 64`
**Problem:** Phrase seed / grammar seed / pending-requests resume each `except Exception` and log-and-continue. Intentional ("non-fatal seeding"), but failure modes are invisible in production until someone reads logs.
**Fix:** Keep the broad catch but emit a structured warning (and ideally a notification or metric). Tighten to specific exceptions where the cause is known.

---

## P3 — Architectural debt. Doesn't break things, but every new feature pays the tax.

### 17. ✅ #17-runtime-test-alignment — root pipeline vs `src/app/` duplication — RESOLVED 2026-07-27

> **Closing note (appended; the inventory and options below are preserved as
> written).** Option (B), selective salvage, was executed in full and then
> completed with the deletion. Five batches ported the valuable behaviour onto
> the runtime modules — `tests/runtime/` went **20 → 93 tests** across nine
> files, including the first ever coverage of `eligibility.py` (the i+1 core)
> and `subtitle_segmenter.py`. `src/` (3,462 lines), the 537 tests that only
> targeted it, and the root `conftest.py` sys.path hack were then deleted. Root
> suite **744 → 207** (93 runtime + 114 scraper), all against shipping code.
> Accepted losses: `pipeline_diagnostics.py` (39 profiling tests, no runtime
> equivalent) and the 11 end-to-end smoke tests (runtime is covered
> stage-by-stage instead). The inventory table and per-test picks below are kept
> as the record of how the salvage was chosen.


**Original framing was wrong.** `src/app/` was tagged "orphan refactor with zero callers." A 2026-05-20 inspection found the real picture:

- **Runtime** (the code that actually ships) lives in root `pipeline.py`, `eligibility.py`, `exposure_counter.py`, `exposure_service.py`, `learning_units.py`, `onboarding.py`, `subtitle_cleaner.py`, `subtitle_merger.py`, `subtitle_segmenter.py`, `utterance_quality_filter.py`, `utterance_unit_extractor.py`, `user_knowledge.py`, `word_knowledge.py`, `pipeline_diagnostics.py`, `validate_tier_lemmas.py`. Used by `subtitle-scraper/profile_full_pipeline.py` and `subtitle-scraper/profile_pipeline.py`. **Zero modern test coverage** — `tests/legacy/` doesn't import them; nothing else does either.
- **`src/app/{exposure,extraction,learning,pipeline,subtitles}/`** is a partial refactor (≈1,065 lines vs 3,114 in the runtime modules — ~34% of the logic ported). **Zero runtime callers.** The root `conftest.py` injects `src/` onto `sys.path` so root-level `tests/` can import from it.
- **`tests/{exposure,learning,pipeline,subtitles}/` runs 537 tests against `src/app/`.** Every collected pipeline test currently exercises the refactor, not the runtime.

**Implication:** the project ships untested runtime code and tests an unused refactor. Deleting `src/app/` is fast (one Tier-1 commit) but vaporises the entire pipeline test suite. Finishing the refactor is huge work (≈2,050 lines to port + scraper imports to flip). The pragmatic middle is salvaging the highest-value tests onto the runtime so the runtime gains coverage, then aborting the refactor.

**Three options:**

- **(A) B-min deletion** — `git rm -r src/app/` + `git rm -r tests/{exposure,learning,pipeline,subtitles}/` + remove `sys.path.insert(..., "src")` from root `conftest.py`. ~30 min. Net: −1,065 LOC + −537 tests. Runtime keeps its current zero coverage. Aligned with the old TODO recommendation (b) but doesn't account for the lost tests.
- **(B) Selective salvage (recommended)** — keep the top-N behavioural tests, port them to import the runtime modules, then do (A) on the leftovers. Pre-flight: build an inventory (below) + pick the first batch to port. ~½ day for the first batch + same again later. Runtime gains real coverage in the parts the refactor managed to cover. Pre-port-batches don't delete anything; each merged batch reduces the cleanup blast radius.
- **(C) Finish the refactor** — port the missing ~2,050 lines from runtime into `src/app/`, flip `subtitle-scraper/` imports to `app.*`, delete runtime root modules. Days. Reverses the standing recommendation. Only worth it if `src/app/` is materially better-structured than the runtime, which the inspection didn't establish.

**Recommendation:** (B). Don't delete anything yet. Step 1 is the inventory + top-20 picks below; step 2 is one PR per batch that ports tests onto runtime imports; step 3 is `git rm` once the salvage is harvested.

#### Inventory — test directories ↔ src/app modules ↔ runtime root modules

| Test directory | File | Tests | Lines | src/app module(s) tested | Closest runtime root module | Salvage value |
|---|---|---|---|---|---|---|
| `tests/subtitles/` | `test_subtitle_cleaner.py`        |  78 | 603 | `app.subtitles.cleaning.SubtitleTextCleaner` | `subtitle_cleaner.py` | **HIGH** — pure string transforms; runtime API likely matches one-to-one |
| `tests/subtitles/` | `test_subtitle_merger.py`         |  67 | 643 | `app.subtitles.merging.SubtitleMerger`, `app.subtitles.models.{SubtitleFragment,MergedSubtitleWindow}` | `subtitle_merger.py` | **HIGH** — fragment/window merge invariants, time-bound logic |
| `tests/subtitles/` | `test_subtitle_ingestion.py`      |  44 | 422 | `app.subtitles.ingestion.parse_srt` | `pipeline.py` (parse_srt lives there) | **HIGH** — SRT parser behaviour (formatting preservation), pure input→output |
| `tests/subtitles/` | `test_multi_speaker_guard.py`     |  33 | 317 | `app.subtitles.merging` (dash-prefix guard) | `subtitle_merger.py` | **HIGH** — heuristic flag, easy to lock with table-driven tests |
| `tests/subtitles/` | `test_noise_filtering.py`         |  33 | 255 | `app.subtitles.cleaning` (symbol filters) | `subtitle_cleaner.py` | **HIGH** — pure transform |
| `tests/subtitles/` | `test_quality_filter_metrics.py`  |  43 | 371 | `app.subtitles.quality.UtteranceQualityEvaluator` | `utterance_quality_filter.py` | MEDIUM — metrics counter API; needs runtime parity check |
| `tests/learning/`  | `test_word_knowledge.py`          |  59 | 539 | `app.learning.knowledge.UserKnowledgeStore`, `ExposurePolicy` | `user_knowledge.py` | **HIGH** — i+1 dedup invariant; the load-bearing learning rule |
| `tests/learning/`  | `test_onboarding.py`              |  48 | 385 | `app.learning.onboarding.VocabularyOnboarding`, `LevelTier` | `onboarding.py` | **HIGH** — tier monotonicity (A1 ⊆ A2 ⊆ B1) |
| `tests/exposure/`  | `test_exposure_integration.py`    |  29 | 534 | `app.exposure.counter.QualifiedExposureCounter`, `app.exposure.service.ExposureService`, `app.exposure.models.{CountingPolicy,DuplicateRule}` | `exposure_counter.py`, `exposure_service.py` | MEDIUM — counter + store integration; depends on runtime ExposureService API matching |
| `tests/pipeline/`  | `test_pipeline.py`                |  53 | 993 | `app.subtitles.{merging,segmentation,quality}`, `app.extraction.extractor`, `app.learning.{eligibility,knowledge,units}` | `pipeline.py` + most root modules | MEDIUM — wide-spanning integration; some tests double the per-component coverage above |
| `tests/pipeline/`  | `test_pipeline_diagnostics.py`    |  39 | 625 | `app.pipeline.diagnostics.PipelineRunDiagnostics` | `pipeline_diagnostics.py` | MEDIUM — counter/rate computations; pure math, but depends on runtime diagnostics object having the same fields |
| `tests/pipeline/`  | `test_pipeline_smoke.py`          |  11 | 274 | `app.pipeline.runner.GermanSubtitlePipeline`, `app.learning.{onboarding,knowledge}` | `pipeline.py` (GermanSubtitlePipeline) | **LOW** — end-to-end with real spaCy; tightly bound to refactor class shape; better-rebuilt against the runtime pipeline once the unit tests pass |

**Totals:** 537 tests, 5,961 lines. ~7 files / ~358 tests rated HIGH-salvage. ~4 files / ~168 tests rated MEDIUM. 1 file / 11 tests rated LOW.

#### Top 20 tests to port first

First batch — pure behavioural locks on runtime modules that currently have no coverage. Each is small (≤30 lines), table-driven, and exercises a single transform. Should fit in one PR.

| # | nodeid | Locks behaviour of | Why first |
|---|---|---|---|
|  1 | `tests/subtitles/test_subtitle_cleaner.py::TestStripPositioning::test_positioning_tag_removed` | `subtitle_cleaner.py: SubtitleTextCleaner.clean` (position tags) | Tag stripping is touched by every downstream stage. Easiest fixture. |
|  2 | `tests/subtitles/test_subtitle_cleaner.py::TestStripPositioning::test_bold_tag_removed` | same | HTML-style tag handling |
|  3 | `tests/subtitles/test_subtitle_cleaner.py::TestStripPositioning::test_italic_tag_removed` | same | HTML-style tag handling |
|  4 | `tests/subtitles/test_subtitle_cleaner.py::TestStripPositioning::test_position_with_coordinates_removed` | same | YouTube-style `{\an8}` coordinate tags |
|  5 | `tests/subtitles/test_subtitle_ingestion.py::test_italic_tag_preserved_in_fragment_text` | `pipeline.py: parse_srt` (formatting preservation) | SRT parser invariant: don't strip mid-fragment |
|  6 | `tests/subtitles/test_subtitle_ingestion.py::test_bold_tag_preserved_in_fragment_text` | same | same |
|  7 | `tests/subtitles/test_subtitle_ingestion.py::test_font_colour_tag_text_content_survives` | same | edge case the cleaner relies on |
|  8 | `tests/subtitles/test_subtitle_ingestion.py::test_nested_italic_and_bold_both_preserved` | same | nested tags |
|  9 | `tests/subtitles/test_subtitle_merger.py::test_merged_text_is_space_joined` | `subtitle_merger.py: SubtitleMerger.merge` | Merger output shape |
| 10 | `tests/subtitles/test_subtitle_merger.py::test_start_time_comes_from_first_fragment` | same | Time bound contract |
| 11 | `tests/subtitles/test_subtitle_merger.py::test_end_time_comes_from_last_fragment` | same | Time bound contract |
| 12 | `tests/subtitles/test_subtitle_merger.py::test_original_fragments_preserved_in_order` | same | Stability under merge |
| 13 | `tests/subtitles/test_multi_speaker_guard.py::test_dash_prefix_overrides_soft_signal` | `subtitle_merger.py` (multi-speaker guard) | Heuristic flag; runtime correctness depends on this |
| 14 | `tests/subtitles/test_multi_speaker_guard.py::test_dash_prefix_overrides_tiny_gap_unconditional_merge` | same | Same guard, different branch |
| 15 | `tests/subtitles/test_multi_speaker_guard.py::test_en_dash_prefix_blocked` | same | Unicode dash handling |
| 16 | `tests/subtitles/test_multi_speaker_guard.py::test_em_dash_prefix_blocked` | same | Unicode dash handling |
| 17 | `tests/subtitles/test_noise_filtering.py::test_standalone_musical_note` | `subtitle_cleaner.py` (symbol filter) | Pure character-set test |
| 18 | `tests/subtitles/test_noise_filtering.py::test_zero_width_format_character` | same | Pure character-set test |
| 19 | `tests/learning/test_word_knowledge.py::test_second_exposure_same_content_is_rejected` | `user_knowledge.py: UserKnowledgeStore` (i+1 dedup invariant) | Load-bearing for the entire learning loop |
| 20 | `tests/learning/test_onboarding.py::test_b1_is_superset_of_a2` | `onboarding.py: VocabularyOnboarding` (tier monotonicity) | Load-bearing for tier semantics |

Coverage of the batch: 8 cleaner + 4 merger + 4 guard + 2 noise + 1 knowledge + 1 onboarding. Hits the most-used runtime helpers (`parse_srt`, `SubtitleTextCleaner`, `SubtitleMerger`, multi-speaker guard) plus the two correctness invariants the learning loop depends on.

**Per-test porting recipe** (for when migration is approved):
1. Change the import in the ported copy from `app.subtitles.cleaning` to `subtitle_cleaner` (etc.).
2. Re-resolve any class/function rename diffs between the refactor and the runtime (verify by grepping the runtime for the symbol name).
3. Run the single test against the runtime; expect either green (pure port) or a runtime-only behaviour difference (in which case adjust the assert or drop the test, don't change the runtime).
4. Once all 20 are ported and green, drop the refactor copies from `tests/`.

#### Out of scope for this card

- **#3 (`os.chdir` import hacks)** has since been resolved separately (2026-05-20 — `phrase_finder` now resolves its data path from `__file__`, so every call site can just `sys.path.insert` and import normally).
- Don't change `subtitle-scraper/` imports.

**Status:** Batch 1 shipped 2026-05-20 (W4) — 20 behavioural tests ported into `tests/runtime/` (subtitle cleaner / merger / ingestion / multi-speaker guard / word_knowledge / onboarding). Runtime APIs matched the refactor exactly; no production code changed. Batches 2+ not yet started; `src/app/` and the rest of `tests/{subtitles,learning,exposure,pipeline}/` still in place pending further salvage.

**Status (appended 2026-07-27):** batches 2–5 shipped and the tree is deleted — see the closing note at the top of this item. Batch 2: segmentation + onboarding tiers (23). Batch 3: quality filter + exposure (24). Batch 4: exposure state progression (13). Batch 5: eligibility + unit extraction (13). No runtime/refactor behaviour mismatch was found in any batch — every ported test passed on first run against the runtime modules, which is the evidence that deleting the refactor lost nothing already recovered.

### 18. ✅ Hardcoded language config in scraper — RESOLVED 2026-05-25
**Was:** `subtitle-scraper/pipeline.py` hardcoded `LANG_MODEL_MAP` / `LANG_TRANSCRIPT_CODES` / `NO_MORPH_LANGS` as three separate dicts; adding a language meant editing all three (+ a duplicated `NO_MORPH_LANGS` in `profile_pipeline.py`).
**Resolution:** new single-source `subtitle-scraper/language_config.py` — a `LANGUAGES` dict `{code: {spacy_model, transcript_codes[], has_morphology, phrase_extractor}}` + helper fns (`get_spacy_model_name`/`get_transcript_codes`/`has_morphology`/`get_supported_languages`) + validation-on-load + derived `MODEL_MAP`/`TRANSCRIPT_CODES`/`NO_MORPH_LANGS`. `pipeline.py` re-exports those under the historical `LANG_*` names (`from language_config import … as LANG_*`), so all 11 call sites + the profile scripts are unchanged; `profile_pipeline.py`'s duplicate now imports from the config. **Adding a language = edit `language_config.py` only** (+ install the spaCy model). See `docs/MAINTENANCE.md` "How to add a scraper language".
**Format choice:** plain-Python module, NOT YAML/table — PyYAML is only transitively available (not declared), and the goal was centralisation, not a config-file format; a module is zero-dependency + trivially testable. Can migrate to a table later if needed.
**Preserved exactly:** `de` → `de_core_news_md` (medium model; the rest `_sm`); the `LANGUAGES` insertion order (load-bearing — `get_transcript` iterates `TRANSCRIPT_CODES.items()` for subtitle auto-detect); unknown-language behaviour. `POS_LIST` stayed in `pipeline.py` (not language-specific). Tests: `tests/test_language_config.py` (9 — values/order/helpers/unknown-lang/malformed-config/pipeline-no-longer-hardcodes); scraper suites green (`test_scraper_channels`/`test_scraper_es_path`/`test_phrase_dispatcher`).

### 19. ✅ Phrase extraction is German-only — RESOLVED 2026-05-21
**File:** `subtitle-scraper/phrase_finder.py`, called from `subtitle-scraper/pipeline.py:376`
**Problem:** `extract_german_logic(doc)` is hard-wired. Any non-German content gets no phrases extracted.
**Resolution:** `extract_phrases(doc, language)` dispatcher shipped 2026-05-21 (Stage 1 of the second-language plan). Routes `de`→`extract_german_logic`, every other language→`[]`, so the pipeline runs cleanly for any language. `insert_phrases` is a no-op for non-German by design (pipeline.py:447–449). The "opens the app to a second language" goal is met — Spanish ingests words-only and clean.
**Remainder:** a Spanish-*specific* extractor (real collocations/reflexives) is tracked separately as **#36** — purely additive, post-MVP.

### 35. ✅/🚫 Subtitle-language selection ignores the video's original audio language — manual-track preference RESOLVED 2026-05-23; auto-caption fallback WON'T DO (product policy)
**Files:** `subtitle-scraper/transcript_fetcher.py:fetch_with_retries` (the `language_codes` loop), `subtitle-scraper/pipeline.py:LANG_TRANSCRIPT_CODES`, `pipeline.py:get_transcript`
**Problem:** Many channels publish a video with **manual subtitles in several languages** (creator-uploaded EN + ES + …) or only auto-translations. Today the fetcher walks a fixed code list and takes the first manual track it finds. For the channel loop the requested language biases this; for `--requests-only` video requests there's no hint, so `LANG_TRANSCRIPT_CODES` dict-order wins (English first). Net effects observed during the Spanish dogfood (2026-05-23):
  - A Spanish-spoken enchufetv video with a manual **English** track + Spanish only as auto-translation got ingested as `language='en'` — the audio is Spanish, but the wrong subtitle track was chosen.
  - We have no signal for "what language is actually spoken in this video," so we can't prefer the matching subtitle track.
**What to add:**
  1. **Detect the original audio language.** yt-dlp's info dict exposes hints — `info.get("language")` (the uploader-declared primary language) and per-format `language` tags on the audio streams; the *original* audio track is often flagged (`format_note`/`language_preference`, or the track without a dub marker). Capture this in `transcript_fetcher`.
  2. **Prefer the subtitle track matching the original audio** when multiple manual tracks exist, instead of fixed dict-order. Fall back to the requested/seeded language, then to auto-generated in that language, then skip.
  3. **Handle multi-language channels** where different videos are in different languages — store the *detected* language per video (already done) but pick the subtitle track from the audio language, not the channel's seed language.
**Why it matters:** unlocks creators who subtitle in multiple languages (the common case for big channels) and stops mis-tagging Spanish-audio videos as English. Pairs with #18 (move `LANG_TRANSCRIPT_CODES` to config) and the eventual auto-caption fallback decision.
**Risk:** medium — yt-dlp's original-audio signal isn't 100% reliable across all videos; needs a sane fallback chain and probably a per-video override. Don't let a wrong guess silently ingest the wrong language — when unsure, prefer the seeded/requested language.
**Blocks:** real multi-language corpus volume; clean Spanish ingestion from mixed-subtitle channels.
**PRODUCT POLICY (2026-05-23): manual target-language subtitles ONLY.** We deliberately do **not** ingest auto-generated captions — machine-transcribed quality is too unreliable to be safe learning material (wrong words, missing punctuation, no speaker boundaries). The scraper already prefers manual tracks; that policy stays. If a video/channel has no manual subtitle track in the target language, **skip it** — do not fall back to auto captions.

**Status (2026-05-23):**
  - ✅ **Parts 1 & 2 (manual-track preference) — RESOLVED.** `pipeline.get_original_audio_language(video_id)` reads `info["language"]`, normalizes (`es-419`→`es`), returns None for unknown/missing. `get_transcript`'s auto-detect branch floats the original-audio language to the front of the search order, so a video with manual EN **and** manual ES + ES audio now tags `es`. Explicit `--language` path is intentionally untouched (seed language stays authoritative — protects the 248-video UNED run). This is fully in line with the manual-only policy: it only ever reorders among *manual* tracks.
  - 🚫 **Auto-generated-caption fallback — WON'T DO.** The enchufetv case (ES audio, manual EN only, ES exists *only* as an auto-translation) will keep tagging EN or skipping. That is the intended outcome under the manual-only policy, not a gap to close. Grow the Spanish corpus from channels/videos that ship **manual Spanish subtitles** (e.g. the UNED educational channel) rather than relaxing the quality bar.
  - 🟡 **Part 3 (per-format audio-track inspection) — deferred, optional.** Current impl uses the single `info["language"]` hint, not per-stream dub detection. Sufficient for now; revisit only if mis-tagging recurs on bilingual-audio videos.
  - The `--requests-only` video path benefits automatically from parts 1 & 2 (it calls `get_transcript` with no language).

### 36. 🟡 Spanish phrase extractor — slices 1–4 shipped 2026-05-24, slice A (gerunds) 2026-07-27; positive imperatives deferred, model swap measured and rejected
**File:** `subtitle-scraper/phrase_finder.py` (`extract_spanish_logic`, registered under `'es'` in `_LANGUAGE_EXTRACTORS`)
**Problem:** Spanish v1 was words-only — `extract_phrases(doc, 'es')` returned `[]`.
**Status (first slice, #36):** ✅ `extract_spanish_logic(doc)` ships two pattern families:
  - **Reflexive verbs** — finite verb + an agreeing reflexive clitic (me/te/se/nos/os); person/number agreement rejects non-reflexive object clitics ("me ve" ≠ `verse`). Canonical = verb lemma + "se" (e.g. "Nos acostamos…" → `acostarse`).
  - **Verb + preposition** — conservative allowlist (`depender de`, `pensar en`, `hablar de`, `soñar con`, `esperar a`, `tratar de`, `ayudar a`, `aprender a`, `empezar a`, `acabar de`). Canonical = "&lt;lemma&gt; &lt;prep&gt;".
  Output shape is identical to the German extractor (`dictionary_entry`/`sentence_phrase`/`logic`/`match_type`/`indices`), so `pipeline.insert_phrases` consumes it unchanged. Tests: `tests/test_spanish_phrase_extractor.py`.
**Status (slice 2, 2026-05-24):** ✅ a third pattern family + a broader allowlist.
  - **Clitic-attached infinitives** — `es_core_news_sm` fuses the enclitic into one `VERB` token (`VerbForm=Inf`, surface ends in the clitic). The base infinitive is recovered by stripping the clitic suffix (longest-first so `lavarnos`→`lavar`, not `lavarn`; validated by an ends-in-`r` check), then `+"se"`: "quiero lavarme" → `lavarse`, "voy a levantarme" → `levantarse`, "necesito ducharme" → `ducharse`, "puedo acostarme" → `acostarse`. Overrides apply to the recovered base.
  - **Broadened verb+prep allowlist** (+`confiar en`, `consistir en`, `creer en`, `jugar a`, `salir de`, `llegar a`) and the prep-attachment search now also matches `mark` deps (so `consiste en practicar` resolves, not just `case`-marked noun objects).
**Status (slice 3, 2026-05-24):** ✅ reflexive+preposition combos (finite forms).
  - New `_ES_REFLEXIVE_PREP` set (`acordar de`, `enamorar de`, `quejar de`, `preocupar por`, `olvidar de`) — deliberately SEPARATE from `_ES_VERB_PREP`. A finite verb with an agreeing reflexive clitic AND a matching prep candidate emits the *reflexive* canonical `f"{lemma}se {prep}"` → "acordarse de" (match_type `es_reflexive_prep`), never "acordar de". The combo **suppresses the bare reflexive for that token** ("acordarse"), since the collocation is the real learning unit; the bare form still surfaces from prep-less occurrences elsewhere (phrase_table dedups across the corpus). All 5 verb lemmas verified correct against `es_core_news_sm` (no #39 override needed). Tests: 7 in `tests/test_spanish_phrase_extractor.py` (5-verb matrix + per-token-suppression guard + match_type lock; the pre-existing deferred-state test updated to the new behaviour).
**Status (slice 4, 2026-05-24):** ✅ reflexive+preposition combos on a clitic-attached infinitive.
  - Block 3 (clitic-infinitive) now also checks `_es_prep_candidates(token)` against `_ES_REFLEXIVE_PREP`: "quiero acordarme de…" → `acordarse de` (was: bare `acordarse` only). Same suppress-the-bare rule as the finite path (block 1a); `match_type=es_reflexive_prep`. The infinitival "a" ("voy a enamorarme…") is filtered for free — only `(base, prep)` pairs in the set emit. Probe-confirmed against `es_core_news_sm`: the prep attaches to the fused infinitive token as a grand-ADP (`case`), so `_es_prep_candidates` finds it; no model switch / override needed. Tests: +9 in `tests/test_spanish_phrase_extractor.py` (5-verb matrix, bare-fallback when no prep, non-allowlisted-prep negative, no-duplicate-bare guard).
**Status (slice A, 2026-07-27):** ✅ gerund + enclitic reflexives.
  - `Está lavándose…` → `ducharse`-style canonicals now extract: `lavarse`, `ducharse`, `levantarse`. Block 3's gate was `VerbForm=Inf` only, so gerunds fell through **both** paths — block 1 finds no separate clitic child (it is fused into the token) and block 3 skipped non-infinitives. Gate now accepts `Ger`.
  - Gerunds cannot reuse the infinitive's surface strip (`lavándose` − `se` = `lavándo`, not an infinitive). The base comes from the lemma instead (`lavar él` → `lavar`), behind `_es_gerund_base`.
  - **That lemma is dangerous, not merely unreliable.** For `preguntándome` the model returns `preguntándomar`, which *ends in -ar* and passes a naive infinitive check while being nonsense; emitting it would put `preguntándomarse` in `phrase_table`. Three checks reject it: ends in ar/er/ir, **shorter than the surface minus its clitic**, and de-accented stem prefixes the surface. A real gerund is always longer than its infinitive, so the length check is what does the work.
  - Tests: +10 in `tests/test_spanish_phrase_extractor.py` (3 positives, `match_type=es_reflexive_gerund`, the garbage-lemma rejection, a direct unit-level guard test with fabricated lemmas, and the NOUN-mistag case pinned).

**Deferred (later slices):**
  - **Imperatives — the previous framing here was wrong.** It said they "extract nothing because `es_core_news_sm` tags them as non-verbs". Measured 2026-07-27, there are **three** distinct causes and that is only one:
      1. **Negative imperatives already work.** `No te levantes.` → `levantarse`, and always did — the clitic is a separate token, so block 1 catches it. Now regression-guarded.
      2. **Positive fused imperatives** (`Lávate las manos.`) tag as VERB with `VerbForm=Fin` and the clitic fused, so no path applies. The lemma is garbage (`lávate` → `lávatir`) and the surface strip leaves `láva`, which is not an infinitive — recovering `lavar` needs real morphology (de-accent + conjugation), not a suffix rule.
      3. **Sentence-initial PROPN mistagging is positional, not categorical.** `Levántate ahora.` → PROPN, but `Por favor, levántate ahora.` → VERB. Capitalisation at sentence start is the trigger. Unfixable inside the extractor.
    Current state is pinned by tests so a model change surfaces visibly.
  - ~~**Measure `es_core_news_md` before choosing an imperative strategy.**~~ **MEASURED 2026-07-27 — both larger models REJECTED. Stay on `es_core_news_sm`. A model swap does not solve slice B; do not re-run this.**
    Compared `sm` / `md` / `lg` over 13 sentences (8 positive fused imperatives + the working reflexive, gerund and verb+prep cases):
      - **0 of 8 positive fused imperatives extract on any model.** Not one.
      - **Usable lemmas: 1 of 8, and only on `md`** (`Lávate` → `lavar tú`, `Reflex=Yes`). Everything else is garbage on every model — `levántatar`, `siéntatir`, `acuérdatir`.
      - **PROPN mistagging moves rather than disappears.** `md`/`lg` fix `Levántate` / `Siéntate`, but *introduce* it on `Dúchate`, `Lavaos` and `duchándome`.
      - **Regression scorecard against the 15 canonicals the suite already expects: `sm` 15/15, `md` 11/15, `lg` 11/15.** The larger models break `No te levantes.` (→ ADJ), `Estoy duchándome.` (→ PROPN, i.e. **slice A**), `Dependo de mis padres.`, `Nos acostamos tarde.` (md) and `Me acuerdo de eso.` (lg).
    So `md`/`lg` add nothing on the target problem and cost five working behaviours. Measurement was local-only; no dependency file changed and the model choice is unchanged.
  - **Slice B, if it happens at all, must be a deterministic exact-match strategy** — e.g. a small curated imperative→infinitive table (`lávate`→`lavarse`, `levántate`→`levantarse`, `siéntate`→`sentarse`, `acuérdate`→`acordarse`, …) keyed on the de-accented surface and emitting *only* on an exact hit. **Not** a stem heuristic: the high-frequency reflexive imperatives are exactly the stem-changing irregulars a heuristic gets wrong (`siéntate` → `sentarse`, `acuérdate` → `acordarse`).
    **A wrong canonical is worse than a missing one.** A miss costs one phrase; a wrong one writes a non-existent word into `phrase_table`, which then reaches recommendations and SRS and gets taught. That asymmetry is why the gerund path in slice A guards its lemma rather than trusting it, and it should govern slice B too.
    **Note the tagging blocker survives any table:** `Dúchate ahora.` is INTJ on `sm` and PROPN on `md`/`lg`, so the token never reaches the extractor at all. A table fixes lemma recovery, not tagging — expect partial coverage even after implementing it.
  - **Corpus check 2026-07-27 — our own Spanish data does NOT justify building the table yet. Slice B stays deferred.**
    Measured the local corpus to see whether it could seed and rank a first table:

    | Metric | Value |
    |---|---|
    | Spanish videos | 248 |
    | Spanish subtitle lines | 53,409 |
    | Spanish tokens | 410,462 |
    | Unique Spanish tokens | 26,513 |

    Large enough in aggregate — but **the register is wrong**. 246 of 248 videos are UNED (open-university lecture/documentary Spanish), with one video each from `elrubiusOMG` and `enchufetv`. Lecture Spanish barely uses second-person commands, which is precisely the form slice B targets.

    Probing 39 candidate forms found **55 occurrences across 12 forms**, and **8 of those 12 occur exactly once**. Only `fíjate` (24) is at all frequent — and it is arguably a discourse marker ("note that") rather than a learning unit. Every named target form is **absent**: `lávate`, `levántate`, `siéntate`, `dúchate`, `cállate`, `duérmete`, `ponte` = 0 occurrences; `acuérdate` and `vete` = 2 each. A table built from this data would be dominated by the one form we least want while missing everything a learner needs first.

    The probe also re-confirmed the blocker on real corpus data rather than constructed sentences: **every** candidate's lemma was garbage (`fíjatar`, `damar`, `quédatir`, `escúchamir`), and the forms scatter across POS tags — `déjame` / `imagínate` / `muévete` tag PROPN, `olvídate` / `acuérdate` / `vámonos` tag NOUN. Most would never reach the verb loop no matter how good the table is.
  - **Recommended future strategy — hybrid, but only once the Spanish corpus turns dialogue-heavy.** In that order, deliberately:
      1. **Local corpus for frequency and examples.** Ranking should come from what our learners actually meet. It cannot do that job today (55 occurrences is not a frequency signal); it can once conversational channels are ingested — and that also sidesteps the licensing question entirely.
      2. **External sources for supplemental ranking only, licence checked BEFORE vendoring anything.** Nothing has been downloaded or added. **Wiktionary** Spanish conjugation data (CC-BY-SA) is the most viable candidate — it maps imperative→infinitive directly, which *is* the table, and share-alike is manageable for a small derived file with attribution. Subtitle corpora (OpenSubtitles / OPUS) have the right register but are user-contributed with unclear rights: usable to derive a ranking offline, **not** to commit into this repo.

    Revisit when the corpus has meaningful conversational content. Until then slice B would ship near-zero user value on the content we actually have, and the tagging blocker would cap it further.
  - Promote the allowlist to data/config; idioms, MWEs, subjunctive.
  - **Lemma quality** is handled by the override layer — see #39 (the `ducha`→`duchaber` family; note the clitic-infinitive path lemmatizes `duchar` correctly without an override).
**Blocks:** nothing — purely additive. German behaviour untouched (regression-guarded by `tests/test_phrase_dispatcher.py` + backend `test_matcher.py`).

### 39. 🟡 Lemma-override layer — slices 1+2+3A+3B+3C(dry-run) shipped 2026-05-24; live-LLM/frontend deferred
**Origin:** #36 first slice surfaced that spaCy's Spanish lemmatizer hard-codes wrong lemmas for some verbs (`ducha`→`duchaber`, `ducho`→`duchir`); confirmed identical across `es_core_news_sm`/`_md`/`_lg` (2026-05-24). No static model fixes it, so phrase canonicals built from `lemma + "se"` are sometimes wrong.
**Fix (recommended): a curated override table.** `lemma_override` consulted by the extractor before trusting spaCy's lemma. Seed it from an LLM pass (Haiku validates/normalizes a batch of extracted canonicals — cheap, cached) and/or a hand-curated list of known errors. Deterministic, correct, simple. Applies to German too if needed.
**Status — slice 1 SHIPPED (2026-05-24):** the deterministic foundation.
  - Migration 033: `lemma_override(id, language, observed_lemma, corrected_lemma, surface_form, pos, source, status, confidence, created_at, updated_at)`. v1 keys on `(language, observed_lemma)` (partial-unique where surface_form/pos NULL); `surface_form`/`pos` reserved for context-sensitive slice-2 rows. Seeded `es: duchaber→duchar, duchir→duchar` (source=`manual`).
  - `phrase_finder.extract_phrases(doc, language, overrides=None)` → extractors apply the map at the single point the verb lemma is read (before `+"se"` and before the verb+prep allowlist check). German extractor accepts the param but doesn't apply it (no German seeds yet; byte-identical).
  - `pipeline.load_lemma_overrides(cursor, language)` loads active context-free rows; `populate` loads once per video and threads them into `insert_phrases`.
  - Tests: `tests/test_spanish_phrase_extractor.py` (override fixes `ducharse`, doesn't touch unrelated lemmas, None==pre-#39, loader, end-to-end through `insert_phrases`).
**Status — slice 2 SHIPPED (2026-05-24):** backend matcher correctness + override wiring.
  - **Fixed the German-model bug:** `matcher_service._extract` parsed *every* language with `_pf.nlp` (the German model), so Spanish chat matching was unreliable regardless of overrides. Now `_model_for(language)` uses phrase_finder's resident model for `de` and `nlp_service`'s per-language cache otherwise; languages without an extractor skip parsing; all load failures fail open to "no phrase extraction" (a `threading.Lock` serialises the first per-language load against the executor's threads).
  - **Override wiring into chat:** `match_sentence_with_ids` (it has the pool) loads `lemma_override` via `_load_overrides_for(pool, language)` and threads the dict into the sync extractor — so `Se ducha…` now matches `ducharse` in chat. Loaded per call (fail-open on missing table / DB error); not gated on language (German no-op until seeded). The override-less `/sentences/match` route is a deliberate exception (stateless utility).
  - Tests: `lexy-app/backend/tests/test_matcher.py` (es parsed via Spanish model — spy + behavioural; German unchanged; unknown→[]) + `test_chat_language.py` (`match_sentence_with_ids` applies the seeded override → `ducharse`).
  - **Residual:** full chat phrase *credit* for Spanish still needs Spanish rows in `phrase_table` + the user learning them (the scraper now produces these); the matcher unblock is necessary but the catalog has to fill in. German extractor still ignores overrides (no German seeds) — apply when needed.
**Status — slice 3A SHIPPED (2026-05-24):** the community-signal inbox (flag, not vote). Full design in `docs/LEMMA_OVERRIDE_WORKFLOW.md`.
  - Migration 034: `lemma_correction_candidate` — a SIGNAL table, never read by the extractor/matcher, never mutates `lemma_override`. `suggested_lemma`/`context_text` are `NOT NULL DEFAULT ''` ('' = absent) so the pending-dedup partial-unique index is on plain columns. Cross-user pending dedup on `(language, surface_form, observed_lemma, suggested_lemma, context_text)` — two people flagging the same thing bump `report_count` on one row (`user_id` deliberately NOT in the key). `user_id` is `ON DELETE SET NULL` (signal outlives the reporter).
  - `POST /api/v1/lemma-corrections` (auth, per-user 30/hr throttle) → create-or-bump pending candidate; `GET /api/v1/admin/lemma-corrections` (`require_admin`, read-only review queue). `lemma_correction_service` (`create_candidate` via INSERT…ON CONFLICT…DO UPDATE report_count+1; `list_candidates`). Schema normalizes missing/blank optionals → '' via a `model_validator` (a `field_validator` skips defaults → would hit the NOT NULL column).
  - Tests: `tests/test_lemma_corrections.py` (+17 — auth gate, create, validation matrix, cross-user dedup, separate-on-different-suggestion, **never-mutates-`lemma_override`** guard, admin-only list, per-user throttle). Per-worker-unique `surface_form` (the dedup key is global/cross-user).
**Status — slice 3B SHIPPED (2026-05-24):** the human-gated promotion (signal → authority).
  - `POST /api/v1/admin/lemma-corrections/{id}/accept` (`require_admin`, transactional): `FOR UPDATE` the candidate → must be `pending` (else 409) → choose corrected lemma (request `corrected_lemma` → candidate `suggested_lemma` → else 400) → **upsert** a context-free `lemma_override` (`source='user_flag_reviewed'`; `ON CONFLICT (language, observed_lemma) WHERE surface_form IS NULL AND pos IS NULL DO UPDATE` — updates, never duplicates) → flip candidate `accepted` (+reviewed_by/at/note). Override-write failure rolls back the candidate flip. The ONLY path from a user signal to extraction truth, through a human.
  - `POST /api/v1/admin/lemma-corrections/{id}/reject` (`require_admin`, transactional): mark `rejected` (+review metadata); never writes `lemma_override`. No reopen (reviewed → 409).
  - `lemma_correction_service.{accept,reject}_candidate` + domain exceptions (`CandidateNotFound`→404, `CandidateNotPending`→409, `NothingToPromote`→400) mapped in the router. Tests: +12 in `test_lemma_corrections.py` (incl. the load-bearing existing-override-updates-not-duplicates upsert test); 3A's `test_post_never_mutates_lemma_override` re-scoped to a unique observed_lemma (3B now writes `lemma_override` concurrently under -n auto).
**Status — slice 3C SHIPPED (2026-05-24, dry-run only):** advisory LLM adjudication, no live calls.
  - `POST /api/v1/admin/lemma-corrections/{id}/adjudicate` (`require_admin`, **read-only**): returns a proposal `{decision, proposed_corrected_lemma, confidence, reason}` for a pending candidate. Never writes `lemma_override` / changes the candidate — an admin still decides via accept/reject. The adjudicator is an **injected** async callable (`get_lemma_adjudicator` dependency, returns `None` in 3C → endpoint **503**); tests inject a fake (service-level or `dependency_overrides`). Malformed output → 502. `adjudicate_candidate_dry_run` + `build_adjudication_input` + `format_adjudication_prompt` (deterministic, no model call — the prompt instructs "do not trust the user suggestion blindly"). Tests: +12 in `test_lemma_corrections.py`.
**Slice 3C live-wiring + frontend (DEFERRED):** a real `llm_service`-backed adjudicator behind `get_lemma_adjudicator` (Haiku, cached) and/or a batch pass; optional persisted LLM-proposal history; a frontend flag button + admin review screen. Will need to invalidate the per-call override load if it moves to a cache.
**Stretch idea (user, 2026-05-24): community voting on lemma corrections.** Let users vote; once a quorum agrees, the override is applied.
  - *Implementable?* The CRUD is easy: a `lemma_vote` table + endpoint + a threshold check that promotes a winning correction into `lemma_override`. The lookup layer is the same as the curated approach.
  - *Will it work correctly?* Risky as the PRIMARY mechanism. (1) Our users are **learners** — by definition the least-qualified to adjudicate lemmatization, and most don't know what a "lemma" is. (2) Lemma correctness is **objective**, not a matter of opinion to vote on — `duchar` is simply right; voting can converge on a wrong answer. (3) Needs abuse/sybil protection, quorum tuning, and moderation — real cost for worse accuracy than an LLM/curated table. (4) Context-sensitivity (noun "ducha" vs verb) makes a flat surface→lemma vote ambiguous.
  - *Better shape if we want community input:* let users **flag** "this looks wrong" (low-skill, low-stakes signal), then route flagged items to an **LLM adjudicator or admin** that writes the override — community surfaces candidates, an authority decides correctness. Keeps the crowd doing what it's good at (spotting oddities) and keeps correctness with something qualified.
**Blocks:** higher-quality Spanish (and any future L2) phrase canonicals; cleaner SRS/display surfaces.

### 37. 🟡 One video can only be ingested in a single language (no multi-track capture)
**Files:** `subtitle-scraper/pipeline.py` (`get_transcript`, `populate`, `main` loop, `processed_videos`/`video_blacklist` dedup), `video` / `sentence` / `word_to_sentence` schema, downstream `videos.py` + frontend video→sentence views.
**Problem:** A single video that ships manual subtitles in **multiple languages** (e.g. a TED talk originally in Spanish with both ES and EN manual subtitle tracks, possibly plus an EN dub audio track) can only be captured **once, in one language**. The current model assumes one video = one language:
  - `video.video_id` is the PK and carries a single `language`; `sentence` rows FK to `video_id`; `word_to_sentence` hangs off those sentences.
  - The scraper's `processed_videos` set + `get_transcript` select exactly one subtitle track per `video_id` (see #35 — it picks the original-audio-language track, or the seeded `--language` track). A second run on the same `video_id` is skipped as already-processed.
  - So the ES and EN tracks of the same talk are mutually exclusive: whichever is selected first wins; the other is never ingested.
**Why it might be worth doing:** the same video in two languages is genuinely useful learning material — aligned content for cross-language reference, and it doubles usable corpus for free on bilingual channels (TED, news, institutional). A learner studying ES gets the ES track; the EN track of the same talk could serve EN learners or power translation/i+1 alignment.
**Why it's costly (the assumptions to unwind):**
  1. **Schema** — `video_id` can no longer be the unit of language. Either make the unit `(video_id, language)` (composite key on `video`, cascade to `sentence`), or split into a `video` (metadata, one row) + `video_track`/`transcript` (per-language, sentences hang off the track). The latter is cleaner but a bigger migration.
  2. **Scraper dedup** — `processed_videos` and `video_blacklist` must become language-aware (`(video_id, language)`), so a video processed in ES can still be processed in EN.
  3. **Transcript selection** — `get_transcript` would need a "fetch ALL available manual tracks" mode rather than "pick one", and the loop would ingest each.
  4. **Downstream** — `videos.py` reading-stats, search, and any frontend that assumes one language per video need to handle a video appearing under multiple languages. The polymorphic `(item_id, item_type)` learning model is already language-agnostic per item, so `user_word_knowledge`/`srs_cards` are fine — the work is all in content tables + scraper + video-facing views.
**Decision needed first:** is bilingual capture actually wanted, or is one-track-per-video (the simpler model) sufficient? Don't build the schema change speculatively. If yes, prefer the `video` + `video_track` split over composite-keying `video` (less churn on existing FKs).
**Risk:** high — touches the most-referenced content tables (`video`, `sentence`) and the scraper's core loop. Sequence it behind a clear product decision.
**Relation to #35:** #35 makes the *single* track we pick the right one (original audio language). #37 is the orthogonal question of capturing *more than one* track. They don't conflict; #37 supersedes the one-track limit only if/when bilingual capture is committed to.

### 38. ✅ SearchBar autocomplete (`/api/suggest`) is German-phrase-only — RESOLVED 2026-05-23 (route B)
**Resolution:** rewrote `search_service.suggest` to do a frequency-ranked, language-scoped word prefix match (route B — precompute). Migration 032 adds `word_table.frequency` (INT, backfilled from `word_to_sentence` counts) + a functional prefix index `(language, lower(word) text_pattern_ops)`; the scraper's new `recompute_word_frequencies()` refreshes it at the end of each run. `suggest` now: `WHERE language=$1 AND lower(word) LIKE lower($2)||'%'`, `DISTINCT ON (lower(word))` (collapses casing), `ORDER BY frequency DESC`. Live-verified: `univ`/es → universidad(195), universidades(40), universal(24)…; `hist`/es → historia(312)…. Query dropped from ~280ms (live aggregate) to ~9ms (indexed column). Response shape unchanged (`{word, score, type}`; `score` now carries frequency, `type='word'`) so the frontend needed no change. 6 tests in `test_suggest.py`. **Remaining (optional):** German now gets *word* suggestions instead of phrase suggestions — if phrase suggestions are still wanted for German, `UNION` `phrase_blueprint` back in. Original writeup below.

**Files:** `lexy-app/backend/services/search_service.py:suggest`, `lexy-app/backend/routers/search.py` (`/suggest`), frontend `SearchBar.tsx` + `api/suggest.ts`.
**Found:** 2026-05-23 during the Spanish dogfood. Typing `univ` with `language=es` returned `[]` even though `search?q=universidad&language=es` returns 107 hits.
**Problem:** `search_service.suggest` only queries `phrase_blueprint`:
```sql
SELECT blueprint AS word, strict_word_similarity($1, lookup_key) AS score, 'phrase' AS type
FROM phrase_blueprint
WHERE strict_word_similarity($1, lookup_key) > 0.3 AND lookup_key ~* ('\m' || $1 || '\M')
```
Two defects:
  1. **Ignores the `language` argument entirely** — the param is accepted by the router + service but never used in the query.
  2. **Phrase-only, German-only** — `phrase_blueprint` is the German verb-blueprint table (seeded from the German verb dictionary). It has no `language` column and no Spanish/other-language rows, and it never suggests plain words. So autocomplete returns German verb phrases for German queries and **nothing at all** for any other language.
**User-visible impact:** medium. Spanish (and any non-German) learners get an empty autocomplete dropdown as they type. Search still works if they type the full word and submit — so it's degraded UX, not broken. German users only ever see phrase suggestions, never word suggestions.
**Fix:** make `suggest` word-capable and language-aware. Query `word_table` filtered by `language` with a prefix/trigram match (the codebase already uses `similarity()` / `strict_word_similarity` + the `~* '\m…\M'` word-boundary pattern elsewhere), returning `type='word'`. Optionally `UNION` the existing `phrase_blueprint` rows for German so German keeps phrase suggestions too. Thread `language` into the query (it's already plumbed from the frontend's `recLanguage`).
**Risk:** low — additive query change in one function; `/suggest` is already auth-gated (#7) and has no dedicated test, so add one (word suggestions returned for `es`, language filter respected, German still gets phrase suggestions).
**Relation:** independent of #35/#37; pure search-UX fix. Pairs naturally with confirming search/suggest parity across languages.

### 40. ✅ ILP playlist optimizer — RESOLVED 2026-07-27
**What shipped:** the `ilp/` folder held a PuLP-based minimum-set-cover CLI that was never wired to the app. Its *technique* now also lives in `playlist_service.ilp_cover` as an opt-in `algorithm="ilp"` mode on `POST /api/v1/playlists/generate`, alongside the existing greedy cover. Re-implemented for the request path rather than imported — the CLI reads word-list files and carries its own DB config.
**Frontend:** `PlaylistPanel` gained a **Planning** control — *Fast* (greedy, default) vs *Optimal (slower)* (ILP).
**Error shape:** PuLP missing or a non-optimal solve raises `RuntimeError` → 503, surfaced by a typed `PlaylistSolverUnavailableError` in `api/playlists.ts` so the panel can say "switch to Fast" instead of leaking the operator-facing `pip install pulp` string. 503 is handled in the API module, not `_http.ts`, because it has no shared meaning across the API — this is the only endpoint whose backend is optional while the request itself is valid.
**Tests:** +15 backend (11 unit on `ilp_cover`, 4 endpoint), +13 frontend. **Commits:** `7735f8b`, `7e6f376`.
**Still open (unchanged):** the standalone `ilp/` CLI remains unwired. That is deliberate, not debt — see §Directory map in `CLAUDE.md`.

### 41. ✅ Vocabulary lists — upload / resolve / export — RESOLVED 2026-07-27
**What shipped:** `POST|GET /api/v1/word-lists`, `GET|DELETE /word-lists/{id}`, `GET /word-lists/{id}/export` (text/plain), `POST /word-lists/{id}/mark-unknown-learning`, plus a `/lists` page. Closes the gap between a pasted vocabulary list and the ILP playlist optimiser: every resolved entry carries a `word_table.word_id`, which is exactly the `item_ids` shape `playlist_service.generate_playlist` consumes.
**Schema:** the `word_lists` / `word_list_items` tables have existed unused since migration 001 and could not represent the feature — no `language`, no text column, and `item_id NOT NULL`. Migration **035** adds `word_lists.language`, `word_list_items.surface NOT NULL`, makes `item_id` nullable, and replaces `UNIQUE (list_id, item_id, item_type)` with a unique index on `(list_id, lower(surface))`. The old constraint had to go rather than be supplemented: NULLs are distinct in Postgres so it could not dedupe unbound rows, and case-insensitive resolution makes two spellings collide on one `word_id`.
**Five states, not four.** A surface with several catalog matches is stored unbound and reported `ambiguous` rather than resolved to a first match — bulk upload has no interactive picker, and a wrong binding attaches mastery progress to the wrong sense invisibly (the W3 / Hole 2 concern). `mark-unknown-learning` skips ambiguous and unresolved entries and routes everything else through `progression_service`.
**Tests:** +26 backend, +13 frontend. **Commit:** `9977a4e`.
**Known scope limit:** entries are `item_type='word'` and resolve against `word_table` only, so a multi-word surface reports `unresolved` even when `phrase_table` holds it. Wiring phrases needs no schema change.

### 42. ✅ LLM provider seam + OpenAI-compatible provider — RESOLVED 2026-07-27
**Problem:** `CLAUDE.md` §12 said "don't instantiate `AsyncAnthropic` ad-hoc" while three services each did exactly that (`llm_service`, `book_llm_service`, `reading_llm_service`), repeating the model literal alongside it.
**What shipped (step 1, `ac5be57`):** `services/llm_provider.py` is now the only place a client is constructed. All 13 call sites go through `structured(system, messages, schema, max_tokens) -> dict`. The interface is one method wide because every call already had one shape: non-streaming, exactly one forced tool, tool input parsed as structured JSON — `tools` was only ever a JSON-schema enforcement mechanism. `title`/`description` ride as JSON Schema keywords so the same dict maps onto either provider. Byte-identity against the pre-seam tool definitions is test-pinned (14 checks): leaving those keys inside `input_schema` would be accepted by the API with no error while changing the prompt the model sees.
**What shipped (step 2, `41f39c7`):** `OpenAICompatibleProvider` POSTs `{LLM_BASE_URL}/chat/completions` via `httpx` (already a declared dependency — no new one added). One adapter covers Ollama `/v1`, llama.cpp's llama-server, vLLM, LM Studio and TGI, so changing runtime is a `LLM_BASE_URL` change. **No host is assumed** — the intended target is a GPU desktop over Tailscale.
**Selection:** `LLM_PROVIDER` (`anthropic` default | `openai_compatible`). Unset ⇒ unchanged behaviour. Misconfiguration raises at import, since `get_provider()` runs at module import — a backend that cannot reach its model server should fail at boot, not on the first learner's message.
**Tests:** +61 in `test_llm_provider.py`. **Security:** new finding **S18** (LOW, opt-in) in `docs/SECURITY.md`.
**Deliberately deferred — MockProvider.** `MOCK_LLM` fakes are computed from call arguments the provider never receives (`target_word in user_content`, `target_text` vs `user_answer`, the requested `language`). The provider sees those only as prose inside the prompt, so a MockProvider would have to regex them back out — coupling the mock to prompt wording and making it strictly worse. The 13 `if _MOCK:` branches stay at domain level.

### 43. 🟡 Built-in / system **word and phrase** lists — INVESTIGATED 2026-07-27, ready to wire
**Question asked:** can we ship built-in German vocabulary lists from data we already have? **Yes**, and the central file is not the one first assumed. Full per-file measurements in [`data/PROVENANCE.md`](../data/PROVENANCE.md) — read it first.

**`data/final_result.txt` is the source of truth, and it is already load-bearing.** It is read at backend startup (`main.py` → `matcher_service.get_blueprint_map()` → `phrase_service.seed_from_blueprint_map()`) to seed `phrase_table`, and at import by `subtitle-scraper/phrase_finder.py`. Editing it is a production change, not a data tweak.

| File | Role |
|---|---|
| `data/final_result.txt` | **Central.** 5,156-row TSV: 4,075 unique headwords + **950 phrase blueprints, 100% of which are already live in `phrase_table`**. Its headwords are a **superset** of `words_4000_old.txt`'s (0 unique to that file). |
| `data/words_4000_old.txt` | **Enrichment, not a competing entry set.** Same headwords, viewed differently — translations 100%, examples 100%, POS 99%, conjugations 71%. Joins 1:1 on the headword. |
| `data/words_4000.txt` | Redundant — strictly column 0 of `_old`. |
| `data/b1_unparsed.txt` | Supplemental — contributes **815 headwords `final_result.txt` lacks**; `final_result.txt` carries 2,050 it lacks. |
| `data/b1_parsed.txt` | Supplemental enrichment — strict subset of unparsed, adds gender + valency. |

**This is a word *and phrase* feature.** 950 of the entries are phrases, already in `phrase_table`. Building "word lists" here would throw away the half of the data that is already wired.

**Phrase support: ✅ SHIPPED 2026-07-27.** Schema and progression were already ready; the service and frontend have now caught up. No migration was needed.

| Layer | State |
|---|---|
| `word_list_items.item_type` | ✅ exists, polymorphic |
| `progression_service.apply_progression` | ✅ already receives `entry["item_type"]` |
| SRS review | ✅ `review_service.get_due_cards` joins per-type display table |
| `word_list_service._resolve_surfaces` | ✅ **shipped 2026-07-27** — resolves both catalogs |
| `word_list_service.create_list` | ✅ **shipped** — persists the resolved `item_type` |
| `word_list_service._load_entries` | ✅ **shipped** — per-type status lookup |
| Frontend `WordListsPage` | ✅ **shipped** — word/phrase badge on resolved entries |

**No migration needed for phrases** — service + frontend work. Export is unaffected (it emits stored surfaces). `mark-unknown-learning` needs the resolved `item_type` threaded through, which `apply_progression` already accepts.

**Recommended order.** Catalog first: only ~51% of `final_result.txt`'s headwords resolve against `word_table` today, so a list built first reads roughly half "unresolved".

1. ✅ **Seed `word_table`** — **shipped 2026-07-27** as `scripts/backfill_word_catalog.py` + `services/word_seed_service.py`. Entry set from `final_result.txt` column 0; ~2,351 rows to insert. **POS enrichment was deliberately dropped**: `pos` is part of `UNIQUE (word, language, pos)` and every existing row has `pos=''`, so writing a real POS forks rows instead of enriching them, turning resolvable words `ambiguous`. Seeds with `pos=''`; enrichment needs a schema decision. See `docs/MAINTENANCE.md`.
2. **Pre-seed the permanent gloss cache** from `words_4000_old.txt`'s translation column — 4,095 glosses replacing a permanently-cached LLM call per item on the SRS due-card path.
3. ~~**Add phrase support to `word_list_service`**~~ — ✅ **done 2026-07-27** (+11 backend / +4 frontend tests).
4. **System-list schema + built-in lists.** `word_lists.user_id` is `NOT NULL` with `ON DELETE CASCADE`, so no shared-list concept exists; needs a migration and widened ownership filters. Then the built-in German word+phrase lists, and the B1 list from `b1_unparsed.txt` enriched by `b1_parsed.txt`.
5. **Optionally later:** preload example generation from `words_4000_old.txt`'s example column.

**Two labelling cautions, accuracy not licensing:** `onboarding.py`'s `LevelTier.A1/A2/B1` label CEFR "informally" with "approximate" boundaries and are unwired — **the next implementation wires documented data files, it does not guess CEFR from those tiers**. And `word_table.frequency` is app-corpus (scraped-subtitle) frequency, where rank 2000 is the English word `trust` — use `words_4000_old.txt`'s own frequency-descending ordering instead.

**Not started.** No code written; this entry plus `data/PROVENANCE.md` are the record.

### 20. ✅ Dark mode theme system — RESOLVED 2026-05-19
**What landed**

CSS variable tokens declared in `src/index.css` on `:root` (light) with a `[data-theme="dark"]` block that overrides them. `App.tsx` Layout writes `document.documentElement.dataset.theme = darkMode ? 'dark' : 'light'` on every prefs change. `SettingsPanel`'s dark-mode toggle also writes the attribute immediately (no 600ms save round-trip wait).

**Token set (in `index.css`):**
```
--color-bg / -surface / -surface-muted / -surface-sunken / -input-bg
--color-text / -text-strong / -text-muted / -text-subtle
--color-border / -border-accent / -border-subtle / -input-border
--color-primary / -primary-text / -primary-soft / -primary-on-soft
--color-success / -success-bg / -success-border
--color-warning / -warning-bg / -warning-border
--color-danger / -danger-bg / -danger-border
--shadow-card
```

Word-status colors (known/learning/unknown) stay user-configurable via `prefs.*_word_color` — explicitly out of the theme system.

**Component refactor (88 of the 104 audit-counted ternaries removed; 5 remain, all driving the `data-theme` attribute itself).**

| Component | Before → After |
|---|---|
| `NotificationToast.tsx` | dropped `darkMode` prop; all 4 ternaries → `var(--color-*)` |
| `ContentRequestPage.tsx` | dropped `darkMode` prop; all 10 ternaries → vars |
| `BookLibraryPage.tsx` | dropped `darkMode` prop; converted local `th` theme object to var(--...) strings |
| `BookReaderPage.tsx` | dropped `darkMode` prop; kept local `dk` state for the in-reader Dark/Light toggle; wrapped reader with `<div data-theme={dk ? 'dark' : 'light'}>` so the toggle creates a SCOPED override without touching the rest of the app. All inner ternaries → vars. `navBtnStyle` and `topBtnStyle` helpers no longer take `dk`. |
| `SelectionPanel.tsx` | dropped `dk` prop (forwarded from BookReaderPage); all 20 ternaries → vars. `llmBtnStyle` helper no longer takes `dk`. |
| `SelectionReviewPanel.tsx` | dropped `dk` prop; all 6 ternaries → vars |
| `SettingsPanel.tsx` | kept `darkMode` LOCAL STATE (drives the checkbox + auto-save), but converted all 27 styling ternaries to vars. Toggle handler now sets `document.documentElement.dataset.theme` immediately so the UI flips without waiting for the save round-trip. Inline `:hover` background flips on suggestion list dropped (not worth porting; can return as a real `:hover` CSS rule later). |
| `App.tsx` | Layout effect: `document.body.style.background` → `document.documentElement.dataset.theme`. NavLink chip + nlReview styles → vars. Main container background/color → vars. HomePage Free Chat / Guided Practice buttons untouched (already light-mode-only; no dark variants). |

**Files NOT touched** (intentionally — no `darkMode` usage):
all other components (`PlayerView`, `SearchBar`, `RecommendationsPanel`, `RecommendationCards`, `InsightsSection`, `PrepView`, `GuidedChatPage`, `MessageInput`, `GrammarRulePanel`, `ReminderBanner`, `FreeChatPage`, `ChatWindow`, `TargetCard`, `TurnFeedbackChip`, `LoginForm`, `SearchBar`, `ResultCard`, `ReadingStatsPanel`, `WordStatusPicker`, `TranscriptPanel`, `SubtitleDisplay`, `PlayerControls`, `YoutubeEmbed`, `SessionSummaryCard`, `ErrorBoundary`, `FollowedChannelsSection`, `PlaylistPanel`, `SRSReviewPage`, `BookReaderPage` sub-components). These were already light-only (no dark variants existed) — converting them to use theme vars would CHANGE behavior in dark mode (currently they stay light-on-light). Future work: audit each, opt-in to var(--color-*) where dark mode should apply.

**Behaviour preserved:**
  - Settings dark-mode toggle still persists via 600ms debounced save.
  - BookReaderPage's local Dark/Light button still works as a per-reader override.
  - Word-status colors still come from `prefs.*_word_color` and are user-customisable.
  - Status pill semantic colors (pending/done/failed in ContentRequestPage, success/warning/danger badges, mistake/recent/freq insight tags) stay fixed across themes.
  - No new theme settings model — `users.settings.dark_mode` JSONB still the single source.

**Tests added** (`src/test/theme.test.tsx`, 4 tests):
1. NotificationToast dismiss button uses `var(--color-text-muted)`.
2. ContentRequestPage container uses `var(--color-surface)` + `var(--color-text)`.
3. ContentRequestPage input uses `var(--color-input-bg)` + `var(--color-input-border)`.
4. SettingsPanel dark-mode toggle flips `document.documentElement.dataset.theme` between `'light'` and `'dark'` (no 600ms wait).

**Validation:** `npx tsc --noEmit` clean. `npx vitest run` → 90/90. `npm run build` clean (414.80 kB JS / 115.97 kB gz, CSS grew 2 kB → 4 kB from the new variable declarations).

**Future work** (out of this scope):
- ~~Convert components currently not touched (light-only across the board) to use the variable set so dark mode is consistent everywhere.~~ **RESOLVED 2026-05-19 in #20b below.**
- Add proper `:hover` / `:focus-visible` styling via CSS classes — inline styles can't express pseudo-classes, which is why some hover behaviour was dropped in this pass.
- Consider syncing `--color-primary` etc. with `prefs.*_word_color` so users can theme their accent palette too.

### 20b. ✅ Dark-mode coverage for remaining light-only components — RESOLVED 2026-05-19
Follow-up pass to #20a. Converts the 22 components called out as still light-only so dark mode is now visually complete across the entire app.

**Components converted to var(--color-*) tokens** (containers, text, borders, inputs, hover/active states):
  - `App.tsx` Layout — main bg/text (already done in #20a; double-checked).
  - `PlayerView.tsx` — outer panel, view-toggle tab bar.
  - `PlayerControls.tsx` — bottom bar + 5 nav buttons (input-border + surface tokens).
  - `SubtitleDisplay.tsx` — single `color: '#1a3a6c'` text → `var(--color-text-strong)`.
  - `TranscriptPanel.tsx` — sentence rows + active sentence highlight + "Loading transcript…" placeholder.
  - `WordStatusPicker.tsx` — picker chrome bg/border/text. Status pills (`#e53935` Unknown / `#fb8c00` Learning / `#43a047` Known) intentionally kept fixed — semantic colors.
  - `SearchBar.tsx` — input chip area, suggestions dropdown, phrase badge.
  - `LoginForm.tsx` — inputs, ghost/outline/primary buttons, close ✕.
  - `ReminderBanner.tsx` — banner bg/border/text use warning tokens.
  - `FreeChatPage.tsx` — outer panel + header bg now uses var(--color-primary) for a real surface in both modes.
  - `ChatWindow.tsx` — user-bubble + assistant-bubble surfaces.
  - `TargetCard.tsx` — header strip uses var(--color-primary-soft).
  - `MessageInput.tsx` — textarea + send button bg/border/text.
  - `RecommendationsPanel.tsx` — container + controls + ReadingUnitsDueCard.
  - `RecommendationCards.tsx` — Item/Video/Sentence card surfaces, secondary text, PrefButton non-active state.
  - `FollowedChannelsSection.tsx` — section header + empty state.
  - `ReadingStatsPanel.tsx` — container + legend chip colors.
  - `InsightsSection.tsx` — primary item button surface, secondary chip surface. Card border + accent kept fixed (semantic: mistake = pink, freq = blue).
  - `PrepView.tsx` — item header chip, grammar-explanation accordion, examples surface, templates surface, linked-grammar-rule chips.
  - `GuidedChatPage.tsx` — outer panel, header (kept semantic warning/success colors for the target-progress state), hint panel, End Session secondary button.
  - `SessionSummaryCard.tsx` — corrective-note highlight strip, feedback row label/text. `TargetBadge` and `QualityPill` semantic palettes kept fixed.
  - `ErrorBoundary.tsx` — fallback panel bg/border/text + Reload button.
  - `PlaylistPanel.tsx` — outer container, BuildView input style, suggestion dropdown, target chips, Generate button, ResultView coverage bar + PlaylistVideoCard surface + Watch button.
  - `SRSReviewPage.tsx` — outer container, language `<select>`, Reload button, progress bar, feedback panel (success/warning surface + Continue button uses `var(--color-primary)`), review card surface, passive/active buttons (use semantic danger/success/primary tokens with surface fallbacks for the disabled state), Skip link.

**Intentionally left unchanged** (semantic colors that must stay recognisable across themes):
  - **Status pills** in `ContentRequestPage.STATUS_STYLES`, `WordStatusPicker` STATUSES, `SessionSummaryCard.TargetBadge` / `QualityPill`, `SelectionReviewPanel` Mastered/Due badges, `RecommendationCards.REASON_STYLES` and reason-tag variants, `InsightsSection` mistake-vs-freq border + accent. These all rely on color to convey meaning quickly (red = bad, green = good, blue = info, orange = warning).
  - **Word-status colors** (known/learning/unknown) — user-configurable via `prefs.*_word_color`. Out of theme system by design.
  - **PrefButton activeColor** (Follow/Like/Dislike on video cards) — passed in by parent at the call site to encode the action's meaning (`#1a237e` follow / `#2e7d32` like / `#c62828` dislike).
  - **YouTube thumbnail backdrops** — `background: '#000'` kept literal so the lazy-loading poster fades in on the same surface in both themes.
  - **LLM-result tints** in `SelectionPanel.llmBtnStyle` — passed-in accent (`#1565c0` translate / `#2e7d32` explain) is the function signature; the wrapping bg now uses `var(--color-surface-muted)` for the disabled state.
  - **SubtitleDisplay highlight `<mark>` background `#fff176`** — search-term highlight; light yellow works on both light and dark text and is recognisable as a highlight.
  - **`ResultCard.tsx`** — dead code (no importers, confirmed in #27g audit). Not converted.

**Tests added** (`src/test/theme.test.tsx`, 4 new tests on top of the existing 4 from #20a):
1. `PlayerControls` button uses `var(--color-surface)` + `var(--color-input-border)`.
2. `LoginForm` primary submit uses `var(--color-primary)` + `var(--color-primary-text)`.
3. `MessageInput` textarea uses `var(--color-input-bg)` + `var(--color-text)`.
4. `ReminderBanner` background uses `var(--color-warning-bg)`.

Existing `TranscriptPanel.test.tsx` updated: `borderLeft` assertion now matches the `var(--color-primary)` literal (jsdom can't compute CSS vars; `toHaveStyle` shorthand resolution fails, so we use `style.borderLeft` + `toContain` directly).

**Validation:** `npx tsc --noEmit` clean. `npx vitest run` → 94/94 (was 90; +4 new). `npm run build` clean (421 kB JS / 116 kB gz, +6 kB from the larger inline-style strings — `var(--color-*)` is longer than `'#ffffff'`).

**Visual caveats:**
- The `:hover` flip on `SearchBar`'s suggestion list and `SettingsPanel`'s TagInput dropdown still doesn't dark-mode-correctly — both used inline `onMouseEnter`/`onMouseLeave` handlers that referenced `darkMode`. Those were already dropped in #20a; not regressed here. Re-introducing via real CSS `:hover` rules is a future task.
- The `ChatWindow` user bubble was previously `#1a237e` (bright purple-blue) on light mode; in dark mode it now reads from `var(--color-primary)` which resolves to `#7986cb` (a lighter purple). This is intentional — pure `#1a237e` is unreadable as a bubble background on a dark surface. Side effect: the bubble is slightly lighter on a light background than before. If this looks off, change `--color-primary` in the light declaration block.

### 21. 🟡 No memoization / re-render hotspots
**Files:** `usePlayerSentences.ts`, `RecommendationCards.tsx`, `BookReaderPage.tsx`, `SearchBar.tsx`
**Problem:** Sentence parsing, recommendation rendering, and book-page rendering happen on every keystroke / state change.
**Fix:** `useMemo` parsed sentences, `React.memo` card components keyed by `item_id`, debounce search input.

### 22. ✅ Frontend error boundary — RESOLVED 2026-05-19
New `components/ErrorBoundary.tsx`. Wraps `<Outlet />` in `App.tsx` (navbar + Layout chrome stay outside the boundary so the user can navigate away after a route crash). Fallback UI: "Something went wrong." + Reload button (`window.location.reload`). Component-stack logged to `console.error` for now. 3 Vitest cases: passthrough, fallback on throw, Reload click invokes reload.

### 22b. (was original #22 problem statement, kept for context)
**File:** `lexy-app/frontend/src/App.tsx`
**Problem:** A render error in any deep component white-screens the whole app.
**Fix:** Wrap `<Outlet />` (or each route element) in an `ErrorBoundary` that shows a fallback + reload button + sends to backend logging endpoint.

### 23. ✅ settings_service exception specificity — RESOLVED 2026-05-19
`services/settings_service.py:_coerce_settings` (fallback `dict(value)` for unknown DB return types) now catches only `(TypeError, ValueError)` — the actual exceptions `dict()` raises for non-iterable / malformed inputs. Anything else (e.g. real DB or system-level error) bubbles up so it's diagnosable.

### 23b. (was original problem statement)
**File:** `lexy-app/backend/services/settings_service.py:49–54`
**Problem:** Bare `except Exception` after `JSONDecodeError`. Hides DB errors, permission errors, type errors.
**Fix:** Catch only `json.JSONDecodeError` and `asyncpg.PostgresError`. Let everything else propagate.

### 24. ✅ LLM cache thundering-herd guard — RESOLVED 2026-05-20
New `llm_cache_service.get_or_compute(pool, cache_key, prompt_key, model, compute, *, ttl_seconds=None)` helper. In-process `asyncio.Lock` per `cache_key` (kept in a module-level dict guarded by an outer Lock so two coroutines see the same Lock object). Flow:

1. Fast path — `get_cached` without any lock; cache hits skip locking entirely.
2. On miss: `await _get_lock(cache_key)` then `async with lock:`.
3. **Double-check** inside the lock — another coroutine may have filled the entry while we waited.
4. Still missing → run `compute()`, then `set_cached(...)`, then release.

Lock cleanup: deliberately NOT done. The set of distinct cache_keys is bounded by the `llm_cache` table itself, and `asyncio.Lock` objects are ~100 bytes — trade memory for simplicity. `reset_for_tests()` exposed for the test suite.

Behaviour guaranteed by 6 new tests in `test_llm_cache.py` (driven with `asyncio.Event` to force deterministic concurrency, no real LLM calls):
  - cache hit ⇒ `compute()` never invoked;
  - 10 concurrent same-key callers ⇒ exactly 1 `compute()` invocation, all 10 receive the same dict;
  - 2 concurrent different-key callers run in parallel (no cross-key blocking — proven by mutual-event-wait that would deadlock if locking serialised them);
  - `compute()` raises ⇒ lock released, no entry written, retry path works;
  - second waiter's `compute()` is short-circuited by the double-check (proves the bypass, not just the count).

**Call-site migration — RESOLVED 2026-05-20 in #24-followup.** All 9 cached LLM paths now route through `get_or_compute`:

| Path | File:Function | Prompt key |
|---|---|---|
| Guided chat opener | `llm_service:guided_open` | `guided_open` |
| Guided chat hints | `llm_service:guided_hints` | `guided_hints` |
| Prep item info | `llm_service:prep_item_info` | `prep_item_info` |
| Prep examples | `llm_service:prep_generate_examples` | `prep_examples` |
| Grammar rule explanation | `llm_service:grammar_rule_explanation` | `grammar_rule_explanation` |
| SRS gloss (passive/active prompt text) | `llm_service:translate_item_gloss` | `item_gloss` |
| Reading translate | `reading_llm_service:translate_sentence` | `reading_translate` |
| Reading explain | `reading_llm_service:explain_in_context` | `reading_explain` |
| Book OCR repair | `book_llm_service:repair_block` | `book_ocr_repair` |

Intentionally untouched (per spec — high-cardinality user input or per-message context that should not be cached):
  - `llm_service:guided_evaluate` (uncached)
  - `llm_service:guided_summarize` (uncached — session-specific)
  - `llm_service:evaluate_and_reply` (free-chat per-message)
  - `llm_service:evaluate_production` (explicit "Not cached — input is user-supplied and high-cardinality" — regression-guarded by `test_evaluate_production_remains_uncached`)
  - `llm_service:get_examples_if_cached` / `get_grammar_explanation_if_cached` (read-only cache peek used by routers; no LLM call to serialise)

Regression-guarded by 6 new tests in `tests/test_llm_cache_migration.py`: concurrent same-key callers prove exactly one provider invocation for `translate_item_gloss`, `reading_translate`, `reading_explain`, `book_repair`; `evaluate_production` proven still-uncached; cache-hit second call skips provider.

**Known limitation.** In-process per-process locks. Multi-worker deployments (gunicorn `--workers N`, multi-pod K8s) won't coordinate across processes — Worker A and Worker B can each see a miss and each call the provider. Same scope as `services/rate_limiter.py`; same eventual fix (Redis or `pg_advisory_xact_lock`). Documented in the module docstring.

### 25. 🚫 Word-status data duplicated across services — DROPPED 2026-05-23
**Files:** several services do their own `SELECT … FROM user_word_knowledge WHERE …`
**Problem:** No single `word_service.get_knowledge(user_id, item_id, item_type)`. Refactors are painful.
**Fix (not pursued):** Extract a single accessor and route all reads through it.
**Decision (2026-05-23):** 🚫 dropped. Audit found the call sites read different column subsets for different purposes, so a single accessor would either over-fetch or grow a pile of flags — no clean shared shape, zero would-be adopters that benefit. Reopen only if a concrete refactor is actually blocked by the duplication.

---

## P4 — Polish. Pleasant to do, not load-bearing.

### 26. 🟢 Accessibility
- Add `aria-label`, `role`, keyboard navigation on WordStatusPicker modal, SRSReviewPage buttons, SelectionPanel
- Alt text on book thumbnails + YouTube thumbnails
- Focus management when modals open/close

### 27a. ✅ Mobile responsive infrastructure — RESOLVED 2026-05-19
Foundation pieces only — no component restyling yet (#27b–f cover that).
  - `index.html` viewport meta updated to `width=device-width, initial-scale=1, viewport-fit=cover` (the `viewport-fit=cover` is what lets a future Capacitor iOS wrap paint behind the notch).
  - `src/index.css` gains `--bp-sm: 480px`, `--bp-md: 768px`, `--safe-top`, `--safe-bottom` design-token variables, plus `html { -webkit-text-size-adjust: 100% }` and `body { font-size: 16px }` to block iOS Safari's input-focus zoom.
  - New `src/hooks/useViewport.ts` returns `{ isMobile }` driven by `window.matchMedia('(max-width: 768px)')`. SSR/test-safe (defaults to non-mobile when `matchMedia` is unavailable). Subscribes via `addEventListener('change', ...)` with legacy `addListener` fallback. 5 Vitest tests.
  - `App.tsx` Layout container is the proof-of-concept consumer: tighter side gutters on mobile (12px/8px vs 24px/16px) and `paddingTop: calc(... + var(--safe-top))` so the notch case is wired in.
  - **Caveat for #27b onward:** CSS variables can't be used inside `@media` query conditions. The breakpoint literal `768px` must be duplicated at every `@media (max-width: 768px)` site (and similarly `480px` for `--bp-sm`). The variables exist for JS consumption + design-token reference only.

### 27b. ✅ PlayerView / YoutubeEmbed / SubtitleDisplay / PlayerControls — RESOLVED 2026-05-19
The main video-learning surface now renders correctly on phone:
  - **YoutubeEmbed**: switched from the `paddingTop: 56.25%` ratio hack to `aspectRatio: '16 / 9'` on a `width: 100%` container. Same responsive behaviour, cleaner CSS.
  - **SubtitleDisplay**: replaced `height: 200px` (clipped on mobile / large fonts) with `minHeight: 200px`. Font size went from a fixed `38px` to `clamp(20px, 5vw, 38px)` — fluid scaling, no JS branch needed. Padding also went fluid via `clamp()`. iOS Safari 14.5+ supports both.
  - **PlayerControls**: added `minWidth/minHeight: 44px` belt-and-braces alongside the existing `width/height: 44px` (so a future refactor can't drop the touch target). Outer container gained `flexWrap: 'wrap'` + `gap: 8px` so the sentence counter falls below the buttons on narrow phones instead of overflowing.
  - **PlayerView**: container was already vertical-stack + `width: 100% / maxWidth: 100%` — no change needed.
  - **No prop-drilling**: each component handles its own responsive concern via CSS (`clamp`, `flexWrap`, `aspectRatio`). `useViewport` not used in this stage because CSS sufficed.

### 27c. ✅ TranscriptPanel + WordStatusPicker — RESOLVED 2026-05-19
Main HomePage / video-learning surface is now fully mobile-friendly (PlayerView half = #27b, transcript half = #27c).
  - **TranscriptPanel**: sentence rows now `minHeight: 44px` + `padding: 10px clamp(10px, 3vw, 16px)` (finger-tappable on phone, no horizontal overflow). Font: `clamp(14px, 3.5vw, 16px)`. Added `overflowWrap: anywhere` + `wordBreak: break-word` so long German compound words wrap instead of overflowing.
  - **WordStatusPicker**: all 3 status buttons (Unknown / Learning / Known) gained `minWidth/minHeight: 44px`. Status button row gained `flexWrap: 'wrap'` so all three stay visible on 320px phones instead of overflowing. Close `×` button widened to 44×44 with flex centering. Side padding became `clamp(10px, 3vw, 16px)`.
  - **No prop drilling**, no `useViewport`. Pure CSS (`clamp` + `flexWrap` + `minHeight`).

### 27e. ✅ SRSReviewPage + GuidedChatPage mobile — RESOLVED 2026-05-19
Daily review + chat surfaces are now mobile-safe. Critical iOS guards in place.
  - **SRS active-production input** (`<input>` from #0a-2): `fontSize: 16px` (iOS Safari focus-zoom blocker — non-negotiable), `minHeight: 44px`, `box-sizing: border-box`.
  - **All SRS review buttons** (passive grade pair, active I-don't-know + Submit, Continue, Reload, Language select): `minHeight: 44px`, font bumped from 13px to 14px for legibility, `touchAction: 'manipulation'`. Button rows gain `flexWrap: 'wrap'` with `flex: 1 1 140px` so they fit one row when there's space and stack on truly narrow phones.
  - **SRS card containers**: padding fluid via `clamp(20px, 5vw, 28px) clamp(16px, 5vw, 24px)`. Prompt text uses `clamp(24px, 7vw, 32px)`. Answer / feedback containers gain `overflowWrap: anywhere` + `wordBreak: break-word` so long German wraps cleanly.
  - **SRS outer container**: `padding: clamp(12px, 4vw, 20px)`.
  - **SRS close `×`**: 44×44 flex-centered button (was bare unstyled text).
  - **MessageInput textarea** (chat input): `fontSize: 16px` (iOS guard), `minWidth: 0` so it doesn't push the Send button out on narrow screens.
  - **MessageInput Send button**: `minHeight: 44px`, `minWidth: 64px`, `touchAction: 'manipulation'`.
  - **GuidedChat header close**: 44×44 flex-centered.
  - **End Session / Need-a-hint buttons** in GuidedChat: `minHeight: 36px` (secondary actions, kept smaller than 44 to not visually dominate the target/hint row but still tappable). Font bumped from 11px to 13px.
  - **HintPanel inline link button**: `minHeight: 32px`, font 13px.
  - **Behaviour unchanged**: passive reveal/self-grade, active produce + I-don't-know, hint level state machine, summary card, language switch.

### 27d. ✅ BookReaderPage mobile — RESOLVED 2026-05-19
PDF/book reading surface now usable on phone.
  - **Content area** switches from horizontal flex (`1 1 55%` + `0 0 42%`) to vertical column on mobile via `useViewport()`. Right-side panels (Scan image, SelectionPanel, SelectionReviewPanel) and the Edit-mode annotation+image split all stack below the reader instead of fighting for ≤375px of width. Each stacked panel gets `maxHeight: 50vh` + `overflowY: auto` so the reader stays the primary content.
  - **Page input** (page-number entry) bumped from `fontSize: 13px` to `fontSize: 16px` — iOS Safari focus-zoom blocker. Also gained `minHeight: 44px`.
  - **Page nav arrows** (◀ / ▶ via `navBtnStyle`): now 44×44 with `touchAction: 'manipulation'` and font 14→16px.
  - **Top-bar chrome buttons** (`topBtnStyle` — Saved, Edit, Dark/Light, Scan): bumped to `minHeight: 36px`, padding `5px 12px` → `7px 12px`, font 12→13px. Top bar still flex-wraps as before.
  - **Back button**: `minHeight: 44px`.
  - **Mode toggle** (Page / Sentence): `minHeight: 36px`, padding bumped, font 12→13px.
  - **Reader padding**: fixed `24px` → `clamp(12px, 4vw, 24px)` so 320px viewports keep more content.
  - **Top-bar side padding**: fixed `16px` → `clamp(10px, 3vw, 16px)`.
  - **Behaviour preserved**: token selection / Page-vs-Sentence mode / scan image overlay / Edit annotation split / SelectionPanel / SelectionReviewPanel / page navigation — all unchanged.

### 27f. ✅ ContentRequestPage + SettingsPanel mobile — RESOLVED 2026-05-19
Last non-reader mobile surfaces are now phone-safe.

**ContentRequestPage:**
  - Container padding fixed `24px` → `clamp(16px, 4vw, 24px)`. Container gains `overflowWrap: anywhere` so long channel IDs / errors wrap inside the 640px max-width.
  - Content-ID input: `fontSize 14px → 16px` (iOS focus-zoom guard), `minHeight: 44px`, padding bumped to `10px 12px`.
  - Channel/Video toggle row: `flexWrap: wrap` added. Each button `minHeight: 36px`, padding `7px 20px → 8px 20px`, font 13→14px, `touchAction: manipulation`.
  - Submit button: `minHeight: 44px`, font 14px (unchanged), padding 9→10px vertical.
  - Close `×`: 44×44 flex-centered, `aria-label="Close"`.
  - Request-list row content column: `flex: '1 1 200px'` so type + ID + error wrap to a new line on narrow phones instead of overflowing the status pill.

**SettingsPanel:**
  - Container padding fixed `20px 24px` → `clamp(14px, 4vw, 24px)`.
  - Shared `input` style helper (used by both reps number inputs): `fontSize 14px → 16px` (iOS guard), `minHeight: 44px`, padding bumped.
  - TagInput text field: `fontSize 13px → 16px` (iOS guard).
  - TagInput preset chip buttons: `minHeight: 32px`, padding `3px 10px → 6px 12px`, font 12→13px.
  - Color picker inputs: height `36px → 44px` (44×44).
  - Close `×`: 44×44 flex-centered.

**Behaviour preserved**: submit flow, debounced auto-save, tag add/remove, dark mode toggle, language/genre/channel preferences, color updates.

### 27g. ✅ Global mobile touch-target + input audit — RESOLVED 2026-05-19
Closing pass over every interactive surface that #27a–f didn't touch.

**Inputs/selects/textareas bumped to fontSize: 16px (iOS focus-zoom guard):**
  - `LoginForm.tsx`: email + password inputs (13 → 16px, +44px minHeight)
  - `PlaylistPanel.tsx`: BuildView shared `inputStyle` (13 → 16px, +44px minHeight; covers language select, word search, max-videos number input)
  - `BookLibraryPage.tsx`: title input + language select (13 → 16px, +44px minHeight)
  - `RecommendationsPanel.tsx`: language select (13 → 16px, +44px minHeight)
  - `SelectionPanel.tsx`: note textarea (13 → 16px)

**Primary CTAs bumped to minHeight: 44px:**
  - `App.tsx` HomePage: Free Chat / Guided Practice buttons
  - `LoginForm.tsx`: Sign-in / Create submit
  - `PlaylistPanel.tsx`: Generate playlist, Add word
  - `BookLibraryPage.tsx`: Upload
  - `PrepView.tsx`: Start Guided Practice
  - `SessionSummaryCard.tsx`: See next recommended item

**Close × buttons bumped to 44×44:**
  - `LoginForm.tsx` cancel
  - `PlaylistPanel.tsx`
  - `BookLibraryPage.tsx`
  - `RecommendationsPanel.tsx`
  - `ReminderBanner.tsx` dismiss
  - `NotificationToast.tsx` dismiss
  - `FreeChatPage.tsx`

**Sidebar close × kept at 36×36 (documented):**
  - `SelectionPanel.tsx` / `SelectionReviewPanel.tsx` — fixed-width 340px sidebars, 44 would crowd header pill + count badge.
  - `GrammarRulePanel.tsx` — inline expansion panel.

**Secondary actions bumped to minHeight: 36px:**
  - `App.tsx` Layout: NavLink chips (top-nav row across every page)
  - `LoginForm.tsx` Sign-in toggle button (outlineBtn)
  - `ReminderBanner.tsx`: "For You →"
  - `RecommendationsPanel.tsx`: Refresh button, Reading Units "Open Books" link
  - `RecommendationCards.tsx`: ActionButton (Watch/Practice/Search/Dismiss/Mark Learning)
  - `BookLibraryPage.tsx`: Read / Delete / Confirm / Cancel
  - `PlaylistPanel.tsx`: Back, Add recommended words, Watch (in video card)
  - `PrepView.tsx`: Back, Generate examples, linked grammar rule chips
  - `SessionSummaryCard.tsx`: Practice again, Back to home
  - `SelectionPanel.tsx`: LLM Translate/Explain (40px in sidebar), Save/Clear (40px in sidebar)
  - `SelectionReviewPanel.tsx`: review chip buttons (Got it / Not quite / ★)
  - `GrammarRulePanel.tsx`: Learn more / Add to study
  - `InsightsSection.tsx`: secondary chip buttons (≥32px)

**Container padding made fluid where wide-margin panels were on small screens:**
  - `PlaylistPanel.tsx`: `16px 20px` → `clamp(14px, 4vw, 20px)`
  - `RecommendationsPanel.tsx`: `16px 20px` → `clamp(14px, 4vw, 20px)`

**Components intentionally left unchanged:**
  - `ChatWindow.tsx`, `TargetCard.tsx`, `TurnFeedbackChip.tsx`, `ReadingStatsPanel.tsx`, `icons.tsx`: display-only, no interactive surfaces.
  - `FollowedChannelsSection.tsx`: relays into `VideoRecommendationCard` (already in `RecommendationCards.tsx`).
  - `ResultCard.tsx`: dead code (no importers — verified via grep). Flagged for removal in a future cleanup pass.
  - `RecommendationCards.tsx` `PrefButton` (Follow/Like/Dislike chips on video cards): kept at ~22px because they render three-up across an already dense 220-280px card. They're discoverability hints; primary watch CTA covers the load-bearing tap.
  - `BookLibraryPage.tsx` Sort chips (`Newest / A→Z / …`): kept at ~22px to avoid pushing the sort row to multiple lines on mobile. They're tertiary controls; users can still tap them, just precisely.

**Other guards added:**
  - All bumped controls have `touchAction: 'manipulation'` to skip iOS Safari's 300ms double-tap-zoom delay.
  - `LoginForm.tsx`: signed-in header row gained `flexWrap: 'wrap'` so long email addresses don't push "Sign out" off-screen.
  - `BookLibraryPage.tsx` Actions column: `flexWrap: 'wrap'` so Confirm/Cancel can drop below the row label on narrow phones.
  - `SessionSummaryCard.tsx` secondary CTA row: `flexWrap: 'wrap'`.

### 27h. ✅ PWA shell (manifest + service worker + offline page) — RESOLVED 2026-05-19
First half of iOS "Add to Home Screen" / Capacitor prep (Path A in #34).

**Files added:**
  - `frontend/public/manifest.webmanifest` — `name`, `short_name: "Lexy"`, `start_url: /`, `scope: /`, `display: standalone`, `orientation: portrait`, `theme_color: #1a237e`, `background_color: #ffffff`. Icon list references only `/favicon.svg` (the one icon asset that exists). 192×192 / 512×512 / apple-touch-icon are intentionally not referenced — see TODO below.
  - `frontend/public/sw.js` — conservative shell SW. Strategy: precache shell (`/`, `/index.html`, `/manifest.webmanifest`, `/favicon.svg`, `/offline.html`) on install; navigation = network-first → cached shell → offline.html; `/assets/*` = cache-first (Vite filenames are content-hashed so collisions are impossible); `/api/*`, cross-origin, and non-GET = bypassed (lets SSE notifications work, lets POSTs hit network). `skipWaiting` + `clients.claim` so updates roll out fast. `CACHE_VERSION = 'v1'` constant for future invalidation.
  - `frontend/public/offline.html` — minimal standalone page, theme-coloured, 44×44 Retry button.

**Files modified:**
  - `frontend/index.html` — added `<link rel="manifest">`, `<meta name="theme-color">`, `mobile-web-app-capable` / `apple-mobile-web-app-capable` / `apple-mobile-web-app-title="Lexy"` / `apple-mobile-web-app-status-bar-style="default"`. Title bumped from `frontend` → `Lexy — Language Learning`. Inline comment documents the missing apple-touch-icon. `viewport-fit=cover` from #27a preserved.
  - `frontend/src/main.tsx` — production-only registration. Guarded by `import.meta.env.PROD && 'serviceWorker' in navigator`. Registers on `window.load` to avoid blocking first paint. Failure logged via `console.warn`, never throws.

**Dev vs prod:** The SW only registers on production bundles, so `npm run dev` is unaffected — Vite's HMR keeps working without a SW intercepting navigations. To test the SW locally: `npm run build && npm run preview`.

**Icons:** ✅ **RESOLVED 2026-05-19** in #27i below. 192×192 + 512×512 PWA icons and 180×180 apple-touch-icon now present.

**Constraints honoured:**
  - No backend changes.
  - No dark-mode refactor.
  - No Capacitor work.
  - `/api/*` and SSE never cached.
  - No missing icon files referenced.

### 27i. ✅ PNG icon set for PWA — RESOLVED 2026-05-19
Closes the PWA stage: Lighthouse PWA audit's "no maskable/png icon" warning is gone, iOS home-screen icon is real (no longer a page screenshot).

**Source asset:** `frontend/public/favicon.svg` — the existing 48×46 stylized purple/blue lightning bolt. Aspect 48:46 ≈ 1.043, close to square; the SVG content already centers the glyph in its viewBox.

**Pipeline (single bash sequence, no committed scripts):**
1. `rsvg-convert -w 820 public/favicon.svg -o /tmp/fav-large.png` → renders the SVG at 820×786 PNG preserving aspect.
2. `sips -p 1024 1024 --padColor FFFFFF /tmp/fav-large.png` → pads to 1024×1024 with solid white background and ~10% safe-zone padding around the glyph (102px horizontal, 119px vertical of white margin around the source render).
3. `sips -z 512 512 / 192 192 / 180 180` → three copies resampled to the target sizes.

**Files added (all binaries, in `frontend/public/`):**
  - `icons/icon-192.png` — 192×192, 16 KB
  - `icons/icon-512.png` — 512×512, 93 KB
  - `apple-touch-icon.png` — 180×180, 14 KB

**Files modified:**
  - `frontend/public/manifest.webmanifest` — `icons[]` now lists the 192 + 512 PNGs first (both `purpose: "any"`) with the SVG kept as a fallback for browsers that prefer vector icons.
  - `frontend/index.html` — added `<link rel="apple-touch-icon" href="/apple-touch-icon.png" />` and removed the inline comment that warned of the missing asset.

**Maskable note:** The icons are NOT marked `purpose: "any maskable"`. The safe zone is ~10% per side which is enough for iOS rounded-corner masking but is below the Android adaptive-icon minimum (18% per side / inner 80% diameter circle). The glyph is also a non-symmetric blob, so adaptive cropping would mangle it. To add maskable support later: regenerate with `sips -p 1280 1280` (≈20% padding on a 1024 source) and add a separate `maskable` icon entry.

**Visual caveats:**
  - Solid white background — looks crisp on light home-screen wallpapers, neutral on dark ones. Not branded.
  - On iOS dark mode the icon still uses the white background (iOS doesn't honour `prefers-color-scheme` for home-screen icons).
  - Glyph is the same purple/blue gradient as the in-app favicon — visually consistent with the loaded app.

### 27. 🟢 Mobile responsiveness (remaining stages)
**Memory note:** explicitly deferred until end-to-end loop works. Don't start until #1–#10 are done.
- `@media` queries for PlayerView, BookReaderPage, modals
- Tighten the 900px max-width container
- Touch-target audit (44px minimum)

### 28. ✅ Clean `index.css` and `App.css` — RESOLVED 2026-05-23 (done in W8)
**Files:** `lexy-app/frontend/src/index.css`, ~~`App.css`~~
**Problem:** Mostly commented-out Panda-CSS skeleton and Vite template leftovers.
**Resolution:** `App.css` deleted in W8. `index.css` is now 163 lines of the live theme system — `:root` / `[data-theme]` CSS-variable blocks (71 lines reference `--color-*` / `data-theme` / `:root`), zero Panda references, no commented-out skeleton. Only what's used remains.

### 29. ✅ Backend end-to-end progression loop test — RESOLVED 2026-05-19
New file `tests/test_e2e_learning_loop.py` with two tests:
  - `test_e2e_learning_loop_golden_path` walks the full HTTP loop: register → mark Learning → /srs/due → passive correct review → active produce (exact-match fast path) → repeat to mastery → confirm /srs/due filters known → manual demote known → learning → manual demote learning → unknown. Each step asserts both the API response and the persisted `user_word_knowledge` + `srs_cards` state, locking #0a-2, #0b, Hole 9, and Hole 26 against regression. ~7s on local DB.
  - `test_e2e_demote_unknown_does_not_fabricate_missing_active_card` mirrors the Hole 26 confidence-click path: known via manual click only (no active card created), then demote to unknown — asserts no active SRS card is fabricated.

LLM is mocked via `llm_service._MOCK = True`. The active production exact-match fast path means the loop never makes an LLM call for evaluation either.

### 30. ✅ Documentation / ERD — RESOLVED 2026-05-23
**Files:** `docs/SCHEMA.md`
**Problem:** 25+ migrations, no schema diagram. New contributors guess relationships.
**Resolution:** `docs/SCHEMA.md` written (184 lines) — documents the tables, the polymorphic `(item_id, item_type)` key, and links tables to their owning services. Satisfies the "drop a `docs/SCHEMA.md` linking each table to its owning service" plan. A rendered ERD image was not generated (text doc judged sufficient); add `eralchemy` output later if a visual is wanted.

### 31. 🟢 Two-pass extractor thresholds
**File:** `pdf_text_extraction/config.py:220–311`
**Problem:** Magic numbers (245.0 brightness, 15.0 chars/pt) untested on diverse PDF sources.
**Fix:** Add a small validation harness against a labelled set of "real text", "ghost text", "scanned text" pages.

### 32. ✅ `getStoredEmail` contract — RESOLVED 2026-05-19
Grep confirmed only one caller (`LoginForm.tsx:30`) and it already null-handles via `?? 'Signed in'`. Tightened the function's docstring to make the contract explicit ("Callers MUST handle null — do not assert non-null"). No behaviour change needed.

### 32b. (was original #32 problem statement, kept for context)
**File:** `lexy-app/frontend/src/auth.ts`
**Problem:** `getStoredEmail()` can return `null` but several callers don't check.
**Fix:** Tighten the return type, add a guard.

### 33. ✅ Legacy test directory deleted — RESOLVED 2026-05-20 (W8)
`tests/legacy/` (9 ad-hoc phrase_finder debug scripts + `__init__.py` + `__pycache__/`) was deleted, and the `collect_ignore_glob = ["tests/legacy/*"]` line was removed from the root `conftest.py` in the same commit. No remaining references.

---

## Major future direction

### 34. 📱 iOS mobile support — HIGH PRIORITY FUTURE WORK
Owner-stated goal: the user wants to use this app on their phone. This is **load-bearing** for the project's long-term direction — language learning happens in pockets of time, and the desktop-only constraint blocks the main use case.

Three viable paths, ordered by effort:

**Path A — PWA (smallest lift).** Add `manifest.json`, service worker, install-to-home-screen support. Users add to home screen → app behaves nearly-native. Limitations on iOS Safari: no push notifications (only since iOS 16.4 with caveats), limited background processing, no App Store presence. Good first step regardless of which other path comes later.

**Path B — Capacitor wrapper (recommended).** Wrap the existing React/Vite frontend in a Capacitor native shell, deploy to App Store. Reuses 95%+ of the existing frontend code. Native APIs (camera, push, deep links, biometrics) accessible via Capacitor plugins. Build pipeline: `npm run build` → `npx cap sync ios` → open in Xcode → archive → submit. The right balance of effort vs platform integration.

**Path C — React Native rewrite.** Best performance, fully native UI primitives, but means rewriting every component (no JSX-DOM, no CSS, different navigation). Months of work for incremental UX gain over Capacitor.

**Path D — Native Swift.** Full rewrite. Cleanest iOS UX, biggest implementation cost. Unless we hit a Capacitor ceiling, not worth it.

**Prerequisites before any of A–D:**
- **#27 mobile responsiveness** — the current layout is desktop-only (`maxWidth: '900px'`, fixed font sizes, no media queries). This MUST land first. Even Capacitor / PWA would deploy a broken UI on phone-sized viewports today.
- **#20 dark mode theme system** — the inline `dk ? '#xxx' : '#yyy'` pattern across 35+ components becomes painful when also factoring in iOS dark-mode (which can change at runtime). A CSS-variable-based theme unblocks both.
- **Auth + SSE on mobile** — the SSE notification stream (`/notifications/stream`) holds a long-lived connection. On iOS, that gets killed when the app backgrounds. Either move to APNs push (requires backend + Apple developer account) or accept that notifications only deliver when the app is open. Capacitor's `@capacitor/push-notifications` is the bridge.
- **Backend deployable** — done as of 2026-05-19 (#11 CORS + deploy-readiness bundle). The backend can now run anywhere CORS_ORIGINS allows.

**Concrete recommended sequence when this work begins:**
1. **Mobile responsive web** (#27): media queries, fluid typography, touch targets ≥44px, viewport meta. Makes the existing site usable on phone Safari today.
2. **Theme system** (#20): CSS variables + `[data-theme="dark"]`. Removes the inline-color pattern.
3. **PWA shell** (Path A): manifest, service worker, install prompt. Users can install to home screen immediately.
4. **Capacitor wrap** (Path B): when the PWA experience proves the UX is right, wrap and submit to App Store.
5. **APNs push notifications**: replace the SSE-only fallback for in-app alerts (#4b's LISTEN/NOTIFY also matters here).

**Non-obvious things to plan for early:**
- App Store review prep: privacy policy, account-deletion flow, age rating.
- iOS dark-mode honors system setting at runtime — the `dark_mode` preference would need to flip to "system / light / dark" tristate.
- Token refresh: currently JWT lasts 7 days (`ACCESS_TOKEN_EXPIRE_MINUTES = 60*24*7` in `core/security.py`). On mobile, expiry mid-session needs to be handled gracefully — `_http.ts` already dispatches `auth:expired` (TODO #14), but the LoginForm flow may need rethinking for a native shell.
- Offline mode for review: SRS due cards could be cached client-side so the user can review on the subway. Major feature, scope it separately.

**Status:** Not started. Captured here so it doesn't get lost in conversation context.

---

## Historical execution order (mostly past-tense)

Kept as a trace of the dependency chain we followed. The live priority view is `docs/ROADMAP.md`.

1. **#0a, #0b** — the SRS review UI was a self-grading checkbox. Resolved 2026-05-19: backend now ships `prompt_text` + `answer_text`, active review is a real production test, `status_marked_learning` creates both passive and active cards.
2. **#1, #5d** — dead `srs_service.py` endpoints removed; progression rule table cleaned. Resolved 2026-05-18/19.
3. **#2, #5a, #5b** — status-update atomicity (`status_override`), insights filter includes `'transcript'`, free chat + guided target both handle phrases. Resolved 2026-05-18/19.
4. **#4a, #5** — notifications: per-row mark-after-yield + `request_failed` firing. `ReadingReviewPage` shipped, mastered→known propagation wired. Dual-schedule (#5 sub-issue / Hole 23) accepted, not closed. Resolved 2026-05-19/20. **#4b LISTEN/NOTIFY still deferred.**
5. **#6, #7** — channel prefs moved to `user_channel_preference` (migration 027 / T1.4); scraper flat-files consolidated to `seed_data/channels.json`. Resolved 2026-05-20.
6. **#11, #12, #13, #14** — deploy gates (CORS env-driven, rate limit, JWT error shape, shared frontend 401 handler). Resolved 2026-05-19.
7. **#17, #20** — structural debt: src/app salvage batch 1 (W4); dark-mode theme system (#20a/b) + tristate (T1.3). Resolved 2026-05-19/20.
8. **#3** — `os.chdir` import hacks replaced by path-from-`__file__` in `phrase_finder.py`. Resolved 2026-05-20.
9. Everything else as it comes up — see `docs/ROADMAP.md` Tier 4 + Tier 5 for what's still open.
