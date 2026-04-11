"""AcousticBrainz Lookup — FastAPI service for audio-feature similarity search.

Ingests the AcousticBrainz data dump on first startup, then serves a REST API
for querying ~29.5M tracks by audio features (BPM, energy, mood, genre, etc.).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ingest import DB_PATH, get_status, run_ingestion

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ab-lookup")

SAMPLE_MODE = os.environ.get("SAMPLE_MODE", "false").lower() in ("true", "1", "yes")

# Module-level DB connection (read-only, created after ingestion)
_db: sqlite3.Connection | None = None
# Ingestion thread reference for status reporting
_ingest_thread: threading.Thread | None = None


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA query_only=ON")
    return _db


def _run_ingest_background() -> None:
    """Run ingestion in a background thread."""
    try:
        run_ingestion(sample_mode=SAMPLE_MODE)
    except Exception:
        logger.exception("Ingestion failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ingest_thread
    # Check if ingestion is needed
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("SELECT 1 FROM ingestion_state WHERE key='status' AND value='completed'").fetchone()
            row = conn.execute(
                "SELECT value FROM ingestion_state WHERE key='status'"
            ).fetchone()
            conn.close()
            if row and row[0] == "completed":
                logger.info("Database ready, skipping ingestion")
            else:
                logger.info("Starting ingestion (background thread)")
                _ingest_thread = threading.Thread(target=_run_ingest_background, daemon=True)
                _ingest_thread.start()
        except sqlite3.OperationalError:
            logger.info("Starting fresh ingestion (background thread)")
            _ingest_thread = threading.Thread(target=_run_ingest_background, daemon=True)
            _ingest_thread.start()
    else:
        logger.info("Starting fresh ingestion (background thread)")
        _ingest_thread = threading.Thread(target=_run_ingest_background, daemon=True)
        _ingest_thread.start()

    yield

    global _db
    if _db is not None:
        _db.close()
        _db = None


app = FastAPI(title="AcousticBrainz Lookup", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RangeFilter(BaseModel):
    min: float | None = None
    max: float | None = None


class MoodFilters(BaseModel):
    happy: RangeFilter | None = None
    sad: RangeFilter | None = None
    aggressive: RangeFilter | None = None
    relaxed: RangeFilter | None = None
    party: RangeFilter | None = None
    acoustic: RangeFilter | None = None
    electronic: RangeFilter | None = None


class SearchRequest(BaseModel):
    bpm: RangeFilter | None = None
    energy: RangeFilter | None = None
    danceability: RangeFilter | None = None
    valence: RangeFilter | None = None
    acousticness: RangeFilter | None = None
    instrumentalness: RangeFilter | None = None
    moods: MoodFilters | None = None
    key: str | None = None
    mode: str | None = None
    genres: list[str] | None = None
    exclude_mbids: list[str] | None = None
    limit: int = Field(default=50, ge=1, le=500)
    strategy: str = Field(default="closest", pattern="^(closest|random)$")


class TrackResult(BaseModel):
    mbid: str
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    bpm: float | None = None
    key: str | None = None
    mode: str | None = None
    energy: float | None = None
    danceability: float | None = None
    valence: float | None = None
    mood_happy: float | None = None
    mood_acoustic: float | None = None
    instrumentalness: float | None = None
    genre_dortmund: str | None = None
    distance: float | None = None
    mb_artist_id: str | None = None
    mb_album_id: str | None = None


class SearchResponse(BaseModel):
    results: list[TrackResult]
    total_matches: int
    query_time_ms: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ingestion_status() -> dict:
    """Get ingestion status, handling missing DB/tables gracefully."""
    if not DB_PATH.exists():
        return {"status": "pending", "total_tracks": 0, "last_archive": ""}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        status = get_status(conn)
        conn.close()
        return status
    except sqlite3.OperationalError:
        return {"status": "pending", "total_tracks": 0, "last_archive": ""}


def _build_search_query(req: SearchRequest) -> tuple[str, dict[str, Any]]:
    """Build SQL query from search request. Returns (sql, params)."""
    where: list[str] = []
    params: dict[str, Any] = {}

    # Range filters for numeric columns
    range_cols = {
        "bpm": req.bpm,
        "energy": req.energy,
        "danceability": req.danceability,
        "valence": req.valence,
        "instrumentalness": req.instrumentalness,
    }
    # acousticness maps to mood_acoustic
    if req.acousticness:
        range_cols["mood_acoustic"] = req.acousticness

    for col, rf in range_cols.items():
        if rf is None:
            continue
        if rf.min is not None:
            where.append(f"{col} >= :{col}_min")
            params[f"{col}_min"] = rf.min
        if rf.max is not None:
            where.append(f"{col} <= :{col}_max")
            params[f"{col}_max"] = rf.max

    # Mood filters
    if req.moods:
        mood_map = {
            "happy": "mood_happy",
            "sad": "mood_sad",
            "aggressive": "mood_aggressive",
            "relaxed": "mood_relaxed",
            "party": "mood_party",
            "acoustic": "mood_acoustic",
            "electronic": "mood_electronic",
        }
        for mood_name, col in mood_map.items():
            rf = getattr(req.moods, mood_name, None)
            if rf is None:
                continue
            if rf.min is not None:
                where.append(f"{col} >= :mood_{mood_name}_min")
                params[f"mood_{mood_name}_min"] = rf.min
            if rf.max is not None:
                where.append(f"{col} <= :mood_{mood_name}_max")
                params[f"mood_{mood_name}_max"] = rf.max

    # Key/mode exact match
    if req.key:
        where.append("key = :key")
        params["key"] = req.key
    if req.mode:
        where.append("mode = :mode")
        params["mode"] = req.mode

    # Genre filter
    if req.genres:
        genre_clauses = []
        for i, g in enumerate(req.genres):
            pk = f"genre_{i}"
            genre_clauses.append(f"genre_dortmund = :{pk}")
            genre_clauses.append(f"genre_rosamerica = :{pk}")
            genre_clauses.append(f"genre_tags LIKE :genre_like_{i}")
            params[pk] = g
            params[f"genre_like_{i}"] = f"%{g}%"
        where.append(f"({' OR '.join(genre_clauses)})")

    # Exclude MBIDs
    if req.exclude_mbids:
        placeholders = ",".join(f":excl_{i}" for i in range(len(req.exclude_mbids)))
        where.append(f"mbid NOT IN ({placeholders})")
        for i, m in enumerate(req.exclude_mbids):
            params[f"excl_{i}"] = m

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # Strategy
    if req.strategy == "random":
        sql = f"SELECT *, NULL AS distance FROM tracks{where_sql} ORDER BY RANDOM() LIMIT :limit"
    else:
        # Closest: weighted distance from range midpoints
        # Compute target values from provided ranges (midpoints)
        def _mid(rf: RangeFilter | None, default: float, scale: float = 1.0) -> tuple[float, float]:
            if rf is None:
                return default, scale
            lo = rf.min if rf.min is not None else default - scale * 0.5
            hi = rf.max if rf.max is not None else default + scale * 0.5
            return (lo + hi) / 2, max(abs(hi - lo), 0.01)

        t_bpm, r_bpm = _mid(req.bpm, 120.0, 40.0)
        t_energy, _ = _mid(req.energy, 0.5)
        t_dance, _ = _mid(req.danceability, 0.5)
        t_valence, _ = _mid(req.valence, 0.5)
        t_happy, _ = _mid(req.moods.happy if req.moods else None, 0.5)
        t_relaxed, _ = _mid(req.moods.relaxed if req.moods else None, 0.5)
        t_instr, _ = _mid(req.instrumentalness, 0.5)
        t_acoustic, _ = _mid(req.acousticness, 0.5)

        params.update({
            "t_bpm": t_bpm, "r_bpm": r_bpm,
            "t_energy": t_energy, "t_dance": t_dance,
            "t_valence": t_valence, "t_happy": t_happy,
            "t_relaxed": t_relaxed, "t_instr": t_instr,
            "t_acoustic": t_acoustic,
        })

        distance_expr = """(
            0.20 * abs(COALESCE(bpm, :t_bpm) - :t_bpm) / :r_bpm +
            0.15 * abs(COALESCE(energy, :t_energy) - :t_energy) +
            0.15 * abs(COALESCE(danceability, :t_dance) - :t_dance) +
            0.10 * abs(COALESCE(valence, :t_valence) - :t_valence) +
            0.10 * abs(COALESCE(mood_happy, :t_happy) - :t_happy) +
            0.10 * abs(COALESCE(mood_relaxed, :t_relaxed) - :t_relaxed) +
            0.10 * abs(COALESCE(instrumentalness, :t_instr) - :t_instr) +
            0.10 * abs(COALESCE(mood_acoustic, :t_acoustic) - :t_acoustic)
        )"""

        sql = f"SELECT *, {distance_expr} AS distance FROM tracks{where_sql} ORDER BY distance ASC LIMIT :limit"

    params["limit"] = req.limit
    return sql, params


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    status = _ingestion_status()
    track_count = status["total_tracks"]

    if status["status"] == "completed":
        return {"status": "ready", "tracks": track_count, "ingestion": "completed"}

    last = status.get("last_archive", "")
    # Extract archive number for friendlier progress (e.g. "archive 3/30")
    progress = "starting"
    if last:
        import re
        m = re.search(r"-(\d+)\.tar\.zst$", last)
        if m:
            idx = int(m.group(1)) + 1
            total = 1 if "sample" in last else 30
            progress = f"archive {idx}/{total}"
        else:
            progress = last
    return {
        "status": "ingesting",
        "progress": progress,
        "tracks_so_far": track_count,
    }


@app.post("/v1/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    status = _ingestion_status()
    if status["status"] != "completed":
        raise HTTPException(503, "Ingestion in progress, search unavailable")

    db = _get_db()
    t0 = time.monotonic()

    sql, params = _build_search_query(req)
    rows = db.execute(sql, params).fetchall()

    # Count total matches (without limit/order)
    # Re-build a simpler count query from the WHERE clause
    try:
        where_part = sql.split(" FROM tracks", 1)[1].split("ORDER BY")[0].split("LIMIT")[0].strip()
        count_sql = f"SELECT COUNT(*) FROM tracks{' ' + where_part if where_part else ''}"
        count_params = {k: v for k, v in params.items() if k != "limit"}
        total = db.execute(count_sql, count_params).fetchone()[0]
    except (sqlite3.OperationalError, IndexError):
        total = len(rows)

    elapsed = (time.monotonic() - t0) * 1000

    results = [
        TrackResult(
            mbid=r["mbid"],
            artist=r["artist"],
            title=r["title"],
            album=r["album"],
            bpm=r["bpm"],
            key=r["key"],
            mode=r["mode"],
            energy=r["energy"],
            danceability=r["danceability"],
            valence=r["valence"],
            mood_happy=r["mood_happy"],
            mood_acoustic=r["mood_acoustic"],
            instrumentalness=r["instrumentalness"],
            genre_dortmund=r["genre_dortmund"],
            distance=r["distance"],
            mb_artist_id=r["mb_artist_id"],
            mb_album_id=r["mb_album_id"],
        )
        for r in rows
    ]

    return SearchResponse(results=results, total_matches=total, query_time_ms=round(elapsed, 1))


@app.get("/v1/track/{mbid}")
async def get_track(mbid: str):
    status = _ingestion_status()
    if status["status"] != "completed":
        raise HTTPException(503, "Ingestion in progress")

    db = _get_db()
    row = db.execute("SELECT * FROM tracks WHERE mbid = ?", (mbid,)).fetchone()
    if not row:
        raise HTTPException(404, "Track not found")

    return dict(row)


@app.get("/v1/stats")
async def stats():
    status = _ingestion_status()
    if status["status"] != "completed":
        return {"status": "ingesting", "total_tracks": status["total_tracks"]}

    db = _get_db()

    total = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    # Genre distribution (top 20 from genre_dortmund)
    genre_rows = db.execute(
        "SELECT genre_dortmund, COUNT(*) AS cnt FROM tracks "
        "WHERE genre_dortmund IS NOT NULL "
        "GROUP BY genre_dortmund ORDER BY cnt DESC LIMIT 20"
    ).fetchall()
    genres = {r["genre_dortmund"]: r["cnt"] for r in genre_rows}

    # BPM histogram (10 bins: 60-200 in steps of 14)
    bpm_bins = []
    for i in range(10):
        lo = 60 + i * 14
        hi = lo + 14
        cnt = db.execute(
            "SELECT COUNT(*) FROM tracks WHERE bpm >= ? AND bpm < ?", (lo, hi)
        ).fetchone()[0]
        bpm_bins.append({"range": f"{lo}-{hi}", "count": cnt})

    return {
        "total_tracks": total,
        "genre_distribution": genres,
        "bpm_histogram": bpm_bins,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8200)
