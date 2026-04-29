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
    _aatd_key,
    _atd_key,
    _canon_artist_set,
    _canon_str,
    _duration_compatible,
    _extract_mbid_from_plex_guid,
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
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
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
            session.add(
                TrackFeatures(
                    track_id="hash_abc123",
                    file_path="/music/Artist One/Album A/Track 1.flac",
                    bpm=120.0,
                    energy=0.8,
                )
            )
            session.add(
                TrackFeatures(
                    track_id="hash_def456",
                    file_path="/music/Artist Two/Album B/Track 2.mp3",
                    bpm=90.0,
                    energy=0.5,
                )
            )
            # A track with an event and interaction
            session.add(
                TrackFeatures(
                    track_id="hash_ghi789",
                    file_path="/music/Artist One/Album A/Track 3.flac",
                    bpm=140.0,
                    energy=0.9,
                )
            )
            session.add(User(user_id="testuser"))
            session.add(
                ListenEvent(
                    user_id="testuser",
                    track_id="hash_ghi789",
                    event_type="play_end",
                    value=0.95,
                    timestamp=int(time.time()),
                )
            )
            session.add(
                TrackInteraction(
                    user_id="testuser",
                    track_id="hash_ghi789",
                    play_count=5,
                    skip_count=0,
                    like_count=1,
                    dislike_count=0,
                    repeat_count=0,
                    playlist_add_count=0,
                    queue_add_count=0,
                    satisfaction_score=0.9,
                    last_event_id=1,
                    updated_at=int(time.time()),
                )
            )
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
        assert result.media_server_id_updated == 2
        assert result.metadata_updated == 2

        # Verify the media_server_id was set and track_id is untouched.
        from sqlalchemy import select

        async with _TestSession() as session:
            t1 = (
                await session.execute(select(TrackFeatures).where(TrackFeatures.media_server_id == "nav-uuid-001"))
            ).scalar_one_or_none()
            assert t1 is not None
            assert t1.title == "Track One"
            assert t1.artist == "Artist One"
            assert t1.album == "Album A"
            # Internal track_id is immutable — the original hash stays.
            assert t1.track_id == "hash_abc123"

    @patch("app.services.media_server.settings")
    async def test_sync_does_not_rename_track_id_or_cascade(self, mock_settings):
        """Post-#37 sync is a metadata refresh: track_id is immutable, so
        listen_events / track_interactions / etc. are never rewritten."""
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

        # The match is recorded — but only as a media_server_id update.
        assert result.media_server_id_updated == 1

        from sqlalchemy import select

        async with _TestSession() as session:
            # The seeded event / interaction still point at the original
            # hash track_id; sync never rewrites them under the new schema.
            events = (
                (await session.execute(select(ListenEvent).where(ListenEvent.track_id == "hash_ghi789")))
                .scalars()
                .all()
            )
            assert len(events) == 1

            no_events_under_navi = (
                (await session.execute(select(ListenEvent).where(ListenEvent.track_id == "nav-uuid-003")))
                .scalars()
                .all()
            )
            assert no_events_under_navi == []

            interaction = (
                await session.execute(select(TrackInteraction).where(TrackInteraction.track_id == "hash_ghi789"))
            ).scalar_one_or_none()
            assert interaction is not None
            assert interaction.play_count == 5

            # The TrackFeatures row got the media_server_id set, internal id unchanged.
            tf = (
                await session.execute(select(TrackFeatures).where(TrackFeatures.track_id == "hash_ghi789"))
            ).scalar_one()
            assert tf.media_server_id == "nav-uuid-003"

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

        assert r1.media_server_id_updated == 1
        assert r2.media_server_id_updated == 0  # Already synced, no change.
        assert r2.tracks_matched == 1

    @patch("app.services.media_server.settings")
    async def test_metadata_update_without_media_server_id_change(self, mock_settings):
        """If media_server_id already matches but metadata drifted, only the
        title/artist/album/genre fields are refreshed."""
        mock_settings.MEDIA_SERVER_TYPE = "navidrome"
        mock_settings.MEDIA_SERVER_URL = "http://localhost:4533"
        mock_settings.MEDIA_SERVER_USER = "admin"
        mock_settings.MEDIA_SERVER_PASSWORD = "pass"
        mock_settings.MEDIA_SERVER_TOKEN = ""
        mock_settings.MEDIA_SERVER_LIBRARY_ID = "1"
        mock_settings.MEDIA_SERVER_MUSIC_PATH = "/music"
        mock_settings.MUSIC_LIBRARY_PATH = "/music"

        # A row whose media_server_id is already populated correctly.
        async with _TestSession() as session:
            session.add(
                TrackFeatures(
                    track_id="hash_internal_001",
                    media_server_id="nav-uuid-001",
                    file_path="/music/Artist/Album/Song.flac",
                    bpm=120.0,
                )
            )
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

        assert result.media_server_id_updated == 0  # Already had the right ID.
        assert result.metadata_updated == 1  # But metadata was refreshed.

        from sqlalchemy import select

        async with _TestSession() as session:
            t = (
                await session.execute(select(TrackFeatures).where(TrackFeatures.media_server_id == "nav-uuid-001"))
            ).scalar_one()
            assert t.track_id == "hash_internal_001"
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


