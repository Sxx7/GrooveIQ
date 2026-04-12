"""
GrooveIQ – Last.fm integration routes.

Auth flow:
- Client apps call POST /v1/users/{id}/lastfm/connect with Last.fm
  credentials.  GrooveIQ exchanges them for a session key via Last.fm's
  auth.getMobileSession, encrypts the key at rest, and discards the
  password immediately.  The admin dashboard never handles credentials.

Endpoints:
  POST   /v1/users/{id}/lastfm/connect  — client app sends credentials
  DELETE /v1/users/{id}/lastfm           — disconnect (admin or app)
  GET    /v1/users/{id}/lastfm/profile   — read-only profile data
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import check_user_access, require_api_key
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


async def _resolve_user(session: AsyncSession, user_id: str, api_key: str = "anonymous") -> User:
    check_user_access(api_key, user_id)
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


@router.post(
    "/users/{user_id}/lastfm/connect",
    response_model=LastfmConnectResponse,
    summary="Connect Last.fm account",
    description="Called by client apps to link a user's Last.fm account. "
    "Credentials are exchanged for a session key via Last.fm and "
    "discarded immediately — never stored or logged.",
)
async def connect_lastfm(
    user_id: str,
    body: LastfmConnectRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    _require_lastfm_enabled()
    user = await _resolve_user(session, user_id, _key)

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
        result = await client.get_mobile_session(
            body.lastfm_username,
            body.lastfm_password,
        )
    except LastFmError as e:
        logger.warning("Last.fm auth failed for %s: %s", user_id, e.message)
        raise HTTPException(
            status_code=401,
            detail="Last.fm authentication failed. Check your username and password.",
        )
    except Exception as e:
        logger.error("Unexpected error during Last.fm auth: %s", e)
        raise HTTPException(
            status_code=502,
            detail="Last.fm API is temporarily unavailable.",
        )

    # Store encrypted session key; password is already out of scope
    user.lastfm_username = body.lastfm_username
    user.lastfm_session_key = encrypt_session_key(result)

    from app.core.audit import audit_log

    audit_log(
        "lastfm_connect",
        api_key=_key,
        detail={
            "user_id": user_id,
            "lastfm_username": body.lastfm_username,
        },
    )

    # Trigger immediate profile pull
    try:
        from app.services.lastfm_profile import refresh_single_user

        await refresh_single_user(session, user)
    except Exception as e:
        logger.warning("Initial Last.fm profile pull failed: %s", e)

    return LastfmConnectResponse(
        status="connected",
        username=body.lastfm_username,
        scrobbling_enabled=True,
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
    user = await _resolve_user(session, user_id, _key)

    from app.core.audit import audit_log

    audit_log("lastfm_disconnect", api_key=_key, detail={"user_id": user_id})

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


@router.post(
    "/users/{user_id}/lastfm/sync",
    response_model=LastfmProfileResponse,
    summary="Force-refresh Last.fm profile data",
    description="Re-fetches the user's Last.fm profile from the API regardless of the normal refresh interval.",
)
async def sync_lastfm_profile(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    _require_lastfm_enabled()
    user = await _resolve_user(session, user_id, _key)

    if not user.lastfm_username:
        raise HTTPException(
            status_code=404,
            detail="Not found.",
        )

    from app.services.lastfm_profile import refresh_single_user

    try:
        await refresh_single_user(session, user)
        await session.commit()
    except Exception as e:
        logger.error("Last.fm profile sync failed for %s: %s", user_id, e)
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch Last.fm profile. Try again later.",
        )

    return LastfmProfileResponse(
        username=user.lastfm_username,
        scrobbling_enabled=user.lastfm_session_key is not None,
        synced_at=user.lastfm_synced_at,
        profile=user.lastfm_cache,
    )


@router.post(
    "/users/{user_id}/lastfm/backfill",
    summary="Backfill missed scrobbles",
    description="Scans past play_end events and enqueues any that qualify "
    "for scrobbling but were missed. The background worker will "
    "send them to Last.fm on its next run.",
)
async def backfill_scrobbles(
    user_id: str,
    _key: str = Depends(require_api_key),
):
    _require_lastfm_enabled()

    from app.core.security import check_user_access

    check_user_access(_key, user_id)

    from app.services.lastfm_scrobbler import backfill_scrobbles as do_backfill

    result = await do_backfill(user_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


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
    user = await _resolve_user(session, user_id, _key)

    if not user.lastfm_username:
        raise HTTPException(
            status_code=404,
            detail="Not found.",
        )

    return LastfmProfileResponse(
        username=user.lastfm_username,
        scrobbling_enabled=user.lastfm_session_key is not None,
        synced_at=user.lastfm_synced_at,
        profile=user.lastfm_cache,
    )
