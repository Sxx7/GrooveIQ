"""
GrooveIQ – Tests for ranking model (Phase 4, Step 6).

Tests feature engineering, model training, scoring, and fallback behaviour.
"""

from __future__ import annotations

import base64
import time

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select
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

    monkeypatch.setattr("app.services.feature_eng.AsyncSessionLocal", _TestSession, raising=False)
    monkeypatch.setattr("app.services.ranker.AsyncSessionLocal", _TestSession)

    yield

    # Reset ranker singleton.
    import app.services.ranker as r
    r._model = None
    r._model_version = None
    r._model_stats = {}

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed_data(n_users: int = 3, n_tracks: int = 20, interactions_per_user: int = 15):
    """Seed tracks, users, and interactions for training."""
    now = _now()
    async with _TestSession() as session:
        # Tracks
        for i in range(n_tracks):
            session.add(TrackFeatures(
                track_id=f"t{i}",
                file_path=f"/music/artist{i % 4}/album{i % 8}/track{i}.mp3",
                bpm=90.0 + i * 3,
                energy=0.2 + (i % 10) * 0.08,
                danceability=0.3 + (i % 7) * 0.1,
                valence=0.4 + (i % 5) * 0.1,
                loudness=-12.0 + i * 0.5,
                instrumentalness=0.1 * (i % 3),
                duration=180.0 + i * 10,
                embedding=_make_embedding(i),
                mood_tags=[{"label": "happy", "confidence": 0.7}] if i % 3 == 0 else [{"label": "chill", "confidence": 0.6}],
                analyzed_at=now,
                analysis_version="1",
            ))

        # Users with taste profiles
        for u in range(n_users):
            top_tracks = [
                {"track_id": f"t{j}", "score": round(0.9 - j * 0.04, 2)}
                for j in range(min(10, n_tracks))
            ]
            session.add(User(
                user_id=f"user{u}",
                last_seen=now,
                taste_profile={
                    "top_tracks": top_tracks,
                    "audio_preferences": {
                        "bpm": {"mean": 120.0, "std": 15.0},
                        "energy": {"mean": 0.6, "std": 0.15},
                        "danceability": {"mean": 0.5, "std": 0.1},
                        "valence": {"mean": 0.5, "std": 0.1},
                    },
                    "mood_preferences": {"happy": 0.5, "chill": 0.3},
                    "time_patterns": {"14": 0.15, "20": 0.25},
                    "updated_at": now,
                },
            ))

        # Interactions (one per user×track to respect unique constraint)
        for u in range(n_users):
            for i in range(min(interactions_per_user, n_tracks)):
                tid = f"t{i}"
                session.add(TrackInteraction(
                    user_id=f"user{u}",
                    track_id=tid,
                    play_count=3 + i % 5,
                    skip_count=i % 3,
                    like_count=1 if i < 3 else 0,
                    dislike_count=0,
                    repeat_count=0,
                    playlist_add_count=0,
                    queue_add_count=0,
                    early_skip_count=i % 2,
                    mid_skip_count=0,
                    full_listen_count=2 + i % 3,
                    total_seekfwd=0,
                    total_seekbk=0,
                    satisfaction_score=0.3 + (i % 8) * 0.08,
                    last_event_id=u * 100 + i,
                    first_played_at=now - 86_400 * 7,
                    last_played_at=now - 3600 * (i + 1),
                    updated_at=now,
                ))
        await session.commit()


