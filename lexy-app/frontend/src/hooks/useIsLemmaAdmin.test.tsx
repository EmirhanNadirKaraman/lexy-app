/**
 * useIsLemmaAdmin tests (#39 P1).
 *
 * The hook decides whether the admin nav entry point renders. `is_admin` is
 * never sent to the client, so the hook probes the admin endpoint and treats a
 * 403 as "not an admin".
 *
 * Note what this does NOT claim: hiding the link is discoverability, not
 * security. `require_admin` on the backend routes is the real gate, which is why
 * AdminLemmaQueuePage still has a 403 error-state test.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

import { useIsLemmaAdmin } from './useIsLemmaAdmin';

function installFetch(status: number) {
    const stub = vi.fn(async (): Promise<Response> => new Response('[]', {
        status,
        headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', stub);
    return stub;
}

describe('useIsLemmaAdmin', () => {
    beforeEach(() => { vi.restoreAllMocks(); });
    afterEach(() => { vi.unstubAllGlobals(); });

    it('is true when the admin endpoint answers 200', async () => {
        installFetch(200);
        const { result } = renderHook(() => useIsLemmaAdmin('tok'));
        await waitFor(() => expect(result.current).toBe(true));
    });

    it('is false when the admin endpoint answers 403 (non-admin)', async () => {
        const stub = installFetch(403);
        const { result } = renderHook(() => useIsLemmaAdmin('tok'));
        // Give the probe a chance to resolve before asserting the negative.
        await waitFor(() => expect(stub).toHaveBeenCalled());
        await waitFor(() => expect(result.current).toBe(false));
    });

    it('starts false so the link never flashes before the probe resolves', () => {
        installFetch(200);
        const { result } = renderHook(() => useIsLemmaAdmin('tok'));
        expect(result.current).toBe(false);
    });

    it('does not probe at all without a token', () => {
        const stub = installFetch(200);
        const { result } = renderHook(() => useIsLemmaAdmin(null));
        expect(result.current).toBe(false);
        expect(stub).not.toHaveBeenCalled();
    });

    it('resolves false when the probe throws (network down)', async () => {
        const stub = vi.fn(async (): Promise<Response> => { throw new Error('offline'); });
        vi.stubGlobal('fetch', stub);
        const { result } = renderHook(() => useIsLemmaAdmin('tok'));
        await waitFor(() => expect(stub).toHaveBeenCalled());
        expect(result.current).toBe(false);
    });
});
