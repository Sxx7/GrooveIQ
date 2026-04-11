"""
GrooveIQ – Async database session management.

Uses SQLAlchemy 2.x async engine. Supports both SQLite (default, zero-config)
and PostgreSQL (recommended for multi-user or high-volume deployments).
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.db import Base

logger = logging.getLogger(__name__)

# SQLite-specific pragma: WAL mode for concurrent reads during writes.
# timeout=30 sets the busy-wait when another connection holds the write
# lock — without it aiosqlite defaults to 5 s which is too short during
# heavy scan-completion operations (log prune, media-server sync, FAISS).
_SQLITE_CONNECT_ARGS = {"check_same_thread": False, "timeout": 30}

_connect_args = (
    _SQLITE_CONNECT_ARGS if "sqlite" in settings.DATABASE_URL else {}
)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE if "sqlite" not in settings.DATABASE_URL else 5,
    max_overflow=settings.DB_MAX_OVERFLOW if "sqlite" not in settings.DATABASE_URL else 10,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db() -> None:
    """Create all tables on startup (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Enable WAL mode for SQLite at runtime
        if "sqlite" in settings.DATABASE_URL:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON;")

        # Lightweight schema migrations for columns added after initial release.
        # SQLAlchemy create_all won't add columns to existing tables.
        await _apply_column_migrations(conn)

    logger.info("Database initialized.")


_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$", re.IGNORECASE)
_SAFE_COL_TYPE = re.compile(
    r"^(VARCHAR\(\d+\)|INTEGER( DEFAULT \d+)?|TEXT|REAL|BLOB)$", re.IGNORECASE
)


async def _apply_column_migrations(conn) -> None:
    """Add missing columns to existing tables. Safe to run repeatedly."""
    migrations = [
        ("track_features", "external_track_id", "VARCHAR(128)"),
        ("track_features", "title", "VARCHAR(512)"),
        ("track_features", "artist", "VARCHAR(512)"),
        ("track_features", "album", "VARCHAR(512)"),
        ("track_features", "genre", "VARCHAR(512)"),
        ("library_scan_state", "files_skipped", "INTEGER DEFAULT 0"),
        ("library_scan_state", "current_file", "TEXT"),
        # Track metadata from ID3 tags
        ("track_features", "album_artist", "VARCHAR(512)"),
        ("track_features", "track_number", "INTEGER"),
        ("track_features", "duration_ms", "INTEGER"),
        ("track_features", "musicbrainz_track_id", "VARCHAR(64)"),
        # Last.fm per-user integration
        ("users", "lastfm_username", "VARCHAR(128)"),
        ("users", "lastfm_session_key", "VARCHAR(512)"),
        ("users", "lastfm_cache", "TEXT"),
        ("users", "lastfm_synced_at", "INTEGER"),
        # Playlist ownership
        ("playlists", "created_by", "VARCHAR(128)"),
        # Chart entry images
        ("chart_entries", "image_url", "VARCHAR(1024)"),
        # User onboarding preferences
        ("users", "onboarding_preferences", "TEXT"),
    ]
    for table, column, col_type in migrations:
        # Validate identifiers to prevent SQL injection via migration list.
        if not _SAFE_IDENTIFIER.match(table):
            raise ValueError(f"Unsafe table name in migration: {table!r}")
        if not _SAFE_IDENTIFIER.match(column):
            raise ValueError(f"Unsafe column name in migration: {column!r}")
        if not _SAFE_COL_TYPE.match(col_type):
            raise ValueError(f"Unsafe column type in migration: {col_type!r}")
        try:
            await conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
            )
            logger.info(f"Migration: added {table}.{column}")
        except Exception:
            pass  # Column already exists


async def get_session() -> AsyncSession:
    """FastAPI dependency: yields a scoped async session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
