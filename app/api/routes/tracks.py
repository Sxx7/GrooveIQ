"""
GrooveIQ – Track & library routes (Phase 3).

POST /v1/library/scan          – trigger a full library scan
GET  /v1/library/scan/{id}     – get scan status
GET  /v1/tracks/{track_id}     – get audio features for a track
GET  /v1/tracks/{track_id}/similar – get acoustically similar tracks
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, asc, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import ScanLog, TrackFeatures
from app.models.schemas import ScanStatusResponse, ScanTriggerResponse, TrackFeaturesResponse
from app.workers.library_scanner import get_scan_status, trigger_scan

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Media server sync
# ---------------------------------------------------------------------------

@router.post(
    "/library/sync",
    status_code=200,
    summary="Sync track IDs with media server (Navidrome / Plex)",
    description="""
Maps GrooveIQ's internal track IDs to the configured media server's native IDs.

Matches tracks by file path, then:
- Updates `track_id` to the Navidrome/Plex ID
- Populates title, artist, album metadata from the server
- Cascades track_id changes to all events, sessions, and interactions

Requires `MEDIA_SERVER_TYPE`, `MEDIA_SERVER_URL`, and credentials in config.
""",
)
async def trigger_sync(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from app.services.media_server import is_configured, sync_track_ids

    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="No media server configured. Set MEDIA_SERVER_TYPE, MEDIA_SERVER_URL, "
                   "and credentials in your .env file.",
        )

    try:
        result = await sync_track_ids(session)
    except Exception as e:
        logger.exception("Media server sync failed")
        raise HTTPException(status_code=502, detail=f"Sync failed: {e}")

    return {
        "message": "Sync complete",
        "server_type": result.server_type,
        "tracks_fetched": result.tracks_fetched,
        "tracks_matched": result.tracks_matched,
        "tracks_updated": result.tracks_updated,
        "tracks_metadata": result.tracks_metadata,
        "tracks_unmatched": result.tracks_unmatched,
        "errors": result.errors,
        "elapsed_seconds": result.elapsed_s,
    }


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------

@router.post(
    "/library/scan",
    response_model=ScanTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a library scan",
    description="""
Kicks off an asynchronous scan of the configured `MUSIC_LIBRARY_PATH`.

- New files are analyzed and added to the track index.
- Changed files (detected via SHA-256 hash) are re-analyzed.
- Unchanged files are skipped.

If a scan is already running, returns the existing scan's ID.
Poll `GET /v1/library/scan/{scan_id}` for progress.
""",
)
async def trigger_library_scan(
    _key: str = Depends(require_api_key),
):
    scan_id = await trigger_scan()
    return ScanTriggerResponse(
        message="Scan started",
        scan_id=scan_id,
        status="running",
    )


@router.get(
    "/library/scan/{scan_id}",
    response_model=ScanStatusResponse,
    summary="Get scan status",
)
async def get_library_scan_status(
    scan_id: int,
    _key: str = Depends(require_api_key),
):
    result = await get_scan_status(scan_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Scan {scan_id} not found.")
    return result


@router.get(
    "/library/scan/{scan_id}/logs",
    summary="Get recent scan log entries",
)
async def get_scan_logs(
    scan_id: int,
    limit: int = Query(50, ge=1, le=200),
    after_id: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns recent per-file log entries for a scan.
    Use `after_id` to poll for new entries since the last fetch."""
    q = select(ScanLog).where(ScanLog.scan_id == scan_id)
    if after_id > 0:
        q = q.where(ScanLog.id > after_id)
    q = q.order_by(ScanLog.id.desc()).limit(limit)
    result = await session.execute(q)
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "timestamp": r.timestamp,
            "level": r.level,
            "filename": r.filename,
            "message": r.message,
        }
        for r in reversed(rows)  # oldest first
    ]


# ---------------------------------------------------------------------------
# Track listing
# ---------------------------------------------------------------------------

