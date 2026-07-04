"""GrooveIQ – Tests for goal C: newly-added-media notification (P4).

Seeds playable ``track_features`` + opted-in ``devices`` in in-memory SQLite and
drives ``notify_newly_added_media`` with Apprise mocked. Covers per-album
coalescing, the bulk-baseline storm guard, the per-scan cap + drain, dedup vs a
prior download event (goal D), and the per-track processed marker (idempotency).

Run with:  .venv-test/bin/pytest tests/test_new_media_notify.py -v
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.new_media_notify as nm
import app.services.notification_dispatch as nd
from app.core.config import settings
from app.models.db import (
    Base,
    Device,
    NotificationDelivery,
    NotificationEvent,
    TrackFeatures,
)
from app.services.new_media_notify import notify_newly_added_media
from app.services.notification_dispatch import emit_notification, media_dedup_key

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(nm, "AsyncSessionLocal", _TestSession)
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)
    monkeypatch.setattr(settings, "PUSH_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "NOTIFY_NEW_MEDIA_ENABLED", True, raising=False)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _add_track(s, *, artist, album, n=1, playable=True, processed=False):
    now = int(time.time())
    for i in range(n):
        tid = f"{artist}:{album}:{i}".lower()
        s.add(
            TrackFeatures(
                track_id=tid,
                file_path=f"/m/{tid}.mp3",
                artist=artist,
                album=album,
                media_server_id=(f"msid-{tid}" if playable else None),
                new_media_notified_at=(now if processed else None),
            )
        )


async def _add_device(s, *, user_id="alice", new_media=True):
    now = int(time.time())
    s.add(
        Device(
            user_id=user_id,
            apprise_urls=[f"jsons://relay/{user_id}"],
            notif_new_media=new_media,
            created_at=now,
            last_seen_at=now,
        )
    )


async def _count(model) -> int:
    async with _TestSession() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _processed_count() -> int:
    async with _TestSession() as s:
        return (
            await s.execute(
                select(func.count()).select_from(TrackFeatures).where(TrackFeatures.new_media_notified_at.isnot(None))
            )
        ).scalar_one()


async def test_disabled_skips(monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_NEW_MEDIA_ENABLED", False, raising=False)
    assert await notify_newly_added_media() == {"skipped": "disabled"}


async def test_per_album_coalesce_and_fanout():
    async with _TestSession() as s:
        await _add_track(s, artist="BoC", album="Geogaddi", n=12)  # 12 tracks → ONE album event
        await _add_track(s, artist="Aphex", album="SAW", n=3)
        await _add_device(s, user_id="alice")
        await _add_device(s, user_id="bob")
        await s.commit()

    result = await notify_newly_added_media()
    assert result["notified_albums"] == 2
    assert await _count(NotificationEvent) == 2  # one per album, not per track
    # 2 albums × 2 users = 4 deliveries
    assert await _count(NotificationDelivery) == 4
    assert await _processed_count() == 15  # all candidate tracks marked
    async with _TestSession() as s:
        d = (await s.execute(select(NotificationDelivery).limit(1))).scalar_one()
        assert d.dispatch_state == "sent"  # inline dispatch fired


async def test_bulk_baseline_no_push(monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_NEW_MEDIA_BASELINE_TRACKS", 5, raising=False)
    async with _TestSession() as s:
        await _add_track(s, artist="A", album="One", n=4)
        await _add_track(s, artist="B", album="Two", n=4)  # 8 candidates > 5 → baseline
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_newly_added_media()
    assert result["baselined"] == 8
    assert result["notified_albums"] == 0
    assert await _count(NotificationEvent) == 0
    assert await _processed_count() == 8  # everything marked, so it never fires later


async def test_no_audience_baselines():
    async with _TestSession() as s:
        await _add_track(s, artist="A", album="One", n=2)
        # a device that opted OUT of new_media is not audience
        await _add_device(s, user_id="alice", new_media=False)
        await s.commit()

    result = await notify_newly_added_media()
    assert result.get("baselined") == 2
    assert await _count(NotificationEvent) == 0
    assert await _processed_count() == 2


async def test_per_scan_cap_defers_rest(monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_NEW_MEDIA_MAX_ALBUMS_PER_SCAN", 2, raising=False)
    async with _TestSession() as s:
        for i in range(5):
            await _add_track(s, artist=f"Art{i}", album=f"Alb{i}", n=1)
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_newly_added_media()
    assert result["notified_albums"] == 2
    assert result["deferred_albums"] == 3
    assert await _count(NotificationEvent) == 2
    assert await _processed_count() == 2  # only the emitted albums marked; 3 remain for next scan

    # Next scan drains the next 2 (oldest-first FIFO, so no album is starved).
    result2 = await notify_newly_added_media()
    assert result2["notified_albums"] == 2
    assert await _processed_count() == 4

    # Third scan drains the final one — every deferred album eventually fires.
    result3 = await notify_newly_added_media()
    assert result3["notified_albums"] == 1
    assert await _processed_count() == 5


async def test_dedup_against_prior_download(monkeypatch):
    # goal D: a download already claimed this album's dedup key → the newly-added
    # delivery for that album is suppressed for that user.
    async with _TestSession() as s:
        await _add_track(s, artist="Artist", album="Album", n=2)
        await _add_device(s, user_id="alice")
        # simulate a prior download_completed delivery for the same album
        await emit_notification(
            s,
            event_type="download_completed",
            title="t",
            body="b",
            user_ids=["alice"],
            dedup_key=media_dedup_key("Artist", "Album"),
        )
        await s.commit()

    before = await _count(NotificationDelivery)
    await notify_newly_added_media()
    after = await _count(NotificationDelivery)
    assert after == before  # newly-added deduped against the download delivery


async def test_idempotent_second_run():
    async with _TestSession() as s:
        await _add_track(s, artist="BoC", album="Geogaddi", n=2)
        await _add_device(s, user_id="alice")
        await s.commit()

    first = await notify_newly_added_media()
    assert first["notified_albums"] == 1
    second = await notify_newly_added_media()
    assert second == {"candidates": 0}  # all marked processed → nothing to do


async def test_unplayable_tracks_not_candidates():
    async with _TestSession() as s:
        await _add_track(s, artist="BoC", album="Geogaddi", n=2, playable=False)  # no media_server_id
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_newly_added_media()
    assert result == {"candidates": 0}
    assert await _processed_count() == 0  # untouched — will be caught once playable


async def test_untagged_marked_and_skipped():
    async with _TestSession() as s:
        # a playable track with no artist/album → no usable dedup key
        s.add(
            TrackFeatures(
                track_id="mystery",
                file_path="/m/mystery.mp3",
                artist=None,
                album=None,
                media_server_id="msid-mystery",
            )
        )
        await _add_track(s, artist="BoC", album="Geogaddi", n=1)
        await _add_device(s, user_id="alice")
        await s.commit()

    result = await notify_newly_added_media()
    assert result["notified_albums"] == 1  # only the tagged album
    assert await _processed_count() == 2  # untagged one is marked too (won't linger)