class TestFeatureEngineering:

    async def test_build_features_shape(self):
        """Feature vectors have the correct number of columns."""
        from app.services.feature_eng import build_features, NUM_FEATURES

        await _seed_data()
        async with _TestSession() as session:
            result = await build_features("user0", [f"t{i}" for i in range(5)], session)

        assert result["features"].shape == (5, NUM_FEATURES)
        assert len(result["track_ids"]) == 5

    async def test_build_features_missing_tracks_excluded(self):
        """Tracks not in track_features are excluded."""
        from app.services.feature_eng import build_features

        await _seed_data(n_tracks=5)
        async with _TestSession() as session:
            result = await build_features("user0", ["t0", "t1", "nonexistent"], session)

        assert len(result["track_ids"]) == 2
        assert "nonexistent" not in result["track_ids"]

    async def test_build_features_cold_start_interaction(self):
        """Tracks the user has never interacted with get zero interaction features."""
        from app.services.feature_eng import build_features, FEATURE_COLUMNS

        await _seed_data(n_tracks=20, interactions_per_user=5)
        async with _TestSession() as session:
            # t15 exists but user0 has no interaction with it (only 5 interactions).
            result = await build_features("user0", ["t15"], session)

        assert len(result["track_ids"]) == 1
        sat_idx = FEATURE_COLUMNS.index("has_prior_interaction")
        assert result["features"][0, sat_idx] == 0.0

    async def test_build_features_no_taste_profile(self):
        """Works when user has no taste profile (uses library-wide defaults via abs(track - track))."""
        from app.services.feature_eng import build_features

        await _seed_data()
        # Create a user without a taste profile.
        async with _TestSession() as session:
            session.add(User(user_id="noprefs", last_seen=_now()))
            await session.commit()

        async with _TestSession() as session:
            result = await build_features("noprefs", ["t0", "t1"], session)

        assert result["features"].shape[0] == 2
        # Delta features should be 0 (abs(track_val - track_val) since no prefs).
        # This is the expected fallback behaviour.

    async def test_build_training_data(self):
        """Training data builds from all interactions."""
        from app.services.feature_eng import build_training_data, NUM_FEATURES

        await _seed_data(n_users=2, n_tracks=10, interactions_per_user=8)
        async with _TestSession() as session:
            data = await build_training_data(session)

        assert data["n_samples"] > 0
        assert data["features"].shape[1] == NUM_FEATURES
        assert len(data["labels"]) == data["n_samples"]
        assert len(data["groups"]) > 0


class TestRanker:

    async def test_train_model(self):
        """Model trains successfully on synthetic data."""
        from app.services.ranker import train_model, is_ready, get_model_version

        await _seed_data(n_users=4, n_tracks=20, interactions_per_user=20)
        result = await train_model()

        assert result["trained"] is True
        assert result["training_samples"] >= 50
        assert is_ready()
        assert get_model_version() is not None

    async def test_score_candidates(self):
        """Scoring produces valid results after training."""
        from app.services.ranker import train_model, score_candidates

        await _seed_data(n_users=4, n_tracks=20, interactions_per_user=20)
        await train_model()

        async with _TestSession() as session:
            scored = await score_candidates("user0", ["t0", "t1", "t2", "t3", "t4"], session)

        assert len(scored) == 5
        for tid, score in scored:
            assert isinstance(tid, str)
            assert isinstance(score, float)

        # Should be sorted descending.
        scores = [s for _, s in scored]
        assert scores == sorted(scores, reverse=True)

    async def test_fallback_without_model(self):
        """Without a trained model, falls back to satisfaction_score."""
        from app.services.ranker import score_candidates, is_ready

        await _seed_data()
        assert not is_ready()

        async with _TestSession() as session:
            scored = await score_candidates("user0", ["t0", "t1", "t2"], session)

        assert len(scored) == 3
        # Should still return results (using satisfaction_score fallback).
        for tid, score in scored:
            assert isinstance(score, float)

    async def test_too_few_samples_skips_training(self):
        """Training is skipped when there are too few samples."""
        from app.services.ranker import train_model, is_ready

        # Only seed minimal data (< 50 samples).
        await _seed_data(n_users=1, n_tracks=5, interactions_per_user=3)
        result = await train_model()

        assert result["trained"] is False
        assert not is_ready()

    async def test_get_model_stats(self):
        """Model stats reflect training status."""
        from app.services.ranker import train_model, get_model_stats

        # Before training.
        stats = get_model_stats()
        assert stats["trained"] is False

        await _seed_data(n_users=4, n_tracks=20, interactions_per_user=20)
        await train_model()

        stats = get_model_stats()
        assert stats["trained"] is True
        assert "model_version" in stats
        assert "training_samples" in stats

    async def test_score_with_context(self):
        """Scoring works when context params are provided."""
        from app.services.ranker import train_model, score_candidates

        await _seed_data(n_users=4, n_tracks=20, interactions_per_user=20)
        await train_model()

        async with _TestSession() as session:
            scored = await score_candidates(
                "user0", ["t0", "t1", "t2", "t3", "t4"], session,
                hour_of_day=14, day_of_week=3,
                device_type="mobile", output_type="headphones",
                context_type="playlist", location_label="commute",
            )

        assert len(scored) == 5
        for tid, score in scored:
            assert isinstance(score, float)
        scores = [s for _, s in scored]
        assert scores == sorted(scores, reverse=True)

    async def test_fallback_with_context(self):
        """Fallback ranking works with context params (no trained model)."""
        from app.services.ranker import score_candidates, is_ready

        await _seed_data()
        assert not is_ready()

        async with _TestSession() as session:
            scored = await score_candidates(
                "user0", ["t0", "t1", "t2"], session,
                device_type="speaker", output_type="bluetooth_speaker",
            )

        assert len(scored) == 3


