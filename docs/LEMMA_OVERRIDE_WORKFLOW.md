# Lemma override workflow (#39)

How a wrong spaCy lemma gets corrected — from a user noticing it, to a row that
actually changes extraction. Companion to the `lemma_override` table (migration
033) and `lemma_correction_candidate` (migration 034).

---

## Problem

The Spanish phrase extractor builds canonical phrases from spaCy lemmas
(`acordar` + `se` → `acordarse`). `es_core_news_sm`'s lemmatizer hard-codes some
wrong lemmas (`ducha`→`duchaber`, `ducho`→`duchir`) — identical across model
sizes, so a bigger model doesn't fix it. The deterministic fix is the
**`lemma_override`** table: a curated map the extractor + matcher consult before
trusting `token.lemma_`. Slice 1 seeded the known `duchar` errors by hand.

The open question slice 3 answers: **how do new wrong lemmas get discovered and
corrected at scale, without a hand-audit of the whole corpus?** Learners reading
Spanish will notice "that's not a word" long before we will. We want their
signal — but we must not let it directly rewrite extraction.

## Two tables, two trust levels

| Table | Trust | Who writes it | Effect on extraction |
|---|---|---|---|
| `lemma_correction_candidate` (034) | **signal** — "someone thinks this is wrong" | any authenticated user (via the flag endpoint) | **none** — never read by the extractor/matcher |
| `lemma_override` (033) | **authority** — "this is the correct lemma" | admin / LLM / curated review only | consulted by the scraper + backend matcher at extraction time |

The candidate table is an **inbox of suspicions**. The override table is the
**adjudicated truth**. Slice 3A (this slice) builds the inbox. Promotion from
inbox → truth is slices 3B/3C.

## Why user flags are signals, not authority

Lemma correctness is **objective, not a vote**: `duchar` is simply right; a
quorum of learners agreeing on `duchaber` doesn't make it correct. And our users
are *learners* — by definition the least-qualified to adjudicate lemmatization,
and most don't know what a "lemma" is. So:

- **Raw flags never mutate `lemma_override`.** There is no code path from the
  flag endpoint to the override table. (Regression-guarded: a test asserts a
  `POST /lemma-corrections` leaves `lemma_override` byte-for-byte unchanged.)
- Flags are **surfacing**, not **deciding**. The crowd is good at spotting
  oddities ("this looks wrong") and bad at resolving them ("the lemma is X").
  We use them for the first and reserve the second for an authority.
- A flag with a `suggested_lemma` is a *hint* for the reviewer, not an
  instruction. The reviewer (admin or LLM) decides.

## Candidate lifecycle

```
  user flags a bad lemma
          │
          ▼
  POST /api/v1/lemma-corrections   (auth required, per-user throttle)
          │
          ▼
  lemma_correction_candidate  status='pending'
     • new suspicion        → INSERT (report_count = 1)
     • already-pending dup   → report_count += 1   (no duplicate row)
          │
          │   (slice 3B — NOT in 3A)
          ▼
  admin / LLM review:  GET /api/v1/admin/lemma-corrections?status=pending
          │
     ┌────┴─────┬───────────┐
     ▼          ▼           ▼
  accepted   rejected    merged
     │
     │   promotion writes a NEW lemma_override row (source='user_flag_reviewed')
     ▼
  lemma_override  status='active'   → extraction now corrected
```

Statuses: `pending` (default) · `accepted` / `rejected` / `merged` (set by a
reviewer in 3B). `source`: `user_flag` (3A) · `llm` / `admin` / `import` (later).

### Dedup key — deliberately cross-user

The pending-uniqueness key is
`(language, surface_form, observed_lemma, suggested_lemma, context_text)` —
**`user_id` is intentionally NOT part of it.** Two different users flagging the
same wrong lemma should land on **one** candidate with `report_count = 2`, not
two rows: that's the whole point of a community signal (report_count is the
"how many people noticed" strength signal a reviewer ranks by). Do not add
`user_id` to the dedup key thinking it's "more correct" — it would fragment the
signal.

`user_id` is recorded (nullable, `ON DELETE SET NULL`) as the *first* reporter
for audit, and survives the reporter deleting their account — the correction
signal is community data, like a crash report (`client_error_log`).

### `''` vs NULL for `suggested_lemma` / `context_text`

Both store **empty string `''` = "absent"**, not NULL (`NOT NULL DEFAULT ''`).
This keeps the partial-unique dedup index on **plain columns** instead of
`COALESCE(...)` expressions (which make `INSERT … ON CONFLICT` inference awkward).
The Pydantic schema accepts `null`/missing and normalizes blank → `''` before
SQL, so callers don't see the convention. The normalizer is a
**`model_validator(mode="after")`**, not a `field_validator`: a field_validator
does NOT run when a field uses its default, so a *missing* optional would reach
the `NOT NULL` column as `None` and error — the model_validator runs regardless
of how the field arrived (provided, null, or defaulted). Consequence: a flag *with* a suggested
lemma and a flag *without* one are **different** candidates (different key) — the
suggestion is part of the suspicion's identity.

## What slices 3A + 3B ship

**3A — the inbox (signal):**
- `lemma_correction_candidate` table (034).
- `POST /api/v1/lemma-corrections` — authenticated; validates + caps input;
  creates a pending candidate or bumps `report_count` on a pending duplicate;
  per-user throttle (30/hour). **Never** touches `lemma_override`.
- `GET /api/v1/admin/lemma-corrections?status=pending` — `require_admin`; lists
  newest pending candidates (capped). Read-only.

**3B — the human-gated promotion (signal → authority):**
- `POST /api/v1/admin/lemma-corrections/{id}/accept` — `require_admin`,
  **transactional**: locks the candidate (`FOR UPDATE`), requires it `pending`
  (else 409), picks the corrected lemma (request `corrected_lemma` → else the
  candidate's `suggested_lemma` → else **400** `nothing_to_promote`), **upserts**
  a context-free `lemma_override` (`source='user_flag_reviewed'`; `ON CONFLICT`
  on the partial unique index updates an existing row rather than duplicating),
  and flips the candidate to `accepted` (+`reviewed_by`/`reviewed_at`/note). An
  override-write failure rolls back the whole thing — the candidate stays
  pending. **This is the ONLY path that mutates `lemma_override` from a user
  signal, and it goes through an admin.**
- `POST /api/v1/admin/lemma-corrections/{id}/reject` — `require_admin`,
  transactional: marks the candidate `rejected` (+review metadata). **Never**
  writes `lemma_override`.
- No reopen: an already-reviewed (`accepted`/`rejected`) candidate returns 409.

## What's deferred

- **3C — LLM adjudication (optional):** a batch job feeds pending candidates to
  Haiku ("is `observed_lemma` wrong for `surface_form` in `context_text`? what's
  the correct lemma?") and proposes overrides for admin confirmation. No live
  LLM calls in 3A/3B.
- **Frontend:** a "this looks wrong" flag button in the reading/SRS UI, and an
  admin review screen over the accept/reject endpoints. None yet.

## Why not let the crowd vote directly (the rejected design)

Considered and rejected as the *primary* mechanism: (1) correctness is objective,
so voting can converge on a wrong answer; (2) learners are unqualified
adjudicators; (3) it needs sybil/abuse protection, quorum tuning, and moderation
— real cost for *worse* accuracy than a curated/LLM table; (4) context-sensitivity
(noun `ducha` vs verb) makes a flat surface→lemma vote ambiguous. Flag-then-
authority keeps the crowd doing what it's good at and correctness with something
qualified.
