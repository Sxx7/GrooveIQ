"""GrooveIQ – Tests for the generic notification outbox: dispatch + producer.

Seeds a Device + NotificationEvent + NotificationDelivery in in-memory SQLite and
drives ``dispatch_pending`` / ``emit_notification`` directly, with Apprise mocked
— no network, no dependency on ``apprise`` being installed (``_apprise_notify`` is
monkeypatched wherever delivery is exercised).

Delivery is Apprise-only: an iOS device's ``apprise_urls`` holds the relay
capability URL; grooveiq holds no Apple creds / relay secret.

Run with:  .venv/bin/pytest tests/test_notification_dispatch.py -v
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.notification_dispatch as nd
from app.core.config import settings
from app.models.db import Base, Device, NotificationDelivery, NotificationEvent, ReleaseEvent
from app.services.notification_dispatch import (
    build_message,
    dispatch_pending,
    emit_notification,
    emit_recommendation,
    media_dedup_key,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)

_ALL_PREFS = {
    "notif_new_releases": True,
    "notif_new_media": True,
    "notif_download_finished": True,
    "notif_recommendations": True,
}


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _enable_push(monkeypatch):
    # push_enabled = PUSH_ENABLED and APPRISE_ENABLED; APPRISE_ENABLED defaults
    # True, so flipping the master switch is enough.
    monkeypatch.setattr(settings, "PUSH_ENABLED", True, raising=False)


@pytest.fixture(autouse=True)
def _apprise_ok(monkeypatch):
    # Default: Apprise "delivers". Tests that need a failure override this.
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)


async def _seed_delivery(
    *,
    user_id="alice",
    event_type="new_release",
    apns_token=None,
    apprise_urls=None,
    device_prefs=None,
    disabled_at=None,
    dedup_key="boards of canada|geogaddi",
    dispatch_state="pending",
    attempt_count=0,
    next_retry_at=None,
    created_at=None,
    with_device=True,
) -> int:
    """Create one event + one delivery (+ optional device). Returns delivery id."""
    now = int(time.time())
    async with _TestSession() as s:
        ev = NotificationEvent(
            event_type=event_type,
            dedup_key=dedup_key,
            title="New release",
            body='Boards of Canada just released the album "Geogaddi"',
            data={"type": event_type},
            created_at=now,
        )
        s.add(ev)
        await s.flush()
        if with_device and (apns_token is not None or apprise_urls is not None):
            prefs = dict(_ALL_PREFS)
            if device_prefs:
                prefs.update(device_prefs)
            s.add(
                Device(
                    user_id=user_id,
                    apns_token=apns_token,
                    apprise_urls=apprise_urls,
                    created_at=now,
                    last_seen_at=now,
                    disabled_at=disabled_at,
                    **prefs,
                )
            )
        d = NotificationDelivery(
            user_id=user_id,
            event_id=ev.id,
            event_type=event_type,
            dedup_key=dedup_key,
            dispatch_state=dispatch_state,
            attempt_count=attempt_count,
            next_retry_at=next_retry_at,
            created_at=created_at if created_at is not None else now,
        )
        s.add(d)
        await s.commit()
        return d.id


async def _delivery(delivery_id: int) -> NotificationDelivery:
    async with _TestSession() as s:
        return (
            await s.execute(select(NotificationDelivery).where(NotificationDelivery.id == delivery_id))
        ).scalar_one()


# ── message builder + dedup key ──────────────────────────────────────────────


def test_build_message_kinds():
    ev = ReleaseEvent(artist_name="BoC", album_title="Geogaddi", kind="ep")
    assert build_message(ev) == ("New release", 'BoC just released the EP "Geogaddi"')
    ev.kind = "single"
    assert build_message(ev)[1].startswith("BoC just released the single ")
    ev.kind = "weird"
    assert build_message(ev)[1].startswith("BoC just released the album ")


def test_media_dedup_key_normalizes():
    assert media_dedup_key("Boards of Canada", "Geogaddi") == "boards of canada|geogaddi"
    assert media_dedup_key("  BoC ", " Tomorrow's Harvest ") == "boc|tomorrow's harvest"
    assert media_dedup_key(None, None) is None
    # A download and a scan of the same record land on the same key.
    assert media_dedup_key("Artist", "Album") == media_dedup_key("artist", "album")
    # No album → no album-level identity → opt out of dedup, so distinct
    # album-less singles by one artist don't collide and suppress each other.
    assert media_dedup_key("Artist", None) is None
    assert media_dedup_key("Artist", "") is None
    assert media_dedup_key("Artist", "   ") is None


async def test_null_dedup_key_singles_do_not_collide():
    # Two distinct album-less "download finished" events for the same user both
    # deliver (dedup_key None → never collapsed).
    async with _TestSession() as s:
        a = await emit_notification(
            s, event_type="download_completed", title="t", body="single A", user_ids=["alice"], dedup_key=None
        )
        b = await emit_notification(
            s, event_type="download_completed", title="t", body="single B", user_ids=["alice"], dedup_key=None
        )
        await s.commit()
    assert a == 1 and b == 1
    assert await _count(NotificationDelivery) == 2


# ── dispatch ─────────────────────────────────────────────────────────────────


async def test_apprise_delivery_marks_sent(monkeypatch):
    d_id = await _seed_delivery(apprise_urls=["jsons://relay/v1/apprise/abc"])
    calls: list[tuple] = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: calls.append((urls, title)) or True)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 1
    row = await _delivery(d_id)
    assert row.dispatch_state == "sent"
    assert row.notified_at is not None
    assert calls and calls[0][0] == ["jsons://relay/v1/apprise/abc"]


async def test_apprise_failure_backs_off_and_stays_pending(monkeypatch):
    d_id = await _seed_delivery(apprise_urls=["ntfy://topic"])
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["retry"] == 1 and summary["failed"] == 0
    row = await _delivery(d_id)
    assert row.dispatch_state == "pending"  # retryable, not failed
    assert row.attempt_count == 1
    assert row.next_retry_at is not None and row.next_retry_at > int(time.time())


async def test_backoff_defers_next_attempt(monkeypatch):
    # A row whose next_retry_at is in the future is NOT reselected.
    future = int(time.time()) + 999
    d_id = await _seed_delivery(apprise_urls=["ntfy://topic"], attempt_count=1, next_retry_at=future)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary == {"processed": 0}
    assert (await _delivery(d_id)).dispatch_state == "pending"


async def test_apprise_failure_past_age_cap_fails(monkeypatch):
    old = int(time.time()) - 48 * 3600  # older than DISPATCH_MAX_AGE_HOURS (24)
    d_id = await _seed_delivery(apprise_urls=["ntfy://topic"], created_at=old)
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["failed"] == 1
    assert (await _delivery(d_id)).dispatch_state == "failed"  # aged out → give up


async def test_max_attempts_terminal(monkeypatch):
    # One more failure at NOTIFY_MAX_ATTEMPTS-1 tips it to 'failed'.
    d_id = await _seed_delivery(apprise_urls=["ntfy://topic"], attempt_count=settings.NOTIFY_MAX_ATTEMPTS - 1)
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["failed"] == 1
    assert (await _delivery(d_id)).dispatch_state == "failed"


async def test_no_channels_suppressed():
    # A device with only an apns_token (no apprise_urls) has nothing to deliver to
    # in the Apprise-only model → suppressed, never retried.
    d_id = await _seed_delivery(apns_token="tok", apprise_urls=None)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["suppressed"] == 1
    assert (await _delivery(d_id)).dispatch_state == "suppressed"


async def test_notif_toggle_off_suppressed():
    d_id = await _seed_delivery(apprise_urls=["ntfy://topic"], device_prefs={"notif_new_releases": False})

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["suppressed"] == 1
    assert (await _delivery(d_id)).dispatch_state == "suppressed"


async def test_per_type_pref_isolation():
    # A device opted OUT of downloads still gets new-release pushes, and vice-versa.
    d_id = await _seed_delivery(
        event_type="download_completed",
        dedup_key="artist|album",
        apprise_urls=["ntfy://topic"],
        device_prefs={"notif_download_finished": False},
    )

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["suppressed"] == 1  # download muted → suppressed
    assert (await _delivery(d_id)).dispatch_state == "suppressed"


async def test_legacy_null_pref_treated_as_opted_in():
    # A device whose per-type column is NULL (predates the migration) is opted-in.
    d_id = await _seed_delivery(
        event_type="newly_added",
        dedup_key="artist|album",
        apprise_urls=["ntfy://topic"],
        device_prefs={"notif_new_media": None},
    )

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 1
    assert (await _delivery(d_id)).dispatch_state == "sent"


async def test_disabled_device_suppressed():
    d_id = await _seed_delivery(apprise_urls=["ntfy://topic"], disabled_at=123)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["suppressed"] == 1
    assert (await _delivery(d_id)).dispatch_state == "suppressed"


async def test_disabled_feature_skips(monkeypatch):
    d_id = await _seed_delivery(apprise_urls=["ntfy://topic"])
    monkeypatch.setattr(settings, "PUSH_ENABLED", False, raising=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary == {"skipped": "disabled"}
    assert (await _delivery(d_id)).dispatch_state == "pending"  # untouched


async def test_idempotent_second_run_processes_nothing():
    await _seed_delivery(apprise_urls=["ntfy://topic"])

    async with _TestSession() as s:
        first = await dispatch_pending(s)
    async with _TestSession() as s:
        second = await dispatch_pending(s)

    assert first["sent"] == 1
    assert second == {"processed": 0}  # already sent → filtered out


# ── producer: emit_notification + dedup ──────────────────────────────────────


async def _count(model) -> int:
    async with _TestSession() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_emit_creates_event_and_deliveries():
    async with _TestSession() as s:
        created = await emit_notification(
            s,
            event_type="download_completed",
            title="Download finished",
            body='Your download of "X" finished',
            user_ids=["alice", "bob"],
            dedup_key="artist|album",
        )
        await s.commit()

    assert created == 2
    assert await _count(NotificationEvent) == 1
    assert await _count(NotificationDelivery) == 2


async def test_emit_dedups_same_user_same_key():
    # Two events (e.g. download then newly-added) for the same (user, album):
    # the second delivery collides on UNIQUE(user_id, dedup_key) and is dropped.
    async with _TestSession() as s:
        first = await emit_notification(
            s, event_type="download_completed", title="t", body="b", user_ids=["alice"], dedup_key="a|b"
        )
        second = await emit_notification(
            s, event_type="newly_added", title="t", body="b", user_ids=["alice", "carol"], dedup_key="a|b"
        )
        await s.commit()

    assert first == 1
    assert second == 1  # alice deduped, carol created
    assert await _count(NotificationDelivery) == 2
    assert await _count(NotificationEvent) == 2  # both events persist; only the delivery is deduped


async def test_emit_null_dedup_never_dedups():
    # Recommendations pass dedup_key=None → many deliveries for one user coexist.
    async with _TestSession() as s:
        await emit_notification(s, event_type="recommendation", title="t", body="b", user_ids=["alice"], dedup_key=None)
        await emit_notification(s, event_type="recommendation", title="t", body="b", user_ids=["alice"], dedup_key=None)
        await s.commit()

    assert await _count(NotificationDelivery) == 2


async def test_emit_dedups_repeated_user_in_one_call():
    async with _TestSession() as s:
        created = await emit_notification(
            s, event_type="newly_added", title="t", body="b", user_ids=["alice", "alice"], dedup_key="a|b"
        )
        await s.commit()

    assert created == 1  # the caller's duplicate user_id collapses to one delivery
    assert await _count(NotificationDelivery) == 1


# ── producer: recommendations (goal F) ───────────────────────────────────────


async def test_emit_recommendation_defaults():
    async with _TestSession() as s:
        created = await emit_recommendation(s, "alice")
        await s.commit()

    assert created == 1
    async with _TestSession() as s:
        ev = (await s.execute(select(NotificationEvent))).scalar_one()
        assert ev.event_type == "recommendation"
        assert ev.dedup_key is None  # no playlist → never dedups
        assert ev.data == {"type": "recommendation"}
        assert ev.title and ev.body


async def test_emit_recommendation_dedups_by_playlist():
    async with _TestSession() as s:
        first = await emit_recommendation(s, "alice", playlist_id="mix-42")
        second = await emit_recommendation(s, "alice", playlist_id="mix-42")
        await s.commit()

    assert first == 1
    assert second == 0  # same mix → not re-notified
    assert await _count(NotificationDelivery) == 1


async def test_recommendation_does_not_collide_with_media():
    # reco: and media keys are distinct namespaces → both deliveries created.
    async with _TestSession() as s:
        await emit_recommendation(s, "alice", playlist_id="mix-1")
        await emit_notification(
            s, event_type="newly_added", title="t", body="b", user_ids=["alice"], dedup_key="artist|album"
        )
        await s.commit()

    assert await _count(NotificationDelivery) == 2
