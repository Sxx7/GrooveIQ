"""
GrooveIQ – Tests for Phase 2 recommendation pipeline.

Tests the sessionizer, track scoring, and taste profile services
using in-memory SQLite.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import (
    Base,
    ListenEvent,
    ListenSession,
    TrackFeatures,
    TrackInteraction,
    User,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    """Create tables and patch AsyncSessionLocal for each test."""
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Patch the session factory used by the services.
    monkeypatch.setattr("app.services.sessionizer.AsyncSessionLocal", _TestSession)
    monkeypatch.setattr("app.services.track_scoring.AsyncSessionLocal", _TestSession)
    monkeypatch.setattr("app.services.taste_profile.AsyncSessionLocal", _TestSession)

    yield

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _now() -> int:
    return int(time.time())


async def _insert_events(events: list[dict]) -> None:
    """Insert raw ListenEvent rows."""
    async with _TestSession() as session:
        for ev in events:
            ev.setdefault("timestamp", _now())
            session.add(ListenEvent(**ev))
        await session.commit()


async def _insert_user(user_id: str) -> None:
    async with _TestSession() as session:
        session.add(User(user_id=user_id, last_seen=_now()))
        await session.commit()


async def _insert_track_features(track_id: str, **kwargs) -> None:
    async with _TestSession() as session:
        session.add(
            TrackFeatures(
                track_id=track_id,
                file_path=f"/music/{track_id}.mp3",
                bpm=kwargs.get("bpm", 120.0),
                energy=kwargs.get("energy", 0.7),
                danceability=kwargs.get("danceability", 0.6),
                valence=kwargs.get("valence", 0.5),
                acousticness=kwargs.get("acousticness", 0.3),
                instrumentalness=kwargs.get("instrumentalness", 0.1),
                loudness=kwargs.get("loudness", -8.0),
                key=kwargs.get("key", "C"),
                mode=kwargs.get("mode", "major"),
                mood_tags=kwargs.get("mood_tags", [{"label": "happy", "confidence": 0.8}]),
                analyzed_at=_now(),
                analysis_version="1",
            )
        )
        await session.commit()


# =============================================================================
# Sessionizer tests
# =============================================================================


class TestSessionizer:
    async def test_basic_session_creation(self):
        """Events within gap threshold form one session."""
        from app.services.sessionizer import run_sessionizer

        now = _now()
        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "play_start", "timestamp": now},
                {"user_id": "u1", "track_id": "t1", "event_type": "play_end", "timestamp": now + 60, "value": 1.0},
                {"user_id": "u1", "track_id": "t2", "event_type": "play_start", "timestamp": now + 120},
                {"user_id": "u1", "track_id": "t2", "event_type": "play_end", "timestamp": now + 180, "value": 0.9},
            ]
        )

        result = await run_sessionizer()
        assert result["sessions_created"] == 1
        assert result["events_processed"] == 4

        async with _TestSession() as session:
            sessions = (await session.execute(select(ListenSession))).scalars().all()
            assert len(sessions) == 1
            s = sessions[0]
            assert s.user_id == "u1"
            assert s.track_count == 2
            assert s.play_count == 2  # one play_start per track
            assert s.skip_count == 0

    async def test_gap_splits_sessions(self):
        """Events separated by more than SESSION_GAP_MINUTES form separate sessions."""
        from app.services.sessionizer import run_sessionizer

        now = _now()
        gap = 31 * 60  # 31 minutes > default 30 min gap
        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "play_start", "timestamp": now},
                {"user_id": "u1", "track_id": "t1", "event_type": "play_end", "timestamp": now + 60, "value": 1.0},
                {"user_id": "u1", "track_id": "t2", "event_type": "play_start", "timestamp": now + gap},
                {
                    "user_id": "u1",
                    "track_id": "t2",
                    "event_type": "play_end",
                    "timestamp": now + gap + 60,
                    "value": 1.0,
                },
            ]
        )

        result = await run_sessionizer()
        assert result["sessions_created"] == 2

    async def test_client_session_id_respected(self):
        """Events with the same client session_id group together."""
        from app.services.sessionizer import run_sessionizer

        now = _now()
        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "play_start", "timestamp": now, "session_id": "s1"},
                {
                    "user_id": "u1",
                    "track_id": "t2",
                    "event_type": "play_start",
                    "timestamp": now + 10,
                    "session_id": "s1",
                },
                {
                    "user_id": "u1",
                    "track_id": "t3",
                    "event_type": "play_start",
                    "timestamp": now + 20,
                    "session_id": "s2",
                },
                {
                    "user_id": "u1",
                    "track_id": "t3",
                    "event_type": "play_end",
                    "timestamp": now + 80,
                    "session_id": "s2",
                    "value": 1.0,
                },
            ]
        )

        result = await run_sessionizer()
        assert result["sessions_created"] == 2

    async def test_single_event_below_min_skipped(self):
        """Sessions with fewer than SESSION_MIN_EVENTS are dropped."""
        from app.services.sessionizer import run_sessionizer

        now = _now()
        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "play_start", "timestamp": now},
            ]
        )

        result = await run_sessionizer()
        assert result["sessions_created"] == 0

    async def test_incremental_processing(self):
        """Second run only processes new events."""
        from app.services.sessionizer import run_sessionizer

        now = _now()
        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "play_start", "timestamp": now},
                {"user_id": "u1", "track_id": "t1", "event_type": "play_end", "timestamp": now + 60, "value": 1.0},
            ]
        )

        r1 = await run_sessionizer()
        assert r1["sessions_created"] == 1

        r2 = await run_sessionizer()
        assert r2["events_processed"] == 0
        assert r2["sessions_created"] == 0

    async def test_multi_user_sessions(self):
        """Different users get separate sessions."""
        from app.services.sessionizer import run_sessionizer

        now = _now()
        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "play_start", "timestamp": now},
                {"user_id": "u1", "track_id": "t1", "event_type": "play_end", "timestamp": now + 60, "value": 1.0},
                {"user_id": "u2", "track_id": "t1", "event_type": "play_start", "timestamp": now},
                {"user_id": "u2", "track_id": "t1", "event_type": "play_end", "timestamp": now + 60, "value": 1.0},
            ]
        )

        result = await run_sessionizer()
        assert result["sessions_created"] == 2


# =============================================================================
# Track scoring tests
# =============================================================================


class TestTrackScoring:
    async def test_basic_scoring(self):
        """A liked, fully-listened track gets a positive score."""
        from app.services.track_scoring import run_track_scoring

        now = _now()
        await _insert_events(
            [
                {
                    "user_id": "u1",
                    "track_id": "t1",
                    "event_type": "play_end",
                    "timestamp": now,
                    "value": 1.0,
                    "dwell_ms": 180_000,
                },
                {"user_id": "u1", "track_id": "t1", "event_type": "like", "timestamp": now + 1},
            ]
        )

        result = await run_track_scoring()
        assert result["interactions_created"] == 1

        async with _TestSession() as session:
            interactions = (await session.execute(select(TrackInteraction))).scalars().all()
            assert len(interactions) == 1
            i = interactions[0]
            assert i.user_id == "u1"
            assert i.track_id == "t1"
            assert i.full_listen_count == 1
            assert i.like_count == 1
            assert i.satisfaction_score is not None

    async def test_early_skip_negative(self):
        """An early-skipped track gets a lower score than a fully listened one."""
        from app.services.track_scoring import run_track_scoring

        now = _now()
        await _insert_events(
            [
                # Track 1: fully listened + liked
                {
                    "user_id": "u1",
                    "track_id": "t1",
                    "event_type": "play_end",
                    "timestamp": now,
                    "value": 1.0,
                    "dwell_ms": 180_000,
                },
                {"user_id": "u1", "track_id": "t1", "event_type": "like", "timestamp": now + 1},
                # Track 2: early skip
                {
                    "user_id": "u1",
                    "track_id": "t2",
                    "event_type": "play_end",
                    "timestamp": now + 2,
                    "value": 0.02,
                    "dwell_ms": 1000,
                },
                {"user_id": "u1", "track_id": "t2", "event_type": "skip", "timestamp": now + 3, "value": 1.0},
            ]
        )

        await run_track_scoring()

        async with _TestSession() as session:
            result = await session.execute(select(TrackInteraction).order_by(TrackInteraction.track_id))
            interactions = result.scalars().all()
            assert len(interactions) == 2
            t1 = next(i for i in interactions if i.track_id == "t1")
            t2 = next(i for i in interactions if i.track_id == "t2")
            # After normalisation: t1 should be higher than t2
            assert t1.satisfaction_score > t2.satisfaction_score

    async def test_incremental_scoring(self):
        """Second run only processes new events for existing pairs."""
        from app.services.track_scoring import run_track_scoring

        now = _now()
        await _insert_events(
            [
                {
                    "user_id": "u1",
                    "track_id": "t1",
                    "event_type": "play_end",
                    "timestamp": now,
                    "value": 1.0,
                    "dwell_ms": 180_000,
                },
            ]
        )

        r1 = await run_track_scoring()
        assert r1["interactions_created"] == 1

        # Add more events for same pair.
        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "like", "timestamp": now + 10},
            ]
        )

        r2 = await run_track_scoring()
        assert r2["interactions_updated"] == 1
        assert r2["interactions_created"] == 0

        async with _TestSession() as session:
            interactions = (await session.execute(select(TrackInteraction))).scalars().all()
            assert len(interactions) == 1
            assert interactions[0].like_count == 1

    async def test_normalisation_single_track(self):
        """User with one track gets score 0.5."""
        from app.services.track_scoring import run_track_scoring

        now = _now()
        await _insert_events(
            [
                {
                    "user_id": "u1",
                    "track_id": "t1",
                    "event_type": "play_end",
                    "timestamp": now,
                    "value": 1.0,
                    "dwell_ms": 180_000,
                },
            ]
        )

        await run_track_scoring()

        async with _TestSession() as session:
            interactions = (await session.execute(select(TrackInteraction))).scalars().all()
            assert len(interactions) == 1
            assert interactions[0].satisfaction_score == pytest.approx(0.5)

    async def test_reco_impression_excluded(self):
        """reco_impression events should not create interactions."""
        from app.services.track_scoring import run_track_scoring

        now = _now()
        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "reco_impression", "timestamp": now},
            ]
        )

        result = await run_track_scoring()
        assert result["events_processed"] == 0
        assert result["interactions_created"] == 0


# =============================================================================
# Taste profile tests
# =============================================================================


class TestTasteProfile:
    async def test_basic_profile_build(self):
        """User with interactions and track features gets a complete profile."""
        from app.services.sessionizer import run_sessionizer
        from app.services.taste_profile import run_taste_profile_builder
        from app.services.track_scoring import run_track_scoring

        now = _now()
        await _insert_user("u1")
        await _insert_track_features("t1", bpm=120.0, energy=0.7, valence=0.5)
        await _insert_track_features("t2", bpm=130.0, energy=0.8, valence=0.6)

        await _insert_events(
            [
                {"user_id": "u1", "track_id": "t1", "event_type": "play_start", "timestamp": now},
                {
                    "user_id": "u1",
                    "track_id": "t1",
                    "event_type": "play_end",
                    "timestamp": now + 60,
                    "value": 1.0,
                    "dwell_ms": 180_000,
                },
                {"user_id": "u1", "track_id": "t2", "event_type": "play_start", "timestamp": now + 120},
                {
                    "user_id": "u1",
                    "track_id": "t2",
                    "event_type": "play_end",
                    "timestamp": now + 180,
                    "value": 0.9,
                    "dwell_ms": 160_000,
                },
            ]
        )

        # Run full pipeline.
        await run_sessionizer()
        await run_track_scoring()
        result = await run_taste_profile_builder()

        assert result["users_updated"] == 1

        async with _TestSession() as session:
            user = (await session.execute(select(User).where(User.user_id == "u1"))).scalar_one()
            profile = user.taste_profile
            assert profile is not None
            assert "audio_preferences" in profile
            assert "bpm" in profile["audio_preferences"]
            assert "top_tracks" in profile
            assert len(profile["top_tracks"]) == 2
            assert "behaviour" in profile
            assert profile["behaviour"]["total_plays"] > 0

    async def test_no_interactions_skipped(self):
        """User with no interactions gets skipped."""
        from app.services.taste_profile import run_taste_profile_builder

        await _insert_user("u1")
        result = await run_taste_profile_builder()
        assert result["users_skipped"] == 1

        async with _TestSession() as session:
            user = (await session.execute(select(User).where(User.user_id == "u1"))).scalar_one()
            assert user.taste_profile is None

    async def test_profile_without_track_features(self):
        """User with interactions but no analysed tracks still gets a partial profile."""
        from app.services.taste_profile import run_taste_profile_builder
        from app.services.track_scoring import run_track_scoring

        now = _now()
        await _insert_user("u1")
        await _insert_events(
            [
                {
                    "user_id": "u1",
                    "track_id": "t_unknown",
                    "event_type": "play_end",
                    "timestamp": now,
                    "value": 1.0,
                    "dwell_ms": 180_000,
                },
                {"user_id": "u1", "track_id": "t_unknown", "event_type": "like", "timestamp": now + 1},
            ]
        )

        await run_track_scoring()
        result = await run_taste_profile_builder()
        assert result["users_updated"] == 1

        async with _TestSession() as session:
            user = (await session.execute(select(User).where(User.user_id == "u1"))).scalar_one()
            profile = user.taste_profile
            assert profile is not None
            # No audio_preferences since track has no features.
            assert "audio_preferences" not in profile
            assert "top_tracks" in profile
