/**
 * LemmaFlagButton tests (#39 P1).
 *
 * Covers the learner half of the flag flow:
 *   - submitting posts the expected body to /api/v1/lemma-corrections
 *   - blank optional fields are OMITTED from the body (not sent as '')
 *   - a failed submit keeps the form open, preserves input, and shows the error
 *   - a 429 surfaces the throttle message from _http.ts
 *   - touch/font guards (44px targets, 16px inputs)
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { LemmaFlagButton } from './LemmaFlagButton';

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

const created = {
    candidate_id: 7,
    language: 'es',
    surface_form: 'ducha',
    observed_lemma: 'duchaber',
    suggested_lemma: 'ducharse',
    context_text: '',
    item_type: 'phrase',
    source: 'user_flag',
    status: 'pending',
    report_count: 1,
    created_at: '2026-07-27T10:00:00Z',
    updated_at: '2026-07-27T10:00:00Z',
};

function renderFlag() {
    return render(
        <LemmaFlagButton
            token="tok"
            language="es"
            surfaceForm="ducha"
            observedLemma="duchaber"
            itemType="phrase"
            itemId={42}
        />
    );
}

function lastBody(): Record<string, unknown> {
    const call = calls[calls.length - 1];
    return JSON.parse(String(call.init?.body)) as Record<string, unknown>;
}

describe('LemmaFlagButton', () => {
    beforeEach(() => { calls.length = 0; });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('posts the expected body including a suggested lemma', async () => {
        installFetch(() => jsonResponse(created, 201));
        renderFlag();

        fireEvent.click(screen.getByTestId('lemma-flag-open'));
        fireEvent.change(screen.getByTestId('lemma-flag-suggested'), {
            target: { value: 'ducharse' },
        });
        fireEvent.click(screen.getByTestId('lemma-flag-submit'));

        await waitFor(() => expect(calls.length).toBe(1));
        expect(calls[0].url).toContain('/api/v1/lemma-corrections');
        expect(calls[0].init?.method).toBe('POST');
        expect(lastBody()).toEqual({
            language: 'es',
            surface_form: 'ducha',
            observed_lemma: 'duchaber',
            suggested_lemma: 'ducharse',
            item_type: 'phrase',
            item_id: 42,
        });
    });

    it('omits blank optional fields from the body', async () => {
        installFetch(() => jsonResponse(created, 201));
        renderFlag();

        fireEvent.click(screen.getByTestId('lemma-flag-open'));
        // Type only whitespace — must be treated as absent, not as ''.
        fireEvent.change(screen.getByTestId('lemma-flag-suggested'), { target: { value: '   ' } });
        fireEvent.click(screen.getByTestId('lemma-flag-submit'));

        await waitFor(() => expect(calls.length).toBe(1));
        const body = lastBody();
        expect(body).not.toHaveProperty('suggested_lemma');
        expect(body).not.toHaveProperty('context_text');
        expect(body).toMatchObject({
            language: 'es',
            surface_form: 'ducha',
            observed_lemma: 'duchaber',
        });
    });

    it('shows a quiet acknowledgement after success', async () => {
        installFetch(() => jsonResponse(created, 201));
        renderFlag();

        fireEvent.click(screen.getByTestId('lemma-flag-open'));
        fireEvent.click(screen.getByTestId('lemma-flag-submit'));

        await waitFor(() => expect(screen.getByTestId('lemma-flag-done')).toBeInTheDocument());
        expect(screen.queryByTestId('lemma-flag-form')).not.toBeInTheDocument();
    });

    it('keeps the form open with an inline error when submit fails', async () => {
        installFetch(() => jsonResponse({ detail: 'boom' }, 500));
        renderFlag();

        fireEvent.click(screen.getByTestId('lemma-flag-open'));
        fireEvent.change(screen.getByTestId('lemma-flag-suggested'), {
            target: { value: 'ducharse' },
        });
        fireEvent.click(screen.getByTestId('lemma-flag-submit'));

        await waitFor(() => expect(screen.getByTestId('lemma-flag-error')).toBeInTheDocument());
        // Form stays open and the typed value survives, so nothing is lost.
        expect(screen.getByTestId('lemma-flag-form')).toBeInTheDocument();
        expect(screen.getByTestId('lemma-flag-suggested')).toHaveValue('ducharse');
        expect(screen.queryByTestId('lemma-flag-done')).not.toBeInTheDocument();
    });

    it('surfaces the 429 throttle message', async () => {
        installFetch(() => jsonResponse({ detail: 'Too many requests. Try again later.' }, 429));
        renderFlag();

        fireEvent.click(screen.getByTestId('lemma-flag-open'));
        fireEvent.click(screen.getByTestId('lemma-flag-submit'));

        await waitFor(() => expect(screen.getByTestId('lemma-flag-error')).toBeInTheDocument());
        expect(screen.getByTestId('lemma-flag-error')).toHaveTextContent(/too many requests/i);
    });

    it('uses 44px submit target and 16px inputs (mobile/iOS guards)', () => {
        installFetch(() => jsonResponse(created, 201));
        renderFlag();
        fireEvent.click(screen.getByTestId('lemma-flag-open'));

        expect(screen.getByTestId('lemma-flag-submit').style.minHeight).toBe('44px');
        expect(screen.getByTestId('lemma-flag-suggested').style.fontSize).toBe('16px');
        expect(screen.getByTestId('lemma-flag-note').style.fontSize).toBe('16px');
    });
});
