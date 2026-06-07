"""
Tests for the Lidarr backfill engine.

Covers:
  * Capacity calc / rate-limit window
  * Match scoring (accept / reject thresholds, year / track-count requirements)
  * Album grouping over streamrip track results
  * dry-run path (no download_album call)
  * Cooldown / max-attempts gates
  * Bulk reset / retry / skip / delete state mutations
  * enabled=false short-circuits the tick
  * Backoff multiplier grows the cooldown correctly
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db import Base, LidarrBackfillRequest
from app.models.lidarr_backfill_schema import LidarrBackfillConfigData, get_defaults
from app.services import lidarr_backfill as lbf
from app.services.streamrip import SearchOutcome

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def cfg() -> LidarrBackfillConfigData:
    """Defaults with sensible starting state for tests."""
    return get_defaults()


def _lidarr_album(
    *,
    album_id: int = 1001,
    artist: str = "Tame Impala",
    title: str = "Currents",
    mb_album_id: str = "mb-currents",
    track_count: int | None = 13,
    release_date: str = "2015-07-17",
    source: str = "missing",
    monitored: bool = True,
) -> dict[str, Any]:
    return {
        "id": album_id,
        "title": title,
        "monitored": monitored,
        "foreignAlbumId": mb_album_id,
        "trackCount": track_count,
        "releaseDate": release_date,
        "artist": {"artistName": artist, "foreignArtistId": "mb-artist-tame"},
        "_source": source,
    }


def _streamrip_track(
    *,
    service: str = "qobuz",
    album_id: str = "qobuz-currents",
    artist: str = "Tame Impala",
    album: str = "Currents",
    year: int = 2015,
    track_count: int = 13,
    track_number: int = 1,
) -> dict[str, Any]:
    """Match the Spotify-like reshape produced by StreamripClient.search()."""
    return {
        "id": f"{service}-track-{track_number}",
        "name": f"Track {track_number}",
        "artists": [{"name": artist}],
        "album": {"name": album, "images": []},
        "type": "track",
        "_service": service,
        "_service_id": f"{service}-track-{track_number}",
        "_album_id": album_id,
        "_album_year": year,
        "_album_track_count": track_count,
        "_track_number": track_number,
        "_quality": "hires",
    }


# ---------------------------------------------------------------------------
# Match scoring
# ---------------------------------------------------------------------------


def test_match_score_perfect(cfg):
    sr = {
        "artist": "Tame Impala",
        "album": "Currents",
        "album_year": 2015,
        "album_track_count": 13,
    }
    score = lbf._score_match(_lidarr_album(), sr, cfg)
    assert score.accepted is True
    assert score.score == pytest.approx(1.0, abs=0.001)


def test_match_score_below_artist_threshold_rejects(cfg):
    sr = {"artist": "Some Other Band", "album": "Currents", "album_year": 2015, "album_track_count": 13}
    score = lbf._score_match(_lidarr_album(), sr, cfg)
    assert score.accepted is False
    assert any("artist_similarity" in r for r in score.reasons)


def test_match_score_album_threshold_rejects(cfg):
    cfg = cfg.model_copy(update={"match": cfg.match.model_copy(update={"min_album_similarity": 0.95})})
    sr = {"artist": "Tame Impala", "album": "different album entirely", "album_year": 2015, "album_track_count": 13}
    score = lbf._score_match(_lidarr_album(), sr, cfg)
    assert score.accepted is False
    assert any("album_similarity" in r for r in score.reasons)


def test_match_score_year_required_rejects_when_diff_too_large(cfg):
    cfg = cfg.model_copy(update={"match": cfg.match.model_copy(update={"require_year_match": True})})
    sr = {"artist": "Tame Impala", "album": "Currents", "album_year": 2010, "album_track_count": 13}
    score = lbf._score_match(_lidarr_album(release_date="2015-07-17"), sr, cfg)
    assert score.accepted is False
    assert any("year_diff" in r for r in score.reasons)


def test_match_score_track_count_required_rejects(cfg):
    cfg = cfg.model_copy(update={"match": cfg.match.model_copy(update={"require_track_count_match": True})})
    sr = {"artist": "Tame Impala", "album": "Currents", "album_year": 2015, "album_track_count": 8}
    score = lbf._score_match(_lidarr_album(track_count=13), sr, cfg)
    assert score.accepted is False
    assert any("track_count_diff" in r for r in score.reasons)


def test_match_score_handles_missing_year_gracefully(cfg):
    sr = {"artist": "Tame Impala", "album": "Currents", "album_year": None, "album_track_count": 13}
    score = lbf._score_match(_lidarr_album(release_date="2015-07-17"), sr, cfg)
    # Without require_year_match it's tolerated; the score just doesn't get the +0.05 bonus.
    assert score.accepted is True


# ---------------------------------------------------------------------------
# Track-result grouping
# ---------------------------------------------------------------------------


def test_group_tracks_by_album_collapses_to_one_entry_per_album():
    tracks = [
        _streamrip_track(track_number=1),
        _streamrip_track(track_number=2),
        _streamrip_track(track_number=3),
        _streamrip_track(album_id="qobuz-other-album", track_number=4),
    ]
    grouped = lbf._group_tracks_by_album(tracks)
    assert set(grouped.keys()) == {"qobuz-currents", "qobuz-other-album"}
    assert grouped["qobuz-currents"]["_tracks_seen"] == 3
    assert grouped["qobuz-currents"]["service"] == "qobuz"
    assert grouped["qobuz-currents"]["album_track_count"] == 13


# ---------------------------------------------------------------------------
# Capacity / rate-limit window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_capacity_counts_only_last_hour(db_session, cfg):
    now = int(time.time())
    # 5 rows in window, 2 outside (older than 1h)
    for i in range(5):
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=900 + i,
                artist="A",
                album_title="T",
                source="missing",
                status="downloading",
                created_at=now - 600,
                updated_at=now,
            )
        )
    for i in range(2):
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=950 + i,
                artist="A",
                album_title="T",
                source="missing",
                status="complete",
                created_at=now - 7200,
                updated_at=now - 7200,
            )
        )
    await db_session.commit()
    capacity = await lbf._compute_capacity(db_session, cfg)
    # Default cap = 10; 5 in window → 5 remaining
    assert capacity == 5


@pytest.mark.asyncio
async def test_compute_capacity_returns_zero_when_full(db_session, cfg):
    now = int(time.time())
    for i in range(cfg.max_downloads_per_hour):
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=2000 + i,
                artist="A",
                album_title="T",
                source="missing",
                status="downloading",
                created_at=now - 60,
                updated_at=now,
            )
        )
    await db_session.commit()
    assert await lbf._compute_capacity(db_session, cfg) == 0


@pytest.mark.asyncio
async def test_compute_capacity_counts_only_dispatched_downloads(db_session, cfg):
    """no_match (search-only), dry-run skipped, and permanently_skipped must NOT
    consume the per-hour download budget — only downloading/complete/failed do.
    Regression for the 'classical dead-zone rate-limits everything' bug."""
    now = int(time.time())
    aid = 11000
    # 4 rows that actually dispatched a download (these count).
    for st in ("downloading", "downloading", "complete", "failed"):
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=aid, artist="A", album_title="T", source="missing",
                status=st, created_at=now - 60, updated_at=now,
            )
        )
        aid += 1
    # A pile of search-only / terminal rows in the same window (these must NOT count).
    for st in (["no_match"] * 20 + ["skipped", "permanently_skipped"]):
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=aid, artist="A", album_title="T", source="missing",
                status=st, attempt_count=1, created_at=now - 60, updated_at=now,
            )
        )
        aid += 1
    await db_session.commit()

    # Only the 4 dispatched downloads count; if the 22 search-only rows counted,
    # capacity would be 0 (26 > default cap of 10).
    assert await lbf._compute_capacity(db_session, cfg) == cfg.max_downloads_per_hour - 4


# ---------------------------------------------------------------------------
# State filter (cooldown / max-attempts / in-flight dedupe)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_drops_in_flight_rows(db_session, cfg):
    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=3001,
            artist="A",
            album_title="T",
            source="missing",
            status="downloading",
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.commit()
    candidates = [_lidarr_album(album_id=3001), _lidarr_album(album_id=3002)]
    filtered = await lbf._filter_by_cooldown_and_state(db_session, candidates, cfg)
    ids = {c["id"] for c in filtered}
    assert ids == {3002}


@pytest.mark.asyncio
async def test_filter_honours_cooldown(db_session, cfg):
    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=3010,
            artist="A",
            album_title="T",
            source="missing",
            status="failed",
            attempt_count=1,
            next_retry_at=now + 3600,  # still in cooldown
            created_at=now - 86400,
            updated_at=now - 60,
        )
    )
    await db_session.commit()
    filtered = await lbf._filter_by_cooldown_and_state(db_session, [_lidarr_album(album_id=3010)], cfg)
    assert filtered == []


@pytest.mark.asyncio
async def test_filter_honours_max_attempts(db_session, cfg):
    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=3020,
            artist="A",
            album_title="T",
            source="missing",
            status="failed",
            attempt_count=cfg.retry.max_attempts,  # cap reached
            next_retry_at=None,
            created_at=now - 86400,
            updated_at=now,
        )
    )
    await db_session.commit()
    filtered = await lbf._filter_by_cooldown_and_state(db_session, [_lidarr_album(album_id=3020)], cfg)
    assert filtered == []


@pytest.mark.asyncio
async def test_filter_lets_eligible_retry_through(db_session, cfg):
    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=3030,
            artist="A",
            album_title="T",
            source="missing",
            status="failed",
            attempt_count=1,
            next_retry_at=now - 60,  # cooldown expired
            created_at=now - 86400,
            updated_at=now - 60,
        )
    )
    await db_session.commit()
    filtered = await lbf._filter_by_cooldown_and_state(db_session, [_lidarr_album(album_id=3030)], cfg)
    assert [c["id"] for c in filtered] == [3030]


@pytest.mark.asyncio
async def test_filter_returns_fresh_candidates_when_top_is_fully_blocked(db_session, cfg):
    """Issue #33: a fresh candidate further down the list must still come
    through even when the first N candidates are all blocked
    (complete / no_match-in-cooldown)."""
    now = int(time.time())
    # 20 blocked rows (the "top of recent_release sort" scenario from prod):
    # 10 complete + 10 no_match in cooldown.
    for i in range(10):
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=4000 + i,
                artist=f"A{i}",
                album_title=f"T{i}",
                source="missing",
                status="complete",
                attempt_count=1,
                created_at=now - 3600,
                updated_at=now - 60,
            )
        )
    for i in range(10):
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=4100 + i,
                artist=f"B{i}",
                album_title=f"U{i}",
                source="missing",
                status="no_match",
                attempt_count=1,
                next_retry_at=now + 3600,  # 1h into 24h cooldown
                created_at=now - 3600,
                updated_at=now - 60,
            )
        )
    await db_session.commit()

    # Caller passes the full Lidarr pool (top 20 blocked + 5 fresh further down).
    candidates = (
        [_lidarr_album(album_id=4000 + i) for i in range(10)]
        + [_lidarr_album(album_id=4100 + i) for i in range(10)]
        + [_lidarr_album(album_id=4200 + i) for i in range(5)]
    )

    filtered = await lbf._filter_by_cooldown_and_state(db_session, candidates, cfg)
    assert {c["id"] for c in filtered} == {4200, 4201, 4202, 4203, 4204}


@pytest.mark.asyncio
async def test_filter_handles_large_candidate_list(db_session, cfg):
    """Caller can pass a Lidarr-pool-sized list (thousands of IDs) without
    tripping SQLite's parameter limit. The filter chunks the IN query."""
    # Persist nothing — every candidate should pass through.
    candidates = [_lidarr_album(album_id=10_000 + i) for i in range(2500)]
    filtered = await lbf._filter_by_cooldown_and_state(db_session, candidates, cfg)
    assert len(filtered) == 2500


