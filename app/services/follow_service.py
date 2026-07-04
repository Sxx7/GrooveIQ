"""GrooveIQ -- follow persistence + artist monitoring wiring (P0).

Turns a follow request into (1) a per-user follow edge, (2) a global
monitored-artist row (dedup across users), and (3) best-effort Lidarr
``monitor="all"`` wiring so future releases are captured. Resolution of the
artist MBID/image is *best-effort and never blocks the follow* — a follow
persists even when Lidarr/streamrip are down or disabled (overview §5, §10).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import FollowedArtist, MonitoredArtist
from app.services.discovery import LidarrClient, _normalize_artist

logger = logging.getLogger(__name__)


def _lidarr_configured() -> bool:
    # Same gate as discovery_enabled / fill_library_enabled (config.py:520, 489).
    return bool(settings.LIDARR_URL and settings.LIDARR_API_KEY)


async def _resolve_artist(artist_name: str, given_mbid: str | None) -> dict[str, Any]:
    """Best-effort resolve name -> {mbid, image_url}. NEVER raises.

    Order: caller-supplied MBID > Lidarr lookup_artist (authoritative MBID via
    foreignArtistId) > streamrip name/image fallback (no MBID). Any failure
    returns what we have so far (possibly mbid=None) — the follow proceeds.
    """
    out: dict[str, Any] = {"mbid": given_mbid, "image_url": None}

    if _lidarr_configured():
        lidarr = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)
        try:
            lookup = await lidarr.lookup_artist(mbid=given_mbid, name=artist_name)
            if lookup:
                fid = lookup.get("foreignArtistId")
                if fid:
                    out["mbid"] = fid
                # Lidarr sometimes returns remoteImages; grab a URL if present.
                for img in lookup.get("images") or []:
                    if img.get("remoteUrl"):
                        out["image_url"] = img["remoteUrl"]
                        break
        except Exception as exc:  # best-effort: resolution must never block the follow
            logger.warning("Lidarr lookup failed for %r: %s", artist_name, exc)
        finally:
            await lidarr.close()

    # streamrip fallback for a display image only (no MBID available there).
    if out["image_url"] is None and settings.streamrip_enabled:
        from app.services.streamrip import StreamripClient

        sr = StreamripClient(settings.STREAMRIP_API_URL)
        try:
            out["image_url"] = await sr.resolve_artist_image(artist_name)
        except Exception as exc:  # best-effort: image fallback is optional
            logger.warning("streamrip artist-image lookup failed for %r: %s", artist_name, exc)
        finally:
            await sr.close()

    return out


async def _ensure_monitored(session: AsyncSession, *, artist_name: str, norm: str, mbid: str | None) -> MonitoredArtist:
    """Upsert the global monitored_artists row; wire Lidarr monitor if new.

    Idempotent. Matches on MBID first, then on artist_name_norm. The counter
    (active_follower_count) is bumped by the caller on genuine follow
    transitions — see follow_artist().
    """
    row = None
    if mbid:
        row = (
            await session.execute(select(MonitoredArtist).where(MonitoredArtist.artist_mbid == mbid))
        ).scalar_one_or_none()
    if row is None:
        row = (
            await session.execute(select(MonitoredArtist).where(MonitoredArtist.artist_name_norm == norm))
        ).scalar_one_or_none()

    if row is None:
        row = MonitoredArtist(
            artist_mbid=mbid,
            artist_name_norm=norm,
            artist_name=artist_name,
            active_follower_count=0,
            created_at=int(time.time()),
        )
        session.add(row)
        await session.flush()  # get row.id, keep it in the identity map
    elif mbid and not row.artist_mbid:
        # We learned the MBID after first creating a name-keyed row.
        row.artist_mbid = mbid

    # Wire Lidarr monitor exactly once (dedup via lidarr_monitored + MBID).
    if mbid and not row.lidarr_monitored and _lidarr_configured():
        lidarr = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)
        try:
            existing = await lidarr.get_existing_artist_mbids()
            if mbid in existing:
                row.lidarr_monitored = True
            else:
                result = await lidarr.add_artist(mbid, artist_name)  # monitor="all"
                row.lidarr_artist_id = result.get("id")
                row.lidarr_monitored = True
        except Exception as exc:  # 409 already-exists handled below; other errors are non-fatal
            # A 409 means already in Lidarr (discovery.py:612) — treat as monitored.
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409:
                row.lidarr_monitored = True
            else:
                logger.warning("Lidarr add_artist failed for %r: %s", artist_name, exc)
        finally:
            await lidarr.close()

    return row


async def follow_artist(
    session: AsyncSession, *, user_id: str, artist_name: str, artist_mbid: str | None, source: str
) -> dict[str, Any]:
    """Idempotent upsert of a follow edge + monitor wiring. Returns the API dict.

    Resolution runs once up front (best-effort, never blocks). The DB upsert +
    commit is retried once on a monitored_artists UNIQUE race (edge case #7):
    two users following the same brand-new artist concurrently can collide on
    artist_name_norm / artist_mbid. On IntegrityError we roll back and re-run —
    the second pass finds the now-committed monitored row and just bumps the
    counter (overview §2: single retry, don't over-engineer at this scale).
    """
    norm = _normalize_artist(artist_name)
    resolved = await _resolve_artist(artist_name, artist_mbid)
    mbid = resolved["mbid"]

    edge: FollowedArtist | None = None
    for attempt in range(2):
        # --- Find an existing edge (active or soft-unfollowed) for this user+artist.
        edge = None
        if mbid:
            edge = (
                await session.execute(
                    select(FollowedArtist).where(FollowedArtist.user_id == user_id, FollowedArtist.artist_mbid == mbid)
                )
            ).scalar_one_or_none()
        if edge is None:
            edge = (
                await session.execute(
                    select(FollowedArtist).where(
                        FollowedArtist.user_id == user_id, FollowedArtist.artist_name_norm == norm
                    )
                )
            ).scalar_one_or_none()

        was_active = edge is not None and edge.unfollowed_at is None

        if edge is None:
            edge = FollowedArtist(
                user_id=user_id,
                artist_mbid=mbid,
                artist_name=artist_name,
                artist_name_norm=norm,
                image_url=resolved["image_url"],
                source=source,
                followed_at=int(time.time()),
                unfollowed_at=None,
            )
            session.add(edge)
        else:
            edge.unfollowed_at = None  # re-follow clears soft-delete
            if mbid and not edge.artist_mbid:
                edge.artist_mbid = mbid
            if resolved["image_url"] and not edge.image_url:
                edge.image_url = resolved["image_url"]

        monitored = await _ensure_monitored(session, artist_name=artist_name, norm=norm, mbid=mbid)
        if not was_active:  # only count a genuine new/re-activated follow
            monitored.active_follower_count = (monitored.active_follower_count or 0) + 1

        try:
            await session.commit()
            break
        except IntegrityError:
            await session.rollback()
            if attempt == 1:
                raise
            # Loop and retry — the conflicting monitored row now exists committed.

    if edge is None:  # unreachable: the loop either set edge and committed, or raised
        raise RuntimeError("follow_artist: no follow edge after upsert loop")
    await session.refresh(edge)
    return {
        "follow": {
            "id": edge.id,
            "user_id": edge.user_id,
            "artist_mbid": edge.artist_mbid,
            "artist_name": edge.artist_name,
            "image_url": edge.image_url,
            "source": edge.source,
            "followed_at": edge.followed_at,
        },
        "artist": {
            "artist_mbid": mbid,
            "artist_name": artist_name,
            "resolved": mbid is not None,
        },
    }


async def unfollow_artist(session: AsyncSession, *, user_id: str, artist_key: str) -> bool:
    """Soft-unfollow. artist_key = MBID or artist_name_norm. Decrements
    active_follower_count. Leaves Lidarr monitor in place while other followers
    remain. Returns True if an active edge was found & soft-deleted."""
    edge = (
        (
            await session.execute(
                select(FollowedArtist).where(
                    FollowedArtist.user_id == user_id,
                    FollowedArtist.unfollowed_at.is_(None),
                    (FollowedArtist.artist_mbid == artist_key) | (FollowedArtist.artist_name_norm == artist_key),
                )
            )
        )
        .scalars()
        .first()
    )
    if edge is None:
        return False

    edge.unfollowed_at = int(time.time())

    # Decrement the global counter (never below 0). Do NOT remove the Lidarr
    # monitor — other users may still follow this artist.
    mon = None
    if edge.artist_mbid:
        mon = (
            await session.execute(select(MonitoredArtist).where(MonitoredArtist.artist_mbid == edge.artist_mbid))
        ).scalar_one_or_none()
    if mon is None:
        mon = (
            await session.execute(
                select(MonitoredArtist).where(MonitoredArtist.artist_name_norm == edge.artist_name_norm)
            )
        ).scalar_one_or_none()
    if mon is not None:
        mon.active_follower_count = max(0, (mon.active_follower_count or 0) - 1)

    await session.commit()
    return True


async def unfollow_all(session: AsyncSession, *, user_id: str) -> int:
    """Soft-unfollow every active follow for a user in one pass (Settings
    "Unfollow all"). Decrements each artist's active_follower_count; leaves
    Lidarr monitors in place (other users may still follow). Idempotent —
    a second call finds nothing active and returns 0. Returns the count removed.
    """
    edges = (
        (
            await session.execute(
                select(FollowedArtist).where(
                    FollowedArtist.user_id == user_id,
                    FollowedArtist.unfollowed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not edges:
        return 0

    now = int(time.time())
    for edge in edges:
        edge.unfollowed_at = now
        mon = None
        if edge.artist_mbid:
            mon = (
                await session.execute(select(MonitoredArtist).where(MonitoredArtist.artist_mbid == edge.artist_mbid))
            ).scalar_one_or_none()
        if mon is None:
            mon = (
                await session.execute(
                    select(MonitoredArtist).where(MonitoredArtist.artist_name_norm == edge.artist_name_norm)
                )
            ).scalar_one_or_none()
        if mon is not None:
            mon.active_follower_count = max(0, (mon.active_follower_count or 0) - 1)

    await session.commit()
    return len(edges)


async def list_follows(session: AsyncSession, *, user_id: str) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(FollowedArtist)
                .where(FollowedArtist.user_id == user_id, FollowedArtist.unfollowed_at.is_(None))
                .order_by(FollowedArtist.followed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "artist_mbid": r.artist_mbid,
            "artist_name": r.artist_name,
            "image_url": r.image_url,
            "followed_at": r.followed_at,
        }
        for r in rows
    ]
