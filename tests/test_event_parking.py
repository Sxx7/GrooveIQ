"""
GrooveIQ – Tests for unresolved-event parking + re-resolution.

When a client plays a track whose id doesn't resolve to a TrackFeatures row yet
(e.g. the track has no media_server_id linked), taste-bearing events used to be
silently dropped — and the batch endpoint even reported them as accepted. These
tests lock in the fix:

  * parkable events (play/like/repeat/…) are PARKED, not dropped,
  * impressions / UI-noise are still dropped,
  * the batch endpoint honors per-event accepted/rejected/deferred,
  * parked events replay once their track gets linked,
  * the parking table stays bounded (age / attempt expiry).
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, ListenEvent, PendingEvent, TrackFeatures
from app.models.schemas import EventCreate
from app.services.event_service import (
    _PENDING_MAX_AGE_DAYS,
    _PENDING_MAX_ATTEMPTS,
    process_event,
    resolve_pending_events,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.dependency_overrides[get_session] = _override_get_session
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


async def _count(s, model) -> int:
    return (await s.execute(select(func.count()).select_from(model))).scalar_one()


def _ev(**kw) -> EventCreate:
    kw.setdefault("user_id", "alice")
    kw.setdefault("timestamp", int(time.time()))
    return EventCreate(**kw)


# ---------------------------------------------------------------------------
# Parking vs dropping
# ---------------------------------------------------------------------------


async def test_unresolved_play_event_is_parked_not_dropped():
    async with _Session() as s:
        res = await process_event(s, _ev(track_id="NAVID-unlinked", event_type="play_start"))
        assert res.deferred == 1 and res.accepted == 0 and res.rejected == 0
        assert await _count(s, PendingEvent) == 1
        assert await _count(s, ListenEvent) == 0


async def test_unresolved_impression_is_dropped_not_parked():
    async with _Session() as s:
        res = await process_event(s, _ev(track_id="NAVID-unlinked", event_type="reco_impression"))
        assert res.rejected == 1 and res.deferred == 0
        assert await _count(s, PendingEvent) == 0
        assert await _count(s, ListenEvent) == 0


async def test_resolved_event_persists_normally():
    async with _Session() as s:
        s.add(TrackFeatures(track_id="hex-1", media_server_id="NAVID-1", file_path="/music/a.flac"))
        await s.flush()
        res = await process_event(s, _ev(track_id="NAVID-1", event_type="play_start"))
        assert res.accepted == 1
        assert await _count(s, ListenEvent) == 1
        assert await _count(s, PendingEvent) == 0


# ---------------------------------------------------------------------------
# Re-resolution
# ---------------------------------------------------------------------------


async def test_parked_event_replays_once_track_is_linked():
    async with _Session() as s:
        # Play an unlinked track → parked.
        await process_event(s, _ev(track_id="NAVID-2", event_type="play_start"))
        await s.flush()
        assert await _count(s, PendingEvent) == 1
        assert await _count(s, ListenEvent) == 0

        # Later, a library sync links the track (media_server_id == the raw id).
        s.add(TrackFeatures(track_id="hex-2", media_server_id="NAVID-2", file_path="/music/b.flac"))
        await s.flush()

        out = await resolve_pending_events(s)
        assert out["resolved"] == 1
        assert await _count(s, PendingEvent) == 0
        # The recovered event landed under the canonical track_id, not the raw id.
        rows = (await s.execute(select(ListenEvent.track_id))).scalars().all()
        assert rows == ["hex-2"]


async def test_still_unlinked_event_stays_parked_and_counts_attempt():
    async with _Session() as s:
        await process_event(s, _ev(track_id="NAVID-3", event_type="like"))
        await s.flush()
        out = await resolve_pending_events(s)
        assert out["still_pending"] == 1 and out["resolved"] == 0
        p = (await s.execute(select(PendingEvent))).scalar_one()
        assert p.attempts == 1  # bumped, retried next cycle


# ---------------------------------------------------------------------------
# Bounded growth
# ---------------------------------------------------------------------------


async def test_pending_expires_when_too_old():
    async with _Session() as s:
        old = int(time.time()) - (_PENDING_MAX_AGE_DAYS + 1) * 86_400
        s.add(PendingEvent(user_id="alice", raw_track_id="NAVID-old", event_type="play_start", payload={}, created_at=old))
        await s.flush()
        out = await resolve_pending_events(s)
        assert out["expired"] == 1
        assert await _count(s, PendingEvent) == 0


async def test_pending_expires_after_max_attempts():
    async with _Session() as s:
        s.add(
            PendingEvent(
                user_id="alice",
                raw_track_id="NAVID-stuck",
                event_type="play_start",
                payload={},
                created_at=int(time.time()),
                attempts=_PENDING_MAX_ATTEMPTS,
            )
        )
        await s.flush()
        out = await resolve_pending_events(s)
        assert out["expired"] == 1
        assert await _count(s, PendingEvent) == 0


# ---------------------------------------------------------------------------
# Batch endpoint accounting (the silent-loss masking bug)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


class TestBatchAccounting:
    async def test_batch_reports_accepted_deferred_and_rejected(self, client: AsyncClient):
        async with _Session() as s:
            s.add(TrackFeatures(track_id="hex-9", media_server_id="NAVID-9", file_path="/music/c.flac"))
            await s.commit()

        ts = int(time.time())
        body = {
            "events": [
                {"user_id": "alice", "track_id": "NAVID-9", "event_type": "play_start", "timestamp": ts},
                {"user_id": "alice", "track_id": "NAVID-unlinked", "event_type": "play_start", "timestamp": ts},
                {"user_id": "alice", "track_id": "NAVID-unlinked", "event_type": "reco_impression", "timestamp": ts},
            ]
        }
        r = await client.post("/v1/events/batch", json=body)
        assert r.status_code == 202, r.text
        data = r.json()
        # Previously this returned accepted=3, rejected=0 (masking 2 drops).
        assert data["accepted"] == 1  # the linked play
        assert data["deferred"] == 1  # the unlinked play, parked
        assert data["rejected"] == 1  # the unlinked impression, dropped

        async with _Session() as s:
            assert await _count(s, PendingEvent) == 1
