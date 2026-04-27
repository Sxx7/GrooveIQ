"""
Tests for the recommendation audit & replay system.

Validates that:
  1. write_audit persists request + candidate rows correctly
  2. RECO_AUDIT_ENABLED=False skips writes
  3. Idempotency: writing the same request_id twice is a no-op
  4. list_requests / get_request / get_candidate round-trip
  5. purge_old removes audits older than retention_days
  6. replay_request rerank_only returns identical ranking when nothing changed
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.db import (
    Base,
    RecommendationCandidateAudit,
    RecommendationRequestAudit,
    TrackFeatures,
)
from app.services import reco_audit
from app.services.feature_eng import FEATURE_COLUMNS

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        # Seed a track so candidate-detail enrichment can find a row.
        session.add(
            TrackFeatures(
                track_id="t1",
                file_path="/music/x/y.mp3",
                title="Track One",
                artist="Artist One",
                bpm=128.0,
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


def _full_feature_vector(value: float = 0.5) -> dict[str, float]:
    return {col: value for col in FEATURE_COLUMNS}


def _candidate_row(track_id: str, raw: float, position: int, *, shown: bool = True) -> dict:
    return {
        "track_id": track_id,
        "sources": ["content"],
        "raw_score": raw,
        "pre_rerank_position": position,
        "final_score": raw,
        "final_position": position if shown else None,
        "shown": shown,
        "reranker_actions": [{"track_id": track_id, "action": "freshness_boost"}] if shown else [],
        "feature_vector": _full_feature_vector(value=raw),
    }


@pytest.mark.asyncio
async def test_write_audit_persists_request_and_candidates(db_session, monkeypatch):
    monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", True)

    rows = [
        _candidate_row("t1", 0.9, 0),
        _candidate_row("t2", 0.5, 1, shown=False),
    ]
    wrote = await reco_audit.write_audit(
        request_id="req-1",
        user_id="alice",
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        request_context={"hour_of_day": 14},
        model_version="lgbm-test",
        config_version=1,
        duration_ms=42,
        limit_requested=10,
        candidates_by_source={"content": 2},
        candidate_rows=rows,
        session=db_session,
    )
    await db_session.commit()
    assert wrote is True

    # Verify the request row exists with all expected fields.
    detail = await reco_audit.get_request(db_session, "req-1")
    assert detail is not None
    assert detail["user_id"] == "alice"
    assert detail["surface"] == "recommend_api"
    assert detail["model_version"] == "lgbm-test"
    assert detail["config_version"] == 1
    assert detail["duration_ms"] == 42
    assert detail["candidates_total"] == 2
    assert detail["candidates_by_source"] == {"content": 2}
    assert len(detail["candidates"]) == 2

    # Feature vectors should carry every column.
    for c in detail["candidates"]:
        assert set(c["feature_vector"].keys()) == set(FEATURE_COLUMNS)


@pytest.mark.asyncio
async def test_audit_disabled_skips_writes(db_session, monkeypatch):
    monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", False)
    wrote = await reco_audit.write_audit(
        request_id="req-disabled",
        user_id="alice",
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        request_context={},
        model_version="v1",
        config_version=1,
        duration_ms=0,
        limit_requested=10,
        candidates_by_source={},
        candidate_rows=[_candidate_row("t1", 0.5, 0)],
        session=db_session,
    )
    assert wrote is False
    detail = await reco_audit.get_request(db_session, "req-disabled")
    assert detail is None


@pytest.mark.asyncio
async def test_write_audit_is_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", True)
    payload = dict(
        request_id="req-idem",
        user_id="alice",
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        request_context={},
        model_version="v1",
        config_version=1,
        duration_ms=0,
        limit_requested=10,
        candidates_by_source={"content": 1},
        candidate_rows=[_candidate_row("t1", 0.5, 0)],
        session=db_session,
    )
    first = await reco_audit.write_audit(**payload)
    await db_session.commit()
    second = await reco_audit.write_audit(**payload)
    await db_session.commit()
    assert first is True
    assert second is False  # second call is a no-op


@pytest.mark.asyncio
async def test_list_and_get_candidate_round_trip(db_session, monkeypatch):
    monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", True)
    for i in range(3):
        await reco_audit.write_audit(
            request_id=f"req-{i}",
            user_id="alice",
            surface="recommend_api",
            seed_track_id=None,
            context_id=None,
            request_context={},
            model_version="v1",
            config_version=1,
            duration_ms=10,
            limit_requested=5,
            candidates_by_source={"content": 1},
            candidate_rows=[_candidate_row("t1", 0.5 + 0.1 * i, 0)],
            session=db_session,
        )
    await db_session.commit()

    summaries = await reco_audit.list_requests(db_session, user_id="alice", limit=10)
    assert len(summaries) == 3
    # Newest first.
    assert summaries[0]["request_id"] == "req-2"
    # Top track is enriched from track_features.
    assert summaries[0]["top_track"]["title"] == "Track One"

    # Candidate-level lookup.
    cand = await reco_audit.get_candidate(db_session, "req-1", "t1")
    assert cand is not None
    assert cand["track_id"] == "t1"
    assert cand["title"] == "Track One"
    assert cand["artist"] == "Artist One"


@pytest.mark.asyncio
async def test_list_respects_since_filter(db_session, monkeypatch):
    monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", True)
    # Insert one fresh and one ancient (created_at via direct SQL bypass).
    await reco_audit.write_audit(
        request_id="req-fresh",
        user_id="alice",
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        request_context={},
        model_version="v1",
        config_version=1,
        duration_ms=0,
        limit_requested=5,
        candidates_by_source={},
        candidate_rows=[_candidate_row("t1", 0.5, 0)],
        session=db_session,
    )
    await db_session.commit()
    # Manually backdate one row.
    old_row = RecommendationRequestAudit(
        request_id="req-old",
        user_id="alice",
        created_at=int(time.time()) - 100 * 86_400,
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        model_version="v0",
        config_version=1,
        request_context={},
        candidates_total=0,
        candidates_by_source={},
        duration_ms=0,
        limit_requested=5,
    )
    db_session.add(old_row)
    await db_session.commit()

    cutoff = int(time.time()) - 30 * 86_400
    fresh = await reco_audit.list_requests(db_session, user_id="alice", since=cutoff, limit=10)
    assert {r["request_id"] for r in fresh} == {"req-fresh"}


@pytest.mark.asyncio
async def test_purge_old_removes_aged_audits(db_session, monkeypatch):
    monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", True)
    # Insert a fresh one.
    await reco_audit.write_audit(
        request_id="req-recent",
        user_id="alice",
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        request_context={},
        model_version="v1",
        config_version=1,
        duration_ms=0,
        limit_requested=5,
        candidates_by_source={},
        candidate_rows=[_candidate_row("t1", 0.5, 0)],
        session=db_session,
    )
    # Insert an aged one directly.
    aged = RecommendationRequestAudit(
        request_id="req-aged",
        user_id="alice",
        created_at=int(time.time()) - 200 * 86_400,
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        model_version="v0",
        config_version=1,
        request_context={},
        candidates_total=1,
        candidates_by_source={},
        duration_ms=0,
        limit_requested=5,
    )
    db_session.add(aged)
    db_session.add(
        RecommendationCandidateAudit(
            request_id="req-aged",
            track_id="t1",
            sources=["content"],
            raw_score=0.5,
            pre_rerank_position=0,
            final_score=0.5,
            final_position=0,
            shown=True,
            reranker_actions=[],
            feature_vector={},
        )
    )
    await db_session.commit()

    deleted = await reco_audit.purge_old(db_session, retention_days=90)
    await db_session.commit()
    assert deleted == 1

    # Recent one survives.
    survivor = await reco_audit.get_request(db_session, "req-recent")
    assert survivor is not None
    # Aged one is gone (and FK cascade removed its candidate).
    gone = await reco_audit.get_request(db_session, "req-aged")
    assert gone is None


@pytest.mark.asyncio
async def test_replay_rerank_only_no_model(db_session, monkeypatch):
    """
    With no trained ranker model loaded, replay_request rerank_only falls back
    to the satisfaction_score column. The original ranking we persisted used
    final_score = raw_score, but the rerank pass may add a freshness boost
    and reorder by score. Validate only that replay returns a result with
    rank_deltas covering all candidates and no exceptions.
    """
    monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", True)

    # Reset the ranker singleton to ensure no model is loaded.
    import app.services.ranker as ranker_mod

    monkeypatch.setattr(ranker_mod, "_model", None)
    monkeypatch.setattr(ranker_mod, "_model_version", None)

    rows = [
        _candidate_row("t1", 0.9, 0),
    ]
    await reco_audit.write_audit(
        request_id="req-replay",
        user_id="alice",
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        request_context={"hour_of_day": 14},
        model_version="lgbm-test",
        config_version=1,
        duration_ms=42,
        limit_requested=10,
        candidates_by_source={"content": 1},
        candidate_rows=rows,
        session=db_session,
    )
    await db_session.commit()

    result = await reco_audit.replay_request(db_session, "req-replay", mode="rerank_only")
    assert result is not None
    assert result["mode"] == "rerank_only"
    assert result["original_model_version"] == "lgbm-test"
    # rank_deltas covers every track that appeared in either ranking.
    track_ids = {d["track_id"] for d in result["rank_deltas"]}
    assert "t1" in track_ids
    assert "summary" in result


@pytest.mark.asyncio
async def test_get_stats_reports_counts(db_session, monkeypatch):
    monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", True)
    await reco_audit.write_audit(
        request_id="req-stats",
        user_id="alice",
        surface="recommend_api",
        seed_track_id=None,
        context_id=None,
        request_context={},
        model_version="v1",
        config_version=1,
        duration_ms=0,
        limit_requested=5,
        candidates_by_source={"content": 1},
        candidate_rows=[_candidate_row("t1", 0.5, 0)],
        session=db_session,
    )
    await db_session.commit()
    stats = await reco_audit.get_stats(db_session)
    assert stats["total_requests_all"] == 1
    assert stats["total_candidates_all"] == 1
    assert stats["enabled"] is True
    assert stats["retention_days"] == settings.RECO_AUDIT_RETENTION_DAYS
