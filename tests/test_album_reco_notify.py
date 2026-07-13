"""GrooveIQ – Tests for the album-recommendation feed producer.

Same in-memory-SQLite harness as tests/test_reco_notify.py. Stubs recommend_albums
+ resolve_cover_art (the per-user roll-up and the cover lookup) and drives
``notify_album_recommendations`` with Apprise mocked. Covers the opted-in audience,
the rich event ``data`` (kind/cover/album), the per-(user, album) dedup (a re-run
adds no new rows), the disabled gate, and the opted-out / no-albums skips.

Run with:  .venv-test/bin/pytest tests/test_album_reco_notify.py -v
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.album_reco_notify as arn
import app.services.notification_dispatch as nd
from app.core.config import settings
from app.models.db import Base, Device, NotificationDelivery, NotificationEvent
from app.services.album_reco_notify import notify_album_recommendations

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


async def _fake_recommend_albums(session, user_id, *, mode="discover", limit=25):
    return {
        "mode": mode,
        "albums": [
            {
                "album": "Album One", "album_artist": "Artist A",
                "reasons": ["sounds like your taste"],
                "representative_tracks": [{"track_id": "t1", "media_server_id": "ms1"}],
            },
            {
                "album": "Album Two", "album_artist": "Artist B",
                "reasons": [], "representative_tracks": [],
            },
        ][:limit],
    }


async def _fake_cover(session, artist, title, client=None):
    return f"http://cover/{title.replace(' ', '_')}"


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(arn, "AsyncSessionLocal", _TestSession)
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)
    monkeypatch.setattr("app.services.album_reco.recommend_albums", _fake_recommend_albums)
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
    monkeypatch.setattr(settings, "PUSH_ENABLED", False, raising=False)
    assert await notify_album_recommendations() == {"skipped": "disabled"}
    assert await _count(NotificationEvent) == 0


async def test_emits_rich_album_rows():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_album_recommendations()
    assert result == {"users": 1, "with_albums": 1, "notified": 2}
    assert await _count(NotificationEvent) == 2
    assert await _count(NotificationDelivery) == 2

    async with _TestSession() as s:
        evs = (await s.execute(select(NotificationEvent).order_by(NotificationEvent.id))).scalars().all()
        assert all(e.event_type == "recommendation" for e in evs)
        e0 = evs[0]
        assert e0.data["type"] == "recommendation"  # push route → Discover
        assert e0.data["kind"] == "album"  # iOS renders an album row
        assert e0.data["cover_url"] == "http://cover/Album_One"
        assert e0.data["album"] == "Album One"
        assert e0.data["album_artist"] == "Artist A"
        assert e0.data["media_server_id"] == "ms1"
        assert e0.data["reason"] == "sounds like your taste"
        assert e0.dedup_key == "reco:album:alice:artist a|album one"
        d = (await s.execute(select(NotificationDelivery).order_by(NotificationDelivery.id))).scalars().first()
        assert d.dispatch_state == "sent"  # inline dispatch fired
        assert d.seen_at is None  # unseen until viewed in the feed


async def test_idempotent_rerun_adds_no_duplicates():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await s.commit()

    first = await notify_album_recommendations()
    second = await notify_album_recommendations()
    assert first["notified"] == 2
    assert second["notified"] == 0  # same albums → per-(user, album) dedup swallows
    assert await _count(NotificationDelivery) == 2


async def test_opted_out_user_gets_nothing():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice", reco=False)
        await s.commit()

    result = await notify_album_recommendations()
    assert result["notified"] == 0
    assert await _count(NotificationEvent) == 0


async def test_no_albums_skips(monkeypatch):
    async def _empty(session, user_id, *, mode="discover", limit=25):
        return {"mode": mode, "albums": []}

    monkeypatch.setattr("app.services.album_reco.recommend_albums", _empty)
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_album_recommendations()
    assert result == {"users": 1, "with_albums": 0, "notified": 0}
    assert await _count(NotificationEvent) == 0