# ---------------------------------------------------------------------------
# Stale-track cleanup endpoint (issue #29)
# ---------------------------------------------------------------------------


class TestCleanupStale:
    async def test_dry_run_reports_counts_without_deleting(self, client: AsyncClient, tmp_path):
        """Default dry_run=true returns counts and deletes nothing."""
        real_file = tmp_path / "exists.flac"
        real_file.write_bytes(b"x")  # touch
        async with _TestSession() as session:
            session.add(
                TrackFeatures(
                    track_id="0123456789abcdef",  # 16-hex, file exists
                    file_path=str(real_file),
                )
            )
            session.add(
                TrackFeatures(
                    track_id="fedcba9876543210",  # 16-hex, file gone
                    file_path="/this/path/does/not/exist.flac",
                )
            )
            session.add(
                TrackFeatures(
                    track_id="22charNavidromeID000A",  # not 16-hex — ignored
                    file_path="/anywhere.flac",
                )
            )
            await session.commit()

        resp = await client.post("/v1/library/cleanup-stale")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern"] == "legacy_hex"
        assert body["dry_run"] is True
        assert body["candidates_total"] == 2  # both 16-hex rows
        assert body["files_present"] == 1
        assert body["files_missing"] == 1
        assert body["deleted_track_features"] == 0  # dry run

        # Verify nothing was actually deleted.
        from sqlalchemy import select

        async with _TestSession() as session:
            count = (await session.execute(select(TrackFeatures))).all()
        assert len(count) == 3

    async def test_actual_delete_removes_only_missing_files(self, client: AsyncClient, tmp_path):
        """dry_run=false deletes track_features rows whose files are gone,
        plus their orphan interactions and events.  Rows whose files still
        exist are left for the next sync to re-match."""
        real_file = tmp_path / "still_there.flac"
        real_file.write_bytes(b"x")
        now = int(time.time())
        async with _TestSession() as session:
            # File exists — keep this row.
            session.add(
                TrackFeatures(
                    track_id="aaaaaaaaaaaaaaaa",
                    file_path=str(real_file),
                )
            )
            # File missing — delete this row + its orphans.
            session.add(
                TrackFeatures(
                    track_id="bbbbbbbbbbbbbbbb",
                    file_path="/gone/forever.flac",
                )
            )
            # 22-char id — must be untouched.
            session.add(
                TrackFeatures(
                    track_id="22charNavidromeID000A",
                    file_path="/something.flac",
                )
            )
            session.add(User(user_id="u1"))
            # Orphan interaction + event at the doomed id.
            session.add(
                TrackInteraction(
                    user_id="u1",
                    track_id="bbbbbbbbbbbbbbbb",
                    play_count=1,
                    last_event_id=1,
                    updated_at=now,
                )
            )
            session.add(
                ListenEvent(
                    user_id="u1",
                    track_id="bbbbbbbbbbbbbbbb",
                    event_type="play_end",
                    value=0.5,
                    timestamp=now,
                )
            )
            # Interaction at the kept id — must survive.
            session.add(
                TrackInteraction(
                    user_id="u1",
                    track_id="aaaaaaaaaaaaaaaa",
                    play_count=2,
                    last_event_id=2,
                    updated_at=now,
                )
            )
            await session.commit()

        resp = await client.post("/v1/library/cleanup-stale?dry_run=false")
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is False
        assert body["files_missing"] == 1
        assert body["files_present"] == 1
        assert body["deleted_track_features"] == 1
        assert body["deleted_interactions"] == 1
        assert body["deleted_events"] == 1

        from sqlalchemy import select

        async with _TestSession() as session:
            remaining_tf = (await session.execute(select(TrackFeatures.track_id))).scalars().all()
            remaining_int = (await session.execute(select(TrackInteraction.track_id))).scalars().all()
            remaining_ev = (await session.execute(select(ListenEvent.track_id))).scalars().all()

        # The doomed id is gone everywhere.
        assert "bbbbbbbbbbbbbbbb" not in remaining_tf
        assert "bbbbbbbbbbbbbbbb" not in remaining_int
        assert "bbbbbbbbbbbbbbbb" not in remaining_ev
        # The kept ids are intact.
        assert "aaaaaaaaaaaaaaaa" in remaining_tf
        assert "aaaaaaaaaaaaaaaa" in remaining_int
        assert "22charNavidromeID000A" in remaining_tf

    async def test_unknown_pattern_rejected(self, client: AsyncClient):
        resp = await client.post("/v1/library/cleanup-stale?pattern=mystery")
        assert resp.status_code == 400
        assert "Unknown pattern" in resp.json()["detail"]

    async def test_does_not_match_22char_base62(self, client: AsyncClient):
        """A 22-char Navidrome base62 id like 'abc...XYZ' must never be
        flagged as legacy-hex even by accident."""
        async with _TestSession() as session:
            # Looks hex but is 22 chars (well outside the 16-char filter).
            session.add(TrackFeatures(track_id="abcdef0123456789abcdef", file_path="/x.flac"))
            await session.commit()
        resp = await client.post("/v1/library/cleanup-stale")
        assert resp.json()["candidates_total"] == 0

    async def test_skips_16char_non_hex(self, client: AsyncClient):
        """A 16-character non-hex id (e.g. some external system's slug)
        must not be touched."""
        async with _TestSession() as session:
            # 16 chars but contains 'g', 'h', etc — not hex.
            session.add(TrackFeatures(track_id="zzzz1234ghyypoll", file_path="/x.flac"))
            await session.commit()
        resp = await client.post("/v1/library/cleanup-stale")
        assert resp.json()["candidates_total"] == 0


