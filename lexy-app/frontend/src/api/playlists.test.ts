/**
 * playlists.ts request-shape and error-handling tests.
 *
 * Two things worth pinning:
 *   - `algorithm` is always sent explicitly and defaults to 'greedy', so the
 *     wire request states the choice rather than depending on the backend
 *     default staying 'greedy'.
 *   - A 503 becomes a distinct `PlaylistSolverUnavailableError` instead of the
 *     generic Error `assertOkJson` would throw, so the UI can tell the user to
 *     switch planners rather than showing an operator-facing PuLP message.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { generatePlaylist, PlaylistSolverUnavailableError } from './playlists';

type FetchCall = { url: string; init: RequestInit | undefined };
const calls: FetchCall[] = [];

function installFetch(res: Response) {
    const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        calls.push({ url: typeof input === 'string' ? input : input.toString(), init });
        return res;
    });
    vi.stubGlobal('fetch', stub);
    return stub;
}

function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}

const OK_RESULT = { videos: [], coverage: {} };

function lastBody(): Record<string, unknown> {
    return JSON.parse(String(calls[calls.length - 1].init?.body)) as Record<string, unknown>;
}

describe('generatePlaylist request shape', () => {
    beforeEach(() => { calls.length = 0; });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('defaults to greedy when no algorithm is given', async () => {
        installFetch(jsonResponse(200, OK_RESULT));
        await generatePlaylist('tok', [1, 2], 'de', 5);

        expect(lastBody()).toEqual({
            item_ids: [1, 2],
            item_type: 'word',
            language: 'de',
            max_videos: 5,
            algorithm: 'greedy',
        });
    });

    it('sends algorithm: "ilp" when optimal is selected', async () => {
        installFetch(jsonResponse(200, OK_RESULT));
        await generatePlaylist('tok', [1], 'de', 3, 'ilp');
        expect(lastBody().algorithm).toBe('ilp');
    });

    it('sends algorithm: "greedy" when fast is selected explicitly', async () => {
        installFetch(jsonResponse(200, OK_RESULT));
        await generatePlaylist('tok', [1], 'de', 3, 'greedy');
        expect(lastBody().algorithm).toBe('greedy');
    });
});

describe('generatePlaylist error handling', () => {
    beforeEach(() => { calls.length = 0; });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it('throws PlaylistSolverUnavailableError on 503', async () => {
        installFetch(jsonResponse(503, {
            detail: "The 'ilp' playlist algorithm requires PuLP. Install it with `pip install pulp`.",
        }));
        await expect(generatePlaylist('tok', [1], 'de', 5, 'ilp'))
            .rejects.toBeInstanceOf(PlaylistSolverUnavailableError);
    });

    it('does not leak the operator-facing PuLP detail into the error message', async () => {
        installFetch(jsonResponse(503, { detail: 'Install it with `pip install pulp`.' }));
        await expect(generatePlaylist('tok', [1], 'de', 5, 'ilp'))
            .rejects.toThrow(/optimal planner is unavailable/i);
    });

    it('leaves other failures as ordinary errors', async () => {
        installFetch(jsonResponse(500, { detail: 'boom' }));
        const err = await generatePlaylist('tok', [1], 'de', 5).catch(e => e);
        expect(err).toBeInstanceOf(Error);
        expect(err).not.toBeInstanceOf(PlaylistSolverUnavailableError);
    });

    it('returns the parsed result on success', async () => {
        installFetch(jsonResponse(200, { videos: [{ video_id: 'v1' }], coverage: { video_count: 1 } }));
        const result = await generatePlaylist('tok', [1], 'de', 5, 'ilp');
        expect(result.coverage.video_count).toBe(1);
    });
});
