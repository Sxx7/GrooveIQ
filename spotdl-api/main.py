"""
spotdl-api — Thin REST wrapper around spotDL.

Provides search, download, and status endpoints so GrooveIQ (or any
HTTP client) can trigger Spotify-matched downloads without embedding
spotDL as a direct dependency.

Downloads are backed by YouTube Music audio; metadata comes from Spotify.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/music")
# Readiness self-check (GitHub issue #123). When > 0, /health also requires the
# /music mount to contain at least this many entries — catches the rarer
# "writable but empty/wrong inode" stale-mount case. 0 (default) gates on
# writability only, which has zero false positives on a legitimately-empty
# library.
MUSIC_MIN_ENTRIES = int(os.environ.get("MUSIC_MIN_ENTRIES", "0"))
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


def _patch_spotdl_sparse_api():
    """Monkey-patch spotDL's Song.from_url to handle Spotify's sparse API responses.

    Spotify's API no longer reliably returns all fields that spotDL expects
    (genres, label, copyrights, external_ids, images, tracks, etc.).
    spotDL 4.x uses raw dict['key'] access throughout Song.from_url and
    crashes with KeyError on any missing field.

    Instead of patching individual fields, we wrap the SpotifyClient's
    track/artist/album methods to return dicts that never raise KeyError
    — a defaulting wrapper that returns sensible empty values for any
    missing key.
    """
    try:
        from spotdl.utils.spotify import SpotifyClient

        class SafeDict(dict):
            """Dict subclass that returns safe defaults for missing keys.

            Prevents KeyError crashes in spotDL's Song.from_url which uses
            raw_meta["key"] access on Spotify API responses.
            """
            _DEFAULTS = {
                "genres": [],
                "label": "",
                "copyrights": [],
                "external_ids": {},
                "external_urls": {},
                "images": [],
                "artists": [],
                "tracks": {"items": []},
                "album": {},
                "name": "",
                "id": "",
                "type": "",
                "release_date": "0000",
                "total_tracks": 0,
                "disc_number": 1,
                "track_number": 0,
                "duration_ms": 0,
                "explicit": False,
                "popularity": 0,
                "is_local": False,
            }

            def __missing__(self, key):
                return self._DEFAULTS.get(key, "")

        def _to_safe(obj):
            """Recursively convert dicts to SafeDict."""
            if isinstance(obj, dict) and not isinstance(obj, SafeDict):
                return SafeDict({k: _to_safe(v) for k, v in obj.items()})
            if isinstance(obj, list):
                return [_to_safe(i) for i in obj]
            return obj

        # Wrap every SpotifyClient method that returns dict-shaped data spotDL
        # then walks with raw key access.  search() and playlist*() are critical
        # — if they're not wrapped, free-text /search calls return [] because
        # Song.from_search_result() KeyErrors on missing 'genres'/'external_ids'
        # and spotDL silently drops the result.
        for method_name in (
            "track",
            "artist",
            "album",
            "search",
            "playlist",
            "playlist_items",
            "user_playlists",
            "album_tracks",
            "artist_albums",
            "artist_top_tracks",
            "artist_related_artists",
        ):
            _orig = getattr(SpotifyClient, method_name, None)
            if _orig is None:
                continue

            def _make_safe(orig_method):
                def _safe(self, *a, **kw):
                    result = orig_method(self, *a, **kw)
                    return _to_safe(result)
                _safe.__name__ = orig_method.__name__
                return _safe

            setattr(SpotifyClient, method_name, _make_safe(_orig))

        logger.info("Patched SpotifyClient methods for sparse Spotify API responses")
    except Exception as exc:
        logger.warning("Failed to patch spotDL for sparse API: %s", exc)


def _build_spotdl():
    """Create the Spotdl instance (called in the main thread once)."""
    from spotdl import Spotdl

    _patch_spotdl_sparse_api()

    # client_id and client_secret are required positional args.
    # When not provided via env vars, pass empty strings — spotDL
    # falls back to its own hardcoded Spotify app credentials.
    client_id = SPOTIFY_CLIENT_ID or ""
    client_secret = SPOTIFY_CLIENT_SECRET or ""

    return Spotdl(
        client_id=client_id,
        client_secret=client_secret,
        downloader_settings={
            "format": OUTPUT_FORMAT,
            "bitrate": BITRATE,
            "output": os.path.join(OUTPUT_DIR, OUTPUT_TEMPLATE),
            "threads": MAX_THREADS,
            "overwrite": "skip",
            "simple_tui": True,
        },
    )


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
    """Convert a spotDL Song object to a flat dict.

    Uses getattr for safety — Song field names vary across spotDL versions.
    """
    return {
        "spotify_id": getattr(song, "song_id", "") or "",
        "title": getattr(song, "name", "") or "",
        "artist": getattr(song, "artist", "") or "",
        "artists": list(getattr(song, "artists", []) or []),
        "album": getattr(song, "album_name", None),
        "album_artist": getattr(song, "album_artist", None),
        "duration": getattr(song, "duration", None),
        "cover_url": getattr(song, "cover_url", None),
        "url": getattr(song, "url", "") or "",
    }


def _do_search(spotdl_instance, query: str, limit: int) -> List[Dict[str, Any]]:
    """Run spotDL search (blocking). Called in thread pool."""
    try:
        songs = spotdl_instance.search([query])
    except Exception as exc:
        logger.error("spotDL search error for %r: %s", query, exc, exc_info=True)
        raise
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
        task.title = getattr(song, "name", "") or ""
        task.artist = getattr(song, "artist", "") or ""
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

def _music_status() -> Dict[str, Any]:
    """Probe the /music bind-mount: existence, entry count, and writability.

    Detects the stale-bind-mount failure mode (GitHub issue #123): when the host
    library dir backing /music is replaced while this long-lived container keeps
    running, the container holds the old (now empty, root-owned) inode and every
    download fails with ``[Errno 13] Permission denied`` — yet a disk-blind
    /health stays green. The writability probe turns that silent failure into an
    unhealthy container.
    """
    path = OUTPUT_DIR
    out: Dict[str, Any] = {"path": path, "exists": False, "entries": None, "writable": False, "error": None}
    try:
        if not os.path.isdir(path):
            out["error"] = "directory does not exist"
            return out
        out["exists"] = True
        try:
            out["entries"] = len(os.listdir(path))
        except OSError as exc:
            out["error"] = f"cannot list: {exc}"
        probe = os.path.join(path, ".grooveiq_write_probe")
        try:
            fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            os.unlink(probe)
            out["writable"] = True
        except FileExistsError:
            # A concurrent probe left it behind — the dir is writable; clean up.
            with contextlib.suppress(OSError):
                os.unlink(probe)
            out["writable"] = True
        except OSError as exc:
            if out["error"] is None:
                out["error"] = f"not writable: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        out["error"] = str(exc)
    return out


def _music_ready(status: Dict[str, Any]) -> bool:
    """Readiness gate for /music: must exist and be writable (the definitive
    stale-mount signal). When MUSIC_MIN_ENTRIES > 0, also require that many
    entries."""
    if not status.get("exists") or not status.get("writable"):
        return False
    if MUSIC_MIN_ENTRIES > 0 and (status.get("entries") or 0) < MUSIC_MIN_ENTRIES:
        return False
    return True


@app.get("/health")
async def health():
    music = _music_status()
    ready = _music_ready(music)
    body: Dict[str, Any] = {
        "status": "ok" if ready else "degraded",
        "service": "spotdl-api",
        "ready": ready,
        "music": music,
    }
    if not ready:
        # 503 → Docker HEALTHCHECK fails → container shows (unhealthy), turning a
        # silent "every download fails" into an obvious signal (issue #123).
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/search", response_model=List[SearchResult])
async def search(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
):
    """Search Spotify for tracks (metadata via Spotify, audio via YouTube Music)."""
    spotdl = await _get_spotdl()
    if spotdl is None:
        raise HTTPException(
            status_code=503,
            detail="spotDL not initialized. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.",
        )
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
    spotdl = await _get_spotdl()
    if spotdl is None:
        raise HTTPException(
            status_code=503,
            detail="spotDL not initialized. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.",
        )
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
    try:
        await _get_spotdl()
        logger.info("spotDL initialized")
    except Exception as exc:
        logger.error(
            "spotDL init failed: %s. "
            "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET env vars. "
            "Get them free at https://developer.spotify.com/dashboard",
            exc,
        )
        # Don't crash — let health endpoint run so container doesn't restart-loop.
        # Search/download endpoints will fail gracefully when _spotdl_instance is None.


@app.on_event("shutdown")
async def shutdown():
    _executor.shutdown(wait=False)
    logger.info("spotdl-api shutting down")
