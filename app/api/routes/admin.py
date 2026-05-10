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

from app.core.security import require_admin, require_api_key
from app.db.session import get_session
from app.services.analysis_health import overall_status, run_invariants

router = APIRouter()


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
