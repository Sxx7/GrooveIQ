"""
streamrip-api — Thin REST wrapper around streamrip.

Provides search, download, and status endpoints so GrooveIQ (or any
HTTP client) can trigger high-quality downloads from Qobuz, Tidal,
Deezer, or SoundCloud without embedding streamrip as a direct dependency.

All search and download operations go through streamrip's `rip` CLI,
which handles authentication with the configured streaming service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
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

_executor = ThreadPoolExecutor(max_workers=MAX_THREADS)

# ---------------------------------------------------------------------------
# streamrip config management
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "streamrip"
_CONFIG_PATH = _CONFIG_DIR / "config.toml"

_available_services: list[str] = []

# Env dict for all rip subprocess calls
_RIP_ENV: dict[str, str] = {}


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
verify_ssl = true

[qobuz]
use_auth_token = false
email_or_userid = "{QOBUZ_EMAIL}"
password_or_token = "{QOBUZ_PASSWORD}"
app_id = ""
quality = {DOWNLOAD_QUALITY}
download_booklets = false
secrets = []

[tidal]
user_id = ""
country_code = ""
access_token = ""
refresh_token = ""
token_expiry = ""
quality = {min(DOWNLOAD_QUALITY, 3)}
download_videos = false

[deezer]
arl = "{DEEZER_ARL}"
quality = {min(DOWNLOAD_QUALITY, 2)}
use_deezloader = true
deezloader_warnings = true

[soundcloud]
client_id = "{SOUNDCLOUD_CLIENT_ID}"
app_version = ""
quality = 0

[youtube]
quality = 0
download_videos = false
video_downloads_folder = ""

[database]
downloads_enabled = true
downloads_path = "/config/streamrip/downloads.db"
failed_downloads_enabled = true
failed_downloads_path = "/config/streamrip/failed_downloads.db"

[conversion]
enabled = false
codec = "ALAC"
sampling_rate = 48000
bit_depth = 24
lossy_bitrate = 320

[qobuz_filters]
extras = false
repeats = false
non_albums = false
features = false
non_studio_albums = false
non_remaster = false

[artwork]
embed = true
embed_size = "large"
embed_max_width = -1
save_artwork = true
saved_max_width = -1

[metadata]
set_playlist_to_album = true
renumber_playlist_tracks = true
exclude = []

[filepaths]
add_singles_to_folder = false
folder_format = "{{albumartist}} - {{title}} ({{year}}) [{{container}}] [{{bit_depth}}B-{{sampling_rate}}kHz]"
track_format = "{{tracknumber:02}}. {{artist}} - {{title}}{{explicit}}"
restrict_characters = false
truncate_to = 120

[lastfm]
source = "qobuz"
fallback_source = ""

[cli]
text_output = true
progress_bars = true
max_search_results = 100

[misc]
version = "2.0.6"
check_for_updates = false
"""
    _CONFIG_PATH.write_text(config.strip())
    logger.info("Wrote streamrip config to %s", _CONFIG_PATH)


def _detect_available_services():
    """Detect which streaming services have credentials configured."""
    global _available_services, _RIP_ENV
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
    _RIP_ENV = {**os.environ, "XDG_CONFIG_HOME": str(_CONFIG_DIR.parent)}
    logger.info("Available services: %s", services or ["none"])


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
    spotify_id: str = ""
    url: str = ""


class DownloadRequest(BaseModel):
    service_id: str = ""
    service: Optional[str] = None
    spotify_id: Optional[str] = None
    artist: Optional[str] = None
    title: Optional[str] = None


class DownloadResponse(BaseModel):
    task_id: str
    status: str


# ---------------------------------------------------------------------------
# Search via `rip search`
# ---------------------------------------------------------------------------

