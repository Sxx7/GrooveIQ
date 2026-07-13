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
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import Device, NotificationDelivery, NotificationEvent, ReleaseEvent

logger = logging.getLogger(__name__)

# Precedence for the daily budget (notifications Phase 2): when a user's budget is
# tight, deliveries are processed in this order so the LOWEST-priority type is the
# one that gets dropped. download_completed is highest (the push the user is
# actively waiting for) and is additionally exempt from suppression + quiet hours.
_EVENT_PRIORITY: dict[str, int] = {
    "download_completed": 0,
    "new_release": 1,
    "newly_added": 2,
    "recommendation": 3,
}
_EXEMPT_EVENT_TYPES = frozenset({"download_completed"})  # never budget-suppressed, never quiet-held
_DAY_SECONDS = 86_400

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
        "cadences": ["instant", "daily"],
    },
    {
        "key": "new_media",
        "event_type": "newly_added",
        "pref_field": "notif_new_media",
        "label": "New Media",
        "description": "New music becomes available in your library, from any source.",
        "cadences": ["instant", "daily"],
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
# pref_field -> allowed cadences, for the types a user can switch to a daily digest
# ("instant" is always the default). Keyed by pref_field so it lines up with the
# per-device pref columns + the client's toggle keys.
NOTIF_CADENCES: dict[str, list[str]] = {t["pref_field"]: t["cadences"] for t in NOTIFICATION_TYPES if "cadences" in t}

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
    reco never collides with A/B/C. An explicit ``dedup_key`` wins — the feed
    producers pass a per-media key (``reco:mix:{user}:{mix_id}`` /
    ``reco:album:{user}:{artist}|{album}``) so each mix/album notifies a user once
    and a re-run is idempotent; digest + the daily budget keep the push sparse.
    When omitted it falls back to ``reco:{playlist_id}`` (NULL when no playlist
    id), keeping the admin/QA seam idempotent per mix. ``data.type =
    "recommendation"`` rides on the event so a tap can open the recommendations
    surface once the relay forwards it."""
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


async def prune_old_notifications(
    session: AsyncSession,
    *,
    now: int | None = None,
    retention_days: int | None = None,
) -> dict[str, int]:
    """Age out the outbox: delete deliveries — then any event no delivery still
    references — older than the retention horizon, so the in-app feed drops stale
    rows and the tables stay bounded. Deliveries go first (the FK); an event fans
    out to many users, so it must outlive the horizon while ANY user's delivery
    does (hence the not-in guard, not a blind age delete). ``retention_days`` <= 0
    is a no-op — keep forever (the config flag defaults to 30). Caller owns the
    commit."""
    days = retention_days if retention_days is not None else settings.NOTIFICATION_RETENTION_DAYS
    if days <= 0:
        return {"deliveries": 0, "events": 0}
    now = now or int(time.time())
    horizon = now - days * 86_400

    deliv = await session.execute(
        delete(NotificationDelivery).where(NotificationDelivery.created_at < horizon)
    )
    events = await session.execute(
        delete(NotificationEvent).where(
            NotificationEvent.created_at < horizon,
            NotificationEvent.id.not_in(select(NotificationDelivery.event_id)),
        )
    )
    return {"deliveries": deliv.rowcount or 0, "events": events.rowcount or 0}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _backoff_seconds(attempt: int) -> int:
    """Exponential backoff for the *attempt*-th failure (1-based): BASE·2^(n-1),
    capped at BACKOFF_MAX."""
    base = max(1, settings.NOTIFY_BACKOFF_BASE_SECONDS)
    step = base * (2 ** max(0, attempt - 1))
    return int(min(step, settings.NOTIFY_BACKOFF_MAX_SECONDS))


# --- Quiet hours (notifications Phase 2) --------------------------------------
# A per-user local window during which non-urgent pushes are HELD (deferred to the
# window's close) rather than sent. The tz comes from the user's most-recently-seen
# device; a tz-less legacy row falls back to a global UTC window.


def _zone(tz_name: str | None) -> ZoneInfo:
    if not tz_name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _in_quiet_hours(now_epoch: int, tz_name: str | None, start_h: int, end_h: int) -> bool:
    """True if the user's LOCAL hour is inside [start_h, end_h) (wrapping midnight
    when end_h <= start_h). A zero-width window (start == end) means "no quiet hours"."""
    if start_h == end_h:
        return False
    h = datetime.fromtimestamp(now_epoch, _zone(tz_name)).hour
    if start_h < end_h:
        return start_h <= h < end_h
    return h >= start_h or h < end_h  # window wraps past midnight (e.g. 22 → 8)


def _quiet_window_open(now_epoch: int, tz_name: str | None, start_h: int, end_h: int) -> int:
    """Epoch of the next instant the quiet window CLOSES (local ``end_h``) — the
    'hold until' time for a deferred delivery. Always strictly after ``now`` when
    currently inside the window, and never itself inside quiet hours."""
    tz = _zone(tz_name)
    local = datetime.fromtimestamp(now_epoch, tz)
    target = local.replace(hour=end_h, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return int(target.timestamp())


def _next_daily_time(now_epoch: int, hour: int) -> int:
    """Epoch of the next UTC occurrence of ``hour`` — the 'hold until' time for a
    daily-cadence delivery (notifications Phase 6). Global UTC (not per-user local)
    to keep the release a single once-a-day window the minute-tick can catch."""
    utc = datetime.fromtimestamp(now_epoch, ZoneInfo("UTC"))
    target = utc.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= utc:
        target += timedelta(days=1)
    return int(target.timestamp())


# --- Digest rollup (notifications Phase 3) ------------------------------------
# When a burst produces several same-type pending deliveries for one user (a scan
# adding 30 albums, a reconcile stamping several new releases), coalesce them into
# ONE summary push instead of one-per-item. A group of one keeps its original
# per-item message, so enabling the digest only changes the >=2 case.


def _humanize_list(names: list[str]) -> str:
    """"A" / "A and B" / "A, B and C" (Oxford-comma-free, em-dash-free)."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _example_names(events: list[NotificationEvent], limit: int = 3) -> list[str]:
    """Up to ``limit`` distinct artist/album display names pulled from the events'
    structured ``data`` (order-preserving). Empty when a type carries no names."""
    out: list[str] = []
    for e in events:
        d = e.data or {}
        name = d.get("artist") or d.get("album")
        if name and name not in out:
            out.append(name)
            if len(out) >= limit:
                break
    return out


