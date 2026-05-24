/**
 * deleteAccount() — S3 password re-auth contract.
 *
 *   - sends the password in the DELETE body
 *   - clears stored auth only on a confirmed 204
 *   - a wrong-password 403 throws WITHOUT clearing auth (user stays logged in,
 *     can retry) — assertOk clears only on 401
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { deleteAccount } from './account';

describe('deleteAccount', () => {
    beforeEach(() => {
        localStorage.setItem('auth_token', 'tok');
        localStorage.setItem('auth_email', 'e@example.com');
    });
    afterEach(() => {
        localStorage.clear();
        vi.restoreAllMocks();
    });

    it('sends the password in the request body', async () => {
        const fetchSpy = vi
            .spyOn(globalThis, 'fetch')
            .mockResolvedValue(new Response(null, { status: 204 }));

        await deleteAccount('tok', 'hunter2');

        expect(fetchSpy).toHaveBeenCalledTimes(1);
        const init = fetchSpy.mock.calls[0][1];
        expect(init?.method).toBe('DELETE');
        expect(JSON.parse(init?.body as string)).toEqual({ password: 'hunter2' });
        expect((init?.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    });

    it('clears stored auth on a 204 success', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

        await deleteAccount('tok', 'hunter2');

        expect(localStorage.getItem('auth_token')).toBeNull();
    });

    it('does NOT clear auth on a 403 wrong password (throws, token preserved)', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({ detail: 'Incorrect password. Please try again.' }), {
                status: 403,
                headers: { 'Content-Type': 'application/json' },
            }),
        );

        await expect(deleteAccount('tok', 'wrong')).rejects.toThrow();
        expect(localStorage.getItem('auth_token')).toBe('tok'); // still logged in
    });
});
