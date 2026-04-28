"""
GrooveIQ — Tests for radio session seed resolution and track ID mapping.

Covers the iOS / Ampster bug where a Navidrome 22-char base62 ID is sent
as ``seed_value``: validation must accept it, the service must resolve it
to the internal track_id before passing to FAISS, and the response must
surface the canonical ``track_id`` (which after library/sync IS the
current Navidrome id — the sync renames track_id to the server id).
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
from app.models.db import Base, TrackFeatures, User

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _make_embedding(seed: int = 0) -> str:
    rng = np.random.RandomState(seed)
    vec = rng.randn(64).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return base64.b64encode(vec.tobytes()).decode()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.dependency_overrides[get_session] = override_get_session

    # Reset radio sessions store between tests so they don't bleed state.
    import app.services.radio as radio_service

    radio_service._sessions.clear()

    yield

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


async def _seed_user_and_track(*, internal_id: str = "12345", external_id: str = "qhiFiRW0x0Ux612N02Xmgu") -> None:
    """Create a user plus one track with both internal and external IDs."""
    now = int(time.time())
    async with _TestSession() as session:
        session.add(User(user_id="testuser", display_name="Test User", profile_updated_at=now))
        session.add(
            TrackFeatures(
                track_id=internal_id,
                external_track_id=external_id,
                file_path=f"/music/{internal_id}.mp3",
                title="Test Song",
                artist="Test Artist",
                duration=240.0,
                bpm=120.0,
                energy=0.7,
                embedding=_make_embedding(1),
                analyzed_at=now,
                analysis_version="1",
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Service-level: create_radio_session resolves external_track_id → internal
# ---------------------------------------------------------------------------


class TestRadioSessionSeedResolution:
    async def test_track_seed_with_external_id_resolves_to_internal(self, monkeypatch):
        """When seed_value is an external_track_id, seed_track_ids must hold the
        internal track_id (FAISS keys), and faiss_index.get_embedding must be
        called with the internal id."""
        from app.services import radio as radio_service

        await _seed_user_and_track(internal_id="42", external_id="qhiFiRW0x0Ux612N02Xmgu")

        # Stub FAISS so the service doesn't need a built index.
        seen_args: list[str] = []

        def _fake_get_embedding(tid: str):
            seen_args.append(tid)
            return np.ones(64, dtype=np.float32)

        monkeypatch.setattr("app.services.faiss_index.get_embedding", _fake_get_embedding)

        async with _TestSession() as db:
            session = await radio_service.create_radio_session(
                user_id="testuser",
                seed_type="track",
                seed_value="qhiFiRW0x0Ux612N02Xmgu",  # external id
                db=db,
            )

        # FAISS must have been queried with the internal id, not the external one.
        assert seen_args == ["42"]
        assert session.seed_track_ids == ["42"]
        assert session.seed_embedding is not None
        # Display name still resolves from the same row.
        assert session.seed_display_name == "Test Artist — Test Song"

    async def test_track_seed_with_internal_id_still_works(self, monkeypatch):
        """Backwards compatibility: callers that pass an internal track_id
        must continue to function unchanged."""
        from app.services import radio as radio_service

        await _seed_user_and_track(internal_id="42", external_id="qhiFiRW0x0Ux612N02Xmgu")

        seen_args: list[str] = []

        def _fake_get_embedding(tid: str):
            seen_args.append(tid)
            return np.ones(64, dtype=np.float32)

        monkeypatch.setattr("app.services.faiss_index.get_embedding", _fake_get_embedding)

        async with _TestSession() as db:
            session = await radio_service.create_radio_session(
                user_id="testuser",
                seed_type="track",
                seed_value="42",
                db=db,
            )

        assert seen_args == ["42"]
        assert session.seed_track_ids == ["42"]


# ---------------------------------------------------------------------------
# Route-level: POST /v1/radio/start accepts either ID format
# ---------------------------------------------------------------------------


class TestRadioStartSeedValidation:
    async def test_seed_external_id_passes_validation(self, client: AsyncClient, monkeypatch):
        """Sending a Navidrome external_track_id as seed_value must not 404 at
        the validation step."""
        await _seed_user_and_track(internal_id="42", external_id="qhiFiRW0x0Ux612N02Xmgu")

        # Audit writes use the production AsyncSessionLocal; not worth wiring
        # for this test so just disable it.
        monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", False)

        # Stub FAISS so the service can build an embedding.
        monkeypatch.setattr(
            "app.services.faiss_index.get_embedding",
            lambda tid: np.ones(64, dtype=np.float32),
        )
        # Stub get_next_tracks so we don't need a built index for candidate gen.
        # The real service returns the internal `track_id` (which post-sync is
        # the current Navidrome id) rather than the legacy external_track_id.
        async def _fake_get_next_tracks(session_id, count, db, *, collect_audit=False):
            track_data = {
                "position": 0,
                "track_id": "42",  # internal/canonical track_id
                "source": "radio_drift",
                "score": 1.0,
                "title": "Test Song",
                "artist": "Test Artist",
            }
            if collect_audit:
                return [track_data], {
                    "candidate_rows": [],
                    "candidates_by_source": {},
                    "candidates_total": 0,
                }
            return [track_data]

        monkeypatch.setattr("app.services.radio.get_next_tracks", _fake_get_next_tracks)

        resp = await client.post(
            "/v1/radio/start",
            json={
                "user_id": "testuser",
                "seed_type": "track",
                "seed_value": "qhiFiRW0x0Ux612N02Xmgu",  # external id from iOS
                "count": 1,
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        # Route echoes back the seed_value the caller passed — preserved as-is
        # in the session record so iOS knows what it asked for.
        assert data["seed_value"] == "qhiFiRW0x0Ux612N02Xmgu"
        # The returned track_id is the canonical id (post-sync = Navidrome id).
        assert data["tracks"][0]["track_id"] == "42"

    async def test_seed_unknown_id_still_404s(self, client: AsyncClient):
        """Unknown seed values must still produce a 404 (regression check on
        the or_() validation widening — it must not accidentally pass through
        bogus ids)."""
        await _seed_user_and_track(internal_id="42", external_id="qhiFiRW0x0Ux612N02Xmgu")

        resp = await client.post(
            "/v1/radio/start",
            json={
                "user_id": "testuser",
                "seed_type": "track",
                "seed_value": "completely-unknown-id",
                "count": 1,
            },
        )
        assert resp.status_code == 404
        assert "Seed track not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Response-level: get_next_tracks returns the canonical track_id
# ---------------------------------------------------------------------------


class TestRadioResponseTrackIdMapping:
    """After library/sync runs, ``TrackFeatures.track_id`` IS the current
    media-server id (sync renames it).  ``external_track_id`` holds the
    *previous* id and is generally stale after a media-server upgrade, so
    the radio response must surface the internal/canonical ``tid`` and
    must NOT fall back to ``external_track_id``."""

    @staticmethod
    def _stub_pipeline(monkeypatch, candidate_id: str) -> None:
        """Stub FAISS / ranker / reranker so a single candidate flows through."""
        monkeypatch.setattr("app.services.faiss_index.is_ready", lambda: True)
        monkeypatch.setattr(
            "app.services.faiss_index.search",
            lambda emb, k=50, exclude_ids=None: [(candidate_id, 0.99)],
        )
        monkeypatch.setattr(
            "app.services.faiss_index.search_by_track_id",
            lambda tid, k=50, exclude_ids=None: [],
        )
        monkeypatch.setattr("app.services.session_embeddings.is_ready", lambda: False)
        monkeypatch.setattr("app.services.lastfm_candidates.is_ready", lambda: False)
        monkeypatch.setattr("app.services.collab_filter.is_ready", lambda: False)

        async def _fake_score_candidates(user_id, candidate_ids, db, **_):
            return [(tid, 1.0) for tid in candidate_ids]

        async def _fake_rerank(scored, user_id, db, **_):
            return scored

        monkeypatch.setattr("app.services.ranker.score_candidates", _fake_score_candidates)
        monkeypatch.setattr("app.services.reranker.rerank", _fake_rerank)
        monkeypatch.setattr("app.services.reranker.get_last_rerank_actions", lambda: [])

    async def test_response_returns_internal_track_id_post_sync(self, monkeypatch):
        """Post-sync, track_id IS the current Navidrome id.  Even though the
        track also has a (stale) external_track_id from a previous sync,
        the response must use track_id so the client hits Navidrome with
        the *current* id, not the stale one."""
        from app.services import radio as radio_service

        # Mimics a track that the new sync just renamed: track_id is the
        # 22-char Navidrome id, external_track_id is the previous (stale)
        # 16-hex id from an older sync.
        await _seed_user_and_track(
            internal_id="iDkrnC4UrRVJ6HHEa83nc9",  # current Navidrome id
            external_id="85637f2c0b1968aa",  # legacy 16-hex
        )

        s = radio_service.RadioSession(
            session_id="sess-1",
            user_id="testuser",
            seed_type="track",
            seed_value="iDkrnC4UrRVJ6HHEa83nc9",
            seed_track_ids=["iDkrnC4UrRVJ6HHEa83nc9"],
            seed_embedding=np.ones(64, dtype=np.float32),
            drift_embedding=np.ones(64, dtype=np.float32),
        )
        radio_service.store_session(s)
        self._stub_pipeline(monkeypatch, "iDkrnC4UrRVJ6HHEa83nc9")

        async with _TestSession() as db:
            tracks = await radio_service.get_next_tracks("sess-1", 1, db)

        assert tracks is not None and len(tracks) == 1
        # MUST be the current id (track_id), NOT the stale external_track_id.
        assert tracks[0]["track_id"] == "iDkrnC4UrRVJ6HHEa83nc9"
        # Internal bookkeeping still uses the internal id.
        assert "iDkrnC4UrRVJ6HHEa83nc9" in s.played_set

    async def test_response_uses_internal_when_no_external(self, monkeypatch):
        """Tracks that the sync hasn't matched yet (no external_track_id)
        still surface the internal id — that's all we have, and the client
        will simply 404 on it (no different from any other unmatched track)."""
        from app.services import radio as radio_service

        now = int(time.time())
        async with _TestSession() as db:
            db.add(User(user_id="testuser", display_name="Test User", profile_updated_at=now))
            db.add(
                TrackFeatures(
                    track_id="legacy-1",
                    external_track_id=None,
                    file_path="/music/legacy-1.mp3",
                    title="Legacy",
                    artist="Old",
                    embedding=_make_embedding(1),
                    analyzed_at=now,
                    analysis_version="1",
                )
            )
            await db.commit()

        s = radio_service.RadioSession(
            session_id="sess-2",
            user_id="testuser",
            seed_type="track",
            seed_value="legacy-1",
            seed_track_ids=["legacy-1"],
            seed_embedding=np.ones(64, dtype=np.float32),
            drift_embedding=np.ones(64, dtype=np.float32),
        )
        radio_service.store_session(s)
        self._stub_pipeline(monkeypatch, "legacy-1")

        async with _TestSession() as db:
            tracks = await radio_service.get_next_tracks("sess-2", 1, db)

        assert tracks is not None and len(tracks) == 1
        assert tracks[0]["track_id"] == "legacy-1"
