"""GrooveIQ – Download routing configuration API routes."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, require_api_key
from app.db.session import get_session
from app.models.db import DownloadRoutingConfig
from app.models.download_routing_schema import (
    ROUTING_GROUPS,
    DownloadRoutingConfigData,
    DownloadRoutingConfigImport,
    DownloadRoutingConfigUpdate,
    get_defaults_dict,
)
from app.services import download_routing as routing_service

router = APIRouter()


def _row_to_response(row: DownloadRoutingConfig) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "name": row.name,
        "config": row.config,
        "is_active": row.is_active,
        "created_at": row.created_at,
        "created_by": row.created_by,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("/downloads/routing", summary="Get active download routing config")
async def get_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await routing_service.get_active(session)
    if row is None:
        raise HTTPException(404, "No active routing config found")
    return _row_to_response(row)


@router.get("/downloads/routing/defaults", summary="Get default routing values")
async def get_defaults_endpoint(
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    return {
        "config": get_defaults_dict(),
        "groups": ROUTING_GROUPS,
    }


@router.get("/downloads/routing/history", summary="Routing config version history")
async def get_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    rows = await routing_service.get_history(session, limit=limit, offset=offset)
    return [_row_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


@router.put("/downloads/routing", summary="Update routing config (creates new version)")
async def update_config(
    body: DownloadRoutingConfigUpdate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await routing_service.save_routing(
        session,
        body.config,
        name=body.name,
        created_by=_key,
    )
    await session.commit()
    return _row_to_response(row)


@router.post("/downloads/routing/reset", summary="Reset routing config to defaults")
async def reset_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await routing_service.reset_to_defaults(session, created_by=_key)
    await session.commit()
    return _row_to_response(row)


@router.post(
    "/downloads/routing/activate/{version}",
    summary="Activate a historical routing config version",
)
async def activate_version(
    version: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await routing_service.activate_version(session, version)
    if row is None:
        raise HTTPException(404, f"Routing config version {version} not found")
    await session.commit()
    return _row_to_response(row)


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


@router.get("/downloads/routing/export", summary="Export active routing config as JSON")
async def export_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await routing_service.get_active(session)
    if row is None:
        raise HTTPException(404, "No active routing config found")

    export_data = {
        "grooveiq_download_routing_config": True,
        "version": row.version,
        "name": row.name,
        "config": row.config,
        "exported_at": int(time.time()),
    }
    return JSONResponse(
        content=export_data,
        headers={"Content-Disposition": f'attachment; filename="grooveiq-routing-v{row.version}.json"'},
    )


@router.post("/downloads/routing/import", summary="Import a routing config from JSON")
async def import_config(
    body: DownloadRoutingConfigImport,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    try:
        validated = DownloadRoutingConfigData.model_validate(body.config)
    except Exception as e:
        raise HTTPException(422, f"Invalid routing config: {e}")

    row = await routing_service.save_routing(
        session,
        validated,
        name=body.name or "Imported",
        created_by=_key,
    )
    await session.commit()
    return _row_to_response(row)


# ---------------------------------------------------------------------------
# Version lookup (must be LAST so /export, /defaults, /history aren't shadowed)
# ---------------------------------------------------------------------------


@router.get("/downloads/routing/{version}", summary="Get a specific routing config version")
async def get_version(
    version: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    result = await session.execute(select(DownloadRoutingConfig).where(DownloadRoutingConfig.version == version))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Routing config version {version} not found")
    return _row_to_response(row)
