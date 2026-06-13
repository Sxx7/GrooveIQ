"""
GrooveIQ – Audio file metadata reader.

Reads ID3 (MP3), Vorbis (FLAC/OGG/Opus), MP4/M4A, and WavPack tags
using mutagen.  Returns a dict of normalised metadata fields.

This runs in the library scanner's process pool alongside Essentia,
so it must be importable without app-level dependencies.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

try:
    import mutagen
    from mutagen.easyid3 import EasyID3  # noqa: F401
    from mutagen.easymp4 import EasyMP4Tags  # noqa: F401

    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False
    logger.warning("mutagen not installed. Metadata extraction unavailable.")


def read_metadata(file_path: str) -> dict:
    """
    Read audio metadata tags from *file_path*.

    Returns a dict with keys: title, artist, album, album_artist,
    track_number, duration_ms, genre, musicbrainz_track_id.
    Missing values are None.
    """
    empty: dict = {
        "title": None,
        "artist": None,
        "album": None,
        "album_artist": None,
        "track_number": None,
        "duration_ms": None,
        "genre": None,
        "musicbrainz_track_id": None,
    }

    if not MUTAGEN_AVAILABLE:
        return empty

    try:
        audio = mutagen.File(file_path, easy=True)
        if audio is None:
            return empty

        result = dict(empty)

        # Duration (mutagen stores as float seconds)
        if audio.info and hasattr(audio.info, "length") and audio.info.length:
            result["duration_ms"] = int(audio.info.length * 1000)

        # EasyID3/EasyMP4/VorbisComment all expose a dict-like interface
        # with list values.  We take the first value for each tag.
        tag_map = {
            "title": "title",
            "artist": "artist",
            "album": "album",
            "albumartist": "album_artist",
            "tracknumber": "track_number",
            "genre": "genre",
            "musicbrainz_trackid": "musicbrainz_track_id",
        }

        for tag_key, result_key in tag_map.items():
            values = audio.get(tag_key)
            if values:
                raw = values[0] if isinstance(values, list) else str(values)
                # PostgreSQL VARCHAR rejects NUL bytes (0x00) that creep in
                # from messy ID3 tags; SQLite tolerated them.
                val = raw.replace("\x00", "").strip()
                if val:
                    if result_key == "track_number":
                        # Track numbers can be "3/12" — take only the number
                        result[result_key] = _parse_track_number(val)
                    else:
                        result[result_key] = val[:512]  # cap length

        return result

    except Exception as e:
        logger.debug("Metadata read failed for %s: %s", file_path, e)
        return empty


def _parse_track_number(val: str) -> int | None:
    """Parse track number from formats like '3', '3/12', '03'."""
    try:
        return int(val.split("/")[0])
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Embedded lyrics (tier 1 of the acquisition cascade — see app/services/lyrics.py)
# ---------------------------------------------------------------------------

# EasyID3 does not expose USLT/SYLT, so embedded lyrics need mutagen's
# low-level (non-easy) interface. This helper is intentionally container-aware
# and pure-Python (no Essentia / app deps) so it runs both inside the scan
# worker pool and in the main-process drain.

_MAX_LYRIC_CHARS = 20000
# An LRC timestamp tag, e.g. "[01:23.45]" or "[1:23]".
_LRC_LINE_RE = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
# Leading run of bracketed tags on a line (one or more timestamps / id tags).
_LRC_TAG_RE = re.compile(r"^(?:\[[^\]]*\])+")


def read_embedded_lyrics(file_path: str) -> dict:
    """
    Read embedded lyrics from *file_path* using mutagen's low-level interface.

    Returns ``{"plain": str|None, "synced": str|None, "source": "embedded"|None}``.
    ``synced`` is LRC text ("[mm:ss.xx] line"); ``plain`` is newline-joined.
    All-None (and ``source`` None) when the file carries no lyric tags, the
    format is unsupported, or mutagen is unavailable.

    Covers ID3 USLT/SYLT (MP3, ID3-tagged WAV/AIFF), VorbisComment
    LYRICS/UNSYNCEDLYRICS/SYNCEDLYRICS (FLAC/OGG/Opus) and MP4 ``\\xa9lyr``
    (M4A/AAC). A "LYRICS" tag that itself contains LRC timestamps is treated as
    synced, with a timestamp-stripped copy used for ``plain``.
    """
    empty: dict = {"plain": None, "synced": None, "source": None}
    if not MUTAGEN_AVAILABLE:
        return empty

    try:
        audio = mutagen.File(file_path)  # non-easy: exposes raw frames/comments
        if audio is None:
            return empty

        plain: str | None = None
        synced: str | None = None
        tags = getattr(audio, "tags", None)

        # --- ID3 (MP3, ID3-tagged WAV/AIFF): USLT (plain), SYLT (synced) ---
        if tags is not None and hasattr(tags, "getall"):
            for frame in _safe_getall(tags, "USLT"):
                text = _clean_lyric_text(getattr(frame, "text", None))
                if text:
                    plain = text
                    break
            for frame in _safe_getall(tags, "SYLT"):
                lrc = _sylt_to_lrc(getattr(frame, "text", None))
                if lrc:
                    synced = lrc
                    break

        # --- MP4 / M4A: \xa9lyr (plain only) ---
        if plain is None and tags is not None:
            try:
                lyr = tags.get("\xa9lyr")
            except Exception:
                lyr = None
            text = _clean_lyric_text(lyr)
            if text:
                plain = text

        # --- VorbisComment (FLAC/OGG/Opus): case-insensitive dict on the file ---
        synced_vc = _vorbis_get(audio, "syncedlyrics")
        unsynced_vc = _vorbis_get(audio, "unsyncedlyrics")
        lyrics_vc = _vorbis_get(audio, "lyrics")
        if synced is None and synced_vc and _looks_like_lrc(synced_vc):
            synced = synced_vc
        if synced is None and lyrics_vc and _looks_like_lrc(lyrics_vc):
            synced = lyrics_vc
        if plain is None:
            if unsynced_vc:
                plain = unsynced_vc
            elif lyrics_vc and not _looks_like_lrc(lyrics_vc):
                plain = lyrics_vc

        # Derive a plain copy from synced LRC when only synced is present.
        if plain is None and synced is not None:
            plain = _strip_lrc_timestamps(synced)

        if not plain and not synced:
            return empty
        return {"plain": plain, "synced": synced, "source": "embedded"}

    except Exception as e:
        logger.debug("Embedded-lyrics read failed for %s: %s", file_path, e)
        return empty


def _safe_getall(tags, key: str) -> list:
    try:
        return list(tags.getall(key)) or []
    except Exception:
        return []


def _vorbis_get(audio, key: str) -> str | None:
    """Read a VorbisComment field (list-valued, case-insensitive) off a file."""
    try:
        values = audio.get(key)
    except Exception:
        return None
    return _clean_lyric_text(values)


def _clean_lyric_text(raw) -> str | None:
    """Strip NUL bytes, normalise newlines, cap length; whitespace-only -> None."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
        if raw is None:
            return None
    text = str(raw).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    return text[:_MAX_LYRIC_CHARS]


