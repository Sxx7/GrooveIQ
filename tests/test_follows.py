"""GrooveIQ – Tests for followed-artist persistence + monitoring (P0).

Uses the same in-memory-SQLite harness as tests/test_events.py. conftest.py
runs the app in the "not configured" degraded path (DISABLE_AUTH, cleared
LIDARR/LASTFM env), which is exactly what the "resolution never blocks the
follow" assertions want. The autouse ``_disable_upstreams`` fixture also pins
STREAMRIP off (conftest does NOT clear STREAMRIP_API_URL) so a dev's .env can't
make _resolve_artist hit the network.

Run with:  .venv-test/bin/pytest tests/test_follows.py -v
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from urllib.parse import quote

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, FollowedArtist, MonitoredArtist
from app.services.discovery import _normalize_artist

# In-memory SQLite (StaticPool → one shared connection, so direct-query
# sessions below see what the app committed over HTTP).
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
def _disable_upstreams(monkeypatch):
    """Pin every resolution upstream OFF so the default path is 'not configured'.

    conftest clears LIDARR/LASTFM/SPOTIZERR env but not STREAMRIP_API_URL; force
    all three empty here regardless of the dev .env. Tests that exercise Lidarr
    wiring re-enable it locally after this fixture runs (later monkeypatch wins).
    """
    monkeypatch.setattr(settings, "LIDARR_URL", "", raising=False)
    monkeypatch.setattr(settings, "LIDARR_API_KEY", "", raising=False)
    monkeypatch.setattr(settings, "STREAMRIP_API_URL", "", raising=False)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Keep this module hermetic w.r.t. the shared rate limiter.

    ``require_api_key`` rate-limits per API-key hash, and the whole suite drives
    one key inside the 60s sliding window, so every request accumulates in a
    single in-process bucket (RATE_LIMIT_DEFAULT = 200/min). Clearing it around
    each test means this file neither inherits a near-full bucket nor leaks its
    own requests into later modules (which would otherwise trip spurious 429s).
    """
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


# ── DB query helpers ─────────────────────────────────────────────────────────


async def _followed_rows(user_id: str | None = None) -> list[FollowedArtist]:
    async with _TestSession() as s:
        q = select(FollowedArtist)
        if user_id is not None:
            q = q.where(FollowedArtist.user_id == user_id)
        return list((await s.execute(q)).scalars().all())


async def _monitored_rows() -> list[MonitoredArtist]:
    async with _TestSession() as s:
        return list((await s.execute(select(MonitoredArtist))).scalars().all())


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_follow_persists_without_lidarr(client: AsyncClient):
    resp = await client.post("/v1/users/alice/follows", json={"artist_name": "Boards of Canada"})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["follow"]["id"], int) and body["follow"]["id"] >= 1
    assert body["artist"]["resolved"] is False
    assert body["follow"]["artist_mbid"] is None

    followed = await _followed_rows("alice")
    assert len(followed) == 1
    assert followed[0].artist_name_norm == "boards of canada"
    assert followed[0].unfollowed_at is None

    monitored = await _monitored_rows()
    assert len(monitored) == 1
    assert monitored[0].active_follower_count == 1
    assert monitored[0].lidarr_monitored is False


