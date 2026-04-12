"""
GrooveIQ – Audio file metadata reader.

Reads ID3 (MP3), Vorbis (FLAC/OGG/Opus), MP4/M4A, and WavPack tags
using mutagen.  Returns a dict of normalised metadata fields.

This runs in the library scanner's process pool alongside Essentia,
so it must be importable without app-level dependencies.
"""

from __future__ import annotations

import logging

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
                val = values[0].strip() if isinstance(values, list) else str(values).strip()
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
