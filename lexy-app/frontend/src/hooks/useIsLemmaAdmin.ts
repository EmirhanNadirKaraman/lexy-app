// Client-side admin gating for the lemma-correction queue (#39 P1).
//
// Why a probe and not a flag: `is_admin` lives in `users.settings` JSONB and is
// deliberately not exposed in any response model — not in the token payload, not
// in /settings/preferences. There is no field to read, so we ask the admin
// endpoint whether it will answer and treat 403 as "not an admin".
//
// This controls DISCOVERABILITY only. `require_admin` on every admin route is
// the real gate; a non-admin who types the URL still gets a 403 from the server
// and the queue renders its error state. Hiding the link is UX, not security.

import { useEffect, useState } from 'react';

import { checkLemmaAdminAccess } from '../api/lemmaCorrections';

/**
 * Returns true only once the probe has confirmed admin access.
 *
 * Starts false so the entry point is hidden during the in-flight check — a link
 * that flashes in and then vanishes is worse than one that appears a moment
 * late. Resolves to false for a missing token without issuing a request.
 */
export function useIsLemmaAdmin(token: string | null): boolean {
    const [isAdmin, setIsAdmin] = useState(false);

    useEffect(() => {
        if (!token) {
            setIsAdmin(false);
            return;
        }
        let cancelled = false;
        void checkLemmaAdminAccess(token).then(ok => {
            if (!cancelled) setIsAdmin(ok);
        });
        return () => { cancelled = true; };
    }, [token]);

    return isAdmin;
}
