/**
 * AdminLemmaQueuePage tests (#39 P1).
 *
 * Covers the admin half:
 *   - the list renders surface_form / observed_lemma / suggested_lemma /
 *     context_text / report_count
 *   - accept hits the accept endpoint and updates the row IN PLACE
 *   - accept sends an overriding corrected_lemma when one is typed, and omits
 *     it when blank (so the backend falls back to suggested_lemma)
 *   - reject hits the reject endpoint and updates the row in place
 *   - a failed decision surfaces inline on the row and leaves it actionable
 *   - a non-admin (403) sees the error state, not a silent empty queue
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { AdminLemmaQueuePage } from './AdminLemmaQueuePage';
import type { LemmaCorrection } from '../api/lemmaCorrections';

type FetchCall = { url: string; init: RequestInit | undefined };
const calls: FetchCall[] = [];

function installFetch(handler: (url: string, init: RequestInit | undefined) => Response | Promise<Response>) {
    const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = typeof input === 'string' ? input : input.toString();
        calls.push({ url, init });
        return handler(url, init);
    });
    vi.stubGlobal('fetch', stub);
    return stub;
}

function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}

function makeCandidate(overrides: Partial<LemmaCorrection> = {}): LemmaCorrection {
    return {
        candidate_id: 7,
        language: 'es',
        surface_form: 'ducha',
        observed_lemma: 'duchaber',
        suggested_lemma: 'ducharse',
        context_text: 'Se ducha por la mañana.',
        item_type: 'phrase',
        source: 'user_flag',
        status: 'pending',
        report_count: 3,
        created_at: '2026-07-27T10:00:00Z',
        updated_at: '2026-07-27T10:00:00Z',
        ...overrides,
    };
}

/** List first, then whatever the decision endpoint should return. */
function installListThen(decision: (url: string) => Response) {
    installFetch((url) => {
        if (url.includes('/accept') || url.includes('/reject')) return decision(url);
        return jsonResponse([makeCandidate()]);
    });
}

