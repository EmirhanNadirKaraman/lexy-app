"""Lemma correction candidate endpoints (#39 slice 3A).

POST /api/v1/lemma-corrections        — any authenticated user flags a bad lemma
GET  /api/v1/admin/lemma-corrections  — admin-only review queue (read-only)

Signal only: neither endpoint writes `lemma_override`. Promotion (accept/reject
→ override) is slice 3B. See docs/LEMMA_OVERRIDE_WORKFLOW.md.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..core.deps import get_current_user, require_admin
from ..database import get_pool
from ..models.schemas import (
    LemmaCorrectionAccept,
    LemmaCorrectionCreate,
    LemmaCorrectionRead,
    LemmaCorrectionReject,
    LemmaCorrectionReviewResult,
)
from ..services import lemma_correction_service, rate_limiter

router = APIRouter(tags=["lemma-corrections"])


@router.post("/lemma-corrections", response_model=LemmaCorrectionRead, status_code=201)
async def flag_lemma(
    body: LemmaCorrectionCreate,
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """Flag a bad lemma/canonical. Creates a pending candidate, or bumps the
    `report_count` on a pending duplicate (cross-user). Per-user throttled.
    **Never** writes `lemma_override` — promotion is admin review (slice 3B)."""
    await rate_limiter.check_window(
        f"lemma_correction:{current_user['user_id']}",
        limit=rate_limiter.LEMMA_CORRECTION_MAX_REPORTS,
        window_seconds=rate_limiter.LEMMA_CORRECTION_WINDOW_SECONDS,
        detail=rate_limiter.PUBLIC_RATE_LIMIT_MESSAGE,
    )
    return await lemma_correction_service.create_candidate(
        pool,
        user_id=str(current_user["user_id"]),
        language=body.language,
        surface_form=body.surface_form,
        observed_lemma=body.observed_lemma,
        suggested_lemma=body.suggested_lemma,
        context_text=body.context_text,
        item_type=body.item_type,
        item_id=body.item_id,
        sentence_id=body.sentence_id,
    )


@router.get("/admin/lemma-corrections", response_model=list[LemmaCorrectionRead])
async def list_lemma_corrections(
    # NB: this `status` param shadows the imported `fastapi.status` WITHIN this
    # function only — safe today (this body uses no HTTP status constants). The
    # accept/reject handlers below have no such param, so their `status.HTTP_*`
    # references resolve to the import.
    status: str = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=100),
    pool=Depends(get_pool),
    _admin: dict = Depends(require_admin),
):
    """Admin review queue — read-only. Lists newest candidates of the given
    status (default 'pending'), capped at 100. Accept/reject + promotion to
    `lemma_override` is slice 3B."""
    return await lemma_correction_service.list_candidates(pool, status=status, limit=limit)


def _review_http_error(exc: Exception) -> HTTPException:
    """Map a lemma_correction_service review exception to an HTTPException."""
    svc = lemma_correction_service
    if isinstance(exc, svc.CandidateNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, "candidate_not_found")
    if isinstance(exc, svc.CandidateNotPending):
        return HTTPException(status.HTTP_409_CONFLICT, f"candidate_not_pending:{exc.status}")
    if isinstance(exc, svc.NothingToPromote):
        return HTTPException(status.HTTP_400_BAD_REQUEST, "nothing_to_promote")
    raise exc  # unexpected — let it surface as a 500


@router.post("/admin/lemma-corrections/{candidate_id}/accept",
             response_model=LemmaCorrectionReviewResult)
async def accept_lemma_correction(
    candidate_id: int,
    body: LemmaCorrectionAccept | None = None,
    pool=Depends(get_pool),
    admin: dict = Depends(require_admin),
):
    """Promote a pending candidate to `lemma_override` and mark it accepted —
    atomically. The corrected lemma is the request's `corrected_lemma`, else the
    candidate's `suggested_lemma`; if both are blank → 400. 404 unknown, 409
    already-reviewed."""
    body = body or LemmaCorrectionAccept()
    try:
        return await lemma_correction_service.accept_candidate(
            pool,
            candidate_id=candidate_id,
            admin_id=str(admin["user_id"]),
            corrected_lemma=body.corrected_lemma,
            review_note=body.review_note,
        )
    except (lemma_correction_service.CandidateNotFound,
            lemma_correction_service.CandidateNotPending,
            lemma_correction_service.NothingToPromote) as exc:
        raise _review_http_error(exc)


@router.post("/admin/lemma-corrections/{candidate_id}/reject",
             response_model=LemmaCorrectionReviewResult)
async def reject_lemma_correction(
    candidate_id: int,
    body: LemmaCorrectionReject | None = None,
    pool=Depends(get_pool),
    admin: dict = Depends(require_admin),
):
    """Mark a pending candidate rejected. Never writes `lemma_override`. 404
    unknown, 409 already-reviewed."""
    note = body.review_note if body else None
    try:
        return await lemma_correction_service.reject_candidate(
            pool,
            candidate_id=candidate_id,
            admin_id=str(admin["user_id"]),
            review_note=note,
        )
    except (lemma_correction_service.CandidateNotFound,
            lemma_correction_service.CandidateNotPending) as exc:
        raise _review_http_error(exc)