def _build_digest(event_type: str, events: list[NotificationEvent]) -> tuple[str, str, dict[str, Any]]:
    """(title, body, data) for a coalesced group. One event → its original message;
    two or more → a synthesized count digest routed to the type's tap surface."""
    if len(events) == 1:
        e = events[0]
        return e.title, e.body, (e.data or {"type": event_type})

    n = len(events)
    route = (events[0].data or {}).get("type") or event_type  # keep the client tap-route
    incl = _humanize_list(_example_names(events))
    if event_type == "newly_added":
        title = f"{n} new albums added"
        body = f"Including {incl}. Tap to browse." if incl else "Tap to browse your new music."
    elif event_type == "new_release":
        title = f"{n} new releases"
        body = f"New from {incl}." if incl else "New releases from artists you follow."
    elif event_type == "download_completed":
        title = "Downloads ready"
        body = f"{n} downloads finished."
    elif event_type == "recommendation":
        title = "New mixes for you"
        body = f"{n} fresh mixes are ready."
    else:
        title = f"{n} updates"
        body = "Tap to view."
    return title, body, {"type": route, "digest": True, "count": n}


async def _user_tz(session: AsyncSession, user_id: str) -> str | None:
    """The IANA tz of the user's most-recently-seen active device that reports one
    (None if none). Used to evaluate quiet hours in the user's local time."""
    return (
        await session.execute(
            select(Device.tz)
            .where(Device.user_id == user_id, Device.disabled_at.is_(None), Device.tz.isnot(None))
            .order_by(Device.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _any_device_quiet_hours(session: AsyncSession) -> bool:
    """True if any active device carries a per-user quiet-hours override, so
    dispatch can skip all per-user resolution when quiet hours is globally off and
    nobody opted in (the default fast path)."""
    return (
        await session.execute(
            select(Device.id).where(Device.disabled_at.is_(None), Device.quiet_hours_enabled.isnot(None)).limit(1)
        )
    ).first() is not None


async def _user_quiet_hours(session: AsyncSession, user_id: str) -> tuple[bool, int, int]:
    """Per-user (enabled, start, end): the most-recently-seen device's override when
    set, else the global ``QUIET_HOURS_*`` default. A non-NULL per-user flag replaces
    the global for this user (so a user can enable quiet hours the operator left off,
    pick their own window, or opt out of a globally-on window)."""
    row = (
        await session.execute(
            select(Device.quiet_hours_enabled, Device.quiet_hours_start, Device.quiet_hours_end)
            .where(Device.user_id == user_id, Device.disabled_at.is_(None), Device.quiet_hours_enabled.isnot(None))
            .order_by(Device.last_seen_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return settings.QUIET_HOURS_ENABLED, settings.QUIET_HOURS_START, settings.QUIET_HOURS_END
    enabled, start, end = row
    return (
        bool(enabled),
        start if start is not None else settings.QUIET_HOURS_START,
        end if end is not None else settings.QUIET_HOURS_END,
    )


async def _any_device_cadence(session: AsyncSession) -> bool:
    """True if any active device set a per-type cadence, so dispatch can skip all
    per-user cadence resolution when nobody chose a daily digest (the default)."""
    return (
        await session.execute(
            select(Device.id).where(Device.disabled_at.is_(None), Device.notif_cadence.isnot(None)).limit(1)
        )
    ).first() is not None


async def _user_cadence(session: AsyncSession, user_id: str) -> dict[str, str]:
    """Per-type cadence for a user ({pref_field: 'instant'|'daily'}) from the
    most-recently-seen device that set one, else ``{}`` (all instant)."""
    row = (
        await session.execute(
            select(Device.notif_cadence)
            .where(Device.user_id == user_id, Device.disabled_at.is_(None), Device.notif_cadence.isnot(None))
            .order_by(Device.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row or {}


async def _pushes_today_by_user(session: AsyncSession, day_start: int) -> dict[str, int]:
    """Per-user count of PUSHES already sent this UTC day — the budget tally's
    starting point (this dispatch run increments it in-memory as it sends).

    Counts ``budget_counted`` HEAD rows, not raw sent deliveries: a coalesced digest
    of N albums is ONE push (one head row), so the daily budget stays a *push* budget
    across dispatch runs. (Counting deliveries here would let a single N-album digest
    consume N budget units and lock the user out for the rest of the day.) Legacy sent
    rows predating the column read NULL and are not counted — a harmless one-time
    under-count that only relaxes the cap on the first day after the column ships."""
    rows = (
        await session.execute(
            select(NotificationDelivery.user_id, func.count())
            .where(NotificationDelivery.budget_counted.is_(True), NotificationDelivery.notified_at >= day_start)
            .group_by(NotificationDelivery.user_id)
        )
    ).all()
    return {uid: n for uid, n in rows}


async def dispatch_pending(session: AsyncSession, *, limit: int = 200) -> dict[str, Any]:
    """Drain ready ``notification_deliveries`` (any event type). Idempotent + safe
    to re-run: a ``sent`` row is never reselected, and a failed/held row is deferred
    by ``next_retry_at`` so the inline call + backstop tick don't double-send.

    Volume controls, all OFF by default (dispatch is unchanged until you opt in):
    a per-user/UTC-day BUDGET drops the lowest-priority pending type first; a
    per-user local QUIET-HOURS window HOLDS non-urgent deliveries until it closes;
    and the DIGEST coalesces a same-type burst for one user into a single summary
    push. ``download_completed`` is exempt from budget + quiet hours (it still counts
    toward the budget). Rows are processed in precedence order so budget/quiet
    decisions favor the more important type; each group is one push = one budget unit.
    """
    if not settings.push_enabled:
        return {"skipped": "disabled"}

    now = int(time.time())
    # Precedence order (download → new_release → newly_added → recommendation, then
    # unknowns) so that when the budget is tight the lowest-priority type is the one
    # dropped, and important pushes go out first within a run.
    priority = case(_EVENT_PRIORITY, value=NotificationDelivery.event_type, else_=len(_EVENT_PRIORITY))
    rows = (
        await session.execute(
            select(NotificationDelivery, NotificationEvent)
            .join(NotificationEvent, NotificationDelivery.event_id == NotificationEvent.id)
            .where(
                NotificationDelivery.dispatch_state == "pending",
                (NotificationDelivery.next_retry_at.is_(None)) | (NotificationDelivery.next_retry_at <= now),
            )
            .order_by(priority, NotificationDelivery.id)
            # Claim the drained rows so a concurrent dispatch — the 60s backstop tick
            # racing an inline producer dispatch, or two inline dispatches on the same
            # event loop — can't reselect them and double-send / double-count the
            # budget. SKIP LOCKED makes the loser skip claimed rows instead of blocking.
            # No-op on SQLite (it serializes writers anyway); effective on Postgres.
            .with_for_update(skip_locked=True, of=NotificationDelivery)
            .limit(limit)
        )
    ).all()
    if not rows:
        return {"processed": 0}

    budget = settings.NOTIF_DAILY_BUDGET
    digest_on = settings.NOTIF_DIGEST_ENABLED
    # Quiet hours may apply via the global default OR a per-user override; skip all
    # per-user resolution entirely when neither is in play (the default fast path).
    quiet_possible = settings.QUIET_HOURS_ENABLED or await _any_device_quiet_hours(session)
    # Daily cadence: a type a user set to "daily" is held until the digest hour (UTC).
    # During that hour the hold lifts (the gate below is skipped) so the accumulated
    # deliveries fall through and coalesce. Only resolve per-user when someone opted in.
    digest_hour = settings.NOTIF_DAILY_DIGEST_HOUR
    is_digest_window = time.gmtime(now).tm_hour == digest_hour
    cadence_possible = await _any_device_cadence(session)
    day_start = now - (now % _DAY_SECONDS)
    tally: dict[str, int] = await _pushes_today_by_user(session, day_start) if budget > 0 else {}
    tz_cache: dict[str, str | None] = {}
    qh_cache: dict[str, tuple[bool, int, int]] = {}  # per-user (enabled, start, end)
    cad_cache: dict[str, dict[str, str]] = {}  # per-user {pref_field: cadence}

    # Coalesce a same-type burst for one user into a single push. With the digest
    # OFF each delivery is its own group (unchanged per-item behavior). Rows arrive
    # in precedence order, so the ordered dict keeps groups in that order and the
    # budget still favors the higher-priority type.
    groups: dict[Any, list] = {}
    for delivery, event in rows:
        key = (delivery.user_id, delivery.event_type) if digest_on else (delivery.id,)
        groups.setdefault(key, []).append((delivery, event))

    sent = failed = suppressed = retry = errored = held = budget_dropped = pushes = deferred = 0
    for members in groups.values():
        # Per-group isolation via a SAVEPOINT: a DB error resolving ONE group (e.g. a
        # transient failure in a per-user query) rolls back only that group instead of
        # poisoning the whole Postgres transaction. Without it, one error aborts the tx
        # so every later group errors AND the final commit fails, discarding the state
        # of groups whose push already went out — which re-sends them next tick. An
        # errored group is left pending; earlier groups' committed state survives.
        deliveries = [d for d, _ in members]
        events = [e for _, e in members]
        uid = deliveries[0].user_id
        et = deliveries[0].event_type
        exempt = et in _EXEMPT_EVENT_TYPES
        try:
            async with session.begin_nested():
                # Daily-cadence hold: outside the digest hour, defer a type the user
                # set to "daily" to the next digest window (UTC). During the window
                # this gate is skipped, so the day's accumulation falls through and
                # coalesces. A hold sets a FUTURE next_retry_at + last_error only, never
                # attempt_count (a hold is not a failure).
                #
                # `not already_quiet_held` breaks a stuck-forever loop: a daily row
                # reaches the quiet gate ONLY during the digest window (this gate holds
                # it otherwise), so last_error == "quiet_hours_hold" means the digest
                # window already passed and it is now only waiting for quiet to close.
                # Re-holding it to the next UTC digest hour would ping-pong it against
                # the user's LOCAL quiet-close (the two clocks never align) and it would
                # never send. Skipping the cadence gate lets the quiet gate release it.
                already_quiet_held = deliveries[0].last_error == "quiet_hours_hold"
                if cadence_possible and not exempt and not is_digest_window and not already_quiet_held:
                    if uid not in cad_cache:
                        cad_cache[uid] = await _user_cadence(session, uid)
                    pref = NOTIF_PREF_COLUMNS.get(et)
                    if pref is not None and cad_cache[uid].get(pref) == "daily":
                        hold_at = _next_daily_time(now, digest_hour)
                        for d in deliveries:
                            d.next_retry_at = hold_at
                            d.last_error = "daily_cadence_hold"
                        deferred += len(deliveries)
                        continue

                # Quiet-hours hold (whole group): defer to the window close. Rows stay
                # pending (attempt_count untouched — a hold is not a failure). The
                # window is resolved per-user (the app's override, else the global).
                if quiet_possible and not exempt:
                    if uid not in qh_cache:
                        qh_cache[uid] = await _user_quiet_hours(session, uid)
                    qh_enabled, qh_start, qh_end = qh_cache[uid]
                    if qh_enabled:
                        if uid not in tz_cache:
                            tz_cache[uid] = await _user_tz(session, uid)
                        if _in_quiet_hours(now, tz_cache[uid], qh_start, qh_end):
                            hold_at = _quiet_window_open(now, tz_cache[uid], qh_start, qh_end)
                            for d in deliveries:
                                d.next_retry_at = hold_at
                                d.last_error = "quiet_hours_hold"
                            held += len(deliveries)
                            continue

                # Daily budget: one group = one push = one budget unit. Drop the whole
                # (non-urgent) group once the user hit the cap.
                if budget > 0 and not exempt and tally.get(uid, 0) >= budget:
                    for d in deliveries:
                        d.dispatch_state = "suppressed"
                        d.last_error = "daily_budget_exceeded"
                    budget_dropped += len(deliveries)
                    continue

                urls = await _channels_for(session, uid, et)
                if not urls:
                    for d in deliveries:
                        d.dispatch_state = "suppressed"  # nothing to deliver to; never retried
                    suppressed += len(deliveries)
                    continue

                # One synthesized message for the group (a group of one keeps its own).
                # Apprise is a sync lib → offload to a thread (bounded by
                # APPRISE_TIMEOUT_S) so it can't block the loop or hang on a wedged relay.
                # Tap-routing: fold the event's structured `data` (type/digest/count/...)
                # onto the relay capability URLs as Apprise `:key=value` add-params so a
                # tapped push deep-links to the right surface. `type` rides as `route`
                # (Apprise reserves `type`); the relay maps it back to `data.type`. A
                # missing `route` is harmless (the relay keeps today's default), so
                # grooveiq and the relay may deploy in either order. Non-relay channels
                # pass through untouched — see _augment_capability_url.
                title, body, data = _build_digest(et, events)
                routed_urls = [_augment_capability_url(u, data) for u in urls]
                ok = await _apprise_notify_bounded(routed_urls, title, body)
                if ok:
                    for d in deliveries:
                        d.dispatch_state = "sent"
                        d.notified_at = now
                        d.last_error = None
                    deliveries[0].budget_counted = True  # one head row per push = the budget unit
                    tally[uid] = tally.get(uid, 0) + 1  # one push per group counts toward the budget
                    sent += len(deliveries)
                    pushes += 1
                    continue

                # Transient failure: back off every member of the group together.
                for d in deliveries:
                    d.attempt_count = (d.attempt_count or 0) + 1
                    d.last_error = "apprise_notify_failed"
                    age_h = (now - (d.created_at or now)) / 3600.0
                    if d.attempt_count >= settings.NOTIFY_MAX_ATTEMPTS or age_h >= settings.DISPATCH_MAX_AGE_HOURS:
                        d.dispatch_state = "failed"
                        failed += 1
                    else:
                        d.next_retry_at = now + _backoff_seconds(d.attempt_count)
                        retry += 1
        except Exception as exc:
            logger.warning("dispatch: group (%s, %s) errored, left pending: %s", uid, et, exc)
            errored += len(deliveries)

    await session.commit()
    summary = {"processed": len(rows), "sent": sent, "failed": failed, "suppressed": suppressed, "retry": retry}
    if pushes and pushes != sent:
        summary["pushes"] = pushes  # coalescing happened: fewer pushes than deliveries
    if held:
        summary["held"] = held
    if deferred:
        summary["deferred"] = deferred  # held for a daily-cadence digest window
    if budget_dropped:
        summary["budget_dropped"] = budget_dropped
    if errored:
        summary["errored"] = errored
    return summary


async def _channels_for(session: AsyncSession, user_id: str, event_type: str) -> list[str]:
    """Apprise URLs of a user's active devices opted-in for ``event_type``.

    A known type's dedicated pref column (``NOTIF_PREF_COLUMNS``) is matched with
    ``isnot(False)`` so a legacy device that predates the column (NULL) is treated
    as opted-in, matching the server-side default + the shipped iOS toggles. A
    server-driven type whose pref_field has no dedicated column yet is gated
    per-device by the ``notif_extra`` JSON map (absent/True = opted-in), so the
    client can turn a future category off. An event_type with no pref mapping at all
    fails OPEN so a new producer is never silently dropped."""
    query = select(Device).where(Device.user_id == user_id, Device.disabled_at.is_(None))
    pref = NOTIF_PREF_COLUMNS.get(event_type)
    pref_is_column = pref is not None and pref in Device.__table__.columns
    if pref_is_column:
        query = query.where(getattr(Device, pref).isnot(False))
    elif pref is None:
        # Unknown type (no producer should hit this) → fail OPEN to all active
        # devices rather than silently drop a new notification type.
        logger.warning(
            "dispatch: unknown event_type %r has no pref column; delivering to all active devices", event_type
        )
    devices = (await session.execute(query)).scalars().all()
    urls: list[str] = []
    for d in devices:
        # Future server-driven type (pref_field without a column): honor the
        # per-device notif_extra override; absent/True stays opted-in (fail open).
        if pref is not None and not pref_is_column and (d.notif_extra or {}).get(pref) is False:
            continue
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


# Keys from a notification event's structured ``data`` that are forwarded to the
# relay for TAP-ROUTING, appended to a capability URL as Apprise "add" params
# (``:key=value``, which the json:// notifier injects verbatim into the POSTed JSON
# body). ``type`` is deliberately RENAMED to ``route``: Apprise's json:// payload
# RESERVES ``type`` for the notify severity, and the ``:type=`` form REMAPS it
# (yielding ``{"<value>": "info"}``) instead of adding a ``type`` key — which would
# corrupt EVERY push, including the working download-finished one. ``route`` is
# non-reserved and adds cleanly; the relay maps it back to ``data.type`` for the app.
_ROUTE_SOURCE_KEYS: tuple[str, ...] = ("type", "digest", "count", "playlist_id")


def _augment_capability_url(url: str, data: dict[str, Any] | None) -> str:
    """Return ``url`` with tap-routing fields from ``data`` appended as Apprise
    ``:key=value`` add-params, so a tapped push can deep-link to the right surface.

    Scoped to the relay's ``json://`` / ``jsons://`` capability scheme — every other
    channel (ntfy/telegram/...) is returned byte-for-byte unchanged, so this can't
    perturb a non-iOS delivery. ``type`` is emitted as ``route`` (see
    ``_ROUTE_SOURCE_KEYS``); booleans become ``true``/``false``; non-scalars are
    skipped. ANY problem — empty data, a non-capability scheme, or an unexpected
    error — returns the ORIGINAL url, so a hiccup degrades to today's bare-URL
    delivery instead of dropping the push. Deterministic, so two device rows sharing
    one capability URL stay identical and de-dupe as before."""
    try:
        if not data:
            return url
        scheme = url.split("://", 1)[0].lower()
        if scheme not in ("json", "jsons"):
            return url
        parts: list[str] = []
        for src in _ROUTE_SOURCE_KEYS:
            if src not in data:
                continue
            val = data[src]
            if not isinstance(val, (str, int, float, bool)):
                continue  # scalars only (bool is an int subclass — normalized below)
            out_key = "route" if src == "type" else src
            sval = ("true" if val else "false") if isinstance(val, bool) else str(val)
            parts.append(f":{quote(out_key, safe='')}={quote(sval, safe='')}")
        if not parts:
            return url
        return url + ("&" if "?" in url else "?") + "&".join(parts)
    except Exception as exc:  # never let tap-routing break delivery
        logger.warning("dispatch: capability-URL route augmentation failed (%s); sending bare URL", exc)
        return url


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
