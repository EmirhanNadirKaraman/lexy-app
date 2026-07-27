from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import PlainTextResponse

from ..core.deps import get_current_user
from ..database import get_pool
from ..models.schemas import (
    WordListCreate,
    WordListDetail,
    WordListMarkLearningResult,
    WordListSummary,
)
from ..services import word_list_service

router = APIRouter(prefix="/word-lists", tags=["word-lists"])

# Every route is scoped by (list_id, user_id) in the service layer and answers
# 404 — not 403 — when the row belongs to someone else. A 403 would confirm the
# id exists, letting one user enumerate another's lists.
_NOT_FOUND = "Word list not found"


@router.post("", response_model=WordListDetail, status_code=201)
async def create_word_list(
    body: WordListCreate,
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """
    Create a list from pasted or uploaded words.

    Surfaces are stripped, deduped case-insensitively (first spelling wins),
    and resolved against word_table language-scoped + case-insensitively.
    Nothing is dropped: a surface with no match is stored as `unresolved`, and
    one with several matches as `ambiguous` rather than being bound to an
    arbitrary sense.
    """
    try:
        return await word_list_service.create_list(
            pool,
            str(current_user["user_id"]),
            name=body.name,
            language=body.language,
            words=body.words,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=list[WordListSummary])
async def get_word_lists(
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    return await word_list_service.list_lists(pool, str(current_user["user_id"]))


@router.get("/{list_id}", response_model=WordListDetail)
async def get_word_list(
    list_id: int = Path(..., ge=1),
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    result = await word_list_service.get_list(
        pool, str(current_user["user_id"]), list_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return result


@router.get("/{list_id}/export", response_class=PlainTextResponse)
async def export_word_list(
    list_id: int = Path(..., ge=1),
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """One surface per line, insertion order, unresolved and ambiguous included."""
    text = await word_list_service.export_list(
        pool, str(current_user["user_id"]), list_id,
    )
    if text is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return PlainTextResponse(
        content=text,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="word-list-{list_id}.txt"'},
    )


@router.post("/{list_id}/mark-unknown-learning", response_model=WordListMarkLearningResult)
async def mark_unknown_learning(
    list_id: int = Path(..., ge=1),
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """
    Mark every resolved-but-unknown entry as 'learning'.

    Runs through progression_service with `status_marked_learning` +
    `status_override="learning"` — the same path as the words.py status route —
    so levels, auto-promotion and both SRS cards match a hand-marked word.
    Unresolved and ambiguous entries are skipped; already learning/known
    entries are untouched, so repeat calls are idempotent.
    """
    result = await word_list_service.mark_unknown_as_learning(
        pool, str(current_user["user_id"]), list_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return result


@router.delete("/{list_id}", status_code=204)
async def delete_word_list(
    list_id: int = Path(..., ge=1),
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    deleted = await word_list_service.delete_list(
        pool, str(current_user["user_id"]), list_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
