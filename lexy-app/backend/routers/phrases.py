"""
Phrase endpoints.

GET  /phrases              — browse all seeded phrases for a language
POST /phrases/seed         — (re)seed phrase_table from the loaded verb dict
POST /phrases/match        — match a sentence and return phrase_ids alongside results

The match endpoint is a phrase-aware counterpart to POST /sentences/match.
The older endpoint is left unchanged so existing callers are not broken.
"""
from fastapi import APIRouter, Depends, Query

from ..core.deps import get_current_user, require_admin
from ..database import get_pool
from ..models.schemas import MatchRequest, MatchResponse, PhraseLookupResult
from ..services import matcher_service, phrase_service

router = APIRouter(prefix="/phrases", tags=["phrases"])


@router.get("", response_model=list[PhraseLookupResult])
async def list_phrases(
    language: str = Query(default="de"),
    limit: int = Query(default=200, ge=1, le=500),
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """Return all phrases seeded in phrase_table for the given language."""
    return await phrase_service.get_phrases_for_language(pool, language, limit)


@router.post("/seed", status_code=201)
async def seed_phrases(
    language: str = Query(default="de"),
    pool=Depends(get_pool),
    current_user: dict = Depends(require_admin),
):
    """
    Populate phrase_table from the verb blueprint dict loaded at startup.

    Idempotent: safe to call repeatedly.  Returns counts of newly inserted
    and total phrases for the language.  **Admin-only (S17)** — this mutates the
    global shared catalog, so it's gated on `require_admin` (403 for non-admins).
    Startup already seeds the table in the lifespan; this endpoint is the manual
    recovery path.
    """
    blueprint_map = matcher_service.get_blueprint_map()
    inserted = await phrase_service.seed_from_blueprint_map(pool, blueprint_map, language)
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM phrase_table WHERE language = $1", language
    )
    return {"inserted": inserted, "total": int(total)}


@router.post("/match", response_model=MatchResponse)
async def match_sentence_with_ids(
    body: MatchRequest,
    language: str = Query(default="de"),
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """
    Match a sentence and return phrase_ids alongside each result.

    phrase_id is null when the matched canonical form is not in phrase_table
    (e.g. verbs matched via trigram fallback to unseeded entries, bare nouns).
    Requires authentication so progress events can be fired in the future.
    """
    phrases = await matcher_service.match_sentence_with_ids(pool, body.sentence, language)
    return MatchResponse(sentence=body.sentence, phrases=phrases)
