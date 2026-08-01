"""`GET /books/packages` and `POST /books/import` are admin-only (rt-01).

Both are the operator ingestion path (`docs/INGESTION_PIPELINE.md` §5), not an
end-user feature. Package names are server-side operator inventory — they are
not scoped to any user — and an import makes the server load, checksum-verify
and structurally validate whatever package it is handed. The two endpoints were
auth-gated but not admin-gated, so any registered learner could enumerate that
inventory and spend server compute on demand, and would gain a write path into
`book_blocks` the moment roadmap A3 activates a real persistence backend.

`is_admin` is planted with direct SQL, deliberately NOT through the settings
API: that API filters writes to `settings_service.DEFAULTS`, and `is_admin` is
not in it — un-self-grantability is the control this gate rests on, so a test
that routed around it would prove nothing. Same pattern (and same SQL) as
`test_phrases_seed_admin.py`, the other admin-gate suite.
"""
from pathlib import Path

from httpx import AsyncClient

from ._auth_helper import register_and_login
from ._email_helper import make_test_email

PACKAGES = "/api/v1/books/packages"
IMPORT = "/api/v1/books/import"


async def _make_admin(db_pool, user_id) -> None:
    """Merge `is_admin` into the user's settings JSONB, preserving other keys.
    No new token needed — `is_admin` is read from the users row per request."""
    await db_pool.execute(
        "UPDATE users SET settings = COALESCE(settings::jsonb, '{}'::jsonb) "
        "|| '{\"is_admin\": true}'::jsonb WHERE user_id = $1::uuid",
        str(user_id),
    )


async def _register_admin(client: AsyncClient, db_pool) -> dict:
    headers, uid = await register_and_login(client, db_pool, make_test_email())
    await _make_admin(db_pool, uid)
    return headers


def _plant_package(root: Path, name: str = "operator-pkg") -> str:
    """The minimum `discover_packages` counts as a package: a directory holding
    a manifest. Nothing here loads it — these tests are about *who may ask*,
    not about what a valid package contains (that is `test_document_package`).
    """
    pkg = root / name
    pkg.mkdir()
    (pkg / "manifest.json").write_text("{}", encoding="utf-8")
    return name


# ── Listing ───────────────────────────────────────────────────────────────────

async def test_listing_forbidden_for_non_admin(client: AsyncClient, db_pool):
    headers, _ = await register_and_login(client, db_pool, make_test_email())
    r = await client.get(PACKAGES, headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


async def test_listing_leaks_no_name_to_a_non_admin(
    client: AsyncClient, db_pool, tmp_path, monkeypatch
):
    """With a package actually present, so the 403 is the gate refusing rather
    than an empty inventory looking like one."""
    name = _plant_package(tmp_path)
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path))

    headers, _ = await register_and_login(client, db_pool, make_test_email())
    r = await client.get(PACKAGES, headers=headers)
    assert r.status_code == 403
    assert name not in r.text


async def test_listing_allowed_for_admin(
    client: AsyncClient, db_pool, tmp_path, monkeypatch
):
    name = _plant_package(tmp_path)
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path))

    headers = await _register_admin(client, db_pool)
    r = await client.get(PACKAGES, headers=headers)
    assert r.status_code == 200
    assert r.json() == [name]


async def test_listing_requires_auth(client: AsyncClient):
    # HTTPBearer → 403 with no header; 401 with a present-but-invalid one.
    assert (await client.get(PACKAGES)).status_code in (401, 403)


# ── Import ────────────────────────────────────────────────────────────────────

async def test_import_forbidden_for_non_admin(client: AsyncClient, db_pool):
    headers, _ = await register_and_login(client, db_pool, make_test_email())
    r = await client.post(IMPORT, json={"package_name": "operator-pkg"}, headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


async def test_non_admin_import_never_reaches_the_service(
    client: AsyncClient, db_pool, monkeypatch
):
    """The gate must stop the work, not merely annotate the response.

    A refused caller must cost the server no package I/O at all — that is the
    whole point of gating a route whose body hashes files on demand. Patched on
    the module object the router itself holds, so this fails if the dependency
    is ever reordered behind the handler body.
    """
    from backend.routers import books as books_router

    async def _explode(*args, **kwargs):
        raise AssertionError("import ran despite a non-admin caller")

    monkeypatch.setattr(books_router.book_import_service, "import_package", _explode)

    headers, _ = await register_and_login(client, db_pool, make_test_email())
    r = await client.post(IMPORT, json={"package_name": "operator-pkg"}, headers=headers)
    assert r.status_code == 403


async def test_import_allowed_for_admin(client: AsyncClient, db_pool):
    """Admin behaviour is unchanged: an unknown package is still **200 with a
    rejected result**, because the body is the diagnostic (see the handler
    docstring). The gate changed who may ask, not what the answer is."""
    headers = await _register_admin(client, db_pool)
    r = await client.post(IMPORT, json={"package_name": "does-not-exist"}, headers=headers)
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "rejected"
    assert payload["errors"][0]["code"] == "package_unreadable"
    assert payload["dry_run"] is True


async def test_import_blank_name_is_422_for_an_admin(client: AsyncClient, db_pool):
    """Authorization now fires before request validation, so this contract is
    only observable by a caller who can reach the handler."""
    headers = await _register_admin(client, db_pool)
    r = await client.post(IMPORT, json={"package_name": "   "}, headers=headers)
    assert r.status_code == 422


async def test_import_requires_auth(client: AsyncClient):
    r = await client.post(IMPORT, json={"package_name": "operator-pkg"})
    assert r.status_code in (401, 403)
