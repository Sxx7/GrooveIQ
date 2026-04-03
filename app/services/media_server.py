"""
GrooveIQ – Media server integration (Navidrome / Plex).

Fetches track catalogues from the configured media server and syncs
track IDs so GrooveIQ uses the same identifiers as the media server.
This means events from iOS/web clients can reference tracks by their
Navidrome/Plex ID and recommendations return those same IDs.

Sync flow:
  1. Fetch all tracks from the media server API.
  2. Match each to a TrackFeatures row by normalised relative file path.
  3. For matched tracks: update track_id to the media server ID,
     populate title/artist/album metadata.
  4. Cascade track_id changes to listen_events, listen_sessions,
     and track_interactions.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import ListenEvent, ListenSession, TrackFeatures, TrackInteraction

logger = logging.getLogger(__name__)

# Timeout for media server HTTP requests (seconds).
_HTTP_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class MediaServerTrack:
    """A single track as reported by the media server."""
    server_id: str
    title: str = ""
    artist: str = ""
    album: str = ""
    file_path: str = ""       # absolute or relative path as reported by the server
    duration: Optional[float] = None


@dataclass
class SyncResult:
    """Summary of a sync operation."""
    server_type: str = ""
    tracks_fetched: int = 0
    tracks_matched: int = 0
    tracks_updated: int = 0   # track_id actually changed
    tracks_metadata: int = 0  # metadata (title/artist/album) updated
    tracks_unmatched: int = 0
    errors: List[str] = field(default_factory=list)
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------

def _normalise_path(file_path: str, music_root: str) -> str:
    """
    Convert an absolute file path to a normalised relative path for matching.

    Both GrooveIQ and the media server see the same music library but possibly
    mounted at different paths.  We strip the music root and normalise
    separators / casing so that:
      /music/Artist/Album/Song.flac  (GrooveIQ, MUSIC_LIBRARY_PATH=/music)
      /data/music/Artist/Album/Song.flac  (Navidrome, MEDIA_SERVER_MUSIC_PATH=/data/music)
    both become:  artist/album/song.flac
    """
    if not file_path:
        return ""
    # Strip music root prefix.
    if music_root:
        root = music_root.rstrip("/\\")
        path = file_path.replace("\\", "/")
        root_norm = root.replace("\\", "/")
        if path.lower().startswith(root_norm.lower()):
            path = path[len(root_norm):]
    else:
        path = file_path.replace("\\", "/")
    # Strip leading slashes, lower-case for case-insensitive matching.
    return path.lstrip("/").lower()


# ---------------------------------------------------------------------------
# Navidrome client  (Subsonic API)
# ---------------------------------------------------------------------------

async def _fetch_navidrome_tracks(
    base_url: str, username: str, password: str
) -> List[MediaServerTrack]:
    """Fetch all tracks from a Navidrome server via the Subsonic API."""
    # Subsonic token-based auth: token = md5(password + salt)
    import secrets as _secrets
    salt = _secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()

    base = base_url.rstrip("/")
    common_params = {
        "u": username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "grooveiq",
        "f": "json",
    }

    tracks: List[MediaServerTrack] = []
    page_size = 500
    offset = 0

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        while True:
            params = {
                **common_params,
                "query": "",
                "songCount": str(page_size),
                "songOffset": str(offset),
                "artistCount": "0",
                "albumCount": "0",
            }
            resp = await client.get(f"{base}/rest/search3.view", params=params)
            resp.raise_for_status()
            data = resp.json()

            sub = data.get("subsonic-response", {})
            if sub.get("status") != "ok":
                error = sub.get("error", {}).get("message", "Unknown Subsonic error")
                raise RuntimeError(f"Navidrome API error: {error}")

            songs = sub.get("searchResult3", {}).get("song", [])
            if not songs:
                break

            for s in songs:
                tracks.append(MediaServerTrack(
                    server_id=str(s["id"]),
                    title=s.get("title", ""),
                    artist=s.get("artist", ""),
                    album=s.get("album", ""),
                    file_path=s.get("path", ""),
                    duration=float(s["duration"]) if s.get("duration") else None,
                ))

            if len(songs) < page_size:
                break
            offset += page_size

    logger.info(f"Navidrome: fetched {len(tracks)} tracks from {base}")
    return tracks


# ---------------------------------------------------------------------------
# Plex client
# ---------------------------------------------------------------------------

async def _fetch_plex_tracks(
    base_url: str, token: str, library_id: str
) -> List[MediaServerTrack]:
    """Fetch all tracks from a Plex server."""
    base = base_url.rstrip("/")
    headers = {
        "X-Plex-Token": token,
        "Accept": "application/json",
    }

    tracks: List[MediaServerTrack] = []

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        # Fetch all tracks from the specified library section.
        url = f"{base}/library/sections/{library_id}/all"
        params = {"type": "10"}  # type 10 = tracks
        offset = 0
        page_size = 500

        while True:
            params["X-Plex-Container-Start"] = str(offset)
            params["X-Plex-Container-Size"] = str(page_size)
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            container = data.get("MediaContainer", {})
            metadata_list = container.get("Metadata", [])

            if not metadata_list:
                break

            for m in metadata_list:
                # Extract file path from nested Media → Part structure.
                file_path = ""
                media = m.get("Media", [])
                if media:
                    parts = media[0].get("Part", [])
                    if parts:
                        file_path = parts[0].get("file", "")

                duration = None
                if m.get("duration"):
                    duration = float(m["duration"]) / 1000.0  # Plex returns ms

                tracks.append(MediaServerTrack(
                    server_id=str(m["ratingKey"]),
                    title=m.get("title", ""),
                    artist=m.get("grandparentTitle", ""),  # artist
                    album=m.get("parentTitle", ""),         # album
                    file_path=file_path,
                    duration=duration,
                ))

            if len(metadata_list) < page_size:
                break
            offset += page_size

    logger.info(f"Plex: fetched {len(tracks)} tracks from {base}")
    return tracks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_tracks() -> List[MediaServerTrack]:
    """
    Fetch all tracks from the configured media server.

    Raises RuntimeError if no media server is configured.
    """
    server_type = settings.MEDIA_SERVER_TYPE.lower().strip()

    if server_type == "navidrome":
        if not settings.MEDIA_SERVER_URL or not settings.MEDIA_SERVER_USER:
            raise RuntimeError("Navidrome requires MEDIA_SERVER_URL and MEDIA_SERVER_USER.")
        return await _fetch_navidrome_tracks(
            settings.MEDIA_SERVER_URL,
            settings.MEDIA_SERVER_USER,
            settings.MEDIA_SERVER_PASSWORD,
        )
    elif server_type == "plex":
        if not settings.MEDIA_SERVER_URL or not settings.MEDIA_SERVER_TOKEN:
            raise RuntimeError("Plex requires MEDIA_SERVER_URL and MEDIA_SERVER_TOKEN.")
        return await _fetch_plex_tracks(
            settings.MEDIA_SERVER_URL,
            settings.MEDIA_SERVER_TOKEN,
            settings.MEDIA_SERVER_LIBRARY_ID,
        )
    else:
        raise RuntimeError(
            f"No media server configured (MEDIA_SERVER_TYPE='{settings.MEDIA_SERVER_TYPE}'). "
            "Set MEDIA_SERVER_TYPE to 'navidrome' or 'plex'."
        )


def is_configured() -> bool:
    """Return True if a media server integration is configured."""
    return settings.MEDIA_SERVER_TYPE.lower().strip() in ("navidrome", "plex")


async def sync_track_ids(session: AsyncSession) -> SyncResult:
    """
    Synchronise GrooveIQ track IDs with the configured media server.

    For each track in the media server catalogue, matches it to a
    TrackFeatures row by normalised relative file path, then:
      - Sets track_id to the media server's ID.
      - Populates title, artist, album from the server metadata.
      - Cascades track_id changes to events, sessions, interactions.

    Returns a SyncResult summary.
    """
    t0 = time.time()
    result = SyncResult(server_type=settings.MEDIA_SERVER_TYPE)

    # 1. Fetch tracks from the media server.
    try:
        server_tracks = await fetch_tracks()
    except Exception as e:
        result.errors.append(str(e))
        result.elapsed_s = time.time() - t0
        return result

    result.tracks_fetched = len(server_tracks)
    if not server_tracks:
        result.elapsed_s = time.time() - t0
        return result

    # 2. Build a normalised-path → MediaServerTrack lookup from the server.
    server_music_root = settings.MEDIA_SERVER_MUSIC_PATH or settings.MUSIC_LIBRARY_PATH
    server_map: Dict[str, MediaServerTrack] = {}
    for st in server_tracks:
        norm = _normalise_path(st.file_path, server_music_root)
        if norm:
            server_map[norm] = st

    # 3. Load all TrackFeatures rows.
    all_tracks = (await session.execute(select(TrackFeatures))).scalars().all()
    grooveiq_music_root = settings.MUSIC_LIBRARY_PATH

    for tf in all_tracks:
        norm = _normalise_path(tf.file_path, grooveiq_music_root)
        if not norm:
            continue

        st = server_map.get(norm)
        if not st:
            result.tracks_unmatched += 1
            continue

        result.tracks_matched += 1
        old_track_id = tf.track_id
        new_track_id = st.server_id

        # Update metadata always (title/artist/album may have changed).
        metadata_changed = (
            tf.title != st.title
            or tf.artist != st.artist
            or tf.album != st.album
        )
        if metadata_changed:
            tf.title = st.title
            tf.artist = st.artist
            tf.album = st.album
            result.tracks_metadata += 1

        # Update track_id if it differs.
        if old_track_id != new_track_id:
            # Check if the new track_id already exists (another row).
            conflict = await session.execute(
                select(TrackFeatures.id)
                .where(TrackFeatures.track_id == new_track_id)
                .where(TrackFeatures.id != tf.id)
            )
            if conflict.scalar_one_or_none() is not None:
                result.errors.append(
                    f"Skipped {norm}: server ID '{new_track_id}' conflicts with existing track."
                )
                continue

            # Preserve the old GrooveIQ hash ID for reference.
            if not tf.external_track_id:
                tf.external_track_id = old_track_id

            # Cascade to related tables.
            await session.execute(
                update(ListenEvent)
                .where(ListenEvent.track_id == old_track_id)
                .values(track_id=new_track_id)
            )
            await session.execute(
                update(TrackInteraction)
                .where(TrackInteraction.track_id == old_track_id)
                .values(track_id=new_track_id)
            )
            # Note: ListenSession doesn't store track_id, no cascade needed.

            tf.track_id = new_track_id
            result.tracks_updated += 1

    await session.commit()
    result.elapsed_s = round(time.time() - t0, 2)

    logger.info(
        "Media server sync complete",
        extra={
            "server": result.server_type,
            "fetched": result.tracks_fetched,
            "matched": result.tracks_matched,
            "updated": result.tracks_updated,
            "metadata": result.tracks_metadata,
            "unmatched": result.tracks_unmatched,
            "elapsed": result.elapsed_s,
        },
    )
    return result
