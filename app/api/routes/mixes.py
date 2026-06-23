"""
GrooveIQ – Session-clustered rotating mixes API.

Two read surfaces + a manual rebuild trigger:

  GET  /v1/users/{user_id}/session-mixes    the live rotating mixes (Made for you / More mixes)
  GET  /v1/users/{user_id}/nostalgic-mixes   archived mixes resurfaced after dormancy (self-hiding)
  POST /v1/users/{user_id}/mixes/rebuild     admin/manual rebuild (the scheduler does this nightly)

The mixes themselves are built by ``app.services.user_mixes``. Names are
deliberately omitted — each mix carries a generic ``ordinal`` (the client shows
"Mix N" or nothing). ``session-mixes`` returns an empty list for cold-start users,
which is the client's signal to fall back to the genre mixes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_admin, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.services import user_mixes
from app.services.algorithm_config import get_config

router = APIRouter()


@router.get("/users/{user_id}/session-mixes", summary="Get the user's session-clustered rotating mixes")
async def session_mixes(
    user_id: str = Path(..., description="Navidrome user identifier"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    if not get_config().mixes.enabled:
        return {"user_id": user_id, "enabled": False, "mixes": []}
    mixes = await user_mixes.get_session_mixes(session, user_id)
    return {"user_id": user_id, "enabled": True, "mixes": mixes}


@router.get("/users/{user_id}/nostalgic-mixes", summary="Get the user's resurfaced 'nostalgic' mixes")
async def nostalgic_mixes(
    user_id: str = Path(..., description="Navidrome user identifier"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    if not get_config().mixes.enabled:
        return {"user_id": user_id, "enabled": False, "mixes": []}
    mixes = await user_mixes.get_nostalgic_mixes(session, user_id)
    return {"user_id": user_id, "enabled": True, "mixes": mixes}


@router.post("/users/{user_id}/mixes/rebuild", summary="Rebuild the user's session mixes now (admin)")
async def rebuild_mixes(
    user_id: str = Path(..., description="Navidrome user identifier"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    require_admin(_key)
    return await user_mixes.rebuild_user_mixes(session, user_id)
