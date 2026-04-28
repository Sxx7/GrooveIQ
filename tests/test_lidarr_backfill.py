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


# ---------------------------------------------------------------------------
# Process album: dry_run, downloading, no_match
# ---------------------------------------------------------------------------


class _FakeStreamrip:
    """In-memory stand-in for StreamripClient. Records calls for assertions."""

    def __init__(self, search_results=None, download_result=None):
        self.search_results = search_results if search_results is not None else []
        self.download_result = download_result or {"task_id": "task-123", "status": "downloading"}
        self.search_calls: list[tuple[str, int, str | None]] = []
        self.download_calls: list[tuple[str, str]] = []

    async def search(self, query, limit=10, service=None):
        self.search_calls.append((query, limit, service))
        # Return per-service hits if a dict was provided, else flat list
        if isinstance(self.search_results, dict):
            return self.search_results.get(service or "qobuz", [])
        return self.search_results

    async def download_album(self, service, album_id):
        self.download_calls.append((service, album_id))
        return self.download_result


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
        await db_session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4001)
        )
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
        await db_session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4002)
        )
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
        await db_session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 4003)
        )
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
# Tick-level: enabled=false / not configured short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tick_skipped_when_disabled(db_session, monkeypatch):
    # Default config has enabled=False. No need to mock anything else; the
    # function returns immediately before touching Lidarr/streamrip.
    summary = await lbf.run_backfill_tick(db_session)
    assert summary == {"skipped": "disabled"}


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
        await db_session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.id == row.id)
        )
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
        await db_session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.id == row.id)
        )
    ).scalar_one()
    assert refreshed.status == "permanently_skipped"


@pytest.mark.asyncio
async def test_reset_backfill_state_scope_failed(db_session):
    now = int(time.time())
    db_session.add_all(
        [
            LidarrBackfillRequest(
                lidarr_album_id=6001, artist="A", album_title="X", source="missing",
                status="failed", created_at=now, updated_at=now,
            ),
            LidarrBackfillRequest(
                lidarr_album_id=6002, artist="A", album_title="Y", source="missing",
                status="complete", created_at=now, updated_at=now,
            ),
            LidarrBackfillRequest(
                lidarr_album_id=6003, artist="A", album_title="Z", source="missing",
                status="failed", created_at=now, updated_at=now,
            ),
        ]
    )
    await db_session.commit()

    deleted = await lbf.reset_backfill_state(db_session, "failed")
    await db_session.commit()
    assert deleted == 2

    remaining = (
        await db_session.execute(select(LidarrBackfillRequest).order_by(LidarrBackfillRequest.lidarr_album_id))
    ).scalars().all()
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

    match = await lbf._find_streamrip_album(
        fake,
        _lidarr_album(),
        cfg,
        available_services=["qobuz"],  # only qobuz is configured
    )
    assert match is not None
    assert match.service == "qobuz"
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
        await db_session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 8001)
        )
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
        await db_session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == 8003)
        )
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
