"""GrooveIQ – generic notification outbox: dispatch + producer primitive.

A single, type-agnostic pipeline behind every notification (a followed artist's
new release, a finished manual download, newly-added media, a fresh
recommendation mix). Producers call :func:`emit_notification` to write ONE
``notification_events`` row plus a per-user ``notification_deliveries`` fan-out;
:func:`dispatch_pending` drains the deliveries and pushes them through Apprise.
Adding a notification type is a new producer + a device pref column — no changes
to the dispatcher.

Dedup (goal D): a delivery carries the event's ``dedup_key`` (media identity,
``norm(artist)|norm(album)``), and ``notification_deliveries`` has a unique
``(user_id, dedup_key)``. Two events that name the same media (e.g. a manual
download and the library scan that re-surfaces it) collide on the second insert,
which is swallowed — the user gets exactly one push. Precedence (download >
new_release > newly_added) is achieved by emit ORDER: the higher-precedence
producer writes first, so the lower one hits the constraint. A NULL ``dedup_key``
opts out of dedup (recommendations).

Delivery is **Apprise-only**. A user's iOS device registers a per-device
*capability URL* minted by the APN relay (``jsons://<relay>/v1/apprise/<id>``) as
one of its ``apprise_urls``; the relay holds Ampster's ``.p8`` and pushes to APNs.
grooveiq holds **no** Apple credentials and **no** relay shared secret. Non-iOS
channels (ntfy/telegram/...) ride the exact same path.

Retry is attempt-counted with exponential backoff on the delivery row
(``attempt_count`` / ``next_retry_at``), with ``DISPATCH_MAX_AGE_HOURS`` as a
final give-up cap. Apprise is a required dependency; the import is still guarded
inside ``_apprise_notify`` so a broken install degrades to "no delivery" instead
of crashing the run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import Device, NotificationDelivery, NotificationEvent, ReleaseEvent

logger = logging.getLogger(__name__)

# Single source of truth for notification types. ``key`` is the client/user-facing
# category (server-driven via GET /v1/notification-types, so a new type needs no
# app rebuild); ``event_type`` is the outbox discriminator producers emit;
# ``pref_field`` is the gating per-device Boolean column. Keep these in lockstep —
# everything else derives from this list.
NOTIFICATION_TYPES: list[dict[str, str]] = [
    {
        "key": "new_releases",
        "event_type": "new_release",
        "pref_field": "notif_new_releases",
        "label": "New Releases",
        "description": "A followed artist releases something new that's playable in your library.",
    },
    {
        "key": "new_media",
        "event_type": "newly_added",
        "pref_field": "notif_new_media",
        "label": "New Media",
        "description": "New music becomes available in your library, from any source.",
    },
    {
        "key": "downloads",
        "event_type": "download_completed",
        "pref_field": "notif_download_finished",
        "label": "Download Finished",
        "description": "A download you started from search finishes.",
    },
    {
        "key": "recommendations",
        "event_type": "recommendation",
        "pref_field": "notif_recommendations",
        "label": "Recommendations",
        "description": "A fresh recommendation mix is ready for you.",
    },
]

# event_type -> the per-device Boolean pref column that gates it. A device that
# predates a column reads NULL (legacy row); the channel filter treats NULL as
# opted-in (``isnot(False)``) to match the model default + the shipped iOS
# toggles. An event_type absent here delivers to all active devices (with a warn)
# so a new producer is never silently dropped for want of a pref column.
NOTIF_PREF_COLUMNS: dict[str, str] = {t["event_type"]: t["pref_field"] for t in NOTIFICATION_TYPES}
# All gating pref columns (for validation / bulk reads).
NOTIF_PREF_FIELDS: tuple[str, ...] = tuple(t["pref_field"] for t in NOTIFICATION_TYPES)

_KIND_NOUN = {"album": "the album", "ep": "the EP", "single": "the single", "track": "the track"}


# ---------------------------------------------------------------------------
# Dedup key + message builders (producers call these)
# ---------------------------------------------------------------------------


def _norm(s: str | None) -> str:
    """Lowercase + trim — MUST match ``release_scan._norm`` so the dedup key and
    the availability match stay in lockstep across producers."""
    return (s or "").strip().lower()


def media_dedup_key(artist: str | None, album: str | None) -> str | None:
    """Cross-type media identity for dedup: ``norm(artist)|norm(album)``.

    The collision unit is the ALBUM, so a manual download and the newly-added scan
    of the same record resolve to one delivery. Returns None (opt out of dedup)
    when there is no album — an album-less loose track has no album-level identity,
    and keying it on ``artist|`` alone would wrongly collapse every album-less
    single by that artist into one, silently suppressing distinct notifications.
    """
    b = _norm(album)
    if not b:
        return None
    return f"{_norm(artist)}|{b}"


def build_message(release: ReleaseEvent) -> tuple[str, str]:
    """(title, body) for a new-release event. Kind → natural noun; unknown → album."""
    noun = _KIND_NOUN.get((release.kind or "album").lower(), "the album")
    return "New release", f'{release.artist_name} just released {noun} "{release.album_title}"'


# ---------------------------------------------------------------------------
# Producer primitive
# ---------------------------------------------------------------------------


async def emit_notification(
    session: AsyncSession,
    *,
    event_type: str,
    title: str,
    body: str,
    user_ids: list[str],
    dedup_key: str | None = None,
    data: dict[str, Any] | None = None,
    now: int | None = None,
) -> int:
    """Write one :class:`NotificationEvent` and a per-user
    :class:`NotificationDelivery` fan-out. Returns the number of deliveries
    actually created (collisions on ``(user_id, dedup_key)`` are swallowed, so a
    re-emit or a lower-precedence duplicate is idempotent).

    The caller owns the transaction (no commit here) so an emit composes with the
    producer's own writes (e.g. the reconciler stamping ``available_at``). Each
    delivery insert is savepoint-isolated so one dedup collision can't roll back
    the batch.
    """
    now = now or int(time.time())
    event = NotificationEvent(
        event_type=event_type,
        dedup_key=dedup_key,
        title=title,
        body=body,
        data=data,
        created_at=now,
    )
    session.add(event)
    await session.flush()  # assign event.id for the FK

    created = 0
    for uid in dict.fromkeys(user_ids):  # de-dupe the caller's list, preserve order
        delivery = NotificationDelivery(
            user_id=uid,
            event_id=event.id,
            event_type=event_type,
            dedup_key=dedup_key,
            dispatch_state="pending",
            created_at=now,
        )
        try:
            async with session.begin_nested():  # savepoint: a dup can't kill the batch
                session.add(delivery)
                await session.flush()
            created += 1
        except IntegrityError:
            pass  # (user, dedup_key) already has a delivery — deduped (goal D)
    return created


async def emit_recommendation(
    session: AsyncSession,
    user_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    playlist_id: str | None = None,
    dedup_key: str | None = None,
    data_extra: dict[str, Any] | None = None,
    now: int | None = None,
) -> int:
    """Goal F: emit a ``recommendation`` notification to one user — a drop-in on
    the outbox (no dispatch plumbing). Call this wherever a per-user mix is built
    (a discover-weekly cron, an inactivity nudge, ...); the caller owns the
    commit + dispatch.

    Dedup: ``reco:*`` keys live in a distinct namespace from the media key, so a
    reco never collides with A/B/C. An explicit ``dedup_key`` wins — the daily-mix
    producer passes a date-scoped ``reco:daily:{user}:{YYYY-MM-DD}`` so ONE push
    lands per user per day no matter how many mixes (or fresh playlist ids) a
    rebuild produced. When omitted it falls back to ``reco:{playlist_id}`` (NULL
    when no playlist id), keeping the admin/QA seam idempotent per mix.
    ``data.type = "recommendation"`` rides on the event so a tap can open the
    recommendations surface once the relay forwards it."""
    data: dict[str, Any] = {"type": "recommendation"}
    if playlist_id:
        data["playlist_id"] = playlist_id
    if data_extra:
        data.update(data_extra)
    key = dedup_key if dedup_key is not None else (f"reco:{playlist_id}" if playlist_id else None)
    return await emit_notification(
        session,
        event_type="recommendation",
        title=title or "A new mix for you",
        body=body or "We put together a fresh mix based on your recent listening.",
        user_ids=[user_id],
        dedup_key=key,
        data=data,
        now=now,
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _backoff_seconds(attempt: int) -> int:
    """Exponential backoff for the *attempt*-th failure (1-based): BASE·2^(n-1),
    capped at BACKOFF_MAX."""
    base = max(1, settings.NOTIFY_BACKOFF_BASE_SECONDS)
    step = base * (2 ** max(0, attempt - 1))
    return int(min(step, settings.NOTIFY_BACKOFF_MAX_SECONDS))


async def dispatch_pending(session: AsyncSession, *, limit: int = 200) -> dict[str, Any]:
    """Drain ready ``notification_deliveries`` (any event type). Idempotent + safe
    to re-run: a ``sent`` row is never reselected, and a failed row is deferred by
    ``next_retry_at`` so the inline call + backstop tick don't double-send.
    """
    if not settings.push_enabled:
        return {"skipped": "disabled"}

    now = int(time.time())
    rows = (
        await session.execute(
            select(NotificationDelivery, NotificationEvent)
            .join(NotificationEvent, NotificationDelivery.event_id == NotificationEvent.id)
            .where(
                NotificationDelivery.dispatch_state == "pending",
                (NotificationDelivery.next_retry_at.is_(None)) | (NotificationDelivery.next_retry_at <= now),
            )
            .order_by(NotificationDelivery.id)
            .limit(limit)
        )
    ).all()
    if not rows:
        return {"processed": 0}

    sent = failed = suppressed = retry = errored = 0
    for delivery, event in rows:
        # Per-delivery isolation: a DB error while resolving channels for ONE row
        # must not abort the drain and discard the state changes (incl. already-
        # sent rows) accumulated so far — that would re-send on the next tick.
        # An errored row is left untouched (pending) and retried next tick.
        try:
            urls = await _channels_for(session, delivery.user_id, delivery.event_type)
            if not urls:
                delivery.dispatch_state = "suppressed"  # nothing to deliver to; never retried
                suppressed += 1
                continue

            # Apprise is a sync lib → offload to a thread (bounded by
            # APPRISE_TIMEOUT_S) so it can't block the loop or hang on a wedged relay.
            ok = await _apprise_notify_bounded(urls, event.title, event.body)
            if ok:
                delivery.dispatch_state = "sent"
                delivery.notified_at = now
                delivery.last_error = None
                sent += 1
                continue

            # Transient failure: attempt-counted exponential backoff, with an age cap.
            delivery.attempt_count = (delivery.attempt_count or 0) + 1
            delivery.last_error = "apprise_notify_failed"
            age_h = (now - (delivery.created_at or now)) / 3600.0
            if delivery.attempt_count >= settings.NOTIFY_MAX_ATTEMPTS or age_h >= settings.DISPATCH_MAX_AGE_HOURS:
                delivery.dispatch_state = "failed"
                failed += 1
            else:
                delivery.next_retry_at = now + _backoff_seconds(delivery.attempt_count)
                retry += 1
        except Exception as exc:
            logger.warning("dispatch: delivery %s errored, left pending: %s", delivery.id, exc)
            errored += 1

    await session.commit()
    summary = {"processed": len(rows), "sent": sent, "failed": failed, "suppressed": suppressed, "retry": retry}
    if errored:
        summary["errored"] = errored
    return summary


async def _channels_for(session: AsyncSession, user_id: str, event_type: str) -> list[str]:
    """Apprise URLs of a user's active devices opted-in for ``event_type``.

    The per-type pref column (``NOTIF_PREF_COLUMNS``) is matched with
    ``isnot(False)`` so a legacy device that predates the column (NULL) is treated
    as opted-in, matching the server-side default + the shipped iOS toggles."""
    query = select(Device).where(Device.user_id == user_id, Device.disabled_at.is_(None))
    pref = NOTIF_PREF_COLUMNS.get(event_type)
    if pref is not None:
        query = query.where(getattr(Device, pref).isnot(False))
    else:
        # Unknown type (no producer should hit this) → fail OPEN to all active
        # devices rather than silently drop a new notification type.
        logger.warning(
            "dispatch: unknown event_type %r has no pref column; delivering to all active devices", event_type
        )
    devices = (await session.execute(query)).scalars().all()
    urls: list[str] = []
    for d in devices:
        if d.apprise_urls:
            urls.extend(d.apprise_urls)
    # Dedupe across devices: two rows can share one capability URL (a pre-guid row
    # + its re-registration), which would otherwise push the same device twice.
    return list(dict.fromkeys(urls))


async def send_test_notification(
    session: AsyncSession, user_id: str, *, device_id: int | None = None
) -> dict[str, Any]:
    """Send an immediate test push to a user's channels — the dashboard's "Send
    test" button. Independent of ``PUSH_ENABLED`` and every per-type mute so an
    operator can verify a channel during setup. Soft-deleted devices are skipped;
    ``device_id`` scopes the test to one device."""
    query = select(Device).where(Device.user_id == user_id, Device.disabled_at.is_(None))
    if device_id is not None:
        query = query.where(Device.id == device_id)
    devices = (await session.execute(query)).scalars().all()
    urls: list[str] = []
    for d in devices:
        if d.apprise_urls:
            urls.extend(d.apprise_urls)
    if not urls:
        return {"sent": False, "channels": 0, "reason": "no_channels"}

    title = "GrooveIQ test"
    body = "Test notification from GrooveIQ. If you can see this, your channel works."
    ok = await _apprise_notify_bounded(urls, title, body)
    return {"sent": bool(ok), "channels": len(urls)}


async def _apprise_notify_bounded(urls: list[str], title: str, body: str) -> bool:
    """Run the sync Apprise call off the loop, bounded by ``APPRISE_TIMEOUT_S``.

    Apprise's HTTP plugins carry their own socket timeouts, but a wedged relay
    connection could still hang a thread-pool worker — and, on the inline
    dispatch path, the caller's coroutine — indefinitely. ``wait_for`` caps how
    long we block; a timeout is treated as a transient failure so the delivery
    stays pending and the backstop tick retries it. The worker thread may still
    finish in the background, so a genuine >timeout call could double-send —
    acceptable versus hanging dispatch, and rare because Apprise's own timeouts
    (~4-8s) almost always fire first (issue #150)."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_apprise_notify, urls, title, body),
            timeout=max(1.0, settings.APPRISE_TIMEOUT_S),
        )
    except TimeoutError:
        logger.warning(
            "apprise notify exceeded %.1fs budget for %d channel(s); deferring to backstop",
            settings.APPRISE_TIMEOUT_S,
            len(urls),
        )
        return False


def _apprise_notify(urls: list[str], title: str, body: str) -> bool:
    """Sync Apprise call — run under ``asyncio.to_thread``. Never raises.

    A missing import (broken install) or any per-URL failure returns False so a
    bad channel can't wedge the whole dispatch run.
    """
    try:
        import apprise
    except ImportError:
        logger.warning("apprise not installed; skipping %d channel(s)", len(urls))
        return False
    try:
        ap = apprise.Apprise()
        # Dedupe: a user can have two device rows sharing one capability URL
        # (e.g. a pre-device_guid row + its re-registration), and Apprise treats
        # repeats as distinct targets — which would deliver the same push twice.
        for u in dict.fromkeys(urls):
            ap.add(u)
        return bool(ap.notify(title=title, body=body))
    except Exception as exc:  # a bad user URL must not crash the dispatch run
        logger.warning("apprise notify failed: %s", exc)
        return False
