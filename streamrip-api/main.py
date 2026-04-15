"""
streamrip-api — Thin REST wrapper around streamrip.

Provides search, download, and status endpoints so GrooveIQ (or any
HTTP client) can trigger high-quality downloads from Qobuz, Tidal,
Deezer, or SoundCloud without embedding streamrip as a direct dependency.

Uses streamrip as a Python library (not CLI) to avoid interactive
prompt issues and get structured results directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import tomlkit
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/music")
DOWNLOAD_QUALITY = int(os.environ.get("DOWNLOAD_QUALITY", "3"))
DOWNLOAD_CODEC = os.environ.get("DOWNLOAD_CODEC", "FLAC")
MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "6"))
MAX_THREADS = int(os.environ.get("MAX_THREADS", "4"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Service credentials
QOBUZ_EMAIL = os.environ.get("QOBUZ_EMAIL", "")
QOBUZ_PASSWORD = os.environ.get("QOBUZ_PASSWORD", "")
TIDAL_EMAIL = os.environ.get("TIDAL_EMAIL", "")
TIDAL_PASSWORD = os.environ.get("TIDAL_PASSWORD", "")
DEEZER_ARL = os.environ.get("DEEZER_ARL", "")
SOUNDCLOUD_CLIENT_ID = os.environ.get("SOUNDCLOUD_CLIENT_ID", "")

DEFAULT_SERVICE = os.environ.get("DEFAULT_SERVICE", "qobuz")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("streamrip-api")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="streamrip-api", version="1.0.0")

# ---------------------------------------------------------------------------
# streamrip config management
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "streamrip"
_CONFIG_PATH = _CONFIG_DIR / "config.toml"

_available_services: list[str] = []


def _write_config():
    """Generate streamrip config.toml from environment variables.

    Uses `rip config reset` to get a valid default, then patches in
    credentials and settings via tomlkit so we never have schema mismatches.
    """
    import subprocess

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "XDG_CONFIG_HOME": str(_CONFIG_DIR.parent)}

    # Generate default config via streamrip itself — guaranteed correct schema
    subprocess.run(
        ["rip", "config", "reset"],
        capture_output=True, text=True, timeout=15,
        input="y\n",  # confirm if prompted
        env=env,
    )

    if not _CONFIG_PATH.exists():
        logger.error("rip config reset did not create %s", _CONFIG_PATH)
        return

    # Patch in our settings
    doc = tomlkit.parse(_CONFIG_PATH.read_text())

    # Downloads
    doc["downloads"]["folder"] = OUTPUT_DIR
    doc["downloads"]["max_connections"] = MAX_CONNECTIONS

    # Qobuz
    if QOBUZ_EMAIL and QOBUZ_PASSWORD:
        doc["qobuz"]["email_or_userid"] = QOBUZ_EMAIL
        doc["qobuz"]["password_or_token"] = QOBUZ_PASSWORD
        doc["qobuz"]["quality"] = DOWNLOAD_QUALITY

    # Tidal (uses OAuth, email/password not directly supported in v2)
    # Users must authenticate via `rip config tidal` interactively once

    # Deezer
    if DEEZER_ARL:
        doc["deezer"]["arl"] = DEEZER_ARL
        doc["deezer"]["quality"] = min(DOWNLOAD_QUALITY, 2)

    # SoundCloud
    if SOUNDCLOUD_CLIENT_ID:
        doc["soundcloud"]["client_id"] = SOUNDCLOUD_CLIENT_ID

    # Database paths (must be non-empty or streamrip asserts)
    if "database" in doc:
        doc["database"]["downloads_path"] = str(_CONFIG_DIR / "downloads.db")
        doc["database"]["failed_downloads_path"] = str(_CONFIG_DIR / "failed_downloads.db")

    # Filepaths
    if "filepaths" in doc:
        doc["filepaths"]["folder_format"] = "{albumartist}/{album} ({year})"
        doc["filepaths"]["track_format"] = "{tracknumber:02}. {artist} - {title}"

    # Disable update checks
    if "misc" in doc:
        doc["misc"]["check_for_updates"] = False

    if "cli" in doc:
        doc["cli"]["max_search_results"] = 100

    _CONFIG_PATH.write_text(tomlkit.dumps(doc))
    logger.info("Patched streamrip config at %s", _CONFIG_PATH)


def _detect_available_services():
    """Detect which streaming services have credentials configured."""
    global _available_services
    services = []
    if QOBUZ_EMAIL and QOBUZ_PASSWORD:
        services.append("qobuz")
    if DEEZER_ARL:
        services.append("deezer")
    if SOUNDCLOUD_CLIENT_ID:
        services.append("soundcloud")
    # Tidal requires OAuth flow, detect from config if tokens exist
    if _CONFIG_PATH.exists():
        try:
            doc = tomlkit.parse(_CONFIG_PATH.read_text())
            if doc.get("tidal", {}).get("access_token"):
                services.append("tidal")
        except Exception:
            pass
    _available_services = services
    logger.info("Available services: %s", services or ["none"])


# ---------------------------------------------------------------------------
# streamrip Python API helpers
# ---------------------------------------------------------------------------

async def _get_main():
    """Create a configured streamrip Main instance."""
    from streamrip.config import Config
    from streamrip.rip.main import Main

    config = Config(_CONFIG_PATH)
    cfg_data = config.__enter__()
    main = Main(cfg_data)
    await main.__aenter__()
    return main, config


async def _close_main(main, config):
    """Clean up a Main instance."""
    try:
        await main.__aexit__(None, None, None)
    except Exception:
        pass
    try:
        config.__exit__(None, None, None)
    except Exception:
        pass


async def _do_search(service: str, query: str, limit: int) -> List[Dict[str, Any]]:
    """Search using streamrip's Python API directly."""
    main = None
    config = None
    try:
        main, config = await _get_main()
        client = await main.get_logged_in_client(service)

        # client.search() returns raw API response pages
        pages = await client.search("track", query, limit=limit)

        results = []
        for page in pages:
            items = _extract_tracks_from_page(page, service)
            results.extend(items)
            if len(results) >= limit:
                break

        return results[:limit]

    except Exception as exc:
        logger.error("Search failed for %r on %s: %s", query, service, exc, exc_info=True)
        return []
    finally:
        if main and config:
            await _close_main(main, config)


