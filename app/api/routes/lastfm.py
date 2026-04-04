"""
GrooveIQ – Last.fm integration routes.

Endpoints for connecting/disconnecting Last.fm accounts and
viewing cached Last.fm profile data.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import ScrobbleQueue, User
from app.models.schemas import (
    LastfmConnectRequest,
    LastfmConnectResponse,
    LastfmProfileResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_lastfm_enabled() -> None:
    if not settings.lastfm_user_enabled:
        raise HTTPException(
            status_code=503,
            detail="Last.fm integration is not enabled. "
                   "Set LASTFM_ENABLED=true, LASTFM_API_KEY, and LASTFM_API_SECRET.",
        )


async def _resolve_user(session: AsyncSession, user_id: str) -> User:
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


@router.post(
    "/users/{user_id}/lastfm/connect",
    response_model=LastfmConnectResponse,
    summary="Connect Last.fm account",
    description="Link a Last.fm account to this user.  Provide only `lastfm_username` "
                "for read-only profile data, or also `lastfm_password` to enable scrobbling.  "
                "The password is used once to obtain a session key and is never stored.",
)
async def connect_lastfm(
    user_id: str,
    body: LastfmConnectRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    _require_lastfm_enabled()
    user = await _resolve_user(session, user_id)

    user.lastfm_username = body.lastfm_username
    scrobbling = False

    # If password provided, exchange for session key
    if body.lastfm_password:
        if not settings.LASTFM_SESSION_ENCRYPTION_KEY:
            raise HTTPException(
                status_code=503,
                detail="Scrobbling requires LASTFM_SESSION_ENCRYPTION_KEY to be configured.",
            )

        from app.services.lastfm_client import (
            LastFmError,
            encrypt_session_key,
            get_lastfm_client,
        )

        client = get_lastfm_client()
        try:
            session_key = await client.get_mobile_session(
                body.lastfm_username, body.lastfm_password,
            )
        except LastFmError as e:
            raise HTTPException(
                status_code=401,
                detail=f"Last.fm authentication failed: {e.message}",
            )

        user.lastfm_session_key = encrypt_session_key(session_key)
        scrobbling = True

    # Trigger immediate profile pull
    try:
        from app.services.lastfm_profile import refresh_single_user
        await refresh_single_user(session, user)
    except Exception as e:
        logger.warning("Initial Last.fm profile pull failed: %s", e)

    return LastfmConnectResponse(
        status="connected",
        username=body.lastfm_username,
        scrobbling_enabled=scrobbling,
    )


@router.delete(
    "/users/{user_id}/lastfm",
    summary="Disconnect Last.fm account",
)
async def disconnect_lastfm(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    user = await _resolve_user(session, user_id)

    user.lastfm_username = None
    user.lastfm_session_key = None
    user.lastfm_cache = None
    user.lastfm_synced_at = None

    # Remove pending scrobbles for this user
    await session.execute(
        delete(ScrobbleQueue).where(
            ScrobbleQueue.user_id == user_id,
            ScrobbleQueue.status == "pending",
        )
    )

    return {"status": "disconnected"}


@router.get(
    "/users/{user_id}/lastfm/profile",
    response_model=LastfmProfileResponse,
    summary="Get Last.fm profile data",
)
async def get_lastfm_profile(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    user = await _resolve_user(session, user_id)

    if not user.lastfm_username:
        raise HTTPException(
            status_code=404,
            detail="No Last.fm account linked for this user.",
        )

    return LastfmProfileResponse(
        username=user.lastfm_username,
        scrobbling_enabled=user.lastfm_session_key is not None,
        synced_at=user.lastfm_synced_at,
        profile=user.lastfm_cache,
    )
