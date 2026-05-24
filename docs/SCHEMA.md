# Schema overview

A lightweight map of the Postgres tables in this project. Source of truth is
the `lexy-app/backend/migrations/versions/` directory; this document is a
plain-text companion. Update it when a migration changes the shape of a group.

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
| `word_table` | (word_id PK, language, word, lemma, pos) — the unit of learning for type='word'. |
| `phrase_table` | (phrase_id PK, language, canonical, surface_form, phrase_type) — multi-token learning units. |
| `grammar_rule_table` | (rule_id PK, title, short_explanation, applicable_phrase_types, applicable_lemmas) — passive-only direction. |
| `language_table` | code → display config. |
| `channel`, `video`, `sentence` | Subtitle pipeline output. |
| `word_to_sentence`, `sentence_to_phrase`, `sentence_to_grammar_rule` | Many-to-many bridges between content tables. |
| `llm_cache` | SHA256(prompt_key + model + params) → cached tool_use output, hit_count, expires_at. |

**None of these tables have a `user_id` column** — they survive any account deletion intact.

### User learning state
| Table | Purpose |
|---|---|
| `user_word_knowledge` | Polymorphic per-user-per-item state: `(user_id, item_id, item_type) → (status, passive_level, active_level, times_seen, times_used_correctly, notes, last_seen)`. `item_type ∈ {word, phrase, grammar_rule}`. Single source of truth — only `progression_service` writes. |
| `word_lists` (+ `word_list_entries`) | Saved word lists (early feature). |

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
| `chat_sessions` | Free or guided session container. |
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
  ├── word_lists             (user_id FK)
  │     └── word_list_entries (list_id FK)
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
    `chat_sessions` (+ `chat_messages`), `word_lists` (+ `word_list_entries`),
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
| 025 | `book_blocks.tokens` JSONB → per-block stable `token_id`s. |
| 026 | Transcript-click dedup — `word_usage_events.sentence_id` + stored `event_day` generated col + unique partial index (T1.1 / Hole 3 + Hole 4). |
| 027 | `user_channel_preference` table — moves channel followed/liked/disliked out of `users.settings` JSONB into a relational shape (T1.4). |
| 028 | `client_error_log` table — W7 backend sink for the frontend ErrorBoundary. |

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