def _do_search(service: str, query: str, limit: int) -> List[Dict[str, Any]]:
    """Search using streamrip's `rip search` CLI.

    streamrip handles all authentication via config.toml — no need
    for manual API calls or app_ids.
    """
    try:
        # rip search <service> track <query>
        # The CLI outputs interactive results; we capture stdout.
        proc = subprocess.run(
            ["rip", "search", service, "track", query],
            capture_output=True,
            text=True,
            timeout=30,
            env=_RIP_ENV,
            input="1\n",  # auto-select first result group if prompted
        )

        logger.debug(
            "rip search %s track %r → rc=%d stdout=%d bytes stderr=%d bytes",
            service, query, proc.returncode,
            len(proc.stdout), len(proc.stderr),
        )

        if proc.returncode != 0 and not proc.stdout.strip():
            logger.warning(
                "rip search failed (rc=%d): %s",
                proc.returncode,
                (proc.stderr or "")[:500],
            )
            return []

        return _parse_rip_search_output(proc.stdout, service, limit)

    except subprocess.TimeoutExpired:
        logger.warning("rip search timed out for %r on %s", query, service)
        return []
    except FileNotFoundError:
        logger.error("rip CLI not found in PATH")
        return []
    except Exception as exc:
        logger.error("rip search error on %s for %r: %s", service, query, exc)
        return []


def _parse_rip_search_output(
    stdout: str, service: str, limit: int,
) -> List[Dict[str, Any]]:
    """Parse the output of `rip search <service> track <query>`.

    streamrip outputs results in a numbered list format like:
        1. Artist - Title (Album)
    or sometimes with extra metadata. The exact format varies by
    version and service, so we're flexible in parsing.
    """
    results = []
    lines = stdout.strip().split("\n")

    # Pattern: "N. Artist - Title" or "N) Artist - Title"
    numbered_re = re.compile(
        r"^\s*(\d+)[.\)]\s+(.+?)\s*[-–—]\s*(.+?)(?:\s*\(([^)]+)\))?\s*$"
    )
    # Simpler pattern: just "Artist - Title"
    simple_re = re.compile(
        r"^\s*(.+?)\s*[-–—]\s*(.+?)(?:\s*\(([^)]+)\))?\s*$"
    )

    for line in lines:
        line = line.strip()
        if not line:
            continue

        m = numbered_re.match(line)
        if m:
            idx, artist, title, album = m.group(1), m.group(2), m.group(3), m.group(4)
            results.append({
                "service_id": f"{service}_{idx}",
                "service": service,
                "title": title.strip(),
                "artist": artist.strip(),
                "artists": [artist.strip()],
                "album": (album or "").strip(),
                "cover_url": "",
                "quality": _quality_label(),
                "spotify_id": f"{service}_{idx}",
                "url": "",
            })
            if len(results) >= limit:
                break
            continue

        # Skip header/separator lines
        if line.startswith(("=", "-", "Search", "Found", "Select")):
            continue

        m = simple_re.match(line)
        if m:
            artist, title, album = m.group(1), m.group(2), m.group(3)
            idx = len(results) + 1
            results.append({
                "service_id": f"{service}_{idx}",
                "service": service,
                "title": title.strip(),
                "artist": artist.strip(),
                "artists": [artist.strip()],
                "album": (album or "").strip(),
                "cover_url": "",
                "quality": _quality_label(),
                "spotify_id": f"{service}_{idx}",
                "url": "",
            })
            if len(results) >= limit:
                break

    return results


def _quality_label() -> str:
    """Human-readable quality label from DOWNLOAD_QUALITY setting."""
    labels = {
        0: "MP3 128kbps",
        1: "MP3 320kbps",
        2: "CD 16bit/44.1kHz",
        3: "Hi-Res 24bit/96kHz",
        4: "Hi-Res 24bit/192kHz",
    }
    return labels.get(DOWNLOAD_QUALITY, f"Quality {DOWNLOAD_QUALITY}")


# ---------------------------------------------------------------------------
# Download via `rip search --first` or `rip url`
# ---------------------------------------------------------------------------

