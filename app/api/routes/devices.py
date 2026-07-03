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
from app.models.schemas import DeviceDelete, DeviceRegister, NotificationSettingsUpdate

router = APIRouter()


@router.post("/devices", summary="Register (upsert) a notification device")
async def register_device(
    body: DeviceRegister,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(body.user_id)          # 400 on malformed
    check_user_access(_key, body.user_id)   # 403 when API_KEY_USERS bound
    now = int(time.time())

    device: Device | None = None
    if body.apns_token:
        # Upsert by the unique apns_token (legacy native path): the constraint is
        # the guard, not a check-then-insert race.
        device = (
            await session.execute(select(Device).where(Device.apns_token == body.apns_token))
        ).scalar_one_or_none()
    elif body.apprise_urls:
        # Apprise-only device (e.g. the relay capability URL): dedup by the stable
        # URL so the app's per-launch re-register updates one row instead of piling
        # up (no unique apns_token to key on). URLs are per-device + unguessable.
        incoming = set(body.apprise_urls)
        candidates = (
            await session.execute(
                select(Device).where(
                    Device.user_id == body.user_id, Device.apns_token.is_(None)
                )
            )
        ).scalars().all()
        for d in candidates:
            if d.apprise_urls and incoming.intersection(d.apprise_urls):
                device = d
                break

    if device is None:
        device = Device(user_id=body.user_id, apns_token=body.apns_token)
        session.add(device)

    device.user_id = body.user_id           # a re-register may re-key the token to a new user
    device.platform = body.platform
    device.apns_environment = body.apns_environment
    device.apprise_urls = body.apprise_urls
    device.notif_new_releases = body.notif_new_releases
    device.last_seen_at = now
    device.disabled_at = None               # clear on re-register (reactivates a pruned token)
    await session.flush()                   # assign id
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
        device = (
            await session.execute(select(Device).where(Device.id == body.device_id))
        ).scalar_one_or_none()
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
        await session.execute(
            select(Device).where(Device.user_id == user_id, Device.disabled_at.is_(None))
        )
    ).scalars().all()
    return {
        "user_id": user_id,
        "devices": [
            {
                "device_id": d.id,
                "platform": d.platform,
                "apns_environment": d.apns_environment,
                "notif_new_releases": d.notif_new_releases,
                "apprise_urls": d.apprise_urls,
                "disabled_at": d.disabled_at,
            }
            for d in devices
        ],
    }


@router.patch(
    "/users/{user_id}/notification-settings",
    summary="Toggle new-release notifications for a user's devices",
)
async def patch_notification_settings(
    body: NotificationSettingsUpdate,
    user_id: str = Path(..., min_length=1, max_length=128),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Flip ``notif_new_releases`` on all of the user's devices, or just one when
    ``device_id`` is given. The ``user_id`` filter also scopes the id lookup, so a
    device_id belonging to another user is a safe no-op."""
    validate_user_id(user_id)
    check_user_access(_key, user_id)
    query = select(Device).where(Device.user_id == user_id)
    if body.device_id is not None:
        query = query.where(Device.id == body.device_id)
    devices = (await session.execute(query)).scalars().all()
    for d in devices:
        d.notif_new_releases = body.notif_new_releases
    await session.commit()
    return await get_notification_settings(user_id=user_id, session=session, _key=_key)
