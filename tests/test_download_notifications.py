"""GrooveIQ – Tests for goal B: "download finished" notification (P3).

Drives ``download_watcher._emit_download_completed`` against a seeded
DownloadRequest + Device in in-memory SQLite, with Apprise mocked. Verifies the
emit → generic outbox → inline dispatch path, attribution gating, and that a
download event dedups a same-album newly-added event (goal D).

Run with:  .venv-test/bin/pytest tests/test_download_notifications.py -v
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.download_watcher as dw
import app.services.notification_dispatch as nd
from app.core.config import settings
from app.models.db import Base, Device, DownloadRequest, NotificationDelivery, NotificationEvent
from app.services.notification_dispatch import emit_notification, media_dedup_key

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # _emit_download_completed + dispatch use AsyncSessionLocal internally.
    monkeypatch.setattr(dw, "AsyncSessionLocal", _TestSession)
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)
    monkeypatch.setattr(settings, "PUSH_ENABLED", True, raising=False)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed_download(
    *, task_id="t1", user_id="alice", status="completed", artist="Boards of Canada", album="Geogaddi", with_device=True
) -> None:
    now = int(time.time())
    async with _TestSession() as s:
        s.add(
            DownloadRequest(
                task_id=task_id,
                status=status,
                source="spotdl",
                track_title="Dawn Chorus",
                artist_name=artist,
                album_name=album,
                user_id=user_id,
                updated_at=now,
            )
        )
        if with_device:
            s.add(
                Device(
                    user_id=user_id,
                    apprise_urls=["jsons://relay/v1/apprise/cap"],
                    notif_new_releases=True,
                    notif_new_media=True,
                    notif_download_finished=True,
                    notif_recommendations=True,
                    created_at=now,
                    last_seen_at=now,
                )
            )
        await s.commit()


async def _count(model) -> int:
    async with _TestSession() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_download_completed_emits_and_dispatches():
    await _seed_download()
    await dw._emit_download_completed("t1")

    assert await _count(NotificationEvent) == 1
    assert await _count(NotificationDelivery) == 1
    async with _TestSession() as s:
        ev = (await s.execute(select(NotificationEvent))).scalar_one()
        assert ev.event_type == "download_completed"
        assert ev.dedup_key == "boards of canada|geogaddi"
        assert "Dawn Chorus" in ev.body
        d = (await s.execute(select(NotificationDelivery))).scalar_one()
        assert d.dispatch_state == "sent"  # inline dispatch fired
        assert d.notified_at is not None


async def test_no_user_id_no_emit():
    # Chart/auto acquisition: no requester → no goal-B push (surfaces via goal C).
    await _seed_download(user_id=None, with_device=False)
    await dw._emit_download_completed("t1")
    assert await _count(NotificationEvent) == 0
    assert await _count(NotificationDelivery) == 0


async def test_not_completed_no_emit():
    await _seed_download(status="error")
    await dw._emit_download_completed("t1")
    assert await _count(NotificationDelivery) == 0


async def test_push_disabled_no_emit(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_ENABLED", False, raising=False)
    await _seed_download()
    await dw._emit_download_completed("t1")
    assert await _count(NotificationEvent) == 0


async def test_download_dedups_later_newly_added(monkeypatch):
    # goal D: after a download push for an album, a newly-added event for the same
    # (user, album) collides on UNIQUE(user_id, dedup_key) and is suppressed.
    await _seed_download(artist="Artist", album="Album")
    await dw._emit_download_completed("t1")
    assert await _count(NotificationDelivery) == 1

    async with _TestSession() as s:
        created = await emit_notification(
            s,
            event_type="newly_added",
            title="New media",
            body="Album added",
            user_ids=["alice"],
            dedup_key=media_dedup_key("Artist", "Album"),
        )
        await s.commit()

    assert created == 0  # deduped against the download delivery
    assert await _count(NotificationDelivery) == 1  # still just the download one
