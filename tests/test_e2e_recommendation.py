"""
GrooveIQ – End-to-end recommendation quality tests.

These tests validate that the full pipeline produces sensible recommendations:
  1. Ingest raw events for synthetic users with distinct preferences
  2. Run the full pipeline (sessionizer → scoring → taste profiles → ranker)
  3. Get recommendations via the API
  4. Verify recommendations match user preferences (not random)

This catches issues that unit tests miss: component interactions,
feature engineering bugs, model training failures, and pipeline wiring.
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncGenerator

import numpy as np
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import (
    Base,
    ListenEvent,
    TrackFeatures,
    TrackInteraction,
    User,
)

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


async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app.dependency_overrides[get_session] = override_get_session

    # Patch all services that create their own sessions.
    monkeypatch.setattr("app.services.feature_eng.AsyncSessionLocal", _TestSession, raising=False)
    monkeypatch.setattr("app.services.ranker.AsyncSessionLocal", _TestSession)
    monkeypatch.setattr("app.services.sessionizer.AsyncSessionLocal", _TestSession, raising=False)
    monkeypatch.setattr("app.services.track_scoring.AsyncSessionLocal", _TestSession, raising=False)
    monkeypatch.setattr("app.services.taste_profile.AsyncSessionLocal", _TestSession, raising=False)
    monkeypatch.setattr("app.services.collab_filter.AsyncSessionLocal", _TestSession, raising=False)
    monkeypatch.setattr("app.services.evaluation.AsyncSessionLocal", _TestSession, raising=False)

    yield

    # Reset singletons.
    import app.services.ranker as r

    r._model = None
    r._model_version = None
    r._model_stats = {}

    import app.services.collab_filter as cf

    cf._user_factors = None
    cf._item_factors = None

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Synthetic data: two users with opposite preferences.
#
# "metal_fan" — likes high-energy, loud, fast tracks (tracks t0-t9)
# "chill_fan" — likes low-energy, quiet, slow tracks (tracks t10-t19)
#
# If the algorithm works, metal_fan's recommendations should lean toward
# t0-t9 and chill_fan's should lean toward t10-t19.
# ---------------------------------------------------------------------------


async def _create_library():
    """Create 20 tracks: 10 high-energy 'metal' + 10 low-energy 'chill'."""
    now = _now()
    async with _TestSession() as session:
        for i in range(20):
            is_metal = i < 10
            session.add(
                TrackFeatures(
                    track_id=f"t{i}",
                    file_path=f"/music/{'Metal' if is_metal else 'Chill'}/album{i % 4}/track{i}.mp3",
                    title=f"Track {i}",
                    artist=f"{'Metal' if is_metal else 'Chill'} Artist {i % 4}",
                    bpm=160.0 + i * 2 if is_metal else 80.0 + i * 2,
                    energy=0.8 + (i % 5) * 0.04 if is_metal else 0.2 + (i % 5) * 0.04,
                    danceability=0.3 if is_metal else 0.7,
                    valence=0.3 if is_metal else 0.7,
                    loudness=-3.0 if is_metal else -15.0,
                    instrumentalness=0.1 if is_metal else 0.5,
                    duration=240.0 + i * 5,
                    embedding=_make_embedding(i),
                    mood_tags=[{"label": "aggressive", "confidence": 0.9}]
                    if is_metal
                    else [{"label": "relaxed", "confidence": 0.9}],
                    analyzed_at=now - 86_400 * 30,
                    analysis_version="1",
                )
            )
        await session.commit()


async def _create_users_and_events():
    """
    Create three users with clear preferences and enough listening history.

    metal_fan:  plays t0-t9 heavily, skips t10-t19
    chill_fan:  plays t10-t19 heavily, skips t0-t9
    mixed_fan:  plays both but prefers metal slightly (needed to exceed 50 training samples)
    """
    now = _now()
    async with _TestSession() as session:
        # Create users.
        session.add(User(user_id="metal_fan", last_seen=now))
        session.add(User(user_id="chill_fan", last_seen=now))
        session.add(User(user_id="mixed_fan", last_seen=now))
        await session.flush()

        for user_id, liked_range, disliked_range in [
            ("metal_fan", range(0, 10), range(10, 20)),
            ("chill_fan", range(10, 20), range(0, 10)),
            ("mixed_fan", range(0, 10), range(10, 20)),
        ]:
            # Generate sessions over the past 7 days.
            for day in range(7):
                session_ts = now - 86_400 * (7 - day) + 3600 * 14  # 2pm each day

                # Play liked tracks with full listens.
                for i, ti in enumerate(liked_range):
                    ts = session_ts + i * 300  # 5 min apart
                    session.add(
                        ListenEvent(
                            user_id=user_id,
                            track_id=f"t{ti}",
                            event_type="play_start",
                            timestamp=ts,
                            session_id=f"{user_id}_day{day}",
                            hour_of_day=14,
                            day_of_week=day % 7 + 1,
                        )
                    )
                    session.add(
                        ListenEvent(
                            user_id=user_id,
                            track_id=f"t{ti}",
                            event_type="play_end",
                            value=0.95,  # 95% completion
                            dwell_ms=240_000,  # 4 minutes
                            timestamp=ts + 240,
                            session_id=f"{user_id}_day{day}",
                            hour_of_day=14,
                            day_of_week=day % 7 + 1,
                        )
                    )

                # Skip disliked tracks early.
                for i, ti in enumerate(disliked_range):
                    ts = session_ts + (10 + i) * 300
                    session.add(
                        ListenEvent(
                            user_id=user_id,
                            track_id=f"t{ti}",
                            event_type="play_start",
                            timestamp=ts,
                            session_id=f"{user_id}_day{day}",
                        )
                    )
                    session.add(
                        ListenEvent(
                            user_id=user_id,
                            track_id=f"t{ti}",
                            event_type="skip",
                            value=1.5,  # skipped at 1.5s
                            dwell_ms=1500,
                            timestamp=ts + 2,
                            session_id=f"{user_id}_day{day}",
                        )
                    )

                # Add some explicit likes for liked tracks.
                if day % 2 == 0:
                    for ti in list(liked_range)[:3]:
                        session.add(
                            ListenEvent(
                                user_id=user_id,
                                track_id=f"t{ti}",
                                event_type="like",
                                timestamp=session_ts + 5000,
                                session_id=f"{user_id}_day{day}",
                            )
                        )

        await session.commit()


async def _run_pipeline():
    """Run the full recommendation pipeline."""
    from app.services.ranker import train_model
    from app.services.sessionizer import run_sessionizer
    from app.services.taste_profile import run_taste_profile_builder
    from app.services.track_scoring import run_track_scoring

    await run_sessionizer()
    await run_track_scoring()
    await run_taste_profile_builder()

    # Try CF but don't fail if it can't train (needs enough data).
    try:
        from app.services.collab_filter import build_model

        await build_model()
    except Exception:
        pass

    ranker_result = await train_model()
    return ranker_result


class TestEndToEndRecommendation:
    """Full pipeline: events → sessionizer → scoring → profiles → ranker → recommend."""

    async def test_pipeline_produces_trained_model(self):
        """The pipeline should train a model from the synthetic data."""
        await _create_library()
        await _create_users_and_events()
        result = await _run_pipeline()

        assert result["trained"] is True
        assert result["training_samples"] >= 20

    async def test_metal_fan_gets_metal(self, client: AsyncClient):
        """metal_fan's top recommendations should be high-energy tracks."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        resp = await client.get("/v1/recommend/metal_fan?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        tracks = data["tracks"]

        assert len(tracks) > 0, "Should return some recommendations"

        # Count how many of the top-5 are from the liked set (t0-t9).
        metal_ids = {f"t{i}" for i in range(10)}
        top5_ids = [t["track_id"] for t in tracks[:5]]
        metal_in_top5 = sum(1 for tid in top5_ids if tid in metal_ids)

        # At least 3 of top 5 should be metal tracks.
        assert metal_in_top5 >= 3, (
            f"metal_fan should get mostly metal tracks in top 5, got {metal_in_top5}/5: {top5_ids}"
        )

    async def test_chill_fan_gets_chill(self, client: AsyncClient):
        """chill_fan's top recommendations should be low-energy tracks."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        resp = await client.get("/v1/recommend/chill_fan?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        tracks = data["tracks"]

        assert len(tracks) > 0

        chill_ids = {f"t{i}" for i in range(10, 20)}
        top5_ids = [t["track_id"] for t in tracks[:5]]
        chill_in_top5 = sum(1 for tid in top5_ids if tid in chill_ids)

        assert chill_in_top5 >= 3, (
            f"chill_fan should get mostly chill tracks in top 5, got {chill_in_top5}/5: {top5_ids}"
        )

    async def test_users_get_different_recommendations(self, client: AsyncClient):
        """Two users with opposite tastes should get different recommendations."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        resp_metal = await client.get("/v1/recommend/metal_fan?limit=10")
        resp_chill = await client.get("/v1/recommend/chill_fan?limit=10")

        metal_tracks = {t["track_id"] for t in resp_metal.json()["tracks"][:5]}
        chill_tracks = {t["track_id"] for t in resp_chill.json()["tracks"][:5]}

        # Top-5 lists should overlap by at most 2 tracks (allowing for
        # exploration slots which inject random low-interaction tracks).
        overlap = metal_tracks & chill_tracks
        assert len(overlap) <= 2, f"Users with opposite tastes should get different recs, overlap={overlap}"

    async def test_recommendations_include_metadata(self, client: AsyncClient):
        """Recommendation response should include track metadata."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        resp = await client.get("/v1/recommend/metal_fan?limit=5")
        data = resp.json()

        assert "request_id" in data
        assert "model_version" in data

        track = data["tracks"][0]
        assert "track_id" in track
        assert "score" in track
        assert "source" in track
        assert "position" in track

    async def test_impression_tracking(self, client: AsyncClient):
        """Recommendations should log reco_impression events for feedback loop."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        resp = await client.get("/v1/recommend/metal_fan?limit=5")
        request_id = resp.json()["request_id"]

        # Check that impression events were logged.
        async with _TestSession() as session:
            from sqlalchemy import func, select

            count = (
                await session.execute(
                    select(func.count(ListenEvent.id)).where(
                        ListenEvent.event_type == "reco_impression",
                        ListenEvent.request_id == request_id,
                    )
                )
            ).scalar_one()

        assert count == 5, f"Should log 5 impression events, got {count}"

    async def test_model_beats_random_baseline(self):
        """The trained model should produce better NDCG than random ordering."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        from app.services.evaluation import evaluate_holdout

        result = await evaluate_holdout()

        metrics = result.get("metrics", {})
        if metrics.get("ndcg_at_10") is not None:
            model_ndcg = metrics["ndcg_at_10"]
            random_ndcg = metrics.get("baseline_random_ndcg_at_10")

            if random_ndcg is not None:
                assert model_ndcg >= random_ndcg, f"Model NDCG@10 ({model_ndcg}) should beat random ({random_ndcg})"

    async def test_model_stats_endpoint(self, client: AsyncClient):
        """GET /v1/stats/model should return ranker info and impression stats."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        # Generate some impressions first.
        await client.get("/v1/recommend/metal_fan?limit=5")

        resp = await client.get("/v1/stats/model")
        assert resp.status_code == 200
        data = resp.json()

        assert "ranker" in data
        assert data["ranker"]["trained"] is True
        assert "impressions" in data
        assert data["impressions"]["impressions"] >= 5

    async def test_empty_user_gets_graceful_response(self, client: AsyncClient):
        """A user with no history should get a reason, not an error."""
        await _create_library()
        async with _TestSession() as session:
            session.add(User(user_id="newbie", last_seen=_now()))
            await session.commit()

        resp = await client.get("/v1/recommend/newbie?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tracks"] == [] or "reason" in data

    async def test_nonexistent_user_404(self, client: AsyncClient):
        """Requesting recs for unknown user should 404."""
        resp = await client.get("/v1/recommend/ghost?limit=10")
        assert resp.status_code == 404


