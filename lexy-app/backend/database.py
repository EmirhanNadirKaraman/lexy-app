import os
from pathlib import Path
from dotenv import load_dotenv
import asyncpg

load_dotenv(Path(__file__).parent.parent.parent / ".env")

_pool: asyncpg.Pool | None = None

# DB_SSL_MODE → asyncpg `ssl=` argument (S4). This is a security knob, not a
# libpq pass-through: we deliberately exclude `prefer`/`allow` because both fall
# back to plaintext on negotiation failure — the exact exposure S4 fixes. Use
# `require` (encrypt, no cert check) for a trusted network, or `verify-ca` /
# `verify-full` (encrypt + validate the server cert against the system CA store)
# for managed Postgres reached over the public internet.
_VALID_SSL_MODES = frozenset({"disable", "require", "verify-ca", "verify-full"})


def _resolve_ssl(mode: str | None) -> bool | str:
    """Translate the DB_SSL_MODE env value into asyncpg's `ssl=` argument.

    Unset/empty defaults to ``"disable"`` so local dev stays plaintext and
    byte-identical to the pre-S4 behaviour. ``"disable"`` becomes ``ssl=False``
    (explicit no-TLS); every other accepted mode is passed straight through —
    asyncpg accepts the libpq sslmode strings and builds the SSL context
    (``verify-*`` validate via ``ssl.create_default_context()``). An
    unrecognised value raises immediately so a typo fails at startup instead of
    silently opening a plaintext pool.
    """
    raw = (mode or "").strip().lower()
    if not raw:
        raw = "disable"
    if raw not in _VALID_SSL_MODES:
        raise ValueError(
            f"Invalid DB_SSL_MODE={mode!r}. "
            f"Expected one of: {', '.join(sorted(_VALID_SSL_MODES))}."
        )
    return False if raw == "disable" else raw


def resolve_sslmode(mode: str | None) -> str | None:
    """Translate DB_SSL_MODE into a libpq `sslmode` string for psycopg2 /
    SQLAlchemy callers (S4 residual: Alembic `migrations/env.py` and the
    standalone `subtitle-scraper` scripts).

    Same accepted values and same rejection of `prefer`/`allow` as the asyncpg
    `_resolve_ssl` above. The principle is identical — *preserve the driver's
    default when unset* — but the concrete result differs because the drivers'
    defaults differ: asyncpg's no-arg default is already no-TLS, so `_resolve_ssl`
    returns ``ssl=False``; libpq's default is opportunistic `prefer`, so here we
    return ``None`` for `unset`/`disable` and the caller OMITS `sslmode`, leaving
    libpq's default untouched (zero behaviour change). `require`/`verify-ca`/
    `verify-full` are returned verbatim; `prefer`/`allow`/anything else raises so
    a typo can't silently weaken TLS.
    """
    raw = (mode or "").strip().lower()
    if raw in ("", "disable"):
        return None
    if raw not in _VALID_SSL_MODES:
        raise ValueError(
            f"Invalid DB_SSL_MODE={mode!r}. "
            f"Expected one of: {', '.join(sorted(_VALID_SSL_MODES))}."
        )
    return raw


async def create_pool():
    global _pool
    _pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        ssl=_resolve_ssl(os.getenv("DB_SSL_MODE")),
    )


async def close_pool():
    if _pool:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    return _pool
