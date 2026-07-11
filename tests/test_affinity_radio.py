"""
GrooveIQ — Tests for affinity (pure-similarity) radio sessions.

Affinity radio is the deliberately-pure counterpart to /radio: a seed-pinned FAISS
nearest-neighbour stream with no drift, ranker, or reranking. These tests pin the
contract that matters:

  - the exclusion set is built correctly (served + seed tracks + disliked, and — when
    unheard_only — already-played tracks) and handed to FAISS;
  - unheard_only=False keeps heard tracks but still drops disliked;
  - served state accumulates across /next calls so the stream never repeats;
  - the route resolves media_server_id seeds, 404s unknown seeds, and reports the
    `exhausted` flag when the neighbourhood runs dry.

FAISS is stubbed throughout so no real index is needed.
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
from app.models.db import Base, TrackFeatures, TrackInteraction, User

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

    # Reset the affinity session store between tests so state doesn't bleed.
    import app.services.affinity_radio as affinity_service

    affinity_service._sessions.clear()

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


async def _seed_user(user_id: str = "testuser") -> None:
    now = int(time.time())
    async with _TestSession() as session:
        session.add(User(user_id=user_id, display_name="Test User", profile_updated_at=now))
        await session.commit()


async def _seed_track(
    *,
    internal_id: str,
    media_server_id: str | None = None,
    title: str = "Song",
    artist: str = "Artist",
    emb_seed: int = 1,
) -> None:
    now = int(time.time())
    # Library tracks are playable by default: give every seeded track a
    # media_server_id unless a test overrides it. Neighbours returned by FAISS
    # must be playable, otherwise the null-msid eligibility gate drops them.
    if media_server_id is None:
        media_server_id = f"ms-{internal_id}"
    async with _TestSession() as session:
        session.add(
            TrackFeatures(
                track_id=internal_id,
                media_server_id=media_server_id,
                file_path=f"/music/{internal_id}.mp3",
                title=title,
                artist=artist,
                duration=200.0,
                bpm=120.0,
                energy=0.6,
                embedding=_make_embedding(emb_seed),
                analyzed_at=now,
                analysis_version="1",
            )
        )
        await session.commit()


async def _seed_interaction(
    *,
    user_id: str,
    track_id: str,
    dislike_count: int = 0,
    play_count: int = 0,
    last_played_at: int | None = None,
) -> None:
    async with _TestSession() as session:
        session.add(
            TrackInteraction(
                user_id=user_id,
                track_id=track_id,
                dislike_count=dislike_count,
                play_count=play_count,
                last_played_at=last_played_at,
                updated_at=int(time.time()),
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Service: exclusion set construction
# ---------------------------------------------------------------------------


class TestAffinityExclusion:
    async def test_excludes_seed_disliked_and_played_when_unheard_only(self, monkeypatch):
        """unheard_only=True → FAISS must be asked to exclude the seed track,
        the user's disliked tracks, AND everything already played."""
        import app.services.affinity_radio as affinity_service

        await _seed_user("u1")
        await _seed_track(internal_id="seed", emb_seed=1)
        await _seed_track(internal_id="played", emb_seed=2)
        await _seed_track(internal_id="disliked", emb_seed=3)
        await _seed_track(internal_id="fresh", emb_seed=4)

        now = int(time.time())
        await _seed_interaction(user_id="u1", track_id="played", play_count=3, last_played_at=now)
        await _seed_interaction(user_id="u1", track_id="disliked", dislike_count=1)

        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: np.ones(64, dtype=np.float32))

        captured: dict = {}

        def _fake_search(emb, k=50, exclude_ids=None):
            captured["exclude"] = set(exclude_ids or set())
            captured["k"] = k
            return [("fresh", 0.91)]

        monkeypatch.setattr("app.services.faiss_index.search", _fake_search)

        async with _TestSession() as db:
            session = await affinity_service.create_affinity_session(
                user_id="u1", seed_type="track", seed_value="seed", db=db, unheard_only=True
            )
            tracks = await affinity_service.get_next_tracks(session.session_id, 5, db)

        assert captured["k"] == 5
        assert {"seed", "played", "disliked"} <= captured["exclude"]
        assert tracks is not None and len(tracks) == 1
        assert tracks[0]["track_id"] == "fresh"
        assert tracks[0]["similarity"] == 0.91
        # Served accumulates for the next round.
        assert "fresh" in session.served_set
        assert session.total_served == 1

    async def test_unheard_only_false_keeps_played_but_drops_disliked(self, monkeypatch):
        """unheard_only=False → played tracks are eligible again; disliked are
        always excluded."""
        import app.services.affinity_radio as affinity_service

        await _seed_user("u1")
        await _seed_track(internal_id="seed", emb_seed=1)
        await _seed_track(internal_id="played", emb_seed=2)
        await _seed_track(internal_id="disliked", emb_seed=3)

        now = int(time.time())
        await _seed_interaction(user_id="u1", track_id="played", play_count=3, last_played_at=now)
        await _seed_interaction(user_id="u1", track_id="disliked", dislike_count=1)

        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: np.ones(64, dtype=np.float32))

        captured: dict = {}

        def _fake_search(emb, k=50, exclude_ids=None):
            captured["exclude"] = set(exclude_ids or set())
            return [("played", 0.8)]

        monkeypatch.setattr("app.services.faiss_index.search", _fake_search)

        async with _TestSession() as db:
            session = await affinity_service.create_affinity_session(
                user_id="u1", seed_type="track", seed_value="seed", db=db, unheard_only=False
            )
            await affinity_service.get_next_tracks(session.session_id, 5, db)

        assert "disliked" in captured["exclude"]
        assert "played" not in captured["exclude"]  # heard-but-not-disliked is eligible

    async def test_served_accumulates_across_next_calls(self, monkeypatch):
        """Successive /next calls must widen the exclusion set with everything
        already served so the stream never repeats."""
        import app.services.affinity_radio as affinity_service

        await _seed_user("u1")
        await _seed_track(internal_id="seed", emb_seed=1)
        await _seed_track(internal_id="a", emb_seed=2)
        await _seed_track(internal_id="b", emb_seed=3)

        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: np.ones(64, dtype=np.float32))

        calls: list[set] = []
        batches = iter([[("a", 0.9)], [("b", 0.7)]])

        def _fake_search(emb, k=50, exclude_ids=None):
            calls.append(set(exclude_ids or set()))
            return next(batches)

        monkeypatch.setattr("app.services.faiss_index.search", _fake_search)

        async with _TestSession() as db:
            session = await affinity_service.create_affinity_session(
                user_id="u1", seed_type="track", seed_value="seed", db=db, unheard_only=True
            )
            await affinity_service.get_next_tracks(session.session_id, 1, db)
            await affinity_service.get_next_tracks(session.session_id, 1, db)

        # First call excludes only the seed; second call also excludes "a".
        assert "a" not in calls[0]
        assert "a" in calls[1]
        assert session.total_served == 2


