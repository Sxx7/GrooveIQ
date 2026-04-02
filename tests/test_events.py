"""
GrooveIQ – Tests for event ingestion (Phase 1).

Run with:  pytest tests/ -v
"""

from __future__ import annotations

import time
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session, init_db
from app.main import app
from app.models.db import Base

# Use in-memory SQLite for tests
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession  = async_sessionmaker(_test_engine, expire_on_commit=False)


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
        headers={"Authorization": f"Bearer {settings.API_KEYS[0]}"}
             if settings.API_KEYS else {},
    ) as c:
        yield c


# ── Tests ──────────────────────────────────────────────────────────────────

class TestEventIngestion:

    async def test_single_event_accepted(self, client: AsyncClient):
        resp = await client.post("/v1/events", json={
            "user_id": "alice",
            "track_id": "track-001",
            "event_type": "play_end",
            "value": 0.95,
        })
        assert resp.status_code == 202
        data = resp.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0

    async def test_play_end_below_threshold_rejected(self, client: AsyncClient):
        resp = await client.post("/v1/events", json={
            "user_id": "alice",
            "track_id": "track-001",
            "event_type": "play_end",
            "value": 0.01,  # below 5% threshold
        })
        assert resp.status_code == 202
        assert resp.json()["rejected"] == 1

    async def test_future_timestamp_rejected(self, client: AsyncClient):
        resp = await client.post("/v1/events", json={
            "user_id": "alice",
            "track_id": "track-001",
            "event_type": "like",
            "timestamp": int(time.time()) + 3600,  # 1 hour in future
        })
        assert resp.status_code == 422   # Pydantic validation error

    async def test_old_timestamp_rejected(self, client: AsyncClient):
        resp = await client.post("/v1/events", json={
            "user_id": "alice",
            "track_id": "track-001",
            "event_type": "like",
            "timestamp": int(time.time()) - 90_000,  # 25 hours ago
        })
        assert resp.status_code == 422

    async def test_batch_ingestion(self, client: AsyncClient):
        events = [
            {"user_id": "bob", "track_id": f"track-{i:03d}",
             "event_type": "play_end", "value": 0.8}
            for i in range(10)
        ]
        resp = await client.post("/v1/events/batch", json={"events": events})
        assert resp.status_code == 202
        assert resp.json()["accepted"] == 10

    async def test_batch_size_limit(self, client: AsyncClient):
        events = [
            {"user_id": "bob", "track_id": f"track-{i}",
             "event_type": "like"}
            for i in range(100)   # exceeds default batch max of 50
        ]
        resp = await client.post("/v1/events/batch", json={"events": events})
        assert resp.status_code == 422

    async def test_duplicate_event_silently_accepted(self, client: AsyncClient):
        payload = {"user_id": "alice", "track_id": "track-001", "event_type": "like"}
        r1 = await client.post("/v1/events", json=payload)
        r2 = await client.post("/v1/events", json=payload)
        assert r1.status_code == 202
        assert r2.status_code == 202   # silent dedup, not error

    async def test_invalid_event_type(self, client: AsyncClient):
        resp = await client.post("/v1/events", json={
            "user_id": "alice", "track_id": "track-001",
            "event_type": "teleport",   # not a real event type
        })
        assert resp.status_code == 422

    async def test_skip_event_with_position(self, client: AsyncClient):
        resp = await client.post("/v1/events", json={
            "user_id": "alice",
            "track_id": "track-002",
            "event_type": "skip",
            "value": 12.5,   # skipped 12.5 seconds in
        })
        assert resp.status_code == 202
        assert resp.json()["accepted"] == 1

    async def test_health_endpoint(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_query_events(self, client: AsyncClient):
        # Seed an event
        await client.post("/v1/events", json={
            "user_id": "carol", "track_id": "track-xyz", "event_type": "like"
        })
        resp = await client.get("/v1/events?user_id=carol")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["user_id"] == "carol"
