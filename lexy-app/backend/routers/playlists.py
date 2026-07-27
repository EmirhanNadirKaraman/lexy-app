from fastapi import APIRouter, Depends, HTTPException, status

from ..core.deps import get_current_user
from ..database import get_pool
from ..models.schemas import PlaylistGenerateRequest, PlaylistResult
from ..services import playlist_service

router = APIRouter(prefix="/playlists", tags=["playlists"])

# Request `algorithm` → optimizer function. Both share greedy_cover's
# signature, so generate_playlist takes either without further branching.
_OPTIMIZERS = {
    "greedy": playlist_service.greedy_cover,
    "ilp": playlist_service.ilp_cover,
}


@router.post("/generate", response_model=PlaylistResult, status_code=status.HTTP_200_OK)
async def generate_playlist(
    body: PlaylistGenerateRequest,
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    try:
        result = await playlist_service.generate_playlist(
            pool,
            item_ids=body.item_ids,
            item_type=body.item_type,
            language=body.language,
            max_videos=body.max_videos,
            optimizer=_OPTIMIZERS[body.algorithm],
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except RuntimeError as exc:
        # ilp_cover raises this when PuLP is missing or the solve does not reach
        # optimality. 503 rather than 500: the request was valid, the optimal
        # backend is just unavailable, and retrying with algorithm='greedy'
        # will work.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return result
