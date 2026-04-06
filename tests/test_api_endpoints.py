"""
GrooveIQ – Tests for user profile, interactions, sessions, recommendation, and user rename API endpoints.
"""

from __future__ import annotations

import time
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, ListenEvent, ListenSession, TrackFeatures, TrackInteraction, User

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.dependency_overrides[get_session] = override_get_session
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"}
        if settings.api_keys_list
        else {},
    ) as c:
        yield c


async def seed_user_with_data():
    """Insert a user with interactions, sessions, and track features."""
    now = int(time.time())
    async with _TestSession() as session:
        user = User(user_id="testuser", display_name="Test User", taste_profile={
            "audio_preferences": {"bpm_mean": 120.0, "energy_mean": 0.7, "danceability_mean": 0.6,
                                  "valence_mean": 0.5, "acousticness_mean": 0.3,
                                  "instrumentalness_mean": 0.1, "loudness_mean": -8.0},
            "mood_preferences": {"happy": 0.8, "relaxed": 0.5},
            "key_preferences": {"C major": 0.4, "G major": 0.3},
            "behaviour": {"total_plays": 100, "active_days": 10, "avg_session_tracks": 8.5,
                          "skip_rate": 0.15, "avg_completion": 0.85},
        }, profile_updated_at=now)
        session.add(user)

        tf = TrackFeatures(
            track_id="track-001", file_path="/music/song.mp3", duration=240.0,
            bpm=120.0, key="C", mode="major", energy=0.8, danceability=0.7, valence=0.6,
            mood_tags=[{"label": "happy", "confidence": 0.9}],
        )
        session.add(tf)

        ti = TrackInteraction(
            user_id="testuser", track_id="track-001",
            play_count=5, skip_count=1, like_count=1, dislike_count=0,
            repeat_count=0, playlist_add_count=0, queue_add_count=0,
            avg_completion=0.9, satisfaction_score=0.85,
            first_played_at=now - 86400, last_played_at=now - 3600,
            last_event_id=10, updated_at=now,
        )
        session.add(ti)

        ls = ListenSession(
            session_key="testuser:1", user_id="testuser",
            started_at=now - 7200, ended_at=now - 3600, duration_s=3600,
            track_count=10, play_count=9, skip_count=1,
            like_count=2, dislike_count=0, seek_count=0,
            skip_rate=0.111, avg_completion=0.88,
            dominant_context_type="playlist", dominant_device_type="desktop",
            hour_of_day=14, day_of_week=3,
            event_id_min=1, event_id_max=20, built_at=now,
        )
        session.add(ls)

        await session.commit()


# ── Tests ──────────────────────────────────────────────────────────────────