class TestFeatureEngineeringContext:

    async def test_context_features_in_output(self):
        """Context features appear in the feature vector at the correct positions."""
        from app.services.feature_eng import build_features, FEATURE_COLUMNS, NUM_FEATURES

        await _seed_data()
        async with _TestSession() as session:
            result = await build_features(
                "user0", ["t0", "t1"], session,
                hour_of_day=10, day_of_week=2,
                device_type="mobile", output_type="headphones",
                context_type="playlist", location_label="gym",
            )

        assert result["features"].shape == (2, NUM_FEATURES)

        # is_mobile should be 1.0 and is_headphones should be 1.0
        is_mobile_idx = FEATURE_COLUMNS.index("is_mobile")
        is_headphones_idx = FEATURE_COLUMNS.index("is_headphones")
        assert result["features"][0, is_mobile_idx] == 1.0
        assert result["features"][0, is_headphones_idx] == 1.0

    async def test_context_features_default_zero(self):
        """Context features default to 0.0 when not provided."""
        from app.services.feature_eng import build_features, FEATURE_COLUMNS

        await _seed_data()
        async with _TestSession() as session:
            result = await build_features("user0", ["t0"], session)

        device_idx = FEATURE_COLUMNS.index("device_affinity")
        is_mobile_idx = FEATURE_COLUMNS.index("is_mobile")
        assert result["features"][0, device_idx] == 0.0
        assert result["features"][0, is_mobile_idx] == 0.0

    async def test_device_affinity_from_profile(self):
        """device_affinity pulls from taste_profile.device_patterns."""
        from app.services.feature_eng import build_features, FEATURE_COLUMNS

        await _seed_data()

        # Add device_patterns to user's taste profile.
        async with _TestSession() as session:
            from sqlalchemy import update
            from app.models.db import User
            user_result = await session.execute(
                select(User.taste_profile).where(User.user_id == "user0")
            )
            profile = user_result.scalar_one()
            profile["device_patterns"] = {"mobile": 0.75, "desktop": 0.25}
            await session.execute(
                update(User).where(User.user_id == "user0").values(taste_profile=profile)
            )
            await session.commit()

        async with _TestSession() as session:
            result = await build_features(
                "user0", ["t0"], session, device_type="mobile",
            )

        dev_idx = FEATURE_COLUMNS.index("device_affinity")
        assert abs(result["features"][0, dev_idx] - 0.75) < 0.01
