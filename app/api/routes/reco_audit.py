"""GrooveIQ – Recommendation audit & replay API routes."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_admin, require_api_key
from app.db.session import get_session
from app.models.schemas import (
    CandidateAuditDetail,
    ReplayRequest,
    ReplayResult,
    RequestAuditDetail,
    RequestAuditSummary,
)
from app.services import reco_audit

router = APIRouter()


@router.get(
    "/recommend/audit/sessions",
    summary="List recent recommendation audit sessions",
    description="""
Returns paginated audit summaries for past recommendation requests.
Filterable by user_id, surface (home|radio|search|recommend_api), and time range.

Each summary carries the request context plus the position-0 (top) track so
the list view can render at-a-glance information.
""",
    response_model=list[RequestAuditSummary],
)
async def list_audit_sessions(
    user_id: str | None = Query(None, description="Filter by user. Required for non-admin keys."),
    surface: str | None = Query(None, description="Filter by surface (home, radio, search, recommend_api)."),
    since_days: int | None = Query(None, ge=1, le=365, description="Only requests newer than N days."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    if user_id is None:
        # Listing across all users requires admin.
        require_admin(_key)
    else:
        check_user_access(_key, user_id)

    since = int(time.time()) - since_days * 86_400 if since_days else None
    rows = await reco_audit.list_requests(
        session,
        user_id=user_id,
        surface=surface,
        limit=limit,
        offset=offset,
        since=since,
    )
    return [RequestAuditSummary(**r) for r in rows]


@router.get(
    "/recommend/audit/stats",
    summary="Audit storage and retention stats",
)
async def audit_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    return await reco_audit.get_stats(session)


@router.get(
    "/recommend/audit/{request_id}",
    summary="Full audit detail for a single request",
    response_model=RequestAuditDetail,
)
async def get_audit_request(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    detail = await reco_audit.get_request(session, request_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Audit not found.")
    check_user_access(_key, detail["user_id"])
    return RequestAuditDetail(**detail)


@router.get(
    "/recommend/audit/{request_id}/track/{track_id}",
    summary="Single candidate's full audit (feature vector, sources, reranker actions)",
    response_model=CandidateAuditDetail,
)
async def get_audit_candidate(
    request_id: str,
    track_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Verify the user owns this request.
    parent = await reco_audit.get_request(session, request_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Audit not found.")
    check_user_access(_key, parent["user_id"])

    detail = await reco_audit.get_candidate(session, request_id, track_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Candidate not found in this audit.")
    return CandidateAuditDetail(**detail)


@router.post(
    "/recommend/audit/{request_id}/replay",
    summary="Replay a past request against the current ranker / config",
    description="""
Re-rank the persisted candidate pool for this request using the current ranker
model and reranker rules. Returns rank deltas + summary metrics (top-10 overlap,
Kendall's tau, avg absolute rank delta).

Modes:
  - **rerank_only** (default, cheap): re-score persisted feature vectors with
    the current model; answers "did the latest tuning help?".
  - **full**: rebuild feature vectors live (so candidate-gen / feature-eng
    changes are reflected) and re-score with the current model.
""",
    response_model=ReplayResult,
)
async def replay_audit_request(
    request_id: str,
    body: ReplayRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    parent = await reco_audit.get_request(session, request_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Audit not found.")
    check_user_access(_key, parent["user_id"])

    try:
        result = await reco_audit.replay_request(session, request_id, mode=body.mode)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if result is None:
        raise HTTPException(status_code=404, detail="Audit not found.")
    return ReplayResult(**result)
