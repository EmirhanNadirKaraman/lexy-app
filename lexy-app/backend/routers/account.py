"""Account self-service endpoints.

DELETE /api/v1/account permanently removes the authenticated user's account
and all owned private data. All FKs to `users(user_id)` declare either
`ON DELETE CASCADE` (the dominant path — see migration audit in
`docs/PRIVACY.md`) or `ON DELETE SET NULL` (`content_request`,
`client_error_log` — intentionally anonymised, not deleted, so error and
operational signal survives).

Shared catalog tables (`word_table`, `phrase_table`, `grammar_rule_table`,
`channel`, `video`, `sentence`, `language_table`, `llm_cache`, …) have no
`user_id` FK and are left intact.

Bearer-token only deletes the *current* user. There is no admin-deletes-other
endpoint; that would need a separate route + admin gate.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel

from ..core.deps import get_current_user
from ..core.security import verify_password
from ..database import get_pool

router = APIRouter(prefix="/account", tags=["account"])


class AccountDeleteRequest(BaseModel):
    # Re-authentication for the destructive delete (S3). Optional at the schema
    # level so a *missing* body is handled uniformly in the route (→ 403) rather
    # than a 422 that would distinguish "missing" from "wrong".
    password: str | None = None


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    body: AccountDeleteRequest | None = None,
    current_user: dict = Depends(get_current_user),
    pool=Depends(get_pool),
) -> Response:
    """Delete the authenticated user and all cascading private data, after
    **password re-authentication** (S3).

    Requires the current password in the request body *in addition to* a valid
    bearer token, so a stolen / long-lived token cannot delete the account on
    its own. Missing or wrong password → 403; the detail string below is shown
    to the user and is deliberately not "generic-to-the-point-of-useless"
    because the caller is already authenticated (a valid token), so confirming
    "wrong password" reveals no account-existence secret. The single-statement
    cascade delete runs only after the password verifies.

    Note: this sends a body on DELETE. Fine for the same-origin SPA → API call;
    a future strict CDN/proxy in front would need to allow DELETE request bodies.
    """
    password = (body.password if body else None) or ""
    row = await pool.fetchrow(
        "SELECT password_hash FROM users WHERE user_id = $1::uuid",
        current_user["user_id"],
    )
    # `row` should exist (get_current_user already loaded the user); guard anyway.
    if row is None or not password or not verify_password(password, row["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Incorrect password. Please try again.",
        )

    await pool.execute(
        "DELETE FROM users WHERE user_id = $1::uuid",
        current_user["user_id"],
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