# ---------------------------------------------------------------------------
# Route: POST /v1/affinity/start
# ---------------------------------------------------------------------------


class TestAffinityStartRoute:
    async def test_start_resolves_media_server_id_and_reports_exhausted(self, client: AsyncClient, monkeypatch):
        """A media_server_id seed passes validation; when FAISS returns fewer than
        `count`, the response flags `exhausted`."""
        await _seed_user("testuser")
        await _seed_track(internal_id="42", media_server_id="qhiFiRW0x0Ux612N02Xmgu", title="Seed", artist="Band")
        await _seed_track(internal_id="near1", emb_seed=5)
        await _seed_track(internal_id="near2", emb_seed=6)

        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: np.ones(64, dtype=np.float32))
        monkeypatch.setattr(
            "app.services.faiss_index.search",
            lambda emb, k=50, exclude_ids=None: [("near1", 0.95), ("near2", 0.88)],
        )

        resp = await client.post(
            "/v1/affinity/start",
            json={
                "user_id": "testuser",
                "seed_type": "track",
                "seed_value": "qhiFiRW0x0Ux612N02Xmgu",
                "count": 5,
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["seed_value"] == "qhiFiRW0x0Ux612N02Xmgu"
        assert data["seed_display_name"] == "Band — Seed"
        assert data["unheard_only"] is True
        assert data["exhausted"] is True  # 2 returned < 5 requested
        assert [t["track_id"] for t in data["tracks"]] == ["near1", "near2"]
        assert data["tracks"][0]["similarity"] == 0.95

    async def test_start_unknown_seed_404s(self, client: AsyncClient):
        await _seed_user("testuser")
        await _seed_track(internal_id="42", media_server_id="qhiFiRW0x0Ux612N02Xmgu")

        resp = await client.post(
            "/v1/affinity/start",
            json={
                "user_id": "testuser",
                "seed_type": "track",
                "seed_value": "does-not-exist",
                "count": 5,
            },
        )
        assert resp.status_code == 404
        assert "Seed track not found" in resp.json()["detail"]

    async def test_start_no_similar_tracks_422s(self, client: AsyncClient, monkeypatch):
        """A seed with an embedding but an empty neighbourhood is a 422, and the
        stray session is cleaned up."""
        import app.services.affinity_radio as affinity_service

        await _seed_user("testuser")
        await _seed_track(internal_id="42", media_server_id="qhiFiRW0x0Ux612N02Xmgu")

        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: np.ones(64, dtype=np.float32))
        monkeypatch.setattr("app.services.faiss_index.search", lambda emb, k=50, exclude_ids=None: [])

        resp = await client.post(
            "/v1/affinity/start",
            json={"user_id": "testuser", "seed_type": "track", "seed_value": "42", "count": 5},
        )
        assert resp.status_code == 422
        assert affinity_service.list_sessions() == []


# ---------------------------------------------------------------------------
# Route: GET /v1/affinity/{id}/next  &  session lifecycle
# ---------------------------------------------------------------------------


class TestAffinityNextRoute:
    async def test_next_missing_session_404s(self, client: AsyncClient):
        await _seed_user("testuser")
        resp = await client.get("/v1/affinity/nope/next")
        assert resp.status_code == 404

    async def test_start_then_next_then_stop(self, client: AsyncClient, monkeypatch):
        await _seed_user("testuser")
        await _seed_track(internal_id="42", media_server_id="msid-42")
        await _seed_track(internal_id="n1", emb_seed=7)
        await _seed_track(internal_id="n2", emb_seed=8)

        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: np.ones(64, dtype=np.float32))
        batches = iter([[("n1", 0.9)], [("n2", 0.8)]])
        monkeypatch.setattr(
            "app.services.faiss_index.search",
            lambda emb, k=50, exclude_ids=None: next(batches),
        )

        start = await client.post(
            "/v1/affinity/start",
            json={"user_id": "testuser", "seed_type": "track", "seed_value": "42", "count": 1},
        )
        assert start.status_code == 201, start.text
        sid = start.json()["session_id"]

        nxt = await client.get(f"/v1/affinity/{sid}/next", params={"count": 1})
        assert nxt.status_code == 200, nxt.text
        body = nxt.json()
        assert body["total_served"] == 2  # 1 from start + 1 from next
        assert body["tracks"][0]["track_id"] == "n2"

        stop = await client.delete(f"/v1/affinity/{sid}")
        assert stop.status_code == 200
        # Session is gone.
        after = await client.get(f"/v1/affinity/{sid}/next")
        assert after.status_code == 404
