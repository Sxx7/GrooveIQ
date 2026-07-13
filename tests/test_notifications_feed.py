"""GrooveIQ – Tests for the unified in-app notification feed (Activity sheet).

Same in-memory-SQLite + dependency-override harness as tests/test_devices.py.
Seeds ``notification_events`` + ``notification_deliveries`` directly and drives
``GET /v1/users/{id}/notifications`` + ``POST .../seen``. Covers the outbox
projection (typed items + FULL captured data survives), ``unseen_count`` + seen
stamping (ids + all), the event_type filter (download/newly_added excluded),
``unseen_only``, and paging via the ``before`` cursor.

Run with:  .venv-test/bin/pytest tests/test_notifications_feed.py -v
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, NotificationDelivery, NotificationEvent

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


async def _seed(user_id, event_type, *, title, body, data, created_at, seen_at=None, dedup_key=None):
    """Insert one event + one delivery for ``user_id``; returns the delivery id."""
    async with _TestSession() as s:
        ev = NotificationEvent(
            event_type=event_type, dedup_key=dedup_key, title=title, body=body,
            data=data, created_at=created_at,
        )
        s.add(ev)
        await s.flush()
        d = NotificationDelivery(
            user_id=user_id, event_id=ev.id, event_type=event_type, dedup_key=dedup_key,
            dispatch_state="pending", created_at=created_at, seen_at=seen_at,
        )
        s.add(d)
        await s.flush()
        did = d.id
        await s.commit()
        return did


@pytest.mark.asyncio
async def test_feed_projects_typed_items_newest_first(client):
    now = int(time.time())
    await _seed(
        "alice", "new_release", title="New album", body="Artist – Album",
        data={"type": "new_release", "cover_url": "http://c/r.jpg", "artist": "Artist",
              "album": "Album", "kind": "album"}, created_at=now - 100,
    )
    await _seed(
        "alice", "recommendation", title="An album you might like", body="Rec – RecArtist",
        data={"type": "recommendation", "kind": "album", "cover_url": "http://c/a.jpg",
              "album": "Rec", "album_artist": "RecArtist"}, created_at=now,
    )

    r = await client.get("/v1/users/alice/notifications")
    assert r.status_code == 200
    body = r.json()
    assert body["unseen_count"] == 2
    items = body["items"]
    assert [i["event_type"] for i in items] == ["recommendation", "new_release"]  # newest first
    reco = items[0]
    assert reco["data"]["cover_url"] == "http://c/a.jpg"  # FULL data survives the projection
    assert reco["data"]["kind"] == "album"
    assert reco["seen_at"] is None
    assert reco["title"] == "An album you might like"


@pytest.mark.asyncio
async def test_seen_ids_then_all_clears_badge(client):
    now = int(time.time())
    d1 = await _seed("alice", "recommendation", title="a", body="b",
                     data={"type": "recommendation"}, created_at=now)
    await _seed("alice", "new_release", title="c", body="d",
                data={"type": "new_release"}, created_at=now - 10)

    assert (await client.get("/v1/users/alice/notifications")).json()["unseen_count"] == 2

    r = await client.post("/v1/users/alice/notifications/seen", json={"ids": [d1]})
    assert r.status_code == 200 and r.json()["updated"] == 1
    assert (await client.get("/v1/users/alice/notifications")).json()["unseen_count"] == 1

    r = await client.post("/v1/users/alice/notifications/seen", json={"all": True})
    assert r.status_code == 200 and r.json()["updated"] == 1
    assert (await client.get("/v1/users/alice/notifications")).json()["unseen_count"] == 0


@pytest.mark.asyncio
async def test_excludes_non_activity_event_types(client):
    now = int(time.time())
    await _seed("alice", "download_completed", title="dl", body="done",
                data={"type": "download_finished"}, created_at=now)
    await _seed("alice", "newly_added", title="nm", body="added",
                data={"type": "new_media"}, created_at=now)
    await _seed("alice", "recommendation", title="r", body="b",
                data={"type": "recommendation"}, created_at=now)

    body = (await client.get("/v1/users/alice/notifications")).json()
    assert body["unseen_count"] == 1  # download/newly_added excluded from the Activity badge
    assert [i["event_type"] for i in body["items"]] == ["recommendation"]


@pytest.mark.asyncio
async def test_unseen_only_and_paging(client):
    now = int(time.time())
    await _seed("alice", "recommendation", title="old-seen", body="b",
                data={"type": "recommendation"}, created_at=now - 200, seen_at=now)
    await _seed("alice", "recommendation", title="mid", body="b",
                data={"type": "recommendation"}, created_at=now - 100)
    await _seed("alice", "new_release", title="new", body="b",
                data={"type": "new_release"}, created_at=now)

    # unseen_only drops the already-seen row
    body = (await client.get("/v1/users/alice/notifications", params={"unseen_only": True})).json()
    assert [i["title"] for i in body["items"]] == ["new", "mid"]

    # paging: limit 1 → next_before cursor walks to the older rows
    p1 = (await client.get("/v1/users/alice/notifications", params={"limit": 1})).json()
    assert [i["title"] for i in p1["items"]] == ["new"]
    assert p1["next_before"] == now
    p2 = (
        await client.get(
            "/v1/users/alice/notifications", params={"limit": 5, "before": p1["next_before"]}
        )
    ).json()
    assert [i["title"] for i in p2["items"]] == ["mid", "old-seen"]
    assert p2["next_before"] is None


@pytest.mark.asyncio
async def test_retention_window_hides_old_from_feed(client):
    now = int(time.time())
    await _seed("alice", "recommendation", title="fresh", body="b",
                data={"type": "recommendation"}, created_at=now)
    await _seed("alice", "new_release", title="ancient", body="b",
                data={"type": "new_release"}, created_at=now - 40 * 86_400)  # > 30d retention

    body = (await client.get("/v1/users/alice/notifications")).json()
    assert [i["title"] for i in body["items"]] == ["fresh"]  # 40d-old dropped by the 30d window
    assert body["unseen_count"] == 1  # badge excludes the aged-out row too


@pytest.mark.asyncio
async def test_prune_deletes_old_keeps_recent():
    from sqlalchemy import func, select

    from app.models.db import NotificationDelivery, NotificationEvent
    from app.services.notification_dispatch import prune_old_notifications

    now = int(time.time())
    await _seed("alice", "recommendation", title="fresh", body="b",
                data={"type": "recommendation"}, created_at=now)
    await _seed("alice", "new_release", title="ancient", body="b",
                data={"type": "new_release"}, created_at=now - 40 * 86_400)

    async with _TestSession() as s:
        result = await prune_old_notifications(s, now=now)
        await s.commit()
    assert result == {"deliveries": 1, "events": 1}  # only the 40d-old pair pruned

    async with _TestSession() as s:
        deliveries = (await s.execute(select(func.count()).select_from(NotificationDelivery))).scalar_one()
        events = (await s.execute(select(func.count()).select_from(NotificationEvent))).scalar_one()
    assert deliveries == 1 and events == 1  # the fresh pair survives
