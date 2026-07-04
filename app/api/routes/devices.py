"""GrooveIQ – device / notification-target registration routes (P2).

Per-user, self-service registration of a notification channel. Delivery is
Apprise-only: an iOS device registers the per-device capability URL minted by the
APN relay (as an ``apprise_urls`` entry); grooveiq holds no Apple creds / relay
secret. This is a normal per-user endpoint (api-key + user_id), NOT admin — so
any user on a multi-user instance can enable push without operator involvement.

Trust (overview §7): a device may only register/list/delete for the ``user_id``
it presents, and must carry the app api-key like every other route. Residual
risk (someone who knows another user's id AND the app api-key could register a
channel against them) is accepted at this ~5–10-user scale; a per-user
registration token is deferred.

DELETE returns ``200 + a JSON body`` (not 204): the iOS Alamofire client throws
``invalidEmptyResponse`` on a true-empty 204, so every grooveiq DELETE in this
initiative (follows, feed-seen) returns a body — devices match that.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.models.db import Device
from app.models.schemas import (
    DeviceDelete,
    DeviceRegister,
    NotificationSettingsUpdate,
    NotificationTest,
)
from app.services.notification_dispatch import NOTIF_PREF_FIELDS, NOTIFICATION_TYPES, send_test_notification

router = APIRouter()

# The per-type toggle fields exposed on a device (single source of truth: the
# notification-type registry in notification_dispatch).
_PREF_FIELDS = NOTIF_PREF_FIELDS


def _device_view(d: Device) -> dict:
    """Serialize a device for the notification-settings responses. Includes the
    per-type prefs (goal C/B/F toggles) + stable identity (goal E) alongside the
    original keys, so an older client that only reads ``notif_new_releases`` keeps
    working."""
    return {
        "device_id": d.id,
        "platform": d.platform,
        "apns_environment": d.apns_environment,
        "device_guid": d.device_guid,
        "device_name": d.device_name,
        "apprise_urls": d.apprise_urls,
        "disabled_at": d.disabled_at,
        # Per-type toggles. NULL (legacy row) reads as True to match the opt-in
        # default the dispatcher applies.
        "notif_new_releases": d.notif_new_releases is not False,
        "notif_new_media": d.notif_new_media is not False,
        "notif_download_finished": d.notif_download_finished is not False,
        "notif_recommendations": d.notif_recommendations is not False,
    }


@router.post("/devices", summary="Register (upsert) a notification device")
async def register_device(
    body: DeviceRegister,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(body.user_id)  # 400 on malformed
    check_user_access(_key, body.user_id)  # 403 when API_KEY_USERS bound
    now = int(time.time())

    device: Device | None = None
    if body.device_guid:
        # Preferred: stable per-frontend identity (goal E). Upsert on
        # (user_id, device_guid) so a rotated capability URL / refreshed token
        # reconciles to the SAME row and per-device prefs survive.
        device = (
            await session.execute(
                select(Device).where(Device.user_id == body.user_id, Device.device_guid == body.device_guid)
            )
        ).scalar_one_or_none()
    if device is None and body.apns_token:
        # Upsert by the unique apns_token (legacy native path): the constraint is
        # the guard, not a check-then-insert race.
        device = (
            await session.execute(select(Device).where(Device.apns_token == body.apns_token))
        ).scalar_one_or_none()
    if device is None and body.apprise_urls:
        # Fall back to the capability URL (per-device + unguessable): dedup by it
        # so a per-launch re-register updates one row instead of piling up. This
        # ALSO runs when a device_guid was supplied but matched nothing yet, so a
        # newly-guid-aware client adopts its existing pre-guid row (the guid is
        # then stamped on below) instead of creating a duplicate that shares the
        # same URL — which would double every push.
        incoming = set(body.apprise_urls)
        candidates = (
            (await session.execute(select(Device).where(Device.user_id == body.user_id, Device.apns_token.is_(None))))
            .scalars()
            .all()
        )
        for d in candidates:
            if d.apprise_urls and incoming.intersection(d.apprise_urls):
                device = d
                break

    if device is None:
        device = Device(user_id=body.user_id, apns_token=body.apns_token)
        session.add(device)

    device.user_id = body.user_id  # a re-register may re-key the token to a new user
    device.platform = body.platform
    device.apns_environment = body.apns_environment
    device.apprise_urls = body.apprise_urls
    device.notif_new_releases = body.notif_new_releases
    device.notif_new_media = body.notif_new_media
    device.notif_download_finished = body.notif_download_finished
    device.notif_recommendations = body.notif_recommendations
    if body.device_guid:
        device.device_guid = body.device_guid
    if body.device_name is not None:
        device.device_name = body.device_name
    device.last_seen_at = now
    device.disabled_at = None  # clear on re-register (reactivates a pruned token)
    await session.flush()  # assign id
    await session.commit()
    return {"device_id": device.id}


@router.delete("/devices", summary="Unregister a device by id or token")
async def delete_device(
    body: DeviceDelete,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Soft-delete by ``device_id`` (any channel, incl. Apprise-only rows) or the
    legacy ``apns_token`` (200 + body; a re-register reactivates a token row).
    Idempotent: an unknown id/token is still 200."""
    if body.device_id is not None:
        device = (await session.execute(select(Device).where(Device.id == body.device_id))).scalar_one_or_none()
    else:
        device = (
            await session.execute(select(Device).where(Device.apns_token == body.apns_token))
        ).scalar_one_or_none()
    if device is None:
        return {"status": "ok", "disabled": 0}  # idempotent — nothing to remove
    check_user_access(_key, device.user_id)
    already = device.disabled_at is not None
    device.disabled_at = int(time.time())
    await session.commit()
    return {"status": "ok", "disabled": 0 if already else 1}


