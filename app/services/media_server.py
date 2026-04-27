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
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import ListenEvent, TrackFeatures, TrackInteraction

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
    genre: str = ""  # comma-separated genre tags
    file_path: str = ""  # absolute or relative path as reported by the server
    duration: float | None = None


@dataclass
class SyncResult:
    """Summary of a sync operation."""

    server_type: str = ""
    tracks_fetched: int = 0
    tracks_matched: int = 0
    tracks_updated: int = 0  # track_id actually changed
    tracks_metadata: int = 0  # metadata (title/artist/album) updated
    tracks_unmatched: int = 0
    errors: list[str] = field(default_factory=list)
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
            path = path[len(root_norm) :]
    else:
        path = file_path.replace("\\", "/")
    # Strip leading slashes, lower-case for case-insensitive matching.
    return path.lstrip("/").lower()


# ---------------------------------------------------------------------------
# Navidrome client  (Subsonic API)
# ---------------------------------------------------------------------------


async def _fetch_navidrome_tracks(base_url: str, username: str, password: str) -> list[MediaServerTrack]:
    """Fetch all tracks from a Navidrome server via the Subsonic API."""
    # Subsonic token-based auth: token = md5(password + salt)
    import secrets as _secrets

    salt = _secrets.token_hex(8)
    # MD5(password + salt) is mandated by the Subsonic API spec for auth tokens.
    token = hashlib.md5((password + salt).encode()).hexdigest()  # nosemgrep

    base = base_url.rstrip("/")
    common_params = {
        "u": username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "grooveiq",
        "f": "json",
    }

    tracks: list[MediaServerTrack] = []
    page_size = 500
    offset = 0

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as client:
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
                tracks.append(
                    MediaServerTrack(
                        server_id=str(s["id"]),
                        title=s.get("title", ""),
                        artist=s.get("artist", ""),
                        album=s.get("album", ""),
                        genre=s.get("genre", ""),
                        file_path=s.get("path", ""),
                        duration=float(s["duration"]) if s.get("duration") else None,
                    )
                )

            if len(songs) < page_size:
                break
            offset += page_size

    logger.info(f"Navidrome: fetched {len(tracks)} tracks from {base}")
    return tracks


# ---------------------------------------------------------------------------
# Plex client
# ---------------------------------------------------------------------------


