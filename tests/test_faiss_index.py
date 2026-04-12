"""
GrooveIQ – Tests for FAISS index service (Phase 4, Step 4).

Tests index build, search, rebuild atomicity, and edge cases.
"""

from __future__ import annotations

import base64
import time

import numpy as np
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


def _make_embedding(seed: int = 0) -> str:
    """Create a deterministic 64-dim embedding encoded as base64 float32."""
    rng = np.random.RandomState(seed)
    vec = rng.randn(64).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return base64.b64encode(vec.tobytes()).decode()


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr("app.services.faiss_index.AsyncSessionLocal", _TestSession)

    yield

    # Reset singleton state after each test.
    import app.services.faiss_index as fi
    fi._index = None
    fi._id_to_track = []
    fi._track_to_id = {}
    fi._embeddings = None

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert_tracks(n: int, seed_offset: int = 0) -> list[str]:
    """Insert n tracks with random embeddings. Returns track IDs."""
    track_ids = []
    async with _TestSession() as session:
        for i in range(n):
            tid = f"track_{i + seed_offset}"
            session.add(TrackFeatures(
                track_id=tid,
                file_path=f"/music/{tid}.mp3",
                bpm=120.0 + i,
                energy=0.5,
                embedding=_make_embedding(i + seed_offset),
                analyzed_at=int(time.time()),
                analysis_version="1",
            ))
            track_ids.append(tid)
        await session.commit()
    return track_ids


class TestFaissIndex:

    async def test_build_index(self):
        """Index builds successfully with valid embeddings."""
        from app.services.faiss_index import build_index, index_size, is_ready

        await _insert_tracks(10)
        count = await build_index()
        assert count == 10
        assert is_ready()
        assert index_size() == 10

    async def test_search_returns_nearest(self):
        """Search returns correct nearest neighbours."""
        from app.services.faiss_index import build_index, search_by_track_id

        await _insert_tracks(20)
        await build_index()

        results = search_by_track_id("track_0", k=5)
        assert len(results) == 5
        # Results should be (track_id, score) tuples.
        for tid, score in results:
            assert isinstance(tid, str)
            assert isinstance(score, float)
            assert tid != "track_0"  # seed excluded

    async def test_search_with_exclude(self):
        """Excluded track IDs are filtered from results."""
        from app.services.faiss_index import build_index, search_by_track_id

        await _insert_tracks(10)
        await build_index()

        exclude = {"track_1", "track_2", "track_3"}
        results = search_by_track_id("track_0", k=5, exclude_ids=exclude)
        result_ids = {tid for tid, _ in results}
        assert not result_ids.intersection(exclude)

    async def test_rebuild_swaps_atomically(self):
        """Rebuild replaces the index without breaking concurrent reads."""
        from app.services.faiss_index import build_index, index_size, search_by_track_id

        await _insert_tracks(5)
        await build_index()
        assert index_size() == 5

        # Add more tracks and rebuild.
        await _insert_tracks(5, seed_offset=100)
        await build_index()
        assert index_size() == 10

        # Old tracks still searchable.
        results = search_by_track_id("track_0", k=3)
        assert len(results) == 3

    async def test_empty_library(self):
        """Empty library produces no errors, index is not ready."""
        from app.services.faiss_index import build_index, index_size, is_ready

        count = await build_index()
        assert count == 0
        assert not is_ready()
        assert index_size() == 0

    async def test_missing_embeddings_skipped(self):
        """Tracks without embeddings are skipped during build."""
        from app.services.faiss_index import build_index

        await _insert_tracks(3)
        # Add a track with no embedding.
        async with _TestSession() as session:
            session.add(TrackFeatures(
                track_id="no_emb",
                file_path="/music/no_emb.mp3",
                bpm=100.0,
                energy=0.5,
                embedding=None,
                analyzed_at=int(time.time()),
                analysis_version="1",
            ))
            await session.commit()

        count = await build_index()
        assert count == 3  # only tracks with embeddings

    async def test_get_centroid(self):
        """Centroid of multiple tracks returns a valid vector."""
        from app.services.faiss_index import build_index, get_centroid

        await _insert_tracks(5)
        await build_index()

        centroid = get_centroid(["track_0", "track_1", "track_2"])
        assert centroid is not None
        assert centroid.shape == (64,)
        # Should be normalised.
        assert abs(np.linalg.norm(centroid) - 1.0) < 1e-5

    async def test_centroid_unknown_tracks(self):
        """Centroid returns None when none of the tracks are in the index."""
        from app.services.faiss_index import build_index, get_centroid

        await _insert_tracks(3)
        await build_index()

        centroid = get_centroid(["unknown_1", "unknown_2"])
        assert centroid is None

    async def test_search_before_build(self):
        """Search before index is built returns empty list."""
        from app.services.faiss_index import search_by_track_id

        results = search_by_track_id("track_0", k=5)
        assert results == []