# ---------------------------------------------------------------------------
# Process album: dry_run, downloading, no_match
# ---------------------------------------------------------------------------


class _FakeStreamrip:
    """In-memory stand-in for StreamripClient. Records calls for assertions."""

    def __init__(self, search_results=None, download_result=None, status_results=None, search_errors=None):
        self.search_results = search_results if search_results is not None else []
        self.download_result = download_result or {"task_id": "task-123", "status": "downloading"}
        # Map of task_id → status payload (for poll_in_flight tests).
        self.status_results: dict[str, dict[str, Any]] = status_results or {}
        # Services (or "*") that should simulate an infra/transient search failure
        # (search_detailed returns ok=False) — for the issue #122 tests.
        self.search_errors: set[str] = set(search_errors or [])
        self.search_calls: list[tuple[str, int, str | None]] = []
        self.download_calls: list[tuple[str, str]] = []
        self.track_download_calls: list[tuple[str, str]] = []
        self.status_calls: list[str] = []

    def _results_for(self, service):
        if isinstance(self.search_results, dict):
            return self.search_results.get(service or "qobuz", [])
        return self.search_results

    async def search(self, query, limit=10, service=None):
        self.search_calls.append((query, limit, service))
        return self._results_for(service)

    async def search_detailed(self, query, limit=10, service=None):
        self.search_calls.append((query, limit, service))
        if "*" in self.search_errors or service in self.search_errors:
            return SearchOutcome(results=[], ok=False, error=f"simulated infra failure ({service})")
        return SearchOutcome(results=self._results_for(service), ok=True, error=None)

    async def download_album(self, service, album_id):
        self.download_calls.append((service, album_id))
        return self.download_result

    async def download_track(self, service, track_id):
        self.track_download_calls.append((service, track_id))
        return self.download_result

    async def get_status(self, task_id):
        self.status_calls.append(task_id)
        return self.status_results.get(
            task_id,
            {"status": "running", "progress": 0.5, "error": None, "raw": {}},
        )

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_process_album_dry_run_persists_skipped_no_download(db_session, cfg):
    cfg = cfg.model_copy(update={"dry_run": True})
    sr_tracks = [
        _streamrip_track(track_number=1),
        _streamrip_track(track_number=2),
    ]
    fake = _FakeStreamrip(search_results={"qobuz": sr_tracks})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=4001), cfg, fake)
    await db_session.commit()

    assert result["decision"] == "dry_run"
    assert fake.download_calls == []  # never queued
    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4001))
    ).scalar_one()
    assert row.status == "skipped"
    assert row.streamrip_task_id is None
    assert row.last_error == "dry_run"


@pytest.mark.asyncio
async def test_process_album_download_path_creates_downloading_row(db_session, cfg):
    sr_tracks = [_streamrip_track(track_number=1), _streamrip_track(track_number=2)]
    fake = _FakeStreamrip(search_results={"qobuz": sr_tracks})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=4002), cfg, fake)
    await db_session.commit()

    assert result["decision"] == "downloading"
    assert fake.download_calls == [("qobuz", "qobuz-currents")]
    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4002))
    ).scalar_one()
    assert row.status == "downloading"
    assert row.streamrip_task_id == "task-123"
    assert row.picked_service == "qobuz"
    assert row.match_score is not None and row.match_score > 0.9


@pytest.mark.asyncio
async def test_process_album_no_match_when_artist_mismatch(db_session, cfg):
    # All "hits" are for an unrelated artist with low fuzzy score.
    sr_tracks = [
        _streamrip_track(artist="Some Other Band", album="Different Album", track_number=1),
        _streamrip_track(artist="Some Other Band", album="Different Album", track_number=2),
    ]
    fake = _FakeStreamrip(search_results={"qobuz": sr_tracks, "tidal": [], "deezer": [], "soundcloud": []})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=4003), cfg, fake)
    await db_session.commit()

    assert result["decision"] == "no_match"
    assert fake.download_calls == []
    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4003))
    ).scalar_one()
    assert row.status == "no_match"
    assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_service_priority_skipped_below_quality_floor(db_session, cfg):
    # Only soundcloud has a hit but its declared quality is lossy_high — below the lossless floor.
    cfg = cfg.model_copy(update={"service_priority": ["soundcloud"]})
    fake = _FakeStreamrip(search_results={"soundcloud": [_streamrip_track(service="soundcloud")]})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=4004), cfg, fake)
    await db_session.commit()

    # soundcloud should be skipped entirely → no_match
    assert result["decision"] == "no_match"
    assert fake.search_calls == []  # never even searched (gated by quality)


