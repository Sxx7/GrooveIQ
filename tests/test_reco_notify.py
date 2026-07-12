"""GrooveIQ – Tests for goal F: the daily "your mix is ready" reco notification.

Seeds opted-in ``devices`` + active session ``mixes`` in in-memory SQLite and
drives ``notify_daily_mixes`` with Apprise mocked. Covers the audience intersection
(opted-in device AND a fresh session mix), the date-scoped dedup key (≤1 push per
user per UTC day even if a rebuild produced many mixes and the job re-runs), the
disabled gate, and the pref/soft-delete filters.

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
from app.models.db import Base, Device, Mix, NotificationDelivery, NotificationEvent
from app.services.reco_notify import notify_daily_mixes

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(rn, "AsyncSessionLocal", _TestSession)
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)
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


async def _add_mix(s, *, user_id="alice", kind="session", state="active", n=1):
    for _ in range(n):
        s.add(Mix(user_id=user_id, kind=kind, state=state, created_at=int(time.time())))


async def _count(model) -> int:
    async with _TestSession() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_disabled_skips(monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_RECOMMENDATIONS_ENABLED", False, raising=False)
    assert await notify_daily_mixes() == {"skipped": "disabled"}
    assert await _count(NotificationEvent) == 0


async def test_notifies_opted_in_user_with_mix():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await _add_mix(s, user_id="alice", n=6)  # six mixes → still ONE push
        await s.commit()

    result = await notify_daily_mixes()
    assert result["users"] == 1
    assert result["notified"] == 1
    assert await _count(NotificationEvent) == 1  # one event, not one per mix
    assert await _count(NotificationDelivery) == 1
    async with _TestSession() as s:
        ev = (await s.execute(select(NotificationEvent))).scalar_one()
        assert ev.event_type == "recommendation"
        assert ev.data == {"type": "recommendation"}
        assert ev.dedup_key.startswith("reco:daily:alice:")
        d = (await s.execute(select(NotificationDelivery))).scalar_one()
        assert d.dispatch_state == "sent"  # inline dispatch fired


async def test_idempotent_same_day():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await _add_mix(s, user_id="alice")
        await s.commit()

    now = int(time.time())
    first = await notify_daily_mixes(now=now)
    second = await notify_daily_mixes(now=now + 3600)  # later same UTC day
    assert first["notified"] == 1
    assert second["notified"] == 0  # same day → deduped
    assert await _count(NotificationDelivery) == 1


async def test_new_day_notifies_again():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await _add_mix(s, user_id="alice")
        await s.commit()

    day1 = 1_700_000_000  # a fixed epoch
    day2 = day1 + 86_400  # +1 day → different date bucket
    assert (await notify_daily_mixes(now=day1))["notified"] == 1
    assert (await notify_daily_mixes(now=day2))["notified"] == 1
    assert await _count(NotificationDelivery) == 2  # one per day


async def test_no_device_no_push():
    # A user with a fresh mix but no reachable device is not notified.
    async with _TestSession() as s:
        await _add_mix(s, user_id="alice")
        await s.commit()
    result = await notify_daily_mixes()
    assert result == {"users": 0, "notified": 0, "reason": "no_opted_in_devices"}
    assert await _count(NotificationDelivery) == 0


async def test_opted_out_device_no_push():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice", reco=False)
        await _add_mix(s, user_id="alice")
        await s.commit()
    result = await notify_daily_mixes()
    assert result["reason"] == "no_opted_in_devices"
    assert await _count(NotificationDelivery) == 0


async def test_disabled_device_no_push():
    async with _TestSession() as s:
        await _add_device(s, user_id="alice", disabled_at=int(time.time()))
        await _add_mix(s, user_id="alice")
        await s.commit()
    result = await notify_daily_mixes()
    assert result["reason"] == "no_opted_in_devices"
    assert await _count(NotificationDelivery) == 0


async def test_device_but_no_fresh_mix_no_push():
    # Cold-start user (only genre mixes, no active *session* mix) is skipped.
    async with _TestSession() as s:
        await _add_device(s, user_id="alice")
        await _add_mix(s, user_id="alice", state="archived")  # not active
        await s.commit()
    result = await notify_daily_mixes()
    assert result == {"users": 0, "notified": 0, "reason": "no_fresh_mixes"}
    assert await _count(NotificationDelivery) == 0


async def test_only_users_with_both_are_notified():
    async with _TestSession() as s:
        # alice: device + mix → notified
        await _add_device(s, user_id="alice")
        await _add_mix(s, user_id="alice")
        # bob: mix but no device → not notified
        await _add_mix(s, user_id="bob")
        # carol: device but no mix → not notified
        await _add_device(s, user_id="carol")
        await s.commit()

    result = await notify_daily_mixes()
    assert result["users"] == 1
    assert result["notified"] == 1
    async with _TestSession() as s:
        d = (await s.execute(select(NotificationDelivery))).scalar_one()
        assert d.user_id == "alice"
