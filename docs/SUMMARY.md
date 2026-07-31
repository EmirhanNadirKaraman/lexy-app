# SUMMARY.md

File-level index of the codebase. **"Where do I look to change X?"** — answered here. Update when files are added, removed, or change responsibility.

Conventions: each entry is `path — purpose. Touchpoints.` Touchpoints list adjacent files you usually edit together.

---

## Backend — `lexy-app/backend/`

### Entry + infra
| Path | Purpose |
|---|---|
| `main.py` | FastAPI app. Lifespan: pool init, phrase + grammar seeds, resume pending content requests. Mounts all routers. CORS env-driven via `CORS_ORIGINS` (`_parse_cors_origins`). |
| `database.py` | asyncpg pool create/close/get. Single global pool. TLS via `DB_SSL_MODE` (S4: `disable`/`require`/`verify-ca`/`verify-full`): `_resolve_ssl` for the asyncpg pool, `resolve_sslmode` (libpq string, omit-on-unset) reused by Alembic + the scraper. |
| `core/deps.py` | `get_current_user` JWT verification dependency. |
| `core/security.py` | password hashing (bcrypt), `encode_token` / `decode_token`. |
| `models/schemas.py` | All Pydantic request/response models. ~600 lines, one file. |
| `alembic.ini`, `migrations/env.py` | Alembic config. `env.py` builds the URL from `DB_*` env + appends `?sslmode=` via `database.resolve_sslmode` (S4). |
| `migrations/versions/0XX_*.py` | 37 migrations (head `037`), append-only. Schema lives here. |

