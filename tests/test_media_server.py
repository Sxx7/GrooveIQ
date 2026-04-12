"""
GrooveIQ – Tests for media server integration (sync, path matching).

Tests the sync logic without actually calling external APIs by mocking
the fetch_tracks function.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, ListenEvent, TrackFeatures, TrackInteraction, User
from app.services.media_server import (
    MediaServerTrack,
    _normalise_path,
    sync_track_ids,
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
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"}
        if settings.api_keys_list
        else {},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------

class TestPathNormalisation:

    def test_relative_from_root(self):
        assert _normalise_path("/music/Artist/Album/Song.flac", "/music") == "artist/album/song.flac"

    def test_different_roots(self):
        # GrooveIQ and Navidrome see different mount points but same relative path.
        giq = _normalise_path("/music/Artist/Album/Song.flac", "/music")
        nav = _normalise_path("/data/music/Artist/Album/Song.flac", "/data/music")
        assert giq == nav

    def test_case_insensitive(self):
        assert _normalise_path("/Music/ARTIST/Album/Song.FLAC", "/Music") == "artist/album/song.flac"

    def test_trailing_slashes(self):
        assert _normalise_path("/music/Artist/Song.flac", "/music/") == "artist/song.flac"

    def test_empty_path(self):
        assert _normalise_path("", "/music") == ""

    def test_no_root(self):
        assert _normalise_path("Artist/Album/Song.flac", "") == "artist/album/song.flac"

    def test_windows_paths(self):
        assert _normalise_path("C:\\Music\\Artist\\Song.flac", "C:\\Music") == "artist/song.flac"


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

class TestSyncTrackIds:

    async def _seed_tracks(self):
        """Create GrooveIQ tracks with hash-based IDs."""
        async with _TestSession() as session:
            session.add(TrackFeatures(
                track_id="hash_abc123",
                file_path="/music/Artist One/Album A/Track 1.flac",
                bpm=120.0, energy=0.8,
            ))
            session.add(TrackFeatures(
                track_id="hash_def456",
                file_path="/music/Artist Two/Album B/Track 2.mp3",
                bpm=90.0, energy=0.5,
            ))
            # A track with an event and interaction
            session.add(TrackFeatures(
                track_id="hash_ghi789",
                file_path="/music/Artist One/Album A/Track 3.flac",
                bpm=140.0, energy=0.9,
            ))
            session.add(User(user_id="testuser"))
            session.add(ListenEvent(
                user_id="testuser", track_id="hash_ghi789",
                event_type="play_end", value=0.95,
                timestamp=int(time.time()),
            ))
            session.add(TrackInteraction(
                user_id="testuser", track_id="hash_ghi789",
                play_count=5, skip_count=0, like_count=1,
                dislike_count=0, repeat_count=0,
                playlist_add_count=0, queue_add_count=0,
                satisfaction_score=0.9,
                last_event_id=1, updated_at=int(time.time()),
            ))
            await session.commit()

    @patch("app.services.media_server.settings")
    async def test_basic_sync(self, mock_settings):
        """Tracks get their IDs and metadata updated from the media server."""
        mock_settings.MEDIA_SERVER_TYPE = "navidrome"
        mock_settings.MEDIA_SERVER_URL = "http://localhost:4533"
        mock_settings.MEDIA_SERVER_USER = "admin"
        mock_settings.MEDIA_SERVER_PASSWORD = "pass"
        mock_settings.MEDIA_SERVER_TOKEN = ""
        mock_settings.MEDIA_SERVER_LIBRARY_ID = "1"
        mock_settings.MEDIA_SERVER_MUSIC_PATH = "/data/music"
        mock_settings.MUSIC_LIBRARY_PATH = "/music"

        await self._seed_tracks()

        server_tracks = [
            MediaServerTrack(
                server_id="nav-uuid-001",
                title="Track One",
                artist="Artist One",
                album="Album A",
                file_path="/data/music/Artist One/Album A/Track 1.flac",
            ),
            MediaServerTrack(
                server_id="nav-uuid-002",
                title="Track Two",
                artist="Artist Two",
                album="Album B",
                file_path="/data/music/Artist Two/Album B/Track 2.mp3",
            ),
        ]

        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_fetched == 2
        assert result.tracks_matched == 2
        assert result.tracks_updated == 2
        assert result.tracks_metadata == 2

        # Verify the track_ids were updated.
        from sqlalchemy import select
        async with _TestSession() as session:
            t1 = (await session.execute(
                select(TrackFeatures).where(TrackFeatures.track_id == "nav-uuid-001")
            )).scalar_one_or_none()
            assert t1 is not None
            assert t1.title == "Track One"
            assert t1.artist == "Artist One"
            assert t1.album == "Album A"
            # Old hash ID preserved in external_track_id.
            assert t1.external_track_id == "hash_abc123"

    @patch("app.services.media_server.settings")
    async def test_sync_cascades_to_events_and_interactions(self, mock_settings):
        """When track_id changes, events and interactions are updated too."""
        mock_settings.MEDIA_SERVER_TYPE = "navidrome"
        mock_settings.MEDIA_SERVER_URL = "http://localhost:4533"
        mock_settings.MEDIA_SERVER_USER = "admin"
        mock_settings.MEDIA_SERVER_PASSWORD = "pass"
        mock_settings.MEDIA_SERVER_TOKEN = ""
        mock_settings.MEDIA_SERVER_LIBRARY_ID = "1"
        mock_settings.MEDIA_SERVER_MUSIC_PATH = "/data/music"
        mock_settings.MUSIC_LIBRARY_PATH = "/music"

        await self._seed_tracks()

        server_tracks = [
            MediaServerTrack(
                server_id="nav-uuid-003",
                title="Track Three",
                artist="Artist One",
                album="Album A",
                file_path="/data/music/Artist One/Album A/Track 3.flac",
            ),
        ]

        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_updated == 1

        # Verify cascade.
        from sqlalchemy import select
        async with _TestSession() as session:
            # Event should now reference the new track_id.
            events = (await session.execute(
                select(ListenEvent).where(ListenEvent.track_id == "nav-uuid-003")
            )).scalars().all()
            assert len(events) == 1

            # Old track_id should have no events.
            old_events = (await session.execute(
                select(ListenEvent).where(ListenEvent.track_id == "hash_ghi789")
            )).scalars().all()
            assert len(old_events) == 0

            # Interaction should be updated.
            interaction = (await session.execute(
                select(TrackInteraction).where(TrackInteraction.track_id == "nav-uuid-003")
            )).scalar_one_or_none()
            assert interaction is not None
            assert interaction.play_count == 5

    @patch("app.services.media_server.settings")
    async def test_unmatched_tracks_counted(self, mock_settings):
        """Tracks in the DB with no media server match are counted."""
        mock_settings.MEDIA_SERVER_TYPE = "navidrome"
        mock_settings.MEDIA_SERVER_URL = "http://localhost:4533"
        mock_settings.MEDIA_SERVER_USER = "admin"
        mock_settings.MEDIA_SERVER_PASSWORD = "pass"
        mock_settings.MEDIA_SERVER_TOKEN = ""
        mock_settings.MEDIA_SERVER_LIBRARY_ID = "1"
        mock_settings.MEDIA_SERVER_MUSIC_PATH = "/data/music"
        mock_settings.MUSIC_LIBRARY_PATH = "/music"

        await self._seed_tracks()

        # Server returns only 1 track, DB has 3.
        server_tracks = [
            MediaServerTrack(
                server_id="nav-uuid-001",
                title="Track One",
                artist="Artist One",
                album="Album A",
                file_path="/data/music/Artist One/Album A/Track 1.flac",
            ),
        ]

        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 1
        assert result.tracks_unmatched == 2

    @patch("app.services.media_server.settings")
    async def test_idempotent_sync(self, mock_settings):
        """Running sync twice doesn't create duplicates or errors."""
        mock_settings.MEDIA_SERVER_TYPE = "navidrome"
        mock_settings.MEDIA_SERVER_URL = "http://localhost:4533"
        mock_settings.MEDIA_SERVER_USER = "admin"
        mock_settings.MEDIA_SERVER_PASSWORD = "pass"
        mock_settings.MEDIA_SERVER_TOKEN = ""
        mock_settings.MEDIA_SERVER_LIBRARY_ID = "1"
        mock_settings.MEDIA_SERVER_MUSIC_PATH = "/data/music"
        mock_settings.MUSIC_LIBRARY_PATH = "/music"

        await self._seed_tracks()

        server_tracks = [
            MediaServerTrack(
                server_id="nav-uuid-001",
                title="Track One",
                artist="Artist One",
                album="Album A",
                file_path="/data/music/Artist One/Album A/Track 1.flac",
            ),
        ]

        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                r1 = await sync_track_ids(session)
            async with _TestSession() as session:
                r2 = await sync_track_ids(session)

        assert r1.tracks_updated == 1
        assert r2.tracks_updated == 0  # Already synced, no change.
        assert r2.tracks_matched == 1

    @patch("app.services.media_server.settings")
    async def test_metadata_update_without_id_change(self, mock_settings):
        """If track_id already matches but metadata changed, only metadata updates."""
        mock_settings.MEDIA_SERVER_TYPE = "navidrome"
        mock_settings.MEDIA_SERVER_URL = "http://localhost:4533"
        mock_settings.MEDIA_SERVER_USER = "admin"
        mock_settings.MEDIA_SERVER_PASSWORD = "pass"
        mock_settings.MEDIA_SERVER_TOKEN = ""
        mock_settings.MEDIA_SERVER_LIBRARY_ID = "1"
        mock_settings.MEDIA_SERVER_MUSIC_PATH = "/music"
        mock_settings.MUSIC_LIBRARY_PATH = "/music"

        # Create a track that already has the correct track_id.
        async with _TestSession() as session:
            session.add(TrackFeatures(
                track_id="nav-uuid-001",
                file_path="/music/Artist/Album/Song.flac",
                bpm=120.0,
            ))
            await session.commit()

        server_tracks = [
            MediaServerTrack(
                server_id="nav-uuid-001",
                title="Updated Title",
                artist="Updated Artist",
                album="Updated Album",
                file_path="/music/Artist/Album/Song.flac",
            ),
        ]

        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_updated == 0   # ID didn't change.
        assert result.tracks_metadata == 1  # But metadata was updated.

        from sqlalchemy import select
        async with _TestSession() as session:
            t = (await session.execute(
                select(TrackFeatures).where(TrackFeatures.track_id == "nav-uuid-001")
            )).scalar_one()
            assert t.title == "Updated Title"
            assert t.artist == "Updated Artist"


# ---------------------------------------------------------------------------
# Sync API endpoint
# ---------------------------------------------------------------------------

class TestSyncEndpoint:

    async def test_sync_not_configured(self, client: AsyncClient):
        """Returns 400 if no media server is configured."""
        resp = await client.post("/v1/library/sync")
        assert resp.status_code == 400
        assert "No media server configured" in resp.json()["detail"]
