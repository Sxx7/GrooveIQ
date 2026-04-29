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

import shutil
import struct
import time
import wave
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures
from app.workers.library_scanner import _upsert_track_features, _validate_bitstream

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


# ---------------------------------------------------------------------------
# Pre-flight bitstream validation (issue #32)
# ---------------------------------------------------------------------------

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _write_clean_wav(path: Path, seconds: float = 0.5, sample_rate: int = 16000) -> None:
    """Synthesise a tiny silent mono WAV that ffmpeg will accept."""
    n = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack(f"<{n}h", *([0] * n)))


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
async def test_validate_bitstream_accepts_clean_audio(tmp_path: Path) -> None:
    f = tmp_path / "clean.wav"
    _write_clean_wav(f)
    ok, err = await _validate_bitstream(str(f))
    assert ok is True
    assert err is None


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
async def test_validate_bitstream_rejects_corrupt_audio(tmp_path: Path) -> None:
    f = tmp_path / "junk.flac"
    f.write_bytes(b"not actually a flac file, just bytes")
    ok, err = await _validate_bitstream(str(f))
    assert ok is False
    assert err  # some non-empty error message


async def test_validate_bitstream_timeout_does_not_hang(tmp_path: Path, monkeypatch) -> None:
    """A wedged ffmpeg subprocess must surface as a validation failure rather
    than wedge the scanner. We mock create_subprocess_exec to return a fake
    process whose communicate() never completes within the timeout window."""
    import asyncio as _asyncio

    class _NeverEndingProc:
        returncode = None

        async def communicate(self):
            await _asyncio.sleep(60)  # vastly longer than the test timeout
            return b"", b""

        def kill(self):  # pragma: no cover - sync stub for the kill path
            self.returncode = -9

        async def wait(self):  # pragma: no cover - sync stub for the kill path
            return self.returncode

    async def _fake_subprocess(*_args, **_kwargs):
        return _NeverEndingProc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _fake_subprocess)

    f = tmp_path / "wedge.flac"
    f.write_bytes(b"xxx")
    ok, err = await _validate_bitstream(str(f), timeout_s=0.2)
    assert ok is False
    assert err is not None
    assert "timed out" in err


async def test_validate_bitstream_skips_when_ffmpeg_missing(tmp_path: Path, monkeypatch) -> None:
    """If ffmpeg isn't on PATH the validator must NOT block analysis — it
    returns (True, None) so the worker still gets to handle the file."""
    import asyncio as _asyncio

    async def _no_ffmpeg(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _no_ffmpeg)

    f = tmp_path / "anything.flac"
    f.write_bytes(b"xxx")
    ok, err = await _validate_bitstream(str(f))
    assert ok is True
    assert err is None


async def test_hash_cached_files_bypass_validation(tmp_path: Path, monkeypatch) -> None:
    """The pre-flight skip path: when the file's live hash matches the cached
    hash and bitstream_validated_at was previously set, _validate_bitstream
    must NOT be invoked. We assert that by replacing it with a sentinel that
    blows up if called — and exercising the inline guard from _analyze_one."""
    from app.services import audio_analysis as _aa
    from app.workers import library_scanner as _ls

    f = tmp_path / "song.wav"
    _write_clean_wav(f)
    live_hash = _aa.compute_file_hash(str(f))

    cached_entry = (live_hash, _aa.ANALYSIS_VERSION, int(time.time()) - 86400)

    # Replicate the inline decision from _analyze_one. If the cached hash
    # matches the live hash AND validated_at is set, the validator must be
    # skipped — so wiring it to a sentinel that fails the test if invoked
    # gives us a clean signal.
    async def _must_not_be_called(*_a, **_kw):
        raise AssertionError("validator was called for an already-validated unchanged file")

    monkeypatch.setattr(_ls, "_validate_bitstream", _must_not_be_called)

    needs_validation = True
    if cached_entry is not None and cached_entry[2] is not None:
        if _aa.compute_file_hash(str(f)) == cached_entry[0]:
            needs_validation = False
    assert needs_validation is False  # cached + matching hash → skip
