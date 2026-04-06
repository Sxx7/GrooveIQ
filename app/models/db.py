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
from typing import Optional

from sqlalchemy import (
    JSON,
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
from sqlalchemy.orm import DeclarativeBase


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

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(String(128), nullable=False, index=True)
    track_id    = Column(String(128), nullable=False, index=True)
    event_type  = Column(String(32),  nullable=False, index=True)
    value       = Column(Float,       nullable=True)
    context     = Column(String(64),  nullable=True)   # "morning_commute", "workout", etc.
    client_id   = Column(String(64),  nullable=True)   # which app sent this
    timestamp   = Column(Integer,     nullable=False, index=True, default=lambda: int(time.time()))
    session_id  = Column(String(64),  nullable=True, index=True)

    # --- Rich behavioral / session / context signals -------------------------
    # Impression & exposure
    surface          = Column(String(64),  nullable=True)   # home, search, now_playing, playlist_view
    position         = Column(Integer,     nullable=True)   # rank position in reco list
    request_id       = Column(String(128), nullable=True, index=True)  # ties impressions → streams
    model_version    = Column(String(64),  nullable=True)   # reco model version

    # Sessionization
    session_position = Column(Integer,     nullable=True)   # track ordinal in session

    # Satisfaction / dwell
    dwell_ms         = Column(Integer,     nullable=True)   # ms listened

    # Pause buckets
    pause_duration_ms = Column(Integer,    nullable=True)   # inter-track gap ms

    # Seek intensity
    num_seekfwd      = Column(Integer,     nullable=True)
    num_seekbk       = Column(Integer,     nullable=True)

    # Shuffle state
    shuffle          = Column(Boolean,     nullable=True)

    # Context / source
    context_type     = Column(String(32),  nullable=True)   # playlist, album, radio, search, home_shelf
    context_id       = Column(String(128), nullable=True)   # playlist/album/radio ID
    context_switch   = Column(Boolean,     nullable=True)   # user just switched context

    # Start / end reason codes
    reason_start     = Column(String(32),  nullable=True)   # autoplay, user_tap, forward_button, external
    reason_end       = Column(String(32),  nullable=True)   # track_done, user_skip, error, new_track

    # Cross-device identity
    device_id        = Column(String(128), nullable=True, index=True)
    device_type      = Column(String(32),  nullable=True)   # mobile, desktop, speaker, car, web

    # Local time context (client-side)
    hour_of_day      = Column(Integer,     nullable=True)   # 0–23
    day_of_week      = Column(Integer,     nullable=True)   # 1=Mon … 7=Sun (ISO 8601)
    timezone         = Column(String(64),  nullable=True)   # IANA, e.g. "Europe/Zurich"

    # Audio output
    output_type         = Column(String(32),  nullable=True)   # headphones, speaker, bluetooth_speaker, …
    output_device_name  = Column(String(128), nullable=True)   # "AirPods Pro", "Sonos Living Room"
    bluetooth_connected = Column(Boolean,     nullable=True)

    # Location
    latitude         = Column(Float,       nullable=True)
    longitude        = Column(Float,       nullable=True)
    location_label   = Column(String(32),  nullable=True)   # home, work, gym, commute

    __table_args__ = (
        Index("ix_events_user_track", "user_id", "track_id"),
        Index("ix_events_user_ts",    "user_id", "timestamp"),
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

    id              = Column(Integer, primary_key=True, autoincrement=True)
    track_id        = Column(String(128), nullable=False, unique=True, index=True)

    # Media server mapping — populated when matching scanner tracks to Navidrome/Plex IDs
    external_track_id = Column(String(128), nullable=True, unique=True, index=True)

    # Track metadata (populated from media server sync + ID3 tags)
    title           = Column(String(512), nullable=True)
    artist          = Column(String(512), nullable=True)
    album           = Column(String(512), nullable=True)
    album_artist    = Column(String(512), nullable=True)
    genre           = Column(String(512), nullable=True)  # comma-separated, e.g. "Hip-Hop, Rap"
    track_number    = Column(Integer,     nullable=True)
    duration_ms     = Column(Integer,     nullable=True)   # from ID3 tags (integer ms)
    musicbrainz_track_id = Column(String(64), nullable=True)

    # File metadata
    file_path       = Column(Text,    nullable=False)
    file_hash       = Column(String(64), nullable=True)  # SHA-256, detects file changes
    duration        = Column(Float,   nullable=True)     # seconds
    analyzed_at     = Column(Integer, nullable=True)     # Unix timestamp

    # --- Rhythm ---
    bpm             = Column(Float,   nullable=True, index=True)
    bpm_confidence  = Column(Float,   nullable=True)

    # --- Tonal ---
    key             = Column(String(4),  nullable=True)   # e.g. "C", "F#"
    mode            = Column(String(6),  nullable=True)   # "major" | "minor"
    key_confidence  = Column(Float,      nullable=True)

    # --- Dynamics ---
    loudness        = Column(Float,   nullable=True)      # LUFS
    dynamic_range   = Column(Float,   nullable=True)

    # --- High-level descriptors (0.0 – 1.0) ---
    energy          = Column(Float,   nullable=True, index=True)
    danceability    = Column(Float,   nullable=True)
    valence         = Column(Float,   nullable=True)      # musical positivity
    acousticness    = Column(Float,   nullable=True)
    instrumentalness = Column(Float,  nullable=True)
    speechiness     = Column(Float,   nullable=True)

    # --- Mood (multi-label, stored as JSON list of {label, confidence}) ---
    mood_tags       = Column(JSON,    nullable=True)      # [{"label": "happy", "confidence": 0.82}]

    # --- Full feature vector for FAISS (serialized numpy array as bytes) ---
    embedding       = Column(Text,    nullable=True)      # base64-encoded float32 array

    # --- Raw Essentia output (for future re-analysis without re-running) ---
    raw_features    = Column(JSON,    nullable=True)

    analysis_version = Column(String(16), nullable=True)  # track model version changes
    analysis_error   = Column(Text,       nullable=True)  # null = success


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

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(String(128), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=True)
    created_at  = Column(Integer, nullable=False, default=lambda: int(time.time()))
    last_seen   = Column(Integer, nullable=True)
    is_active   = Column(Boolean, nullable=False, default=True)

    # Cached taste profile (JSON, updated by the background worker)
    taste_profile = Column(JSON, nullable=True)
    profile_updated_at = Column(Integer, nullable=True)

    # Last.fm integration (per-user, opt-in)
    lastfm_username    = Column(String(128), nullable=True)
    lastfm_session_key = Column(String(512), nullable=True)   # Fernet-encrypted
    lastfm_cache       = Column(JSON,        nullable=True)   # cached Last.fm profile data
    lastfm_synced_at   = Column(Integer,     nullable=True)   # Unix timestamp of last sync

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

    id              = Column(Integer, primary_key=True, autoincrement=True)
    scan_started_at = Column(Integer, nullable=False)
    scan_ended_at   = Column(Integer, nullable=True)
    status          = Column(String(16), nullable=False, default="running")
    files_found     = Column(Integer, nullable=False, default=0)
    files_analyzed  = Column(Integer, nullable=False, default=0)
    files_skipped   = Column(Integer, nullable=False, default=0)
    files_failed    = Column(Integer, nullable=False, default=0)
    current_file    = Column(Text,    nullable=True)      # path being analyzed right now
    last_error      = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

class Playlist(Base):
    """
    A generated playlist. Tracks are stored in the PlaylistTrack join table.
    Strategy records how the playlist was built so it can be regenerated.
    """
    __tablename__ = "playlists"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    name           = Column(String(255), nullable=False)
    strategy       = Column(String(32),  nullable=False)   # flow, mood, energy_curve, key_compatible
    seed_track_id  = Column(String(128), nullable=True)
    params         = Column(JSON,        nullable=True)    # strategy-specific config
    track_count    = Column(Integer,     nullable=False, default=0)
    total_duration = Column(Float,       nullable=True)    # seconds
    created_by     = Column(String(128), nullable=True)    # API key hash that created this playlist
    created_at     = Column(Integer,     nullable=False, default=lambda: int(time.time()))


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

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    session_key           = Column(String(192), nullable=False, unique=True, index=True)  # user_id:seq or client session_id
    user_id               = Column(String(128), nullable=False, index=True)
    started_at            = Column(Integer, nullable=False)          # Unix epoch of first event
    ended_at              = Column(Integer, nullable=False)          # Unix epoch of last event
    duration_s            = Column(Integer, nullable=False)          # ended_at - started_at

    # Counts
    track_count           = Column(Integer, nullable=False, default=0)
    play_count            = Column(Integer, nullable=False, default=0)
    skip_count            = Column(Integer, nullable=False, default=0)
    like_count            = Column(Integer, nullable=False, default=0)
    dislike_count         = Column(Integer, nullable=False, default=0)
    seek_count            = Column(Integer, nullable=False, default=0)

    # Rates (pre-computed for fast feature lookups)
    skip_rate             = Column(Float,   nullable=True)           # skip_count / max(play_count, 1)
    avg_completion        = Column(Float,   nullable=True)           # mean play_end value

    # Total listening time (sum of dwell_ms across events, when available)
    total_dwell_ms        = Column(Integer, nullable=True)

    # Dominant context (most frequent non-null value)
    dominant_context_type = Column(String(32),  nullable=True)
    dominant_device_type  = Column(String(32),  nullable=True)

    # Time context (from first event in session)
    hour_of_day           = Column(Integer, nullable=True)           # 0–23
    day_of_week           = Column(Integer, nullable=True)           # 1–7

    # Bookkeeping
    event_id_min          = Column(Integer, nullable=False)          # earliest event.id in session
    event_id_max          = Column(Integer, nullable=False)          # latest event.id in session
    built_at              = Column(Integer, nullable=False)          # when this row was materialised

    __table_args__ = (
        Index("ix_sessions_user_ts", "user_id", "started_at"),
    )


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

    id                = Column(Integer, primary_key=True, autoincrement=True)
    user_id           = Column(String(128), nullable=False, index=True)
    track_id          = Column(String(128), nullable=False, index=True)

    # Raw counts
    play_count        = Column(Integer, nullable=False, default=0)
    skip_count        = Column(Integer, nullable=False, default=0)
    like_count        = Column(Integer, nullable=False, default=0)
    dislike_count     = Column(Integer, nullable=False, default=0)
    repeat_count      = Column(Integer, nullable=False, default=0)
    playlist_add_count = Column(Integer, nullable=False, default=0)
    queue_add_count   = Column(Integer, nullable=False, default=0)

    # Dwell / completion
    total_dwell_ms    = Column(Integer, nullable=True)
    avg_completion    = Column(Float,   nullable=True)               # mean play_end value (0–1)

    # Skip granularity (derived from dwell_ms)
    early_skip_count  = Column(Integer, nullable=False, default=0)   # dwell < 2s
    mid_skip_count    = Column(Integer, nullable=False, default=0)   # 2s ≤ dwell < 30s
    full_listen_count = Column(Integer, nullable=False, default=0)   # dwell ≥ 30s or completion ≥ 0.8

    # Seek intensity
    total_seekfwd     = Column(Integer, nullable=False, default=0)
    total_seekbk      = Column(Integer, nullable=False, default=0)

    # Temporal
    first_played_at   = Column(Integer, nullable=True)
    last_played_at    = Column(Integer, nullable=True)

    # The computed satisfaction score (main training label)
    satisfaction_score = Column(Float, nullable=True)

    # Bookkeeping: highest event.id already folded in, for incremental updates
    last_event_id     = Column(Integer, nullable=False, default=0)
    updated_at        = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "track_id", name="uq_user_track"),
        Index("ix_interactions_user_track", "user_id", "track_id"),
        Index("ix_interactions_satisfaction", "user_id", "satisfaction_score"),
    )