# ---------------------------------------------------------------------------
# Issue #122 — infra/transient search failures must NOT become no_match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_outcome_distinguishes_empty_from_error():
    """SearchOutcome.ok is the contract _find_streamrip_album relies on:
    a 2xx with no hits is ok=True (searched, empty); an infra failure is ok=False."""
    assert SearchOutcome(results=[], ok=True).ok is True
    assert SearchOutcome(results=[], ok=False, error="boom").ok is False


@pytest.mark.asyncio
async def test_process_album_search_error_when_backend_unreachable(db_session, cfg):
    """Every configured service errors on search → re-queueable search_error,
    NOT no_match. The album may well exist; we just couldn't look (#122)."""
    fake = _FakeStreamrip(search_results={"qobuz": []}, search_errors={"*"})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=4101), cfg, fake)
    await db_session.commit()

    assert result["decision"] == "search_error"
    assert fake.download_calls == []
    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4101))
    ).scalar_one()
    assert row.status == "search_error"
    # Infra outage must NOT burn the retry budget or eventually permanently-skip.
    assert row.attempt_count == 0
    # Short cooldown so it re-queues once the backend recovers.
    assert row.next_retry_at is not None and row.next_retry_at > lbf._now_ts()
    assert "infra failure" in (row.last_error or "")


@pytest.mark.asyncio
async def test_partial_service_failure_still_no_match_not_search_error(db_session, cfg):
    """If at least one service searched successfully (even with no acceptable hit),
    the verdict is a genuine no_match — only a *total* search failure re-queues."""
    sr_tracks = [_streamrip_track(artist="Some Other Band", album="Different Album")]
    # qobuz answers (200, non-matching); tidal + deezer error out.
    fake = _FakeStreamrip(search_results={"qobuz": sr_tracks}, search_errors={"tidal", "deezer"})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=4102), cfg, fake)
    await db_session.commit()

    assert result["decision"] == "no_match"
    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4102))
    ).scalar_one()
    assert row.status == "no_match"
    assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_compute_capacity_ignores_search_error_rows(db_session, cfg):
    """search_error dispatched no download, so it must not consume the hourly cap
    — otherwise a backend outage would rate-limit the engine to a standstill."""
    now = lbf._now_ts()
    for aid in range(7001, 7026):  # 25 search_error rows in the window
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=aid, artist="A", album_title="T", source="missing",
                status="search_error", attempt_count=0, created_at=now - 60, updated_at=now,
            )
        )
    await db_session.commit()
    assert await lbf._compute_capacity(db_session, cfg) == cfg.max_downloads_per_hour


@pytest.mark.asyncio
async def test_search_error_row_requeues_after_cooldown(db_session, cfg):
    """A search_error row inside its cooldown is filtered out; once the cooldown
    expires it's eligible again (it never hits max_attempts because attempts=0)."""
    now = lbf._now_ts()
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=7100, artist="A", album_title="T", source="missing",
            status="search_error", attempt_count=0,
            next_retry_at=now + 300, created_at=now, updated_at=now,
        )
    )
    await db_session.commit()
    # Inside cooldown → dropped.
    assert await lbf._filter_by_cooldown_and_state(db_session, [_lidarr_album(album_id=7100)], cfg) == []
    # Past cooldown → eligible.
    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 7100))
    ).scalar_one()
    row.next_retry_at = now - 1
    await db_session.commit()
    out = await lbf._filter_by_cooldown_and_state(db_session, [_lidarr_album(album_id=7100)], cfg)
    assert {c["id"] for c in out} == {7100}


# ---------------------------------------------------------------------------
# Issue #124 — classical-aware matching + single-track fallback
# ---------------------------------------------------------------------------


def test_classical_relax_artist_accepts_when_album_strong(cfg):
    """Composer (Lidarr) vs performer (service): a strong album-title match
    forgives a weak artist match when classical_relax_artist is on."""
    cfg = cfg.model_copy(update={"match": cfg.match.model_copy(update={"classical_relax_artist": True})})
    sr = {"artist": "Nicola Benedetti", "album": "Currents", "album_year": 2015, "album_track_count": 13}
    score = lbf._score_match(_lidarr_album(artist="Пётр Ильич Чайковский", title="Currents"), sr, cfg)
    assert score.accepted is True
    assert any("classical_artist_relaxed" in r for r in score.reasons)


def test_classical_relax_artist_still_rejects_when_album_weak(cfg):
    """Relaxation only applies when the album title is a strong anchor (≥0.90).
    A weak album match must still be rejected to avoid false accepts."""
    cfg = cfg.model_copy(update={"match": cfg.match.model_copy(update={"classical_relax_artist": True})})
    sr = {"artist": "Nicola Benedetti", "album": "Some Other Symphony", "album_year": 2015, "album_track_count": 13}
    score = lbf._score_match(_lidarr_album(artist="Пётр Ильич Чайковский", title="Currents"), sr, cfg)
    assert score.accepted is False


def test_classical_relax_off_by_default_rejects(cfg):
    """Default behaviour unchanged: weak artist similarity rejects."""
    sr = {"artist": "Nicola Benedetti", "album": "Currents", "album_year": 2015, "album_track_count": 13}
    score = lbf._score_match(_lidarr_album(artist="Пётр Ильич Чайковский", title="Currents"), sr, cfg)
    assert score.accepted is False


@pytest.mark.asyncio
async def test_track_fallback_downloads_single_track_when_no_album(db_session, cfg):
    """A 'missing album' that is really a single: no album-title match exists,
    but a track titled like the album does → download that track (#124)."""
    cfg = cfg.model_copy(update={"match": cfg.match.model_copy(update={"allow_track_fallback": True})})
    # The track's *title* is the Lidarr 'album' title; the album it lives in is
    # a compilation whose title won't match → no album-level acceptance.
    single = _streamrip_track(
        service="qobuz", album="Greatest Hits Compilation", track_number=7
    )
    single["name"] = "Currents"  # track title == Lidarr "album" title
    single["_service_id"] = "qobuztrack123"
    fake = _FakeStreamrip(search_results={"qobuz": [single]})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=4201), cfg, fake)
    await db_session.commit()

    assert result["decision"] == "downloading"
    assert result["entity_type"] == "track"
    assert fake.track_download_calls == [("qobuz", "qobuztrack123")]
    assert fake.download_calls == []  # no whole-album download
    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4201))
    ).scalar_one()
    assert row.status == "downloading"
    assert row.picked_service == "qobuz"


@pytest.mark.asyncio
async def test_track_fallback_disabled_by_default(db_session, cfg):
    """Without allow_track_fallback the single-track tail stays no_match."""
    single = _streamrip_track(service="qobuz", album="Greatest Hits Compilation")
    single["name"] = "Currents"
    single["_service_id"] = "qobuztrack123"
    fake = _FakeStreamrip(search_results={"qobuz": [single]})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=4202), cfg, fake)
    await db_session.commit()

    assert result["decision"] == "no_match"
    assert fake.track_download_calls == []


# ---------------------------------------------------------------------------
# Tick-level: enabled=false / not configured short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tick_skipped_when_disabled(db_session, monkeypatch):
    # Default config has enabled=False. No need to mock anything else; the
    # function returns immediately before touching Lidarr/streamrip.
    summary = await lbf.run_backfill_tick(db_session)
    assert summary == {"skipped": "disabled"}


