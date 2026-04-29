"""
GrooveIQ – Tests for event ingestion (Phase 1).

Run with:  pytest tests/ -v
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, TrackFeatures

# Use in-memory SQLite for tests
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


@pytest_asyncio.fixture(autouse=True)
async def _seed_test_tracks(setup_db):
    # Post-#37 ingest drops events whose track_id resolves to no
    # TrackFeatures row, so every placeholder used in this file needs a stub.
    track_ids = [
        "track-001", "track-002", "track-003",
        "track-rich", "track-ctx", "track-imp", "track-old", "track-xyz",
        "t1", "t2", "t-req",
    ]
    track_ids.extend(f"track-{i:03d}" for i in range(10))
    async with _TestSession() as session:
        for tid in track_ids:
            session.add(TrackFeatures(track_id=tid, file_path=f"/music/{tid}.mp3"))
        await session.commit()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


# ── Tests ──────────────────────────────────────────────────────────────────


class TestEventIngestion:
    async def test_single_event_accepted(self, client: AsyncClient):
        resp = await client.post(
            "/v1/events",
            json={
                "user_id": "alice",
                "track_id": "track-001",
                "event_type": "play_end",
                "value": 0.95,
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0

    async def test_play_end_below_threshold_rejected(self, client: AsyncClient):
        resp = await client.post(
            "/v1/events",
            json={
                "user_id": "alice",
                "track_id": "track-001",
                "event_type": "play_end",
                "value": 0.01,  # below 5% threshold
            },
        )
        assert resp.status_code == 202
        assert resp.json()["rejected"] == 1

    async def test_future_timestamp_rejected(self, client: AsyncClient):
        resp = await client.post(
            "/v1/events",
            json={
                "user_id": "alice",
                "track_id": "track-001",
                "event_type": "like",
                "timestamp": int(time.time()) + 3600,  # 1 hour in future
            },
        )
        assert resp.status_code == 422  # Pydantic validation error

    async def test_old_timestamp_rejected(self, client: AsyncClient):
        resp = await client.post(
            "/v1/events",
            json={
                "user_id": "alice",
                "track_id": "track-001",
                "event_type": "like",
                "timestamp": int(time.time()) - 90_000,  # 25 hours ago
            },
        )
        assert resp.status_code == 422

    async def test_batch_ingestion(self, client: AsyncClient):
        events = [
            {"user_id": "bob", "track_id": f"track-{i:03d}", "event_type": "play_end", "value": 0.8} for i in range(10)
        ]
        resp = await client.post("/v1/events/batch", json={"events": events})
        assert resp.status_code == 202
        assert resp.json()["accepted"] == 10

    async def test_batch_size_limit(self, client: AsyncClient):
        events = [
            {"user_id": "bob", "track_id": f"track-{i}", "event_type": "like"}
            for i in range(100)  # exceeds default batch max of 50
        ]
        resp = await client.post("/v1/events/batch", json={"events": events})
        assert resp.status_code == 422

    async def test_duplicate_event_silently_accepted(self, client: AsyncClient):
        payload = {"user_id": "alice", "track_id": "track-001", "event_type": "like"}
        r1 = await client.post("/v1/events", json=payload)
        r2 = await client.post("/v1/events", json=payload)
        assert r1.status_code == 202
        assert r2.status_code == 202  # silent dedup, not error

    async def test_invalid_event_type(self, client: AsyncClient):
        resp = await client.post(
            "/v1/events",
            json={
                "user_id": "alice",
                "track_id": "track-001",
                "event_type": "teleport",  # not a real event type
            },
        )
        assert resp.status_code == 422

    async def test_skip_event_with_position(self, client: AsyncClient):
        resp = await client.post(
            "/v1/events",
            json={
                "user_id": "alice",
                "track_id": "track-002",
                "event_type": "skip",
                "value": 12.5,  # skipped 12.5 seconds in
            },
        )
        assert resp.status_code == 202
        assert resp.json()["accepted"] == 1

    async def test_health_endpoint(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_query_events(self, client: AsyncClient):
        # Seed an event
        await client.post("/v1/events", json={"user_id": "carol", "track_id": "track-xyz", "event_type": "like"})
        resp = await client.get("/v1/events?user_id=carol")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["user_id"] == "carol"

    # ── Rich signal tests ────────────────────────────────────────────────

    async def test_event_with_rich_signals(self, client: AsyncClient):
        """POST with all new fields populated, verify roundtrip via GET."""
        payload = {
            "user_id": "dave",
            "track_id": "track-rich",
            "event_type": "play_end",
            "value": 0.92,
            "session_id": "sess-001",
            "surface": "home",
            "position": 3,
            "request_id": "req-abc",
            "model_version": "v1.2",
            "session_position": 5,
            "dwell_ms": 195000,
            "pause_duration_ms": 2500,
            "num_seekfwd": 1,
            "num_seekbk": 0,
            "shuffle": True,
            "context_type": "playlist",
            "context_id": "pl-xyz",
            "context_switch": False,
            "reason_start": "user_tap",
            "reason_end": "track_done",
            "device_id": "dev-001",
            "device_type": "desktop",
        }
        resp = await client.post("/v1/events", json=payload)
        assert resp.status_code == 202
        assert resp.json()["accepted"] == 1

        # Verify stored values
        resp = await client.get("/v1/events?user_id=dave")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        ev = data[0]
        assert ev["surface"] == "home"
        assert ev["position"] == 3
        assert ev["request_id"] == "req-abc"
        assert ev["dwell_ms"] == 195000
        assert ev["shuffle"] is True
        assert ev["context_type"] == "playlist"
        assert ev["reason_start"] == "user_tap"
        assert ev["reason_end"] == "track_done"
        assert ev["device_id"] == "dev-001"
        assert ev["device_type"] == "desktop"
        assert ev["model_version"] == "v1.2"

    async def test_event_with_context_and_location_signals(self, client: AsyncClient):
        """POST with time-context, audio output, and location fields."""
        payload = {
            "user_id": "iris",
            "track_id": "track-ctx",
            "event_type": "play_end",
            "value": 0.85,
            "hour_of_day": 14,
            "day_of_week": 3,
            "timezone": "Europe/Zurich",
            "output_type": "bluetooth_speaker",
            "output_device_name": "Sonos Living Room",
            "bluetooth_connected": True,
            "latitude": 47.3769,
            "longitude": 8.5417,
            "location_label": "home",
        }
        resp = await client.post("/v1/events", json=payload)
        assert resp.status_code == 202
        assert resp.json()["accepted"] == 1

        resp = await client.get("/v1/events?user_id=iris")
        ev = resp.json()[0]
        assert ev["hour_of_day"] == 14
        assert ev["day_of_week"] == 3
        assert ev["timezone"] == "Europe/Zurich"
        assert ev["output_type"] == "bluetooth_speaker"
        assert ev["output_device_name"] == "Sonos Living Room"
        assert ev["bluetooth_connected"] is True
        assert ev["latitude"] == pytest.approx(47.3769)
        assert ev["longitude"] == pytest.approx(8.5417)
        assert ev["location_label"] == "home"

    async def test_reco_impression_event(self, client: AsyncClient):
        """The new reco_impression event type is accepted."""
        resp = await client.post(
            "/v1/events",
            json={
                "user_id": "eve",
                "track_id": "track-imp",
                "event_type": "reco_impression",
                "request_id": "req-imp-001",
                "surface": "home",
                "position": 0,
                "model_version": "v1.0",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["accepted"] == 1

    async def test_backwards_compat_no_new_fields(self, client: AsyncClient):
        """Old payload still works; new fields come back as null."""
        await client.post("/v1/events", json={"user_id": "frank", "track_id": "track-old", "event_type": "like"})
        resp = await client.get("/v1/events?user_id=frank")
        ev = resp.json()[0]
        assert ev["surface"] is None
        assert ev["dwell_ms"] is None
        assert ev["device_id"] is None
        assert ev["shuffle"] is None

    async def test_query_filter_by_device_id(self, client: AsyncClient):
        """device_id query filter returns only matching events."""
        await client.post(
            "/v1/events",
            json={
                "user_id": "gina",
                "track_id": "t1",
                "event_type": "play_end",
                "value": 0.9,
                "device_id": "phone-1",
            },
        )
        await client.post(
            "/v1/events",
            json={
                "user_id": "gina",
                "track_id": "t2",
                "event_type": "play_end",
                "value": 0.8,
                "device_id": "desktop-1",
            },
        )
        resp = await client.get("/v1/events?device_id=phone-1")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["device_id"] == "phone-1"

    async def test_query_filter_by_request_id(self, client: AsyncClient):
        """request_id groups impression + stream events."""
        for etype in ("reco_impression", "play_end"):
            await client.post(
                "/v1/events",
                json={
                    "user_id": "hank",
                    "track_id": "t-req",
                    "event_type": etype,
                    "request_id": "req-shared",
                    "value": 0.95 if etype == "play_end" else None,
                },
            )
        resp = await client.get("/v1/events?request_id=req-shared")
        assert len(resp.json()) == 2
