"""
GrooveIQ – Pydantic request/response schemas.

Separate from ORM models to keep API contracts stable independent of
database layout changes.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    # Playback lifecycle
    PLAY_START = "play_start"  # user started playing a track
    PLAY_END = "play_end"  # track finished or player moved on (value = % completed)
    SKIP = "skip"  # user explicitly skipped (value = seconds elapsed)
    PAUSE = "pause"  # playback paused (value = seconds elapsed)
    RESUME = "resume"  # playback resumed after pause

    # Engagement
    LIKE = "like"  # explicit thumbs-up / heart
    DISLIKE = "dislike"  # explicit thumbs-down
    RATING = "rating"  # star rating (value = 1–5)
    PLAYLIST_ADD = "playlist_add"  # user added track to any playlist
    PLAYLIST_REMOVE = "playlist_remove"  # user removed track from a playlist
    QUEUE_ADD = "queue_add"  # user manually added to queue

    # Playback adjustments (implicit quality signals)
    SEEK_BACK = "seek_back"  # user scrubbed backward (value = seconds jumped back)
    SEEK_FORWARD = "seek_forward"  # user scrubbed forward (value = seconds skipped)
    REPEAT = "repeat"  # user hit repeat on a single track
    VOLUME_UP = "volume_up"  # significant volume increase during track
    VOLUME_DOWN = "volume_down"  # significant volume decrease

    # Recommendation / impression
    RECO_IMPRESSION = "reco_impression"  # track was shown as a recommendation (not necessarily played)


# ---------------------------------------------------------------------------
# Inbound: single event
# ---------------------------------------------------------------------------


class EventCreate(BaseModel):
    """A single behavioral event sent by a music player."""

    user_id: str = Field(..., min_length=1, max_length=128, description="Your media server's user identifier.")
    track_id: str = Field(..., min_length=1, max_length=128, description="Your media server's track identifier.")
    event_type: EventType
    value: float | None = Field(
        None,
        description="Event-specific numeric payload. "
        "play_end → completion ratio (0–1). "
        "skip / pause / resume → elapsed seconds. "
        "rating → 1–5. volume_* → 0–100.",
    )
    context: str | None = Field(
        None, max_length=64, description="Optional context label. E.g. 'workout', 'sleep', 'commute'."
    )
    client_id: str | None = Field(None, max_length=64)
    session_id: str | None = Field(None, max_length=64)

    # --- Rich behavioral / session / context signals -------------------------
    # Impression & exposure
    surface: str | None = Field(
        None,
        max_length=64,
        description="UI surface where the track was shown. E.g. 'home', 'search', 'now_playing', 'playlist_view'.",
    )
    position: int | None = Field(
        None, ge=0, description="Rank position if the track was part of a recommendation list."
    )
    request_id: str | None = Field(
        None,
        max_length=128,
        description="Ties an impression to downstream streams/actions. Shared across events from one reco request.",
    )
    model_version: str | None = Field(
        None, max_length=64, description="Which recommendation model version produced this impression."
    )

    # Sessionization
    session_position: int | None = Field(
        None, ge=0, description="Track's ordinal position within the session (0-based)."
    )

    # Satisfaction / dwell
    dwell_ms: int | None = Field(
        None, ge=0, description="Milliseconds the user actually listened to this track. Used to derive skip thresholds."
    )

    # Pause buckets
    pause_duration_ms: int | None = Field(
        None, ge=0, description="Inter-track pause duration in ms before this track started."
    )

    # Seek intensity
    num_seekfwd: int | None = Field(None, ge=0, description="Number of forward seeks during this track.")
    num_seekbk: int | None = Field(None, ge=0, description="Number of backward seeks during this track.")

    # Shuffle state
    shuffle: bool | None = Field(None, description="Whether shuffle was active when this track played.")

    # Context / source
    context_type: str | None = Field(
        None, max_length=32, description="Source context: 'playlist', 'album', 'radio', 'search', 'home_shelf', etc."
    )
    context_id: str | None = Field(
        None, max_length=128, description="ID of the source context (playlist ID, album ID, radio station ID)."
    )
    context_switch: bool | None = Field(
        None, description="True if the user just switched to a new context before this track."
    )

    # Start / end reason codes
    reason_start: str | None = Field(
        None,
        max_length=32,
        description="Why playback started: 'autoplay', 'user_tap', 'forward_button', 'external', etc.",
    )
    reason_end: str | None = Field(
        None, max_length=32, description="Why playback ended: 'track_done', 'user_skip', 'error', 'new_track', etc."
    )

    # Cross-device identity
    device_id: str | None = Field(None, max_length=128, description="Stable device identifier.")
    device_type: str | None = Field(
        None, max_length=32, description="Device class: 'mobile', 'desktop', 'speaker', 'car', 'web', etc."
    )

    # Local time context (client-side — server only has UTC timestamp)
    hour_of_day: int | None = Field(None, ge=0, le=23, description="Client's local hour (0–23).")
    day_of_week: int | None = Field(
        None, ge=1, le=7, description="Client's local day of week (1=Monday … 7=Sunday, ISO 8601)."
    )
    timezone: str | None = Field(None, max_length=64, description="IANA timezone of the client, e.g. 'Europe/Zurich'.")

    # Audio output
    output_type: str | None = Field(
        None,
        max_length=32,
        description="Audio output type: 'headphones', 'speaker', 'bluetooth_speaker', 'car_audio', 'built_in', 'airplay', etc.",
    )
    output_device_name: str | None = Field(
        None,
        max_length=128,
        description="Friendly name of the audio output device, e.g. 'AirPods Pro', 'Sonos Living Room'.",
    )
    bluetooth_connected: bool | None = Field(None, description="Whether audio is routed over Bluetooth.")

    # Location
    latitude: float | None = Field(None, ge=-90, le=90, description="GPS latitude of the client.")
    longitude: float | None = Field(None, ge=-180, le=180, description="GPS longitude of the client.")
    location_label: str | None = Field(
        None, max_length=32, description="Semantic location label: 'home', 'work', 'gym', 'commute', etc."
    )

    timestamp: int | None = Field(
        None,
        description="Unix timestamp (UTC). Defaults to server time if omitted. "
        "Rejected if more than 24 hours in the past or in the future.",
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def default_timestamp(cls, v):
        return v if v is not None else int(time.time())

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        now = int(time.time())
        if v > now + 300:  # max 5 min in future (clock drift)
            raise ValueError("timestamp is too far in the future")
        if v < now - 86_400:  # max 24 hours in the past
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

    events: list[EventCreate] = Field(..., min_length=1)

    @field_validator("events")
    @classmethod
    def check_batch_size(cls, v):
        from app.core.config import settings

        if len(v) > settings.EVENT_BATCH_MAX:
            raise ValueError(f"Batch exceeds maximum of {settings.EVENT_BATCH_MAX} events.")
        return v


# ---------------------------------------------------------------------------
# Outbound: event response
# ---------------------------------------------------------------------------


class EventResponse(BaseModel):
    accepted: int = Field(..., description="Number of events accepted.")
    rejected: int = Field(..., description="Number of events rejected (see errors).")
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Track features (Phase 3 response)
# ---------------------------------------------------------------------------


class MoodTag(BaseModel):
    label: str
    confidence: float


class TrackFeaturesResponse(BaseModel):
    track_id: str
    duration: float | None
    bpm: float | None
    key: str | None
    mode: str | None
    energy: float | None
    danceability: float | None
    valence: float | None
    acousticness: float | None
    instrumentalness: float | None
    mood_tags: list[MoodTag] | None
    analyzed_at: int | None
    analysis_version: str | None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------


class ScanTriggerResponse(BaseModel):
    message: str
    scan_id: int
    status: str


class ScanStatusResponse(BaseModel):
    scan_id: int
    status: str
    files_found: int
    files_analyzed: int
    files_failed: int
    started_at: int
    ended_at: int | None
    last_error: str | None


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Outbound: full event read (GET /v1/events)
# ---------------------------------------------------------------------------


class ListenEventRead(BaseModel):
    """All fields stored for a single event, returned by the query endpoint."""

    id: int
    user_id: str
    track_id: str
    event_type: str
    value: float | None = None
    context: str | None = None
    client_id: str | None = None
    session_id: str | None = None
    timestamp: int

    # Rich signals
    surface: str | None = None
    position: int | None = None
    request_id: str | None = None
    model_version: str | None = None
    session_position: int | None = None
    dwell_ms: int | None = None
    pause_duration_ms: int | None = None
    num_seekfwd: int | None = None
    num_seekbk: int | None = None
    shuffle: bool | None = None
    context_type: str | None = None
    context_id: str | None = None
    context_switch: bool | None = None
    reason_start: str | None = None
    reason_end: str | None = None
    device_id: str | None = None
    device_type: str | None = None

    # Local time context
    hour_of_day: int | None = None
    day_of_week: int | None = None
    timezone: str | None = None

    # Audio output
    output_type: str | None = None
    output_device_name: str | None = None
    bluetooth_connected: bool | None = None

    # Location
    latitude: float | None = None
    longitude: float | None = None
    location_label: str | None = None

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
    seed_track_id: str | None = Field(None, max_length=128)
    params: dict[str, Any] | None = None
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
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    bpm: float | None = None
    key: str | None = None
    mode: str | None = None
    energy: float | None = None
    danceability: float | None = None
    valence: float | None = None
    mood_tags: list[MoodTag] | None = None
    duration: float | None = None


class PlaylistResponse(BaseModel):
    id: int
    name: str
    strategy: str
    seed_track_id: str | None = None
    params: dict[str, Any] | None = None
    track_count: int
    total_duration: float | None = None
    created_at: int

    model_config = {"from_attributes": True}


class PlaylistDetailResponse(PlaylistResponse):
    tracks: list[PlaylistTrackItem]


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Last.fm integration
# ---------------------------------------------------------------------------


class LastfmConnectRequest(BaseModel):
    """Connect a user's Last.fm account.  Sent by client apps only."""

    lastfm_username: str = Field(..., min_length=1, max_length=128)
    lastfm_password: str = Field(
        ...,
        min_length=1,
        description="Exchanged for a session key via Last.fm, then discarded. Never stored.",
    )