@router.get(
    "/users/{user_id}/notification-settings",
    summary="List a user's notification devices + toggles",
)
async def get_notification_settings(
    user_id: str = Path(..., min_length=1, max_length=128),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    devices = (
        (await session.execute(select(Device).where(Device.user_id == user_id, Device.disabled_at.is_(None))))
        .scalars()
        .all()
    )
    return {"user_id": user_id, "devices": [_device_view(d) for d in devices]}


@router.patch(
    "/users/{user_id}/notification-settings",
    summary="Update per-type notification toggles for a user's devices",
)
async def patch_notification_settings(
    body: NotificationSettingsUpdate,
    user_id: str = Path(..., min_length=1, max_length=128),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Apply any subset of the per-type toggles (new_releases / new_media /
    download_finished / recommendations) to all of the user's devices, or just one
    when ``device_id`` is given. Only the fields present in the body are changed.
    The ``user_id`` filter scopes the id lookup, so a device_id owned by another
    user is a safe no-op."""
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    query = select(Device).where(Device.user_id == user_id)
    if body.device_id is not None:
        query = query.where(Device.id == body.device_id)
    devices = (await session.execute(query)).scalars().all()
    updates = {f: getattr(body, f) for f in _PREF_FIELDS if getattr(body, f) is not None}
    for d in devices:
        for field, value in updates.items():
            setattr(d, field, value)
    await session.commit()
    return await get_notification_settings(user_id=user_id, session=session, _key=_key)


@router.get("/notification-types", summary="List notification categories (server-driven)")
async def get_notification_types(_key: str = Depends(require_api_key)):
    """The available notification categories + display metadata, so the client can
    render toggles server-driven (a new type needs no app rebuild). Each entry's
    ``pref_field`` is the toggle key accepted by POST /v1/devices + PATCH
    notification-settings."""
    return {
        "types": [
            {"key": t["key"], "label": t["label"], "description": t["description"], "pref_field": t["pref_field"]}
            for t in NOTIFICATION_TYPES
        ]
    }


@router.post(
    "/users/{user_id}/notification-settings/test",
    summary="Send a test notification to a user's channels",
)
async def test_notification(
    user_id: str = Path(..., min_length=1, max_length=128),
    body: NotificationTest | None = None,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Fire an immediate test push. Ignores the ``PUSH_ENABLED`` master switch and
    the per-device mute so a channel can be verified during setup — returns
    ``{sent, channels}`` (``sent=False, channels=0`` when the user has none)."""
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    return await send_test_notification(session, user_id, device_id=body.device_id if body else None)
