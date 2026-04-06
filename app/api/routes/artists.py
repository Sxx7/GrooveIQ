"""
GrooveIQ -- Artist metadata API routes.

Provides rich artist metadata by combining Last.fm API data with
local library matching.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import settings
from app.core.security import require_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/artists/{name}/meta",
    summary="Get artist metadata",
    description=(
        "Returns rich artist metadata from Last.fm combined with local library info. "
        "Includes bio, tags, similar artists, top tracks, and library match status."
    ),
)
async def get_artist_meta(
    name: str,
    _key: str = Depends(require_api_key),
):
    if not settings.LASTFM_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Last.fm integration is not configured (LASTFM_API_KEY missing).",
        )

    from app.services.artist_meta import get_artist_meta as fetch_meta

    result = await fetch_meta(name)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Last.fm data found for artist '{name}'.",
        )

    return result
