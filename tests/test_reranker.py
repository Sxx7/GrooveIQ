"""
GrooveIQ – Tests for reranker (Phase 4, Step 8).

Tests artist diversity, anti-repetition, skip suppression, and freshness boost.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures, TrackInteraction

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


def _now() -> int:
    return int(time.time())


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _setup_tracks_and_interactions():
    """
    Create 20 tracks from 5 artists (4 tracks each) and interactions for user0.
    Artists determined by file path: /music/artistX/album/trackY.mp3
    """
    now = _now()
    async with _TestSession() as session:
        for i in range(20):
            artist = f"artist{i // 4}"
            session.add(TrackFeatures(
                track_id=f"t{i}",
                file_path=f"/music/{artist}/album1/track{i}.mp3",
                bpm=120.0 + i,
                energy=0.5 + i * 0.03,
                analyzed_at=now,
                analysis_version="1",
            ))

        # user0 has interactions with first 12 tracks.
        for i in range(12):
            session.add(TrackInteraction(
                user_id="user0",
                track_id=f"t{i}",
                play_count=3,
                skip_count=0,
                like_count=0,
                dislike_count=0,
                repeat_count=0,
                playlist_add_count=0,
                queue_add_count=0,
                early_skip_count=3 if i == 2 else 0,  # t2 heavily skipped
                mid_skip_count=0,
                full_listen_count=3,
                total_seekfwd=0,
                total_seekbk=0,
                satisfaction_score=0.7,
                last_event_id=i + 1,
                first_played_at=now - 86_400 * 3,
                # t0: played 1 min ago (anti-repetition); others: played 1 week ago (safe)
                last_played_at=now - 60 if i == 0 else now - 86_400 * 7,
                updated_at=now,
            ))
        await session.commit()


class TestReranker:

    async def test_artist_diversity(self):
        """No more than 2 tracks from the same artist in top 10."""
        from app.services.reranker import rerank

        await _setup_tracks_and_interactions()

        # 20 tracks from 5 artists. Skip t0 (recently played).
        # artist0=t1,t2,t3 | artist1=t4,t5,t6,t7 | artist2=t8..t11 | artist3=t12..t15 | artist4=t16..t19
        ranked = [(f"t{i}", 1.0 - i * 0.005) for i in range(1, 20)]

        async with _TestSession() as session:
            result = await rerank(ranked, "user0", session)

        # In the top 10, count tracks per artist.
        top_10 = result[:10]
        artist_counts = {}
        for tid, _ in top_10:
            idx = int(tid[1:])
            artist = f"artist{idx // 4}"
            artist_counts[artist] = artist_counts.get(artist, 0) + 1

        for artist, count in artist_counts.items():
            assert count <= 2, f"{artist} has {count} tracks in top 10"

    async def test_recently_played_suppressed(self):
        """Tracks played in the last 2 hours are removed."""
        from app.services.reranker import rerank

        await _setup_tracks_and_interactions()
        # t0 was played 1 minute ago.
        ranked = [("t0", 0.99), ("t3", 0.95), ("t5", 0.90)]

        async with _TestSession() as session:
            result = await rerank(ranked, "user0", session)

        result_ids = [tid for tid, _ in result]
        assert "t0" not in result_ids

    async def test_freshness_boost(self):
        """Never-played tracks get a score uplift."""
        from app.services.reranker import rerank

        await _setup_tracks_and_interactions()
        # t10, t11 have no interactions for user0.
        ranked = [("t3", 0.80), ("t10", 0.78), ("t5", 0.75)]

        async with _TestSession() as session:
            result = await rerank(ranked, "user0", session)

        # t10 (never played) should get boosted above t5 (same base score area).
        result_map = {tid: score for tid, score in result}
        assert "t10" in result_map
        # Freshness boost: 0.78 * 1.10 = 0.858 > 0.75
        assert result_map["t10"] > result_map.get("t5", 0)

    async def test_skip_suppression(self):
        """Tracks early-skipped >2 times in 24h get demoted."""
        from app.services.reranker import rerank

        await _setup_tracks_and_interactions()
        # t2 has early_skip_count=3, last_played_at=now-7d.
        # Update to within 24h but outside 2h anti-repetition window.
        async with _TestSession() as session:
            from sqlalchemy import update
            await session.execute(
                update(TrackInteraction)
                .where(
                    TrackInteraction.user_id == "user0",
                    TrackInteraction.track_id == "t2",
                )
                .values(last_played_at=_now() - 10_800)  # 3h ago (within 24h, outside 2h)
            )
            await session.commit()

        ranked = [("t2", 0.90), ("t5", 0.80), ("t7", 0.70)]

        async with _TestSession() as session:
            result = await rerank(ranked, "user0", session)

        result_map = {tid: score for tid, score in result}
        # t2 should be demoted: 0.90 * 0.5 = 0.45 < t5's 0.80.
        assert result_map["t2"] < result_map["t5"]

    async def test_empty_input(self):
        """Empty input returns empty output."""
        from app.services.reranker import rerank

        async with _TestSession() as session:
            result = await rerank([], "user0", session)
        assert result == []