# ---------------------------------------------------------------------------
# StreamripClient.get_status — issue #34: distinguish 404 (lost) from real errors
# ---------------------------------------------------------------------------


class TestStreamripGetStatus:
    """Verify get_status returns four distinct outcomes so poll_in_flight
    can react correctly to each. Uses httpx.MockTransport — no network."""

    @pytest.mark.asyncio
    async def test_complete_status_passed_through(self):
        import httpx

        from app.services.streamrip import StreamripClient

        def handler(request):
            return httpx.Response(
                200,
                json={"status": "complete", "progress": 1.0, "task_id": "abc", "error": None},
            )

        client = StreamripClient("http://streamrip-api")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://streamrip-api")
        try:
            result = await client.get_status("abc")
            assert result["status"] == "complete"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_404_returns_lost(self):
        import httpx

        from app.services.streamrip import StreamripClient

        def handler(request):
            return httpx.Response(404, json={"detail": "Task not found"})

        client = StreamripClient("http://streamrip-api")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://streamrip-api")
        try:
            result = await client.get_status("vanished")
            assert result["status"] == "lost"
            assert "no record" in (result["error"] or "").lower()
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_network_error_returns_transient_error(self):
        import httpx

        from app.services.streamrip import StreamripClient

        def handler(request):
            raise httpx.ConnectError("Connection refused")

        client = StreamripClient("http://streamrip-api")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://streamrip-api")
        try:
            result = await client.get_status("any")
            assert result["status"] == "transient_error"
            assert "connection" in (result["error"] or "").lower()
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_500_returns_error(self):
        import httpx

        from app.services.streamrip import StreamripClient

        def handler(request):
            return httpx.Response(500, text="Internal server error")

        client = StreamripClient("http://streamrip-api")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://streamrip-api")
        try:
            result = await client.get_status("sometask")
            assert result["status"] == "error"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_poll_in_flight_lost_status_resets_row_without_attempt_bump(db_session, cfg, monkeypatch):
    """Issue #34: when streamrip-api restarts and loses a task (returns 404),
    the corresponding `downloading` row must be re-queueable, not penalised
    with an attempt-count bump or 24h cooldown."""
    cfg = cfg.model_copy(update={"enabled": True})
    monkeypatch.setattr(lbf, "get_config", lambda: cfg)
    monkeypatch.setattr(lbf.settings, "STREAMRIP_API_URL", "http://streamrip-api:8282")
    monkeypatch.setattr(lbf.settings, "LIDARR_URL", "")  # avoid lidarr trigger path
    monkeypatch.setattr(lbf.settings, "LIDARR_API_KEY", "")

    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=6001,
            artist="Lost Artist",
            album_title="Lost Album",
            source="missing",
            status="downloading",
            streamrip_task_id="task-vanished",
            picked_service="qobuz",
            attempt_count=1,
            created_at=now - 60,
            updated_at=now - 60,
        )
    )
    await db_session.commit()

    fake = _FakeStreamrip(
        status_results={
            "task-vanished": {
                "status": "lost",
                "progress": None,
                "error": "streamrip-api has no record of this task (likely restarted)",
                "raw": {},
            }
        }
    )
    monkeypatch.setattr(lbf, "StreamripClient", lambda url: fake)

    summary = await lbf.poll_in_flight(db_session)

    assert summary["lost"] == 1
    assert summary["failed"] == 0
    refreshed = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 6001))
    ).scalar_one()
    # Re-queueable: no attempt bump, short cooldown, task_id cleared, status=failed.
    assert refreshed.attempt_count == 1  # unchanged from seeded value
    assert refreshed.streamrip_task_id is None
    assert refreshed.status == "failed"
    assert refreshed.next_retry_at is not None
    assert refreshed.next_retry_at <= now + 320  # ≤ 5min cooldown + a little slack
    assert "lost" in (refreshed.last_error or "").lower()


@pytest.mark.asyncio
async def test_poll_in_flight_transient_error_leaves_row_alone(db_session, cfg, monkeypatch):
    """Issue #34: a network blip mid-poll must NOT promote the row to
    failed.  The next poll cycle gets another chance."""
    cfg = cfg.model_copy(update={"enabled": True})
    monkeypatch.setattr(lbf, "get_config", lambda: cfg)
    monkeypatch.setattr(lbf.settings, "STREAMRIP_API_URL", "http://streamrip-api:8282")
    monkeypatch.setattr(lbf.settings, "LIDARR_URL", "")
    monkeypatch.setattr(lbf.settings, "LIDARR_API_KEY", "")

    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=6002,
            artist="A",
            album_title="T",
            source="missing",
            status="downloading",
            streamrip_task_id="task-flaky",
            attempt_count=1,
            created_at=now - 60,
            updated_at=now - 60,
        )
    )
    await db_session.commit()

    fake = _FakeStreamrip(
        status_results={
            "task-flaky": {
                "status": "transient_error",
                "progress": None,
                "error": "ConnectTimeout",
                "raw": {},
            }
        }
    )
    monkeypatch.setattr(lbf, "StreamripClient", lambda url: fake)

    summary = await lbf.poll_in_flight(db_session)

    assert summary["still_running"] == 1
    assert summary.get("failed", 0) == 0
    assert summary.get("lost", 0) == 0
    refreshed = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 6002))
    ).scalar_one()
    # Untouched.
    assert refreshed.status == "downloading"
    assert refreshed.streamrip_task_id == "task-flaky"
    assert refreshed.attempt_count == 1
    assert refreshed.next_retry_at is None


@pytest.mark.asyncio
async def test_poll_in_flight_real_error_still_fails_row(db_session, cfg, monkeypatch):
    """Sanity check: a genuine status='error' from streamrip-api must still
    advance the row to failed with a normal cooldown.  This is the existing
    behaviour and should not regress with the lost/transient additions."""
    cfg = cfg.model_copy(update={"enabled": True})
    monkeypatch.setattr(lbf, "get_config", lambda: cfg)
    monkeypatch.setattr(lbf.settings, "STREAMRIP_API_URL", "http://streamrip-api:8282")
    monkeypatch.setattr(lbf.settings, "LIDARR_URL", "")
    monkeypatch.setattr(lbf.settings, "LIDARR_API_KEY", "")

    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=6003,
            artist="A",
            album_title="T",
            source="missing",
            status="downloading",
            streamrip_task_id="task-broken",
            attempt_count=0,
            created_at=now - 60,
            updated_at=now - 60,
        )
    )
    await db_session.commit()

    fake = _FakeStreamrip(
        status_results={
            "task-broken": {
                "status": "error",
                "progress": None,
                "error": "qobuz: track not found",
                "raw": {},
            }
        }
    )
    monkeypatch.setattr(lbf, "StreamripClient", lambda url: fake)

    summary = await lbf.poll_in_flight(db_session)

    assert summary["failed"] == 1
    refreshed = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 6003))
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.attempt_count == 1  # bumped
    assert refreshed.next_retry_at is not None
    # Real error gets the configured cooldown (default cooldown_hours=24, so >> 5min).
    assert refreshed.next_retry_at > now + 3600


@pytest.mark.asyncio
async def test_run_tick_records_completion_even_on_early_skip(db_session, monkeypatch):
    """`get_last_tick_at` must be stamped on every tick exit so the dashboard
    can distinguish 'scheduler isn't running' from 'scheduler runs every 5m
    but always early-skips'."""
    # Reset module-level state (other tests may have set it).
    monkeypatch.setattr(lbf, "_last_tick_at", None)
    assert lbf.get_last_tick_at() is None

    # enabled=False short-circuits before the inner work — but the wrapper
    # still records completion.
    before = int(time.time())
    summary = await lbf.run_backfill_tick(db_session)
    after = int(time.time())

    assert summary == {"skipped": "disabled"}
    last = lbf.get_last_tick_at()
    assert last is not None
    assert before <= last <= after


