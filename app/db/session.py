"""
GrooveIQ – Async database session management.

Uses SQLAlchemy 2.x async engine. Supports both SQLite (default, zero-config)
and PostgreSQL (recommended for multi-user or high-volume deployments).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.db import Base

logger = logging.getLogger(__name__)

# SQLite-specific pragma: WAL mode for concurrent reads during writes
_SQLITE_CONNECT_ARGS = {"check_same_thread": False}

_connect_args = (
    _SQLITE_CONNECT_ARGS if "sqlite" in settings.DATABASE_URL else {}
)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.APP_ENV == "development",
    pool_size=settings.DB_POOL_SIZE if "sqlite" not in settings.DATABASE_URL else 1,
    max_overflow=settings.DB_MAX_OVERFLOW if "sqlite" not in settings.DATABASE_URL else 0,
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


async def _apply_column_migrations(conn) -> None:
    """Add missing columns to existing tables. Safe to run repeatedly."""
    migrations = [
        ("track_features", "external_track_id", "VARCHAR(128)"),
    ]
    for table, column, col_type in migrations:
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
