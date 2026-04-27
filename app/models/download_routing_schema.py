"""
GrooveIQ – Download routing configuration schema.

Defines the priority chains, quality fallback thresholds, and parallel-search
policy for the download orchestration layer. This is the *policy* layer —
infrastructure config (URLs, API keys) still lives in env vars.

Design:
  - Global config (not per-user).
  - Three independent priority chains for three purposes:
      individual       — on-demand single-track downloads (POST /v1/downloads)
      bulk_per_track   — per-track bulk flows (chart fill, top-tracks bulk)
      bulk_album       — album/artist-level bulk flows (Lidarr discovery, fill_library)
  - Quality fallback: each chain entry can declare a min_quality. If a backend's
    expected/declared quality is below the threshold, the cascade skips it and
    moves to the next entry.
  - Changes take effect on next request — the in-memory cache is updated on save.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BackendName(str, Enum):
    SPOTDL = "spotdl"
    STREAMRIP = "streamrip"
    SPOTIZERR = "spotizerr"
    SLSKD = "slskd"
    LIDARR = "lidarr"


class QualityTier(str, Enum):
    """Ordinal quality tiers for fallback comparisons.

    Order matters: LOSSY_LOW < LOSSY_HIGH < LOSSLESS < HIRES.
    """

    LOSSY_LOW = "lossy_low"  # mp3 <= 192 kbps, aac <= 192 kbps
    LOSSY_HIGH = "lossy_high"  # mp3 256/320, aac 256+
    LOSSLESS = "lossless"  # FLAC/ALAC 16-bit/44.1 kHz (CD)
    HIRES = "hires"  # FLAC 24-bit/96 kHz+


_QUALITY_ORDER = {
    QualityTier.LOSSY_LOW: 0,
    QualityTier.LOSSY_HIGH: 1,
    QualityTier.LOSSLESS: 2,
    QualityTier.HIRES: 3,
}


def quality_meets(actual: QualityTier | str | None, minimum: QualityTier | str | None) -> bool:
    """Return True if `actual` quality meets or exceeds `minimum`.

    Unknown actual quality (`None`) is treated as `LOSSY_LOW` to be conservative —
    if a min threshold is set, an unknown-quality backend will fail it.
    A `None` minimum means "no threshold", always passes.
    """
    if minimum is None:
        return True
    min_tier = QualityTier(minimum) if isinstance(minimum, str) else minimum
    if actual is None:
        actual_tier = QualityTier.LOSSY_LOW
    else:
        actual_tier = QualityTier(actual) if isinstance(actual, str) else actual
    return _QUALITY_ORDER[actual_tier] >= _QUALITY_ORDER[min_tier]


# Default expected quality per backend, used when picking the chain at start
# (before we've actually searched). Variable-quality backends like slskd are
# evaluated post-search instead.
DEFAULT_BACKEND_QUALITY: dict[BackendName, QualityTier] = {
    BackendName.SPOTDL: QualityTier.LOSSY_HIGH,  # YT Music re-encoded ~256-320kbps
    BackendName.STREAMRIP: QualityTier.HIRES,  # default config quality=3 → 24/96
    BackendName.SPOTIZERR: QualityTier.LOSSY_HIGH,  # Deezer/YT, mostly 320kbps
    BackendName.SLSKD: QualityTier.LOSSY_HIGH,  # variable; assume floor
    BackendName.LIDARR: QualityTier.LOSSLESS,  # depends on quality profile
}


# ---------------------------------------------------------------------------
# Per-purpose chain entries
# ---------------------------------------------------------------------------


class BackendChainEntry(BaseModel):
    """One entry in a download priority chain."""

    backend: BackendName
    enabled: bool = Field(True, description="Whether to try this backend at all")
    min_quality: QualityTier | None = Field(
        None,
        description=(
            "Skip this backend if its expected (or actual, for variable-quality "
            "backends like slskd) result quality is below this tier. "
            "Null means no threshold."
        ),
    )
    timeout_s: int = Field(60, ge=5, le=600, description="Per-backend timeout in seconds for the cascade attempt")


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class DownloadRoutingConfigData(BaseModel):
    """
    Complete download routing policy.

    Defaults match the prior env-var-driven behaviour: spotdl preferred for
    individual, streamrip preferred for bulk_per_track, lidarr only for
    bulk_album. slskd is opt-in (disabled by default in chains).
    """

    individual: list[BackendChainEntry] = Field(
        default_factory=lambda: [
            BackendChainEntry(backend=BackendName.SPOTDL, enabled=True),
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
            BackendChainEntry(backend=BackendName.SPOTIZERR, enabled=True),
            BackendChainEntry(backend=BackendName.SLSKD, enabled=False),
        ],
        description="Cascade for on-demand single-track downloads (POST /v1/downloads)",
    )

    bulk_per_track: list[BackendChainEntry] = Field(
        default_factory=lambda: [
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
            BackendChainEntry(backend=BackendName.SPOTDL, enabled=True),
            BackendChainEntry(backend=BackendName.SPOTIZERR, enabled=True),
            BackendChainEntry(backend=BackendName.SLSKD, enabled=False),
        ],
        description="Cascade for per-track bulk flows (charts auto-add, bulk_download)",
    )

    bulk_album: list[BackendChainEntry] = Field(
        default_factory=lambda: [
            BackendChainEntry(backend=BackendName.LIDARR, enabled=True),
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=False),
        ],
        description=(
            "Cascade for album/artist-level bulk (discovery, fill_library). "
            "Lidarr is the canonical option; streamrip can grab whole albums "
            "via album URLs when enabled."
        ),
    )

    parallel_search_backends: list[BackendName] = Field(
        default_factory=lambda: [
            BackendName.SPOTDL,
            BackendName.STREAMRIP,
            BackendName.SPOTIZERR,
        ],
        description=(
            "Backends queried in parallel by GET /v1/downloads/search/multi. "
            "slskd is opt-in here too — its text-search semantics differ."
        ),
    )

    parallel_search_timeout_ms: int = Field(
        5000,
        ge=500,
        le=30000,
        description="Per-backend timeout for parallel multi-search (milliseconds)",
    )


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------


class DownloadRoutingConfigResponse(BaseModel):
    id: int
    version: int
    name: str | None = None
    config: DownloadRoutingConfigData
    is_active: bool
    created_at: int
    created_by: str | None = None


class DownloadRoutingConfigUpdate(BaseModel):
    name: str | None = None
    config: DownloadRoutingConfigData


class DownloadRoutingConfigImport(BaseModel):
    name: str | None = None
    config: dict[str, Any]


# ---------------------------------------------------------------------------
# Group metadata for the future GUI
# ---------------------------------------------------------------------------


ROUTING_GROUPS: list[dict[str, Any]] = [
    {
        "key": "individual",
        "label": "Individual Downloads",
        "description": (
            "Cascade tried for single-track downloads via POST /v1/downloads. "
            "First backend to succeed wins. Use min_quality to require a tier."
        ),
        "backends_eligible": [
            BackendName.SPOTDL.value,
            BackendName.STREAMRIP.value,
            BackendName.SPOTIZERR.value,
            BackendName.SLSKD.value,
        ],
    },
    {
        "key": "bulk_per_track",
        "label": "Bulk (Per-Track)",
        "description": (
            "Cascade for per-track bulk flows: chart fill, top-tracks bulk download. "
            "Lidarr is excluded — it operates at album granularity."
        ),
        "backends_eligible": [
            BackendName.SPOTDL.value,
            BackendName.STREAMRIP.value,
            BackendName.SPOTIZERR.value,
            BackendName.SLSKD.value,
        ],
    },
    {
        "key": "bulk_album",
        "label": "Bulk (Album/Artist)",
        "description": (
            "Cascade for album- and artist-level bulk: discovery, fill_library. "
            "Per-track backends (spotdl/spotizerr/slskd) don't fit here."
        ),
        "backends_eligible": [
            BackendName.LIDARR.value,
            BackendName.STREAMRIP.value,
        ],
    },
    {
        "key": "parallel_search",
        "label": "Parallel Search",
        "description": (
            "Backends queried concurrently for the multi-agent search endpoint. "
            "Results come back grouped by backend so users can pick a specific result."
        ),
        "backends_eligible": [
            BackendName.SPOTDL.value,
            BackendName.STREAMRIP.value,
            BackendName.SPOTIZERR.value,
            BackendName.SLSKD.value,
        ],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_defaults() -> DownloadRoutingConfigData:
    """Return the default routing configuration."""
    return DownloadRoutingConfigData()


def get_defaults_dict() -> dict[str, Any]:
    """Return the default routing configuration as a plain dict."""
    return get_defaults().model_dump(mode="json")
