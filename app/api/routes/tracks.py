"""
GrooveIQ – Track & library routes (Phase 3).

POST /v1/library/scan          – trigger a full library scan
GET  /v1/library/scan/{id}     – get scan status
GET  /v1/tracks/{track_id}     – get audio features for a track
GET  /v1/tracks/{track_id}/similar – get acoustically similar tracks
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, require_api_key
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
    require_admin(_key)
    from app.services.media_server import is_configured, sync_track_ids

    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="No media server configured. Set MEDIA_SERVER_TYPE, MEDIA_SERVER_URL, "
            "and credentials in your .env file.",
        )

    from app.core.audit import audit_log

    audit_log("media_server_sync", api_key=_key)

    try:
        result = await sync_track_ids(session)
    except Exception:
        logger.exception("Media server sync failed")
        raise HTTPException(status_code=502, detail="Media server sync failed. Check logs for details.")

    return {
        "message": "Sync complete",
        "server_type": result.server_type,
        "tracks_fetched": result.tracks_fetched,
        "tracks_matched": result.tracks_matched,
        "tracks_matched_by_mbid": result.tracks_matched_by_mbid,
        "tracks_matched_by_aatd": result.tracks_matched_by_aatd,
        "tracks_matched_by_path": result.tracks_matched_by_path,
        "tracks_aatd_ambiguous": result.tracks_aatd_ambiguous,
        "tracks_updated": result.tracks_updated,
        "tracks_metadata": result.tracks_metadata,
        "tracks_unmatched": result.tracks_unmatched,
        "errors": result.errors,
        "elapsed_seconds": result.elapsed_s,
    }


# ---------------------------------------------------------------------------
# Stale-track cleanup (one-shot, admin-only)
# ---------------------------------------------------------------------------


@router.post(
    "/library/cleanup-stale",
    status_code=200,
    summary="Delete TrackFeatures rows whose files are gone",
    description="""
One-shot cleanup for legacy track_features rows that point at files no
longer on disk.

Targets rows whose `track_id` looks like a legacy 16-character hex
Navidrome ID (the format used before Navidrome 0.61's switch to 22-char
base62) — those are the typical residue of a pre-MBID/AATD sync that
the new matcher cannot rescue if the file has been moved or deleted.

For each candidate the endpoint checks whether `file_path` still exists.
If the file is missing, the `track_features` row plus its orphaned
`track_interactions` rows are deleted.  Rows whose files still exist
are left alone — the next `POST /v1/library/sync` should pick them up
via the MBID/AATD matcher.

