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
| `migrations/versions/0XX_*.py` | 25 migrations, append-only. Schema lives here. |

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
| `lemma_corrections.py` | `POST /lemma-corrections` (auth + throttle, flag → candidate) + `GET /admin/lemma-corrections` (`require_admin`, queue) + `POST /admin/lemma-corrections/{id}/{accept,reject}` (`require_admin`; accept transactionally upserts `lemma_override`) + `…/{id}/adjudicate` (`require_admin`, read-only dry-run LLM proposal via injected `get_lemma_adjudicator` → 503 if none) — #39 3A/3B/3C. | `lemma_correction_service` |

### Services (business logic) — `lexy-app/backend/services/`
| Path | Owns |
|---|---|
| `progression_service.py` | **Single source of truth** for knowledge-state changes. `_RULES` dict maps event → ProgressionDelta. `apply_progression` is transactional (line 178). `_update_srs` runs SM-2 and now skips active-card creation for grammar_rule (line 268). |
| `review_service.py` | Real SRS implementation. `get_due_cards` joins per-type display table. `submit_answer` maps to `progression_service`. |
| `word_service.py` | `lookup_word_by_text` (ILIKE on word_table; ambiguous on POS), `get_user_knowledge`. (`upsert_word_status` was deleted 2026-05-19 — `progression_service.apply_progression(..., status_override=...)` is now the single writer.) |
| `chat_service.py` | session/message CRUD + `match_learning_words` (free-chat matching against the user's vocab — words **and** phrases, via `matcher_service.match_sentence_with_ids` for the phrase half). |
| `guided_chat_service.py` | `get_next_target` (priority: due active → learning without active → random; considers words AND phrases at every tier), `update_progress` (event mapping). |
| `llm_service.py` | All Claude Haiku calls. tool_use for structured outputs. Cached via `llm_cache_service`. Has `MOCK_LLM=true` mode. |
| `llm_cache_service.py` | SHA256(prompt_key+model+params) → `llm_cache` table. TTL or permanent. |
| `book_service.py` | PDF upload, docling+masking ingestion, page/block CRUD, sentence_count, user_text_override. |
| `book_llm_service.py` | Per-block LLM OCR repair. Used by `/blocks/{id}/llm-repair` and batch. |
| `reading_service.py` | Selection CRUD, word-status lookup for page highlighting, `find_catalog_item` (selection → word/phrase id), `record_review` (own SRS schedule), `get_due_selections`. |
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
| `playlist_service.py` | Video playlist generation from target words. |
| `nlp_service.py` | spaCy wrapper utilities. |
| `auth_service.py` | register, login. |
| `settings_service.py` | get/update preferences from `users.settings` JSONB. Default values centralised here. |
| `reminder_service.py` | Learning reminder summary. |
| `search_service.py` | Full-text video/subtitle search. |

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
`auth.ts`, `books.ts`, `chat.ts`, `contentRequests.ts`, `insights.ts`, `playlists.ts`, `reading.ts`, `recommendations.ts`, `reminders.ts`, `search.ts`, `settings.ts`, `srs.ts`, `suggest.ts`, `words.ts`.

### Types + config + utils
- `src/types/index.ts` — every shared interface (~416 lines, single file).
- `src/config/wordColors.ts` — known/learning/unknown colour scheme.
- `src/utils/sentenceUtils.tsx` — highlightText, renderClickableText, normaliseSentences.
- `src/utils/progressUtils.ts` — formatDueDate, progressDots, PASSIVE_MAX, ACTIVE_MAX.
- `src/utils/recommendationUtils.ts` — formatDuration etc.

---

## Pipeline (root) — in-memory utilities, NOT mounted on FastAPI

Source files at repo root. Used by `subtitle-scraper/` and ad-hoc data prep. A refactored copy lives in `src/app/` (orphan — TODO #17).

| Path | Owns |
|---|---|
| `pipeline.py` | `GermanSubtitlePipeline` orchestrator, `PipelineConfig`, `I1Match`, `parse_srt`. End-to-end SRT → i+1 matches. |
| `subtitle_cleaner.py` | `SubtitleTextCleaner`, `SubtitleFragment` — HTML/formatting removal. |
| `subtitle_merger.py` | `SubtitleMerger`, `MergedSubtitleWindow` — fragment merging, hyphenation. |
| `subtitle_segmenter.py` | `SubtitleSegmenter`, `CandidateUtterance` — sentence segmentation via spaCy. |
| ~~`subtitle_ingestion.py`~~ | **Does not exist at root.** SRT parsing lives in `pipeline.py` as `parse_srt` (row above). The refactor split it out as `src/app/subtitles/ingestion.py`, which is where the name came from. |
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

### `src/app/` (orphan refactor, not wired)
Same shapes as above, reorganised under `src/app/{subtitles,pipeline,extraction,learning,exposure}/`. Don't edit unless consolidating per TODO #17.

### Tests — `tests/`
Pytest tests for pipeline modules. Mostly hermetic (no DB).

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
| `data/final_result.txt` | German verb dictionary used by `phrase_finder`. |
| `data/words_4000.txt`, `b1_parsed.txt`, `known_words.txt`, `verbs.txt` | Reference word lists. |
| `files/book_pdfs/` | 29 German B1 books (PDFs). |
| `files/json/`, `files/masked/`, `files/text/` | PDF pipeline intermediates. |
| `ilp/optimal_set_finder.py` | PuLP ILP — pick minimum books covering target vocab. Outputs in same dir. |
| `scripts/*.py` | One-off data fixers (verb finder, b1 word finder, known words fixer, etc.). Mostly legacy. |
| `youtube_category_test/main.py` | yt-dlp category proof-of-concept. |

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
| `docs/WORKFLOW_AUDIT.md` | Full word-learning trace with numbered holes. |
| `docs/TESTS.md` | Test inventory, coverage gaps, known failures. |
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
