// Account self-service API.
//
// deleteAccount() permanently removes the authenticated user. It now requires
// the current password (S3 re-auth) so a stolen bearer token alone can't delete
// the account. The backend returns 204 on success; only then do we clear the
// local token + email. A wrong password is a 403 — assertOk throws on it
// WITHOUT clearing auth (it clears only on 401), so the user stays logged in and
// can retry.

import { apiUrl } from './_baseUrl';
import { assertOk, signalAuthExpired } from './_http';

export async function deleteAccount(token: string, password: string): Promise<void> {
    const res = await fetch(apiUrl('/api/v1/account'), {
        method: 'DELETE',
        headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ password }),
    });
    await assertOk(res, 'Failed to delete account');
    // Confirmed 204 → clear stored auth and signal layout to navigate home.
    // 'unauthorized' because the token is now functionally invalid (user gone).
    signalAuthExpired('unauthorized');
}
