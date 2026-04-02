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
    files_failed    = Column(Integer, nullable=False, default=0)
    last_error      = Column(Text, nullable=True)
