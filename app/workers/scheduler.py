"""
GrooveIQ – Background scheduler.

Runs periodic tasks:
  - Library re-scan every RESCAN_INTERVAL_HOURS
  - Event retention cleanup (purge events older than EVENT_RETENTION_DAYS)
  - Sessionization + track scoring + taste profile rebuild (Phase 2)
"""

from __future__ import annotations

import logging
import time
import traceback

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent
from app.workers.library_scanner import trigger_scan, resume_interrupted_scans

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
    _scheduler.add_job(
        _periodic_recommendation_pipeline,
        trigger=IntervalTrigger(hours=settings.SCORING_INTERVAL_HOURS),
        id="recommendation_pipeline",
        replace_existing=True,
    )
    if settings.discovery_enabled:
        cron = settings.DISCOVERY_CRON.split()
        _scheduler.add_job(
            _periodic_discovery,
            trigger=CronTrigger(
                minute=cron[0], hour=cron[1], day=cron[2],
                month=cron[3], day_of_week=cron[4], timezone="UTC",
            ),
            id="music_discovery",
            replace_existing=True,
        )

    _scheduler.start()
    logger.info(
        f"Scheduler started. Library scan every {settings.RESCAN_INTERVAL_HOURS}h, "
        f"recommendation pipeline every {settings.SCORING_INTERVAL_HOURS}h."
        + (f", discovery cron '{settings.DISCOVERY_CRON}'" if settings.discovery_enabled else "")
    )

    # Resume any scan interrupted by a previous container restart,
    # or trigger a fresh initial scan if none are pending.
    import asyncio
    asyncio.create_task(_startup_scan())

    # Run recommendation pipeline once on startup so recommendations
    # are available immediately (don't wait for the first scheduled run).
    asyncio.create_task(_delayed_startup_pipeline())


async def stop_scheduler() -> None:
    _scheduler.shutdown(wait=False)


async def _delayed_startup_pipeline() -> None:
    """Run the recommendation pipeline shortly after startup."""
    import asyncio
    await asyncio.sleep(10)  # let DB and scan init settle
    logger.info("Running recommendation pipeline on startup.")
    await _periodic_recommendation_pipeline()


async def _startup_scan() -> None:
    """On startup, resume interrupted scans or trigger a fresh one."""
    resumed = await resume_interrupted_scans()
    if resumed is None:
        await _periodic_library_scan()


async def run_recommendation_pipeline_now() -> dict:
    """Run the recommendation pipeline on demand. Returns summary."""
    await _periodic_recommendation_pipeline()
    return {"status": "completed"}


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


async def _periodic_recommendation_pipeline() -> None:
    """
    Run the Phase 2 recommendation data pipeline:
      1. Sessionizer – materialise sessions from raw events
      2. Track scoring – compute per-(user, track) satisfaction scores
      3. Taste profiles – rebuild user preference profiles

    Each step is independent of failures in the others (best-effort).
    Order matters: sessions feed taste profiles, scoring feeds taste profiles.
    """
    logger.info("Recommendation pipeline started.")

    # Step 1: Sessionization
    try:
        from app.services.sessionizer import run_sessionizer
        sess_result = await run_sessionizer()
        logger.info("Sessionizer done", extra=sess_result)
    except Exception:
        logger.error(f"Sessionizer failed: {traceback.format_exc()}")

    # Step 2: Track scoring
    try:
        from app.services.track_scoring import run_track_scoring
        score_result = await run_track_scoring()
        logger.info("Track scoring done", extra=score_result)
    except Exception:
        logger.error(f"Track scoring failed: {traceback.format_exc()}")

    # Step 3: Taste profiles
    try:
        from app.services.taste_profile import run_taste_profile_builder
        taste_result = await run_taste_profile_builder()
        logger.info("Taste profile builder done", extra=taste_result)
    except Exception:
        logger.error(f"Taste profile builder failed: {traceback.format_exc()}")

    # Step 4: Collaborative filtering model rebuild
    try:
        from app.services.collab_filter import build_model
        cf_result = await build_model()
        logger.info("CF model rebuild done", extra=cf_result)
    except Exception:
        logger.error(f"CF model rebuild failed: {traceback.format_exc()}")

    # Step 5: LightGBM ranker training
    try:
        from app.services.ranker import train_model
        ranker_result = await train_model()
        logger.info("Ranker training done", extra=ranker_result)
    except Exception:
        logger.error(f"Ranker training failed: {traceback.format_exc()}")

    logger.info("Recommendation pipeline complete.")


async def _periodic_discovery() -> None:
    """Run the music discovery pipeline (Last.fm → Lidarr)."""
    try:
        from app.services.discovery import run_discovery_pipeline
        result = await run_discovery_pipeline()
        logger.info("Discovery pipeline done: %s", result)
    except Exception:
        logger.error(f"Discovery pipeline failed: {traceback.format_exc()}")


async def run_discovery_now() -> dict:
    """Run the discovery pipeline on demand. Returns summary."""
    from app.services.discovery import run_discovery_pipeline
    return await run_discovery_pipeline()
