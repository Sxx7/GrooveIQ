"""
GrooveIQ -- Charts API routes.

Exposes Last.fm-sourced charts (top tracks, top artists) globally,
by genre tag, and by country. Charts are rebuilt periodically by the
scheduler and stored in the chart_entries table.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, require_api_key
from app.db.session import get_session
from app.models.db import ChartEntry, DiscoveryRequest, TrackFeatures

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/charts",
    summary="List available charts",
    description="Returns all chart type + scope combinations currently stored.",
)
async def list_charts(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    result = await session.execute(
        select(
            ChartEntry.chart_type,
            ChartEntry.scope,
            func.count().label("entries"),
            func.max(ChartEntry.fetched_at).label("fetched_at"),
        )
        .group_by(ChartEntry.chart_type, ChartEntry.scope)
        .order_by(ChartEntry.chart_type, ChartEntry.scope)
    )
    rows = result.all()
    return {
        "charts": [
            {
                "chart_type": r.chart_type,
                "scope": r.scope,
                "entries": r.entries,
                "fetched_at": r.fetched_at,
            }
            for r in rows
        ],
    }


@router.get(
    "/charts/{chart_type}",
    summary="Get chart entries",
    description="""
Returns chart entries for the given chart type.

**chart_type**: `top_tracks` or `top_artists`

**scope** (optional): Filter by scope. Examples:
- `global` — worldwide chart (default)
- `tag:rock` — genre/tag chart
- `geo:germany` — country chart

Entries are sorted by chart position. Each entry includes library match info
(`matched_track_id` is set when the track/artist exists in your library).
""",
)
async def get_chart(
    chart_type: str,
    scope: str = Query("global", description="Chart scope: global, tag:<name>, geo:<country>"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    if chart_type not in ("top_tracks", "top_artists"):
        raise HTTPException(status_code=400, detail="chart_type must be 'top_tracks' or 'top_artists'")

    count_q = (
        select(func.count())
        .select_from(ChartEntry)
        .where(ChartEntry.chart_type == chart_type, ChartEntry.scope == scope)
    )
    total = (await session.execute(count_q)).scalar() or 0

    if total == 0:
        # Give a helpful error: distinguish "never built" from "scope not configured".
        any_charts = (await session.execute(
            select(func.count()).select_from(ChartEntry)
        )).scalar() or 0

        if any_charts == 0:
            raise HTTPException(
                status_code=404,
                detail="No chart data available. Run POST /v1/charts/build first.",
            )

        # Charts exist but not for this scope — list available scopes.
        available = (await session.execute(
            select(ChartEntry.scope).where(ChartEntry.chart_type == chart_type).distinct()
        )).scalars().all()

        raise HTTPException(
            status_code=404,
            detail=f"No chart data for scope '{scope}'. "
                   f"Available scopes for {chart_type}: {', '.join(sorted(available)) or 'none'}. "
                   f"Configure CHARTS_TAGS or CHARTS_COUNTRIES in .env and rebuild.",
        )

    q = (
        select(ChartEntry)
        .where(ChartEntry.chart_type == chart_type, ChartEntry.scope == scope)
        .order_by(ChartEntry.position)
        .offset(offset)
        .limit(limit)
    )
    entries = (await session.execute(q)).scalars().all()

    # Enrich matched tracks with metadata.
    matched_ids = [e.matched_track_id for e in entries if e.matched_track_id]
    feat_map = {}
    if matched_ids:
        feat_q = await session.execute(
            select(TrackFeatures).where(TrackFeatures.track_id.in_(matched_ids))
        )
        feat_map = {t.track_id: t for t in feat_q.scalars().all()}

    # Build Lidarr status lookup for unmatched artists.
    unmatched_artists = list({e.artist_name for e in entries if not e.matched_track_id and e.artist_name})
    lidarr_status_map = {}
    if unmatched_artists:
        discovery_q = await session.execute(
            select(DiscoveryRequest.artist_name, DiscoveryRequest.status)
            .where(DiscoveryRequest.artist_name.in_(unmatched_artists))
            .order_by(DiscoveryRequest.created_at.desc())
        )
        for artist_name, status in discovery_q.all():
            if artist_name not in lidarr_status_map:
                lidarr_status_map[artist_name] = status

    items = []
    for e in entries:
        item = {
            "position": e.position,
            "artist_name": e.artist_name,
            "playcount": e.playcount,
            "listeners": e.listeners,
            "in_library": e.in_library,
            "matched_track_id": e.matched_track_id,
        }
        if chart_type == "top_tracks":
            item["track_title"] = e.track_title

        if chart_type == "top_artists":
            item["library_track_count"] = e.library_track_count

        # Lidarr status for items not in library.
        if not e.in_library:
            lidarr_st = lidarr_status_map.get(e.artist_name)
            if lidarr_st == "sent":
                item["lidarr_status"] = "downloading"
            elif lidarr_st == "in_lidarr":
                item["lidarr_status"] = "in_lidarr"
            elif lidarr_st == "pending":
                item["lidarr_status"] = "pending"
            elif lidarr_st == "failed":
                item["lidarr_status"] = "failed"
            else:
                item["lidarr_status"] = None  # not sent to Lidarr

        # Enrich with local metadata if matched.
        tf = feat_map.get(e.matched_track_id) if e.matched_track_id else None
        if tf:
            item["library"] = {
                "track_id": tf.track_id,
                "title": tf.title,
                "artist": tf.artist,
                "album": tf.album,
                "genre": tf.genre,
                "bpm": tf.bpm,
                "energy": tf.energy,
                "duration": tf.duration,
            }

        items.append(item)

    return {
        "chart_type": chart_type,
        "scope": scope,
        "total": total,
        "fetched_at": entries[0].fetched_at if entries else None,
        "entries": items,
    }


@router.post(
    "/charts/build",
    summary="Trigger chart rebuild",
    description="Fetches fresh charts from Last.fm, matches to library, and optionally sends missing artists to Lidarr.",
)
async def trigger_chart_build(
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    from app.services.charts import build_charts
    result = await build_charts()
    return {"status": "completed", "result": result}


@router.get(
    "/charts/stats",
    summary="Chart statistics",
    description="Returns summary stats about stored charts: total entries, library match rate, last build time.",
)
async def chart_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    total = (await session.execute(
        select(func.count()).select_from(ChartEntry)
    )).scalar() or 0

    matched = (await session.execute(
        select(func.count()).select_from(ChartEntry)
        .where(ChartEntry.matched_track_id.isnot(None))
    )).scalar() or 0

    last_fetch = (await session.execute(
        select(func.max(ChartEntry.fetched_at))
    )).scalar()

    chart_count = (await session.execute(
        select(func.count())
        .select_from(
            select(ChartEntry.chart_type, ChartEntry.scope)
            .group_by(ChartEntry.chart_type, ChartEntry.scope)
            .subquery()
        )
    )).scalar() or 0

    return {
        "total_entries": total,
        "library_matches": matched,
        "match_rate": round(matched / total, 3) if total > 0 else 0,
        "chart_count": chart_count,
        "last_fetched_at": last_fetch,
    }
