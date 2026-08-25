# Free Chat → Progression Integration

## Goal

When a user sends a message in a free chat session, automatically detect vocabulary items from their active learning list that appear in the message and advance the appropriate progression tracks. The user does not need to explicitly flag words — exposure credit is awarded server-side based on detected language and token matching.

## Scope in

- Server-side matching via `chat_service.match_learning_words`, which combines
  **two** paths for items the user tracks with status `!= 'known'`:
  - WORDS — alphabetic tokens (lowercased) matched against `word_table` on
    surface form OR lemma
  - PHRASES — delegated to `matcher_service.match_sentence_with_ids`, the
    spaCy-based extractor, so inflected production like *ich freue mich auf X*
    matches the canonical `sich freuen auf`
- Language detection gates only whether **active** credit is granted. The
  message is ALWAYS scanned for target-language items:
  - `language_detected == session_language` → `free_chat_used_correctly`: both tracks advance
  - otherwise (including `'mixed'` and `'en'`) → `free_chat_mixed_lang`: passive track only
  - no target-language item present → no progression call at all
- Progression events are awaited (primary knowledge-state changes, not fire-and-forget)
- Analytics events are fire-and-forget

## Scope out

- Matching against items with status `known` (already mastered)
- LLM-driven token identification for the WORDS path (regex + DB join). The
  PHRASES path uses spaCy, still no LLM.

> **Corrected 2026-08-25.** Two entries were removed from this list because
> both had shipped:
> - *"Phrase item matching (only `item_type='word'`)"* — phrases are matched now,
>   and `free_chat_*` events fire for `item_type='phrase'` too.
> - *"Non-German free chat sessions (`language='de'` is hardcoded)"* — migration
>   031 added `chat_sessions.language`, and `routers/chat.py` reads
>   `session_language = session.get("language") or "de"`. The `"de"` is a
>   fallback for pre-migration rows, not a hardcoded target.
