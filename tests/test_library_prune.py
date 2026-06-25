"""
GrooveIQ — Tests for the orphan-row prune: the shared helper and the scanner's
post-scan Phase A2.

Covers the safety contract that matters for an unattended 6h cadence:
  * the helper preserves play history unless explicitly asked to delete it,
  * Phase A2 defaults to REPORT-ONLY (deletes nothing),
  * every guard (empty walk, file floor, prior-scan delta, fraction cap) aborts
    the phase, and
  * the set-diff is only a prefilter — a candidate whose file still exists on
    disk is re-stat'd and kept.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.workers.library_scanner as scanner
from app.core.config import settings
from app.models.db import Base, LibraryScanState, ListenEvent, TrackFeatures, TrackInteraction
from app.services.library_prune import prune_orphan_track_features

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


def _now() -> int:
    return int(time.time())


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _add_track(s, tid, file_path):
    s.add(TrackFeatures(track_id=tid, file_path=file_path, title=tid, analyzed_at=_now(), analysis_version="1"))


async def _add_history(s, tid):
    s.add(TrackInteraction(track_id=tid, user_id="u", play_count=3, last_played_at=_now(), updated_at=_now()))
    s.add(ListenEvent(track_id=tid, user_id="u", event_type="play_start", timestamp=_now()))


async def _counts(s):
    tf = (await s.execute(select(func.count()).select_from(TrackFeatures))).scalar()
    inter = (await s.execute(select(func.count()).select_from(TrackInteraction))).scalar()
    ev = (await s.execute(select(func.count()).select_from(ListenEvent))).scalar()
    return tf, inter, ev


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


async def test_helper_preserves_history_by_default():
    async with _Session() as s:
        await _add_track(s, "orphan", "/gone/a.mp3")
        await _add_history(s, "orphan")
        await s.commit()
        tf_id = (await s.execute(select(TrackFeatures.id).where(TrackFeatures.track_id == "orphan"))).scalar_one()

        counts = await prune_orphan_track_features(s, [(tf_id, "orphan")], delete_history=False)

        tf, inter, ev = await _counts(s)
    assert counts["deleted_track_features"] == 1
    assert tf == 0, "the orphan feature row is gone"
    assert inter == 1 and ev == 1, "history is preserved when delete_history=False"


async def test_helper_deletes_history_when_requested():
    async with _Session() as s:
        await _add_track(s, "orphan", "/gone/a.mp3")
        await _add_history(s, "orphan")
        await s.commit()
        tf_id = (await s.execute(select(TrackFeatures.id).where(TrackFeatures.track_id == "orphan"))).scalar_one()

        counts = await prune_orphan_track_features(s, [(tf_id, "orphan")], delete_history=True)

        tf, inter, ev = await _counts(s)
    assert (tf, inter, ev) == (0, 0, 0)
    assert counts == {"deleted_track_features": 1, "deleted_interactions": 1, "deleted_events": 1}


async def test_helper_chunks_all_rows():
    async with _Session() as s:
        for i in range(23):
            await _add_track(s, f"o{i:02d}", f"/gone/{i}.mp3")
        await s.commit()
        orphans = [
            (r.id, r.track_id) for r in (await s.execute(select(TrackFeatures.id, TrackFeatures.track_id))).all()
        ]

        counts = await prune_orphan_track_features(s, orphans, delete_history=False, chunk_size=5)

        tf, _, _ = await _counts(s)
    assert counts["deleted_track_features"] == 23
    assert tf == 0


# ---------------------------------------------------------------------------
# Phase A2 — _prune_orphans
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_scanner_session(monkeypatch):
    """Point the scanner's AsyncSessionLocal at the in-memory test engine."""
    monkeypatch.setattr(scanner, "AsyncSessionLocal", _Session)


