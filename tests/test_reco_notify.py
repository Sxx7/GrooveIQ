"""GrooveIQ – Tests for the daily mix-recommendation feed producer.

Seeds opted-in ``devices`` in in-memory SQLite and stubs ``get_session_mixes`` +
``resolve_cover_art`` (the mix serialisation and the cover lookup), then drives
``notify_daily_mixes`` with Apprise mocked. Covers the opted-in audience, the rich
per-mix event ``data`` (kind=playlist / mix_id / cover), the per-(user, mix) dedup
(a re-run adds no new rows), the disabled gate, and the cold-start / empty-mix /
opted-out skips.

Run with:  .venv-test/bin/pytest tests/test_reco_notify.py -v
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.notification_dispatch as nd
import app.services.reco_notify as rn
from app.core.config import settings
from app.models.db import Base, Device, NotificationDelivery, NotificationEvent
from app.services.reco_notify import notify_daily_mixes

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


def _mix(mix_id, ordinal, *tracks):
    return {
        "mix_id": mix_id, "ordinal": ordinal, "kind": "session", "track_count": len(tracks),
        "tracks": [
            {"position": i, "track_id": f"t{mix_id}_{i}", "media_server_id": ms,
             "title": f"S{i}", "artist": art, "album": alb, "duration": 200}
            for i, (ms, art, alb) in enumerate(tracks)
        ],
    }


async def _fake_get_session_mixes(session, user_id):
    return [
        _mix(10, 1, ("ms1", "Alpha", "AlbA"), ("ms2", "Beta", "AlbB")),
        _mix(11, 2, ("ms3", "Gamma", "AlbC")),
    ]


async def _fake_cover(session, artist, title, client=None):
    return f"http://cover/{title.replace(' ', '_')}"


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(rn, "AsyncSessionLocal", _TestSession)
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)
    monkeypatch.setattr("app.services.user_mixes.get_session_mixes", _fake_get_session_mixes)
    monkeypatch.setattr("app.services.cover_art.resolve_cover_art", _fake_cover)
    monkeypatch.setattr(settings, "PUSH_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "NOTIFY_RECOMMENDATIONS_ENABLED", True, raising=False)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _add_device(s, *, user_id="alice", reco=True, disabled_at=None):
    now = int(time.time())
    s.add(
        Device(
            user_id=user_id,
            apprise_urls=[f"jsons://relay/{user_id}"],
            notif_recommendations=reco,
            created_at=now,
            last_seen_at=now,
            disabled_at=disabled_at,
        )
    )


async def _count(model) -> int:
    async with _TestSession() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_disabled_skips(monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_RECOMMENDATIONS_ENABLED", False, raising=False)
    assert await notify_daily_mixes() == {"skipped": "disabled"}
    assert await _count(NotificationEvent) == 0


async def test_emits_rich_mix_rows():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_daily_mixes()
    assert result == {"users": 1, "with_mixes": 1, "notified": 2}  # one row per mix, not per user
    assert await _count(NotificationEvent) == 2
    assert await _count(NotificationDelivery) == 2

    async with _TestSession() as s:
        evs = (
            await s.execute(select(NotificationEvent).order_by(NotificationEvent.id))
        ).scalars().all()
        assert all(e.event_type == "recommendation" for e in evs)
        e0 = evs[0]
        assert e0.data["type"] == "recommendation"  # push route → Discover
        assert e0.data["kind"] == "playlist"  # iOS renders a playlist row → opens the mix
        assert e0.data["mix_id"] == 10
        assert e0.data["cover_url"] == "http://cover/AlbA"  # resolved from (artist, album) of track 0
        assert e0.data["media_server_id"] == "ms1"
        assert e0.title == "Your Mix 1"
        assert e0.body == "Alpha, Beta"
        assert e0.dedup_key == "reco:mix:alice:10"
        assert evs[1].dedup_key == "reco:mix:alice:11"
        d = (
            await s.execute(select(NotificationDelivery).order_by(NotificationDelivery.id))
        ).scalars().first()
        assert d.dispatch_state == "sent"  # inline dispatch fired
        assert d.seen_at is None  # unseen until viewed in the feed


async def test_idempotent_rerun_adds_no_duplicates():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await s.commit()

    first = await notify_daily_mixes()
    second = await notify_daily_mixes()  # same mix ids → per-(user, mix) dedup swallows
    assert first["notified"] == 2
    assert second["notified"] == 0
    assert await _count(NotificationDelivery) == 2


async def test_cold_start_no_mixes(monkeypatch):
    async def _empty(session, user_id):
        return []

    monkeypatch.setattr("app.services.user_mixes.get_session_mixes", _empty)
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_daily_mixes()
    assert result == {"users": 1, "with_mixes": 0, "notified": 0}
    assert await _count(NotificationEvent) == 0


async def test_empty_mix_is_skipped(monkeypatch):
    async def _one_empty(session, user_id):
        return [{"mix_id": 20, "ordinal": 1, "kind": "session", "track_count": 0, "tracks": []}]

    monkeypatch.setattr("app.services.user_mixes.get_session_mixes", _one_empty)
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_daily_mixes()
    assert result["notified"] == 0  # no streamable tracks → nothing to open
    assert await _count(NotificationEvent) == 0


async def test_opted_out_user_gets_nothing():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice", reco=False)
        await s.commit()

    result = await notify_daily_mixes()
    assert result["notified"] == 0
    assert await _count(NotificationEvent) == 0