class LastfmConnectResponse(BaseModel):
    status: str
    username: str
    scrobbling_enabled: bool


class LastfmProfileResponse(BaseModel):
    username: str | None = None
    scrobbling_enabled: bool = False
    synced_at: int | None = None
    profile: dict[str, Any] | None = None


class RecommendationContext(BaseModel):
    """Real-time context sent by the client app with recommendation requests."""

    hour_of_day: int | None = Field(None, ge=0, le=23)
    day_of_week: int | None = Field(None, ge=1, le=7)
    device_type: str | None = Field(None, max_length=32)
    output_type: str | None = Field(None, max_length=32)
    context_type: str | None = Field(None, max_length=32)
    location_label: str | None = Field(None, max_length=32)


class OnboardingRequest(BaseModel):
    """User onboarding preferences for cold-start recommendation seeding."""

    favourite_artists: list[str] | None = Field(
        None,
        max_length=50,
        description="List of favourite artist names (matched against local library).",
    )
    favourite_genres: list[str] | None = Field(
        None,
        max_length=30,
        description="Preferred genres, e.g. ['rock', 'electronic', 'jazz'].",
    )
    favourite_tracks: list[str] | None = Field(
        None,
        max_length=50,
        description="List of track_ids from the local library.",
    )
    mood_preferences: list[str] | None = Field(
        None,
        max_length=10,
        description="Preferred moods, e.g. ['happy', 'relaxed', 'energetic'].",
    )
    listening_contexts: list[str] | None = Field(
        None,
        max_length=10,
        description="Typical listening contexts, e.g. ['home', 'gym', 'commute'].",
    )
    device_types: list[str] | None = Field(
        None,
        max_length=10,
        description="Typical devices, e.g. ['mobile', 'desktop', 'speaker'].",
    )
    energy_preference: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Preferred energy level (0=calm, 1=intense).",
    )
    danceability_preference: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Preferred danceability (0=not danceable, 1=very danceable).",
    )

    @model_validator(mode="after")
    def at_least_one_field(self):
        fields = [
            self.favourite_artists,
            self.favourite_genres,
            self.favourite_tracks,
            self.mood_preferences,
            self.listening_contexts,
            self.device_types,
            self.energy_preference,
            self.danceability_preference,
        ]
        if all(f is None for f in fields):
            raise ValueError("At least one onboarding preference must be provided.")
        return self


