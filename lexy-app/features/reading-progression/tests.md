# Reading → Progression — Expected Behavior for Testing

## Happy path — save selection

- Saving a selection whose canonical matches a word in `word_table` fires `status_marked_learning`:
  - `user_word_knowledge.passive_level` incremented by 1
  - `user_word_knowledge.times_seen` incremented by 1
  - A passive SRS card is created for the matched word (if none existed)
- Saving a selection whose canonical matches a phrase in `phrase_table` fires `status_marked_learning` for the phrase item
- Saving a selection with a canonical that has NO catalog match → selection is saved, no progression event, no error

## Happy path — review outcomes

- Review `got_it` on a selection with a catalog item fires `passive_review_correct`:
  - SM-2 passive card advances: interval grows, ease may increase, repetitions +1
  - `reading_selections.review_count` increments by 1
  - `reading_selections.next_review_at` is set to `NOW + interval_days[old_review_count]`
- Review `still_learning` fires `passive_review_incorrect`:
  - SM-2 passive card penalized: interval resets to 1.0, ease decreases
  - `review_count` resets to 0
  - `next_review_at` set to `NOW` (due immediately)
- Review `mastered`:
  - `reading_selections.status` = `'mastered'`
  - `reading_selections.next_review_at` = NULL
  - `status_marked_known` IS fired on the matched catalog item, with
    `status_override='known'`, so `user_word_knowledge.status` flips to `known`
    (Hole 24 / #5, 2026-05-20). Passive SRS card advances; the active card is
    NOT created or touched.
  - The selection does not appear in future due-selections queries

## Reading interval schedule

- First `got_it` (review_count was 0) → `next_review_at` = NOW + 1 day
- Second `got_it` (review_count was 1) → NOW + 2 days
- Third → +4 days, fourth → +7 days, fifth → +14 days, sixth → +30 days
- Beyond sixth: always +30 days (capped)

## `get_due_selections` behavior

- Returns selections with `status='learning'` AND (`next_review_at IS NULL` OR `next_review_at <= NOW`)
- NULL `next_review_at` (newly saved, never reviewed) appears FIRST
- Selections with `status='mastered'` are NOT returned
- Results are limited by the `limit` parameter

## Non-goals (what the feature should NOT do)

- Must NOT fire active SRS events from reading (reading is passive-track only)
- Must NOT fire an **active**-track event or create an active SRS card for the `mastered` outcome (it does fire `status_marked_known` — see above; corrected 2026-08-25)
- Must NOT surface reading selections as cards in `GET /srs/due` (separate review queue)
- Must NOT fire `status_marked_learning` again on re-save of the same canonical (upsert semantics in progression service handle idempotency at the knowledge row level)
