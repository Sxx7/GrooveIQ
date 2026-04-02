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
    PLAYLIST_ADD = "playlist_add"  # user added track to any playlist
    QUEUE_ADD    = "queue_add"     # user manually added to queue

    # Playback adjustments (implicit quality signals)
    SEEK_BACK    = "seek_back"     # user scrubbed backward (value = seconds jumped back)
    SEEK_FORWARD = "seek_forward"  # user scrubbed forward (value = seconds skipped)
    REPEAT       = "repeat"        # user hit repeat on a single track
    VOLUME_UP    = "volume_up"     # significant volume increase during track
    VOLUME_DOWN  = "volume_down"   # significant volume decrease


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

class UserCreate(BaseModel):
    user_id:      str = Field(..., min_length=1, max_length=128)
    display_name: Optional[str] = Field(None, max_length=255)

class UserResponse(BaseModel):
    user_id:      str
    display_name: Optional[str]
    created_at:   int
    last_seen:    Optional[int]

    model_config = {"from_attributes": True}
