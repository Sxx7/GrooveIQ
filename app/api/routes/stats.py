"""GrooveIQ – Dashboard statistics & pipeline control routes."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import ListenEvent, ListenSession, Playlist, TrackFeatures, TrackInteraction, User, LibraryScanState
from app.services.audio_analysis import ANALYSIS_VERSION

router = APIRouter()


@router.get("/stats", summary="Aggregate stats for the dashboard")
async def get_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    now = int(time.time())
    day_ago = now - 86400
    hour_ago = now - 3600

    # Counts
    total_events = (await session.execute(select(func.count(ListenEvent.id)))).scalar() or 0
    total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    total_tracks = (await session.execute(select(func.count(TrackFeatures.id)))).scalar() or 0
    total_playlists = (await session.execute(select(func.count(Playlist.id)))).scalar() or 0
    events_24h = (await session.execute(
        select(func.count(ListenEvent.id)).where(ListenEvent.timestamp >= day_ago)
    )).scalar() or 0
    events_1h = (await session.execute(
        select(func.count(ListenEvent.id)).where(ListenEvent.timestamp >= hour_ago)
    )).scalar() or 0

    # Event type breakdown (last 24h)
    type_rows = (await session.execute(
        select(ListenEvent.event_type, func.count(ListenEvent.id))
        .where(ListenEvent.timestamp >= day_ago)
        .group_by(ListenEvent.event_type)
        .order_by(func.count(ListenEvent.id).desc())
    )).all()
    event_types = {row[0]: row[1] for row in type_rows}

    # Top tracks (last 24h by event count), enriched with metadata
    from sqlalchemy import or_
    track_rows = (await session.execute(
        select(
            ListenEvent.track_id,
            func.count(ListenEvent.id).label("c"),
            TrackFeatures.title,
            TrackFeatures.artist,
        )
        .outerjoin(TrackFeatures, or_(
            ListenEvent.track_id == TrackFeatures.track_id,
            ListenEvent.track_id == TrackFeatures.external_track_id,
        ))
        .where(ListenEvent.timestamp >= day_ago)
        .group_by(ListenEvent.track_id, TrackFeatures.title, TrackFeatures.artist)
        .order_by(func.count(ListenEvent.id).desc())
        .limit(10)
    )).all()
    top_tracks = [
        {"track_id": r[0], "events": r[1], "title": r[2], "artist": r[3]}
        for r in track_rows
    ]

    # Latest scan
    scan_row = (await session.execute(
        select(LibraryScanState).order_by(LibraryScanState.id.desc()).limit(1)
    )).scalar_one_or_none()
    latest_scan = None
    if scan_row:
        now_ts = int(time.time())
        skipped = scan_row.files_skipped or 0
        processed = scan_row.files_analyzed + scan_row.files_failed + skipped
        percent = round(processed / scan_row.files_found * 100, 1) if scan_row.files_found > 0 else 0.0
        elapsed = (scan_row.scan_ended_at or now_ts) - scan_row.scan_started_at

        # Rate and ETA based on analyzed+failed only (actual work, not skips)
        work_done = scan_row.files_analyzed + scan_row.files_failed
        work_remaining = scan_row.files_found - skipped - work_done
        analysis_rate = round(work_done / elapsed, 2) if elapsed > 0 and work_done > 0 else None
        eta = None
        if scan_row.status == "running" and analysis_rate and analysis_rate > 0:
            eta = int(work_remaining / analysis_rate)

        latest_scan = {
            "scan_id": scan_row.id,
            "status": scan_row.status,
            "files_found": scan_row.files_found,
            "files_analyzed": scan_row.files_analyzed,
            "files_skipped": skipped,
            "files_failed": scan_row.files_failed,
            "percent_complete": percent,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "rate_per_sec": analysis_rate,
            "current_file": scan_row.current_file,
            "started_at": scan_row.scan_started_at,
            "ended_at": scan_row.scan_ended_at,
        }

    return {
        "total_events": total_events,
        "total_users": total_users,
        "total_tracks_analyzed": total_tracks,
        "total_playlists": total_playlists,
        "events_last_24h": events_24h,
        "events_last_1h": events_1h,
        "event_types_24h": event_types,
        "top_tracks_24h": top_tracks,
        "latest_scan": latest_scan,
        "analysis_version": ANALYSIS_VERSION,
    }


@router.post(
    "/pipeline/run",
    summary="Trigger the recommendation pipeline manually",
    description="Runs sessionizer → track scoring → taste profiles → CF rebuild immediately.",
)
async def trigger_pipeline(
    _key: str = Depends(require_api_key),
):
    from app.services.pipeline_state import get_current_run
    if get_current_run():
        return {"message": "Pipeline already running", "status": "running"}
    import asyncio
    from app.workers.scheduler import run_recommendation_pipeline_now
    asyncio.create_task(run_recommendation_pipeline_now(trigger="manual"))
    return {"message": "Pipeline started", "status": "running"}


@router.post(
    "/pipeline/reset",
    summary="Reset and rebuild the recommendation pipeline",
    description="Truncates sessions, interactions, and taste profiles, then reruns the full pipeline from raw events.",
)
async def reset_pipeline(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from sqlalchemy import delete, update
    import asyncio
    from app.workers.scheduler import run_recommendation_pipeline_now

    # Clear derived data
    await session.execute(delete(TrackInteraction))
    await session.execute(delete(ListenSession))
    await session.execute(
        update(User).values(taste_profile=None, profile_updated_at=None)
    )
    await session.commit()

    # Rebuild from scratch
    asyncio.create_task(run_recommendation_pipeline_now(trigger="manual"))
    return {"message": "Pipeline reset and rebuild started", "status": "running"}


@router.get(
    "/pipeline/status",
    summary="Pipeline run history and current state",
    description=(
        "Returns the current running pipeline (if any) and the last N completed runs "
        "with per-step timing, status, metrics, and errors."
    ),
)
async def pipeline_status(
    limit: int = Query(10, ge=1, le=50, description="Number of historical runs to return"),
    _key: str = Depends(require_api_key),
):
    from app.services.pipeline_state import get_current_run, get_run_history

    return {
        "current": get_current_run(),
        "history": get_run_history(limit=limit),
    }


@router.get(
    "/pipeline/stream",
    summary="SSE stream of pipeline events",
    description=(
        "Server-Sent Events stream that emits real-time pipeline step events: "
        "pipeline_start, step_start, step_complete, step_failed, pipeline_end. "
        "Connect before triggering a pipeline run to watch it execute live."
    ),
)
async def pipeline_stream(
    _key: str = Depends(require_api_key),
):
    from app.services.pipeline_state import subscribe, unsubscribe

    queue = subscribe()

    async def event_generator():
        try:
            # Send a keepalive comment immediately so the client knows we're connected.
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    event_type = event.pop("event", "message")
                    yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment every 30s to prevent proxy/browser timeouts.
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/pipeline/models",
    summary="Readiness status of all ML models in the pipeline",
    description=(
        "Returns training status for every model subsystem: ranker (LightGBM), "
        "collaborative filtering, session embeddings (Word2Vec), SASRec (transformer), "
        "session GRU, and Last.fm candidate cache."
    ),
)
async def pipeline_models(
    _key: str = Depends(require_api_key),
):
    from app.services.ranker import get_model_stats
    from app.services.collab_filter import model_stats as cf_stats
    from app.services.session_embeddings import vocab_size as emb_vocab_size
    from app.services.sasrec import vocab_size as sasrec_vocab_size
    from app.services.session_gru import is_ready as gru_is_ready
    from app.services.lastfm_candidates import cache_size as lastfm_cache_size, cache_age as lastfm_cache_age

    return {
        "ranker": get_model_stats(),
        "collab_filter": cf_stats(),
        "session_embeddings": {
            "trained": emb_vocab_size() > 0,
            "vocab_size": emb_vocab_size(),
        },
        "sasrec": {
            "trained": sasrec_vocab_size() > 0,
            "vocab_size": sasrec_vocab_size(),
        },
        "session_gru": {
            "trained": gru_is_ready(),
        },
        "lastfm_cache": {
            "built": lastfm_cache_size() > 0,
            "seeds_cached": lastfm_cache_size(),
            "cache_age_seconds": lastfm_cache_age(),
        },
    }
