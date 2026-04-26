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
# Qobuz removed plain email/password login. When true, QOBUZ_EMAIL is the numeric
# user_id and QOBUZ_PASSWORD is the auth token (extracted from a logged-in browser).
QOBUZ_USE_AUTH_TOKEN = os.environ.get("QOBUZ_USE_AUTH_TOKEN", "").strip().lower() in ("1", "true", "yes")
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
        doc["qobuz"]["use_auth_token"] = QOBUZ_USE_AUTH_TOKEN
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

    # Filepaths — always nest in the album folder, even for single-track downloads.
    # Without `add_singles_to_folder = true`, individual tracks land flat at /music
    # root because streamrip treats them as "singles" and skips the folder template.
    #
    # Streamrip's folder placeholders for the album-folder template are:
    #   albumartist, title (= album title), year, bit_depth, sampling_rate, id, albumcomposer
    # (NOT `album` — that key doesn't exist and raises KeyError at rip time).
    if "filepaths" in doc:
        doc["filepaths"]["add_singles_to_folder"] = True
        doc["filepaths"]["folder_format"] = "{albumartist}/{title} ({year})"
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


def _normalize_artist_release(alb: Any, release_type: str) -> Optional[Dict[str, Any]]:
    """Reshape one album-like object from artist/get or artist/getReleasesList
    into the schema the dashboard expects. ``release_type`` is "primary" for
    items returned by ``artist/get?extra=albums`` and "other" for items from
    ``release_type=other`` (covers, tributes, guest features).
    """
    if not isinstance(alb, dict):
        return None
    aid = str(alb.get("id", ""))
    if not aid:
        return None
    rel = alb.get("release_date_original") or alb.get("release_date") or ""
    year = int(rel[:4]) if isinstance(rel, str) and len(rel) >= 4 and rel[:4].isdigit() else None
    image = alb.get("image") or {}
    cover = (
        (image.get("large") if isinstance(image, dict) else None)
        or (image.get("small") if isinstance(image, dict) else None)
        or ""
    )
    # Qobuz inconsistency: on `artist/get` items, ``artist.name`` is a string.
    # On ``artist/getReleasesList`` items, ``artist.name`` is itself a dict
    # like ``{"display": "Gino Laurent"}``. Handle both.
    primary_artist = ""
    primary_artist_obj = alb.get("artist") or {}
    if isinstance(primary_artist_obj, dict):
        name_field = primary_artist_obj.get("name")
        if isinstance(name_field, dict):
            primary_artist = name_field.get("display") or name_field.get("name") or ""
        elif isinstance(name_field, str):
            primary_artist = name_field
        if not primary_artist:
            primary_artist = primary_artist_obj.get("display") or ""
    return {
        "album_id": aid,
        "title": alb.get("title") or "",
        "year": year,
        "cover_url": cover,
        "track_count": alb.get("tracks_count"),
        "duration": alb.get("duration"),
        "release_type": release_type,
        "primary_artist": primary_artist,
    }


