# SRS Review Frontend

## Goal

Give the user a lightweight spaced-repetition flashcard review session directly in the app. Due cards are fetched from the server, shown one at a time, and self-assessed. SM-2 scheduling updates happen on each answer so the card resurfaces at the right interval.

> **Updated 2026-08-25.** T1.2 (Hole 12, 2026-05-20) changed how both
> directions are presented, and this file still described the pre-T1.2 shape.
> Canonical: `CLAUDE.md` §8b, "SRS review is now real".

## Scope in

- Load up to **20** due cards per session (the default at every layer —
  `review_service.get_due_cards(limit=20)`, `GET /srs/due?limit=` default 20
  capped at 100, and the frontend client's own default), filtered by language
  and `due_date <= NOW`
- Item types supported: `word`, `phrase`, `grammar_rule`
- **Both directions prompt with the English gloss and answer with the German
  surface** — `prompt_text = gloss`, `answer_text = display_text`, set
  unconditionally in `review_service`. Grammar rules use `short_explanation` →
  `title`. The directions differ only in **grading**:
  - `passive` — reveal the answer, then self-grade "I knew it" / "I didn't know it"
  - `active` — **type the German**, submitted to `POST /srs/review/{card_id}/produce`
    and graded by `llm_service.evaluate_production` (with an exact-match fast
    path). There is deliberately **no self-grade button on active cards**.
- Per-card feedback screen after answering:
  - Green ✓ Correct or orange ✗ Incorrect
  - For active-direction cards: show the German that should have been produced
  - "Continue →" button advances to the next card
- Session complete screen showing how many cards were reviewed
- Empty state ("Nothing due right now") when no cards are due
- Language selector: user can switch language and reload
- "Reload" button to re-fetch due cards
- "Skip →" to advance past a card without recording an answer

## Scope out

- Audio or pronunciation playback
- Automatic card generation from content
- Hint/hint-level display during review
- Anki or CSV import/export
- Multi-language mixed sessions
- Editing or deleting SRS cards from the review screen
