// Lemma-correction API client (#39 slice 3A/3B frontend).
//
// Backend contract: lexy-app/backend/routers/lemma_corrections.py
//   POST /api/v1/lemma-corrections                     any authed user, 30/hr
//   GET  /api/v1/admin/lemma-corrections               admin only
//   POST /api/v1/admin/lemma-corrections/{id}/accept   admin only
//   POST /api/v1/admin/lemma-corrections/{id}/reject   admin only
//
// /adjudicate is deliberately NOT wired: it returns 503 in production because
// slice 3C ships no adjudicator. Don't build UI against it.

import { apiUrl } from './_baseUrl';
import { assertOkJson } from './_http';

/** A candidate row as returned by the backend (schemas.LemmaCorrectionRead). */
export interface LemmaCorrection {
    candidate_id: number;
    language: string;
    surface_form: string;
    observed_lemma: string;
    /** '' means "no suggestion" — the backend normalises null/blank to ''. */
    suggested_lemma: string;
    /** '' means "no context" — same normalisation. */
    context_text: string;
    item_type: string | null;
    source: string;
    status: string;
    report_count: number;
    created_at: string;
    updated_at: string;
}

export interface LemmaOverride {
    id: number;
    language: string;
    observed_lemma: string;
    corrected_lemma: string;
    source: string;
    status: string;
}

/** Result of accept/reject. `override` is null for reject. */
export interface LemmaCorrectionReviewResult {
    candidate: LemmaCorrection;
    override: LemmaOverride | null;
}

/** What the learner-facing flag form collects. Optional fields may be blank. */
export interface LemmaFlagInput {
    language: string;
    surface_form: string;
    observed_lemma: string;
    suggested_lemma?: string;
    context_text?: string;
    item_type?: 'word' | 'phrase';
    item_id?: number;
    sentence_id?: number;
}

/**
 * Build the POST body, omitting optional fields that are blank.
 *
 * The backend tolerates null/blank (a model_validator normalises them to ''),
 * but sending `"suggested_lemma": ""` and omitting it are different intents on
 * the wire and only the latter is honest about "the user typed nothing".
 * Exported for direct unit testing.
 */
export function buildFlagBody(input: LemmaFlagInput): Record<string, unknown> {
    const body: Record<string, unknown> = {
        language: input.language,
        surface_form: input.surface_form.trim(),
        observed_lemma: input.observed_lemma.trim(),
    };
    const suggested = input.suggested_lemma?.trim();
    if (suggested) body.suggested_lemma = suggested;
    const context = input.context_text?.trim();
    if (context) body.context_text = context;
    if (input.item_type) body.item_type = input.item_type;
    if (typeof input.item_id === 'number') body.item_id = input.item_id;
    if (typeof input.sentence_id === 'number') body.sentence_id = input.sentence_id;
    return body;
}

/** Flag a bad lemma. Throws on failure (429 messages come from _http). */
export async function flagLemma(token: string, input: LemmaFlagInput): Promise<LemmaCorrection> {
    const res = await fetch(apiUrl('/api/v1/lemma-corrections'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(buildFlagBody(input)),
    });
    return assertOkJson<LemmaCorrection>(res, 'Failed to submit correction');
}

export async function listLemmaCorrections(
    token: string,
    status = 'pending',
    limit = 100,
): Promise<LemmaCorrection[]> {
    const res = await fetch(
        apiUrl(`/api/v1/admin/lemma-corrections?status=${encodeURIComponent(status)}&limit=${limit}`),
        { headers: { Authorization: `Bearer ${token}` } },
    );
    return assertOkJson<LemmaCorrection[]>(res, 'Failed to load review queue');
}

export async function acceptLemmaCorrection(
    token: string,
    candidateId: number,
    correctedLemma?: string,
    reviewNote?: string,
): Promise<LemmaCorrectionReviewResult> {
    // Omit a blank corrected_lemma so the backend falls back to the candidate's
    // own suggested_lemma (sending '' would read as an explicit empty override).
    const body: Record<string, unknown> = {};
    const corrected = correctedLemma?.trim();
    if (corrected) body.corrected_lemma = corrected;
    const note = reviewNote?.trim();
    if (note) body.review_note = note;

    const res = await fetch(apiUrl(`/api/v1/admin/lemma-corrections/${candidateId}/accept`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
    });
    return assertOkJson<LemmaCorrectionReviewResult>(res, 'Failed to accept correction');
}

export async function rejectLemmaCorrection(
    token: string,
    candidateId: number,
    reviewNote?: string,
): Promise<LemmaCorrectionReviewResult> {
    const body: Record<string, unknown> = {};
    const note = reviewNote?.trim();
    if (note) body.review_note = note;

    const res = await fetch(apiUrl(`/api/v1/admin/lemma-corrections/${candidateId}/reject`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
    });
    return assertOkJson<LemmaCorrectionReviewResult>(res, 'Failed to reject correction');
}

/**
 * Is this user allowed to see the admin queue?
 *
 * `is_admin` lives in `users.settings` JSONB and is deliberately NOT exposed in
 * any response model — not in the token, not in /settings/preferences — so the
 * client has no flag to read. We probe the admin endpoint instead and treat 403
 * as "not an admin".
 *
 * This hides the ENTRY POINT only. The real gate is `require_admin` on every
 * admin route; a non-admin who navigates directly still gets a 403 from the
 * server. Never throws — any failure resolves to `false`.
 */
export async function checkLemmaAdminAccess(token: string): Promise<boolean> {
    try {
        const res = await fetch(apiUrl('/api/v1/admin/lemma-corrections?status=pending&limit=1'), {
            headers: { Authorization: `Bearer ${token}` },
        });
        return res.ok;
    } catch {
        return false;
    }
}