class OnboardingResponse(BaseModel):
    user_id: str
    preferences_saved: int = Field(..., description="Number of preference fields saved.")
    matched_tracks: int = Field(0, description="Favourite tracks matched to library.")
    matched_artists: int = Field(0, description="Favourite artists matched to library.")
    profile_seeded: bool = Field(False, description="Whether a taste profile was seeded from onboarding.")


class UserCreate(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    display_name: str | None = Field(None, max_length=255)


class UserUpdate(BaseModel):
    """Update a user's mutable fields. At least one field must be provided."""

    user_id: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="New username. Must be unique. Cascades to all event/session/interaction tables.",
    )
    display_name: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def at_least_one_field(self):
        if self.user_id is None and self.display_name is None:
            raise ValueError("At least one of user_id or display_name must be provided.")
        return self


class UserResponse(BaseModel):
    uid: int = Field(..., description="Stable numeric user identifier (never changes).")
    user_id: str = Field(..., description="Username / media server identifier (can be updated).")
    display_name: str | None = None
    created_at: int
    last_seen: int | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Chart download request (Spotizerr integration)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Radio
# ---------------------------------------------------------------------------


class RadioSeedType(str, Enum):
    TRACK = "track"
    ARTIST = "artist"
    PLAYLIST = "playlist"


