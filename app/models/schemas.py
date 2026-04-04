"""
GrooveIQ – Pydantic request/response schemas.

Separate from ORM models to keep API contracts stable independent of
database layout changes.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    # Playback lifecycle
    PLAY_START   = "play_start"    # user started playing a track
    PLAY_END     = "play_end"      # track finished or player moved on (value = % completed)
    SKIP         = "skip"          # user explicitly skipped (value = seconds elapsed)
    PAUSE        = "pause"         # playback paused (value = seconds elapsed)
    RESUME       = "resume"        # playback resumed after pause

    # Engagement
    LIKE         = "like"          # explicit thumbs-up / heart
    DISLIKE      = "dislike"       # explicit thumbs-down
    RATING       = "rating"        # star rating (value = 1–5)
    PLAYLIST_ADD    = "playlist_add"     # user added track to any playlist
    PLAYLIST_REMOVE = "playlist_remove"  # user removed track from a playlist
    QUEUE_ADD       = "queue_add"        # user manually added to queue

    # Playback adjustments (implicit quality signals)
    SEEK_BACK    = "seek_back"     # user scrubbed backward (value = seconds jumped back)
    SEEK_FORWARD = "seek_forward"  # user scrubbed forward (value = seconds skipped)
    REPEAT       = "repeat"        # user hit repeat on a single track
    VOLUME_UP    = "volume_up"     # significant volume increase during track
    VOLUME_DOWN  = "volume_down"   # significant volume decrease

    # Recommendation / impression
    RECO_IMPRESSION = "reco_impression"  # track was shown as a recommendation (not necessarily played)


# ---------------------------------------------------------------------------
# Inbound: single event
# ---------------------------------------------------------------------------

class EventCreate(BaseModel):
    """A single behavioral event sent by a music player."""

    user_id:    str = Field(..., min_length=1, max_length=128,
                            description="Your media server's user identifier.")
    track_id:   str = Field(..., min_length=1, max_length=128,
                            description="Your media server's track identifier.")
    event_type: EventType
    value:      Optional[float] = Field(
        None,
        description="Event-specific numeric payload. "
                    "play_end → completion ratio (0–1). "
                    "skip / pause / resume → elapsed seconds. "
                    "rating → 1–5. volume_* → 0–100."
    )
    context:    Optional[str] = Field(
        None, max_length=64,
        description="Optional context label. E.g. 'workout', 'sleep', 'commute'."
    )
    client_id:  Optional[str] = Field(None, max_length=64)
    session_id: Optional[str] = Field(None, max_length=64)

    # --- Rich behavioral / session / context signals -------------------------
    # Impression & exposure
    surface:    Optional[str] = Field(
        None, max_length=64,
        description="UI surface where the track was shown. E.g. 'home', 'search', 'now_playing', 'playlist_view'."
    )
    position:   Optional[int] = Field(
        None, ge=0,
        description="Rank position if the track was part of a recommendation list."
    )
    request_id: Optional[str] = Field(
        None, max_length=128,
        description="Ties an impression to downstream streams/actions. Shared across events from one reco request."
    )
    model_version: Optional[str] = Field(
        None, max_length=64,
        description="Which recommendation model version produced this impression."
    )

    # Sessionization
    session_position: Optional[int] = Field(
        None, ge=0,
        description="Track's ordinal position within the session (0-based)."
    )

    # Satisfaction / dwell
    dwell_ms:   Optional[int] = Field(
        None, ge=0,
        description="Milliseconds the user actually listened to this track. Used to derive skip thresholds."
    )

    # Pause buckets
    pause_duration_ms: Optional[int] = Field(
        None, ge=0,
        description="Inter-track pause duration in ms before this track started."
    )

    # Seek intensity
    num_seekfwd: Optional[int] = Field(
        None, ge=0,
        description="Number of forward seeks during this track."
    )
    num_seekbk:  Optional[int] = Field(
        None, ge=0,
        description="Number of backward seeks during this track."
    )

    # Shuffle state
    shuffle:    Optional[bool] = Field(
        None,
        description="Whether shuffle was active when this track played."
    )

    # Context / source
    context_type: Optional[str] = Field(
        None, max_length=32,
        description="Source context: 'playlist', 'album', 'radio', 'search', 'home_shelf', etc."
    )
    context_id: Optional[str] = Field(
        None, max_length=128,
        description="ID of the source context (playlist ID, album ID, radio station ID)."
    )
    context_switch: Optional[bool] = Field(
        None,
        description="True if the user just switched to a new context before this track."
    )

    # Start / end reason codes
    reason_start: Optional[str] = Field(
        None, max_length=32,
        description="Why playback started: 'autoplay', 'user_tap', 'forward_button', 'external', etc."
    )
    reason_end:   Optional[str] = Field(
        None, max_length=32,
        description="Why playback ended: 'track_done', 'user_skip', 'error', 'new_track', etc."
    )

    # Cross-device identity
    device_id:   Optional[str] = Field(
        None, max_length=128,
        description="Stable device identifier."
    )
    device_type: Optional[str] = Field(
        None, max_length=32,
        description="Device class: 'mobile', 'desktop', 'speaker', 'car', 'web', etc."
    )

    # Local time context (client-side — server only has UTC timestamp)
    hour_of_day: Optional[int] = Field(
        None, ge=0, le=23,
        description="Client's local hour (0–23)."
    )
    day_of_week: Optional[int] = Field(
        None, ge=1, le=7,
        description="Client's local day of week (1=Monday … 7=Sunday, ISO 8601)."
    )
    timezone: Optional[str] = Field(
        None, max_length=64,
        description="IANA timezone of the client, e.g. 'Europe/Zurich'."
    )

    # Audio output
    output_type: Optional[str] = Field(
        None, max_length=32,
        description="Audio output type: 'headphones', 'speaker', 'bluetooth_speaker', 'car_audio', 'built_in', 'airplay', etc."
    )
    output_device_name: Optional[str] = Field(
        None, max_length=128,
        description="Friendly name of the audio output device, e.g. 'AirPods Pro', 'Sonos Living Room'."
    )
    bluetooth_connected: Optional[bool] = Field(
        None,
        description="Whether audio is routed over Bluetooth."
    )

    # Location
    latitude:  Optional[float] = Field(
        None, ge=-90, le=90,
        description="GPS latitude of the client."
    )
    longitude: Optional[float] = Field(
        None, ge=-180, le=180,
        description="GPS longitude of the client."
    )
    location_label: Optional[str] = Field(
        None, max_length=32,
        description="Semantic location label: 'home', 'work', 'gym', 'commute', etc."
    )

    timestamp:  Optional[int] = Field(
        None,
        description="Unix timestamp (UTC). Defaults to server time if omitted. "
                    "Rejected if more than 24 hours in the past or in the future."
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def default_timestamp(cls, v):
        return v if v is not None else int(time.time())

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        now = int(time.time())
        if v > now + 300:          # max 5 min in future (clock drift)
            raise ValueError("timestamp is too far in the future")
        if v < now - 86_400:       # max 24 hours in the past
            raise ValueError("timestamp is more than 24 hours in the past")
        return v

    @field_validator("value")
    @classmethod
    def validate_value(cls, v):
        if v is not None and (v < -1 or v > 100_000):
            raise ValueError("value out of acceptable range")
        return v

    model_config = {"use_enum_values": True}


# ---------------------------------------------------------------------------
# Inbound: batch of events
# ---------------------------------------------------------------------------

class EventBatch(BaseModel):
    """Up to 50 events in a single request (reduces client-side HTTP overhead)."""

    events: List[EventCreate] = Field(..., min_length=1)

    @field_validator("events")
    @classmethod
    def check_batch_size(cls, v):
        from app.core.config import settings
        if len(v) > settings.EVENT_BATCH_MAX:
            raise ValueError(
                f"Batch exceeds maximum of {settings.EVENT_BATCH_MAX} events."
            )
        return v


# ---------------------------------------------------------------------------
# Outbound: event response
# ---------------------------------------------------------------------------

class EventResponse(BaseModel):
    accepted:  int = Field(..., description="Number of events accepted.")
    rejected:  int = Field(..., description="Number of events rejected (see errors).")
    errors:    List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Track features (Phase 3 response)
# ---------------------------------------------------------------------------

class MoodTag(BaseModel):
    label:      str
    confidence: float

class TrackFeaturesResponse(BaseModel):
    track_id:         str
    duration:         Optional[float]
    bpm:              Optional[float]
    key:              Optional[str]
    mode:             Optional[str]
    energy:           Optional[float]
    danceability:     Optional[float]
    valence:          Optional[float]
    acousticness:     Optional[float]
    instrumentalness: Optional[float]
    mood_tags:        Optional[List[MoodTag]]
    analyzed_at:      Optional[int]
    analysis_version: Optional[str]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------

class ScanTriggerResponse(BaseModel):
    message:     str
    scan_id:     int
    status:      str

class ScanStatusResponse(BaseModel):
    scan_id:         int
    status:          str
    files_found:     int
    files_analyzed:  int
    files_failed:    int
    started_at:      int
    ended_at:        Optional[int]
    last_error:      Optional[str]


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Outbound: full event read (GET /v1/events)
# ---------------------------------------------------------------------------

class ListenEventRead(BaseModel):
    """All fields stored for a single event, returned by the query endpoint."""

    id:           int
    user_id:      str
    track_id:     str
    event_type:   str
    value:        Optional[float] = None
    context:      Optional[str] = None
    client_id:    Optional[str] = None
    session_id:   Optional[str] = None
    timestamp:    int

    # Rich signals
    surface:          Optional[str] = None
    position:         Optional[int] = None
    request_id:       Optional[str] = None
    model_version:    Optional[str] = None
    session_position: Optional[int] = None
    dwell_ms:         Optional[int] = None
    pause_duration_ms: Optional[int] = None
    num_seekfwd:      Optional[int] = None
    num_seekbk:       Optional[int] = None
    shuffle:          Optional[bool] = None
    context_type:     Optional[str] = None
    context_id:       Optional[str] = None
    context_switch:   Optional[bool] = None
    reason_start:     Optional[str] = None
    reason_end:       Optional[str] = None
    device_id:        Optional[str] = None
    device_type:      Optional[str] = None

    # Local time context
    hour_of_day:      Optional[int] = None
    day_of_week:      Optional[int] = None
    timezone:         Optional[str] = None

    # Audio output
    output_type:         Optional[str] = None
    output_device_name:  Optional[str] = None
    bluetooth_connected: Optional[bool] = None

    # Location
    latitude:         Optional[float] = None
    longitude:        Optional[float] = None
    location_label:   Optional[str] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

class PlaylistStrategy(str, Enum):
    FLOW = "flow"
    MOOD = "mood"
    ENERGY_CURVE = "energy_curve"
    KEY_COMPATIBLE = "key_compatible"


class PlaylistCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    strategy: PlaylistStrategy
    seed_track_id: Optional[str] = Field(None, max_length=128)
    params: Optional[Dict[str, Any]] = None
    max_tracks: int = Field(25, ge=5, le=100)

    @model_validator(mode="after")
    def validate_strategy_params(self):
        if self.strategy in (PlaylistStrategy.FLOW, PlaylistStrategy.KEY_COMPATIBLE):
            if not self.seed_track_id:
                raise ValueError(f"seed_track_id is required for '{self.strategy}' strategy")
        if self.strategy == PlaylistStrategy.MOOD:
            mood = (self.params or {}).get("mood")
            if not mood:
                raise ValueError("params.mood is required for 'mood' strategy")
        if self.strategy == PlaylistStrategy.ENERGY_CURVE:
            curve = (self.params or {}).get("curve")
            valid = ("ramp_up", "cool_down", "ramp_up_cool_down", "steady_high", "steady_low")
            if curve not in valid:
                raise ValueError(f"params.curve must be one of {valid}")
        return self

    model_config = {"use_enum_values": True}


class PlaylistTrackItem(BaseModel):
    position: int
    track_id: str
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    file_path: Optional[str] = None
    bpm: Optional[float] = None
    key: Optional[str] = None
    mode: Optional[str] = None
    energy: Optional[float] = None
    danceability: Optional[float] = None
    valence: Optional[float] = None
    mood_tags: Optional[List[MoodTag]] = None
    duration: Optional[float] = None


class PlaylistResponse(BaseModel):
    id: int
    name: str
    strategy: str
    seed_track_id: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    track_count: int
    total_duration: Optional[float] = None
    created_at: int

    model_config = {"from_attributes": True}


class PlaylistDetailResponse(PlaylistResponse):
    tracks: List[PlaylistTrackItem]


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Last.fm integration
# ---------------------------------------------------------------------------

class LastfmConnectRequest(BaseModel):
    """Connect a user's Last.fm account (read-only profile enrichment)."""
    lastfm_username: str = Field(..., min_length=1, max_length=128)