@pytest.fixture
def _prune_settings(monkeypatch):
    """Permissive guard thresholds so individual tests can flip the one they exercise."""
    monkeypatch.setattr(settings, "SCANNER_PRUNE_MIN_FILES", 1, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_MAX_DROP", 0.99, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_MAX_FRACTION", 1.0, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_DELETE_HISTORY", False, raising=False)


async def _seed_present_and_orphans(tmp_path, n_present=3, n_orphans=4):
    """Create n_present real files (present) + n_orphans rows pointing at missing
    paths. Returns (present_paths, present_ids, orphan_ids)."""
    present_paths = set()
    async with _Session() as s:
        for i in range(n_present):
            f = tmp_path / f"present_{i}.mp3"
            f.write_bytes(b"x")
            await _add_track(s, f"present{i}", str(f))
            present_paths.add(str(f))
        for i in range(n_orphans):
            await _add_track(s, f"orphan{i}", f"/gone/orphan_{i}.mp3")
            await _add_history(s, f"orphan{i}")
        await s.commit()
    return present_paths


async def test_phase_a2_report_only_deletes_nothing(tmp_path, monkeypatch, _patch_scanner_session, _prune_settings):
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", False, raising=False)
    present_paths = await _seed_present_and_orphans(tmp_path)

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, inter, ev = await _counts(s)
    assert tf == 7, "report-only must not delete (3 present + 4 orphan rows remain)"
    assert inter == 4 and ev == 4


async def test_phase_a2_deletes_orphans_keeps_present_and_history(
    tmp_path, monkeypatch, _patch_scanner_session, _prune_settings
):
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    present_paths = await _seed_present_and_orphans(tmp_path)

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, inter, ev = await _counts(s)
        remaining = {r[0] for r in (await s.execute(select(TrackFeatures.track_id))).all()}
    assert tf == 3, "only the 3 present rows survive"
    assert remaining == {"present0", "present1", "present2"}
    assert inter == 4 and ev == 4, "history preserved (delete_history defaults False)"


async def test_phase_a2_restat_keeps_present_file_missed_by_walk(
    tmp_path, monkeypatch, _patch_scanner_session, _prune_settings
):
    """A row whose path the walk did NOT yield is only a *candidate*; if the file
    still exists on disk the re-stat keeps it (set-diff is a prefilter, not truth)."""
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    moved = tmp_path / "exists_but_unwalked.mp3"
    moved.write_bytes(b"x")
    async with _Session() as s:
        await _add_track(s, "present", str(tmp_path / "present.mp3"))
        (tmp_path / "present.mp3").write_bytes(b"x")
        await _add_track(s, "still_here", str(moved))  # exists on disk, but NOT in present_paths
        await _add_track(s, "real_orphan", "/gone/x.mp3")
        await s.commit()

    present_paths = {str(tmp_path / "present.mp3")}  # deliberately omits `moved`

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=5)

    async with _Session() as s:
        remaining = {r[0] for r in (await s.execute(select(TrackFeatures.track_id))).all()}
    assert "still_here" in remaining, "re-stat must rescue a present file the walk missed"
    assert "real_orphan" not in remaining
    assert "present" in remaining


async def test_phase_a2_empty_walk_aborts(tmp_path, monkeypatch, _patch_scanner_session, _prune_settings):
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    await _seed_present_and_orphans(tmp_path)

    await scanner._prune_orphans(scan_id=1, present_paths=set(), found_count=0)

    async with _Session() as s:
        tf, _, _ = await _counts(s)
    assert tf == 7, "empty walk (mount lost) must abort — nothing deleted"


async def test_phase_a2_file_floor_aborts(tmp_path, monkeypatch, _patch_scanner_session, _prune_settings):
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_MIN_FILES", 1000, raising=False)
    present_paths = await _seed_present_and_orphans(tmp_path)

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, _, _ = await _counts(s)
    assert tf == 7, "walk below the file floor must abort"


async def test_phase_a2_fraction_cap_aborts(tmp_path, monkeypatch, _patch_scanner_session, _prune_settings):
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_MAX_FRACTION", 0.25, raising=False)
    # 3 present + 4 orphans → 4/7 = 57% confirmed > 25% cap → abort.
    present_paths = await _seed_present_and_orphans(tmp_path, n_present=3, n_orphans=4)

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, _, _ = await _counts(s)
    assert tf == 7, "confirmed orphans over the fraction cap must abort"


async def test_phase_a2_prior_scan_delta_aborts(tmp_path, monkeypatch, _patch_scanner_session, _prune_settings):
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_MAX_DROP", 0.10, raising=False)
    present_paths = await _seed_present_and_orphans(tmp_path)
    # A prior completed scan saw 1000 files; this scan sees only len(present_paths) → huge drop → abort.
    async with _Session() as s:
        s.add(LibraryScanState(scan_started_at=_now(), status="completed", files_found=1000))
        await s.commit()

    await scanner._prune_orphans(scan_id=99, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, _, _ = await _counts(s)
    assert tf == 7, "a sharp files_found drop vs the last scan must abort"