class RadioStartRequest(BaseModel):
    """Start a radio session from a seed."""

    user_id: str = Field(..., min_length=1, max_length=128)
    seed_type: RadioSeedType
    seed_value: str = Field(..., min_length=1, max_length=512, description="track_id, artist name, or playlist_id")
    count: int = Field(10, ge=1, le=50, description="Number of tracks in the first batch")
    # Optional context (updatable on each /next call)
    device_type: str | None = Field(None, max_length=32)
    output_type: str | None = Field(None, max_length=32)
    location_label: str | None = Field(None, max_length=32)
    hour_of_day: int | None = Field(None, ge=0, le=23)
    day_of_week: int | None = Field(None, ge=1, le=7)

    model_config = {"use_enum_values": True}


class RadioFeedbackRequest(BaseModel):
    """In-session feedback for a radio track."""

    track_id: str = Field(..., min_length=1, max_length=128)
    action: str = Field(..., pattern="^(skip|like|dislike)$", description="Feedback action: skip, like, or dislike")


class RadioTrackItem(BaseModel):
    position: int
    track_id: str
    source: str
    score: float
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    bpm: float | None = None
    key: str | None = None
    mode: str | None = None
    energy: float | None = None
    danceability: float | None = None
    valence: float | None = None
    mood_tags: list[MoodTag] | None = None
    duration: float | None = None


class RadioSessionResponse(BaseModel):
    session_id: str
    user_id: str
    seed_type: str
    seed_value: str
    seed_display_name: str | None = None
    total_served: int
    tracks_played: int
    tracks_skipped: int
    tracks_liked: int
    created_at: int
    last_active: int


class RadioStartResponse(BaseModel):
    session_id: str
    seed_type: str
    seed_value: str
    seed_display_name: str | None = None
    tracks: list[RadioTrackItem]


class RadioNextResponse(BaseModel):
    session_id: str
    total_served: int
    tracks: list[RadioTrackItem]


# ---------------------------------------------------------------------------
# Chart download request (Spotizerr integration)
# ---------------------------------------------------------------------------


class ChartDownloadRequest(BaseModel):
    """Request body for POST /v1/charts/download.

    Provide either ``position`` (with chart_type/scope) to download a specific
    chart entry, or ``artist_name`` + ``track_title`` to search and download.
    """

    chart_type: str = Field("top_tracks", max_length=32, description="Chart type: top_tracks")
    scope: str = Field("global", max_length=128, description="Chart scope: global, tag:<name>, geo:<country>")
    position: int | None = Field(None, ge=0, description="Chart position (0-based)")
    artist_name: str | None = Field(None, max_length=512, description="Artist name (alternative to position)")
    track_title: str | None = Field(None, max_length=512, description="Track title (required with artist_name)")

    @model_validator(mode="after")
    def require_position_or_track(self) -> ChartDownloadRequest:
        if self.position is None and not (self.artist_name and self.track_title):
            raise ValueError("Provide either 'position' or both 'artist_name' and 'track_title'.")
        return self


# ---------------------------------------------------------------------------
# Downloads (Spotizerr proxy)
# ---------------------------------------------------------------------------


class DownloadCreateRequest(BaseModel):
    """Request body for POST /v1/downloads — download a specific track."""

    spotify_id: str = Field(..., min_length=1, max_length=64, description="Spotify track ID to download.")
    track_title: str | None = Field(None, max_length=512)
    artist_name: str | None = Field(None, max_length=512)
    album_name: str | None = Field(None, max_length=512)
    cover_url: str | None = Field(None, max_length=1024)


class DownloadResponse(BaseModel):
    """A persisted download request."""

    id: int
    spotify_id: str
    task_id: str | None = None
    status: str
    track_title: str | None = None
    artist_name: str | None = None
    album_name: str | None = None
    cover_url: str | None = None
    error_message: str | None = None
    created_at: int
    updated_at: int | None = None

    model_config = {"from_attributes": True}


class DownloadStatusResponse(BaseModel):
    """Proxied Spotizerr task status."""

    task_id: str
    status: str
    progress: float | None = None
    details: dict[str, Any] | None = None
