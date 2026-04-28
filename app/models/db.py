"""
GrooveIQ – SQLAlchemy ORM models.

Design decisions:
- track_id and user_id are strings (not FKs to external systems).
  GrooveIQ is ID-agnostic: you pass whatever ID your media server uses.
- All timestamps are UTC Unix epoch integers for portability.
- The `events` table is append-only; never update rows.
- `track_features` stores Essentia output as a JSON blob alongside
  individual indexed columns for fast range queries.
"""

from __future__ import annotations

import time

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Events  (Phase 1)
# ---------------------------------------------------------------------------


class ListenEvent(Base):
    """
    A single behavioral event from a music player.

    One row = one discrete action (play_end, skip, like, etc.).
    The `value` column carries event-specific payload:
      - play_end    → percentage completed (0.0 – 1.0)
      - skip        → position in track when skipped (seconds)
      - volume      → new volume level (0 – 100)
      - seek        → target position in seconds
      - rating      → explicit rating (-1 / 0 / +1 or 0–5)
      - (others)    → null / unused
    """

    __tablename__ = "listen_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    track_id = Column(String(128), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    value = Column(Float, nullable=True)
    context = Column(String(64), nullable=True)  # "morning_commute", "workout", etc.
    client_id = Column(String(64), nullable=True)  # which app sent this
    timestamp = Column(Integer, nullable=False, index=True, default=lambda: int(time.time()))
    session_id = Column(String(64), nullable=True, index=True)

    # --- Rich behavioral / session / context signals -------------------------
    # Impression & exposure
    surface = Column(String(64), nullable=True)  # home, search, now_playing, playlist_view
    position = Column(Integer, nullable=True)  # rank position in reco list
    request_id = Column(String(128), nullable=True, index=True)  # ties impressions → streams
    model_version = Column(String(64), nullable=True)  # reco model version

    # Sessionization
    session_position = Column(Integer, nullable=True)  # track ordinal in session

    # Satisfaction / dwell
    dwell_ms = Column(Integer, nullable=True)  # ms listened

    # Pause buckets
    pause_duration_ms = Column(Integer, nullable=True)  # inter-track gap ms

    # Seek intensity
    num_seekfwd = Column(Integer, nullable=True)
    num_seekbk = Column(Integer, nullable=True)

    # Shuffle state
    shuffle = Column(Boolean, nullable=True)

    # Context / source
    context_type = Column(String(32), nullable=True)  # playlist, album, radio, search, home_shelf
    context_id = Column(String(128), nullable=True)  # playlist/album/radio ID
    context_switch = Column(Boolean, nullable=True)  # user just switched context

    # Start / end reason codes
    reason_start = Column(String(32), nullable=True)  # autoplay, user_tap, forward_button, external
    reason_end = Column(String(32), nullable=True)  # track_done, user_skip, error, new_track

    # Cross-device identity
    device_id = Column(String(128), nullable=True, index=True)
    device_type = Column(String(32), nullable=True)  # mobile, desktop, speaker, car, web

    # Local time context (client-side)
    hour_of_day = Column(Integer, nullable=True)  # 0–23
    day_of_week = Column(Integer, nullable=True)  # 1=Mon … 7=Sun (ISO 8601)
    timezone = Column(String(64), nullable=True)  # IANA, e.g. "Europe/Zurich"

    # Audio output
    output_type = Column(String(32), nullable=True)  # headphones, speaker, bluetooth_speaker, …
    output_device_name = Column(String(128), nullable=True)  # "AirPods Pro", "Sonos Living Room"
    bluetooth_connected = Column(Boolean, nullable=True)

    # Location
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_label = Column(String(32), nullable=True)  # home, work, gym, commute

    __table_args__ = (
        Index("ix_events_user_track", "user_id", "track_id"),
        Index("ix_events_user_ts", "user_id", "timestamp"),
    )


# ---------------------------------------------------------------------------
# Track features  (Phase 3)
# ---------------------------------------------------------------------------