def _extract_tracks_from_page(page: Any, service: str) -> List[Dict[str, Any]]:
    """Extract track data from a streamrip search result page.

    The page format varies by service. For Qobuz it's the raw API JSON.
    """
    results = []

    # Handle different page formats
    if isinstance(page, dict):
        # Qobuz returns {"tracks": {"items": [...]}}
        items = []
        if "tracks" in page:
            tracks = page["tracks"]
            items = tracks.get("items", []) if isinstance(tracks, dict) else []
        elif "items" in page:
            items = page["items"]
        elif "data" in page:
            items = page["data"]
        else:
            # Might be a single track result
            if "id" in page and "title" in page:
                items = [page]

        for item in items:
            track = _normalize_track(item, service)
            if track:
                results.append(track)

    elif isinstance(page, list):
        for item in page:
            track = _normalize_track(item, service)
            if track:
                results.append(track)

    return results


def _normalize_track(item: dict, service: str) -> Optional[Dict[str, Any]]:
    """Normalize a raw track dict from any service into our SearchResult shape."""
    if not isinstance(item, dict):
        return None

    track_id = str(item.get("id", ""))
    title = item.get("title", "") or item.get("name", "")
    if not title:
        return None

    # Artist extraction (varies by service)
    artist = ""
    artists_list = []
    if "performer" in item and isinstance(item["performer"], dict):
        # Qobuz format
        artist = item["performer"].get("name", "")
    elif "artist" in item:
        a = item["artist"]
        artist = a.get("name", "") if isinstance(a, dict) else str(a)
    elif "artists" in item:
        arts = item["artists"]
        if isinstance(arts, list) and arts:
            artist = arts[0].get("name", "") if isinstance(arts[0], dict) else str(arts[0])
            artists_list = [a.get("name", "") if isinstance(a, dict) else str(a) for a in arts]

    if not artists_list:
        artists_list = [artist] if artist else []

    # Album
    album = ""
    album_obj = item.get("album", {})
    if isinstance(album_obj, dict):
        album = album_obj.get("title", "") or album_obj.get("name", "")
    elif isinstance(album_obj, str):
        album = album_obj

    # Cover art
    cover_url = ""
    if isinstance(album_obj, dict):
        image = album_obj.get("image", {})
        if isinstance(image, dict):
            cover_url = (
                image.get("large", "")
                or image.get("small", "")
                or image.get("thumbnail", "")
            )
    if not cover_url and isinstance(item.get("image"), dict):
        image = item["image"]
        cover_url = image.get("large", "") or image.get("small", "")

    # Duration
    duration = item.get("duration")

    return {
        "service_id": track_id,
        "service": service,
        "title": title,
        "artist": artist,
        "artists": artists_list,
        "album": album,
        "duration": duration,
        "cover_url": cover_url,
        "quality": _quality_label(),
        "spotify_id": track_id,
        "url": "",
    }


def _quality_label() -> str:
    labels = {
        0: "MP3 128kbps",
        1: "MP3 320kbps",
        2: "CD 16bit/44.1kHz",
        3: "Hi-Res 24bit/96kHz",
        4: "Hi-Res 24bit/192kHz",
    }
    return labels.get(DOWNLOAD_QUALITY, f"Quality {DOWNLOAD_QUALITY}")


# ---------------------------------------------------------------------------
# Download
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
    service: str = ""
    service_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    file_path: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


_tasks: Dict[str, TaskState] = {}

# Global lock to serialize streamrip operations (it's not concurrency-safe)
_streamrip_lock = asyncio.Lock()


