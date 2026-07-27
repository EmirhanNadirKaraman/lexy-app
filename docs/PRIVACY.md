# Privacy & data-deletion notes

The user-facing privacy policy lives at `/privacy` (rendered by
`lexy-app/frontend/src/components/PrivacyPage.tsx`). This doc is the
engineering-side companion: what data each table holds, and how account
deletion fans out.

## Account deletion

`DELETE /api/v1/account` (router: `lexy-app/backend/routers/account.py`).
Requires **both** a valid bearer token and the account's current password in
the request body (password re-authentication, S3, 2026-05-24) — a stolen or
long-lived token cannot delete an account on its own. A missing, empty, or
wrong password returns **403** and leaves the account intact. Only after the
password verifies does the route run
`DELETE FROM users WHERE user_id = $1::uuid`; everything else falls out via
Postgres FK declarations.

### Cascade tables (`ON DELETE CASCADE`)
Removed atomically when the user is deleted:

| Table | Migration |
|---|---|
| `user_word_knowledge` | 001 |
| `srs_cards` | 001 |
| `chat_sessions` (+ `chat_messages` via doc cascade) | 001 |
| `word_lists` (+ `word_list_entries`) | 001 |
| `word_usage_events` | 004 |
| `book_documents` (+ `book_pages` + `book_blocks` per-doc) | 008 |
| `reading_selections` | 009 |
| `notification` | 023 |
| `user_channel_preference` | 027 |

### SET-NULL tables (anonymised, not deleted)
| Table | Migration | Why kept |
|---|---|---|
| `content_request` | 018 | Operational audit: who-requested-what survives even if the user leaves. The `user_id` becomes NULL; the `request_type` / `content_id` / `status` columns are preserved. |
| `client_error_log` | 028 | Crash signal must survive deletion. The `message` / `stack` / `component_stack` survive; `user_id` becomes NULL. |

### Untouched (shared catalog)
No `user_id` column → not affected by user deletion:

- `word_table`, `phrase_table`, `grammar_rule_table`, `language_table`
- `channel`, `video`, `sentence`
- `word_to_sentence`, `sentence_to_phrase`, `sentence_to_grammar_rule`
- `llm_cache`

There is no separate `channel_names_cache` table (an earlier draft of this doc
listed one). The channel display-name cache is the `channel_names` key inside
`users.settings` JSONB — it is per-user data and therefore **deleted with the
`users` row**, not shared catalog. Channel *preferences* live relationally in
`user_channel_preference` and cascade (see the table above).

### Tests
`lexy-app/backend/tests/test_account_deletion.py`:
- positive path (204 + users row gone)
- cascade fan-out (user_word_knowledge, srs_cards, word_usage_events all drain)
- shared catalog preserved (word_table, phrase_table, grammar_rule_table,
  channel, video counts unchanged)
- unauthenticated + invalid-token rejected
- password re-auth (S3): missing body / empty password / wrong password each
  → 403 with the account and its cascade data intact
- other users unaffected
- second delete with same token → 401 (`get_current_user` returns "User not
  found" once the row is gone)
- `content_request.user_id` → NULL
- `client_error_log.user_id` → NULL

## Outstanding App Store / compliance gaps

This is the engineering checklist for app-store submission; not all are blockers
but each is a real risk.

- [x] Account-deletion endpoint and in-app entry point.
- [x] Privacy policy page.
- [ ] Replace the `<YOUR_REAL_PRIVACY_EMAIL_BEFORE_LAUNCH>` placeholder
  (currently in `frontend/src/components/PrivacyPage.tsx:CONTACT_EMAIL`)
  with a monitored privacy/support email. The placeholder is intentionally
  obvious (angle brackets keep `mailto:` from rendering) so this can't ship
  by accident.
- [ ] App Store Connect "Privacy Nutrient Label" / Apple App Privacy form
  declarations once the Capacitor wrap resumes.
- [x] Cookie / local-storage notice — done in the "Browser storage" section
  of `/privacy`. Discloses `auth_token` + `auth_email`, when they clear,
  and confirms they are not sent in client error reports. (We don't use
  cookies for auth, so no cookie banner is required today.)
- [ ] DSAR/export endpoint for GDPR jurisdictions (optional but recommended).
- [ ] Decide whether crash reports retained in anonymised form qualify as
  "personal data" under your jurisdiction — current text says they don't,
  legal review recommended.
- [ ] Decide on a backup retention window. Current policy text says
  "typically up to 30 days"; confirm against actual infra.