# ---------------------------------------------------------------------------
# State mutations (retry / skip / delete / bulk reset)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_request_resets_attempt_count(db_session):
    now = int(time.time())
    row = LidarrBackfillRequest(
        lidarr_album_id=5001,
        artist="A",
        album_title="T",
        source="missing",
        status="failed",
        attempt_count=2,
        next_retry_at=now + 3600,
        last_error="boom",
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    ok = await lbf.retry_request(db_session, row.id)
    await db_session.commit()
    assert ok is True
    refreshed = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.id == row.id))
    ).scalar_one()
    assert refreshed.status == "queued"
    assert refreshed.attempt_count == 0
    assert refreshed.next_retry_at is None
    assert refreshed.last_error is None


@pytest.mark.asyncio
async def test_skip_request_marks_permanently_skipped(db_session):
    now = int(time.time())
    row = LidarrBackfillRequest(
        lidarr_album_id=5002,
        artist="A",
        album_title="T",
        source="missing",
        status="failed",
        attempt_count=1,
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    ok = await lbf.skip_request(db_session, row.id)
    await db_session.commit()
    assert ok is True
    refreshed = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.id == row.id))
    ).scalar_one()
    assert refreshed.status == "permanently_skipped"


@pytest.mark.asyncio
async def test_reset_backfill_state_scope_failed(db_session):
    now = int(time.time())
    db_session.add_all(
        [
            LidarrBackfillRequest(
                lidarr_album_id=6001,
                artist="A",
                album_title="X",
                source="missing",
                status="failed",
                created_at=now,
                updated_at=now,
            ),
            LidarrBackfillRequest(
                lidarr_album_id=6002,
                artist="A",
                album_title="Y",
                source="missing",
                status="complete",
                created_at=now,
                updated_at=now,
            ),
            LidarrBackfillRequest(
                lidarr_album_id=6003,
                artist="A",
                album_title="Z",
                source="missing",
                status="failed",
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    await db_session.commit()

    deleted = await lbf.reset_backfill_state(db_session, "failed")
    await db_session.commit()
    assert deleted == 2

    remaining = (
        (await db_session.execute(select(LidarrBackfillRequest).order_by(LidarrBackfillRequest.lidarr_album_id)))
        .scalars()
        .all()
    )
    assert [r.lidarr_album_id for r in remaining] == [6002]


@pytest.mark.asyncio
async def test_reset_backfill_state_unknown_scope_raises(db_session):
    with pytest.raises(ValueError):
        await lbf.reset_backfill_state(db_session, "garbage")


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_next_retry_timestamp_grows_with_attempts(cfg):
    # cooldown 24h, multiplier 2.0 → 24h, 48h, 96h, 192h, ...
    now = int(time.time())
    a1 = lbf._next_retry_timestamp(1, cfg)
    a2 = lbf._next_retry_timestamp(2, cfg)
    a3 = lbf._next_retry_timestamp(3, cfg)
    # Allow ~5s slop for clock drift between calls.
    assert abs((a1 - now) - 24 * 3600) < 5
    assert abs((a2 - now) - 48 * 3600) < 5
    assert abs((a3 - now) - 96 * 3600) < 5


def test_next_retry_timestamp_capped_at_30_days(cfg):
    cfg = cfg.model_copy(update={"retry": cfg.retry.model_copy(update={"backoff_multiplier": 10.0})})
    now = int(time.time())
    big = lbf._next_retry_timestamp(10, cfg)  # would be 24h × 10^9 hours uncapped
    # Cap is 30 days = 720h × 3600s
    assert big - now <= 30 * 86400 + 5
    assert big - now >= 30 * 86400 - 5


# ---------------------------------------------------------------------------
# Filters (allow / deny)
# ---------------------------------------------------------------------------


def test_normalize_artist_strips_the_prefix_and_casefolds():
    assert lbf._normalize_artist_name("The Beatles") == "beatles"
    assert lbf._normalize_artist_name("  TAME IMPALA  ") == "tame impala"


# ---------------------------------------------------------------------------
# Structural fallback (allow_structural_fallback)
# ---------------------------------------------------------------------------


def _structural_cfg(cfg, *, on: bool):
    return cfg.model_copy(update={"match": cfg.match.model_copy(update={"allow_structural_fallback": on})})


def test_structural_fallback_off_default_rejects_low_album_sim(cfg):
    """Default (off): below-threshold album similarity always rejects."""
    sr = {
        "artist": "The Kinks",
        "album": "Picture Book",  # vs lidarr "Picture Book, Volume 1" → ~0.71 similarity
        "album_year": 2008,
        "album_track_count": 13,
    }
    score = lbf._score_match(
        _lidarr_album(artist="The Kinks", title="Picture Book, Volume 1", track_count=13, release_date="2008-04-21"),
        sr,
        cfg,
    )
    assert score.accepted is False
    assert any("album_similarity" in r and "structural_ok" not in r for r in score.reasons)


def test_structural_fallback_on_accepts_when_artist_track_year_align(cfg):
    """With fallback enabled: low album sim is forgiven when structural metadata matches."""
    cfg = _structural_cfg(cfg, on=True)
    sr = {
        "artist": "The Kinks",
        "album": "Picture Book",
        "album_year": 2008,
        "album_track_count": 13,
    }
    score = lbf._score_match(
        _lidarr_album(artist="The Kinks", title="Picture Book, Volume 1", track_count=13, release_date="2008-04-21"),
        sr,
        cfg,
    )
    assert score.accepted is True
    assert any("structural_ok" in r for r in score.reasons)


def test_structural_fallback_rejects_when_artist_not_exact(cfg):
    """Fuzzy artist (<0.95) blocks the fallback even when track count + year align."""
    cfg = _structural_cfg(cfg, on=True)
    sr = {
        "artist": "The Kinks Tribute Band",  # ~0.65 similarity
        "album": "Picture Book",
        "album_year": 2008,
        "album_track_count": 13,
    }
    score = lbf._score_match(
        _lidarr_album(artist="The Kinks", title="Picture Book, Volume 1", track_count=13, release_date="2008-04-21"),
        sr,
        cfg,
    )
    assert score.accepted is False


def test_structural_fallback_rejects_when_track_count_differs(cfg):
    """Track count must match exactly — different counts block the fallback."""
    cfg = _structural_cfg(cfg, on=True)
    sr = {
        "artist": "The Kinks",
        "album": "Picture Book",
        "album_year": 2008,
        "album_track_count": 26,  # ≠ 13
    }
    score = lbf._score_match(
        _lidarr_album(artist="The Kinks", title="Picture Book, Volume 1", track_count=13, release_date="2008-04-21"),
        sr,
        cfg,
    )
    assert score.accepted is False


def test_structural_fallback_rejects_when_track_count_unknown(cfg):
    """Missing track count on either side prevents the structural fallback."""
    cfg = _structural_cfg(cfg, on=True)
    sr = {
        "artist": "The Kinks",
        "album": "Picture Book",
        "album_year": 2008,
        "album_track_count": None,  # streamrip didn't expose it
    }
    score = lbf._score_match(
        _lidarr_album(artist="The Kinks", title="Picture Book, Volume 1", track_count=13, release_date="2008-04-21"),
        sr,
        cfg,
    )
    assert score.accepted is False


def test_structural_fallback_rejects_when_year_drift_too_large(cfg):
    """Year diff > 1 (re-release / remaster threshold) blocks the fallback."""
    cfg = _structural_cfg(cfg, on=True)
    sr = {
        "artist": "The Kinks",
        "album": "Picture Book",
        "album_year": 2015,  # 7 years off
        "album_track_count": 13,
    }
    score = lbf._score_match(
        _lidarr_album(artist="The Kinks", title="Picture Book, Volume 1", track_count=13, release_date="2008-04-21"),
        sr,
        cfg,
    )
    assert score.accepted is False


# ---------------------------------------------------------------------------
# available_services filtering (no log noise on unconfigured services)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_streamrip_album_skips_unconfigured_services(db_session, cfg):
    """When available_services restricts the set, the engine doesn't even
    call .search() for unlisted services. Asserts the search_calls log
    only contains the allowed service."""
    sr_tracks = [_streamrip_track(track_number=1, service="qobuz")]
    fake = _FakeStreamrip(search_results={"qobuz": sr_tracks})

    lookup = await lbf._find_streamrip_album(
        fake,
        _lidarr_album(),
        cfg,
        available_services=["qobuz"],  # only qobuz is configured
    )
    assert lookup.match is not None
    assert lookup.match.service == "qobuz"
    assert lookup.searched_ok is True
    services_searched = {svc for _, _, svc in fake.search_calls}
    assert services_searched == {"qobuz"}, services_searched  # tidal/deezer/soundcloud skipped silently


@pytest.mark.asyncio
async def test_find_streamrip_album_none_means_dont_filter(db_session, cfg):
    """available_services=None preserves the prior behaviour (try every service)."""
    fake = _FakeStreamrip(search_results={"qobuz": [], "tidal": [], "deezer": [], "soundcloud": []})

    await lbf._find_streamrip_album(fake, _lidarr_album(), cfg, available_services=None)
    services_searched = {svc for _, _, svc in fake.search_calls}
    # soundcloud is filtered out by the quality-floor gate (lossy_high < lossless),
    # so we expect everything except soundcloud.
    assert services_searched == {"qobuz", "tidal", "deezer"}


# ---------------------------------------------------------------------------
# Tick-in-progress flag (powers the dashboard "Running tick" badge)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Queue-order config (sources.queue_order)
# ---------------------------------------------------------------------------


class _FakeLidarrClient:
    """Records the sort args passed to get_missing_albums / get_cutoff_unmet_albums.

    Returns whatever was preloaded via ``rows`` so the test can verify both the
    Lidarr API parameters and the engine's post-fetch handling (e.g. shuffle).
    Also implements ``fetch_wanted_page`` for the streaming path used by the
    runtime tick — pages are sliced from the same preloaded rows.
    """

    def __init__(self, missing_rows=None, cutoff_rows=None):
        self.missing_rows = missing_rows or []
        self.cutoff_rows = cutoff_rows or []
        self.missing_calls: list[dict[str, Any]] = []
        self.cutoff_calls: list[dict[str, Any]] = []
        self.page_calls: list[dict[str, Any]] = []

    async def get_missing_albums(
        self, page_size=100, monitored=True, sort_key="albums.releaseDate", sort_direction="descending"
    ):
        self.missing_calls.append({"sort_key": sort_key, "sort_direction": sort_direction, "monitored": monitored})
        return list(self.missing_rows)

    async def get_cutoff_unmet_albums(
        self, page_size=100, monitored=True, sort_key="albums.releaseDate", sort_direction="descending"
    ):
        self.cutoff_calls.append({"sort_key": sort_key, "sort_direction": sort_direction, "monitored": monitored})
        return list(self.cutoff_rows)

    async def fetch_wanted_page(self, path, *, page, page_size, monitored, sort_key, sort_direction):
        self.page_calls.append({"path": path, "page": page, "page_size": page_size})
        rows = self.missing_rows if path.endswith("/missing") else self.cutoff_rows
        start = (page - 1) * page_size
        return list(rows[start : start + page_size])

    async def close(self):
        pass


def _make_albums(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": 9000 + i,
            "title": f"Album {i}",
            "artist": {"artistName": f"Artist {i}"},
            "foreignAlbumId": f"mb-{i}",
            "trackCount": 10,
            "releaseDate": f"20{20 + (i % 6)}-01-01",
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order,expected_sort,expected_dir",
    [
        ("recent_release", "albums.releaseDate", "descending"),
        ("oldest_release", "albums.releaseDate", "ascending"),
        ("alphabetical", "albums.title", "ascending"),
        ("random", "albums.releaseDate", "descending"),  # base sort, then shuffle
    ],
)
async def test_queue_order_maps_to_correct_lidarr_sort(cfg, order, expected_sort, expected_dir):
    cfg = cfg.model_copy(update={"sources": cfg.sources.model_copy(update={"queue_order": order})})
    fake_lidarr = _FakeLidarrClient(missing_rows=_make_albums(3))

    await lbf._fetch_candidates(cfg, fake_lidarr, limit=10)

    assert len(fake_lidarr.missing_calls) == 1
    call = fake_lidarr.missing_calls[0]
    assert call["sort_key"] == expected_sort
    assert call["sort_direction"] == expected_dir


@pytest.mark.asyncio
async def test_queue_order_random_shuffles_results(cfg):
    cfg = cfg.model_copy(update={"sources": cfg.sources.model_copy(update={"queue_order": "random"})})
    rows = _make_albums(50)
    original_order = [r["id"] for r in rows]
    fake_lidarr = _FakeLidarrClient(missing_rows=rows)

    out = await lbf._fetch_candidates(cfg, fake_lidarr, limit=50)

    out_order = [c["id"] for c in out]
    # Same set of IDs but permuted (random with seed could in theory reproduce
    # the original order — extremely unlikely with 50 elements).
    assert set(out_order) == set(original_order)
    assert out_order != original_order


@pytest.mark.asyncio
async def test_queue_order_default_is_recent_release(cfg):
    """Existing configs without queue_order set should default to recent_release."""
    fake_lidarr = _FakeLidarrClient(missing_rows=_make_albums(3))
    await lbf._fetch_candidates(cfg, fake_lidarr, limit=10)
    call = fake_lidarr.missing_calls[0]
    assert call["sort_key"] == "albums.releaseDate"
    assert call["sort_direction"] == "descending"


# ---------------------------------------------------------------------------
# Streaming candidate fetcher (used by run_backfill_tick)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_fresh_candidates_stops_once_target_reached(db_session, cfg):
    """The streaming fetcher must short-circuit once it has enough fresh
    candidates rather than draining the entire wanted queue every tick."""
    # 500 albums spread across 5 pages of 100. Target is 8 fresh candidates,
    # nothing in the local DB blocks them, so the fetcher should only need
    # the first page (which already yields 100 fresh).
    fake = _FakeLidarrClient(missing_rows=_make_albums(500))

    fresh, stats = await lbf._stream_fresh_candidates(db_session, cfg, fake, target=8, page_size=100)

    assert len(fresh) >= 8
    assert stats["pages_fetched"] == 1
    # We should have fetched only the missing source, exactly once.
    assert [c["page"] for c in fake.page_calls] == [1]


@pytest.mark.asyncio
async def test_stream_fresh_candidates_skips_blocked_rows_and_keeps_paging(db_session, cfg):
    """When the top of the queue is saturated with already-touched (in-flight /
    completed / cooldown'd) albums, the streaming fetcher must keep paging
    until it has accumulated `target` *fresh* rows."""
    # 400 albums in 4 pages of 100. Mark every album in pages 1-3 as
    # "queued" (in-flight) so the cooldown filter rejects them. Page 4 has
    # 100 fresh albums — that's the only place we can find fresh rows.
    rows = _make_albums(400)
    now = int(time.time())
    for r in rows[:300]:
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=r["id"],
                artist=r["artist"]["artistName"],
                album_title=r["title"],
                source="missing",
                status="queued",  # in-flight
                attempt_count=0,
                created_at=now,
                updated_at=now,
            )
        )
    await db_session.commit()

    fake = _FakeLidarrClient(missing_rows=rows)

    fresh, stats = await lbf._stream_fresh_candidates(db_session, cfg, fake, target=5, page_size=100)

    # All 5 we asked for came from page 4.
    assert len(fresh) >= 5
    # We had to walk all 4 pages to find them.
    assert stats["pages_fetched"] == 4
    assert [c["page"] for c in fake.page_calls] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_stream_fresh_candidates_resumes_from_cursor_across_ticks(db_session, cfg):
    """Successive ticks must resume paging from the persisted cursor, not
    restart at page 1 — so the engine sweeps the whole queue over time instead
    of re-scanning the same head every tick."""
    fake = _FakeLidarrClient(missing_rows=_make_albums(1000))  # 10 pages of 100

    # Tick 1: starts at page 1, finds target on page 1, parks the cursor at 2.
    fresh1, stats1 = await lbf._stream_fresh_candidates(db_session, cfg, fake, target=8, page_size=100)
    assert len(fresh1) >= 8
    assert stats1["cursor_missing"] == 2

    # Tick 2 (same session = same persisted state): resumes at page 2.
    fresh2, stats2 = await lbf._stream_fresh_candidates(db_session, cfg, fake, target=8, page_size=100)
    assert len(fresh2) >= 8
    assert stats2["cursor_missing"] == 3
    # Across both ticks we fetched page 1 then page 2 — not page 1 twice.
    assert [c["page"] for c in fake.page_calls] == [1, 2]


@pytest.mark.asyncio
async def test_stream_fresh_candidates_advances_cursor_past_fully_blocked_head(db_session, cfg):
    """The wedge fix: when every album in the scan window is terminal (so 0
    fresh come through), the cursor must still advance so the *next* tick moves
    deeper into the queue instead of re-scanning the same blocked head."""
    rows = _make_albums(300)  # 3 pages of 100
    now = int(time.time())
    # Mark every album as complete → the cooldown/state filter drops them all.
    for r in rows:
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=r["id"],
                artist=r["artist"]["artistName"],
                album_title=r["title"],
                source="missing",
                status="complete",
                attempt_count=1,
                created_at=now,
                updated_at=now,
            )
        )
    await db_session.commit()

    fake = _FakeLidarrClient(missing_rows=rows)

    # Window of 2 pages per tick. No fresh anywhere, but the cursor must move.
    fresh, stats = await lbf._stream_fresh_candidates(
        db_session, cfg, fake, target=5, page_size=100, max_pages_per_source=2
    )
    assert fresh == []
    assert stats["pages_fetched"] == 2
    # Scanned pages 1-2 this tick → cursor parked at page 3 for the next tick.
    assert stats["cursor_missing"] == 3
    assert [c["page"] for c in fake.page_calls] == [1, 2]