async def _do_download(task_id: str, service: str, service_id: str,
                       artist: str = "", title: str = "") -> None:
    """Download a track using streamrip's Python API."""
    task = _tasks.get(task_id)
    if not task:
        return

    main = None
    config = None

    try:
        task.status = TaskStatus.downloading
        task.updated_at = time.time()

        async with _streamrip_lock:
            main, config = await _get_main()

            # Build a URL for direct download if we have a numeric service ID
            url = _build_service_url(service, service_id)

            if url:
                # Direct URL download
                logger.info("Task %s: downloading URL %s", task_id, url)
                await main.handle_urls(url)
            else:
                # Search-based download
                search_query = f"{artist} {title}".strip() if artist and title else service_id
                if not search_query:
                    task.status = TaskStatus.error
                    task.error = "No track ID or artist/title provided"
                    task.updated_at = time.time()
                    return

                download_service = service if service in _available_services else DEFAULT_SERVICE
                logger.info("Task %s: search download %r on %s", task_id, search_query, download_service)
                await main.search_take_first(download_service, "track", search_query)

        task.status = TaskStatus.complete
        task.progress = 100.0
        task.updated_at = time.time()

    except Exception as exc:
        logger.exception("Download failed for task %s: %s", task_id, exc)
        task.status = TaskStatus.error
        task.error = str(exc)[:1024]
        task.updated_at = time.time()
    finally:
        if main and config:
            await _close_main(main, config)


def _build_service_url(service: str, service_id: str) -> str | None:
    if not service_id or not service_id.isdigit():
        return None
    urls = {
        "qobuz": f"https://www.qobuz.com/track/{service_id}",
        "tidal": f"https://tidal.com/browse/track/{service_id}",
        "deezer": f"https://www.deezer.com/track/{service_id}",
    }
    return urls.get(service)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    service_id: str
    service: str
    title: str
    artist: str
    artists: List[str]
    album: Optional[str] = None
    duration: Optional[int] = None
    cover_url: Optional[str] = None
    quality: Optional[str] = None
    spotify_id: str = ""
    url: str = ""


class DownloadRequestBody(BaseModel):
    service_id: str = ""
    service: Optional[str] = None
    spotify_id: Optional[str] = None
    artist: Optional[str] = None
    title: Optional[str] = None


class DownloadResponse(BaseModel):
    task_id: str
    status: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    active_tasks = sum(
        1 for t in _tasks.values()
        if t.status in (TaskStatus.queued, TaskStatus.downloading)
    )
    return {
        "status": "ok",
        "service": "streamrip-api",
        "available_services": _available_services,
        "default_service": DEFAULT_SERVICE,
        "download_quality": DOWNLOAD_QUALITY,
        "download_codec": DOWNLOAD_CODEC,
        "active_tasks": active_tasks,
    }


@app.get("/search", response_model=List[SearchResult])
async def search(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    service: str = Query(None, description="qobuz, tidal, deezer, soundcloud"),
):
    """Search a streaming service for tracks."""
    svc = (service or DEFAULT_SERVICE).lower()
    if svc not in ("qobuz", "tidal", "deezer", "soundcloud"):
        raise HTTPException(400, f"Unknown service '{svc}'")
    if svc not in _available_services:
        raise HTTPException(503, f"Service '{svc}' not configured")

    async with _streamrip_lock:
        results = await _do_search(svc, q, limit)

    return results


@app.post("/download", response_model=DownloadResponse)
async def download(body: DownloadRequestBody):
    """Trigger a track download. Returns immediately with a task_id."""
    track_id = body.service_id or body.spotify_id or ""
    if not track_id and not (body.artist and body.title):
        raise HTTPException(400, "Provide service_id, or artist + title")

    svc = (body.service or DEFAULT_SERVICE).lower()

    now = time.time()
    task_id = uuid.uuid4().hex[:16]
    task = TaskState(
        task_id=task_id,
        status=TaskStatus.queued,
        service=svc,
        service_id=track_id,
        artist=body.artist or "",
        title=body.title or "",
        created_at=now,
        updated_at=now,
    )
    _tasks[task_id] = task

    # Run download in background
    asyncio.create_task(
        _do_download(task_id, svc, track_id, body.artist or "", body.title or "")
    )

    return DownloadResponse(task_id=task_id, status=task.status.value)


@app.get("/status/{task_id}")
async def get_status(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task.model_dump()


@app.get("/tasks")
async def list_tasks(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
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
        "streamrip-api starting: quality=%d, codec=%s, output=%s",
        DOWNLOAD_QUALITY, DOWNLOAD_CODEC, OUTPUT_DIR,
    )
    _write_config()
    _detect_available_services()

    if not _available_services:
        logger.warning(
            "No streaming service credentials configured. "
            "Set QOBUZ_EMAIL/QOBUZ_PASSWORD, DEEZER_ARL, or SOUNDCLOUD_CLIENT_ID."
        )

    # Verify streamrip can load the config
    try:
        main, config = await _get_main()
        await _close_main(main, config)
        logger.info("streamrip config loaded and validated successfully")
    except Exception as exc:
        logger.error("streamrip config validation failed: %s", exc)


@app.on_event("shutdown")
async def shutdown():
    logger.info("streamrip-api shutting down")