class ScanLog(Base):
    """Recent per-file log entries for a scan. Kept as a ring buffer (latest N)."""
    __tablename__ = "scan_logs"

    id       = Column(Integer, primary_key=True, autoincrement=True)
    scan_id  = Column(Integer, ForeignKey("library_scan_state.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(Integer, nullable=False, default=lambda: int(time.time()))
    level    = Column(String(8), nullable=False, default="info")   # ok, skip, fail, info
    filename = Column(String(255), nullable=True)
    message  = Column(Text, nullable=True)


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

    id               = Column(Integer, primary_key=True, autoincrement=True)
    user_id          = Column(String(128), nullable=False)        # who triggered discovery
    artist_name      = Column(String(512), nullable=False)
    artist_mbid      = Column(String(64),  nullable=True)         # MusicBrainz ID from Last.fm
    source           = Column(String(32),  nullable=False)        # lastfm_similar | lastfm_genre
    seed_artist      = Column(String(512), nullable=True)         # library artist that triggered lookup
    seed_genre       = Column(String(256), nullable=True)         # genre tag that triggered lookup
    similarity_score = Column(Float,       nullable=True)         # 0-1 from Last.fm match field
    status           = Column(String(16),  nullable=False, default="pending")  # pending|sent|in_lidarr|failed
    lidarr_artist_id = Column(Integer,     nullable=True)         # Lidarr's internal ID after add
    error_message    = Column(Text,        nullable=True)
    created_at       = Column(Integer,     nullable=False, default=lambda: int(time.time()))
    updated_at       = Column(Integer,     nullable=True)

    __table_args__ = (
        UniqueConstraint("artist_mbid", name="uq_discovery_mbid"),
        Index("ix_discovery_user_status", "user_id", "status"),
        Index("ix_discovery_created", "created_at"),
    )


class ScrobbleQueue(Base):
    """
    Pending Last.fm scrobbles.  Written on qualifying play_end events,
    processed in batches by the background worker.  Survives restarts.
    """
    __tablename__ = "scrobble_queue"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(String(128), nullable=False, index=True)
    track_id    = Column(String(128), nullable=False)
    artist      = Column(String(512), nullable=False)
    track_title = Column(String(512), nullable=False)
    album       = Column(String(512), nullable=True)
    duration_s  = Column(Integer,     nullable=True)
    timestamp   = Column(Integer,     nullable=False)    # when the track was played
    status      = Column(String(16),  nullable=False, default="pending")  # pending|sent|failed
    attempts    = Column(Integer,     nullable=False, default=0)
    last_error  = Column(Text,        nullable=True)
    created_at  = Column(Integer,     nullable=False, default=lambda: int(time.time()))

    __table_args__ = (
        Index("ix_scrobble_status", "status"),
    )


class PlaylistTrack(Base):
    """Ordered track within a playlist."""
    __tablename__ = "playlist_tracks"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False, index=True)
    track_id    = Column(String(128), nullable=False)
    position    = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("playlist_id", "position", name="uq_playlist_position"),
        Index("ix_playlist_track_pos", "playlist_id", "position"),
    )
