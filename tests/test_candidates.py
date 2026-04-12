"""
GrooveIQ – Tests for candidate generation (Phase 4, Steps 4+5).

Tests FAISS content candidates, CF candidates, merged retrieval,
cold-start handling, and disliked track filtering.
"""

from __future__ import annotations

import base64
import time

import numpy as np
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures, TrackInteraction, User

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


def _make_embedding(seed: int = 0) -> str:
    rng = np.random.RandomState(seed)
    vec = rng.randn(64).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return base64.b64encode(vec.tobytes()).decode()


def _now() -> int:
    return int(time.time())


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr("app.services.faiss_index.AsyncSessionLocal", _TestSession)
    monkeypatch.setattr("app.services.candidate_gen.AsyncSessionLocal", _TestSession)
    monkeypatch.setattr("app.services.collab_filter.AsyncSessionLocal", _TestSession)

    yield

    # Reset singletons.
    import app.services.faiss_index as fi
    fi._index = None
    fi._id_to_track = []
    fi._track_to_id = {}
    fi._embeddings = None

    import app.services.collab_filter as cf
    cf._model = None
    cf._user_to_idx = {}
    cf._idx_to_user = []
    cf._track_to_idx = {}
    cf._idx_to_track = []
    cf._interaction_matrix = None

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _setup_library(n_tracks: int = 20) -> list[str]:
    """Insert tracks with embeddings and build FAISS index."""
    track_ids = []
    async with _TestSession() as session:
        for i in range(n_tracks):
            tid = f"t{i}"
            session.add(TrackFeatures(
                track_id=tid,
                file_path=f"/music/artist{i % 3}/{tid}.mp3",
                bpm=100.0 + i * 2,
                energy=0.3 + (i % 10) * 0.07,
                embedding=_make_embedding(i),
                analyzed_at=_now(),
                analysis_version="1",
            ))
            track_ids.append(tid)
        await session.commit()

    from app.services.faiss_index import build_index
    await build_index()
    return track_ids


async def _setup_user(user_id: str, track_ids: list[str], n_interactions: int = 10) -> None:
    """Create user with interactions and a taste profile."""
    now = _now()
    async with _TestSession() as session:
        top_tracks = [
            {"track_id": tid, "score": round(0.9 - i * 0.05, 2)}
            for i, tid in enumerate(track_ids[:min(n_interactions, len(track_ids))])
        ]
        session.add(User(
            user_id=user_id,
            last_seen=now,
            taste_profile={"top_tracks": top_tracks, "updated_at": now},
        ))
        for i, tid in enumerate(track_ids[:n_interactions]):
            session.add(TrackInteraction(
                user_id=user_id,
                track_id=tid,
                play_count=5 - i % 3,
                skip_count=0,
                like_count=1 if i < 3 else 0,
                dislike_count=0,
                repeat_count=0,
                playlist_add_count=0,
                queue_add_count=0,
                early_skip_count=0,
                mid_skip_count=0,
                full_listen_count=3,
                total_seekfwd=0,
                total_seekbk=0,
                satisfaction_score=0.8 - i * 0.05,
                last_event_id=i + 1,
                first_played_at=now - 86_400,
                last_played_at=now - 3600,
                updated_at=now,
            ))
        await session.commit()


class TestContentCandidates:

    async def test_content_candidates_from_seed(self):
        """Content candidates from a seed track via FAISS."""
        from app.services.candidate_gen import get_content_candidates

        await _setup_library(20)
        results = await get_content_candidates("t0", k=5)
        assert len(results) == 5
        for r in results:
            assert r["source"] == "content"
            assert r["track_id"] != "t0"

    async def test_content_candidates_for_user(self):
        """Content candidates from user's taste profile centroid."""
        from app.services.candidate_gen import get_content_candidates_for_user

        track_ids = await _setup_library(20)
        await _setup_user("alice", track_ids, n_interactions=5)

        results = await get_content_candidates_for_user("alice", k=10)
        assert len(results) > 0
        for r in results:
            assert r["source"] == "content_profile"

    async def test_content_candidates_unknown_user(self):
        """Unknown user gets empty content candidates."""
        from app.services.candidate_gen import get_content_candidates_for_user

        await _setup_library(10)
        results = await get_content_candidates_for_user("nobody", k=10)
        assert results == []


