"""
GrooveIQ – Tests for evaluation service (Phase 4, Step 9).

Tests impression logging (via recommend endpoint), holdout evaluation,
and impression-to-stream metrics.
"""

from __future__ import annotations

import base64
import time

import numpy as np
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, ListenEvent, TrackFeatures, TrackInteraction, User

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


def _make_embedding(seed: int = 0) -> str:
    rng = np.random.RandomState(seed)
    vec = rng.randn(64).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return base64.b64encode(vec.tobytes()).decode()


def _now() -> int:
    return int(time.time())


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr("app.services.evaluation.AsyncSessionLocal", _TestSession)
    monkeypatch.setattr("app.services.feature_eng.AsyncSessionLocal", _TestSession, raising=False)
    monkeypatch.setattr("app.services.ranker.AsyncSessionLocal", _TestSession)

    yield

    # Reset singletons.
    import app.services.ranker as r

    r._model = None
    r._model_version = None
    r._model_stats = {}

    import app.services.evaluation as ev

    ev._last_eval = {}

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed_evaluation_data():
    """Create a realistic dataset for holdout evaluation."""
    now = _now()
    async with _TestSession() as session:
        # 20 tracks
        for i in range(20):
            session.add(
                TrackFeatures(
                    track_id=f"t{i}",
                    file_path=f"/music/artist{i % 4}/album/t{i}.mp3",
                    bpm=100.0 + i * 3,
                    energy=0.3 + (i % 10) * 0.07,
                    danceability=0.4 + (i % 6) * 0.1,
                    valence=0.5,
                    loudness=-10.0,
                    instrumentalness=0.1,
                    duration=200.0,
                    embedding=_make_embedding(i),
                    mood_tags=[{"label": "happy", "confidence": 0.7}],
                    analyzed_at=now,
                    analysis_version="1",
                )
            )

        # 3 users, 15 interactions each with varying timestamps
        for u in range(3):
            session.add(
                User(
                    user_id=f"user{u}",
                    last_seen=now,
                    taste_profile={
                        "top_tracks": [{"track_id": f"t{j}", "score": 0.8} for j in range(5)],
                        "audio_preferences": {"bpm": {"mean": 120, "std": 10}, "energy": {"mean": 0.6, "std": 0.1}},
                        "mood_preferences": {"happy": 0.5},
                        "time_patterns": {},
                        "updated_at": now,
                    },
                )
            )
            for i in range(15):
                # Spread timestamps across 10 days (train=first 8 days, test=last 2)
                ts = now - 86_400 * 10 + 86_400 * i * 10 // 15
                session.add(
                    TrackInteraction(
                        user_id=f"user{u}",
                        track_id=f"t{i % 20}",
                        play_count=3,
                        skip_count=i % 3,
                        like_count=1 if i < 3 else 0,
                        dislike_count=0,
                        repeat_count=0,
                        playlist_add_count=0,
                        queue_add_count=0,
                        early_skip_count=0,
                        mid_skip_count=0,
                        full_listen_count=2,
                        total_seekfwd=0,
                        total_seekbk=0,
                        satisfaction_score=0.3 + (i % 8) * 0.08,
                        last_event_id=u * 100 + i,
                        first_played_at=ts,
                        last_played_at=ts + 300,
                        updated_at=now,
                    )
                )
        await session.commit()


class TestImpressionLogging:
    async def test_impressions_logged(self):
        """Reco impressions are stored in listen_events."""
        now = _now()
        async with _TestSession() as session:
            # Simulate what the client logs when it renders a shelf.
            for i in range(5):
                session.add(
                    ListenEvent(
                        user_id="user0",
                        track_id=f"t{i}",
                        event_type="reco_impression",
                        surface="library:in_this_mix",
                        position=i,
                        request_id="req-123",
                        model_version="test-v1",
                        timestamp=now,
                    )
                )
            await session.commit()

        async with _TestSession() as session:
            count = (
                await session.execute(
                    select(func.count(ListenEvent.id)).where(ListenEvent.event_type == "reco_impression")
                )
            ).scalar_one()
            assert count == 5

            # All share the same request_id.
            req_ids = (
                (
                    await session.execute(
                        select(ListenEvent.request_id).where(ListenEvent.event_type == "reco_impression").distinct()
                    )
                )
                .scalars()
                .all()
            )
            assert req_ids == ["req-123"]


