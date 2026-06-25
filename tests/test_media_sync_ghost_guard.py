"""
GrooveIQ — Tests for the media-sync file-existence guard.

A "ghost" row (its file deleted from disk) must not steal a ``media_server_id``
that a present-file row owns. Without the guard, the matcher's bulk-NULL
(step 6) strips the present row and hands the slot to the ghost, so plays keyed
on that server_id resolve to a track the user can no longer play.

These use REAL temp files so the on-disk presence check is meaningful; the
matcher itself is driven via the (artist, album, title) path with fetch_tracks
mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures
from app.services.media_server import MediaServerTrack, sync_track_ids

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)

# Shared (artist, album, title) so every local row + the server track resolve to
# each other via the AATD matcher.
_META = dict(artist="Artist", album="Album", title="Song")


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _configure(mock_settings, *, guard: bool = True):
    mock_settings.MEDIA_SERVER_TYPE = "navidrome"
    mock_settings.MEDIA_SERVER_MUSIC_PATH = "/music"
    mock_settings.MUSIC_LIBRARY_PATH = "/music"
    mock_settings.MEDIA_SYNC_PRESENCE_GUARD = guard


def _server(sid: str) -> MediaServerTrack:
    return MediaServerTrack(server_id=sid, title="Song", artist="Artist", album="Album", file_path="/music/song.flac")


async def _link_map() -> dict[str, str | None]:
    async with _Session() as s:
        rows = (await s.execute(select(TrackFeatures))).scalars().all()
    return {r.track_id: r.media_server_id for r in rows}


@patch("app.services.media_server.settings")
async def test_ghost_does_not_steal_present_holders_slot(mock_settings, tmp_path):
    _configure(mock_settings, guard=True)
    present_file = tmp_path / "present.flac"
    present_file.write_bytes(b"x")
    async with _Session() as s:
        # Present row already linked to nav-X; ghost row (file gone) also matches nav-X.
        s.add(TrackFeatures(track_id="present", file_path=str(present_file), media_server_id="nav-X", **_META))
        s.add(TrackFeatures(track_id="ghost", file_path="/gone/ghost.flac", **_META))
        await s.commit()

    with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=[_server("nav-X")]):
        async with _Session() as s:
            result = await sync_track_ids(s)

    assert result.tracks_ghost_link_skipped == 1
    assert result.media_server_id_updated == 0
    links = await _link_map()
    assert links["present"] == "nav-X", "present row must keep its link"
    assert links["ghost"] is None, "ghost must not acquire the slot"


@patch("app.services.media_server.settings")
async def test_without_guard_the_ghost_steals_the_slot(mock_settings, tmp_path):
    """Documents the bug the guard fixes: disabled, the missing-file row robs the
    present row of its server_id via the step-6 bulk-NULL."""
    _configure(mock_settings, guard=False)
    present_file = tmp_path / "present.flac"
    present_file.write_bytes(b"x")
    async with _Session() as s:
        s.add(TrackFeatures(track_id="present", file_path=str(present_file), media_server_id="nav-X", **_META))
        s.add(TrackFeatures(track_id="ghost", file_path="/gone/ghost.flac", **_META))
        await s.commit()

    with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=[_server("nav-X")]):
        async with _Session() as s:
            result = await sync_track_ids(s)

    assert result.tracks_ghost_link_skipped == 0
    links = await _link_map()
    assert links["present"] is None, "without the guard the present row is robbed"
    assert links["ghost"] == "nav-X"


@patch("app.services.media_server.settings")
async def test_dedup_prefers_present_row_over_ghost(mock_settings, tmp_path):
    """When a present row and a ghost both newly claim the same server_id, the
    present row wins the unique slot — even though the ghost has the lower tf_id
    (which would win the pre-guard (priority, tf_id) tiebreak)."""
    _configure(mock_settings, guard=True)
    present_file = tmp_path / "present.flac"
    present_file.write_bytes(b"x")
    async with _Session() as s:
        s.add(TrackFeatures(track_id="ghost", file_path="/gone/ghost.flac", **_META))  # lower id
        s.add(TrackFeatures(track_id="present", file_path=str(present_file), **_META))
        await s.commit()

    with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=[_server("nav-Y")]):
        async with _Session() as s:
            result = await sync_track_ids(s)

    assert result.tracks_duplicate_local == 1
    links = await _link_map()
    assert links["present"] == "nav-Y", "present row wins the unique slot"
    assert links["ghost"] is None


@patch("app.services.media_server.settings")
async def test_circuit_breaker_disables_guard_when_mount_looks_gone(mock_settings, tmp_path):
    """If a meaningful sample of currently-linked rows looks missing (mount blip),
    the guard self-disables and falls back to the original matcher behaviour
    rather than acting on untrustworthy presence data."""
    _configure(mock_settings, guard=True)
    present_file = tmp_path / "present.flac"
    present_file.write_bytes(b"x")
    async with _Session() as s:
        # 60 currently-linked rows whose files are all "missing" → linked
        # present-rate ≈ 1.6% (< 50%) → breaker trips. They don't match the
        # server track (different metadata), so they only populate the sample.
        for i in range(60):
            s.add(
                TrackFeatures(
                    track_id=f"linked{i}",
                    file_path=f"/gone/l{i}.flac",
                    media_server_id=f"nav-L{i}",
                    title=f"other{i}",
                    artist="zzz",
                    album="zzz",
                )
            )
        s.add(TrackFeatures(track_id="present", file_path=str(present_file), media_server_id="nav-X", **_META))
        s.add(TrackFeatures(track_id="ghost", file_path="/gone/ghost.flac", **_META))
        await s.commit()

    with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=[_server("nav-X")]):
        async with _Session() as s:
            result = await sync_track_ids(s)

    assert result.tracks_ghost_link_skipped == 0, "guard must be disabled under a suspected mount blip"
    links = await _link_map()
    # Fallback = original (unsafe) behaviour: better than mass-mangling on bad data.
    assert links["ghost"] == "nav-X"
    assert links["present"] is None