class TestCFCandidates:

    async def test_cf_build_and_recommend(self):
        """CF model trains and returns recommendations."""
        track_ids = await _setup_library(20)

        # Need at least 2 users for CF.
        await _setup_user("alice", track_ids[:10], n_interactions=10)
        await _setup_user("bob", track_ids[5:15], n_interactions=10)

        from app.services.collab_filter import build_model, get_cf_candidates, is_ready
        result = await build_model()
        assert result["trained"] is True
        assert is_ready()

        recs = get_cf_candidates("alice", k=5)
        assert len(recs) > 0
        for tid, score in recs:
            assert isinstance(tid, str)
            assert isinstance(score, float)

    async def test_cf_cold_start_user(self):
        """Unknown user gets empty CF results."""
        from app.services.collab_filter import get_cf_candidates
        results = get_cf_candidates("unknown_user", k=5)
        assert results == []

    async def test_cf_too_few_interactions(self):
        """CF skips training with too few interactions."""
        from app.services.collab_filter import build_model
        result = await build_model()
        assert result["trained"] is False

    async def test_cf_single_user(self):
        """CF skips when there's only one user."""
        track_ids = await _setup_library(10)
        await _setup_user("alone", track_ids[:5], n_interactions=5)

        # Need to insert enough interactions (>=10).
        async with _TestSession() as session:
            for i in range(5, 10):
                session.add(TrackInteraction(
                    user_id="alone",
                    track_id=f"t{i}",
                    play_count=2,
                    skip_count=0,
                    like_count=0,
                    dislike_count=0,
                    repeat_count=0,
                    playlist_add_count=0,
                    queue_add_count=0,
                    early_skip_count=0,
                    mid_skip_count=0,
                    full_listen_count=2,
                    total_seekfwd=0,
                    total_seekbk=0,
                    satisfaction_score=0.5,
                    last_event_id=100 + i,
                    updated_at=_now(),
                ))
            await session.commit()

        from app.services.collab_filter import build_model
        result = await build_model()
        assert result["trained"] is False  # single user → meaningless

    async def test_cf_similar_items(self):
        """Item-item CF returns similar tracks."""
        track_ids = await _setup_library(20)
        await _setup_user("alice", track_ids[:10], n_interactions=10)
        await _setup_user("bob", track_ids[5:15], n_interactions=10)

        from app.services.collab_filter import build_model, get_similar_items
        await build_model()

        results = get_similar_items("t5", k=3)
        assert len(results) > 0
        for tid, score in results:
            assert tid != "t5"


class TestMergedRetrieval:

    async def test_merged_candidates_dedupe(self):
        """Merged retrieval deduplicates across sources."""
        track_ids = await _setup_library(20)
        await _setup_user("alice", track_ids[:10], n_interactions=10)

        from app.services.candidate_gen import get_candidates
        candidates = await get_candidates("alice", k=15)

        # Check no duplicates.
        seen = set()
        for c in candidates:
            assert c["track_id"] not in seen
            seen.add(c["track_id"])

    async def test_disliked_tracks_filtered(self):
        """Tracks the user disliked are excluded from candidates."""
        track_ids = await _setup_library(20)
        await _setup_user("alice", track_ids[:10], n_interactions=10)

        # Mark a track as disliked.
        async with _TestSession() as session:
            from sqlalchemy import update
            await session.execute(
                update(TrackInteraction)
                .where(
                    TrackInteraction.user_id == "alice",
                    TrackInteraction.track_id == "t0",
                )
                .values(dislike_count=1)
            )
            await session.commit()

        from app.services.candidate_gen import get_candidates
        candidates = await get_candidates("alice", k=50)
        candidate_ids = {c["track_id"] for c in candidates}
        assert "t0" not in candidate_ids

    async def test_cold_start_user_gets_content_only(self):
        """A user with no CF data still gets content-based candidates."""
        track_ids = await _setup_library(20)
        await _setup_user("newuser", track_ids[:3], n_interactions=3)

        from app.services.candidate_gen import get_candidates
        candidates = await get_candidates("newuser", k=10)
        # Should still get results (from content profile or popular/artist recall).
        assert len(candidates) > 0

    async def test_seed_track_biases_content(self):
        """Providing a seed track uses that for content retrieval."""
        track_ids = await _setup_library(20)
        await _setup_user("alice", track_ids[:10], n_interactions=10)

        from app.services.candidate_gen import get_candidates
        candidates = await get_candidates("alice", seed_track_id="t5", k=10)
        # Should have content-sourced candidates.
        sources = {c["source"] for c in candidates}
        assert "content" in sources

    async def test_candidates_have_source_tags(self):
        """Every candidate has a source tag for debugging."""
        track_ids = await _setup_library(20)
        await _setup_user("alice", track_ids[:10], n_interactions=10)

        from app.services.candidate_gen import get_candidates
        candidates = await get_candidates("alice", k=10)
        for c in candidates:
            assert "source" in c
            assert c["source"] in ("content", "content_profile", "cf", "popular", "artist_recall")
