"""
streamrip-api — Thin REST wrapper around streamrip.

Provides search, download, and status endpoints so GrooveIQ (or any
HTTP client) can trigger high-quality downloads from Qobuz, Tidal,
Deezer, or SoundCloud without embedding streamrip as a direct dependency.

Downloads come from real streaming services in lossless/hi-res quality.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/music")
DOWNLOAD_QUALITY = int(os.environ.get("DOWNLOAD_QUALITY", "3"))
# Quality levels: 0=128kbps, 1=320kbps, 2=16bit/44.1k, 3=24bit/96k, 4=24bit/192k
DOWNLOAD_CODEC = os.environ.get("DOWNLOAD_CODEC", "FLAC")
MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "6"))
MAX_THREADS = int(os.environ.get("MAX_THREADS", "4"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Service credentials (at least one service required)
QOBUZ_EMAIL = os.environ.get("QOBUZ_EMAIL", "")
QOBUZ_PASSWORD = os.environ.get("QOBUZ_PASSWORD", "")
TIDAL_EMAIL = os.environ.get("TIDAL_EMAIL", "")
TIDAL_PASSWORD = os.environ.get("TIDAL_PASSWORD", "")
DEEZER_ARL = os.environ.get("DEEZER_ARL", "")
SOUNDCLOUD_CLIENT_ID = os.environ.get("SOUNDCLOUD_CLIENT_ID", "")

# Default service for search/download when not specified
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

# Thread pool for blocking streamrip calls
_executor = ThreadPoolExecutor(max_workers=MAX_THREADS)


# ---------------------------------------------------------------------------
# streamrip config management
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "streamrip"
_CONFIG_PATH = _CONFIG_DIR / "config.toml"

_available_services: list[str] = []


def _write_config():
    """Generate streamrip config.toml from environment variables."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    config = f"""
[downloads]
folder = "{OUTPUT_DIR}"
source_subdirectories = false
disc_subdirectories = true
concurrency = true
max_connections = {MAX_CONNECTIONS}
requests_per_minute = 60

[downloads.artwork]
embed = true
embed_max_width = 1200
save_artwork = true
saved_max_width = 1200

[qobuz]
enabled = {str(bool(QOBUZ_EMAIL and QOBUZ_PASSWORD)).lower()}
email = "{QOBUZ_EMAIL}"
password = "{QOBUZ_PASSWORD}"
quality = {DOWNLOAD_QUALITY}
download_booklets = false

[tidal]
enabled = {str(bool(TIDAL_EMAIL and TIDAL_PASSWORD)).lower()}
email = "{TIDAL_EMAIL}"
password = "{TIDAL_PASSWORD}"
quality = {min(DOWNLOAD_QUALITY, 3)}

[deezer]
enabled = {str(bool(DEEZER_ARL)).lower()}
arl = "{DEEZER_ARL}"
quality = {min(DOWNLOAD_QUALITY, 2)}
use_deezloader = true

[soundcloud]
enabled = {str(bool(SOUNDCLOUD_CLIENT_ID)).lower()}
client_id = "{SOUNDCLOUD_CLIENT_ID}"
quality = 0

[metadata]
set_playlist_to_album = true
exclude = []

[filepaths]
add_singles_to_folder = false
folder_format = "{{albumartist}}/{{album}} ({{year}})"
track_format = "{{tracknumber}}. {{artist}} - {{title}}"
restrict_characters = false
truncate_to = 120

