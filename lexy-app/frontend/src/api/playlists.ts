import { apiUrl } from './_baseUrl';
import { assertOkJson } from './_http';
import type { PlaylistResult } from '../types';

/** Which optimizer the backend should use. Mirrors the API's `algorithm` enum. */
export type PlaylistAlgorithm = 'greedy' | 'ilp';

/**
 * The ILP optimizer was requested but its solver is unavailable (backend 503).
 *
 * A distinct error type rather than a message check: the backend's `detail` is
 * an operator-facing string about installing PuLP, which is not what a learner
 * should read. The caller catches this and suggests the fast planner instead,
 * while every other failure keeps the existing generic message.
 *
 * Handled here rather than in `_http.ts` because 503 has no shared meaning
 * across the API — this is the only endpoint with an optional backend that can
 * be missing while the request itself is perfectly valid.
 */
export class PlaylistSolverUnavailableError extends Error {
    constructor(message = 'The optimal planner is unavailable right now.') {
        super(message);
        this.name = 'PlaylistSolverUnavailableError';
    }
}

export async function generatePlaylist(
    token: string,
    itemIds: number[],
    language: string,
    maxVideos: number,
    // Defaults to 'greedy' so existing call sites keep their exact behaviour.
    // Sent explicitly rather than omitted: the wire request then states the
    // choice, instead of depending on the backend default staying 'greedy'.
    algorithm: PlaylistAlgorithm = 'greedy',
): Promise<PlaylistResult> {
    const res = await fetch(apiUrl('/api/v1/playlists/generate'), {
        method: 'POST',
        headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            item_ids: itemIds,
            item_type: 'word',
            language,
            max_videos: maxVideos,
            algorithm,
        }),
    });

    // Checked before assertOkJson, which would collapse this into a generic
    // Error indistinguishable from a network or server failure.
    if (res.status === 503) {
        throw new PlaylistSolverUnavailableError();
    }

    return assertOkJson<PlaylistResult>(res, 'Failed to generate playlist');
}