class TestSemanticQuality:
    """Verify recommendations are semantically meaningful, not just structurally correct."""

    async def test_high_energy_user_gets_high_energy_tracks(self, client: AsyncClient):
        """metal_fan's recommendations should have higher avg energy than chill_fan's."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        resp_metal = await client.get("/v1/recommend/metal_fan?limit=10")
        resp_chill = await client.get("/v1/recommend/chill_fan?limit=10")

        metal_energies = [t["energy"] for t in resp_metal.json()["tracks"] if t.get("energy")]
        chill_energies = [t["energy"] for t in resp_chill.json()["tracks"] if t.get("energy")]

        if metal_energies and chill_energies:
            avg_metal = sum(metal_energies) / len(metal_energies)
            avg_chill = sum(chill_energies) / len(chill_energies)

            assert avg_metal > avg_chill, (
                f"metal_fan's avg energy ({avg_metal:.2f}) should be higher than chill_fan's ({avg_chill:.2f})"
            )

    async def test_skipped_tracks_suppressed(self, client: AsyncClient):
        """Tracks a user consistently skipped should not dominate top recommendations."""
        await _create_library()
        await _create_users_and_events()
        await _run_pipeline()

        resp = await client.get("/v1/recommend/metal_fan?limit=10")
        tracks = resp.json()["tracks"]

        # metal_fan's disliked tracks (t10-t19) — chill tracks they skip.
        disliked_ids = {f"t{i}" for i in range(10, 20)}
        top3_ids = [t["track_id"] for t in tracks[:3]]
        disliked_in_top3 = sum(1 for tid in top3_ids if tid in disliked_ids)

        # At most 1 disliked track in top 3 (could be exploration slot).
        assert disliked_in_top3 <= 1, (
            f"Skipped tracks should not dominate top recs, got {disliked_in_top3}/3 disliked in top 3: {top3_ids}"
        )

    async def test_satisfaction_scores_match_behavior(self):
        """Track interaction scores should reflect listening behavior."""
        await _create_library()
        await _create_users_and_events()

        from app.services.sessionizer import run_sessionizer
        from app.services.track_scoring import run_track_scoring

        await run_sessionizer()
        await run_track_scoring()

        async with _TestSession() as session:
            from sqlalchemy import select

            result = await session.execute(select(TrackInteraction).where(TrackInteraction.user_id == "metal_fan"))
            interactions = {i.track_id: i for i in result.scalars().all()}

        # metal_fan's liked tracks should have higher satisfaction than skipped.
        liked_scores = [interactions[f"t{i}"].satisfaction_score for i in range(10) if f"t{i}" in interactions]
        skipped_scores = [interactions[f"t{i}"].satisfaction_score for i in range(10, 20) if f"t{i}" in interactions]

        if liked_scores and skipped_scores:
            avg_liked = sum(liked_scores) / len(liked_scores)
            avg_skipped = sum(skipped_scores) / len(skipped_scores)
            assert avg_liked > avg_skipped, (
                f"Liked tracks ({avg_liked:.3f}) should score higher than skipped tracks ({avg_skipped:.3f})"
            )
