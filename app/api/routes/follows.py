"""GrooveIQ -- Followed-artists routes (P0)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.models.schemas import FollowCreateRequest, FollowListResponse, FollowResponse
from app.services import follow_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/users/{user_id}/follows",
    response_model=FollowResponse,
    summary="Follow an artist",
)
async def follow(
    user_id: str,
    body: FollowCreateRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)          # 400 on malformed (user_id.py:45)
    check_user_access(_key, user_id)   # 403 when API_KEY_USERS binds keys (security.py:223)
    return await follow_service.follow_artist(
        session, user_id=user_id, artist_name=body.artist_name,
        artist_mbid=body.artist_mbid, source=body.source,
    )


@router.delete(
    "/users/{user_id}/follows",
    summary="Unfollow all of a user's artists (soft-delete)",
)
async def unfollow_all(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Returns 200 + a JSON body (not 204): the iOS Alamofire client decodes an
    # empty struct from a body but throws on a truly empty 204 — matching every
    # other grooveiq DELETE (e.g. radio stop). Idempotent (overview §10).
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    count = await follow_service.unfollow_all(session, user_id=user_id)
    return {"status": "unfollowed_all", "unfollowed": count}


@router.delete(
    "/users/{user_id}/follows/{artist_key}",
    summary="Unfollow an artist (soft-delete)",
)
async def unfollow(
    user_id: str,
    artist_key: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # 200 + JSON body (see unfollow_all note). Idempotent: `removed` is False
    # when nothing active matched, but it's still a success (overview §10).
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    removed = await follow_service.unfollow_artist(session, user_id=user_id, artist_key=artist_key)
    return {"status": "unfollowed", "removed": removed}


@router.get(
    "/users/{user_id}/follows",
    response_model=FollowListResponse,
    summary="List a user's followed artists",
)
async def get_follows(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    return {"follows": await follow_service.list_follows(session, user_id=user_id)}
