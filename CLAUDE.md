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
| **`src/app/`** | `src/app/{exposure,extraction,learning,pipeline,subtitles}/` | Refactored copies of root pipeline modules, reorganized as a package. **NOT wired into anything** — orphan refactor in progress. |

**Implication:** when you change pipeline logic, you may need to touch *two* copies (root + `src/app/`). Pick one as authoritative before doing real work; right now root is what runs.

---

## 3. Stack

### Backend (`lexy-app/backend/`)
- **FastAPI** + **asyncpg** (async Postgres driver, pool-based)
- **Alembic** migrations (25+ files in `alembic/versions/`)
- **JWT auth** (HS256, secret in `SECRET_KEY` env)
- **Anthropic SDK** → `claude-haiku-4-5-20251001`. All LLM calls use tool_use for structured output.
- **LLM cache**: SHA256(prompt_key + model + params) → `llm_cache` table, with TTL.
- **spaCy** (`de_core_news_md` etc.) for tokenisation/lemmatisation.

### Frontend (`lexy-app/frontend/`)
- **React 19** + **React Router 7** + **Vite 8** + **TypeScript** (strict)
- **No state library** — `useOutletContext` + custom hooks + `localStorage` for token
- **No CSS framework** — inline `React.CSSProperties` everywhere, dark mode via boolean prop
- **Vitest** + jsdom (one test file currently)
- Dev proxy: `/api → http://localhost:8000`

### Data pipelines
- **`subtitle-scraper/`** — yt-dlp + spaCy + Postgres. Pulls transcripts for ~916 YouTube channels.
- **`pdf_text_extraction/`** — Docling + TATR + custom masking. Used for book ingestion.
- **`masking/`** — alternative/older PDF pipeline (`latest_ingest.py`, table reconstruction).
- **`postprocessing/`** — bulk extraction of phrases from already-scraped text into `word_occurrences`.
- **`ilp/`** — PuLP-based book-selection optimiser (find minimum book set covering a target vocab list).

---

## 4. Directory map

```
language-app/
├── lexy-app/
│   ├── backend/                   ← FastAPI app (production)
│   │   ├── main.py                ← lifespan: pool + seed phrases + seed grammar + resume requests
│   │   ├── database.py            ← asyncpg pool
│   │   ├── core/{deps,security}.py
│   │   ├── routers/               ← 14 routers (auth, words, srs, chat, books, reading, …)
│   │   ├── services/              ← 27 services (see §6)
│   │   ├── models/schemas.py      ← Pydantic
│   │   └── alembic/versions/      ← 25+ migrations
│   └── frontend/
│       └── src/
│           ├── App.tsx            ← router + Layout
│           ├── api/               ← 14 fetch wrappers
│           ├── components/        ← 35 components
│           ├── hooks/             ← 12 custom hooks
│           ├── types/index.ts     ← all shared interfaces (~416 lines)
│           └── config/wordColors.ts
├── pipeline.py + (subtitle/learning/exposure root .py)   ← standalone pipeline
├── src/app/                        ← orphan refactor of the above
├── subtitle-scraper/               ← yt-dlp scraper
├── pdf_text_extraction/            ← Docling-based PDF pipeline
├── masking/, postprocessing/, ilp/ ← supporting data jobs
├── data/                           ← word lists, dictionaries (final_result.txt, words_4000.txt, …)
├── files/                          ← book_pdfs/, json/, masked/, text/
├── scripts/                        ← one-off backfills
├── tests/                          ← pytest tests for root pipeline modules
└── Procfile                        ← deploys lexy-app backend
```

---

## 5. Database (Postgres, asyncpg, Alembic)

### Core polymorphic key
`(item_id, item_type)` where `item_type ∈ {word, phrase, grammar_rule}`. This is the join used by every progression / review / analytics row.