_SORT_COLUMNS = {
    "bpm": TrackFeatures.bpm,
    "energy": TrackFeatures.energy,
    "danceability": TrackFeatures.danceability,
    "valence": TrackFeatures.valence,
    "key": TrackFeatures.key,
    "duration": TrackFeatures.duration,
    "analyzed_at": TrackFeatures.analyzed_at,
    "analysis_version": TrackFeatures.analysis_version,
}


@router.get(
    "/tracks",
    summary="List analyzed tracks with filtering and sorting",
)
async def list_tracks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("bpm"),
    sort_dir: str = Query("asc"),
    search: Optional[str] = Query(None, description="Search by title, artist, or track ID"),
    min_bpm: Optional[float] = Query(None),
    max_bpm: Optional[float] = Query(None),
    min_energy: Optional[float] = Query(None),
    max_energy: Optional[float] = Query(None),
    key: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    mood: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from sqlalchemy import or_

    q = select(TrackFeatures).where(TrackFeatures.analysis_error.is_(None))

    # Search
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.where(or_(
            TrackFeatures.title.ilike(term),
            TrackFeatures.artist.ilike(term),
            TrackFeatures.album.ilike(term),
            TrackFeatures.genre.ilike(term),
            TrackFeatures.track_id.ilike(term),
            TrackFeatures.file_path.ilike(term),
        ))

    # Filters
    if min_bpm is not None:
        q = q.where(TrackFeatures.bpm >= min_bpm)
    if max_bpm is not None:
        q = q.where(TrackFeatures.bpm <= max_bpm)
    if min_energy is not None:
        q = q.where(TrackFeatures.energy >= min_energy)
    if max_energy is not None:
        q = q.where(TrackFeatures.energy <= max_energy)
    if key:
        q = q.where(TrackFeatures.key == key)
    if mode:
        q = q.where(TrackFeatures.mode == mode)
    # Count total before pagination
    count_q = select(func.count()).select_from(q.subquery())
    total = (await session.execute(count_q)).scalar() or 0

    # Sort
    sort_col = _SORT_COLUMNS.get(sort_by, TrackFeatures.bpm)
    order = desc(sort_col) if sort_dir == "desc" else asc(sort_col)
    q = q.order_by(order).offset(offset).limit(limit)

    result = await session.execute(q)
    tracks_raw = result.scalars().all()

    # Post-filter by mood tag if requested (JSON column, hard to filter in SQL)
    tracks = tracks_raw
    if mood:
        filtered = []
        for t in tracks_raw:
            if t.mood_tags:
                tags = t.mood_tags if isinstance(t.mood_tags, list) else []
                for tag in tags:
                    if isinstance(tag, dict) and tag.get("label") == mood and tag.get("confidence", 0) > 0.3:
                        filtered.append(t)
                        break
        tracks = filtered

    return {
        "total": total,
        "tracks": [
            {
                "track_id": t.track_id,
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "genre": t.genre,
                "file_path": t.file_path,
                "duration": t.duration,
                "bpm": t.bpm,
                "key": t.key,
                "mode": t.mode,
                "energy": t.energy,
                "danceability": t.danceability,
                "valence": t.valence,
                "instrumentalness": t.instrumentalness,
                "mood_tags": t.mood_tags,
                "analyzed_at": t.analyzed_at,
                "analysis_version": t.analysis_version,
            }
            for t in tracks
        ],
    }


# ---------------------------------------------------------------------------
# Track features
# ---------------------------------------------------------------------------

@router.get(
    "/tracks/{track_id}/features",
    response_model=TrackFeaturesResponse,
    summary="Get audio features for a track",
    description="""
Returns the Essentia-extracted acoustic features for a track.

`track_id` must match the identifier used when sending events —
typically the track ID from your media server (Navidrome, Jellyfin, etc.)
or the file stem if using direct library mode.

Returns **404** if the track has not been analyzed yet.
""",
)
async def get_track_features(
    track_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id == track_id)
    )
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(
            status_code=404,
            detail=f"Track '{track_id}' not found. Trigger a library scan first.",
        )
    return track


