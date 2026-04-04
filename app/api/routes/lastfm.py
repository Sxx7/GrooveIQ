"""
GrooveIQ – Last.fm integration routes.

Auth flow (redirect-based, no passwords touch GrooveIQ):
1. GET  /v1/users/{id}/lastfm/auth    → returns Last.fm authorization URL
2. User authorizes on Last.fm, gets redirected back with ?token=...
3. GET  /v1/users/{id}/lastfm/callback?token=...  → exchanges token for session key

Read-only mode (no scrobbling, just profile enrichment):
  POST /v1/users/{id}/lastfm/connect  → only needs lastfm_username
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


# ---------------------------------------------------------------------------
# Read-only connect (username only, no auth needed)
# ---------------------------------------------------------------------------

@router.post(
    "/users/{user_id}/lastfm/connect",
    response_model=LastfmConnectResponse,
    summary="Connect Last.fm (read-only)",
    description="Link a Last.fm username for profile enrichment only. "
                "For scrobbling, use the /lastfm/auth redirect flow instead.",
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

    # Trigger immediate profile pull
    try:
        from app.services.lastfm_profile import refresh_single_user
        await refresh_single_user(session, user)
    except Exception as e:
        logger.warning("Initial Last.fm profile pull failed: %s", e)

    return LastfmConnectResponse(
        status="connected",
        username=body.lastfm_username,
        scrobbling_enabled=False,
    )


# ---------------------------------------------------------------------------
# Scrobble auth via Last.fm redirect flow (password never touches GrooveIQ)
# ---------------------------------------------------------------------------

@router.get(
    "/users/{user_id}/lastfm/auth",
    summary="Get Last.fm authorization URL",
    description="Returns a URL to redirect the user to Last.fm for authorization. "
                "After authorizing, Last.fm redirects back to the callback URL with a token.",
)
async def get_lastfm_auth_url(
    user_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    _require_lastfm_enabled()
    await _resolve_user(session, user_id)

    if not settings.LASTFM_SESSION_ENCRYPTION_KEY:
        raise HTTPException(
            status_code=503,
            detail="Scrobbling requires LASTFM_SESSION_ENCRYPTION_KEY to be configured.",
        )

    # Build callback URL pointing back to the dashboard.
    # The dashboard JS detects ?lastfm_token=&lastfm_user= on load
    # and automatically completes the token exchange.
    cb = str(request.base_url).rstrip("/") + f"/dashboard?lastfm_user={user_id}"

    auth_url = (
        f"https://www.last.fm/api/auth/"
        f"?api_key={settings.LASTFM_API_KEY}"
        f"&cb={cb}"
    )

    return {"auth_url": auth_url, "user_id": user_id}


@router.get(
    "/users/{user_id}/lastfm/callback",
    response_model=LastfmConnectResponse,
    summary="Exchange Last.fm auth token for session key",
    description="Called after the user authorizes on Last.fm. Exchanges the token "
                "for a permanent session key (encrypted at rest). The user's Last.fm "
                "password never touches GrooveIQ.",
)
async def lastfm_callback(
    user_id: str,
    token: str = Query(..., min_length=1, description="Token from Last.fm redirect"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    _require_lastfm_enabled()
    user = await _resolve_user(session, user_id)

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
        result = await client.get_session(token)
        session_key = result["key"]
        lastfm_username = result["name"]
    except LastFmError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Last.fm token exchange failed: {e.message}",
        )

    user.lastfm_username = lastfm_username
    user.lastfm_session_key = encrypt_session_key(session_key)

    # Trigger immediate profile pull
    try:
        from app.services.lastfm_profile import refresh_single_user
        await refresh_single_user(session, user)
    except Exception as e:
        logger.warning("Initial Last.fm profile pull failed: %s", e)

    return LastfmConnectResponse(
        status="connected",
        username=lastfm_username,
        scrobbling_enabled=True,
    )


# ---------------------------------------------------------------------------
# Disconnect + Profile
# ---------------------------------------------------------------------------

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
