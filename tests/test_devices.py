"""GrooveIQ – Tests for the device / notification-target registry (P2, h30).

Same in-memory-SQLite + dependency-override harness as tests/test_follows.py.
conftest runs the app in the DISABLE_AUTH degraded path with a relaxed
USER_ID_PATTERN, so "alice" is a valid user and "bad!id" is rejected.

DELETE returns 200 + a JSON body (not 204) — the iOS Alamofire client throws on
a true-empty 204, so the whole followed-artists initiative returns bodies.

Run with:  .venv-test/bin/pytest tests/test_devices.py -v
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, Device

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.dependency_overrides[get_session] = override_get_session
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.core import security

    def _clear() -> None:
        windows = getattr(security._limiter, "_windows", None)
        if windows is not None:
            windows.clear()

    _clear()
    yield
    _clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


async def _device_rows(user_id: str | None = None) -> list[Device]:
    async with _TestSession() as s:
        q = select(Device)
        if user_id is not None:
            q = q.where(Device.user_id == user_id)
        return list((await s.execute(q)).scalars().all())


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_register_creates_row(client: AsyncClient):
    resp = await client.post(
        "/v1/devices",
        json={"user_id": "alice", "apns_token": "abc123", "apns_environment": "sandbox"},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json()["device_id"], int)

    rows = await _device_rows("alice")
    assert len(rows) == 1
    assert rows[0].apns_token == "abc123"
    assert rows[0].apns_environment == "sandbox"
    assert rows[0].disabled_at is None
    assert rows[0].last_seen_at > 0
    assert rows[0].notif_new_releases is True


async def test_reregister_same_token_is_idempotent(client: AsyncClient):
    first = await client.post("/v1/devices", json={"user_id": "alice", "apns_token": "tok"})
    # simulate a prior 410-prune, then re-register clears it
    async with _TestSession() as s:
        dev = (await s.execute(select(Device).where(Device.apns_token == "tok"))).scalar_one()
        dev.disabled_at = 111
        await s.commit()

    second = await client.post(
        "/v1/devices", json={"user_id": "alice", "apns_token": "tok", "notif_new_releases": False}
    )
    assert second.status_code == 200
    assert second.json()["device_id"] == first.json()["device_id"]  # same row

    rows = await _device_rows()
    assert len(rows) == 1  # not duplicated
    assert rows[0].disabled_at is None  # cleared on re-register
    assert rows[0].notif_new_releases is False  # updated


async def test_register_apprise_only(client: AsyncClient):
    resp = await client.post("/v1/devices", json={"user_id": "alice", "apprise_urls": ["ntfy://topic"]})
    assert resp.status_code == 200
    rows = await _device_rows("alice")
    assert rows[0].apns_token is None
    assert rows[0].apprise_urls == ["ntfy://topic"]


async def test_reregister_same_apprise_url_is_idempotent(client: AsyncClient):
    # The app re-registers its stable relay capability URL on each launch; dedup
    # by the URL so it updates one row instead of piling up (no apns_token to key on).
    url = "jsons://relay.example/v1/apprise/cap-id-123"
    first = await client.post("/v1/devices", json={"user_id": "alice", "apprise_urls": [url]})
    second = await client.post(
        "/v1/devices", json={"user_id": "alice", "apprise_urls": [url], "notif_new_releases": False}
    )
    assert second.status_code == 200
    assert second.json()["device_id"] == first.json()["device_id"]  # same row

    rows = await _device_rows("alice")
    assert len(rows) == 1  # not duplicated
    assert rows[0].notif_new_releases is False  # updated


async def test_register_requires_a_target(client: AsyncClient):
    resp = await client.post("/v1/devices", json={"user_id": "alice"})
    assert resp.status_code == 422  # pydantic model_validator: needs token and/or urls


async def test_register_malformed_user_id_400(client: AsyncClient):
    resp = await client.post("/v1/devices", json={"user_id": "bad!id", "apns_token": "t"})
    assert resp.status_code == 400


async def test_delete_by_token_soft_deletes(client: AsyncClient):
    await client.post("/v1/devices", json={"user_id": "alice", "apns_token": "tok"})
    resp = await client.request("DELETE", "/v1/devices", json={"apns_token": "tok"})
    assert resp.status_code == 200
    assert resp.json()["disabled"] == 1

    rows = await _device_rows()
    assert rows[0].disabled_at is not None

    # a subsequent register reactivates (clears disabled_at)
    await client.post("/v1/devices", json={"user_id": "alice", "apns_token": "tok"})
    rows = await _device_rows()
    assert len(rows) == 1
    assert rows[0].disabled_at is None


async def test_delete_unknown_token_is_idempotent(client: AsyncClient):
    resp = await client.request("DELETE", "/v1/devices", json={"apns_token": "nope"})
    assert resp.status_code == 200
    assert resp.json()["disabled"] == 0


async def test_delete_by_device_id_soft_deletes(client: AsyncClient):
    # An Apprise-only channel (e.g. added from the dashboard) has no apns_token,
    # so it can only be removed by its device_id.
    reg = await client.post("/v1/devices", json={"user_id": "alice", "apprise_urls": ["ntfy://topic"]})
    device_id = reg.json()["device_id"]

    resp = await client.request("DELETE", "/v1/devices", json={"device_id": device_id})
    assert resp.status_code == 200
    assert resp.json()["disabled"] == 1

    rows = await _device_rows()
    assert rows[0].disabled_at is not None


async def test_delete_unknown_device_id_is_idempotent(client: AsyncClient):
    resp = await client.request("DELETE", "/v1/devices", json={"device_id": 999999})
    assert resp.status_code == 200
    assert resp.json()["disabled"] == 0


async def test_delete_requires_an_identifier(client: AsyncClient):
    resp = await client.request("DELETE", "/v1/devices", json={})
    assert resp.status_code == 422  # model_validator: needs device_id and/or apns_token


async def test_patch_scoped_to_one_device(client: AsyncClient):
    a = await client.post("/v1/devices", json={"user_id": "alice", "apns_token": "t1"})
    await client.post("/v1/devices", json={"user_id": "alice", "apns_token": "t2"})
    only = a.json()["device_id"]

    patched = await client.patch(
        "/v1/users/alice/notification-settings",
        json={"notif_new_releases": False, "device_id": only},
    )
    assert patched.status_code == 200
    by_id = {d["device_id"]: d["notif_new_releases"] for d in patched.json()["devices"]}
    assert by_id[only] is False  # scoped device flipped
    assert all(v for k, v in by_id.items() if k != only)  # the other left untouched


async def test_send_test_notification_hits_apprise(client: AsyncClient, monkeypatch):
    from app.services import notification_dispatch as nd

    seen: dict = {}

    def _fake(urls, title, body):
        seen["urls"] = urls
        seen["title"] = title
        return True

    monkeypatch.setattr(nd, "_apprise_notify", _fake)

    # notif muted → the test must still fire (it verifies the channel, ignoring mute)
    await client.post(
        "/v1/devices",
        json={"user_id": "alice", "apprise_urls": ["ntfy://topic"], "notif_new_releases": False},
    )
    resp = await client.post("/v1/users/alice/notification-settings/test", json={})
    assert resp.status_code == 200
    assert resp.json() == {"sent": True, "channels": 1}
    assert seen["urls"] == ["ntfy://topic"]
    assert "GrooveIQ" in seen["title"]


async def test_send_test_notification_no_channels(client: AsyncClient):
    resp = await client.post("/v1/users/alice/notification-settings/test", json={})
    assert resp.status_code == 200
    assert resp.json()["sent"] is False
    assert resp.json()["channels"] == 0


async def test_notification_settings_list_and_patch(client: AsyncClient):
    await client.post("/v1/devices", json={"user_id": "alice", "apns_token": "t1"})
    await client.post("/v1/devices", json={"user_id": "alice", "apns_token": "t2"})

    got = await client.get("/v1/users/alice/notification-settings")
    assert got.status_code == 200
    assert len(got.json()["devices"]) == 2
    assert all(d["notif_new_releases"] for d in got.json()["devices"])

    patched = await client.patch("/v1/users/alice/notification-settings", json={"notif_new_releases": False})
    assert patched.status_code == 200
    assert all(not d["notif_new_releases"] for d in patched.json()["devices"])


# ── P2: per-type prefs + stable device identity (goal E) ─────────────────────


async def test_register_persists_all_prefs_and_identity(client: AsyncClient):
    resp = await client.post(
        "/v1/devices",
        json={
            "user_id": "alice",
            "apprise_urls": ["jsons://relay/v1/apprise/cap1"],
            "notif_new_media": False,
            "notif_download_finished": True,
            "notif_recommendations": False,
            "device_guid": "GUID-1",
            "device_name": "Alice iPhone",
        },
    )
    assert resp.status_code == 200
    rows = await _device_rows("alice")
    assert len(rows) == 1
    d = rows[0]
    assert d.device_guid == "GUID-1"
    assert d.device_name == "Alice iPhone"
    assert d.notif_new_media is False
    assert d.notif_download_finished is True
    assert d.notif_recommendations is False

    got = await client.get("/v1/users/alice/notification-settings")
    view = got.json()["devices"][0]
    assert view["device_guid"] == "GUID-1"
    assert view["device_name"] == "Alice iPhone"
    assert view["notif_new_media"] is False
    assert view["notif_download_finished"] is True


async def test_device_guid_upsert_survives_url_rotation(client: AsyncClient):
    # Same device_guid, a rotated capability URL → the SAME row, prefs preserved.
    first = await client.post(
        "/v1/devices",
        json={
            "user_id": "alice",
            "apprise_urls": ["jsons://relay/v1/apprise/OLD"],
            "device_guid": "GUID-1",
            "notif_new_media": False,
        },
    )
    second = await client.post(
        "/v1/devices",
        json={
            "user_id": "alice",
            "apprise_urls": ["jsons://relay/v1/apprise/NEW"],
            "device_guid": "GUID-1",
        },
    )
    assert second.json()["device_id"] == first.json()["device_id"]  # reconciled by guid
    rows = await _device_rows("alice")
    assert len(rows) == 1
    assert rows[0].apprise_urls == ["jsons://relay/v1/apprise/NEW"]  # URL rotated in


async def test_patch_partial_leaves_other_prefs_untouched(client: AsyncClient):
    await client.post(
        "/v1/devices",
        json={"user_id": "alice", "apns_token": "t1", "notif_new_releases": True, "notif_new_media": True},
    )
    patched = await client.patch("/v1/users/alice/notification-settings", json={"notif_new_media": False})
    assert patched.status_code == 200
    d = patched.json()["devices"][0]
    assert d["notif_new_media"] is False  # changed
    assert d["notif_new_releases"] is True  # untouched by a partial update


async def test_patch_requires_a_pref(client: AsyncClient):
    await client.post("/v1/devices", json={"user_id": "alice", "apns_token": "t1"})
    resp = await client.patch("/v1/users/alice/notification-settings", json={"device_id": 1})
    assert resp.status_code == 422  # no pref field supplied


async def test_notification_types_endpoint(client: AsyncClient):
    resp = await client.get("/v1/notification-types")
    assert resp.status_code == 200
    keys = {t["key"] for t in resp.json()["types"]}
    assert keys == {"new_releases", "new_media", "downloads", "recommendations"}
    for t in resp.json()["types"]:
        assert t["pref_field"].startswith("notif_")
        assert t["label"] and t["description"]
