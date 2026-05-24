"""
POST /api/v1/books/upload — size enforcement (audit #5).

Covers:
  - Valid small upload passes the size guards and reaches downstream
    book_service. (We mock the service so no real PDF processing runs.)
  - Oversized upload (file.size > limit, Content-Length honest) is rejected
    with 413 BEFORE any of the body is read.
  - Oversized upload that bypasses file.size (e.g. multipart without
    Content-Length-per-part) is caught by the bounded read fallback.
  - Tiny uploads (<4 bytes) still return 400.
  - Non-PDF extension still returns 400.
  - A renamed non-PDF (.pdf extension but wrong magic bytes) returns 400 (S8).
  - A valid PDF with a mismatched/odd Content-Type is still accepted — content
    validation is by %PDF- magic bytes, not the spoofable Content-Type (S8).
  - Rejected uploads never invoke book_service.create_document /
    process_document.

We temporarily lower _MAX_UPLOAD_BYTES to a small number per test so the
oversized-upload assertions run on a handful of bytes instead of >200 MB.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from ._email_helper import make_test_email


URL = "/api/v1/books/upload"


async def _auth_headers(client: AsyncClient) -> dict[str, str]:
    email = make_test_email()
    await client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _doc_row(*, doc_id: str = "doc-123", user_id: str = "user-456", title: str = "T") -> dict:
    """Shape that books.py:_row_to_doc consumes."""
    now = datetime.now(timezone.utc)
    return {
        "doc_id":        doc_id,
        "user_id":       user_id,
        "title":         title,
        "filename":      "tiny.pdf",
        "total_pages":   None,
        "language":      "de",
        "source_type":   "unknown",
        "status":        "pending",
        "error_message": None,
        "created_at":    now,
        "updated_at":    now,
    }


@pytest.fixture
def _mock_book_service(monkeypatch):
    """Stub book_service so no PDF parsing / DB writes run in upload tests."""
    from backend.routers import books

    create_mock  = AsyncMock(return_value="doc-123")
    get_mock     = AsyncMock(return_value=_doc_row())
    process_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(books.book_service, "create_document", create_mock)
    monkeypatch.setattr(books.book_service, "get_document",    get_mock)
    monkeypatch.setattr(books.book_service, "process_document", process_mock)

    # process_document is wrapped in asyncio.create_task; we let those run
    # to completion before tearing down so the event loop doesn't warn about
    # pending tasks. The mock returns immediately so this is cheap.
    yield {"create": create_mock, "get": get_mock, "process": process_mock}


@pytest.fixture
def _shrink_max_upload(monkeypatch):
    """Shrink the upload cap so 'oversized' tests fit in a few KB."""
    from backend.routers import books
    monkeypatch.setattr(books, "_MAX_UPLOAD_MB", 1)         # 1 MB
    monkeypatch.setattr(books, "_MAX_UPLOAD_BYTES", 1024)   # 1 KB for fast tests


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_small_valid_upload_reaches_downstream(
    client: AsyncClient, _mock_book_service, _shrink_max_upload,
):
    headers = await _auth_headers(client)
    payload = b"%PDF-1.4\n%%\n"  # 12 bytes — valid PDF magic, well under cap

    resp = await client.post(
        URL,
        headers=headers,
        files={"file": ("tiny.pdf", payload, "application/pdf")},
        data={"title": "Tiny", "language": "de"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["doc_id"] == "doc-123"
    _mock_book_service["create"].assert_awaited_once()
    _mock_book_service["get"].assert_awaited_once()
    # process_document is fire-and-forget via asyncio.create_task — give it a
    # tick to execute.
    await asyncio.sleep(0)
    _mock_book_service["process"].assert_awaited_once()


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------

async def test_oversized_upload_returns_413_and_does_not_process(
    client: AsyncClient, _mock_book_service, _shrink_max_upload,
):
    headers = await _auth_headers(client)
    # 2 KB > 1 KB cap. With an honest Content-Length, the route uses
    # file.size to short-circuit before reading any of the body.
    payload = b"%PDF-1.4\n" + b"A" * 2048

    resp = await client.post(
        URL,
        headers=headers,
        files={"file": ("big.pdf", payload, "application/pdf")},
        data={"title": "Big", "language": "de"},
    )

    assert resp.status_code == 413
    assert "exceeds maximum size" in resp.json()["detail"]
    _mock_book_service["create"].assert_not_awaited()
    _mock_book_service["process"].assert_not_awaited()


async def test_oversized_upload_caught_by_bounded_read_when_size_missing(
    _mock_book_service, _shrink_max_upload,
):
    """Defense-in-depth: even if UploadFile.size is None (lying / missing
    Content-Length-per-part), the bounded `await file.read(MAX+1)` catches
    oversized bodies. Invoked at the unit level (no HTTP) so we can force
    .size = None deterministically."""
    from io import BytesIO
    from fastapi import HTTPException, UploadFile
    from backend.routers.books import upload_book

    payload = b"%PDF-1.4\n" + b"B" * 2048   # 2 KB > 1 KB cap
    stub = UploadFile(filename="anon.pdf", file=BytesIO(payload))
    # Force size to None so the early-exit branch is bypassed and only the
    # bounded read can catch the oversize.
    try:
        object.__setattr__(stub, "size", None)
    except Exception:
        stub.size = None  # type: ignore[attr-defined]

    with pytest.raises(HTTPException) as exc:
        await upload_book(
            file=stub, title="anon", language="de",
            user={"user_id": "u1"}, pool=None,
        )
    assert exc.value.status_code == 413
    _mock_book_service["create"].assert_not_awaited()


async def test_tiny_upload_returns_400(
    client: AsyncClient, _mock_book_service, _shrink_max_upload,
):
    headers = await _auth_headers(client)

    resp = await client.post(
        URL,
        headers=headers,
        files={"file": ("tiny.pdf", b"ab", "application/pdf")},  # 2 bytes
        data={"title": "Tiny", "language": "de"},
    )

    assert resp.status_code == 400
    assert "empty or too small" in resp.json()["detail"].lower()
    _mock_book_service["create"].assert_not_awaited()


async def test_non_pdf_extension_returns_400(
    client: AsyncClient, _mock_book_service, _shrink_max_upload,
):
    headers = await _auth_headers(client)

    resp = await client.post(
        URL,
        headers=headers,
        files={"file": ("notes.txt", b"hello world", "text/plain")},
        data={"title": "Notes", "language": "de"},
    )

    assert resp.status_code == 400
    assert "PDF" in resp.json()["detail"]
    _mock_book_service["create"].assert_not_awaited()


async def test_wrong_magic_bytes_rejected(
    client: AsyncClient, _mock_book_service, _shrink_max_upload,
):
    """A renamed non-PDF: .pdf extension + valid size, but the bytes don't start
    with %PDF-. Must be rejected (400) before any processing (S8)."""
    headers = await _auth_headers(client)
    payload = b"This is plain text pretending to be a PDF."  # no %PDF- header

    resp = await client.post(
        URL,
        headers=headers,
        files={"file": ("fake.pdf", payload, "application/pdf")},
        data={"title": "Fake", "language": "de"},
    )

    assert resp.status_code == 400
    assert "valid PDF" in resp.json()["detail"]
    _mock_book_service["create"].assert_not_awaited()
    _mock_book_service["process"].assert_not_awaited()


async def test_valid_magic_with_mismatched_content_type_accepted(
    client: AsyncClient, _mock_book_service, _shrink_max_upload,
):
    """Content-Type is not the gate (it's spoofable / varies by client). A file
    with the real %PDF- magic is accepted even when the Content-Type is wrong —
    proving validation relies on the magic bytes, not Content-Type (S8)."""
    headers = await _auth_headers(client)
    payload = b"%PDF-1.4\n%%\n"  # valid magic

    resp = await client.post(
        URL,
        headers=headers,
        files={"file": ("real.pdf", payload, "application/octet-stream")},
        data={"title": "Real", "language": "de"},
    )

    assert resp.status_code == 200, resp.text
    _mock_book_service["create"].assert_awaited_once()
