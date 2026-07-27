/**
 * auth.ts error-handling tests.
 *
 * `auth.ts` is the deliberate exception to the shared-401 handler (#14): every
 * other API client routes failures through `assertOk`, which fires
 * `signalAuthExpired` on a 401. Auth must NOT, because a 401 from login means
 * "wrong email or password" — signing the user out and bouncing them home while
 * they are trying to log in would be absurd.
 *
 * Nothing pinned that invariant before, so it was possible to "unify" auth with
 * the shared handler and only discover the regression by hand-testing a failed
 * login. These tests pin it, and pin that sharing `detailToMessage` with
 * `_http.ts` did not change any user-facing message.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { login, register } from './auth';

const AUTH_EXPIRED_EVENT = 'auth:expired';

function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}

function installFetch(res: Response) {
    vi.stubGlobal('fetch', vi.fn(async () => res));
}

beforeEach(() => {
    localStorage.setItem('auth_token', 'stale-token');
    localStorage.setItem('auth_email', 'stale@example.com');
});

afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

describe('auth.ts is exempt from the global sign-out handler', () => {
    it('a 401 from login does NOT dispatch auth:expired', async () => {
        installFetch(jsonResponse(401, { detail: 'Incorrect email or password' }));
        const seen: Event[] = [];
        const listener = (e: Event) => seen.push(e);
        window.addEventListener(AUTH_EXPIRED_EVENT, listener);

        await expect(login('a@b.com', 'wrong')).rejects.toThrow(
            /Incorrect email or password/
        );

        window.removeEventListener(AUTH_EXPIRED_EVENT, listener);
        expect(seen).toHaveLength(0);
    });

    it('a 401 from login does NOT clear stored auth', async () => {
        installFetch(jsonResponse(401, { detail: 'Incorrect email or password' }));

        await expect(login('a@b.com', 'wrong')).rejects.toThrow();

        // assertOk would have cleared both. auth.ts must leave them alone.
        expect(localStorage.getItem('auth_token')).toBe('stale-token');
        expect(localStorage.getItem('auth_email')).toBe('stale@example.com');
    });

    it('a 401 from register does NOT dispatch auth:expired', async () => {
        installFetch(jsonResponse(401, { detail: 'nope' }));
        const seen: Event[] = [];
        const listener = (e: Event) => seen.push(e);
        window.addEventListener(AUTH_EXPIRED_EVENT, listener);

        await expect(register('a@b.com', 'pw')).rejects.toThrow();

        window.removeEventListener(AUTH_EXPIRED_EVENT, listener);
        expect(seen).toHaveLength(0);
    });
});

describe('auth.ts error messages (shared detailToMessage parser)', () => {
    it('surfaces a plain string detail verbatim', async () => {
        installFetch(jsonResponse(400, { detail: 'Registration failed' }));
        await expect(register('a@b.com', 'pw')).rejects.toThrow('Registration failed');
    });

    it('formats a Pydantic validation array with the field name', async () => {
        installFetch(jsonResponse(422, {
            detail: [{ loc: ['body', 'email'], msg: 'value is not a valid email address' }],
        }));
        await expect(login('bad', 'pw')).rejects.toThrow(
            'Email: value is not a valid email address'
        );
    });

    it('joins multiple validation errors', async () => {
        installFetch(jsonResponse(422, {
            detail: [
                { loc: ['body', 'email'], msg: 'invalid' },
                { loc: ['body', 'password'], msg: 'too short' },
            ],
        }));
        await expect(register('bad', 'x')).rejects.toThrow(
            'Email: invalid. Password: too short'
        );
    });

    it('falls back to a status-bearing message when detail is absent', async () => {
        installFetch(jsonResponse(500, {}));
        await expect(login('a@b.com', 'pw')).rejects.toThrow('Login failed (500)');
    });

    it('falls back when the body is not JSON at all', async () => {
        vi.stubGlobal('fetch', vi.fn(async () => new Response('gateway error', { status: 502 })));
        await expect(register('a@b.com', 'pw')).rejects.toThrow('Registration failed (502)');
    });
});

describe('auth.ts success paths still work', () => {
    it('login returns the access token', async () => {
        installFetch(jsonResponse(200, { access_token: 'tok-123' }));
        await expect(login('a@b.com', 'pw')).resolves.toBe('tok-123');
    });

    it('register resolves without a value', async () => {
        installFetch(jsonResponse(201, {}));
        await expect(register('a@b.com', 'pw')).resolves.toBeUndefined();
    });
});
