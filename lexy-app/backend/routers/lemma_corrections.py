"""Lemma correction candidate endpoints (#39 slice 3A).

POST /api/v1/lemma-corrections        — any authenticated user flags a bad lemma
GET  /api/v1/admin/lemma-corrections  — admin-only review queue (read-only)

Signal only: neither endpoint writes `lemma_override`. Promotion (accept/reject
→ override) is slice 3B. See docs/LEMMA_OVERRIDE_WORKFLOW.md.
"""
from fastapi import APIRouter, Depends, Query

from ..core.deps import get_current_user, require_admin
from ..database import get_pool
from ..models.schemas import LemmaCorrectionCreate, LemmaCorrectionRead
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
    status: str = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=100),
    pool=Depends(get_pool),
    _admin: dict = Depends(require_admin),
):
    """Admin review queue — read-only. Lists newest candidates of the given
    status (default 'pending'), capped at 100. Accept/reject + promotion to
    `lemma_override` is slice 3B."""
    return await lemma_correction_service.list_candidates(pool, status=status, limit=limit)
