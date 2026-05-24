import hmac
import os

import asyncpg

from ..core.security import create_access_token, hash_password, verify_password

# Shown for EVERY registration failure (duplicate email, wrong/missing invite
# code, …) so the response can't be used to enumerate which emails exist (S9) or
# to distinguish failure causes. Note: because a successful register still
# returns a user object (and the frontend then logs in), this only *reduces*
# enumeration — a 400-vs-201 difference remains. Full non-enumeration needs an
# email-verification flow (deferred — see docs/SECURITY.md).
GENERIC_REGISTER_ERROR = (
    "Registration could not be completed. Please check your details, "
    "or sign in if you already have an account."
)


async def register_user(
    pool: asyncpg.Pool,
    email: str,
    password: str,
    registration_code: str | None = None,
) -> dict:
    """
    Create a new user. Returns {user_id: str, email: str}.

    Raises ValueError(GENERIC_REGISTER_ERROR) for any failure (duplicate email,
    or — when REGISTRATION_CODE is configured — a missing/wrong invite code).
    The single generic message keeps the failure non-enumerable (S9/S2).
    """
    email = email.lower().strip()

    # Invite-code gate (S2). Only enforced when REGISTRATION_CODE is set. Checked
    # BEFORE the duplicate lookup so a caller without the code can't enumerate
    # emails; constant-time compare so the code can't be timing-probed.
    required_code = os.getenv("REGISTRATION_CODE", "")
    if required_code and not hmac.compare_digest(registration_code or "", required_code):
        raise ValueError(GENERIC_REGISTER_ERROR)

    existing = await pool.fetchrow("SELECT user_id FROM users WHERE email = $1", email)
    if existing is not None:
        raise ValueError(GENERIC_REGISTER_ERROR)

    hashed = hash_password(password)
    row = await pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING user_id, email",
        email,
        hashed,
    )
    return {"user_id": str(row["user_id"]), "email": row["email"]}


async def login_user(pool: asyncpg.Pool, email: str, password: str) -> str:
    """
    Verify credentials and return a JWT access token.
    Raises ValueError on bad email or password.
    """
    email = email.lower().strip()

    row = await pool.fetchrow(
        "SELECT user_id, password_hash FROM users WHERE email = $1",
        email,
    )
    if row is None or not verify_password(password, row["password_hash"]):
        raise ValueError("Invalid email or password")

    return create_access_token(str(row["user_id"]))
