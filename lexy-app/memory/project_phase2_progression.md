---
name: Phase 2 passive/active progression
description: Progression service design, rule table, and wiring for passive vs active knowledge tracks
type: project
---

> **Canonical source: `CLAUDE.md` §8b ("Loop invariants") and
> `progression_service._RULES` itself.** This file is a design note, not a
> second specification. If it disagrees with either, they win — say so and fix
> this file rather than acting on it.
>
> **Corrected 2026-08-25.** The rule table below had described the pre-2026-05-18
> behaviour as current, and listed as "deferred" a set of events that have all
> shipped. `docs/DOC_AUDIT_2026-05-20.md` flagged exactly this drift on
> 2026-05-20 and its fix was never applied. Every row below is now transcribed
> from `_RULES` in `lexy-app/backend/services/progression_service.py`.

Passive/active knowledge distinction is explicit and consistent. The single
source of truth is `progression_service.py`.

**Why:** no duplicated progression logic across chat, transcript, reading and
SRS code. All knowledge-state changes funnel through one place.

**How to apply:** a new feature that should update knowledge levels adds an
entry to `_RULES` and calls `apply_progression()`. Do NOT write progression
logic in routers or other services.

## Files

- `backend/services/progression_service.py` — `_RULES`, `compute_delta(event)`
  (pure), `apply_progression(...)` (DB write, transactional, also writes
  `status` when the caller passes `status_override`)
- `backend/tests/test_progression.py` — **74 tests** (measured 2026-08-25).
  Also check `test_free_chat_progression.py` and `test_reading_progression.py`.
- `backend/services/guided_chat_service.py` — delegates entirely to
  progression_service
- `backend/routers/words.py` — one `apply_progression(..., status_override=…)`
  call; `word_service.upsert_word_status` no longer exists
- `backend/routers/chat.py` — always calls `update_progress`

## Rule table (transcribed from `_RULES`, 2026-08-25)

Blank = no change / no SRS action.

| Event | passive_delta | active_delta | times_seen | times_used_correctly | passive SRS | active SRS |
|---|---|---|---|---|---|---|
| `guided_counted` | +1 | +1 | | +1 | correct | correct |
| `guided_used` | +1 | | | | correct | |
| `guided_not_used` | | | | | | incorrect |
| `status_marked_learning` | +1 | | +1 | | create | **create** |
| `status_marked_known` | | | | | correct | |
| `status_marked_unknown` | | | | | incorrect | incorrect |
| `transcript_clicked` | +1 | | +1 | | create | |
| `free_chat_matched` | +1 | | +1 | | correct | |
| `free_chat_used_correctly` | +1 | +1 | | +1 | correct | correct |
| `free_chat_mixed_lang` | +1 | | +1 | | correct | |
| `passive_review_correct` | +1 | | | | correct | |
| `passive_review_incorrect` | | | | | incorrect | |
| `active_review_correct` | +1 | +1 | | +1 | | correct |
| `active_review_incorrect` | | | | | | incorrect |

Three rows are the ones people get wrong, all changed on 2026-05-18/19:

- **`status_marked_known` does not touch the active track at all.** Manual
  "Known" is user confidence, not production evidence. It once wrote
  `active_delta=+1, active_srs=correct`, which inflated active mastery on a
  click. A user with `active_level > 0` and no production events in
  `word_usage_events` is pre-fix data.
- **`status_marked_unknown` resets the SRS schedule** (both directions,
  SM-2 incorrect branch) but leaves levels alone. `action="incorrect"` is a
  **no-op on a missing card**, so it never fabricates an active card.
- **`status_marked_learning` creates BOTH cards** (since #0b), so the
  active-direction queue is not permanently empty for users who never open
  guided chat.

## Thresholds

- `PASSIVE_PROMOTION_THRESHOLD = 5`
- `ACTIVE_MASTERY_THRESHOLD = 3`

Promotion is evaluated in `_maybe_promote`, in order, one per call: active
threshold → `known`; passive threshold from `learning` → `known`; passive
threshold from `unknown` → `learning`. Auto-promotion is one-way — nothing
auto-demotes. Manual demotion is handled separately in `_apply_demotion`, which
*sets* levels rather than decrementing them.

## Schema

No migration was needed — `passive_level`, `active_level`, `times_seen`,
`times_used_correctly` and `srs_cards.direction` already existed.