@router.get(
    "/tracks/{track_id}/similar",
    summary="Get acoustically similar tracks",
    description="""
Returns tracks from the library that are acoustically similar to the
given track, ranked by cosine similarity of their feature embeddings.

This is a lightweight pre-FAISS fallback using SQL range filtering
(BPM ± 15, energy ± 0.2). Phase 4 replaces this with full FAISS ANN search.

**limit**: number of results (max 50).
**include_features**: include full feature objects in response.
""",
)
async def get_similar_tracks(
    track_id: str,
    limit: int = Query(10, ge=1, le=50),
    include_features: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Verify seed track exists.
    result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id == track_id)
    )
    seed = result.scalar_one_or_none()
    if seed is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    # Try FAISS first (Phase 4).
    from app.services.faiss_index import is_ready, search_by_track_id
    if is_ready():
        faiss_results = search_by_track_id(track_id, k=limit)
        if faiss_results:
            similar_ids = [tid for tid, _ in faiss_results]
            scores_map = {tid: score for tid, score in faiss_results}
            feat_result = await session.execute(
                select(TrackFeatures).where(TrackFeatures.track_id.in_(similar_ids))
            )
            feat_map = {t.track_id: t for t in feat_result.scalars().all()}
            # Preserve FAISS ranking order.
            candidates = [feat_map[tid] for tid in similar_ids if tid in feat_map]
            return [
                {
                    "track_id": c.track_id,
                    "title": c.title,
                    "artist": c.artist,
                    "album": c.album,
                    "file_path": c.file_path,
                    "bpm": c.bpm,
                    "key": c.key,
                    "mode": c.mode,
                    "energy": c.energy,
                    "danceability": c.danceability,
                    "mood_tags": c.mood_tags,
                    "similarity": round(scores_map.get(c.track_id, 0), 4),
                    **({"features": TrackFeaturesResponse.model_validate(c).model_dump()} if include_features else {}),
                }
                for c in candidates
            ]

    # SQL fallback (pre-FAISS path, kept for when index isn't built yet).
    q = select(TrackFeatures).where(TrackFeatures.track_id != track_id)

    if seed.bpm:
        q = q.where(TrackFeatures.bpm.between(seed.bpm - 15, seed.bpm + 15))
    if seed.energy is not None:
        q = q.where(TrackFeatures.energy.between(
            max(0.0, seed.energy - 0.25),
            min(1.0, seed.energy + 0.25),
        ))
    if seed.mode:
        q = q.where(TrackFeatures.mode == seed.mode)

    q = q.limit(limit * 3)
    candidates_result = await session.execute(q)
    candidates = candidates_result.scalars().all()

    if seed.embedding and candidates:
        import base64
        import numpy as np

        def decode_vec(b64: str):
            return np.frombuffer(base64.b64decode(b64), dtype=np.float32)

        try:
            seed_vec = decode_vec(seed.embedding)
            seed_norm = np.linalg.norm(seed_vec)

            scored = []
            for c in candidates:
                if c.embedding:
                    try:
                        vec = decode_vec(c.embedding)
                        cos_sim = float(np.dot(seed_vec, vec) / (seed_norm * np.linalg.norm(vec) + 1e-9))
                        scored.append((cos_sim, c))
                    except Exception:
                        scored.append((0.0, c))
                else:
                    scored.append((0.5, c))

            scored.sort(key=lambda x: x[0], reverse=True)
            candidates = [c for _, c in scored[:limit]]
        except Exception:
            candidates = candidates[:limit]
    else:
        candidates = candidates[:limit]

    return [
        {
            "track_id": c.track_id,
            "file_path": c.file_path,
            "bpm": c.bpm,
            "key": c.key,
            "mode": c.mode,
            "energy": c.energy,
            "danceability": c.danceability,
            "mood_tags": c.mood_tags,
            **({"features": TrackFeaturesResponse.model_validate(c).model_dump()} if include_features else {}),
        }
        for c in candidates
    ]
