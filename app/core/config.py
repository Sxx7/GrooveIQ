"""
GrooveIQ – Application configuration.

All values can be overridden via environment variables (or a .env file).
See docs/configuration.md for full reference.
"""

from __future__ import annotations

import os
import secrets
from typing import List, Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(v: str) -> List[str]:
    """Split a comma-separated string into a list, stripping whitespace."""
    return [item.strip() for item in v.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------
    APP_ENV: str = "production"  # development | production
    SECRET_KEY: str = secrets.token_urlsafe(32)  # OVERRIDE in production!
    ENABLE_DOCS: bool = False  # set True in development only

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    # SQLite (default, zero-config)
    DATABASE_URL: str = "sqlite+aiosqlite:///./grooveiq.db"
    # Postgres example: postgresql+asyncpg://user:pass@localhost/grooveiq

    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    # Comma-separated list of API keys (hash-compared, never stored plain)
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    # Stored as a raw string to avoid pydantic-settings JSON parsing;
    # split into a list in the model_validator below.
    API_KEYS: str = ""

    # Rate limiting (requests per minute per API key)
    RATE_LIMIT_EVENTS: int = 300   # event ingestion endpoint (high for batch clients)
    RATE_LIMIT_DEFAULT: int = 200  # all other endpoints (dashboard polls every 2s during scans)

    # Hosts allowed to reach this service (guards against Host header attacks)
    # Example: "grooveiq.yourdomain.com,localhost"
    ALLOWED_HOSTS: str = "*"

    # CORS – restrict to your app origins in production
    CORS_ORIGINS: str = "*"

    # ------------------------------------------------------------------
    # Audio analysis (Phase 3)
    # ------------------------------------------------------------------
    MUSIC_LIBRARY_PATH: str = "/music"
    ANALYSIS_WORKERS: int = max(1, (os.cpu_count() or 2) - 1)  # default: CPU cores - 1
    ANALYSIS_BATCH_SIZE: int = 50      # tracks per job batch
    ANALYSIS_TIMEOUT: int = 300        # seconds before a single file analysis is killed
    RESCAN_INTERVAL_HOURS: int = 6     # how often to check for new files
    ANALYSIS_TWO_PASS: bool = True     # fast DSP pass first, then TF enrichment pass
    ANALYSIS_GPU: bool = False         # use ONNX Runtime GPU for TF enrichment pass
    ANALYSIS_GPU_BACKEND: str = ""     # "cuda", "openvino", or "" (auto-detect)
    ANALYSIS_GPU_BATCH_SIZE: int = 64  # mel-spec patches per GPU forward pass
    ANALYSIS_GPU_WORKERS: int = 1      # workers for GPU inference (usually 1)
    ANALYSIS_ONNX_INTRA_THREADS: int = 1  # ONNX intra_op_num_threads per worker
    ANALYSIS_ONNX_INTER_THREADS: int = 1  # ONNX inter_op_num_threads per worker
    ANALYSIS_OMP_THREADS: int = 1         # OMP/BLAS thread count per worker subprocess

    # Supported audio extensions (comma-separated)
    AUDIO_EXTENSIONS: str = ".mp3,.flac,.ogg,.m4a,.wav,.aac,.opus,.wv"

    # ------------------------------------------------------------------
    # Recommendation engine (Phase 2)
    # ------------------------------------------------------------------
    SESSION_GAP_MINUTES: int = 30          # inactivity gap that splits sessions
    SESSION_MIN_EVENTS: int = 2            # ignore sessions with fewer events
    TASTE_PROFILE_DECAY_DAYS: float = 30.0 # half-life for exponential recency weighting
    SCORING_INTERVAL_HOURS: int = 1        # how often to run the scoring/sessionizer worker

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------
    # Maximum events per batch POST
    EVENT_BATCH_MAX: int = 50

    # How long (days) to retain raw events before aggregating
    EVENT_RETENTION_DAYS: int = 365

    # Minimum play percentage to count as a "real" listen (not accidental)
    MIN_PLAY_PERCENTAGE: float = 0.05

    # ------------------------------------------------------------------
    # Media server integration (Navidrome / Plex)
    # ------------------------------------------------------------------
    # Set MEDIA_SERVER_TYPE to "navidrome" or "plex" to enable.
    # When enabled, the library sync maps track_ids to the media server's
    # native IDs so events from clients use the same identifiers.
    MEDIA_SERVER_TYPE: str = ""            # "navidrome" or "plex" (empty = disabled)
    MEDIA_SERVER_URL: str = ""             # e.g. http://192.168.178.49:4533
    MEDIA_SERVER_USER: str = ""            # Navidrome username
    MEDIA_SERVER_PASSWORD: str = ""        # Navidrome password
    MEDIA_SERVER_TOKEN: str = ""           # Plex X-Plex-Token
    MEDIA_SERVER_LIBRARY_ID: str = "1"     # Plex library section ID
    MEDIA_SERVER_MUSIC_PATH: str = ""      # Music root as seen by the media server
                                           # (for path matching if it differs from MUSIC_LIBRARY_PATH)

    # ------------------------------------------------------------------
    # Music discovery (Last.fm + Lidarr)
    # ------------------------------------------------------------------
    LASTFM_API_KEY: str = ""
    LASTFM_API_SECRET: str = ""        # shared secret for authenticated calls (scrobbling)
    LIDARR_URL: str = ""               # e.g. http://lidarr:8686
    LIDARR_API_KEY: str = ""
    LIDARR_QUALITY_PROFILE_ID: int = 1
    LIDARR_METADATA_PROFILE_ID: int = 1
    LIDARR_ROOT_FOLDER: str = "/music"
    DISCOVERY_CRON: str = "0 3 * * *"  # cron schedule (default: 3 AM daily)
    DISCOVERY_MAX_REQUESTS_PER_DAY: int = 500
    DISCOVERY_SIMILAR_LIMIT: int = 20  # similar artists per seed from Last.fm

    # ------------------------------------------------------------------
    # Last.fm per-user integration (profile + scrobbling)
    # ------------------------------------------------------------------
    LASTFM_ENABLED: bool = False                # master toggle
    LASTFM_SCROBBLE_ENABLED: bool = False       # scrobbling (requires session key)
    LASTFM_SESSION_ENCRYPTION_KEY: str = ""     # Fernet key for encrypting session keys at rest
    LASTFM_REFRESH_HOURS: int = 6               # how often to pull Last.fm profiles

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True   # structured JSON logs for prod; False for dev

    # ------------------------------------------------------------------
    # Parsed list accessors (derived from the raw CSV strings above)
    # ------------------------------------------------------------------
    @property
    def api_keys_list(self) -> List[str]:
        return _split_csv(self.API_KEYS)

    @property
    def allowed_hosts_list(self) -> List[str]:
        return _split_csv(self.ALLOWED_HOSTS)

    @property
    def cors_origins_list(self) -> List[str]:
        return _split_csv(self.CORS_ORIGINS)

    @property
    def audio_extensions_list(self) -> List[str]:
        return _split_csv(self.AUDIO_EXTENSIONS)

    @property
    def discovery_enabled(self) -> bool:
        return bool(self.LASTFM_API_KEY and self.LIDARR_URL and self.LIDARR_API_KEY)

    @property
    def lastfm_user_enabled(self) -> bool:
        """True when per-user Last.fm features (profile pull, scrobbling) are configured."""
        return bool(self.LASTFM_ENABLED and self.LASTFM_API_KEY and self.LASTFM_API_SECRET)

    @model_validator(mode="after")
    def warn_insecure_defaults(self) -> "Settings":
        import warnings
        if self.APP_ENV == "production":
            if not self.api_keys_list:
                warnings.warn(
                    "⚠️  No API_KEYS configured. All endpoints are unprotected! "
                    "Set API_KEYS in your .env file.",
                    stacklevel=2,
                )
            if self.allowed_hosts_list == ["*"]:
                warnings.warn(
                    "⚠️  ALLOWED_HOSTS is set to '*'. "
                    "Set ALLOWED_HOSTS to your actual domain for security.",
                    stacklevel=2,
                )
        return self


settings = Settings()
