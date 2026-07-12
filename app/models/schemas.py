"""
GrooveIQ – Pydantic request/response schemas.

Separate from ORM models to keep API contracts stable independent of
database layout changes.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Literal

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
    REPLAY = "replay"  # user played the same track again back-to-back (a strong re-listen signal)
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
    deferred: int = Field(
        0,
        description="Number of events parked for later re-resolution (track not yet linked to a "
        "TrackFeatures row). These are NOT lost — they replay once the track is linked.",
    )
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Track features (Phase 3 response)
# ---------------------------------------------------------------------------


class MoodTag(BaseModel):
    label: str
    confidence: float


class TrackFeaturesResponse(BaseModel):
    track_id: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
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

    # Per-backend external identifiers (issue #37). Any of them may be NULL.
    media_server_id: str | None = None
    spotify_id: str | None = None
    qobuz_id: str | None = None
    tidal_id: str | None = None
    deezer_id: str | None = None
    soundcloud_id: str | None = None
    # NOTE: serialised under the API name `mb_track_id` (column is
    # `musicbrainz_track_id`). Route handlers build this dict explicitly
    # so we don't rely on alias quirks during from_attributes.
    mb_track_id: str | None = None

    model_config = {"from_attributes": True}


class TrackLyricsResponse(BaseModel):
    """Lyrics for one track (GET /v1/tracks/{id}/lyrics).

    ``source`` is one of embedded|lrclib|asr|instrumental. ``quality`` is the
    cascade's display-quality rank (higher = better; null for instrumental).
    ``is_synced`` is true when time-synced LRC is available (karaoke-ready).
    For instrumentals the endpoint returns 200 with ``source="instrumental"``
    and no text, so clients show "instrumental" rather than "no lyrics".
    """

    track_id: str
    source: str  # embedded | lrclib | asr | instrumental
    quality: int | None = None
    plain: str | None = None
    synced: str | None = None
    language: str | None = None
    is_synced: bool = False
    is_explicit: bool | None = None
    fetched_at: int | None = None


class TrackLookupBatchRequest(BaseModel):
    """POST /v1/tracks/lookup — batch resolution of one external-id type to
    internal track_id. Pass exactly one of the *_ids fields."""

    media_server_ids: list[str] | None = None
    spotify_ids: list[str] | None = None
    qobuz_ids: list[str] | None = None
    tidal_ids: list[str] | None = None
    deezer_ids: list[str] | None = None
    soundcloud_ids: list[str] | None = None
    mb_track_ids: list[str] | None = None


class TrackLookupBatchResponse(BaseModel):
    """{external_id: internal_track_id_or_None} for every input id."""

    resolved: dict[str, str | None]


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
    PATH = "path"  # sonic bridge between two tracks (embedding slerp)
    TEXT = "text"  # natural-language prompt → CLAP similarity


class PlaylistCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    strategy: PlaylistStrategy
    seed_track_id: str | None = Field(None, max_length=128)
    params: dict[str, Any] | None = None
    max_tracks: int = Field(25, ge=5, le=100)
    user_id: str | None = Field(None, max_length=128)
    # When present: personalize track selection to this user AND scope the daily
    # idempotency cache per-user. Same Navidrome id the client sends to /recommend.

    @model_validator(mode="after")
    def validate_strategy_params(self):
        if self.strategy in (
            PlaylistStrategy.FLOW,
            PlaylistStrategy.KEY_COMPATIBLE,
            PlaylistStrategy.PATH,
        ):
            if not self.seed_track_id:
                raise ValueError(f"seed_track_id is required for '{self.strategy}' strategy")
        if self.strategy == PlaylistStrategy.MOOD:
            mood = (self.params or {}).get("mood")
            if not mood:
                raise ValueError("params.mood is required for 'mood' strategy")
            from app.services.audio_analysis import SUPPORTED_MOOD_LABELS

            if mood not in SUPPORTED_MOOD_LABELS:
                raise ValueError(
                    f"params.mood {mood!r} is not supported. Must be one of: {sorted(SUPPORTED_MOOD_LABELS)}."
                )
        if self.strategy == PlaylistStrategy.ENERGY_CURVE:
            curve = (self.params or {}).get("curve")
            valid = ("ramp_up", "cool_down", "ramp_up_cool_down", "steady_high", "steady_low")
            if curve not in valid:
                raise ValueError(f"params.curve must be one of {valid}")
        if self.strategy == PlaylistStrategy.PATH:
            target = (self.params or {}).get("target_track_id")
            if not target:
                raise ValueError("params.target_track_id is required for 'path' strategy")
            if target == self.seed_track_id:
                raise ValueError("params.target_track_id must differ from seed_track_id")
            if self.max_tracks < 3:
                raise ValueError("'path' strategy requires max_tracks >= 3")
        if self.strategy == PlaylistStrategy.TEXT:
            prompt = (self.params or {}).get("prompt")
            if not prompt or not str(prompt).strip():
                raise ValueError("params.prompt is required for 'text' strategy")
        return self

    model_config = {"use_enum_values": True}


class PlaylistTrackItem(BaseModel):
    position: int
    track_id: str
    media_server_id: str | None = None
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
        description=(
            "Preferred moods. Must be subset of: happy, sad, aggressive, relaxed, party. "
            "Unknown labels are silently dropped (clients may evolve faster than the backend)."
        ),
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

    @field_validator("mood_preferences", mode="after")
    @classmethod
    def filter_unknown_moods(cls, v: list[str] | None) -> list[str] | None:
        """Silently drop moods the EffNet pipeline doesn't emit. Onboarding is
        forgiving: clients (especially older app versions) may include extra
        labels — we keep what's usable rather than failing the whole call."""
        if not v:
            return v
        from app.services.audio_analysis import SUPPORTED_MOOD_LABELS

        return [m for m in v if m and m.lower() in SUPPORTED_MOOD_LABELS]

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
    discovery: float = Field(
        0.3,
        ge=0.0,
        le=1.0,
        description="Discovery-dial posture (0=familiar … 1=deep discovery). Default 0.3 = balanced.",
    )
    mode: Literal["familiar", "balanced", "discovery", "deep_discovery"] | None = Field(
        None,
        description=(
            "Discovery-dial preset by name. When set, takes precedence over `discovery` and pins the "
            "posture to that preset's anchor — so all four dial levers apply exactly (no interpolation). "
            "Omit to use the continuous `discovery` float."
        ),
    )
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
    media_server_id: str | None = None
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
    discovery: float = 0.3
    mode: str | None = None  # resolved discovery preset name, when the session was started by mode
    tracks: list[RadioTrackItem]


