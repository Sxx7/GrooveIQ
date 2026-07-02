"""GrooveIQ -- followed-artist release detection + availability reconciler (P1).

Two entry points, each opens its own async session (like the scheduler wrappers):

- ``run_follow_scan()`` — polls streamrip for each *due* ``monitored_artists``
  row's discography, inserts ``release_events`` for genuinely-new releases, and
  triggers acquisition through the existing ``bulk_album`` cascade
  (lidarr → streamrip). Advances the per-artist poll watermark.
- ``reconcile_available_releases()`` — stamps ``release_events.available_at``
  ONLY when the release's tracks have ``media_server_id`` (streamable), then
  fans out ``user_release_notifications`` to eligible active followers.

Everything is idempotent (``release_key`` unique for detection; the
``(user_id, release_event_id)`` unique for fanout). Off unless
``settings.follow_scan_enabled``.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import (
    FollowedArtist,
    MonitoredArtist,
    ReleaseEvent,
    TrackFeatures,
    UserReleaseNotification,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def _norm(s: str | None) -> str:
    """Lowercase + trim. Uses ``.lower()`` (NOT casefold) to stay in lockstep
    with the reconciler's SQL ``lower(trim(...))`` match (edge case #11) — a
    mismatch would silently drop availability matches."""
    return (s or "").strip().lower()


def _release_key(artist_norm: str, album_norm: str, year: int | None, rg_mbid: str | None) -> str:
    if rg_mbid:
        return rg_mbid
    return f"{artist_norm}|{album_norm}|{year or 0}"


def _parse_release_epoch(release_date: str | None, year: int | None) -> tuple[int | None, int | None]:
    """(epoch, year) from streamrip's ISO ``release_date`` ('YYYY-MM-DD' /
    'YYYY') with a ``year``-only fallback (Jan 1, coarse). (None, None) when
    neither is usable — such an album can't be window-filtered and is skipped."""
    rd = (release_date or "").strip()
    if len(rd) >= 4 and rd[:4].isdigit():
        yr = int(rd[:4])
        mo = int(rd[5:7]) if len(rd) >= 7 and rd[5:7].isdigit() else 1
        da = int(rd[8:10]) if len(rd) >= 10 and rd[8:10].isdigit() else 1
        try:
            epoch = int(datetime(yr, min(12, max(1, mo)), min(31, max(1, da)), tzinfo=UTC).timestamp())
            return epoch, yr
        except (ValueError, OverflowError):
            pass
    if year:
        try:
            return int(datetime(int(year), 1, 1, tzinfo=UTC).timestamp()), int(year)
        except (ValueError, OverflowError):
            pass
    return None, None


def _infer_kind(track_count: int | None) -> str:
    if not track_count:
        return "album"
    if track_count <= 1:
        return "single"
    if track_count <= 5:
        return "ep"
    return "album"


# ---------------------------------------------------------------------------
# Detection loop (overview §6.2)
# ---------------------------------------------------------------------------


