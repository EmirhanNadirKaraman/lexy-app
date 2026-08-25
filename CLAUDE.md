# CLAUDE.md

Guide for Claude (and humans) working in this repo. Read this before exploring — it pins down what's real, what's scaffolding, and what's dead.

---

## 1. What this project is

A **German language-learning app** built around an *i+1* model (each learning unit appears in a sentence where everything else is already known). It pulls source content from YouTube subtitles and uploaded PDF books, extracts learning units with spaCy + custom logic, schedules review with SM-2, and uses Claude Haiku for guided chat, translations, and explanations.

Primary user goal: see a word in real context, mark/learn it, see it again at the right interval, eventually produce it in chat.

---

## 2. Repo has TWO backends — don't confuse them

| Layer | Path | Status |
|---|---|---|
| **App backend** (production) | `lexy-app/backend/` | FastAPI + asyncpg + Postgres. This is what serves the frontend. |
| **Pipeline modules (root)** | `pipeline.py`, `eligibility.py`, `exposure_counter.py`, `user_knowledge.py`, `learning_units.py`, `onboarding.py`, `subtitle_*.py`, `utterance_*.py`, `word_knowledge.py`, `validate_tier_lemmas.py` | Standalone in-memory utilities. NOT mounted on FastAPI. Used by `subtitle-scraper/` and ad-hoc data prep. |
**Implication:** the root pipeline modules are the **only** pipeline tree. Change them directly; there is no second copy to keep in sync.

> **Historical (resolved 2026-07-27):** a third layer, `src/app/`, used to hold a
> half-finished repackaging of the root pipeline modules. It had zero runtime
> callers while 537 of the root suite's 671 tests exercised it, so the project
> shipped untested runtime code and maintained tests for code nothing ran. The
> valuable behaviour was ported onto the runtime modules (runtime coverage
> 20 → 93 tests) and `src/` was deleted. If you find a doc or comment that still
> says "two copies of the pipeline", it is stale — say so rather than recreating
> the split.

---

## 3. Stack

### Backend (`lexy-app/backend/`)
- **FastAPI** + **asyncpg** (async Postgres driver, pool-based)
- **Alembic** migrations (25+ files in `alembic/versions/`)
- **JWT auth** (HS256, secret in `SECRET_KEY` env)
- **LLM** → `services/llm_provider.py` is the only place a client is constructed. Two backends, selected by `LLM_PROVIDER`: `anthropic` (default, `claude-haiku-4-5-20251001`) and `openai_compatible` (any `/chat/completions` server — Ollama `/v1`, llama.cpp, vLLM, LM Studio). One call shape everywhere: non-streaming, JSON Schema in, structured dict out. **No host is assumed** — `LLM_BASE_URL` is built for a GPU box over Tailscale, not localhost. See §11 for the env vars.
- **LLM cache**: SHA256(prompt_key + model + params) → `llm_cache` table, with TTL. Curated human glosses live in the same table under the sentinel model `curated:words_4000_old`, which `translate_item_gloss` checks before the model-specific key so they survive an `LLM_MODEL` switch.
- **spaCy** (`de_core_news_md` etc.) for tokenisation/lemmatisation.

### Frontend (`lexy-app/frontend/`)
- **React 19** + **React Router 7** + **Vite 8** + **TypeScript** (strict)
- **No state library** — `useOutletContext` + custom hooks + `localStorage` for token
- **No CSS framework** — inline `React.CSSProperties` everywhere, dark mode via boolean prop
- **Vitest** + jsdom (322 tests across 44 files)
- Dev proxy: `/api → http://localhost:8000`

### Data pipelines
- **`subtitle-scraper/`** — yt-dlp + spaCy + Postgres. Pulls transcripts for ~916 YouTube channels.
- **`pdf_text_extraction/`** — Docling + TATR + custom masking. **NOT used for book ingestion, and currently non-importable**: it imports `pipeline.stages.pdf_text_extraction.*`, a layout that doesn't exist in this repo (`ModuleNotFoundError: No module named 'pipeline.stages'`). The shipping book path is `book_service` (PyMuPDF embedded text → sparse-text heuristic → pytesseract OCR fallback), which imports none of this. Verified 2026-07-27.
- **`masking/`** — alternative/older PDF pipeline (`latest_ingest.py`, table reconstruction). Partially functional: run from inside the folder, `mask_tables.py` and `visualize_docling_full.py` import fine, but `latest_ingest.py` / `simple_pdf_processor.py` need a `parsers` package that exists nowhere in this repo — so the **text-stitching** half (`ContextAwareStitcher`) is the missing piece. `book_service` does no cross-block stitching, so that is the one real gap here.
- **`postprocessing/`** — bulk extraction of phrases from already-scraped text into `word_occurrences`.
- **`ilp/`** — PuLP-based book-selection optimiser (find minimum book set covering a target vocab list). Standalone CLI, still unwired. Its *technique* now also lives in `playlist_service.ilp_cover` as the opt-in `algorithm="ilp"` playlist mode — re-implemented for the request path, not imported, since the CLI reads word-list files and its own DB config.

---

## 5. Database (Postgres, asyncpg, Alembic)

### Core polymorphic key
`(item_id, item_type)` where `item_type ∈ {word, phrase, grammar_rule}`. This is the join used by every progression / review / analytics row.

