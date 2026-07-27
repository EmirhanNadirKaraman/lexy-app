// Shared HTTP helpers for the api/ layer.
//
// Centralises:
//   - response error parsing (Pydantic detail unwrapping)
//   - 401 handling (clears stored auth + emits an 'auth:expired' event so the
//     top-level Layout listener can navigate back to '/').
//
// The token_expired detail comes from backend/core/deps.py — see #13.
// Generic 401s also clear stored auth: a session that's no longer authorised
// for any reason should not keep its token around.

import { clearStoredEmail, clearToken } from '../auth';

export const AUTH_EXPIRED_EVENT = 'auth:expired';

interface ErrorBody {
    detail?: unknown;
}

/**
 * Turn a FastAPI/Pydantic `detail` payload into a user-facing message.
 *
 * Exported so `auth.ts` can reuse it. `auth.ts` deliberately does NOT use
 * `assertOk` — a 401 from login/register means "wrong credentials", not
 * "session expired", and routing it through `assertOk` would fire
 * `signalAuthExpired` and sign the user out mid-login. It shares only this
 * parser, not the 401 handling.
 */
export function detailToMessage(detail: unknown, fallback: string): string {
    if (!detail) return fallback;
    if (typeof detail === 'string') return detail || fallback;
    if (Array.isArray(detail) && detail.length > 0) {
        return detail
            .map((d: { msg?: string; loc?: unknown[] }) => {
                const field = d.loc?.findLast(s => s !== 'body');
                const msg = d.msg ?? 'Invalid value';
                return field ? `${String(field).charAt(0).toUpperCase() + String(field).slice(1)}: ${msg}` : msg;
            })
            .join('. ');
    }
    return fallback;
}

/** Clear stored auth state and notify the rest of the app. */
export function signalAuthExpired(reason: 'expired' | 'unauthorized' = 'expired'): void {
    clearToken();
    clearStoredEmail();
    // CustomEvent so listeners can read the reason via event.detail
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
        window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT, { detail: { reason } }));
    }
}

/**
 * Throw on non-OK response. On 401, clear auth and dispatch auth:expired
 * BEFORE throwing so the UI starts navigating immediately.
 *
 * Returns nothing — callers either `await res.json()` themselves or use
 * `assertOkJson(res)` for the common case.
 */
export async function assertOk(res: Response, fallbackMessage = `Request failed (${res.status})`): Promise<void> {
    if (res.ok) return;

    let body: ErrorBody | null = null;
    try {
        body = await res.clone().json() as ErrorBody;
    } catch {
        // not JSON; body stays null
    }

    if (res.status === 401) {
        const detail = typeof body?.detail === 'string' ? body.detail : '';
        signalAuthExpired(detail === 'token_expired' ? 'expired' : 'unauthorized');
        // Surface a useful Error so caller .catch blocks still get something readable.
        throw new Error(detail || 'Session expired. Please log in again.');
    }

    // Friendly LLM rate-limit messages (#12). Backend sends detail='rate_limit_minute'
    // or 'rate_limit_hour' on 429 with a Retry-After header.
    if (res.status === 429) {
        const detail = typeof body?.detail === 'string' ? body.detail : '';
        if (detail === 'rate_limit_minute') {
            throw new Error('Too many AI requests. Try again in a minute.');
        }
        if (detail === 'rate_limit_hour') {
            throw new Error('AI request limit reached. Try again later.');
        }
        throw new Error(detail || 'Too many requests. Please slow down.');
    }

    throw new Error(detailToMessage(body?.detail, fallbackMessage));
}

/** Convenience: assertOk + return parsed JSON. */
export async function assertOkJson<T>(res: Response, fallbackMessage?: string): Promise<T> {
    await assertOk(res, fallbackMessage);
    return res.json() as Promise<T>;
}