class TrackFeatures(Base):
    """
    Audio features extracted by Essentia for a single track.

    Indexed numeric columns allow fast similarity pre-filtering
    (e.g. WHERE bpm BETWEEN 120 AND 140 AND energy > 0.7)
    before the full vector comparison in FAISS.
    """

    __tablename__ = "track_features"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Stable internal GrooveIQ identifier — SHA-256(rel_path)[:16] hex.
    # Computed at first scan, never overwritten thereafter. The single source of
    # truth referenced by every ListenEvent / TrackInteraction / FAISS entry /
    # model token / playlist row. See issue #37.
    track_id = Column(String(128), nullable=False, unique=True, index=True)

    # Per-backend external identifiers. Populated on demand:
    #   - media_server_id   <- POST /v1/library/sync (Navidrome song ID)
    #   - spotify_id        <- download cascade / charts (Spotify track ID)
    #   - qobuz_id          <- streamrip Qobuz download
    #   - tidal_id          <- streamrip Tidal download
    #   - deezer_id         <- streamrip Deezer download
    #   - soundcloud_id     <- streamrip SoundCloud download
    # All nullable + unique so duplicate detection at sync/download time is a
    # single SQL constraint. NULLs are treated as distinct in both SQLite and
    # PostgreSQL UNIQUE indexes, so many rows may legitimately have NULL here.
    media_server_id = Column(String(64), nullable=True, unique=True, index=True)
    spotify_id = Column(String(64), nullable=True, unique=True, index=True)
    qobuz_id = Column(String(64), nullable=True, unique=True, index=True)
    tidal_id = Column(String(64), nullable=True, unique=True, index=True)
    deezer_id = Column(String(64), nullable=True, unique=True, index=True)
    soundcloud_id = Column(String(64), nullable=True, unique=True, index=True)

    # DEPRECATED — kept through the #37 migration window. Old rows held either
    # the legacy 16-hex pre-sync hash or the media-server ID depending on which
    # path created them. Phase 2 of #37 redistributes its content into
    # `track_id` / `media_server_id`; Phase 5 drops this column.
    external_track_id = Column(String(128), nullable=True, unique=True, index=True)

    # Track metadata (populated from media server sync + ID3 tags)
    title = Column(String(512), nullable=True)
    artist = Column(String(512), nullable=True)
    album = Column(String(512), nullable=True)
    album_artist = Column(String(512), nullable=True)
    genre = Column(String(512), nullable=True)  # comma-separated, e.g. "Hip-Hop, Rap"
    track_number = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)  # from ID3 tags (integer ms)
    musicbrainz_track_id = Column(String(64), nullable=True, index=True)

    # File metadata
    file_path = Column(Text, nullable=False)
    file_hash = Column(String(64), nullable=True)  # SHA-256, detects file changes
    duration = Column(Float, nullable=True)  # seconds
    analyzed_at = Column(Integer, nullable=True)  # Unix timestamp

    # --- Rhythm ---
    bpm = Column(Float, nullable=True, index=True)
    bpm_confidence = Column(Float, nullable=True)

    # --- Tonal ---
    key = Column(String(4), nullable=True)  # e.g. "C", "F#"
    mode = Column(String(6), nullable=True)  # "major" | "minor"
    key_confidence = Column(Float, nullable=True)

    # --- Dynamics ---
    loudness = Column(Float, nullable=True)  # LUFS
    dynamic_range = Column(Float, nullable=True)

    # --- High-level descriptors (0.0 – 1.0) ---
    energy = Column(Float, nullable=True, index=True)
    danceability = Column(Float, nullable=True)
    valence = Column(Float, nullable=True)  # musical positivity
    acousticness = Column(Float, nullable=True)
    instrumentalness = Column(Float, nullable=True)
    speechiness = Column(Float, nullable=True)

    # --- Mood (multi-label, stored as JSON list of {label, confidence}) ---
    mood_tags = Column(JSON, nullable=True)  # [{"label": "happy", "confidence": 0.82}]

    # --- Full feature vector for FAISS (serialized numpy array as bytes) ---
    embedding = Column(Text, nullable=True)  # base64-encoded float32 array (64-dim EffNet projection)

    # --- CLAP text-audio joint embedding (optional, 512-dim, L2-normalised) ---
    # When populated (CLAP_ENABLED=true), enables natural-language track search
    # and text-seeded playlists/radio. Stored separately from `embedding` so
    # existing FAISS/ranker code is untouched.
    clap_embedding = Column(Text, nullable=True)  # base64-encoded float32 array (512-dim)

    # --- 2D music-map coordinates (UMAP projection of `embedding`) ---
    # Populated by the music-map pipeline step; both null until first build.
    map_x = Column(Float, nullable=True)
    map_y = Column(Float, nullable=True)

    # --- Raw Essentia output (for future re-analysis without re-running) ---
    raw_features = Column(JSON, nullable=True)

    analysis_version = Column(String(16), nullable=True)  # track model version changes
    analysis_error = Column(Text, nullable=True)  # null = success


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class User(Base):
    """
    Minimal user record.  GrooveIQ does not store passwords – user_id is
    whatever ID your media server uses (Navidrome username, UUID, etc.).

    ``id`` is the stable numeric UID (never changes).
    ``user_id`` is the external username/identifier (can be renamed).
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=True)
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))
    last_seen = Column(Integer, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    # Cached taste profile (JSON, updated by the background worker)
    taste_profile = Column(JSON, nullable=True)
    profile_updated_at = Column(Integer, nullable=True)

    # Onboarding preferences (explicit user input, seeds cold-start)
    onboarding_preferences = Column(JSON, nullable=True)

    # Last.fm integration (per-user, opt-in)
    lastfm_username = Column(String(128), nullable=True)
    lastfm_session_key = Column(String(512), nullable=True)  # Fernet-encrypted
    lastfm_cache = Column(JSON, nullable=True)  # cached Last.fm profile data
    lastfm_synced_at = Column(Integer, nullable=True)  # Unix timestamp of last sync

    @property
    def uid(self) -> int:
        """Stable numeric user identifier, exposed as ``uid`` in API responses."""
        return self.id


# ---------------------------------------------------------------------------
# Library scan state
# ---------------------------------------------------------------------------


class LibraryScanState(Base):
    """Persists incremental scan progress so restarts resume cleanly."""

    __tablename__ = "library_scan_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_started_at = Column(Integer, nullable=False)
    scan_ended_at = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False, default="running")
    files_found = Column(Integer, nullable=False, default=0)
    files_analyzed = Column(Integer, nullable=False, default=0)
    files_skipped = Column(Integer, nullable=False, default=0)
    files_failed = Column(Integer, nullable=False, default=0)
    current_file = Column(Text, nullable=True)  # path being analyzed right now
    last_error = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


class Playlist(Base):
    """
    A generated playlist. Tracks are stored in the PlaylistTrack join table.
    Strategy records how the playlist was built so it can be regenerated.
    """

    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    strategy = Column(String(32), nullable=False)  # flow, mood, energy_curve, key_compatible
    seed_track_id = Column(String(128), nullable=True)
    params = Column(JSON, nullable=True)  # strategy-specific config
    track_count = Column(Integer, nullable=False, default=0)
    total_duration = Column(Float, nullable=True)  # seconds
    created_by = Column(String(128), nullable=True)  # API key hash that created this playlist
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))


# ---------------------------------------------------------------------------
# Sessions  (Phase 2 – materialised from listen_events)
# ---------------------------------------------------------------------------


class ListenSession(Base):
    """
    A materialised listening session, derived from ListenEvent rows.

    Sessions are built by the sessionizer worker using an inactivity-gap
    heuristic (default 30 min).  If the client already supplies session_id
    on events, that is used instead of the gap heuristic.

    One row = one contiguous listening session for one user.
    """

    __tablename__ = "listen_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_key = Column(String(192), nullable=False, unique=True, index=True)  # user_id:seq or client session_id
    user_id = Column(String(128), nullable=False, index=True)
    started_at = Column(Integer, nullable=False)  # Unix epoch of first event
    ended_at = Column(Integer, nullable=False)  # Unix epoch of last event
    duration_s = Column(Integer, nullable=False)  # ended_at - started_at

    # Counts
    track_count = Column(Integer, nullable=False, default=0)
    play_count = Column(Integer, nullable=False, default=0)
    skip_count = Column(Integer, nullable=False, default=0)
    like_count = Column(Integer, nullable=False, default=0)
    dislike_count = Column(Integer, nullable=False, default=0)
    seek_count = Column(Integer, nullable=False, default=0)

    # Rates (pre-computed for fast feature lookups)
    skip_rate = Column(Float, nullable=True)  # skip_count / max(play_count, 1)
    avg_completion = Column(Float, nullable=True)  # mean play_end value

    # Total listening time (sum of dwell_ms across events, when available)
    total_dwell_ms = Column(Integer, nullable=True)

    # Dominant context (most frequent non-null value)
    dominant_context_type = Column(String(32), nullable=True)
    dominant_device_type = Column(String(32), nullable=True)

    # Time context (from first event in session)
    hour_of_day = Column(Integer, nullable=True)  # 0–23
    day_of_week = Column(Integer, nullable=True)  # 1–7

    # Bookkeeping
    event_id_min = Column(Integer, nullable=False)  # earliest event.id in session
    event_id_max = Column(Integer, nullable=False)  # latest event.id in session
    built_at = Column(Integer, nullable=False)  # when this row was materialised

    __table_args__ = (Index("ix_sessions_user_ts", "user_id", "started_at"),)


# ---------------------------------------------------------------------------
# Track interactions  (Phase 2 – materialised per user×track)
# ---------------------------------------------------------------------------


class TrackInteraction(Base):
    """
    Aggregated interaction scores per (user, track).

    Updated incrementally by the scoring worker.  The satisfaction_score
    is a weighted combination of engagement signals used as the training
    label for the ranking model.
    """

    __tablename__ = "track_interactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    track_id = Column(String(128), nullable=False, index=True)

    # Raw counts
    play_count = Column(Integer, nullable=False, default=0)
    skip_count = Column(Integer, nullable=False, default=0)
    like_count = Column(Integer, nullable=False, default=0)
    dislike_count = Column(Integer, nullable=False, default=0)
    repeat_count = Column(Integer, nullable=False, default=0)
    playlist_add_count = Column(Integer, nullable=False, default=0)
    queue_add_count = Column(Integer, nullable=False, default=0)

    # Dwell / completion
    total_dwell_ms = Column(Integer, nullable=True)
    avg_completion = Column(Float, nullable=True)  # mean play_end value (0–1)

    # Skip granularity (derived from dwell_ms)
    early_skip_count = Column(Integer, nullable=False, default=0)  # dwell < 2s
    mid_skip_count = Column(Integer, nullable=False, default=0)  # 2s ≤ dwell < 30s
    full_listen_count = Column(Integer, nullable=False, default=0)  # dwell ≥ 30s or completion ≥ 0.8

    # Seek intensity
    total_seekfwd = Column(Integer, nullable=False, default=0)
    total_seekbk = Column(Integer, nullable=False, default=0)

    # Temporal
    first_played_at = Column(Integer, nullable=True)
    last_played_at = Column(Integer, nullable=True)

    # The computed satisfaction score (main training label)
    satisfaction_score = Column(Float, nullable=True)

    # Bookkeeping: highest event.id already folded in, for incremental updates
    last_event_id = Column(Integer, nullable=False, default=0)
    updated_at = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "track_id", name="uq_user_track"),
        Index("ix_interactions_user_track", "user_id", "track_id"),
        Index("ix_interactions_satisfaction", "user_id", "satisfaction_score"),
    )


class ScanLog(Base):
    """Recent per-file log entries for a scan. Kept as a ring buffer (latest N)."""

    __tablename__ = "scan_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(Integer, ForeignKey("library_scan_state.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(Integer, nullable=False, default=lambda: int(time.time()))
    level = Column(String(8), nullable=False, default="info")  # ok, skip, fail, info
    filename = Column(String(255), nullable=True)
    message = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Music discovery  (Last.fm + Lidarr)
# ---------------------------------------------------------------------------


class DiscoveryRequest(Base):
    """
    A discovered artist from Last.fm that was (or will be) sent to Lidarr.

    One row per unique artist globally — Lidarr's library is shared across
    all users, so the same artist should not be requested twice.
    """

    __tablename__ = "discovery_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False)  # who triggered discovery
    artist_name = Column(String(512), nullable=False)
    artist_mbid = Column(String(64), nullable=True)  # MusicBrainz ID from Last.fm
    source = Column(String(32), nullable=False)  # lastfm_similar | lastfm_genre
    seed_artist = Column(String(512), nullable=True)  # library artist that triggered lookup
    seed_genre = Column(String(256), nullable=True)  # genre tag that triggered lookup
    similarity_score = Column(Float, nullable=True)  # 0-1 from Last.fm match field
    status = Column(String(16), nullable=False, default="pending")  # pending|sent|in_lidarr|failed
    lidarr_artist_id = Column(Integer, nullable=True)  # Lidarr's internal ID after add
    error_message = Column(Text, nullable=True)
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))
    updated_at = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("artist_mbid", name="uq_discovery_mbid"),
        Index("ix_discovery_user_status", "user_id", "status"),
        Index("ix_discovery_created", "created_at"),
    )


class FillLibraryRequest(Base):
    """
    An album queued for download by the Fill Library pipeline.

    The pipeline queries AcousticBrainz Lookup for tracks matching a user's
    taste profile, groups results by album, and sends the best-matching
    albums to Lidarr for download.  One row per album per run.
    """

    __tablename__ = "fill_library_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False)
    artist_name = Column(String(512), nullable=False)
    artist_mbid = Column(String(64), nullable=True)
    album_name = Column(String(512), nullable=True)
    album_mbid = Column(String(64), nullable=True)  # MB release group ID
    matched_tracks = Column(Integer, nullable=False, default=1)
    avg_distance = Column(Float, nullable=True)
    best_distance = Column(Float, nullable=True)
    status = Column(String(24), nullable=False, default="pending")
    # pending → artist_added → album_monitored → sent → failed / skipped
    lidarr_artist_id = Column(Integer, nullable=True)
    lidarr_album_id = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))

    __table_args__ = (
        Index("ix_fill_lib_user_status", "user_id", "status"),
        Index("ix_fill_lib_album_mbid", "album_mbid"),
        Index("ix_fill_lib_created", "created_at"),
    )


class ScrobbleQueue(Base):
    """
    Pending Last.fm scrobbles.  Written on qualifying play_end events,
    processed in batches by the background worker.  Survives restarts.
    """

    __tablename__ = "scrobble_queue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    track_id = Column(String(128), nullable=False)
    artist = Column(String(512), nullable=False)
    track_title = Column(String(512), nullable=False)
    album = Column(String(512), nullable=True)
    duration_s = Column(Integer, nullable=True)
    timestamp = Column(Integer, nullable=False)  # when the track was played
    status = Column(String(16), nullable=False, default="pending")  # pending|sent|failed
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))

    __table_args__ = (Index("ix_scrobble_status", "status"),)


# ---------------------------------------------------------------------------
# Charts  (Last.fm global / genre / country charts)
# ---------------------------------------------------------------------------


class ChartEntry(Base):
    """
    A single entry in a chart snapshot (e.g. position #3 in global top tracks).

    Charts are rebuilt periodically from Last.fm and matched against the local
    library.  chart_type + scope + position uniquely identify an entry.
    """

    __tablename__ = "chart_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chart_type = Column(String(32), nullable=False, index=True)  # top_tracks, top_artists
    scope = Column(String(128), nullable=False, index=True)  # global, tag:rock, geo:germany
    position = Column(Integer, nullable=False)  # 0-based chart position

    # Track/artist info from Last.fm
    track_title = Column(String(512), nullable=True)  # null for artist charts
    artist_name = Column(String(512), nullable=False)
    artist_mbid = Column(String(64), nullable=True)
    playcount = Column(Integer, nullable=True)
    listeners = Column(Integer, nullable=True)

    # Image URL from Last.fm (best available size)
    image_url = Column(String(1024), nullable=True)

    # Library matching
    matched_track_id = Column(String(128), nullable=True, index=True)  # set if matched to library
    in_library = Column(Boolean, nullable=False, default=False)
    library_track_count = Column(Integer, nullable=True)  # for artist charts: how many tracks in library

    fetched_at = Column(Integer, nullable=False)  # when this chart was fetched

    __table_args__ = (Index("ix_chart_type_scope_pos", "chart_type", "scope", "position"),)


# ---------------------------------------------------------------------------
# Cover art cache  (fallback artwork for tracks not in the local library)
# ---------------------------------------------------------------------------


class CoverArtCache(Base):
    """
    Cached cover art URLs for tracks that are not (yet) in the local library.

    Last.fm stopped distributing real track/album images in ~2020, so chart
    entries that don't match the local library have no artwork.  This table
    caches the result of looking up cover art from an external source
    (currently Spotizerr's Spotify search) so we don't repeatedly hit the
    upstream API across chart rebuilds and UI renders.

    Once a track enters the local library and gets synced to the media server,
    the chart API prefers the media server's cover URL and this cached entry
    becomes a passive fallback — intentionally left in place for resilience
    if the media server is unreachable.

    Key is the normalised (artist, title) pair so lookups survive casing,
    punctuation, and "The" prefix differences.
    """

    __tablename__ = "cover_art_cache"

    artist_norm = Column(String(256), primary_key=True)
    title_norm = Column(String(256), primary_key=True)

    url = Column(String(1024), nullable=True)  # nullable = "looked up, found nothing"
    source = Column(String(32), nullable=False)  # spotizerr | deezer | itunes | ...
    fetched_at = Column(Integer, nullable=False, default=lambda: int(time.time()))

    __table_args__ = (Index("ix_cover_art_fetched", "fetched_at"),)


# ---------------------------------------------------------------------------
# Downloads  (Spotizerr proxy)
# ---------------------------------------------------------------------------


class DownloadRequest(Base):
    """
    A track download requested through the Spotizerr proxy.

    GrooveIQ acts as a proxy: the frontend only needs to talk to GrooveIQ,
    which forwards search/download requests to the configured Spotizerr instance.
    """

    __tablename__ = "download_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    spotify_id = Column(String(64), nullable=True, index=True)  # nullable: Soulseek downloads have no Spotify ID
    task_id = Column(String(128), nullable=True, index=True)  # Spotizerr/spotdl task ID
    status = Column(String(32), nullable=False, default="pending")
    # pending | downloading | duplicate | completed | error
    source = Column(String(32), nullable=False, default="spotdl")
    # "spotdl" | "spotizerr" | "soulseek"

    # Track metadata (from search results)
    track_title = Column(String(512), nullable=True)
    artist_name = Column(String(512), nullable=True)
    album_name = Column(String(512), nullable=True)
    cover_url = Column(String(1024), nullable=True)

    # Soulseek-specific fields (slskd)
    slskd_username = Column(String(256), nullable=True)  # Soulseek peer username
    slskd_filename = Column(String(1024), nullable=True)  # Remote file path on peer
    slskd_transfer_id = Column(String(128), nullable=True)  # slskd transfer GUID

    # Cascade attempt log: list of {backend, success, status, task_id, error, ...}
    # Records every backend that was tried for this request, in order, so users
    # can see *why* a download landed on a particular backend (or why it failed).
    attempts = Column(JSON, nullable=True)

    # Who requested it
    requested_by = Column(String(128), nullable=True)  # API key identity

    error_message = Column(Text, nullable=True)
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))
    updated_at = Column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_download_status", "status"),
        Index("ix_download_created", "created_at"),
    )


# ---------------------------------------------------------------------------
# Algorithm config  (tunable pipeline weights & hyperparameters)
# ---------------------------------------------------------------------------


class AlgorithmConfig(Base):
    """
    A versioned snapshot of all tunable algorithm parameters.

    Only one row is active at a time (is_active=True).  Each save creates
    a new version so the history is auditable and rollback is trivial.
    """

    __tablename__ = "algorithm_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Integer, nullable=False)
    name = Column(String(256), nullable=True)  # optional human label
    config = Column(JSON, nullable=False)  # full config dict
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))
    created_by = Column(String(128), nullable=True)  # API key identity

    __table_args__ = (
        Index("ix_algo_config_active", "is_active"),
        Index("ix_algo_config_version", "version"),
    )


# ---------------------------------------------------------------------------
# Download routing config  (priority chains + quality fallback policy)
# ---------------------------------------------------------------------------


class DownloadRoutingConfig(Base):
    """
    A versioned snapshot of download backend routing policy.

    Controls which backends are tried, in what order, for which purpose
    (individual on-demand downloads, per-track bulk, album-level bulk),
    plus quality fallback thresholds and parallel-search opt-ins.

    Same versioning + active-row semantics as AlgorithmConfig.
    """

    __tablename__ = "download_routing_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Integer, nullable=False)
    name = Column(String(256), nullable=True)
    config = Column(JSON, nullable=False)
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))
    created_by = Column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_dl_routing_active", "is_active"),
        Index("ix_dl_routing_version", "version"),
    )


# ---------------------------------------------------------------------------
# Lidarr backfill  (drain Lidarr's wanted queue through streamrip-api)
# ---------------------------------------------------------------------------


class LidarrBackfillConfig(Base):
    """
    A versioned snapshot of the Lidarr backfill policy.

    Controls which Lidarr queues are drained, the sliding-window rate cap,
    fuzzy-match thresholds, retry behaviour, and post-download import
    triggers. Same versioning + active-row semantics as AlgorithmConfig.
    """

    __tablename__ = "lidarr_backfill_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Integer, nullable=False)
    name = Column(String(256), nullable=True)
    config = Column(JSON, nullable=False)
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(Integer, nullable=False, default=lambda: int(time.time()))
    created_by = Column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_lbf_config_active", "is_active"),
        Index("ix_lbf_config_version", "version"),
    )


class LidarrBackfillRequest(Base):
    """
    Per-album state for the Lidarr backfill engine.

    One row per Lidarr album that has been picked up by the engine.
    The ``status`` column drives the state machine; ``created_at`` is the
    sliding-window key for the rate limiter (rows in the last hour count
    against the per-hour cap).

    State machine:
        queued → downloading → complete
                            ↘ failed → (cooldown) → downloading → ...
                            ↘ permanently_skipped (after max_attempts)
        no_match (couldn't find a streamrip match)
        skipped (dry-run; or filtered out)
    """

    __tablename__ = "lidarr_backfill_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lidarr_album_id = Column(Integer, nullable=False, unique=True, index=True)
    mb_album_id = Column(String(64), nullable=True, index=True)
    artist = Column(String(255), nullable=False)
    album_title = Column(String(255), nullable=False)

    source = Column(String(16), nullable=False)  # 'missing' | 'cutoff'
    match_score = Column(Float, nullable=True)
    picked_service = Column(String(32), nullable=True)
    picked_album_id = Column(String(64), nullable=True)
    streamrip_task_id = Column(String(64), nullable=True)

    # State machine — see class docstring for transitions
    status = Column(String(24), nullable=False, index=True)

    attempt_count = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(Integer, nullable=True)
    next_retry_at = Column(Integer, nullable=True, index=True)
    last_error = Column(String(1024), nullable=True)

    # The rate-limit window query (created_at > now - 1h) hits this every tick.
    created_at = Column(Integer, nullable=False, index=True, default=lambda: int(time.time()))
    updated_at = Column(Integer, nullable=False, default=lambda: int(time.time()))

    __table_args__ = (
        Index("ix_lbf_status_retry", "status", "next_retry_at"),
        Index("ix_lbf_created", "created_at"),
    )


class PlaylistTrack(Base):
    """Ordered track within a playlist."""

    __tablename__ = "playlist_tracks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False, index=True)
    track_id = Column(String(128), nullable=False)
    position = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("playlist_id", "position", name="uq_playlist_position"),
        Index("ix_playlist_track_pos", "playlist_id", "position"),
    )


# ---------------------------------------------------------------------------
# Recommendation audit  (always-on persistence of /v1/recommend internals)
# ---------------------------------------------------------------------------


class RecommendationRequestAudit(Base):
    """
    One row per /v1/recommend (or radio batch) call.

    Persists the request context, candidate-source breakdown, and timing
    so past requests can be browsed in the dashboard or replayed offline
    against the current ranker / config to evaluate tuning impact.

    Append-only.  Pruned by a daily cleanup job (RECO_AUDIT_RETENTION_DAYS).
    """

    __tablename__ = "recommendation_request_audits"

    request_id = Column(String(64), primary_key=True)
    user_id = Column(String(255), nullable=False, index=True)
    created_at = Column(BigInteger, nullable=False, index=True)
    surface = Column(String(32), nullable=False)  # home, radio, search, recommend_api
    seed_track_id = Column(String(255), nullable=True)
    context_id = Column(String(255), nullable=True)  # radio session_id, playlist_id, etc.
    model_version = Column(String(64), nullable=False)
    config_version = Column(Integer, nullable=False, default=0)
    request_context = Column(JSON, nullable=True)  # {device_type, output_type, hour_of_day, ...}
    candidates_total = Column(Integer, nullable=False, default=0)
    candidates_by_source = Column(JSON, nullable=True)  # {"content": 50, "cf": 30, ...}
    duration_ms = Column(Integer, nullable=False, default=0)
    limit_requested = Column(Integer, nullable=False, default=25)

    candidates = relationship(
        "RecommendationCandidateAudit",
        back_populates="request",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (Index("idx_reco_audit_user_time", "user_id", "created_at"),)


class RecommendationCandidateAudit(Base):
    """
    One row per candidate considered in a recommendation request.

    Captures the candidate's pre-rerank rank, post-rerank rank, source
    attribution, reranker actions, and the full feature vector that fed
    into the ranker.  This is the data needed to answer "why was track X
    surfaced at position N?" and to replay the request against newer models.
    """

    __tablename__ = "recommendation_candidate_audits"

    # SQLite only treats ``INTEGER PRIMARY KEY`` (not BIGINT) as the rowid
    # alias for autoincrement, so we use Integer here for cross-DB compat.
    # PostgreSQL maps Integer to int4 which still supports 2.1B rows.
    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(
        String(64),
        ForeignKey("recommendation_request_audits.request_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    track_id = Column(String(255), nullable=False, index=True)

    # A candidate may surface from multiple sources (e.g. content + sasrec).
    sources = Column(JSON, nullable=True)  # ["content", "sasrec"]

    # Pre-rerank
    raw_score = Column(Float, nullable=False, default=0.0)
    pre_rerank_position = Column(Integer, nullable=False, default=-1)

    # Post-rerank — null final_position means filtered out.
    final_score = Column(Float, nullable=True)
    final_position = Column(Integer, nullable=True)
    shown = Column(Boolean, nullable=False, default=False, index=True)

    # Why? — full audit data
    reranker_actions = Column(JSON, nullable=True)  # ["freshness_boost", "exploration_slot"]
    feature_vector = Column(JSON, nullable=True)  # the ranker features

    request = relationship("RecommendationRequestAudit", back_populates="candidates")

    __table_args__ = (Index("idx_reco_audit_candidate_track", "request_id", "track_id"),)