describe('AdminLemmaQueuePage', () => {
    beforeEach(() => { calls.length = 0; });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('renders candidate fields including report_count', async () => {
        installFetch(() => jsonResponse([makeCandidate()]));
        render(<AdminLemmaQueuePage token="tok" onClose={() => {}} />);

        await waitFor(() => expect(screen.getByTestId('lemma-candidate-7')).toBeInTheDocument());
        expect(screen.getByTestId('lemma-surface-7')).toHaveTextContent('ducha');
        expect(screen.getByTestId('lemma-observed-7')).toHaveTextContent('duchaber');
        expect(screen.getByTestId('lemma-suggested-7')).toHaveTextContent('ducharse');
        expect(screen.getByTestId('lemma-context-7')).toHaveTextContent('Se ducha por la mañana.');
        expect(screen.getByTestId('lemma-report-count-7')).toHaveTextContent('3 reports');
    });

    it('shows the empty state when nothing is pending', async () => {
        installFetch(() => jsonResponse([]));
        render(<AdminLemmaQueuePage token="tok" onClose={() => {}} />);
        await waitFor(() => expect(screen.getByTestId('lemma-queue-empty')).toBeInTheDocument());
    });

    it('accept hits the accept endpoint and updates the row in place', async () => {
        installListThen(() => jsonResponse({
            candidate: makeCandidate({ status: 'accepted' }),
            override: {
                id: 1, language: 'es', observed_lemma: 'duchaber',
                corrected_lemma: 'ducharse', source: 'user_flag_reviewed', status: 'active',
            },
        }));
        render(<AdminLemmaQueuePage token="tok" onClose={() => {}} />);

        await waitFor(() => expect(screen.getByTestId('lemma-accept-7')).toBeInTheDocument());
        fireEvent.click(screen.getByTestId('lemma-accept-7'));

        await waitFor(() => expect(screen.getByTestId('lemma-status-7')).toBeInTheDocument());
        const accept = calls.find(c => c.url.includes('/accept'));
        expect(accept?.url).toContain('/api/v1/admin/lemma-corrections/7/accept');
        expect(accept?.init?.method).toBe('POST');
        expect(screen.getByTestId('lemma-status-7')).toHaveTextContent(/accepted/i);
        // Updated in place, not refetched.
        expect(calls.filter(c => c.url.includes('?status=pending'))).toHaveLength(1);
        expect(screen.queryByTestId('lemma-accept-7')).not.toBeInTheDocument();
    });

    it('accept omits corrected_lemma when blank, sends it when typed', async () => {
        installListThen(() => jsonResponse({
            candidate: makeCandidate({ status: 'accepted' }), override: null,
        }));
        render(<AdminLemmaQueuePage token="tok" onClose={() => {}} />);
        await waitFor(() => expect(screen.getByTestId('lemma-accept-7')).toBeInTheDocument());

        // Blank → omitted, so the backend falls back to suggested_lemma.
        fireEvent.click(screen.getByTestId('lemma-accept-7'));
        await waitFor(() => expect(calls.some(c => c.url.includes('/accept'))).toBe(true));
        const blankBody = JSON.parse(String(calls.find(c => c.url.includes('/accept'))!.init?.body));
        expect(blankBody).not.toHaveProperty('corrected_lemma');
    });

    it('accept sends an overriding corrected_lemma when typed', async () => {
        installListThen(() => jsonResponse({
            candidate: makeCandidate({ status: 'accepted' }), override: null,
        }));
        render(<AdminLemmaQueuePage token="tok" onClose={() => {}} />);
        await waitFor(() => expect(screen.getByTestId('lemma-corrected-input-7')).toBeInTheDocument());

        fireEvent.change(screen.getByTestId('lemma-corrected-input-7'), {
            target: { value: 'duchar' },
        });
        fireEvent.click(screen.getByTestId('lemma-accept-7'));

        await waitFor(() => expect(calls.some(c => c.url.includes('/accept'))).toBe(true));
        const body = JSON.parse(String(calls.find(c => c.url.includes('/accept'))!.init?.body));
        expect(body.corrected_lemma).toBe('duchar');
    });

    it('reject hits the reject endpoint and updates the row in place', async () => {
        installListThen(() => jsonResponse({
            candidate: makeCandidate({ status: 'rejected' }), override: null,
        }));
        render(<AdminLemmaQueuePage token="tok" onClose={() => {}} />);

        await waitFor(() => expect(screen.getByTestId('lemma-reject-7')).toBeInTheDocument());
        fireEvent.click(screen.getByTestId('lemma-reject-7'));

        await waitFor(() => expect(screen.getByTestId('lemma-status-7')).toBeInTheDocument());
        const reject = calls.find(c => c.url.includes('/reject'));
        expect(reject?.url).toContain('/api/v1/admin/lemma-corrections/7/reject');
        expect(screen.getByTestId('lemma-status-7')).toHaveTextContent(/rejected/i);
    });

    it('surfaces a failed decision inline and keeps the row actionable', async () => {
        installListThen(() => jsonResponse({ detail: 'nothing_to_promote' }, 400));
        render(<AdminLemmaQueuePage token="tok" onClose={() => {}} />);

        await waitFor(() => expect(screen.getByTestId('lemma-accept-7')).toBeInTheDocument());
        fireEvent.click(screen.getByTestId('lemma-accept-7'));

        await waitFor(() => expect(screen.getByTestId('lemma-row-error-7')).toBeInTheDocument());
        expect(screen.getByTestId('lemma-row-error-7')).toHaveTextContent(/nothing_to_promote/);
        expect(screen.getByTestId('lemma-accept-7')).toBeInTheDocument();
    });

    it('shows an error (not an empty queue) when the server refuses a non-admin', async () => {
        installFetch(() => jsonResponse({ detail: 'admin_required' }, 403));
        render(<AdminLemmaQueuePage token="tok" onClose={() => {}} />);

        await waitFor(() => expect(screen.getByTestId('lemma-queue-error')).toBeInTheDocument());
        expect(screen.getByTestId('lemma-queue-error')).toHaveTextContent(/admin_required/);
        expect(screen.queryByTestId('lemma-queue-empty')).not.toBeInTheDocument();
    });
});