@pytest.mark.asyncio
async def test_stream_fresh_candidates_wraps_cursor_at_end_of_queue(db_session, cfg):
    """Reaching the end of the queue (a short final page) wraps the cursor back
    to 1 so the next sweep restarts from the front."""
    fake = _FakeLidarrClient(missing_rows=_make_albums(150))  # page1=100, page2=50 (partial)

    # target high enough that we never short-circuit — we run to the end.
    fresh, stats = await lbf._stream_fresh_candidates(db_session, cfg, fake, target=10_000, page_size=100)

    assert len(fresh) == 150
    assert stats["pages_fetched"] == 2
    assert stats["cursor_missing"] == 1  # wrapped


@pytest.mark.asyncio
async def test_retire_exhausted_no_match_relabels_only_maxed_rows(db_session, cfg):
    """Maxed-out no_match rows become permanently_skipped; under-cap no_match
    and non-no_match rows are left untouched. Idempotent on a second pass."""
    now = int(time.time())
    db_session.add_all(
        [
            LidarrBackfillRequest(
                lidarr_album_id=7001,
                artist="A",
                album_title="maxed",
                source="missing",
                status="no_match",
                attempt_count=cfg.retry.max_attempts,
                next_retry_at=now + 999,
                created_at=now,
                updated_at=now,
            ),
            LidarrBackfillRequest(
                lidarr_album_id=7002,
                artist="A",
                album_title="eligible",
                source="missing",
                status="no_match",
                attempt_count=1,
                created_at=now,
                updated_at=now,
            ),
            LidarrBackfillRequest(
                lidarr_album_id=7003,
                artist="A",
                album_title="complete-maxed",
                source="missing",
                status="complete",
                attempt_count=cfg.retry.max_attempts,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    await db_session.commit()

    n = await lbf._retire_exhausted_no_match(db_session, cfg)
    await db_session.commit()
    assert n == 1

    rows = {
        r.lidarr_album_id: r
        for r in (await db_session.execute(select(LidarrBackfillRequest))).scalars().all()
    }
    assert rows[7001].status == "permanently_skipped"
    assert rows[7001].next_retry_at is None
    assert rows[7002].status == "no_match"  # under the cap — untouched
    assert rows[7003].status == "complete"  # not a no_match — untouched

    # Idempotent: nothing left to retire.
    assert await lbf._retire_exhausted_no_match(db_session, cfg) == 0


@pytest.mark.asyncio
async def test_get_stats_throughput_7d_counts_completes_per_day(db_session, cfg, monkeypatch):
    """The throughput histogram is computed server-side from completed rows'
    updated_at — covering the full 7-day window, not a recency-capped slice of
    the requests list (which is dominated by no_match churn in practice)."""
    monkeypatch.setattr(lbf, "get_config", lambda: cfg)
    now = int(time.time())
    day = 86400

    def add(album_id, updated_at, status="complete"):
        db_session.add(
            LidarrBackfillRequest(
                lidarr_album_id=album_id,
                artist="A",
                album_title="T",
                source="missing",
                status=status,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )

    add(1, now)  # today
    add(2, now)  # today (2 completes today)
    add(3, now - 2 * day)  # 2 days ago
    add(4, now - 8 * day)  # outside the 7-day window → excluded
    add(5, now, status="no_match")  # not complete → excluded
    await db_session.commit()

    # Pass lidarr_totals so get_stats makes no upstream Lidarr HTTP call.
    stats = await lbf.get_stats(db_session, lidarr_totals=lbf.LidarrTotals(None, None, False))

    tp = stats["throughput_7d"]
    assert len(tp) == 7  # always 7 buckets, zero-filled
    assert all(len(b["date"]) == 10 for b in tp)  # YYYY-MM-DD
    assert tp[-1]["count"] == 2  # today (newest bucket)
    assert tp[4]["count"] == 1  # 2 days ago (today=idx6, so 2 days back=idx4)
    # Only the 3 in-window completes count (8-day-old + no_match excluded).
    assert sum(b["count"] for b in tp) == 3


@pytest.mark.asyncio
async def test_no_match_gets_cooldown_to_avoid_back_to_back_retries(db_session, cfg):
    """Regression: no_match rows previously had next_retry_at=None, so the
    cooldown filter let them through every scheduler tick. With default
    poll_interval_minutes=5 and max_attempts=3, users saw 3 attempts on the
    same album within ~15 minutes. They should follow the same cooldown
    curve as STATUS_FAILED rows."""
    # Empty search → no_match outcome
    fake = _FakeStreamrip(search_results={"qobuz": [], "tidal": [], "deezer": [], "soundcloud": []})

    result = await lbf._process_album(db_session, _lidarr_album(album_id=8001), cfg, fake)
    await db_session.commit()
    assert result["decision"] == "no_match"

    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 8001))
    ).scalar_one()
    assert row.status == "no_match"
    assert row.attempt_count == 1
    # The fix: next_retry_at MUST be set for no_match so the row isn't
    # immediately re-picked on the next tick.
    assert row.next_retry_at is not None
    assert row.next_retry_at > int(time.time())  # in the future


@pytest.mark.asyncio
async def test_no_match_filter_honours_its_cooldown(db_session, cfg):
    """An in-cooldown no_match row should not be re-picked from Lidarr."""
    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=8002,
            artist="A",
            album_title="T",
            source="missing",
            status="no_match",
            attempt_count=1,
            next_retry_at=now + 3600,  # 1h cooldown still active
            created_at=now - 86400,
            updated_at=now - 60,
        )
    )
    await db_session.commit()
    filtered = await lbf._filter_by_cooldown_and_state(db_session, [_lidarr_album(album_id=8002)], cfg)
    assert filtered == []


