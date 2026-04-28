"""
GrooveIQ – Media server integration (Navidrome).

Fetches the track catalogue from the configured media server and records
each match's external identifier on ``TrackFeatures.media_server_id``.

Under the post-#37 schema, sync is a *metadata refresh*: ``track_id`` is
the stable internal GrooveIQ id (a SHA-256-prefix hash of the file path
relative to the music library, set once at scan time and never overwritten)
and the media server's ID lives in its own column. The previous behaviour
— renaming ``track_id`` and cascading the change across listen_events /
track_interactions / playlists / etc. — is gone.

Sync flow:
  1. Fetch all tracks from the media server API.
  2. Match each to a TrackFeatures row using one of four strategies in
     priority order: MBID → AATD (artist/album/title + duration) → ATD
     (album/title + strict duration) → file path.
  3. For matched tracks, write the media server ID into
     ``TrackFeatures.media_server_id`` and refresh the title / artist /
     album / genre fields from server metadata when they have drifted.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import TrackFeatures

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
    mb_track_id: str | None = None  # MusicBrainz Recording ID, if known


@dataclass
class SyncResult:
    """Summary of a sync operation."""

    server_type: str = ""
    tracks_fetched: int = 0
    tracks_matched: int = 0
    media_server_id_updated: int = 0  # rows whose media_server_id was newly set / changed
    metadata_updated: int = 0  # rows whose title / artist / album / genre was refreshed
    tracks_unmatched: int = 0
    # Per-strategy matching breakdown.  These sum to tracks_matched (excluding
    # ambiguous, which falls through to the next strategy or unmatched).
    tracks_matched_by_mbid: int = 0
    tracks_matched_by_aatd: int = 0  # (artist, album, title, duration±1s)
    tracks_matched_by_path: int = 0
    tracks_aatd_ambiguous: int = 0  # AATD key had >1 candidate after duration filter
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------


def _canon_str(s: str | None) -> str:
    """Lower-case, strip, and collapse internal whitespace.

    Used to build (artist, album, title) keys that survive small textual
    differences across sources (ID3 tags vs Navidrome's display strings).
    """
    if not s:
        return ""
    return " ".join(s.lower().split())


# Splits an artist string on every common collaborator separator so we can
# treat "Foo & Bar", "Foo and Bar", "Foo, Bar", "Foo / Bar", "Foo feat. Bar",
# "Foo featuring Bar", "Foo ft Bar", "Foo x Bar" as the same set of artists.
# Order matters: longer text tokens (`featuring`) must precede shorter ones
# (`feat`) and word-bounded tokens (` and `, ` x `) must include their
# surrounding spaces so we don't shred ordinary album/song words.
_ARTIST_SEP_RE = re.compile(
    r"(?: featuring | feat\.? | ft\.? | with | and | vs\.? | x | & |/|;|,)",
    flags=re.IGNORECASE,
)


def _canon_artist_set(artist: str | None) -> str:
    """Canonicalise a (possibly multi-)artist string into a sorted token set.

    Navidrome and ID3 tags often disagree on the separator for collab artists
    — disk says ``"2WEI and Elena Westermann and Edda Hayes"``, Navidrome
    reports ``"2WEI & Elena Westermann & Edda Hayes"``.  Splitting on every
    common separator and sorting the result yields the same canonical token
    regardless of which side wrote it.

    Returns an empty string when no usable artist tokens are found.
    """
    if not artist:
        return ""
    parts = _ARTIST_SEP_RE.split(artist)
    tokens = []
    for p in parts:
        c = _canon_str(p)
        if c:
            tokens.append(c)
    if not tokens:
        return ""
    return "|".join(sorted(set(tokens)))


def _aatd_key(artist: str | None, album: str | None, title: str | None) -> tuple[str, str, str] | None:
    """Build the canonical (artist, album, title) tuple key.

    The artist component is a *sorted set* of canonicalised tokens so a track
    tagged "Foo & Bar" matches a server-side "Foo and Bar" or "Bar, Foo".
    Returns ``None`` if either artist or title is missing — without those two
    we have no business asserting two tracks are the same record.  Album is
    allowed to be empty (some standalone singles don't tag it).
    """
    a = _canon_artist_set(artist)
    t = _canon_str(title)
    if not a or not t:
        return None
    return (a, _canon_str(album), t)


def _atd_key(album: str | None, title: str | None) -> tuple[str, str] | None:
    """Build the canonical (album, title) tuple key for the ATD fallback.

    Used when AATD misses entirely (typically because the artist field can't
    be canonicalised — e.g. completely different "Various Artists" wording).
    Caller must apply a strict duration filter; without artist as an anchor,
    duration is the only thing keeping us off near-miss tracks.
    """
    al = _canon_str(album)
    t = _canon_str(title)
    if not al or not t:
        # Require a non-empty album for ATD.  A bare (title, duration) match
        # would be far too lossy; many libraries have multiple tracks named
        # "Intro" that happen to share a duration.
        return None
    return (al, t)


def _duration_compatible(server_dur: float | None, db_dur: float | None, tol: float = 1.5) -> bool:
    """Return True if two durations agree within ``tol`` seconds.

    When either side is missing duration information, we accept the candidate
    rather than reject it — we'd rather over-match than under-match here, and
    the (artist, album, title) tuple is already a strong signal.
    """
    if server_dur is None or db_dur is None:
        return True
    return abs(server_dur - db_dur) <= tol


def _extract_mbid_from_plex_guid(metadata: dict) -> str | None:
    """Extract the MusicBrainz Recording ID from a Plex track's Guid array.

    Plex serializes external IDs as URIs in the per-item ``Guid`` field, e.g.
    ``[{"id": "mbid://recording/<uuid>"}, {"id": "mbid://artist/<uuid>"}]``.
    We only care about the recording-level ID — the artist/album guids are
    already implicit in the (artist, album, title) tuple.
    """
    for guid in metadata.get("Guid", []) or []:
        if not isinstance(guid, dict):
            continue
        gid = guid.get("id") or ""
        for prefix in ("mbid://recording/", "musicbrainz://recording/"):
            if gid.startswith(prefix):
                rec = gid[len(prefix) :].strip()
                if rec:
                    return rec
    return None


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
                        mb_track_id=(s.get("musicBrainzId") or None),
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
                        mb_track_id=_extract_mbid_from_plex_guid(m),
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

    # 2. Build four lookup indexes from the server tracks, used in priority
    #    order: MBID → (artist,album,title)+duration → (album,title)+duration
    #    → path.  The path index is kept as a last-resort matcher because
    #    Navidrome 0.61+ reports a synthetic templated path (computed from
    #    tags) rather than the actual filesystem path, which silently breaks
    #    pure path matching for any library not laid out exactly to
    #    Navidrome's template.  The ATD index handles the residual case where
    #    AATD misses entirely because of artist-field divergence we couldn't
    #    canonicalise (e.g. completely different "Various Artists" wording).
    server_music_root = settings.MEDIA_SERVER_MUSIC_PATH or settings.MUSIC_LIBRARY_PATH
    mb_index: dict[str, MediaServerTrack] = {}
    aatd_index: dict[tuple[str, str, str], list[MediaServerTrack]] = {}
    atd_index: dict[tuple[str, str], list[MediaServerTrack]] = {}
    path_index: dict[str, MediaServerTrack] = {}
    for st in server_tracks:
        if st.mb_track_id:
            mb_index[st.mb_track_id] = st
        key = _aatd_key(st.artist, st.album, st.title)
        if key:
            aatd_index.setdefault(key, []).append(st)
        atd = _atd_key(st.album, st.title)
        if atd:
            atd_index.setdefault(atd, []).append(st)
        norm = _normalise_path(st.file_path, server_music_root)
        if norm:
            path_index[norm] = st

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
                TrackFeatures.media_server_id,
                TrackFeatures.musicbrainz_track_id,
                TrackFeatures.duration,
                TrackFeatures.duration_ms,
            )
        )
    ).all()
    grooveiq_music_root = settings.MUSIC_LIBRARY_PATH

    # 4. Build batch updates. Sync is a metadata refresh under the post-#37
    #    schema: it never rewrites track_id, so there's no cross-table cascade.
    metadata_updates: list[dict] = []  # bulk metadata UPDATE (title/artist/album/genre)
    media_server_id_updates: list[dict] = []  # bulk UPDATE for the per-row external ID

    for r in rows:
        # GrooveIQ's stored duration: prefer the precise duration_ms (from ID3
        # tags) over the analyzer's seconds, falling back to whichever is set.
        db_duration: float | None = None
        if r.duration_ms is not None:
            db_duration = r.duration_ms / 1000.0
        elif r.duration is not None:
            db_duration = float(r.duration)

        st: MediaServerTrack | None = None
        match_via: str | None = None

        # Strategy 1: MusicBrainz Recording ID.  Most reliable when present —
        # it survives renames, retags, and Navidrome's template quirks.
        if r.musicbrainz_track_id:
            st = mb_index.get(r.musicbrainz_track_id)
            if st:
                match_via = "mbid"

        # Strategy 2: (artist, album, title) tuple, optionally disambiguated
        # by duration.  Catches Spotizerr/spotdl downloads that lack MBIDs.
        # Skip when ambiguous (>1 candidate even after duration filter) so
        # we don't risk wrong attributions; let path fall through.
        if st is None:
            key = _aatd_key(r.artist, r.album, r.title)
            if key:
                cands = aatd_index.get(key, [])
                if len(cands) > 1 and db_duration is not None:
                    cands = [c for c in cands if _duration_compatible(c.duration, db_duration)]
                if len(cands) == 1:
                    st = cands[0]
                    match_via = "aatd"
                elif len(cands) > 1:
                    result.tracks_aatd_ambiguous += 1

        # Strategy 2b: (album, title) + strict duration fallback.  Picks up
        # tracks where artist canonicalisation still failed (e.g. "Various
        # Artists" vs the original artist, or non-Latin script variants).
        # We only accept when (a) duration is known on both sides, (b) the
        # match is unique after a strict ±1s filter — without artist as an
        # anchor, duration is the only thing keeping us off near-miss tracks.
        # Counts under tracks_matched_by_aatd to stay backwards-compatible.
        if st is None and db_duration is not None:
            atd = _atd_key(r.album, r.title)
            if atd:
                cands = atd_index.get(atd, [])
                cands = [
                    c
                    for c in cands
                    if c.duration is not None and _duration_compatible(c.duration, db_duration, tol=1.0)
                ]
                if len(cands) == 1:
                    st = cands[0]
                    match_via = "aatd"

        # Strategy 3: legacy file-path matcher.  Still useful for Plex
        # libraries (which return real filesystem paths) and any Navidrome
        # tracks whose actual layout happens to coincide with the template.
        if st is None:
            norm = _normalise_path(r.file_path, grooveiq_music_root)
            if norm:
                st = path_index.get(norm)
                if st:
                    match_via = "path"

        if st is None:
            result.tracks_unmatched += 1
            continue

        result.tracks_matched += 1
        if match_via == "mbid":
            result.tracks_matched_by_mbid += 1
        elif match_via == "aatd":
            result.tracks_matched_by_aatd += 1
        elif match_via == "path":
            result.tracks_matched_by_path += 1

        # Refresh title/artist/album/genre from the server when they've drifted.
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
            result.metadata_updated += 1

        # Record / refresh the media server's ID for this row. No rename, no
        # cascade — the internal `track_id` is immutable.
        if r.media_server_id != st.server_id:
            media_server_id_updates.append({"tf_id": r.id, "media_server_id": st.server_id})
            result.media_server_id_updated += 1

    logger.info(
        f"Media server sync: matched={result.tracks_matched}, "
        f"metadata_updates={len(metadata_updates)}, "
        f"media_server_id_updates={len(media_server_id_updates)}, "
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

    # 6. Apply media_server_id updates in batches.
    for i in range(0, len(media_server_id_updates), batch_size):
        batch = media_server_id_updates[i : i + batch_size]
        for upd in batch:
            await session.execute(
                update(TrackFeatures)
                .where(TrackFeatures.id == upd["tf_id"])
                .values(media_server_id=upd["media_server_id"])
            )
        await session.flush()
        await asyncio.sleep(0)

    await session.commit()
    result.elapsed_s = round(time.time() - t0, 2)

    logger.info(
        "Media server sync complete",
        extra={
            "server": result.server_type,
            "fetched": result.tracks_fetched,
            "matched": result.tracks_matched,
            "matched_mbid": result.tracks_matched_by_mbid,
            "matched_aatd": result.tracks_matched_by_aatd,
            "matched_path": result.tracks_matched_by_path,
            "aatd_ambiguous": result.tracks_aatd_ambiguous,
            "media_server_id_updated": result.media_server_id_updated,
            "metadata_updated": result.metadata_updated,
            "unmatched": result.tracks_unmatched,
            "elapsed": result.elapsed_s,
        },
    )
    return result
