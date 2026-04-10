"""
spotdl-api — Thin REST wrapper around spotDL.

Provides search, download, and status endpoints so GrooveIQ (or any
HTTP client) can trigger Spotify-matched downloads without embedding
spotDL as a direct dependency.

Downloads are backed by YouTube Music audio; metadata comes from Spotify.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/music")
OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "opus")
BITRATE = os.environ.get("BITRATE", "auto")
OUTPUT_TEMPLATE = os.environ.get(
    "OUTPUT_TEMPLATE",
    "{artist}/{album}/{artists} - {title}.{output-ext}",
)
MAX_THREADS = int(os.environ.get("MAX_THREADS", "4"))
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("spotdl-api")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="spotdl-api", version="1.0.0")

# Thread pool for blocking spotDL calls (spotDL is sync internally)
_executor = ThreadPoolExecutor(max_workers=MAX_THREADS)


# ---------------------------------------------------------------------------
# Lazy spotDL init (import is slow, do it once on first use)
# ---------------------------------------------------------------------------

_spotdl_instance = None
_spotdl_lock = asyncio.Lock()


def _build_spotdl():
    """Create the Spotdl instance (called in the main thread once)."""
    from spotdl import Spotdl

    kwargs: Dict[str, Any] = {
        "downloader_settings": {
            "format": OUTPUT_FORMAT,
            "bitrate": BITRATE,
            "output": os.path.join(OUTPUT_DIR, OUTPUT_TEMPLATE),
            "threads": MAX_THREADS,
            "overwrite": "skip",
            "simple_tui": True,
        },
    }
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        kwargs["client_id"] = SPOTIFY_CLIENT_ID
        kwargs["client_secret"] = SPOTIFY_CLIENT_SECRET

    return Spotdl(**kwargs)


async def _get_spotdl():
    global _spotdl_instance
    if _spotdl_instance is None:
        async with _spotdl_lock:
            if _spotdl_instance is None:
                loop = asyncio.get_running_loop()
                _spotdl_instance = await loop.run_in_executor(
                    _executor, _build_spotdl
                )
    return _spotdl_instance


# ---------------------------------------------------------------------------
# Task registry (in-memory, ephemeral)
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    queued = "queued"
    downloading = "downloading"
    complete = "complete"
    error = "error"


class TaskState(BaseModel):
    task_id: str
    status: TaskStatus
    progress: Optional[float] = None
    error: Optional[str] = None
    spotify_id: str = ""
    title: str = ""
    artist: str = ""
    file_path: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


# task_id -> TaskState
_tasks: Dict[str, TaskState] = {}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    spotify_id: str
    title: str
    artist: str
    artists: List[str]
    album: Optional[str] = None
    album_artist: Optional[str] = None
    duration: Optional[int] = None
    cover_url: Optional[str] = None
    url: str


class DownloadRequest(BaseModel):
    spotify_id: str


class DownloadResponse(BaseModel):
    task_id: str
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _song_to_search_result(song) -> Dict[str, Any]:
    """Convert a spotDL Song object to a flat dict."""
    return {
        "spotify_id": song.song_id or "",
        "title": song.name or "",
        "artist": song.artist or "",
        "artists": list(song.artists) if song.artists else [],
        "album": song.album_name or None,
        "album_artist": song.album_artist or None,
        "duration": song.duration or None,
        "cover_url": song.cover_url or None,
        "url": song.url or "",
    }


def _do_search(spotdl_instance, query: str, limit: int) -> List[Dict[str, Any]]:
    """Run spotDL search (blocking). Called in thread pool."""
    songs = spotdl_instance.search([query])
    results = [_song_to_search_result(s) for s in songs[:limit]]
    return results


def _do_download(spotdl_instance, task_id: str, spotify_url: str) -> None:
    """Run spotDL download (blocking). Called in thread pool.

    Updates the task state in _tasks as it progresses.
    """
    task = _tasks.get(task_id)
    if not task:
        return

    try:
        task.status = TaskStatus.downloading
        task.updated_at = time.time()

        # Resolve the Spotify URL to a Song object
        songs = spotdl_instance.search([spotify_url])
        if not songs:
            task.status = TaskStatus.error
            task.error = f"No results found for {spotify_url}"
            task.updated_at = time.time()
            return

        song = songs[0]
        task.title = song.name or ""
        task.artist = song.artist or ""
        task.progress = 0.0
        task.updated_at = time.time()

        # Download
        song_result, path = spotdl_instance.download(song)

        if path is not None:
            task.status = TaskStatus.complete
            task.file_path = str(path)
            task.progress = 100.0
        else:
            task.status = TaskStatus.error
            task.error = "Download returned no file (may already exist or failed silently)"

        task.updated_at = time.time()

    except Exception as exc:
        logger.exception("Download failed for task %s: %s", task_id, exc)
        task.status = TaskStatus.error
        task.error = str(exc)[:1024]
        task.updated_at = time.time()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "spotdl-api"}


@app.get("/search", response_model=List[SearchResult])
async def search(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
):
    """Search Spotify for tracks (metadata via Spotify, audio via YouTube Music)."""
    spotdl = await _get_spotdl()
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            _executor, _do_search, spotdl, q, limit
        )
    except Exception as exc:
        logger.error("Search failed for %r: %s", q, exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return results


@app.post("/download", response_model=DownloadResponse)
async def download(body: DownloadRequest):
    """Trigger a track download by Spotify ID. Returns immediately with a task_id."""
    spotify_url = f"https://open.spotify.com/track/{body.spotify_id}"

    now = time.time()
    task_id = uuid.uuid4().hex[:16]
    task = TaskState(
        task_id=task_id,
        status=TaskStatus.queued,
        spotify_id=body.spotify_id,
        created_at=now,
        updated_at=now,
    )
    _tasks[task_id] = task

    # Fire and forget — download runs in the thread pool
    spotdl = await _get_spotdl()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(_executor, _do_download, spotdl, task_id, spotify_url)

    logger.info(
        "Download queued: task=%s spotify_id=%s", task_id, body.spotify_id
    )
    return DownloadResponse(task_id=task_id, status=task.status.value)


@app.get("/status/{task_id}")
async def get_status(task_id: str):
    """Check download progress for a task."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.model_dump()


@app.get("/tasks")
async def list_tasks(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
):
    """List recent tasks (newest first)."""
    tasks = sorted(_tasks.values(), key=lambda t: t.created_at, reverse=True)
    if status:
        tasks = [t for t in tasks if t.status.value == status]
    return tasks[:limit]


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    logger.info(
        "spotdl-api starting: format=%s, bitrate=%s, output=%s, threads=%d",
        OUTPUT_FORMAT, BITRATE, OUTPUT_DIR, MAX_THREADS,
    )
    # Pre-warm spotDL instance (downloads Spotify client credentials, etc.)
    await _get_spotdl()
    logger.info("spotDL initialized")


@app.on_event("shutdown")
async def shutdown():
    _executor.shutdown(wait=False)
    logger.info("spotdl-api shutting down")