async def _fetch_plex_tracks(base_url: str, token: str, library_id: str) -> list[MediaServerTrack]:
    """Fetch all tracks from a Plex server."""
    base = base_url.rstrip("/")
    headers = {
        "X-Plex-Token": token,
        "Accept": "application/json",
    }

    tracks: list[MediaServerTrack] = []

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as client:
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

                # Genre tags: Plex returns [{"tag": "Hip-Hop"}, {"tag": "Rap"}]
                genre_tags = m.get("Genre", [])
                genre = ", ".join(g["tag"] for g in genre_tags if isinstance(g, dict) and "tag" in g)

                tracks.append(
                    MediaServerTrack(
                        server_id=str(m["ratingKey"]),
                        title=m.get("title", ""),
                        artist=m.get("grandparentTitle", ""),  # artist
                        album=m.get("parentTitle", ""),  # album
                        genre=genre,
                        file_path=file_path,
                        duration=duration,
                    )
                )

            if len(metadata_list) < page_size:
                break
            offset += page_size

    logger.info(f"Plex: fetched {len(tracks)} tracks from {base}")
    return tracks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_tracks() -> list[MediaServerTrack]:
    """
    Fetch all tracks from the configured media server.

    Raises RuntimeError if no media server is configured.
    """
    server_type = settings.MEDIA_SERVER_TYPE.lower().strip()

    if server_type == "navidrome":
        if not settings.MEDIA_SERVER_URL or not settings.MEDIA_SERVER_USER:
            raise RuntimeError("Navidrome requires MEDIA_SERVER_URL and MEDIA_SERVER_USER.")
        from app.core.credentials import get_media_server_password

        return await _fetch_navidrome_tracks(
            settings.MEDIA_SERVER_URL,
            settings.MEDIA_SERVER_USER,
            get_media_server_password(),
        )
    elif server_type == "plex":
        if not settings.MEDIA_SERVER_URL or not settings.MEDIA_SERVER_TOKEN:
            raise RuntimeError("Plex requires MEDIA_SERVER_URL and MEDIA_SERVER_TOKEN.")
        from app.core.credentials import get_media_server_token

        return await _fetch_plex_tracks(
            settings.MEDIA_SERVER_URL,
            get_media_server_token(),
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


# ---------------------------------------------------------------------------
# Library refresh (API-triggered scan)
# ---------------------------------------------------------------------------


async def refresh_library(path: str | None = None) -> bool:
    """Trigger an immediate library rescan on the configured media server.

    Used after a Spotizerr download completes so the new file becomes
    playable without waiting for the next scheduled scan.

    Both Plex and Navidrome efficiently skip files they've already
    indexed, so a full rescan is cheap when only one new file has
    arrived — no need for us to know the exact output path.

    ``path`` is an optional server-visible absolute path for Plex's
    partial-refresh feature (``/library/sections/{id}/refresh?path=``).
    Navidrome's Subsonic ``startScan.view`` doesn't support partial
    scans and ignores the argument.

    Returns True if the upstream API accepted the request.  The scan
    itself runs asynchronously on the media server; this function
    does not wait for it to finish.
    """
    server_type = settings.MEDIA_SERVER_TYPE.lower().strip()

    if server_type == "navidrome":
        return await _refresh_navidrome()
    if server_type == "plex":
        return await _refresh_plex(path)

    logger.debug("refresh_library: no media server configured")
    return False


async def _refresh_navidrome() -> bool:
    """Fire Navidrome's Subsonic ``startScan.view`` endpoint."""
    if not settings.MEDIA_SERVER_URL or not settings.MEDIA_SERVER_USER:
        logger.warning("Navidrome refresh skipped: URL or user not configured")
        return False

    import secrets as _secrets

    from app.core.credentials import get_media_server_password

    base = settings.MEDIA_SERVER_URL.rstrip("/")
    username = settings.MEDIA_SERVER_USER
    password = get_media_server_password()
    if not password:
        logger.warning("Navidrome refresh skipped: no password configured")
        return False

    salt = _secrets.token_hex(8)
    # MD5(password + salt) is mandated by the Subsonic API spec for auth tokens.
    token = hashlib.md5((password + salt).encode()).hexdigest()  # nosemgrep
    params = {
        "u": username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "grooveiq",
        "f": "json",
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as client:
            resp = await client.get(f"{base}/rest/startScan.view", params=params)
            resp.raise_for_status()
            data = resp.json()
            sub = data.get("subsonic-response", {})
            if sub.get("status") == "ok":
                logger.info("Navidrome: library scan triggered")
                return True
            err = sub.get("error", {}).get("message", "unknown")
            logger.warning("Navidrome scan trigger failed: %s", err)
            return False
    except Exception as exc:
        logger.warning("Navidrome scan trigger error: %s", exc)
        return False


async def _refresh_plex(path: str | None = None) -> bool:
    """Fire Plex's ``/library/sections/{id}/refresh`` endpoint.

    When ``path`` is supplied, Plex does a partial scan of that
    directory only (much faster on huge libraries).  The path must
    be Plex-visible — i.e. the path inside Plex's container, which
    we approximate via ``MEDIA_SERVER_MUSIC_PATH``.
    """
    if not settings.MEDIA_SERVER_URL or not settings.MEDIA_SERVER_TOKEN:
        logger.warning("Plex refresh skipped: URL or token not configured")
        return False
    if not settings.MEDIA_SERVER_LIBRARY_ID:
        logger.warning("Plex refresh skipped: MEDIA_SERVER_LIBRARY_ID not set")
        return False

    from app.core.credentials import get_media_server_token

    base = settings.MEDIA_SERVER_URL.rstrip("/")
    token = get_media_server_token()
    if not token:
        logger.warning("Plex refresh skipped: no token configured")
        return False

    url = f"{base}/library/sections/{settings.MEDIA_SERVER_LIBRARY_ID}/refresh"
    params: dict[str, str] = {"X-Plex-Token": token}
    if path:
        params["path"] = path

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            scope = f"partial path={path}" if path else "full library"
            logger.info(
                "Plex: library scan triggered (section=%s, %s)",
                settings.MEDIA_SERVER_LIBRARY_ID,
                scope,
            )
            return True
    except Exception as exc:
        logger.warning("Plex scan trigger error: %s", exc)
        return False


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
    import asyncio

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
    server_map: dict[str, MediaServerTrack] = {}
    for st in server_tracks:
        norm = _normalise_path(st.file_path, server_music_root)
        if norm:
            server_map[norm] = st

    # 3. Load only the columns we need (not full ORM objects — 21k+ rows).
    rows = (
        await session.execute(
            select(
                TrackFeatures.id,
                TrackFeatures.track_id,
                TrackFeatures.file_path,
                TrackFeatures.title,
                TrackFeatures.artist,
                TrackFeatures.album,
                TrackFeatures.genre,
                TrackFeatures.external_track_id,
            )
        )
    ).all()
    grooveiq_music_root = settings.MUSIC_LIBRARY_PATH

    # 4. Build batch updates instead of per-row ORM flush.
    metadata_updates: list[dict] = []  # bulk metadata UPDATE
    track_id_renames: list[tuple] = []  # (tf_id, old_id, new_id, external_id)
    existing_track_ids = {r.track_id for r in rows}

    for r in rows:
        norm = _normalise_path(r.file_path, grooveiq_music_root)
        if not norm:
            continue

        st = server_map.get(norm)
        if not st:
            result.tracks_unmatched += 1
            continue

        result.tracks_matched += 1
        old_track_id = r.track_id
        new_track_id = st.server_id

        # Check metadata changes.
        if r.title != st.title or r.artist != st.artist or r.album != st.album or r.genre != st.genre:
            metadata_updates.append(
                {
                    "tf_id": r.id,
                    "title": st.title,
                    "artist": st.artist,
                    "album": st.album,
                    "genre": st.genre,
                }
            )
            result.tracks_metadata += 1

        # Check track_id rename.
        if old_track_id != new_track_id:
            # Skip if new_track_id already exists (conflict).
            if new_track_id in existing_track_ids:
                logger.warning(
                    "Track ID conflict for %s: server ID '%s' already exists, keeping metadata only", norm, new_track_id
                )
            else:
                ext_id = r.external_track_id or old_track_id
                track_id_renames.append((r.id, old_track_id, new_track_id, ext_id))
                # Update the set so subsequent checks see the new ID.
                existing_track_ids.discard(old_track_id)
                existing_track_ids.add(new_track_id)
                result.tracks_updated += 1

    logger.info(
        f"Media server sync: matched={result.tracks_matched}, "
        f"metadata_updates={len(metadata_updates)}, id_renames={len(track_id_renames)}, "
        f"match_phase={time.time() - t0:.1f}s"
    )

    # 5. Apply metadata updates in batches (yield between batches).
    batch_size = 200
    for i in range(0, len(metadata_updates), batch_size):
        batch = metadata_updates[i : i + batch_size]
        for upd in batch:
            await session.execute(
                update(TrackFeatures)
                .where(TrackFeatures.id == upd["tf_id"])
                .values(title=upd["title"], artist=upd["artist"], album=upd["album"], genre=upd["genre"])
            )
        await session.flush()
        await asyncio.sleep(0)  # yield to event loop

    # 6. Apply track_id renames (with cascading).
    for tf_id, old_id, new_id, ext_id in track_id_renames:
        try:
            await session.execute(
                update(TrackFeatures).where(TrackFeatures.id == tf_id).values(track_id=new_id, external_track_id=ext_id)
            )
            await session.execute(update(ListenEvent).where(ListenEvent.track_id == old_id).values(track_id=new_id))
            await session.execute(
                update(TrackInteraction).where(TrackInteraction.track_id == old_id).values(track_id=new_id)
            )
        except Exception as exc:
            result.errors.append(f"rename {old_id}→{new_id}: {str(exc)[:120]}")
            logger.warning("Sync rename error %s→%s: %s", old_id, new_id, exc)
        # Yield every rename to keep event loop responsive.
        await asyncio.sleep(0)

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