# ---------------------------------------------------------------------------
# Canonicalisation helpers (MBID / AATD matcher)
# ---------------------------------------------------------------------------


class TestCanonicalisationHelpers:
    def test_canon_str_lowercases_and_strips(self):
        assert _canon_str("  Hello  World  ") == "hello world"

    def test_canon_str_handles_none(self):
        assert _canon_str(None) == ""

    def test_canon_str_collapses_internal_whitespace(self):
        assert _canon_str("Foo    Bar\tBaz") == "foo bar baz"

    def test_aatd_key_requires_artist_and_title(self):
        assert _aatd_key("", "Album", "Title") is None
        assert _aatd_key("Artist", "Album", "") is None
        # Album empty is allowed (singles, untagged)
        assert _aatd_key("Artist", "", "Title") == ("artist", "", "title")

    def test_aatd_key_canonicalises(self):
        assert _aatd_key("  ARTIST  ", "Album X", "Track 1") == ("artist", "album x", "track 1")

    def test_canon_artist_set_handles_separators(self):
        """All common collab separators yield the same canonical token set,
        so "Foo & Bar" and "Foo and Bar" key the same row."""
        ampersand = _canon_artist_set("Foo & Bar")
        ampersand_with_and = _canon_artist_set("Foo and Bar")
        slash = _canon_artist_set("Foo / Bar")
        comma = _canon_artist_set("Foo, Bar")
        semicolon = _canon_artist_set("Foo; Bar")
        feat = _canon_artist_set("Foo feat. Bar")
        featuring = _canon_artist_set("Foo featuring Bar")
        ft = _canon_artist_set("Foo ft Bar")
        x = _canon_artist_set("Foo x Bar")
        with_word = _canon_artist_set("Foo with Bar")
        # All forms collapse to the same sorted set.
        assert ampersand == "bar|foo"
        assert ampersand_with_and == "bar|foo"
        assert slash == "bar|foo"
        assert comma == "bar|foo"
        assert semicolon == "bar|foo"
        assert feat == "bar|foo"
        assert featuring == "bar|foo"
        assert ft == "bar|foo"
        assert x == "bar|foo"
        assert with_word == "bar|foo"

    def test_canon_artist_set_three_way_collab(self):
        """The exact mismatch from the production investigation: "and" vs "&"
        across three collaborators."""
        a = _canon_artist_set("2WEI and Elena Westermann and Edda Hayes")
        b = _canon_artist_set("2WEI & Elena Westermann & Edda Hayes")
        assert a == b
        # Sanity: actual content is preserved as a sorted set.
        assert a == "2wei|edda hayes|elena westermann"

    def test_canon_artist_set_single_artist_unchanged(self):
        """A single artist with no separators still canonicalises through."""
        assert _canon_artist_set("Johnny Cash") == "johnny cash"
        assert _canon_artist_set("  Johnny  Cash  ") == "johnny cash"

    def test_canon_artist_set_handles_none_and_empty(self):
        assert _canon_artist_set(None) == ""
        assert _canon_artist_set("") == ""
        assert _canon_artist_set("   ") == ""

    def test_aatd_key_artist_set_normalises_separators(self):
        """End-to-end: AATD key matches across separator variants."""
        a = _aatd_key("2WEI and Elena Westermann and Edda Hayes", "Eternal", "Eternal")
        b = _aatd_key("2WEI & Elena Westermann & Edda Hayes", "Eternal", "Eternal")
        assert a == b
        assert a is not None

    def test_atd_key_requires_album_and_title(self):
        assert _atd_key("", "Title") is None
        assert _atd_key("Album", "") is None
        assert _atd_key(None, "Title") is None
        assert _atd_key("Album", "Title") == ("album", "title")

    def test_atd_key_canonicalises(self):
        assert _atd_key("  ALBUM  X  ", "Track 1") == ("album x", "track 1")

    def test_duration_compatible(self):
        assert _duration_compatible(180.0, 180.0) is True
        assert _duration_compatible(180.0, 181.0) is True  # within 1.5s tolerance
        assert _duration_compatible(180.0, 183.0) is False
        # Missing on either side: accept (don't reject just because we don't know)
        assert _duration_compatible(None, 180.0) is True
        assert _duration_compatible(180.0, None) is True

    def test_extract_mbid_from_plex_guid(self):
        m = {"Guid": [{"id": "mbid://recording/abc-123"}, {"id": "mbid://artist/xyz"}]}
        assert _extract_mbid_from_plex_guid(m) == "abc-123"

    def test_extract_mbid_from_plex_guid_alt_scheme(self):
        m = {"Guid": [{"id": "musicbrainz://recording/uuid-1"}]}
        assert _extract_mbid_from_plex_guid(m) == "uuid-1"

    def test_extract_mbid_from_plex_guid_no_recording(self):
        m = {"Guid": [{"id": "mbid://artist/xyz"}]}
        assert _extract_mbid_from_plex_guid(m) is None

    def test_extract_mbid_from_plex_guid_empty(self):
        assert _extract_mbid_from_plex_guid({}) is None
        assert _extract_mbid_from_plex_guid({"Guid": []}) is None


