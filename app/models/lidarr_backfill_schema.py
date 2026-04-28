"""
GrooveIQ – Lidarr backfill configuration schema.

Drains Lidarr's ``/wanted/missing`` (and optionally ``/wanted/cutoff``) queue by
matching each missing album against the streaming services exposed by
streamrip-api and downloading the first acceptable hit. Throughput is
rate-limited per hour so multi-thousand-album backfills spread cleanly over
days without tripping streaming-service rate limits.

Mirrors the ``download_routing`` / ``algorithm_config`` patterns: this is the
*policy* layer (versioned, GUI-driven). Infrastructure config (Lidarr URL,
streamrip credentials) still lives in env vars on the respective services.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.download_routing_schema import QualityTier

# ---------------------------------------------------------------------------
# Per-group sub-schemas
# ---------------------------------------------------------------------------


class SourcesConfig(BaseModel):
    """Which Lidarr queues to drain."""

    missing: bool = Field(True, description="Drain /api/v1/wanted/missing")
    cutoff_unmet: bool = Field(False, description="Drain /api/v1/wanted/cutoff (quality upgrades)")
    monitored_only: bool = Field(True, description="Skip albums that are unmonitored in Lidarr")


class MatchConfig(BaseModel):
    """Fuzzy-match thresholds and structural checks for accepting a streamrip hit."""

    min_artist_similarity: float = Field(
        0.85, ge=0, le=1, description="Reject if artist fuzzy ratio is below this (0–1)"
    )
    min_album_similarity: float = Field(
        0.80, ge=0, le=1, description="Reject if album-title fuzzy ratio is below this (0–1)"
    )
    require_year_match: bool = Field(False, description="Reject if release year differs by more than 1")
    require_track_count_match: bool = Field(False, description="Reject if track count differs from Lidarr's expectation")
    prefer_album_over_tracks: bool = Field(
        True,
        description="Album-first: only fall back to per-track downloads when no album hit exists",
    )


class RetryConfig(BaseModel):
    """How failures are retried before being permanently skipped."""

    cooldown_hours: float = Field(24.0, ge=0, le=720, description="Wait this many hours before retrying a failed album")
    max_attempts: int = Field(3, ge=1, le=20, description="Permanently skip after this many failed attempts")
    backoff_multiplier: float = Field(
        2.0,
        ge=1.0,
        le=10.0,
        description="Cooldown grows on each retry (cooldown × multiplier^attempts)",
    )


class ImportOptionsConfig(BaseModel):
    """Post-download Lidarr import behaviour.

    ``import`` is a Python reserved keyword, so this group is exposed as
    ``import_options`` in the JSON payload. The GUI label remains "Import".
    """

    trigger_lidarr_scan: bool = Field(
        True,
        description="POST /api/v1/command DownloadedAlbumsScan after a successful download",
    )
    scan_path: str = Field(
        "/downloads/streamrip",
        description="Path streamrip writes into; must match the Lidarr container's view of that mount",
    )


class FiltersConfig(BaseModel):
    """Allowlist / denylist filters applied before queueing."""

    artist_allowlist: list[str] = Field(
        default_factory=list,
        description="If non-empty, only artists on this list are processed (one per line)",
    )
    artist_denylist: list[str] = Field(
        default_factory=list,
        description="Artists on this list are skipped",
    )


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


_DEFAULT_SERVICE_PRIORITY: list[str] = ["qobuz", "tidal", "deezer", "soundcloud"]


class LidarrBackfillConfigData(BaseModel):
    """
    Complete Lidarr backfill policy.

    Defaults are conservative: disabled, 10 downloads/hour ceiling, lossless
    quality floor, strict match thresholds. Operators tune via the dashboard
    and can dry-run before flipping ``enabled``.
    """

    enabled: bool = Field(False, description="Master switch — when off, the scheduler tick is a no-op")
    dry_run: bool = Field(
        False,
        description="Match and persist with status='skipped', but never actually download",
    )

    sources: SourcesConfig = Field(default_factory=SourcesConfig)

    max_downloads_per_hour: int = Field(
        10,
        ge=1,
        le=100,
        description="Sliding-window cap; counts rows in the last 60 minutes",
    )
    max_batch_size: int = Field(5, ge=1, le=25, description="Hard cap on albums processed per scheduler tick")
    poll_interval_minutes: int = Field(
        5,
        ge=1,
        le=60,
        description="How often the scheduler wakes to attempt the next batch",
    )

    service_priority: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_SERVICE_PRIORITY),
        description="Streaming services tried in this order (qobuz/tidal/deezer/soundcloud)",
    )
    min_quality_floor: QualityTier = Field(
        QualityTier.LOSSLESS,
        description="Skip the cascade if streamrip's declared quality is below this tier",
    )

    match: MatchConfig = Field(default_factory=MatchConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    import_options: ImportOptionsConfig = Field(default_factory=ImportOptionsConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------


class LidarrBackfillConfigResponse(BaseModel):
    id: int
    version: int
    name: str | None = None
    config: LidarrBackfillConfigData
    is_active: bool
    created_at: int
    created_by: str | None = None


class LidarrBackfillConfigUpdate(BaseModel):
    name: str | None = None
    config: LidarrBackfillConfigData


class LidarrBackfillConfigImport(BaseModel):
    name: str | None = None
    config: dict[str, Any]


# ---------------------------------------------------------------------------
# Group metadata for the dashboard accordion
# ---------------------------------------------------------------------------

CONFIG_GROUPS: list[dict[str, Any]] = [
    {
        "key": "sources_filters",
        "label": "Sources & Filters",
        "description": "Which Lidarr queues to drain and which artists to include or exclude.",
        "fields": ["enabled", "sources", "filters"],
    },
    {
        "key": "rate_schedule",
        "label": "Rate & Schedule",
        "description": (
            "Sliding-window throttle plus the scheduler's poll cadence. "
            "ETA = (missing + cutoff) / max_downloads_per_hour."
        ),
        "fields": ["max_downloads_per_hour", "max_batch_size", "poll_interval_minutes"],
    },
    {
        "key": "match_quality",
        "label": "Match Quality",
        "description": (
            "Fuzzy-match thresholds, structural checks, streaming-service priority, "
            "and the streamrip quality floor."
        ),
        "fields": ["service_priority", "min_quality_floor", "match"],
    },
    {
        "key": "retry_import",
        "label": "Retry & Import",
        "description": "Failure cooldown / max attempts and the post-download Lidarr scan trigger.",
        "fields": ["retry", "import_options", "dry_run"],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_defaults() -> LidarrBackfillConfigData:
    """Return the default configuration."""
    return LidarrBackfillConfigData()


def get_defaults_dict() -> dict[str, Any]:
    """Return the default configuration as a JSON-safe dict."""
    return get_defaults().model_dump(mode="json")