### Tables (selected)
| Table | Purpose |
|---|---|
| `users` | UUID, email, password_hash, `settings` (JSONB) |
| `user_word_knowledge` | per (user, item_id, item_type) — `status`, `passive_level`, `active_level`, `times_seen`, `times_used_correctly`, `last_seen`, `notes` |
| `srs_cards` | per (user, item, direction=passive\|active) — `due_date`, `repetitions`, `ease_factor`, `interval` |
| `word_table`, `phrase_table`, `grammar_rule_table` | content tables (display surface lives here) |
| `chat_sessions`, `chat_messages` | free/guided sessions, with `corrections`, `evaluation`, `word_matches` |
| `llm_cache` | prompt-keyed cache, hit_count, expires_at |
| `word_usage_events` | analytics: context (transcript/status_change/srs_review/…), outcome (seen/used/correct/incorrect), metadata |
| `video`, `channel`, `sentence`, `word_to_sentence`, `sentence_to_phrase`, `sentence_to_grammar_rule` | scraper-populated content |
| `book_documents`, `book_pages`, `book_blocks` | uploaded PDFs (OCR tokens stored on block.tokens JSONB) |
| `reading_selections` | LingQ-style multi-token selections with anchors (`[{block_id, token_id, surface}]`) |
| `content_request` | user-submitted channel/video adds, status pending → processing → done/failed |
| `notification` | exists, table populated lazily, **generation logic largely missing** |
| `reading_review` | columns on `reading_selections` (`review_count`, `next_review_at`), driven by `/reading/selections/{id}/review` + `ReadingReviewPage` |

### Notable migrations
- **001** — users / user_word_knowledge / srs_cards baseline
- **005** — phrase_table (with `phrase_type` enum)
- **007** — grammar_rule_table (linked via `applicable_phrase_types`, `applicable_lemmas`)
- **008** — books (documents/pages/blocks)
- **009** — reading_selections
- **013–022** — channel surrogate-key refactor (3-phase: youtube_channel_id string → internal SERIAL id)
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

## 7. API surface (selected)

All v1 routes are bearer-token gated; `/api/search`, `/api/suggest`, `/api/video-sentences`, `/api/word-forms`, `/api/languages`, `/api/categories` are public legacy endpoints.

```
auth         POST /api/v1/auth/register | /login
words        GET  /api/v1/words/by-text?word=&language=
             GET  /api/v1/words/knowledge
             PUT  /api/v1/words/{item_type}/{item_id}/status
             POST /api/v1/words/word/{word_id}/transcript-click
srs          GET  /api/v1/srs/due?language=&limit=
             POST /api/v1/srs/review/{card_id}
chat         POST /api/v1/chat/sessions, /guided-sessions
             POST /api/v1/chat/guided-sessions/{sid}/messages, /complete
books        POST /api/v1/books/upload
             GET  /api/v1/books, /api/v1/books/{doc}/pages, /pages/{n}
             PATCH /api/v1/books/{doc}/blocks/{bid}
             POST /api/v1/books/{doc}/blocks/{bid}/llm-repair
             POST /api/v1/books/{doc}/pages/{n}/batch-llm-repair
reading      GET  /api/v1/books/{doc}/pages/{n}/word-statuses
             POST /api/v1/books/{doc}/selections
             POST /api/v1/reading/translate, /explain
             PATCH/DELETE /api/v1/reading/selections/{sid}
phrases      POST /api/v1/phrases/match
insights     GET  /api/v1/insights/cards, /prep
             POST /api/v1/insights/prep/generate-examples
analytics    GET  /api/v1/analytics/unknown-frequent | learning-frequent | recently-failed | most-interacted
content-req  POST/GET /api/v1/content-requests
recs         GET  /api/v1/recommendations/items | videos | sentences
misc         GET  /api/v1/settings/preferences, /reminders/summary, /notifications/stream (SSE),
                  /videos/{vid}/reading-stats
```

---

## 8. Learning-progression model

The thing to internalise — every other service depends on it.

