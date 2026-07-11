"""
Tests for the charts filtering additions:

  * live country/genre picker scope parsing (_resolve_track_fetch / _resolve_artist_fetch)
  * album charts: _build_album_chart matches the library and GET returns album_name
  * cross-snapshot identity keys account for the album title
  * POST /charts/fetch input validation (albums require a tag scope; bad type rejected)

The live Last.fm round-trip in build_single_chart is exercised end-to-end
manually (needs a real API key); here we cover only the offline logic.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes.charts import _identity_key
from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, ChartEntry, TrackFeatures
from app.services.charts import (
    _build_album_chart,
    _ChartClient,
    _resolve_artist_fetch,
    _resolve_track_fetch,
    _snapshot_date,
)

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


# ---------------------------------------------------------------------------
# Scope parsing for the live picker
# ---------------------------------------------------------------------------


async def test_resolve_track_fetch_scope_parsing():
    ch = _ChartClient("fake-key")
    try:
        # global returns the bound global method; tag/geo return a callable.
        assert _resolve_track_fetch(ch, "global") == ch.get_top_tracks
        assert callable(_resolve_track_fetch(ch, "tag:jazz"))
        assert callable(_resolve_track_fetch(ch, "geo:japan"))
        # unknown scope shape -> None (caller returns a bad_scope error)
        assert _resolve_track_fetch(ch, "bogus") is None
    finally:
        await ch.close()


async def test_resolve_artist_fetch_scope_parsing():
    ch = _ChartClient("fake-key")
    try:
        assert _resolve_artist_fetch(ch, "global") == ch.get_top_artists
        assert callable(_resolve_artist_fetch(ch, "tag:metal"))
        assert callable(_resolve_artist_fetch(ch, "geo:germany"))
        assert _resolve_artist_fetch(ch, "") is None
    finally:
        await ch.close()


# ---------------------------------------------------------------------------
# Cross-snapshot identity keys
# ---------------------------------------------------------------------------


def test_identity_key_distinguishes_types():
    # albums key on (artist, album); tracks on (artist, title); artists on (artist, "")
    assert _identity_key("top_albums", "Nina Simone", None, "I Put a Spell on You") == (
        "nina simone",
        "i put a spell on you",
    )
    assert _identity_key("top_tracks", "Nina Simone", "Feeling Good", "ignored") == ("nina simone", "feeling good")
    assert _identity_key("top_artists", "Nina Simone", None, None) == ("nina simone", "")


# ---------------------------------------------------------------------------
# Album chart builder + library matching
# ---------------------------------------------------------------------------


def _album(name: str, artist: str, mbid: str = "") -> dict:
    """A Last.fm tag.getTopAlbums-shaped album dict."""
    return {"name": name, "artist": {"name": artist, "mbid": mbid}, "playcount": "100", "image": []}


async def _run_album_build(albums: list[dict], album_lookup: dict, now: int, scope: str = "tag:jazz") -> dict:
    async def fetch_fn(limit: int = 100, page: int = 1):
        return albums[:limit]

    summary: dict = defaultdict(int)
    async with _TestSession() as session:
        await _build_album_chart(
            None,  # client unused (no cover client)
            session,
            album_lookup,
            chart_type="top_albums",
            scope=scope,
            fetch_fn=fetch_fn,
            limit=100,
            now=now,
            summary=summary,
            cover_client=None,
        )
        await session.commit()
    return summary


async def test_build_album_chart_matches_library():
    now = 1_700_000_000
    # Library owns two tracks off "Blue" by "Joni Mitchell".
    album_lookup = {("joni mitchell", "blue"): ["t1", "t2"]}
    albums = [_album("Blue", "Joni Mitchell"), _album("Kind of Blue", "Miles Davis")]
    summary = await _run_album_build(albums, album_lookup, now)

    async with _TestSession() as s:
        rows = (
            (
                await s.execute(
                    select(ChartEntry).where(ChartEntry.chart_type == "top_albums").order_by(ChartEntry.position)
                )
            )
            .scalars()
            .all()
        )
    assert [r.album_name for r in rows] == ["Blue", "Kind of Blue"]
    assert rows[0].in_library is True
    assert rows[0].library_track_count == 2
    assert rows[0].matched_track_id == "t1"
    assert rows[1].in_library is False
    assert summary["library_matches"] == 1
    assert rows[0].snapshot_date == _snapshot_date(now)


async def test_build_album_chart_same_day_idempotent():
    now = 1_700_000_000
    albums = [_album("Blue", "Joni Mitchell"), _album("Court and Spark", "Joni Mitchell")]
    await _run_album_build(albums, {}, now)
    await _run_album_build(albums, {}, now + 60)  # same UTC day
    async with _TestSession() as s:
        n = (
            await s.execute(select(func.count()).select_from(ChartEntry).where(ChartEntry.chart_type == "top_albums"))
        ).scalar()
    assert n == 2  # replaced, not duplicated


# ---------------------------------------------------------------------------
# GET /charts/top_albums returns album_name + library re-match
# ---------------------------------------------------------------------------


async def _seed_album_rows(rows: list[dict]) -> None:
    async with _TestSession() as s:
        for r in rows:
            s.add(
                ChartEntry(
                    chart_type="top_albums",
                    scope=r.get("scope", "tag:jazz"),
                    position=r["position"],
                    snapshot_date=r["snapshot_date"],
                    artist_name=r["artist"],
                    album_name=r["album"],
                    playcount=r.get("playcount", 0),
                    listeners=0,
                    in_library=False,
                    fetched_at=int(time.time()),
                )
            )
        await s.commit()


async def test_get_chart_albums_returns_album_name_and_matches(client):
    await _seed_album_rows(
        [
            {"position": 0, "snapshot_date": "2024-01-02", "artist": "Joni Mitchell", "album": "Blue"},
            {"position": 1, "snapshot_date": "2024-01-02", "artist": "Miles Davis", "album": "Kind of Blue"},
        ]
    )
    # A library track off "Blue" so the serve-time re-match promotes it.
    async with _TestSession() as s:
        s.add(
            TrackFeatures(
                track_id="lib1",
                title="A Case of You",
                artist="Joni Mitchell",
                album="Blue",
                file_path="/music/joni/blue/a_case_of_you.flac",
                file_hash="hash-lib1",
                analysis_version="test",
            )
        )
        await s.commit()

    resp = await client.get("/v1/charts/top_albums?scope=tag:jazz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["chart_type"] == "top_albums"
    assert data["total"] == 2
    first = data["entries"][0]
    assert first["album_name"] == "Blue"
    assert first["in_library"] is True  # promoted by live re-match
    assert first["library_track_count"] == 1


# ---------------------------------------------------------------------------
# POST /charts/fetch validation (offline — before any Last.fm call)
# ---------------------------------------------------------------------------


async def test_fetch_albums_require_tag_scope(client):
    r = await client.post("/v1/charts/fetch", json={"chart_type": "top_albums", "scope": "global"})
    assert r.status_code == 400
    assert "genre" in r.json()["detail"].lower()


async def test_fetch_rejects_bad_chart_type(client):
    r = await client.post("/v1/charts/fetch", json={"chart_type": "top_bogus", "scope": "global"})
    assert r.status_code == 400