class RadioNextResponse(BaseModel):
    session_id: str
    total_served: int
    discovery: float = 0.3
    mode: str | None = None  # active discovery preset name, when set by mode
    tracks: list[RadioTrackItem]


# ---------------------------------------------------------------------------
# Affinity radio (pure-similarity "closest unheard track" sessions)
# ---------------------------------------------------------------------------


class AffinityStartRequest(BaseModel):
    """Start a pure-similarity ("affinity") radio session from a seed.

    Unlike ``/radio``, this streams the sonically nearest library tracks to the fixed
    seed with no drift, ranker, or diversity reranking — just cosine order.
    """

    user_id: str = Field(..., min_length=1, max_length=128)
    seed_type: RadioSeedType
    seed_value: str = Field(..., min_length=1, max_length=512, description="track_id, artist name, or playlist_id")
    count: int = Field(10, ge=1, le=50, description="Number of tracks in the first batch")
    unheard_only: bool = Field(
        True,
        description=(
            "When true (default) exclude tracks you've already played, so every result is "
            "both close to the seed and new to you. Set false for pure 'more like this', "
            "heard or not. Disliked tracks are always excluded."
        ),
    )
    gate: bool | None = Field(
        None,
        description=(
            "Override the genre/mood gate for this session. None (default) uses the algorithm-config "
            "setting; true forces the gate on (results share the seed's genre family + mood); false "
            "forces pure embedding-cosine order. Handy for A/B-ing the gate by ear."
        ),
    )

    model_config = {"use_enum_values": True}


class AffinityTrackItem(BaseModel):
    position: int
    track_id: str
    media_server_id: str | None = None
    similarity: float  # cosine similarity to the seed embedding (higher = closer)
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


