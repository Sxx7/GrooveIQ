"""
Tests for daily chart snapshots + snapshot-aware API (issue #75).

Covers:
  * builder stamps snapshot_date and is idempotent within a day, while a new
    day appends rather than overwriting (DELETE-by-day -> INSERT)
  * the (chart_type, scope, position, snapshot_date) unique index holds
  * GET /charts/{type} serves the latest snapshot by default
  * ?as_of= returns the snapshot on-or-before a date (robust to gaps)
  * ?compare= adds position_change / previously, incl. NEW entries
  * /track/{artist}/{title}/history returns the full trajectory
  * /snapshots lists retained dates
  * chart_stats / list_charts aggregate the latest snapshot only
  * download position lookup is scoped to the latest snapshot (no
    MultipleResultsFound once a second day exists)
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, ChartEntry
from app.services.charts import _build_track_chart, _snapshot_date

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
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed(rows: list[dict]) -> None:
    """Insert ChartEntry rows. Each dict needs at least position + snapshot_date;
    chart_type/scope/artist/title default to a global top_tracks chart."""
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
                    playcount=r.get("playcount", 0),
                    listeners=r.get("listeners", 0),
                    in_library=False,
                    fetched_at=r.get("fetched_at", int(time.time())),
                )
            )
        await s.commit()


def _track(name: str, artist: str, mbid: str = "") -> dict:
    """A Last.fm-shaped chart track dict for the builder."""
    return {
        "name": name,
        "artist": {"name": artist, "mbid": mbid},
        "playcount": "100",
        "listeners": "50",
        "image": [],
    }


# ---------------------------------------------------------------------------
# Builder: snapshot stamping + idempotency
# ---------------------------------------------------------------------------


async def _run_track_build(tracks: list[dict], now: int, scope: str = "global") -> None:
    async def fetch_fn(limit: int = 100, page: int = 1):
        return tracks[:limit]

    from collections import defaultdict

    async with _TestSession() as session:
        await _build_track_chart(
            None,  # client unused by the track builder
            session,
            {},  # track_lookup
            {},  # artist_lookup
            [],  # lidarr_candidates
            [],  # spotizerr_candidates
            chart_type="top_tracks",
            scope=scope,
            fetch_fn=fetch_fn,
            limit=100,
            now=now,
            summary=defaultdict(int),
            cover_client=None,
        )
        await session.commit()


async def _count(chart_type="top_tracks", scope="global", snapshot_date=None) -> int:
    async with _TestSession() as s:
        q = (
            select(func.count())
            .select_from(ChartEntry)
            .where(ChartEntry.chart_type == chart_type, ChartEntry.scope == scope)
        )
        if snapshot_date is not None:
            q = q.where(ChartEntry.snapshot_date == snapshot_date)
        return (await s.execute(q)).scalar() or 0


async def test_builder_stamps_snapshot_date():
    now = 1_700_000_000  # 2023-11-14 UTC
    await _run_track_build([_track("A", "X"), _track("B", "Y")], now)
    expected = _snapshot_date(now)
    assert await _count(snapshot_date=expected) == 2
    # Every row carries the date.
    async with _TestSession() as s:
        dates = (await s.execute(select(ChartEntry.snapshot_date))).scalars().all()
    assert set(dates) == {expected}


async def test_builder_same_day_rebuild_is_idempotent():
    now = 1_700_000_000
    await _run_track_build([_track("A", "X"), _track("B", "Y"), _track("C", "Z")], now)
    # Rebuild the same day (even with a slightly later timestamp on the same UTC date).
    await _run_track_build([_track("A", "X"), _track("B", "Y"), _track("C", "Z")], now + 60)
    assert await _count() == 3  # replaced, not duplicated


async def test_builder_new_day_appends():
    day1 = 1_700_000_000  # 2023-11-14
    day2 = day1 + 86_400  # 2023-11-15
    await _run_track_build([_track("A", "X"), _track("B", "Y")], day1)
    await _run_track_build([_track("A", "X"), _track("B", "Y")], day2)
    assert await _count() == 4
    assert await _count(snapshot_date=_snapshot_date(day1)) == 2
    assert await _count(snapshot_date=_snapshot_date(day2)) == 2


async def test_unique_index_blocks_duplicate_position_per_day():
    await _seed([{"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "S"}])
    import sqlalchemy.exc

    raised = False
    try:
        await _seed([{"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "S"}])
    except sqlalchemy.exc.IntegrityError:
        raised = True
    assert raised, "duplicate (chart_type, scope, position, snapshot_date) should violate the unique index"


# ---------------------------------------------------------------------------
# Read API: latest / as_of / compare / history / snapshots
# ---------------------------------------------------------------------------


async def test_get_chart_defaults_to_latest_snapshot(client):
    await _seed(
        [
            {"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "Old"},
            {"position": 0, "snapshot_date": "2024-01-02", "artist": "B", "title": "New"},
        ]
    )
    resp = await client.get("/v1/charts/top_tracks?scope=global")
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshot_date"] == "2024-01-02"
    assert data["total"] == 1
    assert data["entries"][0]["track_title"] == "New"


async def test_as_of_exact_snapshot(client):
    await _seed(
        [
            {"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "Old"},
            {"position": 0, "snapshot_date": "2024-01-02", "artist": "B", "title": "New"},
        ]
    )
    resp = await client.get("/v1/charts/top_tracks?as_of=2024-01-01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshot_date"] == "2024-01-01"
    assert data["entries"][0]["track_title"] == "Old"


async def test_as_of_nearest_on_or_before(client):
    await _seed(
        [
            {"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "Old"},
            {"position": 0, "snapshot_date": "2024-01-03", "artist": "C", "title": "Newer"},
        ]
    )
    # Asking for the 2nd: no snapshot that day -> nearest on-or-before is the 1st.
    resp = await client.get("/v1/charts/top_tracks?as_of=2024-01-02")
    assert resp.status_code == 200
    assert resp.json()["snapshot_date"] == "2024-01-01"


async def test_as_of_too_early_404(client):
    await _seed([{"position": 0, "snapshot_date": "2024-01-05", "artist": "A", "title": "S"}])
    resp = await client.get("/v1/charts/top_tracks?as_of=2024-01-01")
    assert resp.status_code == 404


async def test_as_of_bad_format_400(client):
    await _seed([{"position": 0, "snapshot_date": "2024-01-05", "artist": "A", "title": "S"}])
    resp = await client.get("/v1/charts/top_tracks?as_of=01-05-2024")
    assert resp.status_code == 400


async def test_compare_position_deltas_and_new(client):
    # day1 ranking: A(0) B(1) C(2)
    # day2 ranking: B(0) A(1) D(2)   -> B climbed +1, A dropped -1, D is NEW, C gone
    await _seed(
        [
            {"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "SA"},
            {"position": 1, "snapshot_date": "2024-01-01", "artist": "B", "title": "SB"},
            {"position": 2, "snapshot_date": "2024-01-01", "artist": "C", "title": "SC"},
            {"position": 0, "snapshot_date": "2024-01-02", "artist": "B", "title": "SB"},
            {"position": 1, "snapshot_date": "2024-01-02", "artist": "A", "title": "SA"},
            {"position": 2, "snapshot_date": "2024-01-02", "artist": "D", "title": "SD"},
        ]
    )
    resp = await client.get("/v1/charts/top_tracks?compare=1d")
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshot_date"] == "2024-01-02"
    assert data["compared_to"] == "2024-01-01"
    by_title = {e["track_title"]: e for e in data["entries"]}
    assert by_title["SB"]["previously"] == 1 and by_title["SB"]["position_change"] == 1  # climbed
    assert by_title["SA"]["previously"] == 0 and by_title["SA"]["position_change"] == -1  # dropped
    assert by_title["SD"]["previously"] is None and by_title["SD"]["position_change"] is None  # NEW


async def test_compare_no_prior_snapshot_yields_nulls(client):
    await _seed([{"position": 0, "snapshot_date": "2024-01-02", "artist": "A", "title": "SA"}])
    resp = await client.get("/v1/charts/top_tracks?compare=7d")
    assert resp.status_code == 200
    data = resp.json()
    assert data["compared_to"] is None
    assert data["entries"][0]["position_change"] is None


async def test_compare_bad_format_400(client):
    await _seed([{"position": 0, "snapshot_date": "2024-01-02", "artist": "A", "title": "SA"}])
    resp = await client.get("/v1/charts/top_tracks?compare=lots")
    assert resp.status_code == 400


async def test_track_history(client):
    await _seed(
        [
            {"position": 3, "snapshot_date": "2024-01-01", "artist": "Kendrick Lamar", "title": "HUMBLE."},
            {"position": 1, "snapshot_date": "2024-01-02", "artist": "Kendrick Lamar", "title": "HUMBLE."},
            {"position": 0, "snapshot_date": "2024-01-03", "artist": "Kendrick Lamar", "title": "HUMBLE."},
        ]
    )
    resp = await client.get("/v1/charts/top_tracks/track/Kendrick%20Lamar/HUMBLE./history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    positions = [h["position"] for h in data["history"]]
    assert positions == [3, 1, 0]  # ordered by snapshot_date ascending


async def test_list_snapshots(client):
    await _seed(
        [
            {"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "S"},
            {"position": 0, "snapshot_date": "2024-01-02", "artist": "B", "title": "S"},
            {"position": 1, "snapshot_date": "2024-01-02", "artist": "C", "title": "T"},
        ]
    )
    resp = await client.get("/v1/charts/top_tracks/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshots"] == ["2024-01-02", "2024-01-01"]  # newest first, distinct


# ---------------------------------------------------------------------------
# Aggregate endpoints scoped to the latest snapshot
# ---------------------------------------------------------------------------


async def test_chart_stats_scoped_to_latest(client):
    await _seed(
        [
            {"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "S1"},
            {"position": 1, "snapshot_date": "2024-01-01", "artist": "B", "title": "S2"},
            {"position": 0, "snapshot_date": "2024-01-02", "artist": "A", "title": "S1"},
            {"position": 1, "snapshot_date": "2024-01-02", "artist": "B", "title": "S2"},
            {"position": 2, "snapshot_date": "2024-01-02", "artist": "C", "title": "S3"},
        ]
    )
    resp = await client.get("/v1/charts/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_entries"] == 3  # latest snapshot only, not 5
    assert data["latest_snapshot_date"] == "2024-01-02"
    assert data["snapshot_count"] == 2


async def test_list_charts_counts_latest_only(client):
    await _seed(
        [
            {"position": 0, "snapshot_date": "2024-01-01", "artist": "A", "title": "S1"},
            {"position": 1, "snapshot_date": "2024-01-01", "artist": "B", "title": "S2"},
            {"position": 0, "snapshot_date": "2024-01-02", "artist": "A", "title": "S1"},
        ]
    )
    resp = await client.get("/v1/charts")
    assert resp.status_code == 200
    charts = resp.json()["charts"]
    assert len(charts) == 1
    assert charts[0]["entries"] == 1  # latest snapshot has 1 row, not 3 total
    assert charts[0]["snapshot_date"] == "2024-01-02"


async def test_download_position_scoped_to_latest_snapshot(client, monkeypatch):
    # Two snapshots, same position present in each -> an unscoped query would
    # raise MultipleResultsFound. Enable spotizerr + stub the downloader so we
    # exercise the position-resolution path.
    await _seed(
        [
            {"position": 0, "snapshot_date": "2024-01-01", "artist": "OldArtist", "title": "OldSong"},
            {"position": 0, "snapshot_date": "2024-01-02", "artist": "NewArtist", "title": "NewSong"},
        ]
    )
    monkeypatch.setattr(settings, "SPOTIZERR_URL", "http://test-spotizerr")

    async def fake_search_and_download(artist, title):
        return {"status": "queued", "task_id": "t-123", "matched_artist": artist, "matched_title": title}

    monkeypatch.setattr("app.services.spotizerr.search_and_download", fake_search_and_download)

    resp = await client.post(
        "/v1/charts/download",
        json={"chart_type": "top_tracks", "scope": "global", "position": 0},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Resolved the LATEST snapshot's entry, not the old one (and didn't 500).
    assert data["artist_name"] == "NewArtist"
    assert data["track_title"] == "NewSong"