async def _do_artist_search(
    service: str,
    query: str,
    artist_limit: int,
    albums_per_artist: int,
) -> Dict[str, Any]:
    """Search for artists, then expand each match into its discography.

    Returns ``{"artists": [...]}`` where each artist has a flat ``albums``
    array (id/title/year/cover/track_count/duration) but no tracks. Tracks
    are loaded lazily per-album via :func:`_do_album_tracks` so the response
    stays compact for prolific artists (Daft Punk has ~89 albums).
    """
    main = None
    config = None
    try:
        main, config = await _get_main()
        client = await main.get_logged_in_client(service)

        pages = await client.search("artist", query, limit=artist_limit)
        # Top-level shape: {"query":..., "artists": {"items": [...]}}
        artist_items: List[Dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            block = page.get("artists") or page
            items = block.get("items") if isinstance(block, dict) else block
            if isinstance(items, list):
                artist_items.extend(items)
            if len(artist_items) >= artist_limit:
                break
        artist_items = artist_items[:artist_limit]

        out_artists: List[Dict[str, Any]] = []
        for a in artist_items:
            artist_id = str(a.get("id", "")) if a.get("id") is not None else ""
            artist_name = a.get("name") or ""
            image_obj = a.get("image") or {}
            artist_image = (
                (image_obj.get("large") if isinstance(image_obj, dict) else None)
                or (image_obj.get("medium") if isinstance(image_obj, dict) else None)
                or a.get("picture")
                or ""
            )
            albums_total = a.get("albums_count")

            # Fetch the artist's discography in one call. Qobuz's artist/get
            # mixes the artist's own releases with cover/tribute releases
            # whose ``artist.id`` is some other artist; split them by id match.
            albums_out: List[Dict[str, Any]] = []
            others_out: List[Dict[str, Any]] = []
            primary_ids: set[str] = set()
            others_ids: set[str] = set()
            try:
                meta = await client.get_metadata(artist_id, "artist") if artist_id else None
            except Exception as exc:
                logger.warning("Artist metadata fetch failed for %s/%s: %s", service, artist_id, exc)
                meta = None
            if isinstance(meta, dict):
                raw_albums = meta.get("albums")
                if isinstance(raw_albums, dict):
                    raw_albums = raw_albums.get("items") or []
                if not isinstance(raw_albums, list):
                    raw_albums = []
                for alb in raw_albums[:albums_per_artist]:
                    item_artist = alb.get("artist") if isinstance(alb, dict) else None
                    item_artist_id = ""
                    if isinstance(item_artist, dict) and item_artist.get("id") is not None:
                        item_artist_id = str(item_artist["id"])
                    is_primary = bool(artist_id) and item_artist_id == artist_id
                    norm = _normalize_artist_release(alb, "primary" if is_primary else "other")
                    if not norm:
                        continue
                    if is_primary:
                        albums_out.append(norm)
                        primary_ids.add(norm["album_id"])
                    else:
                        others_out.append(norm)
                        others_ids.add(norm["album_id"])

            # Some artists (e.g. The Beatles) have additional appearances not
            # surfaced by artist/get; release_type=other backfills those.
            if artist_id and service == "qobuz":
                try:
                    qcfg = client.config.session.qobuz
                    status, resp = await client._api_request(
                        "artist/getReleasesList",
                        {
                            "app_id": str(qcfg.app_id),
                            "artist_id": artist_id,
                            "limit": 200,
                            "release_type": "other",
                        },
                    )
                    if status == 200:
                        for alb in resp.get("items", []) or []:
                            norm = _normalize_artist_release(alb, "other")
                            if not norm:
                                continue
                            if norm["album_id"] in primary_ids or norm["album_id"] in others_ids:
                                continue
                            others_out.append(norm)
                            others_ids.add(norm["album_id"])
                except Exception as exc:
                    logger.warning("Other-releases fetch failed for %s/%s: %s", service, artist_id, exc)

            out_artists.append({
                "artist_id": artist_id,
                "name": artist_name,
                "image_url": artist_image,
                "service": service,
                "albums_total": albums_total,
                "albums": albums_out,
                "other_releases": others_out,
            })

        return {"query": query, "service": service, "artists": out_artists}
    except Exception as exc:
        logger.error("Artist search failed for %r on %s: %s", query, service, exc, exc_info=True)
        return {"query": query, "service": service, "artists": [], "error": str(exc)}
    finally:
        if main and config:
            await _close_main(main, config)


async def _do_album_tracks(service: str, album_id: str) -> Dict[str, Any]:
    """Fetch full track list for an album. Used to lazy-load tracks when the
    user expands an album card on the artist-search results panel."""
    main = None
    config = None
    try:
        main, config = await _get_main()
        client = await main.get_logged_in_client(service)
        meta = await client.get_metadata(album_id, "album")
        if not isinstance(meta, dict):
            return {"service": service, "album_id": album_id, "tracks": [], "error": "no metadata"}

        rel = meta.get("release_date_original") or meta.get("release_date") or ""
        year = int(rel[:4]) if isinstance(rel, str) and len(rel) >= 4 and rel[:4].isdigit() else None
        image = meta.get("image") or {}
        cover = (
            (image.get("large") if isinstance(image, dict) else None)
            or (image.get("small") if isinstance(image, dict) else None)
            or ""
        )
        artist_obj = meta.get("artist") or {}
        artist_name = artist_obj.get("name") if isinstance(artist_obj, dict) else ""

        raw_tracks = meta.get("tracks")
        if isinstance(raw_tracks, dict):
            raw_tracks = raw_tracks.get("items") or []
        if not isinstance(raw_tracks, list):
            raw_tracks = []

        tracks_out: List[Dict[str, Any]] = []
        for t in raw_tracks:
            if not isinstance(t, dict):
                continue
            tn = t.get("track_number")
            if not isinstance(tn, int):
                tn = t.get("trackNumber") if isinstance(t.get("trackNumber"), int) else None
            duration = t.get("duration")
            tracks_out.append({
                "service_id": str(t.get("id", "")),
                "title": t.get("title") or "",
                "track_number": tn,
                "duration": duration,
                "duration_ms": (duration * 1000) if isinstance(duration, int) else None,
            })

        return {
            "service": service,
            "album_id": album_id,
            "album_title": meta.get("title") or "",
            "album_year": year,
            "artist": artist_name,
            "cover_url": cover,
            "tracks": tracks_out,
        }
    except Exception as exc:
        logger.error("Album tracks fetch failed for %s/%s: %s", service, album_id, exc, exc_info=True)
        return {"service": service, "album_id": album_id, "tracks": [], "error": str(exc)}
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
    album_id = ""
    album_year: Optional[int] = None
    album_track_count: Optional[int] = None
    album_obj = item.get("album", {})
    if isinstance(album_obj, dict):
        album = album_obj.get("title", "") or album_obj.get("name", "")
        # Qobuz/Tidal/Deezer expose the album's service-native id on the
        # album sub-object; surface it so the GUI can group results into
        # an album card and request a whole-album download.
        raw_id = album_obj.get("id")
        if raw_id is not None:
            album_id = str(raw_id)
        rel = (
            album_obj.get("release_date_original")
            or album_obj.get("released_at")
            or album_obj.get("release_date")
        )
        if isinstance(rel, str) and len(rel) >= 4 and rel[:4].isdigit():
            album_year = int(rel[:4])
        elif isinstance(rel, int) and rel > 0:
            try:
                album_year = time.gmtime(rel).tm_year
            except Exception:
                pass
        if album_year is None and isinstance(album_obj.get("year"), int):
            album_year = album_obj["year"]
        tc = (
            album_obj.get("tracks_count")
            or album_obj.get("track_count")
            or album_obj.get("number_of_tracks")
        )
        if isinstance(tc, int):
            album_track_count = tc
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
    # Track ordinal on the album, when present.
    track_number = item.get("track_number")
    if track_number is None:
        track_number = item.get("trackNumber")
    if not isinstance(track_number, int):
        track_number = None

    return {
        "service_id": track_id,
        "service": service,
        "title": title,
        "artist": artist,
        "artists": artists_list,
        "album": album,
        "album_id": album_id,
        "album_year": album_year,
        "album_track_count": album_track_count,
        "track_number": track_number,
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
    duplicate = "duplicate"
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


def _check_pending_or_raise(main, what: str) -> None:
    """Fail loud when ``add_by_id`` / ``search_take_first`` silently dropped the request.

    streamrip 2.1's ``Main.add_by_id`` swallows metadata-fetch failures (it logs an
    ERROR but doesn't raise), leaving ``self.pending`` empty. Without this check
    we'd run ``resolve()`` + ``rip()`` over an empty queue and report
    ``status="complete"`` with no file ever written.
    """
    pending = getattr(main, "pending", None)
    if not pending:
        raise RuntimeError(
            f"streamrip didn't queue anything for {what} — likely an unknown ID, "
            "an unavailable track, or a search returning no results."
        )


def _check_resolved_or_raise(main, what: str) -> None:
    """Fail loud when ``resolve()`` produced no resolved media.

    ``Main.resolve()`` silently filters out items whose metadata fetch raised
    (returning None instead of a ``Media``). An empty ``self.media`` after
    resolve means the underlying service rejected the request — surface it as
    an error rather than reporting a ghost completion.
    """
    media = getattr(main, "media", None)
    if not media:
        raise RuntimeError(
            f"streamrip resolved no media for {what} — track metadata unavailable "
            "or the service returned no playable file (region/license/expired ID)."
        )


def _streamrip_db_track_count() -> Optional[int]:
    """Count rows in streamrip's downloads.db. Returns None if unreadable.

    Used to detect a no-op rip after ``rip()``: if the row count didn't grow,
    streamrip skipped the track. Defense-in-depth alongside the pre-flight
    ``_streamrip_db_has_id()`` check below — covers search-based downloads
    where we don't know the service id upfront.
    """
    import sqlite3
    db_path = _CONFIG_DIR / "downloads.db"
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Could not read streamrip downloads.db: %s", exc)
        return None


def _streamrip_db_has_id(track_id: str) -> bool:
    """Return True iff ``track_id`` is already in streamrip's downloads.db.

    Pre-flight check used to short-circuit re-downloads. Without this,
    ``Pending.resolve()`` silently filters out already-downloaded items and
    we'd hit ``_check_resolved_or_raise`` thinking the track was unavailable.
    """
    import sqlite3
    if not track_id:
        return False
    db_path = _CONFIG_DIR / "downloads.db"
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE id = ? LIMIT 1", (track_id,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Could not check streamrip downloads.db for %s: %s", track_id, exc)
        return False


def _maybe_mark_duplicate(task, before_count: Optional[int]) -> None:
    """Flag ``task`` as duplicate if the rip didn't add a new downloads.db row.

    streamrip writes to ``downloads.db`` *after* a successful download. If the
    row count is unchanged across the rip, streamrip silently skipped the
    track (already in db) — surface that as ``duplicate`` so the caller knows
    nothing new landed.
    """
    if before_count is None:
        return  # couldn't read db; don't second-guess
    after_count = _streamrip_db_track_count()
    if after_count is None or after_count > before_count:
        return  # real download (or unreadable) — leave for the post-block code to mark complete
    # No new row → streamrip skipped this track because it was already downloaded.
    task.status = TaskStatus.duplicate
    task.error = None
    task.progress = 100.0
    task.updated_at = time.time()


async def _do_download(task_id: str, service: str, service_id: str,
                       artist: str = "", title: str = "",
                       entity_type: str = "track") -> None:
    """Download a track or album using streamrip's Python API."""
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

            # Albums always go via add_by_id — there's no search-based fallback for
            # whole albums. Qobuz returns both numeric catalog IDs and alphanumeric
            # IDs (covers, tributes, complete editions, AI remixes), and both are
            # valid inputs to add_by_id, so we don't gate on .isdigit() here.
            if entity_type == "album":
                if not service_id or service not in ("qobuz", "tidal", "deezer"):
                    task.status = TaskStatus.error
                    task.error = "Album downloads require service + album_id"
                    task.updated_at = time.time()
                    return
                logger.info(
                    "Task %s: album %s/%s via add_by_id", task_id, service, service_id,
                )
                await main.add_by_id(service, "album", service_id)
                _check_pending_or_raise(main, f"{service}/album/{service_id}")
                await main.resolve()
                _check_resolved_or_raise(main, f"{service}/album/{service_id}")
                before_count = _streamrip_db_track_count()
                await main.rip()
                _maybe_mark_duplicate(task, before_count)
            elif service_id and service_id.isdigit() and service in ("qobuz", "tidal", "deezer"):
                # Pre-flight: streamrip's downloads.db already has this id?
                # If so, short-circuit — otherwise Pending.resolve() silently
                # drops the item and our _check_resolved_or_raise mistakes it
                # for a metadata-unavailable error.
                if _streamrip_db_has_id(service_id):
                    logger.info(
                        "Task %s: %s/%s already in streamrip downloads.db — duplicate",
                        task_id, service, service_id,
                    )
                    task.status = TaskStatus.duplicate
                    task.progress = 100.0
                    task.updated_at = time.time()
                    return

                # Direct ID-based track download. streamrip 2.1+ uses
                # add_by_id(source, media_type, id) → resolve() → rip().
                # The legacy `handle_urls` method was removed in 2.x.
                logger.info(
                    "Task %s: track %s/%s via add_by_id", task_id, service, service_id,
                )
                await main.add_by_id(service, "track", service_id)
                _check_pending_or_raise(main, f"{service}/track/{service_id}")
                await main.resolve()
                _check_resolved_or_raise(main, f"{service}/track/{service_id}")
                before_count = _streamrip_db_track_count()
                await main.rip()
                _maybe_mark_duplicate(task, before_count)
            else:
                # Search-based track fallback for callers without a service-native ID
                # (e.g. they have a Spotify ID but artist+title are usable for search).
                search_query = f"{artist} {title}".strip() if artist and title else service_id
                if not search_query:
                    task.status = TaskStatus.error
                    task.error = "No track ID or artist/title provided"
                    task.updated_at = time.time()
                    return

                download_service = service if service in _available_services else DEFAULT_SERVICE
                logger.info("Task %s: search download %r on %s", task_id, search_query, download_service)
                # search_take_first only enqueues the first hit by id; we still need to
                # resolve metadata + rip to actually download.
                await main.search_take_first(download_service, "track", search_query)
                _check_pending_or_raise(main, f"search {search_query!r} on {download_service}")
                await main.resolve()
                _check_resolved_or_raise(main, f"search {search_query!r} on {download_service}")
                before_count = _streamrip_db_track_count()
                await main.rip()
                _maybe_mark_duplicate(task, before_count)

        # If the rip turned out to be a duplicate, _maybe_mark_duplicate
        # already set the terminal status — don't override it.
        if task.status != TaskStatus.duplicate:
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


def _build_service_url(service: str, service_id: str, entity_type: str = "track") -> str | None:
    if not service_id or not service_id.isdigit():
        return None
    if entity_type not in ("track", "album"):
        return None
    track_urls = {
        "qobuz": f"https://www.qobuz.com/track/{service_id}",
        "tidal": f"https://tidal.com/browse/track/{service_id}",
        "deezer": f"https://www.deezer.com/track/{service_id}",
    }
    album_urls = {
        "qobuz": f"https://www.qobuz.com/album/{service_id}",
        "tidal": f"https://tidal.com/browse/album/{service_id}",
        "deezer": f"https://www.deezer.com/album/{service_id}",
    }
    return (album_urls if entity_type == "album" else track_urls).get(service)


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
    album_id: Optional[str] = ""
    album_year: Optional[int] = None
    album_track_count: Optional[int] = None
    track_number: Optional[int] = None
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
    # "track" (default) builds /track/{id}; "album" builds /album/{id}.
    entity_type: Optional[str] = "track"


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
    limit: int = Query(25, ge=1, le=100),
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


@app.get("/search/artist")
async def search_artist(
    q: str = Query(..., min_length=1, description="Artist name or partial match"),
    limit: int = Query(2, ge=1, le=10, description="How many artist matches to return"),
    albums_per_artist: int = Query(100, ge=1, le=300, description="Cap primary albums returned per artist"),
    service: str = Query(None, description="qobuz, tidal, deezer, soundcloud"),
):
    """Search for artists; for each match, return the artist's discography
    (album metadata only — no tracks). Tracks are loaded lazily per-album
    via ``/album/{album_id}/tracks`` so the initial payload stays small.
    """
    svc = (service or DEFAULT_SERVICE).lower()
    if svc not in ("qobuz", "tidal", "deezer", "soundcloud"):
        raise HTTPException(400, f"Unknown service '{svc}'")
    if svc not in _available_services:
        raise HTTPException(503, f"Service '{svc}' not configured")

    async with _streamrip_lock:
        return await _do_artist_search(svc, q, limit, albums_per_artist)


@app.get("/album/{album_id}/tracks")
async def get_album_tracks(
    album_id: str,
    service: str = Query(None, description="qobuz, tidal, deezer, soundcloud"),
):
    """Return the track list for an album. Used by the dashboard to lazy-load
    tracks when the user expands an album card."""
    svc = (service or DEFAULT_SERVICE).lower()
    if svc not in ("qobuz", "tidal", "deezer", "soundcloud"):
        raise HTTPException(400, f"Unknown service '{svc}'")
    if svc not in _available_services:
        raise HTTPException(503, f"Service '{svc}' not configured")
    if not album_id or not album_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Invalid album_id")

    async with _streamrip_lock:
        return await _do_album_tracks(svc, album_id)


@app.post("/download", response_model=DownloadResponse)
async def download(body: DownloadRequestBody):
    """Trigger a track or album download. Returns immediately with a task_id."""
    track_id = body.service_id or body.spotify_id or ""
    entity_type = (body.entity_type or "track").lower()
    if entity_type not in ("track", "album"):
        raise HTTPException(400, f"entity_type must be 'track' or 'album', got {entity_type!r}")
    if not track_id and (entity_type == "album" or not (body.artist and body.title)):
        raise HTTPException(400, "Provide service_id (or artist + title for tracks)")

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
        _do_download(task_id, svc, track_id, body.artist or "", body.title or "", entity_type)
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