class AffinityStartResponse(BaseModel):
    session_id: str
    seed_type: str
    seed_value: str
    seed_display_name: str | None = None
    unheard_only: bool = True
    # Whether the genre/mood gate is active for this session (echoes the effective
    # setting after the per-request override / config default / seed-anchor check).
    gate_active: bool = False
    # True when fewer than `count` tracks came back — the reachable neighbourhood
    # (within the library, minus heard/served/disliked) is used up.
    exhausted: bool = False
    tracks: list[AffinityTrackItem]


class AffinityNextResponse(BaseModel):
    session_id: str
    total_served: int
    exhausted: bool = False
    tracks: list[AffinityTrackItem]


class AffinitySessionResponse(BaseModel):
    session_id: str
    user_id: str
    seed_type: str
    seed_value: str
    seed_display_name: str | None = None
    unheard_only: bool = True
    total_served: int
    created_at: int
    last_active: int


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


class ChartFetchRequest(BaseModel):
    """Request body for POST /v1/charts/fetch — build one chart on demand.

    Powers the live country/genre picker: fetch a single chart (chart_type +
    scope) from Last.fm and persist today's snapshot so it reads back through
    the normal GET path. No-op if today's snapshot already exists (unless force).
    """

    chart_type: str = Field("top_tracks", max_length=32, description="top_tracks | top_artists | top_albums")
    scope: str = Field("global", max_length=128, description="global, tag:<genre>, geo:<country>")
    force: bool = Field(False, description="Rebuild even if today's snapshot already exists")


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
    user_id: str | None = Field(
        None, max_length=128, description="Requesting user, so a 'download finished' push can target them (goal B)."
    )


class DownloadResponse(BaseModel):
    """A persisted download request."""

    id: int
    spotify_id: str | None = None
    task_id: str | None = None
    status: str
    source: str = "spotdl"
    track_title: str | None = None
    artist_name: str | None = None
    album_name: str | None = None
    cover_url: str | None = None
    slskd_username: str | None = None
    slskd_filename: str | None = None
    slskd_transfer_id: str | None = None
    attempts: list[dict[str, Any]] | None = None
    error_message: str | None = None
    created_at: int
    updated_at: int | None = None

    model_config = {"from_attributes": True}


class DownloadFromHandleRequest(BaseModel):
    """POST /v1/downloads/from-handle — pick a specific multi-search result.

    The handle is the opaque dict returned in each search result. It carries
    the backend identifier plus whatever fields that backend needs to
    download (Spotify ID, slskd peer+filename, etc.).
    """

    handle: dict[str, Any] = Field(..., description="Opaque handle from /v1/downloads/search/multi")
    track_title: str | None = Field(None, max_length=512)
    artist_name: str | None = Field(None, max_length=512)
    album_name: str | None = Field(None, max_length=512)
    cover_url: str | None = Field(None, max_length=1024)
    user_id: str | None = Field(
        None, max_length=128, description="Requesting user, so a 'download finished' push can target them (goal B)."
    )


class DownloadStatusResponse(BaseModel):
    """Proxied Spotizerr task status."""

    task_id: str
    status: str
    progress: float | None = None
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Soulseek (slskd) downloads
# ---------------------------------------------------------------------------


class SoulseekSearchResult(BaseModel):
    """A single file result from a Soulseek search."""

    username: str
    filename: str
    size: int
    extension: str
    bit_rate: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    length: int | None = None
    has_free_slot: bool = False
    queue_length: int = 0
    score: float = 0.0


class SoulseekDownloadRequest(BaseModel):
    """Request body for POST /v1/soulseek/download."""

    username: str = Field(..., min_length=1, max_length=256)
    filename: str = Field(..., min_length=1, max_length=1024)
    size: int = Field(..., gt=0)
    track_title: str | None = Field(None, max_length=512)
    artist_name: str | None = Field(None, max_length=512)
    album_name: str | None = Field(None, max_length=512)


# ---------------------------------------------------------------------------
# Recommendation audit & replay
# ---------------------------------------------------------------------------


class CandidateAuditDetail(BaseModel):
    """One candidate from a persisted recommendation request."""

    track_id: str
    media_server_id: str | None = None
    sources: list[str] = []
    raw_score: float = 0.0
    pre_rerank_position: int = -1
    final_score: float | None = None
    final_position: int | None = None
    shown: bool = False
    reranker_actions: list[dict[str, Any]] = []
    feature_vector: dict[str, Any] = {}
    title: str | None = None
    artist: str | None = None


