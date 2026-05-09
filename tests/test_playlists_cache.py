"""
GrooveIQ — Tests for the daily idempotency cache on POST /v1/playlists.

Issue #89: same caller + same params + same UTC day must return the existing
playlist (200) instead of creating a duplicate (201). ``?refresh=true`` must
bypass the cache. Different params, different days, or different callers
must each yield a fresh playlist.
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import numpy as np
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, TrackFeatures
from app.services.playlist_service import compute_cache_key, utc_day_bucket

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


def _make_embedding(seed: int) -> str:
    rng = np.random.RandomState(seed)
    vec = rng.randn(64).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return base64.b64encode(vec.tobytes()).decode()


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
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


async def _seed_tracks(n: int = 12) -> None:
    """Seed enough analyzed tracks to satisfy the playlist generator's minimum."""
    now = int(time.time())
    async with _TestSession() as session:
        for i in range(n):
            session.add(
                TrackFeatures(
                    track_id=f"t{i:03d}",
                    file_path=f"/music/t{i:03d}.mp3",
                    title=f"Song {i}",
                    artist=f"Artist {i % 3}",
                    duration=180.0 + i,
                    bpm=100.0 + i,
                    energy=0.5 + (i % 5) / 10.0,
                    valence=0.5,
                    danceability=0.5,
                    embedding=_make_embedding(i),
                    mood_tags=[{"label": "happy", "confidence": 0.8 + (i % 2) * 0.1}],
                    analyzed_at=now,
                    analysis_version="1",
                )
            )
        await session.commit()


# ---------------------------------------------------------------------------
# Unit tests for compute_cache_key
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_identical_inputs_yield_identical_key(self):
        kwargs = dict(
            created_by="hash-a",
            strategy="mood",
            seed_track_id=None,
            params={"mood": "happy"},
            max_tracks=20,
            bucket_date="2026-05-09",
        )
        assert compute_cache_key(**kwargs) == compute_cache_key(**kwargs)

    def test_param_dict_order_does_not_matter(self):
        a = compute_cache_key(
            created_by="x",
            strategy="text",
            seed_track_id=None,
            params={"prompt": "late night drive", "extra": 1},
            max_tracks=25,
            bucket_date="2026-05-09",
        )
        b = compute_cache_key(
            created_by="x",
            strategy="text",
            seed_track_id=None,
            params={"extra": 1, "prompt": "late night drive"},
            max_tracks=25,
            bucket_date="2026-05-09",
        )
        assert a == b

    def test_different_owner_yields_different_key(self):
        kw = dict(
            strategy="mood",
            seed_track_id=None,
            params={"mood": "happy"},
            max_tracks=20,
            bucket_date="2026-05-09",
        )
        assert compute_cache_key(created_by="alice", **kw) != compute_cache_key(created_by="bob", **kw)

    def test_different_day_yields_different_key(self):
        kw = dict(
            created_by="x",
            strategy="mood",
            seed_track_id=None,
            params={"mood": "happy"},
            max_tracks=20,
        )
        assert compute_cache_key(bucket_date="2026-05-09", **kw) != compute_cache_key(bucket_date="2026-05-10", **kw)

    def test_different_max_tracks_yields_different_key(self):
        kw = dict(
            created_by="x",
            strategy="mood",
            seed_track_id=None,
            params={"mood": "happy"},
            bucket_date="2026-05-09",
        )
        assert compute_cache_key(max_tracks=20, **kw) != compute_cache_key(max_tracks=30, **kw)

    def test_different_prompt_yields_different_key(self):
        kw = dict(
            created_by="x",
            strategy="text",
            seed_track_id=None,
            max_tracks=20,
            bucket_date="2026-05-09",
        )
        a = compute_cache_key(params={"prompt": "late night drive"}, **kw)
        b = compute_cache_key(params={"prompt": "rainy morning coffee"}, **kw)
        assert a != b

    def test_utc_day_bucket_format(self):
        # Format must be YYYY-MM-DD so it sorts naturally and is human-readable
        d = utc_day_bucket(datetime(2026, 5, 9, 23, 59, tzinfo=UTC))
        assert d == "2026-05-09"


# ---------------------------------------------------------------------------
# HTTP integration tests
# ---------------------------------------------------------------------------


