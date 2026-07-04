"""
GrooveIQ — Tests for scanner move reconciliation (beets move/retag handling).

A file moved/retagged by beets arrives at a new path, so ``generate_track_id``
(path-derived) mints a new id and the scanner would insert a duplicate row while
the original row's listening history strands on the orphaned old id. These tests
cover the fix: when a newly-seen file carries a MusicBrainz id matching an
existing row whose file has vanished, the row is repointed and its history
migrated onto the new id — no duplicate, no stranded history.
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.workers.library_scanner as scanner
from app.core.config import settings
from app.models.db import Base, ListenEvent, TrackFeatures, TrackInteraction
from app.services.audio_analysis import generate_track_id
from app.services.library_reconcile import find_moved_row, migrate_track_id

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)

_GONE = "/gone/old-location/track.flac"  # never exists on disk
_NEW = "/music/Artist/Album/01 Track.flac"  # the moved-to path (need not exist)
_MBID = "11111111-2222-3333-4444-555555555555"


def _now() -> int:
    return int(time.time())


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _add_track(s, tid, file_path, mbid=None):
    s.add(
        TrackFeatures(
            track_id=tid,
            file_path=file_path,
            title=tid,
            musicbrainz_track_id=mbid,
            analyzed_at=_now(),
            analysis_version="1",
        )
    )


async def _add_history(s, tid):
    s.add(TrackInteraction(track_id=tid, user_id="u", play_count=7, last_played_at=_now(), updated_at=_now()))
    s.add(ListenEvent(track_id=tid, user_id="u", event_type="play_start", timestamp=_now()))


def _upsert_data(file_path, mbid):
    return {
        "file_path": file_path,
        "musicbrainz_track_id": mbid,
        "file_hash": "deadbeef",
        "title": "Track",
        "analyzed_at": _now(),
        "analysis_version": "1",
    }


async def _counts(s):
    tf = (await s.execute(select(func.count()).select_from(TrackFeatures))).scalar()
    inter = (await s.execute(select(func.count()).select_from(TrackInteraction))).scalar()
    ev = (await s.execute(select(func.count()).select_from(ListenEvent))).scalar()
    return tf, inter, ev


# ---------------------------------------------------------------------------
# migrate_track_id helper
# ---------------------------------------------------------------------------


async def test_migrate_moves_history_and_reports_all_tables():
    async with _Session() as s:
        await _add_history(s, "old")
        await s.commit()

        counts = await migrate_track_id(s, "old", "new")
        await s.commit()

        moved_inter = (
            await s.execute(
                select(func.count()).select_from(TrackInteraction).where(TrackInteraction.track_id == "new")
            )
        ).scalar()
        stale_ev = (
            await s.execute(select(func.count()).select_from(ListenEvent).where(ListenEvent.track_id == "old"))
        ).scalar()

    assert moved_inter == 1 and stale_ev == 0, "history rows were repointed old -> new"
    # Every history table is exercised (0 for the unseeded ones) so a bad column
    # name / model would surface here rather than silently no-op in production.
    assert set(counts) == {"listen_events", "track_interactions", "playlist_tracks", "mix_tracks", "scrobble_queue"}
    assert counts["listen_events"] == 1 and counts["track_interactions"] == 1


# ---------------------------------------------------------------------------
# find_moved_row guards
# ---------------------------------------------------------------------------


async def test_find_moved_row_none_without_mbid():
    async with _Session() as s:
        assert await find_moved_row(s, None, "new") is None
        assert await find_moved_row(s, "", "new") is None


async def test_find_moved_row_skips_present_file(tmp_path):
    present = tmp_path / "still-here.flac"
    present.write_bytes(b"x")
    async with _Session() as s:
        await _add_track(s, "dup", str(present), mbid=_MBID)
        await s.commit()
        # Old file still exists -> genuine duplicate, not a move.
        assert await find_moved_row(s, _MBID, "new") is None


async def test_find_moved_row_ambiguous_returns_none():
    async with _Session() as s:
        await _add_track(s, "goneA", "/gone/a.flac", mbid=_MBID)
        await _add_track(s, "goneB", "/gone/b.flac", mbid=_MBID)
        await s.commit()
        # Two vanished rows share the MBID -> refuse to pick, don't collapse them.
        assert await find_moved_row(s, _MBID, "new") is None


# ---------------------------------------------------------------------------
# _upsert_track_features integration
# ---------------------------------------------------------------------------


async def test_upsert_reconciles_move_no_duplicate_history_follows():
    new_tid = generate_track_id(_NEW)
    async with _Session() as s:
        await _add_track(s, generate_track_id(_GONE), _GONE, mbid=_MBID)
        await _add_history(s, generate_track_id(_GONE))
        await s.commit()

        await scanner._upsert_track_features(s, _upsert_data(_NEW, _MBID))
        await s.commit()

        tf, inter, ev = await _counts(s)
        row = (await s.execute(select(TrackFeatures))).scalar_one()
        new_ev = (
            await s.execute(select(func.count()).select_from(ListenEvent).where(ListenEvent.track_id == new_tid))
        ).scalar()

    assert tf == 1, "the row was repointed in place, not duplicated"
    assert row.track_id == new_tid and row.file_path == _NEW
    assert inter == 1 and ev == 1, "history preserved (not stranded, not duplicated)"
    assert new_ev == 1, "the listen event now carries the new track_id"


async def test_upsert_no_mbid_inserts_new_row():
    async with _Session() as s:
        await _add_track(s, generate_track_id(_GONE), _GONE, mbid=None)
        await _add_history(s, generate_track_id(_GONE))
        await s.commit()

        await scanner._upsert_track_features(s, _upsert_data(_NEW, None))
        await s.commit()

        tf, _, _ = await _counts(s)
    assert tf == 2, "without an MBID there is nothing to reconcile against -> new row"


async def test_upsert_present_old_file_is_a_duplicate_not_a_move(tmp_path):
    present = tmp_path / "original.flac"
    present.write_bytes(b"x")
    async with _Session() as s:
        await _add_track(s, generate_track_id(str(present)), str(present), mbid=_MBID)
        await s.commit()

        await scanner._upsert_track_features(s, _upsert_data(_NEW, _MBID))
        await s.commit()

        tf, _, _ = await _counts(s)
    assert tf == 2, "old file still on disk -> genuine duplicate, both rows kept"


async def test_upsert_reconcile_disabled_inserts_new_row(monkeypatch):
    monkeypatch.setattr(settings, "SCANNER_RECONCILE_MOVES", False, raising=False)
    async with _Session() as s:
        await _add_track(s, generate_track_id(_GONE), _GONE, mbid=_MBID)
        await _add_history(s, generate_track_id(_GONE))
        await s.commit()

        await scanner._upsert_track_features(s, _upsert_data(_NEW, _MBID))
        await s.commit()

        tf, _, _ = await _counts(s)
    assert tf == 2, "reconciliation off -> falls back to insert-new (old additive behaviour)"
