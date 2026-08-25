# Schema overview

A lightweight map of the Postgres tables in this project. For everything
Alembic owns, source of truth is the `lexy-app/backend/migrations/versions/`
directory; this document is a plain-text companion. Update it when a migration
changes the shape of a group.

> **Alembic does not own the whole database.** Migration `001` is
> *initial_user_tables* — it starts at the user/auth layer, on top of a content
> schema that already existed. Of the 41 tables live today (plus
> `alembic_version`), **18 are created by no migration in this repo, and by no
> file in this repo** — searched repo-wide for `CREATE TABLE [IF NOT EXISTS] <name>`
> across `*.py`/`*.sql`, 2026-08-25: `word_table`, `phrase_blueprint`,
> `language_table`, `video`, `sentence`, `word_to_sentence`,
> `sentence_to_phrase`, `sentence_to_grammar_rule`, `grammar_rule`,
> `video_category`, `user_video_category`, `video_blacklist`, `word_strength`,
> `leaderboard`, `most_frequent_words`, `user_grammar`, `user_learned_language`,
> `user_stat_table`. They predate Alembic and were created by hand or by the
> scraper. A nineteenth, `word_occurrences`, is also outside Alembic but **is**
> created in-repo — by `postprocessing/script.py`, on its own run, not on any
> app startup path. Practical consequence: **`alembic upgrade head` against an empty
> database does not reproduce this schema** — `006` runs
> `ALTER TABLE video ADD COLUMN ...` and no migration ever creates `video`.
> (`channel` is not an example: migration `015` does create it.)
> Verified 2026-08-25.

**Current as of alembic head `037`** (confirmed against the live database
2026-08-25: `alembic_version.version_num = '037'`, 42 tables in `public`). This is a current-state
map, not a migration log — the table at the bottom lists only high-impact
migrations, and only where the *why* is worth keeping.

No ERD image is generated. `eralchemy` / `schemacrawler` aren't installed and
their Graphviz toolchain is more pain than the diagram is worth here — the
text grouping below is what people actually read when they're new to the repo.

If you do want a diagram later, point `eralchemy` at the production DSN:

```bash
eralchemy -i postgresql://USER:PASS@HOST:5432/german_vocabulary \
          -o docs/schema.svg --exclude llm_cache
```

Adjust the user/secrets handling per your own threat model.

---

## Table groups

### Auth / users
| Table | Purpose | Notes |
|---|---|---|
| `users` | UUID PK, email, password_hash, `settings` JSONB | The fan-out hub for all `ON DELETE CASCADE` private data. |

