"""
Tests for chart-track downloads via the multi-backend cascade (issue #64).

Covers:
  * build_fast_lane_chain reorders spotdl-first / streamrip-last
  * charts_autodownload_enabled honours the legacy Spotizerr flag
  * POST /charts/download works with NO Spotizerr configured (cascade, not the
    old Spotizerr-only 503) and persists a DownloadRequest
  * POST /charts/download 503s only when *no* backend is configured
  * GET /charts/{type} surfaces per-track download_status from DownloadRequest
  * _send_tracks_via_cascade skips tracks already downloaded / in flight
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, ChartEntry, DownloadRequest

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


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


def _fake_cascade(success=True, backend="spotdl", task_id="t-1", status="queued"):
    from app.services.download_chain import CascadeResult

    return CascadeResult(
        success=success,
        attempts=[],
        final_backend=backend if success else None,
        final_task_id=task_id if success else None,
        final_status=status if success else "error",
        final_extra={},
    )


async def _seed_chart(rows: list[dict]) -> None:
    async with _TestSession() as s:
        for i, r in enumerate(rows):
            s.add(
                ChartEntry(
                    chart_type=r.get("chart_type", "top_tracks"),
                    scope=r.get("scope", "global"),
                    position=r["position"],
                    snapshot_date=r["snapshot_date"],
                    artist_name=r.get("artist", f"Artist{i}"),
                    track_title=r.get("title"),
                    in_library=r.get("in_library", False),
                    matched_track_id=r.get("matched_track_id"),
                    fetched_at=int(time.time()),
                )
            )
        await s.commit()


# ---------------------------------------------------------------------------
# build_fast_lane_chain ordering (pure, no DB)
# ---------------------------------------------------------------------------


async def test_fast_lane_puts_spotdl_first_streamrip_last(monkeypatch):
    from app.models.download_routing_schema import BackendChainEntry, BackendName
    from app.services import download_chain

    configured = [
        BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
        BackendChainEntry(backend=BackendName.SPOTDL, enabled=True),
        BackendChainEntry(backend=BackendName.SPOTIZERR, enabled=True),
        BackendChainEntry(backend=BackendName.SLSKD, enabled=False),
    ]
    monkeypatch.setattr(download_chain, "get_chain", lambda purpose="individual": list(configured))

    order = [e.backend for e in download_chain.build_fast_lane_chain()]
    assert order[0] == BackendName.SPOTDL
    assert order[-1] == BackendName.STREAMRIP
    # middle keeps configured order; entry attributes preserved
    assert order == [
        BackendName.SPOTDL,
        BackendName.SPOTIZERR,
        BackendName.SLSKD,
        BackendName.STREAMRIP,
    ]


async def test_autodownload_enabled_honours_legacy_flag(monkeypatch):
    monkeypatch.setattr(settings, "CHARTS_AUTODOWNLOAD_ENABLED", False)
    monkeypatch.setattr(settings, "CHARTS_SPOTIZERR_AUTO_ADD", False)
    assert settings.charts_autodownload_enabled is False
    monkeypatch.setattr(settings, "CHARTS_SPOTIZERR_AUTO_ADD", True)  # legacy switch
    assert settings.charts_autodownload_enabled is True


# ---------------------------------------------------------------------------
# POST /charts/download via cascade
# ---------------------------------------------------------------------------


async def test_download_uses_cascade_without_spotizerr(client, monkeypatch):
    await _seed_chart([{"position": 0, "snapshot_date": "2024-01-01", "artist": "Dua Lipa", "title": "Levitating"}])
    # No Spotizerr — the old code 503'd here. spotdl makes download_enabled True.
    monkeypatch.setattr(settings, "SPOTIZERR_URL", "")
    monkeypatch.setattr(settings, "SPOTDL_API_URL", "http://test-spotdl")

    from app.services import download_chain, download_dispatch

    captured = {}

    async def fake_chain(track_ref, purpose="individual", chain_override=None):
        captured["title"] = track_ref.title
        captured["override"] = chain_override
        return _fake_cascade()

    async def fake_spawn(record, cascade):
        captured["spawned"] = True

    monkeypatch.setattr(download_chain, "build_fast_lane_chain", lambda purpose="individual": ["FASTLANE"])
    monkeypatch.setattr(download_chain, "try_download_chain", fake_chain)
    monkeypatch.setattr(download_dispatch, "spawn_watcher", fake_spawn)

    resp = await client.post("/v1/charts/download", json={"chart_type": "top_tracks", "scope": "global", "position": 0})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert data["task_id"] == "t-1"
    assert data["source"] == "spotdl"
    assert captured["title"] == "Levitating"
    assert captured["override"] == ["FASTLANE"]  # used the fast lane, not the raw chain
    assert captured.get("spawned") is True

    async with _TestSession() as s:
        rows = (await s.execute(select(DownloadRequest))).scalars().all()
    assert len(rows) == 1
    assert rows[0].track_title == "Levitating"
    assert rows[0].source == "spotdl"
    assert rows[0].requested_by is not None  # user-initiated -> hashed key stored


async def test_download_503_when_no_backend(client, monkeypatch):
    await _seed_chart([{"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "B"}])
    for attr in ("SPOTDL_API_URL", "STREAMRIP_API_URL", "SPOTIZERR_URL", "SLSKD_URL"):
        monkeypatch.setattr(settings, attr, "")
    monkeypatch.setattr(settings, "SLSKD_ENABLED", False)

    resp = await client.post("/v1/charts/download", json={"chart_type": "top_tracks", "scope": "global", "position": 0})
    assert resp.status_code == 503, resp.text


async def test_download_502_when_cascade_fails(client, monkeypatch):
    await _seed_chart([{"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "B"}])
    monkeypatch.setattr(settings, "SPOTDL_API_URL", "http://test-spotdl")

    from app.services import download_chain

    async def fake_chain(track_ref, purpose="individual", chain_override=None):
        return _fake_cascade(success=False)

    monkeypatch.setattr(download_chain, "build_fast_lane_chain", lambda purpose="individual": [])
    monkeypatch.setattr(download_chain, "try_download_chain", fake_chain)

    resp = await client.post("/v1/charts/download", json={"chart_type": "top_tracks", "scope": "global", "position": 0})
    assert resp.status_code == 502, resp.text


# ---------------------------------------------------------------------------
# Per-track download_status badge on GET /charts
# ---------------------------------------------------------------------------


async def test_get_chart_surfaces_download_status(client):
    await _seed_chart([{"position": 0, "snapshot_date": "2024-01-01", "artist": "Charli xcx", "title": "Von Dutch"}])
    async with _TestSession() as s:
        s.add(
            DownloadRequest(
                status="downloading",
                source="spotdl",
                track_title="Von Dutch",
                artist_name="Charli xcx",
                requested_by="__charts__",
                updated_at=int(time.time()),
            )
        )
        await s.commit()

    resp = await client.get("/v1/charts/top_tracks?scope=global")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["entries"][0]
    assert entry["in_library"] is False
    assert entry["download_status"] == "downloading"


# ---------------------------------------------------------------------------
# Auto-download history dedup
# ---------------------------------------------------------------------------


async def test_send_tracks_via_cascade_skips_already_downloaded(monkeypatch):
    from app.services import charts as charts_svc
    from app.services import download_chain, download_dispatch

    monkeypatch.setattr(settings, "SPOTDL_API_URL", "http://test-spotdl")
    # Point the service's own session factory at the test DB.
    monkeypatch.setattr(charts_svc, "AsyncSessionLocal", _TestSession)

    async with _TestSession() as s:
        s.add(
            DownloadRequest(
                status="completed",
                source="spotdl",
                artist_name="Taylor Swift",
                track_title="Fortnight",
                updated_at=int(time.time()),
            )
        )
        await s.commit()

    calls = []

    async def fake_chain(track_ref, purpose="individual", chain_override=None):
        calls.append((track_ref.artist, track_ref.title))
        return _fake_cascade()

    async def fake_spawn(record, cascade):
        return None

    monkeypatch.setattr(download_chain, "build_fast_lane_chain", lambda purpose="individual": [])
    monkeypatch.setattr(download_chain, "try_download_chain", fake_chain)
    monkeypatch.setattr(download_dispatch, "spawn_watcher", fake_spawn)

    stats = await charts_svc._send_tracks_via_cascade(
        [("Taylor Swift", "Fortnight"), ("Sabrina Carpenter", "Espresso")],
        max_adds=10,
    )
    # Already-downloaded track skipped; only the fresh one hit the cascade.
    assert calls == [("Sabrina Carpenter", "Espresso")]
    assert stats["already_have"] == 1
    assert stats["sent"] == 1


# ---------------------------------------------------------------------------
# Stable (cacheable) media-server cover auth
# ---------------------------------------------------------------------------


async def test_media_server_auth_is_stable_and_cacheable(monkeypatch):
    # A stable salt/token keeps the Navidrome cover URL constant across requests
    # so the browser's long-lived cache hits (was: random salt per request →
    # cache-busted every load → below-the-fold thumbnails flaked).
    from app.api.routes.charts import _media_server_auth_params

    monkeypatch.setattr(settings, "MEDIA_SERVER_TYPE", "navidrome")
    monkeypatch.setattr(settings, "MEDIA_SERVER_URL", "https://navidrome.example.com")
    monkeypatch.setattr(settings, "MEDIA_SERVER_USER", "alice")
    monkeypatch.setattr(settings, "MEDIA_SERVER_PASSWORD", "s3cret")
    monkeypatch.setattr(settings, "SECRET_KEY", "test-secret")

    a = _media_server_auth_params()
    b = _media_server_auth_params()
    assert a is not None
    assert a == b  # deterministic → stable, cacheable cover URLs
    assert a.startswith("u=alice&t=")
    assert "&s=" in a and a.endswith("&v=1.16.1&c=grooveiq")

    # Salt/token are keyed to SECRET_KEY (opaque + per-deployment).
    monkeypatch.setattr(settings, "SECRET_KEY", "different-secret")
    assert _media_server_auth_params() != a
