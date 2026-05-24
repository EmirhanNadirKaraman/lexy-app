"""S11 — interactive API docs / OpenAPI schema are gated off by default.

`/docs`, `/redoc`, and `/openapi.json` are only mounted when `ENABLE_DOCS` is
truthy, so a production deploy doesn't publish the route+model schema. The parse
helper is unit-tested with a monkeypatched env; the live app is checked
env-adaptively (its docs URLs must agree with `_docs_enabled()` — which is fixed
at import time, so the integration test reads the ambient flag rather than
setting it).
"""
from httpx import AsyncClient

import pytest

from backend.main import _docs_enabled, app


# ---------------------------------------------------------------------------
# _docs_enabled — env parsing (deterministic, monkeypatched)
# ---------------------------------------------------------------------------

def test_docs_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    assert _docs_enabled() is False


@pytest.mark.parametrize("val", ["true", "TRUE", "True", "1", "yes", "YES"])
def test_docs_enabled_for_truthy_values(monkeypatch, val):
    monkeypatch.setenv("ENABLE_DOCS", val)
    assert _docs_enabled() is True


@pytest.mark.parametrize("val", ["", "false", "0", "no", "off", "garbage"])
def test_docs_disabled_for_falsy_or_unknown_values(monkeypatch, val):
    monkeypatch.setenv("ENABLE_DOCS", val)
    assert _docs_enabled() is False


# ---------------------------------------------------------------------------
# Live app — docs config + endpoints agree with the flag (env-adaptive)
# ---------------------------------------------------------------------------

def test_app_docs_config_matches_flag():
    # app.{docs,redoc,openapi}_url are set at import; assert the CAUSE (route not
    # registered) rather than just a 404 symptom.
    if _docs_enabled():
        assert app.docs_url == "/docs"
        assert app.redoc_url == "/redoc"
        assert app.openapi_url == "/openapi.json"
    else:
        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None


async def test_docs_endpoints_404_when_disabled(client: AsyncClient):
    # Belt-and-braces: in the default (flag-off) config the endpoints are gone.
    if _docs_enabled():
        pytest.skip("ENABLE_DOCS is set in this environment; off-path not exercised")
    for path in ("/docs", "/redoc", "/openapi.json"):
        resp = await client.get(path)
        assert resp.status_code == 404
