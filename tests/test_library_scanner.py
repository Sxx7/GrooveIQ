"""
GrooveIQ – Tests for the library scanner's failure-marker behaviour.

Regression for the feature added in this commit: when audio analysis
fails *deterministically* for a given file (track too short, unsupported
codec, LOST_SYNC corruption, …), the scanner now persists a row to
``track_features`` carrying the file's hash + the ``analysis_error``
message. The existing hash-based skip check in ``_analyze_file`` then
skips the file on subsequent scans instead of re-attempting it every run.

Transient failures (worker pool shutdown, per-task timeout) never have
``file_hash`` set — those bypass the marker path and remain eligible for
retry on the next scan.
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures
from app.workers.library_scanner import _upsert_track_features

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def test_failure_marker_persisted_for_too_short_track():
    """A 'Track too short' failure with file_hash present should land in
    track_features so the hash-cache skip check picks it up next scan."""
    failure_result = {
        "file_path": "/music/Test Artist/Album/01 - Intro.flac",
        "analyzed_at": int(time.time()),
        "analysis_version": "essentia-onnx-v3",
        "file_hash": "deadbeef" * 8,
        "analysis_error": "Track too short (<10 s)",
        "title": "Intro",
        "artist": "Test Artist",
        "album": "Album",
    }

    async with _TestSession() as session:
        await _upsert_track_features(session, failure_result)
        await session.commit()

        row = (
            await session.execute(
                select(TrackFeatures).where(TrackFeatures.file_path == failure_result["file_path"])
            )
        ).scalar_one()

    assert row.analysis_error == "Track too short (<10 s)"
    assert row.file_hash == "deadbeef" * 8
    assert row.analysis_version == "essentia-onnx-v3"
    assert row.embedding is None  # no audio data was extracted
    assert row.bpm is None
    # Metadata reads succeed even before audio analysis fails — preserved.
    assert row.title == "Intro"


async def test_failure_marker_appears_in_hash_cache_query():
    """The scanner's pre-load query (file_hash IS NOT NULL) must include
    failure-marker rows so the next scan skips them via the cache."""
    failure_result = {
        "file_path": "/music/Test/short.flac",
        "analyzed_at": int(time.time()),
        "analysis_version": "essentia-onnx-v3",
        "file_hash": "abcd1234" * 8,
        "analysis_error": "Track too short (<10 s)",
    }

    async with _TestSession() as session:
        await _upsert_track_features(session, failure_result)
        await session.commit()

        # Mirror the scanner's pre-load query in app/workers/library_scanner.py.
        rows = (
            await session.execute(
                select(
                    TrackFeatures.file_path,
                    TrackFeatures.file_hash,
                    TrackFeatures.analysis_version,
                ).where(TrackFeatures.file_hash.isnot(None))
            )
        ).all()

    by_path = {r[0]: (r[1], r[2]) for r in rows}
    assert "/music/Test/short.flac" in by_path
    cached_hash, cached_version = by_path["/music/Test/short.flac"]
    assert cached_hash == "abcd1234" * 8
    assert cached_version == "essentia-onnx-v3"


async def test_existing_successful_row_not_clobbered_by_failure():
    """A row that previously analysed successfully shouldn't have its
    BPM / embedding fields overwritten when a later failure marker upsert
    fires for the same file (the failure dict simply doesn't carry those
    keys, so setattr() never touches them)."""
    success_result = {
        "file_path": "/music/Test/track.flac",
        "analyzed_at": int(time.time()),
        "analysis_version": "essentia-onnx-v3",
        "file_hash": "good_hash_aaaa" + "0" * 50,
        "bpm": 128.0,
        "energy": 0.7,
        "embedding": "ZmFrZS1lbWJlZGRpbmc=",  # base64 of "fake-embedding"
        "title": "Banger",
    }

    async with _TestSession() as session:
        await _upsert_track_features(session, success_result)
        await session.commit()

    failure_result = {
        "file_path": "/music/Test/track.flac",
        "analyzed_at": int(time.time()) + 60,
        "analysis_version": "essentia-onnx-v3",
        "file_hash": "new_hash_bbbb" + "0" * 51,
        "analysis_error": "Track too short (<10 s)",
    }

    async with _TestSession() as session:
        await _upsert_track_features(session, failure_result)
        await session.commit()

        row = (
            await session.execute(select(TrackFeatures).where(TrackFeatures.file_path == failure_result["file_path"]))
        ).scalar_one()

    # Failure metadata applied
    assert row.analysis_error == "Track too short (<10 s)"
    assert row.file_hash == "new_hash_bbbb" + "0" * 51
    # Prior good audio data preserved (failure dict didn't carry these keys)
    assert row.bpm == 128.0
    assert row.energy == 0.7
    assert row.embedding == "ZmFrZS1lbWJlZGRpbmc="
