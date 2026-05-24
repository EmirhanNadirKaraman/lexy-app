"""DB_SSL_MODE → psycopg2 `sslmode` for the standalone scraper scripts (S4).

A deliberate small duplicate of `backend.database.resolve_sslmode`: the scraper
is standalone and doesn't import the FastAPI backend. Same policy —
`unset`/`disable` omit the kwarg (preserve libpq's default; zero behaviour
change), `require`/`verify-ca`/`verify-full` enforce TLS, and `prefer`/`allow`
are rejected because they can silently fall back to plaintext (the exposure S4
fixes). Run against a remote DB with DB_SSL_MODE=require (or verify-full).
"""
import os

_VALID = {"disable", "require", "verify-ca", "verify-full"}


def sslmode_from_env() -> str | None:
    """Resolve DB_SSL_MODE to a libpq sslmode string, or None to omit it.

    None → caller leaves `sslmode` off (libpq default). require/verify-ca/
    verify-full → that string. prefer/allow/anything-else → ValueError.
    """
    raw = (os.getenv("DB_SSL_MODE") or "").strip().lower()
    if raw in ("", "disable"):
        return None
    if raw not in _VALID:
        raise ValueError(
            f"Invalid DB_SSL_MODE={os.getenv('DB_SSL_MODE')!r}. "
            f"Expected one of: {', '.join(sorted(_VALID))}."
        )
    return raw


def connect_kwargs() -> dict:
    """`{'sslmode': <mode>}` when TLS is enforced, else `{}` (omit the kwarg)."""
    mode = sslmode_from_env()
    return {"sslmode": mode} if mode else {}