- **Two directions per item**: `passive` (recognise) and `active` (produce). Each has its own SRS card and its own level (0 → mastered).
- **Status enum** on `user_word_knowledge`: `unknown` → `learning` → `known`. Driven by passive/active level crossings.
- **Auto-promotion** happens inside `progression_service` on every recorded event (transcript click, status change, SRS review correct/incorrect, guided chat target_counted, free chat language_detected use).
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

**`status_marked_known` does NOT touch active levels or the active SRS card.** Manual "Known" is user confidence, not production evidence — `active_delta=0`, `active_srs=None`. The status field is forced to `known` by `word_service.upsert_word_status` (called from the router before `apply_progression`). The rule only advances the passive SRS card. Active mastery is reserved for real production events (`guided_counted`, `free_chat_used_correctly`, `active_review_correct`). Historical note: before 2026-05-18 this rule wrote `active_delta=1, active_srs="correct"` — inflating active mastery on a confidence click. Fixed; if you see a user with active_level > 0 and no production events in `word_usage_events`, that's pre-fix data.

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
- **Two copies of pipeline code** (root vs `src/app/`). Until consolidated, edits go in root.

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

### Backend tests
```bash
cd lexy-app/backend
pytest                              # serial — ~165s for 433 tests
pytest -n auto                      # parallel via pytest-xdist — ~48s (3.4× speedup)
```
Each test user's email is tagged with `PYTEST_XDIST_WORKER` (or `main` when serial)
via `tests/_email_helper.make_test_email()`; the autouse cleanup fixture uses the
same per-worker LIKE pattern, so parallel workers don't trample each other's rows.
Tests that need to look up "a test user" must filter via `cleanup_pattern()`
instead of bare `'test+%@example.com'` — see `test_words.py` / `test_recommendations.py`.

### Root-pipeline tests
```bash
pytest tests/                       # tests for root pipeline modules
```

### Env (`.env` at repo root, also read by backend)
```
DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT
SECRET_KEY                # JWT signing
ANTHROPIC_API_KEY
MOCK_LLM=false            # set true to short-circuit LLM in tests
```

---

## 12. Conventions / rules of the road

- **Never read the real `.env` file.** It holds production secrets (DB password, JWT signing key, Anthropic API key). Use `.env.example` for the schema. If you need to confirm a variable name, grep the source — don't open `.env`.
- **Commit messages never mention AI.** No `Co-Authored-By: Claude` (or any AI tool) trailer, no "🤖 Generated with…" line, no "written by Claude / with AI assistance" phrasing anywhere in the subject or body. Write every commit as the author. (This overrides any default tooling that auto-appends such a trailer.)
- **Use `docs/SUMMARY.md` as the file-level index.** "Where do I look to change X?" is answered there. Update it whenever a file is added, removed, or changes responsibility.
- **Update `docs/TESTS.md` whenever you add, remove, or rename a test.** It tracks coverage, known pre-existing failures, and the xfail audit-hole pins (flip strict=True when the fix lands so the test enforces the new behaviour).
- **Consult and update `docs/SECURITY.md` for any security-relevant change.** It's the living tracker of open/resolved findings and the controls we rely on. See §14 for the read/update triggers and the four fields every finding must carry.
- **All state changes go through `progression_service`.** Don't write directly to `user_word_knowledge` or `srs_cards` from a router.
- **All LLM calls go through `llm_service` and cache via `llm_cache_service`.** Don't instantiate `AsyncAnthropic` ad-hoc.
- **Polymorphic key everywhere:** if you add a new tracked content type, it gets an `item_type`, lives in its own content table (with `display_text` available), and plugs into `user_word_knowledge` / `srs_cards`.
- **Migrations are append-only** — never edit an existing migration once it's been run anywhere.
- **Tests live next to the layer they test** (backend tests under `lexy-app/backend/tests/`, pipeline tests under `tests/`).
- **No new top-level Python files** without a reason — root is already crowded with the pipeline modules and the `src/app/` refactor is in flight.

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