class TestPlaylistDailyCache:
    async def test_first_call_returns_201_and_persists(self, client: AsyncClient):
        await _seed_tracks()
        body = {
            "name": "Happy Vibes",
            "strategy": "mood",
            "params": {"mood": "happy"},
            "max_tracks": 5,
        }
        resp = await client.post("/v1/playlists", json=body)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["track_count"] >= 1

    async def test_repeat_call_same_day_returns_200_with_same_id(self, client: AsyncClient):
        await _seed_tracks()
        body = {
            "name": "Happy Vibes",
            "strategy": "mood",
            "params": {"mood": "happy"},
            "max_tracks": 5,
        }
        first = await client.post("/v1/playlists", json=body)
        assert first.status_code == 201
        first_id = first.json()["id"]

        second = await client.post("/v1/playlists", json=body)
        assert second.status_code == 200, "second call should be a cache hit"
        assert second.json()["id"] == first_id

        # Even when the frontend changes the visible name, cache key is unchanged.
        third = await client.post("/v1/playlists", json={**body, "name": "Different Title"})
        assert third.status_code == 200
        assert third.json()["id"] == first_id

    async def test_refresh_query_param_bypasses_cache(self, client: AsyncClient):
        await _seed_tracks()
        body = {
            "name": "Happy Vibes",
            "strategy": "mood",
            "params": {"mood": "happy"},
            "max_tracks": 5,
        }
        first = await client.post("/v1/playlists", json=body)
        assert first.status_code == 201
        first_id = first.json()["id"]

        forced = await client.post("/v1/playlists?refresh=true", json=body)
        assert forced.status_code == 201, "refresh=true must regenerate"
        assert forced.json()["id"] != first_id

    async def test_different_params_yield_separate_playlists(self, client: AsyncClient):
        await _seed_tracks()
        a = await client.post(
            "/v1/playlists",
            json={"name": "A", "strategy": "mood", "params": {"mood": "happy"}, "max_tracks": 5},
        )
        b = await client.post(
            "/v1/playlists",
            json={"name": "B", "strategy": "mood", "params": {"mood": "happy"}, "max_tracks": 7},
        )
        assert a.status_code == 201
        assert b.status_code == 201
        assert a.json()["id"] != b.json()["id"]

    async def test_clap_unavailable_returns_503_not_400(self, client: AsyncClient, monkeypatch):
        """A `text` strategy POST when CLAP is disabled must return HTTP 503,
        not the misleading HTTP 400 'Invalid playlist parameters'. Lets the
        iOS fallback distinguish 'service warming up' from 'bad input'.
        See issue #91 follow-up.
        """
        await _seed_tracks()
        # Force the CLAP-disabled path inside _generate_text. settings is
        # imported lazily inside the function, so we patch the module-level
        # singleton in app.core.config.
        from app.core.config import settings as core_settings

        monkeypatch.setattr(core_settings, "CLAP_ENABLED", False)

        resp = await client.post(
            "/v1/playlists",
            json={
                "name": "clap-503-test",
                "strategy": "text",
                "params": {"prompt": "moody synthwave neon highway driving"},
                "max_tracks": 5,
            },
        )
        assert resp.status_code == 503, resp.text
        assert "CLAP_ENABLED" in resp.json()["detail"]

    async def test_text_with_empty_prompt_returns_422_not_503(self, client: AsyncClient):
        """Bad user input (empty prompt) is caught by Pydantic schema validation
        and returns 422. The point of this test is to lock in that the new 503
        mapping does NOT swallow legitimate input errors."""
        await _seed_tracks()

        resp = await client.post(
            "/v1/playlists",
            json={
                "name": "empty-prompt-test",
                "strategy": "text",
                "params": {"prompt": "   "},
                "max_tracks": 5,
            },
        )
        assert resp.status_code == 422, resp.text
        # Pydantic's error envelope contains the message in the nested detail list.
        assert "prompt" in resp.text.lower()

    async def test_pre_migration_rows_are_not_returned(self, client: AsyncClient):
        """A playlist with cache_key NULL (pre-migration) must not be served as a hit
        for a fresh request — the lookup filters on cache_key equality, not on
        recreating the request."""
        await _seed_tracks()
        # Create a row directly with no cache_key, mimicking a pre-migration playlist.
        from app.models.db import Playlist

        async with _TestSession() as s:
            old = Playlist(
                name="Old Mix",
                strategy="mood",
                params={"mood": "happy"},
                track_count=0,
                total_duration=0.0,
                created_at=int(time.time()),
                cache_key=None,
            )
            s.add(old)
            await s.commit()
            old_id = old.id

        resp = await client.post(
            "/v1/playlists",
            json={"name": "Happy Vibes", "strategy": "mood", "params": {"mood": "happy"}, "max_tracks": 5},
        )
        assert resp.status_code == 201, "should generate a new row, not return the NULL-cache-key one"
        assert resp.json()["id"] != old_id