@pytest.mark.asyncio
async def test_no_match_existing_row_gets_cooldown_on_retry(db_session, cfg):
    """When an existing no_match row (next_retry_at=None from old code) is
    re-picked and stays no_match, the new code sets a cooldown going
    forward — so the third retry doesn't fire on the very next tick."""
    now = int(time.time())
    db_session.add(
        LidarrBackfillRequest(
            lidarr_album_id=8003,
            artist="A",
            album_title="T",
            source="missing",
            status="no_match",
            attempt_count=1,
            next_retry_at=None,  # legacy row from the buggy code path
            created_at=now - 86400,
            updated_at=now - 86400,
        )
    )
    await db_session.commit()

    fake = _FakeStreamrip(search_results={"qobuz": [], "tidal": [], "deezer": [], "soundcloud": []})
    await lbf._process_album(db_session, _lidarr_album(album_id=8003), cfg, fake)
    await db_session.commit()

    row = (
        await db_session.execute(select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 8003))
    ).scalar_one()
    assert row.attempt_count == 2  # bumped
    assert row.next_retry_at is not None  # cooldown now applied
    assert row.next_retry_at > int(time.time())


@pytest.mark.asyncio
async def test_tick_in_progress_flag_lifecycle():
    """The flag flips on inside the context, off on exit, even on exception."""
    assert lbf.is_tick_in_progress() is False
    assert lbf.get_tick_started_at() is None

    async with lbf._mark_tick_running():
        assert lbf.is_tick_in_progress() is True
        assert lbf.get_tick_started_at() is not None

    assert lbf.is_tick_in_progress() is False
    assert lbf.get_tick_started_at() is None

    # And clears on exception.
    with pytest.raises(RuntimeError):
        async with lbf._mark_tick_running():
            assert lbf.is_tick_in_progress() is True
            raise RuntimeError("boom")
    assert lbf.is_tick_in_progress() is False
    assert lbf.get_tick_started_at() is None