Defaults to `dry_run=true` so the first call returns counts only.
Pass `dry_run=false` to actually delete.
""",
)
async def cleanup_stale_track_ids(
    dry_run: bool = Query(True, description="Report counts without deleting (default true)"),
    pattern: str = Query(
        "legacy_hex",
        description="Which stale-id pattern to target. Currently only 'legacy_hex' is supported (16 hex chars).",
    ),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    import os

    from sqlalchemy import delete as sql_delete

    from app.core.audit import audit_log
    from app.models.db import ListenEvent, TrackInteraction

    require_admin(_key)
    audit_log("library_cleanup_stale", api_key=_key, detail={"dry_run": dry_run, "pattern": pattern})

    if pattern != "legacy_hex":
        raise HTTPException(status_code=400, detail=f"Unknown pattern '{pattern}'. Supported: legacy_hex.")

    # SQLite GLOB is the most portable way to express "16 lowercase-hex chars"
    # without a regex extension.  PostgreSQL would need ~ '^[0-9a-f]{16}$' —
    # we keep it portable by post-filtering in Python.
    rows = (
        await session.execute(
            select(TrackFeatures.id, TrackFeatures.track_id, TrackFeatures.file_path).where(
                func.length(TrackFeatures.track_id) == 16
            )
        )
    ).all()

    # Post-filter on hex shape so we never delete a 16-char base62 ID by mistake.
    hex_chars = set("0123456789abcdef")
    candidates = [r for r in rows if all(c in hex_chars for c in r.track_id.lower())]

    files_missing: list[tuple[int, str, str]] = []  # (tf_id, track_id, file_path)
    files_present: list[tuple[int, str, str]] = []
    for r in candidates:
        # File-existence check is sync I/O but on a few hundred to ~1k paths
        # it is well below 1s on any sensible disk; not worth threading.
        if r.file_path and os.path.isfile(r.file_path):
            files_present.append((r.id, r.track_id, r.file_path))
        else:
            files_missing.append((r.id, r.track_id, r.file_path or ""))

    deleted_track_features = 0
    deleted_interactions = 0
    deleted_events = 0
    if not dry_run and files_missing:
        for tf_id, tid, _fp in files_missing:
            interactions_res = await session.execute(
                sql_delete(TrackInteraction).where(TrackInteraction.track_id == tid)
            )
            deleted_interactions += interactions_res.rowcount or 0

            events_res = await session.execute(sql_delete(ListenEvent).where(ListenEvent.track_id == tid))
            deleted_events += events_res.rowcount or 0

            tf_res = await session.execute(sql_delete(TrackFeatures).where(TrackFeatures.id == tf_id))
            deleted_track_features += tf_res.rowcount or 0

        await session.commit()
        logger.info(
            "Stale cleanup: deleted %d track_features, %d interactions, %d events",
            deleted_track_features,
            deleted_interactions,
            deleted_events,
        )

    return {
        "pattern": pattern,
        "dry_run": dry_run,
        "candidates_total": len(candidates),
        "files_missing": len(files_missing),
        "files_present": len(files_present),
        "deleted_track_features": deleted_track_features,
        "deleted_interactions": deleted_interactions,
        "deleted_events": deleted_events,
        "next_step": (
            "Run POST /v1/library/sync to re-match the rows whose files still exist."
            if files_present
            else "All stale rows resolved."
        ),
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
    require_admin(_key)
    from app.core.audit import audit_log

    audit_log("library_scan", api_key=_key)
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
    require_admin(_key)
    result = await get_scan_status(scan_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found.")
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
    require_admin(_key)
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
    search: str | None = Query(None, description="Search by title, artist, or track ID"),
    min_bpm: float | None = Query(None),
    max_bpm: float | None = Query(None),
    min_energy: float | None = Query(None),
    max_energy: float | None = Query(None),
    key: str | None = Query(None),
    mode: str | None = Query(None),
    mood: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from sqlalchemy import or_

    q = select(TrackFeatures).where(TrackFeatures.analysis_error.is_(None))

    # Search (file_path excluded — prevents filesystem enumeration)
    if search and search.strip():
        term = f"%{search.strip()[:256]}%"
        q = q.where(
            or_(
                TrackFeatures.title.ilike(term),
                TrackFeatures.artist.ilike(term),
                TrackFeatures.album.ilike(term),
                TrackFeatures.genre.ilike(term),
                TrackFeatures.track_id.ilike(term),
            )
        )

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
    result = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id == track_id))
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(
            status_code=404,
            detail="Not found.",
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
    result = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id == track_id))
    seed = result.scalar_one_or_none()
    if seed is None:
        raise HTTPException(status_code=404, detail="Not found.")

    # Try FAISS first (Phase 4).
    from app.services.faiss_index import is_ready, search_by_track_id

    if is_ready():
        faiss_results = search_by_track_id(track_id, k=limit)
        if faiss_results:
            similar_ids = [tid for tid, _ in faiss_results]
            scores_map = {tid: score for tid, score in faiss_results}
            feat_result = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(similar_ids)))
            feat_map = {t.track_id: t for t in feat_result.scalars().all()}
            # Preserve FAISS ranking order.
            candidates = [feat_map[tid] for tid in similar_ids if tid in feat_map]
            return [
                {
                    "track_id": c.track_id,
                    "title": c.title,
                    "artist": c.artist,
                    "album": c.album,
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
        q = q.where(
            TrackFeatures.energy.between(
                max(0.0, seed.energy - 0.25),
                min(1.0, seed.energy + 0.25),
            )
        )
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
            "title": c.title,
            "artist": c.artist,
            "album": c.album,
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


# ---------------------------------------------------------------------------
# 2D music map (UMAP projection of EffNet embeddings)
# ---------------------------------------------------------------------------


@router.get(
    "/tracks/map",
    summary="2D music map coordinates",
    description="""
Returns a flat list of tracks with their 2D coordinates (``x``, ``y`` in
``[0, 1]``) for the dashboard music-map visualisation.