class LastfmConnectResponse(BaseModel):
    status: str
    username: str
    scrobbling_enabled: bool


class LastfmProfileResponse(BaseModel):
    username: Optional[str] = None
    scrobbling_enabled: bool = False
    synced_at: Optional[int] = None
    profile: Optional[Dict[str, Any]] = None


class UserCreate(BaseModel):
    user_id:      str = Field(..., min_length=1, max_length=128)
    display_name: Optional[str] = Field(None, max_length=255)


class UserUpdate(BaseModel):
    """Update a user's mutable fields. At least one field must be provided."""
    user_id:      Optional[str] = Field(None, min_length=1, max_length=128,
                                        description="New username. Must be unique. Cascades to all event/session/interaction tables.")
    display_name: Optional[str] = Field(None, max_length=255)

    @model_validator(mode="after")
    def at_least_one_field(self):
        if self.user_id is None and self.display_name is None:
            raise ValueError("At least one of user_id or display_name must be provided.")
        return self


class UserResponse(BaseModel):
    uid:          int = Field(..., description="Stable numeric user identifier (never changes).")
    user_id:      str = Field(..., description="Username / media server identifier (can be updated).")
    display_name: Optional[str] = None
    created_at:   int
    last_seen:    Optional[int] = None

    model_config = {"from_attributes": True}