@pytest.mark.asyncio
async def test_fetch_lidarr_totals_cached_single_flights_concurrent_calls(monkeypatch):
    """50 concurrent cache misses must collapse into one upstream Lidarr fetch.

    Regression test: without an asyncio.Lock around the cache check, a
    thundering herd of dashboard polls each holds a DB connection while
    waiting on a slow Lidarr, exhausting the SQLAlchemy pool and crashing
    the active library scan.
    """
    import asyncio as _asyncio

    # Function makes one HTTP call per endpoint (missing + cutoff) per cache
    # refresh, so a single leader produces two upstream calls. Without
    # single-flight, 50 concurrent leaders would produce 100.
    missing_calls = 0
    cutoff_calls = 0
    started = _asyncio.Event()
    can_finish = _asyncio.Event()

    class _SlowResp:
        def __init__(self, total):
            self._total = total

        def raise_for_status(self):
            return None

        def json(self):
            return {"totalRecords": self._total}

    class _SlowHttpClient:
        async def get(self, url, params=None, timeout=None):  # noqa: ASYNC109 — mirrors httpx.AsyncClient.get
            nonlocal missing_calls, cutoff_calls
            if "missing" in url:
                missing_calls += 1
            else:
                cutoff_calls += 1
            started.set()
            await can_finish.wait()
            return _SlowResp(7 if "missing" in url else 3)

    class _FakeLidarrClient:
        def __init__(self, *a, **kw):
            self._client = _SlowHttpClient()
            self._base_url = "http://lidarr.test"

        async def close(self):
            pass

    monkeypatch.setattr(lbf, "LidarrClient", _FakeLidarrClient)
    monkeypatch.setattr(lbf.settings, "LIDARR_URL", "http://lidarr.test")
    monkeypatch.setattr(lbf.settings, "LIDARR_API_KEY", "test-key")
    # Cold the cache so all callers must fetch.
    lbf._lidarr_totals_cache = (0.0, lbf.LidarrTotals(None, None, True))

    cfg = get_defaults()
    # /wanted/cutoff is only polled when the engine is configured to drain it.
    cfg.sources.cutoff_unmet = True

    # Fire 50 concurrent callers; without single-flight every one would
    # call the upstream client.
    tasks = [_asyncio.create_task(lbf._fetch_lidarr_totals_cached(cfg)) for _ in range(50)]

    # Let the leader actually start its upstream fetch, then release it.
    await started.wait()
    can_finish.set()

    results = await _asyncio.gather(*tasks)

    assert missing_calls == 1, f"expected 1 /missing call, got {missing_calls}"
    assert cutoff_calls == 1, f"expected 1 /cutoff call, got {cutoff_calls}"
    assert all(r.missing == 7 and r.cutoff == 3 and r.reachable for r in results)


@pytest.mark.asyncio
async def test_fetch_lidarr_totals_cached_connect_error_marks_unreachable(monkeypatch):
    """Issue #97: a connect error must mark reachable=False and return fast.

    Previously the dashboard's /v1/lidarr-backfill/stats endpoint hung
    for ~60 s when Lidarr was unreachable, blanking the Active Jobs tile.
    Now upstream errors are caught with a per-call timeout and surfaced
    via `reachable=False`.
    """
    import asyncio as _asyncio
    import time as _time

    import httpx

    class _ErroringHttpClient:
        async def get(self, url, params=None, timeout=None):  # noqa: ASYNC109 — mirrors httpx.AsyncClient.get
            raise httpx.ConnectError("connection refused")

    class _FakeLidarrClient:
        def __init__(self, *a, **kw):
            self._client = _ErroringHttpClient()
            self._base_url = "http://lidarr.test"

        async def close(self):
            pass

    monkeypatch.setattr(lbf, "LidarrClient", _FakeLidarrClient)
    monkeypatch.setattr(lbf.settings, "LIDARR_URL", "http://lidarr.test")
    monkeypatch.setattr(lbf.settings, "LIDARR_API_KEY", "test-key")
    lbf._lidarr_totals_cache = (0.0, lbf.LidarrTotals(None, None, True))

    cfg = get_defaults()
    started = _time.monotonic()
    result = await _asyncio.wait_for(lbf._fetch_lidarr_totals_cached(cfg), timeout=5.0)
    elapsed = _time.monotonic() - started

    assert result.missing is None
    assert result.cutoff is None
    assert result.reachable is False
    # Two connect-errors raised synchronously by the fake; should return
    # well under the per-call timeout, let alone the 60 s the bug exhibited.
    assert elapsed < 1.0, f"unexpectedly slow: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_fetch_lidarr_totals_cached_lock_acquire_timeout(monkeypatch):
    """A wedged single-flight leader must not block followers indefinitely.

    Followers that can't acquire the lock within
    `_LIDARR_LOCK_ACQUIRE_TIMEOUT_S` return the last cached values plus
    `reachable=False` instead of piling up.
    """
    import asyncio as _asyncio

    # Pre-populate cache with a "stale" entry that's older than the TTL
    # so callers attempt a refresh.
    cached = lbf.LidarrTotals(missing=42, cutoff=7, reachable=True)
    lbf._lidarr_totals_cache = (-9999.0, cached)

    monkeypatch.setattr(lbf, "_LIDARR_LOCK_ACQUIRE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(lbf.settings, "LIDARR_URL", "http://lidarr.test")
    monkeypatch.setattr(lbf.settings, "LIDARR_API_KEY", "test-key")
    # Re-bind the lock to this test's event loop (pytest-asyncio runs each
    # test in a fresh loop; the module-level Lock keeps a reference to the
    # first loop it ever saw).
    monkeypatch.setattr(lbf, "_lidarr_totals_lock", _asyncio.Lock())

    # Hold the lock from another task so the follower can't acquire it.
    holder_release = _asyncio.Event()

    async def _hold_lock():
        async with lbf._lidarr_totals_lock:
            await holder_release.wait()

    holder = _asyncio.create_task(_hold_lock())
    # Yield once so the holder starts and acquires the lock.
    await _asyncio.sleep(0)

    cfg = get_defaults()
    try:
        result = await _asyncio.wait_for(lbf._fetch_lidarr_totals_cached(cfg), timeout=2.0)
    finally:
        holder_release.set()
        await holder

    # Returns last cached values but flips reachable to False so the UI
    # can render a degraded state.
    assert result.missing == 42
    assert result.cutoff == 7
    assert result.reachable is False