class TestUserProfile:

    async def test_get_profile(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "testuser"
        assert data["taste_profile"] is not None
        assert data["taste_profile"]["audio_preferences"]["bpm_mean"] == 120.0
        assert "happy" in data["taste_profile"]["mood_preferences"]

    async def test_get_profile_not_found(self, client: AsyncClient):
        resp = await client.get("/v1/users/nonexistent/profile")
        assert resp.status_code == 404


class TestUserInteractions:

    async def test_get_interactions(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser/interactions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["interactions"]) == 1
        i = data["interactions"][0]
        assert i["track_id"] == "track-001"
        assert i["play_count"] == 5
        assert i["satisfaction_score"] == 0.85
        # Track metadata joined (file_path stripped from responses for security)
        assert "file_path" not in i
        assert i["bpm"] == 120.0

    async def test_get_interactions_not_found(self, client: AsyncClient):
        resp = await client.get("/v1/users/nonexistent/interactions")
        assert resp.status_code == 404

    async def test_sort_by_play_count(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser/interactions?sort_by=play_count&sort_dir=desc")
        assert resp.status_code == 200

    async def test_invalid_sort_rejected(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser/interactions?sort_by=invalid_column")
        assert resp.status_code == 422


class TestUserSessions:

    async def test_get_sessions(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        s = data["sessions"][0]
        assert s["track_count"] == 10
        assert s["duration_s"] == 3600
        assert s["dominant_device_type"] == "desktop"

    async def test_get_sessions_not_found(self, client: AsyncClient):
        resp = await client.get("/v1/users/nonexistent/sessions")
        assert resp.status_code == 404


class TestRecommendationHistory:

    async def test_history_empty(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/recommend/testuser/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["history"] == []

    async def test_history_with_impressions(self, client: AsyncClient):
        await seed_user_with_data()
        now = int(time.time())
        async with _TestSession() as session:
            # Add impression events
            session.add(ListenEvent(
                user_id="testuser", track_id="track-001",
                event_type="reco_impression", surface="recommend_api",
                position=0, request_id="req-123", model_version="v1",
                timestamp=now,
            ))
            # Add a stream attributed to the same request_id
            session.add(ListenEvent(
                user_id="testuser", track_id="track-001",
                event_type="play_start", request_id="req-123",
                timestamp=now + 10,
            ))
            await session.commit()

        resp = await client.get("/v1/recommend/testuser/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        h = data["history"][0]
        assert h["track_id"] == "track-001"
        assert h["streamed"] is True
        assert h["position"] == 0

    async def test_history_not_found(self, client: AsyncClient):
        resp = await client.get("/v1/recommend/nonexistent/history")
        assert resp.status_code == 404


class TestUidExposure:

    async def test_create_user_returns_uid(self, client: AsyncClient):
        resp = await client.post("/v1/users", json={"user_id": "alice", "display_name": "Alice"})
        assert resp.status_code == 201
        data = resp.json()
        assert "uid" in data
        assert isinstance(data["uid"], int)
        assert data["user_id"] == "alice"

    async def test_get_user_returns_uid(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser")
        assert resp.status_code == 200
        data = resp.json()
        assert "uid" in data
        assert isinstance(data["uid"], int)

    async def test_list_users_returns_uid(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert "uid" in data[0]

    async def test_profile_returns_uid(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert "uid" in data


class TestUserRename:

    async def test_rename_user_id(self, client: AsyncClient):
        await seed_user_with_data()
        # Get the uid first
        resp = await client.get("/v1/users/testuser")
        uid = resp.json()["uid"]

        # Rename
        resp = await client.patch(f"/v1/users/{uid}", json={"user_id": "simon"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["uid"] == uid  # uid stays the same
        assert data["user_id"] == "simon"

        # Old name should 404
        resp = await client.get("/v1/users/testuser")
        assert resp.status_code == 404

        # New name works
        resp = await client.get("/v1/users/simon")
        assert resp.status_code == 200
        assert resp.json()["uid"] == uid

    async def test_rename_cascades_to_events(self, client: AsyncClient):
        await seed_user_with_data()
        now = int(time.time())
        # Add an event under old name
        async with _TestSession() as session:
            session.add(ListenEvent(
                user_id="testuser", track_id="track-001",
                event_type="play_end", value=0.9, timestamp=now,
            ))
            await session.commit()

        # Get uid and rename
        resp = await client.get("/v1/users/testuser")
        uid = resp.json()["uid"]
        await client.patch(f"/v1/users/{uid}", json={"user_id": "renamed_user"})

        # Events should be queryable under new name
        resp = await client.get("/v1/events?user_id=renamed_user")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) >= 1
        assert all(e["user_id"] == "renamed_user" for e in events)

        # Old name should have no events
        resp = await client.get("/v1/events?user_id=testuser")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    async def test_rename_cascades_to_interactions(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser")
        uid = resp.json()["uid"]

        await client.patch(f"/v1/users/{uid}", json={"user_id": "new_name"})

        resp = await client.get("/v1/users/new_name/interactions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_rename_cascades_to_sessions(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser")
        uid = resp.json()["uid"]

        await client.patch(f"/v1/users/{uid}", json={"user_id": "new_name"})

        resp = await client.get("/v1/users/new_name/sessions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_rename_to_existing_name_rejected(self, client: AsyncClient):
        await seed_user_with_data()
        # Create a second user
        async with _TestSession() as session:
            session.add(User(user_id="other_user"))
            await session.commit()

        resp = await client.get("/v1/users/testuser")
        uid = resp.json()["uid"]

        resp = await client.patch(f"/v1/users/{uid}", json={"user_id": "other_user"})
        assert resp.status_code == 409

    async def test_update_display_name_only(self, client: AsyncClient):
        await seed_user_with_data()
        resp = await client.get("/v1/users/testuser")
        uid = resp.json()["uid"]

        resp = await client.patch(f"/v1/users/{uid}", json={"display_name": "Simon W."})
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Simon W."
        assert resp.json()["user_id"] == "testuser"  # unchanged

    async def test_rename_nonexistent_uid(self, client: AsyncClient):
        resp = await client.patch("/v1/users/99999", json={"user_id": "nope"})
        assert resp.status_code == 404

    async def test_empty_update_rejected(self, client: AsyncClient):
        resp = await client.patch("/v1/users/1", json={})
        assert resp.status_code == 422