### Notable migrations
- **001** — users / user_word_knowledge / srs_cards baseline
- **005** — phrase_table (with `phrase_type` enum)
- **007** — grammar_rule_table (linked via `applicable_phrase_types`, `applicable_lemmas`)
- **008** — books (documents/pages/blocks)
- **009** — reading_selections
- **013–022** — channel surrogate-key refactor (3-phase: youtube_channel_id string → internal SERIAL id)
- **036** — generated `word_norm` / `lookup_key_norm` columns (ICU-normalized) + prefix index, so autocomplete folds Unicode correctly under the C-locale DB
- **037** — `word_lists.user_id` nullable + `is_system` + a CHECK pairing them, so built-in shared lists can exist without a synthetic owner
- **023** — notification

---

## 6. Service layer (`lexy-app/backend/services/`)

| Service | What it owns |
|---|---|
| `auth_service` | register, login, password hashing |
| `progression_service` | **single source of truth** for knowledge-state changes (passive/active levels, SM-2 scheduling, auto-promotion). All state changes go through here. |
| `review_service` | SRS review API (uses srs_cards + user_word_knowledge). Maps (direction, correct) → progression event. |
| `word_service`, `phrase_service`, `grammar_service` | content CRUD + seeding (phrase_table & grammar_rule_table seeded on startup) |
| `llm_service` | Claude Haiku — guided chat eval, openers, hints, free chat, cloze gen, example gen. All structured via tool_use. Has `MOCK_LLM=true` mode. |
| `llm_cache_service` | SHA256 prompt-keyed cache; permanent for translations/explanations, TTL for cloze/examples |
| `book_service`, `book_llm_service` | upload, ingest (docling + masking), per-block LLM repair, page-level batch repair |
| `reading_service`, `reading_llm_service`, `reading_stats_service` | interactive reading (word status overlays, selections, translate, explain, coverage) |
| `chat_service`, `guided_chat_service` | session lifecycle; guided picks target item via `recommendation_service` or caller |
| `recommendation_service`, `prioritization_service`, `insights_service` | recommendations (sentences/videos/items) + insight cards (frequent_unknowns, recent_mistakes) |
| `matcher_service` | calls into `subtitle-scraper/phrase_finder.py` via a fragile `os.chdir` + sys.path hack (see §10) |
| `nlp_service` | spaCy wrapper |
| `playlist_service` | playlist generation from target words |
| `reminder_service` | learning-reminder summary |
| `settings_service` | reads/writes `users.settings` JSONB (channel prefs etc. live here) |
| `usage_events_service` | records `word_usage_events` (analytics, fire-and-forget) |
| `search_service` | full-text search over videos/subtitles |

---

## 8. Learning-progression model

The thing to internalise — every other service depends on it.

- **Two directions per item**: `passive` (recognise) and `active` (produce). Each has its own SRS card and its own level (0 → mastered).
- **Status enum** on `user_word_knowledge`: `unknown` → `learning` → `known`. Driven by passive/active level crossings.
- **Auto-promotion** happens inside `progression_service` on every recorded event (transcript click, status change, SRS review correct/incorrect, guided chat target_counted, free chat language_detected use).
  - The free-chat branch is the one to be careful with: `language_detected == session_language` is the *only* gate between passive-only and active credit (`routers/chat.py:204`), and active credit feeds one-way auto-promotion to `known`. Widening that gate over-grants unrecoverably — see Hole 19 / **N5** in `docs/ROADMAP.md` before touching it.
- **SM-2** scheduling lives in `progression_service._update_srs(conn, ..., direction, action)`.
- **Transactions:** `apply_progression` wraps the upsert + promotion + SRS update in a single `async with conn.transaction():` (line 178). Don't add a competing outer transaction.
- **Events are deferred / fire-and-forget** for non-critical analytics (`usage_events_service.record_event` via `asyncio.create_task`).

If you change progression rules, check `tests/test_progression.py` + `test_free_chat_progression.py` + `test_reading_progression.py`.

---

## 8b. Loop invariants — non-obvious rules in the state machine

Read this before assuming anything about how an event flows through `progression_service._RULES`. These are real, intentional choices (or at least intentional today) that the code does not announce.

**Card creation rules.** Passive cards are created on first exposure (`transcript_clicked`, `status_marked_learning`). Active cards are created on explicit Learning click — `status_marked_learning` has `active_srs="create"` since #0b (2026-05-19) — and on any event that already includes a correct production outcome (`guided_counted`, `free_chat_used_correctly`, `active_review_correct`). `transcript_clicked` and `status_marked_known` do NOT create an active card: the former is recognition-only exposure, the latter is a confidence click (see the rule below). Both `'create'` actions are idempotent via `INSERT … ON CONFLICT DO NOTHING`.

**`passive_review_correct` bumps `passive_level` (since 2026-05-18).** A successful passive review now adds 1 to passive_level — matches `transcript_clicked`'s weight. Historical: before this date `passive_delta=0`, so reviews didn't grow the "Understood" dots.