def _build_service_url(service: str, service_id: str) -> str | None:
    """Build the streaming service URL from a service name and track ID."""
    if not service_id or not service_id.isdigit():
        return None
    urls = {
        "qobuz": f"https://www.qobuz.com/track/{service_id}",
        "tidal": f"https://tidal.com/browse/track/{service_id}",
        "deezer": f"https://www.deezer.com/track/{service_id}",
    }
    return urls.get(service)


def _do_download(task_id: str, service: str, service_id: str,
                 artist: str = "", title: str = "") -> None:
    """Run streamrip download (blocking). Called in thread pool.

    Strategy:
    1. If we have a numeric service_id matching the configured service,
       try `rip url <service_url>` (direct, fastest).
    2. Otherwise use `rip search <service> track "artist title" --first`
       which searches and auto-downloads the top result.
    """
    task = _tasks.get(task_id)
    if not task:
        return

    try:
        task.status = TaskStatus.downloading
        task.updated_at = time.time()

        # Try direct URL download if we have a native service ID
        url = _build_service_url(service, service_id)

        if url:
            cmd = ["rip", "url", url]
        else:
            # Search-based download: use artist + title, or service_id as query
            search_query = f"{artist} {title}".strip() if artist and title else service_id
            if not search_query:
                task.status = TaskStatus.error
                task.error = "No track ID or artist/title provided"
                task.updated_at = time.time()
                return

            download_service = service if service in _available_services else DEFAULT_SERVICE
            cmd = ["rip", "search", download_service, "track", search_query, "--first"]

        logger.info("Task %s: running %s", task_id, " ".join(cmd))

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=OUTPUT_DIR,
            env=_RIP_ENV,
        )

        stdout = proc.stdout.strip() if proc.stdout else ""
        stderr = proc.stderr.strip() if proc.stderr else ""

        logger.debug("Task %s: rc=%d stdout=%s stderr=%s",
                      task_id, proc.returncode, stdout[:200], stderr[:200])

        if proc.returncode == 0:
            task.status = TaskStatus.complete
            task.progress = 100.0
            # Try to extract file path from output
            for line in stdout.split("\n"):
                if ".flac" in line.lower() or ".mp3" in line.lower() or ".opus" in line.lower():
                    task.file_path = line.strip()
                    break
        else:
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


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
    """Search a streaming service for tracks via `rip search`."""
    svc = (service or DEFAULT_SERVICE).lower()
    if svc not in ("qobuz", "tidal", "deezer", "soundcloud"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown service '{svc}'. Use: qobuz, tidal, deezer, soundcloud",
        )
    if svc not in _available_services:
        raise HTTPException(
            status_code=503,
            detail=f"Service '{svc}' not configured. Set credentials via env vars.",
        )

    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            _executor, _do_search, svc, q, limit
        )
    except Exception as exc:
        logger.error("Search failed for %r on %s: %s", q, svc, exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return results


@app.post("/download", response_model=DownloadResponse)
async def download(body: DownloadRequest):
    """Trigger a track download. Returns immediately with a task_id.

    Pass artist + title for search-based download (recommended), or a
    native service_id for direct URL download.
    """
    track_id = body.service_id or body.spotify_id or ""
    if not track_id and not (body.artist and body.title):
        raise HTTPException(
            status_code=400,
            detail="Provide service_id, or artist + title for search-based download",
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

    # Verify rip CLI is available and get version
    try:
        proc = subprocess.run(
            ["rip", "--version"], capture_output=True, text=True, timeout=10,
        )
        rip_version = proc.stdout.strip()
        logger.info("streamrip CLI: %s", rip_version or "available")

        # Run a harmless command to trigger any config auto-update prompt
        # (streamrip may prompt "Need to update config" interactively)
        subprocess.run(
            ["rip", "config", "path"],
            capture_output=True, text=True, timeout=10,
            input="y\n", env=_RIP_ENV,
        )
    except FileNotFoundError:
        logger.error("rip CLI not found! pip install streamrip may have failed.")


@app.on_event("shutdown")
async def shutdown():
    _executor.shutdown(wait=False)
    logger.info("streamrip-api shutting down")