Coordinates are computed offline by the pipeline's ``music_map`` step, which
runs UMAP over the 64-dim EffNet embeddings. Tracks that haven't been
analysed (or where the step hasn't run yet) are excluded.

Use ``limit`` to cap the payload; useful when rendering on low-power devices.
""",
)
async def get_music_map(
    limit: int = Query(5000, ge=100, le=20000),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    q = (
        select(
            TrackFeatures.track_id,
            TrackFeatures.title,
            TrackFeatures.artist,
            TrackFeatures.genre,
            TrackFeatures.bpm,
            TrackFeatures.energy,
            TrackFeatures.mood_tags,
            TrackFeatures.map_x,
            TrackFeatures.map_y,
        )
        .where(TrackFeatures.map_x.isnot(None))
        .where(TrackFeatures.map_y.isnot(None))
        .limit(limit)
    )
    rows = (await session.execute(q)).all()

    def _dominant_mood(tags):
        if not tags:
            return None
        try:
            return max(tags, key=lambda t: t.get("confidence", 0)).get("label")
        except Exception:
            return None

    return {
        "count": len(rows),
        "tracks": [
            {
                "track_id": r.track_id,
                "title": r.title,
                "artist": r.artist,
                "genre": r.genre,
                "bpm": r.bpm,
                "energy": r.energy,
                "mood": _dominant_mood(r.mood_tags),
                "x": r.map_x,
                "y": r.map_y,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# CLAP backfill (admin): populate clap_embedding for existing tracks
# ---------------------------------------------------------------------------


@router.post(
    "/tracks/clap/backfill",
    status_code=202,
    summary="Backfill CLAP embeddings for existing tracks",
    description="""
Fires a background task that computes the CLAP audio embedding for every
track missing one. Useful after first enabling ``CLAP_ENABLED=true`` on a
library that was scanned beforehand.

Admin-only. Returns immediately with ``{status: "accepted", pending: <count>}``;
progress is visible through the normal logs and by polling
``GET /v1/tracks/clap/stats`` (count of tracks with / without ``clap_embedding``).
""",
)
async def trigger_clap_backfill(
    limit: int | None = Query(None, ge=1, le=50000),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)

    from app.core.config import settings

    if not settings.CLAP_ENABLED:
        raise HTTPException(
            status_code=400,
            detail="CLAP_ENABLED=false; enable CLAP and provide model files before backfilling.",
        )

    from sqlalchemy import func as sqlfunc

    pending = await session.scalar(
        select(sqlfunc.count())
        .select_from(TrackFeatures)
        .where(TrackFeatures.clap_embedding.is_(None))
        .where(TrackFeatures.analysis_error.is_(None))
    )

    import asyncio as _asyncio

    from app.services.clap_backfill import backfill_clap_embeddings

    # Run in the background so the HTTP call returns quickly.
    _asyncio.create_task(backfill_clap_embeddings(limit=limit))

    return {"status": "accepted", "pending": pending, "limit": limit}


@router.get(
    "/tracks/clap/stats",
    summary="CLAP embedding coverage stats",
)
async def get_clap_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from sqlalchemy import func as sqlfunc

    from app.core.config import settings

    total = await session.scalar(select(sqlfunc.count()).select_from(TrackFeatures))
    with_clap = await session.scalar(
        select(sqlfunc.count()).select_from(TrackFeatures).where(TrackFeatures.clap_embedding.isnot(None))
    )
    return {
        "enabled": settings.CLAP_ENABLED,
        "total_tracks": total or 0,
        "with_clap_embedding": with_clap or 0,
        "coverage": round((with_clap or 0) / max(1, total or 1), 4),
    }


# ---------------------------------------------------------------------------
# Natural-language track search (CLAP text embedding)
# ---------------------------------------------------------------------------


@router.get(
    "/tracks/text-search",
    summary="Search tracks by a natural-language prompt (CLAP)",
    description="""
Encodes the given text prompt via CLAP and returns the k closest tracks in
CLAP embedding space. Enables queries like ``"melancholic rainy-night jazz"``
or ``"high-energy gym rap"``.

Returns **503** if CLAP is disabled (``CLAP_ENABLED=false``) or the CLAP
FAISS index has not been built yet.
""",
)
async def text_search_tracks(
    q: str = Query(..., min_length=1, max_length=256, description="Natural-language prompt"),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from app.core.config import settings

    if not settings.CLAP_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="CLAP text search is disabled. Set CLAP_ENABLED=true to enable.",
        )

    from app.services import clap_text
    from app.services.faiss_index import clap_index

    if not clap_index.is_ready():
        raise HTTPException(
            status_code=503,
            detail="CLAP index not built yet. Run the pipeline after CLAP embeddings are populated.",
        )

    try:
        query_vec = clap_text.encode_text(q)
    except Exception as e:
        logger.warning("CLAP text encoding failed: %s", e)
        raise HTTPException(status_code=503, detail="CLAP text encoder unavailable.") from e

    hits = clap_index.search(query_vec, k=limit)
    if not hits:
        return {"query": q, "count": 0, "tracks": []}

    ids = [tid for tid, _ in hits]
    scores_map = {tid: score for tid, score in hits}
    feat_result = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(ids)))
    feat_map = {t.track_id: t for t in feat_result.scalars().all()}
    candidates = [feat_map[tid] for tid in ids if tid in feat_map]

    return {
        "query": q,
        "count": len(candidates),
        "tracks": [
            {
                "track_id": c.track_id,
                "title": c.title,
                "artist": c.artist,
                "album": c.album,
                "bpm": c.bpm,
                "energy": c.energy,
                "mood_tags": c.mood_tags,
                "similarity": round(scores_map.get(c.track_id, 0.0), 4),
            }
            for c in candidates
        ],
    }