**`status_marked_unknown` resets the SRS schedule (since 2026-05-18).** Clicking "Unknown" sets `passive_srs="incorrect"` and `active_srs="incorrect"` — both run the SM-2 incorrect branch on cards that exist (interval=1 day, ease-0.15, reps=0). Levels are intentionally left alone (no fabrication, mirrors `status_marked_known`'s choice). Crucially, `action="incorrect"` with a missing card is a **no-op** (see `_update_srs` line ~333) — manual unknown never *creates* a new active card. Historical: before this date the rule was an empty delta and the SRS card kept its long interval.

**`status_marked_known` does NOT touch active levels or the active SRS card.** Manual "Known" is user confidence, not production evidence — `active_delta=0`, `active_srs=None`. The status field is written to `known` inside `apply_progression`'s own transaction, from the `status_override="known"` the router passes (`routers/words.py:153`) — see the atomicity rule below. The rule only advances the passive SRS card. Active mastery is reserved for real production events (`guided_counted`, `free_chat_used_correctly`, `active_review_correct`). Historical note: before 2026-05-18 this rule wrote `active_delta=1, active_srs="correct"` — inflating active mastery on a confidence click. Fixed; if you see a user with active_level > 0 and no production events in `word_usage_events`, that's pre-fix data.

**Auto-promotion is one-way; manual demotion is symmetric.** No event auto-demotes `known` — there is no spaced-forgetting check. Promotion paths (in `_maybe_promote`, evaluated in order, one fires per call): (1) `active_level >= active_threshold and status != 'known'` → `'known'`; (2) `passive_level >= passive_threshold and status == 'learning'` → `'known'` (Hole 9 fix, 2026-05-19); (3) `passive_level >= passive_threshold and status == 'unknown'` → `'learning'`. Crossing the passive threshold from `unknown` reaches `learning` first; the next passive event then promotes to `known`. **Manual demotion** (Hole 26 fix, 2026-05-19) lives in `_apply_demotion`: when `status_override` demotes the prior status (known → learning, known → unknown, learning → unknown), the additive rule is bypassed and levels are *set* (not decremented). `known → learning`: passive=1, active=0, both SRS cards `reset` (due in 1d, ease preserved, missing cards created — see the new `'reset'` action in `_update_srs`). `known → unknown` and `learning → unknown`: passive=0, active=0, both SRS cards `incorrect` (existing cards penalised, missing cards stay missing). `times_seen` and `times_used_correctly` are never rewritten by demotion. Transition rank lives in `_is_demotion` (`unknown < learning < known`).

**Phrases + grammar rules now first-class throughout the loop (2026-05-19).** Chat: `chat_service.match_learning_words` delegates to `matcher_service.match_sentence_with_ids` (spaCy-based; catches inflected production like *ich freue mich auf* → `sich freuen auf`). Guided targets: `guided_chat_service.get_next_target` considers `phrase_table` alongside `word_table` at all three priority tiers. Enrichment: `recommendation_service.enrich_by_type` is the single dispatcher for words + phrases + grammar rules, keyed by `(item_type, item_id)` to avoid SERIAL-key collisions. Used by `recommend_items` and `insights_service._build_card`.

**Reading SRS is parallel to main SRS.** `reading_selections.next_review_at` runs a fixed `[1,2,4,7,14,30]` day schedule. When `find_catalog_item` matches a selection to a `word_table` / `phrase_table` row, BOTH schedules advance (reading on its own table, main via `apply_progression`). They diverge after the first review. Frontend `ReadingReviewPage` (since #5, 2026-05-20) consumes `/api/v1/reading/selections/due`; entry point is a "Reading Review" button in `BookLibraryPage`. **Save + outcome mapping** in `routers/reading.py`: `save_selection → status_marked_learning with status_override="learning"` (#5 follow-up, 2026-05-20 — saving means "I want to learn this", status flips atomically); `got_it → passive_review_correct`, `still_learning → passive_review_incorrect`, `mastered → status_marked_known with status_override="known"` (reading Mastered = manual known confidence, NOT active production — `active_delta=0`, no active SRS card fabricated). Save's catalog progression runs in a *separate* transaction from the `reading_selections` INSERT — same pool, different `apply_progression` call. Failure mode unchanged: if `apply_progression` errors, the reading row persists without catalog state.

**`status_marked_*` event flow is atomic (since 2026-05-19, #2).** `routers/words.py:update_status` makes a single call to `apply_progression(..., status_override=body.status)`. The status flip, level/counter deltas, auto-promotion, and SRS card moves all happen inside one `async with conn.transaction()` in `progression_service.apply_progression`. `word_service.upsert_word_status` has been removed. Historical: before this fix the router wrote status in one transaction and applied progression in another — a second-call failure would leave the user with a flipped status but no SRS/level update and no surfaced error.

**SRS review is now real (since 2026-05-19, #0a complete; T1.2 / Hole 12 closed 2026-05-20).** Backend (#0a-1): `review_service.get_due_cards` carries `prompt_text` + `answer_text`. Since T1.2 both directions use the **same** mapping — `prompt_text = gloss, answer_text = display_text` (word/phrase: English LLM gloss → German surface; grammar_rule: English `short_explanation` → German `title`). The two directions only differ in grading: passive = reveal-then-self-grade, active = typed German via `POST /srs/review/{card_id}/produce` → `llm_service.evaluate_production` (or fast-path exact match). Self-grade is no longer exposed for active cards. Frontend `SRSReviewPage.tsx` consumes `prompt_text` / `answer_text` without direction-specific text-side logic. Glosses for word/phrase come from `llm_service.translate_item_gloss` (cached permanently). Historical: pre-T1.2 passive cards showed German up front + asked "do you recognise this?" (Hole 12) — pure self-report against a German display, no recall test.

**`usage_events_service` insight filter includes transcript clicks (since 2026-05-18).** `most_frequent_unknown_items` now filters `context IN ('free_chat','guided_chat','status_change','transcript')`. Subtitle clicks surface in the "Keeps coming up" insight card. Historical: before this date `'transcript'` was excluded, hiding the dominant exposure channel.

**Transcript-click dedup (since 2026-05-20, T1.1 / Hole 3 + Hole 4).** `POST /api/v1/words/word/{id}/transcript-click` now accepts an optional `{sentence_id}` body. When supplied (the frontend always supplies it — `PlayerView` from the current `sentences[sentenceIdx]`, `TranscriptPanel` from the clicked row), `usage_events_service.record_transcript_click_event` does `INSERT … ON CONFLICT DO NOTHING` against the unique partial index `uq_word_usage_events_transcript_dedup` on `(user_id, item_id, item_type, sentence_id, event_day)` WHERE `context='transcript' AND sentence_id IS NOT NULL` (migration 026, with `event_day` as a stored generated UTC-date column). If the insert is a no-op (same word, same sentence, same UTC day), the router returns 204 without firing progression — `passive_level` / `times_seen` / SRS card all stay put. If the insert is new, the router then calls `apply_progression("transcript_clicked")` as before. Legacy clients that omit `sentence_id` still hit the un-deduped path; a debug log records each occurrence so we can find any caller that hasn't migrated. Frontend: `useWordStatus.recordTranscriptClick` now throws on non-2xx and the hook surfaces failures via `console.warn` (was: silently swallowed in `.catch(() => {})`).

---

## 9. Core user flows

1. **Search → Watch → Mark**: search videos → PlayerView with YouTube embed + subtitles → click a word → WordStatusPicker → status update → exposure recorded.
2. **Guided practice**: ForYou page or insight card → PrepView → start guided session → LLM evaluator scores each turn → on `target_counted=true`, progression event fires → session complete → summary.
3. **Book reading**: upload PDF → docling+masking → BookReaderPage → word colours by status → multi-token select → SelectionPanel (note/translate/explain) → goes into `reading_selections` → eventually surfaces in SRS via `review_service`.
4. **SRS review**: SRSReviewPage → GET /srs/due → show display_text → user marks Got it / Mistake → POST /srs/review/{card_id} → progression_service handles scheduling + level + status.
5. **Content request**: AddContentPage → POST /content-requests → backend spawns `python subtitle-scraper/pipeline.py --requests-only` subprocess → SSE notification when done.

---

## 10. Sharp edges / known traps

- ~~`srs_service.py` is dead code.~~ Resolved 2026-05-19 — file deleted along with the three dead endpoints. Only `review_service.py` + `progression_service._update_srs` remain.
- ~~`matcher_service.py` and `subtitle-scraper/pipeline.py` use `os.chdir()` at import time.~~ Resolved 2026-05-20 (#3). Root cause was `phrase_finder.py:42` loading `"data/final_result.txt"` via a cwd-relative path. Fixed to resolve from `Path(__file__).resolve().parent.parent / "data"`. All five chdir sites (`matcher_service.py`, `pipeline.py`, `profile_pipeline.py`, `profile_full_pipeline.py`, `test_scraper_channels.py`) now just `sys.path.insert(0, scraper_dir)` and import normally.
- **Channels live in the `channel` table only** (since 2026-05-20, #7). `subtitle-scraper/seed_data/channels.json` is the bootstrap seed for fresh deployments; `seed_channels.py` upserts it into the DB (idempotent). Runtime (`pipeline.py:load_channels`) is DB-only — no file fallback. Old flat files at the scraper root (`channels.json`, `merged_channels.json`, `subscribed_channels.txt`) and `merge_channels.py` are gone.
- **Channel preferences** moved to `user_channel_preference (user_id, youtube_channel_id, preference_kind)` since T1.4 / migration 027 (2026-05-20). Display-name cache `channel_names` still lives in JSONB (it's a lookup table, not a preference). Frontend API shape unchanged — `get_preferences` joins on read.
- **`apply_progression` IS transactional** and now writes status too (resolved 2026-05-19). The router calls `apply_progression(..., status_override=body.status)` once — status + level deltas + SRS card moves all happen inside one transaction. `word_service.upsert_word_status` is gone; `progression_service` is the single writer to `user_word_knowledge`.
- **LLM rate limiting** (since 2026-05-19, #12). `services/rate_limiter.py` — in-memory sliding window, 30 req/min + 400 req/hour per user. Wired via `core/deps.rate_limit_llm` into all 10 LLM-backed routes. `GET /srs/due` exempt (cached glosses). 429 → `detail="rate_limit_minute"` or `"rate_limit_hour"` with `Retry-After`. **Multi-worker deploy needs Redis backend** — limiter is in-process today.
- **CORS env-driven** (since 2026-05-19). `CORS_ORIGINS` env var, comma-separated, falls back to `http://localhost:5173` when unset. Documented in `.env.example`. See `_parse_cors_origins` in `main.py`.
- **Theme** is a CSS-variable system driven by `[data-theme="light|dark"]` on `<html>` (#20a/#20b). Since T1.3 (2026-05-20) the user pref is a tristate `theme_mode = system | light | dark` (default "system" for new users). `useResolvedTheme(mode)` resolves "system" via `prefers-color-scheme: dark` and updates live when the OS switches. `dark_mode` boolean remains on the wire for legacy clients (mirrored from theme_mode on every write).
- **Mobile** mostly desktop-only. Memory says polish is blocked until end-to-end loop works.
- **Notifications** — write side: scraper emits `channel_done`, `video_done`, and `request_failed` (since 2026-05-19) via `_notify_user`. Read side: `routers/notifications.py` SSE handler now yields each row first and marks `seen=true` only after the yield resumes (per-row, mark-after-yield via the extracted `_yield_unseen(pool, user_id)` helper). Disconnect mid-stream leaves un-yielded rows unseen for re-delivery. Polling is still 3s (LISTEN/NOTIFY refactor is TODO #4b — not user-visible).
- **Migration 010 ≠ separate `reading_review` table.** It adds `review_count` + `next_review_at` columns to `reading_selections`. Reading SRS now has a frontend (`ReadingReviewPage`, since #5 / 2026-05-20) and a wired `mastered → known` propagation; it still runs a parallel schedule to `srs_cards` for the same item (both advance on review — Hole 23 documented and accepted).
- **`@/scripts/`** is mostly one-off legacy data fixers; check before editing.
- ~~**Two copies of pipeline code** (root vs `src/app/`).~~ Resolved 2026-07-27 — `src/` deleted. Root is the only pipeline tree; edit it directly.

---

## 11. Dev quickstart

### Backend
```bash
cd lexy-app/backend
# create venv, pip install -r requirements.txt
alembic upgrade head
uvicorn main:app --reload --port 8000
```

### Frontend
```bash
cd lexy-app/frontend
npm install
npm run dev          # Vite on :5173, proxies /api → :8000
npm run test         # vitest
```

### Subtitle scraper (one-off / cron)
```bash
cd subtitle-scraper
python seed_channels.py            # bootstrap channel table from seed_data/channels.json
python pipeline.py                  # full run
python pipeline.py --requests-only  # consume content_request queue
```

### Hit an error? Check `docs/COMMON_ERRORS.md` first
Running log of errors actually hit in this repo, symptom-first so you can grep it
by the error text in front of you. It already covers the traps that cost the most
time here: `pytest-randomly` breaking every test at setup, `tsc --noEmit` passing
while `npm run build` fails, `cd` persisting between shell calls and producing
plausible-but-wrong numbers, and the ruff fixes that must **not** be applied.
**Add an entry whenever you hit something new**, in the same change that fixes it.

### Lint — run this as part of every backend change
```bash
# From the REPO ROOT (not lexy-app/backend) — covers backend, scraper,
# root pipeline, scripts, masking and ilp in one pass:
ruff check .                        # must report "All checks passed!"
ruff check . --fix                  # apply the auto-fixable subset
```
Ruff is **part of backend validation, not an optional extra** — run it alongside
`pytest` before calling any Python change done. Baseline as of 2026-07-27: zero
findings (was 153; see the ruff entry in `docs/TESTS.md`).

Since val-03 (2026-08-22) it is also the check the autoloop stops at: a loop
validation run executes the configured commands **in order and stops at the
first one that fails**, reporting the rest as `NOT RUN` (`docs/AUTOLOOP.md`
§4h). So a ruff finding means no test suite ran that round — read `NOT RUN` as
"no evidence either way", never as "passed" — and fixing lint first is how a
round gets past it. Nothing about the verdict changed: a failing run still
fails, and a passing run still runs every command.

Two things to know before "fixing" what it reports:
  - **`# noqa: E402` on a sibling import is load-bearing.** `subtitle-scraper/`
    modules import each other after a `sys.path.insert` — the deliberate
    replacement for the old `os.chdir` hack (§10 / TODO #3). Hoisting those
    imports to the top breaks the scraper. Each carries a comment saying so.
  - **The rule set is pinned in `/ruff.toml`** (`select = ["E", "F"]`,
    `ignore = ["E501"]`) and ruff itself is pinned to `0.14.1` in
    `lexy-app/backend/requirements.txt`. Don't rely on ruff's built-in defaults
    — they're narrower (E4/E7/E9 + F) and shift between releases. **E501
    (line-too-long) is off deliberately**: enabling it reports 405 pre-existing
    violations, i.e. a repo-wide reflow, not a lint fix. Import sorting (`I`) is
    off for the same reason plus the E402 trap above.

### Backend tests
```bash
cd lexy-app/backend
python3 -m pytest                   # serial
python3 -m pytest -n auto           # parallel via pytest-xdist — ~60s for 1258 tests
```
**Use `python3 -m pytest`, not the bare `pytest` entrypoint.** `python -m`
puts the cwd on `sys.path`, which `tests/test_document_package.py` (A2) needs
for its bare `from services import …` imports; the `pytest` console script
does not, and the file then fails collection (1151 instead of 1258 — see
`docs/COMMON_ERRORS.md` §2).
`pytest-randomly` is blocked via `addopts = -p no:randomly` in both ini files.
That is deliberate and load-bearing — see `docs/TESTS.md` for the spaCy/thinc
seed conflict it works around. Don't remove it.
Each test user's email is tagged with `PYTEST_XDIST_WORKER` (or `main` when serial)
via `tests/_email_helper.make_test_email()`; the autouse cleanup fixture uses the
same per-worker LIKE pattern, so parallel workers don't trample each other's rows.
Tests that need to look up "a test user" must filter via `cleanup_pattern()`
instead of bare `'test+%@example.com'` — see `test_words.py` / `test_recommendations.py`.
`llm_cache` is global with no user FK, so it has its own per-worker tag in
`tests/_cache_helper.py`: every row a test causes to be written carries a
`zztest-model-{worker}` model, and the autouse cleanup reaps that pattern.
Never clean it by `prompt_key` — `item_gloss` is a real production key.

### Root tests — what a bare `pytest` actually covers
```bash
pytest                              # root pipeline modules + autoloop (testpaths)
pytest tests/                       # just the root pipeline modules
pytest autoloop/tests               # just the autoloop loop harness
```
Root `pytest.ini` sets `testpaths = tests autoloop/tests`, so a bare `pytest`
at the repo root runs **both** trees. Expect minutes, not seconds — the
autoloop suite shells out to real `git` and spawns real subprocesses. It is
still hermetic (no database, no network, no real `claude` CLI) and derives
every state dir, worker root and lock path from `tmp_path`, so running it
cannot disturb a live loop's `~/.autoloop`.

**The two suites a bare root `pytest` does NOT run**, because each needs its
own runner and environment:
- `lexy-app/backend/tests` — own `pytest.ini` (asyncio settings) + a live
  Postgres. Run `cd lexy-app/backend && python3 -m pytest -n auto`.
- `lexy-app/frontend` — vitest. Run `npx vitest run`.

So "a Python change is validated" means three commands, not one: `ruff check .`,
a bare root `pytest`, and the backend suite. Full table in `docs/TESTS.md`.

### Env (`.env` at repo root, also read by backend)
```
DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT
SECRET_KEY                # JWT signing
ANTHROPIC_API_KEY
MOCK_LLM=false            # set true to short-circuit LLM in tests

# LLM provider — all optional; unset = Anthropic, exactly as before.
LLM_PROVIDER=anthropic    # anthropic | openai_compatible
LLM_MODEL=                # overrides the model; REQUIRED for openai_compatible
LLM_BASE_URL=             # REQUIRED for openai_compatible, e.g.
                          #   http://desktop-tailscale-name:11434/v1
LLM_API_KEY=              # usually ignored by local servers
LLM_TIMEOUT_SECONDS=60
```

**Pointing at a local model server.** Set `LLM_PROVIDER=openai_compatible`,
`LLM_BASE_URL` and `LLM_MODEL`. A misconfigured pair fails at **import**, not on
the first learner's message — a backend that can't reach its model server should
not boot. Two things to know before switching:

- **Keep the server off the public internet.** Ollama/llama.cpp have no
  meaningful auth. Reach them over Tailscale or another authenticated tunnel.
- **Measure quality before moving the grading paths.** `guided_evaluate` and
  `evaluate_production` write real SRS state through `progression_service`; a
  weaker model there corrupts scheduling rather than just reading badly.
  Translations and glosses are the safe places to start.

---

## 12. Conventions / rules of the road

- **Never read the real `.env` file.** It holds production secrets (DB password, JWT signing key, Anthropic API key). Use `.env.example` for the schema. If you need to confirm a variable name, grep the source — don't open `.env`.
- **Commit messages never mention AI.** No `Co-Authored-By: Claude` (or any AI tool) trailer, no "🤖 Generated with…" line, no "written by Claude / with AI assistance" phrasing anywhere in the subject or body. Write every commit as the author. (This overrides any default tooling that auto-appends such a trailer.)
- **Use `docs/SUMMARY.md` as the file-level index.** "Where do I look to change X?" is answered there. Update it whenever a file is added, removed, or changes responsibility.
- **Update `docs/TESTS.md` whenever you add, remove, or rename a test.** It tracks coverage, known pre-existing failures, and the xfail audit-hole pins (flip strict=True when the fix lands so the test enforces the new behaviour).
- **Record a change note as ONE NEW LINE, appended at the END of the tracker. Never grow, edit or reorder an existing line.** This is a merge requirement, not a style preference. `docs/SUMMARY.md`, `docs/TESTS.md`, `docs/SECURITY.md` and `docs/COMMON_ERRORS.md` — the four paths in `autoloop/note_merge.NOTE_TRACKERS` — each end with an append-only **"Change notes"** section opened by a `<!-- CHANGE-NOTES: ... -->` comment, and the loop's own merge path (`auto_merge.AutoMerger._merge` → `autoloop/note_merge.py`) combines two branches' appended lines there automatically. That resolver only fires when **each side left every pre-existing line byte-identical and added nothing outside that section** — so obeying the rules below is what makes a parallel merge work, and breaking any of them stops the merge sweep exactly like a source conflict. The old habit of appending a sentence into an existing row produced a 19,410-character row in `SUMMARY.md`, a 15,729-character row in `TESTS.md`, and a sweep that halted three times in one evening (2026-08-18), leaving five reviewed tasks unmerged for a day. So:
  - Adding a **new table row** for a file or a test you touched — the normal case — is a new line and is fine.
  - Anything else goes at the very end of the file, **below** the `<!-- CHANGE-NOTES: ... -->` comment: one line, `| date | task-id | note |`, appended after the last line. Nothing may follow it — no new heading, no trailing prose — or the next task's append lands inside your section instead of at the end of the ledger.
  - **Each of those lines has a hard length limit**, enforced by `autoloop/tests/test_docs_merge.py::test_every_change_note_line_is_short_enough_to_merge_by_line` over the WHOLE line — the `| date | task-id |` cells count, not just your sentence — so one over-long note fails validation and throws the round away (measured 2026-08-21: two full rounds, merge-04 and blk-02). The number lives in exactly one place, `autoloop/note_merge.MAX_NOTE_LINE_CHARS`, and is deliberately NOT copied here: a second copy would agree today and silently disagree the first time it moved. Read it there — the implementing agent is also told it directly, since `implement_executor._authoring_rules` renders it into every brief. If a note does not fit, append a second line.
  - Do **not** append a clause to an existing row, paragraph or note line to say what your task did — not even to the row for the file you changed. Split it into a second line instead (the four `state.py`, `transcript.py` rows in `SUMMARY.md` are the template: one dated note per line, same first cell).
  - Do not edit, delete or reorder a line someone else wrote. The resolver refuses the whole merge when a side does, and it is right to: a rewritten claim needs a human, and the sweep stopping is how you get one.
  - Since notes-04 (2026-08-23) that resolver fires in **both** merge directions, not only the one line 322 names: merging a task INTO the base branch (`auto_merge.AutoMerger._merge`) and merging the base branch's head INTO a task branch to refresh a stale base (`orchestrator._carry_reviewed_candidate_past`) both call `note_merge.combine_conflicted_notes`. Nothing you have to do differs — same four files, same one-line-at-the-end rule, same refusal for anything else. It means a change-note collision no longer parks a reviewed candidate as `task_base_behind_head`, which is what the rules above buy you. Read line 322's arrow as the first of two call sites.
  - Notes accumulate in arrival order, not date order — two branches' appends are concatenated. Read the date column, don't infer from position.
  - **Inside those four trackers, refer to the marker as `CHANGE-NOTES`; never write the comment out in full a second time.** The resolver finds the section by that text and requires it to appear *exactly once* per tracker, so a note, a table row or a paragraph that quotes the whole comment while explaining how this works switches auto-resolution off for that file — silently, and the sweep starts halting again exactly as it did before. docs-01 shipped that bug in `SUMMARY.md` itself; `test_docs_merge.py::test_every_shipped_tracker_ends_with_an_append_only_section` is what catches it. Quoting the comment *here*, in `CLAUDE.md`, is fine — this file is not one of the four trackers.
  - `merge=union` was tried for this on 2026-08-19 and removed the same day. It never reports a conflict at ALL on a file it is attached to (git cannot scope a merge attribute to a region), so it silently concatenated genuine prose conflicts, and it resolves per LINE, so it duplicated a grown row instead of merging the two additions. Do not reintroduce it; `.gitattributes` is kept rule-free and explains why.
  - `docs/SECURITY.md` and `docs/COMMON_ERRORS.md` joined that list on 2026-08-23 (notes-03), each after being given its own section — the section first, the list second, because a path granted to the resolver without a marker-delimited region is a file whose ordinary prose it would start combining. Only the note goes below the marker: a finding still goes in *Open findings* with its four fields, an error still gets its `###` entry in the section it belongs to, and a conflict in either still stops the sweep.
  - The same rules apply to the remaining two trackers (`CLAUDE.md`, `docs/SCHEMA.md`) as good practice, but they have no append-only section and nothing auto-resolves them — a conflict there stops the sweep, deliberately, because they carry claims that need a human when two branches disagree.
  - Pinned by `autoloop/tests/test_docs_merge.py`, which merges real branches through the production merge path and demonstrates the union failures above rather than asserting them.
- **Read `docs/COMMON_ERRORS.md` when something breaks, and add to it when something new breaks.** Symptom-first log of errors actually hit here — grep it by the error text before debugging from scratch. Several entries are traps where the obvious fix is wrong (ruff E402 on sibling imports, F401 on availability probes), so it is worth a look *before* "fixing" a lint or test failure, not only after being stuck.
- **Consult and update `docs/SECURITY.md` for any security-relevant change.** It's the living tracker of open/resolved findings and the controls we rely on. See §14 for the read/update triggers and the four fields every finding must carry.
- **All state changes go through `progression_service`.** Don't write directly to `user_word_knowledge` or `srs_cards` from a router.
- **All LLM calls go through `llm_provider` and cache via `llm_cache_service`.** Don't instantiate `AsyncAnthropic` ad-hoc — `services/llm_provider.py` holds the only construction (true since 2026-07-27; `llm_service`, `book_llm_service` and `reading_llm_service` each built their own before that). New LLM call sites take a JSON Schema and call `_provider.structured(...)` — never a provider class directly, so both backends stay swappable.
- **Polymorphic key everywhere:** if you add a new tracked content type, it gets an `item_type`, lives in its own content table (with `display_text` available), and plugs into `user_word_knowledge` / `srs_cards`.
- **Migrations are append-only** — never edit an existing migration once it's been run anywhere.
- **Tests live next to the layer they test** (backend tests under `lexy-app/backend/tests/`, pipeline tests under `tests/`).
- **No new top-level Python files** without a reason — root is already crowded with the pipeline modules. (The `src/app/` refactor that used to sit alongside them was deleted 2026-07-27; don't start a replacement without a plan to finish it.)
- **The audit's domain charters describe THIS repository, and live in it — `docs/audit_charters.toml`.** The per-domain briefs the loop's read-only audit agents get (the two-backend split above, the ingestion pipeline, which docs are canonical) are that file, since port-03 (2026-08-19); path configurable via `[repo].audit_charters_file`, format and rules in `docs/AUTOLOOP.md` §7 ("Domain charters"). **Edit BOTH, in the same commit** — the file and `DEFAULT_DOMAINS` in `autoloop/audit/executor.py`, which still holds the same six domains as the fallback for a repository that ships no file at all. `autoloop/tests/test_audit_charters.py` requires the shipped file to parse to exactly that tuple, so editing one alone fails the suite rather than drifting silently. Likewise, if you change what §2's layer table says, update the matching charter in the same commit or the next audit will brief its agents on a repo layout that no longer exists. Only ONE source is ever read per run: a file that exists wins outright, and a file that exists but is malformed — or is a directory, or cannot be read — aborts the audit rather than falling back.

- **You may ask for out-of-scope residue YOUR earlier round created to be deleted, or an out-of-scope EDIT it made to be put back — nothing else.** Path scope has been advisory since 2026-08-05: a write outside `approved_paths` lands, and the loop records it on `TaskExecution.out_of_scope_paths`. Since scope-04 (2026-08-19) that record is also a narrow cleanup capability, and since scope-05 (2026-08-24) a narrow repair one. You have no Bash and no delete tool, so if a review asks you to strip residue your task left out of scope, write, at the start of a line and copying a path exactly as the prompt lists it, either `REMOVE-OUT-OF-SCOPE: <path>` (the executor unlinks it) or `REVERT-OUT-OF-SCOPE: <path>` (the executor restores it from the task's base commit — git's copy, so do not reconstruct the content yourself). Both run before validation. Use REVERT for a file that existed before your task and was merely edited: deleting that would be a worse overrun. A path that is not in the prompt's list is ignored and the round says so; naming one path under both forms removes it and reports the revert superseded. These grant DELETION and RESTORATION only — never permission to edit, recreate or rename into that path — and they widen no scope. If the prompt does not offer the REVERT form, no revert authority is wired for that run and the line does nothing: say so in your report. A file *inside* `approved_paths` is a different question with a different authority — see the next bullet. Full rule in `docs/AUTOLOOP.md` §4e's 2026-08-19 and 2026-08-24 amendments, security accounting in `docs/SECURITY.md` S25.
- **You may also DELETE a file your own `approved_paths` already let you WRITE — since del-01 (2026-08-25).** Write `DELETE-FILE: <path>` at the start of a line, exactly as above, and the executor unlinks it before validation. The authority here is what the task DECLARED, not the out-of-scope record the bullet above selects from — two authorities for two questions, never merged. A deletion is scope-checked by the same code that records an out-of-scope write (`tasks.unauthorized_paths` over `effective_approved_paths`), so a path outside your APPROVED SCOPE list is refused and reported, and deleting is not a way out of your scope. The six documentation trackers are REFUSED even though you may write them: every task is granted those so it can append a change note, and that is not a licence to remove one. A directory is never deleted, only a regular file or symlink. NAME every file you deleted in your own report — the executor names them too, from what it actually unlinked, and the two accounts are meant to be comparable. A MOVE is a write plus a delete, and both paths must be in scope. Full rule in `docs/AUTOLOOP.md` §4e's 2026-08-25 amendment.
- **An operator hands the loop a rough IDEA, not a finished task — since intake-02 (2026-08-25).** `python -m autoloop intake` (and the dashboard's Intake panel, and a dropped-in `.md`/`.txt`) turns free text into a DRAFT through a question-and-answer exchange held in a markdown file beside the inbox. Three things about it are load-bearing and easy to break by "improving" it. (1) **Nothing reaches the registry without the operator submitting.** `inbox.submit_draft` is the only step that queues, and it queues through the same `TaskInbox.submit` `add-task` uses; the exchange itself files nothing, and abandoning it is deleting one file. (2) **No question may be asked while a round is running.** `inbox.refuse_if_round_running` gates `ask`/`suggest`/`plan` and fails CLOSED on a lock it cannot read — `ask_user` was retired for parking the loop on a question nobody was there to answer, and asking mid-round rebuilds it under a new name. Writing, editing and submitting a draft stay safe at any moment, exactly like `add-task`. (3) **`approved_paths` still comes from `path_suggest` and still needs a human.** The draft's scope is FILLED mechanically and authorized only by the submit; a plan reply's own paths are discarded. Readiness is positive evidence — both `inbox.REQUIRED_QUESTIONS` present AND answered — never "the model stopped asking", so a provider that is down cannot make a draft look finished. Full rule in `docs/AUTOLOOP.md` §4f-sexies.

---

## 13. Where to start if you're new

1. Skim `docs/SUMMARY.md` — file-level index of the whole repo. Tells you which file to open for any feature.
2. Read `lexy-app/backend/main.py` — see the routers, the lifespan, the seed calls.
3. Read `progression_service.py` (especially `_RULES`) — the state machine. Cross-check against §8b above before assuming any rule's behaviour.
4. Read `review_service.py` — what happens on every SRS click.
5. Read `App.tsx` + `SRSReviewPage.tsx` — the frontend loop.
6. Read `docs/WORKFLOW_AUDIT.md` — full word-learning trace from discovery → mastery with every known hole numbered.
7. Glance at `docs/TESTS.md` for what is and isn't covered.
8. Then read `docs/TODO.md` and pick something blocking the smallest number of other things.

---

## 14. Security review (ongoing)

`docs/SECURITY.md` is the **living security tracker** — open findings, resolved findings, and the "verified strengths" we depend on. It is not a one-off audit; it's meant to be read and updated as part of normal work.

**Read `docs/SECURITY.md` before you start a PR that touches any of:**
- auth / login / registration / JWT / password handling
- file upload or anything that writes a user-supplied path or filename
- raw SQL (anything that isn't a plain parameterized `$1` query)
- `subprocess` / spawning the scraper / shelling out
- LLM prompts built from user content
- routers with a path param (`/{id}`) — check the ownership filter
- `users.settings` JSONB, CORS, middleware, or HTTP headers

**When you change security-relevant code, update `docs/SECURITY.md` in the same PR:**
- Fixed an open finding → move it to *Resolved* with the date + commit. **Don't delete it** (regression history).
- Found something new → append to *Open findings* with the four required fields below, and add a summary-table row.
- Weakened a *Verified strength* (e.g. dropped an ownership check, widened the settings whitelist) → that's a regression; either don't, or document why and add the new exposure as a finding.

**Every finding must carry four things** (a finding without these rots into an untested claim):
1. `file:line`
2. severity — HIGH / MEDIUM / LOW / INFO
3. a one-line **verification check** — the exact grep/curl/read that confirms it's still true or still fixed
4. a suggested fix

**Fast self-check greps** (run from repo root; each should come back clean or expected):
```bash
# Non-parameterized SQL outside migrations. Expected hits: search_service.py:62,103,216
# (these interpolate only a loop index + a constant threshold — already verified safe).
# Any OTHER hit, or any hit that interpolates a request value, is a real bug:
rg -n 'f"(SELECT|INSERT|UPDATE|DELETE)' lexy-app/backend --glob '!**/migrations/**'
# Shell / eval / exec sinks (review every hit):
rg -n 'shell=True|os\.system|os\.popen|\beval\(|\bexec\(' lexy-app subtitle-scraper -g '*.py'
# Frontend XSS sinks (should be empty):
rg -n 'dangerouslySetInnerHTML|innerHTML|document\.write|eval\(' lexy-app/frontend/src
# Settings privilege-escalation guard still whitelists (must still gate on DEFAULTS):
rg -n 'k in DEFAULTS' lexy-app/backend/services/settings_service.py
```