def _looks_like_lrc(text: str | None) -> bool:
    return bool(text) and _LRC_LINE_RE.search(text) is not None


def _strip_lrc_timestamps(lrc: str) -> str | None:
    """Drop leading [mm:ss.xx]/[id:...] tags from each LRC line, join the rest."""
    out = []
    for line in lrc.splitlines():
        stripped = _LRC_TAG_RE.sub("", line).strip()
        if stripped:
            out.append(stripped)
    text = "\n".join(out).strip()
    return (text[:_MAX_LYRIC_CHARS]) if text else None


def _ms_to_lrc_ts(ms: int) -> str:
    if ms < 0:
        ms = 0
    total_cs = ms // 10  # centiseconds
    minutes = total_cs // 6000
    seconds = (total_cs % 6000) // 100
    centis = total_cs % 100
    return f"{minutes:02d}:{seconds:02d}.{centis:02d}"


def _sylt_to_lrc(sylt_text) -> str | None:
    """Convert a mutagen SYLT frame's ``[(text, time_ms), ...]`` into LRC."""
    if not sylt_text:
        return None
    lines = []
    try:
        for item in sylt_text:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            text, ms = item
            if ms is None:
                continue
            # Normalise line endings the same way _clean_lyric_text does — a
            # SYLT segment from a Windows/old-Mac tagger can carry \r\n / \r,
            # which would otherwise corrupt the LRC line it's spliced into.
            text = str(text).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            lines.append(f"[{_ms_to_lrc_ts(int(ms))}]{text}")
    except Exception:
        return None
    out = "\n".join(lines).strip()
    return (out[:_MAX_LYRIC_CHARS]) if out else None