[misc]
version = "2.0.5"
check_for_updates = false
"""
    _CONFIG_PATH.write_text(config.strip())
    logger.info("Wrote streamrip config to %s", _CONFIG_PATH)


def _detect_available_services():
    """Detect which streaming services have credentials configured."""
    global _available_services
    services = []
    if QOBUZ_EMAIL and QOBUZ_PASSWORD:
        services.append("qobuz")
    if TIDAL_EMAIL and TIDAL_PASSWORD:
        services.append("tidal")
    if DEEZER_ARL:
        services.append("deezer")
    if SOUNDCLOUD_CLIENT_ID:
        services.append("soundcloud")
    _available_services = services
    logger.info("Available services: %s", services or ["none"])


# ---------------------------------------------------------------------------
# streamrip session management
# ---------------------------------------------------------------------------

_rip_config = None
_rip_lock = asyncio.Lock()


async def _get_config():
    """Lazy-load the streamrip Config object."""
    global _rip_config
    if _rip_config is None:
        async with _rip_lock:
            if _rip_config is None:
                _write_config()
                try:
                    from streamrip.config import Config
                    _rip_config = Config.defaults()
                    # Override paths
                    _rip_config.session.downloads.folder = OUTPUT_DIR
                    logger.info("streamrip config loaded")
                except Exception as exc:
                    logger.error("Failed to load streamrip config: %s", exc)
                    raise
    return _rip_config


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
    service: str = ""
    service_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    file_path: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


# task_id -> TaskState
_tasks: Dict[str, TaskState] = {}


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
    # Compatibility fields for GrooveIQ (maps to Spotify-like shape)
    spotify_id: str = ""
    url: str = ""


class DownloadRequest(BaseModel):
    service_id: str = ""
    service: Optional[str] = None  # qobuz, tidal, deezer, soundcloud
    # Compatibility: accept spotify_id as alias
    spotify_id: Optional[str] = None
    # For cross-service downloads (e.g. Deezer search → Qobuz download)
    artist: Optional[str] = None
    title: Optional[str] = None


class DownloadResponse(BaseModel):
    task_id: str
    status: str


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------


def _do_search_cli(service: str, query: str, limit: int) -> List[Dict[str, Any]]:
    """Search using streamrip's CLI interface via subprocess.

    streamrip v2 doesn't expose a clean search API, so we use the CLI
    and parse results. Falls back to a simpler approach if needed.
    """
    import json
    import subprocess

    results = []

    try:
        # Use streamrip's Python API if available
        proc = subprocess.run(
            [
                "rip", "search", service, "track", query,
                "--num-results", str(limit),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "XDG_CONFIG_HOME": str(_CONFIG_DIR.parent),
            },
        )

        if proc.returncode != 0:
            logger.warning(
                "streamrip search failed (rc=%d): %s",
                proc.returncode,
                proc.stderr[:500],
            )
            # Try the Python API directly as fallback
            return _do_search_api(service, query, limit)

        # Parse the CLI output — streamrip outputs numbered results
        # Format varies by version; try to extract what we can
        lines = proc.stdout.strip().split("\n")
        for line in lines:
            line = line.strip()
            if not line or line.startswith("=") or line.startswith("-"):
                continue
            # Basic parsing of "N. Artist - Title"
            results.append({
                "title": line,
                "artist": "",
                "service": service,
            })

    except subprocess.TimeoutExpired:
        logger.warning("streamrip search timed out for %r on %s", query, service)
        return _do_search_api(service, query, limit)
    except FileNotFoundError:
        logger.warning("rip CLI not found, falling back to API search")
        return _do_search_api(service, query, limit)

    return results[:limit]


def _do_search_api(service: str, query: str, limit: int) -> List[Dict[str, Any]]:
    """Search using streaming service APIs directly.

    Supports Qobuz, Tidal, Deezer, SoundCloud.
    Falls back to Deezer (free public API) if the primary service's
    search fails (e.g. Qobuz 401, Tidal token expired).
    """
    results = []

    try:
        if service == "qobuz":
            results = _search_qobuz(query, limit)
        elif service == "tidal":
            results = _search_tidal(query, limit)
        elif service == "deezer":
            results = _search_deezer(query, limit)
        elif service == "soundcloud":
            results = _search_soundcloud(query, limit)
        else:
            logger.warning("Unknown service for search: %s", service)
    except Exception as exc:
        logger.error("API search error on %s for %r: %s", service, query, exc, exc_info=True)

    # Deezer fallback: if the primary service returned nothing and we
    # didn't already try Deezer, use Deezer's free public API for search.
    # The download still goes through the configured service (e.g. Qobuz).
    if not results and service != "deezer":
        logger.info(
            "No results from %s search, falling back to Deezer for %r",
            service, query,
        )
        try:
            results = _search_deezer(query, limit)
        except Exception as exc:
            logger.error("Deezer fallback search error for %r: %s", query, exc)

    return results


def _search_qobuz(query: str, limit: int) -> List[Dict[str, Any]]:
    """Search Qobuz via their public API."""
    import urllib.request
    import json

    app_id = _get_qobuz_app_id()
    if not app_id:
        logger.warning("Qobuz app_id not available for search")
        return []

    url = (
        f"https://www.qobuz.com/api.json/0.2/track/search"
        f"?query={urllib.parse.quote(query)}&limit={limit}&app_id={app_id}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "streamrip-api/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("Qobuz search HTTP error: %s", exc)
        return []

    results = []
    for track in (data.get("tracks", {}).get("items", []))[:limit]:
        album_data = track.get("album", {})
        artist_data = album_data.get("artist", track.get("performer", {}))
        image = album_data.get("image", {})
        cover = image.get("large") or image.get("small") or ""

        # Determine quality string
        hires = track.get("hires_streamable", False)
        quality_str = "Hi-Res 24bit" if hires else "CD 16bit/44.1kHz"

        results.append({
            "service_id": str(track.get("id", "")),
            "service": "qobuz",
            "title": track.get("title", ""),
            "artist": artist_data.get("name", ""),
            "artists": [artist_data.get("name", "")],
            "album": album_data.get("title", ""),
            "duration": track.get("duration"),
            "cover_url": cover,
            "quality": quality_str,
        })
    return results


def _search_tidal(query: str, limit: int) -> List[Dict[str, Any]]:
    """Search Tidal via their API."""
    import urllib.request
    import json

    # Tidal's public API endpoint for search
    url = (
        f"https://api.tidal.com/v1/search/tracks"
        f"?query={urllib.parse.quote(query)}&limit={limit}&countryCode=US"
    )
    headers = {
        "User-Agent": "streamrip-api/1.0",
        "X-Tidal-Token": "CzET4vdadNUFQ5JU",  # Public web token
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("Tidal search HTTP error: %s", exc)
        return []

    results = []
    for track in (data.get("items", []))[:limit]:
        artists = track.get("artists", [])
        artist_names = [a.get("name", "") for a in artists]
        album_data = track.get("album", {})
        cover_id = album_data.get("cover", "")
        cover_url = (
            f"https://resources.tidal.com/images/{cover_id.replace('-', '/')}/640x640.jpg"
            if cover_id else ""
        )

        quality_str = track.get("audioQuality", "LOSSLESS")

        results.append({
            "service_id": str(track.get("id", "")),
            "service": "tidal",
            "title": track.get("title", ""),
            "artist": artist_names[0] if artist_names else "",
            "artists": artist_names,
            "album": album_data.get("title", ""),
            "duration": track.get("duration"),
            "cover_url": cover_url,
            "quality": quality_str,
        })
    return results


def _search_deezer(query: str, limit: int) -> List[Dict[str, Any]]:
    """Search Deezer via their free public API."""
    import urllib.request
    import json

    url = f"https://api.deezer.com/search/track?q={urllib.parse.quote(query)}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "streamrip-api/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("Deezer search HTTP error: %s", exc)
        return []

    results = []
    for track in (data.get("data", []))[:limit]:
        artist_data = track.get("artist", {})
        album_data = track.get("album", {})

        results.append({
            "service_id": str(track.get("id", "")),
            "service": "deezer",
            "title": track.get("title", ""),
            "artist": artist_data.get("name", ""),
            "artists": [artist_data.get("name", "")],
            "album": album_data.get("title", ""),
            "duration": track.get("duration"),
            "cover_url": album_data.get("cover_big") or album_data.get("cover_medium") or "",
            "quality": "FLAC" if DOWNLOAD_QUALITY >= 2 else "320kbps",
        })
    return results


def _search_soundcloud(query: str, limit: int) -> List[Dict[str, Any]]:
    """Search SoundCloud via their API."""
    import urllib.request
    import json

    client_id = SOUNDCLOUD_CLIENT_ID
    if not client_id:
        return []

    url = (
        f"https://api-v2.soundcloud.com/search/tracks"
        f"?q={urllib.parse.quote(query)}&limit={limit}&client_id={client_id}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "streamrip-api/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("SoundCloud search HTTP error: %s", exc)
        return []

    results = []
    for track in (data.get("collection", []))[:limit]:
        user = track.get("user", {})
        artwork = track.get("artwork_url") or ""
        # SoundCloud artwork: replace -large with -t500x500 for higher res
        if artwork:
            artwork = artwork.replace("-large", "-t500x500")

        results.append({
            "service_id": str(track.get("id", "")),
            "service": "soundcloud",
            "title": track.get("title", ""),
            "artist": user.get("username", ""),
            "artists": [user.get("username", "")],
            "album": "",
            "duration": (track.get("duration") or 0) // 1000,
            "cover_url": artwork,
            "quality": "MP3 128kbps",
        })
    return results


def _get_qobuz_app_id() -> str:
    """Extract Qobuz app_id from streamrip config or use a known one."""
    # streamrip embeds app IDs; try to read from config
    try:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "rb") as f:
                cfg = tomllib.load(f)
            app_id = cfg.get("qobuz", {}).get("app_id", "")
            if app_id:
                return str(app_id)
    except Exception:
        pass
    # Fallback: well-known Qobuz web app ID
    return "950096963"


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


def _do_download(task_id: str, service: str, service_id: str,
                  artist: str = "", title: str = "") -> None:
    """Run streamrip download (blocking). Called in thread pool.

    Uses the `rip url` CLI command to download a track by its service URL.
    When the service_id is from a different service (e.g. Deezer ID but
    downloading via Qobuz), falls back to `rip search` by artist + title.
    """
    import subprocess

    task = _tasks.get(task_id)
    if not task:
        return

    rip_env = {**os.environ, "XDG_CONFIG_HOME": str(_CONFIG_DIR.parent)}

    try:
        task.status = TaskStatus.downloading
        task.updated_at = time.time()

        # Build the service URL from the ID
        url = _build_service_url(service, service_id)

        if url:
            # Direct download by URL
            cmd = ["rip", "url", url]
        elif artist and title:
            # Cross-service fallback: search + download via the configured service
            # e.g. Deezer search result → download from Qobuz by name
            download_service = DEFAULT_SERVICE
            search_query = f"{artist} {title}"
            logger.info(
                "Task %s: no direct URL for %s/%s, using 'rip search' on %s for %r",
                task_id, service, service_id, download_service, search_query,
            )
            cmd = ["rip", "search", download_service, "track", search_query, "--first"]
        else:
            task.status = TaskStatus.error
            task.error = f"Cannot build URL for {service} ID {service_id} and no artist/title for search fallback"
            task.updated_at = time.time()
            return

        logger.info("Task %s: running %s", task_id, " ".join(cmd))

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min timeout for downloads
            cwd=OUTPUT_DIR,
            env=rip_env,
        )

        if proc.returncode == 0:
            task.status = TaskStatus.complete
            task.progress = 100.0
            # Try to extract file path from output
            for line in proc.stdout.strip().split("\n"):
                if OUTPUT_DIR in line or ".flac" in line.lower() or ".mp3" in line.lower():
                    task.file_path = line.strip()
                    break
        else:
            stderr = proc.stderr.strip() if proc.stderr else ""
            stdout = proc.stdout.strip() if proc.stdout else ""
            error_msg = stderr or stdout or f"streamrip exited with code {proc.returncode}"
            task.status = TaskStatus.error
            task.error = error_msg[:1024]

        task.updated_at = time.time()

    except subprocess.TimeoutExpired:
        logger.error("Download timed out for task %s", task_id)
        task.status = TaskStatus.error
        task.error = "Download timed out after 10 minutes"
        task.updated_at = time.time()
    except Exception as exc:
        logger.exception("Download failed for task %s: %s", task_id, exc)
        task.status = TaskStatus.error
        task.error = str(exc)[:1024]
        task.updated_at = time.time()


def _build_service_url(service: str, service_id: str) -> str | None:
    """Build the streaming service URL from a service name and track ID."""
    urls = {
        "qobuz": f"https://www.qobuz.com/track/{service_id}",
        "tidal": f"https://tidal.com/browse/track/{service_id}",
        "deezer": f"https://www.deezer.com/track/{service_id}",
        "soundcloud": None,  # SoundCloud needs full URL, not just ID
    }
    return urls.get(service)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

import urllib.parse


@app.get("/health")
async def health():
    active_tasks = sum(
        1
        for t in _tasks.values()
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
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    service: str = Query(
        None,
        description="Streaming service: qobuz, tidal, deezer, soundcloud. Defaults to DEFAULT_SERVICE.",
    ),
):
    """Search a streaming service for tracks."""
    svc = (service or DEFAULT_SERVICE).lower()
    if svc not in ("qobuz", "tidal", "deezer", "soundcloud"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown service '{svc}'. Use: qobuz, tidal, deezer, soundcloud",
        )
    if svc not in _available_services and svc not in ("deezer",):
        # Deezer search works without credentials (free API)
        if svc != "deezer":
            raise HTTPException(
                status_code=503,
                detail=f"Service '{svc}' not configured. Set credentials via env vars.",
            )

    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            _executor, _do_search_api, svc, q, limit
        )
    except Exception as exc:
        logger.error("Search failed for %r on %s: %s", q, svc, exc)
        raise HTTPException(status_code=502, detail=str(exc))

    # Add compatibility fields
    for r in results:
        r.setdefault("spotify_id", r.get("service_id", ""))
        r.setdefault("url", "")

    return results


@app.post("/download", response_model=DownloadResponse)
async def download(body: DownloadRequest):
    """Trigger a track download. Returns immediately with a task_id.

    Accepts service_id (native ID for the service) or artist+title
    for cross-service downloads (e.g. Deezer search → Qobuz download).
    """
    # Resolve the ID — accept either service_id or spotify_id for compatibility
    track_id = body.service_id or body.spotify_id or ""
    if not track_id and not (body.artist and body.title):
        raise HTTPException(
            status_code=400,
            detail="service_id (or spotify_id) required, or artist + title for search-based download",
        )

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

    # Fire and forget — download runs in the thread pool
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        _executor, _do_download, task_id, svc, track_id,
        body.artist or "", body.title or "",
    )

    logger.info(
        "Download queued: task=%s service=%s id=%s artist=%r title=%r",
        task_id, svc, track_id, body.artist, body.title,
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
        "streamrip-api starting: quality=%d, codec=%s, output=%s, threads=%d",
        DOWNLOAD_QUALITY, DOWNLOAD_CODEC, OUTPUT_DIR, MAX_THREADS,
    )
    _detect_available_services()
    _write_config()

    if not _available_services:
        logger.warning(
            "No streaming service credentials configured. "
            "Set QOBUZ_EMAIL/QOBUZ_PASSWORD, TIDAL_EMAIL/TIDAL_PASSWORD, "
            "DEEZER_ARL, or SOUNDCLOUD_CLIENT_ID."
        )


@app.on_event("shutdown")
async def shutdown():
    _executor.shutdown(wait=False)
    logger.info("streamrip-api shutting down")
