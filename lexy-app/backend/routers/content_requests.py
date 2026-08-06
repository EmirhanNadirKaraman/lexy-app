import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, model_validator

from ..core.deps import get_current_user
from ..database import get_pool
from ..services import content_request_service

router = APIRouter(prefix="/content-requests", tags=["content-requests"])

PIPELINE_SCRIPT = Path(__file__).parents[3] / "subtitle-scraper" / "pipeline.py"

# Holds a reference to the currently running pipeline subprocess, if any.
_pipeline_proc: asyncio.subprocess.Process | None = None


async def _spawn_pipeline() -> None:
    """Spawn pipeline.py --requests-only if it isn't already running."""
    global _pipeline_proc
    if _pipeline_proc is not None and _pipeline_proc.returncode is None:
        return  # already running — it will pick up the new request too
    _pipeline_proc = await asyncio.create_subprocess_exec(
        sys.executable, str(PIPELINE_SCRIPT), "--requests-only",
    )


# ── content_id validation (S7) ──────────────────────────────────────────────
# content_id is later interpolated into a youtube.com URL and handed to yt-dlp
# by the scraper. Validate + normalize it to a canonical YouTube id at the API
# boundary so no arbitrary URL / shell-like / non-YouTube string can reach the
# subprocess. The API is the only writer of content_request, so the scraper
# inherits already-validated ids.
_VIDEO_ID_RE   = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YT_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}
_MAX_CONTENT_ID_LEN = 256


def _normalize_video_id(raw: str) -> str | None:
    """Return the 11-char video id from a bare id or a YouTube watch / youtu.be
    / shorts URL; None if it isn't a recognisable YouTube video reference."""
    if _VIDEO_ID_RE.match(raw):            # bare id — happy path, no URL parse
        return raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        vid = parsed.path.lstrip("/")
        return vid if _VIDEO_ID_RE.match(vid) else None
    if host in _YT_HOSTS:
        if parsed.path == "/watch":
            vid = parse_qs(parsed.query).get("v", [""])[0]
            return vid if _VIDEO_ID_RE.match(vid) else None
        m = re.match(r"^/shorts/([A-Za-z0-9_-]{11})$", parsed.path)
        if m:
            return m.group(1)
    return None


def _normalize_channel_id(raw: str) -> str | None:
    """Return the 'UC…' channel id from a bare id or a youtube.com/channel/UC…
    URL; None otherwise. Handle / @ / c / user URLs don't yield a UC id and are
    rejected — the scraper needs the canonical channel id to build its URL."""
    if _CHANNEL_ID_RE.match(raw):
        return raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host in _YT_HOSTS:
        m = re.match(r"^/channel/(UC[A-Za-z0-9_-]{22})/?$", parsed.path)
        if m:
            return m.group(1)
    return None


class ContentRequestCreate(BaseModel):
    request_type: Literal["channel", "video"]
    content_id: str

    @model_validator(mode="after")
    def _validate_content_id(self) -> "ContentRequestCreate":
        # Reject empty / oversized before any parsing, then normalize to a
        # canonical YouTube id per request_type. A ValueError here surfaces as
        # a 422 *before* the handler runs, so an invalid id never reaches the DB
        # or the scraper subprocess.
        raw = (self.content_id or "").strip()
        if not raw or len(raw) > _MAX_CONTENT_ID_LEN:
            raise ValueError("content_id must be a non-empty YouTube id/URL (≤256 chars)")
        if self.request_type == "video":
            normalized = _normalize_video_id(raw)
            if normalized is None:
                raise ValueError(
                    "content_id must be an 11-character YouTube video id or a "
                    "youtube.com / youtu.be video URL"
                )
        else:  # channel
            normalized = _normalize_channel_id(raw)
            if normalized is None:
                raise ValueError(
                    "content_id must be a 'UC…' YouTube channel id or a "
                    "youtube.com/channel/UC… URL"
                )
        self.content_id = normalized
        return self


class ContentRequestRead(BaseModel):
    request_id: int
    request_type: str
    content_id: str
    status: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime


@router.post("", response_model=ContentRequestRead, status_code=201)
async def submit_request(
    body: ContentRequestCreate,
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """Submit a request to add a channel or video to the database.

    - If the request already exists and failed, it is reset to pending.
    - If it's already pending or done, the existing row is returned unchanged.
    The pipeline is spawned immediately in the background to process it.

    Uniqueness is per-user (migration 029): a different user submitting the
    same (request_type, content_id) creates a separate row so each user can
    track their own request and receive their own notifications. Idempotency
    for the *same* user is preserved via the ON CONFLICT in the service.
    """
    result = await content_request_service.create_or_reset(
        pool,
        current_user["user_id"],
        body.request_type,
        body.content_id,
    )

    # Only spawn if the request is actually pending (not already done/in-progress)
    if result["status"] == "pending":
        asyncio.ensure_future(_spawn_pipeline())

    return result


@router.get("", response_model=list[ContentRequestRead])
async def list_requests(
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """Return the current user's content requests, newest first."""
    return await content_request_service.list_for_user(pool, current_user["user_id"])
