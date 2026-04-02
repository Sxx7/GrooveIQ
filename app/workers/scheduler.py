"""
GrooveIQ – Background scheduler.

Runs periodic tasks:
  - Library re-scan every RESCAN_INTERVAL_HOURS
  - Event retention cleanup (purge events older than EVENT_RETENTION_DAYS)
"""

from __future__ import annotations

import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent
from app.workers.library_scanner import trigger_scan

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(timezone="UTC")


async def start_scheduler() -> None:
    _scheduler.add_job(
        _periodic_library_scan,
        trigger=IntervalTrigger(hours=settings.RESCAN_INTERVAL_HOURS),
        id="library_scan",
        replace_existing=True,
    )
    _scheduler.add_job(
        _cleanup_old_events,
        trigger=IntervalTrigger(hours=24),
        id="event_cleanup",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        f"Scheduler started. Library scan every {settings.RESCAN_INTERVAL_HOURS}h."
    )

    # Trigger an initial scan on startup (non-blocking)
    import asyncio
    asyncio.create_task(_periodic_library_scan())


async def stop_scheduler() -> None:
    _scheduler.shutdown(wait=False)


async def _periodic_library_scan() -> None:
    logger.info("Scheduled library scan triggered.")
    scan_id = await trigger_scan()
    logger.info(f"Library scan running (id={scan_id}).")


async def _cleanup_old_events() -> None:
    cutoff = int(time.time()) - (settings.EVENT_RETENTION_DAYS * 86_400)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(ListenEvent).where(ListenEvent.timestamp < cutoff)
        )
        await session.commit()
        deleted = result.rowcount
    if deleted:
        logger.info(f"Cleaned up {deleted} events older than {settings.EVENT_RETENTION_DAYS} days.")
