/**
 * PlaylistPanel planner-selector tests.
 *
 * Covers only the algorithm selector added alongside the backend's opt-in ILP
 * mode — the rest of the panel is untouched and unasserted here.
 *
 * The panel is driven through a stubbed `fetch`, so these exercise the real
 * request body the API client builds rather than mocking the client away.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { PlaylistPanel } from './PlaylistPanel';

type FetchCall = { url: string; init: RequestInit | undefined };
const calls: FetchCall[] = [];

function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}

/** Recommendations power "Add recommended words", which is how a target gets in.
 *  Shape must be `{items: [...]}` — that is what fetchItemRecommendations returns. */
const RECS = { items: [{ item_id: 7, item_type: 'word', display_text: 'Kino' }] };

/** A COMPLETE playlist response. The full `coverage` shape matters: on success
 *  the panel switches to ResultView, which reads `uncovered_item_ids.length`.
 *  A partial stub crashes the render — the backend contract (asserted by
 *  test_endpoint_response_shape) always sends all five fields. */
const OK_PLAYLIST = {
    videos: [],
    coverage: {
        target_count: 1,
        covered_count: 0,
        coverage_pct: 0,
        uncovered_item_ids: [7],
        video_count: 0,
    },
};

function installFetch(generateResponse: () => Response) {
    const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = typeof input === 'string' ? input : input.toString();
        calls.push({ url, init });
        if (url.includes('/playlists/generate')) return generateResponse();
        if (url.includes('/recommendations')) return jsonResponse(RECS);
        return jsonResponse([]);           // suggestions and anything else
    });
    vi.stubGlobal('fetch', stub);
    return stub;
}

function renderPanel() {
    return render(
        <PlaylistPanel
            token="tok"
            language="de"
            onLanguageChange={() => {}}
            onWatch={() => {}}
            onClose={() => {}}
        />
    );
}

/** Add a target, then click Generate. Without a target the button is disabled. */
async function loadTargetAndGenerate() {
    fireEvent.click(screen.getByText(/add recommended words/i));
    await waitFor(() => expect(screen.getByText('Kino')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('playlist-generate'));
}

function generateBody(): Record<string, unknown> {
    const call = calls.find(c => c.url.includes('/playlists/generate'));
    return JSON.parse(String(call?.init?.body)) as Record<string, unknown>;
}

describe('PlaylistPanel planner selector', () => {
    beforeEach(() => { calls.length = 0; });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('defaults to Fast (greedy)', () => {
        installFetch(() => jsonResponse(OK_PLAYLIST));
        renderPanel();
        expect(screen.getByTestId('playlist-algorithm')).toHaveValue('greedy');
    });

    it('sends algorithm: "greedy" when left untouched', async () => {
        installFetch(() => jsonResponse(OK_PLAYLIST));
        renderPanel();
        await loadTargetAndGenerate();

        await waitFor(() => expect(calls.some(c => c.url.includes('/playlists/generate'))).toBe(true));
        expect(generateBody().algorithm).toBe('greedy');
    });

    it('sends algorithm: "ilp" after selecting Optimal', async () => {
        installFetch(() => jsonResponse(OK_PLAYLIST));
        renderPanel();

        fireEvent.change(screen.getByTestId('playlist-algorithm'), { target: { value: 'ilp' } });
        await loadTargetAndGenerate();

        await waitFor(() => expect(calls.some(c => c.url.includes('/playlists/generate'))).toBe(true));
        expect(generateBody().algorithm).toBe('ilp');
    });

    it('shows an inline error pointing at Fast when the solver 503s', async () => {
        installFetch(() => jsonResponse({ detail: 'requires PuLP' }, 503));
        renderPanel();

        fireEvent.change(screen.getByTestId('playlist-algorithm'), { target: { value: 'ilp' } });
        await loadTargetAndGenerate();

        // The message names the recovery action; the operator-facing PuLP
        // detail must not reach the user.
        await waitFor(() =>
            expect(screen.getByText(/optimal planning is unavailable/i)).toBeInTheDocument()
        );
        expect(screen.getByText(/switch to fast/i)).toBeInTheDocument();
        expect(screen.queryByText(/PuLP/)).not.toBeInTheDocument();
        // Still on the build view — the page did not crash or navigate away.
        expect(screen.getByTestId('playlist-algorithm')).toBeInTheDocument();
    });

    it('keeps the generic message for non-503 failures', async () => {
        installFetch(() => jsonResponse({ detail: 'boom' }, 500));
        renderPanel();
        await loadTargetAndGenerate();

        await waitFor(() =>
            expect(screen.getByText(/failed to generate playlist/i)).toBeInTheDocument()
        );
    });

    it('uses a 16px control (iOS focus-zoom guard) and a 44px touch target', () => {
        installFetch(() => jsonResponse(OK_PLAYLIST));
        renderPanel();
        const select = screen.getByTestId('playlist-algorithm');
        expect(select.style.fontSize).toBe('16px');
        expect(select.style.minHeight).toBe('44px');
    });
});