### Vocabulary / catalog (shared, user-agnostic)
| Table | Purpose |
|---|---|
| `word_table` | (word_id PK, language, word, lemma, pos, frequency, **word_norm**) — the unit of learning for type='word'. `frequency` (migration 032) backs the autocomplete ranking; refreshed by the scraper, not written per-request. `word_norm` (migration 036) is a **generated STORED** ICU-normalized copy of `word` — see §Unicode lookups. **`pos` is part of `UNIQUE (word, language, pos)`** and 38,936 of 38,942 rows hold `pos = ''` (measured 2026-08-25 — six rows now carry a real POS); writing a real POS forks rows rather than enriching them, which is why the catalog backfill deliberately seeds `pos = ''`. |
| `phrase_table` | (phrase_id PK, language, canonical, surface_form, phrase_type) — multi-token learning units. Resolution matches `canonical`, **never** `surface_form` (773 German rows differ). |
| `phrase_blueprint` | (blueprint_id PK, blueprint, lookup_key, **lookup_key_norm**) — the phrase-suggestion source behind `/api/suggest`. German-only; it has **no `language` column**, so callers scope it themselves. `lookup_key_norm` (migration 036) is the generated STORED normalized form. |
| `grammar_rule_table` | (rule_id PK, title, short_explanation, applicable_phrase_types, applicable_lemmas) — passive-only direction. |
| `language_table` | code → display config. |
| `channel`, `video`, `sentence` | Subtitle pipeline output. |
| `word_to_sentence`, `sentence_to_phrase`, `sentence_to_grammar_rule` | Many-to-many bridges between content tables. |
| `llm_cache` | SHA256(prompt_key + model + params) → cached tool_use output, hit_count, expires_at. |
| `lemma_override` | (language, observed_lemma) → corrected_lemma (migration 033). Consulted by the phrase extractor *before* trusting spaCy's lemma, because spaCy's Spanish lemmatizer hard-codes some wrong verbs. `surface_form`/`pos` are reserved for future context-sensitive rows. Written only by an admin accept (#39 slice 3B). |

**None of these tables have a `user_id` column** — they survive any account deletion intact.

Note `lemma_correction_candidate` (migration 034) is deliberately *not* in this
group: it carries a nullable `user_id` and is listed under §Notifications /
errors as a signal table. It is never read by the extractor or matcher.

### User learning state
| Table | Purpose |
|---|---|
| `user_word_knowledge` | Polymorphic per-user-per-item state: `(user_id, item_id, item_type) → (status, passive_level, active_level, times_seen, times_used_correctly, notes, last_seen)`. `item_type ∈ {word, phrase, grammar_rule}`. Single source of truth — only `progression_service` writes. |
| `word_lists` (+ `word_list_items`) | Vocabulary lists — **both user-owned and shared built-in ones**. Paste or upload a list, see per-entry known/learning/unknown/**unresolved**/**ambiguous**, mark unknown as learning, export. Every entry stores its original `surface`; `item_id` is NULL when the surface didn't bind to exactly one catalog row (it resolves against `word_table` *and* `phrase_table`). Shipped 2026-07-27 (migration 035); shared lists added 2026-07-28 (migration 037). **The entry table is `word_list_items`** — this doc once called it `word_list_entries`, which has never existed. See §Word lists: ownership below. |

#### Word lists: ownership (migration 037)

`word_lists.user_id` is **nullable**, paired with
`is_system BOOLEAN NOT NULL DEFAULT false`:

| Kind | `user_id` | `is_system` |
|---|---|---|
| User-owned list | `NOT NULL` | `false` |
| Built-in / system list | `NULL` | `true` |

**The CHECK constraint is the load-bearing part, not the flag.**
`word_lists_owner_ck` enforces
`(is_system AND user_id IS NULL) OR (NOT is_system AND user_id IS NOT NULL)`,
which makes the two dangerous rows *unrepresentable*: a "system" list someone
owns, and an ownerless private list. Reads widened from `user_id = $2` to
`(user_id = $2 OR is_system)`, and that widening is exactly where a private
list could leak — the constraint is what makes it safe regardless of what the
application does.

`uq_word_lists_system_name` is a **partial** unique index on `(name)
WHERE is_system`, so seeding is idempotent by name while users keep their
existing freedom to name their own lists anything, including a name a system
list already uses. The stored name is therefore a key: renaming a seeded list
would fork it on the next seed, which is why the frontend overrides the
display name instead.

Nullable-owner rather than a synthetic system user: a `users` row that must
never authenticate is a standing auth hazard, and its `ON DELETE CASCADE`
would make deleting it silently destroy every built-in list.

Writes stay user-only: `delete_list` still filters on `user_id` alone, so a
system list answers **404**; `mark_unknown_as_learning` suppresses its
late-binding UPDATE on shared rows while still applying per-user progression,
so two users marking the same built-in list never interfere.

**Reading a list is paged.** `GET /word-lists/{id}` takes optional `limit`
(1–1000) and `offset` (≥0) which window **`entries` only** — `total` and
`counts` stay whole-list on every page, because they drive the status badges
and the mark-learning count, which describe the list rather than the window.
Omitting both returns the full pre-pagination response unchanged. Ownership
resolves before the params are read, so paging cannot widen access.

### Unicode lookups under the C locale (migration 036)

The database runs `datcollate=C datctype=C`, so Postgres's `lower()`,
`upper()` and `ILIKE` fold **ASCII only** — `lower('Öl') = 'Öl'` and
`'Öl' ILIKE 'öl'` is false. Most case-insensitive lookups fix this by folding
in Python (`services/text_norm.normalize_key` — NFC + strip + `lower()`) and
matching on ids or equality in SQL.

Autocomplete cannot: it fires per keystroke and needs an **indexed prefix**
match. So the normalization is persisted as generated STORED columns whose
expression reproduces `normalize_key` exactly:

```sql
word_norm       = lower(btrim(normalize(word,       NFC)) COLLATE "und-x-icu")
lookup_key_norm = lower(btrim(normalize(lookup_key, NFC)) COLLATE "und-x-icu")
```

- `word_table.word_norm` is backed by `ix_word_table_lang_word_norm` on
  `(language, word_norm text_pattern_ops)` — `text_pattern_ops` keeps
  `LIKE 'x%'` index-usable regardless of database collation.
- `phrase_blueprint.lookup_key_norm` has **no index of its own**; it exists so
  `_suggest_phrases` stops folding with `~*`.
- **`ß` is deliberately preserved** — ICU `lower()` maps `Straße` → `straße`,
  not `strasse`, matching `normalize_key`'s `lower()` rather than `casefold()`.
  German `word_table` holds `schließen` *and* `schliessen` as distinct rows;
  folding ß→ss would merge them.
- **Requires an ICU-enabled Postgres.** `"und-x-icu"` is built in; on a build
  without ICU the `ALTER TABLE` fails outright, deliberately — a silent
  fallback to C-locale `lower()` would reintroduce the bug.
- Generated rather than application-maintained because three code paths insert
  into `word_table` (`word_seed_service`, `word_service`,
  `subtitle-scraper/pipeline.py` in a separate process); a column any of them
  could forget would silently make words unsearchable.

### SRS
| Table | Purpose |
|---|---|
| `srs_cards` | Polymorphic per-(user, item, direction). Direction is `passive` or `active`. SM-2 fields: `due_date`, `interval_days`, `ease_factor`, `repetitions`. Grammar rules: passive only. Joined to `user_word_knowledge` via the same polymorphic key. |

### Reading / books
| Table | Purpose |
|---|---|
| `book_documents` | Uploaded PDFs (UUID PK, user_id, title, total_pages, language). |
| `book_pages` | Per-page metadata + raw text + sentence_count. |
| `book_blocks` | Server-managed text blocks per page, with `tokens` JSONB + `display_text`. |
| `reading_selections` | LingQ-style multi-token selections — `anchors: [{block_id, token_id, surface}]`, `next_review_at`, `review_count`. Parallel review schedule to `srs_cards` (see §"Dual schedule" in `CLAUDE.md`). |

### Chat
| Table | Purpose |
|---|---|
| `chat_sessions` | Free or guided session container. Carries `language` (migration 031) so chat is not German-only; the column is nullable and readers fall back to `"de"` for pre-migration rows. |
| `chat_messages` | Per-turn (user vs assistant), with `corrections`, `evaluation`, `word_matches` JSONB. |

### Recommendations / preferences
| Table | Purpose |
|---|---|
| `user_channel_preference` | (user_id, youtube_channel_id, preference_kind ∈ {followed, liked, disliked}) — relational replacement for the old `users.settings` JSONB arrays (migration 027 / T1.4). |
| Other prefs | Still in `users.settings` JSONB (theme, reps thresholds, reminders, color overrides, channel name display cache). |

### Notifications / errors
| Table | Purpose |
|---|---|
| `notification` | Per-user event queue. Used by the SSE handler in `routers/notifications.py`. Polling-based today; LISTEN/NOTIFY refactor is deferred (T2.2). |
| `client_error_log` | W7 frontend crash sink. `user_id` is nullable (auth-optional) and becomes NULL on account deletion (signal preserved, anonymised). |
| `lemma_correction_candidate` | Community-flag inbox for wrong lemmas (migration 034, #39 slice 3A). Pending rows dedup cross-user on `(language, surface_form, observed_lemma, suggested_lemma, context_text)` and bump `report_count`; `user_id` is deliberately outside that key and is SET NULL on deletion, so the signal outlives the reporter. Promotion into `lemma_override` requires a human admin accept. |

### Content requests
| Table | Purpose |
|---|---|
| `content_request` | User-submitted channel/video adds. Uniqueness is per-user (`(user_id, request_type, content_id)`, migration 029); two users requesting the same channel each get their own row + their own notifications. `user_id` becomes NULL on account deletion (audit signal preserved, anonymised). |

### Usage analytics
| Table | Purpose |
|---|---|
| `word_usage_events` | Per-event polymorphic stream: `(user_id, item_id, item_type, context, outcome, metadata, created_at)`. Used by recommendation ranking and the "Keeps coming up" insight card. Transcript-click dedup via the unique partial index `uq_word_usage_events_transcript_dedup` on `(user_id, item_id, item_type, sentence_id, event_day)` (migration 026 / T1.1). |

---

## Key relationships (text)

```
users (user_id PK)
  ├── user_word_knowledge  (user_id FK, item_id, item_type)   [polymorphic]
  ├── srs_cards            (user_id FK, item_id, item_type, direction)
  ├── word_usage_events    (user_id FK, item_id, item_type, context, outcome)
  ├── chat_sessions        (user_id FK)
  │     └── chat_messages  (session_id FK)
  ├── book_documents       (user_id FK)
  │     ├── book_pages       (doc_id FK)
  │     │     └── book_blocks  (page_id FK)
  │     └── reading_selections (user_id FK, doc_id FK)
  ├── notification           (user_id FK)
  ├── user_channel_preference (user_id FK, youtube_channel_id, preference_kind)
  ├── word_lists             (NULLABLE user_id FK, language, is_system)
  │     │                     user list: user_id NOT NULL, is_system false
  │     │                     built-in : user_id NULL,     is_system true  → owned by nobody,
  │     │                                                                    readable by everyone
  │     └── word_list_items   (list_id FK, surface, nullable item_id, item_type)
  ├── content_request        (user_id FK, ON DELETE SET NULL — anonymised, not deleted)
  └── client_error_log       (user_id FK, ON DELETE SET NULL — anonymised, not deleted)

word_table     (word_id PK)
  ↑ polymorphic join via (item_id, item_type='word')

phrase_table   (phrase_id PK)
  ↑ polymorphic join via (item_id, item_type='phrase')

grammar_rule_table (rule_id PK)
  ↑ polymorphic join via (item_id, item_type='grammar_rule')   [passive direction only]

channel ─┬── video ──── sentence
                          ├── word_to_sentence  (word_id FK)
                          ├── sentence_to_phrase  (phrase_id FK)
                          └── sentence_to_grammar_rule  (rule_id FK)
```

The polymorphic `(item_id, item_type)` shape lets `user_word_knowledge` and
`srs_cards` track every kind of learning unit through the same rows. The
SERIAL primary keys in `word_table`, `phrase_table`, and `grammar_rule_table`
can collide as integers — always pair `item_id` with `item_type` when
joining. (See `recommendation_service.enrich_by_type` for the canonical
multi-type dispatch.)

---

## Account deletion behaviour

Implemented by `DELETE /api/v1/account` (router: `routers/account.py`).

- A single statement: `DELETE FROM users WHERE user_id = $1::uuid`.
- FK declarations carry the fan-out:
  - **CASCADE** (removed atomically): `user_word_knowledge`, `srs_cards`,
    `chat_sessions` (+ `chat_messages`), `word_lists` (+ `word_list_items`),
    `word_usage_events`, `book_documents` (+ `book_pages` + `book_blocks`),
    `reading_selections`, `notification`, `user_channel_preference`.
  - **SET NULL** (kept, anonymised): `content_request`, `client_error_log`, `lemma_correction_candidate` (`user_id` + `reviewed_by` — the community correction signal survives the reporter's deletion).
- **Shared catalog tables are untouched** — `word_table`, `phrase_table`,
  `grammar_rule_table`, `language_table`, `channel`, `video`, `sentence`,
  `llm_cache`, the `*_to_*` bridge tables.

See `docs/PRIVACY.md` for the user-facing privacy policy companion + the
remaining App-Store compliance checklist.

---

## Recent migration history (high-impact only)

| # | What changed |
|---|---|
| 023 | `notification` table — per-user event queue for the SSE stream. |
| 024 | Phrase/grammar polymorphic key consolidation. |
| 025 | `book_blocks.tokens` JSONB → per-block stable `token_id`s. `reading_selections.anchors` references these ids with **no FK**, and the backfill mints fresh UUIDs on every run — so `downgrade()` refuses while any anchor still points at a `token_id` (db-02). |
| 026 | Transcript-click dedup — `word_usage_events.sentence_id` + stored `event_day` generated col + unique partial index (T1.1 / Hole 3 + Hole 4). |
| 027 | `user_channel_preference` table — moves channel followed/liked/disliked out of `users.settings` JSONB into a relational shape (T1.4). |
| 028 | `client_error_log` table — W7 backend sink for the frontend ErrorBoundary. |
| 029 | `content_request` uniqueness scoped per user — the old global unique key meant one user's request blocked everyone else's for the same channel/video. |
| 030 | Seeds Spanish into `language_table` (second-language plan, Stage 0). Idempotent. |
| 031 | `chat_sessions.language` (nullable) — free/guided chat carries its target language instead of assuming German. Nullable so pre-existing rows keep working; readers fall back to `"de"` (closes Hole 20). |
| 032 | `word_table.frequency` INT + functional prefix index on `(language, lower(word))` — backs the frequency-ranked autocomplete (#38). Backfilled from `word_to_sentence` counts; the scraper refreshes it via `recompute_word_frequencies()`. |
| 033 | `lemma_override` table — curated corrections consulted before trusting spaCy's lemma (#39 slice 1). v1 keys on `(language, observed_lemma)`; `surface_form`/`pos` reserved for context-sensitive rows. |
| 034 | `lemma_correction_candidate` table — the community-signal inbox (#39 slice 3A). A signal table only: never read by the extractor or matcher, and never mutates `lemma_override` without a human accept. |
| 035 | `word_lists.language`, `word_list_items.surface` NOT NULL, `item_id` made nullable, and `UNIQUE (list_id, item_id, item_type)` replaced by a unique index on `(list_id, lower(surface))`. Makes the dormant 001 tables usable: a NULL `item_id` is how an unresolved or ambiguous surface is stored instead of being dropped. The old constraint could not dedupe those (NULLs are distinct in Postgres) and would reject two case-variants resolving to the same `word_id`. |
| 036 | `word_table.word_norm` + `phrase_blueprint.lookup_key_norm` — generated STORED ICU-normalized columns, plus `ix_word_table_lang_word_norm`. Makes autocomplete fold Unicode correctly under this C-locale database. See §Unicode lookups. |
| **037 (head)** | `word_lists.user_id` made **nullable** + `is_system BOOLEAN NOT NULL DEFAULT false` + the `word_lists_owner_ck` CHECK pairing them + the partial unique index `uq_word_lists_system_name`. Opens the shape for shared built-in lists; **creates none** — seeding is a separate reviewed script. See §Word lists: ownership. |

There is no migration for the orphan SRS cleanup or the active-card backfill
— both are pure operational scripts under `scripts/` driven by services in
`backend/services/`. See:
- `scripts/backfill_missing_active_srs.py` (W6)
- `scripts/cleanup_orphan_srs_cards.py` (Hole 10, this pass)

---

## Conventions

- **Polymorphic key everywhere:** any new tracked content type must live in
  its own content table (with a SERIAL PK + a `display_text` field) and plug
  into `user_word_knowledge` / `srs_cards` via `(item_id, item_type)`.
- **Migrations are append-only.** Never edit an existing migration once it
  has been run anywhere. Add a new one.
- **`progression_service` is the only writer to `user_word_knowledge`.**
  Routers and other services must call `apply_progression(...)`.
- **`llm_cache` is the only place LLM responses go on the way out.** Cached
  via `llm_cache_service` keyed by SHA256 of the prompt + model + params.