class TestImpressionStats:
    async def test_impression_to_stream_rate(self):
        """I2S rate is computed correctly."""
        from app.services.evaluation import get_impression_stats

        now = _now()
        async with _TestSession() as session:
            # 10 impressions with request_id "rq1".
            for i in range(10):
                session.add(
                    ListenEvent(
                        user_id="user0",
                        track_id=f"t{i}",
                        event_type="reco_impression",
                        request_id="rq1",
                        timestamp=now,
                    )
                )
            # 3 plays attributed to "rq1".
            for i in range(3):
                session.add(
                    ListenEvent(
                        user_id="user0",
                        track_id=f"t{i}",
                        event_type="play_start",
                        request_id="rq1",
                        timestamp=now + i,
                    )
                )
            await session.commit()

        stats = await get_impression_stats()
        assert stats["impressions"] == 10
        assert stats["streams_from_reco"] == 3
        assert stats["i2s_rate"] == 0.3

    async def test_no_impressions(self):
        """No impressions returns null i2s."""
        from app.services.evaluation import get_impression_stats

        stats = await get_impression_stats()
        assert stats["impressions"] == 0
        assert stats["i2s_rate"] is None


class TestImpressionSurfaceExclusion:
    """Regression tests for the reco_impression data-integrity fix.

    Server-side ``surface="recommend_api"`` rows record the *served list* (whether
    or not a track was actually shown), not a real impression. They must be excluded
    from impression-based negatives, position bias, and i2s — while genuinely-shown
    surfaces (client surfaces, ``radio``) and legacy NULL-surface rows are kept.
    """

    async def test_get_impression_stats_excludes_recommend_api(self):
        from app.services.evaluation import get_impression_stats

        now = _now()
        async with _TestSession() as session:
            # 4 server-side recommend_api impressions — must be ignored.
            for i in range(4):
                session.add(
                    ListenEvent(
                        user_id="user0",
                        track_id=f"srv{i}",
                        event_type="reco_impression",
                        surface="recommend_api",
                        request_id="rq-srv",
                        timestamp=now,
                    )
                )
            # 2 genuinely-shown client impressions — must count.
            for i in range(2):
                session.add(
                    ListenEvent(
                        user_id="user0",
                        track_id=f"cli{i}",
                        event_type="reco_impression",
                        surface="library:in_this_mix",
                        request_id="rq-cli",
                        timestamp=now,
                    )
                )
            # 1 play attributed to the client request.
            session.add(
                ListenEvent(
                    user_id="user0",
                    track_id="cli0",
                    event_type="play_start",
                    request_id="rq-cli",
                    timestamp=now + 1,
                )
            )
            await session.commit()

        stats = await get_impression_stats()
        # Only the 2 client impressions count; the 4 recommend_api rows are excluded.
        assert stats["impressions"] == 2
        # The recommend_api request is not counted as an impression request either.
        assert stats["impression_requests"] == 1
        # i2s denominator uses the clean count → 1 stream / 2 impressions.
        assert stats["streams_from_reco"] == 1
        assert stats["i2s_rate"] == 0.5

    async def test_get_impression_stats_keeps_radio_and_null(self):
        from app.services.evaluation import get_impression_stats

        now = _now()
        async with _TestSession() as session:
            session.add_all(
                [
                    ListenEvent(
                        user_id="u",
                        track_id="r0",
                        event_type="reco_impression",
                        surface="radio",
                        request_id="rq-radio",
                        timestamp=now,
                    ),
                    ListenEvent(
                        user_id="u",
                        track_id="n0",
                        event_type="reco_impression",
                        surface=None,
                        request_id="rq-null",
                        timestamp=now,
                    ),
                    ListenEvent(
                        user_id="u",
                        track_id="s0",
                        event_type="reco_impression",
                        surface="recommend_api",
                        request_id="rq-srv",
                        timestamp=now,
                    ),
                ]
            )
            await session.commit()

        stats = await get_impression_stats()
        # radio + NULL-surface kept (2); recommend_api excluded.
        assert stats["impressions"] == 2

    async def test_load_impression_negatives_excludes_recommend_api(self):
        from app.services.feature_eng import _load_impression_negatives

        now = _now()
        async with _TestSession() as session:
            session.add_all(
                [
                    # Shown only via recommend_api, never played → must NOT be a negative.
                    ListenEvent(
                        user_id="u",
                        track_id="server_only",
                        event_type="reco_impression",
                        surface="recommend_api",
                        request_id="rq1",
                        timestamp=now,
                    ),
                    # Genuinely shown (client), never played → SHOULD be a negative.
                    ListenEvent(
                        user_id="u",
                        track_id="client_shown",
                        event_type="reco_impression",
                        surface="library:in_this_mix",
                        request_id="rq2",
                        timestamp=now,
                    ),
                    # Shown (client) AND played → not a negative.
                    ListenEvent(
                        user_id="u",
                        track_id="played",
                        event_type="reco_impression",
                        surface="discover:discover_mix",
                        request_id="rq3",
                        timestamp=now,
                    ),
                    ListenEvent(
                        user_id="u", track_id="played", event_type="play_start", request_id="rq3", timestamp=now + 1
                    ),
                ]
            )
            await session.commit()
            negatives = await _load_impression_negatives(session)

        user_negs = negatives.get("u", set())
        assert "client_shown" in user_negs
        assert "server_only" not in user_negs  # recommend_api excluded — the core fix
        assert "played" not in user_negs

    async def test_load_impression_positions_excludes_recommend_api(self):
        from app.services.feature_eng import _load_impression_positions

        now = _now()
        async with _TestSession() as session:
            session.add_all(
                [
                    # recommend_api rank (10) must be ignored...
                    ListenEvent(
                        user_id="u",
                        track_id="t",
                        event_type="reco_impression",
                        surface="recommend_api",
                        position=10,
                        request_id="rq1",
                        timestamp=now,
                    ),
                    # ...only the real shown position (2) should remain.
                    ListenEvent(
                        user_id="u",
                        track_id="t",
                        event_type="reco_impression",
                        surface="library:in_this_mix",
                        position=2,
                        request_id="rq2",
                        timestamp=now,
                    ),
                ]
            )
            await session.commit()
            positions = await _load_impression_positions(session)

        # Avg position reflects only the shown row → 2, not avg(10, 2) = 6.
        assert positions["u"]["t"] == 2


class TestHoldoutEvaluation:
    async def test_evaluation_runs(self):
        """Holdout evaluation produces valid NDCG metrics."""
        from app.services.evaluation import evaluate_holdout

        await _seed_evaluation_data()
        result = await evaluate_holdout()

        assert "metrics" in result
        metrics = result["metrics"]
        if metrics.get("evaluated_users", 0) > 0:
            assert metrics["ndcg_at_10"] is not None
            assert 0.0 <= metrics["ndcg_at_10"] <= 1.0
            assert metrics["ndcg_at_50"] is not None

    async def test_evaluation_insufficient_data(self):
        """Evaluation returns error with too little data."""
        from app.services.evaluation import evaluate_holdout

        # Empty DB.
        result = await evaluate_holdout()
        assert "error" in result


class TestModelReport:
    async def test_model_report_structure(self):
        """Model report includes ranker, evaluation, and impression sections."""
        from app.services.evaluation import get_model_report

        report = await get_model_report()
        assert "ranker" in report
        assert "latest_evaluation" in report
        assert "impressions" in report
        assert report["ranker"]["trained"] is False
