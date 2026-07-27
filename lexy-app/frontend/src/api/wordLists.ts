import { apiUrl } from './_baseUrl';
import { assertOk, assertOkJson } from './_http';

/**
 * Vocabulary lists — paste/upload a word list, see what you already know.
 *
 * Five entry states, mirroring backend/services/word_list_service.py:
 *   known | learning | unknown  — bound to a catalog word
 *   unresolved                  — no match in the dictionary for that language
 *   ambiguous                   — several matches; the backend deliberately
 *                                 refuses to guess which sense was meant
 */
export type WordListEntryStatus =
    | 'known'
    | 'learning'
    | 'unknown'
    | 'unresolved'
    | 'ambiguous';

export interface WordListEntry {
    id: number;
    surface: string;
    item_id: number | null;
    item_type: string;
    status: WordListEntryStatus;
}

export interface WordListSummary {
    list_id: number;
    name: string;
    language: string;
    description: string | null;
    created_at: string;
    total: number;
}

export interface WordListDetail extends Omit<WordListSummary, 'total'> {
    total: number;
    counts: Record<WordListEntryStatus, number>;
    entries: WordListEntry[];
}

export interface MarkLearningResult {
    list_id: number;
    marked: number;
    marked_item_ids: number[];
    skipped_unresolved: number;
    skipped_ambiguous: number;
}

/** Max words per list — must match WORD_LIST_MAX_WORDS in the backend schemas. */
export const MAX_LIST_WORDS = 500;

/**
 * Split pasted text or an uploaded .txt into words.
 *
 * Splits on newlines, commas, tabs and semicolons so the same parser handles a
 * one-per-line list and a comma-separated one. Spaces are NOT separators —
 * multi-word surfaces stay intact for the phrase catalog. Blank entries are
 * dropped here so the count shown to the user matches what gets posted; the
 * backend dedupes case-insensitively.
 */
export function parseWordInput(raw: string): string[] {
    return raw
        .split(/[\n,;\t]+/)
        .map(w => w.trim())
        .filter(Boolean);
}

function authHeaders(token: string): HeadersInit {
    return { Authorization: `Bearer ${token}` };
}

export async function createWordList(
    token: string,
    name: string,
    language: string,
    words: string[],
): Promise<WordListDetail> {
    const res = await fetch(apiUrl('/api/v1/word-lists'), {
        method: 'POST',
        headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, language, words }),
    });
    return assertOkJson<WordListDetail>(res, 'Failed to create word list');
}

export async function listWordLists(token: string): Promise<WordListSummary[]> {
    const res = await fetch(apiUrl('/api/v1/word-lists'), { headers: authHeaders(token) });
    return assertOkJson<WordListSummary[]>(res, 'Failed to load word lists');
}

export async function getWordList(token: string, listId: number): Promise<WordListDetail> {
    const res = await fetch(apiUrl(`/api/v1/word-lists/${listId}`), {
        headers: authHeaders(token),
    });
    return assertOkJson<WordListDetail>(res, 'Failed to load word list');
}

/** The list as plain text, one surface per line — unresolved/ambiguous included. */
export async function exportWordList(token: string, listId: number): Promise<string> {
    const res = await fetch(apiUrl(`/api/v1/word-lists/${listId}/export`), {
        headers: authHeaders(token),
    });
    // Not assertOkJson: the endpoint returns text/plain, so parsing as JSON
    // would throw on the success path.
    await assertOk(res, 'Failed to export word list');
    return res.text();
}

export async function markUnknownAsLearning(
    token: string,
    listId: number,
): Promise<MarkLearningResult> {
    const res = await fetch(apiUrl(`/api/v1/word-lists/${listId}/mark-unknown-learning`), {
        method: 'POST',
        headers: authHeaders(token),
    });
    return assertOkJson<MarkLearningResult>(res, 'Failed to mark words as learning');
}

export async function deleteWordList(token: string, listId: number): Promise<void> {
    const res = await fetch(apiUrl(`/api/v1/word-lists/${listId}`), {
        method: 'DELETE',
        headers: authHeaders(token),
    });
    await assertOk(res, 'Failed to delete word list');
}
