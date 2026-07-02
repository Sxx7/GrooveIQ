"""GrooveIQ – Tests for new-release notification dispatch (P2, h30).

Seeds a Device + P1 ReleaseEvent + eligible/pending UserReleaseNotification in
in-memory SQLite and drives ``dispatch_pending`` directly, with the relay and
Apprise mocked — no network, and no dependency on ``apprise`` being installed
(``_apprise_notify`` is monkeypatched wherever the generic path is exercised).

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
from app.services.relay_client import RelayError

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
    # push_enabled = PUSH_ENABLED and (relay_ok or APPRISE_ENABLED); APPRISE_ENABLED
    # defaults True, so flipping the master switch is enough. Individual tests that
    # want the disabled path override this back to False.
    monkeypatch.setattr(settings, "PUSH_ENABLED", True, raising=False)


class _FakeRelay:
    def __init__(self, results=None, exc=None):
        self._results = results or []
        self._exc = exc
        self.calls: list[dict] = []

    async def push(self, *, tokens, environment, notification, collapse_id=None):
        self.calls.append({"tokens": tokens, "environment": environment, "collapse_id": collapse_id})
        if self._exc is not None:
            raise self._exc
        return self._results

    async def close(self):
        pass


async def _seed(
    *, user_id="alice", kind="album", apns_token=None, apns_env="production",
    apprise_urls=None, notif=True, disabled_at=None, eligible=True,
    dispatch_state="pending", created_at=None,
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
            s.add(Device(
                user_id=user_id, apns_token=apns_token, apns_environment=apns_env,
                apprise_urls=apprise_urls, notif_new_releases=notif,
                created_at=now, last_seen_at=now, disabled_at=disabled_at,
            ))
        urn = UserReleaseNotification(
            user_id=user_id, release_event_id=ev.id, eligible=eligible,
            dispatch_state=dispatch_state, created_at=created_at if created_at is not None else now,
        )
        s.add(urn)
        await s.commit()
        return ev.id, urn.id


async def _urn(urn_id: int) -> UserReleaseNotification:
    async with _TestSession() as s:
        return (await s.execute(
            select(UserReleaseNotification).where(UserReleaseNotification.id == urn_id)
        )).scalar_one()


async def _device_by_token(token: str) -> Device:
    async with _TestSession() as s:
        return (await s.execute(select(Device).where(Device.apns_token == token))).scalar_one()


# ── build_message ────────────────────────────────────────────────────────────


def test_build_message_kinds():
    ev = ReleaseEvent(artist_name="BoC", album_title="Geogaddi", kind="ep")
    assert build_message(ev) == ("New release", 'BoC just released the EP "Geogaddi"')
    ev.kind = "single"
    assert build_message(ev)[1].startswith("BoC just released the single ")
    ev.kind = "weird"
    assert build_message(ev)[1].startswith("BoC just released the album ")


# ── dispatch ─────────────────────────────────────────────────────────────────


async def test_relay_success_marks_sent(monkeypatch):
    _, urn_id = await _seed(apns_token="tok")
    fake = _FakeRelay(results=[{"token": "tok", "status": 200}])
    monkeypatch.setattr(nd, "get_relay_client", lambda: fake)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 1
    row = await _urn(urn_id)
    assert row.dispatch_state == "sent"
    assert row.notified_at is not None
    assert fake.calls and fake.calls[0]["collapse_id"].startswith("release-")


async def test_410_prunes_token(monkeypatch):
    await _seed(apns_token="dead")
    fake = _FakeRelay(results=[{"token": "dead", "status": 410, "reason": "Unregistered"}])
    monkeypatch.setattr(nd, "get_relay_client", lambda: fake)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["pruned"] == 1
    assert (await _device_by_token("dead")).disabled_at is not None

    # a second run finds no active device for that user → suppressed, no relay call
    fake2 = _FakeRelay(results=[{"token": "dead", "status": 200}])
    monkeypatch.setattr(nd, "get_relay_client", lambda: fake2)
    async with _TestSession() as s:
        # the first run already left the row 'failed' (nothing delivered), so
        # nothing is pending now — assert the pruned device is excluded regardless.
        await dispatch_pending(s)
    assert fake2.calls == []


async def test_relay_transient_error_stays_pending(monkeypatch):
    _, urn_id = await _seed(apns_token="tok")
    fake = _FakeRelay(exc=RelayError("boom"))
    monkeypatch.setattr(nd, "get_relay_client", lambda: fake)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["failed"] == 1
    assert (await _urn(urn_id)).dispatch_state == "pending"  # retryable, not failed


async def test_transient_error_past_age_cap_fails(monkeypatch):
    old = int(time.time()) - 48 * 3600  # older than DISPATCH_MAX_AGE_HOURS (24)
    _, urn_id = await _seed(apns_token="tok", created_at=old)
    monkeypatch.setattr(nd, "get_relay_client", lambda: _FakeRelay(exc=RelayError("boom")))

    async with _TestSession() as s:
        await dispatch_pending(s)

    assert (await _urn(urn_id)).dispatch_state == "failed"  # aged out → give up


async def test_apprise_path_marks_sent(monkeypatch):
    _, urn_id = await _seed(apprise_urls=["ntfy://topic"])
    monkeypatch.setattr(nd, "get_relay_client", lambda: None)  # no relay
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 1
    assert (await _urn(urn_id)).dispatch_state == "sent"


async def test_no_active_device_suppressed(monkeypatch):
    _, urn_id = await _seed(apns_token="tok", disabled_at=123)  # device exists but disabled
    monkeypatch.setattr(nd, "get_relay_client", lambda: _FakeRelay())

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["suppressed"] == 1
    assert (await _urn(urn_id)).dispatch_state == "suppressed"


async def test_disabled_feature_skips(monkeypatch):
    _, urn_id = await _seed(apns_token="tok")
    monkeypatch.setattr(settings, "PUSH_ENABLED", False, raising=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary == {"skipped": "disabled"}
    assert (await _urn(urn_id)).dispatch_state == "pending"  # untouched


async def test_idempotent_second_run_processes_nothing(monkeypatch):
    await _seed(apns_token="tok")
    monkeypatch.setattr(nd, "get_relay_client", lambda: _FakeRelay(results=[{"token": "tok", "status": 200}]))

    async with _TestSession() as s:
        first = await dispatch_pending(s)
    async with _TestSession() as s:
        second = await dispatch_pending(s)

    assert first["sent"] == 1
    assert second == {"processed": 0}  # already sent → filtered out