# ---------------------------------------------------------------------------
# Matcher priority chain (MBID → AATD → path)
# ---------------------------------------------------------------------------


class TestMatcherPriorityChain:
    """Each test seeds a single GrooveIQ row whose path / metadata is rigged
    to make exactly one strategy succeed, so the priority chain and metric
    counters can be asserted in isolation.

    The "Spotizerr / Navidrome 0.61" scenario from the prod investigation is
    the worst case for path matching: the actual filesystem path uses ``and``
    while Navidrome reports ``&`` and a different track-naming template.  We
    encode that exact mismatch in the AATD-only test below to lock in the
    fix.
    """

    @staticmethod
    async def _seed_one(track_id, file_path, **kw):
        async with _TestSession() as session:
            session.add(TrackFeatures(track_id=track_id, file_path=file_path, **kw))
            await session.commit()

    @staticmethod
    def _settings(mock):
        mock.MEDIA_SERVER_TYPE = "navidrome"
        mock.MEDIA_SERVER_URL = "http://localhost:4533"
        mock.MEDIA_SERVER_USER = "admin"
        mock.MEDIA_SERVER_PASSWORD = "pass"
        mock.MEDIA_SERVER_TOKEN = ""
        mock.MEDIA_SERVER_LIBRARY_ID = "1"
        mock.MEDIA_SERVER_MUSIC_PATH = "/data/media/music"
        mock.MUSIC_LIBRARY_PATH = "/music"

    @patch("app.services.media_server.settings")
    async def test_mbid_match(self, mock_settings):
        """MBID match works even when path is wrong."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path/To/Song.flac",  # path can't match
            title="Song",
            artist="Artist",
            album="Album",
            musicbrainz_track_id="mb-recording-1",
        )

        server_tracks = [
            MediaServerTrack(
                server_id="nav-uuid-001",
                title="Different Title",
                artist="Different Artist",
                album="Different Album",
                file_path="some/synthetic/path.ogg",  # path mismatch
                mb_track_id="mb-recording-1",  # but MBID matches
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 1
        assert result.tracks_matched_by_mbid == 1
        assert result.tracks_matched_by_aatd == 0
        assert result.tracks_matched_by_path == 0
        assert result.media_server_id_updated == 1

    @patch("app.services.media_server.settings")
    async def test_aatd_match_when_path_is_synthetic(self, mock_settings):
        """The Navidrome 0.61 scenario: actual filesystem says " and " and the
        Lidarr-style naming, but Navidrome reports " & " with its own template.
        Path matching fails, but artist/album/title align — AATD must rescue.
        """
        self._settings(mock_settings)
        await self._seed_one(
            "legacy-hex-1",
            file_path="/music/2WEI and Elena Westermann and Edda Hayes/Eternal/01. Eternal (NORMAL).ogg",
            title="Eternal",
            artist="2WEI & Elena Westermann & Edda Hayes",  # ID3 tag
            album="Eternal",
            duration=180.0,
        )

        server_tracks = [
            MediaServerTrack(
                server_id="22charNavidromeID000A",
                title="Eternal",
                artist="2WEI & Elena Westermann & Edda Hayes",
                album="Eternal",
                # Navidrome's synthetic path — does NOT correspond to disk
                file_path="2WEI & Elena Westermann & Edda Hayes/Eternal/01-01 - Eternal.ogg",
                duration=180.0,
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 1
        assert result.tracks_matched_by_aatd == 1
        assert result.tracks_matched_by_mbid == 0
        assert result.tracks_matched_by_path == 0
        assert result.media_server_id_updated == 1
        assert result.tracks_aatd_ambiguous == 0

    @patch("app.services.media_server.settings")
    async def test_aatd_disambiguates_by_duration(self, mock_settings):
        """Two server tracks share artist/album/title (e.g. live + studio)
        but differ in duration — duration narrows it to one match."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path.flac",
            title="Hurt",
            artist="Johnny Cash",
            album="American IV",
            duration=219.0,  # studio version
        )
        server_tracks = [
            MediaServerTrack(
                server_id="server-live",
                title="Hurt",
                artist="Johnny Cash",
                album="American IV",
                file_path="x/live.flac",
                duration=247.0,  # live, different length
            ),
            MediaServerTrack(
                server_id="server-studio",
                title="Hurt",
                artist="Johnny Cash",
                album="American IV",
                file_path="y/studio.flac",
                duration=219.0,
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 1
        assert result.tracks_matched_by_aatd == 1
        assert result.tracks_aatd_ambiguous == 0

        from sqlalchemy import select

        async with _TestSession() as session:
            row = (await session.execute(select(TrackFeatures))).scalar_one()
            # Post-#37: sync writes the per-backend ID into media_server_id;
            # track_id stays as the immutable internal hash (here the "old-id"
            # placeholder used by _seed_one).
            assert row.track_id == "old-id"
            assert row.media_server_id == "server-studio"

    @patch("app.services.media_server.settings")
    async def test_aatd_ambiguous_skips_match(self, mock_settings):
        """If AATD has multiple candidates and duration can't disambiguate,
        the row is counted as ambiguous and the matcher falls through to
        path matching (which here also misses, so unmatched)."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path.flac",  # won't match any path
            title="Hurt",
            artist="Johnny Cash",
            album="American IV",
            # No duration — AATD can't disambiguate
        )
        server_tracks = [
            MediaServerTrack(
                server_id="dup1",
                title="Hurt",
                artist="Johnny Cash",
                album="American IV",
                file_path="path1.flac",
                duration=219.0,
            ),
            MediaServerTrack(
                server_id="dup2",
                title="Hurt",
                artist="Johnny Cash",
                album="American IV",
                file_path="path2.flac",
                duration=247.0,
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 0
        assert result.tracks_aatd_ambiguous == 1
        assert result.tracks_unmatched == 1

    @patch("app.services.media_server.settings")
    async def test_path_match_when_mbid_and_aatd_miss(self, mock_settings):
        """Path matching is still used as a last-resort — covers Plex
        libraries that return real filesystem paths and tracks where
        we have no MBID and no aligned tags."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Some/Album/01 - Untagged.flac",
            title="",
            artist="",
            album="",
            # No MBID, no usable AATD (artist+title both empty)
        )
        server_tracks = [
            MediaServerTrack(
                server_id="nav-by-path",
                title="",
                artist="",
                album="",
                file_path="/data/media/music/Some/Album/01 - Untagged.flac",
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 1
        assert result.tracks_matched_by_path == 1
        assert result.tracks_matched_by_mbid == 0
        assert result.tracks_matched_by_aatd == 0

    @patch("app.services.media_server.settings")
    async def test_priority_mbid_over_aatd_and_path(self, mock_settings):
        """When all three strategies could match, MBID wins."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Artist/Album/Song.flac",
            title="Song",
            artist="Artist",
            album="Album",
            musicbrainz_track_id="mb-1",
            duration=180.0,
        )
        server_tracks = [
            MediaServerTrack(
                server_id="nav-id",
                title="Song",
                artist="Artist",
                album="Album",
                file_path="/data/media/music/Artist/Album/Song.flac",  # path matches
                duration=180.0,  # AATD matches
                mb_track_id="mb-1",  # MBID matches — highest priority
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 1
        assert result.tracks_matched_by_mbid == 1
        assert result.tracks_matched_by_aatd == 0
        assert result.tracks_matched_by_path == 0

    @patch("app.services.media_server.settings")
    async def test_aatd_handles_multi_artist_separator_divergence(self, mock_settings):
        """Issue #27: disk says " and ", Navidrome says " & ".  After
        multi-artist canonicalisation, AATD must still resolve the pair.
        """
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path.flac",  # path won't help
            title="Eternal",
            # Disk-side: ID3 tag uses " and " between collaborators.
            artist="2WEI and Elena Westermann and Edda Hayes",
            album="Eternal",
            duration=180.0,
        )
        server_tracks = [
            MediaServerTrack(
                server_id="22charNavidromeID000A",
                title="Eternal",
                # Server-side: Navidrome's display string uses " & ".
                artist="2WEI & Elena Westermann & Edda Hayes",
                album="Eternal",
                file_path="x/synthetic.ogg",
                duration=180.0,
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 1
        assert result.tracks_matched_by_aatd == 1
        assert result.tracks_aatd_ambiguous == 0
        assert result.media_server_id_updated == 1

    @patch("app.services.media_server.settings")
    async def test_atd_fallback_when_artist_diverges_completely(self, mock_settings):
        """Issue #27 stretch: artist tags don't share a single token
        ("Soundtrack" vs "John Williams"), but album+title+duration align.
        ATD fallback must rescue this with strict duration filtering.
        """
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path.flac",
            title="Imperial March",
            artist="Soundtrack",  # ID3 lead-artist tag
            album="Star Wars: The Empire Strikes Back",
            duration=180.0,
        )
        server_tracks = [
            MediaServerTrack(
                server_id="nav-atd-1",
                title="Imperial March",
                artist="John Williams",  # Navidrome's display artist
                album="Star Wars: The Empire Strikes Back",
                file_path="x/y.ogg",
                duration=180.5,  # within ±1.0s strict ATD tolerance
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched == 1
        # ATD is rolled into the aatd counter for backwards-compat reporting.
        assert result.tracks_matched_by_aatd == 1
        assert result.media_server_id_updated == 1

    @patch("app.services.media_server.settings")
    async def test_atd_fallback_rejects_when_duration_misses(self, mock_settings):
        """ATD without an artist anchor MUST be strict on duration. A 5s
        difference (well outside the ±1s ATD tolerance) means we let the
        row fall through to path matching, not gamble."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path.flac",
            title="Imperial March",
            artist="Soundtrack",
            album="Star Wars",
            duration=180.0,
        )
        server_tracks = [
            MediaServerTrack(
                server_id="nav-wrong",
                title="Imperial March",
                artist="John Williams",
                album="Star Wars",
                file_path="x/y.ogg",
                duration=185.0,  # 5s off — too far for ATD without artist
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        # Falls through ATD (duration too far) and path (different paths).
        assert result.tracks_matched == 0
        assert result.tracks_unmatched == 1

    @patch("app.services.media_server.settings")
    async def test_atd_fallback_skips_when_album_empty(self, mock_settings):
        """ATD requires a non-empty album.  A bare (title, duration) match
        is too lossy.  When album is empty we skip ATD and fall through."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path.flac",
            title="Intro",
            artist="Mystery",
            album="",  # no album
            duration=30.0,
        )
        server_tracks = [
            MediaServerTrack(
                server_id="nav-intro",
                title="Intro",
                artist="Different Artist",
                album="",
                file_path="x/y.ogg",
                duration=30.0,
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        # Without album, ATD is unsafe — fall through.  AATD also misses
        # because artists differ entirely.  Path differs.  Unmatched.
        assert result.tracks_matched == 0
        assert result.tracks_unmatched == 1

    @patch("app.services.media_server.settings")
    async def test_atd_fallback_requires_unique_match(self, mock_settings):
        """If two server tracks share (album, title) and both pass the
        strict duration filter, ATD must skip — without artist, we can't
        tell them apart."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path.flac",
            title="Track 1",
            artist="Various",
            album="Compilation",
            duration=180.0,
        )
        server_tracks = [
            MediaServerTrack(
                server_id="cand-1",
                title="Track 1",
                artist="Artist A",
                album="Compilation",
                file_path="x/a.ogg",
                duration=180.0,
            ),
            MediaServerTrack(
                server_id="cand-2",
                title="Track 1",
                artist="Artist B",
                album="Compilation",
                file_path="x/b.ogg",
                duration=180.5,  # also within ±1s
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        # Two candidates pass — ATD refuses to guess.  Falls through.
        assert result.tracks_matched == 0
        assert result.tracks_unmatched == 1

    # NOTE: ``test_rename_merges_colliding_interactions`` (issue #28) was
    # removed as part of issue #37. Sync no longer renames track_id, so the
    # (user_id, track_id) UNIQUE collision can't arise from sync. The merge
    # logic itself now lives in migrations/010 for the one-shot data reshape;
    # see that script for tests of the merge behaviour.

    @patch("app.services.media_server.settings")
    async def test_uses_duration_ms_when_set(self, mock_settings):
        """If duration_ms is populated (from ID3 reader) it takes precedence
        over the analyzer's float ``duration`` for AATD comparisons."""
        self._settings(mock_settings)
        await self._seed_one(
            "old-id",
            file_path="/music/Wrong/Path.flac",
            title="Song",
            artist="Artist",
            album="Album",
            duration=999.0,  # bogus analyzer value
            duration_ms=180_000,  # accurate ID3 value (180.0s)
        )
        server_tracks = [
            MediaServerTrack(
                server_id="match",
                title="Song",
                artist="Artist",
                album="Album",
                file_path="x/y.flac",
                duration=180.5,
            ),
            MediaServerTrack(
                server_id="other",
                title="Song",
                artist="Artist",
                album="Album",
                file_path="x/z.flac",
                duration=400.0,
            ),
        ]
        with patch("app.services.media_server.fetch_tracks", new_callable=AsyncMock, return_value=server_tracks):
            async with _TestSession() as session:
                result = await sync_track_ids(session)

        assert result.tracks_matched_by_aatd == 1
        from sqlalchemy import select

        async with _TestSession() as session:
            row = (await session.execute(select(TrackFeatures))).scalar_one()
            # Post-#37: sync writes media_server_id, never mutates track_id.
            assert row.track_id == "old-id"
            assert row.media_server_id == "match"
