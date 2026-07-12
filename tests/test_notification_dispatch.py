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
    device_tz=None,
    device_qh_enabled=None,
    device_qh_start=None,
    device_qh_end=None,
    device_cadence=None,
    device_extra=None,
    notified_at=None,
    last_error=None,
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
                    tz=device_tz,
                    quiet_hours_enabled=device_qh_enabled,
                    quiet_hours_start=device_qh_start,
                    quiet_hours_end=device_qh_end,
                    notif_cadence=device_cadence,
                    notif_extra=device_extra,
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
            notified_at=notified_at,
            last_error=last_error,
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


async def test_duplicate_url_across_devices_sent_once(monkeypatch):
    # Regression: two active device rows sharing ONE capability URL (a pre-guid
    # row + its re-registration) must not deliver the same push twice.
    now = int(time.time())
    async with _TestSession() as s:
        ev = NotificationEvent(event_type="new_release", dedup_key="a|b", title="t", body="b", created_at=now)
        s.add(ev)
        await s.flush()
        for _ in range(2):
            s.add(
                Device(
                    user_id="alice",
                    apprise_urls=["jsons://relay/same-cap"],
                    created_at=now,
                    last_seen_at=now,
                    **_ALL_PREFS,
                )
            )
        s.add(
            NotificationDelivery(
                user_id="alice", event_id=ev.id, event_type="new_release", dedup_key="a|b",
                dispatch_state="pending", created_at=now,
            )
        )
        await s.commit()

    captured: list[list[str]] = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: captured.append(list(urls)) or True)
    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 1
    assert captured == [["jsons://relay/same-cap"]]  # deduped to a single target


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


# ── Phase 2: quiet-hours helpers (pure) ──────────────────────────────────────

_EPOCH_2213_UTC = 1_700_000_000  # 2023-11-14 22:13:20 UTC → local hour 22 (UTC)


def test_in_quiet_hours_utc_wrap_and_zero_width():
    assert nd._in_quiet_hours(_EPOCH_2213_UTC, "UTC", 22, 8) is True  # 22 in wrapping [22,8)
    assert nd._in_quiet_hours(_EPOCH_2213_UTC, "UTC", 8, 22) is False  # 22 not in [8,22)
    assert nd._in_quiet_hours(_EPOCH_2213_UTC, "UTC", 0, 0) is False  # zero-width → disabled


def test_in_quiet_hours_respects_timezone():
    # 22:13 UTC is 17:13 in New York (UTC-5 in Nov) → outside a 22→8 window.
    assert nd._in_quiet_hours(_EPOCH_2213_UTC, "America/New_York", 22, 8) is False
    # A bad/unknown tz falls back to UTC, so it reads as quiet like the UTC case.
    assert nd._in_quiet_hours(_EPOCH_2213_UTC, "Not/AZone", 22, 8) is True
    assert nd._in_quiet_hours(_EPOCH_2213_UTC, None, 22, 8) is True  # tz-less → UTC


def test_quiet_window_open_is_after_now_and_not_itself_quiet():
    w = nd._quiet_window_open(_EPOCH_2213_UTC, "UTC", 22, 8)
    assert w > _EPOCH_2213_UTC
    assert nd._in_quiet_hours(w, "UTC", 22, 8) is False  # opens exactly when the window closes


# ── Phase 2: daily budget + quiet-hours dispatch gating ──────────────────────


def _window_containing_now() -> tuple[int, int]:
    """A 2h quiet window [h, h+2) that contains the current UTC hour, so a
    UTC-tz device is deterministically inside quiet hours during the test."""
    h = time.gmtime().tm_hour
    return h, (h + 2) % 24


