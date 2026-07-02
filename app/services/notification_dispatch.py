"""GrooveIQ – new-release notification dispatch (overview §6.5).

Consumes ``user_release_notifications`` rows that are ``eligible`` + ``pending``,
builds a message from ``release_events.kind``, fans out to each active device
(the stateless relay for APNs tokens, Apprise for generic URLs), stamps
``dispatch_state``/``notified_at``, prunes 410-Unregistered tokens, and applies a
simple age-capped retry. Everything is gated by ``settings.push_enabled`` at the
callers (the reconciler after fan-out + a scheduler backstop tick).

Apprise is an OPTIONAL dependency, imported lazily inside ``_apprise_notify`` so
grooveiq installs and runs without it — the generic-channel path simply no-ops
(logs + returns False) when the lib is absent.
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
from app.services.relay_client import RelayError, get_relay_client

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

    relay = get_relay_client()
    sent = failed = suppressed = pruned = 0
    try:
        for notif, release in rows:
            devices = (
                await session.execute(
                    select(Device).where(
                        Device.user_id == notif.user_id,
                        Device.notif_new_releases.is_(True),
                        Device.disabled_at.is_(None),
                    )
                )
            ).scalars().all()

            if not devices:
                notif.dispatch_state = "suppressed"  # nothing to deliver to; never retried
                suppressed += 1
                continue

            title, body = build_message(release)
            data = {
                "type": "new_release",
                "release_event_id": release.id,
                "artist_mbid": release.artist_mbid,
                "release_key": release.release_key,
            }
            collapse_id = f"release-{release.id}"

            token_env = {d.apns_token: d.apns_environment for d in devices if d.apns_token}
            apprise_urls: list[str] = []
            for d in devices:
                if d.apprise_urls:
                    apprise_urls.extend(d.apprise_urls)

            delivered_any = False
            transient_error = False

            # --- Native APNs via the stateless relay (grouped by environment) ---
            if token_env and relay is not None:
                for env in sorted(set(token_env.values())):
                    grp = [t for t, e in token_env.items() if e == env]
                    notification = {
                        "title": title, "body": body, "sound": "default",
                        "thread_id": "new-releases", "badge": 1, "data": data,
                    }
                    try:
                        results = await relay.push(
                            tokens=grp, environment=env,
                            notification=notification, collapse_id=collapse_id,
                        )
                    except RelayError as exc:
                        logger.warning("relay push failed (env=%s): %s", env, exc)
                        transient_error = True
                        continue
                    for r in results:
                        if r.get("status") == 200:
                            delivered_any = True
                        elif r.get("status") == 410:  # Unregistered → prune the token
                            await _disable_token(session, r.get("token"), now)
                            pruned += 1

            # --- Generic channels via Apprise (sync lib → offload to a thread) ---
            if apprise_urls and settings.APPRISE_ENABLED:
                ok = await asyncio.to_thread(_apprise_notify, apprise_urls, title, body)
                delivered_any = delivered_any or ok
                transient_error = transient_error or (not ok)

            if delivered_any:
                notif.dispatch_state = "sent"
                notif.notified_at = now
                sent += 1
            elif transient_error:
                # Retryable: leave 'pending' for the backstop tick — unless the row
                # has aged past the cap, in which case give up (no dispatch_attempts
                # column on the P1 table, so cap by age; overview §6.5 step 4).
                age_h = (now - (notif.created_at or now)) / 3600.0
                if age_h >= settings.DISPATCH_MAX_AGE_HOURS:
                    notif.dispatch_state = "failed"
                failed += 1
            else:
                notif.dispatch_state = "failed"  # devices existed but nothing could be delivered
                failed += 1

        await session.commit()
    finally:
        if relay is not None:
            await relay.close()

    return {"processed": len(rows), "sent": sent, "failed": failed,
            "suppressed": suppressed, "pruned": pruned}


async def _disable_token(session: AsyncSession, token: str | None, now: int) -> None:
    """Soft-disable a device whose token the relay reported as 410 Unregistered."""
    if not token:
        return
    dev = (
        await session.execute(select(Device).where(Device.apns_token == token))
    ).scalar_one_or_none()
    if dev is not None:
        dev.disabled_at = now


def _apprise_notify(urls: list[str], title: str, body: str) -> bool:
    """Sync Apprise call — run under ``asyncio.to_thread``. Never raises.

    ``apprise`` is an optional dependency: a missing import (or any per-URL
    failure) returns False so a bad channel can't wedge the whole dispatch run.
    """
    try:
        import apprise  # optional dependency, imported lazily on use
    except ImportError:
        logger.warning("apprise not installed; skipping %d generic channel(s)", len(urls))
        return False
    try:
        ap = apprise.Apprise()
        for u in urls:
            ap.add(u)
        return bool(ap.notify(title=title, body=body))
    except Exception as exc:  # a bad user URL must not crash the dispatch run
        logger.warning("apprise notify failed: %s", exc)
        return False