class RequestAuditSummary(BaseModel):
    """Summary row for the audit sessions list."""

    request_id: str
    user_id: str
    created_at: int
    surface: str
    seed_track_id: str | None = None
    context_id: str | None = None
    model_version: str
    config_version: int
    candidates_total: int
    candidates_by_source: dict[str, Any] = {}
    duration_ms: int
    limit_requested: int
    top_track: dict[str, Any] | None = None  # title/artist of position-0 candidate


class RequestAuditDetail(BaseModel):
    """Full audit detail: request + every persisted candidate."""

    request_id: str
    user_id: str
    created_at: int
    surface: str
    seed_track_id: str | None = None
    context_id: str | None = None
    model_version: str
    config_version: int
    request_context: dict[str, Any] = {}
    candidates_total: int
    candidates_by_source: dict[str, Any] = {}
    duration_ms: int
    limit_requested: int
    candidates: list[CandidateAuditDetail] = []


class RankDelta(BaseModel):
    track_id: str
    media_server_id: str | None = None
    title: str | None = None
    artist: str | None = None
    original_position: int | None = None
    new_position: int | None = None
    delta: int | None = None  # original - new (positive = moved up in new ranking)
    original_score: float | None = None
    new_score: float | None = None


class ReplaySummary(BaseModel):
    avg_abs_delta: float = 0.0
    top10_overlap: float = 0.0
    kendall_tau: float | None = None
    new_top10_tracks: list[str] = []
    dropped_top10_tracks: list[str] = []
    candidates_compared: int = 0


class ReplayResult(BaseModel):
    request_id: str
    mode: str  # "rerank_only" | "full"
    original_model_version: str
    original_config_version: int
    new_model_version: str
    new_config_version: int
    rank_deltas: list[RankDelta] = []
    summary: ReplaySummary


class ReplayRequest(BaseModel):
    mode: str = Field("rerank_only", description="rerank_only | full")

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in ("rerank_only", "full"):
            raise ValueError("mode must be 'rerank_only' or 'full'")
        return v


# ---------------------------------------------------------------------------
# Follows  (new-release notifications initiative, P0)
# ---------------------------------------------------------------------------


class FollowCreateRequest(BaseModel):
    artist_name: str = Field(..., min_length=1, max_length=512)
    artist_mbid: str | None = Field(None, max_length=36)
    source: str = Field("user", max_length=16)


class FollowArtistOut(BaseModel):
    artist_mbid: str | None = None
    artist_name: str
    resolved: bool


class FollowOut(BaseModel):
    id: int
    user_id: str
    artist_mbid: str | None = None
    artist_name: str
    image_url: str | None = None
    source: str
    followed_at: int


class FollowResponse(BaseModel):
    follow: FollowOut
    artist: FollowArtistOut


class FollowListItem(BaseModel):
    artist_mbid: str | None = None
    artist_name: str
    image_url: str | None = None
    followed_at: int