### Routers (HTTP surface) — `lexy-app/backend/routers/`
| Path | Endpoints | Calls |
|---|---|---|
| `auth.py` | `/auth/register`, `/auth/login` | `auth_service` |
| `words.py` | `/words/by-text`, `/words/knowledge`, `/words/{type}/{id}/status`, `/words/word/{id}/transcript-click` | `word_service`, `progression_service`, `usage_events_service` |
| `srs.py` | `/srs/due`, `/srs/review/{card_id}` | `review_service` |
| `chat.py` | `/chat/sessions`, `/chat/sessions/{id}/messages`, `/chat/guided-sessions`, `/chat/guided-sessions/{id}/messages`, `/complete` | `chat_service`, `guided_chat_service`, `llm_service`, `progression_service` |
| `books.py` | `/books/upload`, `/books`, `/books/{id}`, `/books/{id}/pages`, `/books/{id}/pages/{n}`, `/books/{id}/pages/{n}/image`, `/blocks/{id}` PATCH, `/blocks/{id}/llm-repair`, `/pages/{n}/batch-llm-repair` | `book_service`, `book_llm_service` |
| `reading.py` | `/books/{id}/pages/{n}/word-statuses`, `/books/{id}/selections`, `/books/{id}/pages/{n}/selections`, `/reading/selections/due`, `/reading/selections/{id}` PATCH/DELETE, `/reading/selections/{id}/review`, `/reading/translate`, `/reading/explain` | `reading_service`, `reading_llm_service`, `progression_service`, `book_service` |
| `phrases.py` | `/phrases/match`, `/phrases/seed` | `matcher_service`, `phrase_service` |
| `matcher.py` | `/sentences/match` (shared with phrases) | `matcher_service` |
| `insights.py` | `/insights/cards`, `/insights/prep`, `/insights/prep/generate-examples` | `insights_service`, `llm_service` |
| `analytics.py` | `/analytics/unknown-frequent`, `/learning-frequent`, `/recently-failed`, `/most-interacted` | `usage_events_service` |
| `recommendations.py` | `/recommendations/sentences`, `/videos`, `/items` | `recommendation_service` |
| `playlists.py` | `/playlists/generate` | `playlist_service` |
| `settings.py` | `/settings/preferences` GET/PUT | `settings_service` |
| `reminders.py` | `/reminders/summary` | `reminder_service` |
| `search.py` | `/search`, `/suggest`, `/word-forms`, `/languages`, `/categories`, `/video-sentences` (legacy public, no auth) | `search_service` |
| `videos.py` | `/videos/{id}/reading-stats` | `reading_stats_service` |
| `content_requests.py` | `/content-requests` POST/GET. Spawns `subtitle-scraper/pipeline.py --requests-only` subprocess. | direct SQL + subprocess |
| `notifications.py` | `/notifications/stream` (SSE). Per-row mark-after-yield ordering (disconnect leaves un-yielded rows unseen for re-delivery). LISTEN/NOTIFY refactor deferred (TODO #4b). | direct SQL |
| `word_lists.py` | `/word-lists` POST/GET, `/word-lists/{id}` GET/DELETE, `/word-lists/{id}/export` (text/plain), `/word-lists/{id}/mark-unknown-learning`. `GET /word-lists/{id}` takes optional `limit` (1–1000) and `offset` (≥0), which window **`entries` only** — `total` and `counts` stay whole-list on every page, so the status badges and the mark-learning count keep describing the list rather than the window. Omitting both returns the pre-pagination response unchanged. All routes user-scoped, 404 (not 403) on someone else's id — ownership resolves before the params are read, so paging cannot widen access. | `word_list_service`, `progression_service` |
| `lemma_corrections.py` | `POST /lemma-corrections` (auth + throttle, flag → candidate) + `GET /admin/lemma-corrections` (`require_admin`, queue) + `POST /admin/lemma-corrections/{id}/{accept,reject}` (`require_admin`; accept transactionally upserts `lemma_override`) + `…/{id}/adjudicate` (`require_admin`, read-only dry-run LLM proposal via injected `get_lemma_adjudicator` → 503 if none) — #39 3A/3B/3C. | `lemma_correction_service` |

### Services (business logic) — `lexy-app/backend/services/`
| Path | Owns |
|---|---|
| `progression_service.py` | **Single source of truth** for knowledge-state changes. `_RULES` dict maps event → ProgressionDelta. `apply_progression` is transactional (line 178). `_update_srs` runs SM-2 and now skips active-card creation for grammar_rule (line 268). |
| `review_service.py` | Real SRS implementation. `get_due_cards` joins per-type display table. `submit_answer` maps to `progression_service`. |
| `word_seed_service.py` | German catalog backfill from `data/final_result.txt` column 0 (N5a step 1). Article-strips, keeps clean single tokens, inserts only surfaces absent under `text_norm.normalize_key` with `pos=''` (a real POS would fork rows — `pos` is in the unique key). Never updates existing rows, never touches `phrase_table`. Driven by `scripts/backfill_word_catalog.py`. `inserted` comes from `INSERT ... RETURNING`, so a pre-existing row is never counted as written. |
| `text_norm.py` | `normalize_key` (NFC + strip + `lower()`) and `index_by_key` — the Unicode comparison key for catalog lookups. Exists because the DB is C-collation, where Postgres's `lower()`/`ILIKE` fold ASCII only, so `lower(col) = ANY(python_lowered)` never matched an umlaut-initial word. Uses `lower()` **not** `casefold()`: casefold maps ß→ss and would merge `schließen`/`schliessen` into one ambiguous key. |
| `catalog_resolver.py` | **Shared** surface → catalog row resolution, used by `word_list_service` and `reading_service`. Folds case in Python via `text_norm.normalize_key` (SQL `lower()`/`ILIKE` are ASCII-only under this DB's C collation). Shape-based precedence: multi-token → phrase first, single-token → word first. Phrases match `canonical`, **never** `surface_form` (773 German rows differ). Never first-matches: several candidates → `ambiguous` with `item_id=None`. `resolve_surfaces` (batch) + `resolve_one` (single). |
| `gloss_seed_service.py` | Pre-seeds curated English glosses from `data/words_4000_old.txt` into `llm_cache` under the sentinel model `curated:words_4000_old` (TODO #43 step 2). Words only; skips multi-entry cells, multi-sense and >4-word translations. Makes **no LLM calls**. Driven by `scripts/seed_gloss_cache.py` (dry-run default, `--apply` to write, `RETURNING`-based inserted count). |
| `system_list_seed_service.py` | Seeds the two built-in system lists from `data/final_result.txt` (col 0 → *Top German Words*, col 1 → *German Verb & Phrase Patterns*) via `catalog_resolver`, the same rule user lists use. **Keeps ambiguous and unresolved surfaces** with `item_id=NULL` rather than dropping or first-matching them. Idempotent by constraint: `ON CONFLICT (name) WHERE is_system` for the list, `ON CONFLICT (list_id, lower(surface))` for items — append-only. No LLM calls. Driven by `scripts/seed_system_lists.py` (dry-run default, `--apply`, `RETURNING` counts). |
| `WordListsPage.tsx` | Vocabulary lists UI. Splits the index into a **Built-in lists** section (`is_system`) and *Your lists*: built-ins carry a “Built-in” badge, show no Delete control, and keep Open / export / mark-unknown-learning. `SYSTEM_DISPLAY_NAME` overrides a stored list name for display only — the backend name is the seeding idempotency key. Detail entries are **fetched** a page at a time — `ENTRY_CHUNK` (200) is sent as the backend's `limit`, and “Show more” requests `offset=<entries loaded>` and appends. The footer denominator is `detail.total` (whole-list) while the numerator is `entries.length` (loaded), which is what makes it read “Showing 200 of 5,035” instead of “200 of 200”. Counts, export and mark-learning read the whole-list `total`/`counts`, which every page carries. `showDetail()` is the single funnel for *replacing* the open list and resets loading/error state; `handleShowMore` is the one deliberate exception because it *extends* rather than replaces. Late responses are guarded both ways: a page for a list the user navigated away from is dropped, and a late failure cannot overwrite the new list's error state. |
| `word_list_service.py` | User vocabulary lists (upload/paste → resolve → export). Resolves against **both `word_table` and `phrase_table`** (the latter seeded from `data/final_result.txt`), so a pasted blueprint binds to the same phrase row chat/SRS use. Precedence is shape-based: multi-token → phrase first, single-token → word first; several matches within the preferred type mean `ambiguous` and the fallback is not consulted. Five entry states: known/learning/unknown/**unresolved** / **ambiguous** (never auto-bound). `mark_unknown_as_learning` goes through `progression_service` with the resolved `item_type`; it never writes `user_word_knowledge` or `srs_cards`. Since migration 037 reads are `(user_id = $2 OR is_system)` so **built-in system lists** (owner-less, `is_system=true`) are visible to everyone; writes stay user-only, `delete_list` refuses a system list via its unchanged `user_id` filter, and `mark_unknown_as_learning` suppresses its late-binding UPDATE on shared rows while still applying per-user progression. `mark_unknown_as_learning` is **capped at MAX_LIST_WORDS (500) per call** — each entry costs its own `apply_progression` transaction, so an uncapped call on a 5,035-item built-in list would be thousands of sequential round-trips and could add ~9,600 SRS cards in one click, with no undo. The response carries `remaining`/`capped`; because the call is idempotent, capping degrades into chunking (call again). `get_list` takes optional `limit`/`offset` (also exposed as query params on `GET /word-lists/{id}`) that page **`entries` only** — `total` and `counts` stay whole-list on every page. Omitting both reproduces the pre-pagination response exactly. It shrinks the response, not the query: `_load_entries` still resolves the whole list because counts need it. |
| `word_service.py` | `resolve_word_ids` (Unicode-correct case-insensitive match via `text_norm.normalize_key` — **not** `ILIKE`, which folds ASCII only under this DB's C collation), `lookup_word_by_text` (resolves then enriches; ambiguous on POS, never first-matched), `learn_word_anyway` (**resolves before inserting** so an existing `pos=''`/scraper row is reused instead of forked — `ON CONFLICT (word, language, pos)` cannot see a row under a different `pos`), `get_user_knowledge`. (`upsert_word_status` was deleted 2026-05-19 — `progression_service.apply_progression(..., status_override=...)` is now the single writer.) |
| `chat_service.py` | session/message CRUD + `match_learning_words` (free-chat matching against the user's vocab — words **and** phrases, via `matcher_service.match_sentence_with_ids` for the phrase half). `match_learning_words` folds case in **Python** via `text_norm.normalize_key`, inverting the join (fetch the user's tracked non-known words, filter in Python) — SQL `LOWER()` folds ASCII only under this DB's C collation, so umlaut words earned no free-chat credit. The phrase half matches on spaCy-returned `phrase_id`s and never folds. |
| `guided_chat_service.py` | `get_next_target` (priority: due active → learning without active → random; considers words AND phrases at every tier), `update_progress` (event mapping). |
| `llm_provider.py` | **The only place an LLM client is constructed.** Two backends behind `LLM_PROVIDER`: `AnthropicProvider` (default) and `OpenAICompatibleProvider` (POSTs `{LLM_BASE_URL}/chat/completions` via `httpx`, `response_format=json_schema`, one retry falling back to `json_object`). Any host — built for a GPU desktop over Tailscale, not localhost. `LLMProviderError` carries provider/model/base-url and never the API key (`_redact` scrubs URL userinfo too). `LLMProvider` Protocol — `structured(system, messages, schema, max_tokens) -> dict` + `model_id`. `AnthropicProvider` maps a JSON Schema (`title`/`description` + body) onto a forced single-tool call; `split_schema` strips those two keys so the wire bytes match the pre-seam tool dicts. `LLMProviderError` replaces the old bare `StopIteration`. **No image/multimodal support yet** — both backends pass `messages` through verbatim, so adding it means a neutral image content-block normalizer here (the two wire formats diverge). See `docs/INGESTION_PIPELINE.md` §8. |
| `llm_service.py` | All Claude Haiku calls. tool_use for structured outputs. Cached via `llm_cache_service`. Has `MOCK_LLM=true` mode. `translate_item_gloss` reads the **curated** cache key (model `curated:words_4000_old`) before the model-specific one, so seeded human glosses survive an `LLM_MODEL` switch while genuine LLM output stays model-scoped. It never *writes* curated rows. |
| `llm_cache_service.py` | SHA256(prompt_key+model+params) → `llm_cache` table. TTL or permanent. |
| `book_service.py` | PDF upload, docling+masking ingestion, page/block CRUD, sentence_count, user_text_override. |
| `book_llm_service.py` | Per-block LLM OCR repair. Used by `/blocks/{id}/llm-repair` and batch. |
| `reading_service.py` | Selection CRUD, word-status lookup for page highlighting, `find_catalog_item` (selection → word/phrase id), `record_review` (own SRS schedule), `get_due_selections`. `find_catalog_item` delegates to `catalog_resolver` — Unicode-correct, matches phrase `canonical` (was `surface_form`), and **fails safe on ambiguity** instead of silently first-matching. `get_word_statuses_for_page` folds in Python and keys results with plain `.lower()` to match the frontend's JS `toLowerCase()` lookup (deliberately not NFC — the frontend has no `.normalize()`). |
| `reading_llm_service.py` | `translate_sentence`, `explain_in_context`. Permanent cache. |
| `reading_stats_service.py` | Lemma coverage stats for books/videos. |
| `insights_service.py` | `get_insight_cards` (frequent_unknowns, recent_mistakes), `get_prep_data` (translation + grammar + examples + linked rules). |
| `recommendation_service.py` | Pure scoring functions (score_sentence, score_video, channel_category_multiplier) + DB orchestration. Mixed item types go through `enrich_by_type` (dispatches to per-type enrichers for word/phrase/grammar_rule). `enrich_items` remains the word-only backend. |
| `prioritization_service.py` | `get_prioritized_items` — combines is_due (×4), mistake_recency (×3 decay), freq_rank (×2 linear), is_learning (×1). |
| `usage_events_service.py` | `record_event` (analytics fire-and-forget) + `record_transcript_click_event` (atomic dedup-aware insert) + 4 aggregations. Insight filters include `'transcript'` context. |
| `matcher_service.py` | Async wrapper around `subtitle-scraper/phrase_finder.py`. Imports the scraper via `sys.path.insert` (post-#3, no more `os.chdir`). `match_sentence` runs sync spaCy in a thread pool. Parses per-language (#39 slice 2): German via phrase_finder's resident model, others via `nlp_service`'s cache (`_model_for`, lock-guarded); `match_sentence_with_ids` loads `lemma_override` and threads it into the extractor so chat canonicals are corrected. |
| `phrase_service.py` | `seed_from_blueprint_map`, `enrich_phrases`, phrase_type inference from blueprint. |
| `lemma_correction_service.py` | `create_candidate` (INSERT…ON CONFLICT…report_count+1) + `list_candidates`; `accept_candidate` (transactional: upsert `lemma_override`, flip `accepted`) + `reject_candidate`; `adjudicate_candidate_dry_run` (read-only, injected adjudicator → advisory proposal) + `build_adjudication_input`/`format_adjudication_prompt` + domain exceptions (#39 3A/3B/3C). Accept is the only path from a user signal to `lemma_override`. |
| `grammar_service.py` | `seed_rules` (curated DE list), `get_rules_for_phrase_type`, `get_rules_for_lemma`. |
| `playlist_service.py` | Video playlist generation from target words. Two optimizers behind one signature: `greedy_cover` (default, no deps) and `ilp_cover` (opt-in `algorithm="ilp"`, PuLP/CBC, optimal under `max_videos`). ILP solves **maximum coverage under the cap**, not minimum set cover, so it degrades to a partial playlist instead of going infeasible. `pulp` imports lazily — missing solver → 503, greedy unaffected. No frontend selector yet: API-only. |
| `nlp_service.py` | spaCy wrapper utilities. |
| `auth_service.py` | register, login. |
| `settings_service.py` | get/update preferences from `users.settings` JSONB. Default values centralised here. |
| `reminder_service.py` | Learning reminder summary. |
| `search_service.py` | Full-text video/subtitle search. Corpus `search` resolves the query to `word_id`s / `blueprint_id`s in Python via `text_norm.normalize_key`, then matches `= ANY(...)` — SQL `ILIKE`/`~*` fold ASCII only under this DB's C collation. `_resolve_blueprint_ids` also `re.escape`s the term (the old word-boundary predicate interpolated raw user input into a regex — SECURITY.md S19). No debug `print()` in the request path (seven were removed 2026-07-28). **Autocomplete is fixed too** (migration 036): `_suggest_words` matches a `normalize_key`-folded prefix against the generated `word_table.word_norm` column via `ix_word_table_lang_word_norm` (EXPLAIN-verified index scan), and `_suggest_phrases` matches `phrase_blueprint.lookup_key_norm` with the word-boundary check done in Python — Postgres's `\m`/`\M` are ctype-dependent and never match a non-ASCII token under the C locale. Both escape user input (`_escape_like_prefix`, `_escape_regex`). |

### Tests — `lexy-app/backend/tests/`
See `docs/TESTS.md` for full list, coverage status, and known failures.

---

## Frontend — `lexy-app/frontend/`

### Top level
| Path | Purpose |
|---|---|
| `src/App.tsx` | Router + Layout. Outlet context provides token/prefs/actions to every page. |
| `src/main.tsx` | Vite entry. |
| `src/auth.ts` | `localStorage` token/email helpers. |
| `vite.config.ts` | Dev proxy `/api → :8000`. Vitest + jsdom config. |
| `package.json` | React 19, RR 7, Vite 8, TS 5.9, Vitest. |

### Pages (routed) — `src/App.tsx` declares
| Route | Component | What it shows |
|---|---|---|
| `/` | `HomePage` | Search + PlayerView + Free/Guided chat |
| `/for-you` | `ForYouPage` → `RecommendationsPanel` | Items, videos, sentences + InsightsSection |
| `/playlist` | `PlaylistPage` | Build playlists from target words |
| `/books` | `BooksPage` → `BookLibraryPage` / `BookReaderPage` | Upload, list, read PDFs |
| `/review` | `ReviewPage` → `SRSReviewPage` | SRS due cards. Passive = English-gloss prompt + reveal + self-grade; active = typed German + LLM evaluation via `/srs/review/{id}/produce`. |
| `/add-content` | `AddContentPage` → `ContentRequestPage` | Channel/video requests |
| `/settings` | `SettingsPage` → `SettingsPanel` | Prefs, colours, reps, channels, dark mode |

### Components — `src/components/`
Grouped by feature:

**Search / Player flow**
- `SearchBar.tsx` — multi-term search input
- `PlayerView.tsx` — main result player with tabs
- `YoutubeEmbed.tsx` — iframe with seekTo/pause/play
- `PlayerControls.tsx` — prev/next/replay
- `SubtitleDisplay.tsx` — clickable highlighted text
- `TranscriptPanel.tsx` — sentence list with statuses (has its own test)

**Word interaction**
- `WordStatusPicker.tsx` — modal lookup + status switcher. Offers "Learn this anyway" when the surface isn't in the catalog (W2 / Hole 1); shows a candidate chooser when `/by-text` returns `status='ambiguous'` (W3 / Hole 2).
- `TurnFeedbackChip.tsx` — eval chip in chat

**Chat**
- `ChatWindow.tsx`, `MessageInput.tsx` — generic message UI
- `FreeChatPage.tsx`, `GuidedChatPage.tsx` — session containers
- `TargetCard.tsx`, `SessionSummaryCard.tsx` — guided chat target + summary
- `PrepView.tsx` — pre-guided-chat translation + examples + templates + grammar

**Books / reading**
- `BookLibraryPage.tsx` — list, upload, sort, delete
- `BookReaderPage.tsx` — page view, word highlighting, selection panel, translate row
- `SelectionPanel.tsx` — save custom learning unit from selected tokens
- `SelectionReviewPanel.tsx` — review saved selections per-book. Global session loop lives at `/reading-review` (`ReadingReviewPage.tsx`, shipped #5).
- `ReadingStatsPanel.tsx` — coverage stats

**Recommendations / insights**
- `RecommendationsPanel.tsx` — For You container
- `RecommendationCards.tsx` — item/video/sentence cards
- `InsightsSection.tsx` — frequent_unknowns + recent_mistakes
- `GrammarRulePanel.tsx` — grammar rule detail + add to study

**Other**
- `PlaylistPanel.tsx`, `LoginForm.tsx`, `NotificationToast.tsx`, `ReminderBanner.tsx`, `FollowedChannelsSection.tsx`, `SettingsPanel.tsx`, `ContentRequestPage.tsx`, `SRSReviewPage.tsx`, `icons.tsx`
- `WordListsPage.tsx` — vocabulary lists: paste or upload a `.txt`, see per-word known/learning/unknown/unresolved/ambiguous, mark unknown as learning, download. Route `/lists`, nav entry "Lists".

### Hooks — `src/hooks/`
| Hook | Purpose |
|---|---|
| `useSearch.ts` | Query state + pagination. Takes `language` as a parameter (defaults to `'de'`); caller threads `recLanguage`. |
| `usePreferences.ts` | Fetch + update prefs. Exposes `error`; preserves last-known prefs on failure instead of snapping back to defaults. |
| `useChat.ts`, `useGuidedChat.ts` | Free + guided chat lifecycles. |
| `usePlayerSentences.ts` | Sentence parsing, navigation. |
| `useWordStatus.ts` | Lookup + mark + W3 ambiguous-candidate flow. `recordTranscriptClick` surfaces failures via `console.warn`; backend dedups per `(user, item, sentence_id, UTC day)`. |
| `useWordColors.ts` | Page-level word-status fetch for highlighting. |
| `useNotifications.ts` | SSE consumer. |
| `useReminders.ts`, `useReadingStats.ts`, `useRecommendations.ts`, `useInsights.ts` | Feature-specific data fetchers. |

### API clients — `src/api/`
One file per domain, thin fetch wrappers with `assertOk`:
`auth.ts`, `books.ts`, `chat.ts`, `contentRequests.ts`, `insights.ts`, `playlists.ts`, `reading.ts`, `recommendations.ts`, `reminders.ts`, `search.ts`, `settings.ts`, `srs.ts`, `suggest.ts`, `wordLists.ts`, `words.ts`.

### Types + config + utils
- `src/types/index.ts` — every shared interface (~416 lines, single file).
- `src/config/wordColors.ts` — known/learning/unknown colour scheme.
- `src/utils/sentenceUtils.tsx` — highlightText, renderClickableText, normaliseSentences.
- `src/utils/progressUtils.ts` — formatDueDate, progressDots, PASSIVE_MAX, ACTIVE_MAX.
- `src/utils/recommendationUtils.ts` — formatDuration etc.

---

## Pipeline (root) — in-memory utilities, NOT mounted on FastAPI

Source files at repo root. Used by `subtitle-scraper/` and ad-hoc data prep. **This is the only pipeline tree** — the `src/app/` repackaging that used to shadow it was deleted 2026-07-27 (TODO #17). Tests live in `tests/runtime/`.

| Path | Owns |
|---|---|
| `pipeline.py` | `GermanSubtitlePipeline` orchestrator, `PipelineConfig`, `I1Match`, `parse_srt`. End-to-end SRT → i+1 matches. |
| `subtitle_cleaner.py` | `SubtitleTextCleaner`, `SubtitleFragment` — HTML/formatting removal. |
| `subtitle_merger.py` | `SubtitleMerger`, `MergedSubtitleWindow` — fragment merging, hyphenation. |
| `subtitle_segmenter.py` | `SubtitleSegmenter`, `CandidateUtterance` — sentence segmentation via spaCy. |
| ~~`subtitle_ingestion.py`~~ | **Does not exist.** SRT parsing lives in `pipeline.py` as `parse_srt` (row above). The name came from the deleted `src/app/subtitles/ingestion.py`; an earlier draft of this index listed it as if it were a root module. |
| `utterance_quality_filter.py` | `UtteranceQualityEvaluator` — length, noise, proper-noun filters. |
| `utterance_unit_extractor.py` | `UtteranceUnitExtractor` — spaCy-driven unit extraction. |
| `learning_units.py` | `LearningUnit`, `LearningUnitType` enum, lemmatisation. |
| `user_knowledge.py` | `UserKnowledgeStore`, `KnowledgeState` enum, `find_sole_unknown`, `KnowledgeFilterPolicy`. |
| `word_knowledge.py` | `WordKnowledgeStore` — lemma vocab tracking. |
| `exposure_counter.py` | `QualifiedExposureCounter`, `CountingPolicy` (ALLOW_ALL/DEDUPLICATE_UTTERANCE/DEDUPLICATE_SESSION/DIMINISHING_RETURNS). |
| `exposure_service.py` | `ExposureService` — counter + knowledge store coordination. |
| `pipeline_diagnostics.py` | `PipelineRunDiagnostics` profiling/timing. |
| `onboarding.py` | `OnboardingFlow` — tier-based vocab seeding. |
| `eligibility.py` | `EligibilityFilter` — i+1 readiness. |
| `validate_tier_lemmas.py` | One-shot B1 word list validator. |

### Tests — `tests/`
Pytest tests for pipeline modules. Mostly hermetic (no DB). 207 tests:
`tests/runtime/` (93) covers the root pipeline modules above; `tests/*.py`
(114) covers `subtitle-scraper/`. There is no longer a `src/app/` tree or
tests targeting one — see TODO #17.

---

## Subtitle scraper — `subtitle-scraper/`

| Path | Owns |
|---|---|
| `pipeline.py` | yt-dlp scraper orchestrator. `load_channels` (DB), `populate`, `insert_phrases`, `load_lemma_overrides` (#39 — reads `lemma_override` → patches spaCy lemma errors at phrase-generation time), `_notify_user`, `_mark_request`. Run modes: full, `--requests-only`. |
| `transcript_fetcher.py` | yt-dlp wrapper. JSON3 + WebVTT parsing. Cache under `transcript_cache/`. |
| `phrase_finder.py` | Phrase extraction dispatched by language via `extract_phrases(doc, lang)` / `_LANGUAGE_EXTRACTORS`. German (`extract_german_logic`) loads `data/final_result.txt` (path resolved from `__file__`, no chdir) + spaCy at import. Spanish (`extract_spanish_logic`, #36 slices 1+2) covers finite reflexives, an allowlisted verb+prep set, and clitic-attached reflexive infinitives, with the same output dict shape. `matcher_service.match_sentence` wraps the str in `nlp(...)` before calling the German extractor. |
| `seed_channels.py` | Bootstrap upsert of `seed_data/channels.json` → `channel` table. Idempotent. |
| `backfill_video_channels.py`, `backfill_channel_names.py`, `backfill_categories.py` | One-off backfills. |
| `channel_finder.py` | YouTube subscription / CSV channel discovery. Prints IDs to stdout. |
| `db_ssl.py` | S4 — `sslmode_from_env()` / `connect_kwargs()` resolve `DB_SSL_MODE` to a libpq `sslmode` (reject `prefer`/`allow`; omit on unset/disable). All five `psycopg2.connect()` sites pass `**connect_kwargs()`. Standalone duplicate of `database.resolve_sslmode` (scraper can't import the backend). |
| `language_config.py` | #18 — single source for per-language config (`LANGUAGES` dict: spacy_model, transcript_codes, has_morphology, phrase_extractor) + helpers + derived `MODEL_MAP`/`TRANSCRIPT_CODES`/`NO_MORPH_LANGS`. `pipeline.py` re-exports these as `LANG_*`. Add a language by editing here only. Plain Python (no YAML dep); order is load-bearing for transcript auto-detect. |
| `seed_data/channels.json` | Bundled bootstrap seed for the `channel` table. Read by `seed_channels.py`. |
| `debug_transcript.py` | Single-video transcript debug. |
| `profile_pipeline.py`, `profile_full_pipeline.py` | Profiling. |
| `top-1000-most-subscribed-youtube-channels-in-germany.csv` | Input for `channel_finder.py` CSV mode. |
| `transcript_cache/` | Cached transcripts (gitignored). |

---

## PDF text extraction — `pdf_text_extraction/`

| Path | Owns |
|---|---|
| `runner.py` | `PipelineRunner` orchestrator. `run_document`, `run_batch`. |
| `config.py` | All stage dataclasses: PipelineConfig, DoclingConfig, TATRConfig, MaskingConfig, TextAssemblyConfig, TwoPassConfig, DatabaseConfig. |
| `components/layout_extractor.py` | Docling wrapper. |
| `components/two_pass_extractor.py` | Invisible-text detection (4 rules: blank, char density, word coverage, ghost colour). Thresholds TODO #31. |
| `components/region_masker.py` | White-rectangle masking with IOU merge. |
| `components/text_assembler.py` | Hierarchical text assembly + context stitching. |
| `components/artifact_filter.py` | NER + relevance filtering, ligature fixing. |
| `components/node_scorer.py`, `evidence_gatherer.py` | Scoring helpers. |
| `components/media_cropper.py`, `visualizer.py` | Figure/table cropping + diagnostic images. |
| `table_detectors/{docling,tatr,hybrid}_detector.py` | 3 table detectors. |
| `outputs/{writer,media_json_writer,db_ingester}.py` | Output writers. |
| `models/dto.py`, `models/scored_node.py` | DTOs. |

## Older / alternate PDF pipeline — `masking/`, `postprocessing/`

- `masking/latest_ingest.py` — unified pipeline alternative to `pdf_text_extraction/`.
- `masking/simple_pdf_processor.py` — lightweight OCR.
- `masking/visualize_docling_full.py` — debug visualisation.
- `masking/mask_tables.py` — table detection + masking standalone.
- `postprocessing/script.py` — batch text → `word_occurrences` ingest. Calls `phrase_finder`.
- `postprocessing/db_config.py` — DB credentials (gitignored).

---

## Data + scripts

| Path | Purpose |
|---|---|
| `data/final_result.txt` | **Central vocabulary source, and load-bearing.** 5,156-row TSV: 4,075 headwords + 950 phrase blueprints. Seeds `phrase_table` at backend startup via `main.py` → `matcher_service.get_blueprint_map()` → `phrase_service.seed_from_blueprint_map()`, and read at import by `subtitle-scraper/phrase_finder.py`. Editing it changes the live phrase catalog. |
| `data/PROVENANCE.md` | **Read before using anything in `data/`.** Per-file provenance, licence basis, measured content, and which file is preferred vs redundant. Exists because the directory previously had no attribution and an audit had to infer each file from its byte structure. |
| `data/words_4000_old.txt` | **Metadata enrichment over `final_result.txt`'s headwords, despite the name** — 11-column TSV, 4,095 entries with translations (100%), examples (100%), POS (99%), conjugations (71%), ordered frequency-descending. Joins 1:1 on the headword. `words_4000.txt` is strictly its column 0 and is redundant. |
| `data/b1_unparsed.txt`, `b1_parsed.txt` | B1 word list — unparsed is the entry set (2,840, 815 more than parsed); parsed is a strict subset adding gender + valency. Keep both. |
| `data/known_words.txt`, `verbs.txt` | Reference lists; `known_words.txt` is personal study history and is read by `tests/test_free_chat_progression.py`. |
| `files/book_pdfs/` | 29 German B1 books (PDFs). |
| `files/json/`, `files/masked/`, `files/text/` | PDF pipeline intermediates. |
| `ilp/optimal_set_finder.py` | PuLP ILP — pick minimum books covering target vocab. Standalone CLI, reads word-list files + its own DB config; outputs in same dir. **Still standalone and unchanged** — the playlist feature below re-implements the technique rather than importing this module. |
| `scripts/*.py` | One-off data fixers (verb finder, b1 word finder, known words fixer, etc.). Mostly legacy. |
| ~~`youtube_category_test/main.py`~~ | **Deleted 2026-07-27.** yt-dlp category proof-of-concept, superseded by production code: `subtitle-scraper/pipeline.py` reads `info["categories"]` on every ingest, `backfill_categories.py` fills older rows, and the result feeds `video_category` / `user_video_category`. |

---

## Autoloop — `autoloop/` (Fable ↔ ChatGPT orchestration)

Infrastructure for the autonomous engineering loop — see `docs/AUTOLOOP.md`.
ChatGPT is driven through a real browser (Playwright over CDP against a
dedicated, pre-logged-in Chrome profile), **never the OpenAI API**. Runtime
state in `.autoloop/` (gitignored). **Autoloop v1 (2026-07-30):**
produce-then-review (below) is the ONLY commit path — `cli.py`'s
`_build_orchestrator` always constructs the full collaborator set
(`WorkerRepoManager`, `TaskExecutionStore`, `IntentStore`, a provisioned
`Publisher`); the older authorize-then-produce/change-manifest path was
retired, not fixed (`docs/SECURITY.md` S21). There is now a real
write-capable executor (`implement_executor.py`) alongside the audit — both
run through the same path.

**Worker-isolation hardening (2026-07-31, `docs/AUTOLOOP.md` §4e,
`docs/SECURITY.md` S23-S26):** an adversarial review of the M2 worker
isolation (below) found it was git-configuration isolation only —
filesystem location and task-scope authorization were unaddressed. Closed:
worker repos now live at a required, externally-validated
`config.workers_root` instead of inside the checkout; a task's write scope
(`Task.approved_paths`) is fixed before the writer starts and can never be
widened by anything the executor reports; a failed round's residue is
quarantined rather than silently reused; the attempt-count ceiling
persists before the executor runs, not after a commit. Opened, honestly: a
new filesystem-snapshot escape DETECTOR (`escape_detector.py`) catches an
agent writing outside its worker repo, but detection is not an OS-level
sandbox — recorded as open finding S24, on purpose.

| Path | Purpose |
|---|---|
| `orchestrator.py` | Persisted state machine: ready → submitting → awaiting → executing (+ submission_unconfirmed / **submission_rejected** / needs_user / stopped / failed). **Transport recovery (2026-07-31):** a send whose acceptance is merely unobserved still licenses nothing; a send the browser *disproves* routes to `submission_rejected`, which reconciles for confirmation and, on confirmed absence, permits exactly ONE same-chat resend of the same request id. A second confirmed rejection — or a `ConversationUnusableError` — may rotate once (`policy.max_conversation_rotations`, `browser.project_url` required, no derivation) via submit-then-capture-then-**reconcile**: the new URL binds nothing until the conversation itself confirms it holds the request. Every request carries its own authoritative `conversation_url` + `conversation_epoch`, so a late reply in an abandoned chat can never authorize anything. Failure routing, budgets, review-integrity enforcement. `_dispatch_executor` ALWAYS routes to `_dispatch_task_postcommit` (the produce-then-review commit path) — audit included, via `_resolve_audit_task`'s synthetic per-run `Task` (a stable unit id distinct from the protocol pseudo-id `"audit"`, so a `revise` round resumes the same worker repo instead of forking a new one). The legacy manifest-based authorize-then-produce branch and `_dispatch_git` are REMOVED (S21); a stray `commit`/`commit_and_push`/unbound `push` directive is refused via the ordinary `policy_denied` corrective-reprompt machinery (`legacy_git_path_retired`), never routed to an executor. On success the commit is created immediately (hooks enabled) and gated on structural + re-run-validation checks, never a prior chat approval; ANY failure parks in `needs_user` with the commit left exactly where it is (nothing here can roll it back). **Publisher (Autoloop M2, wired 2026-07-30):** `publisher`/`publisher_url_snapshot` constructor params — `_dispatch_task_push` imports the candidate into the dedicated `Publisher` repo and publishes from there, refusing BEFORE publishing if the main checkout's live `remote.origin.url` no longer matches the provisioned snapshot (naming `reprovision-publisher --confirm`) — see `publisher.py`. **Blockers (2026-07-31):** every `_to_needs_user` park is now classified `kind="task_fatal"` or `kind="loop_fatal"` (default `loop_fatal`, fail-closed — see `docs/AUTOLOOP.md` §9c) and, when `blocker_store` is configured (always true in production), persisted as a `blockers.Blocker`. **Worker-isolation hardening (2026-07-31, `docs/SECURITY.md` S24/S25):** for a non-audit write-capable dispatch, `_prepare_write_capable_worker` requires the primary checkout clean (`primary_checkout_dirty`, loop_fatal) and the worker repo free of residue from a failed prior attempt (quarantines + recreates otherwise), then re-checks `Task.approved_paths` for symlink traversal (`approved_path_symlink_traversal`, task_fatal); `_execute_with_escape_detection` brackets the executor call with `escape_detector`'s before/after checkout snapshot (`checkout_escape_detected`, loop_fatal); the pre-commit gate refuses to commit an `outcome.changed_paths` that leaves `task.approved_paths` (`changed_paths_outside_approved`, task_fatal) BEFORE `commit_and_capture` runs; `attempt_count` increments and persists before the executor is ever called, not in `_finish_postcommit` anymore. See `docs/AUTOLOOP.md` §4e for the full design. |
| `tasks.py`, `seed_tasks.json` | Task registry / graph: stable slug ids, dependencies, derived ready/blocked (never stored), `next_ready()`, cycle detection, atomic `tasks.json` persistence. ChatGPT authorizes work by task id only. `seed_tasks.json` (git-tracked) seeds a fresh registry with `rt-01` (admin-gate the books-import endpoints) whenever `tasks.json` doesn't exist yet — read-only, consulted by `cli._load_tasks`/`next-task`/continuous mode's selection policy. **Blockers (2026-07-31):** `block(task_id, reason)`/`unblock(task_id)` quarantine/restore a task via a new `status="blocked"` + `blocked_reason` field, surfaced as `TaskState.BLOCKED_BY_OPERATOR` (distinct from the dependency-derived `BLOCKED`) so `ready_tasks()`/`next_ready()` skip it with no change to either method. **`Task.approved_paths`, added 2026-07-31** (`docs/SECURITY.md` S25): the EXACT repo-relative paths a task's write-capable dispatch may touch — validated on the way into `add_many` (`_validate_approved_path`: no globs, no `..`, not absolute/home-relative, `TaskGraphError` code `bad_approved_path`/`duplicate_approved_path`), defaulted to `()` for backward compatibility with an old `tasks.json`. `seed_tasks.json`'s `rt-01` now declares its three real paths (`lexy-app/backend/routers/books.py`, `lexy-app/backend/tests/test_books_import_admin.py`, `docs/SECURITY.md`); `_seed_registry` also now correctly threads `validation`/`validation_cwd` through (a pre-existing gap — they were declared in the JSON but silently dropped before this pass). |
| `blockers.py` | **New 2026-07-31.** Persisted operator-facing `Blocker` records (`id`, `task_id`, `kind`, `code`, `question`, `detail`, `phase`, `created_at`, `resolved_at`, `answer`) — one JSON file per blocker under `state_dir/blockers/`, atomic temp-file + `os.replace` writes, a corrupt record RAISES (`StateCorruptError`) rather than reading as absent. `BlockerStore.next_id(task_id)` derives `blk-<task_id>-<NNN>` by scanning the directory (no separate counter to drift). `resolve(id, answer)` is one-way — refuses an unknown or already-resolved id. See `docs/AUTOLOOP.md` §9c. |
| `conversation.py` | `LLMConversation` abstract interface + provider registry — `browser_chatgpt` built in; a Claude.ai/Gemini adapter is one class + `register_provider`. |
| `context.py` | Automatic CONTEXT block per request: review-integrity stamp (request_id/timestamp/head_sha/base_sha/report_sha256) + previous decision/task, roadmap, git summary, changed files, validation summary. |
| `browser/chatgpt.py` | `BrowserChatGPT` (the browser LLMConversation): `attach` / `submit` / `reconcile` / `await_response`. **Optimistic rendering is never treated as submission** — confirmation needs an assistant turn or a reconciliation reload; ambiguity returns UNCONFIRMED and never auto-retries. Realistic composer input (focus + keyboard clear + `insert_text` + content verification + Send-enabled wait), no navigation while awaiting, per-stage bounded timeouts, structured secret-free diagnostics. **2026-07-31:** a fourth result, `REJECTED`, when the network observation *disproves* acceptance (persisted history still outranks it, and a 2xx alone still never confirms); `ConversationUnusableError` for the narrow "this chat loaded and is wedged" case that authorizes a rotation — a page that never reached the conversation stays an ordinary `BrowserError`; and `retarget`/`current_url` so a rotation can move the client to a replacement chat. |
| `browser/playwright_session.py` | The only Playwright code. Lazy import; connects over CDP; never launches a browser or touches login. Also implements the optional **send-observation** capability — a passive `page.on("response")`/`on("requestfailed")` listener over the conversation-send endpoint. It records a status and a path; it issues nothing. |
| `browser/observation.py` | **Added 2026-07-31.** The one signal the DOM cannot carry: whether the browser's own send request succeeded. `SendObservation` (path + status + coarse failure — no headers, cookies, bodies or query strings, so a diagnostics dump cannot leak credentials), a narrow send-endpoint allowlist, and `classify_submission`, which folds one Send click's observations into accepted / rejected / **unknown**. Missing or self-contradictory evidence always degrades to unknown: correlation is temporal (bounded by our own click in a single-actor conversation), not by request id, because reading bodies is off-limits. |
| `browser/session.py`, `browser/selectors.py` | Mockable session protocol; every DOM selector in one dataclass (UI-drift fix point). Send observation and rotation (`retarget`/`current_url`) are OPTIONAL capabilities probed with `getattr`, so the in-memory fakes and any future adapter stay valid — absent them, behaviour is exactly what it was before 2026-07-31. |
| `config_writer.py` | **Added 2026-07-31.** Surgical, atomic rewrite of `[browser].conversation_url` after a rotation, so the next session does not walk back into the chat the loop just escaped. Rewrites one line and copies every other byte (the config is hand-maintained and full of comments); temp-file + `os.replace`; and `assert_untracked` **refuses** a git-tracked path, fail-closed if git cannot be consulted — a loop that can quietly edit tracked files while recovering from a browser fault is worse than a failed heal. |
| `contract.py` | Response contract **v3** (audit/plan/implement/revise/commit/push/commit_and_push/stop/ask_user; task-id work authorization; `reviewed` stamp on git approvals; **required non-empty `commit.paths`**) + strict single-envelope parser (one fenced block, or a rendered/plain object with an optional language label; a second object or trailing text is rejected, never resolved by position) + `verify_review` — coded rejects, never guesses. `TaskSpec.approved_paths` (added 2026-07-31, type/shape-validated only — a list of non-empty strings) lets a `plan` declare a task's write-scope up front; NOT required at the protocol level (would be a breaking wire change to every existing `plan`; `PROTOCOL_VERSION` stays 3) — the real, fail-closed enforcement is downstream in `tasks.py`/`orchestrator.py` instead (see `docs/SECURITY.md` S25). |
| `lock.py` | Single-instance lock per state dir: atomic create, pid/host/start/run-id recorded, live-vs-stale distinction, fail-closed, `unlock`-only recovery (refuses live locks), run-id-guarded release. |
| `manifest.py` | **RETIRED 2026-07-30, kept for its own unit tests only** (docs/SECURITY.md S21/S22) — no production caller. Was: task-owned change manifests, content-hash snapshots before/after each executor run; `verify_commit` refused pre-existing or untouched paths. Two kinds: `executor` (provenance-bound — this half's own tests, and its only callers, are gone) and `adopted` (content-bound by SHA-256, immutable-tree commit via `commit_adopted` — sound, still tested directly, but its only caller, `orchestrator.py`'s `_dispatch_git`, is also gone). See `docs/AUTOLOOP.md` §4. |
| `doctor.py` | Non-destructive preflight: config, state dir, lock, git identity, branch policy, **worker isolation** (throwaway probe repo via the real `WorkerRepoManager`), **controlled hooks directories**, **publisher configuration**, **publisher URL drift** (§4d), CDP, playwright, provider, conversation URL, **active conversation + rotations used/cap + project-URL shape** (2026-07-31 — after a rotation the state, not the config, says which chat is live, and that is the one an operator needs to open), live login/selector check. Never submits. |
| `audit/` | Phase-3 audit executor: `findings` (strict agent contract), `agents` (read-only headless `claude -p` runner), `reconcile` (dedupe/classify/reject speculation+style), `taskgen` (`au-NNN` proposal graph), `markdown` (Markdown-only gate, one dated report), `report`, `executor`. |
| `policy.py` | Deterministic safety layer: directive authorization (git gating + task-graph reference checks), git command whitelist (force push structurally impossible), iteration/failure/parse/denial budgets. |
| `git_gateway.py` | The only git runner — argv subprocess (no shell), policy-validated per call, explicit-refspec push. **The legacy `commit()` method is REMOVED** (2026-07-30, S21: it ran plain `git commit`, gated only on manifest provenance — a pre-commit hook could rewrite approved bytes after the check ran). `commit_adopted` (immutable-tree: `write-tree` → verify → `commit-tree` → `update-ref` CAS, refuses when any commit hook is active) is the primitive that closed the same hole correctly, and is kept — sound, but no production caller anymore either (S22). **Produce-then-review path** (the only live commit path): `commit_and_capture` commits with hooks ENABLED and reads the resulting sha honestly from `rev-parse HEAD` (never predicted), leaving review to `range_diff`/`commit_range_paths` against the immutable commit that already exists; `push_exact` publishes only one already-resolved `<sha>:<dest_ref>` refspec (F2/F5-hardened); `worktree_add`/`worktree_remove`/`worktree_prune`/`branch_exists` support `worktree.py`'s `WorktreeManager`, not what production uses (see `worker_env.py`). `fetch_object(source_path, want_sha)` pulls one already-existing object by literal 40-hex id from a local path (policy-whitelisted with F2-style checks); `__init__`'s optional `env: dict | None = None` lets a gateway run under a scrubbed environment instead of silently inheriting the caller's ambient config — see `worker_env.py`. |
| `worktask.py` | Produce-then-review bookkeeping (Autoloop M1 pass 1): `TaskExecution` (per-task branch/worktree/base-sha/candidate-sha/review-round record), `CommitIntent` (durable pre-commit marker), `TaskExecutionStore`/`IntentStore` (crash-safe JSON, corrupt records raise rather than reading as absent), `reconcile_after_crash` — classifies a post-crash branch tip as `NO_COMMIT`/`RECOVERABLE`/`AMBIGUOUS` from parent linkage + changed-path subset alone (never identity or commit message, which a human commit in the same worktree could coincidentally match — F8). |
| `environment.py` | Snapshot/verify pair (Autoloop M1 pass 1) for what a commit or push actually does that isn't the commit itself: active hooks, `core.hooksPath`, every `url.*.insteadOf` rule, and every remote's url/pushurl/push-refspec. `verify_unchanged` catches drift mid-task (e.g. a dependency postinstall script installing a hook) that pre-commit checks alone would miss. |
| `worktree.py` | Per-task worktree lifecycle: `WorktreeManager.create/remove/list_worktrees`. `task_id` doubles as a path component and a git ref component, so it's validated against `[A-Za-z0-9][A-Za-z0-9._-]*` plus an explicit `..` check before touching either. Relies on git's own refusal to check the same branch out into two worktrees at once as a fail-closed concurrency property, rather than reimplementing it. **Not what production `_build_orchestrator` constructs** (it always uses `worker_env.py`'s `WorkerRepoManager` instead, which does not share `.git` with the main checkout); still directly tested (`test_postcommit_flow.py`, `test_postcommit_primitives.py`) and still a supported `Orchestrator(worktrees=...)` mode. |
| `validation.py` | Shared `SAFE_VALIDATION_BINARIES` allowlist + `run_validation_commands` runner, used by both `audit/executor.py` (pre-commit repo-health summary) and the orchestrator's post-commit re-validation (pass 2a) — one definition so the two call sites can't drift on what's safe to launch. **Owns the validation subprocess's ENVIRONMENT too (2026-07-31):** never inherits `os.environ`, always passes `strip_validation_vars(os.environ)` and overlays a `ValidationEnv` when one is configured, then redacts its values out of the returned summary (which becomes `state.last_validation` → state.json, transcript, blockers, review packet). Note `AuditExecutor._run_validation` is a SEPARATE runner sharing only the binary allowlist — it deliberately gets no credentials. |
| `scripts/seed_validation_db.py` | **Added 2026-08-01** (not under `autoloop/` — it seeds the APP's tables). Deterministic, idempotent synthetic corpus for a validation database: two videos, five sentences, five words + `word_to_sentence` links, all with fixed ids in a reserved `900_000+` range so a second run inserts nothing. Exists because a schema-only database makes ~55 backend tests SKIP themselves ("run the subtitle pipeline first") — and a task graded by a suite that silently opted out of every content-touching test is not validated. Rows are chosen to satisfy specific guards: a lemma with two surface forms in one video, a word whose lemma differs from its surface, a non-ASCII surface, a `word_id` colliding with a real `phrase_id`. **Nothing is copied from any real database**, and it refuses to run against the DB name `.env.example` declares (reusing `validation_env.repo_declared_db_name`). `--verify` re-checks each guard without writing. Deliberately does NOT seed `sich freuen auf` — see its `PHRASE` comment and `docs/AUTOLOOP.md` §4g. |
| `dashboard.py` | **Added 2026-07-31** — read-only live tracker on localhost (`python -m autoloop.dashboard --repo <checkout> --port 8787`). Exists because the audit fans out subagents whose output lands only when each FINISHES, so from outside a working loop and a wedged loop look identical. Reads `.autoloop/` state, the newest committed `docs/AUDIT_*.md` (the app backlog lives there, not in the registry), and `ps` — `live_agents()` parses `Your domain: <x>.` out of the process table, the one thing not already on disk. **It never writes**, and every `git` call goes through `--no-optional-locks`: plain `git status` refreshes and rewrites `.git/index`, so a 2s poll was silently dirtying the repo it observes — and a dirty checkout makes the escape detector refuse the next write-capable task. **Visual encoding reworked 2026-08-01 under the dataviz method:** stages are an ordered progression, so exactly one hue marks the stage running now (categorical slot 1), finished stages recede to secondary ink, and `blocked` is the ONLY state allowed a status colour because it is the only genuine health verdict — the previous version painted `active` with status-good green, the "status colour for a non-status series" anti-pattern. Mark hexes are validated by `validate_palette.js` against the node surface, not the page (light `#2a78d6,#d03b3b` on `#f4f3f0`, dark `#3987e5,#d03b3b` on `#2a2a27`, both ALL PASS; the dark critical WARN at 3.0:1 is relieved by icon+label AND the stage table). Also: legend, table-view twin, keyboard focus parity with hover, `data-theme` toggle beating the OS setting both ways, proportional figures on stat tiles, and no re-render when the payload is unchanged. |
| `validation_env.py` | **Added 2026-07-31** (`docs/SECURITY.md` S27, `docs/AUTOLOOP.md` §4g). The validation-environment boundary: dedicated TEST database credentials reach the post-writer validation subprocess and nothing else. `VALIDATION_ENV_ALLOWLIST` is exactly `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`/`SECRET_KEY` (the brief said `JWT_SECRET_KEY`; this repo reads `SECRET_KEY` — `core/security.py:11` — verified by a clean-clone collection, and no alias is accepted). `validate_validation_env_path` refuses a relative path, a missing/non-regular file, a symlink (before `resolve()`, so a link into the real `.env` is caught as a link), a file not owned by the running user, any group/world permission bit, and any location inside the checkout / state dir / `workers_root` / either publisher path. `parse_validation_env` refuses unknown keys (so it can never become a channel for `ANTHROPIC_API_KEY`), duplicates, malformed lines incl. an `export ` prefix, empty values, missing keys, and secrets under 8 chars (closing redaction-by-length at load time instead). `repo_declared_db_name` reads `.env.example`'s `DB_NAME` — the one exact production marker this repo defines, refused as a `DB_NAME`; deliberately NO host refusal (`localhost` is where a legitimate test DB lives) and no name heuristics. `ValidationEnv` is not a dataclass on purpose (a generated `__repr__` would print the values into the first assertion diff); `.apply()` builds the validation subprocess env, `.redact()` scrubs summaries, `.describe()` yields names only. `strip_validation_vars()` is the removal side, used by `ClaudeCliRunner.run` (BOTH tool sets) and `worker_env()`. Loaded once in `cli._build_orchestrator`, handed only to `ImplementExecutor` and `Orchestrator._run_post_commit_validation`; `doctor` reports the identical checks. |
| `worker_env.py` | Worker-side isolation (Autoloop M2), **wired into production 2026-07-30**: `worker_env()` builds the scrubbed subprocess environment (`GIT_CONFIG_NOSYSTEM=1` — NOT `GIT_CONFIG_SYSTEM=/dev/null`, which does not suppress Apple's compiled-in system gitconfig — plus `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_TERMINAL_PROMPT=0`, SSH/askpass vars removed). `WorkerRepoManager` creates one isolated, no-remote repo per task (`git init` + a one-time local-path `git fetch`, never `git worktree add`, which would share the main checkout's `.git`) — `cli._build_orchestrator` always constructs one at `config.workers_root`/`config.worker_hooks_dir` (2026-07-31: NOT `config.workers_dir`, the old nested-inside-the-checkout default; see `validate_workers_root` below) and it takes precedence over `worktree.py`'s `WorktreeManager` in `_dispatch_task_postcommit`. `verify_worker_isolation(git, expected_hooks_dir)` reports remote/pushurl/insteadOf/followTags/mirror/credential-helper/hook violations — `git` must be built with `env=worker_env()` (`WorkerRepo.gateway(policy)`) or it reflects the CALLING process's ambient config instead; `doctor` runs this against a throwaway probe repo. **`validate_workers_root(workers_root, repo_root, state_dir) -> list[str]`, added 2026-07-31** (Autoloop worker-isolation hardening, `docs/SECURITY.md` S23/S25): refuses a `workers_root` that is unset, relative, or nested beneath the checkout, its `.git` (including — verified against a linked worktree, where `.git` is a POINTER FILE — the real gitdir it resolves to), the state dir, or either publisher path; called from both `cli._build_orchestrator` (refuses execution) and `doctor.run_doctor` (a `fail` check). **`WorkerRepoManager.quarantine(task_id, label) -> Path`, added 2026-07-31:** MOVES (never deletes) a dirty worker repo to a sibling `quarantine/<task_id>-<label>` directory when `Orchestrator._prepare_write_capable_worker` finds residue from a failed prior attempt, so a later round never silently commits content that failed its own validation. See `docs/AUTOLOOP.md` §4c/§4e. |
| `escape_detector.py` | **Added 2026-07-31** (Autoloop worker-isolation hardening, `docs/SECURITY.md` S24, OPEN — detection, not prevention). `enumerate_checkout_paths(git)` — every tracked (regardless of working-tree state), untracked-non-ignored, and ignored path, via `git ls-files` (never `git status`, which omits an unchanged tracked file and collapses an ignored directory to one entry). `snapshot_checkout(repo_root, paths) -> CheckoutSnapshot` — content sha256 / symlink target / executable bit per path, read off the filesystem directly (never through git's object store, which would reflect the INDEX for tracked files, not necessarily the working tree). `diff_snapshots(before, after) -> list[str]` — creation/deletion/content/symlink-target/executable-bit violations, paths only, never contents. `find_symlink_traversal(repo_root, paths) -> list[str]` — for `Task.approved_paths`: refuses a path whose ancestor component (or the leaf itself) already exists on disk as a symlink, since a plain relative-looking string can still write through to outside the repository. Wired into `orchestrator.py`'s `_execute_with_escape_detection` (brackets the write-capable `TaskExecutor.execute()` call, non-audit only) and `_prepare_write_capable_worker`. Does NOT inspect the checkout's own `.git/` internals — scoped to the three `git status` categories over the working tree. See `docs/AUTOLOOP.md` §4e. |
| `publisher.py` | Autoloop M2, **wired into production 2026-07-30**: the only path through which a candidate commit is published. `provision_publisher_repo` idempotently creates a dedicated BARE repo (`state_dir/publisher.git`) with its own empty controlled hooks dir; the remote url is a **provision-time snapshot** (`publisher_url.json`, §4d) — the FIRST call copies it from the main checkout and persists it, every later call re-asserts the PERSISTED value (never a fresh read), so an operator changing `origin` is never picked up silently. `Publisher.import_candidate` fetches a candidate by literal 40-hex sha from a worker's local repo path and verifies it via `read_commit` (never a separate `cat-file -t`, which the policy whitelist would deny); `Publisher.publish` reuses `GitGateway.push_exact` unmodified, passed `expected_url=<snapshot>`. `reprovision_publisher(state_dir, source_git, remote, confirm=True)` is the ONLY function that updates the snapshot — no default makes it callable by accident, and nothing directive-reachable calls it; `python -m autoloop reprovision-publisher --confirm` is its sole caller. `redact_url` strips embedded userinfo before any url reaches `status`/`doctor`/a parked question. `Orchestrator`'s `publisher`/`publisher_url_snapshot` constructor params route `_dispatch_task_push` through it — `cli._build_orchestrator` always constructs and passes both. See `docs/AUTOLOOP.md` §4c/§4d. |
| `state.py`, `transcript.py` | Atomic crash-safe JSON state (schema **v3**: adds `task_execution`, the serialised `TaskExecution` for whichever task is running the produce-then-review commit path — a mismatched version refuses to load rather than migrating), stamped requests; append-only JSONL audit log. **2026-07-31, deliberately NOT a schema bump:** `PendingRequest` gains `conversation_url`/`conversation_epoch`/`resends_used`/`last_send_outcome` and `LoopState` gains `conversation_epoch`/`rotations`/`last_rotation` — all defaulted, so an on-disk v3 session keeps loading. The one field that could be misread is handled explicitly rather than by default: an unbound request is adopted onto the loop's URL only while `rotations == 0` (before any rotation the global URL *is* every request's URL) and raises afterwards instead of guessing. |
| `executor.py` | `TaskExecutor` seam (`execute(directive, task)` → `ExecutionOutcome`); `NullExecutor` reports honestly today. `ExecutionOutcome` carries five produce-then-review fields (`task_branch`/`task_base_sha`/`candidate_sha`/`changed_paths`/`post_commit_validation`), all defaulted so every prior caller is unaffected. |
| `implement_executor.py` | **Added 2026-07-31** — the write-capable `implement`/`revise` counterpart to `audit/`: `ImplementExecutor` runs ONE `Edit`/`Write`-capable `claude -p` subagent (`implement_agent_runner`, built on `audit.agents.ClaudeCliRunner` — its tool set is now a constructor param, defaulting to the audit's read-only pair) against the task's own isolated worker repo, derives `changed_paths` from the worker repo's real `git status --porcelain -z -uall` (never the agent's claim — `GitGateway.dirty_entries_all`/`dirty_paths_all`, new), then re-runs validation. No `.autoloop/` writes, no Markdown report. `cli._build_executor`'s new `_DispatchingExecutor` routes `implement`/`revise`-of-a-real-task here and `audit`/`revise("audit")` to `AuditExecutor` — `orchestrator.py` is unchanged. See `docs/AUTOLOOP.md` §7b. |
| `prompts.py`, `config.py`, `cli.py` | Strict `PromptTemplate` library (+ `audit_kickoff`, `smoke_test`, `postcommit_review`); strict TOML config (`[executor]`, `[audit]` sections; new path properties `workers_dir`/`worker_hooks_dir`/`executions_dir`/`intents_dir`/`seed_tasks_file`/`continuous_fingerprint_file`); `run [--continuous] / status / tasks / next-task / doctor / smoke-browser / pause / resume / unlock / reset / reprovision-publisher` CLI with locking on mutating commands. `_build_orchestrator` always constructs the full produce-then-review collaborator set (see the intro above). `run --continuous` loops the phase machine, resuming a saved non-terminal phase and otherwise auto-selecting a ready task or running one audit per repository fingerprint change (`cli.repo_fingerprint`), sleeping — with ZERO Claude/ChatGPT calls — when neither applies. |
| `tests/` | 729 hermetic tests — no network, no playwright, no live claude CLI (see `docs/TESTS.md`). |
| `prompts.py`, `config.py`, `cli.py` | Strict `PromptTemplate` library (+ `audit_kickoff`, `smoke_test`, `postcommit_review`); strict TOML config (`[executor]`, `[audit]` sections; path properties `workers_dir`/`worker_hooks_dir`/`executions_dir`/`intents_dir`/`blockers_dir`/`seed_tasks_file`/`continuous_fingerprint_file`, plus the **required, absolute** `[paths].workers_root`, 2026-07-31 — see below); `run [--continuous] / status / tasks / next-task / blockers / answer / doctor / smoke-browser / pause / resume / unlock / reset / reprovision-publisher` CLI with locking on mutating commands (`blockers` is read-only/no-lock; `answer` locks). `_build_orchestrator` always constructs the full produce-then-review collaborator set, including a `BlockerStore` (see the intro above), and now REFUSES (`ConfigError`) before doing so if `worker_env.validate_workers_root` reports `config.workers_root` unsafe. `run --continuous` loops the phase machine, resuming a saved non-terminal phase and otherwise auto-selecting a ready task or running one audit per repository fingerprint change (`cli.repo_fingerprint`), sleeping — with ZERO Claude/ChatGPT calls — when neither applies. **Blockers (2026-07-31):** a `task_fatal` `needs_user` park (`_handle_parked_task`) quarantines just that task and clears the session so the loop continues with other READY tasks; a `loop_fatal` park still stops the loop. Exhaustion (no ready task, unchanged fingerprint, >=1 open blocker) prints every open blocker and exits 0 instead of sleeping forever. **`_RESOLUTION_PRECONDITIONS` (worker-isolation hardening, 2026-07-31; +3 same-day follow-up keys):** `answer` re-checks an environmental blocker's condition rather than trusting the operator's text — `git_failure_budget_exhausted` (renamed from the dead `git_failure_budget`), `worker_environment_drift` (now a dedicated `verify_worker_isolation`-based recheck, not the browser/login probes it used to share with `login_expired`), `worker_isolation_violation` (reuses the same recheck), `push_refused_protected` (now actually emitted — see `orchestrator.py` below), `primary_checkout_dirty` (dedicated `_precondition_checkout_clean`, re-runs `GitGateway.is_dirty()`), `checkout_escape_detected` (its OWN unconditional-refusal precondition, `_precondition_checkout_escape_detected` — NOT `is_dirty()`-based: the escape detector's snapshot covers untracked+ignored paths `is_dirty()` cannot see, so a checkout-cleanliness recheck would clear on an escape that touched only an ignored path, e.g. `.autoloop/state.json` itself — found in a second round of review and fixed same-day), `publisher_url_drift`, `push_refused`, `login_expired`, `submission_ambiguous`. Verified exhaustive against every code `orchestrator.py` actually emits by an AST walk (catches a stale key), plus a curated reverse list (catches a missing one — added same-day; the AST walk alone cannot) — both in `test_m1_hardening.py`. |

---

## Config + entry

| Path | Purpose |
|---|---|
| `Procfile` | `web: uvicorn backend.main:app` |
| `.github/workflows/dependency-audit.yml` | The repo's CI (S15). `pip-audit` (backend) + `npm audit` (frontend) on push/PR to main + weekly cron. |
| `.env.example` | Env schema. Real `.env` is secrets — never read it. |
| `.gitignore` | |
| `requirements.txt` | Delegates to backend's requirements. |
| `pytest.ini`, `conftest.py` | Pytest config; conftest adds `src/` to path. |
| `README.md` | Repo-root readme. |
| `CLAUDE.md` | This project's master guide (read first). |
| `docs/TODO.md` | Bugs + tasks, ordered by blocking dependency. |
| `docs/AUTOLOOP_TODO.md` | Open work on the loop itself — the live-progress gap during agent fan-out, the unexplained changeset-publish refusal, execution records stranded by a `workers_root` move, and the self-hosting rollback design. App tasks stay in `docs/TODO.md`; security findings stay in `docs/SECURITY.md`, referenced by id. |
| `docs/WORKFLOW_AUDIT.md` | Full word-learning trace with numbered holes. |
| `services/book_import_service.py` | **Document-package import orchestration (roadmap A2).** `verify → validate → dry-run → persist`, no DB mutation before every gate passes. `list_packages`, `validate_package`, `import_package`, `dry_run_package`. Scope is narrow by design: no reconstruction (A4), segmentation (A5), AI review (A8) or worker invocation (A10). |
| `services/document_package/` | The A2 internals. `contract.py` (versions, element enum, coordinate space — no logic), `issues.py` (fatal/warning/info collector), `loader.py` (discovery, checksums, **path-containment chokepoint**), `validators.py` (individually testable structural validators), `coordinates.py` (the single Docling↔fitz flip), `persistence.py` (`PersistenceBackend` seam + dry-run + the A3 placeholder), `result.py` (`import_result.json`). Imports nothing from `nlp_histo`/Docling/Torch — asserted by a test. |
| `evaluation/` | **Document-ingestion evaluation harness (roadmap A1).** No LLM, no `nlp_histo` import, no Docling/torch. `normalize.py` (the single shared normalizer every offset depends on), `annotation.py` (gold format + round-trip), `metrics.py` (Stage 1 fidelity + Stage 2 boundary/reader), `model.py` (`PredictedDocument` + the `DocumentSource` adapter seam), `adapters.py`, `segmentation.py` (spaCy `de` baseline), `runner.py`, `report.py`, `compare.py`, `baselines.py` (frozen constants), `cli.py`. Run: `python -m evaluation run --label X`. |
| `benchmark/documents/` | Gold corpus for the harness — original German text, three files per document (`.blocks.json` input, `.gold.txt` expected units, `.gold.json` sidecar). See `benchmark/README.md`. The real graded readers are commercial and stay in gitignored `files/`. |
| `docs/INGESTION_PIPELINE.md` | Design for the document-ingestion boundary: offline `nlp-histo` worker → versioned document package → import → reconstruction → `book_sentences` → sentence-by-sentence reader. Package schemas, coordinate conventions, AI-review op contract, evaluation plan, task breakdown. ROADMAP **N7** / TODO **#44**. |
| `docs/TESTS.md` | Test inventory, coverage gaps, known failures. |
| `docs/COMMON_ERRORS.md` | Symptom-first log of errors actually hit here (tooling, tests, build, lint, research). Grep it by the error text before debugging. |
| `docs/SUMMARY.md` | This file. |

---

## How to find things — common questions

| Question | Open |
|---|---|
| Where is the state machine? | `lexy-app/backend/services/progression_service.py` (`_RULES`) |
| Where does a transcript click go? | `routers/words.py:record_transcript_click` → `usage_events_service.record_transcript_click_event` (atomic dedup via unique partial index); only on a new insert does it then call `progression_service.apply_progression("transcript_clicked")`. |
| Where do SRS cards come from? | `progression_service._update_srs` — created on `passive_srs/active_srs="create"` or first correct event |
| Where is the LLM called? | `services/llm_service.py` — all calls go through here, cached via `llm_cache_service` |
| Where is the polymorphic `(item_id, item_type)` join? | `review_service.get_due_cards` is the canonical example; `usage_events_service` analytics too |
| Where do book pages render? | `frontend/src/components/BookReaderPage.tsx`, blocks via `book_service.get_page_detail` |
| Where do free-chat words match against vocab? | `chat_service.match_learning_words` (words via direct SQL; phrases via `matcher_service.match_sentence_with_ids`). |
| Where are reading selections wired into main progression? | `routers/reading.py:156` calls `find_catalog_item` then `apply_progression("status_marked_learning")` |
| Where is `users.settings` JSONB read/written? | `services/settings_service.py` — defaults live here |
| Where do notifications get written? | `subtitle-scraper/pipeline.py:_notify_user` for success (`video_done`, `channel_done`); `_mark_request` emits `request_failed` whenever `status='failed'`. |
| Where is auth enforced? | `core/deps.py:get_current_user` — every router uses it as a Depends |
| What seeds run on startup? | `main.py` lifespan: `phrase_service.seed_from_blueprint_map`, `grammar_service.seed_rules`, resume pending content_requests |
| Where is the ChatGPT engineering loop? | `autoloop/orchestrator.py` (state machine); full doc in `docs/AUTOLOOP.md` |
