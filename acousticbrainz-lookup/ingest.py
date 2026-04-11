"""AcousticBrainz data dump ingestion pipeline.

Downloads, decompresses, parses, and loads AcousticBrainz high-level JSON
dumps into a local SQLite database. Supports resumability and sample mode.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import tarfile
import time
from pathlib import Path

import httpx
import zstandard as zstd

logger = logging.getLogger("ab-ingest")

DB_PATH = Path("/data/acousticbrainz.db")
TMP_DIR = Path("/data/tmp")

BASE_URL = "https://data.metabrainz.org/pub/musicbrainz/acousticbrainz/dumps"
FULL_DIR = "acousticbrainz-highlevel-json-20220623"
SAMPLE_DIR = "acousticbrainz-sample-json-20220623"

FULL_ARCHIVES = [
    f"acousticbrainz-highlevel-json-20220623-{i}.tar.zst" for i in range(30)
]
SAMPLE_ARCHIVES = [
    "acousticbrainz-highlevel-sample-json-20220623-0.tar.zst",
]

BATCH_SIZE = 5000

CREATE_TRACKS = """
CREATE TABLE IF NOT EXISTS tracks (
    mbid              TEXT PRIMARY KEY,
    artist            TEXT,
    title             TEXT,
    album             TEXT,
    bpm               REAL,
    key               TEXT,
    mode              TEXT,
    key_strength      REAL,
    loudness          REAL,
    danceability      REAL,
    instrumentalness  REAL,
    mood_happy        REAL,
    mood_sad          REAL,
    mood_aggressive   REAL,
    mood_relaxed      REAL,
    mood_party        REAL,
    mood_acoustic     REAL,
    mood_electronic   REAL,
    energy            REAL,
    valence           REAL,
    genre_dortmund    TEXT,
    genre_rosamerica  TEXT,
    genre_tags        TEXT,
    mb_artist_id      TEXT,
    mb_album_id       TEXT
)
"""

CREATE_STATE = """
CREATE TABLE IF NOT EXISTS ingestion_state (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""

INSERT_TRACK = """
INSERT OR IGNORE INTO tracks (
    mbid, artist, title, album, bpm, key, mode, key_strength, loudness,
    danceability, instrumentalness, mood_happy, mood_sad, mood_aggressive,
    mood_relaxed, mood_party, mood_acoustic, mood_electronic, energy, valence,
    genre_dortmund, genre_rosamerica, genre_tags, mb_artist_id, mb_album_id
) VALUES (
    :mbid, :artist, :title, :album, :bpm, :key, :mode, :key_strength, :loudness,
    :danceability, :instrumentalness, :mood_happy, :mood_sad, :mood_aggressive,
    :mood_relaxed, :mood_party, :mood_acoustic, :mood_electronic, :energy, :valence,
    :genre_dortmund, :genre_rosamerica, :genre_tags, :mb_artist_id, :mb_album_id
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_bpm ON tracks(bpm)",
    "CREATE INDEX IF NOT EXISTS idx_energy ON tracks(energy)",
    "CREATE INDEX IF NOT EXISTS idx_danceability ON tracks(danceability)",
    "CREATE INDEX IF NOT EXISTS idx_valence ON tracks(valence)",
    "CREATE INDEX IF NOT EXISTS idx_mood_happy ON tracks(mood_happy)",
    "CREATE INDEX IF NOT EXISTS idx_mood_relaxed ON tracks(mood_relaxed)",
    "CREATE INDEX IF NOT EXISTS idx_mood_aggressive ON tracks(mood_aggressive)",
    "CREATE INDEX IF NOT EXISTS idx_instrumentalness ON tracks(instrumentalness)",
    "CREATE INDEX IF NOT EXISTS idx_mood_acoustic ON tracks(mood_acoustic)",
    "CREATE INDEX IF NOT EXISTS idx_genre_dortmund ON tracks(genre_dortmund)",
    "CREATE INDEX IF NOT EXISTS idx_artist ON tracks(artist)",
]


def _get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM ingestion_state WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingestion_state (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def _extract_mbid_from_path(path: str) -> str | None:
    """Extract MBID from tar entry path like 'type/mb/id/mbid-N.json'."""
    name = Path(path).stem  # e.g. "abcd1234-...-0"
    # Strip trailing submission number (-0, -1, etc.)
    parts = name.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        # Reconstruct UUID: the base is the MBID
        candidate = parts[0]
    else:
        candidate = name
    # Basic UUID check (36 chars with hyphens)
    if len(candidate) == 36 and candidate.count("-") == 4:
        return candidate
    return None


def _parse_entry(data: dict, mbid: str) -> dict:
    """Extract fields from a high-level JSON entry."""
    hl = data.get("highlevel") or {}
    meta = data.get("metadata") or {}
    tags = meta.get("tags") or {}

    # Low-level fields are present in the full highlevel dump but may be absent
    # in the sample dump or some entries
    rhythm = data.get("rhythm") or {}
    tonal = data.get("tonal") or {}
    lowlevel = data.get("lowlevel") or {}

    bpm = rhythm.get("bpm")
    loudness = lowlevel.get("average_loudness")
    mood_aggressive = (hl.get("mood_aggressive") or {}).get("all", {}).get("aggressive")

    # Derived energy — use available signals, fall back gracefully
    norm_bpm = max(0.0, min(1.0, ((bpm or 120) - 60) / 140)) if bpm else 0.43
    loud_val = loudness if loudness is not None else 0.3
    aggr_val = mood_aggressive if mood_aggressive is not None else 0.2
    energy = max(0.0, min(1.0, 0.3 * norm_bpm + 0.4 * loud_val + 0.3 * aggr_val))

    mood_happy = hl.get("mood_happy", {}).get("all", {}).get("happy")

    return {
        "mbid": mbid,
        "artist": (tags.get("artist") or [None])[0],
        "title": (tags.get("title") or [None])[0],
        "album": (tags.get("album") or [None])[0],
        "bpm": bpm,
        "key": tonal.get("key_key"),
        "mode": tonal.get("key_scale"),
        "key_strength": tonal.get("key_strength"),
        "loudness": loudness,
        "danceability": (hl.get("danceability") or {}).get("all", {}).get("danceable"),
        "instrumentalness": (hl.get("voice_instrumental") or {}).get("all", {}).get("instrumental"),
        "mood_happy": mood_happy,
        "mood_sad": (hl.get("mood_sad") or {}).get("all", {}).get("sad"),
        "mood_aggressive": mood_aggressive,
        "mood_relaxed": (hl.get("mood_relaxed") or {}).get("all", {}).get("relaxed"),
        "mood_party": (hl.get("mood_party") or {}).get("all", {}).get("party"),
        "mood_acoustic": (hl.get("mood_acoustic") or {}).get("all", {}).get("acoustic"),
        "mood_electronic": (hl.get("mood_electronic") or {}).get("all", {}).get("electronic"),
        "energy": energy,
        "valence": mood_happy,
        "genre_dortmund": (hl.get("genre_dortmund") or {}).get("value"),
        "genre_rosamerica": (hl.get("genre_rosamerica") or {}).get("value"),
        "genre_tags": ",".join(tags.get("genre", [])),
        "mb_artist_id": (tags.get("musicbrainz_artistid") or [None])[0],
        "mb_album_id": (tags.get("musicbrainz_albumid") or [None])[0],
    }


def _process_archive(
    conn: sqlite3.Connection,
    archive_path: Path,
    progress_cb: callable | None = None,
) -> int:
    """Decompress and parse a single .tar.zst archive, inserting tracks."""
    dctx = zstd.ZstdDecompressor()
    inserted = 0
    batch: list[dict] = []

    with open(archive_path, "rb") as fh:
        with dctx.stream_reader(fh) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                for member in tar:
                    if not member.isfile() or not member.name.endswith(".json"):
                        continue

                    mbid = _extract_mbid_from_path(member.name)
                    if not mbid:
                        continue

                    f = tar.extractfile(member)
                    if f is None:
                        continue

                    try:
                        data = json.loads(f.read())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

                    track = _parse_entry(data, mbid)
                    batch.append(track)

                    if len(batch) >= BATCH_SIZE:
                        conn.executemany(INSERT_TRACK, batch)
                        conn.commit()
                        inserted += len(batch)
                        if progress_cb:
                            progress_cb(inserted)
                        batch.clear()

    if batch:
        conn.executemany(INSERT_TRACK, batch)
        conn.commit()
        inserted += len(batch)

    return inserted


def get_status(conn: sqlite3.Connection) -> dict:
    """Return current ingestion status for the health endpoint."""
    status = _get_state(conn, "status") or "pending"
    total = _get_state(conn, "total_tracks") or "0"
    last = _get_state(conn, "last_archive") or ""
    return {
        "status": status,
        "total_tracks": int(total),
        "last_archive": last,
    }


def run_ingestion(
    sample_mode: bool = False,
    progress_cb: callable | None = None,
) -> None:
    """Main ingestion entry point. Blocks until complete."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.Connection(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(CREATE_TRACKS)
    conn.execute(CREATE_STATE)
    conn.commit()

    # Check if already completed
    if _get_state(conn, "status") == "completed":
        total = _get_state(conn, "total_tracks") or "0"
        logger.info("Ingestion already completed (%s tracks), skipping", total)
        conn.close()
        return

    archives = SAMPLE_ARCHIVES if sample_mode else FULL_ARCHIVES
    base_dir = SAMPLE_DIR if sample_mode else FULL_DIR

    # Find which archives have been processed
    processed_raw = _get_state(conn, "processed_archives") or ""
    processed = set(processed_raw.split(",")) if processed_raw else set()

    _set_state(conn, "status", "running")
    _set_state(conn, "started_at", str(int(time.time())))

    total_inserted = int(_get_state(conn, "total_tracks") or "0")

    for archive_name in archives:
        if archive_name in processed:
            logger.info("Skipping already-processed archive: %s", archive_name)
            continue

        url = f"{BASE_URL}/{base_dir}/{archive_name}"
        archive_path = TMP_DIR / archive_name

        # Download
        logger.info("Downloading %s ...", archive_name)
        with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client:
            with client.stream("GET", url, follow_redirects=True) as resp:
                resp.raise_for_status()
                with open(archive_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)

        logger.info("Processing %s ...", archive_name)
        count = _process_archive(conn, archive_path, progress_cb)
        total_inserted += count
        logger.info("Inserted %d tracks from %s (total: %d)", count, archive_name, total_inserted)

        # Update state
        processed.add(archive_name)
        _set_state(conn, "processed_archives", ",".join(processed))
        _set_state(conn, "last_archive", archive_name)
        _set_state(conn, "total_tracks", str(total_inserted))

        # Clean up archive
        archive_path.unlink(missing_ok=True)

    # Build indexes after all data is loaded
    logger.info("Building indexes ...")
    for idx_sql in INDEXES:
        conn.execute(idx_sql)
    conn.commit()

    _set_state(conn, "status", "completed")
    _set_state(conn, "completed_at", str(int(time.time())))
    _set_state(conn, "total_tracks", str(total_inserted))

    logger.info("Ingestion complete: %d tracks", total_inserted)

    # Clean up tmp dir
    for f in TMP_DIR.iterdir():
        f.unlink(missing_ok=True)
    TMP_DIR.rmdir()

    conn.close()
