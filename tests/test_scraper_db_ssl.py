"""S4 residual — DB_SSL_MODE for the scraper's psycopg2 connections.

Covers `subtitle-scraper/db_ssl.py` (the standalone resolver) and verifies that
a scraper `connect()` forwards the resolved `sslmode` to psycopg2 (using
`seed_channels`, which is scraper-only so its module name doesn't collide with
the repo-root `pipeline.py`; all five sites share the same `**connect_kwargs()`
wiring). Policy mirrors `backend.database.resolve_sslmode`: unset/disable → omit
the kwarg (preserve libpq's default), require/verify-* → enforce, prefer/allow →
rejected. No real DB or TLS handshake — `psycopg2.connect` is monkeypatched.

Isolation: scraper modules are imported via fixtures that add the scraper dir to
`sys.path` only if absent (removing it again only if this fixture added it) and
`sys.modules.pop` the target first. No module-level `sys.path`/import side
effects, so this file never pollutes (or gets polluted by) `test_scraper_channels`.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRAPER_DIR = str(Path(__file__).resolve().parents[1] / "subtitle-scraper")


def _import_scraper(name: str):
    added = SCRAPER_DIR not in sys.path
    if added:
        sys.path.insert(0, SCRAPER_DIR)
    sys.modules.pop(name, None)  # fresh import from SCRAPER_DIR, not a stale cache
    module = importlib.import_module(name)
    return module, added


@pytest.fixture
def db_ssl():
    module, added = _import_scraper("db_ssl")
    try:
        yield module
    finally:
        sys.modules.pop("db_ssl", None)
        if added:
            try:
                sys.path.remove(SCRAPER_DIR)
            except ValueError:
                pass


@pytest.fixture
def seed_channels():
    # seed_channels imports db_ssl; pop both so they resolve from SCRAPER_DIR.
    sys.modules.pop("db_ssl", None)
    module, added = _import_scraper("seed_channels")
    try:
        yield module
    finally:
        sys.modules.pop("seed_channels", None)
        sys.modules.pop("db_ssl", None)
        if added:
            try:
                sys.path.remove(SCRAPER_DIR)
            except ValueError:
                pass


# --- resolver ---------------------------------------------------------------

def test_sslmode_unset_returns_none(db_ssl, monkeypatch):
    monkeypatch.delenv("DB_SSL_MODE", raising=False)
    assert db_ssl.sslmode_from_env() is None


def test_sslmode_disable_returns_none(db_ssl, monkeypatch):
    monkeypatch.setenv("DB_SSL_MODE", "disable")
    assert db_ssl.sslmode_from_env() is None


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_sslmode_enforced_values_pass_through(db_ssl, monkeypatch, mode):
    monkeypatch.setenv("DB_SSL_MODE", mode)
    assert db_ssl.sslmode_from_env() == mode


def test_sslmode_case_insensitive(db_ssl, monkeypatch):
    monkeypatch.setenv("DB_SSL_MODE", "REQUIRE")
    assert db_ssl.sslmode_from_env() == "require"


@pytest.mark.parametrize("bad", ["prefer", "allow", "true", "1", "ssl", "verifyfull"])
def test_sslmode_invalid_or_fallback_modes_raise(db_ssl, monkeypatch, bad):
    monkeypatch.setenv("DB_SSL_MODE", bad)
    with pytest.raises(ValueError, match="DB_SSL_MODE"):
        db_ssl.sslmode_from_env()


# --- connect_kwargs ---------------------------------------------------------

def test_connect_kwargs_omits_when_unset(db_ssl, monkeypatch):
    monkeypatch.delenv("DB_SSL_MODE", raising=False)
    assert db_ssl.connect_kwargs() == {}


def test_connect_kwargs_includes_when_enforced(db_ssl, monkeypatch):
    monkeypatch.setenv("DB_SSL_MODE", "require")
    assert db_ssl.connect_kwargs() == {"sslmode": "require"}


# --- a scraper connect() forwards sslmode to psycopg2 -----------------------

def test_connect_forwards_sslmode(seed_channels, monkeypatch):
    monkeypatch.setenv("DB_SSL_MODE", "require")
    fake = MagicMock(return_value="CONN")
    monkeypatch.setattr(seed_channels.psycopg2, "connect", fake)
    seed_channels.connect()
    assert fake.call_args.kwargs.get("sslmode") == "require"


def test_connect_omits_sslmode_when_unset(seed_channels, monkeypatch):
    monkeypatch.delenv("DB_SSL_MODE", raising=False)
    fake = MagicMock(return_value="CONN")
    monkeypatch.setattr(seed_channels.psycopg2, "connect", fake)
    seed_channels.connect()
    assert "sslmode" not in fake.call_args.kwargs
