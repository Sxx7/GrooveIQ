"""GrooveIQ – Tests for followed-artist release detection + reconciler + feed (P1).

Uses an in-memory SQLite engine (StaticPool → one shared connection). The
release_scan service opens its own AsyncSessionLocal, so it is monkeypatched to
the test session factory; the streamrip client + download cascade are faked so
nothing hits the network.

Run:  .venv-test/bin/pytest tests/test_release_scan.py -v
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import (
    Base,
    FollowedArtist,
    MonitoredArtist,
    ReleaseEvent,
    TrackFeatures,
    UserReleaseNotification,
)
from app.services import release_scan

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)

# Per-test injectables (reset by the autouse fixture).
_PAYLOADS: dict[str, dict] = {}
_CASCADE = {"ok": True}


async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


class _FakeStreamrip:
    def __init__(self, base_url: str):
        pass

    async def search_artist(self, query: str, limit: int = 2, albums_per_artist: int = 50) -> dict:
        return _PAYLOADS.get(query, {"query": query, "artists": []})

    async def close(self) -> None:
        pass


async def _fake_cascade(album_ref):
    from app.services.download_chain import AlbumCascadeResult

    ok = _CASCADE["ok"]
    return AlbumCascadeResult(success=ok, final_extra={"task_id": "task-1"} if ok else {})


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.dependency_overrides[get_session] = override_get_session

    # release_scan opens its own AsyncSessionLocal — point it at the test engine.
    monkeypatch.setattr(release_scan, "AsyncSessionLocal", _TestSession)
    monkeypatch.setattr("app.services.streamrip.StreamripClient", _FakeStreamrip)
    monkeypatch.setattr("app.services.download_chain.try_album_download_chain", _fake_cascade)

    # Enable the feature + a (fake) streamrip backend; pin the tunables.
    monkeypatch.setattr(settings, "FOLLOW_SCAN_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "STREAMRIP_API_URL", "http://streamrip.test", raising=False)
    monkeypatch.setattr(settings, "FOLLOW_SCAN_POLL_INTERVAL_HOURS", 6, raising=False)
    monkeypatch.setattr(settings, "NEW_RELEASE_WINDOW_DAYS", 60, raising=False)
    monkeypatch.setattr(settings, "FOLLOW_GRACE_DAYS", 30, raising=False)
    monkeypatch.setattr(settings, "FOLLOW_MAX_ELIGIBLE_PER_RUN", 5, raising=False)
    monkeypatch.setattr(settings, "FOLLOW_AVAILABILITY_MIN_FRACTION", 0.0, raising=False)

    _PAYLOADS.clear()
    _CASCADE["ok"] = True
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.core import security

    def _clear() -> None:
        w = getattr(security._limiter, "_windows", None)
        if w is not None:
            w.clear()

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


# ── helpers ──────────────────────────────────────────────────────────────────


def _recent(days_ago: int = 10) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _album(album_id: str, title: str, release_date: str, track_count: int = 10) -> dict:
    return {
        "album_id": album_id,
        "title": title,
        "release_date": release_date,
        "year": int(release_date[:4]) if release_date[:4].isdigit() else None,
        "track_count": track_count,
        "cover_url": f"http://cover/{album_id}.jpg",
    }


def _payload(name: str, albums: list[dict], service: str = "qobuz") -> None:
    _PAYLOADS[name] = {
        "query": name,
        "service": service,
        "artists": [
            {
                "artist_id": "a1",
                "name": name,
                "image_url": "http://img",
                "service": service,
                "albums_total": len(albums),
                "albums": albums,
                "other_releases": [],
            }
        ],
    }


async def _seed_monitored(name: str, mbid: str | None = None) -> None:
    async with _TestSession() as s:
        s.add(
            MonitoredArtist(
                artist_mbid=mbid, artist_name_norm=name.lower(), artist_name=name, created_at=int(time.time())
            )
        )
        await s.commit()


async def _seed_follow(user_id: str, name: str, mbid: str | None = None, followed_days_ago: int = 5) -> None:
    async with _TestSession() as s:
        s.add(
            FollowedArtist(
                user_id=user_id,
                artist_mbid=mbid,
                artist_name=name,
                artist_name_norm=name.lower(),
                source="user",
                followed_at=int(time.time()) - followed_days_ago * 86400,
                unfollowed_at=None,
            )
        )
        await s.commit()


async def _seed_track(artist: str, album: str, media_server_id: str | None) -> None:
    async with _TestSession() as s:
        # track_id must be unique; derive from artist/album/msid
        tid = f"{artist}:{album}:{media_server_id}".lower()
        s.add(
            TrackFeatures(
                track_id=tid, file_path=f"/m/{tid}.mp3", artist=artist, album=album, media_server_id=media_server_id
            )
        )
        await s.commit()


async def _rows(model):
    async with _TestSession() as s:
        return list((await s.execute(select(model))).scalars().all())


# ── detection ────────────────────────────────────────────────────────────────


async def test_detection_insert_and_dedup():
    await _seed_monitored("Boards of Canada")
    _payload("Boards of Canada", [_album("q1", "Trans Canada Highway", _recent(5), track_count=4)])

    r1 = await release_scan.run_follow_scan()
    assert r1["detected"] == 1 and r1["acquired"] == 1
    evs = await _rows(ReleaseEvent)
    assert len(evs) == 1
    assert evs[0].acquisition_state == "downloading"  # cascade succeeded
    assert evs[0].kind == "ep"  # 4 tracks
    assert evs[0].last_acq_task_id == "task-1"

    # Re-run: watermark now covers it AND release_key dedups → no new row.
    r2 = await release_scan.run_follow_scan()
    assert r2["detected"] == 0
    assert len(await _rows(ReleaseEvent)) == 1


async def test_candidate_window_filter():
    await _seed_monitored("Old Artist")
    _payload(
        "Old Artist",
        [
            _album("old1", "Ancient LP", "2015-07-17"),  # far outside the 60-day window
            _album("new1", "Fresh EP", _recent(3), track_count=3),
        ],
    )
    res = await release_scan.run_follow_scan()
    assert res["detected"] == 1  # only the fresh one
    evs = await _rows(ReleaseEvent)
    assert [e.album_title for e in evs] == ["Fresh EP"]


async def test_watermark_advances():
    await _seed_monitored("WM Artist")
    _payload("WM Artist", [_album("w1", "Rel", _recent(4))])
    await release_scan.run_follow_scan()
    mon = (await _rows(MonitoredArtist))[0]
    assert mon.last_poll_at is not None
    assert mon.last_seen_release_date and mon.last_seen_release_date > 0


async def test_cascade_failure_leaves_pending():
    await _seed_monitored("NoDL Artist")
    _payload("NoDL Artist", [_album("n1", "Pending LP", _recent(2))])
    _CASCADE["ok"] = False
    res = await release_scan.run_follow_scan()
    assert res["detected"] == 1 and res["acquired"] == 0
    assert (await _rows(ReleaseEvent))[0].acquisition_state == "pending"


# ── reconciler ───────────────────────────────────────────────────────────────


async def test_reconciler_availability_transition_and_fanout():
    await _seed_monitored("Radiohead")
    await _seed_follow("alice", "Radiohead")
    await _seed_follow("bob", "Radiohead")
    _payload("Radiohead", [_album("r1", "New Album", _recent(3))])
    await release_scan.run_follow_scan()

    # Not available yet (no streamable track) → reconcile is a no-op.
    rec0 = await release_scan.reconcile_available_releases()
    assert rec0["reconciled"] == 0
    ev = (await _rows(ReleaseEvent))[0]
    assert ev.available_at is None

    # A matching track with NO media_server_id must NOT trigger availability.
    await _seed_track("Radiohead", "New Album", media_server_id=None)
    assert (await release_scan.reconcile_available_releases())["reconciled"] == 0
    assert (await _rows(ReleaseEvent))[0].available_at is None

    # Now a streamable track lands → available + fan-out to both followers.
    await _seed_track("Radiohead", "New Album", media_server_id="nav-100")
    rec = await release_scan.reconcile_available_releases()
    assert rec["reconciled"] == 1
    assert rec["notifications_created"] == 2
    ev = (await _rows(ReleaseEvent))[0]
    assert ev.available_at is not None and ev.acquisition_state == "imported"
    assert ev.track_count_available == 1

    # Idempotent: re-run creates no duplicate notifications, no re-transition.
    rec2 = await release_scan.reconcile_available_releases()
    assert rec2["reconciled"] == 0 and rec2["notifications_created"] == 0
    assert len(await _rows(UserReleaseNotification)) == 2


async def test_eligibility_guard_back_catalog():
    # Follower who followed AFTER a within-window-but-old-ish release.
    await _seed_monitored("Guard Artist")
    # released 55 days ago (inside the 60d window) but the user followed 5 days
    # ago → 55d-old release predates (followed_at - 30d grace) → NOT eligible.
    await _seed_follow("carol", "Guard Artist", followed_days_ago=5)
    _payload("Guard Artist", [_album("g1", "Older Release", _recent(55))])
    await release_scan.run_follow_scan()
    await _seed_track("Guard Artist", "Older Release", media_server_id="nav-200")
    await release_scan.reconcile_available_releases()

    urns = await _rows(UserReleaseNotification)
    assert len(urns) == 1
    assert urns[0].eligible is False  # back-catalog guard suppressed it


async def test_eligibility_guard_fresh_release():
    await _seed_monitored("Fresh Artist")
    await _seed_follow("dave", "Fresh Artist", followed_days_ago=20)
    _payload("Fresh Artist", [_album("f1", "Brand New", _recent(2))])  # released after the follow
    await release_scan.run_follow_scan()
    await _seed_track("Fresh Artist", "Brand New", media_server_id="nav-300")
    await release_scan.reconcile_available_releases()

    urns = await _rows(UserReleaseNotification)
    assert len(urns) == 1 and urns[0].eligible is True


async def test_per_run_cap(monkeypatch):
    # cap = 2 eligible/user/run; give the user 3 fresh eligible releases.
    monkeypatch.setattr(settings, "FOLLOW_MAX_ELIGIBLE_PER_RUN", 2, raising=False)
    await _seed_monitored("Prolific")
    await _seed_follow("erin", "Prolific", followed_days_ago=90)
    _payload(
        "Prolific",
        [
            _album("p1", "One", _recent(3)),
            _album("p2", "Two", _recent(4)),
            _album("p3", "Three", _recent(5)),
        ],
    )
    await release_scan.run_follow_scan()
    for alb in ("One", "Two", "Three"):
        await _seed_track("Prolific", alb, media_server_id=f"nav-{alb}")
    await release_scan.reconcile_available_releases()

    urns = await _rows(UserReleaseNotification)
    assert len(urns) == 3
    assert sum(1 for u in urns if u.eligible) == 2  # capped at 2 eligible
    assert sum(1 for u in urns if not u.eligible) == 1


# ── feed + admin endpoints ───────────────────────────────────────────────────


async def _make_available_feed(user_id: str = "frank") -> None:
    await _seed_monitored("FeedArtist")
    await _seed_follow(user_id, "FeedArtist", followed_days_ago=10)
    _payload("FeedArtist", [_album("fe1", "Feed Album", _recent(2))])
    await release_scan.run_follow_scan()
    await _seed_track("FeedArtist", "Feed Album", media_server_id="nav-feed")
    await release_scan.reconcile_available_releases()


async def test_feed_endpoint(client: AsyncClient):
    await _make_available_feed("frank")
    resp = await client.get("/v1/users/frank/feed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unseen_count"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["artist_name"] == "FeedArtist" and item["album_title"] == "Feed Album"
    assert item["available_at"] is not None and item["seen_at"] is None


async def test_feed_seen_endpoint(client: AsyncClient):
    await _make_available_feed("grace")
    feed = (await client.get("/v1/users/grace/feed")).json()
    nid = feed["items"][0]["notification_id"]

    r = await client.post("/v1/users/grace/feed/seen", json={"notification_ids": [nid]})
    assert r.status_code == 200 and r.json()["updated"] == 1  # 200 + body, NOT 204 (iOS decoder)

    after = (await client.get("/v1/users/grace/feed")).json()
    assert after["unseen_count"] == 0
    assert after["items"][0]["seen_at"] is not None

    # mark-all is idempotent + also 200
    r2 = await client.post("/v1/users/grace/feed/seen", json={"all": True})
    assert r2.status_code == 200


async def test_admin_follow_scan(client: AsyncClient):
    await _seed_monitored("AdminArtist")
    _payload("AdminArtist", [_album("ad1", "Admin LP", _recent(3))])
    resp = await client.post("/v1/admin/follow-scan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["result"]["detected"] == 1


async def test_admin_follow_scan_disabled(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "FOLLOW_SCAN_ENABLED", False, raising=False)
    resp = await client.post("/v1/admin/follow-scan")
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
