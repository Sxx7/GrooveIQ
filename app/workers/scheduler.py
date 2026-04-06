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

    # Last.fm scrobble queue processor (every 60s)
    if settings.lastfm_user_enabled and settings.LASTFM_SCROBBLE_ENABLED:
        _scheduler.add_job(
            _process_scrobble_queue,
            trigger=IntervalTrigger(seconds=60),
            id="lastfm_scrobble_queue",
            replace_existing=True,
        )
    # Last.fm profile refresh
    if settings.lastfm_user_enabled:
        _scheduler.add_job(
            _refresh_lastfm_profiles,
            trigger=IntervalTrigger(hours=settings.LASTFM_REFRESH_HOURS),
            id="lastfm_profile_refresh",
            replace_existing=True,
        )

    _scheduler.start()
    logger.info(
        f"Scheduler started. Library scan every {settings.RESCAN_INTERVAL_HOURS}h, "
        f"recommendation pipeline every {settings.SCORING_INTERVAL_HOURS}h."
        + (f", discovery cron '{settings.DISCOVERY_CRON}'" if settings.discovery_enabled else "")
        + (f", Last.fm profile refresh every {settings.LASTFM_REFRESH_HOURS}h" if settings.lastfm_user_enabled else "")
    )

    # Resume any scan interrupted by a previous container restart,
    # or trigger a fresh initial scan if none are pending.
    import asyncio
    asyncio.create_task(_startup_scan())

    # Run recommendation pipeline once on startup so recommendations
    # are available immediately (don't wait for the first scheduled run).
    asyncio.create_task(_delayed_startup_pipeline())

    # Start event loop health watchdog.
    asyncio.create_task(_event_loop_watchdog())


async def stop_scheduler() -> None:
    _scheduler.shutdown(wait=False)


async def _delayed_startup_pipeline() -> None:
    """Run the recommendation pipeline after the initial scan finishes.

    Previously this used a fixed 10-second delay, which meant the pipeline
    and the library scan competed for SQLite's single write lock — starving
    HTTP handlers and causing dashboard timeouts on first run.
    Now we poll until no scan is running before kicking off the pipeline.
    """
    import asyncio
    from app.workers.library_scanner import is_scan_running

    # Short grace period for the scan to start.
    await asyncio.sleep(5)

    # Wait until the initial scan finishes (check every 10s).
    while is_scan_running():
        logger.debug("Startup pipeline waiting for library scan to finish...")
        await asyncio.sleep(10)

    logger.info("Running recommendation pipeline on startup (scan complete).")
    await _periodic_recommendation_pipeline(trigger="startup")


async def _event_loop_watchdog() -> None:
    """
    Periodically measure event loop responsiveness.
    If sleep(0.1) takes >1s, the loop is severely congested.
    Runs every 5s as a background task.
    """
    import asyncio
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        latency_ms = (time.monotonic() - t0 - 0.1) * 1000
        if latency_ms > 1000:
            logger.error(f"Event loop BLOCKED: measured latency {latency_ms:.0f}ms (expected ~0ms)")
        elif latency_ms > 200:
            logger.warning(f"Event loop congested: latency {latency_ms:.0f}ms")
        await asyncio.sleep(5)


async def _startup_scan() -> None:
    """On startup, resume interrupted scans or trigger a fresh one."""
    resumed = await resume_interrupted_scans()
    if resumed is None:
        await _periodic_library_scan()


async def run_recommendation_pipeline_now(trigger: str = "manual") -> dict:
    """Run the recommendation pipeline on demand. Returns summary."""
    await _periodic_recommendation_pipeline(trigger=trigger)
    from app.services.pipeline_state import get_last_run
    return get_last_run() or {"status": "completed"}


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


async def _periodic_recommendation_pipeline(trigger: str = "scheduled") -> None:
    """
    Run the 9-step recommendation data pipeline with full instrumentation.

    Each step is error-isolated (best-effort).
    Order matters: sessions → scoring → taste profiles → downstream models.

    The *trigger* parameter tracks who initiated the run:
    ``"scheduled"`` (APScheduler), ``"manual"`` (API call), ``"startup"``.
    """
    import asyncio
    from app.services.pipeline_state import (
        start_run, finish_run, step_start, step_complete, step_failed,
    )

    run = start_run(trigger=trigger)
    logger.info(f"Recommendation pipeline started (run_id={run.run_id}, trigger={trigger}).")

    # ── Step definitions ─────────────────────────────────────────────
    # (step_name, display, import_path, callable_name)
    _steps = [
        ("sessionizer",        "Sessionizer",         "app.services.sessionizer",        "run_sessionizer"),
        ("track_scoring",      "Track scoring",       "app.services.track_scoring",      "run_track_scoring"),
        ("taste_profiles",     "Taste profiles",      "app.services.taste_profile",      "run_taste_profile_builder"),
        ("collab_filter",      "CF model rebuild",    "app.services.collab_filter",      "build_model"),
        ("ranker",             "Ranker training",     "app.services.ranker",             "train_model"),
        ("session_embeddings", "Session embeddings",  "app.services.session_embeddings", "train"),
        ("lastfm_cache",       "Last.fm cache",       "app.services.lastfm_candidates",  "build_cache"),
        ("sasrec",             "SASRec training",     "app.services.sasrec",             "train"),
        ("session_gru",        "Session GRU",         "app.services.session_gru",        "train"),
    ]

    for step_name, display, module_path, func_name in _steps:
        step_start(run, step_name)
        try:
            import importlib
            mod = importlib.import_module(module_path)
            func = getattr(mod, func_name)
            result = await func()
            metrics = result if isinstance(result, dict) else {}
            step_complete(run, step_name, metrics=metrics)
            logger.info(f"{display} done ({run.steps[step_name].duration_ms}ms)", extra=metrics)
        except Exception:
            err = traceback.format_exc()
            step_failed(run, step_name, error=err)
            logger.error(f"{display} failed: {err}")
        await asyncio.sleep(0.1)  # yield to event loop between heavy steps

    finish_run(run)
    logger.info(
        f"Recommendation pipeline complete (run_id={run.run_id}, "
        f"{run.status.value}, {int((run.ended_at - run.started_at) * 1000)}ms total)"
    )


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


async def _process_scrobble_queue() -> None:
    """Process pending Last.fm scrobbles."""
    try:
        from app.services.lastfm_scrobbler import process_scrobble_queue
        result = await process_scrobble_queue()
        if result.get("processed", 0) > 0 or result.get("failed", 0) > 0:
            logger.info("Scrobble queue: %s", result)
    except Exception:
        logger.error(f"Scrobble queue failed: {traceback.format_exc()}")


async def _refresh_lastfm_profiles() -> None:
    """Refresh cached Last.fm profile data for linked users."""
    try:
        from app.services.lastfm_profile import refresh_lastfm_profiles
        result = await refresh_lastfm_profiles()
        if result.get("refreshed", 0) > 0:
            logger.info("Last.fm profiles refreshed: %s", result)
    except Exception:
        logger.error(f"Last.fm profile refresh failed: {traceback.format_exc()}")
