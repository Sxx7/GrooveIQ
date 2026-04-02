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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import TrackFeatures
from app.models.schemas import ScanStatusResponse, ScanTriggerResponse, TrackFeaturesResponse
from app.workers.library_scanner import get_scan_status, trigger_scan

logger = logging.getLogger(__name__)
router = APIRouter()


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
    # Get the seed track
    result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id == track_id)
    )
    seed = result.scalar_one_or_none()
    if seed is None:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    # SQL-level pre-filter (fast, approximate)
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

    q = q.limit(limit * 3)   # over-fetch, then re-rank by embedding similarity

    candidates_result = await session.execute(q)
    candidates = candidates_result.scalars().all()

    # Re-rank by embedding cosine similarity if available
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
