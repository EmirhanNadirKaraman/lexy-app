"""
routers/books.py

REST API for Book/PDF reading mode.

Endpoints:
  POST  /api/v1/books/upload                           — upload + ingest PDF
  GET   /api/v1/books                                  — list user's books
  GET   /api/v1/books/{doc_id}                         — book detail / status
  GET   /api/v1/books/{doc_id}/pages                   — list pages
  GET   /api/v1/books/{doc_id}/pages/{page_number}     — page blocks (reading view)
  GET   /api/v1/books/{doc_id}/pages/{page_number}/image — rendered page PNG
  PATCH /api/v1/books/{doc_id}/blocks/{block_id}       — update block
  POST  /api/v1/books/{doc_id}/blocks/{block_id}/llm-repair   — trigger LLM repair
  POST  /api/v1/books/{doc_id}/pages/{page_number}/batch-llm-repair — repair low-conf blocks
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import anthropic
import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..core.deps import get_current_user, rate_limit_llm
from ..database import get_pool
from ..models.schemas import (
    BlockPatchRequest,
    BookBlockRead,
    BookDocumentRead,
    BookPageDetail,
    BookPageSummary,
    LLMRepairResponse,
    SentenceCountUpdate,
    StoredBlockToken,
)
from ..services import book_service, book_llm_service

router = APIRouter(tags=["books"])
logger = logging.getLogger(__name__)

_MAX_UPLOAD_MB = 200
_MAX_UPLOAD_BYTES = _MAX_UPLOAD_MB * 1024 * 1024
# Every PDF begins with the "%PDF-" header (ISO 32000). We validate these magic
# bytes rather than trusting the filename extension or the client-supplied
# Content-Type — both are spoofable/unreliable — before handing the file to the
# expensive extraction pipeline. (S8)
_PDF_MAGIC = b"%PDF-"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_doc(row: dict) -> BookDocumentRead:
    return BookDocumentRead(
        doc_id=str(row["doc_id"]),
        user_id=str(row["user_id"]),
        title=row["title"],
        filename=row["filename"],
        total_pages=row["total_pages"],
        language=row["language"],
        source_type=row["source_type"],
        status=row["status"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_block(b: dict) -> BookBlockRead:
    display_text = book_service._resolve_display_text(
        user_text_override=b.get("user_text_override"),
        correction_status=b.get("correction_status", "none"),
        corrected_text=b.get("corrected_text"),
        clean_text=b.get("clean_text"),
    )
    # Parse tokens from JSONB (may be string or list depending on asyncpg version)
    raw_tokens = b.get("tokens")
    if isinstance(raw_tokens, str):
        try:
            raw_tokens = json.loads(raw_tokens)
        except (json.JSONDecodeError, TypeError):
            raw_tokens = []
    if not isinstance(raw_tokens, list):
        raw_tokens = []
    tokens = [StoredBlockToken(**t) for t in raw_tokens]
    return BookBlockRead(
        block_id=b["block_id"],
        block_index=b["block_index"],
        block_type=b["block_type"],
        bbox_x0=b.get("bbox_x0"),
        bbox_y0=b.get("bbox_y0"),
        bbox_x1=b.get("bbox_x1"),
        bbox_y1=b.get("bbox_y1"),
        ocr_text=b.get("ocr_text"),
        clean_text=b.get("clean_text"),
        corrected_text=b.get("corrected_text"),
        correction_status=b.get("correction_status", "none"),
        ocr_confidence=b.get("ocr_confidence"),
        is_header_footer=b.get("is_header_footer", False),
        user_text_override=b.get("user_text_override"),
        display_text=display_text,
        tokens=tokens,
    )


# ── Upload ────────────────────────────────────────────────────────────────────

@router.post("/books/upload", response_model=BookDocumentRead)
async def upload_book(
    file: UploadFile = File(...),
    title: str = Form(...),
    language: str = Form(default="de"),
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    # Size enforcement is two-layered:
    #   1. If Starlette set file.size from Content-Length, reject *before*
    #      reading any of the body. Honest clients pay no I/O for the rejection.
    #   2. Bounded read of at most _MAX_UPLOAD_BYTES+1 bytes. Defends against
    #      a missing/lying Content-Length: we only ever materialise max+1
    #      bytes in memory instead of an unbounded body. Note: Starlette has
    #      already spooled the full request body to disk before this handler
    #      runs — the production-grade fix for the spool itself belongs in
    #      middleware or the reverse proxy. This route-level guard at least
    #      keeps the in-memory bytes object capped.
    if file.size is not None and file.size > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {_MAX_UPLOAD_MB} MB",
        )

    file_bytes = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {_MAX_UPLOAD_MB} MB",
        )
    if len(file_bytes) < 4:
        raise HTTPException(status_code=400, detail="File is empty or too small")

    # Content validation (S8): the .pdf extension and Content-Type are not
    # trustworthy, so confirm the actual bytes start with the PDF magic header
    # before any processing. This rejects a renamed non-PDF (e.g. an .exe or a
    # zip with a .pdf name) up front.
    if not file_bytes.startswith(_PDF_MAGIC):
        raise HTTPException(
            status_code=400,
            detail="File is not a valid PDF (missing %PDF- header)",
        )

    doc_id = await book_service.create_document(
        pool,
        user_id=str(user["user_id"]),
        title=title.strip() or file.filename,
        filename=file.filename,
        language=language,
        file_bytes=file_bytes,
    )

    # Kick off background processing without blocking the response
    pdf_path = book_service._BOOKS_DIR / f"{doc_id}.pdf"
    asyncio.create_task(
        book_service.process_document(pool, doc_id, pdf_path, language)
    )

    row = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    return _row_to_doc(row)


# ── Book list / detail ────────────────────────────────────────────────────────

@router.get("/books", response_model=list[BookDocumentRead])
async def list_books(
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    rows = await book_service.list_documents(pool, str(user["user_id"]))
    return [_row_to_doc(r) for r in rows]


@router.get("/books/{doc_id}", response_model=BookDocumentRead)
async def get_book(
    doc_id: str,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    row = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    return _row_to_doc(row)


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("/books/{doc_id}/pages", response_model=list[BookPageSummary])
async def list_pages(
    doc_id: str,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    # Verify ownership
    doc = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    if not doc:
        raise HTTPException(status_code=404, detail="Book not found")

    pages = await book_service.list_pages(pool, doc_id)
    return [BookPageSummary(**p) for p in pages]


@router.get("/books/{doc_id}/pages/{page_number}", response_model=BookPageDetail)
async def get_page(
    doc_id: str,
    page_number: int,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    doc = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    if not doc:
        raise HTTPException(status_code=404, detail="Book not found")

    page = await book_service.get_page_detail(pool, doc_id, page_number)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    return BookPageDetail(
        page_id=page["page_id"],
        page_number=page["page_number"],
        is_scanned=page["is_scanned"],
        has_image=page["has_image"],
        width_pt=page.get("width_pt"),
        height_pt=page.get("height_pt"),
        blocks=[_row_to_block(b) for b in page["blocks"]],
    )


@router.patch("/books/{doc_id}/pages/{page_number}/sentence-count", status_code=204)
async def update_sentence_count(
    doc_id: str,
    page_number: int,
    body: SentenceCountUpdate,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    doc = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    if not doc:
        raise HTTPException(status_code=404, detail="Book not found")
    await book_service.update_sentence_count(pool, doc_id, page_number, body.sentence_count)


@router.get("/books/{doc_id}/pages/{page_number}/image")
async def get_page_image(
    doc_id: str,
    page_number: int,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    doc = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    if not doc:
        raise HTTPException(status_code=404, detail="Book not found")

    image_path = await book_service.get_page_image_path(pool, doc_id, page_number)
    if not image_path or not Path(image_path).exists():
        raise HTTPException(status_code=404, detail="Page image not available")

    return FileResponse(image_path, media_type="image/png")


# ── Block updates ─────────────────────────────────────────────────────────────

@router.patch("/books/{doc_id}/blocks/{block_id}", response_model=BookBlockRead)
async def patch_block(
    doc_id: str,
    block_id: int,
    body: BlockPatchRequest,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    doc = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    if not doc:
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        updated = await book_service.patch_block(
            pool,
            block_id=block_id,
            doc_id=doc_id,
            block_type=body.block_type,
            user_text_override=body.user_text_override,
            correction_status=body.correction_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not updated:
        raise HTTPException(status_code=404, detail="Block not found")

    return _row_to_block(updated)


# ── LLM repair ────────────────────────────────────────────────────────────────

@router.post(
    "/books/{doc_id}/blocks/{block_id}/llm-repair",
    response_model=LLMRepairResponse,
    dependencies=[Depends(rate_limit_llm)],
)
async def llm_repair_block(
    doc_id: str,
    block_id: int,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    doc = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    if not doc:
        raise HTTPException(status_code=404, detail="Book not found")

    block = await book_service.get_block(pool, block_id, doc_id)
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")

    corrected = await book_llm_service.repair_block_by_id(pool, block_id, doc_id)

    return LLMRepairResponse(
        block_id=block_id,
        ocr_text=block.get("ocr_text"),
        corrected_text=corrected,
        correction_status="suggested",
    )


@router.delete("/books/{doc_id}", status_code=204)
async def delete_book(
    doc_id: str,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    """Delete a book. Owner or admin only."""
    deleted = await book_service.delete_document(
        pool, doc_id, str(user["user_id"]), is_admin=user.get("is_admin", False)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Book not found or not authorized")


@router.delete("/books/{doc_id}/pages/{page_number}", status_code=204)
async def delete_page(
    doc_id: str,
    page_number: int,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    """Delete a single page (and its blocks). Owner or admin only."""
    deleted = await book_service.delete_page(
        pool, doc_id, page_number, str(user["user_id"]), is_admin=user.get("is_admin", False)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Page not found or not authorized")


@router.post(
    "/books/{doc_id}/pages/{page_number}/batch-llm-repair",
    dependencies=[Depends(rate_limit_llm)],
)
async def batch_llm_repair(
    doc_id: str,
    page_number: int,
    user=Depends(get_current_user),
    pool=Depends(get_pool),
):
    """Trigger LLM repair for all low-confidence blocks on a page."""
    doc = await book_service.get_document(pool, doc_id, str(user["user_id"]))
    if not doc:
        raise HTTPException(status_code=404, detail="Book not found")

    candidates = await book_service.list_low_confidence_blocks(pool, doc_id, page_number)
    if not candidates:
        return {"repaired": 0, "message": "No low-confidence blocks found"}

    repaired = 0
    errors   = 0
    for block in candidates:
        try:
            await book_llm_service.repair_block_by_id(pool, block["block_id"], doc_id)
            repaired += 1
        except (anthropic.APIError, asyncpg.PostgresError) as exc:
            # Known failure modes: Anthropic outage / rate-limit / model error,
            # or a DB error during the per-block UPDATE. Log and continue —
            # one bad block must not lose progress made on the others in the batch.
            logger.warning("LLM repair failed for block %s: %s", block["block_id"], exc)
            errors += 1
        except Exception:
            # Defensive top-level catch — a bug in repair_block_by_id should not
            # halt the whole batch and lose already-repaired blocks. Logged at
            # exception level so the full trace is captured.
            logger.exception("LLM repair: unexpected error for block %s", block["block_id"])
            errors += 1

    return {"repaired": repaired, "errors": errors, "total_candidates": len(candidates)}
