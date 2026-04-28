"""GrooveIQ – Lidarr backfill API routes.

Three groups of endpoints under ``/v1/lidarr-backfill``:

* **config** – versioned policy CRUD, mirrors ``algorithm_config.py``.
* **requests** – per-album state machine (list, retry, skip, delete, bulk reset).
* **operations / stats** – trigger a tick, preview matches, dashboard summary.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, require_api_key
from app.db.session import get_session
from app.models.db import LidarrBackfillRequest
from app.models.lidarr_backfill_schema import (
    CONFIG_GROUPS,
    LidarrBackfillConfigData,
    LidarrBackfillConfigImport,
    LidarrBackfillConfigUpdate,
    get_defaults_dict,
)
from app.services import lidarr_backfill as lbf_service
from app.services import lidarr_backfill_config as cfg_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_response(row) -> dict[str, Any]:
    # Round-trip the persisted JSON through the current schema so that
    # fields added in later releases (e.g. sources.queue_order, match.
    # allow_structural_fallback) appear with their defaults on legacy rows
    # that pre-date the field. Matches what the engine actually uses at
    # runtime — the in-memory cache also goes through model_validate.
    try:
        validated = LidarrBackfillConfigData.model_validate(row.config or {}).model_dump(mode="json")
    except Exception:
        validated = row.config  # fall back to raw JSON if validation fails (shouldn't happen)
    return {
        "id": row.id,
        "version": row.version,
        "name": row.name,
        "config": validated,
        "is_active": row.is_active,
        "created_at": row.created_at,
        "created_by": row.created_by,
    }


def _apply_scheduler_change() -> None:
    """Re-evaluate scheduler registration after a config change.

    Importing the scheduler lazily keeps test imports cheap and avoids a
    circular import with ``app.workers.scheduler`` at module load.
    """
    try:
        from app.workers.scheduler import apply_lidarr_backfill_config

        apply_lidarr_backfill_config()
    except Exception:  # pragma: no cover — never fail the API on scheduler hiccups
        pass


# ---------------------------------------------------------------------------
# Config – read
# ---------------------------------------------------------------------------


@router.get("/lidarr-backfill/config", summary="Get active Lidarr backfill config")
async def get_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await cfg_service.get_active(session)
    if row is None:
        raise HTTPException(404, "No active config found")
    return _row_to_response(row)


@router.get("/lidarr-backfill/config/defaults", summary="Get default config values")
async def get_defaults_endpoint(
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    return {
        "config": get_defaults_dict(),
        "groups": CONFIG_GROUPS,
    }


@router.get("/lidarr-backfill/config/history", summary="Config version history")
async def get_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    rows = await cfg_service.get_history(session, limit=limit, offset=offset)
    return [_row_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# Config – write
# ---------------------------------------------------------------------------


@router.put("/lidarr-backfill/config", summary="Update config (creates new version)")
async def update_config(
    body: LidarrBackfillConfigUpdate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await cfg_service.save_config(session, body.config, name=body.name, created_by=_key)
    await session.commit()
    _apply_scheduler_change()
    return _row_to_response(row)


@router.post("/lidarr-backfill/config/reset", summary="Reset config to defaults")
async def reset_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await cfg_service.reset_to_defaults(session, created_by=_key)
    await session.commit()
    _apply_scheduler_change()
    return _row_to_response(row)


@router.post(
    "/lidarr-backfill/config/activate/{version}",
    summary="Activate a historical config version (rollback)",
)
async def activate_version(
    version: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await cfg_service.activate_version(session, version)
    if row is None:
        raise HTTPException(404, f"Config version {version} not found")
    await session.commit()
    _apply_scheduler_change()
    return _row_to_response(row)


# ---------------------------------------------------------------------------
# Config – export / import
# ---------------------------------------------------------------------------


@router.get("/lidarr-backfill/config/export", summary="Export active config as JSON")
async def export_config(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    row = await cfg_service.get_active(session)
    if row is None:
        raise HTTPException(404, "No active config found")

    export_data = {
        "grooveiq_lidarr_backfill_config": True,
        "version": row.version,
        "name": row.name,
        "config": row.config,
        "exported_at": int(time.time()),
    }
    return JSONResponse(
        content=export_data,
        headers={"Content-Disposition": (f'attachment; filename="grooveiq-lidarr-backfill-v{row.version}.json"')},
    )


@router.post("/lidarr-backfill/config/import", summary="Import a config from JSON")
async def import_config(
    body: LidarrBackfillConfigImport,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    try:
        validated = LidarrBackfillConfigData.model_validate(body.config)
    except Exception as exc:
        raise HTTPException(422, f"Invalid config: {exc}")

    row = await cfg_service.save_config(session, validated, name=body.name or "Imported", created_by=_key)
    await session.commit()
    _apply_scheduler_change()
    return _row_to_response(row)


# ---------------------------------------------------------------------------
# Requests – list / mutate
# ---------------------------------------------------------------------------


@router.get("/lidarr-backfill/requests", summary="List backfill request rows")
async def list_requests(
    status: str | None = Query(None, description="Filter by status (queued, downloading, complete, failed, ...)"),
    artist: str | None = Query(None, description="Substring filter on artist name"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    rows = await lbf_service.list_requests(
        session,
        status=status,
        artist=artist,
        limit=limit,
        offset=offset,
    )
    return {"items": rows, "limit": limit, "offset": offset}


@router.post("/lidarr-backfill/requests/{request_id}/retry", summary="Retry a failed/no_match row")
async def retry_request(
    request_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    ok = await lbf_service.retry_request(session, request_id)
    if not ok:
        raise HTTPException(404, f"Request {request_id} not found")
    await session.commit()
    return {"status": "ok"}


@router.post(
    "/lidarr-backfill/requests/{request_id}/skip",
    summary="Mark a row permanently_skipped",
)
async def skip_request(
    request_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    ok = await lbf_service.skip_request(session, request_id)
    if not ok:
        raise HTTPException(404, f"Request {request_id} not found")
    await session.commit()
    return {"status": "ok"}


@router.delete("/lidarr-backfill/requests/{request_id}", summary="Delete a row (will re-pick next tick)")
async def delete_request(
    request_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    ok = await lbf_service.delete_request(session, request_id)
    if not ok:
        raise HTTPException(404, f"Request {request_id} not found")
    await session.commit()
    return {"status": "ok"}


@router.post("/lidarr-backfill/requests/reset", summary="Bulk-delete by scope")
async def reset_requests(
    body: dict[str, Any] = Body(..., description='{"scope": "failed"|"no_match"|"permanently_skipped"|"all"}'),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    scope = (body or {}).get("scope")
    if not scope:
        raise HTTPException(400, "Missing 'scope' in body")
    try:
        deleted = await lbf_service.reset_backfill_state(session, str(scope))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await session.commit()
    return {"status": "ok", "deleted": deleted, "scope": scope}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@router.post("/lidarr-backfill/run", summary="Trigger one backfill tick now")
async def run_now(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    summary = await lbf_service.run_backfill_tick(session)
    return summary


@router.post("/lidarr-backfill/preview", summary="Preview match decisions for the current missing queue")
async def preview(
    body: dict[str, Any] | None = Body(default=None),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    body = body or {}
    cfg_override = body.get("config_override") if isinstance(body.get("config_override"), dict) else None
    limit = int(body.get("limit") or 20)
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    return await lbf_service.preview_matches(cfg_override, limit=limit)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/lidarr-backfill/stats", summary="Dashboard stats for the Backfill panel")
async def stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)

    base = await lbf_service.get_stats(session)

    # Most-recent created_at across persisted rows. Useful as a "last
    # progress" indicator. NOT the same as "last scheduler tick" — see
    # `last_tick_at` below.
    last_row_created_at = await session.scalar(
        select(LidarrBackfillRequest.created_at).order_by(LidarrBackfillRequest.created_at.desc()).limit(1)
    )

    # Try to surface scheduler timing too (best-effort).
    next_tick_at: int | None = None
    try:
        from app.workers.scheduler import _LBF_TICK_JOB_ID, _scheduler

        job = _scheduler.get_job(_LBF_TICK_JOB_ID)
        if job is not None and job.next_run_time is not None:
            next_tick_at = int(job.next_run_time.timestamp())
    except Exception:
        pass

    return {
        **base,
        # When the last `run_backfill_tick` actually completed (any outcome).
        # Falls back to `last_row_created_at` for early calls before any tick
        # has run since process start.
        "last_tick_at": lbf_service.get_last_tick_at() or last_row_created_at,
        "last_row_created_at": last_row_created_at,
        "next_tick_at": next_tick_at,
    }


# ---------------------------------------------------------------------------
# Version lookup (must be LAST so it doesn't shadow /export, /history, ...)
# ---------------------------------------------------------------------------


@router.get("/lidarr-backfill/config/{version}", summary="Get a specific config version")
async def get_version(
    version: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    from app.models.db import LidarrBackfillConfig

    result = await session.execute(select(LidarrBackfillConfig).where(LidarrBackfillConfig.version == version))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Config version {version} not found")
    return _row_to_response(row)
