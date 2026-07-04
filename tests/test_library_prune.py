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
from app.models.db import Base, ListenEvent, TrackFeatures, TrackInteraction
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


async def _add_track(s, tid, file_path, missing_since=None):
    s.add(
        TrackFeatures(
            track_id=tid,
            file_path=file_path,
            title=tid,
            missing_since=missing_since,
            analyzed_at=_now(),
            analysis_version="1",
        )
    )


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
    monkeypatch.setattr(settings, "SCANNER_PRUNE_GRACE_HOURS", 24, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_MAX_FRACTION", 1.0, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_DELETE_HISTORY", False, raising=False)


# Orphans are seeded already tombstoned in the past (well beyond any grace window)
# so a single prune deletes them; the tombstone / grace / reappear paths seed a
# fresh (missing_since=None) or custom value instead.
_LONG_AGO = 1_000_000  # seconds before now


async def _seed_present_and_orphans(tmp_path, n_present=3, n_orphans=4, orphan_missing_since="old"):
    """Create n_present real files (present) + n_orphans rows pointing at missing
    paths. Orphans are pre-tombstoned in the past by default so one prune deletes
    them; pass orphan_missing_since=None for the fresh (just-vanished) case.
    Returns present_paths."""
    ms = (_now() - _LONG_AGO) if orphan_missing_since == "old" else orphan_missing_since
    present_paths = set()
    async with _Session() as s:
        for i in range(n_present):
            f = tmp_path / f"present_{i}.mp3"
            f.write_bytes(b"x")
            await _add_track(s, f"present{i}", str(f))
            present_paths.add(str(f))
        for i in range(n_orphans):
            await _add_track(s, f"orphan{i}", f"/gone/orphan_{i}.mp3", missing_since=ms)
            await _add_history(s, f"orphan{i}")
        await s.commit()
    return present_paths


async def _missing_since(track_id):
    async with _Session() as s:
        return (
            await s.execute(select(TrackFeatures.missing_since).where(TrackFeatures.track_id == track_id))
        ).scalar_one_or_none()


async def test_phase_a2_report_only_deletes_nothing(tmp_path, monkeypatch, _patch_scanner_session, _prune_settings):
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", False, raising=False)
    present_paths = await _seed_present_and_orphans(tmp_path)  # orphans tombstoned past grace

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, inter, ev = await _counts(s)
    assert tf == 7, "report-only must not delete even grace-expired orphans"
    assert inter == 4 and ev == 4


async def test_phase_a2_deletes_grace_expired_keeps_present_and_history(
    tmp_path, monkeypatch, _patch_scanner_session, _prune_settings
):
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    present_paths = await _seed_present_and_orphans(tmp_path)  # orphans tombstoned past grace

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, inter, ev = await _counts(s)
        remaining = {r[0] for r in (await s.execute(select(TrackFeatures.track_id))).all()}
    assert tf == 3, "only the 3 present rows survive"
    assert remaining == {"present0", "present1", "present2"}
    assert inter == 4 and ev == 4, "history preserved (delete_history defaults False)"


async def test_phase_a2_newly_missing_is_tombstoned_not_deleted(
    tmp_path, monkeypatch, _patch_scanner_session, _prune_settings
):
    """First time a file is seen gone it is tombstoned, never deleted the same scan
    — the grace clock must start before anything is removed."""
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    present_paths = await _seed_present_and_orphans(tmp_path, orphan_missing_since=None)  # FRESH

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, _, _ = await _counts(s)
    assert tf == 7, "newly-missing rows are tombstoned, not deleted"
    assert await _missing_since("orphan0") is not None, "the file's missing_since was stamped"


async def test_phase_a2_deletes_after_grace_on_second_scan(
    tmp_path, monkeypatch, _patch_scanner_session, _prune_settings
):
    """Fresh orphans: scan 1 tombstones, scan 2 (now past grace) deletes."""
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_GRACE_HOURS", 0, raising=False)  # any prior tombstone is past grace
    present_paths = await _seed_present_and_orphans(tmp_path, orphan_missing_since=None)  # FRESH

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))
    async with _Session() as s:
        tf_after_1, _, _ = await _counts(s)
    await scanner._prune_orphans(scan_id=2, present_paths=present_paths, found_count=len(present_paths))
    async with _Session() as s:
        tf_after_2, _, _ = await _counts(s)

    assert tf_after_1 == 7, "scan 1 only tombstones"
    assert tf_after_2 == 3, "scan 2 deletes the now-grace-expired orphans"


async def test_phase_a2_reappeared_file_clears_tombstone(
    tmp_path, monkeypatch, _patch_scanner_session, _prune_settings
):
    """A tombstoned row whose file is back on disk clears its tombstone and is NOT
    deleted — the transient/partial-mount safety valve."""
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    back = tmp_path / "came_back.mp3"
    back.write_bytes(b"x")
    async with _Session() as s:
        await _add_track(s, "back", str(back), missing_since=_now() - _LONG_AGO)  # tombstoned but present
        await s.commit()

    await scanner._prune_orphans(scan_id=1, present_paths={str(back)}, found_count=1)

    async with _Session() as s:
        remaining = {r[0] for r in (await s.execute(select(TrackFeatures.track_id))).all()}
    assert "back" in remaining, "a reappeared file must not be deleted"
    assert await _missing_since("back") is None, "its tombstone was cleared"


async def test_phase_a2_restat_keeps_present_file_missed_by_walk(
    tmp_path, monkeypatch, _patch_scanner_session, _prune_settings
):
    """A row whose path the walk did NOT yield is only a *candidate*; if the file
    still exists on disk the re-stat keeps it (set-diff is a prefilter, not truth)."""
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    moved = tmp_path / "exists_but_unwalked.mp3"
    moved.write_bytes(b"x")
    (tmp_path / "present.mp3").write_bytes(b"x")
    async with _Session() as s:
        await _add_track(s, "present", str(tmp_path / "present.mp3"))
        await _add_track(s, "still_here", str(moved))  # exists on disk, but NOT in present_paths
        await _add_track(s, "real_orphan", "/gone/x.mp3", missing_since=_now() - _LONG_AGO)  # tombstoned past grace
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


async def test_phase_a2_per_scan_cap_drains_over_scans(tmp_path, monkeypatch, _patch_scanner_session, _prune_settings):
    """A large grace-expired deletion is not hard-blocked; it drains at most
    MAX_FRACTION of the table per scan."""
    monkeypatch.setattr(settings, "SCANNER_AUTO_PRUNE", True, raising=False)
    monkeypatch.setattr(settings, "SCANNER_PRUNE_MAX_FRACTION", 0.25, raising=False)
    # 2 present + 8 orphans (tombstoned past grace) = 10 rows → cap = int(10*0.25) = 2/scan.
    present_paths = await _seed_present_and_orphans(tmp_path, n_present=2, n_orphans=8)

    await scanner._prune_orphans(scan_id=1, present_paths=present_paths, found_count=len(present_paths))

    async with _Session() as s:
        tf, _, _ = await _counts(s)
    assert tf == 8, "only 25% of 10 rows (=2) deleted this scan; the rest drain next scan"