async def test_daily_budget_drops_lowest_priority(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DAILY_BUDGET", 1, raising=False)
    nr = await _seed_delivery(user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"], dedup_key="nr")
    rc = await _seed_delivery(user_id="u", event_type="recommendation", dedup_key=None, with_device=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 1
    assert summary.get("budget_dropped") == 1
    assert (await _delivery(nr)).dispatch_state == "sent"  # higher-priority type kept
    rc_row = await _delivery(rc)
    assert rc_row.dispatch_state == "suppressed"  # lowest-priority dropped when budget is tight
    assert rc_row.last_error == "daily_budget_exceeded"


async def test_budget_zero_is_unlimited(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DAILY_BUDGET", 0, raising=False)
    await _seed_delivery(user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"], dedup_key="a")
    await _seed_delivery(user_id="u", event_type="newly_added", dedup_key="b", with_device=False)
    await _seed_delivery(user_id="u", event_type="recommendation", dedup_key=None, with_device=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 3
    assert "budget_dropped" not in summary


async def test_download_exempt_from_budget(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DAILY_BUDGET", 1, raising=False)
    now = int(time.time())
    # A prior send today already fills the user's budget.
    await _seed_delivery(
        user_id="u",
        event_type="new_release",
        apprise_urls=["jsons://r/u"],
        dedup_key="prior",
        dispatch_state="sent",
        notified_at=now,
    )
    dl = await _seed_delivery(user_id="u", event_type="download_completed", dedup_key="dl", with_device=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert (await _delivery(dl)).dispatch_state == "sent"  # exempt: sent despite a full budget
    assert summary["sent"] == 1
    assert "budget_dropped" not in summary


async def test_quiet_hours_holds_non_urgent(monkeypatch):
    start, end = _window_containing_now()
    monkeypatch.setattr(settings, "QUIET_HOURS_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_START", start, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_END", end, raising=False)
    calls: list = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda u, t, b: calls.append(u) or True)
    d = await _seed_delivery(user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"], device_tz="UTC")

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary.get("held") == 1
    assert not calls  # nothing actually delivered during quiet hours
    row = await _delivery(d)
    assert row.dispatch_state == "pending"  # deferred, not dropped
    assert row.next_retry_at is not None and row.next_retry_at > int(time.time())
    assert row.attempt_count == 0  # a hold is not a failed attempt
    assert row.last_error == "quiet_hours_hold"


async def test_download_exempt_from_quiet_hours(monkeypatch):
    start, end = _window_containing_now()
    monkeypatch.setattr(settings, "QUIET_HOURS_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_START", start, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_END", end, raising=False)
    d = await _seed_delivery(
        user_id="u", event_type="download_completed", apprise_urls=["jsons://r/u"], device_tz="UTC"
    )

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 1  # the one push the user is waiting for goes through quiet hours
    assert (await _delivery(d)).dispatch_state == "sent"


# ── Phase 3: digest rollup ───────────────────────────────────────────────────


def test_humanize_list():
    assert nd._humanize_list([]) == ""
    assert nd._humanize_list(["A"]) == "A"
    assert nd._humanize_list(["A", "B"]) == "A and B"
    assert nd._humanize_list(["A", "B", "C"]) == "A, B and C"


def test_build_digest_single_keeps_original():
    e = NotificationEvent(
        event_type="newly_added", title="New music added", body='Kind of Blue added', data={"type": "new_media"}
    )
    title, body, data = nd._build_digest("newly_added", [e])
    assert (title, body) == ("New music added", "Kind of Blue added")
    assert data == {"type": "new_media"}


def test_build_digest_multi_counts_and_names():
    e1 = NotificationEvent(event_type="newly_added", title="t", body="b", data={"type": "new_media", "artist": "Miles Davis"})
    e2 = NotificationEvent(event_type="newly_added", title="t", body="b", data={"type": "new_media", "artist": "Portishead"})
    title, body, data = nd._build_digest("newly_added", [e1, e2])
    assert title == "2 new albums added"
    assert "Miles Davis" in body and "Portishead" in body
    assert data == {"type": "new_media", "digest": True, "count": 2}


def test_build_digest_new_release_and_download_wording():
    r1 = NotificationEvent(event_type="new_release", title="t", body="b", data={"type": "new_release", "artist": "Beach House"})
    r2 = NotificationEvent(event_type="new_release", title="t", body="b", data={"type": "new_release", "artist": "Aphex Twin"})
    assert nd._build_digest("new_release", [r1, r2])[0] == "2 new releases"
    d1 = NotificationEvent(event_type="download_completed", title="t", body="b", data={"type": "download_finished"})
    d2 = NotificationEvent(event_type="download_completed", title="t", body="b", data={"type": "download_finished"})
    title, body, _ = nd._build_digest("download_completed", [d1, d2])
    assert title == "Downloads ready" and body == "2 downloads finished."


async def test_digest_coalesces_same_type_burst(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DIGEST_ENABLED", True, raising=False)
    calls: list = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: calls.append((title, body)) or True)
    ids = [await _seed_delivery(user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"], dedup_key="a1")]
    ids.append(await _seed_delivery(user_id="u", event_type="newly_added", dedup_key="a2", with_device=False))
    ids.append(await _seed_delivery(user_id="u", event_type="newly_added", dedup_key="a3", with_device=False))

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 3  # three deliveries marked sent
    assert summary["pushes"] == 1  # via a single coalesced push
    assert len(calls) == 1 and calls[0][0] == "3 new albums added"
    for d_id in ids:
        assert (await _delivery(d_id)).dispatch_state == "sent"


async def test_digest_off_sends_per_item(monkeypatch):
    calls: list = []  # default NOTIF_DIGEST_ENABLED is False
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: calls.append(title) or True)
    await _seed_delivery(user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"], dedup_key="a1")
    await _seed_delivery(user_id="u", event_type="newly_added", dedup_key="a2", with_device=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 2
    assert "pushes" not in summary  # no coalescing → one push each
    assert len(calls) == 2


async def test_digest_is_per_type_not_cross_type(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DIGEST_ENABLED", True, raising=False)
    calls: list = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: calls.append(title) or True)
    await _seed_delivery(user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"], dedup_key="m1")
    await _seed_delivery(user_id="u", event_type="newly_added", dedup_key="m2", with_device=False)
    await _seed_delivery(user_id="u", event_type="new_release", dedup_key="r1", with_device=False)
    await _seed_delivery(user_id="u", event_type="new_release", dedup_key="r2", with_device=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 4
    assert summary["pushes"] == 2  # one digest per type-family, not one across both
    assert set(calls) == {"2 new albums added", "2 new releases"}


async def test_digest_counts_as_one_budget_unit(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DIGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "NOTIF_DAILY_BUDGET", 1, raising=False)
    await _seed_delivery(user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"], dedup_key="a1")
    for i in range(2, 6):
        await _seed_delivery(user_id="u", event_type="newly_added", dedup_key=f"a{i}", with_device=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary["sent"] == 5  # five albums coalesce into one push = one budget unit
    assert summary["pushes"] == 1
    assert "budget_dropped" not in summary


async def test_digest_group_held_together_in_quiet_hours(monkeypatch):
    start, end = _window_containing_now()
    monkeypatch.setattr(settings, "NOTIF_DIGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_START", start, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_END", end, raising=False)
    calls: list = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda u, t, b: calls.append(u) or True)
    await _seed_delivery(user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"], device_tz="UTC", dedup_key="a1")
    await _seed_delivery(user_id="u", event_type="newly_added", device_tz="UTC", dedup_key="a2", with_device=False)

    async with _TestSession() as s:
        summary = await dispatch_pending(s)

    assert summary.get("held") == 2  # the whole group is deferred together
    assert not calls


# ── Phase 5: per-user quiet-hours override ───────────────────────────────────


def _window_not_containing_now() -> tuple[int, int]:
    """A 2h window that does NOT contain the current UTC hour (so a UTC-tz device
    is deterministically OUTSIDE quiet hours during the test)."""
    h = time.gmtime().tm_hour
    return (h + 3) % 24, (h + 5) % 24


async def test_per_user_quiet_hours_enables_when_global_off(monkeypatch):
    monkeypatch.setattr(settings, "QUIET_HOURS_ENABLED", False, raising=False)  # global OFF
    s, e = _window_containing_now()
    calls: list = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda u, t, b: calls.append(u) or True)
    d = await _seed_delivery(
        user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"],
        device_tz="UTC", device_qh_enabled=True, device_qh_start=s, device_qh_end=e,
    )

    async with _TestSession() as sess:
        summary = await dispatch_pending(sess)

    assert summary.get("held") == 1  # per-user override turns quiet hours on
    assert not calls
    assert (await _delivery(d)).dispatch_state == "pending"


async def test_per_user_quiet_hours_optout_overrides_global_on(monkeypatch):
    s, e = _window_containing_now()
    monkeypatch.setattr(settings, "QUIET_HOURS_ENABLED", True, raising=False)  # global ON, window now
    monkeypatch.setattr(settings, "QUIET_HOURS_START", s, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_END", e, raising=False)
    d = await _seed_delivery(
        user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"],
        device_tz="UTC", device_qh_enabled=False,  # explicit per-user opt-out
    )

    async with _TestSession() as sess:
        summary = await dispatch_pending(sess)

    assert summary["sent"] == 1  # opted out → sent despite the global window
    assert "held" not in summary
    assert (await _delivery(d)).dispatch_state == "sent"


async def test_per_user_quiet_hours_custom_window_not_now(monkeypatch):
    monkeypatch.setattr(settings, "QUIET_HOURS_ENABLED", False, raising=False)  # global OFF
    s, e = _window_not_containing_now()
    d = await _seed_delivery(
        user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"],
        device_tz="UTC", device_qh_enabled=True, device_qh_start=s, device_qh_end=e,
    )

    async with _TestSession() as sess:
        summary = await dispatch_pending(sess)

    assert summary["sent"] == 1  # the user's own window does not cover now → sent
    assert "held" not in summary
    assert (await _delivery(d)).dispatch_state == "sent"


async def test_global_quiet_hours_applies_without_user_override(monkeypatch):
    s, e = _window_containing_now()
    monkeypatch.setattr(settings, "QUIET_HOURS_ENABLED", True, raising=False)  # global ON, window now
    monkeypatch.setattr(settings, "QUIET_HOURS_START", s, raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_END", e, raising=False)
    # device has NO quiet-hours override (enabled NULL) → falls back to the global.
    d = await _seed_delivery(user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"], device_tz="UTC")

    async with _TestSession() as sess:
        summary = await dispatch_pending(sess)

    assert summary.get("held") == 1
    assert (await _delivery(d)).dispatch_state == "pending"


# ── Phase 6: per-type daily cadence ──────────────────────────────────────────


def _hour_not_now() -> int:
    return (time.gmtime().tm_hour + 3) % 24


def test_next_daily_time_is_future_at_hour():
    now = 1_700_000_000  # 2023-11-14 22:13:20 UTC
    for hour in (0, 9, 22, 23):
        t = nd._next_daily_time(now, hour)
        assert t > now
        assert time.gmtime(t).tm_hour == hour


async def test_daily_cadence_holds_outside_digest_window(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DAILY_DIGEST_HOUR", _hour_not_now(), raising=False)
    calls: list = []
    monkeypatch.setattr(nd, "_apprise_notify", lambda u, t, b: calls.append(u) or True)
    d = await _seed_delivery(
        user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"],
        device_cadence={"notif_new_media": "daily"},
    )

    async with _TestSession() as sess:
        summary = await dispatch_pending(sess)

    assert summary.get("deferred") == 1  # held for the daily digest window
    assert not calls
    row = await _delivery(d)
    assert row.dispatch_state == "pending"
    assert row.next_retry_at is not None and row.next_retry_at > int(time.time())
    assert row.attempt_count == 0  # a cadence hold is not a failure
    assert row.last_error == "daily_cadence_hold"


async def test_daily_cadence_releases_in_digest_window(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DAILY_DIGEST_HOUR", time.gmtime().tm_hour, raising=False)  # window = now
    d = await _seed_delivery(
        user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"],
        device_cadence={"notif_new_media": "daily"},
    )

    async with _TestSession() as sess:
        summary = await dispatch_pending(sess)

    assert summary["sent"] == 1  # digest window → the hold lifts and it sends
    assert "deferred" not in summary
    assert (await _delivery(d)).dispatch_state == "sent"


async def test_instant_cadence_not_deferred(monkeypatch):
    monkeypatch.setattr(settings, "NOTIF_DAILY_DIGEST_HOUR", _hour_not_now(), raising=False)
    await _seed_delivery(
        user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"],
        device_cadence={"notif_new_media": "instant"},
    )

    async with _TestSession() as sess:
        summary = await dispatch_pending(sess)

    assert summary["sent"] == 1  # instant is unaffected
    assert "deferred" not in summary


async def test_daily_cadence_only_affects_its_type(monkeypatch):
    # new_media set to daily; a new_release delivery keeps the instant default.
    monkeypatch.setattr(settings, "NOTIF_DAILY_DIGEST_HOUR", _hour_not_now(), raising=False)
    d = await _seed_delivery(
        user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"],
        device_cadence={"notif_new_media": "daily"}, dedup_key="rel",
    )

    async with _TestSession() as sess:
        summary = await dispatch_pending(sess)

    assert summary["sent"] == 1  # new_release has no daily cadence → sent now
    assert "deferred" not in summary
    assert (await _delivery(d)).dispatch_state == "sent"


# ── Fix regressions ──────────────────────────────────────────────────────────


async def test_budget_counts_pushes_not_deliveries(monkeypatch):
    # Fix #2: a prior digest of 5 albums is ONE push, so a later recommendation must
    # still be allowed under a budget of 3. The old code counted 5 sent DELIVERIES
    # and wrongly suppressed everything else for the rest of the UTC day.
    monkeypatch.setattr(settings, "NOTIF_DIGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "NOTIF_DAILY_BUDGET", 3, raising=False)
    # Run 1: a 5-album newly_added burst coalesces into one push.
    await _seed_delivery(user_id="u", event_type="newly_added", apprise_urls=["jsons://r/u"], dedup_key="a0")
    for i in range(1, 5):
        await _seed_delivery(user_id="u", event_type="newly_added", dedup_key=f"a{i}", with_device=False)
    async with _TestSession() as s:
        r1 = await dispatch_pending(s)
    assert r1["sent"] == 5 and r1["pushes"] == 1

    # Run 2 (same UTC day): a recommendation. Budget is a PUSH budget → 1 of 3 used.
    rc = await _seed_delivery(user_id="u", event_type="recommendation", dedup_key="rc", with_device=False)
    async with _TestSession() as s:
        r2 = await dispatch_pending(s)
    assert (await _delivery(rc)).dispatch_state == "sent"
    assert "budget_dropped" not in r2


async def test_daily_cadence_quiet_held_row_is_released_not_re_held(monkeypatch):
    # Fix #1: a daily-cadence row already parked by quiet hours must not be re-grabbed
    # by the cadence gate (UTC digest hour), or it ping-pongs against the user's local
    # quiet-close forever. Simulate the post-quiet-hold moment: outside the digest
    # window and no longer in quiet.
    now = int(time.time())
    monkeypatch.setattr(settings, "NOTIF_DAILY_DIGEST_HOUR", _hour_not_now(), raising=False)
    monkeypatch.setattr(settings, "QUIET_HOURS_ENABLED", False, raising=False)
    d = await _seed_delivery(
        user_id="u", event_type="new_release", apprise_urls=["jsons://r/u"],
        device_cadence={"notif_new_releases": "daily"},
        last_error="quiet_hours_hold", next_retry_at=now - 1, dedup_key="rel",
    )
    async with _TestSession() as s:
        summary = await dispatch_pending(s)
    assert (await _delivery(d)).dispatch_state == "sent"  # released, not re-deferred
    assert summary.get("deferred", 0) == 0


async def test_group_error_does_not_abort_the_drain(monkeypatch):
    # Fix #4: one group's error must leave earlier groups' sent state intact and not
    # blow up the whole drain (the per-group savepoint keeps the tx healthy on
    # Postgres; on SQLite this proves the session stays usable after a mid-group raise).
    d1 = await _seed_delivery(user_id="u1", event_type="new_release", apprise_urls=["jsons://r/u1"], dedup_key="k1")
    d2 = await _seed_delivery(user_id="u2", event_type="new_release", apprise_urls=["jsons://r/u2"], dedup_key="k2")
    real = nd._channels_for

    async def flaky(session, user_id, event_type):
        if user_id == "u2":
            raise RuntimeError("boom")
        return await real(session, user_id, event_type)

    monkeypatch.setattr(nd, "_channels_for", flaky)
    async with _TestSession() as s:
        summary = await dispatch_pending(s)
    assert (await _delivery(d1)).dispatch_state == "sent"  # earlier group committed
    assert (await _delivery(d2)).dispatch_state == "pending"  # errored group left pending
    assert summary.get("errored", 0) == 1


async def test_notif_extra_gates_future_server_driven_type(monkeypatch):
    # Fix #6: a server-driven type whose pref_field has no dedicated column is gated
    # per-device by notif_extra (absent/True = opted-in, False = opted-out).
    monkeypatch.setitem(nd.NOTIF_PREF_COLUMNS, "podcast_new", "notif_podcasts")
    monkeypatch.setattr(nd, "_apprise_notify", lambda urls, title, body: True)
    off = await _seed_delivery(
        user_id="off", event_type="podcast_new", apprise_urls=["jsons://r/off"], dedup_key="p1",
        device_extra={"notif_podcasts": False},
    )
    on = await _seed_delivery(
        user_id="on", event_type="podcast_new", apprise_urls=["jsons://r/on"], dedup_key="p2",
        device_extra={"notif_podcasts": True},
    )
    absent = await _seed_delivery(
        user_id="abs", event_type="podcast_new", apprise_urls=["jsons://r/abs"], dedup_key="p3",
    )
    async with _TestSession() as s:
        await dispatch_pending(s)
    assert (await _delivery(off)).dispatch_state == "suppressed"  # opted out
    assert (await _delivery(on)).dispatch_state == "sent"  # opted in
    assert (await _delivery(absent)).dispatch_state == "sent"  # absent = opted in (fail open)
