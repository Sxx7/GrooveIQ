"""
GrooveIQ – Algorithm configuration service.

Manages the active algorithm config: load from DB on startup, cache in
memory, save new versions, export/import.

Config changes take effect on the next pipeline run — no hot-reload.
The in-memory cache is updated on save so that subsequent pipeline runs
pick up the new values immediately.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.algorithm_config_schema import (
    AlgorithmConfigData,
    get_defaults,
    get_defaults_dict,
)
from app.models.db import AlgorithmConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache (singleton)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_active_config: AlgorithmConfigData = get_defaults()
_active_version: int = 0
_active_id: int | None = None


def get_config() -> AlgorithmConfigData:
    """Return the current active config. Thread-safe, never blocks on DB."""
    with _lock:
        return _active_config


def get_config_version() -> int:
    """Return the current active config version number."""
    with _lock:
        return _active_version


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

async def load_active_config() -> None:
    """
    Load the active config from DB into memory.

    Called on startup.  If no config exists, inserts the defaults as v1.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AlgorithmConfig)
            .where(AlgorithmConfig.is_active == True)  # noqa: E712
            .order_by(AlgorithmConfig.version.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()

        if row is None:
            # First run: seed with defaults.
            row = AlgorithmConfig(
                version=1,
                name="Default",
                config=get_defaults_dict(),
                is_active=True,
                created_at=int(time.time()),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            logger.info("Algorithm config: seeded defaults as v1.")

        _apply_to_cache(row)
        logger.info(f"Algorithm config loaded: v{row.version} (id={row.id})")


def _apply_to_cache(row: AlgorithmConfig) -> None:
    """Update the in-memory cache from a DB row."""
    with _lock:
        global _active_config, _active_version, _active_id
        _active_config = AlgorithmConfigData.model_validate(row.config)
        _active_version = row.version
        _active_id = row.id


async def get_active(session: AsyncSession) -> AlgorithmConfig | None:
    """Fetch the active config row."""
    result = await session.execute(
        select(AlgorithmConfig)
        .where(AlgorithmConfig.is_active == True)  # noqa: E712
        .order_by(AlgorithmConfig.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def save_config(
    session: AsyncSession,
    config_data: AlgorithmConfigData,
    name: str | None = None,
    created_by: str | None = None,
) -> AlgorithmConfig:
    """
    Save a new config version and mark it active.

    Deactivates the previous active config.  Returns the new row.
    """
    # Get next version number.
    result = await session.execute(
        select(AlgorithmConfig.version)
        .order_by(AlgorithmConfig.version.desc())
        .limit(1)
    )
    last_version = result.scalar_one_or_none() or 0
    new_version = last_version + 1

    # Deactivate all.
    await session.execute(
        update(AlgorithmConfig)
        .where(AlgorithmConfig.is_active == True)  # noqa: E712
        .values(is_active=False)
    )

    row = AlgorithmConfig(
        version=new_version,
        name=name,
        config=config_data.model_dump(),
        is_active=True,
        created_at=int(time.time()),
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)

    _apply_to_cache(row)
    logger.info(f"Algorithm config saved: v{new_version} (id={row.id})")
    return row


async def activate_version(session: AsyncSession, version: int) -> AlgorithmConfig | None:
    """Activate a specific historical version (rollback)."""
    result = await session.execute(
        select(AlgorithmConfig).where(AlgorithmConfig.version == version)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None

    await session.execute(
        update(AlgorithmConfig)
        .where(AlgorithmConfig.is_active == True)  # noqa: E712
        .values(is_active=False)
    )
    row.is_active = True
    await session.flush()

    _apply_to_cache(row)
    logger.info(f"Algorithm config activated: v{version}")
    return row


async def get_history(
    session: AsyncSession,
    limit: int = 20,
    offset: int = 0,
) -> list[AlgorithmConfig]:
    """Fetch config version history, newest first."""
    result = await session.execute(
        select(AlgorithmConfig)
        .order_by(AlgorithmConfig.version.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def reset_to_defaults(
    session: AsyncSession,
    created_by: str | None = None,
) -> AlgorithmConfig:
    """Create a new version with default values and mark it active."""
    return await save_config(
        session,
        get_defaults(),
        name="Reset to defaults",
        created_by=created_by,
    )
