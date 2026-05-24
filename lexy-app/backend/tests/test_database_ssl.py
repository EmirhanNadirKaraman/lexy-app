"""S4 — configurable DB TLS for the asyncpg pool.

Unit tests for the ``DB_SSL_MODE`` → asyncpg ``ssl=`` mapping (``_resolve_ssl``),
plus mocked ``create_pool`` calls that confirm the resolved value is forwarded.
No real database or real TLS handshake is required — ``asyncpg.create_pool`` is
patched, so these run without a DB.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import backend.database as db
from backend.database import _resolve_ssl, create_pool


# ---------------------------------------------------------------------------
# _resolve_ssl — pure mapping (no DB needed)
# ---------------------------------------------------------------------------

def test_ssl_mode_unset_defaults_to_disabled():
    # Unset must stay byte-identical to pre-S4 plaintext local dev.
    assert _resolve_ssl(None) is False


def test_ssl_mode_empty_or_whitespace_defaults_to_disabled():
    assert _resolve_ssl("") is False
    assert _resolve_ssl("   ") is False


def test_ssl_mode_disable_is_false():
    assert _resolve_ssl("disable") is False


def test_ssl_mode_require_passes_string_through():
    assert _resolve_ssl("require") == "require"


def test_ssl_mode_verify_ca_passes_string_through():
    assert _resolve_ssl("verify-ca") == "verify-ca"


def test_ssl_mode_verify_full_passes_string_through():
    assert _resolve_ssl("verify-full") == "verify-full"


def test_ssl_mode_is_case_insensitive_and_trimmed():
    assert _resolve_ssl("REQUIRE") == "require"
    assert _resolve_ssl("  Verify-Full ") == "verify-full"


@pytest.mark.parametrize("bad", ["true", "yes", "1", "ssl", "enable", "verifyfull", "prefer", "allow"])
def test_invalid_ssl_mode_raises_clear_error(bad):
    # Includes the libpq fallback modes we intentionally reject (prefer/allow):
    # both can silently drop to plaintext, defeating S4.
    with pytest.raises(ValueError, match="DB_SSL_MODE"):
        _resolve_ssl(bad)


# ---------------------------------------------------------------------------
# create_pool — forwards the resolved ssl value to asyncpg
# ---------------------------------------------------------------------------

async def test_create_pool_forwards_ssl_require(monkeypatch):
    monkeypatch.setenv("DB_SSL_MODE", "require")
    fake = AsyncMock(return_value="POOL")
    try:
        with patch("backend.database.asyncpg.create_pool", fake):
            await create_pool()
        assert fake.await_args.kwargs["ssl"] == "require"
    finally:
        db._pool = None  # don't leak the fake pool into other tests


async def test_create_pool_forwards_ssl_false_when_unset(monkeypatch):
    monkeypatch.delenv("DB_SSL_MODE", raising=False)
    fake = AsyncMock(return_value="POOL")
    try:
        with patch("backend.database.asyncpg.create_pool", fake):
            await create_pool()
        assert fake.await_args.kwargs["ssl"] is False
    finally:
        db._pool = None


async def test_create_pool_raises_on_invalid_mode(monkeypatch):
    monkeypatch.setenv("DB_SSL_MODE", "bogus")
    fake = AsyncMock(return_value="POOL")
    with patch("backend.database.asyncpg.create_pool", fake):
        with pytest.raises(ValueError, match="DB_SSL_MODE"):
            await create_pool()
    # Validation happens before the pool is opened.
    fake.assert_not_awaited()
