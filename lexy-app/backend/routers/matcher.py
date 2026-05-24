from fastapi import APIRouter, Depends, Query

from ..core.deps import get_current_user
from ..models.schemas import MatchRequest, MatchResponse
from ..services import matcher_service

router = APIRouter(prefix="/sentences", tags=["matcher"])


@router.post("/match", response_model=MatchResponse)
async def match_sentence(
    body: MatchRequest,
    language: str = Query(default="de"),
    current_user: dict = Depends(get_current_user),
):
    """Extract phrases from a sentence.

    `language` defaults to 'de' for back-compat with pre-Stage-1 callers
    that didn't pass a language query parameter. Non-German content
    routes through the dispatcher and returns an empty phrases list
    (words-only v1 for L2).

    Authenticated (S16): this is a logged-in utility/debug endpoint, not a
    public product route (the frontend never calls it). Requiring a bearer
    token removes the unauthenticated spaCy/CPU-exhaustion vector, so no public
    throttle is needed. `MatchRequest.sentence` keeps its length cap as
    defence-in-depth against an abusive authenticated caller.
    """
    phrases = await matcher_service.match_sentence(body.sentence, language)
    return MatchResponse(sentence=body.sentence, phrases=phrases)
