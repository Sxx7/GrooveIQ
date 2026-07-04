"""GrooveIQ — Admin diagnostics routes.

Endpoints intended for operators / dashboards rather than end-users.
Admin API key required. Surfaces signal that helps catch silent
failures in the analysis pipeline before they require a multi-day
re-analysis to recover from.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import check_user_access, require_admin, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.models.schemas import NotificationRecommendationRequest
from app.services.analysis_health import overall_status, run_invariants

router = APIRouter()


@router.post(
    "/admin/follow-scan",
    summary="Run followed-artist release scan + reconciler once (dev/QA)",
)
async def trigger_follow_scan(_key: str = Depends(require_api_key)):
    """Run the detection loop + availability reconciler synchronously. Admin-gated."""
    require_admin(_key)
    if not settings.follow_scan_enabled:
        return {
            "status": "error",
            "message": "Follow-scan not enabled. Set FOLLOW_SCAN_ENABLED=true and configure a detector backend.",
        }
    from app.workers.scheduler import run_follow_scan_now

    result = await run_follow_scan_now()
    return {"status": "completed", "result": result}


@router.post(
    "/admin/follow-dispatch",
    summary="Drain pending new-release notifications once (dev/QA, P2)",
)
async def trigger_follow_dispatch(_key: str = Depends(require_api_key)):
    """Run the push dispatch step synchronously. Admin-gated. Off unless
    PUSH_ENABLED (Apprise is the delivery transport)."""
    require_admin(_key)
    if not settings.push_enabled:
        return {
            "status": "error",
            "message": "Push not enabled. Set PUSH_ENABLED=true (delivery is via Apprise; APPRISE_ENABLED defaults true).",
        }
    from app.workers.scheduler import run_dispatch_now

    result = await run_dispatch_now()
    return {"status": "completed", "result": result}


@router.post(
    "/admin/notify-recommendation",
    summary="Send a recommendation push to a user (goal F, dev/QA + reco-run seam)",
)
async def trigger_recommendation(
    body: NotificationRecommendationRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Emit a ``recommendation`` notification and dispatch it. Admin-gated. This is
    the integration seam a recommendation run (or an inactivity nudge) calls to
    draw a user back with a fresh mix. Off unless PUSH_ENABLED."""
    require_admin(_key)
    validate_user_id(body.user_id)
    check_user_access(_key, body.user_id)
    if not settings.push_enabled:
        return {"status": "error", "message": "Push not enabled. Set PUSH_ENABLED=true."}

    from app.services.notification_dispatch import emit_recommendation

    created = await emit_recommendation(
        session, body.user_id, title=body.title, body=body.body, playlist_id=body.playlist_id
    )
    await session.commit()

    from app.workers.scheduler import run_dispatch_now

    dispatched = await run_dispatch_now() if created else {"processed": 0}
    return {"status": "completed", "created": created, "dispatched": dispatched}


@router.get(
    "/admin/analysis-health",
    summary="Library-wide invariants over track_features (Layer 3)",
)
async def analysis_health(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Runs every invariant in ``app.services.analysis_health.INVARIANTS``
    and returns per-check pass/fail.

    The intended use is a periodic monitor (or on-demand operator check)
    that surfaces:

      - Range bugs (loudness > 0, valence > 1, BPM > 250) — would have
        caught #88's stale Stevens-law and approachability_regression
        outputs.
      - NULL embedding leaks (#42, #83) on the latest analysis_version.
      - Distribution inversions (#99 voice_instrumental) by checking the
        library-wide AVG against a sane prior.
      - Distribution compressions (#88-style mood_happy pinning) by
        checking AVG(valence) and AVG(danceability) against bands that
        rule out 'all values stuck at one extreme'.
      - Stalled re-analysis (rescan stuck after a version bump).
    """
    require_admin(_key)
    results = await run_invariants(session)

    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r.status == "ok"),
        "fail": sum(1 for r in results if r.status == "fail" and r.invariant.severity == "error"),
        "warn": sum(1 for r in results if r.status == "fail" and r.invariant.severity == "warn"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "error": sum(1 for r in results if r.status == "error"),
    }

    return {
        "checked_at": int(time.time()),
        "overall_status": overall_status(results),
        "summary": summary,
        "checks": [r.to_dict() for r in results],
    }