class FollowListResponse(BaseModel):
    follows: list[FollowListItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Notification devices (new-release push initiative, P2)
# ---------------------------------------------------------------------------


class DeviceRegister(BaseModel):
    """POST /v1/devices — register (upsert) a notification channel.

    A device supplies a list of Apprise URLs (the iOS device's relay capability
    URL, and/or user-supplied ntfy/telegram/... targets). The legacy ``apns_token``
    is still accepted but no longer used for delivery. At least one target is required.
    """

    user_id: str = Field(..., min_length=1, max_length=128, description="Your media server's user identifier.")
    platform: str = Field("ios", max_length=16)
    apns_token: str | None = Field(None, min_length=1, max_length=200, description="Hex APNs device token.")
    apns_environment: str = Field("production", pattern="^(sandbox|production)$")
    apprise_urls: list[str] | None = Field(None, max_length=32, description="Optional Apprise target URLs.")
    # Per-type notification toggles (default opt-in, matching the iOS UI). The
    # client already sends new_media + download_finished; the backend now honors
    # them. recommendations is future (goal F) but accepted now so no rebuild is
    # needed to enable it.
    notif_new_releases: bool = Field(True)
    notif_new_media: bool = Field(True)
    notif_download_finished: bool = Field(True)
    notif_recommendations: bool = Field(True)
    # Stable per-frontend identity (goal E): UIDevice.identifierForVendor + a
    # human label. Lets prefs survive a capability-URL rotation and the app list
    # its devices. Optional for backward compat with installs that don't send it.
    device_guid: str | None = Field(None, min_length=1, max_length=64, description="Stable per-install device id.")
    device_name: str | None = Field(None, max_length=128, description='Human label, e.g. "Simon\'s iPhone".')
    # IANA timezone for quiet-hours (notifications Phase 2). Optional; a client
    # that omits it leaves the device tz-less and the dispatcher uses a UTC window.
    tz: str | None = Field(None, max_length=64, description="IANA timezone, e.g. Europe/Zurich, for quiet-hours.")
    # Per-user quiet-hours override (notifications Phase 5). Sent only once the user
    # configures it in the app; omitted (null) → the server's global default applies.
    # A non-null `quiet_hours_enabled` replaces the global for this user.
    quiet_hours_enabled: bool | None = Field(None, description="Per-user quiet-hours on/off (null = use global default).")
    quiet_hours_start: int | None = Field(None, ge=0, le=23, description="Local hour the quiet window opens.")
    quiet_hours_end: int | None = Field(None, ge=0, le=23, description="Local hour the quiet window closes.")
    # Per-type cadence override (notifications Phase 6): {pref_field: "instant"|"daily"}.
    # A type set to "daily" is held until the daily digest hour, then coalesced.
    notif_cadence: dict[str, str] | None = Field(
        None, description='Per-type cadence, e.g. {"notif_new_media": "daily"}.'
    )

    @field_validator("notif_cadence")
    @classmethod
    def _valid_cadences(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is not None:
            bad = {val for val in v.values() if val not in ("instant", "daily")}
            if bad:
                raise ValueError(f"cadence must be 'instant' or 'daily', got {sorted(bad)}")
        return v

    @model_validator(mode="after")
    def _need_a_target(self) -> DeviceRegister:
        if not self.apns_token and not self.apprise_urls:
            raise ValueError("device must supply apns_token and/or apprise_urls")
        return self


class DeviceDelete(BaseModel):
    """DELETE /v1/devices — unregister a channel.

    Identify it by ``device_id`` (works for any channel, including Apprise-only
    rows that have no token — e.g. one added from the dashboard) or by the legacy
    ``apns_token``. At least one is required.
    """

    device_id: int | None = Field(None, ge=1, description="Device row id (from notification-settings).")
    apns_token: str | None = Field(None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _need_an_identifier(self) -> DeviceDelete:
        if self.device_id is None and not self.apns_token:
            raise ValueError("delete must supply device_id and/or apns_token")
        return self


class NotificationSettingsUpdate(BaseModel):
    """Partial per-type toggle update. Provide any subset of the pref flags; only
    the ones present are applied (backward compatible with a client that sends
    just ``notif_new_releases``)."""

    device_id: int | None = Field(
        None,
        ge=1,
        description="Scope the toggle to one device; omitted → all of the user's devices.",
    )
    notif_new_releases: bool | None = None
    notif_new_media: bool | None = None
    notif_download_finished: bool | None = None
    notif_recommendations: bool | None = None

    @model_validator(mode="after")
    def _need_a_pref(self) -> NotificationSettingsUpdate:
        if all(
            getattr(self, f) is None
            for f in ("notif_new_releases", "notif_new_media", "notif_download_finished", "notif_recommendations")
        ):
            raise ValueError("supply at least one notification preference to update")
        return self


class NotificationTest(BaseModel):
    device_id: int | None = Field(
        None,
        ge=1,
        description="Scope the test to one device; omitted → all of the user's active channels.",
    )


class NotificationRecommendationRequest(BaseModel):
    """POST /v1/admin/notify-recommendation — fire a recommendation push (goal F)."""

    user_id: str = Field(..., min_length=1, max_length=128)
    title: str | None = Field(None, max_length=255)
    body: str | None = Field(None, max_length=1024)
    playlist_id: str | None = Field(None, max_length=128, description="Dedups + deep-links to a specific mix.")