async def test_follow_with_supplied_mbid(client: AsyncClient):
    mbid = "11111111-1111-1111-1111-111111111111"
    resp = await client.post(
        "/v1/users/alice/follows", json={"artist_name": "Autechre", "artist_mbid": mbid}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["artist"]["resolved"] is True
    assert body["artist"]["artist_mbid"] == mbid
    assert body["follow"]["artist_mbid"] == mbid

    followed = await _followed_rows("alice")
    assert followed[0].artist_mbid == mbid
    monitored = await _monitored_rows()
    assert monitored[0].artist_mbid == mbid


async def test_refollow_is_idempotent(client: AsyncClient):
    name = "Portishead"
    key = _normalize_artist(name)  # "portishead"
    assert (await client.post("/v1/users/alice/follows", json={"artist_name": name})).status_code == 200
    assert (await client.delete(f"/v1/users/alice/follows/{key}")).status_code == 200
    assert (await client.post("/v1/users/alice/follows", json={"artist_name": name})).status_code == 200

    followed = await _followed_rows("alice")
    assert len(followed) == 1  # re-follow reuses the soft-deleted edge
    assert followed[0].unfollowed_at is None
    monitored = await _monitored_rows()
    assert monitored[0].active_follower_count == 1  # not 2


async def test_double_follow_no_double_count(client: AsyncClient):
    name = "Aphex Twin"
    await client.post("/v1/users/alice/follows", json={"artist_name": name})
    await client.post("/v1/users/alice/follows", json={"artist_name": name})

    assert len(await _followed_rows("alice")) == 1
    monitored = await _monitored_rows()
    assert monitored[0].active_follower_count == 1


async def test_two_users_one_artist_dedup(client: AsyncClient):
    name = "Radiohead"
    await client.post("/v1/users/alice/follows", json={"artist_name": name})
    await client.post("/v1/users/bob/follows", json={"artist_name": name})

    assert len(await _followed_rows()) == 2  # two edges
    monitored = await _monitored_rows()
    assert len(monitored) == 1  # one global watch row
    assert monitored[0].active_follower_count == 2


async def test_unfollow_soft_deletes_and_decrements(client: AsyncClient):
    name = "Radiohead"
    key = _normalize_artist(name)  # "radiohead"
    await client.post("/v1/users/alice/follows", json={"artist_name": name})
    await client.post("/v1/users/bob/follows", json={"artist_name": name})

    assert (await client.delete(f"/v1/users/alice/follows/{key}")).status_code == 200

    alice = await _followed_rows("alice")
    assert alice[0].unfollowed_at is not None
    bob = await _followed_rows("bob")
    assert bob[0].unfollowed_at is None  # untouched
    monitored = await _monitored_rows()
    assert monitored[0].active_follower_count == 1


async def test_unfollow_by_name_key(client: AsyncClient):
    # A name-fallback key can contain spaces/unicode; the client percent-encodes
    # it into the path segment (open question #4). This confirms the route
    # decodes it back to the normalized name and matches the edge.
    name = "Boards of Canada"
    key = _normalize_artist(name)  # "boards of canada"
    await client.post("/v1/users/alice/follows", json={"artist_name": name})

    resp = await client.delete(f"/v1/users/alice/follows/{quote(key, safe='')}")
    assert resp.status_code == 200
    alice = await _followed_rows("alice")
    assert alice[0].unfollowed_at is not None


async def test_list_returns_active_only(client: AsyncClient):
    await client.post("/v1/users/alice/follows", json={"artist_name": "Radiohead"})
    await client.post("/v1/users/alice/follows", json={"artist_name": "Portishead"})
    await client.delete(f"/v1/users/alice/follows/{_normalize_artist('Radiohead')}")

    resp = await client.get("/v1/users/alice/follows")
    assert resp.status_code == 200
    follows = resp.json()["follows"]
    assert len(follows) == 1  # only the active one
    assert follows[0]["artist_name"] == "Portishead"


async def test_malformed_user_id_400(client: AsyncClient):
    # "bad!id" fails the (test-relaxed) ^[A-Za-z0-9_]+$ pattern → 400 before DB work.
    resp = await client.post("/v1/users/bad!id/follows", json={"artist_name": "Radiohead"})
    assert resp.status_code == 400


async def test_lidarr_wiring_called_when_configured(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "LIDARR_URL", "http://lidarr.test", raising=False)
    monkeypatch.setattr(settings, "LIDARR_API_KEY", "k", raising=False)

    add_calls: list[tuple[str, str]] = []

    class FakeLidarr:
        def __init__(self, url, key):
            pass

        async def lookup_artist(self, *, name=None, mbid=None):
            return {"foreignArtistId": "mbid-boc", "images": [{"remoteUrl": "http://img/boc.jpg"}]}

        async def get_existing_artist_mbids(self):
            return set()  # not yet in Lidarr → add_artist is called

        async def add_artist(self, foreign_artist_id, artist_name):
            add_calls.append((foreign_artist_id, artist_name))
            return {"id": 42}

        async def close(self):
            pass

    monkeypatch.setattr("app.services.follow_service.LidarrClient", FakeLidarr)

    resp = await client.post("/v1/users/alice/follows", json={"artist_name": "Boards of Canada"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["artist"]["resolved"] is True
    assert body["artist"]["artist_mbid"] == "mbid-boc"

    monitored = await _monitored_rows()
    assert len(monitored) == 1
    assert monitored[0].lidarr_monitored is True
    assert monitored[0].lidarr_artist_id == 42
    assert add_calls == [("mbid-boc", "Boards of Canada")]  # called exactly once

    followed = await _followed_rows("alice")
    assert followed[0].image_url == "http://img/boc.jpg"  # adopted from Lidarr


async def test_lidarr_failure_does_not_break_follow(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "LIDARR_URL", "http://lidarr.test", raising=False)
    monkeypatch.setattr(settings, "LIDARR_API_KEY", "k", raising=False)

    class FakeLidarr:
        def __init__(self, url, key):
            pass

        async def lookup_artist(self, *, name=None, mbid=None):
            return {"foreignArtistId": "mbid-x"}

        async def get_existing_artist_mbids(self):
            return set()

        async def add_artist(self, foreign_artist_id, artist_name):
            raise RuntimeError("lidarr exploded")

        async def close(self):
            pass

    monkeypatch.setattr("app.services.follow_service.LidarrClient", FakeLidarr)

    resp = await client.post("/v1/users/alice/follows", json={"artist_name": "Some Artist"})
    assert resp.status_code == 200  # follow must NOT 5xx when Lidarr wiring fails

    followed = await _followed_rows("alice")
    assert len(followed) == 1
    assert followed[0].artist_mbid == "mbid-x"  # resolution still succeeded
    monitored = await _monitored_rows()
    assert monitored[0].lidarr_monitored is False  # wiring failed, stays False for P1 backstop


async def test_unfollow_all(client: AsyncClient):
    await client.post("/v1/users/alice/follows", json={"artist_name": "Radiohead"})
    await client.post("/v1/users/alice/follows", json={"artist_name": "Portishead"})
    await client.post("/v1/users/bob/follows", json={"artist_name": "Radiohead"})

    resp = await client.delete("/v1/users/alice/follows")  # no artist_key → bulk
    assert resp.status_code == 200
    assert resp.json()["unfollowed"] == 2  # alice had two active follows

    alice = await _followed_rows("alice")
    assert alice and all(e.unfollowed_at is not None for e in alice)  # all soft-deleted
    bob = await _followed_rows("bob")
    assert bob[0].unfollowed_at is None  # other users untouched

    mons = {m.artist_name_norm: m for m in await _monitored_rows()}
    assert mons["radiohead"].active_follower_count == 1  # bob still follows
    assert mons["portishead"].active_follower_count == 0

    assert (await client.get("/v1/users/alice/follows")).json()["follows"] == []
    # idempotent: a second unfollow-all is still 200 and changes nothing
    resp2 = await client.delete("/v1/users/alice/follows")
    assert resp2.status_code == 200
    assert resp2.json()["unfollowed"] == 0
