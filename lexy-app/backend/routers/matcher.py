from fastapi import APIRouter, Query, Request

from ..core.deps import rate_limit_sentence_match
from ..models.schemas import MatchRequest, MatchResponse
from ..services import matcher_service

router = APIRouter(prefix="/sentences", tags=["matcher"])


@router.post("/match", response_model=MatchResponse)
async def match_sentence(
    body: MatchRequest,
    request: Request,
    language: str = Query(default="de"),
):
    """Extract phrases from a sentence.

    `language` defaults to 'de' for back-compat with pre-Stage-1 callers
    that didn't pass a language query parameter. Non-German content
    routes through the dispatcher and returns an empty phrases list
    (words-only v1 for L2).

    This route is public (the frontend doesn't call it), so it is throttled
    per client IP (S16) and the input length is capped on `MatchRequest`
    (S16) — both guard against unauthenticated spaCy/CPU exhaustion. The
    rate-limit check fires before any parsing; an over-length body is
    rejected by schema validation (422) before the handler runs.
    """
    await rate_limit_sentence_match(request)
    phrases = await matcher_service.match_sentence(body.sentence, language)
    return MatchResponse(sentence=body.sentence, phrases=phrases)
