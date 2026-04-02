"""
GrooveIQ – Application configuration.

All values can be overridden via environment variables (or a .env file).
See docs/configuration.md for full reference.
"""

from __future__ import annotations

import secrets
from typing import List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
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
    API_KEYS: List[str] = []

    # Rate limiting (requests per minute per API key)
    RATE_LIMIT_EVENTS: int = 120   # event ingestion endpoint
    RATE_LIMIT_DEFAULT: int = 60   # all other endpoints

    # Hosts allowed to reach this service (guards against Host header attacks)
    # Example: ["grooveiq.yourdomain.com", "localhost"]
    ALLOWED_HOSTS: List[str] = ["*"]

    # CORS – restrict to your app origins in production
    CORS_ORIGINS: List[str] = ["*"]

    # ------------------------------------------------------------------
    # Audio analysis (Phase 3)
    # ------------------------------------------------------------------
    MUSIC_LIBRARY_PATH: str = "/music"
    ANALYSIS_WORKERS: int = 2          # parallel Essentia workers
    ANALYSIS_BATCH_SIZE: int = 50      # tracks per job batch
    RESCAN_INTERVAL_HOURS: int = 6     # how often to check for new files

    # Supported audio extensions
    AUDIO_EXTENSIONS: List[str] = [
        ".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac", ".opus", ".wv"
    ]

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
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True   # structured JSON logs for prod; False for dev

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("API_KEYS", mode="before")
    @classmethod
    def split_api_keys(cls, v):
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        return v

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", mode="before")
    @classmethod
    def split_list(cls, v):
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    @model_validator(mode="after")
    def warn_insecure_defaults(self) -> "Settings":
        import warnings
        if self.APP_ENV == "production":
            if not self.API_KEYS:
                warnings.warn(
                    "⚠️  No API_KEYS configured. All endpoints are unprotected! "
                    "Set API_KEYS in your .env file.",
                    stacklevel=2,
                )
            if self.ALLOWED_HOSTS == ["*"]:
                warnings.warn(
                    "⚠️  ALLOWED_HOSTS is set to '*'. "
                    "Set ALLOWED_HOSTS to your actual domain for security.",
                    stacklevel=2,
                )
        return self


settings = Settings()
