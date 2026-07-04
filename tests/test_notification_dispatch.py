"""GrooveIQ – Tests for new-release notification dispatch (P2).

Seeds a Device + P1 ReleaseEvent + eligible/pending UserReleaseNotification in
in-memory SQLite and drives ``dispatch_pending`` directly, with Apprise mocked —
no network, and no dependency on ``apprise`` being installed (``_apprise_notify``
is monkeypatched wherever delivery is exercised).

Delivery is Apprise-only: an iOS device's ``apprise_urls`` holds the relay
capability URL; grooveiq holds no Apple creds / relay secret.

Run with:  .venv-test/bin/pytest tests/test_notification_dispatch.py -v
"""

from __future__ import annotations

import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.notification_dispatch as nd
from app.core.config import settings
from app.models.db import Base, Device, ReleaseEvent, UserReleaseNotification
from app.services.notification_dispatch import build_message, dispatch_pending

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


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
    # True, so flipping the master switch is enough. The disabled-path test
    # overrides PUSH_ENABLED back to False.
    monkeypatch.setattr(settings, "PUSH_ENABLED", True, raising=False)


@pytest.fixture(autouse=True)
def _apprise_ok(monkeypatch):
    # Default: Apprise "delivers". Tests that need a failure override this.
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)


async def _seed(
    *,
    user_id="alice",
    kind="album",
    apns_token=None,
    apns_env="production",
    apprise_urls=None,
    notif=True,
    disabled_at=None,
    eligible=True,
    dispatch_state="pending",
    created_at=None,
) -> tuple[int, int]:
    now = int(time.time())
    async with _TestSession() as s:
        ev = ReleaseEvent(
            release_key=f"rk-{uuid.uuid4().hex}",
            artist_mbid="mbid-x",
            artist_name="Boards of Canada",
            artist_name_norm="boards of canada",
            album_title="Geogaddi",
            album_title_norm="geogaddi",
            kind=kind,
            detected_at=now,
            acquisition_state="imported",
            available_at=now,
            source="streamrip_poll",
            created_at=now,
            updated_at=now,
        )
        s.add(ev)
        await s.flush()
        if apns_token is not None or apprise_urls is not None:
            s.add(
                Device(
                    user_id=user_id,
                    apns_token=apns_token,
                    apns_environment=apns_env,
                    apprise_urls=apprise_urls,
                    notif_new_releases=notif,
                    created_at=now,
                    last_seen_at=now,
                    disabled_at=disabled_at,
                )
            )
        urn = UserReleaseNotification(
            user_id=user_id,
            release_event_id=ev.id,
            eligible=eligible,
            dispatch_state=dispatch_state,
            created_at=created_at if created_at is not None else now,
        )
        s.add(urn)
        await s.commit()
        return ev.id, urn.id


async def _urn(urn_id: int) -> UserReleaseNotification:
    async with _TestSession() as s:
        return (
            await s.execute(select(UserReleaseNotification).where(UserReleaseNotification.id == urn_id))
        ).scalar_one()


# ── build_message ────────────────────────────────────────────────────────────


def test_build_message_kinds():
    ev = ReleaseEvent(artist_name="BoC", album_title="Geogaddi", kind="ep")
    assert build_message(ev) == ("New release", 'BoC just released the EP "Geogaddi"')
    ev.kind = "single"
    assert build_message(ev)[1].startswith("BoC just released the single ")
    ev.kind = "weird"
    assert build_message(ev)[1].startswith("BoC just released the album ")


# ── dispatch ─────────────────────────────────────────────────────────────────


async def test_apprise_delivery_marks_sent(monkeypatch):
    _, urn_id = await _seed(apprise_urls=["jsons://relay/v1/apprise/abc"])
    calls: list[tuple] = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: calls.append((urls, title)) or True)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 1
    row = await _urn(urn_id)
    assert row.dispatch_state == "sent"
    assert row.notified_at is not None
    assert calls and calls[0][0] == ["jsons://relay/v1/apprise/abc"]


async def test_apprise_failure_stays_pending(monkeypatch):
    _, urn_id = await _seed(apprise_urls=["ntfy://topic"])
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["failed"] == 1
    assert (await _urn(urn_id)).dispatch_state == "pending"  # retryable, not failed


async def test_apprise_failure_past_age_cap_fails(monkeypatch):
    old = int(time.time()) - 48 * 3600  # older than DISPATCH_MAX_AGE_HOURS (24)
    _, urn_id = await _seed(apprise_urls=["ntfy://topic"], created_at=old)
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: False)

    async with _TestSession() as s:
        await dispatch_pending(s)

    assert (await _urn(urn_id)).dispatch_state == "failed"  # aged out → give up


async def test_no_channels_suppressed():
    # A device with only an apns_token (no apprise_urls) has nothing to deliver to
    # in the Apprise-only model → suppressed, never retried.
    _, urn_id = await _seed(apns_token="tok", apprise_urls=None)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["suppressed"] == 1
    assert (await _urn(urn_id)).dispatch_state == "suppressed"


async def test_notif_toggle_off_suppressed():
    _, urn_id = await _seed(apprise_urls=["ntfy://topic"], notif=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["suppressed"] == 1
    assert (await _urn(urn_id)).dispatch_state == "suppressed"


async def test_disabled_device_suppressed():
    _, urn_id = await _seed(apprise_urls=["ntfy://topic"], disabled_at=123)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["suppressed"] == 1
    assert (await _urn(urn_id)).dispatch_state == "suppressed"


async def test_disabled_feature_skips(monkeypatch):
    _, urn_id = await _seed(apprise_urls=["ntfy://topic"])
    monkeypatch.setattr(settings, "PUSH_ENABLED", False, raising=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary == {"skipped": "disabled"}
    assert (await _urn(urn_id)).dispatch_state == "pending"  # untouched


async def test_idempotent_second_run_processes_nothing():
    await _seed(apprise_urls=["ntfy://topic"])

    async with _TestSession() as s:
        first = await dispatch_pending(s)
    async with _TestSession() as s:
        second = await dispatch_pending(s)

    assert first["sent"] == 1
    assert second == {"processed": 0}  # already sent → filtered out
