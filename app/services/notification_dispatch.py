"""GrooveIQ – new-release notification dispatch (overview §6.5).

Consumes ``user_release_notifications`` rows that are ``eligible`` + ``pending``,
builds a message from ``release_events.kind``, fans out to each active device's
Apprise channels, stamps ``dispatch_state``/``notified_at``, and applies a simple
age-capped retry. Gated by ``settings.push_enabled`` at the callers (the
reconciler after fan-out + a scheduler backstop tick).

Delivery is **Apprise-only**. A user's iOS device registers a per-device
*capability URL* minted by the APN relay (``jsons://<relay>/v1/apprise/<id>``) as
one of its ``apprise_urls``; the relay holds Ampster's ``.p8`` and pushes to APNs.
grooveiq holds **no** Apple credentials and **no** relay shared secret — the
capability lives entirely in the URL the user registered (per-user, self-service),
which is why a self-hosted, multi-user grooveiq needs no operator secret. Non-iOS
channels (ntfy/telegram/...) ride the exact same path.

Apprise is a required dependency; the import is still guarded inside
``_apprise_notify`` so a broken install degrades to "no delivery" (logs + returns
False) instead of crashing the dispatch run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import Device, ReleaseEvent, UserReleaseNotification  # P1-owned models

logger = logging.getLogger(__name__)

_KIND_NOUN = {"album": "the album", "ep": "the EP", "single": "the single", "track": "the track"}


def build_message(release: ReleaseEvent) -> tuple[str, str]:
    """(title, body) for a release. Kind → natural noun; unknown → 'the album'."""
    noun = _KIND_NOUN.get((release.kind or "album").lower(), "the album")
    title = "New release"
    body = f'{release.artist_name} just released {noun} "{release.album_title}"'
    return title, body


async def dispatch_pending(session: AsyncSession, *, limit: int = 200) -> dict[str, Any]:
    """Process eligible+pending notifications. Idempotent + safe to re-run.

    The driving filter is ``eligible AND dispatch_state == 'pending'``, so a
    ``sent`` row is never reprocessed and the reconciler + backstop tick can both
    run without double-sending.
    """
    if not settings.push_enabled:
        return {"skipped": "disabled"}

    now = int(time.time())
    rows = (
        await session.execute(
            select(UserReleaseNotification, ReleaseEvent)
            .join(ReleaseEvent, UserReleaseNotification.release_event_id == ReleaseEvent.id)
            .where(
                UserReleaseNotification.eligible.is_(True),
                UserReleaseNotification.dispatch_state == "pending",
            )
            .limit(limit)
        )
    ).all()
    if not rows:
        return {"processed": 0}

    sent = failed = suppressed = 0
    for notif, release in rows:
        urls = await _channels_for(session, notif.user_id)
        if not urls:
            notif.dispatch_state = "suppressed"  # nothing to deliver to; never retried
            suppressed += 1
            continue

        title, body = build_message(release)
        # Apprise is a sync lib → offload to a thread so it can't block the loop.
        ok = await asyncio.to_thread(_apprise_notify, urls, title, body)

        if ok:
            notif.dispatch_state = "sent"
            notif.notified_at = now
            sent += 1
        else:
            # Retryable: leave 'pending' for the backstop tick — unless the row has
            # aged past the cap, in which case give up (no dispatch_attempts column
            # on the P1 table, so cap by age; overview §6.5 step 4).
            age_h = (now - (notif.created_at or now)) / 3600.0
            if age_h >= settings.DISPATCH_MAX_AGE_HOURS:
                notif.dispatch_state = "failed"
            failed += 1

    await session.commit()
    return {"processed": len(rows), "sent": sent, "failed": failed, "suppressed": suppressed}


async def _channels_for(session: AsyncSession, user_id: str) -> list[str]:
    """Collect the Apprise URLs of a user's active, opted-in devices."""
    devices = (
        (
            await session.execute(
                select(Device).where(
                    Device.user_id == user_id,
                    Device.notif_new_releases.is_(True),
                    Device.disabled_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    urls: list[str] = []
    for d in devices:
        if d.apprise_urls:
            urls.extend(d.apprise_urls)
    return urls


async def send_test_notification(
    session: AsyncSession, user_id: str, *, device_id: int | None = None
) -> dict[str, Any]:
    """Send an immediate test push to a user's channels — used by the dashboard's
    "Send test" button. Deliberately independent of the ``PUSH_ENABLED`` master
    switch and the per-device ``notif_new_releases`` mute, so an operator can
    verify a channel during setup or while it's muted. Soft-deleted devices
    (``disabled_at``) are skipped; ``device_id`` scopes the test to one device.
    """
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
    ok = await asyncio.to_thread(_apprise_notify, urls, title, body)
    return {"sent": bool(ok), "channels": len(urls)}


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
        for u in urls:
            ap.add(u)
        return bool(ap.notify(title=title, body=body))
    except Exception as exc:  # a bad user URL must not crash the dispatch run
        logger.warning("apprise notify failed: %s", exc)
        return False