async def run_follow_scan() -> dict[str, Any]:
    """Detect + acquire new releases for due monitored artists. Opens its own session."""
    if not settings.follow_scan_enabled:
        return {"skipped": "disabled"}
    if not settings.STREAMRIP_API_URL:
        return {"skipped": "streamrip_not_configured"}

    from app.services.streamrip import StreamripClient

    now = _now()
    window_cutoff = now - settings.NEW_RELEASE_WINDOW_DAYS * 86400
    poll_cutoff = now - settings.FOLLOW_SCAN_POLL_INTERVAL_HOURS * 3600
    detected = 0
    acquired = 0

    async with AsyncSessionLocal() as session:
        due = (
            await session.execute(
                select(MonitoredArtist).where(
                    or_(
                        MonitoredArtist.last_poll_at.is_(None),
                        MonitoredArtist.last_poll_at <= poll_cutoff,
                    )
                )
            )
        ).scalars().all()
        if not due:
            return {"detected": 0, "acquired": 0, "artists_polled": 0}

        client = StreamripClient(settings.STREAMRIP_API_URL)
        try:
            for artist in due:
                try:
                    res = await client.search_artist(
                        artist.artist_name,
                        limit=2,
                        albums_per_artist=settings.FOLLOW_ALBUMS_PER_ARTIST,
                    )
                except Exception as exc:  # transport error → retry next tick, no watermark advance
                    logger.warning("Follow-scan search_artist failed for %r: %s", artist.artist_name, exc)
                    continue

                artists = res.get("artists") or []
                if not artists and res.get("error"):
                    # hard error from streamrip-api → retry next tick, do NOT advance watermark
                    continue

                artist_norm = artist.artist_name_norm or _norm(artist.artist_name)
                newest_epoch = artist.last_seen_release_date or 0

                if artists:
                    a0 = artists[0]
                    service = a0.get("service")
                    albums = (a0.get("albums") or []) + (a0.get("other_releases") or [])
                    for alb in albums:
                        album_id = str(alb.get("album_id") or "")
                        album_title = alb.get("title") or ""
                        if not album_id or not album_title:
                            continue
                        epoch, yr = _parse_release_epoch(alb.get("release_date"), alb.get("year"))
                        if epoch is None:
                            continue  # no usable date → can't apply the back-catalog window
                        # Candidate: newer than the watermark AND within the recency window.
                        if epoch <= (artist.last_seen_release_date or 0) or epoch < window_cutoff:
                            continue
                        album_norm = _norm(album_title)
                        rkey = _release_key(artist_norm, album_norm, yr, None)
                        exists = (
                            await session.execute(
                                select(ReleaseEvent.id).where(ReleaseEvent.release_key == rkey)
                            )
                        ).scalar_one_or_none()
                        if exists is not None:
                            newest_epoch = max(newest_epoch, epoch)
                            continue

                        # Acquire via the existing bulk_album cascade (lidarr → streamrip).
                        state, task_id = "pending", None
                        try:
                            from app.services.download_chain import AlbumRef, try_album_download_chain

                            cascade = await try_album_download_chain(
                                AlbumRef(
                                    mb_release_group_id=None,
                                    artist_name=artist.artist_name,
                                    album_name=album_title,
                                    service=service,
                                    album_id=album_id,
                                )
                            )
                            if cascade.success:
                                state = "downloading"
                                task_id = (cascade.final_extra or {}).get("task_id")
                                acquired += 1
                        except Exception as exc:  # leave pending; Lidarr backstop + reconciler still catch it
                            logger.warning(
                                "Follow-scan acquire failed for %r / %r: %s", artist.artist_name, album_title, exc
                            )

                        ev = ReleaseEvent(
                            release_key=rkey,
                            release_group_mbid=None,
                            artist_mbid=artist.artist_mbid,
                            artist_name=artist.artist_name,
                            artist_name_norm=artist_norm,
                            album_title=album_title,
                            album_title_norm=album_norm,
                            kind=_infer_kind(alb.get("track_count")),
                            first_release_date=epoch,
                            acquisition_state=state,
                            track_count_total=alb.get("track_count"),
                            cover_url=(alb.get("cover_url") or None),
                            source="streamrip_poll",
                            last_acq_task_id=task_id,
                            detected_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                        # Savepoint-isolate the insert so a rare release_key race
                        # doesn't roll back the whole artist's batch.
                        try:
                            async with session.begin_nested():
                                session.add(ev)
                                await session.flush()
                            detected += 1
                            newest_epoch = max(newest_epoch, epoch)
                        except IntegrityError:
                            pass  # already detected concurrently

                # Advance the watermark even on an empty/quiet poll so we don't
                # re-poll this artist every tick (lidarr_backfill's cursor lesson).
                artist.last_poll_at = now
                artist.last_seen_release_date = max(artist.last_seen_release_date or 0, newest_epoch)
                await session.commit()
        finally:
            await client.close()

    return {"detected": detected, "acquired": acquired, "artists_polled": len(due)}


# ---------------------------------------------------------------------------
# Availability reconciler (overview §6.3) + fan-out (§6.4 guard)
# ---------------------------------------------------------------------------


async def reconcile_available_releases() -> dict[str, Any]:
    """Stamp available_at on releases now streamable + fan out notifications.
    Opens its own session. Safe to run repeatedly (idempotent)."""
    now = _now()
    reconciled = 0
    notifications_created = 0

    async with AsyncSessionLocal() as session:
        pending = (
            await session.execute(
                select(ReleaseEvent).where(ReleaseEvent.acquisition_state != "imported")
            )
        ).scalars().all()

        eligible_count: dict[str, int] = {}  # shared across evs → the cap is per-user, per-RUN
        for ev in pending:
            if not ev.artist_name_norm or not ev.album_title_norm:
                continue
            n_avail = (
                await session.scalar(
                    select(func.count())
                    .select_from(TrackFeatures)
                    .where(
                        TrackFeatures.media_server_id.isnot(None),
                        func.lower(func.trim(TrackFeatures.artist)) == ev.artist_name_norm,
                        func.lower(func.trim(TrackFeatures.album)) == ev.album_title_norm,
                    )
                )
            ) or 0

            threshold = 1
            if ev.track_count_total and settings.FOLLOW_AVAILABILITY_MIN_FRACTION > 0:
                threshold = max(1, math.ceil(ev.track_count_total * settings.FOLLOW_AVAILABILITY_MIN_FRACTION))
            if n_avail < threshold:
                continue

            if ev.available_at is None:  # transition ONCE — only here, only with media_server_id
                ev.available_at = now
                ev.acquisition_state = "imported"
                ev.track_count_available = n_avail
                ev.updated_at = now
                await session.flush()  # persist ev transition into the txn before fan-out savepoints
                reconciled += 1
                notifications_created += await _fanout(session, ev, now, eligible_count)

        await session.commit()

    result: dict[str, Any] = {"reconciled": reconciled, "notifications_created": notifications_created}
    # Low-latency push (P2): dispatch the freshly-created pending notifications in
    # a fresh session. Gated + off by default; the scheduler backstop tick retries
    # transient failures and covers rows created before a relay was configured.
    if settings.push_enabled and notifications_created:
        from app.services.notification_dispatch import dispatch_pending
        async with AsyncSessionLocal() as dispatch_session:
            result["dispatched"] = await dispatch_pending(dispatch_session)
    return result


async def _fanout(
    session: AsyncSession, ev: ReleaseEvent, now: int, eligible_count: dict[str, int]
) -> int:
    """Create UserReleaseNotification rows for each active follower of ev's
    artist, applying the eligibility guard (§6.4) + per-run cap (``eligible_count``
    is shared across the whole reconcile run, so it caps per user per run).
    Idempotent via the unique (user_id, release_event_id). Returns new-row count."""
    match = [FollowedArtist.artist_name_norm == ev.artist_name_norm]
    if ev.artist_mbid:
        match.append(FollowedArtist.artist_mbid == ev.artist_mbid)
    followers = (
        await session.execute(
            select(FollowedArtist).where(
                FollowedArtist.unfollowed_at.is_(None),
                or_(*match),
            )
        )
    ).scalars().all()

    window_cutoff = now - settings.NEW_RELEASE_WINDOW_DAYS * 86400
    created = 0
    for f in followers:
        eligible = (
            ev.first_release_date is not None
            and ev.first_release_date >= (f.followed_at - settings.FOLLOW_GRACE_DAYS * 86400)
            and ev.first_release_date >= window_cutoff
        )
        if eligible:
            # Per-run cap so a reissue batch doesn't burst a user's pushes.
            if eligible_count.get(f.user_id, 0) >= settings.FOLLOW_MAX_ELIGIBLE_PER_RUN:
                eligible = False
            else:
                eligible_count[f.user_id] = eligible_count.get(f.user_id, 0) + 1

        urn = UserReleaseNotification(
            user_id=f.user_id,
            release_event_id=ev.id,
            eligible=eligible,
            dispatch_state="pending",
            created_at=now,
        )
        try:
            async with session.begin_nested():  # savepoint: a dup doesn't kill the batch
                session.add(urn)
                await session.flush()
            created += 1
        except IntegrityError:
            pass  # already notified for this (user, release) — idempotent
    return created


async def run_follow_scan_and_reconcile() -> dict[str, Any]:
    """Detection loop + reconciler, back to back (admin endpoint / QA)."""
    det = await run_follow_scan()
    rec = await reconcile_available_releases()
    return {**det, **rec}
