"""GrooveIQ – Algorithm configuration API routes."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, require_api_key
from app.db.session import get_session
from app.models.algorithm_config_schema import (
    CONFIG_GROUPS,
    AlgorithmConfigData,
    AlgorithmConfigImport,
    AlgorithmConfigUpdate,
    get_defaults_dict,
)
from app.services import algorithm_config as config_service

router = APIRouter()


def _row_to_response(row) -> dict:
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


@router.get("/algorithm/config", summary="Get active algorithm config")
async def get_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await config_service.get_active(session)
    if row is None:
        raise HTTPException(404, "No active config found")
    return _row_to_response(row)


@router.get("/algorithm/config/defaults", summary="Get default config values")
async def get_defaults_endpoint(
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    return {
        "config": get_defaults_dict(),
        "groups": CONFIG_GROUPS,
    }


@router.get("/algorithm/config/history", summary="Config version history")
async def get_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    rows = await config_service.get_history(session, limit=limit, offset=offset)
    return [_row_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


@router.put("/algorithm/config", summary="Update algorithm config (creates new version)")
async def update_config(
    body: AlgorithmConfigUpdate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await config_service.save_config(
        session,
        body.config,
        name=body.name,
        created_by=_key,
    )
    await session.commit()
    return _row_to_response(row)


@router.post("/algorithm/config/reset", summary="Reset config to defaults")
async def reset_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await config_service.reset_to_defaults(session, created_by=_key)
    await session.commit()
    return _row_to_response(row)


@router.post(
    "/algorithm/config/activate/{version}",
    summary="Activate a historical config version (rollback)",
)
async def activate_version(
    version: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await config_service.activate_version(session, version)
    if row is None:
        raise HTTPException(404, f"Config version {version} not found")
    await session.commit()
    return _row_to_response(row)


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


@router.get("/algorithm/config/export", summary="Export active config as JSON")
async def export_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await config_service.get_active(session)
    if row is None:
        raise HTTPException(404, "No active config found")

    export_data = {
        "grooveiq_algorithm_config": True,
        "version": row.version,
        "name": row.name,
        "config": row.config,
        "exported_at": int(time.time()),
    }
    return JSONResponse(
        content=export_data,
        headers={"Content-Disposition": f'attachment; filename="grooveiq-config-v{row.version}.json"'},
    )


@router.post("/algorithm/config/import", summary="Import a config from JSON")
async def import_config(
    body: AlgorithmConfigImport,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    # Validate the imported config by parsing through the schema.
    # Missing keys get defaults, invalid values raise validation errors.
    try:
        validated = AlgorithmConfigData.model_validate(body.config)
    except Exception as e:
        raise HTTPException(422, f"Invalid config: {e}")

    row = await config_service.save_config(
        session,
        validated,
        name=body.name or "Imported",
        created_by=_key,
    )
    await session.commit()
    return _row_to_response(row)


# ---------------------------------------------------------------------------
# Version lookup (must be LAST to avoid shadowing fixed paths like /export)
# ---------------------------------------------------------------------------


@router.get("/algorithm/config/{version}", summary="Get a specific config version")
async def get_version(
    version: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    from sqlalchemy import select

    from app.models.db import AlgorithmConfig

    result = await session.execute(select(AlgorithmConfig).where(AlgorithmConfig.version == version))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Config version {version} not found")
    return _row_to_response(row)
