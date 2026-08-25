# SRS Review — Expected Behavior for Testing

## Happy path

- `GET /srs/due?language=de` returns cards whose `due_date <= NOW` and whose item has `status != 'known'` for the requesting user
- Each returned card includes: `card_id`, `item_id`, `item_type`, `direction`, `due_date`, `display_text`, `passive_level`, `active_level`, `repetitions`, and — since #0a-1 — `prompt_text` and `answer_text` (added to this line 2026-08-25; the `SRSReviewCard` schema requires both, so a card missing them fails validation)
- `display_text` is the surface word for words, surface form for phrases, and title for grammar rules
- `POST /srs/review/{card_id}` with `{"correct": true}` on a passive card fires `passive_review_correct`:
  - SM-2 advances: `new_interval = old_interval * ease_factor`, ease increases by 0.05, repetitions +1
  - `due_date` is pushed forward by `new_interval` days
- `POST /srs/review/{card_id}` with `{"correct": false}` on a passive card fires `passive_review_incorrect`:
  - SM-2 resets: `interval = 1.0`, ease decreases by 0.15 (min 1.3), `repetitions = 0`
- `POST /srs/review/{card_id}` with `{"correct": true}` on an active card fires `active_review_correct`:
  - SM-2 advances on the active card
  - `passive_level` and `active_level` also increment (progression rule adds deltas)
- `POST /srs/review/{card_id}` returns `{"card_id": ..., "success": true}`
- Cards are returned ordered by `due_date ASC` (most overdue first)
- A grammar_rule card returns with `display_text = grammar_rule_table.title`

## Edge cases

- Card belonging to a different user returns 404
- Card not found returns 404
- Item with status `known` is NOT included in due cards even if `due_date <= NOW`
- Due card list is empty and returns `[]` when no cards are due; frontend shows "Nothing due right now"
- Cards for a different language are NOT returned by the language-specific query
- `limit` query param is respected: requesting `limit=5` returns at most 5 cards
- A brand-new SRS card (created by `passive_srs="create"`) has `interval_days=1.0`, `ease_factor=2.5`, `repetitions=0`, `due_date=NOW`

## Non-goals (what the feature should NOT do)

- Must NOT return cards that are not yet due (`due_date > NOW`)
- Must NOT allow a user to submit an answer for another user's card
- Must NOT store display text on `srs_cards` — it must always be resolved from the item's source table
- Must NOT update scheduling via any path other than `progression_service`
- "Skip" must NOT submit any review answer to the server
