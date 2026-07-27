// Auth is the one API client that must NOT use `assertOk`. A 401 here means
// "wrong email or password", not "your session expired" — routing it through
// the shared handler would fire `signalAuthExpired`, clear storage and bounce
// the user home while they are trying to log in. So this file keeps its own
// `if (!res.ok) throw` blocks and shares only the message parser.
import { apiUrl } from './_baseUrl';
import { detailToMessage } from './_http';

export async function login(email: string, password: string): Promise<string> {
    const res = await fetch(apiUrl('/api/v1/auth/login'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({})) as { detail?: unknown };
        throw new Error(detailToMessage(err.detail, `Login failed (${res.status})`));
    }
    const data = await res.json() as { access_token: string };
    return data.access_token;
}

export async function register(
    email: string,
    password: string,
    registrationCode?: string,
): Promise<void> {
    // registration_code is only sent when the user supplied one; the backend
    // enforces it only if REGISTRATION_CODE is configured (S2).
    const body: Record<string, unknown> = { email, password };
    if (registrationCode) body.registration_code = registrationCode;

    const res = await fetch(apiUrl('/api/v1/auth/register'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({})) as { detail?: unknown };
        throw new Error(detailToMessage(err.detail, `Registration failed (${res.status})`));
    }
}
