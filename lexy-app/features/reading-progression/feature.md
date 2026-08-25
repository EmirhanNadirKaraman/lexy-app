# Reading → Progression Integration

## Goal

When a user saves a word or phrase as a custom learning unit during interactive reading, and when they review it, mirror those events into the main progression system. Reading should build the same passive knowledge model as the SRS review, transcript, and chat features — it is not an isolated silo.

## Scope in

- **Save selection**: when a reading selection is saved, look up the canonical text in `word_table` (single-word) or `phrase_table` (multi-word). If found, fire `status_marked_learning`:
  - Creates a passive SRS card if one does not exist
  - Increments `passive_level` by 1, `times_seen` by 1
- **Review `got_it`**: fire `passive_review_correct` on the catalog item → SM-2 passive card advances
- **Review `still_learning`**: fire `passive_review_incorrect` on the catalog item → SM-2 passive card penalized
- **Review `mastered`**: fires `status_marked_known` with
  `status_override='known'` on the catalog item, and the reading selection exits
  review rotation (status → `mastered`). Reading "Mastered" is the same shape as
  a manual Known click — passive credit only, `active_delta=0`, no active card
  fabricated. *(Corrected 2026-08-25: this line said "no progression event",
  which stopped being true with the Hole 24 / #5 fix on 2026-05-20.)*
- Selections without catalog matches (novel expressions not in word/phrase tables) still save and participate in reading-internal SRS, but do not feed the main progression system

## Scope out

- Active SRS track (reading only bridges to the passive progression track)
- Selections for multi-word expressions not yet in `phrase_table` (reading-only learning for those)
- Progression events from reading translate/explain LLM calls (those are lookup-only, not learning events)
